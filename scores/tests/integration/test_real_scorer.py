#!/usr/bin/env python3
"""
使用真实数据测试单个评分器
"""

import sys
import os
# 🔧 简化的路径设置
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
print(f"📂 添加路径: {os.path.join(os.path.dirname(__file__), '..', '..')}")

def test_real_scorer(scorer_name, symbol="000001"):
    """
    使用真实数据测试评分器
    
    Args:
        scorer_name: 评分器名称
        symbol: 股票代码
    """
    print(f"🧪 使用真实数据测试 {scorer_name} 评分器...")
    
    try:
        from data_manager import DatabaseManager
        
        # 评分器映射
        scorer_modules = {
            'growth': ('scorers.growth_scorer', 'GrowthScorer'),
            'value': ('scorers.value_scorer', 'ValueScorer'),
            'technical': ('scorers.technical_scorer', 'TechnicalScorer'),
            'fundamental': ('scorers.fundamental_scorer', 'FundamentalScorer'),
            'money_flow': ('scorers.money_flow_scorer', 'MoneyFlowScorer'),
            'cycle': ('scorers.cycle_scorer', 'CycleScorer')
        }
        
        if scorer_name not in scorer_modules:
            print(f"❌ 未知评分器: {scorer_name}")
            print(f"可用评分器: {list(scorer_modules.keys())}")
            return False
        
        # 动态导入评分器
        module_path, class_name = scorer_modules[scorer_name]
        module = __import__(module_path, fromlist=[class_name])
        ScorerClass = getattr(module, class_name)
        
        # 创建数据库管理器
        db_manager = DatabaseManager()
        print("✅ 数据库连接成功")
        
        # 创建评分器
        scorer = ScorerClass(db_manager, "20240930")
        print(f"✅ {scorer_name} 评分器创建成功")
        
        # 检查数据可用性
        print(f"\n🔍 检查 {symbol} 的数据可用性...")
        
        # 根据评分器类型检查不同的数据
        if scorer_name == 'growth':
            data = scorer.get_financial_data(symbol, 3)
            print(f"📊 财务数据: {len(data)} 期")
            if data:
                print("📋 财务数据字段:", list(data[0].keys()))
                print("📅 时间范围:", f"{data[-1].get('report_date', 'N/A')} ~ {data[0].get('report_date', 'N/A')}")
        
        elif scorer_name == 'value':
            valuation_data = db_manager.get_valuation_data(symbol)
            financial_data = db_manager.get_financial_data(symbol, 1)
            print(f"💰 估值数据: {'✅' if valuation_data else '❌'}")
            print(f"📊 财务数据: {'✅' if financial_data else '❌'}")
            if valuation_data:
                print("💰 估值字段:", list(valuation_data.keys()))
        
        elif scorer_name in ['technical', 'money_flow']:
            price_data = db_manager.get_volume_price_data(symbol, 30)
            flow_data = db_manager.get_money_flow_data(symbol, 30)
            print(f"📈 价量数据: {len(price_data)} 条")
            print(f"💰 资金流数据: {len(flow_data)} 条")
            if price_data:
                print("📈 价量字段:", list(price_data[0].keys()))
        
        # 计算评分
        print(f"\n🎯 开始计算 {symbol} 的 {scorer_name} 评分...")
        score, details = scorer.calculate_score_detailed(symbol)
        
        print(f"\n✅ {scorer_name} 评分: {score:.2f}")
        print(f"📊 详细分析:")
        
        for category, info in details.items():
            print(f"\n📋 {category}:")
            if isinstance(info, dict):
                for key, value in info.items():
                    print(f"   {key}: {value}")
            else:
                print(f"   {info}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def batch_test_scorers(symbol="000001"):
    """批量测试所有评分器"""
    print(f"🧪 批量测试所有评分器 - {symbol}")
    
    scorers = ['growth', 'value', 'technical', 'fundamental', 'money_flow', 'cycle']
    results = {}
    
    for scorer_name in scorers:
        print(f"\n{'='*50}")
        success = test_real_scorer(scorer_name, symbol)
        results[scorer_name] = "✅" if success else "❌"
    
    print(f"\n{'='*50}")
    print("📊 测试结果汇总:")
    for scorer, result in results.items():
        print(f"   {scorer}: {result}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='使用真实数据测试评分器')
    parser.add_argument('scorer', nargs='?', 
                       choices=['growth', 'value', 'technical', 'fundamental', 'money_flow', 'cycle', 'all'],
                       default='growth', help='评分器名称 (默认: growth)')
    parser.add_argument('--symbol', default='000001', help='股票代码 (默认: 000001)')
    
    args = parser.parse_args()
    
    if args.scorer == 'all':
        batch_test_scorers(args.symbol)
    else:
        test_real_scorer(args.scorer, args.symbol)