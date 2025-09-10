from stock_scoring_system import StockScoringSystem
def create_scorer_for_date(date_str):
    """为指定日期创建评分系统的便捷函数"""
    return StockScoringSystem(base_date=date_str)

def batch_score_multiple_dates(symbols, dates):
    """批量对多个日期进行评分"""
    results = {}
    
    for date in dates:
        print(f"\n📅 处理日期: {date}")
        scorer = StockScoringSystem(base_date=date)
        date_results = {}
        
        for symbol in symbols:
            score_data = scorer.calculate_stock_score(symbol)
            if score_data:
                date_results[symbol] = score_data
        
        results[date] = date_results
    
    return results

# 使用示例
if __name__ == "__main__":
    # 对比万科在不同日期的评分
    symbols = ["000002", "000001", "000858"]
    dates = ["20250902", "20250910", "20250911"]
    
    results = batch_score_multiple_dates(symbols, dates)
    
    # 显示结果
    for symbol in symbols:
        print(f"\n📊 {symbol} 评分变化:")
        for date in dates:
            if symbol in results[date]:
                score = results[date][symbol]['total_score']
                print(f"  {date}: {score:.2f}")