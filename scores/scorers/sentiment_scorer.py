from . import BaseScorer
import numpy as np

class SentimentScorer(BaseScorer):
    """
    😊 市场情绪评分器
    
    分析市场情绪指标：
    - 新闻舆情分析
    - 社交媒体情绪
    - 市场恐慌指数
    - 投资者行为指标
    """
    
    def calculate_score_detailed(self, symbol):
        """计算市场情绪评分"""
        details = {}
        score = 0
        
        try:
            # 1. 新闻情绪分析 (30%)
            news_score = self._analyze_news_sentiment(symbol)
            details["新闻情绪"] = news_score
            score += news_score * 0.3
            
            # 2. 技术指标情绪 (25%)
            tech_sentiment = self._analyze_technical_sentiment(symbol)
            details["技术情绪"] = tech_sentiment
            score += tech_sentiment * 0.25
            
            # 3. 资金流向情绪 (25%)
            flow_sentiment = self._analyze_flow_sentiment(symbol)
            details["资金情绪"] = flow_sentiment
            score += flow_sentiment * 0.25
            
            # 4. 市场热度 (20%)
            heat_score = self._analyze_market_heat(symbol)
            details["市场热度"] = heat_score
            score += heat_score * 0.2
            
            return round(score, 2), details
            
        except Exception as e:
            return 50.0, {"错误": f"情绪分析失败: {e}"}
    
    def _analyze_news_sentiment(self, symbol):
        """分析新闻情绪"""
        # 这里可以接入新闻API或爬虫数据
        # 暂时返回中性评分
        return 55.0
    
    def _analyze_technical_sentiment(self, symbol):
        """分析技术指标情绪"""
        try:
            price_data = self.get_volume_price_data(symbol, 20)
            if not price_data:
                return 50.0
            
            # 基于价格趋势的情绪分析
            prices = [data.get('close', 0) for data in price_data]
            if len(prices) >= 10:
                recent_trend = np.mean(prices[-5:]) - np.mean(prices[-10:-5])
                if recent_trend > 0:
                    return 70.0  # 上涨趋势，情绪偏乐观
                else:
                    return 40.0  # 下跌趋势，情绪偏悲观
            
            return 50.0
            
        except:
            return 50.0
    
    def _analyze_flow_sentiment(self, symbol):
        """分析资金流向情绪"""
        try:
            flow_data = self.get_money_flow_data(symbol, 10)
            if not flow_data:
                return 50.0
            
            # 简化的资金流情绪分析
            net_flows = []
            for data in flow_data:
                large_flow = data.get('net_mf_amount_l', 0)
                small_flow = data.get('net_mf_amount_s', 0)
                if large_flow > 0:
                    net_flows.append(1)  # 大单流入
                elif large_flow < 0:
                    net_flows.append(-1)  # 大单流出
                else:
                    net_flows.append(0)
            
            if net_flows:
                avg_sentiment = np.mean(net_flows)
                return 50 + avg_sentiment * 30  # 转换为0-100分
            
            return 50.0
            
        except:
            return 50.0
    
    def _analyze_market_heat(self, symbol):
        """分析市场热度"""
        try:
            # 基于成交量和换手率分析市场热度
            price_data = self.get_volume_price_data(symbol, 10)
            if not price_data:
                return 50.0
            
            volumes = [data.get('volume', 0) for data in price_data]
            turnover_rates = [data.get('turnover_rate', 0) for data in price_data if data.get('turnover_rate')]
            
            if volumes and turnover_rates:
                avg_volume = np.mean(volumes[-5:]) / np.mean(volumes[-10:-5]) if len(volumes) >= 10 else 1
                avg_turnover = np.mean(turnover_rates[-5:])
                
                # 成交量放大 + 换手率适中 = 高热度
                if avg_volume > 1.5 and 2 <= avg_turnover <= 8:
                    return 80.0
                elif avg_volume > 1.2:
                    return 65.0
                else:
                    return 45.0
            
            return 50.0
            
        except:
            return 50.0