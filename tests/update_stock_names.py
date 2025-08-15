#!/usr/bin/env python3
"""
为现有的股票价格数据批量添加股票名称字段
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector
from tqdm import tqdm

def update_existing_records_with_names():
    """为现有的价格记录批量添加股票名称"""
    print("🔄 为现有股票价格数据批量添加股票名称")
    print("=" * 60)
    
    collector = VolumePriceCollector()
    
    try:
        # 1. 获取所有不同的股票代码
        print("\n1️⃣ 获取所有股票代码...")
        all_symbols = collector.collection.distinct("symbol")
        print(f"   找到 {len(all_symbols)} 只不同的股票")
        
        # 2. 首先确保股票信息集合有所有股票的名称
        print("\n2️⃣ 更新股票信息集合...")
        updated_names = collector.batch_update_stock_names(all_symbols[:50])  # 限制50只，避免API请求过多
        print(f"   更新了 {updated_names} 只股票的名称信息")
        
        # 3. 批量更新价格记录
        print("\n3️⃣ 为价格记录添加股票名称字段...")
        updated_records = 0
        batch_size = 100  # 每批处理100只股票
        
        for i in range(0, min(len(all_symbols), 500), batch_size):  # 限制500只股票
            batch_symbols = all_symbols[i:i+batch_size]
            print(f"\n   处理第 {i//batch_size + 1} 批 (共 {len(batch_symbols)} 只股票)...")
            
            for symbol in tqdm(batch_symbols, desc=f"批次 {i//batch_size + 1}"):
                try:
                    # 获取股票名称
                    stock_name = collector.get_stock_name(symbol)
                    
                    # 批量更新该股票的所有记录
                    result = collector.collection.update_many(
                        {"symbol": symbol, "stock_name": {"$exists": False}},
                        {"$set": {"stock_name": stock_name}}
                    )
                    
                    updated_records += result.modified_count
                    
                except Exception as e:
                    print(f"\n   ⚠️ 更新 {symbol} 失败: {str(e)}")
                    continue
        
        print(f"\n✅ 批量更新完成！")
        print(f"   更新了 {updated_records} 条价格记录")
        
        # 4. 验证更新结果
        print("\n4️⃣ 验证更新结果...")
        
        # 检查有股票名称的记录数量
        records_with_names = collector.collection.count_documents({"stock_name": {"$exists": True}})
        total_records = collector.collection.count_documents({})
        
        print(f"   总记录数: {total_records}")
        print(f"   有名称的记录: {records_with_names}")
        print(f"   完成率: {(records_with_names/total_records*100):.1f}%")
        
        # 显示几个示例
        sample_records = list(collector.collection.find(
            {"stock_name": {"$exists": True}}, 
            {"symbol": 1, "stock_name": 1, "_id": 0}
        ).limit(5))
        
        print("\n   示例记录:")
        for record in sample_records:
            print(f"     {record['symbol']} -> {record['stock_name']}")
        
        print(f"\n🎉 股票名称更新完成！现在前端应该能显示股票名称了。")
        
    except Exception as e:
        print(f"❌ 批量更新失败: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        collector.close()

if __name__ == "__main__":
    update_existing_records_with_names()
