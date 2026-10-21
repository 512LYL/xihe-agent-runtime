# -*- coding: utf-8 -*-
"""xihe-lite —— 精简可运行版

从生产系统的 200 KB 单体控制台（204,916 B）与 8 个打印脚本中抽取的**最小可运行集**：

  · 总控制台（`xihe.console`）：一个进程、一个端口，提供状态看板 + 服务启停
    + 打印调度 HTTP 接口。
  · 打印链路（`xihe.printing`）：文档转 PDF → 提交打印 → 状态判定 → 暂存归档
    状态机 → 演示前置自检。

设计原则：
  1. 零绝对路径：所有路径来自 config.json（默认相对本目录）。
  2. 零凭据：仓库内只有 config.example.json / .env.example 模板。
  3. 无打印机也能跑：自动降级到模拟模式后端（mock），给出中文提示，
     绝不向任何真实设备提交任务。
"""

__version__ = "1.0.0"
__all__ = ["config", "logset", "console", "printing"]
