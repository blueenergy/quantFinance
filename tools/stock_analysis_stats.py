from pymongo import MongoClient
import os
from collections import Counter
from dotenv import load_dotenv
load_dotenv()


mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client[os.getenv("MONGO_DB", "finance")]
collection = db["analysis_history"]
stock_info = client[os.getenv("DATA_DB_NAME", "quant_data")]["stock_info"]
stock_name_cache = {}

def stock_analysis_stats():
    # 统计每个股票被分析的次数
    cursor = collection.find({}, {"symbol": 1})
    symbols = [doc["symbol"] for doc in cursor if "symbol" in doc]
    counter = Counter(symbols)
    print("股票分析次数统计（按次数降序）:")
    print("股票代码 | 股票名称      | 分析次数")
    print("-----------------------------------")
    for symbol, count in counter.most_common():
        name = get_stock_name(symbol)
        print(f"{symbol:8} | {name:12} | {count}")
def get_stock_name(symbol):
    info = stock_info.find_one({"symbol": symbol})
    return info["name"] if info and "name" in info else "-"

if __name__ == "__main__":
    stock_analysis_stats()
