#!/usr/bin/env python3
"""
检查 quant_data 数据库的索引情况
- 列出所有集合及其索引
- 对比推荐索引
- 生成创建缺失索引的脚本
"""

import os
import sys
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from pymongo import MongoClient, ASCENDING, DESCENDING

# MongoDB 配置
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATA_DB_NAME = os.getenv("DATA_DB_NAME", "quant_data")

# 推荐的索引定义
RECOMMENDED_INDEXES = {
    "volume_price": [
        [("symbol", ASCENDING), ("trade_date", DESCENDING)],
        [("symbol", ASCENDING)],
        [("trade_date", DESCENDING)],
    ],
    "money_flow": [
        [("symbol", ASCENDING), ("trade_date", DESCENDING)],
        [("trade_date", DESCENDING)],
    ],
    "minute_bars": [
        [("symbol", ASCENDING), ("trade_date", DESCENDING)],
        [("symbol", ASCENDING), ("datetime", DESCENDING)],
    ],
    "hot_stock_raw": [
        [("symbol", ASCENDING)],
        [("trade_date", DESCENDING)],
        [("rank_time", DESCENDING)],
    ],
    "daily_limit_up": [
        [("trade_date", DESCENDING)],
        [("symbol", ASCENDING)],
        [("trade_date", DESCENDING), ("symbol", ASCENDING)],
    ],
    "daily_limit_down": [
        [("trade_date", DESCENDING)],
        [("symbol", ASCENDING)],
    ],
    "limit_up_ladder": [
        [("trade_date", DESCENDING)],
        [("symbol", ASCENDING)],
    ],
    "limit_down_ladder": [
        [("trade_date", DESCENDING)],
        [("symbol", ASCENDING)],
    ],
    "sector_flow": [
        [("trade_date", DESCENDING)],
        [("sector_code", ASCENDING)],
    ],
    "market_spectrum": [
        [("trade_date", DESCENDING)],
        [("granularity", ASCENDING)],
    ],
    "stock_info": [
        [("symbol", ASCENDING)],
        [("ts_code", ASCENDING)],
    ],
    "etf_daily": [
        [("ts_code", ASCENDING), ("trade_date", DESCENDING)],
        [("trade_date", DESCENDING)],
    ],
}


def connect_to_mongo():
    try:
        uri = MONGO_URI
        if "authSource" not in uri:
            if "?" in uri:
                uri += "&authSource=admin"
            else:
                uri += "?authSource=admin"
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        print(f"✅ 连接到 MongoDB")
        return client
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        sys.exit(1)


def check_indexes(client, db_name):
    db = client[db_name]
    collections = db.list_collection_names()
    
    print(f"\n📊 数据库: {db_name}, 集合数: {len(collections)}\n")
    
    all_issues = []
    
    for coll_name in sorted(collections):
        if coll_name.startswith('system.'):
            continue
            
        collection = db[coll_name]
        indexes = collection.index_information()
        try:
            doc_count = collection.estimated_document_count()
        except Exception:
            doc_count = "?"
        recommended = RECOMMENDED_INDEXES.get(coll_name, [])
        
        missing = []
        for rec_idx in recommended:
            rec_key = tuple(rec_idx)
            found = False
            for idx_name, idx_info in indexes.items():
                if idx_name == '_id_':
                    continue
                existing_key = tuple(idx_info.get('key', []))
                if existing_key == rec_key:
                    found = True
                    break
            if not found:
                missing.append(rec_idx)
        
        print(f"\n📁 {coll_name}: {doc_count:,} 文档, {len(indexes)} 索引")
        if missing:
            print(f"   ⚠️ 缺失: {len(missing)} 个")
            for idx in missing:
                idx_str = ", ".join([f"{k}" for k, v in idx])
                print(f"     - ({idx_str})")
            all_issues.append((coll_name, missing))
    
    if all_issues:
        print("\n" + "="*60)
        print("📝 生成创建索引脚本:\n")
        
        for coll_name, missing in all_issues:
            print(f"// {coll_name}")
            for idx in missing:
                idx_dict = "{" + ", ".join([f'"{k}": {v}' for k, v in idx]) + "}"
                print(f'db.{coll_name}.createIndex({idx_dict});')
        print()
    else:
        print("\n✅ 所有索引完整！")
    
    return all_issues


def main():
    client = connect_to_mongo()
    try:
        check_indexes(client, DATA_DB_NAME)
    finally:
        client.close()


if __name__ == "__main__":
    main()