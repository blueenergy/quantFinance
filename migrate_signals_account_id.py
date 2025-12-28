#!/usr/bin/env python3
"""
为 trade_signals 和 trade_executions 补充 securities_account_id 字段

策略：
1. 单账户用户：直接关联到该用户的唯一账户
2. 多账户用户（将来）：根据 broker + account_id 匹配
3. 无法匹配的：标记但不删除
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_db_collections
from bson import ObjectId
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


def migrate_signals():
    """为交易信号补充 securities_account_id"""
    collections = get_db_collections()
    signals_coll = collections["trade_signals"]
    securities_accounts_coll = collections["securities_accounts"]
    
    print("=" * 80)
    print("检查 trade_signals 缺失 securities_account_id 的记录")
    print("=" * 80)
    
    # 查找没有 securities_account_id 的信号
    missing_signals = list(signals_coll.find({
        "securities_account_id": {"$exists": False}
    }))
    
    print(f"\n找到 {len(missing_signals)} 个信号缺少 securities_account_id")
    
    if not missing_signals:
        print("✅ 所有信号都已有 securities_account_id 字段")
        return
    
    # 按 user_id 分组统计
    user_stats = {}
    for signal in missing_signals:
        user_id = signal.get("user_id", "unknown")
        user_stats[user_id] = user_stats.get(user_id, 0) + 1
    
    print("\n按用户统计：")
    for user_id, count in user_stats.items():
        print(f"  - {user_id}: {count} 个信号")
    
    confirm = input("\n是否继续迁移？(yes/no): ")
    if confirm.lower() != 'yes':
        print("❌ 已取消")
        return
    
    # 开始迁移
    updated_count = 0
    skipped_count = 0
    
    for signal in missing_signals:
        user_id = signal.get("user_id")
        if not user_id:
            print(f"⚠️  跳过：信号 {signal.get('order_id')} 没有 user_id")
            skipped_count += 1
            continue
        
        # 查找用户的证券账户
        user_accounts = list(securities_accounts_coll.find({"user_id": user_id}))
        
        if len(user_accounts) == 0:
            print(f"⚠️  跳过：用户 {user_id} 没有证券账户")
            skipped_count += 1
            continue
        
        if len(user_accounts) == 1:
            # 单账户：直接关联
            account = user_accounts[0]
            securities_account_id = str(account["_id"])
            
            signals_coll.update_one(
                {"_id": signal["_id"]},
                {"$set": {"securities_account_id": securities_account_id}}
            )
            updated_count += 1
        else:
            # 多账户：尝试通过 broker/account_id 匹配
            signal_broker = signal.get("broker")
            signal_account_id = signal.get("account_id")
            
            matched = None
            if signal_broker and signal_account_id:
                for account in user_accounts:
                    if (account.get("broker") == signal_broker and 
                        account.get("account_id") == signal_account_id):
                        matched = account
                        break
            
            if matched:
                securities_account_id = str(matched["_id"])
                signals_coll.update_one(
                    {"_id": signal["_id"]},
                    {"$set": {"securities_account_id": securities_account_id}}
                )
                updated_count += 1
            else:
                # 无法匹配：使用第一个账户（默认）
                default_account = user_accounts[0]
                securities_account_id = str(default_account["_id"])
                
                signals_coll.update_one(
                    {"_id": signal["_id"]},
                    {"$set": {
                        "securities_account_id": securities_account_id,
                        "_migration_note": "auto_assigned_to_first_account"
                    }}
                )
                updated_count += 1
                print(f"⚠️  信号 {signal.get('order_id')} 无法精确匹配，已关联到默认账户")
    
    print(f"\n✅ 迁移完成")
    print(f"   更新: {updated_count} 个信号")
    print(f"   跳过: {skipped_count} 个信号")

def migrate_executions():
    """为交易执行记录补充 securities_account_id"""
    collections = get_db_collections()
    executions_coll = collections["trade_executions"]
    signals_coll = collections["trade_signals"]
    
    print("\n" + "=" * 80)
    print("检查 trade_executions 缺失 securities_account_id 的记录")
    print("=" * 80)
    
    # 查找没有 securities_account_id 的执行记录
    missing_executions = list(executions_coll.find({
        "securities_account_id": {"$exists": False}
    }))
    
    print(f"\n找到 {len(missing_executions)} 个执行记录缺少 securities_account_id")
    
    if not missing_executions:
        print("✅ 所有执行记录都已有 securities_account_id 字段")
        return
    
    confirm = input("\n是否继续迁移执行记录？(yes/no): ")
    if confirm.lower() != 'yes':
        print("❌ 已取消")
        return
    
    # 开始迁移
    updated_count = 0
    skipped_count = 0
    
    for execution in missing_executions:
        # 尝试从关联的信号获取 securities_account_id
        order_id = execution.get("order_id")
        if not order_id:
            skipped_count += 1
            continue
        
        # 查找对应的信号
        signal = signals_coll.find_one({"order_id": order_id})
        
        if signal and signal.get("securities_account_id"):
            # 从信号复制 securities_account_id
            executions_coll.update_one(
                {"_id": execution["_id"]},
                {"$set": {"securities_account_id": signal["securities_account_id"]}}
            )
            updated_count += 1
        else:
            print(f"⚠️  无法找到执行 {order_id} 对应的信号或信号无账户ID")
            skipped_count += 1
    
    print(f"\n✅ 执行记录迁移完成")
    print(f"   更新: {updated_count} 个记录")
    print(f"   跳过: {skipped_count} 个记录")

if __name__ == "__main__":
    try:
        migrate_signals()
        migrate_executions()
        
        print("\n" + "=" * 80)
        print("✅ 所有迁移完成")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ 迁移失败: {e}")
        import traceback
        traceback.print_exc()
