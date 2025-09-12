import os
from pymongo import MongoClient
from stock_scoring_system import StockScoringSystem
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

# 连接数据库
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client['finance']


def create_scorer_for_date(date_str):
    """为指定日期创建评分系统的便捷函数"""
    return StockScoringSystem(base_date=date_str)

def batch_score_multiple_dates(symbols, dates, save_to_db=True):
    """
    ✅ 改造版：批量对多个日期进行评分 - 使用高效的 score_specific_stocks 方法
    
    Args:
        symbols: 股票代码列表 (只评分这些股票)
        dates: 日期列表
        save_to_db: 是否保存到数据库
    """
    results = {}
    overall_stats = {
        "total_processed": 0,
        "successful": 0,
        "failed": 0,
        "updated": 0,
        "inserted": 0,
        "unchanged": 0
    }
    
    print(f"🎯 开始批量评分 {len(symbols)} 只股票，{len(dates)} 个日期")
    print(f"📋 股票列表: {symbols}")
    print(f"📅 日期列表: {dates}")
    
    for date in dates:
        print(f"\n📅 处理日期: {date}")
        
        try:
            # ✅ 创建评分系统实例
            scorer = StockScoringSystem(base_date=date)
            
            if save_to_db:
                # ✅ 使用新的 score_specific_stocks 方法 (自动保存)
                print(f"📊 对 {len(symbols)} 只股票进行评分并保存...")
                date_results = scorer.score_specific_stocks(symbols, save_to_db=True)
            else:
                # 只计算不保存 - 逐个计算
                print(f"📊 对 {len(symbols)} 只股票进行评分 (不保存)...")
                date_results = {}
                for symbol in symbols:
                    try:
                        score_data = scorer.calculate_stock_score(symbol)
                        if score_data:
                            date_results[symbol] = score_data
                    except Exception as e:
                        print(f"❌ 计算股票 {symbol} 评分失败: {e}")
            
            # 统计当日结果
            successful_count = len([s for s in date_results if date_results[s] is not None])
            failed_count = len(symbols) - successful_count
            
            overall_stats["total_processed"] += len(symbols)
            overall_stats["successful"] += successful_count
            overall_stats["failed"] += failed_count
            
            # 统计保存状态 (如果有的话)
            if save_to_db:
                for symbol, score_data in date_results.items():
                    if score_data and 'save_status' in score_data:
                        status = score_data['save_status']
                        if status in overall_stats:
                            overall_stats[status] += 1
            
            print(f"✅ {date} 完成: 成功 {successful_count}, 失败 {failed_count}")
            
        except Exception as e:
            print(f"❌ 处理日期 {date} 失败: {e}")
            # 所有股票都算失败
            overall_stats["total_processed"] += len(symbols)
            overall_stats["failed"] += len(symbols)
            date_results = {}
        
        results[date] = date_results
    
    # 打印总体统计信息
    print(f"\n📊 总体处理统计:")
    print(f"  总处理: {overall_stats['total_processed']}")
    print(f"  成功: {overall_stats['successful']}")
    print(f"  失败: {overall_stats['failed']}")
    
    if save_to_db:
        print(f"  新增: {overall_stats['inserted']}")
        print(f"  更新: {overall_stats['updated']}")
        print(f"  无变化: {overall_stats['unchanged']}")
        
        # 计算成功率
        if overall_stats["total_processed"] > 0:
            success_rate = (overall_stats["successful"] / overall_stats["total_processed"]) * 100
            print(f"  成功率: {success_rate:.1f}%")
    
    return results

