# ============================================================
# 脚本名: health_system_check.py
# 功能: 运行底座一键健康检查（Ollama / 应龙桥接器 / OpenClaw Gateway），
#       检测端口、HTTP 端点、已安装模型、Docker 状态、QQ Bot 进程，并给出诊断建议
# 依赖: requests, Python标准库(socket/subprocess/json/sys/io/time)
# 用法: python src/health/health_system_check.py
#       返回码 0=全部正常，1=有异常
# 日期: 2026-05-20
# ============================================================

"""System Health Check - checks all core services and reports status"""

# ---- 标准库导入 ----
import requests, json, socket, subprocess, sys, io, time

# 强制stdout使用UTF-8编码，避免中文输出乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ---- 三件套服务定义 ----
# 端口以现役生产配置为准（早期版本为 11441/18789，已随 v2026.6.9 升级迁走）。
# 可用环境变量覆盖：XIHE_BRIDGE_PORT / OPENCLAW_GATEWAY_PORT / OLLAMA_PORT
import os
SERVICES = {
    "Ollama":  {"port": int(os.environ.get("OLLAMA_PORT", "11434")),
                "path": "/api/tags", "method": "GET"},
    "Bridge":  {"port": int(os.environ.get("XIHE_BRIDGE_PORT", "11443")),
                "path": "/health", "method": "GET"},
    "Gateway": {"port": int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18791")),
                "path": "/", "method": "GET"},
}

def check_port(port, host="127.0.0.1"):
    """检查TCP端口是否在监听

    通过socket尝试连接目标端口，3秒超时。
    返回True表示端口开放，False表示端口关闭或不可达。

    Args:
        port: 目标端口号
        host: 目标主机地址，默认本机127.0.0.1
    """
    try:
        # 创建TCP socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 设置3秒连接超时，避免长时间阻塞
        s.settimeout(3)
        # connect_ex返回0表示连接成功，非0表示失败（不会抛异常）
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        # socket创建失败等异常情况，视为端口不可达
        return False

def check_http(port, path, method="GET", host="127.0.0.1", timeout=5):
    """检查HTTP端点是否正常响应

    向指定端口+路径发送HTTP请求，验证服务是否真正可用
    （端口开放不等于服务正常，需要HTTP层验证）

    Args:
        port: 目标端口号
        path: 健康检查路径，如/api/tags、/health
        method: HTTP方法，GET或POST
        host: 目标主机地址
        timeout: 请求超时秒数，默认5秒

    Returns:
        (bool, status_code_or_error): 第一个值表示是否成功，
            第二个值是HTTP状态码或错误描述字符串
    """
    url = f"http://{host}:{port}{path}"
    try:
        # 根据method参数选择GET或POST
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, timeout=timeout)
        # 请求成功，返回状态码
        return True, r.status_code
    except requests.exceptions.ConnectionError:
        # 连接被拒绝——端口关闭或服务未启动
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        # 请求超时——服务可能卡死或负载过高
        return False, "Timeout"
    except Exception as e:
        # 其他异常（DNS解析失败、SSL错误等）
        return False, str(e)

def check_ollama_models():
    """检查Ollama已安装的模型列表

    调用Ollama的/api/tags接口获取所有已安装模型的名称。
    用于诊断"模型列表为空"等常见问题。

    Returns:
        list[str]: 模型名称列表，如["qwen2.5:3b", "qwen2.5:7b"]
        空列表表示Ollama不可达或没有安装模型
    """
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        if r.status_code == 200:
            models = r.json().get("models", [])
            # 提取每个模型的name字段
            return [m.get("name", "unknown") for m in models]
        return []
    except Exception:
        # Ollama未运行或网络不可达
        return []

def check_docker():
    """检查Docker守护进程是否运行

    执行docker info命令，返回码为0表示Docker正常运行。
    本项目部分场景需要Docker（如容器化部署Gateway）。

    Returns:
        bool: True=Docker运行中，False=Docker未运行或未安装
    """
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # FileNotFoundError: docker命令不存在（未安装）
        # TimeoutExpired: docker info挂起（Docker Desktop卡死）
        return False

