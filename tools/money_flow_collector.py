import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import tushare as ts
from pymongo import MongoClient
from datetime import date
from utils import get_ts_code
from dotenv import load_dotenv
load_dotenv()


ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token
pro = ts.pro_api()

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
stock_info_collection = db["stock_info"]
money_flow_collection = db["money_flow"]


df = pro.moneyflow(ts_code= '000002.SZ', start_date='20250101', end_date='20250103')
records = df.to_dict(orient='records')
for record in records:
    money_flow_collection.insert_one({"symbol": '000002', **record})

# for doc in money_flow_collection.find():
#     print(doc)
for doc in stock_info_collection.find():
    ts_code = get_ts_code(doc['symbol'])
    df = pro.moneyflow(ts_code= ts_code, start_date='20250101', end_date='20250903')
    records = df.to_dict(orient='records')
    for record in records:
        money_flow_collection.insert_one({"symbol": doc['symbol'], **record})


#获取单日全部股票数据
#df = pro.moneyflow(trade_date='20190315')

#获取单个股票数据
#df = pro.moneyflow(ts_code='002149.SZ', start_date='20190115', end_date='20190315')
for doc in money_flow_collection.find({"symbol": '000002'}):
    print(doc)