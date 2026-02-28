"""
市场分析缓存读取模块 (Read-Only DAO)
仅负责读取分析结果，写操作由 quantAnalyzer 服务负责
"""

import os
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional
from pymongo import MongoClient
from dotenv import load_dotenv
try:
    from services.tushare_cached import is_trading_day as check_tushare_trading_day
except ImportError:
    # If services package not found (e.g. running script directly), define dummy
    def check_tushare_trading_day(date_str): return None

# 加载环境变量
load_dotenv()

class MarketAnalysisCache:
    """市场分析缓存管理器"""
    
    def __init__(self):
        # ... (unchanged init) ...
        # Attempt to connect to MongoDB. If unavailable (e.g. during unit tests),
        # fall back to a lightweight in-memory collection to avoid import-time failures.
        mongodb_url = os.getenv('DB_CONNECTION_URL', os.getenv('MONGO_URL', os.getenv('MONGO_URI', 'mongodb://localhost:27017')))
        db_name = os.getenv('MONGO_DB', 'finance')

        class _InMemoryCollection:
            def __init__(self):
                self._docs = []

            def create_index(self, *args, **kwargs):
                # no-op for in-memory
                return None

            def find_one(self, query):
                for d in self._docs:
                    match = True
                    for k, v in query.items():
                        if k not in d or d.get(k) != v:
                            match = False
                            break
                    if match:
                        return d.copy()
                return None

            def find(self, query=None):
                results = []
                if not query:
                    results = [d.copy() for d in self._docs]
                else:
                    for d in self._docs:
                        match = True
                        for k, v in query.items():
                            if k not in d or d.get(k) != v:
                                match = False
                                break
                        if match:
                            results.append(d.copy())
                
                # Simple cursor mock
                class Cursor(list):
                    def sort(self, *args, **kwargs):
                        return self
                    def skip(self, n):
                        return Cursor(list(self)[n:])
                    def limit(self, n):
                        return Cursor(list(self)[:n])
                return Cursor(results)
             
        # ... (rest of init unchanged) ...
        use_in_memory = os.getenv('USE_IN_MEMORY_DB', '').lower() in ('1', 'true', 'yes')
        if not use_in_memory and os.getenv('PYTEST_CURRENT_TEST'):
            use_in_memory = True

        if use_in_memory:
            self.db = None
            self.collection = _InMemoryCollection()
        else:
            client = MongoClient(mongodb_url, serverSelectionTimeoutMS=2000)
            self.db = client[db_name]
            self.collection = self.db['market_analysis_cache']
            self.collection.create_index([("analysis_date", 1)], unique=True)
    
    def is_trading_day(self, check_date: date = None) -> bool:
        """
        判断是否为A股交易日
        优先使用 Tushare 接口校验，失败则回退到本地逻辑
        """
        if check_date is None:
            check_date = date.today()
        
        check_date_str = check_date.strftime('%Y-%m-%d')

        # 1. 尝试使用 Tushare 接口 (权威数据)
        try:
            is_open = check_tushare_trading_day(check_date_str)
            if is_open is not None:
                return is_open
        except Exception as e:
            print(f"[警告] Tushare 交易日校验失败，回退到本地逻辑: {e}")

        # 2. 本地兜底逻辑 (Fallback)
        # 排除周末
        if check_date.weekday() >= 5:  # 5=周六, 6=周日
            return False
            
        return True
    
    def get_cached_analysis(self, analysis_date: str = None) -> Optional[Dict[str, Any]]:
        """
        获取缓存的市场分析结果
        
        Args:
            analysis_date: 分析日期，格式 YYYY-MM-DD，默认为今天
            
        Returns:
            缓存的分析结果或None
        """
        if analysis_date is None:
            analysis_date = date.today().strftime('%Y-%m-%d')
        
        try:
            result = self.collection.find_one({"analysis_date": analysis_date})
            if result:
                # 移除MongoDB的_id字段
                result.pop('_id', None)
                print(f"[缓存] 找到 {analysis_date} 的缓存分析结果")
                return result
            else:
                print(f"[缓存] 未找到 {analysis_date} 的缓存分析结果")
                return None
        except Exception as e:
            print(f"[缓存] 获取缓存分析失败: {e}")
            return None
    

    
    def get_latest_trading_day_analysis(self) -> Optional[Dict[str, Any]]:
        """
        获取最近交易日的分析结果
        
        Returns:
            最近交易日的分析结果或None
        """
        try:
            # 查找最近30天内的分析结果
            start_date = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
            
            result = self.collection.find(
                {"analysis_date": {"$gte": start_date}}
            ).sort("analysis_date", -1).limit(1)
            
            result_list = list(result)
            if result_list:
                latest = result_list[0]
                latest.pop('_id', None)
                print(f"[缓存] 找到最近交易日分析结果: {latest['analysis_date']}")
                return latest
            else:
                print("[缓存] 未找到最近的交易日分析结果")
                return None
        except Exception as e:
            print(f"[缓存] 获取最近交易日分析失败: {e}")
            return None
    


# 创建全局实例
market_analysis_cache = MarketAnalysisCache()
