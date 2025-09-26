# app/services/index_service.py
from typing import List
from models.index_models import HS300Constituent
class IndexService:
    """指数数据服务 - 处理业务逻辑"""
    
    def __init__(self):
        # self.db = get_database()
        # self.pro = ts.pro_api()  # tushare API
        pass
    
    async def get_hs300_constituents(self, force_refresh: bool = False) -> List[HS300Constituent]:
        """获取沪深300成分股 - 优先使用缓存

        Args:
            force_refresh: 强制刷新数据，不使用缓存
            
        Returns:
            List[HS300Constituent]: 沪深300成分股列表
        """
        # 步骤1: 如果不强制刷新，先尝试从缓存获取
        if not force_refresh:
            cached_data = await self._get_cached_constituents()
            if cached_data and len(cached_data) > 0:
                print(f"从缓存获取到 {len(cached_data)} 只沪深300成分股")
                return cached_data
        # 步骤2: 缓存未命中或强制刷新，尝试从API获取
        try:
            api_data = await self.get_hs300_constituents_from_api()
            if api_data and len(api_data) > 0:
                print(f"成功从API获取到 {len(api_data)} 只沪深300成分股")
                success = self._cache_constituents(api_data)
                if success:
                    print("成分股数据已成功缓存到数据库")
                return api_data
        except Exception as e:
            print(f"从API获取数据失败，使用本地数据作为备选: {str(e)}")
    
        # 如果API获取失败或返回空数据，使用本地静态数据
        print("使用本地静态数据...")
        hs300_data = [
            {"symbol": "000001", "name": "平安银行", "industry": "银行", "market_cap": 280000000000, "weight": 0.85},
            {"symbol": "000002", "name": "万科A", "industry": "房地产开发", "market_cap": 250000000000, "weight": 0.78},
            {"symbol": "600519", "name": "贵州茅台", "industry": "食品饮料", "market_cap": 2200000000000, "weight": 6.89},
            # 你可以添加更多静态数据在这里
        ]  
        return [HS300Constituent(**item) for item in hs300_data]
        
    async def get_hs300_constituents_from_api(self):
        """从 tushare API 获取沪深300成分股数据"""
        try:
            import tushare as ts
            import pandas as pd
            import os
            import asyncio
            from datetime import datetime
            
            # 检查 tushare token 是否可用
            ts_token = os.getenv("TUSHARE_TOKEN")
            print(ts_token)
            if not ts_token:
                print("警告: 未找到 TUSHARE_TOKEN 环境变量")
                return []
                
            # 初始化 tushare
            ts.set_token(ts_token)
            pro = ts.pro_api()
            
            print("正在从 tushare 获取沪深300成分股数据...")
            
            # 获取当前日期作为交易日期（格式：YYYYMMDD）
            today = datetime.now().strftime('%Y%m%d')
            
            # 获取沪深300成分股
            df_constituents = pro.index_weight(index_code='399300.SZ', trade_date=today)
            
            # 如果当天数据不可用，尝试获取最近的数据
            if df_constituents.empty:
                # 获取交易日历
                df_cal = pro.trade_cal(exchange='SSE', start_date='20200101', end_date=today)
                # 过滤出最近的交易日
                df_cal = df_cal[df_cal['is_open'] == 1].sort_values('cal_date', ascending=False)
                if not df_cal.empty:
                    latest_trade_date = df_cal.iloc[0]['cal_date']
                    latest_trade_date = '20250630'  # 临时硬编码，替换为最新的交易日
                    print(f"当前日期无数据，使用最近交易日: {latest_trade_date}")
                    df_constituents = pro.index_weight(index_code='399300.SZ', trade_date=latest_trade_date)
            
            # 如果仍然没有数据，返回空列表
            if df_constituents.empty:
                print("无法获取沪深300成分股数据")
                return []
                
            print(f"获取到 {len(df_constituents)} 条成分股权重记录")
            
            # 准备结果列表
            constituents = []
            
            # 对于每个成分股，获取额外信息（行业、市值等）
            for idx, row in df_constituents.iterrows():
                # 股票代码转换（去掉后缀.SH或.SZ）
                symbol = row['con_code']
                
                # 从 tushare 获取股票基本信息
                try:
                    stock_info = pro.stock_basic(ts_code=symbol)
                    
                    # 延迟以避免 API 调用过快
                    # await asyncio.sleep(0.1)
                    
                    if not stock_info.empty:
                        name = stock_info.iloc[0]['name']
                        industry = stock_info.iloc[0].get('industry', '')
                    else:
                        name = f"股票{symbol}"
                        industry = ""
                        
                    # 获取最新的日行情以获取市值
                    daily_info = pro.daily_basic(ts_code=symbol, trade_date=today)
                    
                    # 延迟以避免 API 调用过快
                    # await asyncio.sleep(0.1)
                    
                    if not daily_info.empty:
                        market_cap = daily_info.iloc[0].get('total_mv', 0) * 10000  # 单位转换为元
                    else:
                        market_cap = 0
                    
                    # 构建成分股对象
                    constituent = HS300Constituent(
                        symbol=symbol,
                        name=name,
                        industry=industry,
                        market_cap=float(market_cap),
                        weight=float(row['weight']),
                        update_time=datetime.now().strftime('%Y%m%d %H:%M:%S')
                    )
                    
                    constituents.append(constituent)
                    
                    # 每处理5个成分股打印一次进度
                    if (idx + 1) % 5 == 0:
                        print(f"已处理 {idx + 1}/{len(df_constituents)} 只成分股")
                    
                except Exception as e:
                    print(f"处理股票 {symbol} 时出错: {str(e)}")
                    continue
            
            print(f"成功获取 {len(constituents)} 只沪深300成分股信息")
            return constituents
            
        except Exception as e:
            print(f"获取沪深300成分股数据失败: {str(e)}")
            # 返回空列表作为 fallback
            return []
        
    def _cache_constituents(self, constituents):
        """缓存沪深300成分股数据到数据库
    
        Args:
            constituents (List[HS300Constituent]): 沪深300成分股列表
    
        Returns:
            bool: 缓存操作是否成功
        """
        try:
            # 检查是否有数据库连接
            if not hasattr(self, 'db') or self.db is None:
                # 尝试导入和连接数据库
                try:
                    from pymongo import MongoClient
                    import os
                    
                    # 获取MongoDB连接配置（如果有环境变量）
                    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
                    db_name = os.getenv("MONGO_DB", "finance")
                    
                    # 连接到MongoDB
                    client = MongoClient(mongo_uri)
                    self.db = client[db_name]
                    print(f"已连接到数据库: {db_name}")
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
            
            # 首先删除旧数据（保证数据一致性）
            try:
                self.index_constituents_collection.delete_many({
                    "index_code": "399300.SZ",
                    "update_date": today
                })
                print(f"已清理今日({today})缓存的沪深300成分股数据")
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
                    "index_code": "399300.SZ",
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
                print(f"成功缓存 {len(result.inserted_ids)} 只沪深300成分股")
                return True
            
            return False
        
        except Exception as e:
            print(f"缓存沪深300成分股数据失败: {str(e)}")
            return False

    async def _get_cached_constituents(self):
        """从缓存获取沪深300成分股数据
        
        Returns:
            List[HS300Constituent]: 缓存的沪深300成分股列表，如果没有则返回空列表
        """
        try:
            # 检查是否有数据库连接
            if not hasattr(self, 'db') or self.db is None:
                try:
                    from pymongo import MongoClient
                    import os
                    
                    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
                    db_name = os.getenv("MONGO_DB", "finance")
                    
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
                    "index_code": "399300.SZ",
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
                print("缓存中没有找到近期的沪深300成分股数据")
                return []
            
            # 将缓存记录转换为HS300Constituent对象
            constituents = []
            for record in cached_records:
                constituent = HS300Constituent(
                    symbol=record.get("symbol", "").rstrip('.SZ').rstrip('.SH'),
                    name=record.get("name", ""),
                    industry=record.get("industry", ""),
                    market_cap=record.get("market_cap", 0),
                    weight=record.get("weight", 0),
                    update_time=record.get("update_time", "")
                )
                constituents.append(constituent)
            
            print(f"从缓存中获取到 {len(constituents)} 条记录，缓存日期: {cached_records[0].get('update_date')}")
            return constituents
            
        except Exception as e:
            print(f"从缓存获取数据失败: {str(e)}")
            return [HS300Constituent(**item) for item in hs300_data]