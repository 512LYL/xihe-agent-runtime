import sys
# 关键：添加 D 盘 Python 包路径
sys.path.append(os.environ.get("XIHE_EXTRA_SITE_PACKAGES", ""))
import os
# ----------------------------------------------------------------
# 运行期数据根目录：由环境变量 XIHE_RUNTIME_ROOT 决定；未设置时落在
# 本仓库内的 .runtime\ 目录 —— 公开仓库不含任何本机绝对路径。
# ----------------------------------------------------------------
_RUNTIME_ROOT = os.path.abspath(os.environ.get(
    "XIHE_RUNTIME_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".runtime")))
os.makedirs(_RUNTIME_ROOT, exist_ok=True)


import asyncio
import base64
import html
import re
import time
import json
import logging
import requests
from aiocqhttp import CQHttp, Event

# 配置日志
logging.basicConfig(
    filename=os.path.join(_RUNTIME_ROOT, "gateway_qq.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("qq_bridge")

# 反向 WS：aiocqhttp 作为客户端连 NapCat 的正向 WS server(默认 3001)
# 若 NapCat 实际端口不同，请同步修改下方 ws_reverse_url
bot = CQHttp(ws_reverse_url="ws://127.0.0.1:3002", access_token="")

SELF_QQ = None          # 首条事件 self_id 自动填充（即机器人自身 QQ，运行期获得）
MAX_HISTORY = 10
sessions = {}           # key: user_id 或 f"group:{group_id}"
convo_locks = {}        # key -> asyncio.Lock：每会话串行队列（防并发撞 Gateway session 写锁）

OWNER_NAME = os.environ.get("XIHE_OWNER_NAME", "主人")
SYSTEM_PROMPT = ("你是独属于%s的 AI Agent 运行底座助手，通过 QQ 与用户对话，可以处理一些问题。"
                 "请简洁、友好地回答用户的问题。" % OWNER_NAME)

# 测试版 Gateway 端口：优先从环境变量读取，默认 18791（测试版），与 .openclaw_exp 对应
GATEWAY_PORT = os.environ.get("OPENCLAW_GATEWAY_PORT", "18791")
OPENCLAW_URL = f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions"

# === 身份权限配置 ===
# 主人（人类）QQ 白名单：拥有完整权限，可触发 Gateway agent 的全部工具调用。
# 其他所有 QQ 号均为“普通用户”，只能闲聊，从机制上无法调用任何工具。
OWNER_QQ = 0

# 普通用户：纯聊天模式提示词（明确禁止任何文件操作 / 系统命令 / 工具调用）
CHAT_ONLY_PROMPT = (
    "你是一个友好的AI助手，只能进行普通的文字聊天。"
    "你不能执行任何文件操作、系统命令、工具调用或其他任务，"
    "只需友好地陪用户闲聊、回答一般性问题。"
)

# 普通用户直连的本地 LLM 端点（绕过 Gateway agent，从机制上杜绝工具调用）。
# 该端点为 OpenAI 兼容的纯对话接口，不运行 agent，因此模型无法触发任何工具。
CHAT_ONLY_URL = "http://127.0.0.1:11443/v1/chat/completions"
CHAT_ONLY_MODEL = "deepseek-chat"
# 测试版 deepseek key，与 .openclaw_exp 配置一致（local_llm_bridge_测试版.py 后端）
CHAT_ONLY_KEY = "<DEEPSEEK_API_KEY>"

# === 超时策略（2026-09-02 二次修订）===
# 第一版用关键词白名单预测任务长短，实测命中率仅 1/3：换个说法的长任务必然漏判，
# 掉回 60 秒被掐断（19:01 / 19:04 两次 abort 均因此而起）。任务耗时无法从措辞可靠推断，
# 故改为两段式超时，不再做任何预测：
#   connect = 10s  —— Gateway 未启动 / 网络不通时快速失败，不必干等
#   read    = 300s —— 连上之后给足时间，复杂任务（多步工具调用）通常需 60-180 秒
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 300
HTTP_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# 请求超过这个秒数仍未返回，就先给用户发一条进度提示。
# 按真实耗时触发（而非预判）：短任务不受打扰，长任务一定有反馈。
NOTICE_AFTER = 8.0

# ============================================================
# 图片（多模态）支持（2026-09-14 新增）
# QQ 消息里的 [CQ:image,...] 提取 → 下载/读本地 → base64 → image_url 块，
# 直接交给 Gateway(18791) 的 agent 轮次（网关已声明 deepseek 模型 input 含 image）。
# 前置：openclaw.json 中 models.providers.deepseek.models[0].input = ["text","image"]
# ============================================================
MAX_IMAGE_BYTES = 32 * 1024 * 1024     # 单图上限 32MB
MAX_BODY_BYTES = 48 * 1024 * 1024      # 请求体上限 48MB（base64 后约 1.33x）
IMG_DOWNLOAD_TIMEOUT = 30              # 单图下载超时(秒)
MAX_IMAGES = 8                         # 单条消息最多处理的图片数

CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]*)\]")
_EXT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
}


