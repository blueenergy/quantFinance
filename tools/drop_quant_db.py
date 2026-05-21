from pymongo import MongoClient
import os
from dotenv import load_dotenv
load_dotenv()

mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(mongo_url)

# Drop the 'quant' database
client.drop_database("quantFinance")
print("Dropped database 'quantFinance'.")
