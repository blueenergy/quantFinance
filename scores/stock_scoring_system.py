# tools/stock_scoring_system.py
import os
from pymongo import MongoClient
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import tushare as ts
from dotenv import load_dotenv
load_dotenv()
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
# 初始化 tushare
ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token
pro = ts.pro_api()



class StockScoringSystem:
    def __init__(self,base_date=None):
        self.client = client
        self.db = self.client['finance']
        self.stock_scores = self.db['stock_scores']  # 新集合：股票评分
        self.stock_info = self.db['stock_info']
        self.volume_price = self.db['volume_price']
        self.money_flow = self.db['money_flow_records']
        
        # ✅ 修改：通过参数传递基准日期，如果未提供则使用当前日期
        if base_date:
            self.base_date = base_date
        else:
            self.base_date = datetime.now().strftime('%Y%m%d')
            
        print(f"📅 使用基准日期: {self.base_date}")
        self.create_indexes_safely()
    
    def create_indexes_safely(self):
        """安全创建索引，处理各种异常情况"""
        try:
            # 检查数据库连接
            self.client.admin.command('ping')
            
            # 创建索引（静默处理已存在的情况）
            try:
                self.stock_scores.create_index([("symbol", 1), ("score_date", -1)], background=True)
                print("✅ 创建 stock_scores symbol+date 索引")
            except Exception as e:
                if "already exists" in str(e).lower() or "index" in str(e).lower():
                    print("ℹ️  索引已存在: stock_scores symbol+date")
                else:
                    print(f"⚠️  创建索引失败: {e}")
            
            try:
                self.stock_scores.create_index([("total_score", -1)], background=True)
                print("✅ 创建 stock_scores total_score 索引")
            except Exception as e:
                if "already exists" in str(e).lower() or "index" in str(e).lower():
                    print("ℹ️  索引已存在: stock_scores total_score")
                else:
                    print(f"⚠️  创建索引失败: {e}")
                    
        except Exception as e:
            print(f"❌ 索引创建过程中出错: {e}")
            print("程序将继续运行，但可能影响查询性能")
    
    def calculate_stock_score(self, symbol, base_date=None):
        """计算单个股票的综合评分(排除ST股票)"""
        # ✅ 如果提供了 base_date 参数，则临时使用该日期
        original_base_date = self.base_date
        if base_date:
            self.base_date = base_date
            print(f"📅 临时使用基准日期: {self.base_date}")
        
        try:
            # 检查是否为ST股票
            stock_info = self.stock_info.find_one({"symbol": symbol})
            if stock_info:
                name = stock_info.get('name', '')
                if self.is_st_stock(name):
                    print(f"跳过ST股票评分: {symbol} - {name}")
                    return None
    
            # 调用合并后的函数
            cycle_score = self.calculate_cycle_score(symbol, return_details=False)
            fundamental_score = self.calculate_fundamental_score(symbol, return_details=False)
            technical_score = self.calculate_technical_score(symbol, return_details=False)
            money_flow_score = self.calculate_money_flow_score(symbol, return_details=False)
            
            # 综合评分权重
            weights = {
                'cycle': 0.25,        # 经济周期 25%
                'fundamental': 0.35,  # 基本面 35%
                'technical': 0.25,    # 技术面 25%
                'money_flow': 0.15    # 资金流 15%
            }
            
            total_score = (
                cycle_score * weights['cycle'] +
                fundamental_score * weights['fundamental'] +
                technical_score * weights['technical'] +
                money_flow_score * weights['money_flow']
            )
            
            score_data = {
                'symbol': symbol,
                'score_date': self.base_date,  # 使用指定的基准日期
                'cycle_score': round(cycle_score, 2),
                'fundamental_score': round(fundamental_score, 2),
                'technical_score': round(technical_score, 2),
                'money_flow_score': round(money_flow_score, 2),
                'total_score': round(total_score, 2),
                'weights': weights,
                'details': self.get_score_details(symbol)
            }
            
            return score_data
            
        except Exception as e:
            print(f"计算股票 {symbol} 评分失败: {e}")
            return None
        finally:
            # ✅ 恢复原始的基准日期
            self.base_date = original_base_date

    def calculate_cycle_score(self, symbol, return_details=False):
        """计算经济周期评分 - 支持返回详细信息"""
        details = {} 
        try:
            base_date_obj = datetime.strptime(self.base_date, '%Y%m%d')
            two_years_ago = (base_date_obj - timedelta(days=730)).strftime('%Y%m%d')
            
            records = list(self.volume_price.find({
                "symbol": symbol,
                "trade_date": {
                    "$gte": two_years_ago,
                    "$lte": self.base_date
                }
            }).sort("trade_date", 1))
            
            info = f"查询日期范围: {two_years_ago} 到 {self.base_date}, 找到 {len(records)} 条记录"
            if return_details:
                details["查询范围"] = info
                details["数据条数"] = len(records)
            else:
                print(info)
            
            if len(records) < 12:
                if return_details:
                    details["评分原因"] = "数据不足，给中等分"
                    return 50, details
                else:
                    return 50
        
            monthly_data = self.convert_to_monthly(records)
            
            if len(monthly_data) < 6:
                if return_details:
                    details["评分原因"] = "月K线数据不足"
                    return 50, details
                else:
                    return 50
            
            prices = [item['close'] for item in monthly_data]
            max_price = max(prices)
            min_price = min(prices)
            current_price = prices[-1]
            
            if return_details:
                details["历史最高价"] = max_price
                details["历史最低价"] = min_price
                details["当前价格"] = current_price
            
            # 计算评分...
            if max_price == min_price:
                position_score = 50
            else:
                # 价格位置：越接近底部分数越高
                position_ratio = (current_price - min_price) / (max_price - min_price)
                position_score = 100 - (position_ratio * 80)
                
                if return_details:
                    details["价格位置比例"] = f"{position_ratio:.2%}"
                    details["位置评分"] = f"{position_score:.1f}"
        
            # 趋势评分...
            recent_prices = prices[-6:]
            if len(recent_prices) >= 3:
                trend_slope = np.polyfit(range(len(recent_prices)), recent_prices, 1)[0]
                avg_price = np.mean(recent_prices)
                trend_ratio = trend_slope / avg_price if avg_price > 0 else 0
                
                if return_details:
                    details["月度涨跌幅"] = f"{trend_ratio:.2%}"
            
                if -0.02 <= trend_ratio <= 0.05:
                    trend_score = 100
                elif trend_ratio > 0.05:
                    trend_score = max(20, 100 - (trend_ratio - 0.05) * 1000)
                else:
                    trend_score = max(20, 100 + trend_ratio * 1000)
            else:
                trend_score = 50
        
            cycle_score = (position_score * 0.6 + trend_score * 0.4)
            final_score = max(0, min(100, cycle_score))
            
            if return_details:
                details["综合周期评分"] = f"{final_score:.1f}"
                return final_score, details
            else:
                print(f"经济周期评分: {final_score}")
                return final_score
        
        except Exception as e:
            error_msg = f"计算周期评分失败 {symbol}: {e}"
            if return_details:
                details["错误"] = error_msg
                return 50, details
            else:
                print(error_msg)
                return 50
    
    def calculate_fundamental_score(self, symbol, return_details=False):
        """计算基本面评分 - 支持返回详细信息"""
        details = {}
        
        try:
            # 获取股票基本信息
            stock_info = self.stock_info.find_one({"symbol": symbol})
            
            # 查询指定日期的股价数据
            volume_price_info = self.volume_price.find_one({
                "symbol": symbol,
                "trade_date": self.base_date
            })
            
            if not stock_info or not volume_price_info:
                if return_details:
                    details["错误"] = f"未找到 {symbol} 在 {self.base_date} 的完整数据"
                    return 50, details
                else:
                    print(f"未找到 {symbol} 在 {self.base_date} 的完整数据")
                    return 50
            
            score = 50
            if return_details:
                details["基础分"] = 50
            
            # 1. 市值评分
            if 'total_share' in stock_info and volume_price_info:
                current_price = volume_price_info['close']
                total_shares = stock_info['total_share']  # 万股
                market_cap = current_price * total_shares  
                
                if return_details:
                    details["当前价格"] = current_price
                    details["总股本"] = f"{total_shares}万股"
                    details["市值"] = f"{market_cap:.2f}亿元"
                else:
                    print(f'当前价格: {current_price}, 总股本: {total_shares}万股')
                    print(f'市值: {market_cap:.2f}亿元')
                
                if 100 <= market_cap <= 1000:
                    score += 20
                    message = "市值评分: +20 (100-1000亿)"
                elif 50 <= market_cap <= 2000:
                    score += 10
                    message = "市值评分: +10 (50-2000亿)"
                else:
                    message = "市值评分: +0 (市值超出理想范围)"
                
                if return_details:
                    details["市值评分"] = message
                else:
                    print(message)
            
            # 2. 行业评分
            if 'industry' in stock_info:
                industry = stock_info['industry']
                hot_industries = ['新能源', '人工智能', '生物医药', '半导体']
                
                if return_details:
                    details["所属行业"] = industry
                
                if any(hot_ind in industry for hot_ind in hot_industries):
                    score += 15
                    message = f"行业评分: +15 ({industry}为热门行业)"
                else:
                    message = f"行业评分: +0 ({industry}为传统行业)"
                
                if return_details:
                    details["行业评分"] = message
                else:
                    print(message)
            
            # 3. 稳定性评分
            base_date_obj = datetime.strptime(self.base_date, '%Y%m%d')
            one_year_ago = (base_date_obj - timedelta(days=365)).strftime('%Y%m%d')
            
            recent_records = list(self.volume_price.find({
                "symbol": symbol,
                "trade_date": {
                    "$gte": one_year_ago,
                    "$lte": self.base_date
                }
            }).sort("trade_date", 1))
            
            data_info = f"近一年数据 ({one_year_ago} 到 {self.base_date}): {len(recent_records)} 条"
            if return_details:
                details["近一年数据"] = data_info
            else:
                print(data_info)
            
            if len(recent_records) > 50:
                prices = [r['close'] for r in recent_records]
                volatility = np.std(prices) / np.mean(prices)
                
                volatility_info = f'波动率(变异系数): {volatility:.3f}'
                if return_details:
                    details["波动率"] = volatility_info
                else:
                    print(volatility_info)
                
                if 0.2 <= volatility <= 0.4:
                    score += 15
                    message = "稳定性评分: +15 (波动率适中)"
                elif 0.1 <= volatility <= 0.6:
                    score += 10
                    message = "稳定性评分: +10 (波动率可接受)"
                elif volatility < 0.1:
                    score += 5
                    message = "稳定性评分: +5 (波动太小)"
                else:
                    message = "稳定性评分: +0 (波动过大)"
            else:
                message = "稳定性评分: +0 (数据不足)"
            
            if return_details:
                details["稳定性评分"] = message
            else:
                print(message)
            
            final_score = max(0, min(100, score))
            
            if return_details:
                details["最终基本面评分"] = final_score
                return final_score, details
            else:
                print(f"基本面总评分: {final_score}")
                return final_score
        
        except Exception as e:
            error_msg = f"计算基本面评分失败 {symbol}: {e}"
            if return_details:
                details["错误"] = error_msg
                return 50, details
            else:
                print(error_msg)
                return 50
    
    def calculate_technical_score(self, symbol, return_details=False):
        """计算技术面评分 - 支持返回详细信息"""
        details = {} 
        
        try:
            # ✅ 修改：从固定日期往前推90天
            base_date_obj = datetime.strptime(self.base_date, '%Y%m%d')
            three_months_ago = (base_date_obj - timedelta(days=90)).strftime('%Y%m%d')
            
            records = list(self.volume_price.find({
                "symbol": symbol,
                "trade_date": {
                    "$gte": three_months_ago,
                    "$lte": self.base_date
                }
            }).sort("trade_date", 1))
            
            data_info = f"技术面数据 ({three_months_ago} 到 {self.base_date}): {len(records)} 条"
            if return_details:
                details["数据范围"] = data_info
                details["数据条数"] = len(records)
            else:
                print(data_info)
        
            if len(records) < 20:
                if return_details:
                    details["评分原因"] = "数据不足，给中等分"
                    return 50, details
                else:
                    return 50
        
            df = pd.DataFrame(records)
        
            # 计算技术面评分
            price_score = self.calculate_price_trend_score(df, return_details)
            volume_score = self.calculate_volume_trend_score(df, return_details)
            indicator_score = self.calculate_technical_indicators_score(df, return_details)

            # 解包分数（无论 return_details 是否为 True，都只取第一个元素为分数）
            if isinstance(price_score, tuple):
                price_score_val, price_details = price_score
                price_score = price_score_val
                if return_details:
                    details["价格趋势分析"] = price_details
            if isinstance(volume_score, tuple):
                volume_score_val, volume_details = volume_score
                volume_score = volume_score_val
                if return_details:
                    details["成交量分析"] = volume_details
            if isinstance(indicator_score, tuple):
                indicator_score_val, indicator_details = indicator_score
                indicator_score = indicator_score_val
                if return_details:
                    details["技术指标分析"] = indicator_details

            score = (price_score * 0.4 + volume_score * 0.3 + indicator_score * 0.3)
            final_score = max(0, min(100, score))
        
            if return_details:
                details["价格趋势得分"] = f"{price_score:.1f} (权重40%)"
                details["成交量得分"] = f"{volume_score:.1f} (权重30%)"
                details["技术指标得分"] = f"{indicator_score:.1f} (权重30%)"
                details["综合技术面评分"] = f"{final_score:.1f}"
                return final_score, details
            else:
                print(f"技术面评分: {final_score}")
                return final_score
        
        except Exception as e:
            error_msg = f"计算技术面评分失败 {symbol}: {e}"
            if return_details:
                details["错误"] = error_msg
                return 50, details
            else:
                print(error_msg)
                return 50
    def calculate_price_trend_score(self, df, return_details=False):
        """计算价格趋势评分"""
        details = {} 
        prices = df['close'].values
        
        # 计算移动平均线
        if len(prices) >= 20:
            ma5 = np.mean(prices[-5:])
            ma10 = np.mean(prices[-10:])
            ma20 = np.mean(prices[-20:])
            current_price = prices[-1]
            
            if return_details:
                details["MA5"] = f"{ma5:.2f}"
                details["MA10"] = f"{ma10:.2f}"
                details["MA20"] = f"{ma20:.2f}"
                details["当前价格"] = f"{current_price:.2f}"
            
            score = 50
            
            # 均线多头排列加分
            if ma5 > ma10 > ma20:
                score += 25
                if return_details:
                    details["均线排列"] = "多头排列 (+25分)"
            elif ma5 > ma10:
                score += 15
                if return_details:
                    details["均线排列"] = "短期向上 (+15分)"
            else:
                if return_details:
                    details["均线排列"] = "无明显优势 (+0分)"
            
            # 价格在均线上方加分
            above_ma5 = current_price > ma5
            above_ma20 = current_price > ma20
            
            if above_ma5:
                score += 15
            if above_ma20:
                score += 10
            
            if return_details:
                details["价格相对MA5"] = f"{'上方 (+15分)' if above_ma5 else '下方 (+0分)'}"
                details["价格相对MA20"] = f"{'上方 (+10分)' if above_ma20 else '下方 (+0分)'}"
                details["价格趋势得分"] = f"{score:.1f}"
                return score, details
            else:
                return score
        
        if return_details:
            details["评分原因"] = "数据不足"
            return 50, details
        else:
            return 50

    def calculate_volume_trend_score(self, df, return_details=False):
        """计算成交量趋势评分"""
        details = {} 
        volumes = df['volume'].values
        prices = df['close'].values
        
        if len(volumes) < 10:
            if return_details:
                details["评分原因"] = "成交量数据不足"
                return 50, details
            else:
                return 50
        
        score = 50
        
        # 计算量价配合
        recent_volumes = volumes[-10:]
        recent_prices = prices[-10:]
        
        avg_volume = np.mean(recent_volumes)
        recent_avg_volume = np.mean(recent_volumes[-5:])
        
        volume_ratio = recent_avg_volume / avg_volume if avg_volume > 0 else 1
        
        if return_details:
            details["平均成交量"] = f"{avg_volume:,.0f}"
            details["近5日平均量"] = f"{recent_avg_volume:,.0f}"
            details["量比"] = f"{volume_ratio:.2f}"
        
        # 成交量放大
        if recent_avg_volume > avg_volume * 1.2:
            score += 20
            if return_details:
                details["成交量变化"] = "明显放大 (+20分)"
        elif recent_avg_volume > avg_volume:
            score += 10
            if return_details:
                details["成交量变化"] = "温和放大 (+10分)"
        else:
            if return_details:
                details["成交量变化"] = "无明显放大 (+0分)"
        
        # 价格上涨时成交量放大
        price_change = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
        volume_change = (recent_avg_volume - avg_volume) / avg_volume
        
        if return_details:
            details["价格变化"] = f"{price_change:.2%}"
            details["成交量变化率"] = f"{volume_change:.2%}"
        
        if price_change > 0 and volume_change > 0:
            score += 15
            if return_details:
                details["量价配合"] = "价涨量增 (+15分)"
        else:
            if return_details:
                details["量价配合"] = "无明显配合 (+0分)"
        
        final_score = max(0, min(100, score))
        
        if return_details:
            details["成交量得分"] = f"{final_score:.1f}"
            return final_score, details
        else:
            return final_score

    def calculate_technical_indicators_score(self, df, return_details=False):
        """计算技术指标评分"""
        details = {} 
        
        try:
            prices = df['close'].values
            
            if len(prices) < 20:
                if return_details:
                    details["评分原因"] = "价格数据不足"
                    return 50, details
                else:
                    return 50
            
            score = 50
            
            # RSI指标
            rsi = self.calculate_rsi(prices)
            if return_details:
                details["RSI值"] = f"{rsi:.2f}"
            
            if 30 <= rsi <= 70:  # RSI在正常范围
                score += 15
                if return_details:
                    details["RSI评分"] = "正常范围 (+15分)"
            elif 20 <= rsi <= 80:
                score += 10
                if return_details:
                    details["RSI评分"] = "可接受范围 (+10分)"
            else:
                if return_details:
                    details["RSI评分"] = "超买超卖 (+0分)"
            
            # MACD指标
            macd_score = self.calculate_macd_score(prices)
            score += macd_score
            
            if return_details:
                if macd_score == 15:
                    details["MACD评分"] = "金叉向上 (+15分)"
                elif macd_score == 10:
                    details["MACD评分"] = "接近零轴 (+10分)"
                else:
                    details["MACD评分"] = "无明显信号 (+0分)"
            
            final_score = max(0, min(100, score))
            
            if return_details:
                details["技术指标得分"] = f"{final_score:.1f}"
                return final_score, details
            else:
                return final_score
            
        except Exception as e:
            error_msg = f"计算技术指标评分失败: {e}"
            if return_details:
                details["错误"] = error_msg
                return 50, details
            else:
                print(error_msg)
                return 50

    def calculate_money_flow_score(self, symbol, return_details=False):
        """计算资金流向评分 - 支持返回详细信息"""
        details = {} 
        
        try:
            # ✅ 修改：从固定日期往前推30天
            base_date_obj = datetime.strptime(self.base_date, '%Y%m%d')
            thirty_days_ago = (base_date_obj - timedelta(days=30)).strftime('%Y%m%d')
            
            money_flow_records = list(self.money_flow.find({
                "symbol": symbol,
                "trade_date": {
                    "$gte": thirty_days_ago,
                    "$lte": self.base_date
                }
            }).sort("trade_date", -1))
            
            data_info = f"资金流数据 ({thirty_days_ago} 到 {self.base_date}): {len(money_flow_records)} 条"
            if return_details:
                details["数据范围"] = data_info
                details["数据条数"] = len(money_flow_records)
            else:
                print(data_info)
        
            if len(money_flow_records) < 10:
                if return_details:
                    details["评分原因"] = "资金流数据不足，给中等分"
                    return 50, details
                else:
                    return 50
        
            score = 50
            net_buy_amounts = []
            
            if return_details:
                details["基础分"] = 50
        
            # 计算大单净买入比例
            for record in money_flow_records:
                large_buy = (record.get('buy_lg_amount', 0) + record.get('buy_elg_amount', 0))
                large_sell = (record.get('sell_lg_amount', 0) + record.get('sell_elg_amount', 0))
                net_buy = large_buy - large_sell
                net_buy_amounts.append(net_buy)
            
            # 最近的资金流趋势
            recent_net_buy = np.mean(net_buy_amounts[:5]) if len(net_buy_amounts) >= 5 else 0
            total_avg_net_buy = np.mean(net_buy_amounts)
            
            recent_info = f"最近5天平均净买入: {recent_net_buy:,.0f}万元"
            total_info = f"30天平均净买入: {total_avg_net_buy:,.0f}万元"
            
            if return_details:
                details["最近5天净买入"] = recent_info
                details["30天平均净买入"] = total_info
            else:
                print(recent_info)
                print(total_info)
        
            # 最近大资金净买入评分
            if recent_net_buy > 0:
                score += 25
                message = "资金流评分: +25 (最近大资金净买入)"
                if return_details:
                    details["最近资金流评分"] = message
                else:
                    print(message)
        
            # 整体大资金净买入评分
            if total_avg_net_buy > 0:
                score += 15
                message = "资金流评分: +15 (整体大资金净买入)"
                if return_details:
                    details["整体资金流评分"] = message
                else:
                    print(message)
        
            # 连续净买入天数
            consecutive_days = 0
            for amount in net_buy_amounts:
                if amount > 0:
                    consecutive_days += 1
                else:
                    break
            
            if consecutive_days >= 3:
                score += 10
                message = f"资金流评分: +10 (连续{consecutive_days}天净买入)"
                if return_details:
                    details["连续净买入评分"] = message
                    details["连续净买入天数"] = consecutive_days
                else:
                    print(message)
        
            final_score = max(0, min(100, score))
            
            if return_details:
                details["资金流总评分"] = f"{final_score:.1f}"
                return final_score, details
            else:
                print(f"资金流总评分: {final_score}")
                return final_score
        
        except Exception as e:
            error_msg = f"计算资金流评分失败 {symbol}: {e}"
            if return_details:
                details["错误"] = error_msg
                return 50, details
            else:
                print(error_msg)
                return 50
    
    def calculate_rsi(self, prices, period=14):
        """计算RSI指标"""
        if len(prices) < period + 1:
            return 50
        
        deltas = np.diff(prices)
        seed = deltas[:period]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        
        if down == 0:
            return 100
        
        rs = up / down
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def calculate_macd_score(self, prices):
        """计算MACD评分"""
        if len(prices) < 26:
            return 0
        
        # 简化的MACD计算
        ema12 = pd.Series(prices).ewm(span=12).mean().iloc[-1]
        ema26 = pd.Series(prices).ewm(span=26).mean().iloc[-1]
        macd_line = ema12 - ema26
        
        # MACD上穿零轴给分
        if macd_line > 0:
            return 15
        elif macd_line > -0.5:  # 接近零轴
            return 10
        
        return 0
    
    def convert_to_monthly(self, daily_records):
        """将日K数据转换为月K数据"""
        if not daily_records:
            return []
        
        monthly_data = {}
        
        for record in daily_records:
            trade_date = record['trade_date']
            if isinstance(trade_date, str):
                if 'T' in trade_date:
                    date_obj = datetime.strptime(trade_date.split('T')[0], '%Y%m%d')
                else:
                    date_obj = datetime.strptime(trade_date, '%Y%m%d')
            else:
                date_obj = trade_date
            
            month_key = date_obj.strftime('%Y%m')
            
            if month_key not in monthly_data:
                monthly_data[month_key] = {
                    'trade_date': month_key,
                    'open': record['open'],
                    'high': record['high'],
                    'low': record['low'],
                    'close': record['close'],
                    'volume': record['volume']
                }
            else:
                monthly_data[month_key]['high'] = max(monthly_data[month_key]['high'], record['high'])
                monthly_data[month_key]['low'] = min(monthly_data[month_key]['low'], record['low'])
                monthly_data[month_key]['close'] = record['close']
                monthly_data[month_key]['volume'] += record['volume']
        
        return sorted(monthly_data.values(), key=lambda x: x['trade_date'])
    
    def get_score_details(self, symbol):
        """获取评分详细信息"""
        return {
            'symbol': symbol,
            'score_date': datetime.now().strftime('%Y-%m-%d'),
            'description': f'股票 {symbol} 的综合评分详情'
        }
    
    def score_all_stocks(self, base_date=None):
        """对所有股票进行评分(排除ST股票)"""
        # ✅ 如果提供了 base_date 参数，则临时使用该日期
        original_base_date = self.base_date
        if base_date:
            self.base_date = base_date
            print(f"📅 批量评分使用基准日期: {self.base_date}")
        
        try:
            # 查询时排除ST股票
            stocks = list(self.stock_info.find({
                "name": {"$not": {"$regex": "ST|\\*ST|S\\*ST", "$options": "i"}}
            }))
            
            print(f"开始对 {len(stocks)} 只股票进行评分(已排除ST股票)...")
            
            results = []
            
            for i, stock in enumerate(stocks, 1):
                symbol = stock['symbol']
                name = stock.get('name', '')
                
                # 双重检查：再次确认不是ST股票
                if self.is_st_stock(name):
                    print(f"跳过ST股票: {symbol} - {name}")
                    continue
                
                print(f"正在评分: {symbol} - {name} ({i}/{len(stocks)})")
                
                # ✅ 不需要再传递 base_date，因为已经设置了实例变量
                score_data = self.calculate_stock_score(symbol)
                
                if score_data:
                    print(score_data['symbol'], score_data['total_score'])
                    # 更新数据库
                    self.stock_scores.replace_one(
                        {"symbol": symbol, "name": name, "score_date": self.base_date},  # 按日期区分评分记录
                        score_data,
                        upsert=True
                    )
                    results.append(score_data)
            
            print(f"评分完成！共评分 {len(results)} 只股票(已排除ST股票)")
            return results
        
        finally:
            # ✅ 恢复原始的基准日期
            self.base_date = original_base_date

    def get_top_stocks(self, limit=100, score_date=None):
        """获取Top N的股票(排除ST股票)"""
        # ✅ 支持按指定日期查询评分
        query = {}
        if score_date:
            query["score_date"] = score_date
            print(f"📅 查询 {score_date} 的评分排名")
    
        # 先获取所有评分数据
        all_stocks = list(self.stock_scores.find(query).sort("total_score", -1))
        
        # 过滤掉ST股票
        filtered_stocks = []
        for stock in all_stocks:
            stock_info = self.stock_info.find_one({"symbol": stock['symbol']})
            if stock_info:
                name = stock_info.get('name', '')
                # 跳过ST股票
                if self.is_st_stock(name):
                    continue
                
                stock['name'] = name
                stock['industry'] = stock_info.get('industry', '')
                filtered_stocks.append(stock)
                
                # 达到限制数量就停止
                if len(filtered_stocks) >= limit:
                    break
    
        return filtered_stocks

    def get_stock_score_history(self, symbol, days=30):
        """获取股票的历史评分记录"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        history = list(self.stock_scores.find({
            "symbol": symbol,
            "score_date": {"$gte": start_date, "$lte": end_date}
        }).sort("score_date", -1))
        
        return history

    def compare_scores_by_date(self, symbol, date1, date2):
        """比较同一股票在两个不同日期的评分"""
        score1 = self.stock_scores.find_one({"symbol": symbol, "score_date": date1})
        score2 = self.stock_scores.find_one({"symbol": symbol, "score_date": date2})
        
        if not score1 or not score2:
            print(f"❌ 未找到 {symbol} 在指定日期的评分数据")
            return None
        
        print(f"\n📊 {symbol} 评分对比:")
        print(f"日期: {date1} vs {date2}")
        print(f"综合评分: {score1['total_score']:.2f} → {score2['total_score']:.2f} ({score2['total_score'] - score1['total_score']:+.2f})")
        print(f"周期评分: {score1['cycle_score']:.2f} → {score2['cycle_score']:.2f} ({score2['cycle_score'] - score1['cycle_score']:+.2f})")
        print(f"基本面评分: {score1['fundamental_score']:.2f} → {score2['fundamental_score']:.2f} ({score2['fundamental_score'] - score1['fundamental_score']:+.2f})")
        print(f"技术面评分: {score1['technical_score']:.2f} → {score2['technical_score']:.2f} ({score2['technical_score'] - score1['technical_score']:+.2f})")
        print(f"资金流评分: {score1['money_flow_score']:.2f} → {score2['money_flow_score']:.2f} ({score2['money_flow_score'] - score1['money_flow_score']:+.2f})")
        
        return {'score1': score1, 'score2': score2}
    
    def is_st_stock(self, stock_name):
        """判断是否为ST股票"""
        if not stock_name:
            return False
        
        # ST股票的各种标识
        st_patterns = [
            'ST',     # 普通ST
            '*ST',    # 退市风险警示
            'S*ST',   # 暂停上市
            'SST',    # 特别处理
            'N*ST',   # 新上市即退市风险
            'PT',     # 特别转让
        ]
        
        stock_name_upper = stock_name.upper()
        
        for pattern in st_patterns:
            if pattern in stock_name_upper:
                return True
        
        return False

    def analyze_stock_detailed(self, symbol, base_date=None):
        """详细分析单个股票的评分构成"""
        # ✅ 如果提供了 base_date 参数，则临时使用该日期
        original_base_date = self.base_date
        if base_date:
            self.base_date = base_date
    
        try:
            print(f"\n=== 详细分析股票 {symbol} (基准日期: {self.base_date}) ===")
            
            # 获取股票基本信息
            stock_info = self.stock_info.find_one({"symbol": symbol})
            if stock_info:
                print(f"股票名称: {stock_info.get('name', '')}")
                print(f"所属行业: {stock_info.get('industry', '')}")
            
            # 调用带详细信息的版本
            cycle_score, cycle_details = self.calculate_cycle_score(symbol, return_details=True)
            fundamental_score, fundamental_details = self.calculate_fundamental_score(symbol, return_details=True)
            technical_score, technical_details = self.calculate_technical_score(symbol, return_details=True)
            money_flow_score, money_flow_details = self.calculate_money_flow_score(symbol, return_details=True)
            
            # 显示详细信息...
            print(f"\n📈 经济周期评分分析: {cycle_score}")
            for key, value in cycle_details.items():
                print(f"  - {key}: {value}")
            
            print(f"\n📊 基本面评分分析: {fundamental_score}")
            for key, value in fundamental_details.items():
                print(f"  - {key}: {value}")
            
            print(f"\n📈 技术面评分分析: {technical_score}")
            for key, value in technical_details.items():
                print(f"  - {key}: {value}")
            
            print(f"\n💰 资金流评分分析: {money_flow_score}")
            for key, value in money_flow_details.items():
                print(f"  - {key}: {value}")
            
            # 综合评分
            weights = {'cycle': 0.25, 'fundamental': 0.35, 'technical': 0.25, 'money_flow': 0.15}
            total_score = (
                cycle_score * weights['cycle'] +
                fundamental_score * weights['fundamental'] +
                technical_score * weights['technical'] +
                money_flow_score * weights['money_flow']
            )
            
            print(f"\n🎯 综合评分: {total_score:.2f}")
            print(f"各项加权得分:")
            print(f"  - 周期评分贡献: {cycle_score * weights['cycle']:.2f} (权重25%)")
            print(f"  - 基本面贡献: {fundamental_score * weights['fundamental']:.2f} (权重35%)")
            print(f"  - 技术面贡献: {technical_score * weights['technical']:.2f} (权重25%)")
            print(f"  - 资金流贡献: {money_flow_score * weights['money_flow']:.2f} (权重15%)")
            
            return total_score
            
        except Exception as e:
            print(f"分析失败: {e}")
            return None
        finally:
            # ✅ 恢复原始的基准日期
            self.base_date = original_base_date



# 在主程序部分添加万科的详细分析
if __name__ == "__main__":
    import sys
    
    # ✅ 支持命令行参数传递日期
    if len(sys.argv) > 1:
        base_date = sys.argv[1]
        print(f"📅 使用命令行指定的基准日期: {base_date}")
    else:
        base_date = "20250910"  # 默认日期
        print(f"📅 使用默认基准日期: {base_date}")
    
    # 创建评分系统实例
    scorer = StockScoringSystem(base_date=base_date)
    
    # # 示例1: 使用指定日期进行评分
    # print("\n🔍 示例1: 使用指定日期评分万科")
    # result = scorer.calculate_stock_score("000002")
    # if result:
    #     print(f"万科 ({base_date}) 综合评分: {result['total_score']}")
    
    # # 示例2: 使用不同日期进行评分
    # print("\n🔍 示例2: 使用不同日期评分万科")
    # result_20250902 = scorer.calculate_stock_score("000002", base_date="20250902")
    # if result_20250902:
    #     print(f"万科 (20250902) 综合评分: {result_20250902['total_score']}")
    
    # 示例3: 批量评分所有股票
    print("\n🔍 示例3: 批量评分所有股票")
    choice = input("是否要对所有股票进行评分? (y/n): ")
    if choice.lower() == 'y':
        scorer.score_all_stocks(base_date=base_date)
    
    # # 示例4: 详细分析
    # print("\n🔍 示例4: 详细分析万科")
    # scorer.analyze_stock_detailed("000002", base_date=base_date)
    
    # # 示例5: 查看Top股票
    # print("\n🔍 示例5: 查看Top 10股票")
    # top_stocks = scorer.get_top_stocks(limit=10, score_date=base_date)
    # for i, stock in enumerate(top_stocks, 1):
    #     print(f"{i}. {stock['symbol']} - {stock['name']} | 评分: {stock['total_score']}")
    
    # # 示例6: 历史评分对比
    # print("\n🔍 示例6: 万科历史评分")
    # history = scorer.get_stock_score_history("000002", days=10)
    # for record in history:
    #     print(f"{record['score_date']}: {record['total_score']:.2f}")


    # # 测试万科数据
    # print("🔍 分析万科 (000002) - 基于 20250902 数据:")
 
    # if result:
    #     print(f"\n📊 万科评分结果:")
    #     print(f"  - 周期评分: {result['cycle_score']}")
    #     print(f"  - 基本面评分: {result['fundamental_score']}")
    #     print(f"  - 技术面评分: {result['technical_score']}")
    #     print(f"  - 资金流评分: {result['money_flow_score']}")
    #     print(f"  - 综合评分: {result['total_score']}")
    # else:
    #     print("❌ 万科评分失败")
    
    # # 详细分析万科
    # print("🔍 分析万科为什么排名靠前:")
    # scorer.analyze_stock_detailed("000002")
    
    # # 也可以对比分析排名第一的股票
    # top_stocks = scorer.get_top_stocks(5)
    # if len(top_stocks) > 0:
    #     print(f"\n🔍 对比分析排名第一的股票 {top_stocks[0]['symbol']}:")
    #     scorer.analyze_stock_detailed(top_stocks[0]['symbol'])