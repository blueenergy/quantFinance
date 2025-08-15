# 测试文件说明

本目录包含量化金融系统的各种测试文件，按功能分类如下：

## 📊 数据收集测试
- `test_collector.py` - 股票数据收集器的基础测试
- `test_incremental_sync.py` - 增量同步功能测试
- `test_auto_incremental.py` - 自动计算增量同步天数测试
- `test_smart_sync.py` - 智能增量同步功能测试

## 🤖 AI/LLM 相关测试
- `test_llm.py` - LLM服务基础测试
- `test_qwen.py` - Qwen模型专项测试

## 🗄️ 数据库测试
- `test_mongdo.py` - MongoDB数据库连接和操作测试

## 🧪 通用测试
- `test.py` - 通用功能测试

## 使用方法

### 运行单个测试
```bash
cd /home/shuyolin/workbench/own/quantFinance
python tests/test_smart_sync.py
```

### 运行所有测试
```bash
cd /home/shuyolin/workbench/own/quantFinance
python -m pytest tests/
```

### 从tests目录内运行
```bash
cd tests
python test_smart_sync.py
```

## 注意事项

1. 所有测试文件都需要从项目根目录运行，或者确保正确设置了Python路径
2. 某些测试可能需要数据库连接和外部API密钥
3. 建议在运行测试前确保环境变量已正确配置
