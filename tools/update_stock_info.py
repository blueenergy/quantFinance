import os, sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import tushare as ts
from pymongo import MongoClient
from dotenv import load_dotenv
import pandas as pd
from datetime import datetime
import time

load_dotenv()

# 初始化 tushare
ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token
pro = ts.pro_api()

# 连接 MongoDB
mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["stock_info"]


def _stock_info_match_filter(ts_code: str) -> dict:
    """Match doc by ts_code, full symbol, or legacy 6-digit symbol only."""
    ts_code = str(ts_code).strip()
    short = ts_code.split(".", 1)[0] if "." in ts_code else ts_code
    return {"$or": [{"ts_code": ts_code}, {"symbol": ts_code}, {"symbol": short}]}


def update_stock_basic_info():
    """更新股票基础信息到数据库"""
    try:
        # 获取当前交易日的所有股票基础数据
        print("正在获取股票基础数据...")
        df = pro.bak_basic(
            trade_date='20250909',
            fields='trade_date,ts_code,name,industry,pe,pb,total_share,float_share,eps,bvps,' \
                   'list_date,undp,per_undp,rev_yoy,profit_yoy,gpr,npr,holder_num'
        )
        
        if df.empty:
            print("未获取到数据,可能是非交易日或API限制")
            return
        
        print(f"获取到 {len(df)} 条股票数据")
        
        # 转换数据格式
        updated_count = 0
        new_count = 0
        
        for _, row in df.iterrows():
            try:
                ts_code = row["ts_code"]

                # 构建更新数据（symbol 与 ts_code 均为 Tushare 全码）
                stock_data = {
                    "symbol": ts_code,
                    "ts_code": ts_code,
                    'name': row.get('name', ''),
                    'industry': row.get('industry', ''),
                    'pe': float(row['pe']) if pd.notna(row['pe']) else None,
                    'pb': float(row['pb']) if pd.notna(row['pb']) else None,
                    'total_share': float(row['total_share']) if pd.notna(row['total_share']) else None,  # 总股本(万股)
                    'float_share': float(row['float_share']) if pd.notna(row['float_share']) else None,  # 流通股本(万股)
                    'eps': float(row['eps']) if pd.notna(row['eps']) else None,         # 每股收益
                    'bvps': float(row['bvps']) if pd.notna(row['bvps']) else None,     # 每股净资产
                    'list_date': row['list_date'],                                     # 上市日期
                    'undp': float(row['undp']) if pd.notna(row['undp']) else None,     # 未分利润(万元)
                    'per_undp': float(row['per_undp']) if pd.notna(row['per_undp']) else None,  # 每股未分配利润
                    'rev_yoy': float(row['rev_yoy']) if pd.notna(row['rev_yoy']) else None,     # 营业收入同比(%)
                    'profit_yoy': float(row['profit_yoy']) if pd.notna(row['profit_yoy']) else None,  # 利润同比(%)
                    'gpr': float(row['gpr']) if pd.notna(row['gpr']) else None,         # 毛利率
                    'npr': float(row['npr']) if pd.notna(row['npr']) else None,         # 净利率
                    'holder_num': float(row['holder_num']) if pd.notna(row['holder_num']) else None,  # 股东人数
                    'valuation_date': row['trade_date'],
                    'last_update': datetime.now()
                }
                
                # 使用 upsert 更新或插入数据（兼容历史仅 6 位 symbol 的文档）
                result = collection.replace_one(
                    _stock_info_match_filter(ts_code),
                    stock_data,
                    upsert=True,
                )

                if result.upserted_id:
                    new_count += 1
                    print(f"新增股票: {ts_code} - {row.get('name', '')}")
                else:
                    updated_count += 1
                    if updated_count % 100 == 0:
                        print(f"已更新 {updated_count} 条记录...")
                
            except Exception as e:
                print(f"处理股票 {row.get('ts_code', 'Unknown')} 数据时出错: {e}")
                continue
        
        print(f"\n数据更新完成:")
        print(f"- 新增股票: {new_count} 只")
        print(f"- 更新股票: {updated_count} 只")
        print(f"- 总计处理: {new_count + updated_count} 只")
        
    except Exception as e:
        print(f"获取股票基础数据失败: {e}")

