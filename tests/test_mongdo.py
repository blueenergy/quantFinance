from pymongo import MongoClient
client = MongoClient('mongodb://localhost:27017/')
db = client['quant']
for doc in db['volume_price'].find().limit(100000):
    print(doc)