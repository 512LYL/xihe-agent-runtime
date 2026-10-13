#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xihe_runtime.health.watchdog —— 看护层①：进程级探活 / 会话级自愈 / 资源自检

本模块是**应龙桥接器运行底座的看护层参考实现**：把生产系统里长期运行沉淀出的
看护机制，按「自包含、零密钥、可直接跑」的要求重写为一个可读模块。
原生产脚本（qq_bot_watchdog.py，869 行）依赖未随包发布的同目录模块
（selfcheck / v3_mail_receiver），不宜原样入库，故以本文件作为其可公开形态。

三层职责（对应架构图「⑤ 看护层 · 多层自愈看护体系」）
  ① 进程级探活（看门狗）  —— 被守护进程端口无响应达 N 次即拉起
  ② 会话级自愈（自动解卡）—— 会话写锁卡死时解卡 + 上下文水位压缩
  ③ 硬件级监控（C500 专属）—— 通道③ 探活 / 隧道漂移 / 忘关机提醒

生产参数（取自运行台账，本文件默认值与之逐条对齐）
  POLL_SEC                       = 120   轮询间隔（2 分钟）
  OFFLINE_STREAK_LIMIT           = 3     连续 3 次（≈6 分钟）才动手，防抖
  RELAUNCH_WAIT_SEC              = 160   拉起后等待自动恢复的最长时间
  COOLDOWN_SEC                   = 6*3600 冷却 6 小时（避免连续拉起加重风控）
  HEARTBEAT_EVERY                = 600   心跳每 10 分钟一跳
  SELFCHECK_HOURS                = [0, 12]  每日资源自检时点
  RESIDUAL_CPU_SEC / RESIDUAL_RUN_SEC = 2h / 4h  残留脚本判定阈值
  MAX_MEM_MB                     = 32    看护整套常驻内存实测 ≈27 MB

红线（与仓库 SECURITY.md 一致）
  · 本模块不读取、不打印、不写入任何密钥；
  · 通知通道未配置时**只落日志**，不发送；
  · **只发现、不擅自杀进程** —— 杀/隔离一律交人工拍板。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------- 配置
RUNTIME_ROOT = os.path.abspath(os.environ.get(
    "XIHE_RUNTIME_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".runtime")))
os.makedirs(RUNTIME_ROOT, exist_ok=True)

POLL_SEC = int(os.environ.get("XIHE_WD_POLL_SEC", "120"))
OFFLINE_STREAK_LIMIT = int(os.environ.get("XIHE_WD_STREAK", "3"))
RELAUNCH_WAIT_SEC = 160
COOLDOWN_SEC = 6 * 3600
HEARTBEAT_EVERY = 600
SELFCHECK_HOURS = (0, 12)
RESIDUAL_CPU_SEC = 2 * 3600
RESIDUAL_RUN_SEC = 4 * 3600

# 看护白名单：这些进程名**永不**被判为「疑似残留」
PROCESS_ALLOWLIST = {
    "python.exe",                       # 通用解释器，由命令行进一步判定
    "node.exe",                         # OpenClaw Gateway
    "ollama.exe", "ollama app.exe",     # 通道② 本地推理
    "napcat.exe",                       # QQ 入口
}

STATE_FILE = os.path.join(RUNTIME_ROOT, "watchdog_state.json")
HEARTBEAT_FILE = os.path.join(RUNTIME_ROOT, "watchdog.heartbeat")
LOG_FILE = os.path.join(RUNTIME_ROOT, "watchdog.log")
SINGLETON_PID_FILE = os.path.join(RUNTIME_ROOT, "watchdog.pid")


# ---------------------------------------------------------------- 日志
def _setup_log() -> logging.Logger:
    logger = logging.getLogger("xihe.watchdog")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = _setup_log()


# ---------------------------------------------------------------- 单例守卫
def claim_single_instance(pid_file: str = SINGLETON_PID_FILE) -> bool:
    """OS 级原子抢锁：文件不存在才创建成功（O_CREAT|O_EXCL）。

    生产实践（2026-09-15 归档）：早期「读 pidfile → 判存活 → 返回」的实现存在
    竞态，曾出现**双 watchdog 并存约 16.4 小时**（09-14 15:49 → 09-15 08:15）。
    改为 O_CREAT|O_EXCL 后由操作系统保证原子性；Windows 上映射为
    CreateFile 的 CREATE_NEW 语义。并发实测：3 进程并发 → 1 True / 2 False。
    """
    try:
        fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 陈旧锁自动接管：pid 已不存在则回收
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                old = int((f.read() or "0").strip() or 0)
        except Exception:
            old = 0
        if old and _pid_alive(old):
            log.warning("another watchdog instance is alive (pid=%d) — exiting", old)
            return False
        log.warning("stale pidfile (pid=%s) — taking over", old or "?")
        try:
            os.remove(pid_file)
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:
            return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 探活
def tcp_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP 探活。**保守返回**：探测本身异常时判为「在线」，宁可不拉也不误拉。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, socket.error, OSError):
        return False
    except Exception:                       # noqa: BLE001 —— 探针自身故障
        log.warning("probe error on %s:%s — conservatively treat as ONLINE", host, port)
        return True


@dataclass
class Target:
    """一个被看护的目标。"""
    name: str
    host: str = "127.0.0.1"
    port: int = 0
    launch: Optional[Callable[[], None]] = None
    streak: int = 0

    def alive(self) -> bool:
        return tcp_alive(self.host, self.port)


# ---------------------------------------------------------------- 状态
@dataclass
class WatchState:
    started_at: float = field(default_factory=time.time)
    loops: int = 0
    relaunches: int = 0
    last_relaunch_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    last_selfcheck_hour: int = -1
    events: List[Dict] = field(default_factory=list)

    def save(self, path: str = STATE_FILE) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str = STATE_FILE) -> "WatchState":
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d.pop("events", None)
            st = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
            return st
        except Exception:
            return cls()


