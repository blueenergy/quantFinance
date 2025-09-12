#!/usr/bin/env python3
"""
🎯 每日选股工具
自动筛选出高分股票，生成投资候选清单
"""

import pandas as pd
from stock_analyzer import StockAnalyzer
from datetime import datetime
import argparse

def daily_stock_screening(stock_list_file: str = "stock_list.txt", 
                         output_file: str = None,
                         min_score: float = 60.0,
                         top_n: int = 20):
    """每日选股筛选"""
    
    if output_file is None:
        today = datetime.now().strftime('%Y%m%d')
        output_file = f"daily_picks_{today}.csv"
    
    print(f"🔍 开始每日选股筛选...")
    print(f"📂 股票清单文件: {stock_list_file}")
    print(f"📊 最低评分要求: {min_score}")
    print(f"🎯 输出前 {top_n} 只股票")
    
    # 读取股票清单
    try:
        with open(stock_list_file, 'r', encoding='utf-8') as f:
            symbols = [line.strip() for line in f if line.strip()]
        print(f"📋 共读取 {len(symbols)} 只股票")
    except FileNotFoundError:
        print(f"❌ 找不到股票清单文件: {stock_list_file}")
        return
    
    # 创建分析器（这里需要真实的数据库连接）
    from unittest.mock import Mock
    mock_db = Mock()  # 实际使用时替换为真实数据库
    
    analyzer = StockAnalyzer(mock_db)
    
    # 批量分析
    results_df = analyzer.batch_analyze(symbols)
    
    # 筛选高分股票
    good_stocks = results_df[results_df['composite_score'] >= min_score]
    top_stocks = good_stocks.head(top_n)
    
    # 保存结果
    top_stocks.to_csv(output_file, index=False, encoding='utf-8-sig')
    
    print(f"\n📊 筛选结果:")
    print(f"✅ 符合条件股票: {len(good_stocks)} 只")
    print(f"🎯 推荐股票: {len(top_stocks)} 只")
    print(f"💾 结果已保存: {output_file}")
    
    # 显示前5只
    if len(top_stocks) > 0:
        print(f"\n🌟 Top 5 推荐:")
        for i, row in top_stocks.head(5).iterrows():
            print(f"  {i+1}. {row['symbol']}: {row['composite_score']:.2f}分 - {row['overall_rating']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='每日选股工具')
    parser.add_argument('--stock-list', default='stock_list.txt', help='股票清单文件')
    parser.add_argument('--output', help='输出文件名')
    parser.add_argument('--min-score', type=float, default=60.0, help='最低评分')
    parser.add_argument('--top-n', type=int, default=20, help='输出股票数量')
    
    args = parser.parse_args()
    
    daily_stock_screening(
        stock_list_file=args.stock_list,
        output_file=args.output,
        min_score=args.min_score,
        top_n=args.top_n
    )
