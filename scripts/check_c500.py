#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_c500.py —— 通道③（沐曦 C500 自部署 32B）连通性与三通道选路自检

实测口径（2026-09-15 打通当天，通道③ 生产基线，用于对照你本机的数）：
    GET  /v1/models            HTTP 200 /  1.07 s
    POST /v1/chat/completions  HTTP 200 /  1.91 s（中文对话）
    3 轮延迟 1.25 / 1.04 / 1.20 s（均值 1.17 s）
    端到端·点名 model=xihe-32b        HTTP 200 /  4.16 s
    端到端·复杂任务自动路由           HTTP 200 / 19.68 s

用法
    python scripts/check_c500.py                    # 用环境变量/默认值
    python scripts/check_c500.py --base-url http://127.0.0.1:18000/v1
    python scripts/check_c500.py --all             # 三通道一起体检

不依赖本仓库其它模块，只需 requests（缺失时自动退回标准库 urllib）。
本脚本**不读取、不打印任何密钥**；请求头里只带一个占位 token。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_C500 = os.environ.get("C500_BASE_URL", "http://127.0.0.1:18000/v1")
DEFAULT_MODEL = os.environ.get("C500_MODEL", "xihe-32b")
DEFAULT_OLLAMA = os.environ.get("OLLAMA_API", "http://127.0.0.1:11434/v1/chat/completions")


def _post(url, payload, headers, timeout):
    try:
        import requests
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=dict(headers, **{"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            return 0, str(e)


def _get(url, headers, timeout):
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            return 0, str(e)


def check_c500(base_url: str, model: str, rounds: int = 3) -> int:
    print("=" * 72)
    print("通道③ 沐曦 C500 自部署 32B  —— 连通性自检")
    print("base_url = %s   model = %s" % (base_url, model))
    print("=" * 72)

    # 某些隧道/网关需要跳过插页警告头；不带会导致错误页而非 API 响应。
    headers = {
        "ngrok-skip-browser-warning": "true",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    code, body = _get(base_url.rstrip("/") + "/models", headers, timeout=15)
    dt = time.time() - t0
    ok_models = (code == 200)
    print("[1] GET  /v1/models              -> HTTP %s  (%.2fs)" % (code or "-", dt))
    if ok_models:
        try:
            ids = [m.get("id") for m in json.loads(body).get("data", [])]
            print("     可用模型: %s" % ids)
        except Exception:                            # noqa: BLE001
            print("     (响应不是标准 JSON，前 120 字: %s)" % body[:120].replace("\n", " "))
    else:
        print("     失败：%s" % body[:200].replace("\n", " "))
        print("     ⇒ 先确认隧道已建立（scripts/tunnel_c500.bat）且实例已开机。")
        return 1

    # 中文对话 + 多轮延迟
    lat = []
    for i in range(rounds):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "用一句话说明你是哪个模型。"}],
            "max_tokens": 64,
            "stream": False,
        }
        t1 = time.time()
        code, body = _post(base_url.rstrip("/") + "/chat/completions", payload, headers, timeout=180)
        dt = time.time() - t1
        lat.append(dt)
        snippet = ""
        if code == 200:
            try:
                snippet = json.loads(body)["choices"][0]["message"]["content"][:60]
            except Exception:                        # noqa: BLE001
                snippet = body[:60]
        print("[2.%d] POST /v1/chat/completions  -> HTTP %s  (%.2fs)  %s"
              % (i + 1, code or "-", dt, snippet.replace("\n", " ")))
        if code != 200:
            print("     失败：%s" % body[:200].replace("\n", " "))
            return 1
    if lat:
        print("     3 轮延迟均值 = %.2fs （生产基线 1.17s，含排队会更高）" % (sum(lat) / len(lat)))
    print("\n结论：通道③ 可达且可推理 ✅")
    return 0


def check_channels() -> int:
    """三通道一起体检（②本地 Ollama 直接探；①云端由桥接器负责，本脚本只探端点连通性）。"""
    print("=" * 72)
    print("三通道体检")
    print("=" * 72)
    headers = {"Content-Type": "application/json"}
    # ① 云端：仅在配置了 base_url 时探（默认不探，避免误连）
    cloud = os.environ.get("MUXI_BASE_URL", "")
    print("[①] 云端 API（多源韧性对照项）: %s" % (cloud or "未配置 MUXI_BASE_URL —— 跳过"))
    # ② 本地 Ollama
    t0 = time.time()
    code, body = _post(DEFAULT_OLLAMA,
                       {"model": os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"),
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 8, "stream": False}, headers, timeout=120)
    print("[②] 本地 Ollama 兜底            : HTTP %s  (%.2fs)"
          % (code or "-", time.time() - t0))
    # ③ C500
    code, _ = _get(DEFAULT_C500.rstrip("/") + "/models", headers, timeout=15)
    print("[③] C500 自部署 32B（实证通道） : HTTP %s" % (code or "-"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="通道③ / 三通道 连通性自检")
    ap.add_argument("--base-url", default=DEFAULT_C500)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="三通道一起体检")
    args = ap.parse_args(argv)
    if args.all:
        return check_channels()
    return check_c500(args.base_url, args.model, args.rounds)


if __name__ == "__main__":
    sys.exit(main())
