@echo off
chcp 65001 >nul
REM x-operator MVP 启动脚本（Windows）
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

REM uv 的官方安装目录可能尚未进入当前终端的 PATH。
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
  echo [x-operator] 未检测到 uv。请先安装：
  echo   winget install --id=astral-sh.uv -e
  echo 或参考 https://docs.astral.sh/uv/
  pause
  exit /b 1
)

echo [x-operator] 同步依赖...
uv sync --locked
if errorlevel 1 (
  echo [x-operator] 依赖安装失败。
  pause
  exit /b 1
)

echo [x-operator] Starting the background service...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-local.ps1" -StartOnly
set "launcherExit=%errorlevel%"
if errorlevel 1 (
  echo [x-operator] Start failed. See the error and log path above.
)
pause
exit /b %launcherExit%
