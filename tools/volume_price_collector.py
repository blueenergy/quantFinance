import os,sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import pandas as pd
import akshare as ak
from pymongo import MongoClient
from datetime import datetime, date, timedelta
import time
from tqdm import tqdm
import tushare as ts
from utils import get_ts_code
from dotenv import load_dotenv
load_dotenv()



mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")

# 初始化 tushare
ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token
pro = ts.pro_api()

class VolumePriceCollector:
    def __init__(self):
        # MongoDB连接配置
        self.client = MongoClient(mongo_url)
        self.db = self.client['finance']
        self.collection = self.db['volume_price']
        self.stock_info_collection = self.db['stock_info']  # 新增：股票基本信息集合
        
        # 创建复合索引（提升查询效率）
        self.collection.create_index([
            ("symbol", 1), 
            ("trade_date", 1)
        ])
        
        # 为股票信息集合创建索引
        self.stock_info_collection.create_index([("symbol", 1)], unique=True)
        
        print("MongoDB连接成功!数据库索引已创建")
        
        # 股票名称缓存
        self._stock_name_cache = {}

    def get_stock_name(self, symbol):
        """获取股票名称（tushare实现）"""
        try:
            if symbol in self._stock_name_cache:
                return self._stock_name_cache[symbol]
            stock_info = self.stock_info_collection.find_one({"symbol": symbol})
            if stock_info and 'name' in stock_info:
                name = stock_info['name']
                self._stock_name_cache[symbol] = name
                return name
            print(f"🔍 获取股票 {symbol} 的名称信息...")
            # tushare 股票列表接口
            df = pro.stock_basic(ts_code=symbol, fields='ts_code,name')
            if not df.empty:
                name = df.iloc[0]['name']
                self.stock_info_collection.update_one(
                    {"symbol": symbol},
                    {"$set": {"symbol": symbol, "name": name, "update_time": datetime.now()}},
                    upsert=True
                )
                self._stock_name_cache[symbol] = name
                print(f"✅ 获取股票名称: {symbol} -> {name}")
                return name
            else:
                print(f"⚠️ 无法获取股票 {symbol} 的名称")
                return symbol
        except Exception as e:
            print(f"⚠️ 获取股票 {symbol} 名称失败: {str(e)}")
            return symbol

    def batch_update_stock_names(self, symbols=None):
        """批量更新股票名称"""
        try:
            if symbols is None:
                # 获取所有已有股票代码
                symbols = self.collection.distinct("symbol")
            
            print(f"🔄 开始批量更新 {len(symbols)} 只股票的名称信息...")
            
            updated_count = 0
            for symbol in tqdm(symbols, desc="更新股票名称"):
                try:
                    name = self.get_stock_name(symbol)
                    if name != symbol:  # 成功获取到名称
                        updated_count += 1
                    time.sleep(0.2)  # 避免请求过快
                except Exception as e:
                    print(f"⚠️ 更新 {symbol} 名称失败: {str(e)}")
                    continue
            
            print(f"✅ 股票名称更新完成！成功更新 {updated_count} 只股票")
            return updated_count
            
        except Exception as e:
            print(f"❌ 批量更新股票名称失败: {str(e)}")
            return 0

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

    def get_incremental_data(self, symbol, days_back=1):
        """使用tushare获取增量数据"""
        try:
            local_count, latest_date = self.check_local_data(symbol)
            today = date.today()
            if latest_date:
                start_date = (latest_date + timedelta(days=1)).date()
                if start_date > today:
                    #print(f"📚 {symbol} 数据已是最新，无需同步")
                    return pd.DataFrame()
            else:
                start_date = today - timedelta(days=days_back)
            print(f"🔄 {symbol} 增量同步: 从 {start_date} 到 {today}")
            # tushare 需要日期格式为 YYYYMMDD
            start_str = start_date.strftime('%Y%m%d')
            end_str = today.strftime('%Y%m%d')
            try:
                ts_code = get_ts_code(symbol)
                df = pro.daily(ts_code=ts_code, start_date=start_str, end_date=end_str)
            except Exception as api_error:
                print(f"   ⚠️ {ts_code} API获取失败: {str(api_error)}")
                return pd.DataFrame()
            if df is None or len(df) == 0:
                print(f"   ⚠️ {ts_code} 无法获取数据")
                return pd.DataFrame()
            # 字段筛选与重命名
            required_columns = ['trade_date', 'open', 'high', 'low', 'close','pre_close','change','pct_chg', 'vol','amount']
            df_result = df[required_columns].copy()
            df_result.rename(columns={'vol': 'volume'}, inplace=True)
            # 添加元数据
            df_result['symbol'] = symbol
            df_result['update_time'] = datetime.now()
            # 获取并添加股票名称
            stock_name = self.get_stock_name(symbol)
            df_result['stock_name'] = stock_name
            # 类型转换
            df_result['open'] = df_result['open'].astype(float)
            df_result['high'] = df_result['high'].astype(float)
            df_result['low'] = df_result['low'].astype(float)
            df_result['close'] = df_result['close'].astype(float)
            df_result['pre_close'] = df_result['pre_close'].astype(float)
            df_result['change'] = df_result['change'].astype(float)
            df_result['pct_chg'] = df_result['pct_chg'].astype(float)
            df_result['volume'] = df_result['volume'].astype(int)
            df_result['amount'] = df_result['amount'].astype(float)
            print(f"   ✅ 获取到 {len(df_result)} 条新数据")
            return df_result
        except Exception as e:
            traceback.print_exc()
            print(f"⚠️ {symbol} 增量同步失败: {str(e)}")
            return pd.DataFrame()

    def get_clean_data(self, symbol, force_update=False):
        """使用tushare获取清洗后的量价数据"""
        try:
            if not force_update:
                local_count, latest_date = self.check_local_data(symbol)
                if local_count > 0:
                    print(f"📚 {symbol} 已有 {local_count} 条本地数据，最新日期: {latest_date.strftime('%Y-%m-%d') if latest_date else '未知'}，跳过API请求")
                    return pd.DataFrame()
            print(f"🌐 正在从API获取 {symbol} 数据...")
            today = date.today()
            start_str = (today - timedelta(days=365*5)).strftime('%Y%m%d')  # 默认取近5年
            end_str = today.strftime('%Y%m%d')
            df = pro.daily(ts_code=symbol, start_date=start_str, end_date=end_str)
            if df is None or len(df) == 0:
                print(f"⚠️ {symbol} 无法获取数据")
                return pd.DataFrame()
            required_columns = ['trade_date', 'open', 'high', 'low', 'close','pre_close','change','pct_chg', 'vol','amount']
            df = df[required_columns].copy()
            df.rename(columns={'vol': 'volume'}, inplace=True)
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df['symbol'] = symbol
            df['update_time'] = datetime.now()
            stock_name = self.get_stock_name(symbol)
            df['stock_name'] = stock_name
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df['pre_close'] = df['pre_close'].astype(float)
            df['change'] = df['change'].astype(float)
            df['pct_chg'] = df['pct_chg'].astype(float)
            df['volume'] = df['volume'].astype(int)
            df['amount'] = df['amount'].astype(float)
            return df
        except Exception as e:
            print(f"⚠️ 获取 {symbol} 数据失败: {str(e)}")
            return pd.DataFrame()

    def batch_update(self, force_update=False, update_existing=False, incremental_days=None):
        """
        批量更新全市场数据（efinance实现）
        
        Args:
            force_update: 是否强制更新所有股票（忽略本地数据）
            update_existing: 是否只更新已有股票的最新数据
            incremental_days: 增量同步天数，如果指定则只同步最近N天的缺失数据
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
                if incremental_days:
                    mode_desc = f"增量同步最近 {incremental_days} 天数据"
                elif force_update:
                    mode_desc = "强制更新所有股票"
                else:
                    mode_desc = "智能跳过已有数据"
                print(f"📊 更新模式：{mode_desc}")

            success_count = 0
            skipped_count = 0
            error_count = 0
            
            for symbol in tqdm(stock_codes, desc="📊 数据采集进度"):
                try:
                    if incremental_days:
                        # 使用增量同步方法
                        clean_df = self.get_incremental_data(symbol, incremental_days)
                    else:
                        # 使用原有方法
                        clean_df = self.get_clean_data(symbol, force_update=force_update)
                    
                    if not clean_df.empty:
                        records = clean_df.to_dict('records')
                        for record in records:
                            date_str = record['trade_date']
                            date_obj = datetime.strptime(date_str, '%Y%m%d').date()
                            if isinstance(date_obj, date):
                                record['trade_date'] = datetime.combine(
                                    date_obj, datetime.min.time())
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

    def get_last_trade_date(self, symbol=None):
        """获取数据库中最后一天的交易日期
        
        Args:
            symbol: 股票代码，如果指定则获取该股票的最后日期，否则获取全库最新日期
        """
        try:
            query = {"symbol": symbol} if symbol else {}
            latest_record = self.collection.find(query).sort("trade_date", -1).limit(1)
            latest_record = list(latest_record)
            
            if latest_record:
                last_date = latest_record[0]['trade_date']
                if isinstance(last_date, datetime):
                    return last_date.date()
                return last_date
            return None
        except Exception as e:
            print(f"❌ 获取最后交易日期失败: {str(e)}")
            return None

    def get_stock_missing_days(self, symbol):
        """计算指定股票从最后一天到今天需要同步的天数"""
        last_date = self.get_last_trade_date(symbol)
        if last_date is None:
            # 该股票没有数据，返回较大值进行全量同步
            return 90
        
        today = date.today()
        # 如果今天是周日，则回退两天；如果是周六，则回退一天
        if today.weekday() == 6:  # Sunday
            today = today - timedelta(days=2)
        elif today.weekday() == 5:  # Saturday
            today = today - timedelta(days=1)
        delta = (today - last_date).days
        return max(1, delta)  # 至少同步1天

    def calculate_missing_days(self):
        """计算从数据库最后一天到今天的天数差值（全库统计）"""
        last_date = self.get_last_trade_date()
        if last_date is None:
            print("📊 数据库中没有数据，建议进行全量同步")
            return 30  # 默认同步30天
        
        today = date.today()
        delta = (today - last_date).days
        
        print(f"📅 数据库最后日期: {last_date}")
        print(f"📅 当前日期: {today}")
        print(f"📊 计算得出需要同步: {delta} 天")
        
        return max(1, delta)  # 至少同步1天

    def incremental_sync_all(self, days=None, per_stock_calculation=True):
        """增量同步所有已有股票的最新数据
        
        Args:
            days: 统一同步天数，如果为None则自动计算
            per_stock_calculation: 是否为每只股票单独计算需要同步的天数
        """
        try:
            # 获取所有已有股票
            existing_symbols = self.collection.distinct("symbol")
            total_count = len(existing_symbols)
            
            if total_count == 0:
                print("📊 数据库中没有股票数据，请先进行全量同步")
                return 0
            
            if per_stock_calculation and days is None:
                print(f"🔄 开始智能增量同步 {total_count} 只股票（为每只股票单独计算缺失天数）")
            elif days is None:
                days = self.calculate_missing_days()
                print(f"🔄 开始增量同步 {total_count} 只股票的最近 {days} 天数据")
            else:
                print(f"🔄 开始增量同步 {total_count} 只股票的最近 {days} 天数据")
            
            success_count = 0
            skipped_count = 0
            error_count = 0
            
            # 统计每只股票的同步情况
            sync_stats = {}
            
            for symbol in tqdm(existing_symbols, desc="📊 增量同步进度"):
                try:
                    # 为每只股票单独计算需要同步的天数
                    if per_stock_calculation and days is None:
                        stock_days = self.get_stock_missing_days(symbol)
                        last_date = self.get_last_trade_date(symbol)
                        sync_stats[symbol] = {
                            'last_date': last_date,
                            'days_to_sync': stock_days
                        }
                    else:
                        stock_days = days if days is not None else 1
                    
                    clean_df = self.get_incremental_data(symbol, stock_days)
                    
                    if not clean_df.empty:
                        records = clean_df.to_dict('records')
                        for record in records:
                            date_str = record['trade_date']
                            date_obj = datetime.strptime(date_str, '%Y%m%d').date()
                            if isinstance(date_obj, date):
                                record['trade_date'] = datetime.combine(
                                    date_obj, datetime.min.time())
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
                        skipped_count += 1
                    
                    time.sleep(0.1) # 增量同步可以稍快一些
                except Exception as e:
                    print(f"⚠️ 增量同步 {symbol} 失败: {str(e)}")
                    error_count += 1
                    continue
            
            print(f"\n✅ 增量同步完成！")
            print(f"   成功更新: {success_count} 只股票")
            print(f"   无需更新: {skipped_count} 只股票")
            print(f"   同步失败: {error_count} 只股票")
            
            # 如果是按股票单独计算，显示详细统计
            if per_stock_calculation and days is None and sync_stats:
                print(f"\n📊 各股票同步情况统计:")
                
                # 按需要同步的天数分组
                days_groups = {}
                for symbol, info in sync_stats.items():
                    days_needed = info['days_to_sync']
                    if days_needed not in days_groups:
                        days_groups[days_needed] = []
                    days_groups[days_needed].append(symbol)
                
                for days_needed in sorted(days_groups.keys()):
                    symbols = days_groups[days_needed]
                    print(f"   需要同步 {days_needed:2d} 天: {len(symbols):3d} 只股票")
                    
                    # 显示部分示例
                    if len(symbols) <= 5:
                        print(f"      ({', '.join(symbols)})")
                    else:
                        print(f"      (如: {', '.join(symbols[:3])}, 等{len(symbols)}只)")
                
                # 显示极端情况
                max_days = max(sync_stats.values(), key=lambda x: x['days_to_sync'])
                min_days = min(sync_stats.values(), key=lambda x: x['days_to_sync'])
                
                print(f"\n📈 同步范围:")
                print(f"   最新数据: {[s for s, info in sync_stats.items() if info['days_to_sync'] == min_days['days_to_sync']][0]} (需要{min_days['days_to_sync']}天)")
                print(f"   最旧数据: {[s for s, info in sync_stats.items() if info['days_to_sync'] == max_days['days_to_sync']][0]} (需要{max_days['days_to_sync']}天)")
            
            return success_count
            
        except Exception as e:
            print(f"❌ 增量同步失败: {str(e)}")
            return 0

    def get_sync_dates(self,latest_date):
        today = date.today()
        if today.weekday() == 6:
            today -= timedelta(days=2)
        elif today.weekday() == 5:
            today -= timedelta(days=1)
        if latest_date == today or (isinstance(latest_date, datetime) and latest_date.date() == today):
            return None, None  # 已是最新
        start_date = (latest_date + timedelta(days=1)).date() if isinstance(latest_date, datetime) else latest_date + timedelta(days=1)
        return start_date, today

if __name__ == "__main__":
    import sys
    
    # 解析命令行参数
    force_update = "--force" in sys.argv
    update_existing = "--update-existing" in sys.argv
    incremental_sync = "--incremental" in sys.argv
    update_names = "--update-names" in sys.argv
    
    # 解析增量同步天数
    incremental_days = None  # 默认None，自动计算
    for arg in sys.argv:
        if arg.startswith("--days="):
            try:
                incremental_days = int(arg.split("=")[1])
                print(f"📊 指定同步天数: {incremental_days}")
            except ValueError:
                print("⚠️ --days 参数格式错误，使用自动计算天数")
                incremental_days = None
    
    print("=" * 60)
    print("📊 股票数据收集器")
    print("=" * 60)
    
    if update_names:
        print("📝 股票名称更新模式：批量更新所有股票的名称信息")
    elif incremental_sync:
        if incremental_days is None:
            print("🔄 智能增量同步模式：为每只股票单独计算需要同步的天数")
        else:
            print(f"🔄 统一增量同步模式：同步已有股票最近 {incremental_days} 天的缺失数据")
    elif force_update:
        print("🔄 强制更新模式：将重新获取所有股票数据")
    elif update_existing:
        print("📈 存量更新模式：仅更新已有股票的最新数据")
    else:
        print("🧠 智能模式：跳过已有数据的股票，只处理新股票")
    
    print("\n可用参数:")
    print("  --incremental         智能增量同步（为每只股票单独计算需要同步的天数）")
    print("  --days=N             统一增量同步天数（配合--incremental使用）")
    print("  --update-names       批量更新所有股票的名称信息")
    print("  --force              强制更新所有股票数据")
    print("  --update-existing    仅更新已有股票数据")
    print("  (无参数)              智能跳过已有数据")
    print("\n使用示例:")
    print("  python volume_price_collector.py --incremental          # 智能计算每只股票的同步天数")
    print("  python volume_price_collector.py --incremental --days=3 # 统一同步3天")
    print("  python volume_price_collector.py --update-names         # 批量更新股票名称")
    print("  python volume_price_collector.py --force                # 强制全量更新")
    print("\n智能增量同步说明:")
    print("  • 自动检测每只股票的最后更新日期")
    print("  • 根据距离今天的天数差异，为每只股票计算最优同步天数")
    print("  • 避免重复同步已经是最新的股票数据")
    print("  • 确保落后较多的股票能够追上最新数据")
    print("  • 同步过程中自动获取和保存股票名称信息")
    print("-" * 60)
    
    collector = VolumePriceCollector()
    try:
        if update_names:
            collector.batch_update_stock_names()
        elif incremental_sync:
            collector.incremental_sync_all(incremental_days)
        else:
            collector.batch_update(
                force_update=force_update, 
                update_existing=update_existing,
                incremental_days=incremental_days if "--days=" in " ".join(sys.argv) else None
            )
    finally:
        collector.close()