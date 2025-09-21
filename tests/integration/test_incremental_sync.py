#!/usr/bin/env python3
"""
增量同步测试脚本
演示如何使用增量同步功能只获取缺失的股票数据
"""

import sys
import os
from datetime import datetime, date, timedelta

# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector

def test_incremental_sync():
    """测试增量同步功能"""
    print("=" * 60)
    print("🧪 增量同步功能测试")
    print("=" * 60)
    
    collector = VolumePriceCollector()
    
    try:
        # 测试单个股票的增量同步
        test_symbol = "000001"  # 平安银行
        
        print(f"\n1️⃣ 测试股票 {test_symbol} 的增量同步")
        print("-" * 40)
        
        # 检查现有数据状态
        local_count, latest_date = collector.check_local_data(test_symbol)
        print(f"📊 当前数据状态:")
        print(f"   - 记录数量: {local_count}")
        print(f"   - 最新日期: {latest_date.strftime('%Y-%m-%d') if latest_date else '无数据'}")
        
        # 执行增量同步（最近3天）
        print(f"\n🔄 执行增量同步（最近3天）...")
        result = collector.get_incremental_data(test_symbol, days_back=3)
        
        if not result.empty:
            print(f"✅ 同步成功，获取 {len(result)} 条新数据")
            print("📅 新数据日期范围:")
            print(f"   - 从: {result['trade_date'].min().strftime('%Y-%m-%d')}")
            print(f"   - 到: {result['trade_date'].max().strftime('%Y-%m-%d')}")
            
            # 保存到数据库
            records = result.to_dict('records')
            for record in records:
                if isinstance(record['trade_date'], date):
                    record['trade_date'] = datetime.combine(
                        record['trade_date'], datetime.min.time())
                collector.collection.update_one(
                    {
                        "symbol": record['symbol'],
                        "trade_date": record['trade_date']
                    },
                    {"$set": record},
                    upsert=True
                )
            print(f"💾 已保存到数据库")
        else:
            print("📚 无新数据需要同步")
        
        print(f"\n2️⃣ 测试批量增量同步")
        print("-" * 40)
        
        # 获取数据库中已有的前5只股票进行测试
        existing_symbols = collector.collection.distinct("symbol")[:5]
        if existing_symbols:
            print(f"📊 将对 {len(existing_symbols)} 只股票进行增量同步测试: {existing_symbols}")
            
            success_count = 0
            for symbol in existing_symbols:
                try:
                    result = collector.get_incremental_data(symbol, days_back=1)
                    if not result.empty:
                        success_count += 1
                        print(f"   ✅ {symbol}: 获取 {len(result)} 条新数据")
                    else:
                        print(f"   📚 {symbol}: 无新数据")
                except Exception as e:
                    print(f"   ❌ {symbol}: 同步失败 - {str(e)}")
            
            print(f"\n📈 批量测试结果: {success_count}/{len(existing_symbols)} 成功")
        else:
            print("⚠️ 数据库中没有股票数据，无法进行批量测试")
        
        print(f"\n3️⃣ 使用方法说明")
        print("-" * 40)
        print("增量同步的几种使用方式:")
        print()
        print("🔸 方式1: 增量同步所有已有股票(默认1天)")
        print("   python volume_price_collector.py --incremental")
        print()
        print("🔸 方式2: 增量同步指定天数")
        print("   python volume_price_collector.py --incremental --days=3")
        print()
        print("🔸 方式3: 在代码中直接调用")
        print("   collector = VolumePriceCollector()")
        print("   collector.incremental_sync_all(days=1)")
        print("   collector.close()")
        print()
        print("✨ 增量同步的优势:")
        print("   - 只同步缺失的日期数据，节省时间")
        print("   - 智能跳过已是最新的股票")
        print("   - 适合每日定时任务使用")
        print("   - 减少API调用次数，避免限制")
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
    finally:
        collector.close()
        print(f"\n🏁 测试完成")

if __name__ == "__main__":
    test_incremental_sync()
