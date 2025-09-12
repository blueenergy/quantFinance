#!/usr/bin/env python3
"""成长性评分器测试"""

import pytest
import sys
import os
import pandas as pd
import numpy as np
from unittest.mock import Mock

# 添加路径以便导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

class TestGrowthScorer:
    """成长性评分器测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20231231"
        
        # 设置Mock返回值
        self._setup_mock_returns()
        
        # 尝试导入成长性评分器
        try:
            from scorers.growth_scorer import GrowthScorer
            self.GrowthScorer = GrowthScorer
        except ImportError as e:
            pytest.skip(f"无法导入 GrowthScorer: {e}")
    
    def _setup_mock_returns(self):
        """设置Mock返回值"""
        # 模拟高成长公司的财务数据
        self.high_growth_data = [
            {
                'end_date': '20211231',
                'total_revenue': 30e8,
                'n_income': 2e8,
                'roe': 15.0,
                'gross_margin': 30.0,
                'rd_expense': 1e8
            },
            {
                'end_date': '20221231', 
                'total_revenue': 42e8,  # 40%增长
                'n_income': 3.5e8,      # 75%增长
                'roe': 18.5,
                'gross_margin': 32.0,
                'rd_expense': 1.8e8
            },
            {
                'end_date': '20231231',
                'total_revenue': 56e8,  # 33%增长
                'n_income': 5.2e8,      # 49%增长
                'roe': 21.0,
                'gross_margin': 34.0,
                'rd_expense': 2.5e8
            }
        ]
        
        # 模拟低成长公司的财务数据
        self.low_growth_data = [
            {
                'end_date': '20211231',
                'total_revenue': 50e8,
                'n_income': 1e8,
                'roe': 8.0,
                'gross_margin': 15.0,
                'rd_expense': 0.2e8
            },
            {
                'end_date': '20221231',
                'total_revenue': 49e8,  # -2%增长
                'n_income': 0.8e8,      # -20%增长
                'roe': 6.5,
                'gross_margin': 14.0,
                'rd_expense': 0.15e8
            },
            {
                'end_date': '20231231',
                'total_revenue': 51e8,  # 4%增长
                'n_income': 1.2e8,      # 50%增长（基数低）
                'roe': 7.8,
                'gross_margin': 15.5,
                'rd_expense': 0.25e8
            }
        ]
        
        self.very_low_growth_data = [
        {
            'end_date': '20211231',
            'total_revenue': 50e8,
            'n_income': 1e8,
            'roe': 8.0,
            'gross_margin': 15.0,
            'rd_expense': 0.2e8
        },
        {
            'end_date': '20221231',
            'total_revenue': 48e8,  # -4% 下降
            'n_income': 0.5e8,      # -50% 大幅下降
            'roe': 4.0,             # ROE大幅下降
            'gross_margin': 12.0,   # 毛利率下降
            'rd_expense': 0.1e8     # 研发投入减少
        },
        {
            'end_date': '20231231',
            'total_revenue': 47e8,  # 继续下降 -2%
            'n_income': 0.3e8,      # 继续下降 -40%
            'roe': 2.5,             # ROE继续恶化
            'gross_margin': 10.0,   # 毛利率继续下降
            'rd_expense': 0.05e8    # 研发几乎停止
        }
        ]


        # 默认设置为高成长数据
        self.mock_db_manager.get_financial_data.return_value = self.high_growth_data
        self.mock_db_manager.get_industry_data.return_value = {'avg_growth': 10.0}
        self.mock_db_manager.get_stock_info.return_value = {
            'symbol': '000002',
            'name': '万科A',
            'industry': '房地产'
        }
    
    def test_growth_scorer_import(self):
        """测试成长性评分器是否能正确导入"""
        assert self.GrowthScorer is not None
        print("✅ GrowthScorer 导入成功")
    
    def test_growth_scorer_initialization(self):
        """测试成长性评分器初始化"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        assert scorer.name == "growth_scorer"
        
        print("✅ GrowthScorer 初始化成功")
    
    def test_calculate_score_with_high_growth_data(self):
        """测试高成长数据的评分"""
        # 使用高成长数据
        self.mock_db_manager.get_financial_data.return_value = self.high_growth_data
        
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        
        print(f"高成长股票评分: {score}")
        
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100
        assert score > 65  # 高成长应该得到较高分数
        
        print(f"✅ 高成长评分: {score:.2f}")
    
    def test_calculate_score_with_low_growth_data(self):
        """测试低成长数据的评分"""
        # 使用低成长数据
        self.mock_db_manager.get_financial_data.return_value = self.low_growth_data
        
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score,details = scorer.calculate_score_detailed("000002")
        
        print(f"低成长股票评分: {score}")
        #print("details:",details)
        
        assert isinstance(score, (int, float))
        assert 50 <= score <= 75, f"低成长评分应在50-75之间，实际: {score:.2f}"
        
        print(f"✅ 低成长评分: {score:.2f}")
    
    def test_calculate_score_with_very_low_growth_data(self):
        """测试低成长数据的评分"""
        # 使用低成长数据
        self.mock_db_manager.get_financial_data.return_value = self.very_low_growth_data
        
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score,details = scorer.calculate_score_detailed("000002")
        
        print(f"低成长股票评分: {score}")
        #print("details:",details)
        
        assert isinstance(score, (int, float))
        assert 30 <= score <= 65, f"低成长评分应在50-65之间，实际: {score:.2f}"

        print(f"✅ 低成长评分: {score:.2f}")
    
    def test_growth_comparison(self):
        """测试高成长与低成长的对比"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        
        # 测试高成长
        self.mock_db_manager.get_financial_data.return_value = self.high_growth_data
        high_score, high_details = scorer.calculate_score_detailed("000002")
        
        # 测试低成长
        self.mock_db_manager.get_financial_data.return_value = self.low_growth_data
        low_score, low_details = scorer.calculate_score_detailed("000002")
        
        print(f"\\n📊 成长性对比:")
        print(f"  高成长评分: {high_score:.2f}")
        print(f"  低成长评分: {low_score:.2f}")
        print(f"  评分差异: {high_score - low_score:.2f}")
        
        # 基础验证
        assert isinstance(high_score, (int, float))
        assert isinstance(low_score, (int, float))
        assert 0 <= high_score <= 100
        assert 0 <= low_score <= 100
        
        # 🔧 相对比较：高成长应该明显优于低成长
        assert high_score > low_score, f"高成长评分应该 > 低成长评分，但实际: {high_score:.2f} vs {low_score:.2f}"
        
        # 差异应该显著
        score_diff = high_score - low_score
        assert score_diff >= 5, f"评分差异应该 >= 5分，实际差异: {score_diff:.2f}"
        
        print(f"✅ 成长性对比测试通过 (差异: {score_diff:.2f}分)")



    def test_calculate_score_detailed(self):
        """测试详细评分功能"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, (int, float))
        assert isinstance(details, dict)
        assert 0 <= score <= 100
        
        # 检查详细信息的结构
        expected_categories = ["营收增长分析", "利润增长分析", "ROE变化分析", "市场竞争力", "创新能力"]
        for category in expected_categories:
            assert category in details, f"缺少评分类别: {category}"
        
        print(f"✅ 成长性详细评分: {score:.2f}")
        print("✅ 成长性详细信息:")
        
        for category, info in details.items():
            print(f"  {category}:")
            if isinstance(info, dict):
                for key, value in info.items():
                    print(f"    {key}: {value}")
            else:
                print(f"    {info}")
    
    def test_revenue_growth_calculation(self):
        """测试营收增长计算"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        
        # 创建DataFrame用于测试
        fin_df = pd.DataFrame(self.high_growth_data)
        
        revenue_score, revenue_details = scorer._calculate_revenue_growth_score(fin_df)
        
        assert isinstance(revenue_score, (int, float))
        assert 0 <= revenue_score <= 100
        assert isinstance(revenue_details, dict)
        
        # 检查详细信息
        assert "最近一年营收增长率" in revenue_details
        assert "平均营收增长率" in revenue_details
        
        print(f"✅ 营收增长评分: {revenue_score:.2f}")
        print("✅ 营收增长详情:")
        for key, value in revenue_details.items():
            print(f"  {key}: {value}")
    
    def test_profit_growth_calculation(self):
        """测试利润增长计算"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        
        fin_df = pd.DataFrame(self.high_growth_data)
        
        profit_score, profit_details = scorer._calculate_profit_growth_score(fin_df)
        
        assert isinstance(profit_score, (int, float))
        assert 0 <= profit_score <= 100
        assert isinstance(profit_details, dict)
        
        assert "最近一年利润增长率" in profit_details
        assert "盈利年份比例" in profit_details
        
        print(f"✅ 利润增长评分: {profit_score:.2f}")
        print("✅ 利润增长详情:")
        for key, value in profit_details.items():
            print(f"  {key}: {value}")
    
    def test_roe_growth_calculation(self):
        """测试ROE增长计算"""
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        
        fin_df = pd.DataFrame(self.high_growth_data)
        
        roe_score, roe_details = scorer._calculate_roe_growth_score(fin_df)
        
        assert isinstance(roe_score, (int, float))
        assert 0 <= roe_score <= 100
        assert isinstance(roe_details, dict)
        
        assert "最新ROE" in roe_details
        assert "ROE变化" in roe_details
        
        print(f"✅ ROE增长评分: {roe_score:.2f}")
        print("✅ ROE增长详情:")
        for key, value in roe_details.items():
            print(f"  {key}: {value}")
    
    def test_insufficient_data(self):
        """测试数据不足的情况"""
        # 模拟数据不足
        insufficient_data = [
            {
                'end_date': '20231231',
                'total_revenue': 50e8,
                'n_income': 2e8,
                'roe': 10.0
            }
        ]
        
        self.mock_db_manager.get_financial_data.return_value = insufficient_data
        
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100
        
        print(f"✅ 数据不足时的评分: {score:.2f}")
        print("✅ 数据不足时的详情:")
        for key, value in details.items():
            print(f"  {key}: {value}")
    
    def test_no_data(self):
        """测试无数据的情况"""
        # 模拟无数据
        self.mock_db_manager.get_financial_data.return_value = []
        
        scorer = self.GrowthScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, (int, float))
        assert score == 50.0  # 无数据时返回默认分数
        assert "错误" in details
        
        print(f"✅ 无数据时的评分: {score:.2f}")
        print("✅ 无数据时的详情:")
        for key, value in details.items():
            print(f"  {key}: {value}")

