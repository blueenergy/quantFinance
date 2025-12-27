#!/usr/bin/env python3
"""
数据迁移脚本：为所有旧的 securities_accounts 添加 account_type 字段
"""

import os
from pymongo import MongoClient
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 连接数据库
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
client = MongoClient(MONGODB_URI)
db = client["finance"]

print("=" * 80)
print("开始迁移 securities_accounts")
print("=" * 80)

# 查找所有没有 account_type 字段的账户
missing_type = list(db.securities_accounts.find({"account_type": {"$exists": False}}))
print(f"\n找到 {len(missing_type)} 个账户没有 account_type 字段")

if missing_type:
    print("\n即将为这些账户设置 account_type='real'：")
    for acc in missing_type:
        print(f"  - ID: {acc['_id']}")
        print(f"    券商: {acc.get('broker')}")
        print(f"    账户ID: {acc.get('account_id')}")
    
    confirm = input("\n确认执行迁移？(yes/no): ")
    if confirm.lower() == 'yes':
        result = db.securities_accounts.update_many(
            {"account_type": {"$exists": False}},
            {"$set": {"account_type": "real"}}
        )
        print(f"\n✅ 成功更新 {result.modified_count} 个账户")
        
        # 同时更新对应的 trader_accounts
        print("\n检查 trader_accounts...")
        trader_missing = list(db.trader_accounts.find({"account_type": {"$exists": False}}))
        if trader_missing:
            print(f"找到 {len(trader_missing)} 个 trader_accounts 没有 account_type 字段")
            trader_result = db.trader_accounts.update_many(
                {"account_type": {"$exists": False}},
                {"$set": {"account_type": "real"}}
            )
            print(f"✅ 成功更新 {trader_result.modified_count} 个 trader_accounts")
        else:
            print("✅ 所有 trader_accounts 都有 account_type 字段")
    else:
        print("\n❌ 已取消迁移")
else:
    print("\n✅ 所有账户都已有 account_type 字段，无需迁移")

# 检查是否有 securities_account_id 字段
print("\n" + "=" * 80)
print("检查 securities_account_id 字段")
print("=" * 80)

missing_sec_id = list(db.securities_accounts.find({"securities_account_id": {"$exists": False}}))
print(f"\n找到 {len(missing_sec_id)} 个账户没有 securities_account_id 字段")

if missing_sec_id:
    print("\n即将为这些账户添加 securities_account_id 字段：")
    for acc in missing_sec_id:
        print(f"  - ID: {acc['_id']}")
    
    confirm = input("\n确认执行？(yes/no): ")
    if confirm.lower() == 'yes':
        for acc in missing_sec_id:
            db.securities_accounts.update_one(
                {"_id": acc["_id"]},
                {"$set": {"securities_account_id": str(acc["_id"])}}
            )
        print(f"\n✅ 成功更新 {len(missing_sec_id)} 个账户")
    else:
        print("\n❌ 已取消")
else:
    print("\n✅ 所有账户都有 securities_account_id 字段")

print("\n" + "=" * 80)
print("迁移完成")
print("=" * 80)