def check_qq_bot():
    """检查QQ Bot插件是否运行

    通过PowerShell查找QQ/NapCat/Lagrange相关进程，
    判断QQ Bot适配器是否已启动。

    Returns:
        bool: True=检测到QQ Bot进程，False=未检测到
    """
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             # 搜索进程名包含qq/napcat/lagrange的进程
             "Get-Process | Where-Object { $_.ProcessName -like '*qq*' -or $_.ProcessName -like '*napcat*' -or $_.ProcessName -like '*lagrange*' }"],
            capture_output=True, timeout=10, text=True
        )
        # 有输出说明找到了匹配进程
        return result.stdout.strip() != ""
    except Exception:
        return False

def run_health_check():
    """执行完整的系统健康检查

    检查流程：
    1. 遍历SERVICES中的三个核心服务（Ollama/Bridge/Gateway）
    2. 先检查TCP端口，再检查HTTP端点
    3. 检查Ollama模型列表
    4. 检查Docker状态
    5. 检查QQ Bot进程
    6. 输出诊断建议

    Returns:
        bool: True=所有核心服务正常，False=有服务异常
    """
    results = []
    all_ok = True

    # 打印标题和时间戳
    print("=" * 50)
    print(f"System Health Check - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # ---- 第一部分：检查三个核心服务 ----
    for name, config in SERVICES.items():
        # 先检查TCP端口是否开放
        port_ok = check_port(config["port"])
        # 端口开放时进一步检查HTTP端点；端口关闭则跳过HTTP检查
        http_ok, status = check_http(config["port"], config["path"], config["method"]) if port_ok else (False, "Port closed")

        # 根据检查结果判断状态等级
        if port_ok and http_ok:
            # 端口和HTTP都正常
            status_str = f"OK (HTTP {status})"
            icon = "[OK]"
        elif port_ok:
            # 端口开放但HTTP异常——服务可能正在启动或出错
            status_str = f"WARN (Port open, HTTP: {status})"
            icon = "[!!]"
            all_ok = False
        else:
            # 端口关闭——服务未运行
            status_str = "DOWN (Port closed)"
            icon = "[XX]"
            all_ok = False

        print(f"  {icon} {name:10s} (:{config['port']}) - {status_str}")
        results.append({"name": name, "port": config["port"], "ok": port_ok and http_ok, "detail": status_str})

    # ---- 第二部分：检查Ollama模型 ----
    print()
    models = check_ollama_models()
    if models:
        # 显示所有已安装模型
        print(f"  [OK] Ollama Models: {', '.join(models)}")
    else:
        # 模型列表为空，可能是联接(junction)问题或未拉取模型
        print(f"  [!!] Ollama Models: None found or Ollama not responding")

    # ---- 第三部分：检查Docker ----
    print()
    docker_ok = check_docker()
    print(f"  {'[OK]' if docker_ok else '[--]'} Docker: {'Running' if docker_ok else 'Not running / Not installed'}")

    # ---- 第四部分：检查QQ Bot ----
    qq_ok = check_qq_bot()
    print(f"  {'[OK]' if qq_ok else '[--]'} QQ Bot: {'Running' if qq_ok else 'Not running'}")

    # ---- 第五部分：诊断建议 ----
    # 针对每个异常服务给出具体的修复命令
    print()
    print("=" * 50)
    print("Diagnostics:")
    print("=" * 50)

    fixes = []
    for r in results:
        if not r["ok"]:
            # 根据服务名称给出对应的启动命令
            if r["name"] == "Ollama":
                fixes.append(f"  - Ollama DOWN: Run 'ollama serve' to start, or check if port 11434 is blocked")
            elif r["name"] == "Bridge":
                fixes.append("  - Bridge DOWN: run 'python src/bridge/yinglo_bridge.py' to start")
            elif r["name"] == "Gateway":
                fixes.append(f"  - Gateway DOWN: Run 'openclaw gateway' to start")

    if fixes:
        print("Issues found:")
        for f in fixes:
            print(f)
    else:
        print("All core services are running normally.")

    # 输出总体状态
    print()
    overall = "ALL OK" if all_ok else "ISSUES DETECTED"
    print(f"Overall: {overall}")
    return all_ok

# ---- 主入口 ----
# 正常退出返回0，有异常返回1，可用于脚本链式调用判断
if __name__ == "__main__":
    ok = run_health_check()
    sys.exit(0 if ok else 1)
