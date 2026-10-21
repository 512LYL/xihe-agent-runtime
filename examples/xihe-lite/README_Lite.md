# xihe-lite · 精简可运行版（丐版运行底座）

> 代号：**xihe-lite**｜定位：**去业务化，只保留底层核心骨架**
> 一句话：**跑不出真实加速卡的 314 tok/s，也没有真实物理打印机出纸 —— 但两者的接口调用逻辑、
> 降级链路、状态机与日志在这里一模一样，换掉一个后端就能接真。**

---

## 一、定位声明

**本目录是「曦和」项目的精简版（lite），即丐版运行底座**：去业务化，只保留底层核心骨架 ——
**极简控制台 + 智能路由骨架 + 打印调度守护** 三个模块，构成一条可复现的最小闭环。

它**刻意不包含个人业务**：音乐播放、壁纸引擎、真实 QQ 桥接、真实私有智能体编排，
一律不在本仓库。这些组件要么依赖特定硬件（加速卡实机 + 19 GB 级权重），要么依赖私有服务
（消息平台账号、内网隧道），开源出去既无法复现、也会带出凭据。

**能力口径**：本版**不追求与生产版功能对等**，只证明「这条闭环在干净机器上真能跑通、
代码看得懂、结果可复现」。性能与规模相关的结论以生产版实测为准；本版只对**接口逻辑与工程可得性**负责。

### 丐版 vs 满血版

| 本仓库已包含（丐版，可复现） | 满血版另有（依赖特定硬件/私有服务，不在开源范围） |
|---|---|
| **极简控制台**：单进程 Web 看板，服务健康、打印队列、一键启停 | **多端口分页控制台**（前台/后台/主页/硬件/打印 5 页）与硬件监控面板 |
| **智能路由骨架**：四级降级链 + Mock 通道 + `[ROUTE]` 流转日志 | **真实多模型路由**：云端大模型 / 本地小模型 / 自部署大参数通道，含预算预检与真实计费 |
| **打印调度守护**：队列、状态机、5 种结局、暂存归档（模拟模式默认） | **真实打印链路**：Windows 驱动渲染、双面/纸源、OWS 权威状态、真机出纸判定 |
| **四级路由的界面与接口**：看板下发测试指令、`router.log` 流转 | **国产 GPU 自部署大参数通道**：加速卡实机 + 19 GB 级权重 + 推理框架适配 |
| **服务注册表**：任意 `python -m <module>` 纳入启停与判活 | **看护自愈体系**：进程保活、卡死重启、日志水位压缩、异常告警外发 |
| **配置模板 + 一键启停 + 23 条离线自检 + 脱敏扫描** | **私有业务组件**：QQ 机器人接入、音乐/壁纸/桌宠、多智能体编排 |
| **零绝对路径 / 零真实 IP / 零凭据 / 零内部代号**（见 §八） | **生产编排**：内网隧道、计划任务、多机协同，以及与之配套的真实凭据 |

> 右列不是「没做完」，而是**按需裁剪**：要有对应的硬件与服务才谈得上复现。
> 左列每一条都在 `selftest.py` 里有对应用例，或可用 §三 的三步命令当场验证。

---

## 二、三大核心模块

### 1) 极简控制台 Lite Console（`xihe/console/`）

单进程、单端口（默认 18910）。看板一屏看完：

* **服务健康**：每个受管服务一张卡（运行中 / 端口 / pid / probe），顶部汇总 `健康 x/y`
* **打印队列状态**：队列任务数、EWS 状态、6 个暂存计数（待打印/处理中/未确认/已打印/模拟归档/失败）
* **下发一条文本测试指令**：输入文本 → 走四级路由 → 看 `[ROUTE]` 流转与最终命中
* **一键启停**：一个按钮（在跑就停、没跑就起），也可逐服务启停/重启/看日志

### 2) 智能路由骨架 Router Skeleton（`xihe/router.py`）

把四级路由逻辑从业务里剥出来，**不连任何大模型，只保留骨架**：

```
tier1  cloud_api       云端 API          需在 config.router.api_key 填 Key；未填 → 视为不可用
tier2  local_llm       本地大模型（兜底）  默认可用 ← 日志会打「切换至本地兜底」
tier3  remote_compute  异地算力          默认可配成不可达，用来演示继续降级
tier4  offline_rule    离线规则          永不失败（最后一道保障，保证演示不空转）
```

每次路由都写 `runtime/logs/router.log`，同时把同样的行回给看板/CLI/HTTP 接口：

```
[ROUTE] 开始路由：输入 9 字符，共 4 级（cloud_api → local_llm → remote_compute → offline_rule）
[ROUTE] tier1 云端 API 尝试中…
[ROUTE] tier1 云端 API 不可用：未配置 api_key（本版不联网，按 mock 处理）
[ROUTE] 切换至本地兜底（tier2 local_llm）
[ROUTE] tier2 本地大模型（兜底） 成功（0 ms）
[ROUTE] 完成：实际走 本地大模型（兜底）（总耗时 2 ms）
```

