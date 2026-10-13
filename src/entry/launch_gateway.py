#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_gateway.py —— OpenClaw Gateway 启动器（零硬编码路径）

为什么不用一行 `node index.js gateway` 就够了？生产里踩过的两个坑决定了要包装：

坑①（orphan-self-pid 锁报错，2026-06-25 定位）
  编辑器/IDE 的 safe-delete 钩子会通过**环境变量**注入到 gateway 子进程，
  累计 N 次文件操作后要求交互式确认 → gateway 无法应答 → 会话文件写入阻塞
  44 秒 → 超时 → 被误判为 session write lock stale（orphan-self-pid）。
  跨 3 个版本排查后才定位；本启动器在拉起前清除这些环境变量，复发 0 次。

坑②（窗口空白 / 日志看不见）
  gateway 默认不往 stdout 输出运行日志，前台窗口一片空白；加 --verbose 才能看到。

用法
  set OPENCLAW_STATE_DIR=<你的状态目录>
  set OPENCLAW_INDEX=<openclaw 安装目录>/dist/index.js
  python src/entry/launch_gateway.py [--port 18791] [--verbose]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 需要清除的「外部工具注入」环境变量前缀（大小写不敏感匹配）
INJECTED_PREFIXES = ("SAFE_DELETE", "CODEBUDDY")


def scrub_injected_env(verbose: bool = False) -> int:
    """清除注入型环境变量。返回清除条数。"""
    removed = 0
    for k in list(os.environ.keys()):
        if any(p in k.upper() for p in INJECTED_PREFIXES):
            os.environ.pop(k, None)
            removed += 1
            if verbose:
                print("[launcher] scrubbed env: %s" % k)
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OpenClaw Gateway 启动器（零硬编码路径）")
    ap.add_argument("--port", default=os.environ.get("OPENCLAW_GATEWAY_PORT", "18791"))
    ap.add_argument("--node", default=os.environ.get("OPENCLAW_NODE", "node"),
                    help="node 可执行文件（默认取 PATH 中的 node）")
    ap.add_argument("--index", default=os.environ.get("OPENCLAW_INDEX", ""),
                    help="openclaw 的 dist/index.js 路径（必填）")
    ap.add_argument("--state-dir", default=os.environ.get("OPENCLAW_STATE_DIR", ""),
                    help="OpenClaw 状态目录（会话/配置所在）")
    ap.add_argument("--verbose", action="store_true", default=True,
                    help="让 gateway 把运行日志输出到控制台（默认开）")
    ap.add_argument("--no-new-window", action="store_true",
                    help="在当前窗口运行（默认另开新控制台窗口）")
    args = ap.parse_args(argv)

    if not args.index:
        print("[launcher][ERROR] 未指定 OPENCLAW_INDEX（openclaw 的 dist/index.js）", file=sys.stderr)
        return 2
    if not os.path.exists(args.index):
        print("[launcher][ERROR] 找不到 openclaw 入口: %s" % args.index, file=sys.stderr)
        return 2

    if args.state_dir:
        os.environ["OPENCLAW_STATE_DIR"] = args.state_dir
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 避免用户级环境变量覆盖 openclaw.json 里的 summaryModel 配置
    os.environ.pop("LCM_SUMMARY_MODEL", None)
    os.environ.pop("LCM_SUMMARY_PROVIDER", None)

    n = scrub_injected_env(args.verbose)
    if n:
        print("[launcher] cleared %d injected env var(s) — avoids orphan-self-pid 锁误判" % n)

    cmd = [args.node, args.index, "gateway", "--port", str(args.port)]
    if args.verbose:
        cmd.append("--verbose")

    # Windows：CREATE_NEW_CONSOLE = 0x00000010（独立新窗口前台运行，便于观察）
    creationflags = 0x00000010 if (os.name == "nt" and not args.no_new_window) else 0
    print("[launcher] %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, creationflags=creationflags)
    print("[launcher] gateway PID = %d" % proc.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
