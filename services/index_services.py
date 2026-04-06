# app/services/index_service.py
from typing import List, Optional
from models.index_models import IndexConstituent, HS300Constituent
from services.tushare_cached import (
    get_index_weight,
    get_latest_trade_date,
    get_stock_basic_map,
    get_daily_basic_map,
    TushareNotConfigured
)
class IndexService:
    """指数数据服务 - 处理业务逻辑"""
    
    def __init__(self):
        # self.db = get_database()
        # self.pro = ts.pro_api()  # tushare API
        pass
    
    async def get_index_constituents(self, index_code: str, force_refresh: bool = False) -> List[IndexConstituent]:
        """获取任意指数成分股（优先缓存）

        index_code 示例：
          399300.SZ  -> 沪深300
          000688.SH  -> 科创50 (示例，在 tushare 中真实代码可能为 000688.SH)
          000905.SH  -> 中证500
          000922.SH  -> 中证红利等（这
        此处先映射我们需求：
          a500  -> 000922.SH (示例)  (如果后续确认真实代码替换)
          star50 -> 000688.SH (示例)
          hs300 -> 399300.SZ
        """
        if not force_refresh:
            cached = await self._get_cached_constituents(index_code=index_code)
            if cached:
                print(f"从缓存获取到 {len(cached)} 只 {index_code} 成分股")
                return cached

        try:
            api_data = await self.get_index_constituents_from_api(index_code=index_code)
            if api_data:
                print(f"成功从API获取到 {len(api_data)} 只 {index_code} 成分股")
                self._cache_constituents(api_data, index_code=index_code)
                return api_data
        except Exception as e:
            print(f"获取 {index_code} 成分股 API 失败: {e}")

        # fallback 静态数据（根据 index_code 提供简单模拟）
        # ✅ 本地兜底数据（当 API / Token 缺失时使用）
        #   补充了小写别名，避免别名查不到而返回空列表的问题（之前 star50 -> 无数据）
        fallback_map = {
            # HS300
            "399300.SZ": [
                {"symbol": "000001", "name": "平安银行", "industry": "银行", "market_cap": 280000000000, "weight": 0.85},
                {"symbol": "000002", "name": "万科A", "industry": "房地产开发", "market_cap": 250000000000, "weight": 0.78},
                {"symbol": "600519", "name": "贵州茅台", "industry": "食品饮料", "market_cap": 2200000000000, "weight": 6.89},
            ],
            "hs300": [
                {"symbol": "000001", "name": "平安银行", "industry": "银行", "market_cap": 280000000000, "weight": 0.85},
                {"symbol": "000002", "name": "万科A", "industry": "房地产开发", "market_cap": 250000000000, "weight": 0.78},
                {"symbol": "600519", "name": "贵州茅台", "industry": "食品饮料", "market_cap": 2200000000000, "weight": 6.89},
            ],
            # STAR50 / 科创50
            "STAR50": [
                {"symbol": "688001", "name": "华兴源创", "industry": "半导体", "market_cap": 45000000000, "weight": 1.10},
                {"symbol": "688012", "name": "中微公司", "industry": "半导体设备", "market_cap": 120000000000, "weight": 2.20}
            ],
            "star50": [
                {"symbol": "688001", "name": "华兴源创", "industry": "半导体", "market_cap": 45000000000, "weight": 1.10},
                {"symbol": "688012", "name": "中微公司", "industry": "半导体设备", "market_cap": 120000000000, "weight": 2.20}
            ],
            # A500
            "A500": [
                {"symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market_cap": 2500000000000, "weight": 2.50},
                {"symbol": "000333", "name": "美的集团", "industry": "家电", "market_cap": 450000000000, "weight": 1.20 }
            ],
            "a500": [
                {"symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market_cap": 2500000000000, "weight": 2.50},
                {"symbol": "000333", "name": "美的集团", "industry": "家电", "market_cap": 450000000000, "weight": 1.20 }
            ],
            "csi500": [
                {"symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market_cap": 2500000000000, "weight": 2.50},
                {"symbol": "000333", "name": "美的集团", "industry": "家电", "market_cap": 450000000000, "weight": 1.20 }
            ]
        }
        simple_code = self._alias_simple_code(index_code)
        upper_code = index_code.upper()
        data = (fallback_map.get(index_code)
                or fallback_map.get(simple_code)
                or fallback_map.get(upper_code)
                or fallback_map.get(simple_code.upper())
                or [])
        return [IndexConstituent(**item) for item in data]

    # 兼容旧方法名称
    async def get_hs300_constituents(self, force_refresh: bool = False) -> List[HS300Constituent]:
        data = await self.get_index_constituents("399300.SZ", force_refresh=force_refresh)
        return [HS300Constituent(**c.dict()) for c in data]
        
    async def get_index_constituents_from_api(self, index_code: str) -> List[IndexConstituent]:
        """通用：从 tushare API 获取指定指数成分股 (当前保留原 HS300 逻辑，逐步泛化)

        index_code: tushare 指数代码，例如 399300.SZ
        如果传入别名（hs300/star50/a500）会转换。
        """
        try:
            import asyncio
            from datetime import datetime
            try:
                # Determine real code
                real_code = self._resolve_index_real_code(index_code)
                # Attempt to get latest trade date
                trade_date = get_latest_trade_date()
                if not trade_date:
                    from datetime import datetime as _dt
                    trade_date = _dt.now().strftime('%Y%m%d')
                df_constituents = get_index_weight(real_code, trade_date)
                if df_constituents is None or df_constituents.empty:
                    return []
                # Prepare batch maps
                basic_map = get_stock_basic_map()
                daily_map = get_daily_basic_map(trade_date)
                constituents: List[IndexConstituent] = []
                for idx, row in df_constituents.iterrows():
                    symbol = row['con_code']  # ts_code with suffix
                    base = basic_map.get(symbol, {})
                    name = base.get('name', f'股票{symbol}')
                    industry = base.get('industry', '')
                    daily = daily_map.get(symbol, {})
                    total_mv = daily.get('total_mv', 0) * 10000  # to yuan
                    constituent = IndexConstituent(
                        symbol=symbol,
                        name=name,
                        industry=industry,
                        market_cap=float(total_mv),
                        weight=float(row.get('weight', 0)),
                        update_time=datetime.now().strftime('%Y%m%d %H:%M:%S')
                    )
                    constituents.append(constituent)
                    if (idx + 1) % 100 == 0:
                        await asyncio.sleep(0)  # yield control
                return constituents
            except TushareNotConfigured:
                print("Tushare token not configured; skipping live fetch.")
                return []
        except Exception as e:
            print(f"获取指数({index_code}) 成分股数据失败: {str(e)}")
            return []
    
    # 旧名称向后兼容
    async def get_hs300_constituents_from_api(self):  # type: ignore
        return await self.get_index_constituents_from_api("399300.SZ")

    def _cache_constituents(self, constituents: List[IndexConstituent], index_code: str):
        """缓存任意指数成分股数据到数据库"""
        try:
            # 检查是否有数据库连接
            if not hasattr(self, 'db') or self.db is None:
                # 尝试导入和连接数据库
                try:
                    from pymongo import MongoClient
                    import os
                    
                    # 获取MongoDB连接配置（如果有环境变量）
                    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
                    db_name = os.getenv("DATA_DB_NAME", "quant_data")
                    
                    # 连接到MongoDB
                    client = MongoClient(mongo_uri)
                    self.db = client[db_name]
                    print(f"已连接到数据数据库: {db_name}")
                except Exception as e:
                    print(f"数据库连接失败，无法缓存数据: {str(e)}")
                    return False
            
            # 确保集合存在
            if not hasattr(self, 'index_constituents_collection'):
                self.index_constituents_collection = self.db['index_constituents']
                
                # 创建索引（如果不存在）
                try:
                    self.index_constituents_collection.create_index([
                        ("index_code", 1),
                        ("symbol", 1)
                    ])
                    self.index_constituents_collection.create_index([
                        ("update_date", 1)
                    ])
                except Exception as e:
                    print(f"创建索引失败: {str(e)}")
            
            # 当前日期
            from datetime import datetime
            today = datetime.now().strftime('%Y%m%d')
            
            # 删除当日该指数旧数据
            try:
                self.index_constituents_collection.delete_many({
                    "index_code": index_code,
                    "update_date": today
                })
                print(f"已清理今日({today})缓存的 {index_code} 成分股数据")
            except Exception as e:
                print(f"清理旧数据失败: {str(e)}")
            
            # 如果没有数据，直接返回
            if not constituents or len(constituents) == 0:
                print("无数据可缓存")
                return False
                
            # 构建批量插入的文档
            documents = []
            for constituent in constituents:
                # 如果是HS300Constituent对象，转换为字典
                if hasattr(constituent, "dict"):
                    data = constituent.dict()
                else:
                    data = constituent
                    
                # 添加额外的索引字段
                document = {
                    "index_code": index_code,
                    "update_date": today,
                    "symbol": data.get("symbol", ""),
                    "name": data.get("name", ""),
                    "industry": data.get("industry", ""),
                    "market_cap": data.get("market_cap", 0),
                    "weight": data.get("weight", 0),
                    "update_time": data.get("update_time", datetime.now().strftime('%Y%m%d %H:%M:%S'))
                }
                documents.append(document)
                
            # 批量插入数据
            if documents:
                result = self.index_constituents_collection.insert_many(documents)
                print(f"成功缓存 {len(result.inserted_ids)} 只 {index_code} 成分股")
                return True
            
            return False
        
        except Exception as e:
            print(f"缓存指数({index_code}) 成分股数据失败: {str(e)}")
            return False

    async def _get_cached_constituents(self, index_code: str):
        """从缓存获取任意指数成分股数据"""
        try:
            # 检查是否有数据库连接
            if not hasattr(self, 'db') or self.db is None:
                try:
                    from pymongo import MongoClient
                    import os
                    
                    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
                    db_name = os.getenv("DATA_DB_NAME", "quant_data")
                    
                    client = MongoClient(mongo_uri)
                    self.db = client[db_name]
                except Exception as e:
                    print(f"数据库连接失败，无法读取缓存: {str(e)}")
                    return []
            
            # 确保集合存在
            if not hasattr(self, 'index_constituents_collection'):
                self.index_constituents_collection = self.db['index_constituents']
            
            # 获取最近缓存的数据（2天内）
            from datetime import datetime, timedelta
            today = datetime.now().strftime('%Y%m%d')
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            pipeline = [
                {"$match": {
                    "index_code": index_code,
                    "update_date": {"$in": [today, yesterday]}
                }},
                {"$sort": {"update_time": -1}},
                {"$group": {
                    "_id": "$symbol",
                    "doc": {"$first": "$$ROOT"}
                }},
                {"$replaceRoot": {"newRoot": "$doc"}}
            ]
            
            cached_records = list(self.index_constituents_collection.aggregate(pipeline))
            
            if not cached_records:
                print(f"缓存中没有找到近期的 {index_code} 成分股数据")
                return []
            
            # 将缓存记录转换为HS300Constituent对象
            constituents: List[IndexConstituent] = []
            for record in cached_records:
                constituent = IndexConstituent(
                    symbol=record.get("symbol", "").rstrip('.SZ').rstrip('.SH'),
                    name=record.get("name", ""),
                    industry=record.get("industry", ""),
                    market_cap=record.get("market_cap", 0),
                    weight=record.get("weight", 0),
                    update_time=record.get("update_time", "")
                )
                constituents.append(constituent)
            
            print(f"从缓存中获取到 {len(constituents)} 条 {index_code} 记录，缓存日期: {cached_records[0].get('update_date')}")
            return constituents
            
        except Exception as e:
            print(f"从缓存获取数据失败: {str(e)}")
            return []

    # 工具：别名 -> tushare 真实代码
    def _resolve_index_real_code(self, code: str) -> str:
        alias = code.lower()
        mapping = {
            'hs300': '000300.SH',
            '399300.sz': '399300.SZ',
            'star50': '000688.SH',  # 需核实真实代码
            'a500': '000510.SH',    # 占位示例，真实A500指数代码需确认
            'csi500':'000905.SH',
            'a1000': '000852.SH',
        }
        return mapping.get(alias, code)

    def _alias_simple_code(self, code: str) -> str:
        code_lower = code.lower()
        if code_lower in ('000300.sh','399300.sz', 'hs300'): return 'hs300'
        if code_lower in ('000688.sh', 'star50'): return 'star50'
        if code_lower in ('000510.sh', 'a500'): return 'a500'
        if code_lower in ('000905.sh', 'csi500'): return 'csi500'
        return code