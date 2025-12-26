"""
工具脚本：手动交易操作
- 重试失败的交易信号
- 手动生成交易信号
"""

import os
import sys
import time
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv
import uuid

# Load environment variables
load_dotenv()

# MongoDB connection
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = "finance"
client = MongoClient(mongo_url)
db = client[DB_NAME]


def retry_failed_signal(order_id: str, user_id: str):
    """手动重试一个失败的交易信号"""
    signals_coll = db["trade_signals"]
    
    # 查找指定的信号
    signal = signals_coll.find_one({
        "order_id": order_id,
        "user_id": user_id
    })
    
    if not signal:
        print(f"❌ 交易信号不存在: {order_id}")
        return False
    
    current_status = signal.get("status", "unknown")
    
    # 检查是否为可以重试的状态
    if current_status not in ["failed", "rejected", "retry_pending"]:
        print(f"❌ 信号状态为 {current_status}，无法重试。仅支持 failed、rejected 或 retry_pending 状态的信号。")
        return False
    
    # 重置状态为 pending，重置重试次数
    update_result = signals_coll.update_one(
        {"order_id": order_id, "user_id": user_id},
        {
            "$set": {
                "status": "pending",
                "retry_count": 0,
                "last_error": None,
                "updated_at": time.time(),
                "retried_at": time.time(),  # 记录重试时间
                "retried_by": user_id  # 记录重试用户
            }
        }
    )
    
    if update_result.modified_count > 0:
        print(f"✅ 信号 {order_id} 已重置为待执行状态")
        print(f"   之前状态: {current_status} -> 新状态: pending")
        return True
    else:
        print(f"❌ 更新信号状态失败: {order_id}")
        return False


def create_manual_signal(symbol: str, action: str, size: int, price: float, strategy: str, user_id: str):
    """手动创建一个交易信号"""
    signals_coll = db["trade_signals"]
    
    # 验证参数
    action = action.lower()
    if action not in ["buy", "sell"]:
        print("❌ 操作类型必须是 'buy' 或 'sell'")
        return False
    
    if size <= 0:
        print("❌ 交易数量必须大于0")
        return False
    
    # 生成订单ID
    order_id = f"manual_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    
    # 创建信号文档
    signal_doc = {
        "order_id": order_id,
        "symbol": symbol,
        "action": action,
        "size": size,
        "price": price,
        "strategy": strategy,
        "user_id": user_id,
        "status": "pending",  # 新建信号为待执行状态
        "retry_count": 0,
        "timestamp": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_executable": True,  # 标记为可执行
        "mode": "live",  # 实盘模式
        "source": "manual",  # 标记为手动创建
        "created_by": user_id,
        "manual_entry": True  # 标记为手动输入
    }
    
    # 插入信号
    result = signals_coll.insert_one(signal_doc)
    
    print(f"✅ 手动交易信号创建成功")
    print(f"   订单ID: {order_id}")
    print(f"   股票: {symbol}")
    print(f"   操作: {action.upper()}")
    print(f"   数量: {size}")
    print(f"   价格: {price if price else '市价'}")
    print(f"   策略: {strategy}")
    
    return True


