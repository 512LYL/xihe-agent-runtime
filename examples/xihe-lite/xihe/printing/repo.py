# -*- coding: utf-8 -*-
"""打印仓库（控制台里「丢文件进来就能打」的那个目录）

只做四件事：列 / 存 / 删 / 造演示样本。所有文件名一律 basename 化，
并校验解析后的绝对路径仍在仓库目录内，防目录穿越。
"""
import base64
import os
import time

SAFE_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".csv",
            ".log", ".json", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def relpath(p):
    """相对项目根目录；跨盘符时退回绝对路径（不抛异常）。"""
    try:
        from .. import config as _config
        return os.path.relpath(p, _config.ROOT)
    except Exception:
        return p


def repo_dir(cfg):
    from .. import config as _config
    d = _config.path(cfg, "paths", "repo")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def safe_path(cfg, name):
    name = os.path.basename(str(name or "").replace("\\", "/"))
    if not name or name.startswith("."):
        return None
    root = os.path.abspath(repo_dir(cfg))
    p = os.path.abspath(os.path.join(root, name))
    if not p.startswith(root + os.sep):
        return None
    return p if os.path.isfile(p) else None


def list_files(cfg):
    root = repo_dir(cfg)
    items = []
    try:
        for f in os.listdir(root):
            p = os.path.join(root, f)
            if not os.path.isfile(p) or f.startswith("."):
                continue
            st = os.stat(p)
            items.append({"name": f, "size": st.st_size,
                          "mtime": int(st.st_mtime * 1000),
                          "ext": os.path.splitext(f)[1].lower().lstrip("."),
                          "printable": os.path.splitext(f)[1].lower() in SAFE_EXT})
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e), "items": []}
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"ok": True, "dir": relpath(root), "items": items}


def save_upload(cfg, name, b64):
    name = os.path.basename(str(name or "").replace("\\", "/"))
    if not name:
        return {"ok": False, "msg": "缺文件名"}
    if os.path.splitext(name)[1].lower() not in SAFE_EXT:
        return {"ok": False, "msg": "不支持的类型：%s" % os.path.splitext(name)[1]}
    try:
        raw = base64.b64decode(b64 or "")
    except Exception:
        return {"ok": False, "msg": "内容解码失败（需 base64）"}
    try:
        p = os.path.join(repo_dir(cfg), name)
        with open(p, "wb") as f:
            f.write(raw)
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "name": name, "size": len(raw)}


def delete(cfg, names):
    done, bad = [], []
    for n in (names or []):
        p = safe_path(cfg, n)
        if not p:
            bad.append(os.path.basename(str(n)))
            continue
        try:
            os.remove(p)
            done.append(os.path.basename(str(n)))
        except Exception:
            bad.append(os.path.basename(str(n)))
    return {"ok": True, "deleted": done, "bad": bad}


# ---------------------------------------------------------------- 演示样本
_SAMPLE_TXT = """曦和 · 精简可运行版（xihe-lite）演示工单
================================================

本文件是由 `python -m xihe demo` 生成的演示样本，
可以直接在控制台「打印调度」里提交，用来演示整条打印链路。

链路：仓库 → 暂存 ready → 守护认领 → 提交后端 → 出纸判定 → 归档
状态：printed / simulated / submitted / failed / cancelled

提示：
  · 没有打印机时，系统自动走模拟模式（mock），结果进 `_模拟` 目录；
  · 有打印机时，只有确认出纸才会进 `_已打印`，未确认的进 `_队列`。

生成时间：%s
""" % time.strftime("%Y-%m-%d %H:%M:%S")

_SAMPLE_MD = """# 曦和 · 精简可运行版

- 控制台：状态看板 + 服务启停 + 打印调度
- 打印链路：文档转 PDF → 提交 → 状态判定 → 暂存归档
- 离线可用：无打印机自动降级 mock

## 三步跑起来

1. `python -m pip install -r requirements.txt`
2. `copy config.example.json config.json`
3. `start.bat`（或 `python -m xihe serve`），浏览器打开 http://127.0.0.1:18910
"""

_SAMPLE_CSV = """序号,检查项,期望结果,实际结果
1,控制台看板,6 个服务卡片 + 打印面板,待填
2,提交打印,任务进入 ready 队列,待填
3,出纸判定,printed 或 simulated,待填
4,暂存归档,_已打印 或 _模拟 有文件,待填
5,前置自检,一行中文就绪结论,待填
"""


def make_samples(cfg):
    """在仓库目录生成几个演示文件（已存在就跳过）。返回生成的绝对路径列表。"""
    root = repo_dir(cfg)
    made = []
    files = {
        "演示工单.txt": _SAMPLE_TXT,
        "精简版说明.md": _SAMPLE_MD,
        "验收清单.csv": _SAMPLE_CSV,
    }
    for name, text in files.items():
        p = os.path.join(root, name)
        if not os.path.exists(p):
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                made.append(p)
            except Exception:
                pass
    # 有 Pillow 就顺手生成一张可打印的 PNG（演示「图片 → A4 PDF」路径）
    png = os.path.join(root, "演示图片.png")
    if not os.path.exists(png):
        try:
            from PIL import Image, ImageDraw
            im = Image.new("RGB", (900, 600), "white")
            d = ImageDraw.Draw(im)
            d.rectangle([20, 20, 880, 580], outline="black", width=3)
            d.text((60, 80), "xihe-lite / offline demo", fill="black")
            d.text((60, 140), "image -> A4 pdf -> print chain", fill="black")
            im.save(png)
            made.append(png)
        except Exception:
            pass
    return made
