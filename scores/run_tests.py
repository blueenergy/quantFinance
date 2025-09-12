#!/usr/bin/env python3
"""统一的评分系统测试运行脚本"""

import sys
import os
import argparse

# 添加路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

TEST_MAPPING = {
    "fundamental": ("基本面评分器", "test_fundamental_scorer", "test_run_all_fundamental_tests"),
    "cycle": ("周期评分器", "test_cycle_scorer", "test_run_all_cycle_tests"),
    "technical": ("技术面评分器", "test_technical_scorer", "test_run_all_technical_tests"),
    "money_flow": ("资金流评分器", "test_money_flow_scorer", "test_run_all_money_flow_tests"),
    "growth": ("成长性评分器", "test_growth_scorer", "test_run_all_growth_tests"),
    "stock_analyzer": ("股票分析器", "test_stock_analyzer", "test_run_all_stock_analyzer_tests"),
    "value": ("价值评分器", "test_value_scorer", "test_run_all_value_scorer_tests")
}

# 🔧 全局方法映射配置
METHOD_MAPPINGS = {
    "technical": {
        "bearish": "test_calculate_score_with_bearish_pattern",
        "bullish": "test_calculate_score_with_bullish_pattern", 
        "basic": ["test_technical_scorer_import", "test_technical_scorer_initialization"],
        "detailed": "test_calculate_score_detailed",
        "ma": "test_ma_system_calculation",
        "rsi": "test_rsi_calculation"
    },
    "fundamental": {
        "basic": ["test_fundamental_scorer_import", "test_fundamental_scorer_initialization"],
        "detailed": "test_calculate_score_detailed",
        "roe": "test_roe_calculation",
        "growth": "test_growth_rate_calculation"
    },
    "cycle": {
        "basic": ["test_cycle_scorer_import", "test_cycle_scorer_initialization"],
        "uptrend": "test_calculate_score_with_uptrend_data",
        "downtrend": "test_calculate_score_with_downtrend_data",
        "detailed": "test_calculate_score_detailed"
    },
    "money_flow": {
        "basic": ["test_money_flow_scorer_import", "test_money_flow_scorer_initialization"],
        "positive": "test_calculate_score_with_positive_flow",
        "negative": "test_calculate_score_with_negative_flow",
        "detailed": "test_calculate_score_detailed",
        "mainflow": "test_main_flow_calculation",
        "activity": "test_activity_score_calculation",
        "nodata": ["test_missing_data_columns", "test_no_data"]
    },
    "growth": {
        "basic": ["test_growth_scorer_import", "test_growth_scorer_initialization"],
        "high_growth": "test_calculate_score_with_high_growth_data",
        "low_growth": ["test_calculate_score_with_low_growth_data","test_calculate_score_with_very_low_growth_data"],
        "comparison":"test_growth_comparison",
        "detailed": "test_calculate_score_detailed",
        "revenue": "test_revenue_growth_calculation",
        "profit": "test_profit_growth_calculation",
        "roe": "test_roe_growth_calculation",
        "nodata": ["test_insufficient_data", "test_no_data"]
    },
    "stock_analyzer": {
        "basic": ["test_stock_analyzer_initialization", "test_default_date_handling"],
        "weights": ["test_set_custom_weights_valid", "test_create_conservative_analyzer"],
        "analysis": ["test_analyze_stock_success", "test_batch_analyze_success"],
        "recommendations": ["test_generate_recommendations_high_score", "test_generate_recommendations_low_score"],
        "reports": ["test_export_analysis_report"],
        "utils": ["test_is_st_stock", "test_normalize_date"],
        "errors": ["test_analyze_stock_invalid_symbol"]
    },
    "value": {
        "basic": ["test_value_scorer_initialization"],
        "pe": ["test_pe_score_calculation", "test_pe_score_edge_cases"],
        "pb": ["test_pb_score_calculation"],
        "dividend": ["test_dividend_score_calculation", "test_dividend_stability_adjustment"],
        "peg": ["test_peg_score_calculation", "test_peg_score_edge_cases"],
        "comprehensive": ["test_calculate_score_detailed_success", "test_calculate_score_simple"],
        "data": ["test_get_valuation_data_success", "test_get_financial_data_success"],
        "relative": ["test_relative_valuation_calculation"],
        "errors": ["test_calculation_with_database_error", "test_partial_data_handling"]
    }
}

