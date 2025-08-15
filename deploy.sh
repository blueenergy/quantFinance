#!/bin/bash

# 悟空量化金融智能助手 - 自动化部署脚本
# 使用方法: ./deploy.sh [环境] [版本]
# 例如: ./deploy.sh production v1.0.0

set -e

# ===========================================
# 配置变量
# ===========================================
ENV=${1:-production}
VERSION=${2:-latest}
PROJECT_NAME="quant-finance"
BACKUP_DIR="/opt/backups/$PROJECT_NAME"
LOG_FILE="/var/log/${PROJECT_NAME}-deploy.log"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ===========================================
# 工具函数
# ===========================================
log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" | tee -a "$LOG_FILE"
    exit 1
}

warning() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1" | tee -a "$LOG_FILE"
}

info() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')] INFO:${NC} $1" | tee -a "$LOG_FILE"
}

# 检查命令是否存在
check_command() {
    if ! command -v $1 &> /dev/null; then
        error "$1 命令未找到，请先安装"
    fi
}

# 检查文件是否存在
check_file() {
    if [ ! -f "$1" ]; then
        error "文件不存在: $1"
    fi
}

# ===========================================
# 环境检查
# ===========================================
check_environment() {
    log "🔍 检查部署环境..."
    
    # 检查必要的命令
    check_command "docker"
    check_command "docker-compose"
    check_command "git"
    check_command "curl"
    
    # 检查Docker服务状态
    if ! systemctl is-active --quiet docker; then
        error "Docker服务未运行，请启动Docker服务"
    fi
    
    # 检查环境配置文件
    if [ "$ENV" = "production" ]; then
        check_file ".env.production"
        cp .env.production .env
    else
        check_file ".env.development"
        cp .env.development .env
    fi
    
    # 检查磁盘空间 (至少需要5GB)
    DISK_SPACE=$(df . | awk 'NR==2 {print $4}')
    if [ "$DISK_SPACE" -lt 5242880 ]; then
        warning "磁盘空间不足5GB，建议清理空间"
    fi
    
    info "✅ 环境检查完成"
}

# ===========================================
# 备份操作
# ===========================================
backup_database() {
    log "💾 开始备份数据库..."
    
    # 创建备份目录
    mkdir -p "$BACKUP_DIR/$(date +%Y%m%d)"
    
    # 备份MongoDB (如果容器运行中)
    if docker ps | grep -q quant-mongodb; then
        docker exec quant-mongodb mongodump --out /data/backup/$(date +%Y%m%d_%H%M%S) || warning "数据库备份失败"
        docker cp quant-mongodb:/data/backup/$(date +%Y%m%d_%H%M%S) "$BACKUP_DIR/$(date +%Y%m%d)/" || warning "备份文件复制失败"
    fi
    
    # 清理老备份 (保留30天)
    find "$BACKUP_DIR" -type d -mtime +30 -exec rm -rf {} \; 2>/dev/null || true
    
    info "✅ 数据库备份完成"
}

# ===========================================
# 代码更新
# ===========================================
update_code() {
    log "📥 更新代码..."
    
    # 检查Git状态
    if [ ! -d ".git" ]; then
        error "不是Git仓库，无法更新代码"
    fi
    
    # 保存本地修改
    git stash push -m "部署前自动保存 $(date)" || true
    
    # 拉取最新代码
    git fetch origin
    
    if [ "$VERSION" = "latest" ]; then
        git checkout main
        git pull origin main
    else
        git checkout tags/"$VERSION" -b deploy-"$VERSION" 2>/dev/null || git checkout deploy-"$VERSION"
    fi
    
    info "✅ 代码更新完成 (版本: $VERSION)"
}

# ===========================================
# 构建应用
# ===========================================
build_application() {
    log "🔨 构建应用..."
    
    # 构建前端
    info "📦 构建前端应用..."
    cd dashboard
    
    # 检查Node.js环境
    if ! command -v npm &> /dev/null; then
        error "npm命令未找到，请先安装Node.js"
    fi
    
    # 安装依赖并构建
    npm ci --only=production
    npm run build
    cd ..
    
    # 构建Docker镜像
    info "🐳 构建Docker镜像..."
    docker-compose build --no-cache
    
    # 清理无用镜像
    docker image prune -f
    
    info "✅ 应用构建完成"
}

