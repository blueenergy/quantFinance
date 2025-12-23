# 📬 通知提醒系统使用指南

## 概述

通知提醒系统为用户提供实时的 Worker 状态监控、交易信号提醒和日报推送功能，提升用户与系统的互动性。

## 🎯 功能特性

### 1. Worker 超时警告
- **触发条件**: Worker 超过 3 分钟（可配置）未发送心跳
- **通知方式**: 
  - 🔔 站内通知（实时）
  - 📧 邮件通知（如已启用）
- **防重复**: 同一 Worker 30 分钟内只发送一次警告

### 2. 信号生成通知
- **触发条件**: Worker 发现新的交易机会
- **包含信息**:
  - 股票代码
  - 操作类型（买入/卖出）
  - 信号价格
  - 策略名称
- **优先级**: 高

### 3. 日报邮件推送
- **发送时间**: 每日 17:00（市场收盘后）
- **报告内容**:
  - 当日扫描次数
  - 信号生成数量
  - 交易执行笔数
  - 每个 Worker 的详细工作记录
  - 运行时长统计

## 📦 系统架构

```
┌─────────────────────┐
│  Frontend (Vue 3)   │
│  NotificationCenter │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Backend (FastAPI) │
│   /api/notifications│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Celery Beat       │
│  - Worker Timeout   │
│  - Daily Reports    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  Email Service      │
│  (SMTP)             │
└─────────────────────┘
```

## 🚀 快速开始

### 1. 配置邮件服务

编辑 `.env` 文件：

```bash
# Email配置
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password  # Gmail 需要使用应用专用密码
FROM_EMAIL=your-email@gmail.com
FROM_NAME=悟空量化
```

#### Gmail 配置步骤：
1. 登录 Google 账户
2. 访问: https://myaccount.google.com/security
3. 启用"两步验证"
4. 生成"应用专用密码"
5. 将密码填入 `SMTP_PASSWORD`

#### 其他邮箱配置：
- **QQ邮箱**: 
  - SMTP_HOST: smtp.qq.com
  - SMTP_PORT: 587
  - 需要在邮箱设置中开启 SMTP 服务
  
- **163邮箱**:
  - SMTP_HOST: smtp.163.com
  - SMTP_PORT: 465

### 2. 启动 Celery Worker 和 Beat

```bash
# 启动 Celery Worker（处理后台任务）
celery -A celery_app worker --loglevel=info

# 启动 Celery Beat（定时任务调度器）
celery -A celery_app beat --loglevel=info
```

或使用一个命令同时启动：
```bash
celery -A celery_app worker --beat --loglevel=info
```

### 3. 访问前端通知中心

1. 登录系统
2. 点击右上角铃铛图标 🔔
3. 查看通知列表

## 🔧 前端使用

### 查看通知

```vue
<!-- 通知中心位于 App.vue header-right -->
<NotificationCenter />
```

#### 功能按钮：
- 🔔 **铃铛图标**: 点击展开/收起通知面板
- ✓ **全部已读**: 标记所有通知为已读
- ⚙️ **设置**: 打开通知设置面板
- 🗑️ **删除**: 删除单条通知

### 通知设置

在设置面板中可配置：

1. **Worker 超时警告**
   - ☑️ 启用/禁用
   - ⏱️ 超时阈值（分钟）

2. **信号通知**
   - ☑️ 启用/禁用

3. **日报推送**
   - ☑️ 启用/禁用
   - 🕐 发送时间

4. **邮件通知**
   - ☑️ 启用/禁用
   - 📧 测试邮件功能

## 🔌 API 接口

### 获取通知列表

```http
GET /api/notifications?unread_only=false&limit=50
```

**响应**:
```json
{
  "success": true,
  "data": {
    "notifications": [
      {
        "_id": "notification_id",
        "type": "worker_timeout",
        "title": "⚠️ Worker 超时警告",
        "message": "000858.SZ (turtle) 已超过 3 分钟未响应",
        "is_read": false,
        "created_at": 1703001234.567
      }
    ],
    "unread_count": 5,
    "total": 10
  }
}
```

### 标记已读

```http
POST /api/notifications/{notification_id}/read
```

### 获取/更新通知设置

```http
GET /api/notifications/settings
PUT /api/notifications/settings
```

**请求体**:
```json
{
  "worker_timeout_enabled": true,
  "worker_timeout_threshold_minutes": 3,
  "signal_notification_enabled": true,
  "daily_report_enabled": true,
  "daily_report_time": "17:00",
  "email_notifications_enabled": true
}
```

### 发送测试邮件

```http
POST /api/notifications/test-email
```

## 🤖 Worker 集成

在 Worker 代码中集成通知功能：

```python
from worker.activity_reporter import ActivityReporter

# 初始化 Reporter
reporter = ActivityReporter(
    api_base_url="http://backend:8000/api",
    api_token="your_token",
    worker_id="worker_000858_turtle",
    enable_auto_heartbeat=True
)

# 当发现信号时
if signal_found:
    reporter.log_signal_check(
        symbol="000858.SZ",
        strategy="turtle",
        signal_found=True,
        signal_data={
            "order_id": "ORDER123",
            "action": "买入",
            "price": 15.50
        }
    )
    # ↑ 这会自动创建信号通知
```

