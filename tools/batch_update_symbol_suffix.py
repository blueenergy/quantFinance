import sys
import os
from datetime import datetime
import time
import math

# Add the project root to the Python path to import modules
# This assumes the script is run from within the quantFinance project
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.insert(0, project_root)

from db import get_db_collections, get_collection

def update_collection_in_batches(collection_name, batch_size=1000):
    """Update a collection in batches to avoid hanging with large datasets"""
    collections = get_db_collections()
    collection = collections.get(collection_name)
    if collection is None:
        collection = get_collection(collection_name)
    
    # Get total count
    total_count = collection.count_documents({})
    print(f"Processing {collection_name} with {total_count} documents in batches of {batch_size}")
    
    if total_count == 0:
        print(f"No documents found in {collection_name}")
        return
    
    # Calculate number of batches
    num_batches = math.ceil(total_count / batch_size)
    
    processed = 0
    for batch_num in range(num_batches):
        print(f"Processing batch {batch_num + 1}/{num_batches} for {collection_name}...")
        
        # Get batch of documents
        batch_docs = list(collection.find({}).skip(batch_num * batch_size).limit(batch_size))
        
        if not batch_docs:
            print("No more documents to process")
            break
            
        # Process each document in the batch
        updated_count = 0
        for doc in batch_docs:
            updated_fields = False
            
            # Update symbol field if exists
            if "symbol" in doc and doc["symbol"]:
                old_symbol = doc["symbol"]
                # If it doesn't have exchange suffix, add it
                if not (old_symbol.endswith('.SZ') or old_symbol.endswith('.SH') or old_symbol.endswith('.BJ')):
                    # Simple rule: if symbol starts with 0 or 3, it's SZ; if it starts with 6, it's SH; if it starts with 4 or 8, it's BJ
                    if old_symbol.startswith(('0', '3')):
                        new_symbol = f"{old_symbol}.SZ"
                    elif old_symbol.startswith('6'):
                        new_symbol = f"{old_symbol}.SH"
                    elif old_symbol.startswith(('4', '8')):
                        new_symbol = f"{old_symbol}.BJ"
                    else:
                        # Default to SH for other cases
                        new_symbol = f"{old_symbol}.SH"
                    
                    doc["symbol"] = new_symbol
                    updated_fields = True
            
            # Update stock_code field if exists
            if "stock_code" in doc and doc["stock_code"]:
                old_code = doc["stock_code"]
                if not (old_code.endswith('.SZ') or old_code.endswith('.SH') or old_code.endswith('.BJ')):
                    if old_code.startswith(('0', '3')):
                        new_code = f"{old_code}.SZ"
                    elif old_code.startswith('6'):
                        new_code = f"{old_code}.SH"
                    elif old_code.startswith(('4', '8')):
                        new_code = f"{old_code}.BJ"
                    else:
                        new_code = f"{old_code}.SH"
                    
                    doc["stock_code"] = new_code
                    updated_fields = True
            
            # Update any array fields that might contain symbols
            for key, value in doc.items():
                if isinstance(value, list):
                    updated_list = False
                    new_list = []
                    for item in value:
                        if isinstance(item, str) and (item.startswith(('00', '30', '60', '68', '43', '83')) and len(item) == 6):
                            if not (item.endswith('.SZ') or item.endswith('.SH') or item.endswith('.BJ')):
                                if item.startswith(('0', '3')):
                                    new_item = f"{item}.SZ"
                                elif item.startswith('6'):
                                    new_item = f"{item}.SH"
                                elif item.startswith(('4', '8')):
                                    new_item = f"{item}.BJ"
                                else:
                                    new_item = f"{item}.SH"
                                new_list.append(new_item)
                                updated_list = True
                            else:
                                new_list.append(item)
                        else:
                            new_list.append(item)
                    
                    if updated_list:
                        doc[key] = new_list
                        updated_fields = True
            
            # Update the document if any fields were changed
            if updated_fields:
                collection.update_one({"_id": doc["_id"]}, {"$set": {k: v for k, v in doc.items() if k != "_id"}})
                updated_count += 1
        
        processed += len(batch_docs)
        print(f"  Batch {batch_num + 1}: Processed {len(batch_docs)} documents, updated {updated_count} documents")
        print(f"  Progress: {processed}/{total_count} ({processed/total_count*100:.1f}%)")
        
        # Small delay to prevent overwhelming the database
        time.sleep(0.1)
    
    print(f"Completed processing {collection_name}. Total processed: {processed}")

