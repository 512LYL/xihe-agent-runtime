# -*- coding: utf-8 -*-
"""打印调度（控制台 HTTP 接口背后的业务层）

控制台**不直接打印**，只做三件事：
    1. 把文件放进暂存 ready 队列（真正的打印由独立守护进程串行做）；
    2. 读状态（暂存计数 + 队列 + EWS + 守护最近一次结果）；
    3. 发取消信号（clear），让守护把在途任务判 cancelled。

这样即使守护崩了，控制台也照常响应；守护一起来就把积压的任务按 FIFO 打掉。
状态快照带缓存 + 后台刷新：真机 EWS 单次可能超时 8 秒，绝不能让看板卡住。
"""
import os
import threading
import time

from .. import config as _config
from ..printing import backend as _backend
from ..printing import repo as _repo
from ..printing.staging import Staging


class Dispatcher:
    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log
        self.staging = Staging(cfg)
        self.staging.ensure()
        self.backend, self.backend_line = _backend.make_backend(cfg, log)
        self._cache = {"t": 0.0, "v": None}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- 状态
    def _snapshot(self):
        snap = {}
        try:
            snap = self.backend.snapshot()
        except Exception as e:
            snap = {"backend": "?", "error": "%s: %s" % (type(e).__name__, e),
                    "queue": {}, "ews": {}}
        try:
            counts = self.staging.counts()
        except Exception:
            counts = {}
        dport = int((self.cfg.get("daemon") or {}).get("port") or 18911)
        daemon = {"port": dport, "running": _backend.tcp_ok("127.0.0.1", dport, 0.4),
                  "status": None}
        if daemon["running"]:
            try:
                import json
                import urllib.request
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/status" % dport, timeout=1.5) as r:
                    daemon["status"] = json.loads(r.read().decode("utf-8", "ignore"))
                    daemon["running"] = True
            except Exception:
                daemon["running"] = False
        return {
            "ok": True, "ts": int(time.time()),
            "backend": {"name": self.backend.name(),
                        "simulated": bool(getattr(self.backend, "simulated", False)),
                        "line": self.backend_line},
            "queue": snap.get("queue") or {},
            "ews": snap.get("ews") or {},
            "printer": snap.get("printer") or "",
            "staging": counts,
            "ready": [os.path.basename(p) for p in self.staging.ready_files()][:20],
            "last": self.staging.read_last(),
            "daemon": daemon,
        }

    def refresh_once(self):
        v = self._snapshot()
        with self._lock:
            self._cache = {"t": time.time(), "v": v}
        return v

    def status(self, ttl=None, max_age=None):
        """读缓存；缓存过期就地刷新一次（服务端启动时会挂后台刷新线程）。"""
        ttl = float(ttl if ttl is not None else
                    (self.cfg.get("console") or {}).get("status_ttl", 2.0))
        with self._lock:
            t, v = self._cache["t"], self._cache["v"]
        if v is not None and (time.time() - t) < max(ttl, 0.5):
            return v
        return self.refresh_once()

    def start_refresher(self, interval=3.0):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.is_set():
                try:
                    self.refresh_once()
                except Exception:
                    pass
                self._stop.wait(interval)
        self._thread = threading.Thread(target=_loop, daemon=True,
                                        name="xihe-status-refresher")
        self._thread.start()

    def stop_refresher(self):
        self._stop.set()

    # ---------------------------------------------------------------- 提交
    def submit(self, path=None, name=None, sided=None):
        """提交一个文件：绝对/相对路径可直接给，也可以只给仓库里的文件名。"""
        src = ""
        if name and not path:
            src = _repo.safe_path(self.cfg, name)
            if not src:
                return {"ok": False, "msg": "打印仓库里没有这个文件：%s" % name}
        else:
            src = str(path or "")
            if src and not os.path.isabs(src):
                src = os.path.join(_config.ROOT, src)
            if not os.path.isfile(src):
                cand = _repo.safe_path(self.cfg, os.path.basename(src))
                if cand:
                    src = cand
        if not src or not os.path.isfile(src):
            return {"ok": False, "msg": "文件不存在：%s" % (path or name or "(空)")}
        queued, err = self.staging.add(src, {"sided": sided or
                                             (self.cfg.get("printer") or {}).get(
                                                 "duplex") or "simplex",
                                             "from": "console"})
        if err:
            return {"ok": False, "msg": err}
        if self.log:
            self.log.info("调度入队：%s → %s", os.path.basename(src), queued)
        st = self.status()
        hint = ("已入队，打印守护会按 FIFO 处理"
                if st.get("daemon", {}).get("running")
                else "已入队；但打印守护未启动，启动后会自动处理")
        return {"ok": True, "queued": queued, "src": os.path.basename(src), "hint": hint}

    def enqueue_path(self, src):
        return self.submit(path=src)

    def test_page(self, text=""):
        """生成一条文本测试页并投进队列（控制台的「下发测试指令」）。

        模拟模式下它只会进队列、被守护"打印"并归档到 `_模拟/`，不会接触任何设备。
        """
        from ..printing import repo
        root = repo.repo_dir(self.cfg)
        name = "测试页.txt"
        path = os.path.join(root, name)
        body = ("xihe-lite 打印测试页\n"
                "=====================\n"
                "时间：%s\n"
                "内容：%s\n"
                "说明：本页由控制台「下发测试指令」生成；模拟模式下不会真的出纸。\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), text or "（默认测试内容）"))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            return {"ok": False, "msg": "测试页写入失败：%s: %s" % (type(e).__name__, e)}
        r = self.submit(path=path)
        r["text"] = text
        r["file"] = name
        return r

    # ---------------------------------------------------------------- 取消
    def clear(self):
        """清空打印队列：**先写取消信号，再删队列任务**。

        生产事故：旧版只删 spooler 任务，守护不知道任务被清，超时后仍归档成
        「已打印」。所以顺序不能反 —— 信号先落盘，守护每轮 + 归档前都会查。
        """
        jobs, docs = [], []
        q = {}
        try:
            q = self.backend.queue() or {}
        except Exception:
            pass
        for j in (q.get("jobs_detail") or []):
            if j.get("id") is not None:
                jobs.append(j["id"])
            if j.get("doc"):
                docs.append(j["doc"])
        # 演练/无队列时，用当前正在处理（_printing）的文件名当匹配依据
        try:
            for f in os.listdir(self.staging.dir("printing")):
                if not f.endswith(".meta.json"):
                    docs.append(f)
        except Exception:
            pass
        ts = time.time()
        payload = {"ts": ts, "by": "console", "pid": os.getpid(),
                   "cleared": len(jobs), "jobs": jobs, "docs": docs,
                   "stage": "intent"}
        if not self.staging.write_clear_signal(payload):
            return {"ok": False, "msg": "取消信号写入失败，未清队列"}
        deleted, failed = 0, []
        try:
            import win32print
            name = (self.cfg.get("printer") or {}).get("name") or ""
            handle = win32print.OpenPrinter(
                name or win32print.GetDefaultPrinter(),
                {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
            try:
                for j in (win32print.EnumJobs(handle, 0, 9999, 1) or []):
                    try:
                        win32print.SetJob(handle, j["JobId"], 0, None,
                                          win32print.JOB_CONTROL_DELETE)
                        deleted += 1
                    except Exception as e:
                        failed.append({"id": j.get("JobId"), "err": str(e)[:80]})
            finally:
                win32print.ClosePrinter(handle)
        except Exception:
            pass          # 没有 pywin32 / 没有打印机：只发信号，这在演练模式是正确行为
        payload.update({"cleared": deleted, "stage": "done", "failed": failed})
        self.staging.write_clear_signal(payload)
        if self.log:
            self.log.info("clear：信号已写（jobs=%s docs=%s），实际删除 %d 个",
                          jobs, docs[:3], deleted)
        return {"ok": True, "cleared": deleted, "failed": failed,
                "signal": {"ts": int(ts), "jobs": jobs, "docs": docs},
                "note": "已写取消信号：守护会把在途任务判 cancelled，不会归档为已打印"}

    # ---------------------------------------------------------------- 重投
    def retry(self, name):
        base, err = self.staging.retry(name)
        if err:
            return {"ok": False, "msg": err}
        if self.log:
            self.log.info("重投：%s", base)
        return {"ok": True, "requeued": base,
                "hint": "已重新投回 ready 队列，守护会再处理一次"}

    # ---------------------------------------------------------------- 仓库
    def repo_list(self):
        return _repo.list_files(self.cfg)

    def repo_upload(self, name, b64):
        r = _repo.save_upload(self.cfg, name, b64)
        if r.get("ok") and self.log:
            self.log.info("上传到打印仓库：%s（%s 字节）", name, r.get("size"))
        return r

    def repo_delete(self, names):
        return _repo.delete(self.cfg, names)

    def repo_print(self, names):
        """仓库文件一键提交打印（逐个入队，不走线程；真正的串行由守护保证）。"""
        ok, bad = [], []
        for n in (names or []):
            p = _repo.safe_path(self.cfg, n)
            if not p:
                bad.append(os.path.basename(str(n)))
                continue
            r = self.submit(path=p)
            (ok if r.get("ok") else bad).append(os.path.basename(str(n)))
        return {"ok": True, "queued": ok, "bad": bad}

    def demo_files(self):
        made = _repo.make_samples(self.cfg)
        return {"ok": True, "created": [os.path.basename(p) for p in made],
                "dir": _repo.relpath(_repo.repo_dir(self.cfg))}