想模拟"云端可用"或"连兜底都挂"，改 `config.json → router.mock_available` 即可，
不用改代码；真实接入时只替换 `_call_cloud / _call_local / _call_remote` 三个函数，
路由顺序、降级与日志都不用动。

### 3) 打印调度守护 Print Daemon（`xihe/printing/` + `print_daemon.py` + `print_cli.py`）

* `print_daemon.py`：独立进程，盯 `runtime/staging/` 的 ready 队列，串行处理，
  暴露只读 `/status`（默认 18911），崩溃残留会自动回迁
* `print_cli.py`：`status / jobs / staging / add / test / clear / retry / preflight`
* 收到指令后输出 **「已进入打印队列：<文件名>」**，随后由守护走完判定与归档
* **默认模拟模式（mock）**：不接触任何设备，结果进 `_模拟/`；
  只有把 `printer.backend` 显式改成 `win32`（并填 `printer.name`）才会真的打印

判定仍是生产版那五档（这是整条链路最值钱的部分）：

| 结果 | 归档 | 判据 |
|---|---|---|
| `printed` | `_已打印/` | 见过 processing + 队列清空 + 无硬错误 + 静置复核 |
| `simulated` | `_模拟/` | 同上，但走模拟后端（未接触设备） |
| `submitted` | `_队列/` | 队列清空但从没见过 processing / 超时不确定 |
| `failed` | `_失败/` + `.err.txt` | 硬错误（卡纸/门开/无墨粉/离线…）且 EWS 也说不出「可用/在打」 |
| `cancelled` | `_失败/` + `.err.txt` | 命中 clear 取消信号（先写信号再删队列，顺序不能反） |

---

## 三、安装与运行（三步）

```bat
:: 1) 装依赖（极简；模拟模式其实一行都不用装）
python -m pip install -r requirements.txt

:: 2) 生成配置（start.bat 会自动做；也可以手动复制）
copy config.example.json config.json

:: 3) 双击 start.bat（或命令行 python -m xihe serve --open）
```

浏览器会自动打开 **http://127.0.0.1:18910/**（端口来自 `config.json → console.port`）。
停止：双击 `stop.bat`，或 `python -m xihe stop`。

**验收链路（一条命令一行，可照着做）**

1. 解压 → `start.bat`（自动生成 config.json）→ 控制台弹出
2. 在「测试指令」里输入 `帮我打印一张测试页` → 点【走智能路由】
3. 看板显示 `[ROUTE] 切换至本地兜底（tier2 local_llm）`，日志文件 `runtime/logs/router.log` 同款记录
4. 点【下发打印测试页】→ 暂存 `待打印` +1 → `处理中` → `模拟归档` +1
5. 点右上【前置自检】→ 一行中文就绪结论

> 无需修改任何代码；无需打印机；无需任何 Key。

---

## 四、目录结构

```
xihe-lite/
├─ start.bat / stop.bat        一键启停（找 Python → 建 config.json → 起控制台）
├─ README_Lite.md              本文件
├─ requirements.txt            极简依赖（仅真机打印才需要）
├─ config.example.json         配置模板：api_key / 打印机 IP 的填写位都标好了
├─ print_daemon.py             打印守护入口（= python -m xihe.printing.daemon）
├─ print_cli.py                打印命令行（add/test/clear/retry/status…）
├─ selftest.py                 23 条离线自检（不接触打印机）
├─ tools/privacy_scan.py       脱敏扫描（盘符/IP/凭据/内部代号）
└─ xihe/
   ├─ config.py                配置加载：默认值 + 相对路径 + 环境变量覆盖
   ├─ router.py                ★ 智能路由骨架（四级 + Mock + [ROUTE]）
   ├─ logset.py                自持句柄日志 + 大小轮转
   ├─ cli.py                   serve/stop/status/route/preflight/add/retry/clear/demo/selftest/where
   ├─ console/
   │  ├─ server.py             单端口 HTTP（看板 + 15 个接口）
   │  ├─ services.py           服务注册表（端口/HTTP/pid 判活 + 启停）
   │  ├─ dispatch.py           打印调度（入队 / 状态快照 / 取消信号 / 仓库）
   │  └─ web/index.html        看板前端（原生 JS，2 秒轮询）
   └─ printing/
      ├─ states.py             状态口径 + 5 种结果 + 结果→目录映射 + 中文回执
      ├─ staging.py            暂存状态机（认领/归档/取消信号/重投/清理）
      ├─ backend.py            真机后端 / 模拟后端 / 自检替身
      ├─ converter.py          Office→PDF、多图→A4 PDF
      ├─ win_printer.py        唯一真正接触打印机的地方（真机模式才走到）
      ├─ driver.py             链路编排（等可用→提交→出纸判定→归档）
      ├─ daemon.py             打印守护
      ├─ preflight.py          演示前置自检（只读，一行结论）
      └─ repo.py               打印仓库 + 演示样本
```

---

## 五、命令行与接口

