import os
from pymongo import MongoClient
from datetime import date
from dotenv import load_dotenv
load_dotenv()

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["volume_price"]
docs = list(collection.find({"symbol": '000002'}, sort=[("trade_date", -1)]).limit(5))
for doc in docs:
    print(doc)