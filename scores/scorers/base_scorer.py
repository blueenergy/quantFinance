from abc import ABC, abstractmethod
import pandas as pd
import numpy as np

class BaseScorer(ABC):
    """基础评分器 - 简化版本"""
    
    def __init__(self, db_manager, base_date=None):
        self.db_manager = db_manager
        self.base_date = base_date
    
    @abstractmethod
    def calculate_score(self, symbol):
        """计算评分（子类必须实现）"""
        pass
    
    @abstractmethod
    def calculate_score_detailed(self, symbol):
        """计算详细评分（子类必须实现）"""
        pass
    
    # ==================== 简单的数据访问方法 ====================
    
    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        try:
            if hasattr(self.db_manager, 'get_stock_info'):
                return self.db_manager.get_stock_info(symbol)
            else:
                collection = self.db_manager.db['stocks_basic']
                return collection.find_one({"symbol": symbol}) or {}
        except Exception as e:
            print(f"⚠️ 获取股票信息失败 {symbol}: {e}")
            return {}
    
    def get_volume_price_data(self, symbol, days):
        """获取价格成交量数据"""
        try:
            if hasattr(self.db_manager, 'get_volume_price_data'):
                return self.db_manager.get_volume_price_data(symbol, days)
            else:
                collection = self.db_manager.db['volume_price']
                cursor = collection.find(
                    {"symbol": symbol},
                    sort=[("trade_date", -1)]
                ).limit(days)
                return list(cursor)
        except Exception as e:
            print(f"⚠️ 获取价格数据失败 {symbol}: {e}")
            return []