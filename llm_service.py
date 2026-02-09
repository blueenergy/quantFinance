"""
LLM API service class
Supports OpenAI, DeepSeek, etc. external LLM API services compatible with OpenAI interface
"""

import os
import json
import httpx
from typing import Dict, List, Optional, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class LLMService:
    def __init__(self, provider: str = None, model: str = None, api_key: str = None, base_url: str = None):
        """
        Initialize LLM service
        
        Args:
            provider: LLM service provider (openai, deepseek, custom) - REQUIRED
            model: Model name to use - REQUIRED
            api_key: API key - REQUIRED
            base_url: API base URL (optional, will use provider defaults if not provided)
        """
        # 不使用任何默认值，完全依赖用户配置
        if not provider:
            raise ValueError("必须指定 LLM provider")
        if not model:
            raise ValueError("必须指定 LLM model")
        if not api_key:
            raise ValueError("必须指定 API key")
            
        self.provider = provider
        self.model = model
        self.api_key_override = api_key
        self.base_url_override = base_url
        
        # Set up API configuration based on provider
        self._setup_api_config()
        
    def _setup_api_config(self):
        """设置 API 配置 - 不使用环境变量，完全依赖用户配置"""
        # API key 必须由用户提供
        self.api_key = self.api_key_override
        
        # Base URL: 优先使用用户配置，否则使用 provider 的默认值
        if self.base_url_override:
            self.base_url = self.base_url_override
        else:
            # 仅为不同 provider 提供默认的 API endpoint
            if self.provider == 'openai':
                self.base_url = 'https://api.openai.com/v1'
            elif self.provider == 'deepseek':
                self.base_url = 'https://api.deepseek.com/v1'
            elif self.provider == 'custom':
                raise ValueError("对于 custom provider，必须提供 base_url")
            else:
                raise ValueError(f"不支持的 provider: {self.provider}")
    
    async def analyze_stock(self, stock_code: str, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze stock data
        
        Args:
            stock_code: Stock code
            stock_data: Stock data
            
        Returns:
            Analysis result
        """
        print(f"[股票分析] 开始分析股票: {stock_code}")
        
        # Build analysis prompt
        prompt = self._build_stock_analysis_prompt(stock_code, stock_data)
        print(f"[股票分析] 提示词构建完成，长度: {len(prompt)} 字符")
        
        try:
            # Call LLM API
            response = await self._call_llm_api(prompt)
            
            # Parse response
            analysis_result = self._parse_analysis_result(response)
            
            print(f"[股票分析] 分析完成 - 股票: {stock_code}")
            
            return {
                "success": True,
                "stock_code": stock_code,
                "analysis": analysis_result,
                "provider": self.provider,
                "model": self.model,
                "raw_response": response[:500] + "..." if len(response) > 500 else response  # Keep first 500 characters of raw response for debugging
            }
        except Exception as e:
            error_msg = str(e) if str(e) else f"Unknown error: {type(e).__name__}"
            print(f"股票分析失败 - 股票: {stock_code}, 错误: {error_msg}")
            print(f"错误类型: {type(e)}")
            print(f"错误详情: {repr(e)}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": type(e).__name__,
                "stock_code": stock_code,
                "provider": self.provider,
                "model": self.model
            }
    
    async def analyze_market(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data
        
        Args:
            market_data: Market data (including major index information)
            
        Returns:
            Market analysis result
        """
        print(f"[市场分析] 开始分析市场数据")
        print(f"[市场分析] 包含 {len(market_data)} 个指数数据")
        
        # Build market analysis prompt
        prompt = self._build_market_analysis_prompt(market_data)
        print(f"[市场分析] 提示词构建完成，长度: {len(prompt)} 字符")
        
        try:
            # Call LLM API
            response = await self._call_llm_api(prompt)
            
            # Parse response
            analysis_result = self._parse_market_analysis_result(response)
            
            print(f"[市场分析] 分析完成")
            
            return {
                "success": True,
                "analysis": analysis_result,
                "provider": self.provider,
                "model": self.model,
                "raw_response": response[:500] + "..." if len(response) > 500 else response
            }
        except Exception as e:
            error_msg = str(e) if str(e) else f"Unknown error: {type(e).__name__}"
            print(f"市场分析失败, 错误: {error_msg}")
            print(f"错误类型: {type(e)}")
            print(f"错误详情: {repr(e)}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": type(e).__name__,
                "provider": self.provider,
                "model": self.model
            }
    
    def _build_market_analysis_prompt(self, market_data: Dict[str, Any]) -> str:
        """Build market analysis prompt"""
        
        # Get analysis date and market data
        analysis_date = market_data.get("analysis_date", "未知日期")
        
        # Handle different data formats
        if "market_data" in market_data:
            # New format: market_data nested field
            data = market_data["market_data"]
        else:
            # Old format: directly contains index data
            data = {k: v for k, v in market_data.items() 
                   if k not in ["analysis_date", "data_source", "ladder_data"] and isinstance(v, dict)}
        
        # 获取连板天梯数据（如果有）
        ladder_data = market_data.get("ladder_data")
        
        # Add current time information
        from datetime import datetime
        current_date = datetime.now().strftime('%Y年%m月%d日')
        
        prompt_parts = [
            f"当前时间：{current_date}\n",
            f"你是一位资深的证券分析师，请对{analysis_date}的A股市场表现进行全面分析。\n",
            f"以下是{analysis_date}的主要指数数据：\n"
        ]
        
        # Add data for each index
        for symbol, stock_data in data.items():
            latest = stock_data.get('latest_record', {})
            historical = stock_data.get('historical_records', [])
            
            index_name = {
                '1A0001': '上证指数',
                '399001': '深证成指',
                '399006': '创业板指',
                '899050': '北证50'
            }.get(symbol, f'指数{symbol}')
            
            trade_date = latest.get('trade_date', '未知日期')
            if hasattr(trade_date, 'strftime'):
                trade_date = trade_date.strftime('%Y-%m-%d')
            elif isinstance(trade_date, str) and len(trade_date) > 10:
                trade_date = trade_date[:10]
            
            prompt_parts.append(f"{index_name} ({symbol}) - {trade_date}:")
            prompt_parts.append(f"收盘点位: {latest.get('close', 0)}")
            
            # Calculate daily change
            open_price = latest.get('open', 0)
            close_price = latest.get('close', 0)
            if open_price > 0:
                daily_change = ((close_price - open_price) / open_price * 100)
                prompt_parts.append(f"当日涨跌幅: {daily_change:.2f}%")
            
            prompt_parts.append(f"成交量: {latest.get('volume', 0):,}")
            
            # Calculate change compared to previous trading day
            if len(historical) >= 2:
                prev_close = historical[1].get('close', close_price)
                if prev_close > 0:
                    change_pct = ((close_price - prev_close) / prev_close * 100)
                    prompt_parts.append(f"较前一交易日: {change_pct:+.2f}%")
            
            prompt_parts.append("")
        
        # 新增: 连板天梯数据部分
        if ladder_data:
            prompt_parts.append("## 连板天梯数据\n")
            prompt_parts.append(f"- 情绪得分: {ladder_data.get('sentiment_score', 'N/A')} ({ladder_data.get('sentiment_level', '')})")
            prompt_parts.append(f"- 涨停家数: {ladder_data.get('total_limit_up', 'N/A')}只")
            prompt_parts.append(f"- 最高连板: {ladder_data.get('max_streak', 'N/A')}板")
            
            # 最高连板股票
            highest = ladder_data.get('highest_stock')
            if highest and highest.get('name'):
                prompt_parts.append(f"  (龙头: {highest.get('name')} {highest.get('streak')}连板)")
            
            # 梯队分布
            tier_dist = ladder_data.get('tier_distribution', {})
            if tier_dist:
                dist_str = ', '.join([f"{k}板: {v}只" for k, v in sorted(tier_dist.items(), key=lambda x: x[0], reverse=True)])
                prompt_parts.append(f"- 梯队分布: {dist_str}")
            
            # 晋级率
            promotion_rate = ladder_data.get('promotion_rate', 0)
            prompt_parts.append(f"- 晋级率: {promotion_rate*100:.1f}% (首板→二板及以上)")
            
            # 主线板块
            mainlines = ladder_data.get('mainlines', [])
            if mainlines:
                prompt_parts.append(f"- 主线板块: {', '.join(mainlines)}")
            
            prompt_parts.append("")
        
        prompt_parts.extend([
            f"请基于{analysis_date}的市场数据，从以下几个维度进行分析:",
            "1. 市场整体走势分析",
            "2. 各板块表现对比", 
            "3. 成交量变化分析",
            "4. 市场情绪判断（结合连板天梯情绪得分）" if ladder_data else "4. 市场情绪判断",
            "5. 短期市场展望（1-3个交易日）",
            "6. 风险提示与投资建议",
        ])
        
        # 如果有天梯数据，增加额外分析维度
        if ladder_data:
            prompt_parts.extend([
                "7. 连板天梯分析（龙头股演绎、板块轮动特征）",
                "8. 主线板块追踪（今日主线延续性判断）",
            ])
        
        prompt_parts.extend([
            "",
            "请以JSON格式返回分析结果，包含以下字段:",
            "{",
            '  "mood": "市场情绪(如:谨慎乐观/积极向好/谨慎观望)",',
            '  "summary": "市场概览(100-200字)",',
            '  "keyPoints": ["关键要点1", "关键要点2", "关键要点3"],',
            '  "outlook": "市场展望(100-150字)",',
            '  "riskLevel": "风险等级(low/medium/high)",',
            '  "riskNote": "风险提示(50-100字)",',
        ])
        
        if ladder_data:
            prompt_parts.extend([
                '  "ladderInsight": "连板天梯分析(50-100字，分析龙头演绎和情绪周期)",',
                '  "mainlineAnalysis": "主线板块分析(50-100字，分析板块轮动和资金动向)"',
            ])
        
        prompt_parts.extend([
            "}",
            "",
            f"请确保分析客观专业，明确基于{analysis_date}的市场表现。"
        ])
        
        return "\n".join(prompt_parts)

    
    def _parse_market_analysis_result(self, response_text: str) -> Dict[str, Any]:
        """Parse market analysis result"""
        try:
            # Try to parse JSON directly
            result = json.loads(response_text)
            return result
        except json.JSONDecodeError:
            # If not valid JSON, try to extract JSON part
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    return result
                except json.JSONDecodeError:
                    pass
            
            # If still fails, return parsed text structure
            return {
                "mood": "市场情绪复杂",
                "summary": response_text[:200] + "..." if len(response_text) > 200 else response_text,
                "keyPoints": ["AI分析正在优化中", "请稍后重试获取详细分析"],
                "outlook": "建议关注后续市场动态",
                "riskLevel": "medium",
                "riskNote": "请注意控制风险，理性投资"
            }

    def _build_stock_analysis_prompt(self, stock_code: str, stock_data: Dict[str, Any]) -> str:
        """Build stock analysis prompt"""
        
        # Extract key data
        latest_data = stock_data.get('latest_record', {})
        historical_data = stock_data.get('historical_records', [])
        
        # Calculate some basic indicators
        current_price = latest_data.get('close', 0)
        volume = latest_data.get('volume', 0)
        
        # Build historical price trend
        price_trend = []
        if historical_data:
            for record in historical_data[:10]:  # Last 10 days
                volume_price = record.get('volume_price', {})
                money_flow = record.get('money_flow', {})
                price_trend.append({
                    'date': record.get('trade_date', ''),
                    'close': volume_price.get('close', 0),
                    'volume': volume_price.get('volume', 0),
                # Money flow related fields (if available)
                # Small order: <50k Medium order: 50k~200k Large order: 200k~1m Super large order: >=1m, data based on active buy/sell orders
                'net_mf_amount': money_flow.get('net_mf_amount') if money_flow else None,
                'buy_sm_amount': money_flow.get('buy_sm_amount') if money_flow else None,
                'sell_sm_amount': money_flow.get('sell_sm_amount') if money_flow else None,
                'buy_md_amount': money_flow.get('buy_md_amount') if money_flow else None,
                'sell_md_amount': money_flow.get('sell_md_amount') if money_flow else None,
                'buy_lg_amount': money_flow.get('buy_lg_amount') if money_flow else None,
                'sell_lg_amount': money_flow.get('sell_lg_amount') if money_flow else None,
                'buy_elg_amount': money_flow.get('buy_elg_amount') if money_flow else None,
                'sell_elg_amount': money_flow.get('sell_elg_amount') if money_flow else None,
                })
        
        prompt = f"""
你是一位专业的股票分析师，请分析以下股票数据并给出投资建议。

股票代码: {stock_code}
当前价格: {current_price}
当前成交量: {volume}

最近10日价格趋势:
{json.dumps(price_trend, ensure_ascii=False, indent=2)}

请从以下几个方面进行分析:
1. 技术面分析 (价格走势、成交量变化、技术指标)
2. 长期走势预测 (1-3月)
3. 中期走势预测 (1-3周)
4. 短期走势预测 (1-3个交易日)
5. 风险评估 (高/中/低风险)
6. 投资建议 (买入/持有/卖出)
7. 关键支撑位和阻力位

请以JSON格式返回分析结果，包含以下字段:
{{
    "technical_analysis": "技术面分析内容",
    "long_term_forecast": "长期走势预测",
    "mid_term_forecast": "中期走势预测",
    "short_term_forecast": "短期走势预测",
    "risk_level": "风险等级(high/medium/low)",
    "investment_advice": "投资建议(buy/hold/sell)",
    "support_level": 支撑位价格,
    "resistance_level": 阻力位价格,
    "confidence_score": 置信度(0-100),
    "key_points": ["关键要点1", "关键要点2", "关键要点3"]
}}

请确保返回的是有效的JSON格式。
"""
        print(prompt)
        return prompt
    
    async def _call_llm_api(self, prompt: str) -> str:
        """Call LLM API"""
        print(f"[LLM API] 开始调用 - Provider: {self.provider}, Model: {self.model}")
        print(f"[LLM API] 请求URL: {self.base_url}/chat/completions")
        print(f"[LLM API] Prompt长度: {len(prompt)} 字符")
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一位专业的股票分析师，具有丰富的市场分析经验。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.3,
            "max_tokens": 2000
        }
        
        print(f"[LLM API] 请求参数: model={self.model}, temperature=0.3, max_tokens=2000")
        
        async with httpx.AsyncClient(timeout=60.0) as client:  # Increase timeout to 60 seconds
            print(f"[LLM API] 发送请求...")
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            print(f"[LLM API] 收到响应 - 状态码: {response.status_code}")
            
            if response.status_code != 200:
                error_msg = f"API调用失败: {response.status_code} - {response.text}"
                print(f"[LLM API] 错误: {error_msg}")
                raise Exception(error_msg)
            
            result = response.json()
            
            if 'choices' not in result or not result['choices']:
                error_msg = "API返回格式错误"
                print(f"[LLM API] 错误: {error_msg}")
                print(f"[LLM API] 原始响应: {result}")
                raise Exception(error_msg)
            
            content = result['choices'][0]['message']['content']
            print(f"[LLM API] 响应成功 - 内容长度: {len(content)} 字符")
            print(f"[LLM API] 响应预览: {content[:200]}...")
            
            return content
    
    def _parse_analysis_result(self, response_text: str) -> Dict[str, Any]:
        """Parse LLM returned analysis result"""
        try:
            # Try to parse JSON directly
            return json.loads(response_text)
        except json.JSONDecodeError:
            # If not valid JSON, try to extract JSON part
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
            
            # If still fails, return raw text
            return {
                "technical_analysis": response_text,
                "short_term_forecast": "无法解析",
                "risk_level": "medium",
                "investment_advice": "hold",
                "support_level": 0,
                "resistance_level": 0,
                "confidence_score": 50,
                "key_points": ["分析结果格式异常"]
            }

    async def analyze_portfolio_opportunities(
        self, 
        watchlist: List[Dict[str, Any]], 
        positions: List[Dict[str, Any]], 
        ladder_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        分析自选股和持仓的投资机会
        
        Args:
            watchlist: 自选股列表，每项包含 symbol, name, industry, change_pct 等
            positions: 持仓列表，每项包含 symbol, name, industry, cost_price, current_price 等
            ladder_data: 当日连板天梯数据
            
        Returns:
            包含补涨机会和高抛低吸建议的分析结果
        """
        print(f"[组合机会分析] 开始分析 - 自选股: {len(watchlist)}只, 持仓: {len(positions)}只")
        
        # Build analysis prompt
        prompt = self._build_portfolio_analysis_prompt(watchlist, positions, ladder_data)
        print(f"[组合机会分析] 提示词构建完成，长度: {len(prompt)} 字符")
        
        try:
            # Call LLM API
            response = await self._call_llm_api(prompt)
            
            # Parse response
            analysis_result = self._parse_portfolio_analysis_result(response)
            
            print(f"[组合机会分析] 分析完成")
            
            return {
                "success": True,
                "analysis": analysis_result,
                "provider": self.provider,
                "model": self.model,
                "raw_response": response[:500] + "..." if len(response) > 500 else response
            }
        except Exception as e:
            error_msg = str(e) if str(e) else f"Unknown error: {type(e).__name__}"
            print(f"组合机会分析失败, 错误: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": type(e).__name__,
                "provider": self.provider,
                "model": self.model
            }
    
    def _build_portfolio_analysis_prompt(
        self, 
        watchlist: List[Dict[str, Any]], 
        positions: List[Dict[str, Any]], 
        ladder_data: Dict[str, Any]
    ) -> str:
        """构建组合机会分析提示词"""
        from datetime import datetime
        current_date = datetime.now().strftime('%Y年%m月%d日')
        
        prompt_parts = [
            f"当前时间：{current_date}\n",
            "你是一位资深的短线策略分析师，请基于今日连板天梯数据，分析用户的自选股和持仓。\n",
        ]
        
        # 连板天梯数据部分
        if ladder_data:
            prompt_parts.append("## 今日连板天梯\n")
            prompt_parts.append(f"- 情绪得分: {ladder_data.get('sentiment_score', 'N/A')} ({ladder_data.get('sentiment_level', '')})")
            
            mainlines = ladder_data.get('mainlines', [])
            if mainlines:
                prompt_parts.append(f"- 主线板块: {', '.join(mainlines)}")
            
            prompt_parts.append(f"- 最高连板: {ladder_data.get('max_streak', 'N/A')}板")
            
            highest = ladder_data.get('highest_stock')
            if highest and highest.get('name'):
                prompt_parts.append(f"  (龙头: {highest.get('name')} {highest.get('streak')}连板)")
            
            prompt_parts.append(f"- 晋级率: {ladder_data.get('promotion_rate', 0)*100:.1f}%")
            prompt_parts.append("")
        
        # 自选股部分
        if watchlist:
            prompt_parts.append("## 用户自选股\n")
            prompt_parts.append("| 代码 | 名称 | 行业 | 今日涨幅 |")
            prompt_parts.append("|------|------|------|----------|")
            for stock in watchlist[:20]:  # 限制最多20只
                symbol = stock.get('symbol', '')
                name = stock.get('name', '')
                industry = stock.get('industry', '未知')
                change = stock.get('change_pct', 0)
                prompt_parts.append(f"| {symbol} | {name} | {industry} | {change:+.2f}% |")
            prompt_parts.append("")
        
        # 持仓部分
        if positions:
            prompt_parts.append("## 用户持仓\n")
            prompt_parts.append("| 代码 | 名称 | 行业 | 成本价 | 现价 | 盈亏% |")
            prompt_parts.append("|------|------|------|--------|------|-------|")
            for pos in positions[:20]:  # 限制最多20只
                symbol = pos.get('symbol', '')
                name = pos.get('stock_name', pos.get('name', ''))
                industry = pos.get('industry', '未知')
                cost = pos.get('cost_price', 0)
                current = pos.get('current_price', 0)
                pnl_pct = pos.get('unrealized_pnl_pct', 0)
                prompt_parts.append(f"| {symbol} | {name} | {industry} | {cost:.2f} | {current:.2f} | {pnl_pct:+.2f}% |")
            prompt_parts.append("")
        
        prompt_parts.extend([
            "请分析：",
            "1. **补涨机会**: 自选股/持仓中哪些与今日主线板块关联（通过行业语义匹配），是否存在补涨空间",
            "2. **高抛低吸**: 持仓股票中是否有适合高抛或低吸的机会（基于涨幅、盈亏、技术位置）",
            "",
            "请以JSON格式返回分析结果:",
            "{",
            '  "catchup_opportunities": [',
            '    {',
            '      "symbol": "股票代码",',
            '      "name": "股票名称",',
            '      "industry": "所属行业",',
            '      "related_mainline": "关联的主线板块",',
            '      "catchup_score": 0-100的补涨评分,',
            '      "analysis": "分析理由(30-50字)",',
            '      "risk": "风险提示(20-30字)"',
            '    }',
            '  ],',
            '  "swing_trading": [',
            '    {',
            '      "symbol": "股票代码",',
            '      "name": "股票名称",',
            '      "action": "高抛/低吸/持有观望",',
            '      "reason": "建议理由(30-50字)",',
            '      "target_price": 目标价位(可选),',
            '      "stop_loss": 止损价位(可选)',
            '    }',
            '  ],',
            '  "summary": "整体分析总结(50-100字)"',
            "}",
            "",
            "注意：",
            "- 行业与板块的关联需要语义判断，如'通信设备'可能关联'算力'板块",
            "- 补涨评分越高表示机会越大",
            "- 如果没有明显机会，相应数组可以为空"
        ])
        
        return "\n".join(prompt_parts)
    
    def _parse_portfolio_analysis_result(self, response_text: str) -> Dict[str, Any]:
        """解析组合机会分析结果"""
        try:
            result = json.loads(response_text)
            return result
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    return result
                except json.JSONDecodeError:
                    pass
            
            return {
                "catchup_opportunities": [],
                "swing_trading": [],
                "summary": "分析结果解析失败，请稍后重试"
            }


class LLMServiceManager:
    """LLM service manager"""
    
    def __init__(self):
        self.services = {}
    
    def get_service(self, provider: str = None, model: str = None, api_key: str = None, base_url: str = None) -> LLMService:
        """Get or create LLM service instance"""
        # Create service instance with user-specific configuration
        # If API key or base URL is provided, create a temporary service instance
        if api_key or base_url:
            return LLMService(provider, model, api_key, base_url)
        else:
            # Use cached service instance (based on provider and model)
            key = f"{provider}_{model}"
            if key not in self.services:
                self.services[key] = LLMService(provider, model)
            return self.services[key]
    
    def list_available_providers(self) -> List[str]:
        """List available service providers"""
        providers = []
        if os.getenv('OPENAI_API_KEY'):
            providers.append('openai')
        if os.getenv('DEEPSEEK_API_KEY'):
            providers.append('deepseek')
        if os.getenv('CUSTOM_LLM_API_KEY'):
            providers.append('custom')
        return providers


# Global service manager instance
llm_manager = LLMServiceManager()
