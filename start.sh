#!/usr/bin/env bash
# x-operator MVP 启动脚本（Linux/macOS）
set -e

if ! command -v uv >/dev/null 2>&1; then
  echo "[x-operator] 未检测到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "[x-operator] 同步依赖..."
uv sync

echo "[x-operator] 启动中：http://localhost:8080"
uv run python -m x_operator.main
