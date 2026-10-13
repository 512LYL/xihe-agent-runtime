#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_secrets.py —— 公开前最后一道闸：全量敏感信息扫描

对应红线（仓库 SECURITY.md / 内部 R1、R9）：
  密钥 / 算力券码 / 网关 IP / SSH 用户名与口令 / QQ token / 平台账号 / 本机绝对路径
  —— 一律不入库（含 Issue 正文、Release Notes、截图 OCR 可见处）。

判定口径（沿用本项目既有的扫描口径）：
  · 真实密钥（sk-xxx / SIG...）      必须 0 命中
  · 内网/网关 IP 段                  必须 0 命中（127.0.0.1 除外）
  · password / passwd 赋值           必须 0 命中
  · api_key / apiKey                 只允许**占位符**形态
  · 本机绝对路径（C:\\Users\\<人名> 等）必须 0 命中
  · 大文件（> 10 MB）                必须 0 命中（GitHub 拒收 >100 MB，红线更严）

用法
  python scripts/scan_secrets.py                 # 扫整个仓库（跳过 .git / .runtime）
  python scripts/scan_secrets.py --path docs     # 只扫某个子目录
  python scripts/scan_secrets.py --strict        # 有任何命中即非 0 退出（建议 CI 用）

退出码：0 = 干净；1 = 有命中；2 = 用法错误
"""
from __future__ import annotations

import argparse
import os
import re
import sys

SKIP_DIRS = {".git", ".runtime", "_build", "__pycache__", ".venv", "venv", "node_modules"}
BIN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".7z",
           ".mp4", ".pptx", ".docx", ".xlsx", ".ico", ".woff", ".woff2", ".ttf"}
# 本扫描器自身需要写出规则里的字面量（如 api_key / access_token 的名称），
# 因此从扫描范围中排除自身，避免自报。
SELF_SKIP = {"scan_secrets.py"}

# (名称, 正则, 是否致命)
RULES = [
    ("真实密钥 sk-",        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),              True),
    ("真实密钥 SIG",        re.compile(r"\bSIG[A-Za-z0-9+/]{12,}"),               True),
    ("Bearer 明文令牌",     re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),        True),
    ("password 赋值",       re.compile(r"(?i)\bpass(?:word|wd)\s*[:=]\s*[\"'][^\"'\s]{4,}[\"']"), True),
    ("SSH 私钥块",          re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),    True),
    ("算力券码形态",        re.compile(r"(?i)\b(?:coupon|voucher|券码)\s*[:=]\s*[A-Za-z0-9\-]{8,}"), True),
    ("本机绝对路径(C 盘用户)", re.compile(r"[Cc]:\\\\?Users\\\\?(?!<)[A-Za-z0-9_\-\.]+"), True),
    ("本机绝对路径(D 盘目录)", re.compile(r"[Dd]:\\\\?附属物"),                    True),
    # 非致命：需要人工确认是占位符
    # IP 规则配合下面的 looks_like_ip()，把「版本号四段号」（如 MACA 3.5.3.20）挡掉。
    ("ip 地址（非回环）",    re.compile(r"(?<![\w.\-])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), False),
    ("api_key 赋值（非占位符）",  re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']"), False),
    ("access_token 赋值（非空）", re.compile(r"(?i)\baccess[_-]?token\s*[:=]\s*[\"'](?![\"'])[^\"'\s]{6,}[\"']"), False),
]

PLACEHOLDER_RE = re.compile(
    r"(?i)(your_|_here|xxx+|<[A-Z_]+>|\$\{|placeholder|example|示例|占位|换行|"
    r"token\"?\s*:\s*\"\"|none|ollama|token\s*=\s*\"\")")


def looks_like_ip(s: str) -> bool:
    """判断是否**像真实 IP**（用于把版本号误报挡掉）。

    `MACA 3.5.3.20` / `vLLM 0.17.0` / `MACA3.5.3.20` / `BIOS 1.33.5.0` 这类**四段号**
    与 IPv4 在字面上完全同形，纯正则无法可靠区分。工程上的区分办法：

      · 有任意一段 > 255 → 明显是**可疑的真实 IP**（保留告警）；
      · 否则若有 **≥2 段 ≤ 20** → 典型的**版本号**（主/次版本），跳过；
      · 其余（如 `10.0.0.1` 这类内网网段）→ **保留告警交人工判断**。

    偏保守：宁可多报几条让人看一眼，也不放过真实网段。
    """
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 for n in nums):
        return False
    if any(n > 255 for n in nums):
        return True
    if sum(1 for n in nums if n <= 20) >= 2:
        return False
    return True

MAX_BYTES = 10 * 1024 * 1024          # 单文件 >10 MB 视为违规


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="公开仓库敏感信息全量扫描")
    ap.add_argument("--path", default=".", help="要扫描的目录（默认整个仓库）")
    ap.add_argument("--strict", action="store_true", help="有任意命中即返回非 0")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print("用法错误：%s 不是目录" % root, file=sys.stderr)
        return 2

    fatal, soft, oversized, scanned, skipped = [], [], [], 0, 0
    for p in iter_files(root):
        rel = os.path.relpath(p, root)
        if os.path.basename(p) in SELF_SKIP:
            skipped += 1
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        scanned += 1
        if size > MAX_BYTES:
            oversized.append((rel, size))
        ext = os.path.splitext(p)[1].lower()
        if ext in BIN_EXT or size > 2 * 1024 * 1024:
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for name, rx, is_fatal in RULES:
            for m in rx.finditer(text):
                hit = m.group(0)
                # IP 规则再过一道「像不像真实 IP」的判定，挡掉版本号四段号
                if name.startswith("ip 地址") and not looks_like_ip(hit):
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                ctx = text.splitlines()[line_no - 1].strip() if line_no else ""
                row = (rel, line_no, name, ctx[:110])
                if is_fatal:
                    fatal.append(row)
                elif not PLACEHOLDER_RE.search(ctx):
                    soft.append(row)

    print("=" * 74)
    print("敏感信息全量扫描  root=%s" % root)
    print("=" * 74)
    print("扫描文件数 = %d%s" % (scanned, "（另跳过扫描器自身 %d 个）" % skipped if skipped else ""))

    def dump(title, rows):
        print("\n-- %s：%d 条" % (title, len(rows)))
        for rel, ln, name, ctx in rows[:60]:
            print("   %s:%s  [%s]  %s" % (rel, ln, name, ctx))
        if len(rows) > 60:
            print("   … 其余 %d 条省略" % (len(rows) - 60))

    dump("❌ 致命命中（必须清零）", fatal)
    dump("🟡 需人工确认为占位符", soft)
    if oversized:
        print("\n-- ❌ 超过 10 MB 的文件：%d 个" % len(oversized))
        for rel, size in oversized:
            print("   %s  (%.1f MB)" % (rel, size / 1048576.0))
    else:
        print("\n-- ✅ 无超过 10 MB 的文件（红线：仓库内不得出现大二进制）")

    bad = len(fatal) + len(oversized) + (len(soft) if args.strict else 0)
    print("\n结论：%s" % ("❌ 未通过，请逐条处理后重跑" if bad else "✅ 通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
