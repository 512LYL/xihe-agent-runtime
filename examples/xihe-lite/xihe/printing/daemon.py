# -*- coding: utf-8 -*-
"""打印守护（独立进程）

职责：盯暂存区 ready 队列，把文件**串行**投给打印后端，判定出纸结果并归档。
它不做任何界面，只暴露一个只读 HTTP `/status` 供控制台/自检探活。

为什么单独一个进程：打印可能要等几分钟（有人在复印、纸盒空了），
把它挡在控制台进程里会把状态看板一起拖死。生产版也是这个结构。

用法：
    python -m xihe.printing.daemon
    python -m xihe.printing.daemon --port 18911 --once    # 只跑一轮（调试）
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import config as _config
from .. import logset
from . import states
from .driver import PrintDriver
from .staging import Staging

_DRIVER = {"d": None}
_STARTED = time.time()


def pidfile(cfg):
    return os.path.join(_config.path(cfg, "paths", "run"), "printd.pid")


def write_pidfile(cfg):
    p = pidfile(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return p
    except Exception:
        return ""


def _snapshot():
    d = _DRIVER.get("d")
    if not d:
        return {}
    try:
        snap = d.backend.snapshot()
    except Exception as e:
        snap = {"backend": "?", "error": "%s: %s" % (type(e).__name__, e)}
    try:
        counts = d.staging.counts()
    except Exception:
        counts = {}
    snap["staging"] = counts
    snap["last"] = d.staging.read_last()
    return snap


def status_payload(cfg):
    snap = _snapshot()
    return {
        "ok": True,
        "service": "printd",
        "pid": os.getpid(),
        "uptime": int(time.time() - _STARTED),
        "ts": int(time.time()),
        "backend": snap.get("backend", "?"),
        "simulated": bool(snap.get("simulated")),
        "printer": snap.get("printer") or "",
        "queue": snap.get("queue") or {},
        "ews": snap.get("ews") or {},
        "staging": snap.get("staging") or {},
        "last": snap.get("last") or {},
        "config": {
            "mock": bool(snap.get("simulated")),
            "staging": os.path.relpath(_config.path(cfg, "paths", "staging"),
                                       _config.ROOT),
        },
    }


class _H(BaseHTTPRequestHandler):
    cfg = None

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # 客户端提前断开（生产版曾因此刷了 17 万个 traceback，把日志涨到 219MB）
            pass

    def do_GET(self):
        try:
            if self.path.startswith("/status") or self.path.startswith("/health"):
                try:
                    self._send(status_payload(self.cfg))
                except Exception as e:
                    self._send({"ok": False, "err": "%s: %s" % (type(e).__name__, e)}, 500)
            else:
                self._send({"err": "not found"}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def log_message(self, *a):
        pass


class _Server(ThreadingHTTPServer):
    """客户端提前断开（RST）时不刷 traceback —— 生产版曾因此把日志涨到 219 MB。"""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def _port_busy(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.8)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="xihe-lite 打印守护")
    ap.add_argument("--config", default=None)
    ap.add_argument("--port", type=int, default=None, help="覆盖 /status 端口")
    ap.add_argument("--once", action="store_true", help="只处理一轮就退出（调试）")
    a = ap.parse_args(argv)

    cfg = _config.load(a.config)
    _config.ensure_dirs(cfg)
    log = logset.get_logger(cfg, "print_daemon", echo=True)
    port = a.port or int((cfg.get("daemon") or {}).get("port") or 18911)

    # 单例守卫：ThreadingHTTPServer 默认 allow_reuse_address=1，Windows 上第二个实例
    # 也能绑上同一端口 → 静默共生成僵尸进程。先探活。
    if _port_busy(port):
        log.error("端口 %d 已被占用：已有打印守护在运行，本次启动中止（防多实例残留）", port)
        return 2

    staging = Staging(cfg)
    staging.ensure()
    moved = staging.recover()
    if moved:
        log.info("崩溃恢复：%d 个文件从 _printing 回迁 ready：%s", len(moved), moved[:5])

    driver = PrintDriver(cfg, staging=staging, log=log)
    _DRIVER["d"] = driver
    log.info("===== xihe-lite 打印守护启动 pid=%d =====", os.getpid())
    log.info("%s", "打印后端：%s%s" % (driver.backend.name(),
                                      "（模拟模式，不接触任何设备）"
                                      if driver.backend.simulated else ""))
    log.info("暂存区：%s", _config.path(cfg, "paths", "staging"))
    write_pidfile(cfg)

    _H.cfg = cfg
    srv = _Server(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("状态接口：http://127.0.0.1:%d/status", port)

    if a.once:
        src = staging.next_ready()
        if src:
            log.info("发现待打印文件：%s", os.path.basename(src))
            driver.process(src)
        else:
            log.info("ready 队列为空，无任务")
        return 0

    try:
        driver.run_forever()
    except KeyboardInterrupt:
        log.info("打印守护被用户停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
