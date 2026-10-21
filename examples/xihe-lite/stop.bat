@echo off
rem ============================================================================
rem  xihe-lite  one-click stop  --  stops the print daemon and the console
rem  (pids come from runtime/run/*.pid, plus a port scan as a fallback)
rem ============================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -V >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Install Python 3.9+ first.
  if /i not "%~1"=="--quiet" pause
  exit /b 1
)

%PY% -m xihe stop
set "RC=%ERRORLEVEL%"
if /i not "%~1"=="--quiet" (
  echo.
  echo [i] done. You can close this window.
  pause
)
exit /b %RC%
