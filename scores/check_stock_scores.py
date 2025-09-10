from stock_scoring_system import StockScoringSystem
scorer = StockScoringSystem()


# 示例5: 查看Top股票
print("\n🔍 示例5: 查看Top 10股票")
top_stocks = scorer.get_top_stocks(limit=100)
for i, stock in enumerate(top_stocks, 1):
    print(f"{i}. {stock['symbol']} - {stock['name']} | 评分: {stock['total_score']}")

# 示例6: 历史评分对比
print("\n🔍 示例6: 万科历史评分")
history = scorer.get_stock_score_history("000002", days=10)
for record in history:
    print(f"{record['score_date']}: {record['total_score']:.2f}")
