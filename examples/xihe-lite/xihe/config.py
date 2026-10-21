# -*- coding: utf-8 -*-
"""配置加载（xihe-lite）

红线：本模块**不含任何与本机相关的绝对路径 / 用户名 / 主机名 / IP**。
DEFAULTS 里的路径全部相对项目根目录；换到别人机器只需改 config.json，不改代码。

用法：
    from xihe import config
    cfg = config.load()                 # 读 config.json（缺失则读 .example 并给出提示）
    cfg = config.load("config.json", root="/path/to/xihe-lite")
    staging = config.path(cfg, "paths", "staging")
"""
import copy
import json
import os

# 项目根目录 = 本文件上两级（xihe-lite/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_NAME = "config.json"
EXAMPLE_NAME = "config.example.json"

DEFAULTS = {
    "meta": {"name": "xihe-lite", "version": "1.0.0"},
    "console": {
        "host": "127.0.0.1",
        "port": 18910,
        "auto_open": False,
        "status_ttl": 2.0,
    },
    "paths": {
        "staging": "runtime/staging",     # 暂存区（打印状态机的根）
        "logs": "runtime/logs",
        "repo": "runtime/repo",           # 打印仓库（拖进来的文件放这里）
        "run": "runtime/run",             # pid 文件
    },
    "printer": {
        "name": "",                       # 空 = 用系统默认打印机
        "ip": "",                         # 空 = 不探测网络/EWS
        # mock=模拟模式（默认，不接触任何设备）；auto=有可用打印机才走真机；
        # win32=强制真机。想真机打印必须显式改成 win32 并填 printer.name。
        "backend": "mock",
        "duplex": "simplex",              # simplex | long-edge | short-edge
        "paper_source": 15,               # 15=驱动自动选择（不同驱动机型可改）
    },
    # ── 智能路由骨架（Router Skeleton）──────────────────────────────
    "router": {
        # 云端 API 的 Key 填在这里；留空 = 该级视为不可用（本版默认不联网）
        "api_key": "",
        # 云端 API 的地址（仅占位；本版不发任何网络请求）
        "endpoint": "",
        # 四级顺序：cloud_api → local_llm → remote_compute → offline_rule
        "tiers": ["cloud_api", "local_llm", "remote_compute", "offline_rule"],
        # 各 Mock 通道的可用性（改这里就能演示不同的降级路径）
        "mock_available": {"cloud_api": False, "local_llm": True,
                           "remote_compute": False},
        "reply_prefix": "[mock]",
        "timeout_sec": 5,
    },
    "print": {
        "poll_sec": 3,                    # 守护空转轮询间隔
        "drain_poll": 2,                   # 出纸判定轮询间隔
        "drain_wait": 300,                 # 提交后最多等多久
        "ready_wait": 600,                 # 提交前最多等打印机多久
        "confirm_grace": 4,                # 队列清空后静置复核秒数
        "max_resubmit": 3,                 # 提交脚本失败重投次数
        "submit_timeout": 300,             # 单次提交超时
        "ews_timeout": 8,                  # 单次 EWS 请求超时
        "job_appear_wait": 20,             # 等任务出现在队列的秒数
        "keep_done_days": 7,
        "keep_fail_days": 30,
    },
    "daemon": {"port": 18911},
    # 控制台可启停的服务清单；留空 = 用内置默认（只有一个「打印守护」）
    "services": [],
}

# 内置默认服务清单（与上一版单体的 SERVICES 同构，只保留精简版需要的一项）
DEFAULT_SERVICES = [
    {
        "id": "printd",
        "name": "打印守护",
        "port": 18911,
        "module": "xihe.printing.daemon",
        "probe": "http://127.0.0.1:18911/status",
        "log": "",
        "order": 1,
        "group": "print",
    },
]


def _merge(base, over):
    """递归合并：over 覆盖 base（只覆盖出现的键）。"""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def find_config(root=None):
    """返回 (实际使用的配置文件路径, 是否回退到了 .example)。"""
    root = root or ROOT
    p = os.path.join(root, CONFIG_NAME)
    if os.path.isfile(p):
        return p, False
    q = os.path.join(root, EXAMPLE_NAME)
    if os.path.isfile(q):
        return q, True
    return "", False


