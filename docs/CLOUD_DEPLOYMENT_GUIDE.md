# 悟空量化金融智能助手 - 云服务部署指南

## 🚀 部署架构概览

### 技术栈
- **后端**: FastAPI (Python)
- **前端**: Vue.js 3 + Vite
- **数据库**: MongoDB
- **认证**: JWT Token
- **API**: RESTful API

### 推荐云服务架构
```
用户浏览器 → CDN → 负载均衡器 → Web服务器 → API服务器 → MongoDB数据库
```

## ☁️ 云服务提供商选择

### 1. 阿里云 (推荐)
**优势**: 国内访问速度快，支持人民币支付
- **ECS**: 弹性计算服务器
- **RDS**: 云数据库MongoDB版
- **SLB**: 负载均衡
- **CDN**: 内容分发网络
- **OSS**: 对象存储

### 2. 腾讯云
**优势**: 性价比高，国内服务稳定
- **CVM**: 云服务器
- **TencentDB**: MongoDB数据库
- **CLB**: 负载均衡
- **CDN**: 加速服务

### 3. 华为云
**优势**: 企业级可靠性
- **ECS**: 弹性云服务器
- **DDS**: 文档数据库服务
- **ELB**: 弹性负载均衡

### 4. AWS (国际)
**优势**: 功能最全面
- **EC2**: 虚拟服务器
- **DocumentDB**: MongoDB兼容数据库
- **ALB**: 应用负载均衡器
- **CloudFront**: CDN

## 🏗️ 基础设施配置

### 服务器配置要求

#### 最小配置 (开发/测试)
```yaml
CPU: 2核
内存: 4GB
存储: 40GB SSD
带宽: 3Mbps
操作系统: Ubuntu 20.04/22.04 LTS
```

#### 推荐配置 (生产环境)
```yaml
CPU: 4核
内存: 8GB
存储: 100GB SSD
带宽: 10Mbps
操作系统: Ubuntu 22.04 LTS
```

#### 高可用配置
```yaml
Web服务器: 2台 (4核8GB)
API服务器: 2台 (4核8GB)
数据库: MongoDB集群 (3节点)
负载均衡器: 1台
CDN: 全球节点
```

## 🗄️ 数据库配置

### MongoDB云数据库配置
```yaml
# 最小配置
实例规格: 2核4GB
存储空间: 100GB
副本集: 1主2从
备份策略: 每日自动备份
```

### 连接配置
```python
# 生产环境MongoDB连接
MONGODB_URL = "mongodb://username:password@mongodb-cluster.xxx.com:27017/quantfinance?authSource=admin&replicaSet=rs0"
```

## 🐳 Docker容器化部署

### 1. 后端Dockerfile
```dockerfile
# Dockerfile.backend
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 暴露端口
EXPOSE 3001

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3001", "--workers", "4"]
```

### 2. 前端 Docker 镜像（`quantFinance-dashboard`）

前端为独立仓库 **`quantFinance-dashboard`**。与本仓库 `docker-compose.yml` 对齐时，`quant-web` 服务使用：

```yaml
build:
  context: ../quantFinance-dashboard
  dockerfile: Dockerfile.fast
```

多阶段构建（Node 20 + nginx）、国内镜像与 `VITE_*` 构建参数等，均以该仓库内 **`Dockerfile.fast`** 为准，此处不重复粘贴。

### 3. docker-compose.yml
```yaml
version: '3.8'

services:
  # 后端API服务
  api:
    build: 
      context: .
      dockerfile: Dockerfile.backend
    ports:
      - "3001:3001"
    environment:
      - MONGODB_URL=${MONGODB_URL}
      - JWT_SECRET=${JWT_SECRET}
      - ENVIRONMENT=production
    depends_on:
      - mongodb
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3001/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # 前端 Web（与 quantFinance/docker-compose.yml 一致：dashboard 仓库 + Dockerfile.fast）
  web:
    build:
      context: ../quantFinance-dashboard
      dockerfile: Dockerfile.fast
    ports:
      - "80:80"
    depends_on:
      - api
    restart: unless-stopped

  # MongoDB数据库 (开发环境)
  mongodb:
    image: mongo:7.0
    ports:
      - "27017:27017"
    environment:
      - MONGO_INITDB_ROOT_USERNAME=${MONGO_USERNAME}
      - MONGO_INITDB_ROOT_PASSWORD=${MONGO_PASSWORD}
      - MONGO_INITDB_DATABASE=quantfinance
    volumes:
      - mongodb_data:/data/db
      - ./mongo-init.js:/docker-entrypoint-initdb.d/mongo-init.js:ro
    restart: unless-stopped

  # Redis缓存 (可选)
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    restart: unless-stopped

volumes:
  mongodb_data:
  redis_data:
```

## 🌐 Nginx配置

当前 Docker 前端服务不再从 `quantFinance/nginx.conf` 挂载配置。`quant-web` 使用 `../quantFinance-dashboard` 构建镜像，并在容器启动时通过 `quantFinance-dashboard/nginx.template.conf` 或 `nginx.http.template.conf` 渲染 Nginx 配置。下面的完整 `nginx.conf` 仅适用于手工维护宿主机 Nginx 的传统部署方式。

### 手工宿主机 nginx.conf 示例
```nginx
events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # 日志格式
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log /var/log/nginx/access.log main;
    error_log /var/log/nginx/error.log;

    # 基础设置
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;

    # Gzip压缩
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/json;

    # 上游API服务器
    upstream api_backend {
        server api:3001;
        # 如果有多个API服务器，可以添加更多
        # server api2:3001;
    }

    server {
        listen 80;
        server_name _;
        root /usr/share/nginx/html;
        index index.html;

        # 前端静态文件
        location / {
            try_files $uri $uri/ /index.html;
            
            # 缓存设置
            location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg)$ {
                expires 1y;
                add_header Cache-Control "public, immutable";
            }
        }

        # API代理
        location /api/ {
            proxy_pass http://api_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            
            # WebSocket支持
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            
            # 超时设置
            proxy_connect_timeout 60s;
            proxy_send_timeout 60s;
            proxy_read_timeout 60s;
        }

        # 健康检查
        location /health {
            access_log off;
            return 200 "healthy\n";
            add_header Content-Type text/plain;
        }
    }
}
```

## 🔒 SSL/HTTPS配置

### Let's Encrypt免费SSL证书
```bash
# 安装Certbot
sudo apt install certbot python3-certbot-nginx

# 获取SSL证书
sudo certbot --nginx -d yourdomain.com

# 自动续期
sudo crontab -e
# 添加: 0 12 * * * /usr/bin/certbot renew --quiet
```

### Nginx HTTPS配置
```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    
    # 其他配置...
}

# HTTP重定向到HTTPS
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

## 📊 监控和日志

### 1. 应用监控配置
```python
# 在main.py中添加健康检查端点
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    }

# 添加Prometheus监控
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app)
```

### 2. 日志配置
```python
import logging
from logging.handlers import RotatingFileHandler

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('app.log', maxBytes=10485760, backupCount=5),
        logging.StreamHandler()
    ]
)
```

## 🚀 部署脚本

### deploy.sh
```bash
#!/bin/bash
set -e

echo "🚀 开始部署悟空量化金融智能助手..."

# 拉取最新代码
git pull origin main

# 构建前端
echo "📦 构建前端..."
cd dashboard
npm ci
npm run build
cd ..

# 构建Docker镜像
echo "🐳 构建Docker镜像..."
docker-compose build

# 启动服务
echo "▶️ 启动服务..."
docker-compose down
docker-compose up -d

# 健康检查
echo "🔍 健康检查..."
sleep 30
curl -f http://localhost/health || exit 1
curl -f http://localhost:3001/health || exit 1

echo "✅ 部署完成！"
echo "🌐 前端地址: http://yourdomain.com"
echo "🔧 API地址: http://yourdomain.com/api"
```

## ⚙️ 环境变量配置

### .env.production
```bash
# 数据库配置
MONGODB_URL=mongodb://username:password@your-mongodb-cluster/quantfinance
MONGODB_DB_NAME=quantfinance

# JWT配置
JWT_SECRET=your-super-secret-jwt-key-change-this-in-production
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

# 应用配置
ENVIRONMENT=production
DEBUG=false
CORS_ORIGINS=https://yourdomain.com

# API密钥 (如果使用第三方服务)
OPENAI_API_KEY=your-openai-api-key
QWEN_API_KEY=your-qwen-api-key

# 邮件配置 (如果需要)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
```

## 🔧 生产环境优化

### 1. 性能优化
```python
# 在main.py中添加
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

# 添加中间件
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["yourdomain.com"])

# 数据库连接池
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio

class Database:
    client: AsyncIOMotorClient = None

db = Database()

async def get_database() -> AsyncIOMotorClient:
    return db.client

@app.on_event("startup")
async def startup_db_client():
    db.client = AsyncIOMotorClient(
        MONGODB_URL,
        maxPoolSize=10,
        minPoolSize=5,
        maxIdleTimeMS=45000,
    )

@app.on_event("shutdown")
async def shutdown_db_client():
    db.client.close()
```

### 2. 安全配置
```python
# 安全头设置
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(HTTPSRedirectMiddleware)

# 更严格的CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# 限流
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.get("/api/data")
@limiter.limit("100/minute")
def get_data(request: Request):
    return {"data": "example"}
```

## 💰 成本估算

### 阿里云成本 (月费用)
```
- ECS服务器 (4核8GB): ¥300-500
- RDS MongoDB: ¥200-400
- SLB负载均衡: ¥50-100
- CDN流量: ¥50-200
- 域名+SSL: ¥100-200
总计: ¥700-1400/月
```

### 优化建议
1. **使用预付费**: 可节省20-30%成本
2. **弹性伸缩**: 根据访问量自动调整资源
3. **CDN优化**: 减少服务器带宽成本
4. **数据库读写分离**: 提高性能降低成本

## 📋 部署检查清单

### 部署前检查
- [ ] 域名已备案并解析
- [ ] SSL证书已配置
- [ ] 数据库备份策略已制定
- [ ] 监控和告警已设置
- [ ] 安全扫描已完成
- [ ] 性能测试已通过

### 部署后验证
- [ ] 网站可正常访问
- [ ] API接口功能正常
- [ ] 用户注册登录正常
- [ ] 数据库连接正常
- [ ] 监控指标正常
- [ ] 日志记录正常

这个部署指南涵盖了从基础设施到生产环境的完整配置，可以根据实际需求选择适合的云服务提供商和配置方案。
