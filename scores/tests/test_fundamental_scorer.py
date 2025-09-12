import pytest
import sys
import os
from unittest.mock import Mock, patch, MagicMock

# 添加路径以便导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

class TestFundamentalScorer:
    """基本面评分器测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        # 创建模拟的数据库管理器
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 模拟数据库管理器的方法
        self.mock_db_manager.get_stock_info.return_value = {
            "symbol": "000002",
            "name": "万科A",
            "industry": "房地产开发",
            "total_share": 1085640.52  # 万股
        }
        
        self.mock_db_manager.get_volume_price_data.return_value = [{
            "symbol": "000002",
            "trade_date": "20250910",
            "open": 12.50,
            "high": 12.80,
            "low": 12.30,
            "close": 12.65,
            "vol": 1250000,
            "amount": 15812500
        }]
        
        # 模拟财务数据集合
        mock_financial_collection = Mock()
        self.mock_db_manager.db = {"financial_data": mock_financial_collection}
        
        # 尝试导入基本面评分器
        try:
            from scorers.fundamental_scorer import FundamentalScorer
            self.FundamentalScorer = FundamentalScorer
        except ImportError as e:
            pytest.skip(f"无法导入 FundamentalScorer: {e}")
    
    def test_fundamental_scorer_import(self):
        """测试基本面评分器是否能正确导入"""
        assert self.FundamentalScorer is not None
        print("✅ FundamentalScorer 导入成功")
    
    def test_fundamental_scorer_initialization(self):
        """测试基本面评分器初始化"""
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        print("✅ FundamentalScorer 初始化成功")
    
    def test_calculate_score_with_good_financial_data(self):
        """测试良好财务数据的评分"""
        # 设置良好的财务数据
        good_financial_data = {
            "roe": 0.15,              # 优秀的ROE
            "roa": 0.08,              # 优秀的ROA
            "debt_to_asset": 0.40,    # 良好的资产负债率
            "current_ratio": 2.0,     # 优秀的流动比率
            "net_profit_growth": 0.20, # 优秀的净利润增长率
            "gross_margin": 0.40,     # 优秀的毛利率
            "net_margin": 0.15,       # 优秀的净利率
            "pe_ratio": 12.0,         # 合理的PE
            "pb_ratio": 1.5,          # 合理的PB
            "eps_growth": 0.15        # 良好的EPS增长率
        }
        
        # 模拟财务数据查询返回良好数据
        mock_financial_collection = self.mock_db_manager.db["financial_data"]
        mock_financial_collection.find_one.return_value = good_financial_data
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        # 测试简单评分
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score > 70  # 好的财务数据应该得到高分
        print(f"✅ 良好财务数据评分: {score:.2f}")
    
    def test_calculate_score_with_poor_financial_data(self):
        """测试较差财务数据的评分"""
        # 设置较差的财务数据
        poor_financial_data = {
            "roe": 0.02,              # 较差的ROE
            "roa": 0.01,              # 较差的ROA
            "debt_to_asset": 0.80,    # 高资产负债率
            "current_ratio": 0.8,     # 较差的流动比率
            "net_profit_growth": -0.05, # 负增长
            "gross_margin": 0.10,     # 较差的毛利率
            "net_margin": 0.02,       # 较差的净利率
            "pe_ratio": 45.0,         # 过高的PE
            "pb_ratio": 4.0,          # 较高的PB
            "eps_growth": -0.02       # 负的EPS增长率
        }
        
        # 模拟财务数据查询返回较差数据
        mock_financial_collection = self.mock_db_manager.db["financial_data"]
        mock_financial_collection.find_one.return_value = poor_financial_data
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        # 测试简单评分
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score < 50  # 差的财务数据应该得到低分
        print(f"✅ 较差财务数据评分: {score:.2f}")
    
    def test_calculate_score_with_no_financial_data(self):
        """测试无财务数据的情况"""
        # 模拟没有财务数据
        mock_financial_collection = self.mock_db_manager.db["financial_data"]
        mock_financial_collection.find_one.return_value = None
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        # 测试简单评分
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        # 无数据时应该返回中性评分
        print(f"✅ 无财务数据评分: {score:.2f}")
    
    def test_calculate_score_detailed(self):
        """测试详细评分功能"""
        # 设置中等水平的财务数据
        moderate_financial_data = {
            "roe": 0.10,              # 中等ROE
            "roa": 0.05,              # 中等ROA
            "debt_to_asset": 0.50,    # 中等资产负债率
            "current_ratio": 1.5,     # 中等流动比率
            "net_profit_growth": 0.10, # 中等净利润增长率
            "gross_margin": 0.25,     # 中等毛利率
            "net_margin": 0.08,       # 中等净利率
            "pe_ratio": 20.0,         # 中等PE
            "pb_ratio": 2.0,          # 中等PB
            "eps_growth": 0.08        # 中等EPS增长率
        }
        
        mock_financial_collection = self.mock_db_manager.db["financial_data"]
        mock_financial_collection.find_one.return_value = moderate_financial_data
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        # 测试详细评分
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, float)
        assert isinstance(details, dict)
        assert 0 <= score <= 100
        
        # 检查详细信息的结构
        expected_keys = ["财务健康度", "盈利能力", "估值水平", "市值规模", "行业地位"]
        for key in expected_keys:
            assert key in details, f"详细信息中缺少 {key}"
        
        print(f"✅ 详细评分: {score:.2f}")
        print("✅ 详细信息结构正确")
        
        # 打印详细信息用于调试
        for key, value in details.items():
            print(f"  {key}: {value}")
    
    def test_stock_not_found(self):
        """测试股票信息不存在的情况"""
        # 模拟股票信息不存在
        self.mock_db_manager.get_stock_info.return_value = None
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        score, details = scorer.calculate_score_detailed("INVALID")
        
        assert score == 50.0  # 默认分数
        assert "错误" in details
        print("✅ 股票不存在情况处理正确")
    
    def test_price_data_not_found(self):
        """测试价格数据不存在的情况"""
        # 模拟价格数据不存在
        self.mock_db_manager.get_volume_price_data.return_value = []
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        score, details = scorer.calculate_score_detailed("000002")
        
        assert score == 50.0  # 默认分数
        assert "错误" in details
        print("✅ 价格数据不存在情况处理正确")
    
    def test_financial_health_calculation(self):
        """测试财务健康度计算的具体逻辑"""
        financial_data = {
            "roe": 0.15,           # 应该得到90分
            "roa": 0.08,           # 应该得到90分
            "debt_to_asset": 0.25, # 应该得到90分
            "current_ratio": 2.0   # 应该得到90分
        }
        
        mock_financial_collection = self.mock_db_manager.db["financial_data"]
        mock_financial_collection.find_one.return_value = financial_data
        
        scorer = self.FundamentalScorer(self.mock_db_manager, self.base_date)
        
        # 直接测试财务健康度计算方法
        health_score, health_details = scorer._calculate_financial_health(financial_data)
        
        assert isinstance(health_score, (int, float))
        assert 80 <= health_score <= 100  # 优秀的财务指标应该得高分
        assert isinstance(health_details, dict)
        
        print(f"✅ 财务健康度评分: {health_score:.2f}")
        print("✅ 财务健康度详细信息:")
        for key, value in health_details.items():
            print(f"  {key}: {value}")

def test_run_all_fundamental_tests():
    """运行所有基本面评分器测试的便捷函数"""
    print("\n🔍 开始测试基本面评分器...")
    
    test_class = TestFundamentalScorer()
    test_class.setup_method()
    
    try:
        test_class.test_fundamental_scorer_import()
        test_class.test_fundamental_scorer_initialization()
        test_class.test_calculate_score_with_good_financial_data()
        test_class.test_calculate_score_with_poor_financial_data()
        test_class.test_calculate_score_with_no_financial_data()
        test_class.test_calculate_score_detailed()
        test_class.test_stock_not_found()
        test_class.test_price_data_not_found()
        test_class.test_financial_health_calculation()
        
        print("\n🎉 所有基本面评分器测试通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        raise

if __name__ == "__main__":
    test_run_all_fundamental_tests()