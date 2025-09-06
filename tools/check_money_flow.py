import os
from pymongo import MongoClient

from dotenv import load_dotenv
load_dotenv()

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
money_flow_collection = db["money_flow_test"]
count = money_flow_collection.count_documents({'trade_date': '20250728'})
print(f"Total records in money_flow_collection: {count}")
for doc in money_flow_collection.find({"symbol": '000002','trade_date': '20250728'}):
    print(doc)


# money_flow_records = list(money_flow_collection.find(
#             {"symbol": '000002'},
#             sort=[("trade_date", -1)]
#         ).limit(30))
# print(money_flow_records)
# # 查找所有 trade_date 重复的记录
# from collections import defaultdict

# # 按 trade_date 分组
# records_by_date = defaultdict(list)
# for doc in money_flow_collection.find({"symbol": '000002'}):
#     records_by_date[doc.get('trade_date')].append(doc)

# # 只删除 trade_date 相同且数量大于1的重复记录（保留一条）
# for trade_date, docs in records_by_date.items():
#     if len(docs) > 1:
#         # 保留第一条，删除其余
#         for doc in docs[1:]:
#             money_flow_collection.delete_one({"_id": doc["_id"]})
#             print(f"Deleted duplicate record with trade_date {trade_date} and _id: {doc['_id']}")