# ============================================================
# 脚本名: bridge_local_llm.py
# 功能: 本地LLM桥接器——智能路由云端沐曦27B与本地Ollama 4B
#       联网时透传沐曦API（完整工具能力）
#       断网时智能评估：简单任务→本地4B，复杂任务→暂停+续传
# 依赖: Python 3.10+, requests, 标准库(http.server, threading, socket等)
# 用法: python bridge_local_llm.py  (监听 :11441)
# 日期: 2026-05-21
# ============================================================
#
# ============================================================
# 比赛专用版（基于桥接器源码修改）
# 改动：
# 1. MUXI_READ_TIMEOUT 30→90秒（防止沐曦误判超时）
# 2. 禁用 DeepSeek：只保留沐曦云端 + 本地 Ollama 两后端
# 3. 路由逻辑：沐曦失败→简单任务降级本地，复杂任务→暂停
# 4. 诊断快照标注 provider 字段
# 5. Gateway fallbacks 为空数组（降级由 Bridge 内部分配）
#
# 配套文件: openclaw_competition.json, 比赛版本说明.txt
# ============================================================

# ==================== 导入与常量 ====================
import json, requests, sys, io, re, time, uuid, logging, os, datetime, threading, socket, glob as globmod
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# tiktoken：精确token计数，用于上下文预检防止400错误
try:
    import tiktoken
    _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    _TIKTOKEN_AVAILABLE = True
except Exception:
    _TIKTOKEN_AVAILABLE = False

