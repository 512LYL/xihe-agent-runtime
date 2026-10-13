# 曦和 · AI Agent 运行底座（Xihe Agent Runtime）

> **面向国产算力的多通道 AI Agent 运行底座**
> 核心叙事：**上层能力不变，下层算力可换**（＝算力来源无关）
> 三通道智能路由 · 断网无感降级 · 复杂任务暂停续传 · 多层自愈看护

[![License: MulanPSL-2.0](https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%2011-lightgrey)
![Tested on](https://img.shields.io/badge/Tested%20on-MetaX%20C500%20%7C%20MACA%203.5.3.20%20%7C%20vLLM%200.17.0-orange)

**许可证**：[木兰宽松许可证第 2 版](LICENSE)（Mulan PSL v2，OSI 认证）
**主源（GitHub）**：`https://github.com/LYL512/xihe-agent-runtime` ｜ **平台镜像（GitLink）**：待建（见 [docs/08_仓库拓扑与落地形态.md](docs/08_仓库拓扑与落地形态.md)）
**比赛版历史仓库（封存留证）**：[GitLink `LYL512/Xihe-AI-Assistant`](https://www.gitlink.org.cn/LYL512/Xihe-AI-Assistant)

---

## 一、项目简介

曦和（Xihe）是一套**面向国产算力的多通道 AI Agent 运行底座**。

它解决的问题是：**当推理算力来自完全不同的地方**——云端 API、本机小模型、国产 GPU 上的自部署大模型——**上层 Agent 的能力与工作流必须保持完全不变**。这一层「完全不动下层算力」的桥接与调度，就是本项目的核心原创资产。

| 层 | 组件 | 说明 |
|---|---|---|
| 入口层 | QQ → NapCat(3002) → QQ 桥(8080)；浏览器 webchat → 直连网关 | 多模态入口：桥里提取图片转 `image_url` |
| 核心调度 | OpenClaw Gateway（开源框架复用） | 会话管理 · MCP 工具调度 · 安全审批 |
| **桥接层 ★** | **应龙桥接器**（[`src/bridge/yinglo_bridge.py`](src/bridge/yinglo_bridge.py)） | **三通道智能路由 · 断网无感降级 · 复杂任务暂停续传 · 协议双向适配** |
| 算力层 | ① 云端 API ｜ ② 本地 Ollama 4B ｜ ③ 沐曦 C500 自部署 32B | 三通道并列可切换 |
| 看护层 | 进程级探活 · 会话级自愈 · 硬件级监控 · C500 自动报警 | 多层自愈看护体系（[`src/health/`](src/health/)） |

> 🖼 **架构总图（成图）**：[`assets/architecture/应龙桥接器架构_五层三通道_成图.png`](assets/architecture/应龙桥接器架构_五层三通道_成图.png)
> —— 图内主标题《应龙桥接器架构：上层能力不变，下层算力可换》，五层结构 + 三通道并列可切换，
> 并把**定性口径**（通道①＝多源韧性对照项／通道③＝核心国产算力实证通道）直接画在图内右上角。
> 逐层文字说明见 [docs/01_架构与五层结构.md](docs/01_架构与五层结构.md)。

**一句话**：桥接层做协议适配与智能路由，模型可替换、可降级、可扩展；已在**沐曦 C500 国产 GPU** 上完成 32B 模型自部署、接入与**两轮重载压测**。

---

## 二、功能说明

### 2.1 三通道智能路由（本项目核心）

| 通道 | 后端 | 定位（定性口径见 §三） | 触发条件 |
|---|---|---|---|
| **①** | 云端 API：DeepSeek / 沐曦 **Qwen3.5-27B** | **多源韧性对照**（对照组，非主力叙事） | 联网且未点名通道③ |
| **②** | 本地 Ollama `qwen3.5:4b` | 断网兜底，零成本 | 断网 + 简单任务 |
| **③** | **沐曦 C500 自部署 32B**（`xihe-32b`，vLLM 0.17.0） | **核心国产算力实证通道** | 点名 `model=xihe-32b`，或"联网 + 通道③在线 + 复杂任务/长上下文" |

**为什么必须在桥接层做**：Gateway 原生 fallback 只捕获 HTTP 层错误（4xx/5xx/超时），**网络层异常**（`ECONNREFUSED` / `ENOTFOUND`）会直接抛给前端、不进重试链。所以桥接器做网络探测（`ai.gitee.com:443` TCP，1 秒结果缓存）→ 自己决定后端。

**不静默降级原则**：通道③未启用时，桥接器**明确回落云端并告警**，**绝不静默降到本地 4B**。

### 2.2 断网续传（复杂任务暂停/续传）

断网时桥接器按三维度评估任务复杂度（token 数 / 工具数 / 消息长度 + 复杂意图关键词），任一超标即判为复杂任务：

- **简单任务** → 本地 4B 直接答；
- **复杂任务** → **暂停并保存现场**（`/v1/paused-tasks`），后台线程每 30 秒探测网络，恢复后**断点续传**；
- **长上下文 + 断网** → 不做不兑现的承诺，改为诚实的简短回复（`LONG_CONTEXT_HONEST_CAP = 8000 tokens`）。

### 2.3 多智能体协同容灾（DSH 主节点 + 龙虾备份节点）

系统以 **DSH 主节点**承担日常调度与长任务，并以 **龙虾（OpenClaw）备份节点**做协同容灾：两个节点共享同一套应龙桥接器与通道配置，主节点不可用时由备份节点接管会话入口与看护职责。看护层的「死法 → 负责者」映射保证每一类故障都有明确接管者：

| 故障形态 | 负责者 |
|---|---|
| 会话写锁卡死 | [`src/health/gateway_unstick.py`](src/health/gateway_unstick.py)（自动解卡 + 两级水位压缩） |
| QQ 掉线 / 被踢 | [`src/health/watchdog.py`](src/health/watchdog.py)（探活 + 拉起） |
| 看护自身死掉 | 守护链 + **OS 级原子单例守卫**（防双实例） |
| 网关进程死 | 守护链端口探活（判 DOWN 2 轮 ≈90 s 后拉起） |
| C500 实例失联 / 隧道漂移 / 忘关机 | 通道③ 探活 + 报警（零推理消耗） |

### 2.4 多模态识图

完整链路：QQ 收到的图片 → 提取 → base64 → 以 `image_url` 块送入模型 → 返回识别结果。含工程护栏：单图 ≤ 32 MB、请求体 ≤ 48 MB、单条最多 8 张、取不到图**不崩**（保留文字继续）。见 [`src/entry/qq_multimodal_gateway.py`](src/entry/qq_multimodal_gateway.py)。

### 2.5 可观测与自愈看护

两级会话水位（500 KB 检测 / 600 KB 强制摘要瘦身）、进程探活与自动拉起、每日 0:00 / 12:00 资源自检（残留脚本判定：白名单外 + 运行 >4 h + CPU 累计 >2 h；**只发现不擅自杀进程**）、通知防刷屏（同类冷却 + 全局配额 + 自动退出）。

> ⚠️ **如实说明**：看护层的完整生产脚本依赖若干未随包发布的内部模块，本仓库提供的是**按生产参数重写的自包含可运行版本**（参数逐条对齐台账）。差异与理由见 [docs/09_初稿与生产的差异声明.md](docs/09_初稿与生产的差异声明.md)。

---

## 三、模型与算力环境

### 3.1 精确环境（照抄实测值）

| 项 | 值 |
|---|---|
| GPU | **沐曦 C500（MetaX C500）**，物理显存 **65536 MiB（64G）**，本实例为软件切片：**Vram Quota 32000 MiB（32G 切片）、Compute 50%** |
| 软件栈 | **MACA 3.5.3.20**（驱动 3.8.30 / BIOS 1.33.5.0 / mx-smi 2.2.12） |
| 推理引擎 | **vLLM 0.17.0**（沐曦适配版，`VLLM_USE_MXGPU=1`），OpenAI 兼容接口 |
| 模型 | **Qwen/Qwen2.5-32B-Instruct-AWQ**（AWQ 4bit，19 GB，16 文件）→ 部署路径 `/models/Qwen2.5-32B-AWQ`，服务名 `xihe-32b` |
| 启动命令 | `vllm serve /models/Qwen2.5-32B-AWQ --served-model-name xihe-32b --port 8000 --gpu-memory-utilization 0.85 --max-model-len 8192 --trust-remote-code` |
| 通道③ 接入 | SSH 隧道 本机 `18000` → 实例 `8000`（模板见 [`scripts/tunnel_c500.bat`](scripts/tunnel_c500.bat)） |
| 本机兜底 | Ollama `qwen3.5:4b`（2.55 GB） |
| 操作系统 | Windows 11 ｜ Python 3.12+ ｜ Node.js 24.x（OpenClaw 要求 ≥ 22.19.0） |

> 上下文口径提醒：模型**原生**上限 131,072 tokens（128K），但 32G 切片下 **32K 是显存合理上限**（32K 需 8.44 GiB KV cache，实测占用 29516/32000 MiB）。单次生成上限 8192 tokens。桥接侧超限前置拦截阈值 `C500_MAX_INPUT_TOKENS = 24000`。

### 3.2 实测数据（通道③）

| 指标 | 值 | 出处 |
|---|---|---|
| `/v1/models` 探活 | HTTP 200 / **1.07 s** | 2026-09-15 打通实测 |
| 中文对话 | HTTP 200 / **1.91 s** | 同上 |
| 3 轮平均延迟 | 1.25 / 1.04 / 1.20 s → **均值 1.17 s** | 同上 |
| 端到端·点名 `model=xihe-32b` | HTTP 200 / **4.16 s** | 同上 |
| 端到端·复杂任务自动路由 | HTTP 200 / **19.68 s** | 同上（桥接日志 `C500 non-stream response: 18993ms`） |
| 稳态压测（16 并发，持续供料 83.4 分钟） | 整窗 GPU util **80.5%**；有负载均值 82.2%；峰值生成 **314.7 tok/s**；HBM 峰 **861 GB/s**；功耗峰 **338 W**（满配 350 W） | 2026-10-02 |
| 可靠性 | POST 1579 次 → `finish_reason`：stop 1529 / length 17 / **abort 0 / error 0 / OOM 0** | 2026-10-02 |
| 调度改造效果 | 管家轮询 300s→30s：整窗 util **26.8% → 80.5%（≈3×）**，占空比 **21.3% → 74.1%** | 09-30 vs 10-02 对比 |
| 已定位瓶颈 | 主因 `max_num_batched_tokens=2048`（调度步预算偏小 → 排队）；辅因 KV 容量 34,544 tokens 结构性超容 | 2026-10-02 |

> ⚠️ 延迟指标照实写：TTFT p50 20 s / p99 640 s，queue p50 15 s，e2e p50 40 s —— **瓶颈在排队，不在裸算力**（两轮生成吞吐峰值持平：314.8 vs 314.7 tok/s 即为证）。

### 3.3 ★ 合规与数据来源红线（防自伤，务必遵守）

> **本项目提交的全部调用日志与性能数据，均来自通道③（沐曦 C500 自部署 32B）。通道① 云端 API 仅作为多源韧性架构的对照项陈述，不用于本次实证数据。**

配套口径（写材料时逐条照做）：

1. **实证一律记通道③**；通道① 只作韧性对照 —— ❌ 不得把三通道**平铺成「多源编排」而不区分实证来源**（会自伤「国产算力适配」这一最被看重的维度）；
2. ❌ **不得**写成「买了云 API 额度」；
3. **成本必须双口径**（否则两个数字互相打脸）：**券消费总额 122.61 元**（09-21 余额 239.60 → 10-11 余额 116.9917）**≠ 当前实例机时 20h29m**；单价 2.50 元/小时；
4. **截图口径（2026-10-13 第 2 版）**：对外写「**23 张截图 / 6 组**」＝ **部署实证 21 张（5 组）**（验卡 3 / 拉模型 7 / 推理 4 / 打通与本地验证 4 / 排障过程 3）＋ **环境实证与平台台账 2 张（1 组）**（平台实例页 1 + C500 全量重载压测详细检测报告 1）；❌ **不写内部未剔除台账的「24 张」**；
5. **沐曦生态数字照抄官方措辞**（「超 6000 个国际主流**生态应用**」「超 1000 个模型的原生适配」），❌ 不写「6000+ 开源项目」「40+ AI 框架」，❌ 不引用 C500 的 TFLOPS（媒体两套口径冲突，裁定不引用）。

---

## 四、运行与部署方法

### 4.0 前置依赖

```powershell
# Python 依赖
pip install -r requirements.txt

# 环境变量：复制模板后填入你自己的值（真实密钥永不入库）
Copy-Item .env.example .env
```

### 4.1 通道②｜本地兜底（断网可用，零成本）

```powershell
# 1) 确认 Ollama 已拉起本地模型
ollama list                      # 应能看到 qwen3.5:4b
# 2) 启动应龙桥接器（本机 :11443）
python src/bridge/yinglo_bridge.py
# 3) 一键健康检查
python src/health/health_system_check.py
```

### 4.2 通道①｜云端（多源韧性对照项）

```powershell
# 在 .env 中填 MUXI_API_KEY / MUXI_PACKAGE_ID（占位符见 .env.example）
# 然后把 src/config/openclaw.template.json 复制为 openclaw.json 并设置：
#   OPENCLAW_STATE_DIR / OPENCLAW_INDEX
python src/entry/launch_gateway.py --port 18791 --verbose
```

> ⚠️ 通道①只作架构对照项陈述，**本次实证数据不取自该通道**（见 §3.3）。

### 4.3 通道③｜沐曦 C500 自部署 32B

```powershell
# 1) 建隧道：本机 18000 → 实例 8000（先编辑 scripts\tunnel_c500.bat 填入你自己的实例信息）
scripts\tunnel_c500.bat

# 2) 连通性自检（会打印实测延迟，可与 §3.2 基线对照）
python scripts\check_c500.py
python scripts\check_c500.py --all        # 三通道一起体检

# 3) 模型侧（在 C500 实例内执行）
vllm serve /models/Qwen2.5-32B-AWQ --served-model-name xihe-32b --port 8000 `
     --gpu-memory-utilization 0.85 --max-model-len 8192 --trust-remote-code
```

### 4.4 看护层

```powershell
python src\health\watchdog.py --once          # 跑一轮探活
python src\health\watchdog.py --selfcheck     # 只做一次资源自检（不杀进程）
python src\health\watchdog.py                 # 常驻（默认 120s 轮询）
python src\health\gateway_unstick.py --once --json   # 会话级自愈扫描（默认只报告）
```

### 4.5 公开前 / 推送前自检

```powershell
python scripts\scan_secrets.py          # 敏感信息 + 大文件全量扫描（红线闸门）
pwsh scripts\push-all.ps1 -DryRun       # 只体检不推送；正式推送见脚本头注释
```

---

## 五、示例输入输出

> 以下为**真实调用样例**（通道③，2026-09-15 打通当天实测）。命令行里的域名为占位符，请换成你自己的隧道地址。

### 5.1 探活：`GET /v1/models`

```bash
curl -s http://127.0.0.1:18000/v1/models -H "ngrok-skip-browser-warning: true"
```

```json
{
  "object": "list",
  "data": [
    { "id": "xihe-32b", "object": "model", "created": 1757894400, "owned_by": "vllm",
      "max_model_len": 32768 }
  ]
}
```

实测：**HTTP 200 / 1.07 s**。

### 5.2 对话：`POST /v1/chat/completions`（非流式）

```bash
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "ngrok-skip-browser-warning: true" \
  -d '{
    "model": "xihe-32b",
    "messages": [{"role": "user", "content": "你是谁？"}],
    "max_tokens": 64,
    "stream": false
  }'
```

```json
{
  "id": "chatcmpl-8f2c1a0e9b",
  "object": "chat.completion",
  "model": "xihe-32b",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "我是Qwen，由阿里云开发的大型语言模型助手。" },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 41, "completion_tokens": 9, "total_tokens": 50 }
}
```

实测：**HTTP 200 / 1.91 s**。

### 5.3 工具调用（通道③ 升级后，`finish_reason = tool_calls`）

```json
{
  "id": "chatcmpl-tool-1",
  "object": "chat.completion",
  "model": "xihe-32b",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_0",
        "type": "function",
        "function": { "name": "get_weather", "arguments": "{\"city\": \"北京\"}" }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

> 启动参数需加 `--enable-auto-tool-choice --tool-call-parser hermes`。

### 5.4 反例（诚实的失败样例）：上下文超限

```json
{
  "error": {
    "message": "This model's maximum context length is 32768 tokens. However, you requested 28673 tokens in the messages and 4096 tokens in the completion...",
    "type": "BadRequestError",
    "code": 400,
    "param": "input_too_long"
  }
}
```

> 2026-10-02 轮共出现 **33 个 400，全部是这类上下文超限**（28673+4096 > 32768，超 1 token），属**客户端参数问题、非引擎缺陷**。桥接侧的对策是**超限前置拦截**（`C500_MAX_INPUT_TOKENS = 24000`）+ **无状态裁剪**（消息 4→2 条）。

### 5.5 本地兜底（通道②）：`POST http://127.0.0.1:11434/v1/chat/completions`

```json
{
  "model": "qwen3.5:4b",
  "messages": [{"role": "user", "content": "ping"}],
  "max_tokens": 16, "stream": false
}
```

> 本地 4B 为 GGUF 模型，**不支持原生 tools 参数** → 桥接器剥离工具定义并注入 system prompt（最多 4 个，优先 file/read/write/search 类）。

### 5.6 一键复现

```powershell
python scripts\check_c500.py --rounds 3     # 直接跑出 §5.1/§5.2 的延迟数字
```

---

## 六、参考来源说明

### 6.1 开源框架与上游项目复用声明

| 组件 / 来源 | 复用方式 | 说明 |
|---|---|---|
| **OpenClaw**（Agent 框架） | **框架复用**（不是本项目原创） | 提供入口层与核心调度（会话管理 / MCP 工具调度 / 安全审批）。本项目**原创的是桥接层与看护层**——即那层「完全不动下层算力」的适配与路由。 |
| **vLLM**（推理引擎） | 直接使用沐曦适配版 | `VLLM_USE_MXGPU=1`；本项目贡献方向为「Windows/WSL 下 C500 单卡 32G 切片跑 32B AWQ 注意事项」（**待提交，见缺口清单**）。 |
| **MetaX MACA / mx-smi** | 直接使用 | 硬件与驱动栈，非本项目代码。 |
| **Qwen2.5-32B-Instruct-AWQ** | 模型权重（Apache-2.0） | 来源 ModelScope / HuggingFace；本项目做的是**部署、量化后运行与调度适配**，不是模型训练。 |
| **NapCat / aiocqhttp** | 直接使用 | QQ 入口链路。 |
| **Ollama** | 直接使用 | 本地兜底推理。 |

> **上游回馈意向**：向 `MetaX-MACA/vLLM-metax` 提交带压测基线的 Issue（主题：`max_num_batched_tokens=2048` 排队与 KV 容量 34,544 tokens 结构性超容）与一份文档级 PR。**状态：待提交**（受网络可达性影响，照实登记）。

### 6.2 为什么选木兰宽松许可证第 2 版（Mulan PSL v2）

- 它是 **OSI 认证**、列入 OSI 官方许可证目录的**国产开源许可证** —— **与国产开源生态定位一致**；
- **宽松许可**（无传染性），便于二次开发与被集成；
- 与「面向国产算力」的叙事**天然同频**；
- 配套动作：`LICENSE` 头部项目名已改为 `Xihe Agent Runtime（曦和 · AI Agent 运行底座）` 并补 `SPDX-License-Identifier: MulanPSL-2.0`。
  > ❌ **不为了通过平台自动识别而更换许可证全文** —— 那等于事实上改了许可证。

### 6.3 外部规范与资料链接

- [OSI · Mulan PSL v2](https://opensource.org/license/mulanpsl-2.0) ｜ [木兰许可证全文](http://license.coscl.org.cn/MulanPSL2)
- [沐曦开发者社区](https://developer.metax-tech.com/activities/5)
- [GitHub · 大文件限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)（**>100 MB 单文件拒收**，本项目红线更严：单文件 >10 MB 不入库）
- 演示视频（**外链，不入库**）：B 站 `BV1EEMJ63E29`
  > 视频录制于 2026-06，当时定位为「本地 AI 办公系统」；项目定位已于 2026-09 演化为**多通道 AI Agent 运行底座** —— 观看时请以本 README 的定位为准。

---

## 七、目录结构

```
xihe-agent-runtime/
├── README.md                    ← 本文件（评审首屏六要素）
├── LICENSE                      ← 木兰宽松许可证第 2 版（全文）
├── .gitignore                   ← ★ 第一道防线：媒体/密钥/大文件护栏
├── .gitattributes               ← 「大二进制不入库」策略声明（不上 LFS）
├── .env.example                 ← 环境变量占位符（真实密钥永不入库）
├── requirements.txt
├── src/
│   ├── bridge/yinglo_bridge.py        ← ★ 应龙桥接器（三通道智能路由核心，1741 行）
│   ├── entry/qq_multimodal_gateway.py ← 多模态入口（图片 → image_url）
│   ├── entry/launch_gateway.py        ← Gateway 启动器（含 orphan-self-pid 规避）
│   ├── health/watchdog.py             ← 看护层①：进程探活 / 资源自检 / 单例守卫
│   ├── health/gateway_unstick.py      ← 看护层②：会话级自愈 + 两级水位压缩
│   ├── health/health_system_check.py  ← 一键健康检查
│   └── config/openclaw.template.json  ← Gateway 配置模板（无外部云端 provider）
├── scripts/
│   ├── tunnel_c500.bat          ← 通道③ SSH 隧道模板（占位符，零真实信息）
│   ├── check_c500.py            ← 通道③/三通道 连通性与延迟自检
│   ├── scan_secrets.py          ← 公开前最后一道闸：敏感信息 + 大文件全量扫描
│   └── push-all.ps1             ← 单一上游 + 一推两发（模板化）
├── docs/                        ← 架构 / 三通道机制 / 九条踩坑根因 / 合规声明 / 开发历史
├── assets/                      ← 23 张截图（6 组）+ 性能图表（SVG）
└── .github/ISSUE_TEMPLATE/      ← Bug / Feature / 算力适配反馈
```

---

## 八、演进路径

> **项目早期为本地 AI 办公系统，随算力需求增长，重构为适应国产算力 C500 的多通道 Agent 运行底座。**

| 阶段 | 时间 | 里程碑 |
|---|---|---|
| ① 本地办公系统 | 2026-04 ~ 2026-05 | 本地 AI 办公自动化系统上线；双账号权限隔离；桥接器雏形（670 行） |
| ② Agent 化 | 2026-05 ~ 2026-06 | 接入 OpenClaw 框架；智能路由与断网降级；MCP 工具治理；桥接器 1769 行 |
| ③ 国产算力接入 | **2026-09-15** | **通道③打通**：C500 验卡 → 32B-AWQ → vLLM 0.17.0 → 隧道 → 桥接接入（一天内全链路） |
| ④ 压测与优化 | 2026-09-30 / 10-02 | 两轮 16 并发重载压测；整窗 GPU util 26.8% → **80.5%**；瓶颈定位到调度预算与 KV 容量 |
| ⑤ 运行底座成型 | 2026-10 | 看护层补盲、单例守卫原子化、定位口径冻结；本仓库作为**基金版门面**建立 |

历史证据（12 次提交 / T01–T33 开发台账 / 8 个 Issue 索引）见 **[docs/dev-history.md](docs/dev-history.md)**。

---

## 九、文档导航

| 文档 | 说明 |
|---|---|
| [docs/01_架构与五层结构.md](docs/01_架构与五层结构.md) | 五层结构逐层说明 + 一次请求怎么走 |
| [docs/02_三通道机制.md](docs/02_三通道机制.md) | 三通道路由判定、回落链、探活与超限拦截 |
| [docs/03_九条踩坑根因.md](docs/03_九条踩坑根因.md) | 9 条真实工程坑的根因与修复（工程韧性证据） |
| [docs/04_部署指南.md](docs/04_部署指南.md) | 环境搭建、端口分配、双账号隔离 |
| [docs/05_合规声明.md](docs/05_合规声明.md) | 合规逐条对照（含数据来源红线） |
| [docs/06_脱敏与删除记录.md](docs/06_脱敏与删除记录.md) | 本次公开前做了什么脱敏、删了什么、为什么 |
| [docs/07_证据索引.md](docs/07_证据索引.md) | 23 张截图（6 组）+ 压测数据的逐项对应 |
| [docs/08_仓库拓扑与落地形态.md](docs/08_仓库拓扑与落地形态.md) | 一主源 + 双 GitLink 仓；单向推纪律与扩展性 |
| [docs/09_初稿与生产的差异声明.md](docs/09_初稿与生产的差异声明.md) | 诚实边界：哪些是原样、哪些是重写、哪些还没做 |
| [docs/10_大文件哈希清单.md](docs/10_大文件哈希清单.md) | 不入库的大件（PPT / 视频 / 大日志）的 **SHA-256 哈希引用** |
| [docs/dev-history.md](docs/dev-history.md) | 12 次提交历史 + T01–T33 开发台账 |
| [docs/PPT_待补.md](docs/PPT_待补.md) | **PPT 待补**占位说明 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献方式（**DCO `Signed-off-by`，不引入 CLA**） |
| [SECURITY.md](SECURITY.md) | 安全策略（**密钥与算力券码绝不入库**） |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

---

## 十、作者与致谢

**崔克莉**（GitLink: [@LYL512](https://www.gitlink.org.cn/LYL512)）—— 本作品由个人独立完成。

从架构设计、应龙桥接器开发、国产 GPU（沐曦 C500）部署，到看护体系与工程细节打磨，均为独立实践。欢迎通过仓库 Issue 交流与指正。

> **如实说明（冷启动）**：本项目当前社区处于冷启动阶段（GitHub / GitLink 侧 star、fork、贡献者均为 0），定位为**从生产自用走向社区共建**。已建立的是**可被第三方核验的工程痕迹**：真实提交历史、8 个 Issue 记录、两轮压测原始数据（4 个 CSV）、23 张截图（部署实证 21 + 环境实证与平台台账 2）—— 而不是数字。
>
> 本仓库为**基金申报版门面**（干净入场，不含任何大二进制与密钥）。比赛期历史仓库 `LYL512/Xihe-AI-Assistant`（702.6 MB，含大体积演示素材）**已封存保留、可随时回退查看**。