def _cq_params(raw):
    """把 CQ 码参数串解析成 dict。"""
    d = {}
    for kv in raw.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def _guess_mime(data, mime):
    """优先用响应头，缺失/不对时按魔数猜。"""
    if mime and mime.startswith("image/"):
        return mime
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def _fetch_image(params):
    """按 url（优先）或本地 file 路径取图，返回 (bytes, mime)。取不到抛异常。"""
    url = html.unescape(params.get("url") or "")
    if url.startswith("http://") or url.startswith("https://"):
        r = requests.get(url, timeout=IMG_DOWNLOAD_TIMEOUT)
        r.raise_for_status()
        return r.content, (r.headers.get("Content-Type") or "").split(";")[0].strip()
    fp = html.unescape(params.get("file") or "")
    if fp and os.path.exists(fp):
        with open(fp, "rb") as f:
            return f.read(), _EXT_MIME.get(os.path.splitext(fp)[1].lower().lstrip("."), "")
    raise RuntimeError("no usable url/file")


def extract_images(raw_msg):
    """从 CQ 串里提取图片。
    返回 (去掉图片码的文本, [data_url...], [错误说明...])。"""
    text = raw_msg or ""
    data_urls, errs = [], []
    matches = list(CQ_IMAGE_RE.finditer(raw_msg or ""))
    for i, m in enumerate(matches):
        if i >= MAX_IMAGES:
            errs.append("图片超过 %d 张，其余已忽略" % MAX_IMAGES)
            break
        try:
            data, mime = _fetch_image(_cq_params(m.group(1)))
            if not data:
                errs.append("图片为空")
                continue
            if len(data) > MAX_IMAGE_BYTES:
                errs.append("图片过大 %.1fMB（上限 32MB）" % (len(data) / 1048576))
                continue
            mime = _guess_mime(data, mime)
            data_urls.append("data:%s;base64,%s" % (mime, base64.b64encode(data).decode()))
        except Exception as e:
            errs.append("图片获取失败：%s" % str(e)[:80])
    return CQ_IMAGE_RE.sub("", text).strip(), data_urls, errs


def build_owner_content(text, images, errs):
    """拼主人轮次的 content。无图=纯文本；有图=多模态块。
    返回 (content, 需要发给用户的错误提示或 None)。"""
    if not images:
        if errs:
            return text, "[图片] " + "；".join(errs)
        return text, None
    body = len(text.encode("utf-8")) + sum(len(u) for u in images)
    if body > MAX_BODY_BYTES:
        return None, ("[图片] 附件过大（约 %.1fMB，上限 48MB），请压缩后再发。"
                      % (body / 1048576))
    blocks = [{"type": "text", "text": text or "请看看这张图片。"}]
    for u in images:
        blocks.append({"type": "image_url", "image_url": {"url": u}})
    warn = ("[图片] 部分未处理：" + "；".join(errs)) if errs else None
    return blocks, warn


async def post_with_progress(event, url, payload, headers=None):
    """发送阻塞式 POST；若超过 NOTICE_AFTER 秒仍未返回，先发进度提示再继续等待结果。"""
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(
        None,
        lambda: requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT),
    )
    done, _ = await asyncio.wait([fut], timeout=NOTICE_AFTER)
    if not done:
        try:
            await bot.send(event, "收到，正在处理，复杂任务可能需要 1-3 分钟……")
        except Exception:
            pass
    return await fut

# 普通用户消息实时通知主人（“陌生人提醒”）
NOTIFY_OWNER = True            # 是否把普通用户发来的消息立即私聊通知主人
NOTIFY_OWNER_COOLDOWN = 0      # 同一用户通知最小间隔(秒)：0=每条都通知；>0 可防止刷屏
_last_notify = {}             # 运行时记录各用户上次通知时间


async def notify_owner_of_stranger(uid, msg, mtype, event):
    """普通用户给机器人发消息时，立即把来源与内容私聊通知主人。"""
    if not NOTIFY_OWNER:
        return
    if NOTIFY_OWNER_COOLDOWN > 0:
        now = time.time()
        if now - _last_notify.get(uid, 0) < NOTIFY_OWNER_COOLDOWN:
            return
        _last_notify[uid] = now
    sender = getattr(event, "sender", None) or {}
    nickname = sender.get("nickname") or sender.get("card") or "未知"
    scope = f"群 {event.group_id}" if mtype == "group" else "私聊"
    text = (f"【陌生人消息提醒】\n来源：{scope}\n"
            f"QQ：{uid}（{nickname}）\n内容：{msg[:200]}")
    try:
        await bot.send_private_msg(user_id=OWNER_QQ, message=text)
        log.info(f"已通知主人: {text[:60]}")
    except Exception as e:
        log.warning(f"通知主人失败: {e}")


