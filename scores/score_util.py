import os
import sys
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv

# 添加当前目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

class ScoreUtil:
    """股票评分工具类 - 简化版本，直接使用 StockAnalyzer 和 DatabaseManager"""
    
    def __init__(self, base_date=None, use_tushare=False):
        """
        初始化评分工具
        
        Args:
            base_date: 基准日期，格式 YYYYMMDD
            use_tushare: 是否使用 Tushare API
        """
        # 设置基准日期
        if base_date:
            self.base_date = base_date
        else:
            self.base_date = datetime.now().strftime('%Y%m%d')
        
        # 🔧 直接使用 DatabaseManager 的连接
        self.db_manager = None
        self.analyzer = None
        self._init_components(use_tushare)
        
        print(f"✅ ScoreUtil 初始化完成，基准日期: {self.base_date}")
    
    def _init_components(self, use_tushare=False):
        """初始化组件"""
        try:
            from data_manager import DatabaseManager
            from stock_analyzer import StockAnalyzer
            
            # 🔧 创建数据库管理器
            self.db_manager = DatabaseManager()
            
            # 🔧 创建股票分析器（自动处理保存）
            self.analyzer = StockAnalyzer(
                db_manager=self.db_manager,
                base_date=self.base_date,
                save_to_db=True  # 让 StockAnalyzer 自己处理保存
            )
            
            print("✅ StockAnalyzer 和 DatabaseManager 初始化成功")
            
        except Exception as e:
            print(f"❌ 组件初始化失败: {e}")
            self.analyzer = None
            self.db_manager = None
    
    def score_all_stocks(self, base_date=None, exclude_st=True, batch_size=100, force_refresh=False):
        """
        🔧 简化：对数据库中所有股票进行批量评分
        
        Args:
            base_date: 基准日期
            exclude_st: 是否排除ST股票
            batch_size: 批处理大小
            force_refresh: 是否强制重新计算
            
        Returns:
            dict: 评分结果统计
        """
        if not self.analyzer or not self.db_manager:
            print("❌ 组件未初始化，无法进行评分")
            return {"error": "组件未初始化"}
        
        # 🔧 设置工作日期
        working_date = base_date or self.base_date
        original_base_date = self.analyzer.base_date
        
        if base_date:
            self.analyzer.base_date = base_date
            print(f"📅 临时使用基准日期: {base_date}")
        
        try:
            # 🔧 获取股票列表
            symbols = self._get_stock_symbols(exclude_st)
            
            if not symbols:
                print("❌ 未找到股票信息")
                return {"error": "未找到股票信息"}
            
            total_stocks = len(symbols)
            print(f"🎯 开始批量评分...")
            print(f"   总股票数: {total_stocks}")
            print(f"   基准日期: {working_date}")
            print(f"   排除ST股: {'是' if exclude_st else '否'}")
            print(f"   批处理大小: {batch_size}")
            print(f"   强制刷新: {'是' if force_refresh else '否'}")
            
            # 统计信息
            stats = {
                "total_stocks": total_stocks,
                "successful": 0,
                "failed": 0,
                "from_cache": 0,
                "newly_calculated": 0,
                "errors": [],
                "start_time": datetime.now(),
                "base_date": working_date
            }
            
            # 🔧 分批处理 - 简化版本
            for batch_start in range(0, total_stocks, batch_size):
                batch_end = min(batch_start + batch_size, total_stocks)
                batch_symbols = symbols[batch_start:batch_end]
                
                print(f"\n📊 处理批次 {batch_start//batch_size + 1}: 股票 {batch_start+1}-{batch_end}")
                
                self._process_symbol_batch(batch_symbols, stats, force_refresh)
                
                # 显示进度
                progress = (batch_end / total_stocks) * 100
                print(f"   进度: {progress:.1f}% ({batch_end}/{total_stocks})")
                print(f"   累计: 成功 {stats['successful']}, 失败 {stats['failed']}")
            
            # 🔧 完成统计
            stats["end_time"] = datetime.now()
            stats["duration"] = (stats["end_time"] - stats["start_time"]).total_seconds()
            
            self._print_final_stats(stats)
            
            return {"stats": stats}
            
        except Exception as e:
            print(f"❌ 批量评分过程出错: {e}")
            import traceback
            traceback.print_exc()
            return {"stats": {"error": str(e)}}
            
        finally:
            # 恢复原始基准日期
            if self.analyzer:
                self.analyzer.base_date = original_base_date
    
    def _get_stock_symbols(self, exclude_st=True):
        """获取股票代码列表"""
        try:
            # 🔧 构建查询条件
            query = {}
            if exclude_st:
                query["name"] = {
                    "$not": {
                        "$regex": "ST|\\*ST|S\\*ST|SST|N\\*ST|PT", 
                        "$options": "i"
                    }
                }
            
            # 🔧 只获取 symbol 字段
            stocks = list(self.db_manager.stock_info.find(query, {"symbol": 1, "name": 1}))
            
            # 🔧 进一步过滤ST股票
            symbols = []
            for stock in stocks:
                if exclude_st and self._is_st_stock(stock.get('name', '')):
                    continue
                symbols.append(stock['symbol'])
            
            print(f"📊 获取到 {len(symbols)} 只股票代码")
            return symbols
            
        except Exception as e:
            print(f"❌ 获取股票代码失败: {e}")
            return []
    
    def _process_symbol_batch(self, symbols, stats, force_refresh):
        """🔧 简化：处理一批股票代码"""
        for symbol in symbols:
            try:
                print(f"   📊 评分: {symbol}")
                
                # 🔧 直接使用 StockAnalyzer，它会自动处理保存
                result = self.analyzer.analyze_single_stock(symbol, force_refresh=force_refresh)
                
                if result and 'composite_score' in result:
                    stats["successful"] += 1
                    
                    # 统计数据来源
                    data_source = result.get('data_source', '')
                    if '缓存' in data_source:
                        stats["from_cache"] += 1
                    else:
                        stats["newly_calculated"] += 1
                    
                    print(f"     ✅ 评分: {result['composite_score']:.2f} ({data_source})")
                else:
                    stats["failed"] += 1
                    stats["errors"].append(f"{symbol}: 分析返回空结果")
                    print(f"     ❌ 分析返回空结果")
                    
            except Exception as e:
                stats["failed"] += 1
                error_msg = f"{symbol}: {str(e)}"
                stats["errors"].append(error_msg)
                print(f"     ❌ 评分失败: {e}")
    
    def score_specific_stocks(self, symbols, force_refresh=False):
        """
        🔧 简化：对指定股票进行评分
        
        Args:
            symbols: 股票代码列表
            force_refresh: 是否强制重新计算
            
        Returns:
            dict: 评分结果统计
        """
        if not self.analyzer:
            print("❌ StockAnalyzer 未初始化，无法进行评分")
            return {"error": "StockAnalyzer 未初始化"}
        
        print(f"🎯 开始对指定 {len(symbols)} 只股票进行评分")
        print(f"   强制刷新: {'是' if force_refresh else '否'}")
        
        stats = {"successful": 0, "failed": 0, "from_cache": 0, "newly_calculated": 0, "errors": []}
        results = {}
        
        for symbol in symbols:
            try:
                print(f"📊 评分股票: {symbol}")
                
                # 🔧 直接使用 StockAnalyzer，让它处理所有逻辑
                result = self.analyzer.analyze_single_stock(symbol, force_refresh=force_refresh)
                
                if result and 'composite_score' in result:
                    results[symbol] = result  # 🔧 直接使用原结果，不转换
                    stats["successful"] += 1
                    
                    # 统计数据来源
                    data_source = result.get('data_source', '')
                    if '缓存' in data_source:
                        stats["from_cache"] += 1
                    else:
                        stats["newly_calculated"] += 1
                    
                    print(f"✅ {symbol} 评分: {result['composite_score']:.2f} ({data_source})")
                else:
                    stats["failed"] += 1
                    stats["errors"].append(f"{symbol}: 分析返回空结果")
                    print(f"❌ {symbol} 评分失败: 返回空结果")
                    
            except Exception as e:
                stats["failed"] += 1
                error_msg = f"{symbol}: {str(e)}"
                stats["errors"].append(error_msg)
                print(f"❌ 评分 {symbol} 时出错: {e}")
        
        print(f"\n📊 指定股票评分完成:")
        print(f"   成功: {stats['successful']}")
        print(f"   失败: {stats['failed']}")
        print(f"   来自缓存: {stats['from_cache']}")
        print(f"   新计算: {stats['newly_calculated']}")
        
        return {"results": results, "stats": stats}
    
    def batch_score_multiple_dates(self, symbols, dates, force_refresh=False):
        """
        🔧 简化：批量对多个日期进行评分
        
        Args:
            symbols: 股票代码列表
            dates: 日期列表
            force_refresh: 是否强制重新计算
        """
        if not self.analyzer:
            print("❌ StockAnalyzer 未初始化，无法进行评分")
            return {"error": "StockAnalyzer 未初始化"}
        
        results = {}
        overall_stats = {
            "total_processed": 0,
            "successful": 0,
            "failed": 0,
            "from_cache": 0,
            "newly_calculated": 0
        }
        
        print(f"🎯 开始批量评分 {len(symbols)} 只股票，{len(dates)} 个日期")
        
        # 保存原始基准日期
        original_base_date = self.analyzer.base_date
        
        try:
            for date in dates:
                print(f"\n📅 处理日期: {date}")
                
                # 🔧 更新分析器的基准日期
                self.analyzer.base_date = date
                
                try:
                    # 🔧 使用简化后的方法
                    date_result = self.score_specific_stocks(symbols, force_refresh=force_refresh)
                    
                    if 'results' in date_result:
                        results[date] = date_result['results']
                        
                        # 更新总体统计
                        date_stats = date_result.get('stats', {})
                        overall_stats["total_processed"] += len(symbols)
                        overall_stats["successful"] += date_stats.get("successful", 0)
                        overall_stats["failed"] += date_stats.get("failed", 0)
                        overall_stats["from_cache"] += date_stats.get("from_cache", 0)
                        overall_stats["newly_calculated"] += date_stats.get("newly_calculated", 0)
                        
                        print(f"✅ {date} 完成: 成功 {date_stats.get('successful', 0)}, 失败 {date_stats.get('failed', 0)}")
                    else:
                        print(f"❌ {date} 处理失败")
                        overall_stats["total_processed"] += len(symbols)
                        overall_stats["failed"] += len(symbols)
                        results[date] = {}
                        
                except Exception as e:
                    print(f"❌ 处理日期 {date} 失败: {e}")
                    overall_stats["total_processed"] += len(symbols)
                    overall_stats["failed"] += len(symbols)
                    results[date] = {}
            
            # 打印总体统计信息
            print(f"\n📊 总体处理统计:")
            print(f"  总处理: {overall_stats['total_processed']}")
            print(f"  成功: {overall_stats['successful']}")
            print(f"  失败: {overall_stats['failed']}")
            print(f"  来自缓存: {overall_stats['from_cache']}")
            print(f"  新计算: {overall_stats['newly_calculated']}")
            
            if overall_stats["total_processed"] > 0:
                success_rate = (overall_stats["successful"] / overall_stats["total_processed"]) * 100
                print(f"  成功率: {success_rate:.1f}%")
            
            return results
            
        finally:
            # 恢复原始基准日期
            if self.analyzer:
                self.analyzer.base_date = original_base_date

    def get_top_stocks(self, limit=100, score_date=None, exclude_st=True, min_score=0):
        """
        🔧 增强版：获取评分最高的股票
        
        Args:
            limit: 返回数量限制
            score_date: 指定评分日期，默认使用最新
            exclude_st: 是否排除ST股票
            min_score: 最低评分要求
            
        Returns:
            list: 高评分股票列表，包含完整信息
        """
        try:
            # 构建查询条件
            query = {
                "$or": [
                    {"composite_score": {"$gte": min_score}},
                    {"total_score": {"$gte": min_score}}
                ]
            }
            
            if score_date:
                query["score_date"] = score_date
            else:
                # 🔧 获取最新评分数据
                latest_date = self._get_latest_score_date()
                if latest_date:
                    query["score_date"] = latest_date
                    print(f"📅 使用最新评分日期: {latest_date}")
                else:
                    print("⚠️ 未找到评分数据")
                    return []
            
            print(f"📊 查询高评分股票...")
            print(f"   评分日期: {query.get('score_date', '所有')}")
            print(f"   最低评分: {min_score}")
            print(f"   排除ST股: {'是' if exclude_st else '否'}")
            print(f"   数量限制: {limit}")
            
            # 🔧 使用聚合查询优化性能
            pipeline = [
                {"$match": query},
                {"$sort": {"composite_score": -1}}
            ]
            
            # 获取评分数据
            score_cursor = self.db_manager.stock_scores.aggregate(pipeline)
            
            # 🔧 关联股票基本信息并过滤
            filtered_stocks = []
            processed_count = 0
            
            for score_doc in score_cursor:
                processed_count += 1
                
                if len(filtered_stocks) >= limit:
                    break
                
                symbol = score_doc['symbol']
                
                # 获取股票基本信息
                stock_info = self.db_manager.stock_info.find_one({"symbol": symbol})
                
                if not stock_info:
                    continue
                
                name = stock_info.get('name', '')
                
                # 检查是否为ST股票
                if exclude_st and self._is_st_stock(name):
                    continue
                
                # 🔧 合并股票信息和评分信息
                stock_data = {
                    # 基本信息
                    'symbol': symbol,
                    'name': name,
                    'industry': stock_info.get('industry', ''),
                    'area': stock_info.get('area', ''),
                    'market': stock_info.get('market', ''),
                    'concept': stock_info.get('concept', ''),
                    
                    # 评分信息
                    'composite_score': score_doc.get('composite_score', 0),
                    'total_score': score_doc.get('total_score', 0),
                    'overall_rating': score_doc.get('overall_rating', ''),
                    'score_date': score_doc.get('score_date', ''),
                    
                    # 各维度评分
                    'technical_score': score_doc.get('technical_score', 0),
                    'fundamental_score': score_doc.get('fundamental_score', 0),
                    'money_flow_score': score_doc.get('money_flow_score', 0),
                    'value_score': score_doc.get('value_score', 0),
                    'growth_score': score_doc.get('growth_score', 0),
                    'cycle_score': score_doc.get('cycle_score', 0),
                    
                    # 元数据
                    'algorithm_version': score_doc.get('algorithm_version', ''),
                    'updated_at': score_doc.get('updated_at', '')
                }
                
                filtered_stocks.append(stock_data)
            
            print(f"✅ 获取到 {len(filtered_stocks)} 只高评分股票 (处理了 {processed_count} 条记录)")
            
            # 🔧 按评分排序确保顺序正确
            filtered_stocks.sort(key=lambda x: x['composite_score'], reverse=True)
            
            return filtered_stocks
            
        except Exception as e:
            print(f"❌ 获取高评分股票失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def _get_latest_score_date(self):
        """获取最新的评分日期"""
        try:
            # 查找最新的评分日期
            latest_record = self.db_manager.stock_scores.find_one(
                {},
                sort=[("score_date", -1)]
            )
            
            if latest_record:
                return latest_record['score_date']
            
            return None
            
        except Exception as e:
            print(f"❌ 获取最新评分日期失败: {e}")
            return None
    
    def get_top_stocks_by_dimension(self, dimension, limit=50, score_date=None, exclude_st=True):
        """
        🔧 新增：按维度获取高评分股票
        
        Args:
            dimension: 评分维度 (technical, fundamental, money_flow, value, growth, cycle)
            limit: 返回数量
            score_date: 评分日期
            exclude_st: 是否排除ST股票
        """
        try:
            valid_dimensions = ['technical', 'fundamental', 'money_flow', 'value', 'growth', 'cycle']
            
            if dimension not in valid_dimensions:
                print(f"❌ 无效的维度: {dimension}")
                print(f"有效维度: {valid_dimensions}")
                return []
            
            score_field = f"{dimension}_score"
            
            # 构建查询条件
            query = {score_field: {"$exists": True, "$ne": None}}
            
            if score_date:
                query["score_date"] = score_date
            else:
                latest_date = self._get_latest_score_date()
                if latest_date:
                    query["score_date"] = latest_date
                else:
                    return []
            
            print(f"📊 查询 {dimension} 维度高评分股票...")
            
            # 按指定维度评分排序
            cursor = self.db_manager.stock_scores.find(query).sort(score_field, -1)
            
            # 过滤和关联信息
            filtered_stocks = []
            
            for score_doc in cursor:
                if len(filtered_stocks) >= limit:
                    break
                
                symbol = score_doc['symbol']
                stock_info = self.db_manager.stock_info.find_one({"symbol": symbol})
                
                if not stock_info:
                    continue
                
                name = stock_info.get('name', '')
                if exclude_st and self._is_st_stock(name):
                    continue
                
                stock_data = {
                    'symbol': symbol,
                    'name': name,
                    'industry': stock_info.get('industry', ''),
                    f'{dimension}_score': score_doc.get(score_field, 0),
                    'composite_score': score_doc.get('composite_score', 0),
                    'overall_rating': score_doc.get('overall_rating', ''),
                    'score_date': score_doc.get('score_date', '')
                }
                
                filtered_stocks.append(stock_data)
            
            print(f"✅ {dimension} 维度获取到 {len(filtered_stocks)} 只股票")
            return filtered_stocks
            
        except Exception as e:
            print(f"❌ 按维度获取股票失败: {e}")
            return []
    
    def get_top_stocks_as_dataframe(self, limit=100, score_date=None, exclude_st=True, min_score=0):
        """
        🔧 新增：返回 DataFrame 格式的高评分股票（兼容旧接口）
        """
        try:
            stocks_list = self.get_top_stocks(limit, score_date, exclude_st, min_score)
            
            if stocks_list:
                import pandas as pd
                df = pd.DataFrame(stocks_list)
                print(f"✅ 转换为 DataFrame: {len(df)} 行 x {len(df.columns)} 列")
                return df
            else:
                import pandas as pd
                return pd.DataFrame()
                
        except Exception as e:
            print(f"❌ 转换为 DataFrame 失败: {e}")
            import pandas as pd
            return pd.DataFrame()    

    def get_score_trends(self, symbol, days=30):
        """获取评分趋势"""
        try:
            history = self.db_manager.get_stock_score_history(symbol, days)
            
            if history:
                df = pd.DataFrame(history)
                df['score_date'] = pd.to_datetime(df['score_date'], format='%Y%m%d')
                return df[['score_date', 'composite_score', 'overall_rating']].sort_values('score_date')
            
            return pd.DataFrame()
            
        except Exception as e:
            print(f"❌ 获取评分趋势失败: {e}")
            return pd.DataFrame()

    def get_score_summary(self, score_date=None):
        """
        🔧 使用 DatabaseManager 获取评分统计摘要
        
        Args:
            score_date: 指定日期，默认为当前base_date
            
        Returns:
            dict: 统计摘要
        """
        if not self.db_manager:
            print("❌ DatabaseManager 未初始化")
            return {}
        
        try:
            query_date = score_date or self.base_date
            
            # 🔧 使用聚合查询
            pipeline = [
                {"$match": {"score_date": query_date}},
                {"$group": {
                    "_id": None,
                    "total_count": {"$sum": 1},
                    "avg_score": {"$avg": "$composite_score"},
                    "max_score": {"$max": "$composite_score"},
                    "min_score": {"$min": "$composite_score"},
                    "excellent_count": {"$sum": {"$cond": [{"$gte": ["$composite_score", 85]}, 1, 0]}},
                    "good_count": {"$sum": {"$cond": [{"$and": [{"$gte": ["$composite_score", 70]}, {"$lt": ["$composite_score", 85]}]}, 1, 0]}},
                    "average_count": {"$sum": {"$cond": [{"$and": [{"$gte": ["$composite_score", 55]}, {"$lt": ["$composite_score", 70]}]}, 1, 0]}},
                    "poor_count": {"$sum": {"$cond": [{"$lt": ["$composite_score", 55]}, 1, 0]}}
                }}
            ]
            
            result = list(self.db_manager.stock_scores.aggregate(pipeline))
            
            if result:
                summary = result[0]
                summary["score_date"] = query_date
                summary["excellent_rate"] = (summary["excellent_count"] / summary["total_count"]) * 100 if summary["total_count"] > 0 else 0
                summary["good_rate"] = (summary["good_count"] / summary["total_count"]) * 100 if summary["total_count"] > 0 else 0
                
                print(f"📊 评分统计摘要 ({query_date}):")
                print(f"   总股票数: {summary['total_count']}")
                print(f"   平均评分: {summary['avg_score']:.2f}")
                print(f"   最高评分: {summary['max_score']:.2f}")
                print(f"   最低评分: {summary['min_score']:.2f}")
                print(f"   优秀股票 (85+分): {summary['excellent_count']} ({summary['excellent_rate']:.1f}%)")
                print(f"   良好股票 (70-85分): {summary['good_count']} ({summary['good_rate']:.1f}%)")
                print(f"   一般股票 (55-70分): {summary['average_count']}")
                print(f"   较差股票 (<55分): {summary['poor_count']}")
                
                return summary
            else:
                print(f"⚠️ 未找到 {query_date} 的评分数据")
                return {}
                
        except Exception as e:
            print(f"❌ 获取评分摘要失败: {e}")
            return {}
    
    def _is_st_stock(self, stock_name):
        """判断是否为ST股票"""
        if not stock_name:
            return False
        
        st_patterns = ['ST', '*ST', 'S*ST', 'SST', 'N*ST', 'PT']
        stock_name_upper = stock_name.upper()
        
        return any(pattern in stock_name_upper for pattern in st_patterns)
    
    def _print_final_stats(self, stats):
        """打印最终统计信息"""
        print(f"\n🎉 批量评分完成!")
        print(f"=" * 50)
        print(f"📊 统计信息:")
        print(f"   总股票数: {stats['total_stocks']}")
        print(f"   成功评分: {stats['successful']}")
        print(f"   评分失败: {stats['failed']}")
        print(f"   来自缓存: {stats['from_cache']}")
        print(f"   新计算: {stats['newly_calculated']}")
        print(f"   成功率: {(stats['successful'] / stats['total_stocks']) * 100:.1f}%")
        print(f"   耗时: {stats['duration']:.1f} 秒")
        print(f"   评分日期: {stats['base_date']}")
        
        if stats['errors']:
            print(f"\n❌ 错误详情 (前10个):")
            for error in stats['errors'][:10]:
                print(f"   {error}")
            if len(stats['errors']) > 10:
                print(f"   ... 还有 {len(stats['errors']) - 10} 个错误")
    
    # 🔧 保留兼容性方法
    def clean_duplicate_scores(self, date_str=None):
        """清理重复的评分记录"""
        if not self.db_manager:
            print("❌ DatabaseManager 未初始化")
            return {"error": "DatabaseManager 未初始化"}
        
        try:
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
            
            duplicates = list(self.db_manager.stock_scores.aggregate(pipeline))
            
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
                    result = self.db_manager.stock_scores.delete_many({"_id": {"$in": docs_to_delete}})
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


def main():
    """主函数 - 命令行使用"""
    import argparse
    
    parser = argparse.ArgumentParser(description='股票评分工具 - 简化版本')
    parser.add_argument('--date', '-d', type=str, help='基准日期 (YYYYMMDD)')
    parser.add_argument('--action', '-a', choices=['all', 'specific', 'top', 'summary', 'clean'], 
                        default='summary', help='执行动作')
    parser.add_argument('--symbols', '-s', nargs='+', help='指定股票代码')
    parser.add_argument('--limit', '-l', type=int, default=20, help='Top股票数量限制')
    parser.add_argument('--batch-size', '-b', type=int, default=50, help='批处理大小')
    parser.add_argument('--exclude-st', action='store_true', default=True, help='排除ST股票')
    parser.add_argument('--force-refresh', '-f', action='store_true', help='强制重新计算')
    parser.add_argument('--min-score', '-m', type=float, default=0, help='最低评分要求')
    
    args = parser.parse_args()
    
    # 创建评分工具
    score_util = ScoreUtil(base_date=args.date)
    
    if args.action == 'all':
        # 对所有股票评分
        print("🚀 开始对数据库中所有股票进行评分...")
        result = score_util.score_all_stocks(
            exclude_st=args.exclude_st,
            batch_size=args.batch_size,
            force_refresh=args.force_refresh
        )
        
    elif args.action == 'specific':
        # 对指定股票评分
        if not args.symbols:
            print("❌ 请指定股票代码 (--symbols)")
            return
        
        result = score_util.score_specific_stocks(
            args.symbols, 
            force_refresh=args.force_refresh
        )
        
    elif args.action == 'top':
        # 获取高评分股票
        # 🔧 修复：获取高评分股票 - 正确传递参数
        print(f"📊 查询参数:")
        print(f"   日期: {args.date}")
        print(f"   限制: {args.limit}")
        print(f"   最低分: {args.min_score}")
        print(f"   排除ST: {args.exclude_st}")

        top_stocks = score_util.get_top_stocks(
            limit=args.limit, 
            score_date=args.date,
            exclude_st=args.exclude_st,
            min_score=args.min_score
        )
        
        if top_stocks:
            print(f"\n🌟 Top {len(top_stocks)} 股票:")
            for i, stock in enumerate(top_stocks, 1):
                score = stock.get('composite_score') or stock.get('total_score', 0)
                name = stock.get('name', '')
                industry = stock.get('industry', '')
                print(f"{i:2d}. {stock['symbol']} - {name} | {score:.2f}分 | {industry}")
        
    elif args.action == 'summary':
        # 获取评分摘要
        summary = score_util.get_score_summary()
        
    elif args.action == 'clean':
        # 清理重复记录
        result = score_util.clean_duplicate_scores(args.date)
        print(f"🧹 清理结果: {result}")


if __name__ == "__main__":
    main()