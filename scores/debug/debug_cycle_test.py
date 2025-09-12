#!/usr/bin/env python3
"""调试周期评分器测试问题"""

import sys
import os

# 添加路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

def debug_cycle_scorer():
    """调试周期评分器的下降趋势评分"""
    
    try:
        from tests.test_cycle_scorer import TestCycleScorer
        
        test_class = TestCycleScorer()
        test_class.setup_method()
        
        # 测试下降趋势数据
        print("🔍 调试下降趋势评分...")
        
        # 生成下降趋势数据
        downtrend_data = test_class._generate_downtrend_data()
        test_class.mock_db_manager.get_volume_price_data.return_value = downtrend_data
        
        scorer = test_class.CycleScorer(test_class.mock_db_manager, test_class.base_date)
        
        # 获取详细评分看看具体数值
        score, details = scorer.calculate_score_detailed("000002")
        
        print(f"下降趋势评分: {score:.2f}")
        print("详细信息:")
        for key, value in details.items():
            print(f"  {key}: {value}")
        
        # 检查期望值
        print(f"\n期望: score < 50")
        print(f"实际: {score}")
        print(f"测试结果: {'通过' if score < 50 else '失败'}")
        
        # 如果测试失败，我们需要调整测试数据或评分逻辑
        if score >= 50:
            print("\n❌ 问题分析:")
            print("下降趋势数据生成的评分仍然 >= 50")
            print("可能的原因:")
            print("1. 下降趋势不够明显")
            print("2. 评分算法对下降的惩罚不够")
            print("3. 基础分太高")
            
            # 让我们看看具体的价格数据
            print(f"\n价格数据分析 (最近10天):")
            recent_prices = [item['close'] for item in downtrend_data[-10:]]
            print(f"最近10天收盘价: {recent_prices}")
            
            first_price = recent_prices[0]
            last_price = recent_prices[-1]
            change_rate = (last_price - first_price) / first_price
            print(f"10天价格变化率: {change_rate:.2%}")
        
        return score < 50
        
    except Exception as e:
        print(f"❌ 调试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    debug_cycle_scorer()