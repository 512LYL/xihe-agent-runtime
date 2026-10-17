# C500 压测原始数据（perf_csv）

本目录存放**沐曦 MetaX C500** 上 vLLM 0.17.0 + Qwen2.5-32B-Instruct-AWQ 的**原始性能采样 CSV**，供第三方独立复算本仓库与申报材料中引用的所有吞吐 / 利用率 / 延迟指标。

## 文件清单

| 文件 | 大小 | 时段 | 说明 |
|---|---|---|---|
| `perf_loadtest_20260930.csv` | 236,064 B | 2026-09-30 21:51 → 22:35 | 第一轮重载压测（16 并发接入） |
| `perf_round2.csv` | 35,627 B | 2026-09-30 22:57 → 23:04 | 第二轮短窗复测 |
| `perf_20260930_203004.csv` | 105,130 B | 2026-09-30 19:54 → 21:30 | 首轮基线采样 |
| `perf_20261002_200636.csv` | 623,315 B | 2026-10-02 19:27 → 21:56 | **第二轮稳态长压（持续 83.4 分钟，本材料主引用）** |

合计 ≈1.0 MB。

## 字段说明

**核心性能列**

- `ts` 采样时间戳；`elapsed_s` 距压测开始的秒数
- `gen_tokens_total` / `prompt_tokens_total` 累计生成 / 提示 token 数
- `chat_requests_total` 累计请求数
- `gen_tps` 生成吞吐（tok/s）← **峰值 314.84 tok/s 即由此列取得**
- `prompt_tps` 预填充吞吐（tok/s）
- `chat_rps` 请求速率（req/s）
- `running` / `waiting` 正在运行 / 排队等待的请求数
- `kv_perc` KV Cache 占用百分比（**峰值 100%，对应 34,544 容量上限**）
- `preemptions_total` / `preemptions_delta` 抢占次数（累计 / 增量）

**延迟列（毫秒）**

- `ttft_*` 首 token 延迟（mean / p50 / p95）
- `e2e_*` 端到端延迟（mean / p50 / p95）
- `itl_mean_ms` 平均 token 间隔；`queue_mean_ms` 平均排队时长；`prefill_mean_ms` 平均预填充时长

**收尾状态列**

- `fin_stop` 正常结束；`fin_length` 触达长度上限；`fin_abort` 中断；`fin_error` 报错

**GPU 列**（后三个文件含）

- `gpu_util` GPU 利用率（%）← **整窗均值 26.8%（首轮）→ 80.5%（稳态轮）即由此列计算**
- `gpu_power_w` 功耗（W，卡上限 350 W）；`gpu_temp_c` 温度（℃）
- `hbm_mb_s` HBM 带宽（MB/s）；`slice_vram_mib` 切片显存占用（MiB）

## 复算方法

```bash
# 峰值生成吞吐
awk -F, 'NR>1 && $6>m {m=$6} END {print m}' perf_20261002_200636.csv

# 稳态轮整窗 GPU 利用率均值（有 GPU 列的文件）
awk -F, 'NR>1 && $27!="" {s+=$27; n++} END {print s/n}' perf_20261002_200636.csv
```

## 环境（采集时）

- 硬件：沐曦 MetaX **C500**（物理 64 GB / 切片 32 GB，Compute 50%）
- 驱动栈：**MACA 3.5.3.20**（驱动 3.8.30 / BIOS 1.33.5.0 / `mx-smi` 2.2.12）
- 推理引擎：**vLLM 0.17.0**（`VLLM_USE_MXGPU=1`）
- 模型：**Qwen2.5-32B-Instruct-AWQ**（AWQ 4bit / 16 分片 / 19 GB）
- 并发：16；调度键 `max_num_batched_tokens=2048`

数据为原始采样，**未做任何平滑、插值或后处理**。
