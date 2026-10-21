# -*- coding: utf-8 -*-
"""打印演示「前置自检」—— **只读，绝不提交任何打印任务**

演示前 30 秒跑一次，确认这台机器现在能不能打。它只做读操作：
读配置、读目录、探端口、读 EWS 的 XML、读 Windows 打印队列属性；
**不调用任何提交函数、不写暂存、不改打印机状态**。

检查项：
    1 配置       config.json 是否就绪、运行期目录是否可写
    2 依赖       pywin32 / pypdfium2 / pillow 是否装了（模拟模式不强求）
    3 打印后端  真机可用 → win32；否则自动降级 mock
    4 网络       ping 打印机 IP + TCP 80/631/9100（仅当配了 printer.ip）
    5 EWS 状态   http://<ip>/DevMgmt/ProductStatusDyn.xml 的 StatusCategory
    6 Windows 队列  cJobs 是否 0、是否 Paused、状态位是否报错
    7 纸盒       EWS 报缺纸 / Windows 0x10
    8 暂存       ready 里有没有待打印文件（有的话演示一开始就会打出去）
    9 守护       打印守护是否在跑（端口探活）

输出：**默认一行中文就绪结论**（给演示者直读）；-v 追加逐项明细；--json 出机器可读。
退出码：0 就绪 / 1 不可演示 / 2 可演示但有告警。

用法：
    python -m xihe.printing.preflight
    python -m xihe.printing.preflight -v
    python -m xihe.printing.preflight --json
"""
import argparse
import json
import os
import sys

from .. import config as _config
from . import backend as _backend
from . import states
from .staging import Staging

STATUS_TEXT = [(bit, code, cn) for bit, code, cn in states.STATUS_BITS]


