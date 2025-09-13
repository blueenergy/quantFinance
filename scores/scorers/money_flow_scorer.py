import pandas as pd
import numpy as np
from typing import Dict, Any, Tuple, Optional
from .base_scorer import BaseScorer

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
            
            # 🔧 修复：先检查数据是否为空
            if not money_flow_data:
                return 50.0, {"错误": "未找到资金流数据"}
            
            if not price_data:
                return 50.0, {"错误": "未找到价格数据"}
            
            # 🔧 修复：检查数据结构
            print(f"📊 资金流数据: {len(money_flow_data)} 条")
            print(f"📈 价格数据: {len(price_data)} 条")
            
            # 检查第一条数据的字段
            if money_flow_data:
                sample_mf_fields = list(money_flow_data[0].keys())
                print(f"💰 资金流字段: {sample_mf_fields}")
                
                # 检查必要的资金流字段
            # 🔧 检查实际存在的字段
            required_raw_fields = [
                'buy_elg_amount', 'sell_elg_amount',  # 超大单
                'buy_lg_amount', 'sell_lg_amount',    # 大单
                'buy_md_amount', 'sell_md_amount',    # 中单
                'buy_sm_amount', 'sell_sm_amount'     # 小单
            ]
            missing_fields = [field for field in required_raw_fields if field not in sample_mf_fields]
            
            if missing_fields:
                return 50.0, {"错误": f"资金流数据缺少字段: {missing_fields}"}
            
            if price_data:
                sample_price_fields = list(price_data[0].keys())
                print(f"📈 价格字段: {sample_price_fields}")
                
                # 检查必要的价格字段
                volume_field = None
                close_field = None
                
                # 查找成交量字段
                for field in ['vol', 'volume', 'amount']:
                    if field in sample_price_fields:
                        volume_field = field
                        break
                
                # 查找收盘价字段
                for field in ['close', 'close_price', 'adj_close']:
                    if field in sample_price_fields:
                        close_field = field
                        break
                
                if not volume_field:
                    return 50.0, {"错误": "价格数据缺少成交量字段"}
                
                if not close_field:
                    return 50.0, {"错误": "价格数据缺少收盘价字段"}
        
            # 转换为DataFrame
            mf_df = pd.DataFrame(money_flow_data)
            price_df = pd.DataFrame(price_data)
            
            # 🔧 计算净流入字段
            mf_df = self._calculate_net_flow_fields(mf_df)
            
            # 验证计算后的字段
            calculated_fields = ['net_mf_amount_xl', 'net_mf_amount_l', 'net_mf_amount_m', 'net_mf_amount_s']
            for field in calculated_fields:
                if field in mf_df.columns:
                    mf_df[field] = pd.to_numeric(mf_df[field], errors='coerce').fillna(0)
                else:
                    return 50.0, {"错误": f"计算净流入字段失败: {field}"}

            # 🔧 数据预处理：确保数值类型
            required_mf_fields = ['net_mf_amount_xl', 'net_mf_amount_l', 'net_mf_amount_m', 'net_mf_amount_s']
            for field in required_mf_fields:
                if field in mf_df.columns:
                    mf_df[field] = pd.to_numeric(mf_df[field], errors='coerce').fillna(0)
            
            # 确保价格数据为数值类型
            if volume_field in price_df.columns:
                col = pd.to_numeric(price_df[volume_field], errors='coerce')
                if isinstance(col, pd.Series):
                    price_df[volume_field] = col.fillna(0)
                else:
                    price_df[volume_field] = col.fillna(0)
            if close_field in price_df.columns:
                col = pd.to_numeric(price_df[close_field], errors='coerce')
                price_df[close_field] = col.fillna(0)
            
            score = 0
            
            # 1. 主力资金流向评分 (40%)
            main_flow_score, main_flow_details = self._calculate_main_flow_score(mf_df)
            details["主力资金流向"] = main_flow_details
            score += main_flow_score * 0.4
            
            # 2. 资金流强度评分 (30%)
            flow_strength_score, flow_strength_details = self._calculate_flow_strength_score(mf_df)
            details["资金流强度"] = flow_strength_details
            score += flow_strength_score * 0.3
            
            # 3. 成交活跃度评分 (20%) - 传递字段映射
            activity_score, activity_details = self._calculate_activity_score(
                price_df, mf_df, volume_field, close_field
            )
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
            print(f"❌ 资金流评分详细错误: {e}")
            import traceback
            traceback.print_exc()
            error_details = {"错误": f"资金流评分计算失败: {e}"}
            return 50.0, error_details

    def _calculate_main_flow_score(self, mf_df):
        """计算主力资金流向评分 - 确保时间正确性"""
        details = {}
        
        try:
            # 🔧 确保数据按时间正确排序
            mf_df_sorted = self._ensure_time_sorted(mf_df)
            
            # 🔧 获取最近5天和10天的数据
            recent_5 = self._get_recent_trading_days(mf_df_sorted, 5)
            recent_10 = self._get_recent_trading_days(mf_df_sorted, 10)
            
            if len(recent_5) < 3 or len(recent_10) < 5:
                return 50, {"错误": f"数据不足，5日数据{len(recent_5)}条，10日数据{len(recent_10)}条"}
            
            # 🔧 验证时间范围
            time_info_5d = self._validate_time_range(recent_5)
            time_info_10d = self._validate_time_range(recent_10)
            
            details["5日分析"] = time_info_5d
            details["10日分析"] = time_info_10d
            
            # 超大单 + 大单 = 主力资金
            recent_5['main_flow'] = recent_5['net_mf_amount_xl'] + recent_5['net_mf_amount_l']
            recent_10['main_flow'] = recent_10['net_mf_amount_xl'] + recent_10['net_mf_amount_l']
            
            main_flow_5d = recent_5['main_flow'].sum()
            main_flow_10d = recent_10['main_flow'].sum()
            
            details["5日主力净流入"] = f"{main_flow_5d:.2f}万元"
            details["10日主力净流入"] = f"{main_flow_10d:.2f}万元"
            
            # 计算连续流入天数
            consecutive_inflow = 0
            for i in range(len(recent_10) - 1, -1, -1):
                if recent_10.iloc[i]['main_flow'] > 0:
                    consecutive_inflow += 1
                else:
                    break
            
            details["连续流入天数"] = f"{consecutive_inflow}天"
            
            score = 50  # 基础分
            
            # 🔧 根据资金规模动态调整评分标准
            total_abs_flow = abs(main_flow_10d)
            
            if total_abs_flow > 50000:  # 超过5亿，大市值
                threshold_5d_large = 5000    # 5000万
                threshold_5d_medium = 2000   # 2000万
                threshold_10d_large = 10000  # 1亿
                threshold_10d_medium = 5000  # 5000万
                scale_desc = "大市值"
            elif total_abs_flow > 10000:  # 1-5亿，中市值
                threshold_5d_large = 1000    # 1000万
                threshold_5d_medium = 500    # 500万
                threshold_10d_large = 2000   # 2000万
                threshold_10d_medium = 1000  # 1000万
                scale_desc = "中市值"
            else:  # 小市值
                threshold_5d_large = 500     # 500万
                threshold_5d_medium = 200    # 200万
                threshold_10d_large = 1000   # 1000万
                threshold_10d_medium = 500   # 500万
                scale_desc = "小市值"
            
            details["股票规模判断"] = scale_desc
            
            # 使用动态标准评分
            if main_flow_5d > threshold_5d_large:
                score += 25
                details["5日主力资金评价"] = f"✅ 大量净流入（{main_flow_5d:.0f}万元）"
            elif main_flow_5d > threshold_5d_medium:
                score += 15
                details["5日主力资金评价"] = f"✅ 净流入（{main_flow_5d:.0f}万元）"
            elif main_flow_5d > 0:
                score += 5
                details["5日主力资金评价"] = "小幅净流入"
            elif main_flow_5d > -100:  # -100万以上
                score -= 5
                details["5日主力资金评价"] = "小幅净流出"
            else:
                score -= 15
                details["5日主力资金评价"] = "❌ 大量净流出"
            
            # 10日主力资金评分
            if main_flow_10d > threshold_10d_large:  # 1000万以上
                score += 20
                details["10日主力资金评价"] = "✅ 持续大量流入"
            elif main_flow_10d > threshold_10d_medium:  # 200万以上
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
            
            details["10日总净流入"] = f"{total_net_flow:.2f}万元"
            
            # 计算资金流入流出的标准差（波动性）
            daily_flows = (recent_10['net_mf_amount_xl'] + recent_10['net_mf_amount_l'] + 
                          recent_10['net_mf_amount_m'] + recent_10['net_mf_amount_s'])
            
            flow_volatility = daily_flows.std()
            details["资金流波动性"] = f"{flow_volatility:.2f}万元"
            
            score = 50
            
            # 总净流入评分
            if total_net_flow > 1000:  # 1000万以上
                score += 30
                details["总资金流评价"] = "✅ 强劲净流入"
            elif total_net_flow > 0:
                score += 15
                details["总资金流评价"] = "✅ 净流入"
            elif total_net_flow > -500:  # -500万以上
                score += 0
                details["总资金流评价"] = "小幅净流出"
            else:
                score -= 20
                details["总资金流评价"] = "❌ 大幅净流出"
            
            # 波动性评分（适中的波动性更好）
            if flow_volatility < 200:  # 200万以下
                score += 10
                details["资金流稳定性"] = "✅ 稳定"
            elif flow_volatility < 500:  # 500万以下
                score += 5
                details["资金流稳定性"] = "相对稳定"
            else:
                details["资金流稳定性"] = "波动较大"
            
            details["资金流强度得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"资金流强度计算失败: {e}"
            return 50, details
    
    def _calculate_activity_score(self, price_df, mf_df, volume_field='vol', close_field='close'):
        """计算成交活跃度评分 - 修复字段映射问题"""
        details = {}
        
        try:
            # 🔧 使用传入的字段名
            if volume_field not in price_df.columns or close_field not in price_df.columns:
                return 50, {"错误": f"缺少字段: {volume_field} 或 {close_field}"}
            
            recent_10 = price_df.tail(10).copy()
            
            # 计算平均成交量
            avg_volume = recent_10[volume_field].mean()
            current_volume = recent_10.iloc[-1][volume_field]
            
            details["当日成交量"] = f"{current_volume:,.0f}"
            details["10日平均成交量"] = f"{avg_volume:,.0f}"
            
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            details["成交量比率"] = f"{volume_ratio:.1f}倍"
            
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
            
            # 🔧 简化换手率计算，避免复杂的股本数据获取
            details["换手率"] = "暂无股本数据"
            details["换手率评价"] = "基于成交量分析"
            
            details["成交活跃度得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            print(f"❌ 成交活跃度计算错误: {e}")
            details["错误"] = f"成交活跃度计算失败: {e}"
            return 50, details
    
    def _calculate_institution_score(self, mf_df):
        """计算机构关注度评分 - 确保时间正确性"""
        details = {}
        
        try:
            # 🔧 确保数据按时间正确排序
            mf_df_sorted = self._ensure_time_sorted(mf_df)
            
            # 🔧 获取最近10个交易日的数据（而非最后10条记录）
            recent_10 = self._get_recent_trading_days(mf_df_sorted, 10)
            
            if len(recent_10) < 5:  # 至少需要5天数据
                return 50, {"错误": f"数据不足，仅有{len(recent_10)}天数据"}
            
            # 🔧 验证时间范围
            time_info = self._validate_time_range(recent_10)
            details.update(time_info)
            
            # 超大单通常代表机构资金
            xl_flow = recent_10['net_mf_amount_xl'].sum()
            l_flow = recent_10['net_mf_amount_l'].sum()
            m_flow = recent_10['net_mf_amount_m'].sum()
            s_flow = recent_10['net_mf_amount_s'].sum()
            total_flow = xl_flow + l_flow + m_flow + s_flow
            
            print(f"🔍 时间验证 - 实际分析天数: {len(recent_10)}天")
            print(f"🔍 时间范围: {details.get('分析开始日期')} ~ {details.get('分析结束日期')}")
            print(f"🔍 最近{len(recent_10)}日各类资金流入明细(万元):")
            print(f"   超大单: {xl_flow:.2f}万元")
            print(f"   大单: {l_flow:.2f}万元")
            print(f"   中单: {m_flow:.2f}万元")
            print(f"   小单: {s_flow:.2f}万元")
            print(f"   总净流入: {total_flow:.2f}万元")
            
            # 🔧 智能格式化显示 - 正确的单位转换
            def format_amount(amount_wan):
                """格式化金额显示（输入为万元）"""
                if abs(amount_wan) >= 100000:  # 10万万元 = 10亿元
                    return f"{amount_wan / 10000:.2f}亿元"
                elif abs(amount_wan) >= 10000:   # 1万万元 = 1亿元
                    return f"{amount_wan / 10000:.1f}亿元"
                else:
                    return f"{amount_wan:.2f}万元"
            
            # 🔧 修复占比计算 - 处理小总流入问题
            if abs(total_flow) < 100:  # 总流入小于100万元
                details["超大单净流入"] = format_amount(xl_flow)
                details["总净流入"] = format_amount(total_flow)
                details["超大单占比"] = "总流入过小，占比无意义"
                details["分析说明"] = "总资金流入很小，以绝对金额分析"
                
                # 基于绝对金额评分
                score = 50
                if xl_flow > 1000:      # 1000万 = 1亿
                    score += 25
                    details["机构资金评价"] = "✅ 机构大量流入（1亿+）"
                elif xl_flow > 500:     # 500万 = 5000万
                    score += 20
                    details["机构资金评价"] = "✅ 机构流入（5000万+）"
                elif xl_flow > 100:     # 100万
                    score += 15
                    details["机构资金评价"] = "✅ 机构流入（100万+）"
                elif xl_flow > 0:
                    score += 5
                    details["机构资金评价"] = "机构小幅流入"
                elif xl_flow > -100:
                    score -= 5
                    details["机构资金评价"] = "机构小幅流出"
                else:
                    score -= 15
                    details["机构资金评价"] = "❌ 机构流出"
                    
            else:
                # 总流入足够大，可以计算占比
                xl_ratio = xl_flow / total_flow
                
                details["超大单净流入"] = format_amount(xl_flow)
                details["总净流入"] = format_amount(total_flow)
                details["超大单占比"] = f"{xl_ratio:.1%}"
                
                score = 50
                
                # 🔧 动态评分标准 - 根据股票规模调整
                # 大市值股票的评分标准应该更高
                if abs(total_flow) > 50000:  # 总流入超过5亿，大市值股票
                    large_threshold = 10000   # 1亿
                    medium_threshold = 5000   # 5000万
                    details["股票规模"] = "大市值"
                elif abs(total_flow) > 10000:  # 总流入1-5亿，中市值
                    large_threshold = 2000    # 2000万
                    medium_threshold = 1000   # 1000万  
                    details["股票规模"] = "中市值"
                else:  # 小市值
                    large_threshold = 500     # 500万
                    medium_threshold = 200    # 200万
                    details["股票规模"] = "小市值"
                
                # 绝对金额评分
                if xl_flow > large_threshold:
                    score += 25
                    details["机构资金评价"] = f"✅ 机构大量流入（{format_amount(xl_flow)}）"
                elif xl_flow > medium_threshold:
                    score += 15
                    details["机构资金评价"] = f"✅ 机构流入（{format_amount(xl_flow)}）"
                elif xl_flow > 0:
                    score += 5
                    details["机构资金评价"] = f"机构小幅流入（{format_amount(xl_flow)}）"
                elif xl_flow > -medium_threshold:
                    score -= 5
                    details["机构资金评价"] = f"机构小幅流出（{format_amount(xl_flow)}）"
                else:
                    score -= 15
                    details["机构资金评价"] = f"❌ 机构大量流出（{format_amount(xl_flow)}）"
                
                # 占比评分
                if abs(xl_ratio) > 0.7:
                    score += 20
                    details["机构参与度"] = f"✅ 机构高度参与（占比{xl_ratio:.1%}）"
                elif abs(xl_ratio) > 0.4:
                    score += 10
                    details["机构参与度"] = f"机构高度参与（占比{xl_ratio:.1%}）"
                elif abs(xl_ratio) > 0.2:
                    score += 5
                    details["机构参与度"] = f"机构中度参与（占比{xl_ratio:.1%}）"
                else:
                    details["机构参与度"] = f"机构参与度一般（占比{xl_ratio:.1%}）"
                
                # 显示评分标准
                details["评分标准"] = f"大额标准: {format_amount(large_threshold)}, 中额标准: {format_amount(medium_threshold)}"
            
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
        
    def _calculate_net_flow_fields(self, mf_df):
        """
        🔧 计算净流入字段
        
        从买入卖出数据计算净流入：
        - net_mf_amount_xl = buy_elg_amount - sell_elg_amount  (超大单净流入)
        - net_mf_amount_l = buy_lg_amount - sell_lg_amount     (大单净流入)
        - net_mf_amount_m = buy_md_amount - sell_md_amount     (中单净流入)
        - net_mf_amount_s = buy_sm_amount - sell_sm_amount     (小单净流入)
        """
        try:
            print("🔧 开始计算净流入字段...")
            
            # 确保必要的买入卖出字段存在并转换为数值类型
            buy_sell_fields = [
                'buy_elg_amount', 'sell_elg_amount',  # 超大单
                'buy_lg_amount', 'sell_lg_amount',    # 大单
                'buy_md_amount', 'sell_md_amount',    # 中单
                'buy_sm_amount', 'sell_sm_amount'     # 小单
            ]
            
            for field in buy_sell_fields:
                if field in mf_df.columns:
                    mf_df[field] = pd.to_numeric(mf_df[field], errors='coerce').fillna(0)
                else:
                    print(f"⚠️ 缺少字段 {field}，设为0")
                    mf_df[field] = 0
            
            # 计算净流入字段
            mf_df['net_mf_amount_xl'] = mf_df['buy_elg_amount'] - mf_df['sell_elg_amount']  # 超大单净流入
            mf_df['net_mf_amount_l'] = mf_df['buy_lg_amount'] - mf_df['sell_lg_amount']    # 大单净流入
            mf_df['net_mf_amount_m'] = mf_df['buy_md_amount'] - mf_df['sell_md_amount']    # 中单净流入
            mf_df['net_mf_amount_s'] = mf_df['buy_sm_amount'] - mf_df['sell_sm_amount']    # 小单净流入
            

            # 🔧 同时计算成交量净流入（如果有成交量数据）
            volume_fields = [
                'buy_elg_vol', 'sell_elg_vol',    # 超大单成交量
                'buy_lg_vol', 'sell_lg_vol',      # 大单成交量
                'buy_md_vol', 'sell_md_vol',      # 中单成交量
                'buy_sm_vol', 'sell_sm_vol'       # 小单成交量
            ]
            
            # 检查是否有成交量数据
            has_volume_data = any(field in mf_df.columns for field in volume_fields)
            
            if has_volume_data:
                for field in volume_fields:
                    if field in mf_df.columns:
                        mf_df[field] = pd.to_numeric(mf_df[field], errors='coerce').fillna(0)
                    else:
                        mf_df[field] = 0
                
                # 计算成交量净流入
                mf_df['net_mf_vol_xl'] = mf_df['buy_elg_vol'] - mf_df['sell_elg_vol']
                mf_df['net_mf_vol_l'] = mf_df['buy_lg_vol'] - mf_df['sell_lg_vol']
                mf_df['net_mf_vol_m'] = mf_df['buy_md_vol'] - mf_df['sell_md_vol']
                mf_df['net_mf_vol_s'] = mf_df['buy_sm_vol'] - mf_df['sell_sm_vol']
                
                print("✅ 成交量净流入字段计算完成")
            
            # 验证计算结果
            net_fields = ['net_mf_amount_xl', 'net_mf_amount_l', 'net_mf_amount_m', 'net_mf_amount_s']
            print(f"Now is {len(mf_df)} days money flow data")
            for field in net_fields:
                if field in mf_df.columns:
                    field_sum = mf_df[field].sum()
                    print(f"📊 {field}: 总计 {field_sum:.2f}万元")
                else:
                    print(f"❌ 计算字段失败: {field}")
            
            print("✅ 净流入字段计算完成")
            return mf_df
            
        except Exception as e:
            print(f"❌ 计算净流入字段失败: {e}")
            import traceback
            traceback.print_exc()
            
            # 如果计算失败，创建默认的零值字段
            net_fields = ['net_mf_amount_xl', 'net_mf_amount_l', 'net_mf_amount_m', 'net_mf_amount_s']
            for field in net_fields:
                mf_df[field] = 0
            
            return mf_df


    def _ensure_time_sorted(self, mf_df):
        """确保数据按时间正确排序"""
        try:
            # 检查是否有时间字段
            time_fields = ['trade_date', 'date', 'trading_date', 'dt']
            date_field = None
            
            for field in time_fields:
                if field in mf_df.columns:
                    date_field = field
                    break
            
            if not date_field:
                print("⚠️ 未找到时间字段，假设数据已按时间排序")
                return mf_df
            
            # 🔧 确保时间字段为正确的日期格式
            mf_df = mf_df.copy()
            
            # 尝试转换时间格式
            if mf_df[date_field].dtype == 'object':
                # 字符串格式，尝试转换
                mf_df[date_field] = pd.to_datetime(mf_df[date_field], format='%Y%m%d', errors='coerce')
            elif mf_df[date_field].dtype == 'int64':
                # 整数格式，如 20240910
                mf_df[date_field] = pd.to_datetime(mf_df[date_field].astype(str), format='%Y%m%d', errors='coerce')
            
            # 按时间排序（升序）
            mf_df_sorted = mf_df.sort_values(by=date_field).reset_index(drop=True)
            
            print(f"✅ 数据已按 {date_field} 字段排序")
            print(f"📅 时间范围: {mf_df_sorted[date_field].iloc[0]} ~ {mf_df_sorted[date_field].iloc[-1]}")
            
            return mf_df_sorted
            
        except Exception as e:
            print(f"⚠️ 时间排序失败: {e}")
            return mf_df

    def _get_recent_trading_days(self, mf_df_sorted, days=10):
        """获取最近N个交易日的数据"""
        try:
            # 检查时间字段
            time_fields = ['trade_date', 'date', 'trading_date', 'dt']
            date_field = None
            
            for field in time_fields:
                if field in mf_df_sorted.columns:
                    date_field = field
                    break
            
            if not date_field:
                # 没有时间字段，直接取最后N条
                print("⚠️ 无时间字段，取最后10条记录")
                return mf_df_sorted.tail(days)
            
            # 🔧 去除重复日期，保留最新的记录
            mf_df_unique = mf_df_sorted.drop_duplicates(subset=[date_field], keep='last')
            
            # 🔧 过滤掉无效日期
            valid_df = mf_df_unique.dropna(subset=[date_field])
            
            if len(valid_df) == 0:
                print("❌ 没有有效的时间数据")
                return mf_df_sorted.tail(days)
            
            # 取最近的N个交易日
            recent_data = valid_df.tail(days)
            
            print(f"✅ 获取最近{len(recent_data)}个交易日数据")
            
            return recent_data
            
        except Exception as e:
            print(f"⚠️ 获取最近交易日失败: {e}")
            return mf_df_sorted.tail(days)

    def _validate_time_range(self, recent_df):
        """验证时间范围的合理性"""
        time_info = {}
        
        try:
            # 检查时间字段
            time_fields = ['trade_date', 'date', 'trading_date', 'dt']
            date_field = None
            
            for field in time_fields:
                if field in recent_df.columns:
                    date_field = field
                    break
            
            if not date_field:
                time_info["时间验证"] = "无时间字段，无法验证"
                return time_info
            
            # 获取时间范围
            start_date = recent_df[date_field].iloc[0]
            end_date = recent_df[date_field].iloc[-1]
            
            time_info["分析开始日期"] = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
            time_info["分析结束日期"] = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
            time_info["实际数据天数"] = f"{len(recent_df)}天"
            
            # 🔧 检查时间间隔合理性
            if hasattr(start_date, 'strftime') and hasattr(end_date, 'strftime'):
                time_diff = (end_date - start_date).days
                
                if time_diff > 20:  # 超过20天
                    time_info["时间警告"] = f"⚠️ 时间跨度过大({time_diff}天)，可能包含过多历史数据"
                elif time_diff < 7:  # 少于7天
                    time_info["时间警告"] = f"⚠️ 时间跨度过小({time_diff}天)，数据可能不足"
                else:
                    time_info["时间验证"] = "✅ 时间范围合理"
            
            # 🔧 检查数据连续性
            if len(recent_df) > 1:
                # 检查是否有缺失的交易日
                date_list = recent_df[date_field].tolist()
                gaps = []
                
                for i in range(1, len(date_list)):
                    if hasattr(date_list[i], 'strftime'):
                        prev_date = date_list[i-1]
                        curr_date = date_list[i]
                        days_gap = (curr_date - prev_date).days
                        
                        if days_gap > 3:  # 超过3天间隔（考虑周末）
                            gaps.append(f"{prev_date.strftime('%m-%d')}~{curr_date.strftime('%m-%d')}({days_gap}天)")
                
                if gaps:
                    time_info["数据缺失"] = f"⚠️ 发现数据间隔: {', '.join(gaps)}"
                else:
                    time_info["数据连续性"] = "✅ 数据连续"
            
            return time_info
            
        except Exception as e:
            time_info["时间验证错误"] = f"时间验证失败: {e}"
            return time_info