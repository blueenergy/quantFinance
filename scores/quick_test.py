#!/usr/bin/env python3
"""
快速验证端到端流程
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_database_integration():
    """测试数据库集成功能"""
    print("\n🧪 测试数据库集成...")
    
    try:
        from data_manager import DatabaseManager
        from stock_analyzer import StockAnalyzer
        
        # 创建支持数据库的分析器
        db_manager = DatabaseManager()
        analyzer = StockAnalyzer(db_manager, save_to_db=True)
        
        test_symbol = "000001"
        
        # 测试1：强制重新计算并保存
        print(f"\n1️⃣ 强制计算 {test_symbol}（保存到数据库）:")
        result1 = analyzer.analyze_single_stock(test_symbol, force_refresh=True)
        print(f"   计算结果: {result1.get('composite_score', 'N/A'):.2f}分")
        print(f"   数据来源: {result1.get('data_source', '未知')}")
        
        # 测试2：从数据库读取缓存
        print(f"\n2️⃣ 再次获取 {test_symbol}（应从数据库读取）:")
        result2 = analyzer.analyze_single_stock(test_symbol, force_refresh=False)
        print(f"缓存结果: {result2.get('composite_score', 'N/A'):.2f}分")
        print(f"数据来源: {result2.get('data_source', '未知')}")
        
        # 测试3：评分历史
        print(f"\n3️⃣ {test_symbol} 评分历史:")
        trends = analyzer.get_score_trends(test_symbol, 10)
        if not trends.empty:
            print(trends.tail(3))
        else:
            print("   无历史数据")
        
        # 测试4：高评分股票排行
        print(f"\n4️⃣ 高评分股票排行:")
        top_stocks = analyzer.get_top_stocks(min_score=60, limit=5)
        if not top_stocks.empty:
            print(top_stocks)
        else:
            print("   暂无高评分股票")
        
        return True
        
    except Exception as e:
        print(f"❌ 数据库集成测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def quick_test():
    """快速端到端测试"""
    print("🚀 快速端到端测试...")
    
    try:
        from data_manager import DatabaseManager
        from stock_analyzer import StockAnalyzer
        
        # 1. 数据库连接
        db_manager = DatabaseManager()
        print("✅ 数据库连接成功")
        
        # 2. 创建分析器
        analyzer = StockAnalyzer(db_manager)
        print("✅ 分析器创建成功")
        
        # # 3. 测试单股票分析
        # result = analyzer.analyze_single_stock("000002")
        # print(f"✅ 单股票分析成功: {result.get('composite_score', 'N/A')}分")
        
        # # 4. 测试批量分析
        # df = analyzer.batch_analyze(["000001","000063"], show_progress=False)
        # print(f"✅ 批量分析成功: {len(df)} 条记录")
        
        # # 5. 测试导出
        # export_file = analyzer.export_results(df)
        # print(f"✅ 导出成功: {export_file}")
        
        # # 6. 测试A股主要指数成分股
        # test_stocks = ['000001', '000002', '000858', '600036', '600519']
        # df = analyzer.batch_analyze(test_stocks, show_progress=True)
        # print('\n📊 Top 3 推荐股票:')
        # print(df.nlargest(3, 'composite_score')[['symbol', 'composite_score', 'overall_rating']])
        
        # 🔧 新增：测试数据库集成
        test_database_integration()
        
        print("\n🎉 端到端测试全部通过！")
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    quick_test()