#!/usr/bin/env python3
"""
检查哪些股票还没有名字
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector

def check_missing_stock_names():
    """检查没有名字的股票"""
    print("🔍 检查缺失股票名称")
    print("=" * 50)
    
    collector = VolumePriceCollector()
    
    try:
        # 1. 统计总股票数量
        print("\n1️⃣ 统计股票总数...")
        total_stocks = collector.collection.distinct("symbol")
        print(f"   数据库中共有 {len(total_stocks)} 只股票")
        
        # 2. 统计有名字的股票数量
        print("\n2️⃣ 统计有名字的股票...")
        stocks_with_names = collector.collection.distinct("symbol", {
            "stock_name": {"$exists": True, "$ne": None, "$ne": ""}
        })
        print(f"   有名字的股票: {len(stocks_with_names)} 只")
        
        # 3. 统计没有名字的股票
        print("\n3️⃣ 统计没有名字的股票...")
        stocks_without_names = collector.collection.distinct("symbol", {
            "$or": [
                {"stock_name": {"$exists": False}},
                {"stock_name": None},
                {"stock_name": ""}
            ]
        })
        print(f"   没有名字的股票: {len(stocks_without_names)} 只")
        
        # 4. 显示覆盖率
        print(f"\n📊 名字覆盖率: {len(stocks_with_names)}/{len(total_stocks)} = {len(stocks_with_names)/len(total_stocks)*100:.1f}%")
        
        # 5. 显示前20个没有名字的股票
        if stocks_without_names:
            print(f"\n📋 前20个没有名字的股票:")
            print("-" * 30)
            for i, symbol in enumerate(sorted(stocks_without_names)[:20]):
                print(f"   {i+1:2d}. {symbol}")
            
            if len(stocks_without_names) > 20:
                print(f"   ... 还有 {len(stocks_without_names) - 20} 只股票")
        
        # 6. 检查stock_info集合中的名字数量
        print(f"\n6️⃣ 检查stock_info集合...")
        stock_info_count = collector.stock_info_collection.count_documents({})
        print(f"   stock_info集合中有 {stock_info_count} 条名字记录")
        
        # 7. 随机显示几个有名字的股票作为对比
        print(f"\n✅ 随机显示5个有名字的股票:")
        print("-" * 40)
        import random
        sample_stocks = random.sample(stocks_with_names, min(5, len(stocks_with_names)))
        for symbol in sample_stocks:
            record = collector.collection.find_one(
                {"symbol": symbol, "stock_name": {"$exists": True}},
                {"symbol": 1, "stock_name": 1, "_id": 0}
            )
            if record:
                print(f"   {record['symbol']} -> {record['stock_name']}")
        
        return stocks_without_names
        
    except Exception as e:
        print(f"❌ 检查失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        collector.close()

if __name__ == "__main__":
    missing_stocks = check_missing_stock_names()
    
    if missing_stocks:
        print(f"\n🔧 建议操作:")
        print(f"   1. 运行批量更新: python tests/update_stock_names.py")
        print(f"   2. 或单独更新特定股票: python tests/update_single_stock_name.py <股票代码>")
