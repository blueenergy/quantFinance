#!/usr/bin/env python3
"""
💰 ValueScorer 价值评分器测试

测试价值评分器的所有功能：
- PE、PB、PS估值评分
- 股息率评分
- PEG评分
- 相对估值评分
- 边界条件和错误处理
"""

import pytest
import os
import sys
import time
import traceback
import math
from unittest.mock import Mock, patch

# 添加父目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# 导入待测试的类
from scorers.value_scorer import ValueScorer

class TestValueScorer:
    """ValueScorer 测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 设置默认模拟数据
        self._setup_mock_data()
    
    def _setup_mock_data(self):
        """设置模拟数据"""
        
        # 标准估值数据
        self.standard_valuation_data = {
            'pe_ratio': 18.5,
            'pe_ttm': 17.8,
            'pb_ratio': 2.1,
            'ps_ratio': 3.2
        }
        
        # 标准财务数据
        self.standard_financial_data = {
            'dividend_yield': 2.8,
            'net_profit_growth': 15.2,
            'dividend_stability': 0.75
        }
        
        # 设置默认返回值
        self.mock_db_manager.get_valuation_data.return_value = self.standard_valuation_data
        self.mock_db_manager.get_financial_data.return_value = self.standard_financial_data
    
    # ==================== 初始化测试 ====================
    
    def test_value_scorer_initialization(self):
        """测试价值评分器初始化"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 验证基本属性
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        assert scorer.name == "value_scorer"
        assert scorer.description == "评估股票价格合理性和投资价值"
        
        print("✅ ValueScorer 初始化测试通过")
    
    # ==================== PE评分测试 ====================
    
    def test_pe_score_calculation(self):
        """测试PE评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 测试不同PE值的评分 - 基于实际代码逻辑调整期望值
        test_cases = [
            (5.0, 95, ["极度低估"]),     # PE < 8
            (10.0, 85, ["明显低估"]),    # PE < 12
            (15.0, 75, ["合理偏低"]),    # PE < 18
            (22.0, 60, ["合理"]),        # PE < 25
            (30.0, 40, ["偏高"]),        # PE < 35
            (45.0, 25, ["高估"]),        # PE < 50
            (60.0, 10, ["严重高估"])     # PE >= 50
        ]
        
        for pe_value, expected_score, expected_keywords in test_cases:
            valuation_data = {'pe_ratio': pe_value}
            score, details = scorer._calculate_pe_score(valuation_data)
            
            # 验证评分
            assert score == expected_score, f"PE={pe_value} 期望{expected_score}, 实际{score}"
            assert details["PE比率"] == f"{pe_value:.2f}"
            
            # 验证评价关键词
            pe_evaluation = details.get("PE评价", "")
            keyword_found = any(keyword in pe_evaluation for keyword in expected_keywords)
            assert keyword_found, f"PE评价 '{pe_evaluation}' 中未找到关键词 {expected_keywords}"
            
        print("✅ PE评分计算测试通过")
    
    def test_pe_score_edge_cases(self):
        """测试PE评分边界情况"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 测试无效数据
        invalid_cases = [
            {},                           # 空数据
            {'pe_ratio': None},          # None值
            {'pe_ratio': 0},             # 零值
            {'pe_ratio': -5.0}           # 负值
        ]
        
        for valuation_data in invalid_cases:
            score, details = scorer._calculate_pe_score(valuation_data)
            
            assert score == 50  # 默认分数
            assert "数据缺失或无效" in details.get("PE", "")
        
        print("✅ PE评分边界情况测试通过")
    
    # ==================== PB评分测试 ====================
    
    def test_pb_score_calculation(self):
        """测试PB评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 基于实际代码的PB评分逻辑
        test_cases = [
            (0.6, 95, ["极度低估", "破净"]),
            (1.0, 85, ["明显低估"]),
            (1.5, 70, ["合理"]),
            (2.5, 50, ["一般"]),
            (4.0, 30, ["偏高"]),
            (6.0, 15, ["高估"])
        ]
        
        for pb_value, expected_score, expected_keywords in test_cases:
            valuation_data = {'pb_ratio': pb_value}
            score, details = scorer._calculate_pb_score(valuation_data)
            
            # 验证评分
            assert score == expected_score, f"PB={pb_value} 期望{expected_score}, 实际{score}"
            assert details["PB比率"] == f"{pb_value:.2f}"
            
            # 验证关键词
            pb_evaluation = details.get("PB评价", "")
            keyword_found = any(keyword in pb_evaluation for keyword in expected_keywords)
            assert keyword_found, f"PB评价 '{pb_evaluation}' 中未找到关键词 {expected_keywords}"
            
            # 验证破净情况的特殊标识
            if pb_value < 1.0:
                assert "破净" in details.get("PB评价", "")
        
        print("✅ PB评分计算测试通过")
    
    # ==================== PS评分测试 ====================
    
    def test_ps_score_calculation(self):
        """测试PS评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 基于实际代码的PS评分逻辑
        test_cases = [
            (0.8, 90, ["极度低估"]),
            (1.5, 80, ["低估"]),
            (2.5, 65, ["合理"]),
            (4.0, 50, ["一般"]),
            (6.0, 35, ["偏高"]),
            (10.0, 20, ["高估"])
        ]
        
        for ps_value, expected_score, expected_keywords in test_cases:
            valuation_data = {'ps_ratio': ps_value}
            score, details = scorer._calculate_ps_score(valuation_data)
            
            # 验证评分
            assert score == expected_score, f"PS={ps_value} 期望{expected_score}, 实际{score}"
            assert details["PS比率"] == f"{ps_value:.2f}"
            
            # 验证关键词
            ps_evaluation = details.get("PS评价", "")
            keyword_found = any(keyword in ps_evaluation for keyword in expected_keywords)
            assert keyword_found, f"PS评价 '{ps_evaluation}' 中未找到关键词 {expected_keywords}"
            
        print("✅ PS评分计算测试通过")
    
    # ==================== 股息率评分测试 ====================
    
    def test_dividend_score_calculation(self):
        """测试股息率评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 基于实际代码的股息率评分逻辑
        test_cases = [
            (6.5, 90, ["高股息"]),        # >= 6%
            (4.5, 80, ["良好股息"]),      # >= 4%
            (2.5, 60, ["适中股息"]),      # >= 2%
            (1.2, 40, ["低股息"]),        # >= 1%
            (0.5, 20, ["无或极低股息"])   # < 1%
        ]
        
        for dividend_yield, expected_score, expected_keywords in test_cases:
            financial_data = {'dividend_yield': dividend_yield}
            score, details = scorer._calculate_dividend_score(financial_data)
            
            # 验证评分
            assert score == expected_score, f"股息率={dividend_yield}% 期望{expected_score}, 实际{score}"
            assert details["股息率"] == f"{dividend_yield:.2f}%"
            
            # 验证关键词
            dividend_evaluation = details.get("股息评价", "")
            keyword_found = any(keyword in dividend_evaluation for keyword in expected_keywords)
            assert keyword_found, f"股息评价 '{dividend_evaluation}' 中未找到关键词 {expected_keywords}"
            
        print("✅ 股息率评分计算测试通过")
    
    def test_dividend_stability_adjustment(self):
        """测试股息稳定性调整"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 基础数据
        base_data = {'dividend_yield': 3.0}
        base_score, _ = scorer._calculate_dividend_score(base_data)
        
        # 测试不同稳定性的影响
        stability_cases = [
            (0.9, "稳定", 10),       # 高稳定性，加分
            (0.7, "较稳定", 5),      # 中稳定性，少量加分
            (0.5, "一般", 0),        # 一般稳定性，不调整
            (0.2, "不稳定", -10)     # 低稳定性，减分
        ]
        
        for stability, expected_desc, expected_adjustment in stability_cases:
            data_with_stability = {
                'dividend_yield': 3.0,
                'dividend_stability': stability
            }
            
            score, details = scorer._calculate_dividend_score(data_with_stability)
            
            # 验证稳定性描述存在
            assert "稳定性" in details
            # 验证分数在合理范围内
            assert 10 <= score <= 100
            
            # 验证稳定性调整效果
            if expected_adjustment > 0:
                assert score >= base_score, f"高稳定性应该加分: {score} vs {base_score}"
            elif expected_adjustment < 0:
                assert score <= base_score, f"低稳定性应该减分: {score} vs {base_score}"
        
        print("✅ 股息稳定性调整测试通过")
    
    # ==================== PEG评分测试 ====================
    
    def test_peg_score_calculation(self):
        """测试PEG评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 基于实际代码的PEG评分逻辑
        test_cases = [
            (10.0, 25.0, 0.4, 95, ["极度低估"]),    # PEG < 0.5
            (15.0, 20.0, 0.75, 85, ["低估"]),       # PEG < 1.0
            (18.0, 15.0, 1.2, 70, ["合理"]),        # PEG < 1.5
            (20.0, 12.0, 1.67, 55, ["一般"]),       # PEG < 2.0
            (25.0, 10.0, 2.5, 40, ["偏高"]),        # PEG < 3.0
            (30.0, 8.0, 3.75, 25, ["高估"])         # PEG >= 3.0
        ]
        
        for pe, growth, expected_peg, expected_score, expected_keywords in test_cases:
            valuation_data = {'pe_ratio': pe}
            financial_data = {'net_profit_growth': growth}
            
            score, details = scorer._calculate_peg_score(valuation_data, financial_data)
            
            # 验证PEG计算正确性
            calculated_peg = float(details["PEG比率"])
            assert abs(calculated_peg - expected_peg) < 0.1, f"PEG计算错误: 期望{expected_peg}, 实际{calculated_peg}"
            
            # 验证评分
            assert score == expected_score, f"PEG={expected_peg} 期望{expected_score}, 实际{score}"
            
            # 验证关键词
            peg_evaluation = details.get("PEG评价", "")
            keyword_found = any(keyword in peg_evaluation for keyword in expected_keywords)
            assert keyword_found, f"PEG评价 '{peg_evaluation}' 中未找到关键词 {expected_keywords}"
            
        print("✅ PEG评分计算测试通过")
    
    def test_peg_score_edge_cases(self):
        """测试PEG评分边界情况"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 测试无效情况
        edge_cases = [
            ({}, {}, "数据缺失"),                              # 空数据
            ({'pe_ratio': 20}, {}, "数据缺失"),               # 缺少增长率
            ({'pe_ratio': 20}, {'net_profit_growth': 0}, "增长率为负或零"),     # 零增长
            ({'pe_ratio': 20}, {'net_profit_growth': -5}, "增长率为负或零")     # 负增长
        ]
        
        for val_data, fin_data, expected_desc in edge_cases:
            score, details = scorer._calculate_peg_score(val_data, fin_data)
            print(f"测试用例: {val_data}, {fin_data}")
            print(f"返回: score={score}, details={details}")
            
            # 先验证基本的分数范围
            assert isinstance(score, (int, float)), f"评分应该是数字，实际: {type(score)}"
            assert 0 <= score <= 100, f"评分应该在0-100之间,实际: {score}"
            
            # 验证错误描述存在
            assert "PEG" in details, f"详情中应该包含PEG字段,实际: {details}"
            
            # 验证错误描述
            peg_desc = details.get("PEG", "")
            assert expected_desc in peg_desc, f"期望包含 '{expected_desc}', 实际 '{peg_desc}'"
        
        print("✅ PEG评分边界情况测试通过")
    
    # ==================== 综合评分测试 ====================
    
    def test_calculate_score_detailed_success(self):
        """测试完整的详细评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        score, details = scorer.calculate_score_detailed("000002")
        
        # 验证返回结构
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100
        assert isinstance(details, dict)
        
        # 验证包含所有评分维度
        expected_dimensions = [
            "PE估值分析", "PB估值分析", "PS估值分析", 
            "股息率分析", "PEG分析", "相对估值", "价值评分"
        ]
        
        for dimension in expected_dimensions:
            assert dimension in details, f"缺少维度: {dimension}"
        
        # 验证数据库调用
        self.mock_db_manager.get_valuation_data.assert_called_with("000002", self.base_date)
        self.mock_db_manager.get_financial_data.assert_called_with("000002", self.base_date)
        
        print("✅ 详细评分计算测试通过")
    
    def test_calculate_score_no_data(self):
        """测试无数据情况的处理"""
        
        # 设置无数据返回
        self.mock_db_manager.get_valuation_data.return_value = {}
        self.mock_db_manager.get_financial_data.return_value = {}
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        score, details = scorer.calculate_score_detailed("INVALID")
        
        # 验证默认处理
        assert score == 50.0
        assert "错误" in details
        assert "缺少估值数据" in details["错误"]
        
        print("✅ 无数据情况处理测试通过")
    
    def test_calculate_score_simple(self):
        """测试简单评分计算"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        score = scorer.calculate_score("000002")
        
        # 验证返回简单分数
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100
        
        print("✅ 简单评分计算测试通过")
    
    # ==================== 数据获取测试 ====================
    
    def test_get_valuation_data_success(self):
        """测试估值数据获取成功"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        data = scorer.get_valuation_data("000002")
        
        # 验证数据获取
        assert data == self.standard_valuation_data
        # 验证数据库方法被正确调用
        self.mock_db_manager.get_valuation_data.assert_called_with("000002", self.base_date)
        
        print("✅ 估值数据获取测试通过")
    
    def test_get_valuation_data_failure(self):
        """测试估值数据获取失败"""
        
        # 模拟数据库异常
        self.mock_db_manager.get_valuation_data.side_effect = Exception("数据库错误")
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        data = scorer.get_valuation_data("000002")
        
        # 验证异常处理
        assert data == {}
        
        print("✅ 估值数据获取失败处理测试通过")
    
    def test_get_financial_data_success(self):
        """测试财务数据获取成功"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        data = scorer.get_financial_data("000002")
        
        # 验证数据获取
        assert data == self.standard_financial_data
        # 验证数据库方法被正确调用
        self.mock_db_manager.get_financial_data.assert_called_with("000002", self.base_date)
        
        print("✅ 财务数据获取测试通过")
    
    # ==================== 相对估值测试 ====================
    
    @patch.object(ValueScorer, '_get_industry_average_valuation')
    def test_relative_valuation_calculation(self, mock_industry_avg):
        """测试相对估值计算"""
        
        # 设置行业平均数据
        mock_industry_avg.return_value = {
            'avg_pe': 20.0,
            'avg_pb': 2.5,
            'avg_ps': 3.0
        }
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 测试低于行业平均的情况
        valuation_data = {
            'pe_ratio': 15.0,  # 低于行业平均20.0
            'pb_ratio': 2.0    # 低于行业平均2.5
        }
        
        score, details = scorer._calculate_relative_valuation("000002", valuation_data)
        
        # 验证相对估值分析
        assert "PE相对比" in details
        assert "PB相对比" in details
        # 验证分数在合理范围内
        assert 0 <= score <= 100
        
        print("✅ 相对估值计算测试通过")
    
    @patch.object(ValueScorer, '_get_industry_average_valuation')
    def test_relative_valuation_no_industry_data(self, mock_industry_avg):
        """测试无行业数据的相对估值"""
        
        # 设置无行业数据
        mock_industry_avg.return_value = None
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        valuation_data = {'pe_ratio': 15.0}
        score, details = scorer._calculate_relative_valuation("000002", valuation_data)
        
        # 验证无数据处理
        assert score == 50
        assert "行业数据缺失" in details.get("相对估值", "")
        
        print("✅ 无行业数据相对估值测试通过")
    
    # ==================== 错误处理测试 ====================
    
    def test_calculation_with_database_error(self):
        """测试数据库错误时的处理"""
        
        # 模拟数据库异常
        self.mock_db_manager.get_valuation_data.side_effect = Exception("数据库连接失败")
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        score = scorer.calculate_score("000002")
        
        # 验证异常处理
        assert score == 50.0  # 默认分数
        
        print("✅ 数据库错误处理测试通过")
    
    def test_partial_data_handling(self):
        """测试部分数据缺失的处理"""
        
        # 设置部分数据缺失 - 确保返回有效的字典而不是None
        self.mock_db_manager.get_valuation_data.return_value = {
            'pe_ratio': 18.5,
            # 缺少 pb_ratio, ps_ratio
        }
        
        self.mock_db_manager.get_financial_data.return_value = {
            'dividend_yield': 2.8,
            # 缺少 net_profit_growth
        }
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        score, details = scorer.calculate_score_detailed("000002")
        
        # 验证能够处理部分数据
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100
        
        # PE数据存在，应该有PE分析
        assert "PE估值分析" in details
        
        print("✅ 部分数据缺失处理测试通过")
    
    # ==================== 极端值测试 ====================
    
    def test_extreme_values_handling(self):
        """测试极端值处理"""
        
        scorer = ValueScorer(self.mock_db_manager, self.base_date)
        
        # 极端值测试用例
        extreme_cases = [
            {
                "name": "极大PE值",
                "data": {'pe_ratio': 999.99, 'pb_ratio': 1.0}
            },
            {
                "name": "极小PE值", 
                "data": {'pe_ratio': 0.1, 'pb_ratio': 1.0}
            },
            {
                "name": "极大PB值",
                "data": {'pe_ratio': 15.0, 'pb_ratio': 100.0}
            },
            {
                "name": "极小PB值",
                "data": {'pe_ratio': 15.0, 'pb_ratio': 0.1}
            }
        ]
        
        for case in extreme_cases:
            self.mock_db_manager.get_valuation_data.return_value = case['data']
            self.mock_db_manager.get_financial_data.return_value = {}
            
            score, details = scorer.calculate_score_detailed("TEST")
            
            # 验证程序不会崩溃
            assert isinstance(score, (int, float))
            assert 0 <= score <= 100
            
            print(f"  ✅ {case['name']} 处理正确，评分: {score}")
        
        print("✅ 极端值处理测试通过")

