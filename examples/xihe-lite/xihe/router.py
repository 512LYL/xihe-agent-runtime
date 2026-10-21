# -*- coding: utf-8 -*-
"""智能路由骨架（Router Skeleton）——四级降级链，全部 Mock，不联网

生产版有「云端 API → 本地大模型 → 异地算力」的多通道调度；这一版只保留**路由骨架**：
通道探测、失败降级、耗时统计、流转日志，一个都不少；真正的大模型调用换成 Mock 函数。

四级顺序（配置里可改）：

    tier1  cloud_api       云端 API        需 api_key；未填 → 视为不可用（日志写明原因）
    tier2  local_llm       本地大模型（兜底）本机模型/模板；默认可用 ← 会打「切换至本地兜底」
    tier3  remote_compute  异地算力        远端推理服务；默认 mock 不可达
    tier4  offline_rule    离线规则        确定性规则，永不出错（保证演示一定有结果）

每次路由都会：
  1. 往 `runtime/logs/router.log` 逐行写 `[ROUTE] ...`；
  2. 把同样的行作为 `route_log` 返回给调用方（控制台看板 / CLI / HTTP 接口都能看到流转过程）。

> 本模块不发任何网络请求。真实接入时，只要替换 `_call_*` 三个函数即可，路由与日志不用动。
"""
import time

from . import logset

# 四级定义（顺序 = 优先级）
TIERS = [
    {"id": "cloud_api", "name": "云端 API", "role": "tier1",
     "desc": "外部大模型 API；需在 config.router.api_key 填入你自己的 Key，留空则跳过"},
    {"id": "local_llm", "name": "本地大模型（兜底）", "role": "tier2",
     "desc": "本机小模型/模板兜底；本版默认可用，保证演示不空转"},
    {"id": "remote_compute", "name": "异地算力", "role": "tier3",
     "desc": "远端推理服务；本版为 mock，默认可配成不可达以演示降级"},
    {"id": "offline_rule", "name": "离线规则", "role": "tier4",
     "desc": "确定性规则；永不失败，最后一道保障"},
]

DEFAULT_ORDER = [t["id"] for t in TIERS]
_LAST = {"result": None}


class ProviderError(RuntimeError):
    """某一路由通道不可用/失败。"""


def _r(cfg):
    return cfg.get("router") or {}


def tiers(cfg=None):
    """返回四级清单（含当前 mock 可用性），给看板/README 用。"""
    cfg = cfg or {}
    avail = dict(_r(cfg).get("mock_available") or {})
    out = []
    for t in TIERS:
        d = dict(t)
        d["available"] = bool(avail.get(t["id"], t["id"] in ("local_llm", "offline_rule")))
        if t["id"] == "cloud_api" and not (_r(cfg).get("api_key") or ""):
            d["available"] = False
            d["note"] = "未配置 api_key"
        out.append(d)
    return out


def order(cfg=None):
    """实际尝试顺序（配置里的 router.tiers 覆盖默认，去重并只保留已知 id）。"""
    raw = (_r(cfg).get("tiers") or DEFAULT_ORDER) if cfg else DEFAULT_ORDER
    seen, out = set(), []
    for x in raw:
        if x in DEFAULT_ORDER and x not in seen:
            seen.add(x)
            out.append(x)
    return out or DEFAULT_ORDER


# ---------------------------------------------------------------- 四个 Mock 通道
def _call_cloud(prompt, cfg):
    """云端 API：本版不联网。填了 api_key 也只是"看起来可用"，仍由 mock 开关决定。"""
    key = str(_r(cfg).get("api_key") or "").strip()
    if not key:
        raise ProviderError("未配置 api_key（本版不联网，按 mock 处理）")
    if not bool((_r(cfg).get("mock_available") or {}).get("cloud_api", False)):
        raise ProviderError("mock：云端通道不可用（可把 mock_available.cloud_api 置 true 演示成功态）")
    raise ProviderError("mock：本版未接入真实云端实现")


def _call_local(prompt, cfg):
    """本地大模型（兜底）：返回一条 mock 响应，证明链路通了、时延可测。"""
    if not bool((_r(cfg).get("mock_available") or {}).get("local_llm", True)):
        raise ProviderError("mock：本地兜底被显式关闭")
    head = str(_r(cfg).get("reply_prefix") or "[mock]")
    body = prompt.strip().replace("\n", " ")
    if len(body) > 60:
        body = body[:60] + "…"
    return "%s 本地兜底已处理：%s（字符数 %d）" % (head, body or "（空指令）", len(prompt))


