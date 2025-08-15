# 测试与工具目录说明

本目录包含量化金融系统的测试文件和数据管理工具，按功能分类如下：

## 📊 数据收集测试
- `test_collector.py` - 股票数据收集器的基础测试
- `test_incremental_sync.py` - 增量同步功能测试
- `test_auto_incremental.py` - 自动计算增量同步天数测试
- `test_smart_sync.py` - 智能增量同步功能测试

## 🏷️ 股票名称管理
- `test_stock_name_sync.py` - 股票名称同步功能测试
- `test_stock_names.py` - 股票名称相关功能测试
- `check_missing_stock_names.py` - 检查缺失股票名称的工具脚本
- `batch_update_stock_names.py` - 批量更新股票名称的优化工具
- `update_single_stock_name.py` - 为特定股票添加名称信息
- `update_stock_names.py` - 股票名称更新脚本

## 🤖 AI/LLM 相关测试
- `test_llm.py` - LLM服务基础测试
- `test_qwen.py` - Qwen模型专项测试

## 🗄️ 数据库测试
- `test_mongdo.py` - MongoDB数据库连接和操作测试

## 🔧 工具脚本
- `run_tests.py` - 测试运行器，批量运行所有测试文件

## 🧪 通用测试
- `test.py` - 通用功能测试

## 使用方法

### 🧪 运行测试

#### 运行单个测试
```bash
cd /home/shuyolin/workbench/own/quantFinance
python tests/test_smart_sync.py
```

#### 运行所有测试 (使用测试运行器)
```bash
cd /home/shuyolin/workbench/own/quantFinance
python tests/run_tests.py
```

#### 使用pytest运行
```bash
cd /home/shuyolin/workbench/own/quantFinance
python -m pytest tests/
```

### 🏷️ 股票名称管理工具

#### 检查股票名称覆盖率
```bash
python tests/check_missing_stock_names.py
```

#### 批量更新股票名称
```bash
# 更新100只股票，每批20只
python tests/batch_update_stock_names.py --limit 100 --batch-size 20

# 更新所有缺失的股票 (需要很长时间)
python tests/batch_update_stock_names.py --batch-size 25
```

#### 更新特定股票名称
```bash
python tests/update_single_stock_name.py 300347
```

#### 传统批量更新方式
```bash
python tests/update_stock_names.py
```

### 📊 数据同步测试

#### 测试智能增量同步
```bash
python tests/test_smart_sync.py
```

#### 测试数据收集器
```bash
python tests/test_collector.py
```

## 📈 工具特性

### 批量股票名称更新工具 (`batch_update_stock_names.py`)
- ✅ 支持指定批次大小和处理数量限制
- ✅ 优雅停止支持 (Ctrl+C)
- ✅ 实时进度显示和统计
- ✅ 自动错误处理和重试机制
- ✅ 批次间休息避免API限流

### 股票名称检查工具 (`check_missing_stock_names.py`)
- 📊 统计总股票数量和名称覆盖率
- 📋 列出缺失名称的股票
- ✅ 显示随机样本验证数据质量
- 📈 提供详细的统计报告

### 测试运行器 (`run_tests.py`)
- 🔄 批量运行所有测试文件
- ✅ 统计测试通过率
- 📊 生成测试报告
- 🎯 支持选择性测试运行

## 🚀 快速开始

### 首次设置股票名称
```bash
# 1. 检查当前状态
python tests/check_missing_stock_names.py

# 2. 批量更新股票名称 (建议从少量开始)
python tests/batch_update_stock_names.py --limit 50 --batch-size 10

# 3. 验证结果
python tests/check_missing_stock_names.py
```

### 运行系统测试
```bash
# 运行所有测试
python tests/run_tests.py

# 或运行特定功能测试
python tests/test_stock_name_sync.py
```

## ⚠️ 注意事项

1. **路径要求**: 所有脚本都需要从项目根目录运行，或确保正确设置了Python路径
2. **数据库连接**: 大部分工具需要MongoDB连接和相关配置
3. **API限制**: 批量更新股票名称时注意API调用频率限制
4. **环境配置**: 确保虚拟环境已激活且依赖包已安装
5. **数据备份**: 批量操作前建议备份重要数据

## 📝 文件组织

```
tests/
├── README.md                     # 本说明文件
├── __init__.py                   # Python包初始化
├── run_tests.py                  # 测试运行器
├── 
├── 📊 数据收集测试
├── test_collector.py
├── test_incremental_sync.py
├── test_auto_incremental.py
├── test_smart_sync.py
├── 
├── 🏷️ 股票名称管理
├── check_missing_stock_names.py
├── batch_update_stock_names.py
├── update_single_stock_name.py
├── update_stock_names.py
├── test_stock_name_sync.py
├── test_stock_names.py
├── 
├── 🤖 AI/LLM测试
├── test_llm.py
├── test_qwen.py
├── 
├── 🗄️ 数据库测试
├── test_mongdo.py
├── 
└── 🧪 通用测试
    └── test.py
```

## 📞 支持

如有问题或需要添加新功能，请在项目中创建issue或直接修改相关测试文件。
