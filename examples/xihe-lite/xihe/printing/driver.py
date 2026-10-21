# -*- coding: utf-8 -*-
"""打印链路编排（production 版 `print_daemon._process` 的重写）

一次任务的生命周期：

    ready 队列 → 认领（_printing）→ 等打印机可用 → 提交 → 等进队列
      → 出纸判定（_drain）→ 归档前再查一次 clear → 归档 → 写回执

出纸判定只有四种结果（判定规则见 `states.py` 顶部）：

    printed    见过 processing + 队列清空 + 无硬错误 + 静置复核通过
    simulated  同上，但走的是模拟模式后端（未接触设备）
    submitted  队列清空但从未见过 processing / 超时不确定 → `_队列`，不自动重打
    failed     硬错误（卡纸/门开/无墨粉/离线…，且 EWS 也说不出「可用/在打」）
    cancelled  被 clear 取消 → `_失败`，绝不进 `_已打印`

最关键的修复承自生产事故：**绝不再用 `result != "failed"` 当成功判据**。
"""
import os
import time

from . import states
from .backend import make_backend
from .staging import Staging


class PrintDriver:
    def __init__(self, cfg, backend=None, staging=None, log=None):
        from .. import logset
        self.cfg = cfg
        self.log = log or logset.get_logger(cfg, "print_daemon")
        p = cfg.get("print") or {}
        self.poll = float(p.get("poll_sec", 3))
        self.drain_poll = float(p.get("drain_poll", 2))
        self.drain_wait = float(p.get("drain_wait", 300))
        self.ready_wait = float(p.get("ready_wait", 600))
        self.grace = float(p.get("confirm_grace", 4))
        self.max_resubmit = int(p.get("max_resubmit", 3))
        self.job_appear_wait = float(p.get("job_appear_wait", 20))
        self.keep_done = int(p.get("keep_done_days", 7))
        self.keep_fail = int(p.get("keep_fail_days", 30))
        self.backend = backend or make_backend(cfg, self.log)[0]
        self.staging = staging or Staging(cfg)
        self.duplex = (cfg.get("printer") or {}).get("duplex") or "simplex"

    # ---------------------------------------------------------------- 等待可用
    def wait_ready(self, deadline):
        """等打印机「可用」再提交：有人在复印/打印时自动排队（返回 (bool, 原因)）。"""
        t0 = time.time()
        last, warned = "", False
        while time.time() < deadline:
            ews = self.backend.ews()
            cat = ews.get("cat") or "unknown"
            if cat in states.EWS_IDLE:
                if warned:
                    self.log.info("打印机已恢复可用，继续打印")
                return True, ""
            if cat in states.EWS_ERROR:
                last = states.ews_text(cat)
                if not warned:
                    self.log.info("打印机不可用（%s），自动排队等待…", last)
                    warned = True
            elif cat in states.EWS_BUSY:
                last = "打印机正忙（有人在用）"
                if not warned:
                    self.log.info("打印机正忙，自动排队等待…")
                    warned = True
            elif cat == "unknown":
                st = self.backend.queue()
                hard = st.get("hard") or []
                soft = st.get("soft") or []
                if not st.get("ok"):
                    # 连队列都读不到 → 没有可用打印机，快速失败并说明原因
                    return False, "读不到打印队列（%s）" % (st.get("msg") or "无打印机")
                if hard or soft:
                    last = "Windows 状态 0x%X（%s）" % (int(st.get("status") or 0),
                                                        "/".join(hard or soft))
                    if not warned:
                        self.log.info("EWS 读不到，但 Windows 报 %s，自动排队等待…", last)
                        warned = True
                elif time.time() - t0 > 30:
                    return True, "EWS 读不到状态，按可用处理（fail-open）"
            else:
                last = "状态：%s" % cat
            time.sleep(self.poll)
        return False, last or "状态未知"

    # ---------------------------------------------------------------- 等任务进队列
    def wait_job_appear(self, base, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.backend.queue()
            if st.get("ok") and int(st.get("jobs") or 0) > base:
                return st.get("jobs_detail") or [{"id": None, "doc": None}]
            time.sleep(min(1.0, self.drain_poll))
        return []

    # ---------------------------------------------------------------- 失败裁决
    def job_problems(self, st, name, job_ids=()):
        """只看**本次任务**的 Job.Status（全局 Status 可能还是 0，会漏）。"""
        ids = {str(x) for x in (job_ids or [])}
        hard, soft = [], []
        for j in (st.get("jobs_detail") or []):
            jid = j.get("id")
            hit = (jid is not None and str(jid) in ids) or \
                self.staging.doc_matches(j.get("doc"), name)
            if not hit:
                continue
            _, h, s = states.decode_status(j.get("status") or 0)
            hard += [x for x in h if x not in hard]
            soft += [x for x in s if x not in soft]
        return hard, soft

    def failure_verdict(self, st, cat, name, job_ids=(), prefix=""):
        """能不能判 failed —— **先看 EWS 权威状态，缺纸只算软告警**。

        返回 (verdict, reason, soft_notes, gate_blocked)
          verdict      "failed" 或 ""（本轮不判失败）
          soft_notes   本轮该记的中文软告警（调用方去重）
          gate_blocked True = 本该 failed，被 EWS 的「可用/在打」挡下并降级
        """
        win_status = int(st.get("status") or 0)
        jobs = st.get("jobs")
        hard = [x for x in (st.get("hard") or []) if x]
        soft = [x for x in (st.get("soft") or []) if x]
        jhard, jsoft = self.job_problems(st, name, job_ids)
        hard += [x for x in jhard if x not in hard]
        soft += [x for x in jsoft if x not in soft]

        ews_hard = []
        if cat in states.EWS_SOFT:
            cn = states.ews_text(cat)
            if cn not in soft:
                soft.append(cn)
        elif cat in states.EWS_ERROR:
            ews_hard.append(states.ews_text(cat))

        notes = ["%s（Windows 0x%X，jobs=%s，JobId=%s）" % (
            x, win_status, jobs, job_ids or "?") for x in soft]

        blockers = []
        for x in list(hard) + list(ews_hard):
            if x not in blockers:
                blockers.append(x)
        if not blockers:
            return "", "", notes, False
        if cat in states.EWS_AUTHORITATIVE_OK:
            notes.append("Windows/Job 报 %s（Windows 0x%X，jobs=%s），但 EWS 权威状态=%s"
                         " → 降级为软告警，不判失败"
                         % ("/".join(blockers), win_status, jobs, cat))
            return "", "", notes, True
        why = "%s打印机状态错误：%s（EWS %s，Windows 0x%X，jobs=%s，JobId=%s）" % (
            prefix, "/".join(blockers), cat, win_status, jobs, job_ids or "?")
        return "failed", why, notes, False

    def _log_soft(self, notes, seen):
        for n in (notes or []):
            if n in seen:
                continue
            seen.add(n)
            self.log.warning("软告警（不判失败）：%s", n)

    # ---------------------------------------------------------------- 出纸判定
    def drain(self, base, deadline, name, claim_ts, job_ids=()):
        """等打印真正完成。返回 (result, reason)。"""
        seen_busy, last_cat, clean_since, soft_seen = False, None, None, set()
        while time.time() < deadline:
            hit = self.staging.cancel_hit(claim_ts, name, job_ids)
            if hit:
                self.log.warning("检测到 clear 取消信号：%s", hit)
                return states.RESULT_CANCELLED, hit
            time.sleep(self.drain_poll)
            st = self.backend.queue()
            ews = self.backend.ews()
            cat = ews.get("cat") or "unknown"
            if cat != last_cat:
                self.log.info("打印机状态变化：%s -> %s（Windows=0x%X jobs=%s）",
                              last_cat or "?", cat, int(st.get("status") or 0),
                              st.get("jobs"))
                last_cat = cat

            verdict, why, notes, _g = self.failure_verdict(st, cat, name, job_ids)
            self._log_soft(notes, soft_seen)
            if verdict == "failed":
                self.log.warning("打印中断：%s", why)
                return states.RESULT_FAILED, why

            if cat in states.EWS_BUSY:
                if not seen_busy:
                    self.log.info("打印机进入打印态（processing）")
                seen_busy, clean_since = True, None
                continue

            if st.get("ok") and int(st.get("jobs") or 0) <= base:
                hit = self.staging.cancel_hit(claim_ts, name, job_ids)
                if hit:
                    return states.RESULT_CANCELLED, hit
                if not seen_busy:
                    self.log.warning("队列已清空但打印机从未进入打印态 → 不判成功")
                    return states.RESULT_SUBMITTED, (
                        "任务从队列消失，但打印机从未进入打印态（可能被驱动丢弃或被 clear）")
                if clean_since is None:
                    clean_since = time.time()
                    self.log.info("队列已清空，静置 %.1fs 复核出纸状态…", self.grace)
                    continue
                if time.time() - clean_since < self.grace:
                    continue
                # ---- 复核：状态位 + 队列 + EWS + clear 信号
                hit = self.staging.cancel_hit(claim_ts, name, job_ids)
                if hit:
                    return states.RESULT_CANCELLED, hit
                st2 = self.backend.queue()
                cat2 = self.backend.ews().get("cat") or "unknown"
                v2, why2, notes2, _g2 = self.failure_verdict(
                    st2, cat2, name, job_ids, prefix="复核时发现")
                self._log_soft(notes2, soft_seen)
                if v2 == "failed":
                    self.log.warning("打印中断：%s", why2)
                    return states.RESULT_FAILED, why2
                if st2.get("ok") and int(st2.get("jobs") or 0) <= base:
                    if cat2 in states.EWS_IDLE:
                        self.log.info("打印任务已完成（EWS=%s、队列清空、Windows 无硬错误）", cat2)
                        return states.RESULT_PRINTED, ""
                    if cat2 == "unknown":
                        self.log.info("打印任务已完成（EWS 读不到，但已见 processing、"
                                      "队列清空、无硬错误）")
                        return states.RESULT_PRINTED, ""
                    if cat2 in states.EWS_SOFT:
                        # 正常完成：打完最后一页 → 纸盒变空 → EWS/Windows 报缺纸
                        self.log.info("打印任务已完成（EWS=%s=纸盒空，已见 processing 且"
                                      "队列清空，按正常完成处理，缺纸只作软告警）", cat2)
                        return states.RESULT_PRINTED, ""
                self.log.warning("复核未通过（EWS=%s jobs=%s），继续等待…",
                                 cat2, st2.get("jobs"))
                clean_since = None
                continue
            clean_since = None

        # ---- 超时：同样先看 EWS
        hit = self.staging.cancel_hit(claim_ts, name, job_ids)
        if hit:
            return states.RESULT_CANCELLED, hit
        st = self.backend.queue()
        cat = self.backend.ews().get("cat") or "unknown"
        verdict, why, notes, gated = self.failure_verdict(st, cat, name, job_ids,
                                                         prefix="超时时")
        self._log_soft(notes, soft_seen)
        if verdict == "failed":
            return states.RESULT_FAILED, why
        if cat in states.EWS_BUSY:
            return states.RESULT_SUBMITTED, "打印机仍在忙（可能有人在用，任务还在排队）"
        if gated:
            return states.RESULT_SUBMITTED, (
                "超时未确认出纸（EWS=%s 说可用/在打，Windows 报错已降级为软告警，未判失败）" % cat)
        probs = "/".join(st.get("problems") or [])
        tail = ("；软告警=%s（不判失败）" % "/".join(st["soft"])) if st.get("soft") else ""
        return states.RESULT_SUBMITTED, "超时未确认出纸（EWS=%s，Windows=0x%X%s，jobs=%s）%s" % (
            cat, int(st.get("status") or 0), ("/" + probs) if probs else "",
            st.get("jobs"), tail)

    # ---------------------------------------------------------------- 主流程
    def process(self, src):
        """处理 ready 队列里的一个文件，返回任务结果 dict（也写进 last_job.json）。"""
        claim_ts = time.time()
        name = os.path.basename(src)
        meta = self.staging.read_meta(src)
        claimed = self.staging.claim(src)
        if not claimed:
            return {"ok": False, "result": "skipped", "reason": "认领失败（已被其他进程取走）",
                    "name": name}
        orig = meta.get("orig") or name
        duplex = meta.get("sided") or self.duplex
        self.log.info("开始打印 %s（orig=%s，backend=%s，duplex=%s）",
                      name, orig, self.backend.name(), duplex)

        result, reason, detail, job_ids = states.RESULT_FAILED, "", "", []
        result_ts = time.time()
        for attempt in range(1, self.max_resubmit + 1):
            self.log.info("第 %d/%d 次尝试：等待打印机可用…", attempt, self.max_resubmit)
            ready, why = self.wait_ready(time.time() + self.ready_wait)
            if not ready:
                result, reason = states.RESULT_FAILED, "打印机长时间不可用：%s" % why
                self.log.warning("打印机不可用，放弃本次：%s", why)
                break
            st = self.backend.queue()
            base = int(st.get("jobs") or 0) if st.get("ok") else 0
            self.log.info("提交打印：%s（队列基线=%d）", name, base)
            ok, out = self.backend.submit(claimed, duplex=duplex)
            detail = out or ""
            self.log.info("打印后端返回：%s｜%s", "成功" if ok else "失败",
                          detail.strip().replace("\r", " ").replace("\n", " | ")[-300:])
            if not ok:
                self.log.warning("提交失败（第 %d 次）%s：%s", attempt, name, detail[-200:])
                self.backend.wake()
                time.sleep(min(2.0, 1.0 * attempt))
                continue
            new_jobs = self.wait_job_appear(base, self.job_appear_wait)
            if new_jobs:
                job_ids = [j.get("id") for j in new_jobs if j.get("id") is not None]
                self.log.info("任务已进入打印队列：JobId=%s", job_ids or "(演练模式无 JobId)")
                result, reason = self.drain(base, time.time() + self.drain_wait,
                                            name, claim_ts, job_ids)
                result_ts = time.time()
                break
            result, reason = states.RESULT_SUBMITTED, "任务未出现在打印队列（可能被驱动丢弃）"
            result_ts = time.time()
            break
        else:
            result, reason = states.RESULT_FAILED, reason or "提交脚本连续失败"

        # 归档前最后一道 clear 检查：被清掉的任务绝不进 `_已打印`
        hit = self.staging.cancel_hit(claim_ts, name, job_ids,
                                      until_ts=result_ts if result == states.RESULT_PRINTED
                                      else None)
        if hit and result != states.RESULT_FAILED:
            self.log.warning("归档前发现 clear 信号，改判 cancelled：%s", hit)
            result, reason = states.RESULT_CANCELLED, hit

        # 模拟模式：判定结果与真机一致，但必须与真机归档严格区分
        if result == states.RESULT_PRINTED and getattr(self.backend, "simulated", False):
            result = states.RESULT_SIMULATED

        if result == states.RESULT_SUBMITTED:
            errtext = "未确认出纸：%s\n已留在 _队列，请人工确认打印机后再 retry。" % (
                reason or "原因未知")
        elif result == states.RESULT_CANCELLED:
            errtext = "已被 clear 取消：%s\n未确认出纸，未归档为已打印。" % (
                reason or "队列被清空")
        elif result == states.RESULT_FAILED:
            errtext = "打印失败：%s\n%s" % (reason or "打印机不可用", (detail or "")[-800:])
        else:
            errtext = ""

        dest = self.staging.archive(claimed, result, errtext, detail)
        # 给界面看的相对路径（不把本机绝对路径打到看板/截图里）
        try:
            dest_rel = os.path.relpath(dest, self.staging.root) if dest else ""
        except Exception:
            dest_rel = os.path.basename(dest) if dest else ""
        ok = result in (states.RESULT_PRINTED, states.RESULT_SIMULATED)
        job = {
            "name": orig, "file": name, "ok": ok,
            "result": result, "result_text": states.RESULT_TEXT.get(result, result),
            "verified": result == states.RESULT_PRINTED,
            "simulated": getattr(self.backend, "simulated", False),
            "backend": self.backend.name(),
            "reason": "" if ok else (reason or (detail or "")[-200:]),
            "dest": dest, "dest_rel": dest_rel, "job_ids": job_ids,
            "ts": int(time.time()),
            "receipt": states.receipt_text(orig, result, reason),
        }
        self.staging.write_last(job)
        self.log.info("归档结果 %s：%s -> %s%s", result, name,
                      dest or "(归档失败)", ("（%s）" % reason) if reason else "")

        # 清理过期归档
        try:
            self.staging.cleanup(self.keep_done, self.keep_fail)
        except Exception:
            pass
        return job

    # ---------------------------------------------------------------- 循环
    def run_forever(self, should_stop=lambda: False):
        """守护主循环：FIFO 取 ready，串行处理。"""
        last_cleanup = 0.0
        while not should_stop():
            try:
                src = self.staging.next_ready()
                if not src:
                    time.sleep(self.poll)
                    continue
                self.log.info("发现待打印文件：%s", os.path.basename(src))
                self.process(src)
                if time.time() - last_cleanup > 3600:
                    last_cleanup = time.time()
                    self.staging.cleanup(self.keep_done, self.keep_fail)
            except Exception as e:
                self.log.error("循环异常：%s: %s", type(e).__name__, e)
                time.sleep(self.poll)