## 📊 数据库集合

### notifications
```javascript
{
  "_id": "notification_id",
  "user_id": "user_id",
  "type": "worker_timeout/signal_generated/daily_report/system_alert",
  "title": "通知标题",
  "message": "通知消息",
  "data": {},  // 额外数据
  "priority": "low/medium/high",
  "is_read": false,
  "created_at": 1703001234.567,
  "read_at": null
}
```

### notification_settings
```javascript
{
  "_id": "setting_id",
  "user_id": "user_id",
  "worker_timeout_enabled": true,
  "worker_timeout_threshold_minutes": 3,
  "signal_notification_enabled": true,
  "daily_report_enabled": true,
  "daily_report_time": "17:00",
  "email_notifications_enabled": true,
  "updated_at": 1703001234.567
}
```

## 🎨 UI 设计

### 通知面板
- **位置**: Header 右上角
- **尺寸**: 400px × 600px（最大）
- **主题**: 白色背景，圆角卡片
- **动画**: 淡入淡出，流畅过渡

### 通知类型图标
- ⚠️ Worker 超时
- ⚡ 信号生成
- 📊 日报
- 🔔 系统提醒

### 未读标记
- 蓝色背景（#e3f2fd）
- 红色徽章显示数量
- 点击自动标记已读

## 🔍 故障排查

### 1. 收不到邮件

**检查步骤**:
```bash
# 1. 检查环境变量
cat .env | grep SMTP

# 2. 查看 Celery 日志
tail -f celery.log

# 3. 测试 SMTP 连接
python -c "
import smtplib
server = smtplib.SMTP('smtp.gmail.com', 587)
server.starttls()
server.login('your-email', 'your-password')
print('SMTP OK')
"

# 4. 发送测试邮件（通过 API）
curl -X POST http://localhost:8000/api/notifications/test-email \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 2. Worker 超时警告不触发

**检查**:
- Celery Beat 是否运行
- Worker 是否在发送心跳
- 通知设置是否已启用

```bash
# 查看 Celery Beat 任务
celery -A celery_app inspect scheduled

# 查看活跃任务
celery -A celery_app inspect active
```

### 3. 通知不显示

**前端检查**:
```javascript
// 打开浏览器控制台
console.log(localStorage.getItem('access_token'))  // 检查 token
await axios.get('/api/notifications')  // 测试 API
```

## 📈 性能优化

### 1. 通知数量限制
- 每次最多返回 50 条
- 自动清理 30 天前的已读通知

### 2. 邮件发送频率
- Worker 超时警告: 30 分钟内同一 Worker 只发送一次
- 日报: 每天一次
- 信号通知: 实时，无限制

### 3. 数据库索引
```javascript
// MongoDB 索引
db.notifications.createIndex({ user_id: 1, is_read: 1, created_at: -1 })
db.notification_settings.createIndex({ user_id: 1 })
```

## 🔒 安全考虑

1. **邮件密码**: 使用应用专用密码，不要使用主密码
2. **Token 验证**: 所有 API 接口需要认证
3. **用户隔离**: 通知只对所属用户可见
4. **SQL 注入**: 使用 MongoDB ORM，自动防护

## 📝 维护建议

### 定期清理
```python
# 清理 30 天前的已读通知
from datetime import datetime, timedelta
from db import get_db_collections

collections = get_db_collections()
cutoff = (datetime.now() - timedelta(days=30)).timestamp()

result = collections["notifications"].delete_many({
    "is_read": True,
    "created_at": {"$lt": cutoff}
})
print(f"Deleted {result.deleted_count} old notifications")
```

### 监控指标
- 邮件发送成功率
- 通知响应时间
- 未读通知堆积数量
- Celery 队列长度

## 🎉 使用示例

### 场景1: 新用户启用通知

1. 用户登录系统
2. 点击 🔔 铃铛图标
3. 点击 ⚙️ 设置按钮
4. 勾选"启用邮件通知"
5. 点击"发送测试邮件"验证配置
6. 保存设置

### 场景2: Worker 超时处理

1. Worker 因网络问题停止心跳
2. 3 分钟后，Celery Beat 检测到超时
3. 系统自动创建通知
4. 用户收到站内通知 + 邮件
5. 用户点击通知查看详情
6. 重启 Worker 或检查问题

### 场景3: 查看每日工作报告

1. 每天 17:00，系统自动生成日报
2. 用户邮箱收到精美的 HTML 报告
3. 报告包含:
   - 今日扫描 128 次
   - 发现信号 3 个
   - 执行交易 1 笔
   - Worker 详细列表

## 🚀 未来扩展

- [ ] 微信推送集成
- [ ] 钉钉机器人通知
- [ ] Telegram Bot
- [ ] 通知分组管理
- [ ] 自定义通知规则
- [ ] 通知统计分析
- [ ] 批量操作通知

## 📞 技术支持

如有问题，请参考：
- [Worker 监控指南](./WORKER_MONITOR_GUIDE.md)
- [API 文档](http://localhost:8000/docs)
- [系统日志](./logs/)