| 命令 | 作用 |
|---|---|
| `python -m xihe serve [--port N] [--open]` | 起控制台 |
| `python -m xihe route "文本"` | 下发一条文本测试指令，打印四级降级过程 |
| `python -m xihe stop` | 停控制台 + 全部受管服务 |
| `python -m xihe preflight [-v] [--json]` | 演示前置自检（只读，退出码 0/1/2） |
| `python -m xihe selftest` | 23 条离线自检 |
| `python print_daemon.py [--once]` | 起打印守护 |
| `python print_cli.py test "文本"` | 造一条文本测试页并入队（输出「已进入打印队列」） |
| `python print_cli.py status` / `clear` / `retry <名>` | 打印侧常用操作 |
| `python tools/privacy_scan.py` | 脱敏扫描（19 条规则，零命中也算一道验收） |

HTTP（控制台单端口）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 状态看板 |
| GET | `/api/status` | 看板数据（健康 + 打印 + 路由最近结果） |
| GET | `/api/route/status` | 四级清单 + 最近一次路由 |
| POST | `/api/route/test` `{"text":"…"}` | ★ 下发文本测试指令（走四级路由） |
| POST | `/api/print/test` `{"text":"…"}` | ★ 下发打印测试页（入队） |
| POST | `/api/toggle` | 一键启停 |
| POST | `/api/svc/start` / `stop` / `restart` | 单个服务 |
| GET | `/api/repo` · POST `/api/repo/print` / `upload` / `delete` | 打印仓库 |
| POST | `/api/print/submit` / `clear` / `retry` | 打印调度 |
| GET | `/api/preflight` | 前置自检（20 秒缓存） |
| GET | `/api/log?id=printd&n=120` | 服务日志尾巴 |
| GET | `/api/config` | 配置摘要（`api_key` 已打码、路径已相对化） |

---

## 六、配置：Key 与打印机填哪里

`config.example.json` 里两处关键填写位，其余保持默认即可：

```json
"router": { "api_key": "", … }            // ★ 云端 API 的 Key 填这里；留空 = 该级不可用（走 mock）
"printer": {
  "backend": "mock",                       // mock（默认，不接触设备）/ auto / win32
  "name": "",                              // ★ 真机打印时填本机打印机队列名；留空 = 系统默认
  "ip": ""                                 // ★ 只有需要读打印机 EWS 状态时才填网络地址
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `console.port` | 18910 | 看板端口 |
| `printer.backend` | `mock` | 模拟模式，绝不碰设备；`auto`=有可用打印机才走真机；`win32`=强制真机 |
| `router.tiers` | 四级默认序 | 想调优先级就改这里 |
| `router.mock_available` | 云端✗ 本地✓ 异地✗ | 改 true/false 就能演示不同降级路径 |
| `daemon.port` | 18911 | 守护只读 `/status` |
| `paths.*` | `runtime/…` | 全部相对本目录；也可填绝对路径 |

**扩展服务**（让看板多管一个进程）：

```json
"services": [
  { "id": "myd", "name": "我的守护", "port": 18912,
    "module": "my_pkg.daemon", "args": ["--quiet"],
    "probe": "http://127.0.0.1:18912/status", "order": 2 }
]
```

---

## 七、自检

```bat
python selftest.py            :: 23/23，约 4 秒，全程不接触打印机
python tools/privacy_scan.py  :: 脱敏扫描（19 条规则）
```

覆盖：配置/路径 → 暂存状态机 → 模拟链路 → 真机口径归档 → 卡纸失败 → 缺纸软告警 →
EWS 压过 Windows 报错 → 从未 processing 不判成功 → clear 取消 → 结果映射 → 重投 →
自检一行结论 → 服务注册表 → 控制台 HTTP（含路由接口）→ 看板资产 → 全量编译 →
脱敏扫描 → 图片转 PDF → auto 降级 → **四级路由降级** → **离线规则兜底**。

---

## 八、脱敏声明（四条红线，产出后逐条自查）

| 红线 | 落实方式 |
|---|---|
| **零绝对路径** | 代码与文档里不出现任何盘符路径；运行期目录全部相对 `runtime/…`，由配置解析 |
| **零真实 IP** | 不出现任何 IPv4 字面量；打印机地址留空，由使用者自己填 |
| **零凭据** | 无 API Key、无 QQ 号、无密码、无 Token；`config.example.json` 只有空占位 |
| **零内部代号** | 不用任何内部角色名，涉及角色时统一写作 `MainController` / `SubAgent_A` |

`python tools/privacy_scan.py` 会扫盘符路径、IPv4、`sk-`/`AKIA`/`ghp_` 前缀密钥、
明文口令、QQ 号形态数字、内部代号等 19 条规则；**当前结果零命中**。
运行期产物（`runtime/`、`config.json`、`__pycache__/`）已在 `.gitignore` 里排除。

---

## 九、已验证环境

* Windows 11 + Python 3.12
* `python selftest.py` → **23/23 passed**
* 控制台实跑：看板 200、`/api/status`、`/api/route/test`（`[ROUTE]` 流转）、
  `/api/print/test`（已进入打印队列）、`clear`→`cancelled`、一键启停 全部实测
* 真机打印：**未验证**（本版默认模拟模式，不接触任何设备）

---

> **这只是精简版**：完整多智能体生产版（多模型真实路由、自部署大参数通道、QQ 接入、
> 看护自愈、音乐壁纸等）请参考**决赛演示**。