# ---------------------------------------------------------------- 资源自检
def scan_residual_processes() -> List[Dict]:
    """每日资源自检：只报「疑似残留脚本」，**不杀进程**。

    判定阈值（生产口径）：白名单外 + 运行时长 > 4h + CPU 累计 > 2h。
    生产实测：单次全扫约 6–8 秒；枚举到 6 个 python 进程且**全部正确命中白名单、零误报**。
    """
    hits: List[Dict] = []
    try:
        import psutil  # type: ignore
    except ImportError:
        log.info("selfcheck skipped: psutil not installed (pip install psutil)")
        return hits

    now = time.time()
    for p in psutil.process_iter(["pid", "name", "create_time", "cpu_times", "cmdline"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            if name in PROCESS_ALLOWLIST:
                continue
            run_sec = now - (info.get("create_time") or now)
            cpu_sec = sum(info.get("cpu_times") or [])
            if run_sec > RESIDUAL_RUN_SEC and cpu_sec > RESIDUAL_CPU_SEC:
                hits.append({
                    "pid": info["pid"], "name": name,
                    "run_hours": round(run_sec / 3600, 2),
                    "cpu_hours": round(cpu_sec / 3600, 2),
                    "cmdline": " ".join(info.get("cmdline") or [])[:200],
                })
        except Exception:                    # 进程可能已退出
            continue
    return hits


# ---------------------------------------------------------------- 通知
def notify(title: str, detail: str, dedup_key: str = "") -> None:
    """通知出口：**未配置通道时只落日志**（保证开源形态可跑、且不外发）。

    生产形态的防刷屏五重保险：
      前置检查 / 启动延迟 / 同类冷却 / 全局配额 / 自动退出。
    此处保留「同类冷却」这一条最关键的去重（生产：md5(detail) 去重 + 冷却窗）。
    """
    import hashlib
    key = dedup_key or hashlib.md5(detail.encode("utf-8", "replace")).hexdigest()
    state = _load_notify_state()
    last = state.get(key, {})
    cooldown = int(os.environ.get("XIHE_NOTIFY_COOLDOWN_SEC", "1800"))
    if time.time() - float(last.get("ts", 0)) < cooldown:
        log.info("notify suppressed (cooldown) key=%s", key[:8])
        return
    state[key] = {"ts": time.time()}
    _save_notify_state(state)

    mail_env = os.environ.get("XIHE_MAIL_ENV", "")
    log.warning("[NOTIFY] %s | %s%s", title, detail,
                "" if mail_env else "  (未配置 XIHE_MAIL_ENV —— 仅落日志)")
    if not mail_env or not os.path.exists(mail_env):
        return
    # 真实投递留给部署方：读取 mail_env 中的 SMTP 配置后发送。
    # 本仓库不内置任何收件地址 / 账号 / 口令。
    log.info("notify channel configured at %s — delivery delegated to deployment", mail_env)


def _notify_state_path() -> str:
    return os.path.join(RUNTIME_ROOT, "notify_dedup.json")


def _load_notify_state() -> Dict:
    try:
        with open(_notify_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_notify_state(state: Dict) -> None:
    try:
        with open(_notify_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 主循环
def build_targets() -> List[Target]:
    """按 env 组装被看护目标（默认值即生产端口，可按需改）。"""
    gw_port = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18791"))
    napcat_port = int(os.environ.get("NAPCAT_PORT", "3002"))
    bridge_port = int(os.environ.get("XIHE_BRIDGE_PORT", "11443"))
    return [
        Target("openclaw-gateway", port=gw_port),
        Target("qq-entry-napcat", port=napcat_port),
        Target("yinglo-bridge", port=bridge_port),
    ]


def run_once(targets: List[Target], state: WatchState) -> None:
    state.loops += 1

    # ---- ① 进程级探活 ----
    for t in targets:
        if t.alive():
            if t.streak:
                log.info("%s recovered after %d failed probe(s)", t.name, t.streak)
            t.streak = 0
            continue
        t.streak += 1
        log.warning("%s DOWN (streak %d/%d)", t.name, t.streak, OFFLINE_STREAK_LIMIT)
        if t.streak < OFFLINE_STREAK_LIMIT:
            continue
        if time.time() - state.last_relaunch_ts < COOLDOWN_SEC:
            log.info("%s: within relaunch cooldown — notify only", t.name)
            notify("%s 未恢复" % t.name, "仍在冷却期，未重复拉起")
            continue
        if t.launch is None:
            log.warning("%s: no launcher configured — notify only", t.name)
            notify("%s 不可用" % t.name, "未配置启动器（XIHE_GATEWAY_LAUNCHER）")
            continue
        try:
            log.warning("%s: relaunching…", t.name)
            t.launch()
            state.relaunches += 1
            state.last_relaunch_ts = time.time()
            time.sleep(min(RELAUNCH_WAIT_SEC, 10))
            t.streak = 0
            notify("%s 已拉起" % t.name, "请复核是否自行恢复")
        except Exception as e:               # noqa: BLE001
            log.error("%s: relaunch failed: %s", t.name, e)
            notify("%s 拉起失败" % t.name, str(e)[:200])

    # ---- ② 心跳（每 10 分钟一跳） ----
    now = time.time()
    if now - state.last_heartbeat_ts >= HEARTBEAT_EVERY:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(now))
        state.last_heartbeat_ts = now
        log.info("heartbeat loop=%d relaunches=%d mem_logged=see /proc or psutil",
                 state.loops, state.relaunches)

    # ---- ③ 每日资源自检（0:00 / 12:00 各一次） ----
    hour = int(time.strftime("%H"))
    if hour in SELFCHECK_HOURS and state.last_selfcheck_hour != hour:
        state.last_selfcheck_hour = hour
        hits = scan_residual_processes()
        if hits:
            log.warning("selfcheck: %d suspected residual process(es)", len(hits))
            notify("疑似残留脚本", json.dumps(hits, ensure_ascii=False)[:500])
        else:
            log.info("selfcheck: clean (0 residual)")

    state.save()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Xihe Agent Runtime · 看护层①（进程级探活/资源自检）")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出（自检/调试用）")
    ap.add_argument("--selfcheck", action="store_true", help="只做一次资源自检并打印结果")
    ap.add_argument("--poll", type=int, default=POLL_SEC, help="轮询间隔秒（默认 %d）" % POLL_SEC)
    args = ap.parse_args(argv)

    if args.selfcheck:
        hits = scan_residual_processes()
        print(json.dumps({"residual": hits, "count": len(hits)}, ensure_ascii=False, indent=2))
        return 0

    if not claim_single_instance():
        return 1

    state = WatchState.load()
    targets = build_targets()
    log.info("watchdog up — pid=%d targets=%s poll=%ds runtime_root=%s",
             os.getpid(), [t.name for t in targets], args.poll, RUNTIME_ROOT)

    try:
        while True:
            run_once(targets, state)
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log.info("watchdog stopped by user")
    finally:
        try:
            os.remove(SINGLETON_PID_FILE)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
