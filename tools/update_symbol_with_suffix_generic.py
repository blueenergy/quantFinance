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
mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "finance"
client = MongoClient(mongo_url)
db = client[DB_NAME]

def update_symbol_with_suffix_array(collection_name, symbol_field="symbols"):
    """Update symbol array field to include exchange suffix for all documents in a collection"""
    collection = db[collection_name]
    
    # Count total documents with symbol array
    total_docs_with_symbol = collection.count_documents({
        symbol_field: {"$exists": True, "$ne": None, "$type": "array"}
    })
    print(f"Found {total_docs_with_symbol} documents with {symbol_field} array field in collection '{collection_name}'")
    
    if total_docs_with_symbol == 0:
        print(f"No documents found with {symbol_field} array field in collection '{collection_name}'. Exiting.")
        return 0
    
    # Find documents where at least one symbol in the array doesn't have suffix
    docs_to_update = list(collection.find({
        symbol_field: {"$exists": True, "$ne": None, "$type": "array"},
        # At least one element in the array doesn't contain a dot
        symbol_field: {"$elemMatch": {"$not": {"$regex": "\\."}}}
    }, {symbol_field: 1, "_id": 1}))
    
    print(f"Found {len(docs_to_update)} documents with {symbol_field} array but containing symbols without exchange suffix in collection '{collection_name}'")
    
    updated_count = 0
    total_docs = len(docs_to_update)
    
    for i, doc in enumerate(docs_to_update):
        original_symbols = doc.get(symbol_field, [])
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
        
        # Only update if there were changes
        if has_changes:
            result = collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {symbol_field: updated_symbols}}
            )
            if result.modified_count > 0:
                updated_count += 1
                if updated_count <= 5:  # Only print first 5 updates
                    print(f"  Updated {symbol_field} array: {len([s for s in original_symbols if normalize_symbol(s) != s])} symbols updated")
        
        # Print progress every 100 updates or at 10%, 20%, etc.
        if (i + 1) % 100 == 0 or (i + 1) % max(1, total_docs // 10) == 0:
            progress = ((i + 1) / total_docs) * 100
            print(f"  Progress: {i + 1}/{total_docs} ({progress:.1f}%) - Updated: {updated_count}")
    
    print(f"\nUpdated {updated_count} documents in collection '{collection_name}' where {symbol_field} array had symbols missing exchange suffix")
    
    return updated_count

def update_symbol_with_suffix_generic(collection_name, symbol_field="symbol", is_array=False):
    """Update symbol field to include exchange suffix for all documents in a collection where symbol doesn't have one"""
    if is_array:
        return update_symbol_with_suffix_array(collection_name, symbol_field)
    else:
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
        total_docs = len(docs_without_suffix)
        
        for i, doc in enumerate(docs_without_suffix):
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
            
            # Print progress every 1000 updates or at 10%, 20%, etc.
            if (i + 1) % 1000 == 0 or (i + 1) % max(1, total_docs // 10) == 0:
                progress = ((i + 1) / total_docs) * 100
                print(f"  Progress: {i + 1}/{total_docs} ({progress:.1f}%) - Updated: {updated_count}")
        
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

def process_single_collection():
    print("Generic script to update symbol field to include exchange suffix in any collection...")
    print("This will standardize all symbols to include exchange suffixes (e.g., '002050.SZ', '600519.SH').")
    
    # List available collections
    collections = list_collections()
    
    collection_name = input(f"\nEnter the collection name to update (e.g., stock_info): ").strip()
    if not collection_name:
        print("No collection name provided.")
        return
    
    if collection_name not in collections:
        print(f"Collection '{collection_name}' does not exist. Available collections: {collections}")
        return
    
    symbol_field = input("Enter the symbol field name (default is 'symbol'): ").strip()
    if not symbol_field:
        symbol_field = "symbol"
    
    field_type = input("Is this field an array/list of symbols? (yes/no, default is 'no'): ").strip().lower()
    is_array = field_type in ['yes', 'y', 'true']
    
    print(f"\nUpdating {symbol_field} {'array' if is_array else 'field'} to include exchange suffix in collection '{collection_name}'...")
    
    response = input("Are you sure you want to proceed? (yes/no): ")
    if response.lower() in ['yes', 'y']:
        updated_count = update_symbol_with_suffix_generic(collection_name, symbol_field, is_array)
        if updated_count > 0:
            verify_changes(collection_name, symbol_field)
        print(f"\nUpdate completed for collection '{collection_name}'!")
    else:
        print("Update cancelled.")


def process_multiple_collections():
    collections = list_collections()
    processed_collections = []
    
    while True:
        print(f"\nCollections processed so far: {processed_collections}")
        print("\nOptions:")
        print("1. Process another collection")
        print("2. Exit")
        
        choice = input("\nEnter your choice (1-2): ").strip()
        
        if choice == "1":
            collection_name = input(f"\nEnter the collection name to update (e.g., stock_info): ").strip()
            if not collection_name:
                print("No collection name provided.")
                continue
            
            if collection_name not in collections:
                print(f"Collection '{collection_name}' does not exist. Available collections: {collections}")
                continue
            
            symbol_field = input("Enter the symbol field name (default is 'symbol'): ").strip()
            if not symbol_field:
                symbol_field = "symbol"
            
            field_type = input("Is this field an array/list of symbols? (yes/no, default is 'no'): ").strip().lower()
            is_array = field_type in ['yes', 'y', 'true']
            
            print(f"\nUpdating {symbol_field} {'array' if is_array else 'field'} to include exchange suffix in collection '{collection_name}'...")
            
            response = input("Are you sure you want to proceed? (yes/no): ")
            if response.lower() in ['yes', 'y']:
                updated_count = update_symbol_with_suffix_generic(collection_name, symbol_field, is_array)
                if updated_count > 0:
                    verify_changes(collection_name, symbol_field)
                    processed_collections.append(collection_name)
                print(f"\nUpdate completed for collection '{collection_name}'!")
            else:
                print("Update cancelled for this collection.")
        elif choice == "2":
            print(f"\nProcessing completed. Processed collections: {processed_collections}")
            break
        else:
            print("Invalid choice. Please enter 1 or 2.")

if __name__ == "__main__":
    print("Generic script to update symbol field to include exchange suffix in any collection...")
    print("This will standardize all symbols to include exchange suffixes (e.g., '002050.SZ', '600519.SH').")
    
    print("\nOptions:")
    print("1. Process single collection")
    print("2. Process multiple collections in one session")
    
    main_choice = input("\nEnter your choice (1-2): ").strip()
    
    if main_choice == "1":
        process_single_collection()
    elif main_choice == "2":
        process_multiple_collections()
    else:
        print("Invalid choice. Exiting.")
        sys.exit(1)
