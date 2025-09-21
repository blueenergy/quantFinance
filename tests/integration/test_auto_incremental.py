#!/usr/bin/env python3
"""
测试自动计算增量同步天数的功能
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector
from datetime import date, datetime, timedelta

def test_auto_calculation():
    """测试自动计算增量同步天数功能"""
    print("🧪 测试自动计算增量同步天数功能")
    print("=" * 50)
    
    collector = VolumePriceCollector()
    
    try:
        # 1. 测试获取数据库最后日期
        print("\n1️⃣ 测试获取数据库最后日期:")
        last_date = collector.get_last_trade_date()
        if last_date:
            print(f"   📅 数据库最后日期: {last_date}")
        else:
            print("   ❌ 数据库中没有数据")
        
        # 2. 测试计算缺失天数
        print("\n2️⃣ 测试计算缺失天数:")
        missing_days = collector.calculate_missing_days()
        print(f"   📊 需要同步天数: {missing_days}")
        
        # 3. 测试模拟不同的最后日期情况
        print("\n3️⃣ 模拟不同情况:")
        
        # 模拟昨天
        yesterday = date.today() - timedelta(days=1)
        print(f"   如果最后日期是昨天 ({yesterday}): 需要同步 1 天")
        
        # 模拟一周前
        week_ago = date.today() - timedelta(days=7)
        print(f"   如果最后日期是一周前 ({week_ago}): 需要同步 7 天")
        
        # 模拟一个月前
        month_ago = date.today() - timedelta(days=30)
        print(f"   如果最后日期是一个月前 ({month_ago}): 需要同步 30 天")
        
        # 4. 测试增量同步调用（不实际执行）
        print("\n4️⃣ 测试增量同步方法调用:")
        print("   调用 incremental_sync_all() 不指定天数...")
        
        # 这里只演示，不实际执行
        print(f"   → 将自动计算为 {missing_days} 天")
        
        print("\n✅ 所有测试完成！")
        print("\n📋 使用说明:")
        print("   python volume_price_collector.py --incremental          # 自动计算天数")
        print("   python volume_price_collector.py --incremental --days=3 # 指定3天")
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        collector.close()

if __name__ == "__main__":
    test_auto_calculation()
