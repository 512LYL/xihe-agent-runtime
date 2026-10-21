#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印命令行（Print CLI）

    python print_cli.py status            # 打印机状态 + 队列 + 暂存计数（JSON）
    python print_cli.py jobs              # 只看打印队列
    python print_cli.py staging           # 暂存各目录明细
    python print_cli.py add <文件>         # 投进队列 → 输出「已进入打印队列」
    python print_cli.py test [文本]        # 造一条文本测试页并投进队列
    python print_cli.py clear             # 清空队列（先写取消信号）
    python print_cli.py retry <归档名>     # 把 _失败/_队列/_模拟 里的文件重投
    python print_cli.py preflight [-v]     # 打印演示前置自检（只读）

默认 mock（模拟模式）下不会接触任何设备；只有 config.json 里
`printer.backend` 显式设成 win32 才会真的往打印机提交。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xihe import config as _config                      # noqa: E402
from xihe.console.dispatch import Dispatcher            # noqa: E402
from xihe.printing import preflight as _preflight        # noqa: E402
from xihe.printing.staging import Staging               # noqa: E402


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if (not isinstance(obj, dict)) or obj.get("ok") else 1


_CFG = {"cfg": None}


def _make_cfg():
    if _CFG["cfg"] is None:
        _CFG["cfg"] = _config.load()
    return _CFG["cfg"]


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="print_cli.py", description="xihe-lite 打印命令行")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="打印机状态 + 队列 + 暂存计数")
    sub.add_parser("jobs", help="只看打印队列")
    sub.add_parser("staging", help="暂存各目录明细")
    a = sub.add_parser("add", help="投进打印队列")
    a.add_argument("path")
    t = sub.add_parser("test", help="生成一条文本测试页并投进队列")
    t.add_argument("text", nargs="?", default="")
    sub.add_parser("clear", help="清空打印队列")
    r = sub.add_parser("retry", help="重投已归档文件")
    r.add_argument("name")
    pf = sub.add_parser("preflight", help="打印演示前置自检（只读）")
    pf.add_argument("-v", "--verbose", action="store_true")
    pf.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    args.cmd = args.cmd or "status"

    if args.config:
        _CFG["cfg"] = _config.load(args.config)
    cfg = _make_cfg()

    if args.cmd == "preflight":
        argv2 = [a for a in ("-v" if args.verbose else None,
                             "--json" if args.json else None) if a]
        if args.config:
            argv2 += ["--config", args.config]
        return _preflight.main(argv2)

    disp = Dispatcher(cfg)

    if args.cmd == "status":
        st = disp.status()
        return _out({"ok": True, "backend": st["backend"], "queue": st["queue"],
                     "ews": st["ews"], "staging": st["staging"],
                     "daemon_running": (st.get("daemon") or {}).get("running"),
                     "last": st.get("last")})
    if args.cmd == "jobs":
        return _out({"ok": True, "jobs": (disp.status().get("queue") or {})})
    if args.cmd == "staging":
        staging = Staging(cfg)
        return _out({"ok": True, "counts": staging.counts(),
                     "ready": [os.path.basename(p) for p in staging.ready_files()],
                     "hold": staging.list_dir("hold"),
                     "sim": staging.list_dir("sim"),
                     "done": staging.list_dir("done"),
                     "failed": staging.list_dir("failed")})
    if args.cmd == "add":
        r = disp.submit(path=args.path)
        if r.get("ok"):
            print("已进入打印队列：%s" % r.get("queued"))
            print("（%s）" % r.get("hint", ""))
        return _out(r)
    if args.cmd == "test":
        r = disp.test_page(args.text)
        if r.get("ok"):
            print("已进入打印队列：%s" % r.get("queued"))
            print("（%s）" % r.get("hint", ""))
        return _out(r)
    if args.cmd == "clear":
        return _out(disp.clear())
    if args.cmd == "retry":
        return _out(disp.retry(args.name))
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
