"""
市场分析缓存管理模块
处理交易日判断、分析结果缓存和数据库存储
"""

import os
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional
from pymongo import MongoClient
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

class MarketAnalysisCache:
    """市场分析缓存管理器"""
    
    def __init__(self):
        # Attempt to connect to MongoDB. If unavailable (e.g. during unit tests),
        # fall back to a lightweight in-memory collection to avoid import-time failures.
        mongodb_url = os.getenv('DB_CONNECTION_URL', os.getenv('MONGO_URL', 'mongodb://localhost:27017'))
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

            def replace_one(self, filter_q, doc, upsert=False):
                # simplistic upsert behavior
                existing = self.find_one(filter_q)
                if existing:
                    # replace fields
                    for i, d in enumerate(self._docs):
                        if all(item in d.items() for item in filter_q.items()):
                            self._docs[i] = doc.copy()
                            return
                if upsert:
                    self._docs.append(doc.copy())

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
                class Cursor(list):
                    def sort(self, *args, **kwargs):
                        return self

                    def skip(self, n):
                        return Cursor(list(self)[n:])

                    def limit(self, n):
                        return Cursor(list(self)[:n])

                return Cursor(results)

            def delete_many(self, query):
                before = len(self._docs)
                self._docs = [d for d in self._docs if not all(item in d.items() for item in query.items())]
                class Result:
                    def __init__(self, deleted_count):
                        self.deleted_count = deleted_count
                return Result(before - len(self._docs))

        # Decide whether to use an in-memory fallback. Prefer explicit opt-in
        # so production does not silently use test behavior. Tests will set
        # USE_IN_MEMORY_DB=1 in their environment (see tests/conftest.py).
        use_in_memory = os.getenv('USE_IN_MEMORY_DB', '').lower() in ('1', 'true', 'yes')
        # Pytest sets PYTEST_CURRENT_TEST in the environment for running tests;
        # allow that as an implicit opt-in for local test runs.
        if not use_in_memory and os.getenv('PYTEST_CURRENT_TEST'):
            use_in_memory = True

        if use_in_memory:
            # Use in-memory implementation without attempting network connection
            self.db = None
            self.collection = _InMemoryCollection()
        else:
            # In non-test environments, attempt to connect to a real MongoDB
            # and surface connection errors so deployment issues are visible.
            client = MongoClient(mongodb_url, serverSelectionTimeoutMS=2000)
            self.db = client[db_name]
            self.collection = self.db['market_analysis_cache']
            # attempt a quick operation to validate connection
            self.collection.create_index([("analysis_date", 1)], unique=True)
    
    def is_trading_day(self, check_date: date = None) -> bool:
        """
        判断是否为A股交易日
        简化版本：排除周末，实际应用中可以接入更精确的交易日历API
        """
        if check_date is None:
            check_date = date.today()
        
        # 排除周末
        if check_date.weekday() >= 5:  # 5=周六, 6=周日
            return False
        
        # 这里可以添加更多的节假日判断逻辑
        # 比如春节、国庆节等
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
    
    def save_analysis(self, analysis_result: Dict[str, Any], analysis_date: str = None) -> bool:
        """
        保存市场分析结果到缓存
        
        Args:
            analysis_result: 分析结果
            analysis_date: 分析日期，格式 YYYY-MM-DD，默认为今天
            
        Returns:
            保存是否成功
        """
        if analysis_date is None:
            analysis_date = date.today().strftime('%Y-%m-%d')
        
        try:
            # 准备缓存数据
            cache_data = {
                "analysis_date": analysis_date,
                "created_at": datetime.now(),
                "analysis_result": analysis_result,
                "cache_version": "1.0"
            }
            
            # 使用 upsert 操作：如果存在则更新，不存在则插入
            self.collection.replace_one(
                {"analysis_date": analysis_date},
                cache_data,
                upsert=True
            )
            
            print(f"[缓存] 保存 {analysis_date} 的分析结果成功")
            return True
        except Exception as e:
            print(f"[缓存] 保存分析结果失败: {e}")
            return False
    
    def should_analyze_market(self, check_date: date = None) -> tuple[bool, str]:
        """
        判断是否需要进行市场分析
        
        Args:
            check_date: 检查日期，默认为今天
            
        Returns:
            (是否需要分析, 原因说明)
        """
        if check_date is None:
            check_date = date.today()
        
        analysis_date = check_date.strftime('%Y-%m-%d')
        
        # 1. 检查是否为交易日
        if not self.is_trading_day(check_date):
            weekday_name = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][check_date.weekday()]
            return False, f"今天是{weekday_name}，非交易日"
        
        # 2. 检查是否已有缓存
        cached_result = self.get_cached_analysis(analysis_date)
        if cached_result:
            return False, f"已有 {analysis_date} 的分析结果缓存"
        
        return True, f"{analysis_date} 是交易日且无缓存，需要分析"
    
    def get_latest_trading_day_analysis(self) -> Optional[Dict[str, Any]]:
        """
        获取最近交易日的分析结果
        
        Returns:
            最近交易日的分析结果或None
        """
        try:
            # 查找最近7天内的分析结果
            start_date = (date.today() - timedelta(days=7)).strftime('%Y-%m-%d')
            
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
    
    def clean_old_cache(self, keep_days: int = 30):
        """
        清理旧的缓存数据
        
        Args:
            keep_days: 保留天数，默认30天
        """
        try:
            cutoff_date = (date.today() - timedelta(days=keep_days)).strftime('%Y-%m-%d')
            result = self.collection.delete_many({"analysis_date": {"$lt": cutoff_date}})
            print(f"[缓存] 清理 {result.deleted_count} 条超过 {keep_days} 天的缓存记录")
        except Exception as e:
            print(f"[缓存] 清理旧缓存失败: {e}")

# 创建全局实例
market_analysis_cache = MarketAnalysisCache()
