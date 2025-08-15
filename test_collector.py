#!/usr/bin/env python3
"""
股票数据收集器测试脚本
"""

from volume_price_collector import VolumePriceCollector
from pymongo import MongoClient
import pandas as pd

def test_data_check():
    """测试本地数据检查功能"""
    print("🧪 测试本地数据检查功能")
    print("-" * 40)
    
    collector = VolumePriceCollector()
    
    # 测试几只已知股票
    test_symbols = ['000001', '000002', '600519', '999999']  # 最后一个应该不存在
    
    for symbol in test_symbols:
        count, latest_date = collector.check_local_data(symbol)
        if count > 0:
            print(f"✅ {symbol}: {count} 条记录，最新: {latest_date.strftime('%Y-%m-%d')}")
        else:
            print(f"❌ {symbol}: 无本地数据")
    
    collector.close()

def show_database_stats():
    """显示数据库统计信息"""
    print("\n📊 数据库统计信息")
    print("-" * 40)
    
    client = MongoClient('mongodb://localhost:27017/')
    db = client['quant']
    collection = db['volume_price']
    
    # 基本统计
    total_records = collection.count_documents({})
    total_symbols = len(collection.distinct('symbol'))
    
    print(f"总记录数: {total_records:,}")
    print(f"股票数量: {total_symbols}")
    print(f"平均每股记录数: {total_records/total_symbols:.1f}")
    
    # 按股票统计记录数（显示前10）
    pipeline = [
        {"$group": {"_id": "$symbol", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    
    print("\n📈 记录数最多的股票TOP10:")
    for result in collection.aggregate(pipeline):
        print(f"  {result['_id']}: {result['count']} 条记录")
    
    # 最新数据时间
    latest_record = collection.find_one(sort=[("trade_date", -1)])
    if latest_record:
        print(f"\n📅 最新数据日期: {latest_record['trade_date'].strftime('%Y-%m-%d')}")
    
    client.close()

def test_incremental_update():
    """测试增量更新模式"""
    print("\n🔄 测试增量更新模式（模拟）")
    print("-" * 40)
    
    # 这里只是展示逻辑，不实际执行API调用
    collector = VolumePriceCollector()
    
    # 模拟检查几只股票的本地数据
    test_symbols = ['000001', '000002', '600519']
    
    for symbol in test_symbols:
        count, latest_date = collector.check_local_data(symbol)
        if count > 0:
            days_old = (pd.Timestamp.now() - latest_date).days
            if days_old <= 1:
                print(f"🟢 {symbol}: 数据较新（{days_old}天前），建议跳过")
            else:
                print(f"🟡 {symbol}: 数据较旧（{days_old}天前），建议更新")
        else:
            print(f"🔴 {symbol}: 无本地数据，需要全量获取")
    
    collector.close()

if __name__ == "__main__":
    print("🧪 股票数据收集器 - 功能测试")
    print("=" * 60)
    
    test_data_check()
    show_database_stats()
    test_incremental_update()
    
    print("\n" + "=" * 60)
    print("✅ 测试完成！")
    print("\n使用说明:")
    print("  python volume_price_collector.py                # 智能模式（跳过已有数据）")
    print("  python volume_price_collector.py --force        # 强制更新所有数据")
    print("  python volume_price_collector.py --update-existing  # 仅更新已有股票")
