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

class TestMoneyFlowScorer:
    """资金流评分器测试类"""
    
    def setup_method(self):
        """每个测试方法执行前的设置"""
        self.mock_db_manager = Mock()
        self.base_date = "20250910"
        
        # 模拟股票基本信息
        self.mock_db_manager.get_stock_info.return_value = {
            "symbol": "000002",
            "name": "万科A",
            "industry": "房地产开发",
            "total_share": 1085640.52  # 万股
        }
        
        # 生成模拟的资金流数据
        self.mock_money_flow_data = self._generate_money_flow_data()
        self.mock_db_manager.get_money_flow_data.return_value = self.mock_money_flow_data
        
        # 生成价格数据
        self.mock_price_data = self._generate_price_data()
        self.mock_db_manager.get_volume_price_data.return_value = self.mock_price_data
        
        # 尝试导入资金流评分器
        try:
            from scorers.money_flow_scorer import MoneyFlowScorer
            self.MoneyFlowScorer = MoneyFlowScorer
        except ImportError as e:
            pytest.skip(f"无法导入 MoneyFlowScorer: {e}")
    
    def _generate_money_flow_data(self, days=30):
        """生成模拟的资金流数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        money_flow_data = []
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 模拟不同大小的资金流
            net_mf_amount_xl = random.randint(-5000000, 8000000)  # 超大单
            net_mf_amount_l = random.randint(-3000000, 5000000)   # 大单
            net_mf_amount_m = random.randint(-2000000, 3000000)   # 中单
            net_mf_amount_s = random.randint(-1000000, 2000000)   # 小单
            
            money_flow_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "net_mf_amount_xl": net_mf_amount_xl,
                "net_mf_amount_l": net_mf_amount_l,
                "net_mf_amount_m": net_mf_amount_m,
                "net_mf_amount_s": net_mf_amount_s,
                "net_mf_vol_xl": abs(net_mf_amount_xl) // 12,  # 假设价格约12元
                "net_mf_vol_l": abs(net_mf_amount_l) // 12,
                "net_mf_vol_m": abs(net_mf_amount_m) // 12,
                "net_mf_vol_s": abs(net_mf_amount_s) // 12
            })
        
        return money_flow_data
    
    def _generate_price_data(self, days=30):
        """生成价格数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        price_data = []
        base_price = 12.0
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            volatility = random.uniform(-0.03, 0.03)
            price = base_price * (1 + volatility)
            volume = random.randint(1000000, 2000000)
            
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
    
    def test_money_flow_scorer_import(self):
        """测试资金流评分器是否能正确导入"""
        assert self.MoneyFlowScorer is not None
        print("✅ MoneyFlowScorer 导入成功")
    
    def test_money_flow_scorer_initialization(self):
        """测试资金流评分器初始化"""
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        
        assert scorer.db_manager == self.mock_db_manager
        assert scorer.base_date == self.base_date
        print("✅ MoneyFlowScorer 初始化成功")
    
    def test_calculate_score_with_positive_flow(self):
        """测试正向资金流的评分"""
        # 生成正向资金流数据
        positive_flow_data = self._generate_positive_flow_data()
        self.mock_db_manager.get_money_flow_data.return_value = positive_flow_data
        
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        print('positive flow',score)
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score > 60  # 正向资金流应该得到较高分数
        print(f"✅ 正向资金流评分: {score:.2f}")
    
    def test_calculate_score_with_negative_flow(self):
        """测试负向资金流的评分"""
        # 生成负向资金流数据
        negative_flow_data = self._generate_negative_flow_data()
        self.mock_db_manager.get_money_flow_data.return_value = negative_flow_data
        
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        score = scorer.calculate_score("000002")
        print('negative flow',score)
        assert isinstance(score, float)
        assert 0 <= score <= 100
        assert score < 50  # 负向资金流应该得到较低分数
        print(f"✅ 负向资金流评分: {score:.2f}")
    
    def test_calculate_score_detailed(self):
        """测试详细评分功能"""
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert isinstance(score, float)
        assert isinstance(details, dict)
        assert 0 <= score <= 100
        
        # 检查详细信息结构
        expected_keys = ["主力资金流向", "资金流强度", "成交活跃度", "机构关注度"]
        for key in expected_keys:
            assert key in details, f"详细信息中缺少 {key}"
        
        print(f"✅ 资金流详细评分: {score:.2f}")
        print("✅ 资金流详细信息结构正确")
        
        for key, value in details.items():
            print(f"  {key}: {value}")
    
    def test_main_flow_calculation(self):
        """测试主力资金流计算"""
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        
        # 创建DataFrame用于测试
        mf_df = pd.DataFrame(self.mock_money_flow_data)
        
        main_flow_score, main_flow_details = scorer._calculate_main_flow_score(mf_df)
        
        assert isinstance(main_flow_score, (int, float))
        assert 0 <= main_flow_score <= 100
        assert isinstance(main_flow_details, dict)
        
        print(f"✅ 主力资金流评分: {main_flow_score:.2f}")
        print("✅ 主力资金流详细信息:")
        for key, value in main_flow_details.items():
            print(f"  {key}: {value}")
    
    def test_activity_score_calculation(self):
        """测试成交活跃度计算"""
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        
        # 创建DataFrame用于测试
        price_df = pd.DataFrame(self.mock_price_data)
        mf_df = pd.DataFrame(self.mock_money_flow_data)
        
        activity_score, activity_details = scorer._calculate_activity_score(price_df, mf_df)
        
        assert isinstance(activity_score, (int, float))
        assert 0 <= activity_score <= 100
        assert isinstance(activity_details, dict)
        
        print(f"✅ 成交活跃度评分: {activity_score:.2f}")
        print("✅ 成交活跃度详细信息:")
        for key, value in activity_details.items():
            print(f"  {key}: {value}")
    
    def test_missing_data_columns(self):
        """测试缺少必要数据列的情况"""
        # 创建缺少必要列的数据
        incomplete_data = [{"symbol": "000002", "trade_date": "20250910"}]
        self.mock_db_manager.get_money_flow_data.return_value = incomplete_data
        
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        print(score)
        print(details)
        assert score == 50.0  # 数据不完整应该返回默认分数
        assert "错误" in details
        print("✅ 数据列缺失情况处理正确")
    
    def test_no_data(self):
        """测试无数据的情况"""
        # 模拟无数据
        self.mock_db_manager.get_money_flow_data.return_value = []
        self.mock_db_manager.get_volume_price_data.return_value = []
        
        scorer = self.MoneyFlowScorer(self.mock_db_manager, self.base_date)
        score, details = scorer.calculate_score_detailed("000002")
        
        assert score == 50.0  # 无数据应该返回默认分数
        assert "错误" in details
        print("✅ 无数据情况处理正确")
    
    def _generate_positive_flow_data(self, days=30):
        """生成正向资金流数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        money_flow_data = []
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 主力资金明显流入
            net_mf_amount_xl = random.randint(2000000, 10000000)  # 超大单流入
            net_mf_amount_l = random.randint(1000000, 8000000)    # 大单流入
            net_mf_amount_m = random.randint(-1000000, 3000000)   # 中单略流入
            net_mf_amount_s = random.randint(-2000000, 1000000)   # 小单略流出
            
            money_flow_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "net_mf_amount_xl": net_mf_amount_xl,
                "net_mf_amount_l": net_mf_amount_l,
                "net_mf_amount_m": net_mf_amount_m,
                "net_mf_amount_s": net_mf_amount_s,
                "net_mf_vol_xl": abs(net_mf_amount_xl) // 12,
                "net_mf_vol_l": abs(net_mf_amount_l) // 12,
                "net_mf_vol_m": abs(net_mf_amount_m) // 12,
                "net_mf_vol_s": abs(net_mf_amount_s) // 12
            })
        
        return money_flow_data
    
    def _generate_negative_flow_data(self, days=30):
        """生成负向资金流数据"""
        import random
        from datetime import datetime, timedelta
        
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        money_flow_data = []
        
        for i in range(days):
            current_date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 主力资金明显流出
            net_mf_amount_xl = random.randint(-10000000, -2000000)  # 超大单流出
            net_mf_amount_l = random.randint(-8000000, -1000000)    # 大单流出
            net_mf_amount_m = random.randint(-3000000, 1000000)     # 中单略流出
            net_mf_amount_s = random.randint(-1000000, 2000000)     # 小单略流入
            
            money_flow_data.append({
                "symbol": "000002",
                "trade_date": current_date,
                "net_mf_amount_xl": net_mf_amount_xl,
                "net_mf_amount_l": net_mf_amount_l,
                "net_mf_amount_m": net_mf_amount_m,
                "net_mf_amount_s": net_mf_amount_s,
                "net_mf_vol_xl": abs(net_mf_amount_xl) // 12,
                "net_mf_vol_l": abs(net_mf_amount_l) // 12,
                "net_mf_vol_m": abs(net_mf_amount_m) // 12,
                "net_mf_vol_s": abs(net_mf_amount_s) // 12
            })
        
        return money_flow_data
    
    def _generate_mock_money_flow_data(self, symbol, days=30):
        """生成模拟资金流数据 - 仅用于测试"""
        import random
        from datetime import datetime, timedelta
        
        mock_data = []
        base_date = datetime.strptime(self.base_date, '%Y%m%d')
        
        for i in range(days):
            date = (base_date - timedelta(days=days-1-i)).strftime('%Y%m%d')
            
            # 生成模拟资金流数据
            xl_flow = random.randint(-5000000, 8000000)  # 超大单：-500万到800万
            l_flow = random.randint(-3000000, 5000000)   # 大单：-300万到500万
            m_flow = random.randint(-2000000, 3000000)   # 中单：-200万到300万
            s_flow = -(xl_flow + l_flow + m_flow) + random.randint(-1000000, 1000000)  # 小单平衡
            
            mock_data.append({
                'symbol': symbol,
                'trade_date': date,
                'net_mf_amount_xl': xl_flow,    # 超大单净额
                'net_mf_amount_l': l_flow,      # 大单净额
                'net_mf_amount_m': m_flow,      # 中单净额
                'net_mf_amount_s': s_flow,      # 小单净额
                'net_mf_vol_xl': abs(xl_flow) // 100,  # 超大单净量（简化）
                'net_mf_vol_l': abs(l_flow) // 100,    # 大单净量
                'net_mf_vol_m': abs(m_flow) // 100,    # 中单净量
                'net_mf_vol_s': abs(s_flow) // 100     # 小单净量
            })
        
        return mock_data

    def _generate_mock_stock_info(self, symbol):
        """生成模拟股票信息 - 仅用于测试"""
        return {
            'symbol': symbol,
            'total_share': 100000,  # 10亿股
            'float_share': 80000,   # 8亿股
            'name': f'股票{symbol}'
        }

def test_run_all_money_flow_tests():
    """运行所有资金流评分器测试的便捷函数"""
    print("\n🔍 开始测试资金流评分器...")
    
    test_class = TestMoneyFlowScorer()
    test_class.setup_method()
    
    try:
        test_class.test_money_flow_scorer_import()
        test_class.test_money_flow_scorer_initialization()
        test_class.test_calculate_score_with_positive_flow()
        test_class.test_calculate_score_with_negative_flow()
        test_class.test_calculate_score_detailed()
        test_class.test_main_flow_calculation()
        test_class.test_activity_score_calculation()
        test_class.test_missing_data_columns()
        test_class.test_no_data()
        
        print("\n🎉 所有资金流评分器测试通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        raise

if __name__ == "__main__":
    test_run_all_money_flow_tests()