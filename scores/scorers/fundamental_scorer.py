from . import BaseScorer
from datetime import datetime

class FundamentalScorer(BaseScorer):
    """
    ✅ 基本面评分器
    """
    
    def calculate_score(self, symbol):
        """计算基本面评分（简单版本）"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 基本面评分失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的基本面评分"""
        details = {}
        
        try:
            # 获取股票基本信息
            stock_info = self.get_stock_info(symbol)
            if not stock_info:
                return 50.0, {"错误": "未找到股票信息"}
            
            # 获取当前价格数据
            volume_price_data = self.get_volume_price_data(symbol, 1)
            if not volume_price_data:
                return 50.0, {"错误": "未找到价格数据"}
            
            current_price_data = volume_price_data[-1]
            
            # 获取财务数据
            financial_data = self._get_financial_data(symbol)
            
            score = 0
            
            # 1. 财务健康度评分 (40%)
            health_score, health_details = self._calculate_financial_health(financial_data)
            details["财务健康度"] = health_details
            score += health_score * 0.4
            
            # 2. 盈利能力评分 (25%)
            profit_score, profit_details = self._calculate_profitability(financial_data)
            details["盈利能力"] = profit_details
            score += profit_score * 0.25
            
            # 3. 估值水平评分 (20%)
            valuation_score, valuation_details = self._calculate_valuation(financial_data, current_price_data)
            details["估值水平"] = valuation_details
            score += valuation_score * 0.2
            
            # 4. 市值规模评分 (10%)
            market_score, market_details = self._calculate_market_cap(stock_info, current_price_data)
            details["市值规模"] = market_details
            score += market_score * 0.1
            
            # 5. 行业地位评分 (5%)
            industry_score, industry_details = self._calculate_industry_position(stock_info)
            details["行业地位"] = industry_details
            score += industry_score * 0.05
            
            details["最终评分"] = round(score, 2)
            
            return round(score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"基本面评分计算失败: {e}"}
            return 50.0, error_details
    
    def _get_financial_data(self, symbol):
        """获取财务数据"""
        try:
            # 尝试从财务数据集合获取
            financial_collection = self.db_manager.db['financial_data']
            financial_data = financial_collection.find_one(
                {"symbol": symbol},
                sort=[("report_date", -1)]
            )
            
            if financial_data:
                return financial_data
            else:
                # 返回默认值
                return self._get_default_financial_data()
        except:
            return self._get_default_financial_data()
    
    def _get_default_financial_data(self):
        """默认财务数据"""
        return {
            "roe": None, "roa": None, "debt_to_asset": None,
            "current_ratio": None, "net_profit_growth": None,
            "gross_margin": None, "net_margin": None,
            "pe_ratio": None, "pb_ratio": None, "eps_growth": None
        }
    
    def _calculate_financial_health(self, financial_data):
        """计算财务健康度"""
        details = {}
        score = 50
        
        # ROE评分
        roe = financial_data.get('roe')
        if roe is not None:
            if roe >= 0.15:
                roe_score = 90
            elif roe >= 0.10:
                roe_score = 75
            elif roe >= 0.05:
                roe_score = 60
            elif roe >= 0:
                roe_score = 40
            else:
                roe_score = 20
            
            details["ROE"] = f"{roe:.2%} (评分: {roe_score})"
        else:
            roe_score = 50
            details["ROE"] = "无数据"
        
        # 简化版本，只计算ROE
        # 可以继续添加ROA、资产负债率、流动比率等
        
        final_score = roe_score  # 这里可以加权计算多个指标
        details["财务健康度评分"] = final_score
        
        return final_score, details
    
    def _calculate_profitability(self, financial_data):
        """计算盈利能力"""
        details = {}
        
        # 净利润增长率
        growth = financial_data.get('net_profit_growth')
        if growth is not None:
            if growth >= 0.3:
                score = 95
            elif growth >= 0.15:
                score = 80
            elif growth >= 0.05:
                score = 65
            else:
                score = 45
            details["净利润增长率"] = f"{growth:.2%} (评分: {score})"
        else:
            score = 50
            details["净利润增长率"] = "无数据"
        
        details["盈利能力评分"] = score
        return score, details
    
    def _calculate_valuation(self, financial_data, price_data):
        """计算估值水平"""
        details = {}
        score = 50
        
        pe_ratio = financial_data.get('pe_ratio')
        if pe_ratio and pe_ratio > 0:
            if 8 <= pe_ratio <= 15:
                score = 90
            elif 5 <= pe_ratio <= 25:
                score = 75
            elif pe_ratio <= 35:
                score = 60
            else:
                score = 40
            details["PE市盈率"] = f"{pe_ratio:.2f} (评分: {score})"
        else:
            details["PE市盈率"] = "无数据"
        
        details["估值水平评分"] = score
        return score, details
    
    def _calculate_market_cap(self, stock_info, price_data):
        """计算市值规模"""
        details = {}
        
        if 'total_share' in stock_info:
            current_price = price_data['close']
            total_shares = stock_info['total_share']
            market_cap_yi = (current_price * total_shares) / 10000
            
            if 100 <= market_cap_yi <= 1000:
                score = 80
                desc = "优秀规模"
            elif 50 <= market_cap_yi <= 2000:
                score = 65
                desc = "良好规模"
            else:
                score = 50
                desc = "一般规模"
            
            details["市值"] = f"{market_cap_yi:.2f}亿元"
            details["市值评分"] = f"{score} - {desc}"
        else:
            score = 50
            details["市值评分"] = "无数据"
        
        return score, details
    
    def _calculate_industry_position(self, stock_info):
        """计算行业地位"""
        details = {}
        
        industry = stock_info.get('industry', '')
        hot_industries = ['新能源', '人工智能', '生物医药', '半导体']
        
        if any(hot in industry for hot in hot_industries):
            score = 80
            desc = "热门行业"
        else:
            score = 55
            desc = "一般行业"
        
        details["所属行业"] = industry
        details["行业评分"] = f"{score} - {desc}"
        
        return score, details
    
    def _calculate_operating_efficiency_score(self, financial_data):
        """计算经营效率评分"""
        details = {}
        
        try:
            # 资产周转率分析
            asset_turnover = financial_data.get('asset_turnover', None)
            inventory_turnover = financial_data.get('inventory_turnover', None)
            receivable_turnover = financial_data.get('receivable_turnover', None)
            
            efficiency_score = 0
            efficiency_count = 0
            
            # 总资产周转率评分
            if asset_turnover is not None:
                if asset_turnover > 1.5:
                    score = 90
                    details["资产周转"] = f"✅ 优秀 ({asset_turnover:.2f})"
                elif asset_turnover > 1.0:
                    score = 75
                    details["资产周转"] = f"✅ 良好 ({asset_turnover:.2f})"
                elif asset_turnover > 0.5:
                    score = 60
                    details["资产周转"] = f"一般 ({asset_turnover:.2f})"
                else:
                    score = 40
                    details["资产周转"] = f"⚠️ 较差 ({asset_turnover:.2f})"
                
                efficiency_score += score
                efficiency_count += 1
            
            # 存货周转率评分
            if inventory_turnover is not None:
                if inventory_turnover > 8:
                    score = 90
                    details["存货周转"] = f"✅ 优秀 ({inventory_turnover:.2f})"
                elif inventory_turnover > 4:
                    score = 75
                    details["存货周转"] = f"✅ 良好 ({inventory_turnover:.2f})"
                elif inventory_turnover > 2:
                    score = 60
                    details["存货周转"] = f"一般 ({inventory_turnover:.2f})"
                else:
                    score = 40
                    details["存货周转"] = f"⚠️ 较差 ({inventory_turnover:.2f})"
                
                efficiency_score += score
                efficiency_count += 1
            
            # 应收账款周转率评分
            if receivable_turnover is not None:
                if receivable_turnover > 8:
                    score = 90
                    details["应收账款周转"] = f"✅ 优秀 ({receivable_turnover:.2f})"
                elif receivable_turnover > 4:
                    score = 75
                    details["应收账款周转"] = f"✅ 良好 ({receivable_turnover:.2f})"
                elif receivable_turnover > 2:
                    score = 60
                    details["应收账款周转"] = f"一般 ({receivable_turnover:.2f})"
                else:
                    score = 40
                    details["应收账款周转"] = f"⚠️ 较差 ({receivable_turnover:.2f})"
                
                efficiency_score += score
                efficiency_count += 1
            
            if efficiency_count > 0:
                final_score = efficiency_score / efficiency_count
                details["经营效率得分"] = final_score
                return final_score, details
            else:
                return 50, {"经营效率": "数据缺失"}
        
        except Exception as e:
            details["错误"] = f"经营效率计算失败: {e}"
            return 50, details
    
    def _calculate_cash_flow_quality_score(self, financial_data):
        """计算现金流质量评分"""
        details = {}
        
        try:
            operating_cash_flow = financial_data.get('operating_cash_flow', None)
            net_profit = financial_data.get('net_profit', None)
            free_cash_flow = financial_data.get('free_cash_flow', None)
            
            if operating_cash_flow is None or net_profit is None:
                return 50, {"现金流质量": "数据缺失"}
            
            # 经营现金流与净利润比率
            if net_profit > 0:
                cash_profit_ratio = operating_cash_flow / net_profit
                details["现金流净利润比"] = f"{cash_profit_ratio:.2f}"
                
                if cash_profit_ratio > 1.2:
                    score = 90
                    details["现金流质量"] = "✅ 优秀"
                elif cash_profit_ratio > 1.0:
                    score = 80
                    details["现金流质量"] = "✅ 良好"
                elif cash_profit_ratio > 0.8:
                    score = 65
                    details["现金流质量"] = "一般"
                elif cash_profit_ratio > 0.5:
                    score = 50
                    details["现金流质量"] = "⚠️ 较差"
                else:
                    score = 30
                    details["现金流质量"] = "❌ 很差"
            else:
                score = 30
                details["现金流质量"] = "⚠️ 净利润为负"
            
            # 自由现金流补充评分
            if free_cash_flow is not None:
                if free_cash_flow > 0:
                    score = min(95, score + 10)
                    details["自由现金流"] = f"✅ 正值 ({free_cash_flow:.2e})"
                else:
                    score = max(20, score - 10)
                    details["自由现金流"] = f"⚠️ 负值 ({free_cash_flow:.2e})"
            
            details["现金流质量得分"] = score
            return score, details
            
        except Exception as e:
            details["错误"] = f"现金流质量计算失败: {e}"
            return 50, details