# 🔧 测试类名映射
TEST_CLASS_MAPPING = {
    "technical": "TestTechnicalScorer",
    "fundamental": "TestFundamentalScorer",
    "cycle": "TestCycleScorer",
    "money_flow": "TestMoneyFlowScorer",
    "growth": "TestGrowthScorer",
    "stock_analyzer": "TestStockAnalyzer",
    "value": "TestValueScorer"
}
# 🔧 测试类名映射
TEST_CLASS_MAPPING = {
    "technical": "TestTechnicalScorer",
    "fundamental": "TestFundamentalScorer",
    "cycle": "TestCycleScorer",
    "money_flow": "TestMoneyFlowScorer",
    "growth": "TestGrowthScorer",
    "stock_analyzer": "TestStockAnalyzer",
    "value": "TestValueScorer"
}

def run_single_test_method(test_name, method_name=None):
    """运行单个测试或测试中的特定方法"""
    
    if test_name not in TEST_MAPPING:
        print(f"❌ 未知的测试名称: {test_name}")
        print(f"可用的测试: {', '.join(TEST_MAPPING.keys())}")
        return False
    
    name, module_name, default_function = TEST_MAPPING[test_name]
    
    # 🔧 如果指定了具体方法，运行特定方法
    if method_name:
        return run_specific_method(test_name, name, module_name, method_name)
    else:
        # 运行默认的全套测试
        return run_single_test(test_name)

