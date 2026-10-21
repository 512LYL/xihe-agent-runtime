# -*- coding: utf-8 -*-
"""总控制台 HTTP 服务（单进程 / 单端口）

一个端口同时提供：
    /                     状态看板（静态 HTML）
    /api/status           看板数据（服务健康 + 打印队列 + 暂存 + 路由最近结果）
    /api/services         服务清单与判活
    /api/svc/start|stop|restart   单个服务启停（POST）
    /api/start|stop       全部启停（POST，看板的「一键启停」）
    /api/route/test       下发一条文本测试指令，走四级路由骨架（POST）
    /api/route/status     四级清单 + 最近一次路由结果
    /api/print/test       造一条文本测试页并投进打印队列（POST）
    /api/print/submit     提交指定文件打印（POST，入队由守护执行）
    /api/print/clear      清空队列（POST，先写取消信号）
    /api/print/retry      重投 `_失败`/`_队列`/`_模拟` 里的文件（POST）
    /api/repo/*           打印仓库：列表 / 上传 / 删除 / 一键打印
    /api/preflight        打印前置自检（一行中文结论）
    /api/log?id=&n=       看某个服务的日志尾巴
    /api/config           配置摘要（api_key 已打码、路径已相对化）
    /api/shutdown         关服务并退出（POST）

设计要点：
  · 只绑 config.console.host（默认 127.0.0.1 回环），**不含任何鉴权凭据**；
  · 客户端提前断开时静默忽略，不刷 traceback（生产版为此写过 17 万条、日志涨到 219 MB）；
  · 所有响应 no-store，避免看板看到旧状态。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from .. import config as _config
from .. import logset
from .. import router
from ..printing import preflight as _preflight
from .dispatch import Dispatcher
from .services import ServiceManager

_STARTED = time.time()
_STATE = {"cfg": None, "log": None, "svc": None, "disp": None, "router_log": None}
_PF = {"t": 0.0, "v": None, "lock": threading.Lock()}


def _web_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------------------------------------------------------- 组装数据
def _status_payload():
    cfg = _STATE["cfg"]
    disp, svc = _STATE["disp"], _STATE["svc"]
    st = disp.status()
    try:
        services = svc.status()
    except Exception as e:
        services = [{"id": "?", "name": "服务状态读取失败", "error": str(e)}]
    healthy = sum(1 for s in services if s.get("ok"))
    return {
        "ok": True,
        "app": {"name": "xihe-lite", "version": __version__,
                "uptime": int(time.time() - _STARTED), "ts": int(time.time()),
                "port": int((cfg.get("console") or {}).get("port") or 0),
                "host": (cfg.get("console") or {}).get("host") or "127.0.0.1",
                "python": "%d.%d.%d" % tuple(sys.version_info[:3])},
        "health": {"healthy": healthy, "total": len(services),
                   "ok": bool(services) and healthy == len(services)},
        "services": services,
        "print": {
            "backend": st.get("backend") or {},
            "queue": st.get("queue") or {},
            "ews": st.get("ews") or {},
            "printer": st.get("printer") or "",
            "staging": st.get("staging") or {},
            "ready": st.get("ready") or [],
            "last": st.get("last") or {},
            "daemon": st.get("daemon") or {},
        },
        "route": router.status(cfg),
    }


def _preflight_cached(force=False, ttl=20.0):
    """前置自检带 20 秒缓存：真机 EWS 可能超时 8 秒，不能每次点都等满。"""
    with _PF["lock"]:
        if not force and _PF["v"] is not None and time.time() - _PF["t"] < ttl:
            v = dict(_PF["v"])
            v["cached"] = True
            return v
        data = _preflight.run(_STATE["cfg"])
        _PF.update(t=time.time(), v=data)
        v = dict(data)
        v["cached"] = False
        return v


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "xihe-lite/%s" % __version__
    protocol_version = "HTTP/1.0"     # 每请求一条连接：看板轮询够用，也不会留 keep-alive 残连

    # ---------------------------------------------------------------- 工具
    def _safe_write(self, body):
        try:
            self.wfile.write(body)
            return True
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return False

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8", cache=False):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False,
                                                             default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if cache else "no-store")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return
        self._safe_write(body)

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            if p in ("/", "/index.html"):
                fp = os.path.join(_web_dir(), "index.html")
                if os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        return self._send(f.read(), ctype="text/html; charset=utf-8",
                                          cache=True)
                return self._send("<h1>xihe-lite</h1><p>缺 web/index.html</p>".encode("utf-8"),
                                  500, "text/html; charset=utf-8")
            if p == "/favicon.ico":
                return self._send(b"", 204, "image/x-icon")
            if p == "/api/health":
                return self._send({"ok": True, "app": "xihe-lite",
                                   "version": __version__,
                                   "uptime": int(time.time() - _STARTED)})
            if p == "/api/status":
                return self._send(_status_payload())
            if p == "/api/services":
                return self._send({"ok": True, "items": _STATE["svc"].status()})
            if p == "/api/config":
                return self._send({"ok": True, "config": _config.safe_view(_STATE["cfg"])})
            if p == "/api/repo":
                return self._send(_STATE["disp"].repo_list())
            if p == "/api/route/status":
                return self._send(router.status(_STATE["cfg"]))
            if p == "/api/preflight":
                force = (q.get("force") or ["0"])[0] not in ("0", "", "false")
                return self._send(_preflight_cached(force=force))
            if p == "/api/log":
                sid = (q.get("id") or [""])[0]
                n = int((q.get("n") or ["120"])[0])
                return self._send({"ok": True, "id": sid,
                                   "file": os.path.basename(_STATE["svc"].logpath(sid) or ""),
                                   "lines": _STATE["svc"].tail(sid, n)})
            return self._send({"ok": False, "msg": "no such api: %s" % p}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            try:
                self._send({"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        try:
            u = urlparse(self.path)
            p = u.path
            body = {}
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                if ln:
                    body = json.loads(self.rfile.read(ln).decode("utf-8")) or {}
            except Exception:
                body = {}

            svc, disp, log = _STATE["svc"], _STATE["disp"], _STATE["log"]
            cfg = _STATE["cfg"]

            if p in ("/api/svc/start", "/api/svc/stop", "/api/svc/restart"):
                sid = str(body.get("id") or "")
                r = {"start": svc.start, "stop": svc.stop,
                     "restart": svc.restart}[p.rsplit("/", 1)[1]](sid)
                r["id"] = sid
                return self._send(r)
            if p == "/api/start":
                return self._send({"ok": True, "result": svc.start_all()})
            if p == "/api/stop":
                return self._send({"ok": True, "result": svc.stop_all()})
            if p == "/api/toggle":
                # 一键启停：在跑的停掉、没跑的全起
                cur = svc.status(ttl=0)
                if any(x.get("running") for x in cur):
                    return self._send({"ok": True, "action": "stop",
                                       "result": svc.stop_all()})
                return self._send({"ok": True, "action": "start",
                                  "result": svc.start_all()})
            if p == "/api/route/test":
                text = str(body.get("text") or "").strip() or "ping"
                res = router.route(text, cfg, log=_STATE["router_log"])
                if log:
                    log.info("测试指令下发：%d 字符 → %s（%d ms）",
                             len(text), res.get("tier_name") or "未命中", res["elapsed_ms"])
                return self._send(res)
            if p == "/api/print/test":
                text = str(body.get("text") or "").strip()
                return self._send(disp.test_page(text))
            if p == "/api/print/submit":
                return self._send(disp.submit(path=body.get("path"),
                                              name=body.get("name"),
                                              sided=body.get("sided")))
            if p == "/api/print/clear":
                return self._send(disp.clear())
            if p == "/api/print/retry":
                return self._send(disp.retry(body.get("name") or body.get("file") or ""))
            if p == "/api/repo/print":
                return self._send(disp.repo_print(body.get("names") or []))
            if p == "/api/repo/delete":
                return self._send(disp.repo_delete(body.get("names") or []))
            if p == "/api/repo/upload":
                return self._send(disp.repo_upload(body.get("name") or "",
                                                   body.get("b64") or ""))
            if p == "/api/demo":
                r = disp.demo_files()
                if log:
                    log.info("生成演示样本：%s", r.get("created"))
                return self._send(r)
            if p == "/api/shutdown":
                self._send({"ok": True, "msg": "正在停止服务并退出…"})
                threading.Timer(0.3, _shutdown).start()
                return
            return self._send({"ok": False, "msg": "no such api: %s" % p}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            try:
                self._send({"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass


def _shutdown():
    try:
        _STATE["svc"].stop_all()
    except Exception:
        pass
    try:
        _STATE["disp"].stop_refresher()
    except Exception:
        pass
    os._exit(0)


class QuietServer(ThreadingHTTPServer):
    """客户端提前断开时不刷 traceback。

    生产版的日志曾因为客户端 RST 断开而涨到 219 MB（17 万个 traceback）；这类异常发生在
    `handle_one_request` 读请求行时，业务的 try/except 包不到，必须在服务器层兜住。
    """
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


# ---------------------------------------------------------------- 启动
def build(cfg, log=None):
    _STATE["cfg"] = cfg
    _STATE["log"] = log or logset.get_logger(cfg, "console", echo=True)
    _STATE["svc"] = ServiceManager(cfg, _STATE["log"])
    _STATE["disp"] = Dispatcher(cfg, _STATE["log"])
    _STATE["router_log"] = logset.get_logger(cfg, "router", echo=True, tag="ROUTE")
    return _STATE


def main(cfg=None, open_browser=False):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = cfg or _config.load()
    _config.ensure_dirs(cfg)
    log = logset.get_logger(cfg, "console", echo=True)
    st = build(cfg, log)
    disp = st["disp"]
    disp.start_refresher()

    host = (cfg.get("console") or {}).get("host") or "127.0.0.1"
    port = int((cfg.get("console") or {}).get("port") or 18910)

    # 单例守卫：端口被占就报清楚，而不是静默起两个
    from .services import port_open, write_pidfile
    if port_open(port, host):
        log.error("端口 %d 已被占用：控制台可能已在运行。先 stop.bat 或换 console.port", port)
        print("端口 %d 已被占用：控制台可能已在运行。" % port)
        return 2

    srv = QuietServer((host, port), Handler)
    srv.daemon_threads = True
    pidp = os.path.join(_config.path(cfg, "paths", "run"), "console.pid")
    write_pidfile(pidp)

    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    log.info("===== xihe-lite 控制台启动 pid=%d =====", os.getpid())
    log.info("状态看板：%s（%s）", url, disp.backend_line)
    log.info("监听 %s（仅本机回环，不含鉴权凭据）", host)
    print("")
    print("  曦和 · 精简可运行版（xihe-lite）控制台已启动")
    print("  地址：%s" % url)
    print("  %s" % disp.backend_line)
    print("  按 Ctrl+C 停止（或用 stop.bat）")
    print("", flush=True)

    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("控制台被用户停止")
    finally:
        try:
            disp.stop_refresher()
        except Exception:
            pass
        try:
            os.remove(pidp)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