def load(path=None, root=None, env=True):
    """读配置并解析路径。返回 dict（含 `_meta` 说明实际来源）。

    优先级：显式 path > $XIHE_CONFIG > config.json > config.example.json > DEFAULTS
    """
    root = os.path.abspath(root or ROOT)
    used, fallback = "", False
    if path:
        used = os.path.abspath(path)
    else:
        if env and os.environ.get("XIHE_CONFIG"):
            used = os.path.abspath(os.environ["XIHE_CONFIG"])
        else:
            used, fallback = find_config(root)
    raw = {}
    err = ""
    if used and os.path.isfile(used):
        try:
            with open(used, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception as e:                      # 配置写坏了也不能让程序起不来
            err = "%s: %s" % (type(e).__name__, e)
            raw = {}
    cfg = _merge(DEFAULTS, raw)

    if env:
        _apply_env(cfg)

    cfg["_meta"] = {
        "root": root,
        "config_file": used,
        "using_example": bool(fallback),
        "parse_error": err,
    }
    abspathify(cfg, root)
    return cfg


def _apply_env(cfg):
    """少量环境变量覆盖（便于容器/CI 里不写文件就跑）。"""
    port = os.environ.get("XIHE_PORT")
    if port and port.isdigit():
        cfg["console"]["port"] = int(port)
    backend = os.environ.get("XIHE_BACKEND")
    if backend:
        cfg["printer"]["backend"] = backend
    ip = os.environ.get("XIHE_PRINTER_IP")
    if ip:
        cfg["printer"]["ip"] = ip
    name = os.environ.get("XIHE_PRINTER_NAME")
    if name:
        cfg["printer"]["name"] = name
    home = os.environ.get("XIHE_HOME")
    if home:
        cfg["_meta"] = {"home": os.path.abspath(home)}


def _root(cfg):
    home = (cfg.get("_meta") or {}).get("home")
    return home or (cfg.get("_meta") or {}).get("root") or ROOT


def resolve(cfg, value):
    """相对路径 → 相对项目根目录；绝对路径原样返回（便于用户自定义）。"""
    if not value:
        return ""
    value = str(value)
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(_root(cfg), value))


def abspathify(cfg, root=None):
    """把所有 paths.* 解析成绝对路径（仅在内存里，不写回文件）。"""
    if root:
        cfg.setdefault("_meta", {}).setdefault("root", root)
    for k, v in list((cfg.get("paths") or {}).items()):
        cfg["paths"][k] = resolve(cfg, v)
    return cfg


def path(cfg, section, key):
    """取某个配置路径（已解析为绝对路径）。"""
    return (cfg.get(section) or {}).get(key) or ""


def ensure_dirs(cfg):
    """建好运行期需要的目录，返回 {名字: 路径或错误}。"""
    made = {}
    for key in ("staging", "logs", "repo", "run"):
        p = path(cfg, "paths", key)
        if not p:
            continue
        try:
            os.makedirs(p, exist_ok=True)
            made[key] = p
        except Exception as e:
            made[key] = "ERROR %s: %s" % (type(e).__name__, e)
    return made


def safe_view(cfg):
    """给控制台看的配置摘要（剥掉 _meta 里的绝对路径与 api_key 明文）。"""
    c = copy.deepcopy(cfg)
    key = (c.get("router") or {}).get("api_key") or ""
    if key:
        c["router"]["api_key"] = "***"
    meta = c.pop("_meta", {}) or {}
    c["_meta"] = {
        "root": "(项目根目录)",
        "config_file": os.path.basename(meta.get("config_file") or "") or "(未找到)",
        "using_example": bool(meta.get("using_example")),
        "parse_error": meta.get("parse_error") or "",
    }
    c["paths"] = {k: os.path.relpath(v, _root(cfg)) if os.path.isabs(str(v)) else v
                  for k, v in (c.get("paths") or {}).items()}
    return c
