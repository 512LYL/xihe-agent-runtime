# -*- coding: utf-8 -*-
"""命令行入口：python -m xihe <命令>

    serve        启动总控制台（状态看板 + 服务启停 + 打印调度）
    stop         停止控制台与全部受管服务
    status       打印一次当前状态（服务 + 打印链路 + 暂存计数）
    preflight    打印演示前置自检（只读，一行中文结论）
    add <文件>   把文件投进暂存 ready 队列
    retry <名>   把 _失败/_队列 里的文件重投
    clear        清空打印队列（写取消信号）
    demo         在打印仓库里生成演示样本
    selftest     跑内置自检（模拟模式，不接触打印机）
    where        打印配置来源与解析后的路径（排查用）
"""
import argparse
import json
import os
import sys


def _out(obj, as_json=True):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def _load(args):
    from . import config as _config
    return _config.load(getattr(args, "config", None))


def _rel(p):
    """相对项目根目录；跨盘符时退回绝对路径。"""
    from . import config as _config
    try:
        return os.path.relpath(p, _config.ROOT)
    except Exception:
        return p


# ---------------------------------------------------------------- 子命令
def cmd_serve(args, cfg):
    from .console import server
    if getattr(args, "port", None):
        cfg["console"]["port"] = int(args.port)
    return server.main(cfg, open_browser=getattr(args, "open", False))


def cmd_stop(args, cfg):
    from . import config as _config
    from . import logset
    from .console.services import ServiceManager, kill_pid, pids_on_port, read_pidfile
    log = logset.get_logger(cfg, "console")
    svc = ServiceManager(cfg, log)
    res = svc.stop_all()
    console_pid = read_pidfile(os.path.join(_config.path(cfg, "paths", "run"),
                                            "console.pid"))
    killed = []
    if console_pid:
        if kill_pid(console_pid):
            killed.append(console_pid)
    port = int((cfg.get("console") or {}).get("port") or 18910)
    for p in pids_on_port(port):
        if p != os.getpid() and kill_pid(p):
            killed.append(p)
    print("服务已停止：%s" % ", ".join(sorted(k for k, v in res.items()
                                            if v.get("killed")) or ["（本来就没在跑）"]))
    print("控制台已停止：%s" % (", ".join(str(x) for x in sorted(set(killed)))
                              or "（本来就没在跑）"))
    for k, v in res.items():
        if not v.get("ok"):
            print("  ! %s: %s" % (k, v.get("msg")))
    return 0


def cmd_status(args, cfg):
    from .console.dispatch import Dispatcher
    from .console.services import ServiceManager
    svc = ServiceManager(cfg)
    disp = Dispatcher(cfg)
    st = disp.status()
    data = {
        "config": os.path.basename((cfg.get("_meta") or {}).get("config_file") or "")
                  or "(未找到)",
        "using_example": bool((cfg.get("_meta") or {}).get("using_example")),
        "services": svc.status(),
        "print": {
            "backend": st.get("backend"),
            "daemon_running": (st.get("daemon") or {}).get("running"),
            "queue": st.get("queue"),
            "ews": st.get("ews"),
            "staging": st.get("staging"),
            "ready": st.get("ready"),
            "last": st.get("last"),
        },
        "paths": {k: _rel(v) for k, v in (cfg.get("paths") or {}).items()},
    }
    _out(data)
    return 0


def cmd_route(args, cfg):
    """下发一条文本测试指令，走四级路由骨架（Mock），打印 [ROUTE] 流转。"""
    from . import router
    res = router.route(args.text, cfg)
    print(res["reply"])
    print("")
    print("路由结果：tier=%s（%s），总耗时 %d ms" % (
        res["tier"] or "未命中", res["tier_name"] or "-", res["elapsed_ms"]))
    print("流转日志：")
    for line in res["route_log"]:
        print("  " + line)
    print("")
    print("（日志文件：runtime/logs/router.log）")
    return 0 if res["ok"] else 1


def cmd_preflight(args, cfg):
    from .printing import preflight
    argv = []
    if getattr(args, "config", None):
        argv += ["--config", args.config]
    if getattr(args, "verbose", False):
        argv.append("-v")
    if getattr(args, "json", False):
        argv.append("--json")
    return preflight.main(argv)


