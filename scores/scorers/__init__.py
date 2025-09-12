from abc import ABC, abstractmethod

class BaseScorer(ABC):
    """
    ✅ 评分器基类
    """
    def __init__(self, db_manager, base_date):
        self.db_manager = db_manager
        self.base_date = base_date
    
    @abstractmethod
    def calculate_score(self, symbol):
        """
        计算简单评分
        
        Args:
            symbol: 股票代码
            
        Returns:
            float: 评分 (0-100)
        """
        pass
    
    @abstractmethod
    def calculate_score_detailed(self, symbol):
        """
        计算详细评分
        
        Args:
            symbol: 股票代码
            
        Returns:
            tuple: (评分, 详细信息字典)
        """
        pass
    
    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        return self.db_manager.get_stock_info(symbol)
    
    def get_volume_price_data(self, symbol, days_back=30):
        """获取价量数据"""
        from datetime import datetime, timedelta
        
        base_dt = datetime.strptime(self.base_date, '%Y%m%d')
        start_date = (base_dt - timedelta(days=days_back)).strftime('%Y%m%d')
        
        return self.db_manager.get_volume_price_data(symbol, start_date, self.base_date)