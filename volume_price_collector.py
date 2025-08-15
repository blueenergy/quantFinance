import pandas as pd
import akshare as ak
from pymongo import MongoClient
from datetime import datetime, date
import time
from tqdm import tqdm
import efinance as ef

class VolumePriceCollector:
    def __init__(self):
        # MongoDB连接配置
        self.client = MongoClient('mongodb://localhost:27017/')
        self.db = self.client['quant']
        self.collection = self.db['volume_price']
        # 创建复合索引（提升查询效率）
        self.collection.create_index([
            ("symbol", 1), 
            ("trade_date", 1)
        ])
        print("MongoDB连接成功!数据库索引已创建")

    def close(self):
        self.client.close()
        print("MongoDB连接已安全关闭")

    def check_local_data(self, symbol):
        """检查本地是否已有该股票的数据"""
        try:
            # 检查是否存在该股票的任何记录
            count = self.collection.count_documents({"symbol": symbol})
            if count > 0:
                # 获取最新记录日期
                latest_record = self.collection.find_one(
                    {"symbol": symbol}, 
                    sort=[("trade_date", -1)]
                )
                latest_date = latest_record['trade_date'] if latest_record else None
                return count, latest_date
            return 0, None
        except Exception as e:
            print(f"⚠️ 检查 {symbol} 本地数据失败: {str(e)}")
            return 0, None

    def get_clean_data(self, symbol, force_update=False):
        """使用efinance获取清洗后的量价数据"""
        try:
            # 检查本地数据
            if not force_update:
                local_count, latest_date = self.check_local_data(symbol)
                if local_count > 0:
                    print(f"📚 {symbol} 已有 {local_count} 条本地数据，最新日期: {latest_date.strftime('%Y-%m-%d') if latest_date else '未知'}，跳过API请求")
                    return pd.DataFrame()  # 返回空DataFrame，表示跳过
            
            # 如果没有本地数据或强制更新，则请求API
            print(f"🌐 正在从API获取 {symbol} 数据...")
            # efinance股票历史行情（默认前复权）
            df = ef.stock.get_quote_history(symbol)
            # 字段筛选与重命名
            df = df[['日期', '开盘', '最高', '最低', '收盘', '成交量']].copy()
            df.columns = ['trade_date', 'open', 'high', 'low', 'close', 'volume']
            # 转换日期格式
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            # 添加元数据
            df['symbol'] = symbol
            df['update_time'] = datetime.now()
            # 类型转换
            df['open'] = df['open'].astype(float)
            df['volume'] = df['volume'].astype(int)
            return df
        except Exception as e:
            print(f"⚠️ 获取 {symbol} 数据失败: {str(e)}")
            return pd.DataFrame()

    def batch_update(self, force_update=False, update_existing=False):
        """
        批量更新全市场数据（efinance实现）
        
        Args:
            force_update: 是否强制更新所有股票（忽略本地数据）
            update_existing: 是否只更新已有股票的最新数据
        """
        try:
            # 获取全A股代码列表（efinance）
            stock_df = ak.stock_info_a_code_name()
            total_stocks = len(stock_df)
            print(f"获取到 {total_stocks} 只A股代码")
            
            if update_existing:
                # 只更新已有股票
                existing_symbols = self.collection.distinct("symbol")
                stock_codes = [code for code in stock_df['code'] if code in existing_symbols]
                print(f"📊 更新模式：仅更新已有的 {len(stock_codes)} 只股票")
            else:
                stock_codes = stock_df['code']
                mode_desc = "强制更新所有股票" if force_update else "智能跳过已有数据"
                print(f"📊 更新模式：{mode_desc}")

            success_count = 0
            skipped_count = 0
            error_count = 0
            
            for symbol in tqdm(stock_codes, desc="📊 数据采集进度"):
                try:
                    clean_df = self.get_clean_data(symbol, force_update=force_update)
                    if not clean_df.empty:
                        records = clean_df.to_dict('records')
                        for record in records:
                            if isinstance(record['trade_date'], date):
                                record['trade_date'] = datetime.combine(
                                    record['trade_date'], datetime.min.time())
                            self.collection.update_one(
                                {
                                    "symbol": record['symbol'],
                                    "trade_date": record['trade_date']
                                },
                                {"$set": record},
                                upsert=True
                            )
                        success_count += 1
                    else:
                        # 空DataFrame表示跳过（本地已有数据）
                        skipped_count += 1
                    time.sleep(0.2)
                except Exception as e:
                    print(f"⚠️ 处理 {symbol} 时出错: {str(e)}")
                    error_count += 1
                    continue
                    
            print(f"\n✅ 更新完成！")
            print(f"   成功处理: {success_count} 只股票")
            print(f"   跳过处理: {skipped_count} 只股票")
            print(f"   处理失败: {error_count} 只股票")
            return success_count
        except Exception as e:
            print(f"❌ 批量更新失败: {str(e)}")
            return 0

if __name__ == "__main__":
    import sys
    
    # 解析命令行参数
    force_update = "--force" in sys.argv
    update_existing = "--update-existing" in sys.argv
    
    print("=" * 60)
    print("📊 股票数据收集器")
    print("=" * 60)
    
    if force_update:
        print("🔄 强制更新模式：将重新获取所有股票数据")
    elif update_existing:
        print("📈 增量更新模式：仅更新已有股票的最新数据")
    else:
        print("🧠 智能模式：跳过已有数据的股票，只处理新股票")
    
    print("\n可用参数:")
    print("  --force          强制更新所有股票数据")
    print("  --update-existing 仅更新已有股票数据")
    print("  (无参数)          智能跳过已有数据")
    print("-" * 60)
    
    collector = VolumePriceCollector()
    try:
        collector.batch_update(force_update=force_update, update_existing=update_existing)
    finally:
        collector.close()