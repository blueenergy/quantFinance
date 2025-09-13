#!/usr/bin/env python3
"""
简单测试 StockAnalyzer
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_manager import DatabaseManager
from scores.stock_analyzer import StockAnalyzer

def simple_test():
    """简单测试"""
    print("🧪 简单测试 StockAnalyzer...")
    
    try:
        # 创建数据库连接
        db_manager = DatabaseManager()
        print("✅ 数据库连接成功")
        
        # 创建分析器
        analyzer = StockAnalyzer(db_manager)
        print("✅ 分析器创建成功")
        
        # 测试一只股票
        test_symbol = "000001"
        print(f"\n🔍 测试分析股票: {test_symbol}")
        
        result = analyzer.analyze_single_stock(test_symbol)
        
        print(f"✅ 分析结果:")
        print(f"   股票代码: {result.get('symbol', 'N/A')}")
        print(f"   综合评分: {result.get('composite_score', 'N/A')}")
        print(f"   投资评级: {result.get('overall_rating', 'N/A')}")
        
        # 显示各维度评分
        print(f"\n📊 各维度评分:")
        for key, value in result.items():
            if key.endswith('_score'):
                scorer_name = key.replace('_score', '')
                print(f"   {scorer_name:12}: {value:6.2f}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    simple_test()