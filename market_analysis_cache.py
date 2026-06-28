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
        mongodb_url = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
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
            
            # Use quant_analyzer DB for market analysis cache (where quantAnalyzer writes)
            analyzer_db_name = os.getenv("ANALYZER_DB_NAME", "quant_analyzer")
            analyzer_db = client[analyzer_db_name]
            self.collection = analyzer_db['market_analysis_cache']
            # 复合唯一索引：(analysis_date, time_slot)，允许同一日期多个时段（HHMM）共存
            # 旧的 analysis_date 单字段唯一索引由迁移脚本负责 drop（详见 quantFinance/migrations/）
            try:
                self.collection.create_index(
                    [("analysis_date", 1), ("time_slot", 1)],
                    unique=True,
                    name="analysis_date_time_slot_unique",
                )
            except Exception as e:
                # 旧索引未迁移时会冲突，记录但不阻塞应用启动；运维需手动跑迁移脚本
                print(f"[警告] 复合索引创建失败（可能旧 unique 索引仍存在，请运行迁移脚本）：{e}")
    
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
    
    def get_cached_analysis(
        self,
        analysis_date: str = None,
        time_slot: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取缓存的市场分析结果

        Args:
            analysis_date: 分析日期，格式 YYYY-MM-DD，默认为今天
            time_slot: 时段（HHMM 4 位字符串，如 "1130"）。
                - 给定时精确匹配该时段
                - 未给定时返回该日所有 slot 中时间最晚的一条（按 time_slot 字符串降序）

        Returns:
            缓存的分析结果或 None
        """
        if analysis_date is None:
            analysis_date = date.today().strftime('%Y-%m-%d')

        try:
            if time_slot is not None:
                result = self.collection.find_one({
                    "analysis_date": analysis_date,
                    "time_slot": time_slot,
                })
            else:
                # 取该日最近 slot；同时兼容旧无 time_slot 的文档
                cursor = self.collection.find(
                    {"analysis_date": analysis_date}
                ).sort("time_slot", -1).limit(1)
                docs = list(cursor)
                result = docs[0] if docs else None

            if result:
                result.pop('_id', None)
                slot_label = result.get('time_slot', '(legacy)')
                print(f"[缓存] 找到 {analysis_date} slot={slot_label} 的缓存分析结果")
                return result
            else:
                print(f"[缓存] 未找到 {analysis_date} 的缓存分析结果")
                return None
        except Exception as e:
            print(f"[缓存] 获取缓存分析失败：{e}")
            return None

    def list_slots_for_date(self, analysis_date: str) -> list:
        """
        返回指定日期已生成的所有 time_slot（升序）。
        旧文档（无 time_slot 字段）记为 "legacy"。
        """
        try:
            cursor = self.collection.find(
                {"analysis_date": analysis_date},
                # 仅取 time_slot 字段（如果驱动支持 projection）
            )
            slots = []
            for doc in cursor:
                slot = doc.get("time_slot") or "legacy"
                slots.append(slot)
            slots.sort()
            return slots
        except Exception as e:
            print(f"[缓存] 列出 slots 失败：{e}")
            return []

    def get_nearest_previous_snapshot(
        self,
        analysis_date: str,
        target_slot: str,
        max_lookback_days: int = 7,
        max_lag_minutes: int = 30,
    ) -> Optional[Dict[str, Any]]:
        """
        查找上一交易日中与 target_slot 足够接近的 snapshot 字段。

        成交额等快照字段是日内累计值，不能把收盘快照与午盘快照混比。
        因此默认只允许 30 分钟以内的邻近时段；缺少同阶段快照时返回 None。

        Args:
            analysis_date: 当前分析日期 YYYY-MM-DD
            target_slot: 当前分析时段 HHMM
            max_lookback_days: 最大回溯日历天数（默认 7，含周末/假期）
            max_lag_minutes: 允许的最大时段差。None 表示不限制（仅用于兼容/排查）。

        Returns:
            {
                "snapshot": {...},
                "analysis_date": "YYYY-MM-DD",
                "time_slot": "HHMM",
                "lag_minutes": int,  # 与 target_slot 的分钟差（绝对值）
            }
            未找到返回 None。
        """
        try:
            target_minutes = int(target_slot[:2]) * 60 + int(target_slot[2:])
        except (ValueError, IndexError):
            print(f"[缓存] 非法 target_slot={target_slot}")
            return None

        try:
            current_day = date.fromisoformat(analysis_date)
        except ValueError:
            return None

        for offset in range(1, max_lookback_days + 1):
            candidate_day = current_day - timedelta(days=offset)
            if not self.is_trading_day(candidate_day):
                continue
            candidate_str = candidate_day.strftime('%Y-%m-%d')

            try:
                docs = list(self.collection.find({"analysis_date": candidate_str}))
            except Exception as e:
                print(f"[缓存] 查询 {candidate_str} 失败：{e}")
                continue

            # 过滤含 snapshot 的文档（旧 legacy 数据没有 snapshot）
            candidates = []
            for doc in docs:
                slot = doc.get("time_slot")
                snapshot = doc.get("snapshot")
                if not slot or not snapshot:
                    continue
                try:
                    slot_minutes = int(slot[:2]) * 60 + int(slot[2:])
                except (ValueError, IndexError):
                    continue
                lag = abs(slot_minutes - target_minutes)
                if max_lag_minutes is not None and lag > max_lag_minutes:
                    continue
                candidates.append((lag, slot, snapshot, candidate_str))

            if not candidates:
                continue

            # 取 lag 最小的；并列时取 slot 最大（更接近收盘的更新数据）
            candidates.sort(key=lambda x: (x[0], -int(x[1])))
            lag, slot, snapshot, day_str = candidates[0]
            return {
                "snapshot": snapshot,
                "analysis_date": day_str,
                "time_slot": slot,
                "lag_minutes": lag,
            }

        return None

    def get_latest_trading_day_analysis(self) -> Optional[Dict[str, Any]]:
        """
        获取最近交易日的分析结果（取最近一条，含最近 slot）
            
        Returns:
            最近交易日的分析结果或 None
        """
        try:
            start_date = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
                
            # 按 analysis_date 降序，再按 time_slot 降序取首条
            result = self.collection.find(
                {"analysis_date": {"$gte": start_date}}
            ).sort([("analysis_date", -1), ("time_slot", -1)]).limit(1)
                
            result_list = list(result)
            if result_list:
                latest = result_list[0]
                latest.pop('_id', None)
                slot_label = latest.get('time_slot', '(legacy)')
                print(f"[缓存] 找到最近交易日分析结果：{latest['analysis_date']} slot={slot_label}")
                return latest
            else:
                print("[缓存] 未找到最近的交易日分析结果")
                return None
        except Exception as e:
            print(f"[缓存] 获取最近交易日分析失败：{e}")
            return None
    


# 创建全局实例
market_analysis_cache = MarketAnalysisCache()
