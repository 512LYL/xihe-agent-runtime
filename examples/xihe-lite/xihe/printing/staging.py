# -*- coding: utf-8 -*-
"""暂存区状态机（打印链路的核心数据结构）

目录约定（全部在 config.paths.staging 下，默认 runtime/staging/）：

    顶层 <名字>__<时间戳>.<扩展名>         ready 队列（待打印）
         <名字>__<时间戳>.<扩展名>.meta.json  sidecar：谁放进来的、原始文件名、双面设置
    _pending/   待确认（生产版由聊天桥接用；精简版保留目录语义，不主动使用）
    _printing/  已认领（守护正在处理，进程崩了启动时回迁 ready）
    _队列/       未确认出纸（让人确认，不自动重打）
    _已打印/     确认出纸（printed）
    _模拟/       模拟模式完成（simulated，与真机的 _已打印 严格分开）
    _失败/       失败/被取消，附 <归档名>.err.txt 写明原因

一条铁律：**只有 printed 才进 `_已打印`**。生产版曾把 submitted（未确认出纸）
当成功归档，导致「没出纸却显示已打印」——这里用 RESULT_DIR 一张表钉死。
"""
import json
import os
import shutil
import time

from . import states

DIR_NAMES = {
    "pending": "_pending",
    "printing": "_printing",
    "hold": "_队列",
    "done": "_已打印",
    "sim": "_模拟",
    "failed": "_失败",
}

CLEAR_SIGNAL = "_clear_signal.json"


