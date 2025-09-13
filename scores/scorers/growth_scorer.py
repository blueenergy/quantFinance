#!/usr/bin/env python3
"""
🚀 成长性评分器 (Growth Scorer)

评估股票的成长性，包括：
- 营收增长率
- 利润增长率  
- ROE变化趋势
- 市场竞争力
- 创新投入能力
"""

import pandas as pd
import numpy as np
from .base_scorer import BaseScorer
from datetime import datetime, timedelta

class GrowthScorer(BaseScorer):
    """成长性评分器"""
    
    def __init__(self, db_manager, base_date):
        super().__init__(db_manager, base_date)
        self.name = "growth_scorer"
        self.description = "评估股票成长性和发展潜力"
    
    def calculate_score(self, symbol):
        """计算成长性评分"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 成长性评分计算失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的成长性评分"""
        details = {}
        
        try:
            # 获取财务数据
            financial_data = self.get_financial_data(symbol, years=3)
            industry_data = self.get_industry_data(symbol)
            
            # 检查数据完整性
            if not financial_data or len(financial_data) < 2:
                return 50.0, {"错误": "财务数据不足，无法计算成长性"}
            
            # 转换为DataFrame便于分析
            fin_df = pd.DataFrame(financial_data)
            print(f"📊 原始数据字段: {list(fin_df.columns)}")
            print(f"📊 数据形状: {fin_df.shape}")
            
            # 🔧 修复字段名问题 - 检测实际的日期字段
            date_field = None
            for field in ['end_date', 'report_date', 'ann_date', 'f_ann_date']:
                if field in fin_df.columns:
                    date_field = field
                    break
            
            if date_field:
                print(f"📅 使用日期字段: {date_field}")
                fin_df = fin_df.sort_values(date_field)
            else:
                print("⚠️ 未找到日期字段，使用原始顺序")
                # 如果没有日期字段，假设数据已经按时间排序
            
            score = 0
            
            # 1. 营收增长评分 (30%)
            revenue_score, revenue_details = self._calculate_revenue_growth_score(fin_df)
            details["营收增长分析"] = revenue_details
            score += revenue_score * 0.3
            #print(revenue_score, revenue_details)
            
            # 2. 利润增长评分 (30%)
            profit_score, profit_details = self._calculate_profit_growth_score(fin_df)
            details["利润增长分析"] = profit_details
            score += profit_score * 0.3
            #print(profit_score, profit_details)
            
            # 3. ROE增长评分 (20%)
            roe_score, roe_details = self._calculate_roe_growth_score(fin_df)
            details["ROE变化分析"] = roe_details
            score += roe_score * 0.2
            #print(roe_score, roe_details)

            # 4. 市场竞争力评分 (10%)
            market_score, market_details = self._calculate_market_competitiveness_score(fin_df, industry_data)
            details["市场竞争力"] = market_details
            score += market_score * 0.1
            #print(market_score, market_details)
            
            # 5. 创新能力评分 (10%)
            innovation_score, innovation_details = self._calculate_innovation_score(fin_df)
            details["创新能力"] = innovation_details
            score += innovation_score * 0.1
            #print(innovation_score, innovation_details)
            
            final_score = max(0, min(100, score))
            details["成长性综合评分"] = round(final_score, 2)
            
            return round(final_score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"成长性评分计算失败: {e}"}
            return 50.0, error_details
    
    def _calculate_revenue_growth_score(self, fin_df):
        """计算营收增长评分"""
        details = {}
        
        try:
            # 检查是否有足够的数据
            if len(fin_df) < 2:
                return 50, {"错误": "数据不足，无法计算营收增长"}
            
            # 获取最近几年的营收数据
            revenues = fin_df['total_revenue'].dropna()
            if len(revenues) < 2:
                return 50, {"错误": "营收数据缺失"}
            
            # 转换为数值类型
            revenues = pd.to_numeric(revenues, errors='coerce').dropna()
            
            if len(revenues) < 2:
                return 50, {"错误": "营收数据无效"}
            
            # 计算同比增长率
            growth_rates = []
            for i in range(1, len(revenues)):
                if revenues.iloc[i-1] != 0:
                    growth_rate = (revenues.iloc[i] / revenues.iloc[i-1] - 1) * 100
                    growth_rates.append(growth_rate)
            
            if not growth_rates:
                return 50, {"警告": "无法计算营收增长率"}
            
            # 最近一年增长率
            latest_growth = growth_rates[-1] if growth_rates else 0
            
            # 平均增长率
            avg_growth = np.mean(growth_rates)
            
            # 增长稳定性（标准差越小越稳定）
            growth_stability = np.std(growth_rates) if len(growth_rates) > 1 else 0
            
            details["最近一年营收增长率"] = f"{latest_growth:.2f}%"
            details["平均营收增长率"] = f"{avg_growth:.2f}%"
            details["增长稳定性"] = f"{growth_stability:.2f}% (波动率)"
            details["历年营收"] = [f"{rev/1e8:.2f}亿" for rev in revenues.tolist()]
            
            # 评分逻辑
            score = 50  # 基础分
            
            # 最近一年增长率评分
            if latest_growth >= 30:
                score += 30
                details["最近增长评价"] = "✅ 高速增长"
            elif latest_growth >= 15:
                score += 20
                details["最近增长评价"] = "✅ 快速增长"
            elif latest_growth >= 5:
                score += 10
                details["最近增长评价"] = "稳定增长"
            elif latest_growth >= 0:
                score += 0
                details["最近增长评价"] = "微幅增长"
            elif latest_growth >= -5:
                score -= 10
                details["最近增长评价"] = "⚠️ 轻微下降"
            else:
                score -= 20
                details["最近增长评价"] = "❌ 明显下降"
            
            # 平均增长率调整
            if avg_growth >= 20:
                score += 15
                details["平均增长评价"] = "✅ 长期高增长"
            elif avg_growth >= 10:
                score += 10
                details["平均增长评价"] = "✅ 长期稳增长"
            elif avg_growth >= 0:
                score += 0
                details["平均增长评价"] = "基本稳定"
            else:
                score -= 10
                details["平均增长评价"] = "❌ 长期下降"
            
            # 稳定性调整
            if growth_stability <= 5:
                score += 5
                details["稳定性评价"] = "✅ 增长稳定"
            elif growth_stability <= 15:
                score += 0
                details["稳定性评价"] = "增长波动适中"
            else:
                score -= 5
                details["稳定性评价"] = "⚠️ 增长波动较大"
            
            details["营收增长得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"营收增长计算失败: {e}"
            return 50, details
    
    def _calculate_profit_growth_score(self, fin_df):
        """计算利润增长评分"""
        details = {}
        
        try:
            # 获取净利润数据
            net_profits = fin_df['n_income'].dropna()
            
            if len(net_profits) < 2:
                return 50, {"错误": "利润数据不足"}
            
            # 转换为数值类型
            net_profits = pd.to_numeric(net_profits, errors='coerce').dropna()
            
            if len(net_profits) < 2:
                return 50, {"错误": "利润数据无效"}
            
            # 计算利润增长率
            profit_growth_rates = []
            for i in range(1, len(net_profits)):
                if net_profits.iloc[i-1] > 0:  # 避免负数基数
                    growth_rate = (net_profits.iloc[i] / net_profits.iloc[i-1] - 1) * 100
                    profit_growth_rates.append(growth_rate)
                elif net_profits.iloc[i-1] < 0 and net_profits.iloc[i] > 0:
                    # 从亏损转为盈利
                    profit_growth_rates.append(200)  # 给一个较高的增长率
                elif net_profits.iloc[i-1] < 0 and net_profits.iloc[i] < 0:
                    # 都是亏损，看亏损是否在减少
                    if abs(net_profits.iloc[i]) < abs(net_profits.iloc[i-1]):
                        profit_growth_rates.append(50)  # 亏损减少
                    else:
                        profit_growth_rates.append(-50)  # 亏损增加
            
            latest_profit_growth = profit_growth_rates[-1] if profit_growth_rates else 0
            avg_profit_growth = np.mean(profit_growth_rates) if profit_growth_rates else 0
            
            details["最近一年利润增长率"] = f"{latest_profit_growth:.2f}%"
            details["平均利润增长率"] = f"{avg_profit_growth:.2f}%"
            details["历年净利润"] = [f"{profit/1e8:.2f}亿" for profit in net_profits.tolist()]
            
            # 检查盈利稳定性
            profitable_years = sum(1 for profit in net_profits if profit > 0)
            profitability_ratio = profitable_years / len(net_profits)
            
            details["盈利年份比例"] = f"{profitability_ratio:.1%}"
            
            # 评分逻辑
            score = 50  # 基础分
            
            # 最近利润增长评分
            if latest_profit_growth >= 50:
                score += 25
                details["最近利润增长评价"] = "✅ 利润高速增长"
            elif latest_profit_growth >= 20:
                score += 20
                details["最近利润增长评价"] = "✅ 利润快速增长"
            elif latest_profit_growth >= 5:
                score += 10
                details["最近利润增长评价"] = "利润稳定增长"
            elif latest_profit_growth >= 0:
                score += 0
                details["最近利润增长评价"] = "利润微幅增长"
            elif latest_profit_growth >= -10:
                score -= 10
                details["最近利润增长评价"] = "⚠️ 利润小幅下降"
            else:
                score -= 20
                details["最近利润增长评价"] = "❌ 利润大幅下降"
            
            # 盈利稳定性评分
            if profitability_ratio >= 1.0:
                score += 15
                details["盈利稳定性"] = "✅ 持续盈利"
            elif profitability_ratio >= 0.8:
                score += 10
                details["盈利稳定性"] = "✅ 基本盈利"
            elif profitability_ratio >= 0.6:
                score += 0
                details["盈利稳定性"] = "盈利不够稳定"
            else:
                score -= 15
                details["盈利稳定性"] = "❌ 经常亏损"
            
            # 平均利润增长调整
            if avg_profit_growth >= 25:
                score += 10
                details["长期利润趋势"] = "✅ 长期高增长"
            elif avg_profit_growth >= 10:
                score += 5
                details["长期利润趋势"] = "✅ 长期稳增长"
            elif avg_profit_growth >= 0:
                score += 0
                details["长期利润趋势"] = "基本稳定"
            else:
                score -= 10
                details["长期利润趋势"] = "❌ 长期下降"
            
            details["利润增长得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"利润增长计算失败: {e}"
            return 50, details
    
    def _calculate_roe_growth_score(self, fin_df):
        """计算ROE增长评分"""
        details = {}
        
        try:
            # 获取ROE数据
            roe_data = fin_df['roe'].dropna()
            
            if len(roe_data) < 2:
                return 50, {"错误": "ROE数据不足"}
            
            # 转换为数值类型
            roe_data = pd.to_numeric(roe_data, errors='coerce').dropna()
            
            if len(roe_data) < 2:
                return 50, {"错误": "ROE数据无效"}
            
            # 计算ROE变化趋势
            latest_roe = roe_data.iloc[-1]
            previous_roe = roe_data.iloc[-2]
            avg_roe = roe_data.mean()
            
            # ROE变化
            roe_change = latest_roe - previous_roe
            
            details["最新ROE"] = f"{latest_roe:.2f}%"
            details["前期ROE"] = f"{previous_roe:.2f}%"
            details["平均ROE"] = f"{avg_roe:.2f}%"
            details["ROE变化"] = f"{roe_change:+.2f}%"
            details["历年ROE"] = [f"{roe:.2f}%" for roe in roe_data.tolist()]
            
            # 评分逻辑
            score = 50  # 基础分
            
            # ROE绝对水平评分
            if latest_roe >= 20:
                score += 20
                details["ROE水平评价"] = "✅ ROE优秀"
            elif latest_roe >= 15:
                score += 15
                details["ROE水平评价"] = "✅ ROE良好"
            elif latest_roe >= 10:
                score += 10
                details["ROE水平评价"] = "ROE一般"
            elif latest_roe >= 5:
                score += 0
                details["ROE水平评价"] = "ROE偏低"
            else:
                score -= 10
                details["ROE水平评价"] = "❌ ROE很低"
            
            # ROE变化趋势评分
            if roe_change >= 3:
                score += 15
                details["ROE趋势评价"] = "✅ ROE快速提升"
            elif roe_change >= 1:
                score += 10
                details["ROE趋势评价"] = "✅ ROE稳步提升"
            elif roe_change >= 0:
                score += 5
                details["ROE趋势评价"] = "ROE基本稳定"
            elif roe_change >= -1:
                score -= 5
                details["ROE趋势评价"] = "⚠️ ROE轻微下降"
            else:
                score -= 15
                details["ROE趋势评价"] = "❌ ROE明显下降"
            
            # ROE稳定性评分
            roe_std = roe_data.std()
            if roe_std <= 2:
                score += 10
                details["ROE稳定性"] = "✅ ROE稳定"
            elif roe_std <= 5:
                score += 5
                details["ROE稳定性"] = "ROE波动适中"
            else:
                score -= 5
                details["ROE稳定性"] = "⚠️ ROE波动较大"
            
            details["ROE得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"ROE计算失败: {e}"
            return 50, details
    
    def _calculate_market_competitiveness_score(self, fin_df, industry_data):
        """计算市场竞争力评分"""
        details = {}
        
        try:
            score = 50  # 基础分
            
            # 获取最新财务数据
            if len(fin_df) == 0:
                return 50, {"错误": "无财务数据"}
            
            latest_data = fin_df.iloc[-1]
            
            # 1. 营收规模评分
            revenue = latest_data.get('total_revenue', 0)
            if revenue:
                revenue_billion = revenue / 1e8  # 转换为亿
                details["营业收入"] = f"{revenue_billion:.2f}亿"
                
                if revenue_billion >= 1000:
                    score += 10
                    details["规模评价"] = "✅ 大型企业"
                elif revenue_billion >= 100:
                    score += 8
                    details["规模评价"] = "✅ 中大型企业"
                elif revenue_billion >= 10:
                    score += 5
                    details["规模评价"] = "中型企业"
                else:
                    score += 0
                    details["规模评价"] = "小型企业"
            
            # 2. 毛利率评分
            gross_margin = latest_data.get('gross_margin', 0)
            if gross_margin:
                details["毛利率"] = f"{gross_margin:.2f}%"
                
                if gross_margin >= 50:
                    score += 15
                    details["毛利率评价"] = "✅ 毛利率优秀"
                elif gross_margin >= 30:
                    score += 10
                    details["毛利率评价"] = "✅ 毛利率良好"
                elif gross_margin >= 15:
                    score += 5
                    details["毛利率评价"] = "毛利率一般"
                else:
                    score += 0
                    details["毛利率评价"] = "⚠️ 毛利率偏低"
            
            # 3. 市占率代理指标（营收增长vs行业增长）
            if len(fin_df) >= 2:
                current_revenue = fin_df.iloc[-1]['total_revenue']
                previous_revenue = fin_df.iloc[-2]['total_revenue']
                
                if previous_revenue > 0:
                    company_growth = (current_revenue / previous_revenue - 1) * 100
                    details["公司营收增长"] = f"{company_growth:.2f}%"
                    
                    # 假设行业平均增长5%（实际应该从industry_data获取）
                    industry_growth = 5.0
                    if industry_data and 'avg_growth' in industry_data:
                        industry_growth = industry_data['avg_growth']
                    
                    details["行业平均增长"] = f"{industry_growth:.2f}%"
                    
                    if company_growth > industry_growth + 5:
                        score += 15
                        details["市场竞争力"] = "✅ 超越行业增长"
                    elif company_growth > industry_growth:
                        score += 10
                        details["市场竞争力"] = "✅ 跑赢行业"
                    elif company_growth > industry_growth - 5:
                        score += 0
                        details["市场竞争力"] = "与行业同步"
                    else:
                        score -= 10
                        details["市场竞争力"] = "❌ 落后行业"
            
            details["市场竞争力得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"市场竞争力计算失败: {e}"
            return 50, details
    
    def _calculate_innovation_score(self, fin_df):
        """计算创新能力评分"""
        details = {}
        
        try:
            score = 50  # 基础分
            
            if len(fin_df) == 0:
                return 50, {"错误": "无财务数据"}
            
            latest_data = fin_df.iloc[-1]
            
            # 1. 研发投入比例
            rd_expense = latest_data.get('rd_expense', 0)
            revenue = latest_data.get('total_revenue', 0)
            
            if revenue > 0 and rd_expense > 0:
                rd_ratio = (rd_expense / revenue) * 100
                details["研发投入占营收比"] = f"{rd_ratio:.2f}%"
                
                if rd_ratio >= 10:
                    score += 20
                    details["研发投入评价"] = "✅ 研发投入很高"
                elif rd_ratio >= 5:
                    score += 15
                    details["研发投入评价"] = "✅ 研发投入较高"
                elif rd_ratio >= 3:
                    score += 10
                    details["研发投入评价"] = "研发投入适中"
                elif rd_ratio >= 1:
                    score += 5
                    details["研发投入评价"] = "研发投入一般"
                else:
                    score += 0
                    details["研发投入评价"] = "⚠️ 研发投入较低"
            else:
                details["研发投入占营收比"] = "数据缺失"
                details["研发投入评价"] = "无研发数据"
            
            # 2. 研发投入增长趋势
            if len(fin_df) >= 2:
                current_rd = fin_df.iloc[-1].get('rd_expense', 0)
                previous_rd = fin_df.iloc[-2].get('rd_expense', 0)
                
                if previous_rd > 0 and current_rd > 0:
                    rd_growth = (current_rd / previous_rd - 1) * 100
                    details["研发投入增长"] = f"{rd_growth:.2f}%"
                    
                    if rd_growth >= 20:
                        score += 15
                        details["研发投入趋势"] = "✅ 研发投入快速增长"
                    elif rd_growth >= 10:
                        score += 10
                        details["研发投入趋势"] = "✅ 研发投入稳步增长"
                    elif rd_growth >= 0:
                        score += 5
                        details["研发投入趋势"] = "研发投入保持增长"
                    else:
                        score -= 5
                        details["研发投入趋势"] = "⚠️ 研发投入下降"
            
            # 3. 创新效率指标（专利/研发投入等，如果有数据的话）
            # 这里可以扩展，加入专利数量、新产品收入占比等指标
            
            details["创新能力得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"创新能力计算失败: {e}"
            return 50, details
    
    # 数据获取方法
    def get_financial_data(self, symbol, years=3):
        """获取财务数据"""
        try:
            return self.db_manager.get_financial_data(symbol, years)
        except Exception as e:
            print(f"⚠️ 获取财务数据失败: {e}")
            return []
    
    def get_industry_data(self, symbol):
        """获取行业数据"""
        try:
            return self.db_manager.get_industry_data(symbol)
        except Exception as e:
            print(f"⚠️ 获取行业数据失败: {e}")
            return {}
    
    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        try:
            return self.db_manager.get_stock_info(symbol)
        except Exception as e:
            print(f"⚠️ 获取股票信息失败: {e}")
            return {}

# 测试函数
def test_growth_scorer():
    """测试成长性评分器"""
    print("🧪 测试成长性评分器...")
    
    from unittest.mock import Mock
    
    # 创建模拟数据库管理器
    mock_db = Mock()
    
    # 模拟财务数据
    mock_financial_data = [
        {
            'end_date': '20211231',
            'total_revenue': 45e8,  # 45亿
            'n_income': 3e8,        # 3亿
            'roe': 12.5,
            'gross_margin': 25.0,
            'rd_expense': 1.5e8     # 1.5亿研发
        },
        {
            'end_date': '20221231', 
            'total_revenue': 52e8,  # 52亿
            'n_income': 4.2e8,      # 4.2亿
            'roe': 14.8,
            'gross_margin': 27.5,
            'rd_expense': 2e8       # 2亿研发
        },
        {
            'end_date': '20231231',
            'total_revenue': 60e8,  # 60亿
            'n_income': 5.5e8,      # 5.5亿  
            'roe': 16.2,
            'gross_margin': 29.0,
            'rd_expense': 2.8e8     # 2.8亿研发
        }
    ]
    
    mock_db.get_financial_data.return_value = mock_financial_data
    mock_db.get_industry_data.return_value = {'avg_growth': 8.0}
    mock_db.get_stock_info.return_value = {'symbol': '000002', 'name': '万科A'}
    
    # 创建评分器
    scorer = GrowthScorer(mock_db, "20231231")
    
    # 测试评分
    try:
        score, details = scorer.calculate_score_detailed("000002")
        
        print(f"✅ 成长性评分: {score:.2f}")
        print(f"📊 详细分析:")
        
        for category, info in details.items():
            print(f"  {category}:")
            if isinstance(info, dict):
                for key, value in info.items():
                    print(f"    {key}: {value}")
            else:
                print(f"    {info}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_growth_scorer()