def update_existing_stocks_pe_pb():
    """为现有股票更新 PE/PB 等估值指标"""
    try:
        # 获取全部股票的估值数据
        df = pro.bak_basic(
            trade_date="20250909",
            fields="trade_date,ts_code,name,industry,pe,pb,total_share,float_share,eps,bvps,"
            "list_date,undp,per_undp,rev_yoy,profit_yoy,gpr,npr,holder_num",
        )

        if df.empty:
            print("未获取到估值数据")
            return

        print(f"开始为 API 返回的 {len(df)} 只股票更新估值数据...")

        updated_count = 0
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", "")).strip()
            if not ts_code:
                continue
            payload = {
                "pe": float(row["pe"]) if pd.notna(row["pe"]) else None,
                "pb": float(row["pb"]) if pd.notna(row["pb"]) else None,
                "industry": row["industry"] if pd.notna(row["industry"]) else None,
                "total_share": float(row["total_share"]) if pd.notna(row["total_share"]) else None,  # 总股本(万股)
                "float_share": float(row["float_share"]) if pd.notna(row["float_share"]) else None,  # 流通股本(万股)
                "eps": float(row["eps"]) if pd.notna(row["eps"]) else None,  # 每股收益
                "bvps": float(row["bvps"]) if pd.notna(row["bvps"]) else None,  # 每股净资产
                "list_date": row["list_date"],  # 上市日期
                "undp": float(row["undp"]) if pd.notna(row["undp"]) else None,  # 未分利润(万元)
                "per_undp": float(row["per_undp"]) if pd.notna(row["per_undp"]) else None,  # 每股未分配利润
                "rev_yoy": float(row["rev_yoy"]) if pd.notna(row["rev_yoy"]) else None,  # 营业收入同比(%)
                "profit_yoy": float(row["profit_yoy"]) if pd.notna(row["profit_yoy"]) else None,  # 利润同比(%)
                "gpr": float(row["gpr"]) if pd.notna(row["gpr"]) else None,  # 毛利率
                "npr": float(row["npr"]) if pd.notna(row["npr"]) else None,  # 净利率
                "holder_num": float(row["holder_num"]) if pd.notna(row["holder_num"]) else None,  # 股东人数
                "valuation_date": row["trade_date"],
                "symbol": ts_code,
                "ts_code": ts_code,
            }
            try:
                result = collection.update_one(
                    _stock_info_match_filter(ts_code),
                    {"$set": payload},
                    upsert=False,
                )
                if result.modified_count > 0:
                    updated_count += 1
                    if updated_count % 100 == 0:
                        print(f"已更新 {updated_count} 只股票的估值数据...")
            except Exception as e:
                print(f"更新股票 {ts_code} 估值数据失败: {e}")
            time.sleep(0.1)

        print(f"估值数据更新完成，共更新 {updated_count} 只股票")
        
    except Exception as e:
        print(f"获取估值数据失败: {e}")

def update_specific_fields_only():
    """只更新特定字段（如PE、PB等）而不覆盖其他数据"""
    try:
        # 获取估值数据
        df = pro.bak_basic(
            trade_date='20250909',
            fields='trade_date,ts_code,pe,pb,total_mv,circ_mv'
        )
        
        updated_count = 0
        
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", "")).strip()
            if not ts_code:
                continue

            # 只更新特定字段，不影响其他已有数据
            update_fields: dict = {}

            if pd.notna(row["pe"]):
                update_fields["pe"] = float(row["pe"])
            if pd.notna(row["pb"]):
                update_fields["pb"] = float(row["pb"])
            if pd.notna(row["total_mv"]):
                update_fields["market_cap"] = float(row["total_mv"]) / 10000  # 转换为亿元
            if pd.notna(row["circ_mv"]):
                update_fields["circ_market_cap"] = float(row["circ_mv"]) / 10000

            update_fields["valuation_update_date"] = row["trade_date"]

            if update_fields:
                result = collection.update_one(
                    _stock_info_match_filter(ts_code),
                    {"$set": update_fields},
                    upsert=False,  # 不创建新记录，只更新已有记录
                )
                
                if result.modified_count > 0:
                    updated_count += 1
        
        print(f"更新了 {updated_count} 只股票的估值数据")
        
    except Exception as e:
        print(f"更新特定字段失败: {e}")

def show_database_stats():
    """显示数据库统计信息"""
    total_stocks = collection.count_documents({})
    stocks_with_pe = collection.count_documents({"pe": {"$ne": None}})
    stocks_with_industry = collection.count_documents({"industry": {"$ne": ""}})
    
    print(f"\n=== 数据库统计 ===")
    print(f"总股票数量: {total_stocks}")
    print(f"有PE数据的股票: {stocks_with_pe}")
    print(f"有行业信息的股票: {stocks_with_industry}")
    
    # 显示几个示例股票
    print(f"\n=== 示例股票数据 ===")
    for stock in collection.find().limit(1):
        print(stock)
        print(f"股票: {stock['symbol']} - {stock.get('name', '')} | "
              f"行业: {stock.get('industry', '')} | "
              f"PE: {stock.get('pe', 'N/A')} | "
              f"市值: {stock.get('total_mv', 'N/A')}万元")

def performance_test():
    """实际性能测试"""
    import time
    
    print("\n=== 性能测试对比 ===")
    
    # 删除所有索引（除了_id）
    collection.drop_indexes()
    
    # 测试无索引性能
    start_time = time.time()
    for i in range(100):
        collection.find_one({"symbol": f"00000{i % 10}"})
    no_index_time = time.time() - start_time
    print(f"无索引100次查询耗时: {no_index_time:.3f}秒")
    
    # 创建索引
    collection.create_index("symbol")
    
    # 测试有索引性能
    start_time = time.time()
    for i in range(100):
        collection.find_one({"symbol": f"00000{i % 10}"})
    with_index_time = time.time() - start_time
    print(f"有索引100次查询耗时: {with_index_time:.3f}秒")
    
    speedup = no_index_time / with_index_time
    print(f"性能提升: {speedup:.1f}倍")

def create_indexes():
    """为股票信息集合创建索引"""
    try:
        #clean existing indexes
        collection.drop_indexes()
        # 为常用查询字段创建索引
        collection.create_index("symbol", unique=True)
        collection.create_index("ts_code")
        collection.create_index("industry")
        collection.create_index("pe")

        
        print("索引创建完成")
        
        # 显示所有索引
        indexes = collection.list_indexes()
        print("当前索引:")
        for index in indexes:
            print(f"- {index}")
            
    except Exception as e:
        print(f"创建索引失败: {e}")



if __name__ == "__main__":
    print("开始更新股票基础信息...")
    
    # 方法1: 获取全部股票基础数据并写入数据库
    #update_stock_basic_info()
    
    # 方法2: 为现有股票更新估值数据（如果你已有股票列表）
    #update_existing_stocks_pe_pb()
    
    # 显示统计信息
    show_database_stats()
    
    # 性能测试
    #performance_test()

    # 创建索引
    # create_indexes()
    
    print("数据更新完成！")