def cmd_add(args, cfg):
    from .console.dispatch import Dispatcher
    r = Dispatcher(cfg).submit(path=args.path)
    _out(r)
    return 0 if r.get("ok") else 1


def cmd_retry(args, cfg):
    from .console.dispatch import Dispatcher
    r = Dispatcher(cfg).retry(args.name)
    _out(r)
    return 0 if r.get("ok") else 1


def cmd_clear(args, cfg):
    from .console.dispatch import Dispatcher
    r = Dispatcher(cfg).clear()
    _out(r)
    return 0 if r.get("ok") else 1


def cmd_demo(args, cfg):
    from .printing import repo
    made = repo.make_samples(cfg)
    print("已生成演示样本（打印仓库）：")
    for p in made:
        print("  · %s" % os.path.basename(p))
    if not made:
        print("  （都已存在，没新建文件）")
    print("仓库目录：%s" % repo.relpath(repo.repo_dir(cfg)))
    return 0


def cmd_selftest(args, cfg):
    import selftest
    return selftest.main(["--config", args.config] if args.config else [])


def cmd_where(args, cfg):
    meta = cfg.get("_meta") or {}
    print("项目根目录：%s" % meta.get("root"))
    print("配置文件  ：%s%s" % (meta.get("config_file") or "(没找到)",
                              "（用的是 .example，建议复制成 config.json）"
                              if meta.get("using_example") else ""))
    if meta.get("parse_error"):
        print("解析错误  ：%s" % meta["parse_error"])
    print("预留目录  ：")
    for k, v in (cfg.get("paths") or {}).items():
        print("  %-8s %s  %s" % (k, _rel(v),
                                 "（存在）" if os.path.isdir(v) else "（将自动创建）"))
    p = cfg.get("printer") or {}
    print("打印机    ：%s  后端=%s  IP=%s" % (p.get("name") or "(系统默认)",
                                             p.get("backend"), p.get("ip") or "(未配置)"))
    print("控制台    ：http://%s:%s/" % ((cfg.get("console") or {}).get("host"),
                                         (cfg.get("console") or {}).get("port")))
    return 0


# ---------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(prog="python -m xihe",
                                description="曦和 · 精简可运行版（控制台 + 打印链路）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="启动总控制台")
    p.add_argument("--port", type=int, default=None, help="覆盖 console.port")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")

    sub.add_parser("stop", help="停止控制台与全部服务")
    sub.add_parser("status", help="打印一次状态")
    rt = sub.add_parser("route", help="下发一条文本测试指令，看四级路由降级过程")
    rt.add_argument("text", nargs="?", default="ping", help="要路由的文本")
    pf = sub.add_parser("preflight", help="打印前置自检（只读）")
    pf.add_argument("-v", "--verbose", action="store_true")
    pf.add_argument("--json", action="store_true")

    a = sub.add_parser("add", help="把文件投进暂存 ready 队列")
    a.add_argument("path")
    r = sub.add_parser("retry", help="重投 _失败/_队列 里的文件")
    r.add_argument("name")
    sub.add_parser("clear", help="清空打印队列（写取消信号）")
    sub.add_parser("demo", help="在打印仓库生成演示样本")
    sub.add_parser("selftest", help="跑内置自检")
    sub.add_parser("where", help="打印配置来源与路径")
    return ap


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 允许 `python -m xihe.console.server` 之外，也用 `python -m xihe` 起控制台
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "serve"

    cfg = _load(args)
    table = {"serve": cmd_serve, "stop": cmd_stop, "status": cmd_status,
             "route": cmd_route, "preflight": cmd_preflight, "add": cmd_add,
             "retry": cmd_retry, "clear": cmd_clear, "demo": cmd_demo,
             "selftest": cmd_selftest, "where": cmd_where}
    fn = table.get(args.cmd)
    if not fn:
        ap.print_help()
        return 2
    if args.cmd in ("serve", "stop", "status", "demo", "where", "route"):
        from . import config as _config
        _config.ensure_dirs(cfg)
    return fn(args, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
