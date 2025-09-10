from pymongo import MongoClient
import os
from dotenv import load_dotenv
load_dotenv()

# 获取MongoDB连接信息
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)

def list_db_collections():
    db_names = client.list_database_names()
    for db_name in db_names:
        db = client[db_name]
        collections = db.list_collection_names()
        print(f"Database: {db_name}")
        for coll in collections:
            print(f"  Collection: {coll}")

if __name__ == "__main__":
    list_db_collections()
