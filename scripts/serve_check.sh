#!/usr/bin/env bash
# 起一次服务，检查所有页面（含带参数的抓取记录页）都能 200 渲染且日志无异常。用法：bash scripts/serve_check.sh
# 只会结束本脚本自己启动的那个进程，不会误杀你正在用的实例。
cd "$(dirname "$0")/.."
LOG=$(mktemp)
timeout 75 uv run python -m x_operator.main > "$LOG" 2>&1 &
PID=$!
for i in $(seq 1 40); do sleep 1; curl -s -o /dev/null http://localhost:8080/ && break; done
for p in / /queue /targets "/targets?source=search&rule=1&status=filtered" /materials /watched /rules /schedule /settings; do
  printf "%-50s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8080$p")"
done
echo "--- 关键文案:"
curl -s http://localhost:8080/targets | grep -o "各状态是什么意思" | head -1
curl -s http://localhost:8080/rules | grep -o "查看结果\|暂无搜索规则" | head -1
curl -s http://localhost:8080/settings | grep -o "本机系统代理" | head -1
curl -s http://localhost:8080/ | grep -o "Mock" | head -1 | sed 's/^/!! 仍有 Mock 字样: /'
sleep 2
echo "--- errors:"; grep -iE "error|traceback|exception" "$LOG" | head
echo "--- log tail:"; tail -2 "$LOG"
kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; rm -f "$LOG"; true