class Staging:
    def __init__(self, cfg):
        from .. import config as _config
        self.cfg = cfg
        self.root = _config.path(cfg, "paths", "staging")
        self.dirs = {"ready": self.root}
        for k, v in DIR_NAMES.items():
            self.dirs[k] = os.path.join(self.root, v)
        self.clear_signal = os.path.join(self.root, CLEAR_SIGNAL)
        self.last_job_file = os.path.join(
            _config.path(cfg, "paths", "run") or self.root, "last_job.json")

    # ---------------------------------------------------------------- 目录
    def ensure(self):
        for d in self.dirs.values():
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        return self.dirs

    def dir(self, key):
        return self.dirs.get(key) or ""

    # ---------------------------------------------------------------- 元数据
    def read_meta(self, path):
        try:
            with open(path + ".meta.json", "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def write_meta(self, path, meta):
        try:
            with open(path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- ready 队列
    def ready_files(self):
        """遍历 ready 队列：跳过 `_` 开头的内部目录、sidecar、取消信号。"""
        out = []
        try:
            for root, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if not d.startswith("_")]
                for f in files:
                    if f.startswith(".") or f.lower().endswith(".meta.json"):
                        continue
                    if f == CLEAR_SIGNAL or f.startswith(CLEAR_SIGNAL + "."):
                        continue
                    p = os.path.join(root, f)
                    if os.path.isfile(p):
                        out.append(p)
        except Exception:
            pass
        return out

    def next_ready(self):
        """严格 FIFO：取 mtime 最早的一个。"""
        fs = self.ready_files()
        if not fs:
            return None
        try:
            return min(fs, key=os.path.getmtime)
        except Exception:
            return fs[0]

    def claim(self, src):
        """原子认领：ready → `_printing`。失败（被别人抢走）返回 ""。"""
        name = os.path.basename(src)
        claimed = os.path.join(self.dirs["printing"], name)
        try:
            os.replace(src, claimed)
        except Exception:
            return ""
        try:
            if os.path.exists(src + ".meta.json"):
                os.replace(src + ".meta.json", claimed + ".meta.json")
        except Exception:
            pass
        return claimed

    def recover(self):
        """启动时把 `_printing` 残留回迁 ready（上次崩溃留下的）。返回回迁名单。"""
        moved = []
        try:
            for f in os.listdir(self.dirs["printing"]):
                src = os.path.join(self.dirs["printing"], f)
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(self.root, f)
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(src, dst)
                    moved.append(f)
                except Exception:
                    pass
        except Exception:
            pass
        return moved

    # ---------------------------------------------------------------- 入队 / 重投
    def add(self, src, meta=None):
        """把外部文件复制进 ready 队列。返回 (队列文件名, 错误)。"""
        if not src or not os.path.isfile(src):
            return "", "文件不存在：%s" % (src or "(空)")
        self.ensure()
        base = os.path.basename(src)
        stem, ext = os.path.splitext(base)
        dst = os.path.join(self.root, "%s__%s%s" % (
            stem, time.strftime("%Y%m%d_%H%M%S"), ext))
        n = 1
        while os.path.exists(dst):
            n += 1
            dst = os.path.join(self.root, "%s__%s_%d%s" % (
                stem, time.strftime("%Y%m%d_%H%M%S"), n, ext))
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            return "", "复制失败：%s: %s" % (type(e).__name__, e)
        m = {"orig": base, "ts": int(time.time()), "sided": "simplex"}
        m.update(meta or {})
        self.write_meta(dst, m)
        return os.path.basename(dst), ""

    def retry(self, name):
        """把 `_失败` / `_队列` 里的文件重新投回 ready。"""
        base = os.path.basename(str(name or ""))
        if not base:
            return "", "缺文件名"
        for key in ("failed", "hold", "sim"):
            src = os.path.join(self.dirs[key], base)
            if os.path.isfile(src):
                dst = os.path.join(self.root, base)
                try:
                    shutil.copy2(src, dst)
                    if os.path.exists(src + ".meta.json"):
                        shutil.copy2(src + ".meta.json", dst + ".meta.json")
                    m = self.read_meta(dst)
                    m.update({"retried": int(time.time()), "prev_dir": key})
                    self.write_meta(dst, m)
                    return base, ""
                except Exception as e:
                    return "", "重投失败：%s: %s" % (type(e).__name__, e)
        return "", "`_失败` / `_队列` / `_模拟` 里都没有：%s" % base

    # ---------------------------------------------------------------- 归档
    def archive(self, claimed, result, reason="", detail=""):
        """按结果归档。返回归档路径（失败返回 ""）。"""
        key = states.RESULT_DIR.get(result, "failed")
        dest_dir = self.dirs.get(key) or self.dirs["failed"]
        name = os.path.basename(claimed)
        dest = os.path.join(dest_dir, "%s__%s" % (time.strftime("%Y%m%d_%H%M%S"), name))
        try:
            os.replace(claimed, dest)
        except Exception:
            return ""
        try:
            if os.path.exists(claimed + ".meta.json"):
                os.replace(claimed + ".meta.json", dest + ".meta.json")
        except Exception:
            pass
        if result not in (states.RESULT_PRINTED, states.RESULT_SIMULATED):
            try:
                with open(dest + ".err.txt", "w", encoding="utf-8") as f:
                    f.write("result=%s\n%s\n%s\n" % (result, reason or "", (detail or "")[-1500:]))
            except Exception:
                pass
        try:
            mp = dest + ".meta.json"
            if os.path.exists(mp):
                m = self.read_meta(dest)
                m.update({"result": result, "ok": result in (states.RESULT_PRINTED,
                                                             states.RESULT_SIMULATED),
                          "reason": (reason or "")[:1000],
                          "archived_ts": int(time.time()), "archive_dir": key})
                self.write_meta(dest, m)
        except Exception:
            pass
        return dest

    # ---------------------------------------------------------------- 取消信号
    def write_clear_signal(self, payload):
        """原子写取消信号（临时文件 + replace），防止守护读到半个文件。"""
        tmp = self.clear_signal + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.clear_signal)
            return True
        except Exception:
            return False

    def read_clear_signal(self):
        try:
            with open(self.clear_signal, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def doc_matches(self, doc, name):
        """把 spooler 的 pDocument 与本地认领名对上。

        docx 走「Office → PDF」，队列里的作业名是 `<stem>_print.pdf`；
        PDF/图片直打时就是原文件名。两边去扩展名、去 `_print` 再比。
        """
        if not doc or not name:
            return False
        dn = os.path.splitext(os.path.basename(str(doc)))[0].lower()
        cn = os.path.splitext(os.path.basename(str(name)))[0].lower()
        if dn.endswith("_print"):
            dn = dn[:-6]
        return bool(dn) and (dn == cn or dn.startswith(cn) or cn.startswith(dn))

    def cancel_hit(self, claim_ts, name, job_ids=(), until_ts=None):
        """本次任务是否被 clear 清掉。命中返回中文原因，否则 ""。

        · 只认 claim 之后发生的 clear（不会被上一轮的 clear 误杀）
        · 按 JobId 或 pDocument 匹配
        · until_ts：已确认 printed 时，只认 printed 之前发生的 clear
        """
        sig = self.read_clear_signal()
        if not sig:
            return ""
        try:
            ts = float(sig.get("ts") or 0)
        except Exception:
            return ""
        if ts + 0.5 < claim_ts:
            return ""
        if until_ts is not None and ts > until_ts + 0.001:
            return ""
        for d in (sig.get("docs") or []):
            if self.doc_matches(d, name):
                return "队列已被 clear 清空（pDocument=%s，clear_ts=%s）" % (d, int(ts))
        ids = {str(x) for x in (sig.get("jobs") or [])}
        mine = {str(x) for x in (job_ids or [])}
        if ids and mine and (ids & mine):
            return "队列已被 clear 清空（JobId=%s，clear_ts=%s）" % (
                ",".join(sorted(ids & mine)), int(ts))
        return ""

    # ---------------------------------------------------------------- 统计
    @staticmethod
    def _is_job_file(f):
        """计数只数真正的任务文件，别把 sidecar / 错误说明算进去。"""
        return not (f.endswith(".meta.json") or f.endswith(".err.txt"))

    def counts(self):
        c = {}
        for key in ("pending", "printing", "hold", "done", "sim", "failed"):
            try:
                c[key] = len([f for f in os.listdir(self.dirs[key])
                              if self._is_job_file(f)])
            except Exception:
                c[key] = 0
        c["ready"] = len(self.ready_files())
        return c

    def list_dir(self, key, limit=20):
        try:
            fs = sorted(os.listdir(self.dirs[key]),
                        key=lambda f: os.path.getmtime(os.path.join(self.dirs[key], f)),
                        reverse=True)
        except Exception:
            return []
        return [f for f in fs if self._is_job_file(f)][:limit]

    # ---------------------------------------------------------------- 最近一次结果
    def write_last(self, job):
        try:
            with open(self.last_job_file, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
        except Exception:
            pass

    def read_last(self):
        try:
            with open(self.last_job_file, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    # ---------------------------------------------------------------- 清理
    def cleanup(self, keep_done_days=7, keep_fail_days=30):
        """归档目录按天清理。"""
        now = time.time()
        n = 0
        for key, days in (("done", keep_done_days), ("sim", keep_done_days),
                          ("failed", keep_fail_days), ("hold", keep_fail_days)):
            try:
                for f in os.listdir(self.dirs[key]):
                    p = os.path.join(self.dirs[key], f)
                    try:
                        if os.path.isfile(p) and (now - os.path.getmtime(p)) > days * 86400:
                            os.remove(p)
                            n += 1
                    except Exception:
                        pass
            except Exception:
                pass
        return n
