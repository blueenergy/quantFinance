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
        # 构建分析提示词
        prompt = self._build_stock_analysis_prompt(stock_code, stock_data)
        
        try:
            # 调用LLM API
            response = await self._call_llm_api(prompt)
            
            # 解析返回结果
            analysis_result = self._parse_analysis_result(response)
            
            return {
                "success": True,
                "stock_code": stock_code,
                "analysis": analysis_result,
                "provider": self.provider,
                "model": self.model,
                "raw_response": response[:500] + "..." if len(response) > 500 else response  # 保留原始响应的前500字符用于调试
            }
        except Exception as e:
            print(f"股票分析失败 - 股票: {stock_code}, 错误: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "stock_code": stock_code,
                "provider": self.provider,
                "model": self.model
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
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200:
                raise Exception(f"API调用失败: {response.status_code} - {response.text}")
            
            result = response.json()
            
            if 'choices' not in result or not result['choices']:
                raise Exception("API返回格式错误")
            
            return result['choices'][0]['message']['content']
    
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
