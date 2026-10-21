# -*- coding: utf-8 -*-
"""让 `python -m xihe` 直接进命令行入口。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
