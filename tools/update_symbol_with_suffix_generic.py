"""
Generic script to update symbol field to include exchange suffix based on stock code rules.
This will standardize all symbol fields to include exchange suffixes (e.g., '002050.SZ', '600519.SH')
to match the format used in position data and external APIs.

Supports updating symbol fields in multiple collections.
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

def update_symbol_with_suffix_generic(collection_name, symbol_field="symbol"):
    """Update symbol field to include exchange suffix for all documents in a collection where symbol doesn't have one"""
    collection = db[collection_name]
    
    # Count total documents with symbol
    total_docs_with_symbol = collection.count_documents({
        symbol_field: {"$exists": True, "$ne": None, "$ne": ""}
    })
    print(f"Found {total_docs_with_symbol} documents with {symbol_field} field in collection '{collection_name}'")
    
    if total_docs_with_symbol == 0:
        print(f"No documents found with {symbol_field} field in collection '{collection_name}'. Exiting.")
        return 0
    
    # Find documents where symbol doesn't have exchange suffix (no dot in symbol)
    docs_without_suffix = list(collection.find({
        symbol_field: {"$exists": True, "$ne": None, "$ne": ""},
        "$expr": {"$not": {"$regexMatch": {"input": f"${symbol_field}", "regex": "\\."}}}  # No dot in symbol
    }, {symbol_field: 1, "_id": 1}))
    
    print(f"Found {len(docs_without_suffix)} documents with {symbol_field} field but without exchange suffix in collection '{collection_name}'")
    
    if len(docs_without_suffix) == 0:
        print(f"All symbols in collection '{collection_name}' already have exchange suffixes. No updates needed.")
        return 0
    
    updated_count = 0
    for doc in docs_without_suffix:
        symbol = doc.get(symbol_field, "")
        
        # Normalize the symbol to add exchange suffix
        normalized_symbol = normalize_symbol(symbol)
        
        # Only update if the normalized symbol is different from the original
        if normalized_symbol != symbol:
            result = collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {symbol_field: normalized_symbol}}
            )
            if result.modified_count > 0:
                updated_count += 1
                if updated_count <= 10:  # Only print first 10 updates
                    print(f"  Updated: {symbol} -> {normalized_symbol}")
    
    print(f"\nUpdated {updated_count} documents in collection '{collection_name}' where {symbol_field} was missing exchange suffix")
    
    # Verify the update by showing some examples
    sample_docs = list(collection.find(
        {symbol_field: {"$exists": True, "$ne": None, "$ne": ""}}, 
        {symbol_field: 1}
    ).limit(10))
    
    print(f"\nSample updated documents from collection '{collection_name}':")
    for i, doc in enumerate(sample_docs[:10]):
        symbol = doc.get(symbol_field, "N/A")
        has_suffix = "✓" if '.' in symbol else "✗"
        print(f"  {has_suffix} {symbol_field}: {symbol}")
    
    # Count how many symbols still don't have suffixes
    docs_without_suffix_after = collection.count_documents({
        symbol_field: {"$exists": True, "$ne": None, "$ne": ""},
        "$expr": {"$not": {"$regexMatch": {"input": f"${symbol_field}", "regex": "\\."}}}  # No dot in symbol
    })
    
    print(f"\nDocuments with {symbol_field} still missing exchange suffix in collection '{collection_name}': {docs_without_suffix_after}")
    
    return updated_count

def verify_changes(collection_name, symbol_field="symbol"):
    """Verify that the changes were applied correctly"""
    collection = db[collection_name]
    
    # Count documents where symbol has suffix
    docs_with_suffix = collection.count_documents({
        symbol_field: {"$exists": True, "$ne": None, "$ne": ""},
        "$expr": {"$regexMatch": {"input": f"${symbol_field}", "regex": "\\."}}  # Has dot in symbol
    })
    
    total_docs_with_symbol = collection.count_documents({
        symbol_field: {"$exists": True, "$ne": None, "$ne": ""}
    })
    
    print(f"\nVerification for collection '{collection_name}':")
    print(f"Documents where {symbol_field} has exchange suffix: {docs_with_suffix}")
    print(f"Total documents with {symbol_field}: {total_docs_with_symbol}")
    
    if total_docs_with_symbol > 0:
        percentage = (docs_with_suffix / total_docs_with_symbol) * 100
        print(f"Symbols with suffix rate: {percentage:.2f}%")

def list_collections():
    """List all collections in the database"""
    collections = db.list_collection_names()
    print(f"Available collections: {collections}")
    return collections

if __name__ == "__main__":
    print("Generic script to update symbol field to include exchange suffix in any collection...")
    print("This will standardize all symbols to include exchange suffixes (e.g., '002050.SZ', '600519.SH').")
    
    # List available collections
    collections = list_collections()
    
    collection_name = input(f"\nEnter the collection name to update (e.g., stock_info): ").strip()
    if not collection_name:
        print("No collection name provided. Exiting.")
        sys.exit(1)
    
    if collection_name not in collections:
        print(f"Collection '{collection_name}' does not exist. Available collections: {collections}")
        sys.exit(1)
    
    symbol_field = input("Enter the symbol field name (default is 'symbol'): ").strip()
    if not symbol_field:
        symbol_field = "symbol"
    
    print(f"\nUpdating {symbol_field} field to include exchange suffix in collection '{collection_name}'...")
    
    response = input("Are you sure you want to proceed? (yes/no): ")
    if response.lower() in ['yes', 'y']:
        updated_count = update_symbol_with_suffix_generic(collection_name, symbol_field)
        if updated_count > 0:
            verify_changes(collection_name, symbol_field)
        print(f"\nUpdate completed for collection '{collection_name}'!")
    else:
        print("Update cancelled.")