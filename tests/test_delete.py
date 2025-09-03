from bson import ObjectId
from pymongo import MongoClient
from datetime import datetime,timedelta
client = MongoClient("mongodb://localhost:27017")
db = client["finance"]


object_id = "68b8521cc09e9f647d0f5add"  # 例如 "64f8e2c2b2e4a3d1c8e4a123"
db.money_flow.delete_one({"_id": ObjectId(object_id)})


#money_flow_collection = db["money_flow"]
# today_str = datetime.now().strftime("%Y-%m-%d")
# result = login_logs_collection.delete_many({
#     "timestamp": {
#         "$gte": datetime.strptime(today_str, "%Y-%m-%d"),
#         "$lt": datetime.strptime(today_str, "%Y-%m-%d") + timedelta(days=1)
#     }
# })
# print(f"Deleted count: {result.deleted_count}")

# result = login_logs_collection.delete_many({
#     "success": {"$ne": True}
# })
# print(f"Deleted count: {result.deleted_count}")


# for log in login_logs_collection.find():
#     print(log)