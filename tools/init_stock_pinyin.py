
import os
import sys
import argparse
from pypinyin import pinyin, Style

# Add project root to path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from db import get_db_collections

def get_pinyin_initials(text):
    """获取中文拼音首字母，例如 '万科A' -> 'WKA'"""
    if not text:
        return ""
    initials = pinyin(text, style=Style.FIRST_LETTER)
    result = "".join([i[0] for i in initials])
    return result.upper()

def init_stock_pinyin():
    print("Connecting to database...")
    collections = get_db_collections()
    stock_col = collections["stock_info"]
    
    # 查找所有股票
    cursor = stock_col.find({})
    total = stock_col.count_documents({})
    print(f"Found {total} stocks.")
    
    count = 0
    updated = 0
    
    for stock in cursor:
        count += 1
        name = stock.get("name", "")
        symbol = stock.get("symbol", "")
        
        if count % 100 == 0:
            print(f"Processing {count}/{total}...")
            
        if not name:
            continue
            
        py = get_pinyin_initials(name)
        
        # 即使已经存在也可以更新，以防逻辑变更
        stock_col.update_one(
            {"_id": stock["_id"]},
            {"$set": {"pinyin": py}}
        )
        updated += 1
        
    print(f"Done. Processed {count} stocks, updated {updated} pinyin fields.")

if __name__ == "__main__":
    init_stock_pinyin()
