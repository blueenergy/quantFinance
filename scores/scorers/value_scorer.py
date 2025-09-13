#!/usr/bin/env python3
"""
💰 价值评分器 (Value Scorer)

专注于股票价格合理性和投资价值分析，包括：
- PE、PB、PS 等估值指标分析
- 股息率和分红稳定性分析  
- PEG 等综合估值指标
- 相对估值水平分析

注：基本面质量分析已移至 FundamentalScorer
"""

import pandas as pd
import numpy as np
from .base_scorer import BaseScorer
from datetime import datetime, timedelta

class ValueScorer(BaseScorer):
    """价值评分器 - 专注于估值分析"""
    
    def __init__(self, db_manager, base_date):
        super().__init__(db_manager, base_date)
        self.name = "value_scorer"
        self.description = "评估股票价格合理性和投资价值"
    
    def calculate_score(self, symbol):
        """计算价值评分"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 价值评分计算失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的价值评分"""
        details = {}
        
        try:
            # 获取估值数据
            valuation_data = self.get_valuation_data(symbol)
            financial_data = self.get_financial_data(symbol)
            
            if not valuation_data:
                return 50.0, {"错误": "缺少估值数据"}
            
            score = 0
            
            # 1. PE评分 (30%)
            pe_score, pe_details = self._calculate_pe_score(valuation_data)
            details["PE估值分析"] = pe_details
            score += pe_score * 0.3
            
            # 2. PB评分 (25%)
            pb_score, pb_details = self._calculate_pb_score(valuation_data)
            details["PB估值分析"] = pb_details
            score += pb_score * 0.25
            
            # 3. PS评分 (15%)
            ps_score, ps_details = self._calculate_ps_score(valuation_data)
            details["PS估值分析"] = ps_details
            score += ps_score * 0.15
            
            # 4. 股息率评分 (15%)
            dividend_score, dividend_details = self._calculate_dividend_score(financial_data)
            details["股息率分析"] = dividend_details
            score += dividend_score * 0.15
            
            # 5. PEG评分 (10%)
            peg_score, peg_details = self._calculate_peg_score(valuation_data, financial_data)
            details["PEG分析"] = peg_details
            score += peg_score * 0.1
            
            # 6. 相对估值 (5%)
            relative_score, relative_details = self._calculate_relative_valuation(symbol, valuation_data)
            details["相对估值"] = relative_details
            score += relative_score * 0.05
            
            final_score = max(0, min(100, score))
            details["价值评分"] = round(final_score, 2)
            
            return round(final_score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"价值评分计算失败: {e}"}
            return 50.0, error_details
    
    def _calculate_pe_score(self, valuation_data):
        """计算PE评分"""
        details = {}
        
        try:
            pe_ratio = valuation_data.get('pe_ratio', None)
            pe_ttm = valuation_data.get('pe_ttm', pe_ratio)  # 优先使用TTM数据
            
            pe_value = pe_ttm if pe_ttm is not None else pe_ratio
            
            if pe_value is None or pe_value <= 0:
                return 50, {"PE": "数据缺失或无效", "PE值": "N/A"}
            
            details["PE比率"] = f"{pe_value:.2f}"
            
            # PE评分逻辑 - 考虑不同行业的PE水平
            if pe_value < 8:
                score = 95
                details["PE评价"] = "✅ 极度低估"
            elif pe_value < 12:
                score = 85
                details["PE评价"] = "✅ 明显低估"
            elif pe_value < 18:
                score = 75
                details["PE评价"] = "✅ 合理偏低"
            elif pe_value < 25:
                score = 60
                details["PE评价"] = "合理"
            elif pe_value < 35:
                score = 40
                details["PE评价"] = "⚠️ 偏高"
            elif pe_value < 50:
                score = 25
                details["PE评价"] = "❌ 高估"
            else:
                score = 10
                details["PE评价"] = "❌ 严重高估"
            
            details["PE得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"PE计算失败: {e}"
            return 50, details
    
    def _calculate_pb_score(self, valuation_data):
        """计算PB评分"""
        details = {}
        
        try:
            pb_ratio = valuation_data.get('pb_ratio', None)
            
            if pb_ratio is None or pb_ratio <= 0:
                return 50, {"PB": "数据缺失或无效", "PB值": "N/A"}
            
            details["PB比率"] = f"{pb_ratio:.2f}"
            
            # PB评分逻辑
            if pb_ratio < 0.8:
                score = 95
                details["PB评价"] = "✅ 极度低估（破净）"
                details["投资价值"] = "安全边际极高"
            elif pb_ratio < 1.2:
                score = 85
                details["PB评价"] = "✅ 明显低估"
                details["投资价值"] = "安全边际较高"
            elif pb_ratio < 2.0:
                score = 70
                details["PB评价"] = "✅ 合理"
                details["投资价值"] = "适中"
            elif pb_ratio < 3.0:
                score = 50
                details["PB评价"] = "一般"
                details["投资价值"] = "需谨慎"
            elif pb_ratio < 5.0:
                score = 30
                details["PB评价"] = "⚠️ 偏高"
                details["投资价值"] = "风险较高"
            else:
                score = 15
                details["PB评价"] = "❌ 高估"
                details["投资价值"] = "风险很高"
            
            details["PB得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"PB计算失败: {e}"
            return 50, details
    
    def _calculate_ps_score(self, valuation_data):
        """计算PS评分"""
        details = {}
        
        try:
            ps_ratio = valuation_data.get('ps_ratio', None)
            
            if ps_ratio is None or ps_ratio <= 0:
                return 50, {"PS": "数据缺失或无效", "PS值": "N/A"}
            
            details["PS比率"] = f"{ps_ratio:.2f}"
            
            # PS评分逻辑 - 相对PE和PB更宽松，因为PS受盈利能力影响
            if ps_ratio < 1.0:
                score = 90
                details["PS评价"] = "✅ 极度低估"
            elif ps_ratio < 2.0:
                score = 80
                details["PS评价"] = "✅ 低估"
            elif ps_ratio < 3.0:
                score = 65
                details["PS评价"] = "✅ 合理"
            elif ps_ratio < 5.0:
                score = 50
                details["PS评价"] = "一般"
            elif ps_ratio < 8.0:
                score = 35
                details["PS评价"] = "⚠️ 偏高"
            else:
                score = 20
                details["PS评价"] = "❌ 高估"
            
            details["PS得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"PS计算失败: {e}"
            return 50, details
    
    def _calculate_dividend_score(self, financial_data):
        """计算股息率评分"""
        details = {}
        
        try:
            dividend_yield = financial_data.get('dividend_yield', 0)
            dividend_stability = financial_data.get('dividend_stability', None)
            
            details["股息率"] = f"{dividend_yield:.2f}%"
            
            # 基础股息率评分
            if dividend_yield >= 6:
                base_score = 90
                details["股息评价"] = "✅ 高股息"
            elif dividend_yield >= 4:
                base_score = 80
                details["股息评价"] = "✅ 良好股息"
            elif dividend_yield >= 2:
                base_score = 60
                details["股息评价"] = "适中股息"
            elif dividend_yield >= 1:
                base_score = 40
                details["股息评价"] = "低股息"
            else:
                base_score = 20
                details["股息评价"] = "⚠️ 无或极低股息"
            
            # 股息稳定性调整
            if dividend_stability is not None:
                if dividend_stability > 0.8:  # 稳定性好
                    score = min(95, base_score + 10)
                    details["稳定性"] = "✅ 分红稳定"
                elif dividend_stability > 0.6:
                    score = base_score + 5
                    details["稳定性"] = "分红较稳定"
                elif dividend_stability > 0.3:
                    score = base_score
                    details["稳定性"] = "分红一般"
                else:
                    score = max(10, base_score - 10)
                    details["稳定性"] = "⚠️ 分红不稳定"
            else:
                score = base_score
                details["稳定性"] = "数据缺失"
            
            details["股息得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"股息率计算失败: {e}"
            return 50, details
    
    def _calculate_peg_score(self, valuation_data, financial_data):
        """计算PEG评分"""
        details = {}
        try:
            pe_ratio = valuation_data.get('pe_ratio', None)
            # 尝试多个增长率字段
            growth_rate = None
            for field in ['net_profit_growth', 'earnings_growth', 'revenue_growth']:
                if field in financial_data and financial_data[field] is not None:
                    growth_rate = financial_data[field]
                    break
            if pe_ratio is None or growth_rate is None:
                return 50, {"PEG": "数据缺失", "PE": pe_ratio, "增长率": growth_rate}
            
            if growth_rate <= 0:
                return 30, {"PEG": "增长率为负或零", "增长率": f"{growth_rate:.2f}%"}
            
            peg = pe_ratio / growth_rate
            details["PEG比率"] = f"{peg:.2f}"
            details["PE比率"] = f"{pe_ratio:.2f}"
            details["增长率"] = f"{growth_rate:.2f}%"
            
            # PEG评分逻辑
            if peg < 0.5:
                score = 95
                details["PEG评价"] = "✅ 极度低估"
                details["投资建议"] = "成长价值兼备"
            elif peg < 1.0:
                score = 85
                details["PEG评价"] = "✅ 低估"
                details["投资建议"] = "成长性价比高"
            elif peg < 1.5:
                score = 70
                details["PEG评价"] = "✅ 合理"
                details["投资建议"] = "成长性合理"
            elif peg < 2.0:
                score = 55
                details["PEG评价"] = "一般"
                details["投资建议"] = "成长性一般"
            elif peg < 3.0:
                score = 40
                details["PEG评价"] = "⚠️ 偏高"
                details["投资建议"] = "需谨慎评估"
            else:
                score = 25
                details["PEG评价"] = "❌ 高估"
                details["投资建议"] = "成长性价比低"
            
            details["PEG得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"PEG计算失败: {e}"
            return 50, details
    
    def _calculate_relative_valuation(self, symbol, valuation_data):
        """计算相对估值评分"""
        details = {}
        
        try:
            # 获取行业平均估值（如果有的话）
            industry_avg = self._get_industry_average_valuation(symbol)
            
            if not industry_avg:
                return 50, {"相对估值": "行业数据缺失"}
            
            pe_ratio = valuation_data.get('pe_ratio')
            pb_ratio = valuation_data.get('pb_ratio')
            
            score = 50  # 基础分
            relative_score = 0
            
            # PE相对评估
            if pe_ratio and industry_avg.get('avg_pe'):
                pe_relative = pe_ratio / industry_avg['avg_pe']
                details["PE相对比"] = f"{pe_relative:.2f}"
                
                if pe_relative < 0.7:
                    relative_score += 20
                    details["PE相对评价"] = "✅ 显著低于行业"
                elif pe_relative < 0.9:
                    relative_score += 10
                    details["PE相对评价"] = "✅ 低于行业"
                elif pe_relative < 1.1:
                    relative_score += 0
                    details["PE相对评价"] = "与行业持平"
                else:
                    relative_score -= 10
                    details["PE相对评价"] = "⚠️ 高于行业"
            
            # PB相对评估
            if pb_ratio and industry_avg.get('avg_pb'):
                pb_relative = pb_ratio / industry_avg['avg_pb']
                details["PB相对比"] = f"{pb_relative:.2f}"
                
                if pb_relative < 0.7:
                    relative_score += 20
                    details["PB相对评价"] = "✅ 显著低于行业"
                elif pb_relative < 0.9:
                    relative_score += 10
                    details["PB相对评价"] = "✅ 低于行业"
                elif pb_relative < 1.1:
                    relative_score += 0
                    details["PB相对评价"] = "与行业持平"
                else:
                    relative_score -= 10
                    details["PB相对评价"] = "⚠️ 高于行业"
            
            final_score = max(0, min(100, score + relative_score))
            details["相对估值得分"] = final_score
            
            return final_score, details
            
        except Exception as e:
            details["错误"] = f"相对估值计算失败: {e}"
            return 50, details
    
    def _get_industry_average_valuation(self, symbol):
        """获取行业平均估值水平"""
        try:
            # 这里应该调用数据库获取行业平均数据
            # 暂时返回模拟数据
            return {
                'avg_pe': 20.0,
                'avg_pb': 2.5,
                'avg_ps': 3.0
            }
        except Exception as e:
            print(f"⚠️ 获取行业平均估值失败: {e}")
            return None
    
    def get_valuation_data(self, symbol):
        """获取估值数据"""
        try:
            # 调用数据管理器获取估值数据
            data = self.db_manager.get_valuation_data(symbol, self.base_date)
            return data if data else {}
        except Exception as e:
            print(f"⚠️ 获取估值数据失败 {symbol}: {e}")
            return {}
    
    def get_financial_data(self, symbol):
        """获取财务数据"""
        try:
            # 调用数据管理器获取财务数据
            data = self.db_manager.get_financial_data(symbol, self.base_date)
            return data if data else {}
        except Exception as e:
            print(f"⚠️ 获取财务数据失败 {symbol}: {e}")
            return {}

# 快速测试函数
def test_value_scorer():
    """测试价值评分器"""
    print("🧪 测试 ValueScorer...")
    
    from unittest.mock import Mock
    
    # 创建模拟数据管理器
    mock_db = Mock()
    
    # 设置模拟估值数据
    mock_db.get_valuation_data.return_value = {
        'pe_ratio': 15.5,
        'pb_ratio': 1.8,
        'ps_ratio': 2.2
    }
    
    # 设置模拟财务数据
    mock_db.get_financial_data.return_value = {
        'dividend_yield': 3.2,
        'net_profit_growth': 12.5,
        'dividend_stability': 0.85
    }
    
    # 创建评分器
    scorer = ValueScorer(mock_db, "20250910")
    
    # 测试评分
    try:
        score, details = scorer.calculate_score_detailed("000002")
        print(f"✅ 价值评分: {score}")
        print(f"📊 详细分析:")
        for category, detail in details.items():
            print(f"  {category}: {detail}")
        
        # 测试单独评分
        simple_score = scorer.calculate_score("000002")
        print(f"✅ 简单评分: {simple_score}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False

if __name__ == "__main__":
    test_value_scorer()
