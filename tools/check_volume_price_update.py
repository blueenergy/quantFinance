from pymongo import MongoClient
from datetime import date,timedelta

mongo_url = "mongodb://10.181.200.185:27017"
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["volume_price"]

# 获取所有股票代码
symbols = collection.distinct("symbol")

latest_date = date.today()
# 如果今天是周日，则回退两天；如果是周六，则回退一天
if latest_date.weekday() == 6:  # Sunday
    latest_date = latest_date - timedelta(days=2)
elif latest_date.weekday() == 5:  # Saturday
    latest_date = latest_date - timedelta(days=1)

print(f"最新交易日期: {latest_date}")

updated = []
not_updated = []

# 删除 symbol 以 .SZ 结尾的记录
# delete_result = collection.delete_many({"symbol": {"$regex": r"\.SZ$"}})
# print(f"已删除以 .SZ 结尾的记录数: {delete_result.deleted_count}")

for symbol in symbols:
    doc = collection.find_one({"symbol": symbol}, sort=[("trade_date", -1)])
    if doc and "trade_date" in doc:
        
        doc_date = doc["trade_date"]
        if hasattr(doc_date, 'date'):
            doc_date = doc_date.date()
        if doc_date == latest_date:
            updated.append(symbol)
        else:
            not_updated.append(symbol)
            print(f"{symbol} 最后交易日: {doc['trade_date']}")
    else:
        not_updated.append(symbol)
        print(f"{symbol} 最后交易日: {doc['trade_date']}")

print(f"总股票数: {len(symbols)}")
print(f"已更新到最新日期({latest_date})的股票数: {len(updated)}")
print(f"未更新到最新日期的股票数: {len(not_updated)}")
if not_updated:
    print("未更新股票示例:", not_updated[:10])
