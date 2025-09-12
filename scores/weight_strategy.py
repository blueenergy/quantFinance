#!/usr/bin/env python3
"""权重配置策略分析"""

def analyze_scorer_differences():
    """分析各评分器的差异和定位"""
    
    scorer_analysis = {
        "value": {
            "核心关注": "价格合理性",
            "主要指标": ["PE比率", "PB比率", "PS比率", "股息率", "PEG"],
            "回答问题": "这只股票便宜吗？",
            "投资逻辑": "价值投资 - 买入被低估的股票",
            "时间特性": "相对稳定，变化较慢",
            "适用场景": "熊市、震荡市、长期投资"
        },
        
        "fundamental": {
            "核心关注": "公司经营质量",
            "主要指标": ["ROE", "ROA", "负债率", "利润率", "经营稳定性"],
            "回答问题": "这家公司经营得好吗？",
            "投资逻辑": "质量投资 - 买入优质公司",
            "时间特性": "相对稳定，反映长期竞争力",
            "适用场景": "所有市场环境的基础评估"
        },
        
        "growth": {
            "核心关注": "成长潜力",
            "主要指标": ["收入增长率", "利润增长率", "ROE增长", "市场扩张"],
            "回答问题": "这家公司未来能快速增长吗？",
            "投资逻辑": "成长投资 - 买入高增长公司",
            "时间特性": "变化较快，受业务周期影响",
            "适用场景": "牛市、新兴行业、中长期投资"
        },
        
        "technical": {
            "核心关注": "价格趋势和时机",
            "主要指标": ["移动平均线", "RSI", "MACD", "价格形态"],
            "回答问题": "现在是买入的好时机吗？",
            "投资逻辑": "趋势投资 - 顺势而为",
            "时间特性": "变化最快，反映市场情绪",
            "适用场景": "短期交易、择时投资"
        },
        
        "money_flow": {
            "核心关注": "资金流向和机构态度",
            "主要指标": ["主力资金流入", "北向资金", "机构持仓变化"],
            "回答问题": "聪明钱在买入还是卖出？",
            "投资逻辑": "跟随聪明钱",
            "时间特性": "变化较快，领先价格变化",
            "适用场景": "短中期投资、情绪判断"
        },
        
        "cycle": {
            "核心关注": "行业和经济周期位置",
            "主要指标": ["行业景气度", "周期性指标", "相对强度"],
            "回答问题": "现在是这个行业的好时机吗？",
            "投资逻辑": "周期投资 - 逆周期布局",
            "时间特性": "中等变化速度，跟随经济周期",
            "适用场景": "周期性行业、宏观配置"
        }
    }
    
    return scorer_analysis

def design_weight_strategies():
    """设计不同的权重策略"""
    
    strategies = {
        # 1. 均衡型策略
        "balanced": {
            "名称": "🎯 均衡型投资策略",
            "适用对象": "普通投资者、追求稳健收益",
            "权重配置": {
                'fundamental': 0.25,    # 基本面 25% - 质量基础
                'value': 0.20,          # 价值面 20% - 安全边际  
                'growth': 0.20,         # 成长面 20% - 收益来源
                'technical': 0.20,      # 技术面 20% - 择时参考
                'money_flow': 0.10,     # 资金流 10% - 情绪指标
                'cycle': 0.05           # 周期面 5% - 宏观参考
            },
            "说明": "注重公司质量和估值安全，兼顾成长和择时"
        },
        
        # 2. 价值导向策略
        "value_oriented": {
            "名称": "💰 价值导向策略",
            "适用对象": "价值投资者、熊市/震荡市",
            "权重配置": {
                'fundamental': 0.30,    # 基本面 30% - 质量优先
                'value': 0.30,          # 价值面 30% - 估值核心
                'growth': 0.15,         # 成长面 15% - 适度成长
                'technical': 0.15,      # 技术面 15% - 基础择时
                'money_flow': 0.05,     # 资金流 5% - 参考指标
                'cycle': 0.05           # 周期面 5% - 宏观背景
            },
            "说明": "以基本面和估值为核心，寻找优质低估股票"
        },
        
        # 3. 成长导向策略  
        "growth_oriented": {
            "名称": "🚀 成长导向策略",
            "适用对象": "成长投资者、牛市环境",
            "权重配置": {
                'growth': 0.35,         # 成长面 35% - 成长核心
                'fundamental': 0.25,    # 基本面 25% - 质量保障
                'technical': 0.20,      # 技术面 20% - 趋势确认
                'money_flow': 0.10,     # 资金流 10% - 资金确认
                'value': 0.05,          # 价值面 5% - 基础参考
                'cycle': 0.05           # 周期面 5% - 行业背景
            },
            "说明": "以成长性为核心，在优质公司中寻找高增长标的"
        },
        
        # 4. 交易导向策略
        "trading_oriented": {
            "名称": "⚡ 交易导向策略", 
            "适用对象": "短期交易者、专业投资者",
            "权重配置": {
                'technical': 0.40,      # 技术面 40% - 择时核心
                'money_flow': 0.25,     # 资金流 25% - 资金确认
                'fundamental': 0.15,    # 基本面 15% - 质量筛选
                'growth': 0.10,         # 成长面 10% - 题材支撑
                'cycle': 0.05,          # 周期面 5% - 行业背景
                'value': 0.05           # 价值面 5% - 基础参考
            },
            "说明": "以技术分析和资金流为核心，追求短期收益"
        },
        
        # 5. 防御型策略
        "defensive": {
            "名称": "🛡️ 防御型策略",
            "适用对象": "保守投资者、风险厌恶者",
            "权重配置": {
                'fundamental': 0.35,    # 基本面 35% - 质量优先
                'value': 0.35,          # 价值面 35% - 安全边际
                'growth': 0.10,         # 成长面 10% - 温和成长
                'technical': 0.10,      # 技术面 10% - 基础择时
                'money_flow': 0.05,     # 资金流 5% - 流动性
                'cycle': 0.05           # 周期面 5% - 防御性行业
            },
            "说明": "极度重视公司质量和估值安全，追求稳健收益"
        }
    }
    
    return strategies

