from pymongo import MongoClient
import os
import pprint
from dotenv import load_dotenv
load_dotenv()


mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client[os.getenv("MONGO_DB", "finance")]
collection = db["analysis_history"]

def show_analysis_history(limit=10):
    docs = list(collection.find().sort("timestamp", -1).limit(limit))
    for doc in docs:
        if doc['symbol'] =='002050':
            pprint.pprint(doc)
            print("-"*60)

if __name__ == "__main__":
    show_analysis_history()
