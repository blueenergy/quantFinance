#!/bin/bash

# 悟空量化 - 通知系统启动脚本
# 启动 Celery Worker 和 Beat 用于处理定时任务

set -e

echo "🚀 启动悟空量化通知系统..."

# 检查 Redis 是否运行
if ! redis-cli ping > /dev/null 2>&1; then
    echo "❌ Redis 未运行，请先启动 Redis:"
    echo "   brew services start redis  # macOS"
    echo "   或"
    echo "   sudo systemctl start redis  # Linux"
    exit 1
fi

echo "✅ Redis 已运行"

# 检查环境变量
if [ ! -f .env ]; then
    echo "⚠️  .env 文件不存在，从模板创建..."
    cp .env.template .env
    echo "❗ 请编辑 .env 文件配置邮件服务"
fi

# 启动 Celery Worker 和 Beat
echo "📬 启动 Celery Worker 和 Beat..."
echo "   - Worker: 处理后台任务"
echo "   - Beat: 定时任务调度器"
echo ""

# 使用 -B 参数同时启动 worker 和 beat
celery -A celery_app worker --beat --loglevel=info \
    --logfile=logs/celery.log \
    --pidfile=logs/celery.pid \
    --detach

echo ""
echo "✅ Celery 启动成功"
echo ""
echo "📊 定时任务："
echo "   - Worker 超时检查: 每 60 秒"
echo "   - 日报发送: 每天 17:00"
echo ""
echo "📝 日志文件: logs/celery.log"
echo ""
echo "🛑 停止服务: ./stop_notification_service.sh"
echo ""
