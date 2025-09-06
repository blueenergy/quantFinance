import os
from pymongo import MongoClient
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

# MongoDB connection settings
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = "finance"

# Singleton client (reuse across app)
client = MongoClient(mongo_url)
db = client[DB_NAME]

def get_db_collections() -> Dict[str, object]:
    """
    Return a dict of commonly used collections.
    Usage:
        collections = get_db_collections()
        user_col = collections['users']
    """
    return {
        "users": db["users"],
        "stock_info": db["stock_info"],
        "volume_price": db["volume_price"],
        "analysis_history": db["analysis_history"],
        "login_logs": db["login_logs"],
        "admin_logs": db["admin_logs"],
        "market_analysis_cache": db["market_analysis_cache"],
        "user_watchlists": db["user_watchlists"],
        "money_flow": db["money_flow"]  # <--- 补充这一行
        # Add more collections as needed
    }

def get_collection(name: str):
    """Get a single collection by name."""
    return db[name]

def close_db():
    """Close the MongoDB connection (if needed)."""
    client.close()