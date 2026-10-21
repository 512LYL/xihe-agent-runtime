#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印守护入口（Print Daemon）

等价于 `python -m xihe.printing.daemon`，两种写法都支持，方便按习惯调用：

    python print_daemon.py                 # 守护前台跑（Ctrl+C 停）
    python print_daemon.py --once           # 只处理一轮就退出（调试）
    python print_daemon.py --port 18911     # 覆盖 /status 端口

它盯 `runtime/staging/` 的 ready 队列，把文件串行"投给"打印后端并判定结果：
  · 默认 mock（模拟模式）：不接触任何设备，结果进 `_模拟/`
  · 显式配 printer.backend=win32 才会真的打印
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xihe.printing.daemon import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
