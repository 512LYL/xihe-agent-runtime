# -*- coding: utf-8 -*-
"""打印机状态口径（Windows 状态位 + HP EWS 权威状态）

这一层是精简版最值钱的部分：生产版曾因为「只看队列水位、不看状态位」，
把「缺纸 + 未出纸」误判成成功并归档；又因为「缺纸位一律当硬错误」，
把「打完最后一页、纸盒变空」误判成失败。两条教训在这里定成硬规则：

  1. 只有 `printed` 才算成功；`submitted`（未确认出纸）单独进 `_队列`。
  2. EWS 是权威状态源：EWS 报 ready / inPowerSave / processing 时，
     Windows / Job 的报错只降级为**软告警**，不许判失败。
  3. 缺纸（PAPER_OUT 0x10 / EWS paperOut）只算软告警，绝不单独判失败。
  4. 判失败前必须同时看：Windows 全局 Status、本次 Job.Status、EWS StatusCategory。
"""
# ---------------------------------------------------------------- Windows 位
STATUS_BITS = [
    (0x00000001, "PAUSED", "打印机已暂停"),
    (0x00000002, "ERROR", "打印机报错"),
    (0x00000008, "PAPER_JAM", "卡纸"),
    (0x00000010, "PAPER_OUT", "缺纸"),
    (0x00000020, "PAPER_PROBLEM", "纸张问题"),
    (0x00000080, "OFFLINE", "打印机离线"),
    (0x00000800, "OUTPUT_BIN_FULL", "出纸盒满"),
    (0x00001000, "NOT_AVAILABLE", "打印机不可用"),
    (0x00020000, "TONER_LOW", "墨粉不足"),
    (0x00040000, "NO_TONER", "无墨粉"),
    (0x00100000, "USER_INTERVENTION", "需要人工干预"),
    (0x00400000, "DOOR_OPEN", "舱门未关"),
    (0x00800000, "SERVER_UNKNOWN", "打印服务状态未知"),
]

# 硬错误：出现即不得判成功（PAUSED 只作提示，见 README「已知取舍」）
STATUS_HARD = {"ERROR", "PAPER_JAM", "PAPER_PROBLEM", "OFFLINE",
               "OUTPUT_BIN_FULL", "NO_TONER", "USER_INTERVENTION", "DOOR_OPEN"}
# 软告警：只记录，绝不单独作为失败依据
STATUS_SOFT = {"PAPER_OUT"}

# ---------------------------------------------------------------- EWS 类别
EWS_IDLE = {"ready", "inPowerSave"}
EWS_BUSY = {"processing"}
EWS_AUTHORITATIVE_OK = EWS_IDLE | EWS_BUSY
EWS_SOFT = {"paperOut", "mediaEmpty"}
EWS_ERROR = {"paperOut", "jam", "coverOpen", "noToner", "tonerLow",
             "outputBinFull", "mediaEmpty", "mediaJam", "doorOpen",
             "offline", "serviceRequest", "error", "interventionRequired"}
EWS_TEXT = {
    "paperOut": "缺纸", "mediaEmpty": "缺纸", "jam": "卡纸", "mediaJam": "卡纸",
    "coverOpen": "舱门未关", "doorOpen": "舱门未关",
    "noToner": "无墨粉", "tonerLow": "墨粉不足",
    "outputBinFull": "出纸盒满", "offline": "打印机离线",
    "serviceRequest": "需要维修", "error": "打印机报错",
    "interventionRequired": "需要人工干预", "processing": "打印机正忙",
    "inPowerSave": "打印机休眠", "ready": "就绪", "unknown": "未知",
}

# ---------------------------------------------------------------- 结果取值
RESULT_PRINTED = "printed"        # 真机确认出纸
RESULT_SIMULATED = "simulated"    # 模拟模式完成（未接触任何设备）
RESULT_SUBMITTED = "submitted"    # 未确认出纸（进 _队列，等人确认）
RESULT_FAILED = "failed"
RESULT_CANCELLED = "cancelled"

RESULT_TEXT = {
    RESULT_PRINTED: "已打印",
    RESULT_SIMULATED: "模拟打印完成",
    RESULT_SUBMITTED: "未确认出纸",
    RESULT_FAILED: "打印失败",
    RESULT_CANCELLED: "已取消",
}

# 结果 → 暂存目录键（状态机核心一张表）
RESULT_DIR = {
    RESULT_PRINTED: "done",
    RESULT_SIMULATED: "sim",
    RESULT_SUBMITTED: "hold",
    RESULT_FAILED: "failed",
    RESULT_CANCELLED: "failed",
}


def decode_status(st):
    """把 Windows `Status` 位掩码拆成 (problems, hard, soft) 三个中文/代号列表。"""
    problems, hard, soft = [], [], []
    st = int(st or 0)
    for bit, code, cn in STATUS_BITS:
        if not (st & bit):
            continue
        problems.append(code)
        if code in STATUS_HARD:
            hard.append(cn)
        elif code in STATUS_SOFT:
            soft.append(cn)
    return problems, hard, soft


def ews_text(cat):
    return EWS_TEXT.get(cat or "unknown", cat or "未知")


def receipt_text(orig, result, reason=""):
    """给控制台/API 显示的一句中文回执（生产版这一步是发给聊天窗口的）。"""
    if result == RESULT_PRINTED:
        return "✅ 已打印：%s（已见 processing、队列清空、状态位无硬错误）" % orig
    if result == RESULT_SIMULATED:
        return ("🧪 模拟打印完成：%s（模拟模式，未接触任何设备；"
                "判定逻辑与真机一致）" % orig)
    if result == RESULT_SUBMITTED:
        return ("🖨 未确认出纸：%s（%s）\n已留在 _队列，不会归档为已打印；"
                "请检查打印机后用 retry 重投。" % (orig, reason or "原因未知"))
    if result == RESULT_CANCELLED:
        return ("⚠️ 已取消：%s（%s）\n文件已移入 _失败，不会归档为已打印。"
                % (orig, reason or "队列被 clear 清空"))
    return "❌ 打印失败：%s（%s）" % (orig, reason or "打印机不可用")