def process_volume_price_collection():
    """Specifically handle the volume_price collection with optimized batch processing"""
    collections = get_db_collections()
    collection = collections.get("volume_price")
    if collection is None:
        collection = get_collection("volume_price")
    
    # Get total count
    total_count = collection.count_documents({})
    print(f"Processing volume_price collection with {total_count} documents")
    
    if total_count == 0:
        print("No documents found in volume_price collection")
        return
    
    # For volume_price, use a smaller batch size due to potentially larger documents
    batch_size = 2000
    num_batches = math.ceil(total_count / batch_size)
    
    processed = 0
    updated_count = 0
    
    for batch_num in range(num_batches):
        print(f"Processing volume_price batch {batch_num + 1}/{num_batches}...")
        
        # Get batch of documents
        batch_docs = list(collection.find({}).skip(batch_num * batch_size).limit(batch_size))
        
        if not batch_docs:
            print("No more documents to process")
            break
        
        # Process each document in the batch
        batch_updated = 0
        for doc in batch_docs:
            updated_fields = False
            
            # Update symbol field if exists
            if "symbol" in doc and doc["symbol"]:
                old_symbol = doc["symbol"]
                # If it doesn't have exchange suffix, add it
                if not (old_symbol.endswith('.SZ') or old_symbol.endswith('.SH') or old_symbol.endswith('.BJ')):
                    # Simple rule: if symbol starts with 0 or 3, it's SZ; if it starts with 6, it's SH; if it starts with 4 or 8, it's BJ
                    if old_symbol.startswith(('0', '3')):
                        new_symbol = f"{old_symbol}.SZ"
                    elif old_symbol.startswith('6'):
                        new_symbol = f"{old_symbol}.SH"
                    elif old_symbol.startswith(('4', '8')):
                        new_symbol = f"{old_symbol}.BJ"
                    else:
                        # Default to SH for other cases
                        new_symbol = f"{old_symbol}.SH"
                    
                    doc["symbol"] = new_symbol
                    updated_fields = True
            
            # Update stock_code field if exists
            if "stock_code" in doc and doc["stock_code"]:
                old_code = doc["stock_code"]
                if not (old_code.endswith('.SZ') or old_code.endswith('.SH') or old_code.endswith('.BJ')):
                    if old_code.startswith(('0', '3')):
                        new_code = f"{old_code}.SZ"
                    elif old_code.startswith('6'):
                        new_code = f"{old_code}.SH"
                    elif old_code.startswith(('4', '8')):
                        new_code = f"{old_code}.BJ"
                    else:
                        new_code = f"{old_code}.SH"
                    
                    doc["stock_code"] = new_code
                    updated_fields = True
            
            # Update the document if any fields were changed
            if updated_fields:
                collection.update_one({"_id": doc["_id"]}, {"$set": {k: v for k, v in doc.items() if k != "_id"}})
                batch_updated += 1
        
        processed += len(batch_docs)
        updated_count += batch_updated
        print(f"  Batch {batch_num + 1}: Processed {len(batch_docs)} documents, updated {batch_updated} documents")
        print(f"  Progress: {processed}/{total_count} ({processed/total_count*100:.1f}%)")
        
        # Small delay to prevent overwhelming the database
        time.sleep(0.1)
    
    print(f"Completed processing volume_price collection. Total processed: {processed}, Total updated: {updated_count}")

def batch_process_collections():
    """Allow user to process multiple collections in one session"""
    print("=== Batch Update Symbol Suffix Tool ===")
    print("This tool updates symbol fields to include exchange suffixes (.SZ, .SH, .BJ)")
    
    while True:
        print("\nSelect an option:")
        print("1. Process specific collection")
        print("2. Process volume_price collection specifically (optimized for large data)")
        print("3. Process all collections with symbol fields")
        print("4. Exit")
        
        choice = input("Enter your choice (1-4): ").strip()
        
        if choice == "1":
            collection_name = input("Enter collection name: ").strip()
            if not collection_name:
                print("Invalid collection name")
                continue
            
            try:
                collections = get_db_collections()
                all_collection_names = [name for name in collections.keys()]
                
                # Check if collection exists by listing all collections
                from db import client, DB_NAME
                db = client[DB_NAME]
                if collection_name not in db.list_collection_names():
                    print(f"Collection {collection_name} does not exist")
                    continue
                
                batch_size_input = input("Enter batch size (default 1000): ").strip()
                batch_size = int(batch_size_input) if batch_size_input else 1000
                
                update_collection_in_batches(collection_name, batch_size)
            except ValueError:
                print("Invalid batch size")
            except Exception as e:
                print(f"Error processing collection: {e}")
        
        elif choice == "2":
            process_volume_price_collection()
        
        elif choice == "3":
            # Process all collections that might have symbol fields
            from db import client, DB_NAME
            db = client[DB_NAME]
            collections = db.list_collection_names()
            
            symbol_collections = []
            for coll_name in collections:
                coll = db[coll_name]
                # Check if collection has symbol-related fields
                sample_doc = coll.find_one()
                if sample_doc:
                    has_symbol_field = any(field in sample_doc for field in ["symbol", "symbols", "stock_code", "stock_symbol", "code", "secucode", "security_code", "ticker", "tickers"])
                    if has_symbol_field:
                        symbol_collections.append(coll_name)
            
            print(f"Found {len(symbol_collections)} collections with symbol fields: {symbol_collections}")
            
            for coll_name in symbol_collections:
                print(f"\nProcessing {coll_name}...")
                update_collection_in_batches(coll_name)
        
        elif choice == "4":
            print("Exiting...")
            break
        
        else:
            print("Invalid choice")

if __name__ == "__main__":
    batch_process_collections()