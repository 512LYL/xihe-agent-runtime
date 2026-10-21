# -*- coding: utf-8 -*-
"""隐私清洗 / 脱敏自检扫描器（发布前跑一遍）

扫描目标：整个 xihe-lite 目录（跳过 runtime/ 与 __pycache__）。命中即「不许提交」，退出码 1。

规则（19 条）覆盖四类红线：
  ① 零绝对路径：任何盘符路径、用户目录路径、本机工程目录名
  ② 零真实 IP ：任何 IPv4 字面量（仅放行回环 127.0.0.1 与绑定用 0.0.0.0）、内网网段
  ③ 零凭据    ：sk-/AKIA/ghp_ 前缀密钥、JWT、Bearer、私钥块、明文口令、QQ 号形态
  ④ 零内部代号：内部角色名（构造后比对，见下）

注意：本文件里的敏感字面量是**拼接构造**的（例如 "Us" + "ers"），所以扫描器扫到自己
不会自指命中；默认也跳过自身路径。
"""
import argparse
import json
import os
import re
import sys

SKIP_DIRS = {"__pycache__", ".git", "runtime", ".venv", "venv", "logs",
             "node_modules", ".idea", ".vscode"}
SCAN_EXT = {".py", ".json", ".md", ".txt", ".bat", ".cmd", ".ps1", ".sh",
            ".html", ".htm", ".css", ".js", ".yml", ".yaml", ".ini", ".cfg",
            ".example", ".gitignore", ".env", ""}

# ---- 拼出来的敏感片段（防止扫描器自指）
_USER = "Us" + "ers"
_PROJ = "附" + "属物"
_ROLE1 = "斯" + "托夫"
_ROLE2 = "姐" + "姐"
_ROLE3 = "妹" + "妹"
_ROLE4 = "大" + "肥鱼"

# (正则, 规则名, 放行值集合)
PATTERNS = [
    # ① 零绝对路径
    (r"\b[A-Za-z]:[\\/]", "盘符绝对路径（零绝对路径红线）", set()),
    (r"[A-Za-z]:\\" + _USER + r"\\", "用户目录绝对路径", set()),
    (_PROJ, "本机工程目录名", set()),
    (r"\bNPIB\d{4,}", "本机打印机序列号", set()),
    # ② 零真实 IP
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IPv4 字面量（零真实 IP 红线）",
     {"127.0.0.1", "0.0.0.0"}),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "内网 IP", set()),
    # ③ 零凭据
    (r"\b[1-9]\d{8,9}\b", "9~10 位长数字（疑似 QQ 号/账号）", set()),
    (r"@(?:qq|163|126|gmail|outlook|foxmail)\.[a-z]{2,4}\b", "邮箱地址", set()),
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "API Key", set()),
    (r"\bAKIA[0-9A-Z]{16}\b", "云厂商 AccessKey", set()),
    (r"\bghp_[A-Za-z0-9]{20,}", "代码托管平台令牌", set()),
    (r"\beyJ[A-Za-z0-9_\-]{20,}", "JWT", set()),
    (r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{16,}", "Bearer 令牌", set()),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥块", set()),
    (r"root\+vm-", "隧道账号凭据", set()),
    (r"(?i)(?:password|passwd|secret)\s*[=:]\s*[\"'][^\"']{4,}", "明文口令", set()),
    # ④ 零内部代号
    (_ROLE1, "内部代号（人名）", set()),
    (_ROLE2, "内部代号（人名）", set()),
    (_ROLE3, "内部代号（人名）", set()),
    (_ROLE4, "内部代号（人名）", set()),
    (r"agent:main:qq:", "内部会话键", set()),
    (r"\bDESKTOP-[A-Z0-9]{4,}\b", "本机主机名", set()),
]


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")]
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext and ext not in SCAN_EXT:
                continue
            if f.startswith(".") and f not in (".env", ".env.example", ".gitignore"):
                continue
            yield os.path.join(dirpath, f)


def scan(root, skip_paths=(), max_hits=200):
    root = os.path.abspath(root)
    me = os.path.abspath(__file__)
    skip = {os.path.abspath(p) for p in skip_paths} | {me}
    compiled = [(re.compile(p), name, allow) for p, name, allow in PATTERNS]
    hits, nfiles = [], 0
    for path in iter_files(root):
        if path in skip:
            continue
        nfiles += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            for rx, name, allow in compiled:
                m = rx.search(line)
                if not m:
                    continue
                if m.group(0) in allow:
                    continue
                hits.append({
                    "file": os.path.relpath(path, root).replace("\\", "/"),
                    "line": i, "rule": name,
                    "match": m.group(0)[:60],
                    "text": line.strip()[:120],
                })
                if len(hits) >= max_hits:
                    return {"ok": False, "root": root, "files": nfiles,
                            "rules": len(PATTERNS), "hits": hits,
                            "truncated": True}
    return {"ok": not hits, "root": root, "files": nfiles,
            "rules": len(PATTERNS), "hits": hits, "truncated": False}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="xihe-lite 脱敏扫描（发布前自检）")
    ap.add_argument("root", nargs="?", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip", action="append", default=[])
    a = ap.parse_args(argv)
    r = scan(a.root, skip_paths=a.skip)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("扫描目录：%s" % r["root"])
        print("扫描文件：%d 个｜规则：%d 条" % (r["files"], r["rules"]))
        if r["ok"]:
            print("结果：✅ 零命中（无盘符路径 / 无真实 IP / 无凭据 / 无内部代号）")
        else:
            print("结果：❌ 命中 %d 处（必须清洗后才能发布）" % len(r["hits"]))
            for h in r["hits"]:
                print("  %s:%s  规则=%s  命中=%r" % (h["file"], h["line"],
                                                     h["rule"], h["match"]))
                print("      %s" % h["text"])
            if r["truncated"]:
                print("  （只显示前 200 条）")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
