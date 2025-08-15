# 量化金融数据平台

一个集成了数据收集、可视化和AI分析的股票数据平台。

## 功能特性

### 📊 数据收集
- **智能数据收集器**: 使用efinance API收集A股市场数据
- **增量更新**: 自动检测本地数据，避免重复请求
- **MongoDB存储**: 高效的数据存储和索引

### 📈 数据可视化
- **Vue3前端**: 现代化的Web界面
- **K线图表**: 基于ECharts的交互式股票图表
- **多时间周期**: 支持日线、周线、月线切换
- **股票监控**: 自定义股票关注列表

### 🤖 AI股票分析
- **多LLM支持**: 集成OpenAI、DeepSeek等多种AI模型
- **智能分析**: 技术面分析、风险评估、投资建议
- **历史记录**: 分析结果存储和对比

## 技术栈

### 后端
- **FastAPI**: 高性能Web框架
- **MongoDB**: 数据存储
- **efinance**: 金融数据源
- **Python 3.9+**: 核心语言

### 前端
- **Vue 3**: 渐进式JavaScript框架
- **Vite**: 快速构建工具
- **ECharts**: 数据可视化
- **Axios**: HTTP客户端

### AI集成
- **LLM服务**: 支持多种大语言模型
- **qwen3-30b**: 默认使用的中文模型
- **Token认证**: 安全的API调用

## 快速开始

### 环境要求
```bash
Python 3.9+
Node.js 18+
MongoDB 4.4+
```

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
