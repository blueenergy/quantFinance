import numpy as np
import pandas as pd
from . import BaseScorer

class TechnicalScorer(BaseScorer):
    """
    ✅ 技术面评分器
    
    评分构成:
    - 均线系统 (30%): MA5、MA10、MA20、MA60的排列和价格位置
    - 量价配合 (25%)：价格与成交量的配合度
    - 技术指标 (25%): RSI、MACD、KDJ等
    - 趋势强度 (20%)：价格趋势的强度和稳定性
    """
    
    def calculate_score(self, symbol):
        """计算技术面评分（简单版本）"""
        try:
            score, _ = self.calculate_score_detailed(symbol)
            return score
        except Exception as e:
            print(f"❌ 技术面评分失败 {symbol}: {e}")
            return 50.0
    
    def calculate_score_detailed(self, symbol):
        """计算详细的技术面评分"""
        details = {}
        
        try:
            # 获取价量数据
            price_data = self.get_volume_price_data(symbol, 60)
            if not price_data or len(price_data) < 20:
                return 50.0, {"错误": "价量数据不足"}
            
            # 转换为DataFrame便于计算
            df = pd.DataFrame(price_data)
            df['close'] = pd.to_numeric(df['close'])
            df['volume'] = pd.to_numeric(df['vol'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            df['open'] = pd.to_numeric(df['open'])
            
            score = 0
            
            # 1. 均线系统评分 (30%)
            ma_score, ma_details = self._calculate_ma_score(df)
            details["均线系统"] = ma_details
            score += ma_score * 0.3
            
            # 2. 量价配合评分 (25%)
            volume_price_score, vp_details = self._calculate_volume_price_score(df)
            details["量价配合"] = vp_details
            score += volume_price_score * 0.25
            
            # 3. 技术指标评分 (25%)
            indicator_score, indicator_details = self._calculate_indicator_score(df)
            details["技术指标"] = indicator_details
            score += indicator_score * 0.25
            
            # 4. 趋势强度评分 (20%)
            trend_score, trend_details = self._calculate_trend_score(df)
            details["趋势强度"] = trend_details
            score += trend_score * 0.2
            
            print(ma_score,ma_details)
            print(volume_price_score,vp_details)
            print(indicator_score,indicator_details)
            print(trend_score,trend_details)
            print(ma_score,volume_price_score,indicator_score,trend_score)
            final_score = max(0, min(100, score))
            details["技术面评分"] = round(final_score, 2)
            
            return round(final_score, 2), details
            
        except Exception as e:
            error_details = {"错误": f"技术面评分计算失败: {e}"}
            return 50.0, error_details
    
    def _calculate_ma_score(self, df):
        """计算均线系统评分 - 修复版本"""
        details = {}
        
        try:
            # 计算各期均线
            df['ma5'] = df['close'].rolling(5).mean()
            df['ma10'] = df['close'].rolling(10).mean()
            df['ma20'] = df['close'].rolling(20).mean()
            df['ma60'] = df['close'].rolling(60).mean()
            
            current = df.iloc[-1]
            
            # 当前价格相对于均线的位置
            current_price = current['close']
            ma5 = current['ma5']
            ma10 = current['ma10']
            ma20 = current['ma20']
            ma60 = current['ma60']
            
            details["当前价格"] = f"{current_price:.2f}"
            details["MA5"] = f"{ma5:.2f}"
            details["MA10"] = f"{ma10:.2f}"
            details["MA20"] = f"{ma20:.2f}"
            details["MA60"] = f"{ma60:.2f}"
            
            score = 50  # 基础分
            
            # 🔧 修改：价格在均线之下要减分
            if current_price > ma5:
                score += 15
                details["价格vs MA5"] = "✅ 价格在MA5之上"
            else:
                score -= 10  # 减分而不是不加分
                details["价格vs MA5"] = "❌ 价格在MA5之下"
        
            if current_price > ma10:
                score += 12
                details["价格vs MA10"] = "✅ 价格在MA10之上"
            else:
                score -= 8
                details["价格vs MA10"] = "❌ 价格在MA10之下"
        
            if current_price > ma20:
                score += 10
                details["价格vs MA20"] = "✅ 价格在MA20之上"
            else:
                score -= 6
                details["价格vs MA20"] = "❌ 价格在MA20之下"
        
            if current_price > ma60:
                score += 8
                details["价格vs MA60"] = "✅ 价格在MA60之上"
            else:
                score -= 4
                details["价格vs MA60"] = "❌ 价格在MA60之下"
        
            # 🔧 修改：空头排列要大幅减分
            if ma5 > ma10 > ma20 > ma60:
                score += 15
                details["均线排列"] = "✅ 完美多头排列"
            elif ma5 > ma10 > ma20:
                score += 10
                details["均线排列"] = "✅ 短期多头排列"
            elif ma5 > ma10:
                score += 5
                details["均线排列"] = "✅ 极短期多头排列"
            elif ma5 < ma10 < ma20 < ma60:  # 完美空头排列
                score -= 20
                details["均线排列"] = "❌ 完美空头排列"
            elif ma5 < ma10 < ma20:  # 短期空头排列
                score -= 15
                details["均线排列"] = "❌ 短期空头排列"
            elif ma5 < ma10:  # 极短期空头排列
                score -= 10
                details["均线排列"] = "❌ 极短期空头排列"
            else:
                details["均线排列"] = "横盘震荡"
            
            details["均线系统得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"均线计算失败: {e}"
            return 50, details
    
    def _calculate_volume_price_score(self, df):
        """计算量价配合评分 - 修复版本"""
        details = {}
        
        try:
            # 计算最近5天的量价配合
            recent_days = df.tail(5).copy()
            
            score = 50
            多头配合度 = 0
            空头信号度 = 0
            总交易日 = 0
            
            for i in range(1, len(recent_days)):
                current = recent_days.iloc[i]
                prev = recent_days.iloc[i-1]
                
                price_change = current['close'] - prev['close']
                volume_change = current['volume'] - prev['volume']
                price_change_rate = price_change / prev['close']
                volume_change_rate = volume_change / prev['volume'] if prev['volume'] > 0 else 0
                
                总交易日 += 1
                
                if price_change > 0:  # 上涨日
                    if volume_change > 0:  # 价涨量增 - 多头信号
                        多头配合度 += 1
                        # 涨幅越大，放量越多，分数越高
                        if price_change_rate > 0.03 and volume_change_rate > 0.5:  # 大涨放量
                            多头配合度 += 0.5
                    # 价涨量减 - 虚涨，不加分也不减分
                    
                else:  # 下跌日
                    if volume_change > 0:  # 价跌量增 - 空头信号
                        空头信号度 += 1
                        # 跌幅越大，放量越多，空头信号越强
                        if abs(price_change_rate) > 0.03 and volume_change_rate > 0.5:  # 大跌放量
                            空头信号度 += 0.5
                    # 价跌量减 - 技术性调整，相对好一些
                    else:
                        多头配合度 += 0.3  # 给一点分，但不多
            
            # 🎯 核心评分逻辑修改
            if 总交易日 > 0:
                多头比例 = 多头配合度 / 总交易日
                空头比例 = 空头信号度 / 总交易日
                
                details["多头信号比例"] = f"{多头比例:.1%}"
                details["空头信号比例"] = f"{空头比例:.1%}"
                
                # 🔧 关键修改：空头信号越强，分数越低
                if 空头比例 >= 0.6:  # 空头信号强烈
                    score = 25
                    details["量价配合评价"] = "强烈空头信号"
                elif 空头比例 >= 0.4:  # 空头信号明显
                    score = 35
                    details["量价配合评价"] = "明显空头信号"
                elif 多头比例 >= 0.75:  # 多头信号强烈
                    score = 85
                    details["量价配合评价"] = "优秀多头信号"
                elif 多头比例 >= 0.5:  # 多头信号良好
                    score = 70
                    details["量价配合评价"] = "良好多头信号"
                else:  # 信号混乱
                    score = 50
                    details["量价配合评价"] = "信号混乱"
            
            # 检查成交量状态
            current_vol = df.iloc[-1]['volume']
            avg_vol_20 = df['volume'].tail(20).mean()
            
            if current_vol > avg_vol_20 * 1.5:
                # 🔧 修改：如果是下跌放量，要减分而不是加分
                latest_price_change = df.iloc[-1]['close'] - df.iloc[-2]['close']
                if latest_price_change < 0:  # 下跌放量
                    score -= 5
                    details["成交量状态"] = f"⚠️ 下跌放量 (是20日均量的{current_vol/avg_vol_20:.1f}倍)"
                else:  # 上涨放量
                    score += 10
                    details["成交量状态"] = f"✅ 上涨放量 (是20日均量的{current_vol/avg_vol_20:.1f}倍)"
            else:
                details["成交量状态"] = f"普通成交量 (是20日均量的{current_vol/avg_vol_20:.1f}倍)"
            
            details["量价配合得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"量价配合计算失败: {e}"
            return 50, details
    
    def _calculate_indicator_score(self, df):
        """计算技术指标评分"""
        details = {}
        
        try:
            score = 50
            
            # 1. RSI指标
            rsi = self._calculate_rsi(df['close'])
            current_rsi = rsi.iloc[-1] if len(rsi) > 0 else 50
            
            details["RSI"] = f"{current_rsi:.1f}"
            
            if 30 <= current_rsi <= 70:
                rsi_score = 80
                details["RSI评价"] = "正常区间"
            elif 20 <= current_rsi < 30:
                rsi_score = 90
                details["RSI评价"] = "超卖，可能反弹"
            elif 70 < current_rsi <= 80:
                rsi_score = 60
                details["RSI评价"] = "超买，注意风险"
            else:
                rsi_score = 40
                details["RSI评价"] = "极端区间"
            
            score = score * 0.4 + rsi_score * 0.6
            
            # 2. MACD指标
            macd_line, signal_line, histogram = self._calculate_macd(df['close'])
            
            if len(histogram) > 1:
                current_hist = histogram.iloc[-1]
                prev_hist = histogram.iloc[-2]
                
                details["MACD柱状图"] = f"{current_hist:.4f}"
                
                if current_hist > 0 and current_hist > prev_hist:
                    score += 10
                    details["MACD趋势"] = "✅ 金叉向上"
                elif current_hist < 0 and current_hist > prev_hist:
                    score += 5
                    details["MACD趋势"] = "✅ 底部回升"
                else:
                    details["MACD趋势"] = "普通"
            
            details["技术指标得分"] = min(100, max(0, score))
            return min(100, max(0, score)), details
            
        except Exception as e:
            details["错误"] = f"技术指标计算失败: {e}"
            return 50, details
    
    def _calculate_trend_score(self, df):
        """计算趋势强度评分 - 修复版本"""
        details = {}
        
        try:
            # 计算最近20天的价格趋势
            recent_20 = df.tail(20).copy()
            prices = recent_20['close'].values
            
            # 线性回归计算趋势斜率
            days = np.arange(len(prices))
            slope = np.polyfit(days, prices, 1)[0]
            
            # 计算R²（趋势的稳定性）
            trend_line = np.poly1d(np.polyfit(days, prices, 1))(days)
            ss_res = np.sum((prices - trend_line) ** 2)
            ss_tot = np.sum((prices - np.mean(prices)) ** 2)
            r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
            
            # 计算价格变化率
            price_change_rate = (prices[-1] - prices[0]) / prices[0]
            
            details["趋势斜率"] = f"{slope:.4f}"
            details["趋势稳定性(R²)"] = f"{r_squared:.3f}"
            details["20日价格变化率"] = f"{price_change_rate:.2%}"
            
            score = 50  # 基础分
            
            # 🔧 修复趋势方向评分逻辑
            if slope > 0:  # 上升趋势
                # 上升幅度越大，分数越高
                trend_strength = min(25, abs(slope) * 2000)  # 限制最大加分25分
                score += trend_strength
                
                if price_change_rate > 0.10:  # 20天涨幅超过10%
                    score += 15
                    details["趋势方向"] = "✅ 强势上升趋势"
                elif price_change_rate > 0.05:  # 20天涨幅超过5%
                    score += 10
                    details["趋势方向"] = "✅ 中等上升趋势"
                else:
                    score += 5
                    details["趋势方向"] = "✅ 温和上升趋势"
                
            else:  # 下降趋势
                # 🔧 关键修复：下降幅度越大，扣分越多
                trend_weakness = min(25, abs(slope) * 2000)  # 计算下降强度
                score -= trend_weakness  # 直接减分
                
                if price_change_rate < -0.10:  # 20天跌幅超过10%
                    score -= 20  # 额外大幅减分
                    details["趋势方向"] = "❌ 强势下降趋势"
                elif price_change_rate < -0.05:  # 20天跌幅超过5%
                    score -= 15  # 额外中等减分
                    details["趋势方向"] = "❌ 中等下降趋势"
                else:
                    score -= 10  # 额外轻微减分
                    details["趋势方向"] = "❌ 温和下降趋势"
        
            # 根据趋势稳定性调整评分
            if r_squared > 0.8:  # 趋势非常稳定
                stability_bonus = 15
                details["趋势稳定性评价"] = "✅ 趋势非常稳定"
            elif r_squared > 0.6:  # 趋势比较稳定
                stability_bonus = 10
                details["趋势稳定性评价"] = "✅ 趋势比较稳定"
            elif r_squared > 0.4:  # 趋势一般稳定
                stability_bonus = 5
                details["趋势稳定性评价"] = "趋势一般稳定"
            else:  # 趋势不稳定
                stability_bonus = -5  # 不稳定的趋势要减分
                details["趋势稳定性评价"] = "❌ 趋势不稳定"
        
            # 🔧 对于下降趋势，稳定性不是好事
            if slope < 0 and r_squared > 0.6:
                # 稳定的下降趋势更危险，要额外减分
                score -= 5
                details["趋势稳定性评价"] += " (稳定下跌更危险)"
            else:
                score += stability_bonus
            
            # 🔧 添加短期趋势确认
            recent_5 = df.tail(5)['close'].values
            short_slope = np.polyfit(np.arange(len(recent_5)), recent_5, 1)[0]
            
            if slope > 0 and short_slope > 0:
                score += 5  # 长短期趋势一致，上升
                details["短期趋势确认"] = "✅ 短期趋势确认上升"
            elif slope < 0 and short_slope < 0:
                score -= 5  # 长短期趋势一致，下降
                details["短期趋势确认"] = "❌ 短期趋势确认下降"
            else:
                details["短期趋势确认"] = "趋势分歧"
            
            final_score = min(100, max(0, score))
            details["趋势强度得分"] = final_score
            
            return final_score, details
        
        except Exception as e:
            details["错误"] = f"趋势强度计算失败: {e}"
            return 50, details
    
    def _calculate_rsi(self, prices, period=14):
        """计算RSI指标"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calculate_macd(self, prices, fast=12, slow=26, signal=9):
        """计算MACD指标"""
        ema_fast = prices.ewm(span=fast).mean()
        ema_slow = prices.ewm(span=slow).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram