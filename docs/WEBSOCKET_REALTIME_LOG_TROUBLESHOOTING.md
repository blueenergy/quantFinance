# WebSocket 实时日志排障手册

## 适用范围

本文用于排查实盘「实时日志」页面连接失败问题，覆盖以下链路：

- 前端页面：`/log-viewer.html?ws=...`
- 业务接口：`/api/strategy/workers`
- WebSocket 网关：`/ws/{port}`
- 后端服务：`quant-strategy-manager` 的 worker 日志流

## 典型现象

- 页面提示：
  - `❌ WebSocket 连接错误`
  - `⚠️ WebSocket 连接已关闭`
- 浏览器 Network 中 `/ws/{port}` 返回 `502` 或连接瞬断。

## 架构与数据流

1. 前端从 `/api/strategy/workers` 获取 `log_stream_url`
2. `quantFinance` 转发到 `REMOTE_WORKER_API`（通常是 `quant-strategy-manager`）
3. `quant-strategy-manager/api_server.py` 根据环境变量生成 `log_stream_url`
4. `log-viewer.html` 用 `new WebSocket(wsUrl)` 建连
5. 生产建议通过 Nginx `/ws/{port}` 转发到 worker 动态端口

## 模式说明

### 模式 A：直连 worker 端口（开发友好）

- `USE_NGINX_WEBSOCKET=false`
- 返回：`ws://<worker-host>:<worker-port>`
- 优点：链路简单，便于本地调试
- 风险：跨机器、云环境、防火墙场景下容易失败

### 模式 B：Nginx 网关转发（生产推荐）

- `USE_NGINX_WEBSOCKET=true`
- 返回：`ws://<PUBLIC_HOST>/ws/{port}` 或 `wss://...`
- 优点：统一入口，不暴露动态端口给浏览器
- 要求：Nginx 必须正确配置 `/ws/{port}` 代理

## 关键配置

### 1) `quant-strategy-manager` 相关变量

建议在 `.env` 管理：

```bash
USE_NGINX_WEBSOCKET=true
PUBLIC_HOST=10.181.200.185:8088
USE_HTTPS=false
```

### 2) `docker-compose.yml` 变量引用

避免写死，改为读取 `.env`：

```yaml
- USE_NGINX_WEBSOCKET=${USE_NGINX_WEBSOCKET:-true}
- PUBLIC_HOST=${PUBLIC_HOST}
- USE_HTTPS=${USE_HTTPS:-false}
```

### 3) Nginx `/ws/{port}` 反向代理

```nginx
location ~ ^/ws/(\d+)$ {
    proxy_pass http://quant-strategy-manager:$1;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

## Docker 场景关键坑

当 `proxy_pass` 使用变量（例如 `$1`）时，Nginx 需要 `resolver`，否则会报：

- `no resolver defined to resolve quant-strategy-manager`

修复：

```nginx
resolver 127.0.0.11 ipv6=off;
```

说明：`127.0.0.11` 是 Docker 内置 DNS。

## 最小验证流程

### 第 1 步：重启关键服务

```bash
cd /home/shuyolin/trading/quantFinance
docker compose up -d --force-recreate quant-strategy-manager quant-web
```

### 第 2 步：检查 workers 返回

```bash
curl -s http://10.181.200.185:8088/api/strategy/workers | jq .
```

检查 `log_stream_url` 是否形如：

- `ws://10.181.200.185:8088/ws/<port>`

### 第 3 步：浏览器确认握手

- 打开实时日志页面
- 在浏览器开发者工具 Network 查看 WebSocket 请求
- 预期状态：`101 Switching Protocols`

### 第 4 步：查看网关日志（失败时）

```bash
docker logs quant-web --tail 200 | egrep -i "ws/|resolver|upstream|502|connect"
```

## 常见错误与处理

### 错误 1：`no resolver defined to resolve quant-strategy-manager`

- 原因：`proxy_pass` 使用变量端口，但未配置 `resolver`
- 处理：添加 `resolver 127.0.0.11 ipv6=off;` 并重启 `quant-web`

### 错误 2：返回还是 `ws://ip:动态端口`

- 原因：`USE_NGINX_WEBSOCKET` 未生效或容器未重建
- 处理：确认环境变量、重建 `quant-strategy-manager`

### 错误 3：页面是 HTTPS，WebSocket 却是 `ws://`

- 原因：混合内容策略拦截
- 处理：`USE_HTTPS=true`，返回 `wss://...`

## 非 Docker 场景说明

如果 `quant-strategy-manager` 非 Docker 部署：

- 不能依赖 `resolver 127.0.0.11`
- `proxy_pass http://quant-strategy-manager:$1` 也可能无法解析

可选方案：

- 使用固定地址：`proxy_pass http://127.0.0.1:$1`（同机）
- 或使用可解析的内网域名/IP，并配置系统 DNS

## 回滚方案

如需回退到直连模式：

1. 设置 `USE_NGINX_WEBSOCKET=false`
2. 移除或忽略 Nginx `/ws/{port}` 路由
3. 重启 `quant-strategy-manager`（必要时重启 `quant-web`）

## 经验总结

- 生产优先选择网关模式（`/ws/{port}`）
- 配置项放 `.env`，避免写死到 `docker-compose.yml`
- 先看 `workers` 返回 URL，再看浏览器 Network，最后看 Nginx 日志
- `502` 往往不是前端 bug，而是网关转发链路问题
