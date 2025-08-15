#!/bin/bash

echo "=== 查看FastAPI进程日志 ==="

# 查找FastAPI进程
PID=$(pgrep -f "uvicorn main:app")

if [ -n "$PID" ]; then
    echo "FastAPI进程ID: $PID"
    echo "进程信息:"
    ps -p $PID -o pid,ppid,cmd
    echo ""
    echo "如果你想查看实时日志，请运行："
    echo "journalctl --follow -u your-service-name"
    echo "或者直接查看运行uvicorn的终端窗口"
else
    echo "未找到运行中的FastAPI进程"
fi

echo ""
echo "=== 最近的系统日志（可能包含Python/FastAPI错误）==="
journalctl --no-pager -n 20 | grep -i "python\|fastapi\|uvicorn" || echo "未找到相关日志"
