@echo off
REM x-operator MVP 启动脚本（Windows）
setlocal

where uv >nul 2>nul
if errorlevel 1 (
  echo [x-operator] 未检测到 uv。请先安装：
  echo   winget install --id=astral-sh.uv -e
  echo 或参考 https://docs.astral.sh/uv/
  pause
  exit /b 1
)

echo [x-operator] 同步依赖...
uv sync
if errorlevel 1 (
  echo [x-operator] 依赖安装失败。
  pause
  exit /b 1
)

echo [x-operator] 启动中，浏览器将自动打开 http://localhost:8080
uv run python -m x_operator.main

pause
