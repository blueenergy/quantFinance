#!/usr/bin/env python3
"""
为特定股票添加名称信息
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector

def update_specific_stock_name(symbol):
    """为特定股票更新名称"""
    print(f"🔄 为股票 {symbol} 添加名称信息")
    print("=" * 50)
    
    collector = VolumePriceCollector()
    
    try:
        # 1. 获取股票名称
        print(f"\n1️⃣ 获取股票 {symbol} 的名称...")
        stock_name = collector.get_stock_name(symbol)
        print(f"   获取到名称: {stock_name}")
        
        # 2. 更新该股票的所有价格记录
        print(f"\n2️⃣ 更新价格记录中的名称字段...")
        result = collector.collection.update_many(
            {"symbol": symbol},
            {"$set": {"stock_name": stock_name}}
        )
        
        print(f"   更新了 {result.modified_count} 条记录")
        
        # 3. 验证更新结果
        print(f"\n3️⃣ 验证更新结果...")
        sample_record = collector.collection.find_one(
            {"symbol": symbol}, 
            {"symbol": 1, "stock_name": 1, "trade_date": 1, "_id": 0}
        )
        
        if sample_record:
            print(f"   示例记录: {sample_record}")
        else:
            print(f"   ❌ 未找到股票 {symbol} 的价格记录")
        
        # 4. 测试API
        print(f"\n4️⃣ 测试API返回...")
        import requests
        try:
            response = requests.get(f"http://localhost:3001/api/stock-info/{symbol}")
            print(f"   API响应: {response.json()}")
        except Exception as e:
            print(f"   API测试失败: {str(e)}")
        
        print(f"\n✅ 股票 {symbol} 名称更新完成！")
        
    except Exception as e:
        print(f"❌ 更新失败: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        collector.close()

if __name__ == "__main__":
    # 可以通过命令行参数指定股票代码
    if len(sys.argv) > 1:
        symbol = sys.argv[1]
    else:
        symbol = "300347"  # 默认处理300347
    
    update_specific_stock_name(symbol)
