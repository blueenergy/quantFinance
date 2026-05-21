#!/usr/bin/env python3
"""
Diagnostic script to check for stuck 'submitted' orders.

Usage:
    python tools/diagnose_orders.py
"""
import os
import sys
import logging
from pymongo import MongoClient
from datetime import datetime
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("diagnose_orders")

def check_submitted_orders():
    # MongoDB connection details (default to localhost if not set)
    mongo_url = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    mongo_db_name = os.environ.get("MONGO_DB", "quant_finance")
    
    try:
        client = MongoClient(mongo_url)
        db = client[mongo_db_name]
        signals_coll = db["trade_signals"]
        
        # Query for submitted orders
        query = {"status": "submitted"}
        submitted_orders = list(signals_coll.find(query).sort("timestamp", -1))
        
        print(f"\n=== Diagnostic Report: Submitted Orders ===")
        print(f"Time: {datetime.now()}")
        print(f"Total 'submitted' orders found: {len(submitted_orders)}")
        
        if not submitted_orders:
            print("Good news! No stuck orders found.")
            return

        print("\n--- Details ---")
        for order in submitted_orders:
            order_id = order.get("order_id")
            symbol = order.get("symbol")
            qt_order_id = order.get("qmt_order_id", "N/A")
            created_at = datetime.fromtimestamp(order.get("timestamp", 0))
            updated_at = datetime.fromtimestamp(order.get("updated_at", 0)) if order.get("updated_at") else "N/A"
            submitted_at = datetime.fromtimestamp(order.get("submitted_at", 0)) if order.get("submitted_at") else "N/A"
            
            print(f"Order ID: {order_id}")
            print(f"  Symbol: {symbol}")
            print(f"  Broker ID (QMT): {qt_order_id}")
            print(f"  Created: {created_at}")
            print(f"  Submitted: {submitted_at}")
            print(f"  Last Update: {updated_at}")
            print(f"  Gap (Now - Update): {(datetime.now() - updated_at) if isinstance(updated_at, datetime) else 'N/A'}")
            print("-" * 30)
            
        print("\n=== Troubleshooting Tips ===")
        print("1. Check quantTrader logs for 'Resumed tracking for order ...' on startup.")
        print("2. Check if QMT Order ID exists in miniQMT terminal.")
        print("   If it exists but status is stuck, check 'quantTrader' logs for 'Poll execution status'.")
        print("3. If QMT Order ID is 'N/A', the order might not have reached the broker correctly.")

    except Exception as e:
        log.error(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)

if __name__ == "__main__":
    check_submitted_orders()
