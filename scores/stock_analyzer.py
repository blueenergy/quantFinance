import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import concurrent.futures
import threading
from tqdm import tqdm

# 导入所有评分器 - 包括 fundamental_scorer
from scorers.technical_scorer import TechnicalScorer
from scorers.value_scorer import ValueScorer
from scorers.growth_scorer import GrowthScorer
from scorers.money_flow_scorer import MoneyFlowScorer
from scorers.cycle_scorer import CycleScorer
from scorers.fundamental_scorer import FundamentalScorer  # 🔧 添加基本面评分器

class StockAnalyzer:
    """
    🎯 综合股票分析器
    
    整合所有评分器，提供全面的股票分析
    """    
    def __init__(self, db_manager, base_date=None, max_workers: int = 4, save_to_db=True):
        """初始化分析器"""
        self.db_manager = db_manager
        self.base_date = base_date or datetime.now().strftime('%Y%m%d')
        self.max_workers = max_workers
        self.save_to_db = save_to_db
        
        # 初始化所有评分器 - 包括 fundamental_scorer
        self.scorers = {
            'technical': TechnicalScorer(db_manager, self.base_date),
            'fundamental': FundamentalScorer(db_manager, self.base_date),  # 🔧 添加基本面评分器
            'value': ValueScorer(db_manager, self.base_date),
            'growth': GrowthScorer(db_manager, self.base_date),
            'money_flow': MoneyFlowScorer(db_manager, self.base_date),
            'cycle': CycleScorer(db_manager, self.base_date)
        }
        
        # 评分权重配置 - 包含所有6个评分器
        self.weights = {
            'technical': 0.20,      # 技术分析 20%
            'fundamental': 0.25,    # 基本面分析 25%  🔧 添加基本面权重
            'value': 0.20,          # 价值分析 20%
            'growth': 0.15,         # 成长分析 15%
            'money_flow': 0.10,     # 资金流分析 10%
            'cycle': 0.10           # 周期分析 10%
        }
        
        # 🔧 加载权重策略配置
        self.weight_strategies = self.load_weight_strategies()
        self.current_strategy = 'balanced'  # 默认策略
        
        # 线程锁
        self._lock = threading.Lock()
        
        print(f"✅ StockAnalyzer 初始化完成 (基准日期: {self.base_date})")
        print(f"📊 使用评分器: {list(self.weights.keys())}")

    # 添加配置类方法
    @classmethod
    def create_conservative_analyzer(cls, db_manager, base_date=None):
        """创建保守型分析器（更重视价值和基本面）"""
        analyzer = cls(db_manager, base_date)
        analyzer.weights = {
            'fundamental': 0.30,    # 基本面分析 30% ⬆️
            'value': 0.25,          # 价值分析 25% ⬆️
            'growth': 0.20,         # 成长分析 20%
            'technical': 0.15,      # 技术分析 15% ⬇️
            'money_flow': 0.05,     # 资金流分析 5% ⬇️
            'cycle': 0.05           # 周期分析 5% ⬇️
        }
        analyzer.current_strategy = 'conservative'
        return analyzer

    @classmethod
    def create_aggressive_analyzer(cls, db_manager, base_date=None):
        """创建激进型分析器（更重视技术面和资金流）"""
        analyzer = cls(db_manager, base_date)
        analyzer.weights = {
            'technical': 0.35,      # 技术分析 35% ⬆️
            'money_flow': 0.25,     # 资金流分析 25% ⬆️
            'fundamental': 0.15,    # 基本面分析 15% ⬇️
            'growth': 0.10,         # 成长分析 10% ⬇️
            'value': 0.10,          # 价值分析 10% ⬇️
            'cycle': 0.05           # 周期分析 5% ⬇️
        }
        analyzer.current_strategy = 'aggressive'
        return analyzer

    def set_custom_weights(self, **weights):
        """设置自定义权重"""
        # 验证权重键是否有效
        valid_scorers = set(self.scorers.keys())
        invalid_scorers = set(weights.keys()) - valid_scorers
        
        if invalid_scorers:
            print(f"❌ 包含无效的评分器: {invalid_scorers}")
            print(f"✅ 有效的评分器: {valid_scorers}")
            return False
        
        # 验证权重总和
        total_weight = sum(weights.values())
        if abs(total_weight - 1.0) > 0.01:
            print(f"⚠️ 权重总和不为1: {total_weight}")
            # 自动归一化
            normalized_weights = {k: v/total_weight for k, v in weights.items()}
            self.weights.update(normalized_weights)
            print("✅ 权重已自动归一化")
        else:
            self.weights.update(weights)
            print("✅ 权重更新成功")
        
        print(f"📊 当前权重: {self.weights}")
        return True

    def analyze_stock(self, symbol: str) -> Dict:
        """
        综合分析单只股票
        
        Args:
            symbol: 股票代码
            
        Returns:
            Dict: 包含所有维度评分和综合评分的分析结果
        """
        print(f"\n🔍 开始分析股票: {symbol}")
        
        analysis_result = {
            'symbol': symbol,
            'analysis_date': self.base_date,
            'timestamp': datetime.now().isoformat(),
            'scores': {},
            'details': {},
            'recommendations': {}
        }
        
        composite_score = 0
        valid_scores = 0
        
        # 逐个评分器分析
        for scorer_name, scorer in self.scorers.items():
            try:
                print(f"  📊 {scorer_name} 分析中...")
                
                score, details = scorer.calculate_score_detailed(symbol)
                
                analysis_result['scores'][scorer_name] = score
                analysis_result['details'][scorer_name] = details
                
                composite_score += score * self.weights[scorer_name]
                valid_scores += 1
                
                print(f"    ✅ {scorer_name}: {score:.2f}分")
                
            except Exception as e:
                print(f"    ❌ {scorer_name} 分析失败: {e}")
                analysis_result['scores'][scorer_name] = 50.0
                analysis_result['details'][scorer_name] = {"错误": str(e)}
                composite_score += 50.0 * self.weights[scorer_name]  # 使用默认分数
        
        # 计算综合评分
        analysis_result['composite_score'] = round(composite_score, 2)
        analysis_result['valid_scorers'] = valid_scores
        
        # 生成投资建议
        analysis_result['recommendations'] = self._generate_recommendations(analysis_result)
        
        print(f"  🎯 综合评分: {analysis_result['composite_score']:.2f}分")
        print(f"  📋 投资评级: {analysis_result['recommendations']['overall_rating']}")
        
        return analysis_result
    
    def analyze_single_stock(self, symbol: str, force_refresh=False) -> Dict:
        """
        分析单只股票 - 支持数据库缓存，兼容 daily_screening.py 的接口
        
        Args:
            symbol: 股票代码
            force_refresh: 是否强制刷新（忽略缓存）
            
        Returns:
            包含所有评分和分析结果的字典
        """
        try:
            from datetime import datetime
            score_date = datetime.now().strftime('%Y%m%d')
            
            # 🔧 检查是否已有今日评分（避免重复计算）
            if (not force_refresh and self.save_to_db and 
                self.db_manager.check_score_exists(symbol, score_date)):
                
                print(f"📋 {symbol} 今日评分已存在，从数据库获取...")
                cached_result = self._load_score_from_db(symbol, score_date)
                if cached_result:
                    return cached_result
                else:
                    print(f"⚠️ 数据库读取失败，重新计算 {symbol}")
            
            # 执行分析计算
            print(f"🔧 计算 {symbol} 评分...")
            result = self._perform_analysis(symbol)
            
            # 🔧 保存到数据库
            if self.save_to_db and result:
                success = self.db_manager.save_stock_score(symbol, result, score_date)
                if success:
                    result["data_source"] = "实时计算并已保存"
                else:
                    result["data_source"] = "实时计算但保存失败"
            else:
                result["data_source"] = "实时计算"
            
            return result
            
        except Exception as e:
            print(f"❌ 分析失败 {symbol}: {e}")
            return self._get_default_result(symbol)
    
    def _load_score_from_db(self, symbol, score_date):
        """从现有 stock_scores 表加载评分"""
        try:
            score_record = self.db_manager.db.stock_scores.find_one({
                "symbol": symbol,
                "score_date": score_date
            })
            
            if score_record:
                result = {
                    "symbol": symbol,
                    "composite_score": score_record.get("composite_score", 0),
                    "overall_rating": score_record.get("overall_rating", "未评级"),
                    "data_source": "数据库缓存",
                    "score_date": score_date,
                    "cache_time": score_record.get("created_at", "未知")
                }
                
                # 🔧 如果有详细评分，也加载
                if "score_details" in score_record:
                    details = score_record["score_details"]
                    result.update({
                        "technical_score": details.get("technical_details", {}),
                        "fundamental_score": details.get("fundamental_details", {}),
                        "money_flow_score": details.get("money_flow_details", {}),
                        "market_sentiment_score": details.get("market_sentiment_details", {})
                    })
                
                print(f"✅ {symbol} 从数据库加载: {result['composite_score']:.2f}分")
                return result
            
            return None
            
        except Exception as e:
            print(f"❌ 从数据库加载评分失败: {e}")
            return None
    
    def _generate_recommendations(self, analysis_result: Dict) -> Dict:
        """生成投资建议"""
        scores = analysis_result['scores']
        composite_score = analysis_result['composite_score']
        
        recommendations = {
            'overall_rating': '',
            'investment_suggestion': '',
            'strengths': [],
            'weaknesses': [],
            'risk_level': '',
            'time_horizon': ''
        }
        
        # 综合评级
        if composite_score >= 80:
            recommendations['overall_rating'] = '🌟 强烈推荐'
            recommendations['investment_suggestion'] = '优质标的，建议重点关注'
            recommendations['risk_level'] = '中低风险'
        elif composite_score >= 70:
            recommendations['overall_rating'] = '👍 推荐'
            recommendations['investment_suggestion'] = '较好标的，可以配置'
            recommendations['risk_level'] = '中等风险'
        elif composite_score >= 60:
            recommendations['overall_rating'] = '🤔 谨慎关注'
            recommendations['investment_suggestion'] = '一般标的，谨慎投资'
            recommendations['risk_level'] = '中高风险'
        elif composite_score >= 50:
            recommendations['overall_rating'] = '⚠️ 不推荐'
            recommendations['investment_suggestion'] = '表现一般，暂不推荐'
            recommendations['risk_level'] = '高风险'
        else:
            recommendations['overall_rating'] = '❌ 回避'
            recommendations['investment_suggestion'] = '表现较差，建议回避'
            recommendations['risk_level'] = '极高风险'
        
        # 分析优势和劣势
        for category, score in scores.items():
            if score >= 70:
                recommendations['strengths'].append(f"{category}: {score:.1f}分")
            elif score < 50:
                recommendations['weaknesses'].append(f"{category}: {score:.1f}分")
        
        # 投资时间建议 - 考虑基本面评分
        technical_score = scores.get('technical', 50)
        money_flow_score = scores.get('money_flow', 50)
        fundamental_score = scores.get('fundamental', 50)
        value_score = scores.get('value', 50)
        growth_score = scores.get('growth', 50)
        
        if technical_score >= 70 and money_flow_score >= 70:
            recommendations['time_horizon'] = '短中期(1-6个月)'
        elif fundamental_score >= 70 and value_score >= 60:
            recommendations['time_horizon'] = '中长期(6个月-2年)'
        elif growth_score >= 70:
            recommendations['time_horizon'] = '长期(1-3年)'
        else:
            recommendations['time_horizon'] = '需要进一步观察'
        
        return recommendations
    
    def batch_analyze(self, symbols: List[str], show_progress: bool = True) -> pd.DataFrame:
        """
        批量分析多只股票 - 兼容 daily_screening.py 的接口
        
        Args:
            symbols: 股票代码列表
            show_progress: 是否显示进度条
            
        Returns:
            包含所有分析结果的 DataFrame
        """
        print(f"\n🔍 开始批量分析 {len(symbols)} 只股票...")
        
        results = []
        
        if show_progress:
            # 使用进度条的串行处理
            for symbol in tqdm(symbols, desc="分析进度"):
                try:
                    result = self.analyze_single_stock(symbol)
                    results.append(result)
                except Exception as e:
                    print(f"❌ {symbol} 分析失败: {e}")
                    error_result = {
                        'symbol': symbol,
                        'composite_score': 50.0,
                        'overall_rating': '❌ 分析失败',
                        'error': str(e)
                    }
                    results.append(error_result)
        else:
            # 原有的批量处理逻辑保持不变
            for i, symbol in enumerate(symbols, 1):
                print(f"\n[{i}/{len(symbols)}] 分析 {symbol}")
                
                try:
                    result = self.analyze_stock(symbol)
                    
                    # 扁平化结果用于DataFrame
                    flat_result = {
                        'symbol': symbol,
                        'composite_score': result['composite_score'],
                        'overall_rating': result['recommendations']['overall_rating'],
                        'investment_suggestion': result['recommendations']['investment_suggestion']
                    }
                    
                    # 添加各维度评分
                    for scorer_name, score in result['scores'].items():
                        flat_result[f'{scorer_name}_score'] = score
                    
                    results.append(flat_result)
                    
                except Exception as e:
                    print(f"❌ {symbol} 分析失败: {e}")
                    results.append({
                        'symbol': symbol,
                        'composite_score': 0,
                        'overall_rating': '❌ 分析失败',
                        'investment_suggestion': str(e)
                    })
        
        df = pd.DataFrame(results)
        
        # 按综合评分排序
        if 'composite_score' in df.columns:
            df = df.sort_values('composite_score', ascending=False).reset_index(drop=True)
        
        print(f"\n📊 批量分析完成！")
        if not df.empty and 'composite_score' in df.columns:
            top_score = df['composite_score'].max()
            top_symbol = df.iloc[0]['symbol']
            print(f"📈 最佳标的: {top_symbol} ({top_score:.2f}分)")
        
        return df
    
    # def get_top_stocks(self, df: pd.DataFrame = None, symbols: List[str] = None, 
    #                   top_n: int = 10, min_score: float = 60.0) -> pd.DataFrame:
    #     """
    #     获取评分最高的股票 - 兼容原接口
        
    #     Args:
    #         df: 已分析的DataFrame，如果为None则使用symbols重新分析
    #         symbols: 股票代码列表  
    #         top_n: 返回股票数量
    #         min_score: 最低评分要求
            
    #     Returns:
    #         筛选后的 DataFrame
    #     """
    #     if df is None:
    #         if symbols is None:
    #             raise ValueError("df 和 symbols 不能同时为 None")
    #         df = self.batch_analyze(symbols)
        
    #     try:
    #         # 筛选符合条件的股票
    #         if 'composite_score' in df.columns:
    #             filtered_df = df[df['composite_score'] >= min_score].copy()
    #             return filtered_df.head(top_n)
    #         else:
    #             print("⚠️ DataFrame 中缺少 composite_score 列")
    #             return pd.DataFrame()
                
    #     except Exception as e:
    #         print(f"❌ 筛选股票失败: {e}")
    #         return pd.DataFrame()
    
    def export_results(self, df: pd.DataFrame, output_file: str = None) -> str:
        """
        导出分析结果 - 新增兼容接口
        
        Args:
            df: 分析结果 DataFrame
            output_file: 输出文件名
            
        Returns:
            输出文件路径
        """
        try:
            if output_file is None:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                output_file = f"stock_analysis_{timestamp}.csv"
            
            # 选择主要列进行导出 - 包含所有6个评分器
            export_columns = [
                'symbol', 'composite_score', 'overall_rating',
                'technical_score', 'fundamental_score', 'value_score', 
                'growth_score', 'money_flow_score', 'cycle_score'
            ]
            
            available_columns = [col for col in export_columns if col in df.columns]
            export_df = df[available_columns].copy()
            
            # 导出到 CSV
            export_df.to_csv(output_file, index=False, encoding='utf-8-sig')
            
            print(f"✅ 结果已导出: {output_file}")
            return output_file
            
        except Exception as e:
            print(f"❌ 导出失败: {e}")
            return ""
    
    def export_analysis_report(self, analysis_result: Dict, filename: str = None):
        """导出分析报告"""
        if filename is None:
            filename = f"analysis_{analysis_result['symbol']}_{self.base_date}.txt"
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"📊 股票分析报告\n")
            f.write(f"{'='*50}\n")
            f.write(f"股票代码: {analysis_result['symbol']}\n")
            f.write(f"分析日期: {analysis_result['analysis_date']}\n")
            f.write(f"综合评分: {analysis_result['composite_score']:.2f}分\n")
            f.write(f"投资评级: {analysis_result['recommendations']['overall_rating']}\n")
            f.write(f"投资建议: {analysis_result['recommendations']['investment_suggestion']}\n")
            f.write(f"\n📈 各维度评分:\n")
            
            for category, score in analysis_result['scores'].items():
                f.write(f"  {category}: {score:.2f}分\n")
            
            f.write(f"\n💪 优势领域:\n")
            for strength in analysis_result['recommendations']['strengths']:
                f.write(f"  ✅ {strength}\n")
            
            f.write(f"\n⚠️ 需要关注:\n")
            for weakness in analysis_result['recommendations']['weaknesses']:
                f.write(f"  ❌ {weakness}\n")
            
            f.write(f"\n📋 详细分析:\n")
            for category, details in analysis_result['details'].items():
                f.write(f"\n{category.upper()}:\n")
                for key, value in details.items():
                    f.write(f"  {key}: {value}\n")
        
        print(f"✅ 分析报告已导出: {filename}")

    def is_st_stock(self, name):
        """检查是否为ST股票"""
        if not name:
            return False
        st_patterns = ['ST', '*ST', 'S*ST', 'SST']
        return any(pattern in name.upper() for pattern in st_patterns)

    def _normalize_date(self, date_input):
        """日期格式标准化"""
        if isinstance(date_input, str):
            # 处理各种日期格式
            if len(date_input) == 8:  # YYYYMMDD
                return date_input
            elif len(date_input) == 10:  # YYYY-MM-DD
                return date_input.replace('-', '')
            # 其他格式处理...
        elif isinstance(date_input, datetime):
            return date_input.strftime('%Y%m%d')
        return date_input

    def load_weight_strategies(self):
        """加载预定义的权重策略 - 包含所有6个评分器"""
        return {
            'balanced': {
                'name': '🎯 均衡型策略',
                'description': '追求稳健收益，各维度均衡配置',
                'weights': {
                    'fundamental': 0.25,    # 基本面 25%
                    'value': 0.20,          # 价值面 20%
                    'technical': 0.20,      # 技术面 20%
                    'growth': 0.15,         # 成长面 15%
                    'money_flow': 0.10,     # 资金流 10%
                    'cycle': 0.10           # 周期面 10%
                }
            },
            'value_oriented': {
                'name': '💰 价值导向策略',
                'description': '重视基本面和估值，适合熊市和震荡市',
                'weights': {
                    'fundamental': 0.30,    # 基本面 30%
                    'value': 0.30,          # 价值面 30%
                    'growth': 0.15,         # 成长面 15%
                    'technical': 0.15,      # 技术面 15%
                    'money_flow': 0.05,     # 资金流 5%
                    'cycle': 0.05           # 周期面 5%
                }
            },
            'growth_oriented': {
                'name': '🚀 成长导向策略',
                'description': '重视成长性，适合牛市和新兴行业',
                'weights': {
                    'growth': 0.35,         # 成长面 35%
                    'fundamental': 0.25,    # 基本面 25%
                    'technical': 0.20,      # 技术面 20%
                    'money_flow': 0.10,     # 资金流 10%
                    'value': 0.05,          # 价值面 5%
                    'cycle': 0.05           # 周期面 5%
                }
            },
            'trading_oriented': {
                'name': '⚡ 交易导向策略',
                'description': '重视技术面和资金流，适合短期交易',
                'weights': {
                    'technical': 0.40,      # 技术面 40%
                    'money_flow': 0.25,     # 资金流 25%
                    'fundamental': 0.15,    # 基本面 15%
                    'growth': 0.10,         # 成长面 10%
                    'cycle': 0.05,          # 周期面 5%
                    'value': 0.05           # 价值面 5%
                }
            },
            'defensive': {
                'name': '🛡️ 防御型策略',
                'description': '极度重视质量和安全，适合保守投资者',
                'weights': {
                    'fundamental': 0.35,    # 基本面 35%
                    'value': 0.35,          # 价值面 35%
                    'growth': 0.10,         # 成长面 10%
                    'technical': 0.10,      # 技术面 10%
                    'money_flow': 0.05,     # 资金流 5%
                    'cycle': 0.05           # 周期面 5%
                }
            }
        }
    
    def apply_strategy(self, strategy_name):
        """应用预定义策略"""
        strategies = self.load_weight_strategies()
        
        if strategy_name not in strategies:
            print(f"❌ 未知策略: {strategy_name}")
            print(f"可用策略: {', '.join(strategies.keys())}")
            return False
        
        strategy = strategies[strategy_name]
        self.weights = strategy['weights'].copy()
        self.current_strategy = strategy_name
        
        print(f"✅ 已应用策略: {strategy['name']}")
        print(f"📝 策略说明: {strategy['description']}")
        print(f"📊 权重配置:")
        for scorer, weight in self.weights.items():
            print(f"  {scorer}: {weight*100:.1f}%")
        
        return True
    
    def compare_strategies(self, symbol, strategies=None):
        """对比不同策略的评分结果"""
        if strategies is None:
            strategies = ['balanced', 'value_oriented', 'growth_oriented', 'trading_oriented']
        
        print(f"📊 策略对比分析 - {symbol}")
        print("="*60)
        
        original_weights = self.weights.copy()
        original_strategy = self.current_strategy
        results = {}
        
        for strategy in strategies:
            print(f"🔍 {strategy} 策略...")
            
            if not self.apply_strategy(strategy):
                continue
                
            try:
                result = self.analyze_single_stock(symbol)
                results[strategy] = result['composite_score']
                print(f"  📊 评分: {result['composite_score']:.2f}")
            except Exception as e:
                results[strategy] = None
                print(f"  ❌ 失败: {e}")
        
        # 恢复原始权重
        self.weights = original_weights
        self.current_strategy = original_strategy
        
        # 输出对比
        print(f"\n📈 策略对比结果:")
        print("-"*40)
        sorted_results = sorted(
            [(k, v) for k, v in results.items() if v is not None], 
            key=lambda x: x[1], reverse=True
        )
        
        for strategy, score in sorted_results:
            print(f"{strategy:20}: {score:6.2f}分")
        
        return results
    
    def get_analysis_summary(self, df: pd.DataFrame) -> Dict:
        """
        获取分析摘要 - 新增兼容接口
        
        Args:
            df: 分析结果 DataFrame
            
        Returns:
            分析摘要字典
        """
        try:
            if df.empty:
                return {'error': '无分析数据'}
            
            summary = {
                'total_stocks': len(df),
                'analysis_date': self.base_date,
                'scorers_used': list(self.weights.keys()),
                'current_strategy': self.current_strategy,
                'weights': self.weights
            }
            
            # 评分统计
            if 'composite_score' in df.columns:
                summary.update({
                    'avg_composite_score': round(df['composite_score'].mean(), 2),
                    'top_score': df['composite_score'].max(),
                    'min_score': df['composite_score'].min(),
                    'high_score_count': len(df[df['composite_score'] >= 75])
                })
            
            # 评级分布
            if 'overall_rating' in df.columns:
                summary['rating_distribution'] = df['overall_rating'].value_counts().to_dict()
            
            # 各评分器平均分 - 包含所有6个评分器
            for scorer in self.weights.keys():
                score_col = f'{scorer}_score'
                if score_col in df.columns:
                    summary[f'avg_{scorer}_score'] = round(df[score_col].mean(), 2)
            
            return summary
            
        except Exception as e:
            print(f"❌ 生成摘要失败: {e}")
            return {'error': str(e)}

    def _get_default_result(self, symbol):
        """获取默认分析结果（当分析失败时使用）"""
        try:
            return {
                'symbol': symbol,
                'analysis_date': self.base_date,
                'timestamp': datetime.now().isoformat(),
                'composite_score': 50.0,  # 默认中性评分
                'overall_rating': '❌ 分析失败',
                'data_source': '默认结果',
                'error': '分析过程中发生错误',
                
                # 各维度默认评分
                'technical_score': 50.0,
                'fundamental_score': 50.0,
                'value_score': 50.0,
                'growth_score': 50.0,
                'money_flow_score': 50.0,
                'cycle_score': 50.0,
                
                # 默认分析详情
                'technical_analysis': {'错误': '技术分析失败'},
                'fundamental_analysis': {'错误': '基本面分析失败'},
                'value_analysis': {'错误': '价值分析失败'},
                'growth_analysis': {'错误': '成长分析失败'},
                'money_flow_analysis': {'错误': '资金流分析失败'},
                'cycle_analysis': {'错误': '周期分析失败'},
            }
        except Exception as e:
            # 如果连默认结果都创建失败，返回最基础的结果
            return {
                'symbol': symbol,
                'composite_score': 50.0,
                'overall_rating': '❌ 严重错误',
                'error': f'无法创建默认结果: {e}'
            }

    def _perform_analysis(self, symbol):
        """执行实际的股票分析计算"""
        try:
            print(f"🔍 开始分析股票: {symbol}")
            
            analysis_result = {
                'symbol': symbol,
                'analysis_date': self.base_date,
                'timestamp': datetime.now().isoformat(),
                'scores': {},
                'details': {},
                'recommendations': {}
            }
            
            composite_score = 0
            valid_scores = 0
            
            # 逐个评分器分析
            for scorer_name, scorer in self.scorers.items():
                try:
                    print(f"  📊 {scorer_name} 分析中...")
                    
                    score, details = scorer.calculate_score_detailed(symbol)
                    
                    analysis_result['scores'][scorer_name] = score
                    analysis_result['details'][scorer_name] = details
                    
                    composite_score += score * self.weights[scorer_name]
                    valid_scores += 1
                    
                    print(f"    ✅ {scorer_name}: {score:.2f}分")
                    
                except Exception as e:
                    print(f"    ❌ {scorer_name} 分析失败: {e}")
                    analysis_result['scores'][scorer_name] = 50.0
                    analysis_result['details'][scorer_name] = {"错误": str(e)}
                    composite_score += 50.0 * self.weights[scorer_name]  # 使用默认分数
            
            # 计算综合评分
            analysis_result['composite_score'] = round(composite_score, 2)
            analysis_result['valid_scorers'] = valid_scores
            analysis_result['weights'] = self.weights
            
            # 生成投资建议
            analysis_result['recommendations'] = self._generate_recommendations(analysis_result)
            
            
            print(f"  🎯 综合评分: {analysis_result['composite_score']:.2f}分")
            print(f"  📋 投资评级: {analysis_result['recommendations']['overall_rating']}")
            
            return analysis_result
            
        except Exception as e:
            print(f"❌ 分析计算失败 {symbol}: {e}")
            raise e  # 重新抛出异常，让调用者处理

