from bson import ObjectId
from pymongo import MongoClient
from datetime import datetime,timedelta
client = MongoClient("mongodb://localhost:27017")
db = client["finance"]


# object_id = "68b6e1a804acf670168f330a"  # 例如 "64f8e2c2b2e4a3d1c8e4a123"
# db.analysis_history.delete_one({"_id": ObjectId(object_id)})


login_logs_collection = db["login_logs"]
# today_str = datetime.now().strftime("%Y-%m-%d")
# result = login_logs_collection.delete_many({
#     "timestamp": {
#         "$gte": datetime.strptime(today_str, "%Y-%m-%d"),
#         "$lt": datetime.strptime(today_str, "%Y-%m-%d") + timedelta(days=1)
#     }
# })
# print(f"Deleted count: {result.deleted_count}")

result = login_logs_collection.delete_many({
    "success": {"$ne": True}
})
print(f"Deleted count: {result.deleted_count}")


for log in login_logs_collection.find():
    print(log)