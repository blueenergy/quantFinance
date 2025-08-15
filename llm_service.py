"""
LLM API服务类
支持OpenAI、DeepSeek等兼容OpenAI接口的外部LLM API服务
"""

import os
import json
import httpx
from typing import Dict, List, Optional, Any
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class LLMService:
    def __init__(self, provider: str = None, model: str = None):
        """
        初始化LLM服务
        
        Args:
            provider: LLM服务提供商 (openai, deepseek, custom)
            model: 使用的模型名称
        """
        self.provider = provider or os.getenv('DEFAULT_LLM_PROVIDER', 'openai')
        self.model = model or os.getenv('DEFAULT_MODEL', 'gpt-3.5-turbo')
        
        # 根据提供商设置API配置
        self._setup_api_config()
        
    def _setup_api_config(self):
        """根据提供商设置API配置"""
        if self.provider == 'openai':
            self.api_key = os.getenv('OPENAI_API_KEY')
            self.base_url = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
        elif self.provider == 'deepseek':
            self.api_key = os.getenv('DEEPSEEK_API_KEY')
            self.base_url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
            if not self.model or self.model == 'gpt-3.5-turbo':
                self.model = 'deepseek-chat'
        elif self.provider == 'custom':
            self.api_key = os.getenv('CUSTOM_LLM_API_KEY')
            self.base_url = os.getenv('CUSTOM_LLM_BASE_URL')
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")
            
        if not self.api_key:
            raise ValueError(f"API key not found for provider: {self.provider}")
        if not self.base_url:
            raise ValueError(f"Base URL not found for provider: {self.provider}")
    
    async def analyze_stock(self, stock_code: str, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析股票数据
        
        Args:
            stock_code: 股票代码
            stock_data: 股票数据
            
        Returns:
            分析结果
        """
        print(f"[股票分析] 开始分析股票: {stock_code}")
        
        # 构建分析提示词
        prompt = self._build_stock_analysis_prompt(stock_code, stock_data)
        print(f"[股票分析] 提示词构建完成，长度: {len(prompt)} 字符")
        
        try:
            # 调用LLM API
            response = await self._call_llm_api(prompt)
            
            # 解析返回结果
            analysis_result = self._parse_analysis_result(response)
            
            print(f"[股票分析] 分析完成 - 股票: {stock_code}")
            
            return {
                "success": True,
                "stock_code": stock_code,
                "analysis": analysis_result,
                "provider": self.provider,
                "model": self.model,
                "raw_response": response[:500] + "..." if len(response) > 500 else response  # 保留原始响应的前500字符用于调试
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
        分析市场数据
        
        Args:
            market_data: 市场数据（包含主要指数信息）
            
        Returns:
            市场分析结果
        """
        print(f"[市场分析] 开始分析市场数据")
        print(f"[市场分析] 包含 {len(market_data)} 个指数数据")
        
        # 构建市场分析提示词
        prompt = self._build_market_analysis_prompt(market_data)
        print(f"[市场分析] 提示词构建完成，长度: {len(prompt)} 字符")
        
        try:
            # 调用LLM API
            response = await self._call_llm_api(prompt)
            
            # 解析返回结果
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
        """构建市场分析提示词"""
        
        # 获取分析日期和市场数据
        analysis_date = market_data.get("analysis_date", "未知日期")
        
        # 处理不同的数据格式
        if "market_data" in market_data:
            # 新格式：有 market_data 嵌套字段
            data = market_data["market_data"]
        else:
            # 旧格式：直接包含指数数据
            data = {k: v for k, v in market_data.items() 
                   if k not in ["analysis_date", "data_source"] and isinstance(v, dict)}
        
        # 添加当前时间信息
        from datetime import datetime
        current_date = datetime.now().strftime('%Y年%m月%d日')
        
        prompt_parts = [
            f"当前时间：{current_date}\n",
            f"你是一位资深的证券分析师，请对{analysis_date}的A股市场表现进行全面分析。\n",
            f"以下是{analysis_date}的主要指数数据：\n"
        ]
        
        # 添加各个指数的数据
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
            
            # 计算当日涨跌幅
            open_price = latest.get('open', 0)
            close_price = latest.get('close', 0)
            if open_price > 0:
                daily_change = ((close_price - open_price) / open_price * 100)
                prompt_parts.append(f"当日涨跌幅: {daily_change:.2f}%")
            
            prompt_parts.append(f"成交量: {latest.get('volume', 0):,}")
            
            # 计算较前一交易日涨跌幅
            if len(historical) >= 2:
                prev_close = historical[1].get('close', close_price)
                if prev_close > 0:
                    change_pct = ((close_price - prev_close) / prev_close * 100)
                    prompt_parts.append(f"较前一交易日: {change_pct:+.2f}%")
            
            prompt_parts.append("")
        
        prompt_parts.extend([
            f"请基于{analysis_date}的市场数据，从以下几个维度进行分析:",
            "1. 市场整体走势分析",
            "2. 各板块表现对比", 
            "3. 成交量变化分析",
            "4. 市场情绪判断",
            "5. 短期市场展望（1-3个交易日）",
            "6. 风险提示与投资建议",
            "",
            "请以JSON格式返回分析结果，包含以下字段:",
            "{",
            '  "mood": "市场情绪(如:谨慎乐观/积极向好/谨慎观望)",',
            '  "summary": "市场概览(100-200字)",',
            '  "keyPoints": ["关键要点1", "关键要点2", "关键要点3"],',
            '  "outlook": "市场展望(100-150字)",',
            '  "riskLevel": "风险等级(low/medium/high)",',
            '  "riskNote": "风险提示(50-100字)"',
            "}",
            "",
            f"请确保分析客观专业，明确基于{analysis_date}的市场表现。"
        ])
        
        return "\n".join(prompt_parts)
    
    def _parse_market_analysis_result(self, response_text: str) -> Dict[str, Any]:
        """解析市场分析结果"""
        try:
            # 尝试直接解析JSON
            result = json.loads(response_text)
            return result
        except json.JSONDecodeError:
            # 如果不是有效JSON，尝试提取JSON部分
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    return result
                except json.JSONDecodeError:
                    pass
            
            # 如果仍然失败，返回解析后的文本结构
            return {
                "mood": "市场情绪复杂",
                "summary": response_text[:200] + "..." if len(response_text) > 200 else response_text,
                "keyPoints": ["AI分析正在优化中", "请稍后重试获取详细分析"],
                "outlook": "建议关注后续市场动态",
                "riskLevel": "medium",
                "riskNote": "请注意控制风险，理性投资"
            }

    def _build_stock_analysis_prompt(self, stock_code: str, stock_data: Dict[str, Any]) -> str:
        """构建股票分析提示词"""
        
        # 提取关键数据
        latest_data = stock_data.get('latest_record', {})
        historical_data = stock_data.get('historical_records', [])
        
        # 计算一些基本指标
        current_price = latest_data.get('close', 0)
        volume = latest_data.get('volume', 0)
        
        # 构建历史价格趋势
        price_trend = []
        if historical_data:
            for record in historical_data[-10:]:  # 最近10天
                price_trend.append({
                    'date': record.get('date', ''),
                    'close': record.get('close', 0),
                    'volume': record.get('volume', 0)
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
2. 短期走势预测 (1-3个交易日)
3. 风险评估 (高/中/低风险)
4. 投资建议 (买入/持有/卖出)
5. 关键支撑位和阻力位

请以JSON格式返回分析结果，包含以下字段:
{{
    "technical_analysis": "技术面分析内容",
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
        return prompt
    
    async def _call_llm_api(self, prompt: str) -> str:
        """调用LLM API"""
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
        
        async with httpx.AsyncClient(timeout=60.0) as client:  # 增加超时到60秒
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
        """解析LLM返回的分析结果"""
        try:
            # 尝试直接解析JSON
            return json.loads(response_text)
        except json.JSONDecodeError:
            # 如果不是有效JSON，尝试提取JSON部分
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
            
            # 如果仍然失败，返回原始文本
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


class LLMServiceManager:
    """LLM服务管理器"""
    
    def __init__(self):
        self.services = {}
    
    def get_service(self, provider: str = None, model: str = None) -> LLMService:
        """获取或创建LLM服务实例"""
        key = f"{provider}_{model}"
        if key not in self.services:
            self.services[key] = LLMService(provider, model)
        return self.services[key]
    
    def list_available_providers(self) -> List[str]:
        """列出可用的服务提供商"""
        providers = []
        if os.getenv('OPENAI_API_KEY'):
            providers.append('openai')
        if os.getenv('DEEPSEEK_API_KEY'):
            providers.append('deepseek')
        if os.getenv('CUSTOM_LLM_API_KEY'):
            providers.append('custom')
        return providers


# 全局服务管理器实例
llm_manager = LLMServiceManager()
