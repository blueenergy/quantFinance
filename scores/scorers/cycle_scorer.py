import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from . import BaseScorer

class CycleScorer(BaseScorer):
    """
    ✅ 周期评分器
    
    评分构成:
    - 短期周期 (40%)：5日、10日周期分析
    - 中期周期 (35%)：20日、60日周期分析  
    - 长期周期 (25%)：120日、250日周期分析
    """
    
    def calculate_score(self, symbol):
        """计算周期评分（简单版本）"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 周期评分失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的周期评分"""
        details = {}
        
        try:
            # 获取足够长的价量数据用于周期分析
            price_data = self.get_volume_price_data(symbol, 300)  # 约1年数据
            if not price_data or len(price_data) < 60:
                return 50.0, {"错误": "价量数据不足，无法进行周期分析"}
            
            # 转换为DataFrame
            df = pd.DataFrame(price_data)
            df['close'] = pd.to_numeric(df['close'])
            df['volume'] = pd.to_numeric(df['vol'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            
            score = 0
            
            # 1. 短期周期评分 (40%)
            short_score, short_details = self._calculate_short_cycle_score(df)
            details["短期周期(5-10日)"] = short_details
            score += short_score * 0.4
            print(short_score,short_details)
            print(score)

            # 2. 中期周期评分 (35%)
            medium_score, medium_details = self._calculate_medium_cycle_score(df)
            details["中期周期(20-60日)"] = medium_details
            score += medium_score * 0.35
            print(medium_score,medium_details)
            print(score)

            # 3. 长期周期评分 (25%)
            long_score, long_details = self._calculate_long_cycle_score(df)
            details["长期周期(120-250日)"] = long_details
            score += long_score * 0.25
            print(long_score,long_details)
            print(score)

            final_score = max(0, min(100, score))
            details["周期综合评分"] = round(final_score, 2)
            return round(final_score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"周期评分计算失败: {e}"}
            return 50.0, error_details
    
    def _calculate_short_cycle_score(self, df):
        """计算短期周期评分"""
        details = {}
        
        try:
            current_price = df.iloc[-1]['close']
            
            # 5日周期分析
            df['ma5'] = df['close'].rolling(5).mean()
            recent_5_days = df.tail(10)  # 最近10天看5日均线变化
            
            ma5_trend = self._calculate_trend_strength(recent_5_days['ma5'])
            details["5日均线趋势强度"] = f"{ma5_trend:.2f}"
            
            # 10日周期分析
            df['ma10'] = df['close'].rolling(10).mean()
            recent_10_days = df.tail(20)  # 最近20天看10日均线变化
            
            ma10_trend = self._calculate_trend_strength(recent_10_days['ma10'])
            details["10日均线趋势强度"] = f"{ma10_trend:.2f}"
            
            # 短期支撑压力分析
            support_resistance = self._analyze_support_resistance(df.tail(30))
            details.update(support_resistance)
            
            # 评分计算
            score = 50  # 基础分
            
            # 5日趋势加分
            if ma5_trend > 0.5:
                score += 20
                details["5日趋势评价"] = "✅ 强劲上升"
            elif ma5_trend > 0:
                score += 10
                details["5日趋势评价"] = "✅ 温和上升"
            elif ma5_trend > -0.3:
                score += 0
                details["5日趋势评价"] = "横盘整理"
            else:
                score -= 10
                details["5日趋势评价"] = "❌ 下降趋势"
            
            # 10日趋势加分
            if ma10_trend > 0.3:
                score += 15
                details["10日趋势评价"] = "✅ 中期上升"
            elif ma10_trend > 0:
                score += 8
                details["10日趋势评价"] = "✅ 缓慢上升"
            else:
                details["10日趋势评价"] = "❌ 中期调整"
            
            # 价格位置评分
            current_ma5 = df.iloc[-1]['ma5']
            current_ma10 = df.iloc[-1]['ma10']
            
            if current_price > current_ma5 > current_ma10:
                score += 15
                details["价格位置"] = "✅ 价格在短期均线之上"
            elif current_price > current_ma10:
                score += 8
                details["价格位置"] = "价格在10日线之上"
            else:
                details["价格位置"] = "❌ 价格在短期均线之下"
            
            details["短期周期得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"短期周期计算失败: {e}"
            return 50, details
    
    def _calculate_medium_cycle_score(self, df):
        """计算中期周期评分"""
        details = {}
        
        try:
            # 20日和60日周期分析
            df['ma20'] = df['close'].rolling(20).mean()
            df['ma60'] = df['close'].rolling(60).mean()
            
            if len(df) < 60:
                return 50, {"错误": "数据不足，无法计算中期周期"}
            
            # 中期趋势分析
            recent_60 = df.tail(60)
            ma20_trend = self._calculate_trend_strength(recent_60['ma20'].tail(30))
            ma60_trend = self._calculate_trend_strength(recent_60['ma60'])
            
            details["20日均线趋势强度"] = f"{ma20_trend:.2f}"
            details["60日均线趋势强度"] = f"{ma60_trend:.2f}"
            
            # 波动率分析
            volatility = self._calculate_volatility(recent_60['close'])
            details["60日波动率"] = f"{volatility:.2%}"
            
            score = 50
            
            # 20日趋势评分
            if ma20_trend > 0.3:
                score += 20
                details["20日趋势评价"] = "✅ 强势上升"
            elif ma20_trend > 0:
                score += 10
                details["20日趋势评价"] = "✅ 温和上升"
            else:
                details["20日趋势评价"] = "❌ 调整趋势"
            
            # 60日趋势评分
            if ma60_trend > 0.2:
                score += 15
                details["60日趋势评价"] = "✅ 中长期上升"
            elif ma60_trend > 0:
                score += 8
                details["60日趋势评价"] = "✅ 缓慢上升"
            else:
                details["60日趋势评价"] = "❌ 中长期调整"
            
            # 波动率评分（适中的波动率更好）
            if 0.15 <= volatility <= 0.35:
                score += 10
                details["波动率评价"] = "✅ 波动率适中"
            elif volatility < 0.15:
                score += 5
                details["波动率评价"] = "低波动率"
            else:
                details["波动率评价"] = "❌ 高波动率"
            
            # 均线排列评分
            current = df.iloc[-1]
            if current['ma20'] > current['ma60']:
                score += 15
                details["中期均线排列"] = "✅ 多头排列"
            else:
                details["中期均线排列"] = "❌ 空头排列"
            
            details["中期周期得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"中期周期计算失败: {e}"
            return 50, details
    
    def _calculate_long_cycle_score(self, df):
        """计算长期周期评分"""
        details = {}
        
        try:
            # 长期均线
            df['ma120'] = df['close'].rolling(120).mean()
            df['ma250'] = df['close'].rolling(250).mean()
            
            if len(df) < 250:
                # 数据不足时使用现有数据
                available_days = len(df)
                details["数据情况"] = f"仅有{available_days}天数据"
                
                if available_days >= 120:
                    long_trend = self._calculate_trend_strength(df['ma120'].dropna())
                    details["120日趋势强度"] = f"{long_trend:.2f}"
                    
                    if long_trend > 0.1:
                        score = 70
                        details["长期趋势评价"] = "✅ 长期上升"
                    elif long_trend > 0:
                        score = 60
                        details["长期趋势评价"] = "缓慢上升"
                    else:
                        score = 40
                        details["长期趋势评价"] = "❌ 长期调整"
                else:
                    score = 50
                    details["长期趋势评价"] = "数据不足"
                
                return score, details
            
            # 完整的长期分析
            ma120_trend = self._calculate_trend_strength(df['ma120'].tail(60))
            ma250_trend = self._calculate_trend_strength(df['ma250'].tail(120))
            
            details["120日均线趋势强度"] = f"{ma120_trend:.2f}"
            details["250日均线趋势强度"] = f"{ma250_trend:.2f}"
            
            # 长期位置分析
            current = df.iloc[-1]
            yearly_high = df.tail(250)['high'].max()
            yearly_low = df.tail(250)['low'].min()
            
            price_position = (current['close'] - yearly_low) / (yearly_high - yearly_low)
            details["年度价格位置"] = f"{price_position:.1%} (0%=年低点, 100%=年高点)"
            
            score = 50
            
            # 120日趋势评分
            if ma120_trend > 0.2:
                score += 20
                details["120日趋势评价"] = "✅ 长期强势"
            elif ma120_trend > 0:
                score += 10
                details["120日趋势评价"] = "✅ 长期上升"
            else:
                details["120日趋势评价"] = "❌ 长期调整"
            
            # 250日趋势评分
            if ma250_trend > 0.1:
                score += 15
                details["250日趋势评价"] = "✅ 超长期上升"
            elif ma250_trend > 0:
                score += 8
                details["250日趋势评价"] = "超长期缓升"
            else:
                details["250日趋势评价"] = "❌ 超长期调整"
            
            # 价格位置评分
            if price_position > 0.7:
                score += 10
                details["价格位置评价"] = "✅ 接近年度高点"
            elif price_position > 0.5:
                score += 5
                details["价格位置评价"] = "中等位置"
            elif price_position > 0.3:
                score += 0
                details["价格位置评价"] = "相对低位"
            else:
                score -= 5
                details["价格位置评价"] = "❌ 接近年度低点"
            
            details["长期周期得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"长期周期计算失败: {e}"
            return 50, details
    
    def _calculate_trend_strength(self, series):
        """计算趋势强度"""
        if len(series) < 2:
            return 0
        
        # 计算线性回归斜率作为趋势强度
        series_clean = series.dropna()
        if len(series_clean) < 2:
            return 0
        
        x = np.arange(len(series_clean))
        slope = np.polyfit(x, series_clean.values, 1)[0]
        
        # 标准化斜率（相对于价格的百分比变化）
        avg_price = series_clean.mean()
        trend_strength = (slope / avg_price) * len(series_clean) if avg_price > 0 else 0
        
        return trend_strength
    
    def _calculate_volatility(self, prices):
        """计算价格波动率"""
        returns = prices.pct_change().dropna()
        return returns.std() * np.sqrt(252)  # 年化波动率
    
    def _analyze_support_resistance(self, df):
        """分析支撑压力位"""
        details = {}
        
        try:
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            
            # 简单的支撑压力识别
            recent_high = np.max(highs)
            recent_low = np.min(lows)
            current_price = closes[-1]
            
            resistance_distance = (recent_high - current_price) / current_price
            support_distance = (current_price - recent_low) / current_price
            
            details["近期高点"] = f"{recent_high:.2f}"
            details["近期低点"] = f"{recent_low:.2f}"
            details["距离压力位"] = f"{resistance_distance:.1%}"
            details["距离支撑位"] = f"{support_distance:.1%}"
            
            if resistance_distance < 0.05:
                details["压力位状态"] = "❌ 接近压力位"
            elif support_distance < 0.05:
                details["支撑位状态"] = "⚠️ 接近支撑位"
            else:
                details["位置状态"] = "✅ 远离关键位置"
            
            return details
            
        except Exception as e:
            return {"支撑压力分析": f"计算失败: {e}"}