# ==================== 运行所有测试的函数 ====================

def test_run_all_value_scorer_tests():
    """运行所有 ValueScorer 测试"""
    print("🧪 开始运行 ValueScorer 所有测试...")
    
    test_instance = TestValueScorer()
    
    test_methods = [
        "test_value_scorer_initialization",
        "test_pe_score_calculation", 
        "test_pe_score_edge_cases",
        "test_pb_score_calculation",
        "test_ps_score_calculation",
        "test_dividend_score_calculation",
        "test_dividend_stability_adjustment",
        "test_peg_score_calculation",
        "test_peg_score_edge_cases",
        "test_calculate_score_detailed_success",
        "test_calculate_score_no_data",
        "test_calculate_score_simple",
        "test_get_valuation_data_success",
        "test_get_valuation_data_failure",
        "test_get_financial_data_success",
        "test_relative_valuation_calculation",
        "test_relative_valuation_no_industry_data",
        "test_calculation_with_database_error",
        "test_partial_data_handling",
        "test_extreme_values_handling"
    ]
    
    passed = 0
    failed = 0
    
    for method_name in test_methods:
        try:
            print(f"\n🔍 执行: {method_name}")
            test_instance.setup_method()  # 重新设置模拟数据
            test_method = getattr(test_instance, method_name)
            test_method()
            passed += 1
        except Exception as e:
            print(f"❌ {method_name} 失败: {e}")
            print(f"详细错误: {traceback.format_exc()}")
            failed += 1
    
    print(f"\n📊 ValueScorer 测试结果:")
    print(f"✅ 通过: {passed}")
    print(f"❌ 失败: {failed}")
    print(f"📈 通过率: {passed/(passed+failed)*100:.1f}%")
    
    return passed, failed

if __name__ == "__main__":
    test_run_all_value_scorer_tests()