async def _owner_call(event, key, content):
    """主人请求 Gateway 完成一次 agent 轮次。
    由 handle_message 内每会话 asyncio.Lock 包裹调用，保证同会话串行、不并发撞写锁。
    content 可为纯文本字符串，或多模态块数组（含 image_url）。
    返回回复文本；出错返回 None（已向用户发送对应错误提示）。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": content})
    # key 独立化：qq:/qqg: 前缀，让 QQ 拥有独立 session 文件，不与网页 main session 共享
    _sess_key = ("qqg:" + str(event.group_id)) if getattr(event, "message_type") == "group" else ("qq:" + str(event.user_id))
    _sess_headers = {"x-openclaw-session-key": _sess_key}
    try:
        resp = await post_with_progress(
            event,
            OPENCLAW_URL,
            {"model": "openclaw", "messages": messages, "stream": False},
            headers=_sess_headers,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError as e:
        # ConnectionError 必须先于 Timeout：ConnectTimeout 同时继承两者，语义是"连不上服务"
        log.error(f"[主人] 连接失败 {key}: {e}")
        try:
            await bot.send(event, "[网络] 无法连接到 AI 服务，请检查 Gateway(18791) 是否已启动。")
        except Exception:
            pass
        return None
    except requests.exceptions.Timeout as e:
        log.error(f"[主人] 任务超时 {key} (read={READ_TIMEOUT}s): {e}")
        try:
            await bot.send(event, f"[超时] 任务处理超过 {READ_TIMEOUT} 秒仍未完成，请稍后重试或换个问法。")
        except Exception:
            pass
        return None
    except Exception as e:
        log.error(f"[主人] Error handling {key}: {e}")
        try:
            await bot.send(event, f"[错误] {str(e)[:100]}")
        except Exception:
            pass
        return None


@bot.on_message()
async def handle_message(event: Event):
    """处理收到的 QQ 消息"""
    global SELF_QQ
    if SELF_QQ is None:
        SELF_QQ = event.self_id
        log.info(f"self_id 识别为 {SELF_QQ}")

    uid = event.user_id
    msg = event.message
    mtype = event.message_type

    # 权限判定与来源记录
    is_owner = (uid == OWNER_QQ)
    log.info(f"消息来源: user_id={uid}, 权限: {'主人' if is_owner else '普通用户'}")

    # 群聊仅响应 @ 本机器人，避免死循环
    if mtype == "group":
        if f"[CQ:at,qq={SELF_QQ}]" not in (event.raw_message or ""):
            return
        msg = msg.replace(f"[CQ:at,qq={SELF_QQ}]", "").strip()
        key = f"group:{event.group_id}"
    else:
        key = str(uid)

    if not msg:
        return

    # 提取图片（CQ:image）→ 文本 + 多模态素材
    text_msg, images, img_errs = extract_images(msg)
    if images or img_errs:
        log.info("图片已提取: %d 张%s" % (len(images), ("，异常 %d: %s" % (len(img_errs), img_errs)) if img_errs else ""))
    if not text_msg and not images:
        if img_errs:
            try:
                await bot.send(event, "[图片] " + "；".join(img_errs))
            except Exception:
                pass
        return

    # 普通用户（非主人）发消息 → 立即私聊通知主人
    if not is_owner:
        await notify_owner_of_stranger(uid, text_msg or "[图片]", mtype, event)

    hist = sessions.setdefault(key, [])

    if is_owner:
        # ===== 主人：完整 agent（含全部工具调用能力）=====
        # 每会话串行队列：同会话并发消息排队处理（asyncio.Lock），
        # 排队中先回"收到"，避免"以为没收到→补发→串行→吞消息"，
        # 也避免多条同时打同一 Gateway session 撞写锁。
        _lock = convo_locks.setdefault(key, asyncio.Lock())
        if _lock.locked():
            try:
                await bot.send(event, "收到，正在处理上一条消息，这条我排队稍后回复～")
            except Exception:
                pass
        async with _lock:
            content, warn = build_owner_content(text_msg, images, img_errs)
            if content is None:
                await bot.send(event, warn)
                return
            if warn:
                try:
                    await bot.send(event, warn)
                except Exception:
                    pass
            reply = await _owner_call(event, key, content)
        if reply is None:
            return
    else:
        # ===== 普通用户：直连 LLM，绕过 agent，从机制上无法调用任何工具 =====
        if images:
            try:
                await bot.send(event, "[图片] 普通用户模式暂不支持图片识别，请用文字提问。")
            except Exception:
                pass
            return
        chat_messages = [{"role": "system", "content": CHAT_ONLY_PROMPT}]
        # 仅保留 user/assistant 历史，排除任何工具调用记录
        for m in hist:
            if m.get("role") in ("user", "assistant"):
                chat_messages.append(m)
        chat_messages.append({"role": "user", "content": text_msg})
        try:
            resp = await post_with_progress(
                event,
                CHAT_ONLY_URL,
                {"model": CHAT_ONLY_MODEL, "messages": chat_messages, "stream": False},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {CHAT_ONLY_KEY}",
                },
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.ConnectionError as e:
            log.error(f"[普通用户] 连接失败 {key}: {e}")
            try:
                await bot.send(event, "抱歉，AI 服务暂时不可用，请稍后再试。")
            except Exception:
                pass
            return
        except requests.exceptions.Timeout as e:
            log.error(f"[普通用户] 超时 {key} (read={READ_TIMEOUT}s): {e}")
            try:
                await bot.send(event, "抱歉，这个问题我需要多一点时间，请稍后再问一次。")
            except Exception:
                pass
            return
        except Exception as e:
            log.error(f"[普通用户] Error handling {key}: {e}")
            try:
                await bot.send(event, "抱歉，我暂时无法回复，请稍后再试。")
            except Exception:
                pass
            return

    # 更新会话历史（仅保留最近 MAX_HISTORY 轮）
    hist.append({"role": "user", "content": text_msg or "[图片]"})
    hist.append({"role": "assistant", "content": reply})
    if len(hist) > MAX_HISTORY * 2:
        sessions[key] = hist[-MAX_HISTORY * 2:]

    await bot.send(event, reply)
    log.info(f"{key}: {(text_msg or '[图片]')[:50]} -> {len(reply)} chars")


# ============================================================
# 主动发送能力（2026-09-03 新增）
# 供外部（如 OpenClaw 定时任务 cron）主动推送消息到 QQ，不依赖收到的消息事件。
# 复用 aiocqhttp 已建立的 NapCat 连接；通过 bot.server_app(Quart) 暴露 HTTP 端点。
# 发送身份为机器人自身账号（小号），不会与主人的会话冲突。
# ============================================================
from quart import request  # noqa: E402


def _resolve_target(target):
    """解析目标。支持: 'user:12345' | 'private:12345' | 'group:67890' | 'g:67890' | 纯数字(=私聊)。"""
    t = str(target).strip()
    if ":" in t:
        kind, _, tid = t.partition(":")
        return kind.lower(), tid.strip()
    return "user", t


async def send_qq_message(user_id, message):
    """向指定 QQ 私聊发送消息（主动外发）。"""
    await bot.send_private_msg(user_id=int(user_id), message=str(message))
    log.info(f"[proactive] private -> {user_id}: {str(message)[:60]}")


async def send_group_message(group_id, message):
    """向指定 QQ 群发送消息（主动外发）。"""
    await bot.send_group_msg(group_id=int(group_id), message=str(message))
    log.info(f"[proactive] group -> {group_id}: {str(message)[:60]}")


@bot.server_app.route("/send", methods=["POST", "GET"])
async def _http_send():
    """HTTP 主动发送端点。
    GET  http://127.0.0.1:8080/send?target=user:0&message=hi
    POST http://127.0.0.1:8080/send   body: {"target": "user:0", "message": "hi"}
    target 支持 user:/private:/group:/g:/纯数字(=私聊)。"""
    if request.method == "GET":
        target = request.args.get("target", "")
        message = request.args.get("message", "")
    else:
        body = await request.get_json(force=True, silent=True) or {}
        target = body.get("target", "")
        message = body.get("message", "")
    if not target or not message:
        return {"ok": False, "error": "target and message required"}, 400
    kind, tid = _resolve_target(target)
    try:
        if kind in ("group", "g"):
            await send_group_message(tid, message)
        else:
            await send_qq_message(tid, message)
        return {"ok": True, "target": f"{kind}:{tid}"}
    except Exception as e:
        log.error(f"[proactive] send failed target={target}: {e}")
        return {"ok": False, "error": str(e)}, 500


if __name__ == "__main__":
    # bot.run 同时维持反向 WS 连接(连 NapCat 3002)与 HTTP server(端口 8080)
    log.info("QQ <-> OpenClaw 桥接启动，连接 NapCat ws://127.0.0.1:3002")
    bot.run(host="127.0.0.1", port=8080)
