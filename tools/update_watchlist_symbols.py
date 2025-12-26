"""
Script to update symbol lists in user watchlists to include exchange suffixes.
This handles the special case where symbols are stored as an array in the 'symbols' field.
"""

import os
import sys
from pymongo import MongoClient
from dotenv import load_dotenv
from utils.symbol_utils import normalize_symbol

# Load environment variables
load_dotenv()

# MongoDB connection
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = "finance"
client = MongoClient(mongo_url)
db = client[DB_NAME]

def update_watchlist_symbols():
    """Update symbol lists in user watchlists to include exchange suffixes"""
    watchlist_collection = db["user_watchlists"]
    
    # Count total documents with symbols
    total_docs_with_symbols = watchlist_collection.count_documents({
        "symbols": {"$exists": True, "$ne": None}
    })
    print(f"Found {total_docs_with_symbols} documents with symbols field in user_watchlists collection")
    
    if total_docs_with_symbols == 0:
        print("No documents found with symbols field in user_watchlists collection. Exiting.")
        return
    
    # Get all documents with symbols
    watchlists = list(watchlist_collection.find({
        "symbols": {"$exists": True, "$ne": None}
    }, {"symbols": 1, "_id": 1, "user_id": 1}))
    
    print(f"Processing {len(watchlists)} watchlist documents...")
    
    updated_count = 0
    total_symbols_processed = 0
    
    for i, watchlist in enumerate(watchlists):
        original_symbols = watchlist.get("symbols", [])
        if not isinstance(original_symbols, list):
            continue
            
        # Normalize each symbol in the list
        updated_symbols = []
        has_changes = False
        
        for symbol in original_symbols:
            if not isinstance(symbol, str):
                updated_symbols.append(symbol)  # Keep non-string values as-is
                continue
                
            normalized_symbol = normalize_symbol(symbol)
            updated_symbols.append(normalized_symbol)
            
            # Check if the symbol was actually changed
            if normalized_symbol != symbol:
                has_changes = True
            
            total_symbols_processed += 1
        
        # Only update if there were changes
        if has_changes:
            result = watchlist_collection.update_one(
                {"_id": watchlist["_id"]},
                {"$set": {"symbols": updated_symbols}}
            )
            if result.modified_count > 0:
                updated_count += 1
                if updated_count <= 5:  # Only print first 5 updates
                    user_id = watchlist.get("user_id", "unknown")
                    print(f"  Updated watchlist for user {user_id}: {len([s for s in original_symbols if normalize_symbol(s) != s])} symbols updated")
        
        # Print progress every 100 updates
        if (i + 1) % 100 == 0:
            progress = ((i + 1) / len(watchlists)) * 100
            print(f"  Progress: {i + 1}/{len(watchlists)} ({progress:.1f}%) - Watchlists updated: {updated_count}")
    
    print(f"\nCompleted! Updated {updated_count} watchlist documents")
    print(f"Processed {total_symbols_processed} total symbols")
    
    # Verify the update by showing some examples
    sample_watchlists = list(watchlist_collection.find({
        "symbols": {"$exists": True, "$ne": None}
    }, {"symbols": 1, "user_id": 1}).limit(5))
    
    print("\nSample updated watchlists:")
    for watchlist in sample_watchlists:
        user_id = watchlist.get("user_id", "unknown")
        symbols = watchlist.get("symbols", [])[:5]  # Show first 5 symbols
        print(f"  User {user_id}: {symbols}")


def verify_changes():
    """Verify that the changes were applied correctly"""
    watchlist_collection = db["user_watchlists"]
    
    # Check a sample of documents to verify all symbols have suffixes
    sample_docs = list(watchlist_collection.find({
        "symbols": {"$exists": True, "$ne": None}
    }, {"symbols": 1}).limit(20))
    
    total_symbols = 0
    symbols_with_suffix = 0
    
    for doc in sample_docs:
        symbols = doc.get("symbols", [])
        for symbol in symbols:
            if isinstance(symbol, str) and '.' in symbol:
                symbols_with_suffix += 1
            total_symbols += 1
    
    print(f"\nVerification:")
    print(f"Total symbols checked: {total_symbols}")
    print(f"Symbols with suffix: {symbols_with_suffix}")
    
    if total_symbols > 0:
        percentage = (symbols_with_suffix / total_symbols) * 100
        print(f"Symbols with suffix rate: {percentage:.2f}%")


if __name__ == "__main__":
    print("Updating symbol lists in user watchlists to include exchange suffixes...")
    print("This will standardize all symbols in watchlists to include exchange suffixes (e.g., '002050.SZ', '600519.SH').")
    
    response = input("Are you sure you want to proceed? (yes/no): ")
    if response.lower() in ['yes', 'y']:
        update_watchlist_symbols()
        verify_changes()
        print("\nUpdate completed!")
    else:
        print("Update cancelled.")