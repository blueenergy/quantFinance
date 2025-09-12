#!/usr/bin/env python3
"""调试技术面评分器测试问题"""

import sys
import os

# 添加路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

def debug_technical_scorer():
    """调试技术面评分器的空头形态评分"""
    
    try:
        from tests.test_technical_scorer import TestTechnicalScorer
        
        test_class = TestTechnicalScorer()
        test_class.setup_method()
        
        print("🔍 调试技术面评分器空头形态评分...")
        
        # 生成空头形态数据
        bearish_data = test_class._generate_bearish_pattern_data()
        test_class.mock_db_manager.get_volume_price_data.return_value = bearish_data
        
        scorer = test_class.TechnicalScorer(test_class.mock_db_manager, test_class.base_date)
        
        # 获取详细评分
        score, details = scorer.calculate_score_detailed("000002")
        
        print(f"空头形态评分: {score:.2f}")
        print("详细信息:")
        for key, value in details.items():
            if isinstance(value, dict):
                print(f"  {key}:")
                for sub_key, sub_value in value.items():
                    print(f"    {sub_key}: {sub_value}")
            else:
                print(f"  {key}: {value}")
        
        # 检查期望值
        print(f"\n期望: score < 50")
        print(f"实际: {score}")
        print(f"测试结果: {'通过' if score < 50 else '失败'}")
        
        # 如果测试失败，分析原因
        if score >= 50:
            print("\n❌ 问题分析:")
            print("空头形态数据生成的评分仍然 >= 50")
            
            # 分析价格数据
            print(f"\n价格数据分析:")
            recent_prices = [item['close'] for item in bearish_data[-20:]]
            print(f"最近20天收盘价前10天: {recent_prices[:10]}")
            print(f"最近20天收盘价后10天: {recent_prices[10:]}")
            
            first_price = recent_prices[0]
            last_price = recent_prices[-1]
            change_rate = (last_price - first_price) / first_price
            print(f"20天价格变化率: {change_rate:.2%}")
            
            # 分析均线数据
            import pandas as pd
            df = pd.DataFrame(bearish_data)
            df['close'] = pd.to_numeric(df['close'])
            
            # 计算均线
            df['ma5'] = df['close'].rolling(5).mean()
            df['ma10'] = df['close'].rolling(10).mean()
            df['ma20'] = df['close'].rolling(20).mean()
            
            current = df.iloc[-1]
            print(f"\n均线分析:")
            print(f"当前价格: {current['close']:.2f}")
            print(f"MA5: {current['ma5']:.2f}")
            print(f"MA10: {current['ma10']:.2f}")
            print(f"MA20: {current['ma20']:.2f}")
            
            # 检查均线排列
            if current['ma5'] > current['ma10'] > current['ma20']:
                print("⚠️ 发现问题：仍然是多头排列!")
            elif current['ma5'] < current['ma10'] < current['ma20']:
                print("✅ 均线排列正确：空头排列")
            else:
                print("📊 均线排列：混乱状态")
            
            # 分析各个评分组件
            if 'details' in locals() and isinstance(details, dict):
                print(f"\n评分组件分析:")
                for component in ['均线系统', '量价配合', '技术指标', '趋势强度']:
                    if component in details and isinstance(details[component], dict):
                        component_score = details[component].get(f'{component}得分', 'N/A')
                        print(f"{component}: {component_score}")
        
        return score < 50
        
    except Exception as e:
        print(f"❌ 调试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def suggest_fixes():
    """建议修复方案"""
    print("\n🔧 建议的修复方案:")
    print("A. 增强空头数据生成（让下降更明显）")
    print("   - 增加下降幅度")
    print("   - 确保均线空头排列")
    print("   - 减少成交量")
    
    print("B. 调整测试期望值")
    print("   - 将 assert score < 50 改为 assert score < 55")
    
    print("C. 检查技术面评分算法")
    print("   - 检查基础分设置")
    print("   - 检查各组件权重")
    print("   - 检查评分逻辑")

if __name__ == "__main__":
    success = debug_technical_scorer()
    if not success:
        suggest_fixes()