# 强制UTF-8编码环境（修复中文路径乱码问题）
os.environ["PYTHONIOENCODING"] = "utf-8"
# ----------------------------------------------------------------
# 运行期数据根目录：由环境变量 XIHE_RUNTIME_ROOT 决定；未设置时落在
# 本仓库内的 .runtime\ 目录 —— 公开仓库不含任何本机绝对路径。
# ----------------------------------------------------------------
_RUNTIME_ROOT = os.path.abspath(os.environ.get(
    "XIHE_RUNTIME_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".runtime")))
os.makedirs(_RUNTIME_ROOT, exist_ok=True)

os.environ["PYTHONUTF8"] = "1"

# 重定向stdout为UTF-8，防止print中文时崩溃
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Ollama的OpenAI兼容API地址（支持tool_calls返回格式）
OLLAMA_API = "http://127.0.0.1:11434/v1/chat/completions"
# 桥接器监听端口——Gateway通过此端口与桥接器通信
LISTEN_PORT = 11441
# 桥接器识别的模型名集合（Gateway请求这些模型时走桥接逻辑）
BRIDGE_MODELS = {"qwen2.5-7b-local", "qwen2.5-7b-tools", "qwen3.5-4b-iron", "qwen3.5-4b-local", "qwen3.5:4b", "gemma4:e4b"}

# ==================== 模型名映射表 ====================
# 桥接器模型名 -> Ollama实际模型名
# 所有qwen2.5-7b变体统一映射到当前Ollama中实际安装的qwen3.5:4b
# 原因：历史遗留模型名仍在Gateway配置中使用，但本地Ollama已升级为4B
OLLAMA_MODEL_MAP = {
    "qwen2.5-7b-tools": "qwen3.5:4b",
    "qwen2.5-7b-local": "qwen3.5:4b",
    "qwen3.5-4b-iron": "qwen3.5:4b",
    "qwen3.5-4b-local": "qwen3.5:4b",
    "qwen3.5:4b": "qwen3.5:4b",
    "gemma4:e4b": "gemma4:e4b",
}

# ==================== 沐曦云端API配置 ====================
MUXI_API = "https://ai.gitee.com/v1/chat/completions"
# API密钥从环境变量读取，避免硬编码泄露
# 设置环境变量: set MUXI_API_KEY=your_key_here
MUXI_API_KEY = os.environ.get("MUXI_API_KEY", "")
# 沐曦云端使用的模型——27B参数，能力远强于本地4B
MUXI_MODEL = "Qwen3.5-27B"
# 套餐ID从环境变量读取
# 设置环境变量: set MUXI_PACKAGE_ID=your_package_id_here
MUXI_PACKAGE_ID = os.environ.get("MUXI_PACKAGE_ID", "")
# 全局超时：5分钟（云端长任务如代码生成、长文总结需要更长时间）
MUXI_TIMEOUT_SECONDS = 300
# 连接超时：10秒——断网时快速失败，不让客户端久等
MUXI_CONNECT_TIMEOUT = 10
# 读取超时：30秒——stream两个chunk之间的最大等待，超过则判定连接中断
MUXI_READ_TIMEOUT = 90  # 比赛版：30→90秒，给沐曦27B更多思考时间
# =====================================================

# ==================== 智能降级阈值（可调） ====================
# 断网时判断任务复杂度的三个维度，任一超标即判定为"复杂任务"
# 复杂任务不交给本地4B（效果差），而是暂停等网络恢复后用云端27B

# 维度1：消息总token数。超过4000 token说明上下文很长，4B容易丢重点
COMPLEX_TOKEN_THRESHOLD = 4000
# 维度2：工具数量。超过2个工具调用说明需要多步操作，4B工具能力弱
COMPLEX_TOOL_COUNT_THRESHOLD = 2
# 维度3：用户消息字符数。超过500字符说明用户需求详细复杂
COMPLEX_MESSAGE_LENGTH = 500
# 复杂任务意图关键词——命中任何一个即判定为复杂
# 这些关键词暗示需要长文生成、深度分析等4B模型做不好的任务
COMPLEX_INTENT_KEYWORDS = ["写一篇", "生成PPT", "总结这篇长文", "写一个", "生成报告", "分析一下", "详细说明", "详细解释", "全面分析", "帮我写"]
# =====================================================

# ==================== 暂停任务配置 ====================
# 断网时复杂任务暂停后保存到此目录，网络恢复后可续传
PAUSED_TASKS_DIR = os.path.join(_RUNTIME_ROOT, "paused_tasks")
# 网络恢复检测间隔：每30秒探测一次网络是否恢复
NET_RECOVERY_CHECK_INTERVAL = 30
# =====================================================

# ==================== 网络连通性检测 ====================
# 缓存机制：避免每次请求都做TCP探测（耗时），1秒内复用上次结果
# 断网场景需要快速感知，所以缓存窗口只有1秒
_net_available = True      # 上次检测结果
_net_check_time = 0        # 上次检测时间戳
_net_check_lock = threading.Lock()  # 多线程安全锁

def is_network_available(force=False):
    """检测沐曦API(ai.gitee.com)是否可达
    - 带1秒缓存：1秒内的重复调用直接返回缓存结果
    - force=True：强制刷新，忽略缓存（网络恢复检测线程使用）
    - 使用TCP探测而非HTTP请求，更快更轻量
    """
    global _net_available, _net_check_time
    now = time.time()
    if not force:
        # 非强制模式：1秒内复用缓存
        with _net_check_lock:
            if now - _net_check_time < 1:
                return _net_available
            _net_check_time = now
    else:
        # 强制模式：直接更新时间戳，跳过缓存
        with _net_check_lock:
            _net_check_time = now
    try:
        # TCP连接探测：尝试连接ai.gitee.com的443端口(HTTPS)
        # 3秒超时——太长会影响请求响应速度
        sock = socket.create_connection(("ai.gitee.com", 443), timeout=3)
        sock.close()
        with _net_check_lock:
            _net_available = True
        return True
    except (socket.timeout, socket.error, OSError):
        # 任何网络错误都视为不可达
        with _net_check_lock:
            _net_available = False
        log.info("Network check: ai.gitee.com unreachable, using local model")
        return False

# ==================== 诊断日志系统 ====================
# 当桥接器出错时，保存完整的请求/响应快照用于事后排查
DIAG_LOG_DIR = os.path.join(_RUNTIME_ROOT, "diagnostics")
os.makedirs(DIAG_LOG_DIR, exist_ok=True)
os.makedirs(PAUSED_TASKS_DIR, exist_ok=True)

# 主日志文件：记录桥接器运行时的所有事件
logging.basicConfig(
    filename=os.path.join(_RUNTIME_ROOT, "bridge.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8"
)
log = logging.getLogger("bridge")

# 诊断日志专用logger——独立于主日志，只记录诊断事件
diag_logger = logging.getLogger("bridge_diag")
diag_handler = logging.FileHandler(
    os.path.join(DIAG_LOG_DIR, "diagnostic_incidents.log"),
    encoding="utf-8"
)
diag_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
diag_logger.addHandler(diag_handler)
diag_logger.setLevel(logging.INFO)
# 不向主logger传播，避免诊断日志在主日志中重复出现
diag_logger.propagate = False

def save_diagnostic_snapshot(incident_type, raw_data, messages, ollama_messages, tools,
                              model_id, error_msg, total_time_ms, ollama_status=None):
    """保存完整的诊断快照到独立JSON文件，用于事后云端排查

    参数:
        incident_type: 事件类型，如"timeout"/"ollama_crash"/"bridge_exception"
        raw_data: 原始请求数据（bytes或str）
        messages: OpenAI格式的原始消息列表
        ollama_messages: 转换后发给Ollama的消息列表
        tools: 工具定义列表
        model_id: 请求的模型名
        error_msg: 错误信息
        total_time_ms: 请求耗时（毫秒）
        ollama_status: Ollama健康状态字符串
        provider: 提供者标注（如"muxi/Qwen3.5-27B"或"local/qwen3.5:4b"）

    返回:
        (snapshot_id, filepath) 元组
    """
    # 生成唯一快照ID：时间戳+6位随机hex
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_id = f"{timestamp}_{uuid.uuid4().hex[:6]}"
    filename = f"snapshot_{snapshot_id}.json"
    filepath = os.path.join(DIAG_LOG_DIR, filename)

    # 构建快照数据结构
    snapshot = {
        "snapshot_id": snapshot_id,
        "incident_type": incident_type,
        "provider": provider or "unknown",
        "timestamp": datetime.datetime.now().isoformat(),
        "error_message": error_msg,
        "total_time_ms": total_time_ms,
        "model_id": model_id,
        "ollama_status_before": ollama_status,   # 请求前Ollama状态
        "request_summary": {
            "raw_messages_count": len(messages),
            "ollama_messages_count": len(ollama_messages),
            "tools_count": len(tools) if tools else 0,
            "raw_data_size_bytes": len(raw_data),
        },
        # 只记录每条消息的角色和内容长度，不记录全文（避免快照过大）
        "messages_roles": [{"role": m.get("role"), "content_len": len(m.get("content",""))} for m in messages],
        # 原始请求数据预览（最多2000字符）
        "raw_data_preview": raw_data[:2000].decode("utf-8", errors="replace") if isinstance(raw_data, bytes) else str(raw_data)[:2000],
        # 发给Ollama的完整payload（含转换后的消息）
        "ollama_payload": {
            "model": OLLAMA_MODEL_MAP.get(model_id, "qwen3.5:4b"),
            "messages": ollama_messages,
            "tools_injected": bool(tools),
            "tools_count": len(tools) if tools else 0,
        },
    }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        diag_logger.info(f"Diagnostic snapshot saved: {filename} ({incident_type})")
    except Exception as e:
        log.error(f"Failed to save diagnostic snapshot: {e}")

    return snapshot_id, filepath

# ==================== 输出过滤工具 ====================

def strip_thinking_tags(text):
    """过滤模型输出中的thinking标记和假工具调用

    某些模型（如qwen3.5）会在输出中包含<think/>推理过程和<action/>伪工具调用，
    这些对用户不可见也不需要，需要清除。

    清除内容:
    - <think...>...</think 推理过程（完整标签对）
    - <|im_start|>think...<|im_end|> 推理过程（ChatML格式）
    - 未闭合的<think...> 推理过程（模型输出被截断时）
    - <action>...</action> 伪工具调用
    - {"name":"xxx","arguments":{...}} 假工具调用JSON
    """
    # 完整的<think...>...</think标签对
    text = re.sub(r'<think[^>]*>.*?</think\s*>', '', text, flags=re.DOTALL)
    # ChatML格式的推理标签
    text = re.sub(r'<\|im_start\|>think\n.*?<\|im_end\|>', '', text, flags=re.DOTALL)
    # 未闭合的<think标签（模型输出被截断或正在思考中）
    text = re.sub(r'<think[^>]*>.*', '', text, flags=re.DOTALL)
    # 完整的<action>标签对
    text = re.sub(r'<action>.*?</action>', '', text, flags=re.DOTALL)
    # 未闭合的<action标签
    text = re.sub(r'<action>.*', '', text, flags=re.DOTALL)
    # 假工具调用JSON（模型试图调用工具但格式不对）
    text = re.sub(r'\n?\{"name"\s*:\s*"[^"]*"[^}]*"arguments"\s*:\s*\{[^}]*\}[^}]*\}', '', text, flags=re.DOTALL)
    return text.strip()

# ==================== Ollama健康检查 ====================

def check_ollama_health():
    """检查Ollama服务是否存活
    通过访问/api/tags获取模型列表来判断
    返回值示例: "alive (3 models)" / "error_code_503" / "unreachable: ..."
    """
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        if r.status_code == 200:
            models = r.json().get("models", [])
            return f"alive ({len(models)} models)"
        return f"error_code_{r.status_code}"
    except Exception as e:
        return f"unreachable: {str(e)[:80]}"

# ==================== 辅助函数 ====================

def extract_model_id(data):
    """从请求体中提取模型ID
    - 处理 "namespace/model" 格式，只取斜杠后的部分
    - 如果模型名不在BRIDGE_MODELS中，默认返回"qwen2.5-7b-tools"
    """
    model = data.get("model", "")
    if "/" in model:
        model = model.split("/", 1)[1]
    if model in BRIDGE_MODELS:
        return model
    return "qwen2.5-7b-tools"

def estimate_tokens(text):
    """精确计数文本的token数量

    优先使用tiktoken(cl100k_base编码)进行精确计数，
    tiktoken不可用时回退到估算（中文2.5:1 + 英文1.5:1）。
    """
    if _TIKTOKEN_AVAILABLE:
        try:
            return len(_tiktoken_enc.encode(text))
        except Exception:
            pass
    # 回退：粗略估算
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 2.5 + other_chars * 1.5)

# ==================== 智能任务评估 ====================
def is_task_suitable_for_local(messages, tools=None):
    """评估断网时任务是否适合本地模型处理

    评估维度（任一超标即判定为复杂，返回False）:
    1. 消息总token数 > COMPLEX_TOKEN_THRESHOLD (4000)
    2. 工具数量 > COMPLEX_TOOL_COUNT_THRESHOLD (2)
    3. 用户消息字符数 > COMPLEX_MESSAGE_LENGTH (500)
    4. 用户消息包含复杂意图关键词

    返回:
        True  = 简单任务，可切本地4B
        False = 复杂任务，应暂停等网络恢复后用云端27B
    """
    # 维度1：消息总token数
    total_tokens = sum(estimate_tokens(m.get("content", "")) for m in messages)
    if total_tokens > COMPLEX_TOKEN_THRESHOLD:
        log.info(f"Task assessment: COMPLEX (tokens={total_tokens} > {COMPLEX_TOKEN_THRESHOLD})")
        return False

    # 维度2：工具数量
    tool_count = len(tools) if tools else 0
    if tool_count > COMPLEX_TOOL_COUNT_THRESHOLD:
        log.info(f"Task assessment: COMPLEX (tools={tool_count} > {COMPLEX_TOOL_COUNT_THRESHOLD})")
        return False

    # 维度3：用户消息长度和复杂意图
    # 只检查最新一条用户消息（reversed找到第一条user即break）
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            # 消息太长 → 复杂
            if len(content) > COMPLEX_MESSAGE_LENGTH:
                log.info(f"Task assessment: COMPLEX (user_msg_len={len(content)} > {COMPLEX_MESSAGE_LENGTH})")
                return False
            # 包含复杂意图关键词 → 复杂
            for kw in COMPLEX_INTENT_KEYWORDS:
                if kw in content:
                    log.info(f"Task assessment: COMPLEX (intent keyword: '{kw}')")
                    return False
            break  # 只检查最近一条用户消息

    # 所有维度都未超标 → 简单任务
    log.info(f"Task assessment: SIMPLE (tokens={total_tokens}, tools={tool_count})")
    return True
# =====================================================

# ==================== 暂停任务管理 ====================
# 断网时复杂任务暂停后保存到磁盘，网络恢复后可续传
# 文件存储在 PAUSED_TASKS_DIR 下，每个任务一个JSON文件

def save_paused_task(task_id, raw_request, partial_output, model_id):
    """保存暂停的任务到磁盘

    参数:
        task_id: 任务唯一ID
        raw_request: 原始请求数据（dict或bytes）
        partial_output: 云端已输出的部分文本（用于续传时拼接）
        model_id: 请求的模型名
    """
    task_data = {
        "task_id": task_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "model_id": model_id,
        # 如果raw_request是bytes则解码为dict
        "raw_request": raw_request if isinstance(raw_request, dict) else json.loads(raw_request.decode("utf-8", errors="replace")),
        "partial_output": partial_output,
        "status": "paused"
    }
    filepath = os.path.join(PAUSED_TASKS_DIR, f"{task_id}.json")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(task_data, f, ensure_ascii=False, indent=2)
        log.info(f"Paused task saved: {task_id}")
    except Exception as e:
        log.error(f"Failed to save paused task {task_id}: {e}")

def load_paused_tasks():
    """加载所有未完成的暂停任务
    扫描PAUSED_TASKS_DIR下所有JSON文件，返回status为"paused"的任务列表
    """
    tasks = []
    for filepath in globmod.glob(os.path.join(PAUSED_TASKS_DIR, "*.json")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                task = json.load(f)
                if task.get("status") == "paused":
                    tasks.append(task)
        except Exception as e:
            log.error(f"Failed to load paused task {filepath}: {e}")
    return tasks

def mark_task_resumed(task_id):
    """标记任务已恢复（网络恢复后任务被重新提交时调用）
    将status从"paused"改为"resumed"，记录恢复时间
    """
    filepath = os.path.join(PAUSED_TASKS_DIR, f"{task_id}.json")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            task = json.load(f)
        task["status"] = "resumed"
        task["resumed_at"] = datetime.datetime.now().isoformat()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to mark task resumed {task_id}: {e}")
# =====================================================

# ==================== 网络恢复检测线程 ====================
# 后台守护线程：定期检测网络是否恢复
# 检测到从断网→联网的转换时，扫描暂停任务并生成恢复提示
_net_recovery_running = True

def network_recovery_watcher():
    """后台线程：定期检测网络恢复，扫描暂停任务并生成恢复通知

    工作流程:
    1. 每30秒强制检测一次网络连通性
    2. 如果检测到从"断网"→"联网"的转换（was_down=True→current=True）
    3. 扫描所有暂停任务
    4. 为每个暂停任务生成recovery_{task_id}.json通知文件
    5. Gateway/前端可通过 GET /v1/paused-tasks 查看恢复通知
    """
    global _net_available
    was_down = False  # 上一轮检测时网络是否断开

    while _net_recovery_running:
        try:
            # 强制刷新网络状态（忽略缓存）
            current = is_network_available(force=True)

            # 检测从断网到联网的转换
            if was_down and current:
                log.info("NETWORK RECOVERED: scanning paused tasks...")
                paused = load_paused_tasks()
                if paused:
                    log.info(f"Found {len(paused)} paused task(s), logging recovery notice")
                    for task in paused:
                        task_id = task.get("task_id", "unknown")
                        partial_len = len(task.get("partial_output", ""))
                        log.info(f"  Paused task: {task_id}, partial_output={partial_len} chars")
                        # 生成恢复通知文件——Gateway/前端可轮询 /v1/paused-tasks 查看
                        recovery_notice = {
                            "status": "resumed",
                            "task_id": task_id,
                            "message": f"网络已恢复，暂停任务 {task_id} 可继续。在聊天中回复'继续'即可。",
                            "partial_output_preview": task.get("partial_output", "")[:200],
                            "timestamp": datetime.datetime.now().isoformat()
                        }
                        notice_path = os.path.join(PAUSED_TASKS_DIR, f"recovery_{task_id}.json")
                        with open(notice_path, "w", encoding="utf-8") as f:
                            json.dump(recovery_notice, f, ensure_ascii=False, indent=2)

            # 更新上一轮状态：当前联网→was_down=False，当前断网→was_down=True
            was_down = not current

        except Exception as e:
            log.error(f"Network recovery watcher error: {e}")

        # 每30秒检测一次
        time.sleep(NET_RECOVERY_CHECK_INTERVAL)

# 启动网络恢复检测守护线程（主线程退出时自动结束）
recovery_thread = threading.Thread(target=network_recovery_watcher, daemon=True)
recovery_thread.start()
# =====================================================

# ==================== 上下文裁剪配置 ====================
# 本地4B模型上下文窗口有限，需要裁剪过大的system prompt和消息历史
MAX_SYSTEM_TOKENS = 4000   # system prompt最大token数
MAX_CONTEXT_TOKENS = 6000  # 非system消息总token上限

# ==================== 本地模型超时配置 ====================
# 本地模型推理可能较慢（尤其是长输出），7分钟超时
LOCAL_MODEL_TIMEOUT_SECONDS = 420
# ====================================================

def trim_system_prompt(content, has_tools=False):
    """裁剪过大的system prompt，按token估算而非字符数

    策略:
    1. 如果token数未超限，直接返回
    2. 如果has_tools=True（有工具定义）：
       - 找到"## Tooling"标记，保留到下一个章节标记之间的内容
       - 如果找不到标记，按70%比例逐步截断
    3. 如果has_tools=False（无工具）：
       - 找到"## Tooling"标记，截断并追加"纯对话模式"提示
       - 如果找不到标记，按70%比例逐步截断并追加[system prompt trimmed]标记
    """
    # 未超限，直接返回
    if estimate_tokens(content) <= MAX_SYSTEM_TOKENS:
        return content

    if has_tools:
        # 有工具时：保留Tooling章节（本地模型不使用tools参数，而是注入到system prompt）
        tooling_marker = "## Tooling"
        idx = content.find(tooling_marker)
        if idx > 0:
            # 找到Tooling章节的结束位置（下一个##章节标记）
            tooling_end_markers = ["## Instructions", "## Rules", "## Important"]
            end_idx = len(content)
            for marker in tooling_end_markers:
                pos = content.find(marker, idx + len(tooling_marker))
                if pos > 0:
                    end_idx = min(end_idx, pos)
            trimmed = content[:end_idx].strip()
            # 如果裁剪后仍然超限，按70%比例逐步缩小
            while estimate_tokens(trimmed) > MAX_SYSTEM_TOKENS and len(trimmed) > 500:
                trimmed = trimmed[:int(len(trimmed) * 0.7)].strip()
            return trimmed
        # 没找到Tooling标记，按70%比例逐步截断
        trimmed = content
        while estimate_tokens(trimmed) > MAX_SYSTEM_TOKENS and len(trimmed) > 500:
            trimmed = trimmed[:int(len(trimmed) * 0.7)].strip()
        return trimmed

    # 无工具时：直接截掉Tooling章节（本地模型用不上），追加纯对话模式提示
    tooling_marker = "## Tooling"
    idx = content.find(tooling_marker)
    if idx > 100:
        trimmed = content[:idx].strip()
        # 追加语言规则：确保模型用中文回复
        trimmed += "\n\nNote: You are running in text-only mode without tool access.\nIMPORTANT LANGUAGE RULE: Always respond in the EXACT SAME LANGUAGE as the user\'s most recent message. If the user writes in Chinese (中文), you MUST reply in Chinese. If they write in English, reply in English. Never switch languages."
        return trimmed

    # 都没有匹配标记，按70%比例逐步截断
    trimmed = content
    while estimate_tokens(trimmed) > MAX_SYSTEM_TOKENS and len(trimmed) > 300:
        trimmed = trimmed[:int(len(trimmed) * 0.7)].strip()
    return trimmed + "\n\n[system prompt trimmed]"


def build_ollama_messages(openai_messages, has_tools=False):
    """将OpenAI格式消息转换为Ollama兼容格式

    转换规则:
    1. tool角色消息 → 转为user角色，前缀[Tool Result]
    2. 非system/user/assistant角色 → 过滤掉
    3. content为list时 → 提取text部分合并
    4. assistant带tool_calls → 转为文本格式[Call Tool: name(args)]
    5. 过滤垃圾消息（失败消息、心跳等）
    6. system prompt → 裁剪+剥离工具定义（本地模型不支持tools参数）
    7. 合并连续同角色消息
    8. 上下文超限时保留system+最近的消息
    9. 追加身份规则后缀
    10. 确保最后一条是user消息
    """
    result = []
    for msg in openai_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", None)
        tool_call_id = msg.get("tool_call_id", None)

        # tool角色的消息：转为user角色，前缀[Tool Result]
        # Ollama GGUF模型不支持tool角色，需要转换
        if role == "tool":
            if content:
                if isinstance(content, bytes):
                    content = content.decode('utf-8', errors='replace')
                result.append({"role": "user", "content": f"[Tool Result]\n{content}"})
            continue

        # 过滤非标准角色
        if role not in ("system", "user", "assistant"):
            continue

        # content为list时（多模态格式），提取text部分合并
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)

        # assistant带tool_calls：转为文本格式描述
        # 本地Ollama不支持tool_calls参数，改为纯文本描述
        if role == "assistant" and tool_calls:
            tool_text_parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                args = fn.get("arguments", "{}")
                tool_text_parts.append(f"[Call Tool: {name}({args})]")
            combined = (content + "\n" if content else "") + "\n".join(tool_text_parts)
            result.append({"role": "assistant", "content": combined.strip()})
            continue

        # 过滤垃圾消息：失败消息、心跳等
        if role == "assistant" and content in (
            "[assistant turn failed before producing content]",
            "NO_REPLY",
            "HEARTBEAT_OK",
        ):
            continue

        # 过滤心跳轮询消息
        if role == "user" and content.strip() == "OpenClaw heartbeat poll":
            continue

        # system消息：裁剪+剥离工具定义
        if role == "system":
            content = trim_system_prompt(content, has_tools=False)
            # 尝试找到工具定义章节并截断
            stripped = False
            for marker in ["## Tooling", "## Tools", "# Tooling", "# Tools"]:
                marker_idx = content.find(marker)
                if marker_idx > 50:
                    content = content[:marker_idx].strip()
                    stripped = True
                    break
            if stripped:
                # 截断后追加提示：告诉模型没有工具可用
                content += "\n\n注意：当前运行在纯对话模式，没有工具可用。直接回答用户的问题，不要假装调用任何工具或输出工具调用格式。"
                log.info(f"Stripped tool section from system prompt for local model (marker found)")
            elif has_tools and ("function" in content or "tool" in content.lower()):
                # 没找到章节标记但内容中包含工具相关文本，按特征截断
                cut_patterns = ["{\"name\":", '"name":', "function:", "Available tools:"]
                for cp in cut_patterns:
                    cp_idx = content.find(cp)
                    if cp_idx > 200:
                        content = content[:cp_idx].strip()
                        content += "\n\n注意：当前运行在纯对话模式，没有工具可用。直接回答用户的问题，不要假装调用任何工具或输出工具调用格式。"
                        log.info(f"Stripped tool section from system prompt for local model (cut pattern: {cp[:20]})")
                        break

        # 清理assistant消息中的垃圾标记
        if role == "assistant":
            content = content.replace("NO_REPLY", "").replace("HEARTBEAT_OK", "").strip()

        # 过滤空消息（system除外，system可以为空框架）
        if not content and role != "system":
            continue

        # 合并连续同角色消息（Ollama要求角色交替出现）
        if result and result[-1]["role"] == role and role not in ("tool",):
            result[-1]["content"] += "\n" + content
        else:
            result.append({"role": role, "content": content})

    # 如果过滤后消息为空，添加一个默认消息
    if not result:
        result.append({"role": "user", "content": "Hello"})

    # 上下文超限裁剪：保留system消息+最近的非system消息
    total_tokens = sum(estimate_tokens(m["content"]) for m in result)
    if total_tokens > MAX_CONTEXT_TOKENS:
        # 先分出system和非system消息
        system_msgs = [m for m in result if m["role"] == "system"]
        non_system = [m for m in result if m["role"] != "system"]
        system_tokens = sum(estimate_tokens(m["content"]) for m in system_msgs)
        budget = MAX_CONTEXT_TOKENS - system_tokens  # 非system消息的token预算
        # 从最新消息开始保留（reversed），直到预算用完
        kept = []
        for msg in reversed(non_system):
            msg_tokens = estimate_tokens(msg["content"])
            if budget >= msg_tokens:
                kept.insert(0, msg)
                budget -= msg_tokens
            else:
                break  # 预算不够了，更早的消息也不保留
        result = system_msgs + kept
        log.info(f"Context trimmed: {total_tokens} -> {sum(estimate_tokens(m['content']) for m in result)} est tokens, {len(result)} messages")

    # 确保最后一条消息是user或tool角色（Ollama要求）
    if result[-1]["role"] not in ("user", "tool"):
        result.append({"role": "user", "content": "Please continue."})

    # 追加身份规则后缀——注入到system消息末尾
    identity_suffix = (
        "\n\n## 关键规则\n"
        "1. 你的名字是 Xihe，你是项目作者的 AI 助手。\n"
        "2. 你必须用中文回复。即使用户用英文提问，也要用中文回答。\n"
        "3. 不要输出思考过程、推理步骤或任何<think/>标签内容。直接给出最终回答。\n"
        "4. 不要假装调用工具或输出工具调用格式的文本。你是一个对话助手，直接回答问题。\n"
    )
    # 找到system消息并追加身份规则
    for msg in result:
        if msg["role"] == "system":
            msg["content"] += identity_suffix
            break
    else:
        # 没有system消息，在最前面插入
        result.insert(0, {"role": "system", "content": identity_suffix.strip()})

    return result


# ==================== HTTP请求处理器 ====================
class BridgeHandler(BaseHTTPRequestHandler):
    """桥接器HTTP服务器处理器

    核心职责：
    - POST /v1/chat/completions: 接收OpenAI兼容的chat请求，智能路由到云端或本地
    - GET /v1/models: 返回桥接器支持的模型列表
    - GET /health: 健康检查
    - GET /v1/paused-tasks: 查询暂停任务列表

    路由逻辑：
    1. 联网 → 透传沐曦云端API（完整工具能力）
    2. 断网 + 简单任务 → 本地Ollama 4B
    3. 断网 + 复杂任务 → 暂停，等网络恢复后续传
    """

    def log_message(self, format, *args):
        """重写默认的HTTP日志方法，输出到桥接器logger而非stderr"""
        msg = format % args
        log.info(f"HTTP: {msg}")

    def do_POST(self):
        """处理POST /v1/chat/completions请求——桥接器的核心入口

        请求流程:
        1. 解析请求体（OpenAI chat completion格式）
        2. 提取模型ID、是否stream
        3. 检测网络连通性
        4. 联网 → 调用_handle_muxi_request()透传云端
        5. 断网 + 简单任务 → 继续走本地Ollama逻辑
        6. 断网 + 复杂任务 → 调用_handle_task_paused()暂停
        7. 本地处理：工具裁剪、消息格式转换、stream/非stream分支
        """
        log.info(f"POST request: path={self.path}")
        if self.path == "/v1/chat/completions":
            try:
                # 读取并解析请求体
                content_length = int(self.headers.get('content-length', 0))
                raw_data = self.rfile.read(content_length)
                data = json.loads(raw_data.decode('utf-8'))

                # 提取关键参数
                model_id = extract_model_id(data)
                stream_requested = data.get("stream", False)

                # ========== 智能路由：联网→沐曦云端，断网→评估降级 ==========
                # 强制刷新网络状态（每个请求都重新检测）
                net_ok = is_network_available(force=True)
                if net_ok:
                    # 联网：直接透传到沐曦云端，完整工具能力
                    log.info(f"ROUTE: network available, forwarding to Muxi cloud ({MUXI_MODEL})")
                    self._handle_muxi_request(data, model_id, stream_requested, raw_data)
                    return
                else:
                    # ===== 断网：智能评估任务复杂度 =====
                    messages = data.get("messages", [])
                    tools = data.get("tools", None)

                    if is_task_suitable_for_local(messages, tools):
                        # 简单任务：继续走本地Ollama逻辑（下方）
                        log.info(f"ROUTE: network unavailable, task SIMPLE -> local Ollama (qwen3.5:4b)")
                    else:
                        # 复杂任务：暂停，等网络恢复
                        log.info(f"ROUTE: network unavailable, task COMPLEX -> pause with notice")
                        self._handle_task_paused(data, model_id, stream_requested, raw_data, "")
                        return
                # ============================================================

                # ===== 以下为本地Ollama处理逻辑 =====
                messages = data.get("messages", [])
                tools = data.get("tools", None)
                has_tools = tools is not None and len(tools) > 0

                # 工具数量裁剪：本地4B模型最多处理4个工具
                # 优先保留文件/目录/读写/搜索相关工具
                MAX_TOOLS = 4
                if has_tools and len(tools) > MAX_TOOLS:
                    priority_tools = []
                    other_tools = []
                    for t in tools:
                        fn_name = t.get("function", {}).get("name", "")
                        # 文件/目录/列表/读写/搜索相关工具优先级最高
                        if any(kw in fn_name.lower() for kw in ("file", "directory", "list", "read", "write", "search")):
                            priority_tools.append(t)
                        else:
                            other_tools.append(t)
                    # 先取优先工具，不够4个再补其他
                    tools = priority_tools[:MAX_TOOLS]
                    if len(tools) < MAX_TOOLS:
                        tools.extend(other_tools[:MAX_TOOLS - len(tools)])
                    log.info(f"Tools trimmed: {len(data.get('tools', []))} -> {len(tools)} (7B model limit)")

                # 映射到Ollama实际模型名
                ollama_model = OLLAMA_MODEL_MAP.get(model_id, "qwen3.5:4b")
                log.info(f"Request: model={model_id} -> ollama={ollama_model}, messages={len(messages)}, stream={stream_requested}, tools={len(tools) if tools else 0}")

                # 转换消息格式为Ollama兼容格式
                ollama_messages = build_ollama_messages(messages, has_tools=has_tools)
                log.info(f"Ollama messages: {len(ollama_messages)} (from {len(messages)} raw), roles: {[m['role'] for m in ollama_messages]}")

                # 生成响应ID
                chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

                # 本地模型不使用tools参数，工具定义已注入到system prompt
                if has_tools:
                    log.info(f"Tools injected into system prompt (Ollama GGUF does not support tools param): {len(tools)} tools")

                if stream_requested:
                    # ===== Stream模式：SSE流式输出 =====
                    request_start = time.time()

                    payload = {
                        "model": ollama_model,
                        "messages": ollama_messages,
                        "stream": True
                    }

                    # 请求前记录Ollama健康状态（用于诊断快照）
                    ollama_health_before = check_ollama_health()

                    # 向Ollama发送stream请求
                    ollama_resp = requests.post(OLLAMA_API, json=payload, stream=True,
                                                timeout=LOCAL_MODEL_TIMEOUT_SECONDS)
                    ollama_resp.encoding = 'utf-8'

                    # Ollama返回错误：构造错误响应
                    if ollama_resp.status_code != 200:
                        error_body = ollama_resp.text[:500]
                        log.error(f"Ollama error: {ollama_resp.status_code}: {error_body}")
                        ollama_resp.close()
                        err_resp = {
                            "id": chat_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model_id,
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": f"[Bridge Error] Ollama returned {ollama_resp.status_code}: {error_body}"},
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }
                        resp_body = json.dumps(err_resp, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                        return

                    # 发送SSE响应头
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()

                    # 发送SSE心跳chunk——防止客户端在Ollama首次响应前超时
                    heartbeat_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_id,
                        "choices": [{"index": 0, "delta": {"content": ""}}]
                    }
                    try:
                        self.wfile.write(f"data: {json.dumps(heartbeat_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass

                    log.info(f"SSE keepalive sent for {chat_id}")

                    # 流式转发Ollama响应
                    total_chars = 0           # 已输出字符数
                    saw_done = False          # 是否收到[DONE]标记
                    timeout_occurred = False  # 是否超时
                    last_chunk_time = time.time()
                    in_thinking = False       # 是否在<think块中
                    think_buffer = ""         # think块缓冲

                    iter_start = time.time()
                    try:
                        for line in ollama_resp.iter_lines(decode_unicode=True):
                            iter_elapsed = time.time() - iter_start

                            # 全局超时检查
                            if iter_elapsed > LOCAL_MODEL_TIMEOUT_SECONDS:
                                timeout_occurred = True
                                log.warning(f"LOCAL MODEL TIMEOUT after {LOCAL_MODEL_TIMEOUT_SECONDS}s")
                                break

                            if not line:
                                continue

                            if line.startswith("data: "):
                                chunk_str = line[6:]
                                if chunk_str == "[DONE]":
                                    # 流结束：如果在think块中则丢弃
                                    if in_thinking:
                                        in_thinking = False
                                        think_buffer = ""
                                        log.debug("Stream ended while in thinking block, discarded")
                                    saw_done = True
                                    self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                                    self.wfile.flush()
                                    continue
                                try:
                                    chunk_json = json.loads(chunk_str)
                                    # 替换ID和模型名为桥接器的
                                    chunk_json["id"] = chat_id
                                    chunk_json["model"] = model_id
                                    delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        content = delta["content"]

                                        # ===== 过滤<think推理过程 =====
                                        # 某些模型（qwen3.5）会输出推理过程，需要过滤掉
                                        if not in_thinking:
                                            if '<think' in content or '<|im_start|>think' in content:
                                                # 进入think块
                                                in_thinking = True
                                                think_buffer = content
                                                # think标签前可能有正常内容，需要输出
                                                pre_think = ""
                                                for tag in ['<think', '<|im_start|>think']:
                                                    idx = content.find(tag)
                                                    if idx >= 0:
                                                        pre_think = content[:idx]
                                                        break
                                                if pre_think.strip():
                                                    delta["content"] = pre_think
                                                    total_chars += len(pre_think)
                                                    out = f"data: {json.dumps(chunk_json, ensure_ascii=False)}\n\n"
                                                    self.wfile.write(out.encode("utf-8"))
                                                    self.wfile.flush()
                                                continue
                                        if in_thinking:
                                            think_buffer += content
                                            if '</think' in content or '<|im_end|>' in content:
                                                # think块结束，丢弃整个think内容
                                                in_thinking = False
                                                think_buffer = ""
                                                log.debug("Thinking block ended in stream, skipping")
                                            continue

                                        total_chars += len(content)
                                    # 转发SSE chunk到客户端
                                    out = f"data: {json.dumps(chunk_json, ensure_ascii=False)}\n\n"
                                    self.wfile.write(out.encode("utf-8"))
                                    self.wfile.flush()
                                    last_chunk_time = time.time()
                                except json.JSONDecodeError:
                                    # 非JSON行：直接透传
                                    self.wfile.write((line + "\n\n").encode("utf-8"))
                                    self.wfile.flush()
                            else:
                                # 非data行：直接透传
                                self.wfile.write((line + "\n\n").encode("utf-8"))
                                self.wfile.flush()

                        ollama_resp.close()

                        # ===== 超时处理 =====
                        if timeout_occurred and not saw_done:
                            ollama_status_after = check_ollama_health()
                            elapsed_ms = int((time.time() - request_start) * 1000)

                            # 保存诊断快照
                            snapshot_id, snapshot_path = save_diagnostic_snapshot(
                                incident_type="timeout",
                                raw_data=raw_data,
                                messages=messages,
                                ollama_messages=ollama_messages,
                                tools=tools,
                                model_id=model_id,
                                error_msg=f"Local model timeout after {LOCAL_MODEL_TIMEOUT_SECONDS}s",
                                total_time_ms=elapsed_ms,
                                ollama_status=ollama_health_before
                            )

                            # 发送超时降级消息
                            fallback_msg = (
                                f"[Bridge Timeout] 本地模型超时 ({LOCAL_MODEL_TIMEOUT_SECONDS}s)，自动切换到云端模型。\n"
                                f"诊断日志: {snapshot_path}\n"
                                f"Ollama状态: {ollama_status_after}\n"
                                f"请求数据: {len(messages)}条消息, {len(tools) if tools else 0}个工具"
                            )
                            fallback_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_id,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": f"[{fallback_msg}]"
                                    },
                                    "finish_reason": "timeout"
                                }]
                            }
                            out = f"data: {json.dumps(fallback_chunk, ensure_ascii=False)}\n\n"
                            self.wfile.write(out.encode("utf-8"))
                            self.wfile.flush()
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()

                            log.warning(
                                f"TIMEOUT: model={model_id}, elapsed={elapsed_ms}ms, "
                                f"partial_chars={total_chars}, ollama={ollama_status_after}, "
                                f"diag={snapshot_path}"
                            )

                        # 正常完成
                        if saw_done:
                            total_elapsed = int((time.time() - request_start) * 1000)
                            log.info(f"Stream response: {total_chars} chars, {total_elapsed}ms")

                    except requests.exceptions.ConnectionError as e:
                        # Ollama进程崩溃——保存诊断快照+发送崩溃消息
                        ollama_status_after = check_ollama_health()
                        elapsed_ms = int((time.time() - request_start) * 1000)

                        snapshot_id, snapshot_path = save_diagnostic_snapshot(
                            incident_type="ollama_crash",
                            raw_data=raw_data,
                            messages=messages,
                            ollama_messages=ollama_messages,
                            tools=tools,
                            model_id=model_id,
                            error_msg=f"Ollama connection lost: {str(e)[:200]}",
                            total_time_ms=elapsed_ms,
                            ollama_status=ollama_health_before
                        )

                        fallback_msg = (
                            f"[Bridge Crash] 本地模型连接断开，自动切换到云端模型。\n"
                            f"诊断日志: {snapshot_path}\n"
                            f"Ollama状态: {ollama_status_after}\n"
                            f"原因: {str(e)[:120]}"
                        )
                        fallback_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_id,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": f"[{fallback_msg}]"
                                },
                                "finish_reason": "crash"
                            }]
                        }
                        out = f"data: {json.dumps(fallback_chunk, ensure_ascii=False)}\n\n"
                        self.wfile.write(out.encode("utf-8"))
                        self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()

                        log.error(
                            f"OLLAMA_CRASH: model={model_id}, elapsed={elapsed_ms}ms, "
                            f"ollama={ollama_status_after}, diag={snapshot_path}"
                        )

                else:
                    # ===== 非stream模式：一次性返回完整响应 =====
                    request_start = time.time()
                    ollama_health_before = check_ollama_health()

                    payload = {
                        "model": ollama_model,
                        "messages": ollama_messages,
                        "stream": False
                    }
                    resp = requests.post(OLLAMA_API, json=payload, timeout=LOCAL_MODEL_TIMEOUT_SECONDS)
                    resp.encoding = 'utf-8'

                    # Ollama返回错误
                    if resp.status_code != 200:
                        error_body = resp.text[:500]
                        log.error(f"Ollama error: {resp.status_code}: {error_body}")
                        err_resp = {
                            "id": chat_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model_id,
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": f"[Bridge Error] Ollama returned {resp.status_code}: {error_body}"},
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }
                        resp_body = json.dumps(err_resp, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                        return

                    # 正常响应：替换ID和模型名，过滤thinking标签
                    resp_json = resp.json()
                    resp_json["id"] = chat_id
                    resp_json["model"] = model_id

                    for choice in resp_json.get("choices", []):
                        msg = choice.get("message", {})
                        if "content" in msg and msg["content"]:
                            original = msg["content"]
                            cleaned = strip_thinking_tags(original)
                            if cleaned != original:
                                msg["content"] = cleaned
                                log.info(f"Non-stream: stripped thinking tags ({len(original)} -> {len(cleaned)} chars)")

                    # 发送响应
                    resp_body = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                    tc_count = len(resp_json.get("choices", [{}])[0].get("message", {}).get("tool_calls", []))
                    log.info(f"Non-stream response: finish={resp_json.get('choices',[{}])[0].get('finish_reason')}, tool_calls={tc_count}")

            except requests.exceptions.Timeout:
                # requests层超时（requests库层面的超时，区别于上面的流超时）
                elapsed_ms = int((time.time() - request_start) * 1000)
                log.error(f"TIMEOUT (requests layer): {elapsed_ms}ms")
                ollama_status_after = check_ollama_health()

                # 保存诊断快照
                if 'raw_data' in dir():
                    snapshot_id, snapshot_path = save_diagnostic_snapshot(
                        incident_type="requests_timeout",
                        raw_data=raw_data,
                        messages=messages,
                        ollama_messages=ollama_messages,
                        tools=tools,
                        model_id=model_id,
                        error_msg=f"requests timeout after {LOCAL_MODEL_TIMEOUT_SECONDS}s",
                        total_time_ms=elapsed_ms,
                        ollama_status=ollama_status_after,
                        provider="local/qwen3.5:4b"
                    )

                # 返回超时错误响应
                err_msg = json.dumps({
                    "error": {
                        "message": f"Local model timeout after {LOCAL_MODEL_TIMEOUT_SECONDS}s. "
                                   f"Ollama: {ollama_status_after}",
                        "type": "timeout",
                        "fallback": True
                    }
                }).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_msg)

            except Exception as e:
                # 未预期的异常：打印堆栈、保存诊断快照、返回500错误
                print(f"[ERROR] {e}")
                import traceback; traceback.print_exc()
                log.error(f"Bridge error: {e}")

                try:
                    snapshot_id, snapshot_path = save_diagnostic_snapshot(
                        incident_type="bridge_exception",
                        raw_data=raw_data,
                        messages=messages,
                        ollama_messages=ollama_messages,
                        tools=tools,
                        model_id=model_id,
                        error_msg=str(e)[:300],
                        total_time_ms=int((time.time() - request_start) * 1000) if 'request_start' in dir() else 0,
                        ollama_status=check_ollama_health(),
                        provider="local/qwen3.5:4b"
                    )
                except Exception:
                    pass

                try:
                    err = json.dumps({"error": {"message": str(e), "type": "internal_error"}}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(err)
                except Exception:
                    pass

    def _handle_task_paused(self, data, model_id, stream_requested, raw_data, partial_output):
        """断网时复杂任务暂停处理

        流程:
        1. 生成任务ID并保存到磁盘
        2. SSE模式：在SSE流中发送暂停通知
        3. 非SSE模式：返回JSON格式的暂停通知
        """
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        messages = data.get("messages", [])

        # 保存暂停任务到磁盘（供网络恢复后续传）
        save_paused_task(task_id, data, partial_output, model_id)

        if stream_requested:
            # SSE 模式：发送暂停通知作为 SSE chunk
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # 1. 先发送部分输出（如果云端已输出了一些内容）
            if partial_output:
                partial_chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": partial_output}, "finish_reason": None}]
                }
                self.wfile.write(f"data: {json.dumps(partial_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

            # 2. 发送暂停状态消息
            pause_notice = (
                "\n\n---\n**[任务已暂停]** 网络已断开，当前任务较复杂（需要云端模型能力），本地4B模型无法胜任。"
                f"已保存云端部分输出，联网后可继续。\n\n任务ID: `{task_id}`\n"
                "网络恢复后，直接回复「继续」即可恢复任务。\n---\n"
            )
            pause_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "model": model_id,
                "choices": [{"index": 0, "delta": {"content": pause_notice}, "finish_reason": "stop"}]
            }
            self.wfile.write(f"data: {json.dumps(pause_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

            log.info(f"Task paused notification sent via SSE: {task_id}")
        else:
            # 非 stream 模式：返回 JSON 响应
            pause_resp = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_id,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            "**[任务已暂停]** 网络已断开，当前任务较复杂（需要云端模型能力），本地4B模型无法胜任。"
                            f"已保存云端部分输出，联网后可继续。\n\n任务ID: `{task_id}`\n"
                            "网络恢复后，直接回复「继续」即可恢复任务。"
                        ) + (f"\n\n已收到的部分输出：\n{partial_output}" if partial_output else "")
                    },
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            resp_body = json.dumps(pause_resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

            log.info(f"Task paused notification sent via JSON: {task_id}")

    def _handle_muxi_request(self, data, model_id, stream_requested, raw_data):
        """处理沐曦云端API请求——消息原样透传，不做裁剪/工具剥离

        与本地处理不同，云端27B模型能力强，无需裁剪上下文或剥离工具。
        只注入保活指令防止长时间思考被Gateway超时切断。

        流程:
        1. 注入保活指令到system prompt
        2. 构建沐曦API请求payload（原样透传消息和工具）
        3. Stream模式：透传SSE流，带全局超时保护+缓存部分输出
        4. 非Stream模式：透传JSON响应
        5. 云端失败时：根据任务复杂度决定本地降级或暂停
        """
        _competition_last_provider = "muxi/Qwen3.5-27B"
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        messages = data.get("messages", [])
        tools = data.get("tools", None)
        request_start = time.time()
        sse_headers_sent = False  # 标记SSE头是否已发送（影响降级策略）

        # 注入进度保活指令：防止长时间思考被Gateway超时切断
        # 沐曦27B能力强但思考久，如果60秒内无输出，Gateway会判定超时
        KEEPALIVE_INSTRUCTION = (
            "\n\n[系统指令] 在处理复杂任务时，如果你需要较长时间思考或生成内容，"
            "请每隔约60秒输出一段简短的进度说明（如'正在分析中...'、'已完成第1部分，继续第2部分...'），"
            "避免因长时间无输出而被判定为超时。这比完成任务更重要。"
        )
        # 追加到system消息末尾
        if messages and len(messages) > 0 and messages[0].get("role") == "system":
            messages[0]["content"] = messages[0]["content"] + KEEPALIVE_INSTRUCTION
            log.info("Injected keepalive instruction into system prompt for Muxi")

        # ===== 上下文预检：估算总token数，超过阈值则裁剪 =====
        # 沐曦API上限65536 tokens，预留1500 tokens给输出和开销
        MUXI_CONTEXT_LIMIT = 64000
        total_estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        if tools:
            total_estimated += estimate_tokens(json.dumps(tools, ensure_ascii=False))
        log.info(f"Pre-check: estimated {total_estimated} tokens (limit={MUXI_CONTEXT_LIMIT})")
        if total_estimated > MUXI_CONTEXT_LIMIT:
            # 从最早的非system消息开始裁剪
            trimmed_count = 0
            while total_estimated > MUXI_CONTEXT_LIMIT and len(messages) > 2:
                # 找到第一条非system消息并删除
                for i in range(1, len(messages)):
                    if messages[i].get("role") != "system":
                        messages.pop(i)
                        trimmed_count += 1
                        break
                else:
                    break
                total_estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
                if tools:
                    total_estimated += estimate_tokens(json.dumps(tools, ensure_ascii=False))
            log.warning(f"Context trimmed: removed {trimmed_count} messages, new estimate={total_estimated} tokens")

        # 构建沐曦API请求payload——消息和工具原样透传
        muxi_payload = {
            "model": MUXI_MODEL,
            "messages": messages,
            "stream": stream_requested,
        }
        if tools:
            muxi_payload["tools"] = tools

        # 沐曦API请求头：包含认证和套餐信息
        muxi_headers = {
            "Authorization": f"Bearer {MUXI_API_KEY}",
            "Content-Type": "application/json",
            "X-Package": MUXI_PACKAGE_ID,
        }

        log.info(f"Muxi request: model={MUXI_MODEL}, messages={len(messages)}, stream={stream_requested}, tools={len(tools) if tools else 0}")

        try:
            if stream_requested:
                # ===== Stream模式：透传沐曦SSE流 =====
                muxi_resp = requests.post(MUXI_API, json=muxi_payload, headers=muxi_headers,
                                          stream=True, timeout=(MUXI_CONNECT_TIMEOUT, MUXI_READ_TIMEOUT))
                muxi_resp.encoding = 'utf-8'

                # 沐曦返回错误：降级到本地Ollama
                if muxi_resp.status_code != 200:
                    error_body = muxi_resp.text[:500]
                    log.error(f"Muxi API error: {muxi_resp.status_code}: {error_body}")
                    muxi_resp.close()
                    log.info("Muxi cloud failed, falling back to local Ollama")
                    self._handle_local_fallback(data, model_id, raw_data)
                    return

                # 发送SSE响应头给客户端
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sse_headers_sent = True

                # 透传SSE流，带全局超时保护 + 缓存部分输出
                total_chars = 0
                partial_output_buffer = ""  # 缓存已输出的文本，用于断网时保存暂停任务
                muxi_deadline = time.time() + MUXI_TIMEOUT_SECONDS  # 全局超时截止时间
                for line in muxi_resp.iter_lines(decode_unicode=True):
                    # 全局超时检查
                    if time.time() > muxi_deadline:
                        log.warning(f"Muxi stream global timeout ({MUXI_TIMEOUT_SECONDS}s), closing")
                        break
                    if not line:
                        continue
                    if line.startswith("data: "):
                        chunk_str = line[6:]
                        if chunk_str == "[DONE]":
                            # 流结束标记
                            self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                            self.wfile.flush()
                            continue
                        try:
                            chunk_json = json.loads(chunk_str)
                            # 替换ID和模型名为桥接器的
                            chunk_json["id"] = chat_id
                            chunk_json["model"] = model_id
                            delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                            # 过滤reasoning字段（沐曦模型可能返回推理过程）
                            if "reasoning" in delta:
                                del delta["reasoning"]
                            # 缓存已输出文本（用于断网时保存暂停任务）
                            if "content" in delta and delta["content"]:
                                total_chars += len(delta["content"])
                                partial_output_buffer += delta["content"]
                            # 转发SSE chunk到客户端
                            out = f"data: {json.dumps(chunk_json, ensure_ascii=False)}\n\n"
                            self.wfile.write(out.encode("utf-8"))
                            self.wfile.flush()
                        except json.JSONDecodeError:
                            # 非JSON行：直接透传
                            self.wfile.write((line + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                    else:
                        # 非data行：直接透传
                        self.wfile.write((line + "\n\n").encode("utf-8"))
                        self.wfile.flush()

                muxi_resp.close()
                total_elapsed = int((time.time() - request_start) * 1000)
                log.info(f"Muxi stream response: {total_chars} chars, {total_elapsed}ms")

            else:
                # ===== 非Stream模式：透传JSON响应 =====
                muxi_resp = requests.post(MUXI_API, json=muxi_payload, headers=muxi_headers,
                                          timeout=(MUXI_CONNECT_TIMEOUT, MUXI_READ_TIMEOUT))
                muxi_resp.encoding = 'utf-8'

                # 沐曦返回错误：降级到本地Ollama
                if muxi_resp.status_code != 200:
                    error_body = muxi_resp.text[:500]
                    log.error(f"Muxi API error (non-stream): {muxi_resp.status_code}: {error_body}")
                    muxi_resp.close()
                    log.info("Muxi cloud failed (non-stream), falling back to local Ollama")
                    self._handle_local_fallback(data, model_id, raw_data)
                    return

                # 解析响应，替换ID/模型名，过滤thinking标签
                resp_json = muxi_resp.json()
                resp_json["id"] = chat_id
                resp_json["model"] = model_id

                for choice in resp_json.get("choices", []):
                    msg = choice.get("message", {})
                    # 过滤reasoning字段
                    if "reasoning" in msg:
                        del msg["reasoning"]
                    # 过滤thinking标签
                    if "content" in msg and msg["content"]:
                        cleaned = strip_thinking_tags(msg["content"])
                        if cleaned != msg["content"]:
                            msg["content"] = cleaned

                # 发送响应
                resp_body = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

                total_elapsed = int((time.time() - request_start) * 1000)
                log.info(f"Muxi non-stream response: {total_elapsed}ms")

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # ===== 云端连接失败（断网/超时）→ 降级处理 =====
            elapsed_ms = int((time.time() - request_start) * 1000)
            log.warning(f"Muxi cloud unreachable ({type(e).__name__}) after {elapsed_ms}ms")

            # 获取缓存的部分输出（stream模式下可能已有部分输出）
            partial = ""
            if 'partial_output_buffer' in dir():
                partial = partial_output_buffer

            if sse_headers_sent:
                # SSE头已发送——客户端正在等待流式响应
                # 根据任务复杂度决定降级方式
                if is_task_suitable_for_local(data.get("messages", []), data.get("tools", None)):
                    # 简单任务：在同一个SSE流中插入本地模型输出
                    try:
                        err_chunk = {
                            "id": chat_id, "object": "chat.completion.chunk", "model": model_id,
                            "choices": [{"index": 0, "delta": {"content": "\n[网络中断，降级到本地模型...]\n"},
                                         "finish_reason": None}]
                        }
                        self.wfile.write(f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        # 在同一SSE流中输出本地模型回复
                        self._handle_local_fallback_sse(data, model_id, chat_id)
                    except Exception:
                        # 降级也失败，关闭SSE流
                        try:
                            self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except Exception:
                            pass
                else:
                    # 复杂任务：暂停，保存已输出的部分
                    task_id = f"task-{uuid.uuid4().hex[:12]}"
                    save_paused_task(task_id, data, partial, model_id)
                    try:
                        pause_notice = (
                            f"\n\n---\n**[任务已暂停]** 网络中断，当前任务较复杂，本地模型无法胜任。"
                            f"已保存云端部分输出（{len(partial)}字），联网后可继续。\n\n任务ID: `{task_id}`\n"
                            "网络恢复后回复「继续」即可恢复。\n---\n"
                        )
                        pause_chunk = {
                            "id": chat_id, "object": "chat.completion.chunk", "model": model_id,
                            "choices": [{"index": 0, "delta": {"content": pause_notice}, "finish_reason": "stop"}]
                        }
                        self.wfile.write(f"data: {json.dumps(pause_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        log.info(f"Complex task paused after muxi failure (SSE): {task_id}")
                    except Exception:
                        pass
            else:
                # SSE头未发送——客户端还在等待初始响应
                # 可以直接切换到本地模型或暂停
                if is_task_suitable_for_local(data.get("messages", []), data.get("tools", None)):
                    # 简单任务：降级到本地Ollama
                    try:
                        self._handle_local_fallback(data, model_id, raw_data)
                    except Exception as fallback_err:
                        # 本地也失败：返回503
                        log.error(f"Local fallback also failed: {fallback_err}")
                        err = json.dumps({"error": {"message": f"Both cloud and local failed: {str(e)[:100]}", "type": "all_providers_failed"}}).encode()
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(err)
                else:
                    # 复杂任务：暂停
                    self._handle_task_paused(data, model_id, stream_requested, raw_data, partial)

    def _handle_local_fallback(self, data, model_id, raw_data):
        """云端失败后的本地降级处理——复用Ollama逻辑（非SSE模式）

        与do_POST中的本地处理逻辑类似，但简化了stream处理（只支持非stream）
        用于云端请求失败时的快速降级。
        """
        _competition_last_provider = "local/qwen3.5:4b"
        log.info(f"LOCAL FALLBACK: model={model_id}, using Ollama")
        messages = data.get("messages", [])
        stream_requested = data.get("stream", False)
        tools = data.get("tools", None)
        has_tools = tools is not None and len(tools) > 0
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        # 工具数量裁剪（与do_POST中相同逻辑）
        MAX_TOOLS = 4
        if has_tools and len(tools) > MAX_TOOLS:
            priority_tools = []
            other_tools = []
            for t in tools:
                fn_name = t.get("function", {}).get("name", "")
                if any(kw in fn_name.lower() for kw in ("file", "directory", "list", "read", "write", "search")):
                    priority_tools.append(t)
                else:
                    other_tools.append(t)
            tools = priority_tools[:MAX_TOOLS]
            if len(tools) < MAX_TOOLS:
                tools.extend(other_tools[:MAX_TOOLS - len(tools)])

        # 映射模型名+转换消息格式
        ollama_model = OLLAMA_MODEL_MAP.get(model_id, "qwen3.5:4b")
        ollama_messages = build_ollama_messages(messages, has_tools=has_tools)

        log.info(f"Local fallback: ollama={ollama_model}, {len(ollama_messages)} messages, stream={stream_requested}")

        # 非stream请求（降级场景通常不走stream，避免复杂度）
        payload = {
            "model": ollama_model,
            "messages": ollama_messages,
            "stream": False
        }
        resp = requests.post(OLLAMA_API, json=payload, timeout=LOCAL_MODEL_TIMEOUT_SECONDS)
        resp.encoding = 'utf-8'

        # Ollama也失败：返回双重失败错误
        if resp.status_code != 200:
            error_body = resp.text[:500]
            log.error(f"Local fallback Ollama error: {resp.status_code}: {error_body}")
            err_resp = {
                "id": chat_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_id,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": f"[Bridge Fallback Error] 云端不可用，本地模型也返回错误: {error_body}"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            resp_body = json.dumps(err_resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return

        # Ollama正常响应：替换ID/模型名，过滤thinking标签
        resp_json = resp.json()
        resp_json["id"] = chat_id
        resp_json["model"] = model_id

        for choice in resp_json.get("choices", []):
            msg = choice.get("message", {})
            if "content" in msg and msg["content"]:
                cleaned = strip_thinking_tags(msg["content"])
                if cleaned != msg["content"]:
                    msg["content"] = cleaned

        # 发送响应
        resp_body = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)
        log.info(f"Local fallback response sent for {chat_id}")

    def _handle_local_fallback_sse(self, data, model_id, chat_id):
        """SSE流中途断网时，在同一个SSE流中输出本地模型回复

        与_handle_local_fallback不同，此方法在SSE流已建立的情况下使用，
        直接向已打开的SSE流写入本地模型的stream输出，不需要重新发送HTTP头。
        """
        _competition_last_provider = "local/qwen3.5:4b"
        log.info(f"LOCAL FALLBACK SSE: model={model_id}, using Ollama inline")
        messages = data.get("messages", [])
        tools = data.get("tools", None)
        has_tools = tools is not None and len(tools) > 0

        # 工具裁剪（简化版：直接截取前4个）
        MAX_TOOLS = 4
        if has_tools and len(tools) > MAX_TOOLS:
            tools = tools[:MAX_TOOLS]

        # 映射模型名+转换消息格式
        ollama_model = OLLAMA_MODEL_MAP.get(model_id, "qwen3.5:4b")
        ollama_messages = build_ollama_messages(messages, has_tools=has_tools)

        payload = {
            "model": ollama_model,
            "messages": ollama_messages,
            "stream": True
        }

        try:
            # 向Ollama发送stream请求
            resp = requests.post(OLLAMA_API, json=payload, stream=True, timeout=(10, 30))
            resp.encoding = 'utf-8'

            if resp.status_code != 200:
                # 本地模型也失败：发送错误chunk
                error_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk", "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": f"[本地模型也失败: HTTP {resp.status_code}]"},
                                 "finish_reason": "stop"}]
                }
                self.wfile.write(f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                self.wfile.flush()
                resp.close()
                return

            # 透传Ollama SSE流
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                chunk_str = line[6:]
                if chunk_str == "[DONE]":
                    self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                    self.wfile.flush()
                    continue
                try:
                    chunk_json = json.loads(chunk_str)
                    # 替换ID和模型名
                    chunk_json["id"] = chat_id
                    chunk_json["model"] = model_id
                    delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                    # 过滤reasoning字段
                    if "reasoning" in delta:
                        del delta["reasoning"]
                    out = f"data: {json.dumps(chunk_json, ensure_ascii=False)}\n\n"
                    self.wfile.write(out.encode("utf-8"))
                    self.wfile.flush()
                except json.JSONDecodeError:
                    pass
            resp.close()
            log.info(f"Local fallback SSE response sent for {chat_id}")
        except Exception as e:
            # 本地模型也失败：发送最后的错误消息
            log.error(f"Local fallback SSE also failed: {e}")
            try:
                error_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk", "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": f"[所有模型不可用: {str(e)[:80]}]"},
                                 "finish_reason": "stop"}]
                }
                self.wfile.write(f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write("data: [DONE]\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    # ==================== GET请求处理 ====================
    def do_GET(self):
        """处理GET请求

        路由:
        - /v1/models: 返回桥接器支持的模型列表（供Gateway发现）
        - /health: 健康检查端点
        - /v1/paused-tasks: 查询暂停任务列表（前端/Gateway可轮询）
        """
        if self.path == "/v1/models":
            # 返回桥接器支持的模型列表
            models = [{"id": m, "object": "model", "owned_by": "local"} for m in BRIDGE_MODELS]
            resp = json.dumps({"object": "list", "data": models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        elif self.path == "/health":
            # 健康检查：返回ok
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        elif self.path == "/v1/paused-tasks":
            # 查询暂停任务列表（前端可轮询此端点查看网络恢复通知）
            paused = load_paused_tasks()
            resp = json.dumps({"paused_tasks": paused}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

# ==================== 启动入口（比赛版）====================
if __name__ == "__main__":
    print(f"[比赛版] Bridge listening on http://127.0.0.1:{LISTEN_PORT}")
    print(f"[比赛版] Providers: Muxi (Qwen3.5-27B) + Local (Ollama qwen3.5:4b)")
    print(f"[比赛版] No DeepSeek. MUXI_READ_TIMEOUT={MUXI_READ_TIMEOUT}s")
    HTTPServer(("127.0.0.1", LISTEN_PORT), BridgeHandler).serve_forever()
