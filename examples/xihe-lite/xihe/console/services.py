# -*- coding: utf-8 -*-
"""服务注册表与启停（生产版 `server.py` 里 SERVICES / start_one / kill_service 的最小子集）

精简版只保留一件事：**能不能起来、起来后活没活着、停了能不能干净停掉**。
所以每条服务只有这些字段：

    id / name / port / module / args / probe / log / order / group

判活口径（与生产版一致）：
    · 端口在听   → listen=True
    · 端口在听 + probe 能返回 2xx/3xx/4xx → ok=True
    · pidfile 里的进程还活着 → running=True（用于无端口的常驻脚本）
"""
import copy
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------- 基础探测
def port_open(port, host="127.0.0.1", timeout=0.4):
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_ok(url, timeout=1.2):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 500
    except Exception:
        return False


def pid_alive(pid):
    """进程是否还活着。

    ★ 不能用 os.kill(pid, 0)：Windows 上它会走 TerminateProcess —— 那是真杀。
      这里用 OpenProcess + GetExitCodeProcess（STILL_ACTIVE=259）。
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259        # STILL_ACTIVE
            return True
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def pids_on_port(port):
    """按端口找 PID（只用系统命令，不依赖 psutil）。"""
    out = set()
    if not port or os.name != "nt":
        return out
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                           text=True, timeout=10, creationflags=CREATE_NO_WINDOW,
                           encoding="gbk", errors="ignore")
        needle = ":%d" % int(port)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and \
                    parts[1].endswith(needle) and parts[-2].upper() == "LISTENING":
                try:
                    out.add(int(parts[-1]))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def kill_pid(pid, tree=True):
    if not pid:
        return False
    try:
        if os.name == "nt":
            args = ["taskkill", "/F", "/PID", str(int(pid))]
            if tree:
                args.insert(2, "/T")
            subprocess.run(args, capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
            return not pid_alive(pid)
        os.kill(int(pid), 15)
        return True
    except Exception:
        return False


def read_pidfile(p):
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return int((f.read() or "").strip())
    except Exception:
        return None


def relpath(p, root):
    """相对化路径；跨盘符（如配置把 staging 放到另一个盘）时退回绝对路径。"""
    try:
        return os.path.relpath(p, root)
    except Exception:
        return p


def write_pidfile(p, pid=None):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(pid or os.getpid()))
        return p
    except Exception:
        return ""


# ---------------------------------------------------------------- 注册表
class ServiceManager:
    def __init__(self, cfg, log=None):
        from .. import config as _config
        self.cfg = cfg
        self.log = log
        self._config = _config
        self.entries = self._build()
        self._cache = {"t": 0.0, "v": None}

    def _build(self):
        raw = self.cfg.get("services") or []
        if not raw:
            raw = copy.deepcopy(self._config.DEFAULT_SERVICES)
            dport = int((self.cfg.get("daemon") or {}).get("port") or 18911)
            for s in raw:
                s["port"] = dport
                s["probe"] = "http://127.0.0.1:%d/status" % dport
        logs = self._config.path(self.cfg, "paths", "logs")
        runs = self._config.path(self.cfg, "paths", "run")
        out = []
        for i, s in enumerate(raw):
            e = dict(s)
            e.setdefault("args", [])
            e.setdefault("order", i)
            e["log"] = e.get("log") or os.path.join(logs, "%s.log" % e["id"])
            e["pidfile"] = e.get("pidfile") or os.path.join(runs, "%s.pid" % e["id"])
            out.append(e)
        return sorted(out, key=lambda x: x.get("order", 99))

    def get(self, sid):
        return next((s for s in self.entries if s["id"] == sid), None)

    # ---------------------------------------------------------------- 状态
    def status_one(self, s):
        listen = port_open(s.get("port"))
        pid = read_pidfile(s["pidfile"])
        alive = pid_alive(pid) if pid else False
        probe = http_ok(s.get("probe")) if (listen and s.get("probe")) else None
        return {
            "id": s["id"], "name": s.get("name") or s["id"], "port": s.get("port"),
            "group": s.get("group"), "order": s.get("order"),
            "listen": bool(listen), "pid": pid if alive else None,
            "running": bool(listen or alive),
            "probe": probe,
            "ok": bool(listen and probe) if s.get("probe") else bool(listen or alive),
            "log": relpath(s["log"], self._config.ROOT),
        }

    def status(self, ttl=None):
        ttl = float(ttl if ttl is not None else
                    (self.cfg.get("console") or {}).get("status_ttl", 2.0))
        now = time.time()
        if self._cache["v"] is not None and now - self._cache["t"] < ttl:
            return self._cache["v"]
        v = []
        for s in self.entries:
            try:
                v.append(self.status_one(s))
            except Exception as e:
                v.append({"id": s["id"], "name": s.get("name"), "error": str(e),
                          "running": False, "ok": False})
        self._cache = {"t": now, "v": v}
        return v

    # ---------------------------------------------------------------- 启停
    def _env(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if os.environ.get("XIHE_CONFIG"):
            env["XIHE_CONFIG"] = os.environ["XIHE_CONFIG"]
        return env

    def logpath(self, sid):
        s = self.get(sid)
        return s["log"] if s else ""

    def tail(self, sid, n=120):
        from .. import logset
        return logset.tail(self.logpath(sid), n)

    def start(self, sid, wait=12.0):
        s = self.get(sid)
        if not s:
            return {"ok": False, "msg": "没有这个服务：%s" % sid}
        st = self.status_one(s)
        if st.get("listen"):
            return {"ok": True, "msg": "已在运行（:%s）" % s.get("port"), "pid": st.get("pid")}
        if not s.get("module"):
            return {"ok": False, "msg": "该服务没有可执行模块，需手动启动"}
        try:
            os.makedirs(os.path.dirname(s["log"]), exist_ok=True)
            out = open(s["log"], "ab", buffering=0)
        except Exception as e:
            return {"ok": False, "msg": "打不开日志文件：%s" % e}
        try:
            p = subprocess.Popen(
                [sys.executable, "-m", s["module"]] + list(s.get("args") or []),
                cwd=self._config.ROOT, env=self._env(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, close_fds=True)
        except Exception as e:
            return {"ok": False, "msg": "启动失败：%s: %s" % (type(e).__name__, e)}
        write_pidfile(s["pidfile"], p.pid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if port_open(s.get("port")) or not pid_alive(p.pid):
                break
            time.sleep(0.4)
        ok = port_open(s.get("port")) or pid_alive(p.pid)
        if self.log:
            self.log.info("启动服务 %s（pid=%s）→ %s", sid, p.pid, "ok" if ok else "失败")
        self._cache["v"] = None
        return {"ok": bool(ok), "pid": p.pid,
                "msg": ("已启动（:%s）" % s.get("port")) if ok else
                       ("进程没起来，看日志：%s" % relpath(s["log"], self._config.ROOT)),
                "log": relpath(s["log"], self._config.ROOT)}

    def stop(self, sid):
        s = self.get(sid)
        if not s:
            return {"ok": False, "msg": "没有这个服务：%s" % sid}
        killed = []
        pid = read_pidfile(s["pidfile"])
        if pid and pid_alive(pid):
            if kill_pid(pid):
                killed.append(pid)
        for p in pids_on_port(s.get("port")):
            if p != os.getpid() and kill_pid(p):
                killed.append(p)
        try:
            if os.path.exists(s["pidfile"]):
                os.remove(s["pidfile"])
        except Exception:
            pass
        if self.log:
            self.log.info("停止服务 %s → killed=%s", sid, killed)
        self._cache["v"] = None
        return {"ok": True, "killed": sorted(set(killed)),
                "msg": "已停止" if killed else "本来就没在跑"}

    def restart(self, sid):
        self.stop(sid)
        time.sleep(0.5)
        return self.start(sid)

    def start_all(self):
        return {s["id"]: self.start(s["id"]) for s in self.entries}

    def stop_all(self):
        return {s["id"]: self.stop(s["id"]) for s in reversed(self.entries)}

    # ---------------------------------------------------------------- 汇总
    def summary(self):
        st = self.status()
        return {"total": len(st), "running": sum(1 for x in st if x.get("running")),
                "ok": sum(1 for x in st if x.get("ok")), "services": st}