def create_adaptive_analyzer():
    """创建自适应权重分析器"""
    
    adaptive_code = '''
class AdaptiveStockAnalyzer(StockAnalyzer):
    """
    🤖 自适应股票分析器
    
    根据市场环境、行业特点、投资风格自动调整权重
    """
    
    def __init__(self, db_manager, base_date=None, strategy='balanced'):
        """
        初始化自适应分析器
        
        Args:
            strategy: 投资策略 ('balanced', 'value_oriented', 'growth_oriented', 
                     'trading_oriented', 'defensive')
        """
        super().__init__(db_manager, base_date)
        self.strategy = strategy
        self.weight_strategies = self._load_weight_strategies()
        self._apply_strategy(strategy)
    
    def _load_weight_strategies(self):
        """加载权重策略配置"""
        return {
            'balanced': {
                'fundamental': 0.25,
                'value': 0.20,
                'growth': 0.20,
                'technical': 0.20,
                'money_flow': 0.10,
                'cycle': 0.05
            },
            'value_oriented': {
                'fundamental': 0.30,
                'value': 0.30,
                'growth': 0.15,
                'technical': 0.15,
                'money_flow': 0.05,
                'cycle': 0.05
            },
            'growth_oriented': {
                'growth': 0.35,
                'fundamental': 0.25,
                'technical': 0.20,
                'money_flow': 0.10,
                'value': 0.05,
                'cycle': 0.05
            },
            'trading_oriented': {
                'technical': 0.40,
                'money_flow': 0.25,
                'fundamental': 0.15,
                'growth': 0.10,
                'cycle': 0.05,
                'value': 0.05
            },
            'defensive': {
                'fundamental': 0.35,
                'value': 0.35,
                'growth': 0.10,
                'technical': 0.10,
                'money_flow': 0.05,
                'cycle': 0.05
            }
        }
    
    def _apply_strategy(self, strategy):
        """应用投资策略"""
        if strategy in self.weight_strategies:
            self.weights = self.weight_strategies[strategy].copy()
            print(f"✅ 已应用投资策略: {strategy}")
            self._print_strategy_details()
        else:
            print(f"⚠️ 未知策略: {strategy}，使用默认均衡策略")
            self.weights = self.weight_strategies['balanced'].copy()
    
    def _print_strategy_details(self):
        """打印策略详情"""
        print(f"📊 当前权重配置:")
        for scorer, weight in self.weights.items():
            print(f"  {scorer}: {weight*100:.1f}%")
    
    def switch_strategy(self, new_strategy):
        """切换投资策略"""
        print(f"🔄 切换投资策略: {self.strategy} -> {new_strategy}")
        self.strategy = new_strategy
        self._apply_strategy(new_strategy)
    
    def adapt_to_market(self, market_condition):
        """根据市场环境自适应调整"""
        adaptation_map = {
            'bull_market': 'growth_oriented',      # 牛市偏向成长
            'bear_market': 'defensive',            # 熊市偏向防御
            'volatile_market': 'trading_oriented', # 震荡市偏向交易
            'normal_market': 'balanced'            # 正常市场均衡配置
        }
        
        if market_condition in adaptation_map:
            recommended_strategy = adaptation_map[market_condition]
            print(f"🎯 检测到市场环境: {market_condition}")
            print(f"💡 推荐策略: {recommended_strategy}")
            
            choice = input(f"是否切换到推荐策略? (y/N): ")
            if choice.lower() == 'y':
                self.switch_strategy(recommended_strategy)
        else:
            print(f"⚠️ 未知市场环境: {market_condition}")
    
    def adapt_to_industry(self, industry_type):
        """根据行业特点调整权重"""
        industry_adjustments = {
            'technology': {  # 科技行业 - 重视成长和技术面
                'growth': 0.05,
                'technical': 0.05,
                'fundamental': -0.05,
                'value': -0.05
            },
            'utilities': {   # 公用事业 - 重视价值和基本面
                'value': 0.05,
                'fundamental': 0.05,
                'growth': -0.05,
                'technical': -0.05
            },
            'cyclical': {    # 周期性行业 - 重视周期和技术面
                'cycle': 0.10,
                'technical': 0.05,
                'value': -0.05,
                'growth': -0.05,
                'fundamental': -0.05
            },
            'financial': {   # 金融行业 - 重视价值和周期
                'value': 0.05,
                'cycle': 0.05,
                'growth': -0.05,
                'technical': -0.05
            }
        }
        
        if industry_type in industry_adjustments:
            print(f"🏭 检测到行业类型: {industry_type}")
            adjustments = industry_adjustments[industry_type]
            
            # 应用行业调整
            adjusted_weights = self.weights.copy()
            for scorer, adjustment in adjustments.items():
                if scorer in adjusted_weights:
                    adjusted_weights[scorer] += adjustment
            
            # 确保权重总和为1
            total_weight = sum(adjusted_weights.values())
            if abs(total_weight - 1.0) > 0.01:
                # 标准化权重
                for scorer in adjusted_weights:
                    adjusted_weights[scorer] /= total_weight
            
            print(f"🔧 行业调整后的权重:")
            for scorer, weight in adjusted_weights.items():
                change = weight - self.weights[scorer]
                arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➡️"
                print(f"  {scorer}: {weight*100:.1f}% {arrow}")
            
            choice = input(f"是否应用行业调整? (y/N): ")
            if choice.lower() == 'y':
                self.weights = adjusted_weights
                print("✅ 行业权重调整已应用")
    
    def get_strategy_comparison(self, symbol, strategies=None):
        """对比不同策略下的评分结果"""
        if strategies is None:
            strategies = ['balanced', 'value_oriented', 'growth_oriented']
        
        print(f"\\n📊 策略对比分析 - {symbol}")
        print("="*60)
        
        results = {}
        original_strategy = self.strategy
        
        for strategy in strategies:
            print(f"\\n🔍 {strategy} 策略分析...")
            self.switch_strategy(strategy)
            
            try:
                result = self.analyze_stock(symbol)
                results[strategy] = {
                    'composite_score': result['composite_score'],
                    'rating': result['recommendations']['overall_rating'],
                    'scores': result['scores']
                }
                print(f"  📊 综合评分: {result['composite_score']:.2f}")
                print(f"  🏆 投资评级: {result['recommendations']['overall_rating']}")
                
            except Exception as e:
                results[strategy] = {'error': str(e)}
                print(f"  ❌ 分析失败: {e}")
        
        # 恢复原始策略
        self.switch_strategy(original_strategy)
        
        # 输出对比结果
        print(f"\\n📋 策略对比总结:")
        print("-"*60)
        for strategy, result in results.items():
            if 'composite_score' in result:
                print(f"{strategy:15}: {result['composite_score']:6.2f}分 - {result['rating']}")
            else:
                print(f"{strategy:15}: 分析失败")
        
        return results
    
    @classmethod
    def create_for_market_condition(cls, db_manager, market_condition, base_date=None):
        """根据市场环境创建分析器"""
        condition_strategy_map = {
            'bull': 'growth_oriented',
            'bear': 'defensive', 
            'volatile': 'trading_oriented',
            'normal': 'balanced'
        }
        
        strategy = condition_strategy_map.get(market_condition, 'balanced')
        analyzer = cls(db_manager, base_date, strategy)
        
        print(f"🎯 已创建 {market_condition} 市场环境下的分析器")
        return analyzer
'''
    
    return adaptive_code