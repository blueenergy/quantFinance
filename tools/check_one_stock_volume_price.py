import os
from pymongo import MongoClient
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["volume_price"]

def check_trade_date_formats():
    """检查 trade_date 的不同格式"""
    print("=== 检查 trade_date 格式 ===")
    
    # 检查不同类型的数据
    datetime_count = collection.count_documents({"trade_date": {"$type": "date"}})
    string_count = collection.count_documents({"trade_date": {"$type": "string"}})
    
    print(f"DateTime 格式: {datetime_count} 条")
    print(f"String 格式: {string_count} 条")
    
    # 显示样例
    if datetime_count > 0:
        print("\nDateTime 格式样例:")
        sample = collection.find_one({"trade_date": {"$type": "date"}})
        if sample:
            print(f"  {sample['symbol']}: {sample['trade_date']}")
    
    if string_count > 0:
        print("\nString 格式样例:")
        sample = collection.find_one({"trade_date": {"$type": "string"}})
        if sample:
            print(f"  {sample['symbol']}: {sample['trade_date']}")

def check_specific_stock(symbol='000002'):
    """检查特定股票的数据"""
    print(f"\n=== 检查股票 {symbol} 的最新数据 ===")
    
    # 获取最新的5条记录
    docs = list(collection.find(
        {"symbol": symbol}, 
        sort=[("trade_date", -1)]
    ).limit(5))
    
    for doc in docs:
        trade_date = doc.get('trade_date', 'N/A')
        close_price = doc.get('close', 'N/A')
        volume = doc.get('volume', 'N/A')
        
        print(f"日期: {trade_date} | 收盘价: {close_price} | 成交量: {volume}")

def query_date_range_example(symbol='000002'):
    """演示如何查询日期范围（新格式）"""
    print(f"\n=== 查询 {symbol} 最近30天数据 ===")
    
    # 计算30天前的日期，转换为字符串格式
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    
    # 查询最近30天的数据
    recent_docs = list(collection.find({
        "symbol": symbol,
        "trade_date": {"$gte": thirty_days_ago}
    }).sort("trade_date", -1).limit(10))
    
    print(f"找到 {len(recent_docs)} 条最近30天的记录:")
    for doc in recent_docs:
        print(f"  {doc['trade_date']}: 收盘价 {doc.get('close', 'N/A')}")

if __name__ == "__main__":
    # 检查数据格式
    check_trade_date_formats()
    
    # 检查万科数据
    check_specific_stock('000002')
    
    # 演示日期范围查询
    query_date_range_example('000002')