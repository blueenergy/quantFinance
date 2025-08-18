from pymongo import MongoClient
from datetime import date

mongo_url = "mongodb://localhost:27017"
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["volume_price"]

# 获取所有股票代码
symbols = collection.distinct("symbol")

latest_date = date.today()
updated = []
not_updated = []

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
    else:
        not_updated.append(symbol)

print(f"总股票数: {len(symbols)}")
print(f"已更新到最新日期({latest_date})的股票数: {len(updated)}")
print(f"未更新到最新日期的股票数: {len(not_updated)}")
if not_updated:
    print("未更新股票示例:", not_updated[:10])
