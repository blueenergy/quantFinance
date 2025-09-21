#!/usr/bin/env python3
"""
测试股票名称同步功能
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector

def test_stock_name_functionality():
    """测试股票名称相关功能"""
    print("🧪 测试股票名称同步功能")
    print("=" * 60)
    
    collector = VolumePriceCollector()
    
    try:
        # 测试获取单个股票名称
        print("\n1️⃣ 测试获取单个股票名称:")
        test_symbols = ["000001", "000002", "600000"]
        
        for symbol in test_symbols:
            name = collector.get_stock_name(symbol)
            print(f"   {symbol} -> {name}")
        
        # 测试数据库中的股票信息集合
        print("\n2️⃣ 检查股票信息数据库:")
        stock_info_count = collector.stock_info_collection.count_documents({})
        print(f"   股票信息记录数: {stock_info_count}")
        
        if stock_info_count > 0:
            # 显示前5条记录作为示例
            sample_stocks = list(collector.stock_info_collection.find().limit(5))
            print("   示例记录:")
            for stock in sample_stocks:
                print(f"     {stock['symbol']} -> {stock['name']}")
        
        # 测试批量更新（少量股票）
        print("\n3️⃣ 测试批量更新股票名称（限制5只股票）:")
        existing_symbols = collector.collection.distinct("symbol")
        test_batch = existing_symbols[:5] if len(existing_symbols) >= 5 else existing_symbols
        
        if test_batch:
            print(f"   测试股票: {test_batch}")
            updated_count = collector.batch_update_stock_names(test_batch)
            print(f"   更新结果: {updated_count} 只股票")
        else:
            print("   数据库中没有股票数据可以测试")
        
        # 检查价格数据中是否包含股票名称
        print("\n4️⃣ 检查价格数据中的股票名称字段:")
        sample_record = collector.collection.find_one({"symbol": {"$in": test_symbols}})
        
        if sample_record:
            symbol = sample_record['symbol']
            name = sample_record.get('stock_name', '未找到')
            print(f"   示例记录: {symbol} -> {name}")
            if 'stock_name' in sample_record:
                print("   ✅ 价格数据中包含股票名称字段")
            else:
                print("   ⚠️ 价格数据中缺少股票名称字段，需要重新同步数据")
        else:
            print("   ❌ 未找到测试股票的价格数据")
        
        print("\n✅ 股票名称功能测试完成！")
        print("\n💡 使用建议:")
        print("   1. 运行 python volume_price_collector.py --update-names 批量更新所有股票名称")
        print("   2. 新同步的数据会自动包含股票名称")
        print("   3. 前端界面将显示股票代码和对应的中文名称")
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        collector.close()

if __name__ == "__main__":
    test_stock_name_functionality()
