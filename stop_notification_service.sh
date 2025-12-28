#!/bin/bash

# 悟空量化 - 通知系统停止脚本

set -e

echo "🛑 停止悟空量化通知系统..."

# 停止 Celery
if [ -f logs/celery.pid ]; then
    PID=$(cat logs/celery.pid)
    if ps -p $PID > /dev/null; then
        echo "停止 Celery (PID: $PID)..."
        kill $PID
        sleep 2
        
        # 强制杀死如果还在运行
        if ps -p $PID > /dev/null; then
            echo "强制停止 Celery..."
            kill -9 $PID
        fi
        
        rm -f logs/celery.pid
        echo "✅ Celery 已停止"
    else
        echo "⚠️  Celery 进程不存在"
        rm -f logs/celery.pid
    fi
else
    echo "⚠️  未找到 celery.pid 文件"
fi

echo ""
echo "✅ 通知系统已停止"
echo ""
