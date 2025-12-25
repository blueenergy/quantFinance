"""Clean up duplicate trade signals.

This script finds and removes duplicate signals, keeping only the earliest one
(by _id) for each combination of (user_id, symbol, strategy, timestamp, action).

Run this before creating the unique index.
"""

import os
from pymongo import MongoClient
from dotenv import load_dotenv
from bson import ObjectId


def cleanup_duplicates(dry_run=True):
    """Remove duplicate signals, keeping the earliest one.
    
    Args:
        dry_run: If True, only show what would be deleted without actually deleting
    """
    load_dotenv()
    
    mongo_uri = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "finance")
    
    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    signals_coll = db["trade_signals"]
    
    print("=" * 70)
    print("Trade Signals Duplicate Cleanup")
    print("=" * 70)
    print("")
    
    if dry_run:
        print("🔍 DRY RUN MODE - No data will be deleted")
    else:
        print("⚠️  LIVE MODE - Duplicates will be DELETED")
    print("")
    
    # Find duplicates
    pipeline = [
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "symbol": "$symbol",
                    "strategy": "$strategy",
                    "timestamp": "$timestamp",
                    "action": "$action"
                },
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"},
                "order_ids": {"$push": "$order_id"},
                "statuses": {"$push": "$status"}
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}}
    ]
    
    print("Searching for duplicates...")
    duplicates = list(signals_coll.aggregate(pipeline))
    
    if not duplicates:
        print("✅ No duplicates found!")
        client.close()
        return 0
    
    print(f"Found {len(duplicates)} duplicate groups")
    print("")
    
    total_to_delete = 0
    deleted_count = 0
    
    for i, dup in enumerate(duplicates, 1):
        group_key = dup['_id']
        ids = dup['ids']
        order_ids = dup['order_ids']
        statuses = dup['statuses']
        count = dup['count']
        
        # Handle missing fields gracefully
        symbol = group_key.get('symbol', 'N/A')
        strategy = group_key.get('strategy', 'N/A')
        action = group_key.get('action', 'N/A')
        timestamp = group_key.get('timestamp', 'N/A')
        
        print(f"{i}. {symbol} - {strategy} - {action}")
        print(f"   Timestamp: {timestamp}")
        print(f"   Duplicates: {count}")
        
        # Sort IDs to keep the earliest (lowest ObjectId = earliest created)
        sorted_ids = sorted(ids)
        keep_id = sorted_ids[0]
        delete_ids = sorted_ids[1:]
        
        print(f"   Keep: {keep_id} (order: {order_ids[ids.index(keep_id)]})")
        print(f"   Delete: {len(delete_ids)} records")
        
        for del_id in delete_ids:
            idx = ids.index(del_id)
            print(f"     - {del_id} (order: {order_ids[idx]}, status: {statuses[idx]})")
            total_to_delete += 1
        
        print("")
        
        # Delete duplicates if not dry run
        if not dry_run and delete_ids:
            result = signals_coll.delete_many({"_id": {"$in": delete_ids}})
            deleted_count += result.deleted_count
            print(f"   ✅ Deleted {result.deleted_count} duplicate records")
            print("")
    
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Duplicate groups found: {len(duplicates)}")
    print(f"Total duplicate records: {total_to_delete}")
    
    if dry_run:
        print("")
        print("⚠️  This was a DRY RUN - no data was deleted")
        print("Run with --live flag to actually delete duplicates:")
        print("  python tools/cleanup_duplicate_signals.py --live")
    else:
        print(f"Records deleted: {deleted_count}")
        print("")
        print("✅ Cleanup complete!")
        print("")
        print("Next steps:")
        print("1. Run tools/create_trade_signals_index.py to create unique index")
        print("2. This will prevent future duplicates")
    
    client.close()
    return deleted_count


if __name__ == "__main__":
    import sys
    
    # Check for --live flag
    live_mode = "--live" in sys.argv or "-l" in sys.argv
    
    if live_mode:
        print("")
        response = input("⚠️  Are you sure you want to DELETE duplicate signals? (yes/no): ")
        if response.lower() != "yes":
            print("Aborted.")
            sys.exit(0)
        print("")
    
    cleanup_duplicates(dry_run=not live_mode)
