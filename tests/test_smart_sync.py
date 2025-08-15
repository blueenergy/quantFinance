#!/usr/bin/env python3
"""
测试智能增量同步功能 - 为每只股票单独计算需要同步的天数
"""

import sys
import os
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector
from datetime import date, datetime, timedelta

def test_per_stock_calculation():
    """测试按股票单独计算增量同步功能"""
    print("🧪 测试智能增量同步功能（按股票单独计算）")
    print("=" * 60)
    
    collector = VolumePriceCollector()
    
    try:
        # 1. 获取所有股票列表
        print("\n1️⃣ 获取数据库中的股票列表:")
        existing_symbols = collector.collection.distinct("symbol")
        print(f"   📊 数据库中共有 {len(existing_symbols)} 只股票")
        
        if len(existing_symbols) == 0:
            print("   ❌ 数据库为空，请先运行数据收集")
            return
        
        # 2. 展示前几只股票的同步状态
        print("\n2️⃣ 各股票的同步状态分析:")
        sample_symbols = existing_symbols[:10]  # 取前10只作为样本
        
        sync_analysis = {}
        for symbol in sample_symbols:
            last_date = collector.get_last_trade_date(symbol)
            missing_days = collector.get_stock_missing_days(symbol)
            sync_analysis[symbol] = {
                'last_date': last_date,
                'missing_days': missing_days
            }
        
        # 按缺失天数排序显示
        sorted_analysis = sorted(sync_analysis.items(), key=lambda x: x[1]['missing_days'])
        
        print(f"   {'股票代码':<10} {'最后日期':<12} {'缺失天数':<8} {'状态'}")
        print(f"   {'-'*10} {'-'*12} {'-'*8} {'-'*10}")
        
        for symbol, info in sorted_analysis:
            last_date = info['last_date'] or '无数据'
            missing_days = info['missing_days']
            
            if missing_days == 1:
                status = "✅ 最新"
            elif missing_days <= 3:
                status = "🟡 略旧"
            elif missing_days <= 7:
                status = "🟠 较旧"
            else:
                status = "🔴 很旧"
            
            print(f"   {symbol:<10} {str(last_date):<12} {missing_days:<8} {status}")
        
        # 3. 统计分布情况
        print("\n3️⃣ 同步状态分布统计:")
        all_missing_days = []
        for symbol in existing_symbols:
            missing_days = collector.get_stock_missing_days(symbol)
            all_missing_days.append(missing_days)
        
        # 分组统计
        groups = {
            '最新 (1天)': len([d for d in all_missing_days if d == 1]),
            '略旧 (2-3天)': len([d for d in all_missing_days if 2 <= d <= 3]),
            '较旧 (4-7天)': len([d for d in all_missing_days if 4 <= d <= 7]),
            '很旧 (>7天)': len([d for d in all_missing_days if d > 7])
        }
        
        total = len(all_missing_days)
        for group_name, count in groups.items():
            percentage = (count / total * 100) if total > 0 else 0
            print(f"   {group_name:<12}: {count:3d} 只股票 ({percentage:5.1f}%)")
        
        # 4. 演示智能同步的优势
        print("\n4️⃣ 智能同步 vs 统一同步对比:")
        
        # 统一同步方案
        unified_days = max(all_missing_days) if all_missing_days else 1
        unified_total_requests = len(existing_symbols) * unified_days
        
        # 智能同步方案
        smart_total_requests = sum(all_missing_days)
        
        print(f"   统一同步方案: 每只股票同步 {unified_days} 天")
        print(f"   - 总请求次数: {len(existing_symbols)} × {unified_days} = {unified_total_requests}")
        
        print(f"   智能同步方案: 按需同步")
        print(f"   - 总请求次数: {smart_total_requests}")
        
        savings = unified_total_requests - smart_total_requests
        savings_percent = (savings / unified_total_requests * 100) if unified_total_requests > 0 else 0
        
        print(f"   💡 节省请求: {savings} 次 ({savings_percent:.1f}%)")
        
        print("\n✅ 分析完成！智能增量同步的优势:")
        print("   • 自动检测每只股票的实际同步需求")
        print("   • 避免不必要的重复数据请求")
        print("   • 显著减少API调用次数")
        print("   • 提高同步效率和速度")
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        collector.close()

if __name__ == "__main__":
    test_per_stock_calculation()