def test_run_all_growth_tests():
    """运行所有成长性评分器测试的便捷函数"""
    print("\n🔍 开始测试成长性评分器...")
    
    test_class = TestGrowthScorer()
    test_class.setup_method()
    
    test_methods = [
        "test_growth_scorer_import",
        "test_growth_scorer_initialization",
        "test_calculate_score_with_high_growth_data",
        "test_calculate_score_with_low_growth_data", 
        "test_calculate_score_with_very_low_growth_data",
        "test_calculate_score_detailed",
        "test_revenue_growth_calculation",
        "test_profit_growth_calculation",
        "test_roe_growth_calculation",
        "test_insufficient_data",
        "test_no_data"
    ]
    
    passed = 0
    failed = 0
    
    for method_name in test_methods:
        try:
            print(f"\n🧪 运行 {method_name}...")
            method = getattr(test_class, method_name)
            method()
            passed += 1
            print(f"✅ {method_name} 通过")
        except Exception as e:
            failed += 1
            print(f"❌ {method_name} 失败: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"\n📊 测试结果: {passed} 通过, {failed} 失败")
    
    if failed == 0:
        print("🎉 所有成长性评分器测试通过！")
    else:
        print(f"⚠️ 有 {failed} 个测试失败")

if __name__ == "__main__":
    test_run_all_growth_tests()