def _check_config(cfg):
    meta = cfg.get("_meta") or {}
    d = {"ok": True, "file": os.path.basename(meta.get("config_file") or ""),
         "using_example": bool(meta.get("using_example")),
         "error": meta.get("parse_error") or "", "dirs": {}, "msgs": []}
    if meta.get("parse_error"):
        d["ok"] = False
        d["msgs"].append("配置文件解析失败：%s" % meta["parse_error"])
    if not meta.get("config_file"):
        d["ok"] = False
        d["msgs"].append("缺少 config.json（复制 config.example.json 改名即可）")
    elif meta.get("using_example"):
        d["msgs"].append("当前用的是 config.example.json（建议复制成 config.json 再改）")
    for key in ("staging", "logs", "repo", "run"):
        p = _config.path(cfg, "paths", key)
        try:
            os.makedirs(p, exist_ok=True)
            probe = os.path.join(p, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            d["dirs"][key] = p
        except Exception as e:
            d["ok"] = False
            d["msgs"].append("目录不可写（%s）：%s: %s" % (key, type(e).__name__, e))
    return d


def _check_deps():
    d = {}
    for mod, pkg in (("win32print", "pywin32"), ("pypdfium2", "pypdfium2"),
                     ("PIL", "pillow"), ("psutil", "psutil")):
        try:
            __import__(mod)
            d[pkg] = True
        except Exception:
            d[pkg] = False
    return d


def _check_backend(cfg):
    be, line = _backend.make_backend(cfg)
    snap = be.snapshot()
    return {"name": be.name(), "simulated": be.simulated, "line": line,
            "printer": snap.get("printer") or "", "ip": snap.get("ip") or "",
            "available": (not be.simulated)}


def _check_network(ip):
    d = {"ok": False, "ip": ip, "ping": False, "ports": {}, "text": "未配置打印机 IP"}
    if not ip:
        return d
    d["ping"] = _backend.ping_ok(ip)
    for port in (80, 631, 9100):
        d["ports"][port] = _backend.tcp_ok(ip, port, 1.2)
    d["ok"] = d["ping"] or any(d["ports"].values())
    d["text"] = "ping=%s，端口 %s" % (
        "通" if d["ping"] else "不通",
        "/".join("%d:%s" % (p, "通" if v else "不通") for p, v in d["ports"].items()))
    return d


def _check_ews(ip, timeout=8.0):
    d = _backend.ews_status(ip, timeout)
    d["text"] = states.ews_text(d.get("cat"))
    return d


def _check_win(printer_name):
    d = _backend.win_queue(printer_name)
    d["text"] = "空" if (d.get("ok") and int(d.get("jobs") or 0) == 0) else (
        "%s 个任务" % d.get("jobs") if d.get("ok") else "读不到")
    return d


def _check_paper(ews, win):
    cats = set(ews.get("cats") or [])
    win_paper = bool(int(win.get("status") or 0) & 0x10)
    if cats & {"paperOut", "mediaEmpty"}:
        return False, "空（EWS=%s）" % "/".join(sorted(cats & {"paperOut", "mediaEmpty"}))
    if ews.get("ok"):
        if win_paper:
            return True, "有纸（EWS 未报缺纸；Windows 0x10 仅软告警）"
        return True, "有纸（EWS=%s）" % (ews.get("cat") or "unknown")
    if win.get("ok"):
        return (not win_paper), ("空（仅 Windows 0x10，EWS 读不到）" if win_paper
                                 else "有纸（仅 Windows 状态，EWS 读不到）")
    return None, "未知（EWS 与 Windows 都读不到）"


def _check_daemon(cfg):
    port = int((cfg.get("daemon") or {}).get("port") or 18911)
    ok = _backend.tcp_ok("127.0.0.1", port, 0.6)
    return {"ok": ok, "port": port,
            "text": "在跑（:%d）" % port if ok else "未启动（:%d 无监听）" % port}


def run(cfg=None, verbose=False, timeout=8.0):
    cfg = cfg or _config.load()
    conf = _check_config(cfg)
    deps = _check_deps()
    be = _check_backend(cfg)
    ip = (cfg.get("printer") or {}).get("ip") or ""
    net = _check_network(ip)
    ews = _check_ews(ip, timeout)
    win = _check_win((cfg.get("printer") or {}).get("name") or "")
    paper_ok, paper_txt = _check_paper(ews, win)
    staging = Staging(cfg)
    staging.ensure()
    backlog = [os.path.basename(p) for p in staging.ready_files()]
    dmn = _check_daemon(cfg)

    blockers, warns = [], []
    if not conf["ok"]:
        blockers += conf["msgs"]
    elif conf["msgs"]:
        warns += conf["msgs"]
    for pkg in ("pillow",):
        if not deps.get(pkg):
            warns.append("缺依赖 %s（图片/PDF 真机打印需要）" % pkg)

    if be["simulated"]:
        # 模拟模式：没有任何设备阻塞项，但必须让演示者知道这是演练
        if not dmn["ok"]:
            warns.append("打印守护未启动（控制台一键启动即可）")
        if backlog:
            warns.append("暂存有 %d 个待打印文件" % len(backlog))
        tail = "｜暂存:%s｜守护:%s" % (("空" if not backlog else
                                       "%d 个待打" % len(backlog)), dmn["text"])
        if warns:
            concl = ("⚠️ 模拟模式可用（有告警）｜%s｜不接触任何打印设备%s｜告警：%s"
                     % (be["line"], tail, "、".join(warns)))
            code = 2
        else:
            concl = ("✅ 模拟模式就绪：%s → 本次不接触任何打印设备，"
                     "状态机/归档/回执可完整演示%s" % (be["line"], tail))
            code = 0
        return _pack(concl, code, cfg, conf, deps, be, net, ews, win, paper_ok,
                     paper_txt, backlog, dmn, blockers, warns)

    # ---- 真机模式
    if ip:
        if not net["ok"]:
            blockers.append("网络不通（%s）" % net["text"])
        if not ews.get("ok"):
            warns.append("EWS 读不到（%s）" % (ews.get("msg") or "超时"))
        else:
            cats = set(ews.get("cats") or [])
            hard = cats & (states.EWS_ERROR - states.EWS_SOFT)
            if hard:
                blockers.append("EWS 报错：%s"
                                % "/".join(states.ews_text(c) for c in sorted(hard)))
    else:
        # 没配 printer.ip：驱动照样能打，但拿不到 EWS 权威状态 → 只告警不阻塞
        warns.append("未配置 printer.ip：EWS 权威状态不可用，出纸判定只依赖 Windows 状态位")
    if win.get("ok"):
        if int(win.get("jobs") or 0) > 0:
            blockers.append("Windows 队列有 %d 个任务" % win.get("jobs"))
        if win.get("paused"):
            blockers.append("打印机处于 Paused")
        if win.get("hard"):
            blockers.append("Windows 状态位报错：%s（0x%X）" % (
                "/".join(win["hard"]), int(win.get("status") or 0)))
    elif ip:
        warns.append("读不到 Windows 打印队列（%s）" % (win.get("msg") or "未知"))
    else:
        blockers.append("读不到 Windows 打印队列（%s）" % (win.get("msg") or "未知"))
    if paper_ok is False:
        blockers.append("纸盒%s" % paper_txt)
    elif paper_ok is None:
        warns.append("纸盒状态未知")
    if not dmn["ok"]:
        warns.append("打印守护未启动（控制台一键启动即可）")
    if backlog:
        warns.append("暂存有 %d 个待打印文件" % len(backlog))

    q_txt = win.get("text") or "未知"
    net_short = ("通" if net["ok"] else "不通") if ip else "跳过"
    ews_short = ("%s(%s)" % (ews.get("cat"), ews.get("text"))) if ews.get("ok") else (
        "读不到" if ip else "跳过")
    paused_txt = "是" if win.get("paused") else ("否" if win.get("ok") else "未知")
    if blockers:
        concl = ("❌ 不可演示 | 网络:%s | EWS:%s | 队列:%s | 暂停:%s | 纸盒:%s；原因：%s"
                 % (net_short, ews_short, q_txt, paused_txt, paper_txt,
                    "、".join(blockers)))
        if warns:
            concl += "；另告警：" + "、".join(warns)
        code = 1
    elif warns:
        concl = ("⚠️ 可演示但有告警 | 网络:%s | EWS:%s | 队列:%s | 暂停:%s | 纸盒:%s；告警：%s"
                 % (net_short, ews_short, q_txt, paused_txt, paper_txt, "、".join(warns)))
        code = 2
    else:
        concl = ("✅ 演示就绪：网络通、EWS=%s、Windows 队列空、未暂停、%s —— 可以开始演示（%s）"
                 % (ews.get("text"), paper_txt, be["printer"] or "默认打印机"))
        code = 0
    return _pack(concl, code, cfg, conf, deps, be, net, ews, win, paper_ok,
                 paper_txt, backlog, dmn, blockers, warns)


def _pack(concl, code, cfg, conf, deps, be, net, ews, win, paper_ok, paper_txt,
          backlog, dmn, blockers, warns):
    return {
        "ok": code == 0, "code": code, "conclusion": concl,
        "mode": "mock" if be["simulated"] else "win32",
        "config": {k: conf[k] for k in ("ok", "file", "using_example", "error", "msgs")},
        "dirs": conf["dirs"],
        "deps": deps,
        "backend": be,
        "network": net, "ews": ews, "windows": win,
        "paper": {"ok": paper_ok, "text": paper_txt},
        "staging": {"ready": backlog, "count": len(backlog)},
        "daemon": dmn,
        "blockers": blockers, "warnings": warns,
    }


def print_report(data, verbose=False):
    if verbose:
        c = data["config"]
        print("配置：%s（%s）" % (c["file"] or "缺失",
                                "用的是 .example，建议复制成 config.json" if c["using_example"]
                                else "已就绪"))
        print("  1 配置      : %s%s" % ("ok" if c["ok"] else "有阻塞",
                                       ("｜" + "；".join(c["msgs"])) if c["msgs"] else ""))
        d = data["deps"]
        print("  2 依赖      : pywin32=%s pypdfium2=%s pillow=%s"
              % (d.get("pywin32"), d.get("pypdfium2"), d.get("pillow")))
        b = data["backend"]
        print("  3 打印后端  : %s（%s）" % (b["name"], "模拟模式" if b["simulated"] else "真机"))
        n = data["network"]
        print("  4 网络      : %s" % (n["text"] if n.get("ip") else "未配置 printer.ip，跳过"))
        e = data["ews"]
        print("  5 EWS 状态  : %s" % (("%s（%s）" % (e.get("cat"), e["text"]))
                                      if e.get("ok") else ("读不到（%s）" % (e.get("msg") or ""))))
        w = data["windows"]
        print("  6 Windows   : 队列%s｜Paused=%s｜Status=0x%X%s" % (
            w.get("text"), "是" if w.get("paused") else "否", int(w.get("status") or 0),
            ("｜" + "/".join(w.get("problems") or [])) if w.get("problems") else ""))
        print("  7 纸盒      : %s" % data["paper"]["text"])
        print("  8 暂存      : %s" % ("空" if not data["staging"]["count"]
                                      else "、".join(data["staging"]["ready"])))
        print("  9 守护      : %s" % data["daemon"]["text"])
        for b_ in data["blockers"]:
            print("  ✗ 阻塞：" + b_)
        for w_ in data["warnings"]:
            print("  ! 告警：" + w_)
        print("")
    print(data["conclusion"])


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="xihe-lite 打印演示前置自检（只读，不打印）")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true", help="追加逐项明细")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--timeout", type=float, default=8.0, help="EWS 超时秒数")
    a = ap.parse_args(argv)
    cfg = _config.load(a.config)
    data = run(cfg, verbose=a.verbose, timeout=a.timeout)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_report(data, verbose=a.verbose)
    return data["code"]


if __name__ == "__main__":
    sys.exit(main())
