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

class TestTechnicalScorer:
    """技术面评分器测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 模拟股票基本信息
        self.mock_db_manager.get_stock_info.return_value = {
            "symbol": "000002",
            "name": "万科A",
            "industry": "房地产开发"
        }
        
        # 生成技术分析用的价格数据
        self.mock_price_data = self._generate_technical_data()
        self.mock_db_manager.get_volume_price_data.return_value = self.mock_price_data
        
        # 尝试导入技术面评分器
        try:
            from scorers.technical_scorer import TechnicalScorer
            self.TechnicalScorer = TechnicalScorer
        except ImportError as e:
            pytest.skip(f"无法导入 TechnicalScorer: {e}")
    
    def _generate_technical_data(self, days=60):
        """生成技术分析用的价格数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 12.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 创建一些技术形态
            if i < 20:  # 前20天：下降
                trend = -0.001 * (20 - i)
            elif i < 40:  # 中20天：盘整
                trend = random.uniform(-0.005, 0.005)
            else:  # 后20天：上升
                trend = 0.002 * (i - 40)
            
            volatility = random.uniform(-0.02, 0.02)
            price = base_price * (1 + trend + volatility)
            
            # 成交量与价格趋势相关
            if trend > 0:
                volume = random.randint(1200000, 2000000)  # 上涨放量
            else:
                volume = random.randint(800000, 1200000)   # 下跌缩量
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 0.998, 2),
                "high": round(price * 1.015, 2),
                "low": round(price * 0.985, 2),
                "close": round(price, 2),
                "vol": volume,
                "amount": round(price * volume)
            })
        
        return price_data
    
    def test_technical_scorer_import(self):
        """测试技术面评分器是否能正确导入"""
        assert self.TechnicalScorer is not None
        print("✅ TechnicalScorer 导入成功")
    
    def test_technical_scorer_initialization(self):
        """测试技术面评分器初始化"""
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        print("✅ TechnicalScorer 初始化成功")
    
    def test_calculate_score_with_bullish_pattern(self):
        """测试多头技术形态的评分"""
        # 生成多头排列的数据
        bullish_data = self._generate_bullish_pattern_data()
        self.mock_db_manager.get_volume_price_data.return_value = bullish_data
        
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score > 60  # 多头形态应该得到较高分数
        print(f"✅ 多头技术形态评分: {score:.2f}")
    
    def test_calculate_score_with_bearish_pattern(self):
        """测试空头技术形态的评分"""
        # 生成空头排列的数据
        bearish_data = self._generate_bearish_pattern_data()
        self.mock_db_manager.get_volume_price_data.return_value = bearish_data
        
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score < 50  # 空头形态应该得到较低分数
        print(f"✅ 空头技术形态评分: {score:.2f}")
    
    def test_calculate_score_detailed(self):
        """测试详细评分功能"""
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, float)
        assert isinstance(details, dict)
        assert 0 <= score <= 100
        
        # 检查详细信息结构
        expected_keys = ["均线系统", "量价配合", "技术指标", "趋势强度"]
        for key in expected_keys:
            assert key in details, f"详细信息中缺少 {key}"
        
        print(f"✅ 技术面详细评分: {score:.2f}")
        print("✅ 技术面详细信息结构正确")
        
        for key, value in details.items():
            print(f"  {key}: {value}")
    
    def test_ma_system_calculation(self):
        """测试均线系统计算"""
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        
        # 创建DataFrame用于测试
        df = pd.DataFrame(self.mock_price_data)
        df['close'] = pd.to_numeric(df['close'])
        
        ma_score, ma_details = scorer._calculate_ma_score(df)
        
        assert isinstance(ma_score, (int, float))
        assert 0 <= ma_score <= 100
        assert isinstance(ma_details, dict)
        
        print(f"✅ 均线系统评分: {ma_score:.2f}")
        print("✅ 均线系统详细信息:")
        for key, value in ma_details.items():
            print(f"  {key}: {value}")
    
    def test_rsi_calculation(self):
        """测试RSI指标计算"""
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        
        # 创建价格序列
        prices = pd.Series([12.0, 12.1, 12.05, 11.95, 12.2, 12.3, 12.15, 12.4, 12.35, 12.5])
        
        rsi = scorer._calculate_rsi(prices, period=5)
        
        assert isinstance(rsi, pd.Series)
        assert len(rsi) == len(prices)
        
        # RSI 应该在 0-100 之间
        valid_rsi = rsi.dropna()
        if len(valid_rsi) > 0:
            assert all(0 <= val <= 100 for val in valid_rsi), "RSI值应该在0-100之间"
        
        print(f"✅ RSI计算正确，最新值: {rsi.iloc[-1]:.1f}")
    
    def test_insufficient_data(self):
        """测试数据不足的情况"""
        # 只提供少量数据
        insufficient_data = self.mock_price_data[:5]
        self.mock_db_manager.get_volume_price_data.return_value = insufficient_data
        
        scorer = self.TechnicalScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert score == 50.0  # 数据不足应该返回默认分数
        assert "错误" in details
        print("✅ 数据不足情况处理正确")
    
    def _generate_bullish_pattern_data(self, days=60):
        """生成多头技术形态数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 10.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 持续上升趋势，形成多头排列
            trend = 0.005 * i  # 每天0.5%的上升
            volatility = random.uniform(-0.01, 0.02)  # 略偏正向的波动
            
            price = base_price * (1 + trend + volatility)
            volume = random.randint(1200000, 2000000)  # 上涨放量
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 0.995, 2),
                "high": round(price * 1.02, 2),
                "low": round(price * 0.99, 2),
                "close": round(price, 2),
                "vol": volume,
                "amount": round(price * volume)
            })
        
        return price_data
    
    def _generate_bearish_pattern_data(self, days=60):
        """生成空头技术形态数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 15.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 持续下降趋势，形成空头排列
            trend = -0.003 * i  # 每天-0.3%的下降
            volatility = random.uniform(-0.02, 0.01)  # 略偏负向的波动
            
            price = base_price * (1 + trend + volatility)
            volume = random.randint(800000, 1200000)  # 下跌缩量
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 1.005, 2),
                "high": round(price * 1.01, 2),
                "low": round(price * 0.98, 2),
                "close": round(price, 2),
                "vol": volume,
                "amount": round(price * volume)
            })
        
        return price_data

    def _generate_strong_bearish_pattern_data(self, days=60):
        """生成强烈空头技术形态数据"""
        import random
        from datetime import datetime, timedelta
        
        base_dt = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 15.0
        
        for i in range(days):
            current_date = (base_dt - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 🔧 更强的下降趋势
            trend = -0.006 * i  # 从 -0.003 改为 -0.006，每天-0.6%的下降
            volatility = random.uniform(-0.025, 0.005)  # 更多负向波动
            
            price = base_price * (1 + trend + volatility)
            
            # 确保价格不会太低
            price = max(price, base_price * 0.2)
            
            # 下跌缩量更明显
            volume = random.randint(600000, 900000)  # 更低的成交量

            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 1.008, 2),  # 开盘略高
                "high": round(price * 1.012, 2),  # 高点不高
                "low": round(price * 0.975, 2),   # 低点更低
                "close": round(price, 2),
                "vol": volume,
                "amount": round(price * volume)
            })
        
        return price_data


    def _generate_extreme_bearish_data(self, days=60):
        """生成极端空头数据"""
        import random
        from datetime import datetime, timedelta
        
        base_dt = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 20.0  # 更高的起始价格
        
        for i in range(days):
            current_date = (base_dt - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 极端下降趋势
            trend = -0.015 * i  # 每天-1.5%的下降
            volatility = random.uniform(-0.03, -0.01)  # 只有负向波动
            
            price = base_price * (1 + trend + volatility)
            price = max(price, base_price * 0.1)  # 最低10%
            
            # 极低成交量
            volume = random.randint(300000, 600000)
            
            price_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "open": round(price * 1.02, 2),   # 高开
                "high": round(price * 1.03, 2),   # 冲高
                "low": round(price * 0.96, 2),    # 大幅下跌
                "close": round(price, 2),         # 低收
                "vol": volume,
                "amount": round(price * volume)
            })
        
        return price_data

def test_run_all_technical_tests():
    """运行所有技术面评分器测试的便捷函数"""
    print("\n🔍 开始测试技术面评分器...")
    
    test_class = TestTechnicalScorer()
    test_class.setup_method()
    
    try:
        test_class.test_technical_scorer_import()
        test_class.test_technical_scorer_initialization()
        test_class.test_calculate_score_with_bullish_pattern()
        test_class.test_calculate_score_with_bearish_pattern()
        test_class.test_calculate_score_detailed()
        test_class.test_ma_system_calculation()
        test_class.test_rsi_calculation()
        test_class.test_insufficient_data()
        
        print("\n🎉 所有技术面评分器测试通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        raise

if __name__ == "__main__":
    test_run_all_technical_tests()