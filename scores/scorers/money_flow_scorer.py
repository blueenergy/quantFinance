import pandas as pd
import numpy as np
from . import BaseScorer

class MoneyFlowScorer(BaseScorer):
    """
    ✅ 资金流评分器
    
    评分构成:
    - 主力资金流向 (40%)：大单净流入分析
    - 资金流强度 (30%)：资金流入流出的强度和持续性
    - 成交活跃度 (20%)：换手率和成交量分析
    - 机构关注度 (10%)：机构资金参与度
    """
    
    def calculate_score(self, symbol):
        """计算资金流评分（简单版本）"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 资金流评分失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的资金流评分"""
        details = {}
        
        try:
            # 获取资金流数据
            money_flow_data = self.get_money_flow_data(symbol, 30)
            price_data = self.get_volume_price_data(symbol, 30)
            if 'net_mf_amount_xl' not in money_flow_data[0].keys():
                return 50.0, {"错误": "资金流数据或价格数据不足"}
            if not money_flow_data or not price_data:
                return 50.0, {"错误": "资金流数据或价格数据不足"}
            
            # 转换为DataFrame
            mf_df = pd.DataFrame(money_flow_data)
            price_df = pd.DataFrame(price_data)
            
            score = 0
            
            # 1. 主力资金流向评分 (40%)
            main_flow_score, main_flow_details = self._calculate_main_flow_score(mf_df)
            details["主力资金流向"] = main_flow_details
            score += main_flow_score * 0.4
            
            # 2. 资金流强度评分 (30%)
            flow_strength_score, flow_strength_details = self._calculate_flow_strength_score(mf_df)
            details["资金流强度"] = flow_strength_details
            score += flow_strength_score * 0.3
            
            # 3. 成交活跃度评分 (20%)
            activity_score, activity_details = self._calculate_activity_score(price_df, mf_df)
            details["成交活跃度"] = activity_details
            score += activity_score * 0.2
            
            # 4. 机构关注度评分 (10%)
            institution_score, institution_details = self._calculate_institution_score(mf_df)
            details["机构关注度"] = institution_details
            score += institution_score * 0.1
            
            final_score = max(0, min(100, score))
            details["资金流综合评分"] = round(final_score, 2)
            
            return round(final_score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"资金流评分计算失败: {e}"}
            return 50.0, error_details
    
    def _calculate_main_flow_score(self, mf_df):
        """计算主力资金流向评分"""
        details = {}
        
        try:
            # 确保数据列存在
            required_columns = ['net_mf_amount_xl', 'net_mf_amount_l', 'net_mf_amount_m', 'net_mf_amount_s']
            
            for col in required_columns:
                if col not in mf_df.columns:
                    return 50, {"错误": f"缺少资金流数据列: {col}"}
            
            # 转换为数值类型
            for col in required_columns:
                mf_df[col] = pd.to_numeric(mf_df[col], errors='coerce')
            
            # 计算最近5天和10天的主力资金净流入
            recent_5 = mf_df.tail(5).copy()
            recent_10 = mf_df.tail(10).copy()
            
            # 超大单 + 大单 = 主力资金
            recent_5['main_flow'] = recent_5['net_mf_amount_xl'] + recent_5['net_mf_amount_l']
            recent_10['main_flow'] = recent_10['net_mf_amount_xl'] + recent_10['net_mf_amount_l']
            
            main_flow_5d = recent_5['main_flow'].sum()
            main_flow_10d = recent_10['main_flow'].sum()
            
            details["5日主力净流入"] = f"{main_flow_5d/10000:.2f}万元"
            details["10日主力净流入"] = f"{main_flow_10d/10000:.2f}万元"
            
            # 计算连续流入天数
            consecutive_inflow = 0
            for i in range(len(recent_10) - 1, -1, -1):
                if recent_10.iloc[i]['main_flow'] > 0:
                    consecutive_inflow += 1
                else:
                    break
            
            details["连续流入天数"] = f"{consecutive_inflow}天"
            
            score = 50  # 基础分
            
            # 5日主力资金评分
            if main_flow_5d > 5000000:  # 500万以上
                score += 25
                details["5日主力资金评价"] = "✅ 大量净流入"
            elif main_flow_5d > 1000000:  # 100万以上
                score += 15
                details["5日主力资金评价"] = "✅ 净流入"
            elif main_flow_5d > 0:
                score += 5
                details["5日主力资金评价"] = "小幅净流入"
            elif main_flow_5d > -1000000:  # -100万以上
                score -= 5
                details["5日主力资金评价"] = "小幅净流出"
            else:
                score -= 15
                details["5日主力资金评价"] = "❌ 大量净流出"
            
            # 10日主力资金评分
            if main_flow_10d > 10000000:  # 1000万以上
                score += 20
                details["10日主力资金评价"] = "✅ 持续大量流入"
            elif main_flow_10d > 2000000:  # 200万以上
                score += 10
                details["10日主力资金评价"] = "✅ 持续流入"
            elif main_flow_10d > 0:
                score += 0
                details["10日主力资金评价"] = "整体流入"
            else:
                score -= 10
                details["10日主力资金评价"] = "❌ 整体流出"
            
            # 连续性评分
            if consecutive_inflow >= 3:
                score += 15
                details["资金流连续性"] = "✅ 连续流入"
            elif consecutive_inflow >= 2:
                score += 8
                details["资金流连续性"] = "短期流入"
            else:
                details["资金流连续性"] = "流入不连续"
            
            details["主力资金流向得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"主力资金流向计算失败: {e}"
            return 50, details
    
    def _calculate_flow_strength_score(self, mf_df):
        """计算资金流强度评分"""
        details = {}
        
        try:
            # 计算资金流强度指标
            recent_10 = mf_df.tail(10)
            
            # 总净流入
            total_net_flow = (recent_10['net_mf_amount_xl'].sum() + 
                             recent_10['net_mf_amount_l'].sum() + 
                             recent_10['net_mf_amount_m'].sum() + 
                             recent_10['net_mf_amount_s'].sum())
            
            details["10日总净流入"] = f"{total_net_flow/10000:.2f}万元"
            
            # 计算资金流入流出的标准差（波动性）
            daily_flows = (recent_10['net_mf_amount_xl'] + recent_10['net_mf_amount_l'] + 
                          recent_10['net_mf_amount_m'] + recent_10['net_mf_amount_s'])
            
            flow_volatility = daily_flows.std()
            details["资金流波动性"] = f"{flow_volatility/10000:.2f}万元"
            
            score = 50
            
            # 总净流入评分
            if total_net_flow > 10000000:  # 1000万以上
                score += 30
                details["总资金流评价"] = "✅ 强劲净流入"
            elif total_net_flow > 0:
                score += 15
                details["总资金流评价"] = "✅ 净流入"
            elif total_net_flow > -5000000:  # -500万以上
                score += 0
                details["总资金流评价"] = "小幅净流出"
            else:
                score -= 20
                details["总资金流评价"] = "❌ 大幅净流出"
            
            # 波动性评分（适中的波动性更好）
            if flow_volatility < 2000000:  # 200万以下
                score += 10
                details["资金流稳定性"] = "✅ 稳定"
            elif flow_volatility < 5000000:  # 500万以下
                score += 5
                details["资金流稳定性"] = "相对稳定"
            else:
                details["资金流稳定性"] = "波动较大"
            
            details["资金流强度得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"资金流强度计算失败: {e}"
            return 50, details
    
    def _calculate_activity_score(self, price_df, mf_df):
        """计算成交活跃度评分"""
        details = {}
        
        try:
            # 确保有价格数据
            if 'vol' not in price_df.columns or 'close' not in price_df.columns:
                return 50, {"错误": "缺少成交量或价格数据"}
            
            price_df['vol'] = pd.to_numeric(price_df['vol'], errors='coerce')
            price_df['close'] = pd.to_numeric(price_df['close'], errors='coerce')
            
            recent_10 = price_df.tail(10)
            
            # 计算平均成交量和换手率
            avg_volume = recent_10['vol'].mean()
            current_volume = recent_10.iloc[-1]['vol']
            
            details["当日成交量"] = f"{current_volume:,.0f}股"
            details["10日平均成交量"] = f"{avg_volume:,.0f}股"
            
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            details["成交量比率"] = f"{volume_ratio:.1f}倍"
            
            # 获取股票信息计算换手率
            stock_info = self.get_stock_info(price_df.iloc[0]['symbol'] if 'symbol' in price_df.columns else '')
            
            score = 50
            
            # 成交量活跃度评分
            if volume_ratio > 2.0:
                score += 25
                details["成交量评价"] = "✅ 异常活跃"
            elif volume_ratio > 1.5:
                score += 15
                details["成交量评价"] = "✅ 活跃"
            elif volume_ratio > 1.0:
                score += 8
                details["成交量评价"] = "正常"
            elif volume_ratio > 0.5:
                score += 0
                details["成交量评价"] = "偏低"
            else:
                score -= 10
                details["成交量评价"] = "❌ 成交低迷"
            
            # 换手率评分（如果有股本数据）
            if stock_info and 'total_share' in stock_info:
                total_shares = stock_info['total_share'] * 10000  # 万股转股
                turnover_rate = current_volume / total_shares * 100
                details["换手率"] = f"{turnover_rate:.2f}%"
                
                if 3 <= turnover_rate <= 8:
                    score += 15
                    details["换手率评价"] = "✅ 换手率适中"
                elif 1 <= turnover_rate <= 12:
                    score += 8
                    details["换手率评价"] = "换手率正常"
                elif turnover_rate > 12:
                    score += 0
                    details["换手率评价"] = "⚠️ 换手率偏高"
                else:
                    details["换手率评价"] = "换手率偏低"
            else:
                details["换手率"] = "无股本数据"
            
            details["成交活跃度得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"成交活跃度计算失败: {e}"
            return 50, details
    
    def _calculate_institution_score(self, mf_df):
        """计算机构关注度评分"""
        details = {}
        
        try:
            recent_10 = mf_df.tail(10)
            
            # 超大单通常代表机构资金
            xl_flow = recent_10['net_mf_amount_xl'].sum()
            total_flow = (recent_10['net_mf_amount_xl'].sum() + 
                         recent_10['net_mf_amount_l'].sum() + 
                         recent_10['net_mf_amount_m'].sum() + 
                         recent_10['net_mf_amount_s'].sum())
            
            xl_ratio = xl_flow / total_flow if total_flow != 0 else 0
            details["超大单净流入"] = f"{xl_flow/10000:.2f}万元"
            details["超大单占比"] = f"{xl_ratio:.1%}"
            
            score = 50
            
            # 超大单流入评分
            if xl_flow > 5000000:  # 500万以上
                score += 30
                details["机构资金评价"] = "✅ 机构大量流入"
            elif xl_flow > 1000000:  # 100万以上
                score += 20
                details["机构资金评价"] = "✅ 机构流入"
            elif xl_flow > 0:
                score += 10
                details["机构资金评价"] = "机构小幅流入"
            elif xl_flow > -1000000:  # -100万以上
                score += 0
                details["机构资金评价"] = "机构小幅流出"
            else:
                score -= 15
                details["机构资金评价"] = "❌ 机构大量流出"
            
            # 机构参与度评分
            if abs(xl_ratio) > 0.6:
                score += 20
                details["机构参与度"] = "✅ 机构高度参与"
            elif abs(xl_ratio) > 0.3:
                score += 10
                details["机构参与度"] = "机构中度参与"
            else:
                details["机构参与度"] = "机构参与度一般"
            
            details["机构关注度得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"机构关注度计算失败: {e}"
            return 50, details
    
    def get_money_flow_data(self, symbol, days=30):
        """获取资金流数据"""
        try:
            if hasattr(self.db_manager, 'get_money_flow_data'):
                return self.db_manager.get_money_flow_data(symbol, days)
            else:
                raise AttributeError("数据库管理器缺少 get_money_flow_data 方法")
        except Exception as e:
            print(f"❌ 获取资金流数据失败 {symbol}: {e}")
            return []

    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        try:
            if hasattr(self.db_manager, 'get_stock_info'):
                return self.db_manager.get_stock_info(symbol)
            else:
                raise AttributeError("数据库管理器缺少 get_stock_info 方法")
        except Exception as e:
            print(f"❌ 获取股票信息失败 {symbol}: {e}")
            return {}