@echo off
rem ============================================================================
rem  xihe-lite  one-click start
rem    1) locate python       2) create config.json if missing
rem    3) run the console in THIS window (close it or run stop.bat to stop)
rem  messages are kept ASCII so the .bat never shows mojibake; the console
rem  itself prints Chinese.
rem ============================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -V
if errorlevel 1 (
  echo [X] Python not found. Install Python 3.8+ first, then add it to PATH.
  pause
  exit /b 1
)

if not exist "config.json" if exist "config.example.json" (
  copy /y "config.example.json" "config.json" >nul
  echo [i] config.json created from config.example.json
)

if not exist "config.json" (
  echo [X] neither config.json nor config.example.json found.
  pause
  exit /b 1
)

echo ============================================================================
echo  xihe-lite is starting.  Dashboard: http://127.0.0.1:18910/
echo  Stop with:  stop.bat          (or close this window / press Ctrl+C)
echo  No physical printer?  The print chain falls back to dry-run automatically
echo  and nothing is sent to any device.
echo ============================================================================
echo.

%PY% -m xihe serve --open

echo.
echo [i] console stopped. You can close this window now.
pause
exit /b 0
