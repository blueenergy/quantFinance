from pymongo import MongoClient
import os

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)

source_db = client["quantFinance"]
target_db = client["finance"]
collection_name = "market_analysis_cache"

# Copy documents
docs = list(source_db[collection_name].find())
if docs:
    target_db[collection_name].insert_many(docs)
    print(f"Copied {len(docs)} documents to finance.{collection_name}")

#Optional: Drop the original collection
source_db[collection_name].drop()
print(f"Dropped quantFinance.{collection_name}")