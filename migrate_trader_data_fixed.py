#!/usr/bin/env python3
"""
数据迁移脚本：为旧的 trader_accounts 和 trader_positions 关联 securities_account_id
改进版：处理 broker 字段为空的情况，按 user_id + account_id 匹配
"""

import os
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 连接数据库
MONGODB_URI = os.getenv("MONGODB_URL", "mongodb://localhost:27017/")
client = MongoClient(MONGODB_URI)
db = client["finance"]

print("=" * 80)
print("开始迁移 trader_accounts 和 trader_positions")
print("策略：按 user_id + account_id 匹配，优先选择真实账户")
print("=" * 80)

# 1. 处理 trader_accounts
print("\n【1. 迁移 trader_accounts】")
orphaned_accounts = list(db.trader_accounts.find({"securities_account_id": {"$exists": False}}))
print(f"找到 {len(orphaned_accounts)} 条未关联的记录")

for ta in orphaned_accounts:
    user_id = ta.get("user_id")
    account_id = ta.get("account_id")
    
    print(f"\n  处理: User={user_id}, Account={account_id}")
    
    # 查找匹配的证券账户（按 user_id + account_id）
    matches = list(db.securities_accounts.find({
        "user_id": user_id,
        "account_id": account_id
    }))
    
    if matches:
        # 优先选择真实账户
        selected = None
        for m in matches:
            if m.get("account_type") == "real":
                selected = m
                break
        if not selected:
            selected = matches[0]
        
        securities_account_id = str(selected["_id"])
        account_type = selected.get("account_type", "real")
        broker = selected.get("broker")
        
        print(f"    ✅ 匹配到: Broker={broker}, Type={account_type}, ID={securities_account_id}")
        
        # 更新记录
        db.trader_accounts.update_one(
            {"_id": ta["_id"]},
            {"$set": {
                "securities_account_id": securities_account_id,
                "account_type": account_type,
                "broker": broker  # 同时修正 broker 字段
            }}
        )
    else:
        print(f"    ❌ 未找到匹配的证券账户")

# 2. 处理 trader_positions
print("\n【2. 迁移 trader_positions】")
orphaned_positions = list(db.trader_positions.find({"securities_account_id": {"$exists": False}}))
print(f"找到 {len(orphaned_positions)} 条未关联的记录")

updated_count = 0
for tp in orphaned_positions:
    user_id = tp.get("user_id")
    account_id = tp.get("account_id")
    
    # 查找匹配的证券账户（按 user_id + account_id）
    matches = list(db.securities_accounts.find({
        "user_id": user_id,
        "account_id": account_id
    }))
    
    if matches:
        # 优先选择真实账户
        selected = None
        for m in matches:
            if m.get("account_type") == "real":
                selected = m
                break
        if not selected:
            selected = matches[0]
        
        securities_account_id = str(selected["_id"])
        account_type = selected.get("account_type", "real")
        broker = selected.get("broker")
        
        # 更新记录
        db.trader_positions.update_one(
            {"_id": tp["_id"]},
            {"$set": {
                "securities_account_id": securities_account_id,
                "account_type": account_type,
                "broker": broker  # 同时修正 broker 字段
            }}
        )
        updated_count += 1

print(f"  ✅ 已更新 {updated_count} 条持仓记录")

# 3. 最终验证
print("\n" + "=" * 80)
print("【3. 验证结果】")
print("=" * 80)

orphaned_accounts_after = list(db.trader_accounts.find({"securities_account_id": {"$exists": False}}))
orphaned_positions_after = list(db.trader_positions.find({"securities_account_id": {"$exists": False}}))

print(f"\ntrader_accounts 未关联: {len(orphaned_accounts_after)} 条")
print(f"trader_positions 未关联: {len(orphaned_positions_after)} 条")

if len(orphaned_accounts_after) == 0 and len(orphaned_positions_after) == 0:
    print("\n✅ 迁移成功！所有数据都已关联")
else:
    print("\n⚠️  仍有未关联的数据，请检查")

print("\n" + "=" * 80)
print("迁移完成")
print("=" * 80)