def run_specific_method(test_name, name, module_name, method_name):
    """运行指定的测试方法"""
    
    print(f"\\n🧪 运行 {name} - {method_name} 测试...")
    print(f"{'='*50}")
    
    try:
        # 动态导入测试模块
        module = __import__(f"tests.{module_name}", fromlist=["TestClass"])
        
        # 获取测试类和方法映射
        if test_name not in TEST_CLASS_MAPPING:
            print(f"❌ 未知的测试类型: {test_name}")
            return False
        
        test_class_name = TEST_CLASS_MAPPING[test_name]
        method_mapping = METHOD_MAPPINGS[test_name]
        
        test_class = getattr(module, test_class_name)
        test_instance = test_class()
        test_instance.setup_method()
        
        # 🔧 执行指定的方法
        if method_name in method_mapping:
            methods_to_run = method_mapping[method_name]
            
            # 如果是方法列表，逐个执行
            if isinstance(methods_to_run, list):
                for method in methods_to_run:
                    print(f"  🔍 执行: {method}")
                    getattr(test_instance, method)()
                    print(f"  ✅ {method} 通过")
            else:
                # 单个方法
                print(f"  🔍 执行: {methods_to_run}")
                getattr(test_instance, methods_to_run)()
                print(f"  ✅ {methods_to_run} 通过")
        else:
            # 尝试直接运行方法名
            if hasattr(test_instance, method_name):
                print(f"  🔍 执行: {method_name}")
                getattr(test_instance, method_name)()
                print(f"  ✅ {method_name} 通过")
            else:
                print(f"❌ 方法 '{method_name}' 不存在")
                available_methods = [k for k in method_mapping.keys()]
                print(f"可用的方法: {', '.join(available_methods)}")
                return False
        
        print(f"✅ {name} - {method_name} 测试通过！")
        return True
        
    except ImportError as e:
        print(f"❌ 无法导入 {module_name}: {e}")
        return False
        
    except Exception as e:
        print(f"❌ {name} - {method_name} 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def run_single_test(test_name):
    """运行单个测试的所有方法"""
    if test_name not in TEST_MAPPING:
        print(f"❌ 未知的测试名称: {test_name}")
        print(f"可用的测试: {', '.join(TEST_MAPPING.keys())}")
        return False
    
    name, module_name, function_name = TEST_MAPPING[test_name]
    
    print(f"\n🧪 运行 {name} 完整测试...")
    print(f"{'='*50}")
    
    try:
        # 动态导入并运行测试
        module = __import__(f"tests.{module_name}", fromlist=[function_name])
        test_function = getattr(module, function_name)
        test_function()
        print(f"✅ {name} 测试通过！")
        return True
        
    except ImportError as e:
        print(f"❌ 无法导入 {module_name}: {e}")
        return False
        
    except Exception as e:
        print(f"❌ {name} 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def run_all_tests():
    """运行所有评分器测试"""
    
    print("🚀 开始运行所有评分器测试...")
    print(f"{'='*60}")
    
    results = {}
    
    for test_key, (name, _, _) in TEST_MAPPING.items():
        success = run_single_test(test_key)
        results[name] = "✅ 通过" if success else "❌ 失败"
    
    # 输出测试总结
    print(f"\\n{'='*60}")
    print("📊 测试结果总结")
    print(f"{'='*60}")
    
    for name, result in results.items():
        print(f"{name}: {result}")
    
    # 统计通过率
    passed = len([r for r in results.values() if r.startswith("✅")])
    total = len(results)
    pass_rate = (passed / total) * 100 if total > 0 else 0
    
    print(f"\\n📈 总体通过率: {passed}/{total} ({pass_rate:.1f}%)")
    
    if passed == total:
        print("🎉 所有测试都通过了！")
        return True
    else:
        print("⚠️ 部分测试需要检查和修复")
        return False

def list_available_tests():
    """列出可用的测试"""
    print("📋 可用的测试:")
    print("-" * 50)
    for key, (name, _, _) in TEST_MAPPING.items():
        print(f"  {key:<12} - {name}")
    
    print("\\n📋 各评分器可用的单个方法:")
    print("-" * 50)
    
    for test_name, methods in METHOD_MAPPINGS.items():
        test_display_name = TEST_MAPPING[test_name][0]
        print(f"\\n🔧 {test_display_name} ({test_name}):")
        for method, _ in methods.items():
            print(f"  {test_name}:{method:<12}")
    
    print("\\n💡 使用示例:")
    print("  python run_tests.py                        # 运行所有测试")
    print("  python run_tests.py technical              # 运行技术面完整测试")
    print("  python run_tests.py technical:bearish      # 只运行技术面空头测试")
    print("  python run_tests.py growth:high_growth     # 只运行成长性高增长测试")
    print("  python run_tests.py money_flow:positive    # 只运行资金流正向测试")
    print("  python run_tests.py -l                     # 列出可用测试")

def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="评分系统测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python run_tests.py                        # 运行所有测试
  python run_tests.py technical              # 运行技术面完整测试
  python run_tests.py technical:bearish      # 只运行技术面空头测试
  python run_tests.py technical:bullish      # 只运行技术面多头测试
  python run_tests.py fundamental:basic      # 只运行基本面基础测试
  python run_tests.py cycle:downtrend        # 只运行周期下降趋势测试
  python run_tests.py -l                     # 列出所有可用测试和方法
        """
    )
    
    parser.add_argument(
        'tests', 
        nargs='*', 
        help='要运行的测试名称，格式: test_name 或 test_name:method_name'
    )
    
    parser.add_argument(
        '-l', '--list', 
        action='store_true',
        help='列出所有可用的测试和方法'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='详细输出模式'
    )
    
    args = parser.parse_args()
    
    # 列出可用测试
    if args.list:
        list_available_tests()
        return
    
    # 如果没有指定测试，运行所有测试
    if not args.tests:
        success = run_all_tests()
        sys.exit(0 if success else 1)
    
    # 🔧 解析测试参数，支持 test_name:method_name 格式
    results = []
    for test_spec in args.tests:
        if ':' in test_spec:
            # 包含方法名
            test_name, method_name = test_spec.split(':', 1)
            success = run_single_test_method(test_name, method_name)
        else:
            # 只有测试名
            success = run_single_test(test_spec)
        
        results.append(success)
    
    # 如果所有指定测试都通过，退出码为0
    all_passed = all(results)
    
    if len(args.tests) > 1:
        passed = sum(results)
        total = len(results)
        print(f"\n📈 指定测试通过率: {passed}/{total}")
        
        if all_passed:
            print("🎉 所有指定测试都通过了！")
        else:
            print("⚠️ 部分指定测试失败")
    
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    main()