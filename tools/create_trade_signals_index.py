"""Create unique index for trade_signals collection to prevent duplicates.

This script creates a compound unique index on trade_signals collection
to prevent the same strategy from generating duplicate signals for the
same symbol at the same timestamp.

Index: (user_id, symbol, strategy, timestamp, action) - UNIQUE

This ensures:
- Same user
- Same stock
- Same strategy  
- Same timestamp (minute-level precision)
- Same action (buy/sell)

Will only allow ONE signal per combination.
"""

import os
from pymongo import MongoClient, ASCENDING
from dotenv import load_dotenv


def create_trade_signals_index():
    """Create unique index on trade_signals collection."""
    load_dotenv()
    
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "finance")
    
    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    signals_coll = db["trade_signals"]
    
    print("Creating unique index on trade_signals...")
    print("Index keys: (user_id, order_id)")
    print("")
    
    # Check existing indexes
    existing_indexes = list(signals_coll.list_indexes())
    print(f"Existing indexes: {len(existing_indexes)}")
    for idx in existing_indexes:
        print(f"  - {idx['name']}: {idx.get('key', {})}")
    print("")
    
    index_name = "unique_live_order_id_per_user"
    
    try:
        signals_coll.create_index(
            [
                ("user_id", ASCENDING),
                ("order_id", ASCENDING),
            ],
            name=index_name,
            unique=True,
            background=True  # Build in background to avoid blocking
        )
        print(f"✅ Created unique index: {index_name}")
        print("")
        print("This will prevent duplicate signals from:")
        print("  - Same approved portfolio plan being published more than once")
        print("  - quantTrader retry/restart seeing duplicate live orders")
        print("  - Strategy logic bugs reusing the same executable order_id")
        print("")
        print("⚠️  IMPORTANT:")
        print("  - Existing duplicates will NOT be removed")
        print("  - Future inserts will fail if duplicate detected")
        print("  - Check logs for 'DuplicateKeyError' if signals are missing")
        
    except Exception as e:
        print(f"❌ Failed to create index: {e}")
        print("")
        if "duplicate key error" in str(e).lower():
            print("This means duplicates already exist in the collection!")
            print("You need to clean up duplicates first before creating unique index.")
            print("")
            print("Run this query to find duplicates:")
            print("""
db.trade_signals.aggregate([
  {
    $group: {
      _id: {
        user_id: "$user_id",
        order_id: "$order_id"
      },
      count: { $sum: 1 },
      ids: { $push: "$_id" }
    }
  },
  { $match: { count: { $gt: 1 } } },
  { $sort: { count: -1 } }
])
            """)
    
    client.close()


def find_duplicates():
    """Find existing duplicate signals."""
    load_dotenv()
    
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "finance")
    
    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    signals_coll = db["trade_signals"]
    
    print("Searching for duplicate signals...")
    print("")
    
    pipeline = [
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "order_id": "$order_id"
                },
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"},
                "order_ids": {"$push": "$order_id"}
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20}  # Show top 20 duplicates
    ]
    
    duplicates = list(signals_coll.aggregate(pipeline))
    
    if not duplicates:
        print("✅ No duplicates found! Safe to create unique index.")
    else:
        print(f"❌ Found {len(duplicates)} duplicate groups:")
        print("")
        for i, dup in enumerate(duplicates, 1):
            print(f"{i}. {dup['_id']}")
            print(f"   Count: {dup['count']}")
            print(f"   IDs: {dup['ids']}")
            print(f"   Order IDs: {dup['order_ids']}")
            print("")
        
        print("You need to remove duplicates before creating unique index!")
        print("Suggested approach:")
        print("1. Keep the earliest signal (lowest _id)")
        print("2. Delete the rest")
    
    client.close()
    return len(duplicates) == 0


if __name__ == "__main__":
    print("=" * 60)
    print("Trade Signals Duplicate Prevention Tool")
    print("=" * 60)
    print("")
    
    # First check for duplicates
    no_duplicates = find_duplicates()
    print("")
    
    if no_duplicates:
        # Safe to create index
        create_trade_signals_index()
    else:
        print("⚠️  Cannot create unique index while duplicates exist.")
        print("Please clean up duplicates first, then run this script again.")