def _call_remote(prompt, cfg):
    """异地算力：远端推理服务。默认不可达，用来演示第三级降级。"""
    if not bool((_r(cfg).get("mock_available") or {}).get("remote_compute", False)):
        raise ProviderError("mock：异地算力不可达（配置里可置 true 演示成功态）")
    raise ProviderError("mock：本版未接入真实远端实现")


def _call_rule(prompt, cfg):
    """离线规则：永不失败。给出确定性的、可预期的结果。"""
    text = prompt.strip()
    kind = "指令"
    if any(w in text for w in ("打印", "print", "打印测试页")):
        kind = "打印类指令"
    elif any(w in text for w in ("状态", "status", "健康")):
        kind = "状态查询"
    return "【离线规则】识别为%s；字数 %d；本通道不依赖任何模型与网络。" % (kind, len(text))


_CALLERS = {
    "cloud_api": _call_cloud,
    "local_llm": _call_local,
    "remote_compute": _call_remote,
    "offline_rule": _call_rule,
}


def _tier_name(tid):
    for t in TIERS:
        if t["id"] == tid:
            return t["name"]
    return tid


# ---------------------------------------------------------------- 主流程
def route(prompt, cfg, log=None):
    """跑一次路由，返回结果 dict（含 route_log 行）。"""
    text = str(prompt or "")
    log = log or logset.get_logger(cfg, "router", echo=True, tag="ROUTE")
    seq = order(cfg)
    lines = []

    def emit(msg):
        line = "[ROUTE] " + msg
        lines.append(line)
        try:
            # 日志器自身的 tag 就是 [ROUTE]，这里只写正文，避免出现 "[ROUTE] [ROUTE]"
            log.info(msg)
        except Exception:
            pass

    t0 = time.time()
    emit("开始路由：输入 %d 字符，共 %d 级（%s）" % (len(text), len(seq), " → ".join(seq)))
    chosen, reply, attempts = None, "", []

    for i, tid in enumerate(seq, 1):
        name = _tier_name(tid)
        # 首次尝试不写"切换"，之后每级都写「切换至 X」
        if i == 1:
            emit("tier%d %s 尝试中…" % (i, name))
        elif tid == "local_llm":
            emit("切换至本地兜底（tier%d local_llm）" % i)
        else:
            emit("切换至%s（tier%d %s）" % (name, i, name))
        st = time.time()
        try:
            reply = _CALLERS[tid](text, cfg) or ""
            used = int((time.time() - st) * 1000)
            attempts.append({"tier": tid, "ok": True, "ms": used})
            emit("tier%d %s 成功（%d ms）" % (i, name, used))
            chosen = tid
            break
        except ProviderError as e:
            used = int((time.time() - st) * 1000)
            attempts.append({"tier": tid, "ok": False, "ms": used, "err": str(e)})
            emit("tier%d %s 不可用：%s" % (i, name, e))
        except Exception as e:                     # 兜底：任何异常都不能中断降级链
            attempts.append({"tier": tid, "ok": False, "ms": 0,
                             "err": "%s: %s" % (type(e).__name__, e)})
            emit("tier%d %s 异常：%s: %s" % (i, name, type(e).__name__, e))

    elapsed = int((time.time() - t0) * 1000)
    if chosen:
        emit("完成：实际走 %s（总耗时 %d ms）" % (_tier_name(chosen), elapsed))
    else:
        emit("完成：四级全部不可用（总耗时 %d ms）—— 这不应发生，离线规则是最后一道保障" % elapsed)

    result = {
        "ok": bool(chosen),
        "tier": chosen,
        "tier_name": _tier_name(chosen) if chosen else "",
        "reply": reply,
        "elapsed_ms": elapsed,
        "attempts": attempts,
        "route_log": lines,
        "ts": int(time.time()),
    }
    _LAST["result"] = result
    return result


def last_result():
    return _LAST.get("result")


def status(cfg):
    """给看板用：四级清单 + 最近一次路由结果。"""
    return {"ok": True, "tiers": tiers(cfg), "order": order(cfg),
            "last": last_result()}
