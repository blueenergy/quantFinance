from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)
    client.admin.command('ping')
    print("MongoDB is usable.")
except ConnectionFailure:
    print("MongoDB is not usable or not running.")




# db = client['quant']
# for doc in db['volume_price'].find().limit(100000):
#     print(doc)
