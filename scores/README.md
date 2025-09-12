# 🎯 量化投资评分系统

## 📖 项目简介

这是一个综合的股票评分系统，通过多维度分析为股票投资提供量化评分和建议。

### 🏗️ 系统架构

```
quantFinance/scores/
├── scorers/                    # 评分器模块
│   ├── __init__.py            # 基础评分器
│   ├── technical_scorer.py    # 技术分析评分器
│   ├── value_scorer.py        # 价值分析评分器  
│   ├── growth_scorer.py       # 成长分析评分器
│   ├── money_flow_scorer.py   # 资金流分析评分器
│   └── cycle_scorer.py        # 周期分析评分器
├── tests/                     # 测试模块
├── stock_analyzer.py          # 综合分析器
├── daily_screening.py         # 每日选股工具
└── run_tests.py              # 测试运行器
```

### 🎯 评分维度

| 维度 | 权重 | 说明 |
|------|------|------|
| 🔧 技术分析 | 25% | 技术指标、趋势分析、支撑阻力 |
| 💰 价值分析 | 20% | PE、PB、ROE等估值指标 |
| 📈 成长分析 | 20% | 营收增长、利润增长、行业前景 |
| 💸 资金流分析 | 20% | 主力资金、机构资金流向 |
| 🔄 周期分析 | 15% | 短中长期技术周期 |

### 🚀 快速开始

#### 1. 安装依赖
```bash
pip install pandas numpy
```

#### 2. 运行测试
```bash
python run_tests.py all
```

#### 3. 单股分析
```python
from stock_analyzer import StockAnalyzer

# 创建分析器（需要配置数据库连接）
analyzer = StockAnalyzer(db_manager)

# 分析单只股票
result = analyzer.analyze_stock("000002")
print(f"综合评分: {result['composite_score']}")
```

#### 4. 批量筛选
```python
# 批量分析
symbols = ["000002", "000001", "000858"]
df = analyzer.batch_analyze(symbols)

# 获取高分股票
top_stocks = analyzer.get_top_stocks(symbols, top_n=10)
```

### 📊 评分解读

| 评分区间 | 投资建议 | 说明 |
|----------|----------|------|
| 80-100分 | 🌟 强烈推荐 | 各维度表现优秀，值得重点关注 |
| 70-79分 | 👍 推荐 | 综合表现较好，可以配置 |
| 60-69分 | 🤔 谨慎关注 | 表现一般，需要谨慎投资 |
| 50-59分 | ⚠️ 不推荐 | 存在较多问题，暂不推荐 |
| <50分 | ❌ 回避 | 表现较差，建议回避 |

### 🔧 使用工具

#### 每日选股
```bash
python daily_screening.py --min-score 70 --top-n 20
```

#### 自定义分析
```python
# 自定义权重
analyzer.weights = {
    'technical': 0.3,
    'value': 0.3, 
    'growth': 0.25,
    'money_flow': 0.1,
    'cycle': 0.05
}
```

### 🧪 测试

运行全部测试：
```bash
python run_tests.py all
```

运行特定测试：
```bash
python run_tests.py technical
python run_tests.py value
python run_tests.py money_flow
```

### 📋 数据要求

系统需要以下数据：
- 📈 日线价格数据（开高低收、成交量）
- 💰 财务数据（PE、PB、ROE等）
- 💸 资金流数据（大单、超大单净流入）
- 📊 基本信息（股本、行业等）

### ⚠️ 免责声明

本系统仅供学习和研究使用，不构成投资建议。投资有风险，决策需谨慎。

### 📞 联系方式

如有问题或建议，请通过以下方式联系：
- 📧 Email: your.email@example.com
- 🐛 Issues: GitHub Issues

---
⭐ 如果这个项目对您有帮助，请给个星标！
