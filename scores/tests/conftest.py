import pytest
import os
import sys
from unittest.mock import Mock, MagicMock
from datetime import datetime

# 添加父目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

@pytest.fixture
def mock_db_manager():
    """创建模拟的数据库管理器"""
    mock_manager = Mock()
    
    # 模拟股票基本信息
    mock_manager.get_stock_info.return_value = {
        "symbol": "000002",
        "name": "万科A",
        "industry": "房地产开发",
        "total_share": 1085640.52  # 万股
    }
    
    # 模拟价量数据
    mock_manager.get_volume_price_data.return_value = [{
        "symbol": "000002",
        "trade_date": "20250910",
        "open": 12.50,
        "high": 12.80,
        "low": 12.30,
        "close": 12.65,
        "vol": 1250000,  # 成交量
        "amount": 15812500  # 成交额
    }]
    
    return mock_manager

@pytest.fixture
def sample_financial_data():
    """示例财务数据"""
    return {
        "symbol": "000002",
        "report_date": "20240630",
        "roe": 0.125,              # ROE 12.5%
        "roa": 0.045,              # ROA 4.5%
        "debt_to_asset": 0.65,     # 资产负债率 65%
        "current_ratio": 1.8,      # 流动比率 1.8
        "net_profit_growth": 0.08,  # 净利润增长率 8%
        "gross_margin": 0.35,      # 毛利率 35%
        "net_margin": 0.12,        # 净利率 12%
        "pe_ratio": 18.5,          # PE 18.5倍
        "pb_ratio": 1.5,           # PB 1.5倍
        "eps_growth": 0.06,        # EPS增长率 6%
        "revenue_growth": 0.05,    # 营收增长率 5%
    }

@pytest.fixture
def no_financial_data():
    """无财务数据的情况"""
    return {
        "roe": None, "roa": None, "debt_to_asset": None,
        "current_ratio": None, "net_profit_growth": None,
        "gross_margin": None, "net_margin": None,
        "pe_ratio": None, "pb_ratio": None, "eps_growth": None
    }

@pytest.fixture
def base_date():
    """测试用基准日期"""
    return "20250910"