# 云服务部署快速指南

## 🚀 快速部署步骤

### 1. 准备工作
```bash
# 1. 购买云服务器 (推荐配置: 4核8GB)
# 2. 安装Docker和Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# 3. 克隆项目
git clone <your-repo-url>
cd quantFinance
```

### 2. 配置环境
```bash
# 复制并编辑环境配置
cp .env.production .env
nano .env

# 必须修改的配置项:
# - MONGODB_URL (云数据库地址)
# - JWT_SECRET (强密码)
# - CORS_ORIGINS (你的域名)
# - 各种API密钥
```

### 3. 一键部署
```bash
# 给部署脚本执行权限
chmod +x deploy.sh

# 执行部署
./deploy.sh production latest
```

### 4. 配置域名和SSL
```bash
# 安装nginx和certbot
sudo apt update
sudo apt install nginx certbot python3-certbot-nginx

# 配置域名解析后获取SSL证书
sudo certbot --nginx -d yourdomain.com

# 配置nginx代理到容器
sudo nano /etc/nginx/sites-available/quantfinance
```

## 🔧 主要配置文件说明

### Docker配置
- `Dockerfile.backend` - 后端服务容器
- **本地全栈 Docker**：使用 [`quant-infrastructure/apps/compose-dev.sh`](../../quant-infrastructure/apps/compose-dev.sh) 与 `docker-compose.dev.yml`（已从本仓库迁出；不再维护根目录 `docker-compose.yml`）
- **前端**：`quant-web` 使用 **`../quantFinance-dashboard`** 作为 build context，**`Dockerfile.fast`** 构建（需与 `quantFinance` 同级克隆 `quantFinance-dashboard`）
- **生产编排**：见 `quant-infrastructure/apps/docker-compose.yml` + `deploy.sh`
- **前端 Nginx**：由 `quantFinance-dashboard/nginx.template.conf` / `nginx.http.template.conf` 在容器启动时渲染，`quantFinance` 不再挂载独立 `nginx.conf`

### 环境配置
- `.env.production` - 生产环境变量
- `mongo-init.js` - MongoDB初始化脚本

### 部署脚本
- `deploy.sh` - 一键部署脚本，支持：
  - 环境检查
  - 代码更新
  - 自动构建
  - 服务部署
  - 健康检查
  - 自动备份

### 监控配置
- `monitoring/prometheus.yml` - 指标监控
- 支持Grafana仪表板
- 自动告警配置

## 💡 部署建议

### 阿里云部署
1. **ECS**: 选择4核8GB配置
2. **RDS**: 使用MongoDB云数据库
3. **SLB**: 配置负载均衡
4. **CDN**: 加速静态资源

### 腾讯云部署  
1. **CVM**: 推荐标准型SA2
2. **MongoDB**: 使用TencentDB
3. **CLB**: 七层负载均衡
4. **CDN**: 全球加速

### 安全设置
1. 防火墙只开放必要端口 (80,443,22)
2. 使用强密码和密钥认证
3. 定期更新系统和依赖
4. 配置监控和日志

### 性能优化
1. 启用Gzip压缩
2. 配置Redis缓存
3. 使用CDN加速
4. 数据库索引优化

## 🆘 故障排查

### 查看日志
```bash
# 查看所有服务日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f api
docker-compose logs -f web
docker-compose logs -f mongodb
```

### 健康检查
```bash
# 检查服务状态
docker-compose ps

# 手动健康检查
./deploy.sh production latest health
```

### 常见问题
1. **端口冲突**: 修改docker-compose.yml中的端口映射
2. **内存不足**: 增加服务器配置或优化应用
3. **数据库连接失败**: 检查MongoDB URL和认证信息
4. **CORS错误**: 确认CORS_ORIGINS配置正确

## 📞 支持
如需帮助，请查看：
- 完整部署指南：`CLOUD_DEPLOYMENT_GUIDE.md`
- 监控配置：`monitoring/prometheus.yml`
- 部署脚本：`deploy.sh`