def batch_score_selected_stocks_smart(symbols, dates, save_to_db=True, force_recalculate=False):
    """
    🚀 智能批量评分：先检查已有评分，只计算需要的
    
    Args:
        symbols: 股票代码列表
        dates: 日期列表
        save_to_db: 是否保存到数据库
        force_recalculate: 是否强制重新计算
    """
    print(f"🚀 智能批量评分开始")
    
    if not force_recalculate and save_to_db:
        # 检查哪些需要计算
        need_calculation = check_existing_scores(symbols, dates)
        
        if not need_calculation:
            print("✅ 所有股票评分都已存在，无需计算")
            return get_existing_scores(symbols, dates)
        
        # 按日期分组需要计算的股票
        by_date = {}
        for symbol, date in need_calculation:
            if date not in by_date:
                by_date[date] = []
            by_date[date].append(symbol)
        
        print(f"📊 需要计算的组合: {len(need_calculation)}")
        
        # 只对需要计算的进行评分
        results = {}
        for date, date_symbols in by_date.items():
            print(f"\n📅 处理日期 {date}，股票: {date_symbols}")
            scorer = StockScoringSystem(base_date=date)
            date_results = scorer.score_specific_stocks(date_symbols, save_to_db=True)
            results[date] = date_results
        
        # 合并已有的评分结果
        existing_results = get_existing_scores(symbols, dates)
        for date in dates:
            if date in existing_results and date in results:
                results[date].update(existing_results[date])
            elif date in existing_results and date not in results:
                results[date] = existing_results[date]
        
        return results
    else:
        # 强制重新计算或者不保存到数据库
        return batch_score_multiple_dates(symbols, dates, save_to_db)

def check_existing_scores(symbols, dates):
    """
    检查哪些股票已经有评分，避免重复计算
    
    Args:
        symbols: 股票代码列表
        dates: 日期列表
        
    Returns:
        需要计算的 (symbol, date) 组合列表
    """
    try:
        collection = db['stock_scores']
        need_calculation = []
        existing_count = 0
        
        for date in dates:
            for symbol in symbols:
                # 检查是否已存在评分
                existing = collection.find_one({
                    "symbol": symbol,
                    "score_date": date
                })
                
                if existing:
                    existing_count += 1
                    print(f"✅ {symbol} {date} 评分已存在")
                else:
                    need_calculation.append((symbol, date))
                    print(f"📊 {symbol} {date} 需要计算")
        
        print(f"\n📊 检查结果:")
        print(f"  已存在评分: {existing_count}")
        print(f"  需要计算: {len(need_calculation)}")
        
        return need_calculation
        
    except Exception as e:
        print(f"❌ 检查现有评分失败: {e}")
        # 如果检查失败，返回所有组合
        return [(symbol, date) for date in dates for symbol in symbols]

def get_existing_scores(symbols, dates):
    """
    获取已存在的评分数据
    
    Args:
        symbols: 股票代码列表
        dates: 日期列表
        
    Returns:
        按日期分组的评分结果
    """
    try:
        collection = db['stock_scores']
        results = {}
        
        for date in dates:
            date_results = {}
            
            # 查询该日期的所有指定股票评分
            cursor = collection.find({
                "symbol": {"$in": symbols},
                "score_date": date
            })
            
            for doc in cursor:
                symbol = doc.get("symbol")
                if symbol:
                    # 移除 MongoDB 特有的 _id 字段
                    if '_id' in doc:
                        del doc['_id']
                    date_results[symbol] = doc
            
            results[date] = date_results
        
        return results
        
    except Exception as e:
        print(f"❌ 获取已有评分失败: {e}")
        return {}

