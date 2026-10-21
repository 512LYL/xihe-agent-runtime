# -*- coding: utf-8 -*-
"""打印后端：一家真机（Windows 驱动）+ 一个模拟模式替身

`make_backend()` 是唯一入口：
    printer.backend = "auto"     有可用打印机 → Win32Backend，否则 → SimulatedBackend
    printer.backend = "win32"    强制走真机（不可用就如实失败，不会假装成功）
    printer.backend = "mock"  强制模拟模式

两个后端暴露同一套接口，所以驱动/守护/preflight 不需要知道背后有没有设备：
    available() -> (bool, 中文说明)
    queue()     -> {"ok","jobs","status","problems","hard","soft","msg"}
    ews()       -> {"ok","cat","msg"}          # 权威状态
    submit()    -> (bool, 输出尾部)
    wake()      -> None                        # 唤醒网络打印机（尽力而为）
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

from . import states

# ---------------------------------------------------------------- 通用探测
def tcp_ok(ip, port, timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def ping_ok(ip, timeout_ms=800):
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(["ping", "-n" if os.name == "nt" else "-c", "1",
                            "-w" if os.name == "nt" else "-W", str(timeout_ms), ip],
                           capture_output=True, timeout=6, creationflags=flags)
        return r.returncode == 0
    except Exception:
        return False


def ews_status(ip, timeout=8.0):
    """读 HP 类打印机 EWS 的 ProductStatusDyn.xml，取 StatusCategory 作为权威状态。"""
    if not ip:
        return {"ok": False, "cat": "unknown", "msg": "未配置打印机 IP", "cats": []}
    try:
        url = "http://%s/DevMgmt/ProductStatusDyn.xml" % ip
        req = urllib.request.Request(url, headers={"User-Agent": "xihe-lite"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "ignore")
        cats = [c.strip() for c in
                re.findall(r"<pscat:StatusCategory>([^<]+)</pscat:StatusCategory>", txt)]
        return {"ok": True, "cat": (cats[0] if cats else "unknown"),
                "msg": "", "cats": cats}
    except Exception as e:
        return {"ok": False, "cat": "unknown", "cats": [],
                "msg": "%s: %s" % (type(e).__name__, str(e)[:80])}


def _friendly_win_err(e, name=""):
    """把 pywin32 的错误元组翻成一句人话（别把 (1801, 'OpenPrinter', ...) 直接抛给用户）。"""
    txt = str(e)
    if "1801" in txt or "打印机名无效" in txt or "无效的打印机名称" in txt:
        return "打印机名无效或该打印机未安装：%s" % (name or "(空)")
    if "1722" in txt or "RPC" in txt:
        return "打印后台服务（Spooler）未运行，请先启动它"
    if "1800" in txt:
        return "打印机名无效：%s" % (name or "(空)")
    if "拒绝访问" in txt or "5" == txt.strip():
        return "打开打印队列被拒绝（权限不足）：%s" % (name or "(空)")
    return "%s: %s" % (type(e).__name__, txt[:90])


def win_queue(printer_name=""):
    """读 Windows 打印队列（名字留空 = 系统默认打印机）。无 pywin32 时 ok=False。"""
    out = {"ok": False, "jobs": 0, "status": 0, "problems": [], "hard": [], "soft": [],
           "paused": False, "name": printer_name or "(系统默认)", "msg": ""}
    try:
        import win32print
    except Exception as e:
        out["msg"] = "未安装 pywin32（%s）" % type(e).__name__
        return out
    try:
        name = printer_name or win32print.GetDefaultPrinter()
        out["name"] = name
        h = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(h, 2)
            jobs = win32print.EnumJobs(h, 0, 9999, 1) or []
        finally:
            win32print.ClosePrinter(h)
        st = int(info.get("Status") or 0)
        problems, hard, soft = states.decode_status(st)
        out.update(ok=True, status=st, jobs=int(info.get("cJobs") or len(jobs)),
                   problems=problems, hard=hard, soft=soft, paused=bool(st & 0x1),
                   jobs_detail=[{"id": j.get("JobId"), "doc": j.get("pDocument"),
                                 "status": j.get("Status")} for j in jobs][:20])
    except Exception as e:
        out["msg"] = _friendly_win_err(e, out.get("name") or printer_name)
    return out


def list_printers():
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        return []


# ---------------------------------------------------------------- 真机后端
class Win32Backend:
    """Windows 驱动渲染路径：PDF → 位图 → 驱动；文本 → 驱动排版。

    为什么不用纯 IPP：这类机型的 IPP 只认 PCLm / pwg-raster / urf，
    实测「IPP + application/pdf」被拒（status 1034），
    「IPP + octet-stream + PDF」能收但打出横向条纹乱码 —— 所以走驱动。
    """

    simulated = False

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log
        p = cfg.get("printer") or {}
        self.printer_name = p.get("name") or ""
        self.ip = p.get("ip") or ""
        self.paper_source = p.get("paper_source", 15)
        self.ews_timeout = float((cfg.get("print") or {}).get("ews_timeout", 8))
        self.timeout = float((cfg.get("print") or {}).get("submit_timeout", 300))
        self._last = None

    def available(self):
        """能不能「现在就接任务」：队列读得到、没暂停、没有硬错误。

        注意：`auto` 模式下这里返回 False 就会降级到模拟模式 —— 这是有意的。
        一台暂停/报错/离线的打印机如果硬着头皮提交，只会把文件卡在队列里
        （生产事故里就是这么积累出「未确认出纸」的）。想强制走真机请设
        `printer.backend = "win32"`，那时失败也是如实失败。
        """
        q = win_queue(self.printer_name)
        if not q.get("ok"):
            return False, q.get("msg") or "打印队列读不到"
        if q.get("paused"):
            return False, "打印机处于暂停状态（%s）" % q.get("name")
        if q.get("hard"):
            return False, "打印机状态位报错：%s（%s）" % (
                "/".join(q["hard"]), q.get("name"))
        return True, "打印机：%s" % q.get("name")

    def name(self):
        return "win32"

    def queue(self):
        return win_queue(self.printer_name)

    def ews(self):
        return ews_status(self.ip, self.ews_timeout)

    def wake(self):
        """唤醒网络打印机：ping + 探常见端口（尽力而为，不报错）。"""
        if not self.ip:
            return
        try:
            subprocess.run(["ping", "-n", "1", "-w", "800", self.ip],
                           capture_output=True, timeout=5,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        except Exception:
            pass
        for port in (9100, 631, 80):
            if tcp_ok(self.ip, port, 0.8):
                return

    def snapshot(self):
        """给 /status、preflight 用的只读快照（不推进任何状态）。"""
        q = self.queue()
        e = self.ews()
        return {"backend": self.name(), "simulated": False, "queue": q, "ews": e,
                "printer": q.get("name") or "", "ip": self.ip}

    def submit(self, path, duplex="simplex"):
        """按扩展名分派：Office → PDF；PDF → 驱动；图片 → 合成 A4 PDF；文本 → 驱动排版。"""
        from . import converter, win_printer
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in converter.OFFICE_EXT:
                pdf, err = converter.office_to_pdf(path, self.cfg)
                if not pdf:
                    return False, err
                return self._run_print_pdf(pdf, duplex)
            if ext == ".pdf":
                return self._run_print_pdf(path, duplex)
            if ext in converter.IMAGE_EXT:
                pdf, err = converter.images_to_pdf([path], self.cfg)
                if not pdf:
                    return False, err
                return self._run_print_pdf(pdf, duplex)
            if ext in converter.TEXT_EXT:
                text = open(path, encoding="utf-8", errors="ignore").read()
                ok = win_printer.print_text(text, printer_name=self.printer_name,
                                            paper_source=self.paper_source,
                                            duplex=duplex)
                return bool(ok), ("[OK] 文本已提交打印" if ok else "[X] 文本打印失败")
            return False, "[X] 不支持的文件类型：%s" % ext
        except Exception as e:
            return False, "[X] 提交异常：%s: %s" % (type(e).__name__, e)

    def _run_print_pdf(self, pdf, duplex):
        from . import win_printer
        ok = win_printer.print_pdf(pdf, printer_name=self.printer_name,
                                   paper_source=self.paper_source, duplex=duplex)
        return bool(ok), ("[OK] PDF 已提交打印：%s" % os.path.basename(pdf) if ok
                          else "[X] PDF 打印失败")


# ---------------------------------------------------------------- 模拟模式后端
# 一帧 = 一次队列采样；cat 是「此刻的 EWS 权威状态」。
# 时序约定：`driver.process` 里「等任务进队列」会消费第 1 帧，之后才是出纸轮询。
HAPPY_FRAMES = [
    {"jobs": 1, "status": 0x2000, "cat": "processing"},   # 任务进了队列
    {"jobs": 1, "status": 0x2000, "cat": "processing"},   # 在打
    {"jobs": 0, "status": 0x0000, "cat": "ready"},        # 队列清空、回就绪
    {"jobs": 0, "status": 0x0000, "cat": "ready"},
]


class SimulatedBackend:
    """模拟打印后端：没有打印机时让整条链路（含状态机）照样跑通。

    它按脚本给出「队列水位 / 状态位 / EWS 类别」，驱动完全按真机规则判定，
    所以演练结论与真机一致；区别只有两点：
      1. 没有任何字节发往真实设备（不 import win32print / 不 Popen 打印进程）；
      2. 结果标记为 `simulated`，归档进 `_模拟/`，绝不冒充 `_已打印`。
    """

    simulated = True

    def __init__(self, frames=None, name="mock", reason="未检测到可用打印机"):
        self.frames = [dict(f) for f in (frames or HAPPY_FRAMES)]
        self._name = name
        self.reason = reason
        self._phase = "idle"      # idle → run
        self._i = 0
        self.submitted = []

    def name(self):
        return self._name

    def available(self):
        return True, "模拟模式后端（%s）：%s，不会接触任何打印设备" % (self._name, self.reason)

    def queue(self):
        if self._phase != "run":
            return {"ok": True, "jobs": 0, "status": 0, "problems": [],
                    "hard": [], "soft": [], "paused": False, "msg": "", "sim": True}
        f = self.frames[min(self._i, len(self.frames) - 1)]
        self._i += 1
        problems, hard, soft = states.decode_status(f.get("status"))
        return {"ok": True, "jobs": int(f.get("jobs") or 0),
                "status": int(f.get("status") or 0), "problems": problems,
                "hard": hard, "soft": soft, "paused": bool(int(f.get("status") or 0) & 1),
                "msg": "", "sim": True}

    def ews(self):
        if self._phase != "run":
            return {"ok": True, "cat": "ready", "msg": "模拟模式（空闲）", "sim": True}
        f = self.frames[max(0, min(self._i - 1, len(self.frames) - 1))]
        cat = f.get("cat") or "ready"
        return {"ok": True, "cat": cat, "msg": "模拟模式（%s）" % states.ews_text(cat),
                "sim": True}

    def wake(self):
        return None

    def snapshot(self):
        """只读快照：**不推进脚本帧**（否则外部轮询会把演练时间线打乱）。"""
        if self._phase != "run":
            f = {"jobs": 0, "status": 0, "cat": "ready"}
        else:
            f = self.frames[max(0, min(self._i - 1, len(self.frames) - 1))]
        problems, hard, soft = states.decode_status(f.get("status"))
        return {"backend": self.name(), "simulated": True,
                "queue": {"ok": True, "jobs": int(f.get("jobs") or 0),
                          "status": int(f.get("status") or 0), "problems": problems,
                          "hard": hard, "soft": soft, "sim": True},
                "ews": {"ok": True, "cat": f.get("cat") or "ready", "sim": True},
                "printer": "(模拟模式)", "ip": ""}

    def submit(self, path, duplex="simplex"):
        self._phase = "run"
        self._i = 0
        self.submitted.append({"path": os.path.basename(path), "duplex": duplex,
                               "ts": time.time()})
        return True, ("[SIM] 模拟模式：已受理 %s（%s 字节），"
                      "未向任何真实打印机提交" % (os.path.basename(path),
                                                 os.path.getsize(path)))


class ScriptedBackend(SimulatedBackend):
    """自检专用：状态序列和模拟模式一样按脚本给，但**声称自己是真机**。

    只用来自检断言「printed → `_已打印`」这条真机分支（`simulated=False`）。
    它同样不 import pywin32、不向任何设备提交任务。
    """

    simulated = False

    def __init__(self, frames=None):
        SimulatedBackend.__init__(self, frames, name="scripted-test",
                                  reason="自检脚本")

    def submit(self, path, duplex="simplex"):
        ok, out = SimulatedBackend.submit(self, path, duplex)
        return ok, out.replace("[SIM]", "[TEST]")


def make_backend(cfg, log=None):
    """按配置造后端。返回 (backend, 一行中文说明)。"""
    want = ((cfg.get("printer") or {}).get("backend") or "auto").lower()
    if want == "mock":
        b = SimulatedBackend(reason="配置指定 mock")
        return b, "打印后端：模拟模式（配置指定）"
    win = Win32Backend(cfg, log=log)
    ok, why = win.available()
    if want == "win32":
        return win, "打印后端：Windows 驱动（%s）" % why
    if ok:
        return win, "打印后端：Windows 驱动（%s）" % why
    b = SimulatedBackend(reason=why)
    line = ("未检测到可用打印机（%s）→ 已自动降级为模拟模式后端（mock），"
            "不会向任何设备提交任务" % why)
    if log:
        log.warning(line)
    return b, line