def list_failed_signals(user_id: str, limit: int = 20):
    """列出失败的交易信号"""
    signals_coll = db["trade_signals"]
    
    # 查询失败的信号
    failed_signals = list(signals_coll.find({
        "user_id": user_id,
        "status": {"$in": ["failed", "rejected"]}
    }).sort("timestamp", -1).limit(limit))
    
    if not failed_signals:
        print("✅ 没有找到失败的交易信号")
        return []
    
    print(f"📊 找到 {len(failed_signals)} 个失败的交易信号:")
    print("-" * 80)
    
    for i, signal in enumerate(failed_signals, 1):
        timestamp = datetime.fromtimestamp(signal.get("timestamp", 0))
        print(f"{i:2d}. {signal['order_id']}")
        print(f"    股票: {signal['symbol']} | 操作: {signal['action'].upper()} | 数量: {signal['size']}")
        print(f"    状态: {signal['status']} | 时间: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        if signal.get("last_error"):
            print(f"    错误: {signal['last_error'][:60]}...")
        print()
    
    return failed_signals


def list_manual_signals(user_id: str, limit: int = 20):
    """列出手动创建的交易信号"""
    signals_coll = db["trade_signals"]
    
    # 查询手动创建的信号
    manual_signals = list(signals_coll.find({
        "user_id": user_id,
        "$or": [
            {"source": "manual"},
            {"manual_entry": True}
        ]
    }).sort("timestamp", -1).limit(limit))
    
    if not manual_signals:
        print("✅ 没有找到手动创建的交易信号")
        return []
    
    print(f"📊 找到 {len(manual_signals)} 个手动创建的交易信号:")
    print("-" * 80)
    
    for i, signal in enumerate(manual_signals, 1):
        timestamp = datetime.fromtimestamp(signal.get("timestamp", 0))
        status = signal.get("status", "unknown")
        print(f"{i:2d}. {signal['order_id']}")
        print(f"    股票: {signal['symbol']} | 操作: {signal['action'].upper()} | 数量: {signal['size']}")
        print(f"    状态: {status} | 时间: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print()
    
    return manual_signals


def batch_retry_failed_signals(user_id: str, status_filter: str = "failed", limit: int = 10):
    """批量重试失败的交易信号"""
    signals_coll = db["trade_signals"]
    
    # 确定要重试的状态
    valid_statuses = {
        "failed": ["failed"],
        "rejected": ["rejected"], 
        "retry_pending": ["retry_pending"],
        "all_failed": ["failed", "rejected", "retry_pending"]
    }
    
    status_list = valid_statuses.get(status_filter, ["failed"])
    
    # 查询符合条件的信号
    signals_to_retry = list(signals_coll.find({
        "user_id": user_id,
        "status": {"$in": status_list}
    }).sort("timestamp", -1).limit(limit))
    
    if not signals_to_retry:
        print(f"✅ 没有找到状态为 {status_filter} 的信号")
        return 0
    
    print(f"🔄 找到 {len(signals_to_retry)} 个状态为 {status_filter} 的信号，开始批量重试...")
    
    # 批量更新
    order_ids = [signal["order_id"] for signal in signals_to_retry]
    
    update_result = signals_coll.update_many(
        {
            "user_id": user_id,
            "order_id": {"$in": order_ids}
        },
        {
            "$set": {
                "status": "pending",
                "retry_count": 0,
                "last_error": None,
                "updated_at": time.time(),
                "retried_at": time.time(),
                "retried_by": user_id
            }
        }
    )
    
    print(f"✅ 成功重试 {update_result.modified_count} 个信号")
    return update_result.modified_count


def main():
    print("🔧 手动交易操作工具")
    print("=" * 50)
    
    # 获取用户ID
    user_id = input("请输入用户ID: ").strip()
    if not user_id:
        print("❌ 用户ID不能为空")
        sys.exit(1)
    
    while True:
        print("\n" + "=" * 50)
        print("请选择操作:")
        print("1. 重试单个失败信号")
        print("2. 手动生成交易信号")
        print("3. 查看失败的信号")
        print("4. 查看手动创建的信号")
        print("5. 批量重试失败信号")
        print("6. 退出")
        
        choice = input("\n请输入选择 (1-6): ").strip()
        
        if choice == "1":
            order_id = input("请输入要重试的订单ID: ").strip()
            if order_id:
                retry_failed_signal(order_id, user_id)
        
        elif choice == "2":
            symbol = input("请输入股票代码 (如 002050.SZ): ").strip()
            action = input("请输入操作 (buy/sell): ").strip().lower()
            try:
                size = int(input("请输入数量: ").strip())
                price_input = input("请输入价格 (回车为市价): ").strip()
                price = float(price_input) if price_input else None
                strategy = input("请输入策略名称 (默认: manual): ").strip()
                if not strategy:
                    strategy = "manual"
                
                if symbol and action:
                    create_manual_signal(symbol, action, size, price, strategy, user_id)
            except ValueError:
                print("❌ 输入格式错误，请输入有效数字")
        
        elif choice == "3":
            try:
                limit = int(input("请输入显示数量 (默认20): ").strip() or "20")
                list_failed_signals(user_id, limit)
            except ValueError:
                print("❌ 输入格式错误")
        
        elif choice == "4":
            try:
                limit = int(input("请输入显示数量 (默认20): ").strip() or "20")
                list_manual_signals(user_id, limit)
            except ValueError:
                print("❌ 输入格式错误")
        
        elif choice == "5":
            print("可选状态: failed, rejected, retry_pending, all_failed")
            status_filter = input("请输入要重试的状态 (默认failed): ").strip()
            if not status_filter:
                status_filter = "failed"
            try:
                limit = int(input("请输入最大重试数量 (默认10): ").strip() or "10")
                batch_retry_failed_signals(user_id, status_filter, limit)
            except ValueError:
                print("❌ 输入格式错误")
        
        elif choice == "6":
            print("👋 退出工具")
            break
        
        else:
            print("❌ 无效选择，请重新输入")


if __name__ == "__main__":
    main()