# ===========================================
# 部署应用
# ===========================================
deploy_application() {
    log "🚀 部署应用..."
    
    # 停止旧服务
    info "⏹️ 停止旧服务..."
    docker-compose down --remove-orphans || true
    
    # 启动新服务
    info "▶️ 启动新服务..."
    docker-compose up -d
    
    # 等待服务启动
    info "⏳ 等待服务启动..."
    sleep 30
    
    info "✅ 应用部署完成"
}

# ===========================================
# 健康检查
# ===========================================
health_check() {
    log "🔍 进行健康检查..."
    
    local max_attempts=10
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        info "尝试 $attempt/$max_attempts ..."
        
        # 检查前端
        if curl -f http://localhost/health >/dev/null 2>&1; then
            info "✅ 前端服务正常"
        else
            warning "❌ 前端服务异常"
            if [ $attempt -eq $max_attempts ]; then
                error "前端服务启动失败"
            fi
        fi
        
        # 检查API
        if curl -f http://localhost:3001/health >/dev/null 2>&1; then
            info "✅ API服务正常"
            break
        else
            warning "❌ API服务异常"
            if [ $attempt -eq $max_attempts ]; then
                error "API服务启动失败"
            fi
        fi
        
        attempt=$((attempt + 1))
        sleep 10
    done
    
    # 检查数据库连接
    if docker exec quant-mongodb mongosh --eval "db.adminCommand('ping')" >/dev/null 2>&1; then
        info "✅ 数据库连接正常"
    else
        warning "❌ 数据库连接异常"
    fi
    
    info "✅ 健康检查完成"
}

# ===========================================
# 显示部署信息
# ===========================================
show_deployment_info() {
    log "📋 部署信息"
    echo "=================================="
    echo "🐒 悟空量化金融智能助手"
    echo "=================================="
    echo "环境: $ENV"
    echo "版本: $VERSION"
    echo "部署时间: $(date)"
    echo "=================================="
    echo "服务地址:"
    echo "🌐 前端: http://localhost"
    echo "🔧 API: http://localhost:3001"
    echo "📊 监控: http://localhost:3000 (如果启用)"
    echo "=================================="
    echo "Docker容器状态:"
    docker-compose ps
    echo "=================================="
    echo "日志查看命令:"
    echo "docker-compose logs -f [服务名]"
    echo "=================================="
}

# ===========================================
# 清理操作
# ===========================================
cleanup() {
    log "🧹 清理临时文件..."
    
    # 清理Docker相关
    docker system prune -f
    
    # 清理npm缓存
    cd dashboard && npm cache clean --force && cd ..
    
    info "✅ 清理完成"
}

# ===========================================
# 回滚操作
# ===========================================
rollback() {
    log "🔄 开始回滚..."
    
    # 停止当前服务
    docker-compose down
    
    # 恢复到上一个版本 (需要实现版本管理)
    git checkout HEAD~1
    
    # 重新部署
    docker-compose up -d
    
    info "✅ 回滚完成"
}

# ===========================================
# 主函数
# ===========================================
main() {
    log "🚀 开始部署悟空量化金融智能助手 ($ENV 环境)"
    
    # 检查是否为root用户
    if [ "$EUID" -eq 0 ]; then
        warning "不推荐使用root用户部署，建议使用普通用户"
    fi
    
    # 创建日志目录
    mkdir -p "$(dirname "$LOG_FILE")"
    
    # 捕获错误和中断信号
    trap 'error "部署被中断"' INT TERM
    
    case "${3:-deploy}" in
        "deploy")
            check_environment
            backup_database
            update_code
            build_application
            deploy_application
            health_check
            show_deployment_info
            ;;
        "rollback")
            rollback
            ;;
        "cleanup")
            cleanup
            ;;
        "health")
            health_check
            ;;
        *)
            echo "使用方法: $0 [环境] [版本] [操作]"
            echo "环境: production|development"
            echo "版本: latest|v1.0.0|..."
            echo "操作: deploy|rollback|cleanup|health"
            exit 1
            ;;
    esac
    
    log "🎉 部署完成！"
}

# 执行主函数
main "$@"
