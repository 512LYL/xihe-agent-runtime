# 精简可运行版（xihe-lite）

本目录是「曦和 · AI Agent 运行底座」的**最小可运行闭环**，用于快速试用与复现，
不是完整版。完整能力（三通道智能路由、沐曦 C500 自部署 32B、QQ 机器人接入、
自愈看护层等）见[仓库根目录 README](../../README.md)。

## 这是什么

| 本目录包含 | 完整版另有 |
|---|---|
| 控制台（启停 / 状态看板 / 前置自检） | 三通道智能路由（云端 / 本地 / 异地算力实连） |
| 四级路由**骨架**（云端 API → 本地兜底 → 异地算力 → 离线规则） | 沐曦 C500 + MXMACA 自部署 32B 通道 |
| 打印调度守护（转 PDF、入队、状态机、归档、回执） | QQ 机器人接入、多模态识图 |
| 一键启停脚本、配置文件模板、自检脚本、脱敏扫描工具 | 看护自愈体系（watchdog / 锁解除 / 进程守护） |

## 三步跑起来

```bat
:: 1) 进入本目录
cd examples\xihe-lite

:: 2) 启动（首次会自动把 config.example.json 复制成 config.json）
start.bat

:: 3) 浏览器打开控制台
::    http://127.0.0.1:18910/
```

停止：双击 `stop.bat`，或关闭启动窗口 / 按 Ctrl+C。

## 默认是安全的模拟模式

- `printer.backend` 默认 `mock`：**不会向任何真实打印设备提交内容**，
  结果归档进 `runtime/staging/_模拟/`，绝不冒充「已打印」。
- 路由默认四级降级：未配置云端 Key 时日志会写「未配置 api_key」并自动走本地兜底，
  保证演示不空转、也不发任何网络请求。
- 想接真实能力：只改 `config.json`（`printer.backend` 改 `win32` / 填 `router.api_key`），
  不用改任何代码。真实配置已被 `.gitignore` 排除，不会误提交。

## 自检

```bat
python selftest.py      :: 23 项自检（含 HTTP、状态机、路由降级、隐私扫描）
```

## 目录结构

```
examples/xihe-lite/
├─ start.bat / stop.bat          一键启停
├─ config.example.json           配置模板（无任何凭据）
├─ requirements.txt              仅真机打印需要额外依赖
├─ selftest.py                   自检脚本
├─ print_cli.py / print_daemon.py 打印链路命令行与守护入口
├─ tools/privacy_scan.py         脱敏扫描工具
└─ xihe/
   ├─ console/                   控制台（HTTP + 网页看板）
   ├─ router.py                  四级路由骨架
   └─ printing/                  打印调度（后端 / 驱动 / 状态机 / 转换 / 暂存 / 前置自检）
```

## 说明

- 本目录代码已通过脱敏自检（无绝对路径、无真实 IP、无凭据、无内部代号）。
- 缺省只监听本机回环地址，不含任何鉴权凭据；如需对外暴露请自行加固。
