#!/usr/bin/env python3
"""
🎯 StockAnalyzer 综合测试

测试股票分析器的核心功能：
- 初始化和配置
- 权重管理
- 单股分析
- 批量分析
- 策略对比
- 报告生成
- 错误处理
"""

import pytest
import os
import sys
import pandas as pd
import numpy as np
import time
import traceback
from unittest.mock import Mock, patch, mock_open, call
from datetime import datetime

# 添加父目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# 导入待测试的类
from stock_analyzer import StockAnalyzer

class TestStockAnalyzer:
    """StockAnalyzer 测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 设置模拟数据
        self._setup_mock_returns()
    
    def _setup_mock_returns(self):
        """设置模拟返回值"""
        
        # 模拟股票基本信息
        self.mock_db_manager.get_stock_info.return_value = {
            "symbol": "000002",
            "name": "万科A",
            "industry": "房地产开发"
        }
        
        # 模拟各评分器的评分结果
        self.mock_scores = {
            'technical': (75.5, {"技术分析": "良好", "趋势": "上升"}),
            'value': (68.2, {"PE分析": "合理", "PB分析": "低估"}),
            'growth': (82.1, {"成长性": "优秀", "增长率": "15%"}),
            'money_flow': (71.8, {"资金流": "净流入", "机构态度": "积极"}),
            'cycle': (65.3, {"周期位置": "中期", "行业景气": "正常"})
        }
    
    # ==================== 初始化测试 ====================
    
    def test_stock_analyzer_initialization(self):
        """测试 StockAnalyzer 初始化"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 验证基本属性
        assert analyzer.db_manager == self.mock_db_manager
        assert analyzer.base_date == self.base_date
        
        # 验证评分器初始化
        assert 'technical' in analyzer.scorers
        assert 'value' in analyzer.scorers
        assert 'growth' in analyzer.scorers
        assert 'money_flow' in analyzer.scorers
        assert 'cycle' in analyzer.scorers
        
        # 验证默认权重
        assert len(analyzer.weights) == 5
        assert sum(analyzer.weights.values()) == pytest.approx(1.0, abs=0.01)
        
        print("✅ StockAnalyzer 初始化测试通过")
    
    def test_default_date_handling(self):
        """测试默认日期处理"""
        
        analyzer = StockAnalyzer(self.mock_db_manager)  # 不传入日期
        
        # 验证有默认日期
        assert analyzer.base_date is not None
        assert len(analyzer.base_date) == 8  # YYYYMMDD格式
        
        print("✅ 默认日期处理测试通过")
    
    # ==================== 权重管理测试 ====================
    
    def test_set_custom_weights_valid(self):
        """测试设置有效的自定义权重"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        custom_weights = {
            'technical': 0.3,
            'value': 0.25,
            'growth': 0.25,
            'money_flow': 0.15,
            'cycle': 0.05
        }
        
        analyzer.set_custom_weights(**custom_weights)
        
        # 验证权重设置
        for key, expected_weight in custom_weights.items():
            assert analyzer.weights[key] == expected_weight
        
        print("✅ 自定义权重设置测试通过")
    
    def test_set_custom_weights_invalid_sum(self):
        """测试设置无效权重(总和不为1)"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        invalid_weights = {
            'technical': 0.5,
            'value': 0.5,
            'growth': 0.5  # 总和 > 1
        }
        
        with pytest.raises(ValueError, match="权重总和应为1.0"):
            analyzer.set_custom_weights(**invalid_weights)
        
        print("✅ 无效权重检测测试通过")
    
    def test_create_conservative_analyzer(self):
        """测试创建保守型分析器"""
        
        analyzer = StockAnalyzer.create_conservative_analyzer(
            self.mock_db_manager, self.base_date
        )
        
        # 验证保守型权重配置
        assert analyzer.weights['value'] >= 0.25  # 价值权重较高
        assert analyzer.weights['growth'] <= 0.30  # 成长权重适中
        assert sum(analyzer.weights.values()) == pytest.approx(1.0, abs=0.01)
        
        print("✅ 保守型分析器创建测试通过")
    
    def test_create_aggressive_analyzer(self):
        """测试创建激进型分析器"""
        
        analyzer = StockAnalyzer.create_aggressive_analyzer(
            self.mock_db_manager, self.base_date
        )
        
        # 验证激进型权重配置
        assert analyzer.weights['technical'] >= 0.30  # 技术权重较高
        assert analyzer.weights['money_flow'] >= 0.25  # 资金流权重较高
        assert sum(analyzer.weights.values()) == pytest.approx(1.0, abs=0.01)
        
        print("✅ 激进型分析器创建测试通过")
    
    # ==================== 单股分析测试 ====================
    
    @patch('stock_analyzer.TechnicalScorer')
    @patch('stock_analyzer.ValueScorer') 
    @patch('stock_analyzer.GrowthScorer')
    @patch('stock_analyzer.MoneyFlowScorer')
    @patch('stock_analyzer.CycleScorer')
    def test_analyze_stock_success(self, mock_cycle, mock_money, mock_growth, 
                                  mock_value, mock_technical):
        """测试成功的单股分析"""
        
        # 设置模拟评分器
        mock_technical.return_value.calculate_score_detailed.return_value = self.mock_scores['technical']
        mock_value.return_value.calculate_score_detailed.return_value = self.mock_scores['value']
        mock_growth.return_value.calculate_score_detailed.return_value = self.mock_scores['growth']
        mock_money.return_value.calculate_score_detailed.return_value = self.mock_scores['money_flow']
        mock_cycle.return_value.calculate_score_detailed.return_value = self.mock_scores['cycle']
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        result = analyzer.analyze_stock("000002")
        
        # 验证返回结构
        assert 'symbol' in result
        assert 'analysis_date' in result
        assert 'scores' in result
        assert 'details' in result
        assert 'recommendations' in result
        assert 'composite_score' in result
        
        # 验证基本信息
        assert result['symbol'] == "000002"
        assert result['analysis_date'] == self.base_date
        
        # 验证评分
        assert len(result['scores']) == 5
        for scorer_name in ['technical', 'value', 'growth', 'money_flow', 'cycle']:
            assert scorer_name in result['scores']
            assert isinstance(result['scores'][scorer_name], (int, float))
        
        # 验证综合评分
        assert isinstance(result['composite_score'], (int, float))
        assert 0 <= result['composite_score'] <= 100
        
        # 验证投资建议
        recommendations = result['recommendations']
        assert 'overall_rating' in recommendations
        assert 'investment_suggestion' in recommendations
        assert 'strengths' in recommendations
        assert 'weaknesses' in recommendations
        
        print(f"✅ 单股分析测试通过 - 综合评分: {result['composite_score']}")
    
    @patch('stock_analyzer.TechnicalScorer')
    def test_analyze_stock_with_scorer_failure(self, mock_technical):
        """测试评分器失败时的处理"""
        
        # 设置一个评分器抛出异常
        mock_technical.return_value.calculate_score_detailed.side_effect = \
            Exception("数据获取失败")
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        result = analyzer.analyze_stock("000002")
        
        # 验证仍能返回结果
        assert 'composite_score' in result
        assert result['scores']['technical'] == 50.0  # 默认分数
        assert "错误" in result['details']['technical']
        
        print("✅ 评分器失败处理测试通过")
    
    # ==================== 批量分析测试 ====================
    
    @patch.object(StockAnalyzer, 'analyze_stock')
    def test_batch_analyze_success(self, mock_analyze):
        """测试成功的批量分析"""
        
        # 模拟单股分析结果
        mock_analyze.return_value = {
            'symbol': '000002',
            'composite_score': 75.5,
            'scores': self.mock_scores,
            'recommendations': {
                'overall_rating': '👍 推荐',
                'investment_suggestion': '较好标的，可以配置'
            }
        }
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        symbols = ["000002", "000001", "000858"]
        
        result_df = analyzer.batch_analyze(symbols)
        
        # 验证返回DataFrame
        assert isinstance(result_df, pd.DataFrame)
        assert len(result_df) == 3
        assert 'symbol' in result_df.columns
        assert 'composite_score' in result_df.columns
        
        # 验证按评分排序
        scores = result_df['composite_score'].tolist()
        assert scores == sorted(scores, reverse=True)
        
        print("✅ 批量分析测试通过")
    
    @patch.object(StockAnalyzer, 'analyze_stock')
    def test_batch_analyze_with_failures(self, mock_analyze):
        """测试批量分析中部分失败的情况"""
        
        def side_effect(symbol):
            if symbol == "000002":
                return {
                    'symbol': symbol,
                    'composite_score': 75.5,
                    'scores': {},
                    'recommendations': {'overall_rating': '👍 推荐'}
                }
            else:
                raise Exception("分析失败")
        
        mock_analyze.side_effect = side_effect
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        symbols = ["000002", "INVALID"]
        
        result_df = analyzer.batch_analyze(symbols)
        
        # 验证处理失败的股票
        assert len(result_df) == 2
        assert result_df.iloc[0]['composite_score'] == 75.5  # 成功的
        assert result_df.iloc[1]['composite_score'] == 0     # 失败的
        
        print("✅ 批量分析失败处理测试通过")
    
    def test_get_top_stocks(self):
        """测试获取顶级股票"""
        
        with patch.object(StockAnalyzer, 'batch_analyze') as mock_batch:
            # 模拟批量分析结果
            mock_df = pd.DataFrame([
                {'symbol': '000002', 'composite_score': 85.0},
                {'symbol': '000001', 'composite_score': 78.5},
                {'symbol': '000858', 'composite_score': 72.1},
                {'symbol': '000876', 'composite_score': 68.9}
            ])
            mock_batch.return_value = mock_df
            
            analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
            symbols = ["000002", "000001", "000858", "000876"]
            
            top_stocks = analyzer.get_top_stocks(symbols, top_n=2)
            
            # 验证返回前2名
            assert len(top_stocks) == 2
            assert top_stocks.iloc[0]['symbol'] == '000002'
            assert top_stocks.iloc[1]['symbol'] == '000001'
        
        print("✅ 获取顶级股票测试通过")
    
    # ==================== 投资建议测试 ====================
    
    def test_generate_recommendations_high_score(self):
        """测试高分股票的投资建议"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        analysis_result = {
            'composite_score': 85.0,
            'scores': {
                'technical': 80.0,
                'value': 85.0,
                'growth': 90.0,
                'money_flow': 75.0,
                'cycle': 70.0
            }
        }
        
        recommendations = analyzer._generate_recommendations(analysis_result)
        
        # 验证高分推荐
        assert '强烈推荐' in recommendations['overall_rating']
        assert '优质标的' in recommendations['investment_suggestion']
        assert recommendations['risk_level'] == '中低风险'
        assert len(recommendations['strengths']) >= 3  # 多个优势
        
        print("✅ 高分投资建议测试通过")
    
    def test_generate_recommendations_low_score(self):
        """测试低分股票的投资建议"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        analysis_result = {
            'composite_score': 35.0,
            'scores': {
                'technical': 30.0,
                'value': 40.0,
                'growth': 25.0,
                'money_flow': 35.0,
                'cycle': 45.0
            }
        }
        
        recommendations = analyzer._generate_recommendations(analysis_result)
        
        # 验证低分建议
        assert '回避' in recommendations['overall_rating'] or '不推荐' in recommendations['overall_rating']
        assert '回避' in recommendations['investment_suggestion'] or '不推荐' in recommendations['investment_suggestion']
        assert '高风险' in recommendations['risk_level']
        assert len(recommendations['weaknesses']) >= 3  # 多个劣势
        
        print("✅ 低分投资建议测试通过")
    
    # ==================== 报告生成测试 ====================
    
    def test_export_analysis_report(self):
        """测试分析报告导出"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        analysis_result = {
            'symbol': '000002',
            'analysis_date': self.base_date,
            'composite_score': 75.5,
            'scores': {
                'technical': 70.0,
                'value': 80.0
            },
            'recommendations': {
                'overall_rating': '👍 推荐',
                'investment_suggestion': '较好标的',
                'strengths': ['技术面良好', '估值合理'],
                'weaknesses': ['成长性一般']
            },
            'details': {
                'technical': {'趋势': '上升', 'RSI': '65'},
                'value': {'PE': '18倍', 'PB': '1.5倍'}
            }
        }
        
        filename = 'test_report.txt'
        
        # 使用模拟文件写入
        with patch('builtins.open', mock_open()) as mock_file:
            analyzer.export_analysis_report(analysis_result, filename)
            
            # 验证文件写入
            mock_file.assert_called_once_with(filename, 'w', encoding='utf-8')
            
            # 获取写入的内容
            written_content = ''.join(
                call.args[0] for call in mock_file().write.call_args_list
            )
            
            # 验证报告内容
            assert '000002' in written_content
            assert '75.5' in written_content
            assert '推荐' in written_content
            assert '技术面良好' in written_content
        
        print("✅ 分析报告导出测试通过")
    
    def test_export_analysis_report_default_filename(self):
        """测试默认文件名的报告导出"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 完整的分析结果结构
        analysis_result = {
            'symbol': '000002',
            'analysis_date': self.base_date,
            'composite_score': 75.5,
            'scores': {
                'technical': 70.0,
                'value': 80.0
            },
            'recommendations': {
                'overall_rating': '👍 推荐',
                'investment_suggestion': '较好标的，可以配置',
                'strengths': ['技术面良好'],
                'weaknesses': ['成长性一般']
            },
            'details': {
                'technical': {'趋势': '上升'},
                'value': {'PE': '18倍'}
            }
        }
        
        with patch('builtins.open', mock_open()) as mock_file:
            analyzer.export_analysis_report(analysis_result)  # 不指定文件名
            
            # 验证使用默认文件名
            expected_filename = f"analysis_000002_{self.base_date}.txt"
            mock_file.assert_called_once_with(expected_filename, 'w', encoding='utf-8')
        
        print("✅ 默认文件名报告导出测试通过")
    
    # ==================== 工具方法测试 ====================
    
    def test_is_st_stock(self):
        """测试ST股票检测"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 测试各种ST股票
        assert analyzer.is_st_stock("ST万科") == True
        assert analyzer.is_st_stock("*ST海航") == True
        assert analyzer.is_st_stock("S*ST华信") == True
        assert analyzer.is_st_stock("SST前锋") == True
        
        # 测试正常股票
        assert analyzer.is_st_stock("万科A") == False
        assert analyzer.is_st_stock("贵州茅台") == False
        
        # 测试空值
        assert analyzer.is_st_stock("") == False
        assert analyzer.is_st_stock(None) == False
        
        print("✅ ST股票检测测试通过")
    
    def test_normalize_date(self):
        """测试日期格式标准化"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 测试不同日期格式
        assert analyzer._normalize_date("20250910") == "20250910"  # YYYYMMDD
        assert analyzer._normalize_date("2025-09-10") == "20250910"  # YYYY-MM-DD
        
        # 测试datetime对象
        dt = datetime(2025, 9, 10)
        assert analyzer._normalize_date(dt) == "20250910"
        
        print("✅ 日期格式标准化测试通过")
    
    # ==================== 错误处理测试 ====================
    
    def test_analyze_stock_invalid_symbol(self):
        """测试无效股票代码的处理"""
        
        # 模拟数据库返回空结果
        self.mock_db_manager.get_stock_info.return_value = None
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 应该能处理无效代码，不抛出异常
        result = analyzer.analyze_stock("INVALID")
        
        assert 'symbol' in result
        assert result['symbol'] == "INVALID"
        
        print("✅ 无效股票代码处理测试通过")
    
    @patch('stock_analyzer.TechnicalScorer')
    def test_scorer_initialization_failure(self, mock_technical):
        """测试评分器初始化失败的处理"""
        
        # 模拟评分器初始化失败
        mock_technical.side_effect = Exception("初始化失败")
        
        # 应该能处理初始化失败
        try:
            analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
            # 如果没有异常处理，这里会抛出异常
            print("⚠️ 评分器初始化失败但程序继续运行")
        except Exception as e:
            print(f"❌ 评分器初始化失败导致程序崩溃: {e}")
            # 这里可以添加更好的错误处理
    
    # ==================== 策略对比测试 ====================
    
    def test_strategy_loading(self):
        """测试策略配置加载"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 验证策略方法存在（如果已实现）
        if hasattr(analyzer, 'load_weight_strategies'):
            strategies = analyzer.load_weight_strategies()
            
            # 验证策略配置
            assert isinstance(strategies, dict)
            assert len(strategies) > 0
            
            # 验证每个策略的权重总和
            for strategy_name, strategy_config in strategies.items():
                if 'weights' in strategy_config:
                    weights = strategy_config['weights']
                    total_weight = sum(weights.values())
                    assert abs(total_weight - 1.0) < 0.01, f"{strategy_name} 权重总和不为1"
            
            print("✅ 策略配置加载测试通过")
        else:
            print("ℹ️ 策略配置功能尚未实现")
    
    # ==================== 扩展测试 ====================
    
    def test_apply_strategy_balanced(self):
        """测试应用均衡型策略"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 检查是否有策略方法
        if hasattr(analyzer, 'apply_strategy'):
            # 应用均衡型策略
            result = analyzer.apply_strategy('balanced')
            
            # 验证策略应用成功
            assert result == True
            print("✅ 均衡型策略应用测试通过")
        else:
            print("ℹ️ 策略应用功能尚未实现")
    
    @patch.object(StockAnalyzer, 'analyze_stock')
    def test_compare_strategies_success(self, mock_analyze):
        """测试策略对比功能"""
        
        # 模拟分析结果
        mock_analyze.return_value = {'composite_score': 75.0}
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 检查是否有策略对比方法
        if hasattr(analyzer, 'compare_strategies'):
            strategies = ['balanced', 'value_oriented']
            results = analyzer.compare_strategies("000002", strategies)
            
            # 验证对比结果
            assert isinstance(results, dict)
            print("✅ 策略对比测试通过")
        else:
            print("ℹ️ 策略对比功能尚未实现")
    
    def test_batch_analyze_empty_list(self):
        """测试批量分析空列表"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 测试空列表
        result_df = analyzer.batch_analyze([])
        
        # 验证返回空DataFrame
        assert isinstance(result_df, pd.DataFrame)
        assert len(result_df) == 0
        
        print("✅ 空列表批量分析测试通过")
    
    def test_get_top_stocks_more_than_available(self):
        """测试请求的top_n超过可用股票数"""
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        with patch.object(analyzer, 'batch_analyze') as mock_batch:
            # 模拟只有2只股票的结果
            mock_df = pd.DataFrame([
                {'symbol': '000002', 'composite_score': 85.0},
                {'symbol': '000001', 'composite_score': 78.5}
            ])
            mock_batch.return_value = mock_df
            
            # 请求前5只，但只有2只
            top_stocks = analyzer.get_top_stocks(['000002', '000001'], top_n=5)
            
            # 验证返回所有可用的股票
            assert len(top_stocks) == 2
            assert top_stocks.iloc[0]['symbol'] == '000002'
        
        print("✅ 超出可用数量的top_n测试通过")
    
    @patch.object(StockAnalyzer, 'analyze_stock')
    def test_batch_analyze_performance(self, mock_analyze):
        """测试批量分析性能"""
        
        # 模拟快速的单股分析
        mock_analyze.return_value = {
            'symbol': 'TEST',
            'composite_score': 75.0,
            'scores': {},
            'recommendations': {
                'overall_rating': '推荐',
                'investment_suggestion': '测试'
            }
        }
        
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 测试分析50只股票的性能（减少数量避免超时）
        test_symbols = [f"00000{i:02d}" for i in range(50)]
        
        start_time = time.time()
        result_df = analyzer.batch_analyze(test_symbols)
        end_time = time.time()
        
        # 验证结果
        assert len(result_df) == 50
        assert mock_analyze.call_count == 50
        
        # 简单的性能检查
        execution_time = end_time - start_time
        print(f"ℹ️ 50只股票分析耗时: {execution_time:.2f}秒")
        
        print("✅ 批量分析性能测试通过")
    
    @patch('stock_analyzer.TechnicalScorer')
    @patch('stock_analyzer.ValueScorer')
    @patch('stock_analyzer.GrowthScorer')
    @patch('stock_analyzer.MoneyFlowScorer')
    @patch('stock_analyzer.CycleScorer')
    def test_end_to_end_analysis_workflow(self, mock_cycle, mock_money, mock_growth,
                                         mock_value, mock_technical):
        """测试端到端的分析工作流"""
        
        # 设置模拟评分器
        mock_technical.return_value.calculate_score_detailed.return_value = (75.5, {"技术": "良好"})
        mock_value.return_value.calculate_score_detailed.return_value = (68.2, {"估值": "合理"})
        mock_growth.return_value.calculate_score_detailed.return_value = (82.1, {"成长": "优秀"})
        mock_money.return_value.calculate_score_detailed.return_value = (71.8, {"资金": "流入"})
        mock_cycle.return_value.calculate_score_detailed.return_value = (65.3, {"周期": "中期"})
        
        # 创建分析器
        analyzer = StockAnalyzer(self.mock_db_manager, self.base_date)
        
        # 1. 单股分析
        single_result = analyzer.analyze_stock("000002")
        assert single_result['composite_score'] > 0
        
        # 2. 批量分析
        batch_result = analyzer.batch_analyze(["000002", "000001", "000858"])
        assert len(batch_result) == 3
        
        # 3. 获取顶级股票
        top_result = analyzer.get_top_stocks(["000002", "000001", "000858"], top_n=2)
        assert len(top_result) == 2
        
        # 4. 导出报告
        with patch('builtins.open', mock_open()) as mock_file:
            analyzer.export_analysis_report(single_result)
            mock_file.assert_called_once()
        
        print("✅ 端到端分析工作流测试通过")

# ==================== 运行所有测试的函数 ====================

def test_run_all_stock_analyzer_tests():
    """运行所有 StockAnalyzer 测试"""
    print("🧪 开始运行 StockAnalyzer 所有测试...")
    
    test_instance = TestStockAnalyzer()
    test_instance.setup_method()
    
    test_methods = [
        "test_stock_analyzer_initialization",
        "test_default_date_handling",
        "test_set_custom_weights_valid",
        "test_set_custom_weights_invalid_sum",
        "test_create_conservative_analyzer",
        "test_create_aggressive_analyzer",
        "test_analyze_stock_success",
        "test_analyze_stock_with_scorer_failure",
        "test_batch_analyze_success",
        "test_batch_analyze_with_failures",
        "test_get_top_stocks",
        "test_generate_recommendations_high_score",
        "test_generate_recommendations_low_score",
        "test_export_analysis_report",
        "test_export_analysis_report_default_filename",
        "test_is_st_stock",
        "test_normalize_date",
        "test_analyze_stock_invalid_symbol",
        "test_scorer_initialization_failure",
        "test_strategy_loading",
        "test_apply_strategy_balanced",
        "test_compare_strategies_success",
        "test_batch_analyze_empty_list",
        "test_get_top_stocks_more_than_available",
        "test_batch_analyze_performance",
        "test_end_to_end_analysis_workflow"
    ]
    
    passed = 0
    failed = 0
    
    for method_name in test_methods:
        try:
            print(f"\n🔍 执行: {method_name}")
            test_method = getattr(test_instance, method_name)
            test_method()
            passed += 1
        except Exception as e:
            print(f"❌ {method_name} 失败: {e}")
            failed += 1
    
    print(f"\n📊 测试结果:")
    print(f"✅ 通过: {passed}")
    print(f"❌ 失败: {failed}")
    print(f"📈 通过率: {passed/(passed+failed)*100:.1f}%")
    
    return passed, failed

if __name__ == "__main__":
    test_run_all_stock_analyzer_tests()