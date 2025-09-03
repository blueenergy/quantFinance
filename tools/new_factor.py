from pymongo import MongoClient, UpdateMany,UpdateOne
import pandas as pd
import numpy as np
from tqdm import tqdm
import time
import os
from dotenv import load_dotenv
load_dotenv()
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
class FactorCalculator:
    def __init__(self, db_name='quant', collection_name='volume_price'):
        self.client = MongoClient(mongo_url)
        self.db = self.client[db_name]
        self.collection = self.db[collection_name]

        # 创建索引（提升查询效率）
        self.collection.create_index([
            ("symbol", 1), 
            ("trade_date", 1),
            ("MA5", 1),
            ("MA20", 1)
        ])
        print(f"已连接MongoDB: {db_name}.{collection_name}")

    def close_connection(self):
        self.client.close()
        print("MongoDB连接已关闭")

    def calculate_factors(self):
        """计算因子并批量写入MongoDB"""
        print("开始计算技术指标因子...")
        start_time = time.time()
        
        # 获取所有股票代码
        stock_codes = self.collection.distinct("symbol")
        processed = 0
        failed = 0
        
        for symbol in tqdm(stock_codes, desc="因子计算进度"):
            try:
                # 获取单只股票全部历史数据
                cursor = self.collection.find({"symbol": symbol}).sort("trade_date", 1)
                data = list(cursor)
                df = pd.DataFrame(data)
                
                # 跳过数据不足的股票
                if len(df) < 21:  # 至少需要20个交易日计算MA20
                    continue
                    
                # 计算技术指标因子
                df = self._compute_price_factors(df)
                df = self._compute_volume_factors(df)
                
                # 准备批量更新操作
                updates = []
                for _, row in df.iterrows():
                    update = UpdateOne(
                        {"_id": row['_id']},
                        {"$set": {
                            "MA5": row.get('MA5'),
                            "MA20": row.get('MA20'),
                            "Price_Change_3D": row.get('Price_Change_3D'),
                            "Price_Volatility_20D": row.get('Price_Volatility_20D'),
                            "Volume_Spike": row.get('Volume_Spike')
                        }}
                    )
                    updates.append(update)
                
                # 批量写入数据库
                if updates:
                    self.collection.bulk_write(updates, ordered=False)
                    processed += 1
            except Exception as e:
                print(f"❌ {symbol}因子计算失败: {str(e)}")
                failed += 1
                continue
        
        elapsed = time.time() - start_time
        print(f"\n✅ 因子计算完成! 成功:{processed} 失败:{failed} 耗时:{elapsed:.2f}秒")

    def _compute_price_factors(self, df):
        """计算价格类因子"""
        close = pd.to_numeric(df['close'])
        
        # 移动平均线
        df['MA5'] = close.rolling(5).mean()  # 5日均线
        df['MA20'] = close.rolling(20).mean().shift(1)  # 昨日20日均线（避免未来函数）
        
        # 动量因子
        df['Price_Change_3D'] = close.pct_change(periods=3)  # 3日涨跌幅
        df['Price_Volatility_20D'] = close.rolling(20).std()  # 20日波动率
        
        return df

    def _compute_volume_factors(self, df):
        """计算成交量类因子"""
        volume = pd.to_numeric(df['volume'])
        close = pd.to_numeric(df['close'])
        
        # 成交量异动
        df['Volume_MA5'] = volume.rolling(5).mean()
        df['Volume_Spike'] = (volume / df['Volume_MA5'] - 1)  # 量比
        
        # 量价相关性因子
        df['Price_Up'] = np.where(df['close'] > df['open'], 1, 0)
        df['Price_Volume_Corr'] = df['Price_Up'].rolling(10).corr(volume)
        
        return df

    def get_factor_data(self, symbol, start_date, end_date):
        """获取带因子的股票数据"""
        start_date = pd.to_datetime(start_date)
        end_date = pd.to_datetime(end_date)
        
        query = {
            "symbol": symbol,
            "trade_date": {"$gte": start_date, "$lte": end_date}
        }
        projection = {
            "_id": 0,
            "symbol": 1,
            "trade_date": 1,
            "open": 1,
            "close": 1,
            "MA5": 1,
            "MA20": 1,
            "Price_Change_3D": 1,
            "Volume_Spike": 1
        }
        
        cursor = self.collection.find(query, projection).sort("trade_date", 1)
        return pd.DataFrame(list(cursor))

if __name__ == "__main__":
    # 初始化因子计算器
    factor_calc = FactorCalculator()

    # 计算并写入因子（首次运行使用）
    factor_calc.calculate_factors()

    # 示例：获取贵州茅台2025年因子数据
    df_600519 = factor_calc.get_factor_data("000001", "20250101", "20250609")
    print(f"因子数据:\n{df_600519.head()}")

    # 关闭连接
    factor_calc.close_connection()