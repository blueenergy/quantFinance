# 🐒 悟空量化金融智能助手

一个基于Vue.js和FastAPI的现代化量化金融分析平台，提供股票数据可视化、智能分析和投资组合管理功能。

## ✨ 主要功能

- 📊 **实时股票数据**: 支持A股、港股、美股数据获取
- 🔍 **智能分析**: 集成多种量化分析指标
- 📈 **数据可视化**: 基于ECharts的专业图表
- 👤 **用户系统**: 完整的用户注册、登录、权限管理
- ⭐ **自选股管理**: 个性化投资组合跟踪
- 🤖 **AI分析**: 集成大语言模型进行投资建议
- 🌙 **现代UI**: 酷炫的暗色主题界面

## 🏗️ 技术栈

### 后端
- **FastAPI**: 高性能Web框架
- **MongoDB**: 文档数据库
- **JWT**: 安全认证
- **AKShare**: 金融数据接口
- **Python 3.12**: 现代Python特性

### 前端  
- **Vue.js 3**: 响应式前端框架
- **Vite**: 快速构建工具
- **ECharts**: 专业图表库
- **Axios**: HTTP客户端

### 部署
- **Docker**: 容器化部署
- **Nginx**: 反向代理
- **Prometheus**: 监控系统
- **Let's Encrypt**: 免费SSL证书

## 🚀 快速开始

### 本地开发

1. **克隆项目**
```bash
git clone <repo-url>
cd quantFinance
```

2. **后端设置**
```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑.env文件设置MongoDB连接等

# 启动后端
uvicorn main:app --reload --port 3001
```

3. **前端设置**
```bash
cd dashboard
npm install
npm run dev
```

4. **访问应用**
- 前端: http://localhost:5173
- 后端API: http://localhost:3001
- API文档: http://localhost:3001/docs

### 云服务部署

> 🔥 **一键部署**: 使用我们提供的自动化部署脚本

```bash
# 1. 服务器环境准备
curl -fsSL https://get.docker.com | sh
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# 2. 配置环境
cp .env.production .env
nano .env  # 修改配置

# 3. 一键部署
chmod +x deploy.sh
./deploy.sh production latest
```

详细部署指南请查看:
- 📘 [完整部署指南](CLOUD_DEPLOYMENT_GUIDE.md)
- ⚡ [快速部署指南](DEPLOYMENT_QUICK_START.md)

### 安装依赖
```bash
# Python依赖
pip install -r requirements.txt

# 前端依赖
cd dashboard
npm install
```

### 环境配置
```bash
# 复制环境变量模板
cp .env.template .env

# 编辑环境变量
nano .env
```

### 启动服务

1. **启动MongoDB**
```bash
sudo systemctl start mongod
```

2. **启动后端API**
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 3001
```

3. **启动前端**
```bash
cd dashboard
npm run dev
```

### 数据收集
```bash
# 智能模式（跳过已有数据）
python volume_price_collector.py

# 强制更新所有数据
python volume_price_collector.py --force

# 仅更新已有股票
python volume_price_collector.py --update-existing
```

## API端点

### 数据查询
- `GET /records/` - 获取股票数据
- `GET /record/{id}` - 获取单条记录

### AI分析
- `POST /api/analyze` - 股票AI分析
- `GET /api/llm-providers` - 获取可用AI服务商
- `GET /api/analysis/history` - 分析历史记录

## 项目结构

```
quantFinance/
├── main.py                 # FastAPI主应用
├── volume_price_collector.py # 数据收集器
├── llm_service.py          # LLM服务封装
├── requirements.txt        # Python依赖
├── .env.template          # 环境变量模板
├── dashboard/             # Vue前端
│   ├── src/
│   │   ├── components/    # Vue组件
│   │   ├── App.vue        # 主应用
│   │   └── main.js        # 入口文件
│   ├── package.json       # 前端依赖
│   └── vite.config.mjs    # Vite配置
└── tests/                 # 测试文件
```

## 数据库结构

### volume_price 集合
```javascript
{
  "symbol": "000001",           // 股票代码
  "trade_date": "2025-08-14",   // 交易日期
  "open": 12.1,                 // 开盘价
  "high": 12.3,                 // 最高价
  "low": 12.0,                  // 最低价
  "close": 12.2,                // 收盘价
  "volume": 1234567,            // 成交量
  "update_time": "2025-08-14T10:00:00" // 更新时间
}
```

### analysis_results 集合
```javascript
{
  "symbol": "000001",           // 股票代码
  "analysis": {...},            // 分析结果
  "provider": "openai",         // AI服务商
  "model": "qwen3-30b",         // 使用模型
  "created_at": "2025-08-14T10:00:00", // 创建时间
  "success": true               // 分析状态
}
```

## 配置说明

### 环境变量
```bash
# LLM API配置
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=http://your_llm_server:8080/v1
DEFAULT_LLM_PROVIDER=openai
DEFAULT_MODEL=qwen3-30b

# 数据库配置
MONGO_URI=mongodb://localhost:27017

# 服务器配置
HOST=0.0.0.0
PORT=3001
```

## 开发指南

### 添加新的数据源
1. 在 `volume_price_collector.py` 中扩展 `get_clean_data` 方法
2. 添加新的数据字段映射
3. 更新数据库索引

### 集成新的LLM服务
1. 在 `llm_service.py` 中添加新的服务商配置
2. 实现对应的API调用逻辑
3. 更新前端服务商选择器

### 自定义分析指标
1. 修改 `_build_stock_analysis_prompt` 方法
2. 扩展分析结果解析逻辑
3. 更新前端显示组件

## 许可证

MIT License

## 贡献

欢迎提交Issue和Pull Request！

## 联系方式

- 项目地址: [GitLab Repository]
- 问题反馈: [Issues]
