from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017/")
db = client["finance"]
collection = db["analysis_history"]

for doc in collection.find({"symbol": "603022"}):
    print(doc.keys())
    print(doc["analysis_result"]["analysis"])
    print(doc["analysis_result"]["analysis"].keys())
    print(doc["model"])
