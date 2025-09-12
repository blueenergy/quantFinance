from pymongo import MongoClient
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class DatabaseManager:
    def __init__(self):
        mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_url)
        self.db = self.client['finance']
        self.stock_scores = self.db['stock_scores']
        self.stock_info = self.db['stock_info']
        self.volume_price = self.db['volume_price']
        self.money_flow = self.db['money_flow_records']
        self.financial_data = self.db['financial_data']

    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        return self.stock_info.find_one({"symbol": symbol})

    def get_stock_scores(self, symbol, score_date=None):
        """获取股票评分记录"""
        query = {"symbol": symbol}
        if score_date:
            query["score_date"] = score_date
        return list(self.stock_scores.find(query))

    def save_stock_score(self, score_data):
        """保存单个股票评分"""
        self.stock_scores.update_one(
            {"symbol": score_data['symbol'], "score_date": score_data['score_date']},
            {"$set": score_data},
            upsert=True
        )

    def save_scores_batch(self, score_results, force_overwrite=False, batch_mode=True):
        """
        ✅ 批量保存评分数据
        """
        stats = {
            "total_processed": 0,
            "successful": 0,
            "failed": 0,
            "inserted": 0,
            "updated": 0,
            "overwritten": 0
        }
        
        valid_scores = {
            symbol: data for symbol, data in score_results.items() 
            if data and 'total_score' in data
        }
        
        if not valid_scores:
            return stats
        
        stats["total_processed"] = len(valid_scores)
        
        if force_overwrite:
            # 先删除已有记录
            symbols = list(valid_scores.keys())
            base_date = list(valid_scores.values())[0]['score_date']
            
            delete_result = self.stock_scores.delete_many({
                "symbol": {"$in": symbols},
                "score_date": base_date
            })
            stats["overwritten"] = delete_result.deleted_count
        
        # 保存数据
        for symbol, score_data in valid_scores.items():
            try:
                if force_overwrite:
                    # 直接插入
                    self.stock_scores.insert_one(score_data)
                    stats["successful"] += 1
                else:
                    # 使用 upsert
                    result = self.stock_scores.update_one(
                        {"symbol": symbol, "score_date": score_data['score_date']},
                        {"$set": score_data},
                        upsert=True
                    )
                    stats["successful"] += 1
                    if result.upserted_id:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
            except Exception as e:
                print(f"❌ 保存 {symbol} 失败: {e}")
                stats["failed"] += 1
        
        return stats

    def get_volume_price_data(self, symbol, start_date, end_date):
        """获取价量数据"""
        return list(self.volume_price.find({
            "symbol": symbol,
            "trade_date": {
                "$gte": start_date,
                "$lte": end_date
            }
        }).sort("trade_date", 1))

    def get_money_flow_data(self, symbol, start_date, end_date):
        """获取资金流数据"""
        return list(self.money_flow.find({
            "symbol": symbol,
            "trade_date": {
                "$gte": start_date,
                "$lte": end_date
            }
        }).sort("trade_date", 1))

    def create_indexes(self):
        """创建必要的数据库索引"""
        try:
            # 股票评分索引
            self.stock_scores.create_index([("symbol", 1), ("score_date", -1)], background=True)
            
            # 价量数据索引
            self.volume_price.create_index([("symbol", 1), ("trade_date", -1)], background=True)
            
            # 资金流数据索引
            self.money_flow.create_index([("symbol", 1), ("trade_date", -1)], background=True)
            
            # 财务数据索引
            self.financial_data.create_index([("symbol", 1), ("report_date", -1)], background=True)
            
            print("✅ 数据库索引创建完成")
        except Exception as e:
            print(f"❌ 创建索引失败: {e}")