from pymongo import MongoClient
import os

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)

# Drop the 'quant' database
client.drop_database("quantFinance")
print("Dropped database 'quantFinance'.")
