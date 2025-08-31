from pymongo import MongoClient

class DatabaseManager:
    """Handles MongoDB connections and operations."""

    def __init__(self, uri="mongodb://10.181.200.185:27017/", db_name="finance"):
        self.client = MongoClient(uri)
        self.db = self.client[db_name]
        self.volume_collection = self.db['volume_price']
        self.stock_info_collection = self.db['stock_info']

    def find_latest_trade_date(self, symbol):
        """Return the latest trade_date for a symbol."""
        doc = self.volume_collection.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        )
        return doc["trade_date"] if doc else None

    def insert_records(self, records):
        """Insert multiple records into volume_price."""
        if records:
            self.volume_collection.insert_many(records)

    def update_record(self, symbol, trade_date, record):
        """Update or upsert a single record."""
        self.volume_collection.update_one(
            {"symbol": symbol, "trade_date": trade_date},
            {"$set": record},
            upsert=True
        )

    def close(self):
        self.client.close()