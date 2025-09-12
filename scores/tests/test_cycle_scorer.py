import pytest
import sys
import os
import pandas as pd
import numpy as np
from unittest.mock import Mock, patch

# 添加路径以便导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

class TestCycleScorer:
    """周期评分器测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 模拟股票基本信息
        self.mock_db_manager.get_stock_info.return_value = {
            "symbol": "000002",
            "name": "万科A",
            "industry": "房地产开发",
            "total_share": 1085640.52
        }
        
        # 生成模拟的历史价格数据（60天）
        self.mock_price_data = self._generate_mock_price_data()
        self.mock_db_manager.get_volume_price_data.return_value = self.mock_price_data
        
        # 尝试导入周期评分器
        try:
            from scorers.cycle_scorer import CycleScorer
            self.CycleScorer = CycleScorer
        except ImportError as e:
            pytest.skip(f"无法导入 CycleScorer: {e}")
    
    def _generate_mock_price_data(self, days=300):
        """生成模拟的价格数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        
        base_price = 12.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 添加一些趋势和波动
            trend = 0.001 * i  # 轻微上升趋势
            volatility = random.uniform(-0.03, 0.03)  # 3%的日波动
            
            price = base_price * (1 + trend + volatility)
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 0.995, 2),
                "high": round(price * 1.02, 2),
                "low": round(price * 0.985, 2),
                "close": round(price, 2),
                "vol": random.randint(800000, 2000000),
                "amount": round(price * random.randint(800000, 2000000))
            })
        
        return price_data
    
    def test_cycle_scorer_import(self):
        """测试周期评分器是否能正确导入"""
        assert self.CycleScorer is not None
        print("✅ CycleScorer 导入成功")
    
    def test_cycle_scorer_initialization(self):
        """测试周期评分器初始化"""
        scorer = self.CycleScorer(self.mock_db_manager, self.base_date)
        
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        print("✅ CycleScorer 初始化成功")
    
    def test_calculate_score_with_uptrend_data(self):
        """测试上升趋势数据的评分"""
        # 生成上升趋势数据
        uptrend_data = self._generate_uptrend_data()
        self.mock_db_manager.get_volume_price_data.return_value = uptrend_data
        
        scorer = self.CycleScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score > 60  # 上升趋势应该得到较高分数
        print(f"✅ 上升趋势评分: {score:.2f}")
    
    def test_calculate_score_with_downtrend_data(self):
        """测试下降趋势数据的评分"""
        # 生成下降趋势数据
        downtrend_data = self._generate_downtrend_data()
        self.mock_db_manager.get_volume_price_data.return_value = downtrend_data
        
        scorer = self.CycleScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        print("downstream score",score)
        print(f"评分详情: {details}")
        assert isinstance(score, float)
        
        # 🎯 合理的范围断言
        assert 30 <= score <= 65, f"下降趋势评分应在合理范围内，实际: {score}"
        
        # 🔍 检查是否确实识别了下降特征
        details_str = str(details)
        downtrend_indicators = ["下降", "调整", "空头", "低点", "❌"]
        has_downtrend_signs = any(indicator in details_str for indicator in downtrend_indicators)
        
        assert has_downtrend_signs, "评分详情应该包含下降趋势的特征"
        
        print(f"✅ 下降趋势评分 {score:.2f} 在合理范围内")
    
    def test_calculate_score_detailed(self):
        """测试详细评分功能"""
        scorer = self.CycleScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, float)
        assert isinstance(details, dict)
        assert 0 <= score <= 100
        
        # 检查详细信息结构
        expected_keys = ["短期周期(5-10日)", "中期周期(20-60日)", "长期周期(120-250日)"]
        for key in expected_keys:
            assert key in details, f"详细信息中缺少 {key}"
        
        print(f"✅ 周期详细评分: {score:.2f}")
        print("✅ 周期详细信息结构正确")
        
        for key, value in details.items():
            print(f"  {key}: {value}")
    
    def test_insufficient_data(self):
        """测试数据不足的情况"""
        # 只提供少量数据
        insufficient_data = self.mock_price_data[:10]
        self.mock_db_manager.get_volume_price_data.return_value = insufficient_data
        
        scorer = self.CycleScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert score == 50.0  # 数据不足应该返回默认分数
        assert "错误" in details
        print("✅ 数据不足情况处理正确")
    
    def _generate_uptrend_data(self, days=300):
        """生成上升趋势数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 10.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 明显的上升趋势
            trend = 0.003 * i  # 每天0.3%的上升
            volatility = random.uniform(-0.02, 0.02)
            
            price = base_price * (1 + trend + volatility)
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 0.998, 2),
                "high": round(price * 1.015, 2),
                "low": round(price * 0.992, 2),
                "close": round(price, 2),
                "vol": random.randint(1000000, 1500000),
                "amount": round(price * random.randint(1000000, 1500000))
            })
        
        return price_data
    
    def _generate_downtrend_data(self, days=300):
        """生成下降趋势数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 15.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 明显的下降趋势
            trend = -0.002 * i  # 每天-0.2%的下降
            volatility = random.uniform(-0.02, 0.02)
            
            price = base_price * (1 + trend + volatility)
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 1.002, 2),
                "high": round(price * 1.008, 2),
                "low": round(price * 0.985, 2),
                "close": round(price, 2),
                "vol": random.randint(800000, 1200000),
                "amount": round(price * random.randint(800000, 1200000))
            })
        
        return price_data

def test_run_all_cycle_tests():
    """运行所有周期评分器测试的便捷函数"""
    print("\n🔍 开始测试周期评分器...")
    
    test_class = TestCycleScorer()
    test_class.setup_method()
    
    try:
        test_class.test_cycle_scorer_import()
        test_class.test_cycle_scorer_initialization()
        test_class.test_calculate_score_with_uptrend_data()
        test_class.test_calculate_score_with_downtrend_data()
        test_class.test_calculate_score_detailed()
        test_class.test_insufficient_data()
        
        print("\n🎉 所有周期评分器测试通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        raise

if __name__ == "__main__":
    test_run_all_cycle_tests()