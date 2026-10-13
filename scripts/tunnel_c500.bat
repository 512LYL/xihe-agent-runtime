@echo off
REM ============================================================
REM  tunnel_c500.bat —— 通道③（沐曦 C500 自部署 32B）SSH 隧道模板
REM
REM  作用：把本机 <LOCAL_PORT> 转发到实例内的 vLLM（127.0.0.1:8000），
REM        使应龙桥接器可以用 http://127.0.0.1:<LOCAL_PORT>/v1 调用通道③。
REM
REM  ⚠️ 本文件是**模板**，不含任何真实实例信息。
REM     使用前把下面 5 个 set 的值换成你自己的（或改成从 .env 读）。
REM     **真实网关 IP / SSH 端口 / 用户名 / 私钥路径严禁入库。**
REM
REM  用法：双击本文件；窗口保持开着（关掉 = 隧道断）
REM        建好后运行： python scripts\check_c500.py
REM ============================================================

set "KEY=%USERPROFILE%\.ssh\id_ed25519"
set "SSH_HOST=<C500_GATEWAY_HOST>"
set "SSH_PORT=<C500_SSH_PORT>"
set "SSH_USER=<C500_SSH_USER>"
set "LOCAL_PORT=18000"
set "REMOTE=127.0.0.1:8000"

if "%SSH_HOST%"=="<C500_GATEWAY_HOST>" (
    echo [错误] 请先编辑本文件，把 SSH_HOST / SSH_PORT / SSH_USER 换成你自己的值。
    echo        公开仓库里的这份永远是占位符，真实值只放在本地 .env 或本机备份中。
    pause
    exit /b 1
)

title C500 tunnel %LOCAL_PORT% -^> %SSH_HOST%:%SSH_PORT%

echo ============================================================
echo   C500 通道③ SSH 隧道
echo   本机 %LOCAL_PORT%  ==^>  %SSH_HOST%:%SSH_PORT%  ==^>  实例 %REMOTE%
echo   私钥: %KEY%
echo ============================================================
echo.

netstat -ano | findstr "LISTENING" | findstr ":%LOCAL_PORT% " >nul 2>&1
if not errorlevel 1 (
    echo [提示] 本机 %LOCAL_PORT% 已在监听 —— 隧道可能已开着。
    echo        要重开请先关掉旧窗口。
    echo.
)

if not exist "%KEY%" (
    echo [错误] 找不到私钥: %KEY%
    echo        请确认已把实例私钥放到 ^^/.ssh^/ 下。
    pause
    exit /b 1
)

echo 正在建立隧道，请保持本窗口开着...
echo （实例没开机 / 已过期时会卡住或报错，属正常现象）
echo.
ssh -i "%KEY%" -p %SSH_PORT% -N -L %LOCAL_PORT%:%REMOTE% %SSH_USER%@%SSH_HOST%

echo.
echo [隧道已断开]
pause
