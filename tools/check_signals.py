
import pymongo
import os
import sys
from bson import ObjectId

# Connect to MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = pymongo.MongoClient(MONGO_URI)
db = client["finance"]

def list_signals():
    users_coll = db["users"]
    signals_coll = db["trade_signals"]
    executions_coll = db["trade_executions"]

    print("--- User Check ---")
    active_user = users_coll.find_one({"is_active": True})
    
    if not active_user:
        print("No active user found.")
        return

    user_id_obj = active_user["_id"]
    user_id_str = str(user_id_obj)
    username = active_user.get("username", "Unknown")
    
    print(f"Active User: {username}")
    print(f"  _id (ObjectId): {user_id_obj}")
    print(f"  _id (String)  : {user_id_str}")

    print("\n--- Signal Check (Global) ---")
    total_signals = signals_coll.count_documents({})
    print(f"Total Trade Signals in DB: {total_signals}")
    
    if total_signals > 0:
        sample = signals_coll.find_one()
        print("Sample Signal Structure:")
        print(f"  _id: {sample.get('_id')}")
        print(f"  user_id: {sample.get('user_id')} (Type: {type(sample.get('user_id'))})")
        print(f"  symbol: {sample.get('symbol')}")
    else:
        print("No signals found globally.")

    print("\n--- Signal Check (Specific User) ---")
    # Try querying with both String and ObjectId
    count_str = signals_coll.count_documents({"user_id": user_id_str})
    count_obj = signals_coll.count_documents({"user_id": user_id_obj})
    
    print(f"Signals for user_id (String)  : {count_str}")
    print(f"Signals for user_id (ObjectId): {count_obj}")
    
    if count_str > 0:
        print("Found signals using String ID. First record:")
        sig = signals_coll.find_one({"user_id": user_id_str})
        print(f"  {sig.get('symbol')} {sig.get('action')} {sig.get('size')} @ {sig.get('price')}")
    elif count_obj > 0:
        print("Found signals using ObjectId. First record:")
        sig = signals_coll.find_one({"user_id": user_id_obj})
        print(f"  {sig.get('symbol')} {sig.get('action')} {sig.get('size')} @ {sig.get('price')}")
    else:
        print("No signals found for this user with either ID format.")

    print("\n--- Execution Check (Global) ---")
    total_execs = executions_coll.count_documents({})
    print(f"Total Trade Executions in DB: {total_execs}")
    
    if total_execs > 0:
        sample = executions_coll.find_one()
        print("Sample Execution Structure:")
        print(f"  user_id: {sample.get('user_id')} (Type: {type(sample.get('user_id'))})")

if __name__ == "__main__":
    list_signals()