def clean_duplicate_scores(date_str=None, db_name='finance', collection_name='stock_scores'):
    """
    清理重复的评分记录
    
    Args:
        date_str: 指定日期，为空则处理所有日期
        db_name: 数据库名称，默认 'finance'
        collection_name: 集合名称，默认 'stock_scores'
    """
    try:
        # 使用传入的数据库和集合名称
        target_db = client[db_name]
        collection = target_db[collection_name]
        
        # 构建查询条件
        match_stage = {}
        if date_str:
            match_stage["score_date"] = date_str
        
        # 查找重复记录
        pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": {
                        "symbol": "$symbol",
                        "score_date": "$score_date"
                    },
                    "count": {"$sum": 1},
                    "docs": {"$push": "$_id"}
                }
            },
            {
                "$match": {
                    "count": {"$gt": 1}
                }
            }
        ]
        
        duplicates = list(collection.aggregate(pipeline))
        
        if not duplicates:
            print("✅ 没有发现重复记录")
            return {"cleaned": 0, "duplicates_found": 0}
        
        print(f"🔍 发现 {len(duplicates)} 组重复记录")
        
        deleted_count = 0
        for duplicate in duplicates:
            symbol = duplicate["_id"]["symbol"]
            score_date = duplicate["_id"]["score_date"]
            doc_ids = duplicate["docs"]
            
            # 保留最新的记录，删除其他的
            docs_to_delete = doc_ids[1:]  # 保留第一个，删除其余的
            
            if docs_to_delete:
                result = collection.delete_many({"_id": {"$in": docs_to_delete}})
                deleted_count += result.deleted_count
                print(f"🗑️ 删除 {symbol} {score_date} 的 {result.deleted_count} 条重复记录")
        
        print(f"✅ 清理完成，共删除 {deleted_count} 条重复记录")
        
        return {
            "cleaned": deleted_count,
            "duplicates_found": len(duplicates)
        }
        
    except Exception as e:
        print(f"❌ 清理重复记录失败: {e}")
        return {"cleaned": 0, "duplicates_found": 0, "error": str(e)}

def verify_data_integrity(date_str=None, db_name='finance', collection_name='stock_scores'):
    """
    验证数据完整性
    
    Args:
        date_str: 指定日期，为空则检查所有日期
        db_name: 数据库名称，默认 'finance'
        collection_name: 集合名称，默认 'stock_scores'
    """
    try:
        # 使用传入的数据库和集合名称
        target_db = client[db_name]
        collection = target_db[collection_name]
        
        # 构建查询条件
        match_stage = {}
        if date_str:
            match_stage["score_date"] = date_str
        
        # 检查重复记录
        pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": {
                        "symbol": "$symbol",
                        "score_date": "$score_date"
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$match": {
                    "count": {"$gt": 1}
                }
            }
        ]
        
        duplicates = list(collection.aggregate(pipeline))
        
        if duplicates:
            print(f"⚠️ 发现 {len(duplicates)} 组重复记录")
            for dup in duplicates[:5]:  # 只显示前5个
                print(f"  {dup['_id']['symbol']} {dup['_id']['score_date']}: {dup['count']} 条")
        else:
            print("✅ 数据完整性检查通过，无重复记录")
        
        # 统计记录数
        total_count = collection.count_documents(match_stage)
        print(f"📊 总记录数: {total_count}")
        
        if date_str:
            unique_symbols = len(collection.distinct("symbol", match_stage))
            print(f"📊 {date_str} 独立股票数: {unique_symbols}")
        
        return {
            "duplicates_found": len(duplicates),
            "total_records": total_count,
            "unique_symbols": len(collection.distinct("symbol", match_stage)) if date_str else None
        }
        
    except Exception as e:
        print(f"❌ 数据完整性检查失败: {e}")
        return {"error": str(e)}


# 使用示例
if __name__ == "__main__":
    # ✅ 先清理可能存在的重复记录
    print("🧹 清理重复记录...")
    clean_duplicate_scores()
    
    # ✅ 验证数据完整性
    print("\n🔍 验证数据完整性...")
    verify_data_integrity()
    
    # ✅ 测试新的批量评分方法
    symbols = ["000002", "000001", "000858"]
    dates = ["20250910", "20250911"]
    
    # print("\n🎯 测试改造后的批量评分...")
    results = batch_score_multiple_dates(symbols, dates, save_to_db=True)
    
    print("\n🚀 测试智能批量评分...")
    smart_results = batch_score_selected_stocks_smart(symbols, dates, save_to_db=True)
    
    # 显示结果
    print(f"\n📊 评分结果总览:")
    for date in dates:
        print(f"\n📅 {date}:")
        if date in results:
            for symbol in symbols:
                if symbol in results[date] and results[date][symbol]:
                    score = results[date][symbol].get('total_score', 0)
                    print(f"  {symbol}: {score:.2f}")
                else:
                    print(f"  {symbol}: 评分失败")
    
    # ✅ 最终验证
    print("\n🔍 最终数据完整性验证...")
    verify_data_integrity()