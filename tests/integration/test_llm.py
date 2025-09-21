#!/usr/bin/env python3
"""
测试LLM服务和API端点
"""

import os
import sys
sys.path.append('/home/shuyolin/workbench/own/quantFinance')

from llm_service import llm_manager
import asyncio
import json
import requests
import time
from dotenv import load_dotenv

load_dotenv()

async def test_llm_providers():
    """测试LLM服务提供商"""
    print("🧪 测试LLM服务提供商")
    print("-" * 40)
    
    try:
        providers = llm_manager.list_available_providers()
        print(f"可用服务商: {providers}")
        
        if not providers:
            print("❌ 没有可用的LLM服务商")
            print("\n请检查环境变量配置:")
            print("  OPENAI_API_KEY - OpenAI API密钥")
            print("  DEEPSEEK_API_KEY - DeepSeek API密钥")
            print("  CUSTOM_LLM_API_KEY - 自定义API密钥")
            return False
        
        # 测试创建服务实例
        for provider in providers:
            try:
                service = llm_manager.get_service(provider)
                print(f"✅ {provider} 服务创建成功")
            except Exception as e:
                print(f"❌ {provider} 服务创建失败: {e}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False

async def test_stock_analysis():
    """测试股票分析功能"""
    print("\n🧪 测试股票分析功能")
    print("-" * 40)
    
    try:
        providers = llm_manager.list_available_providers()
        if not providers:
            print("❌ 没有可用的LLM服务商，跳过分析测试")
            return
        
        # 模拟股票数据
        mock_stock_data = {
            "symbol": "000001",
            "latest_record": {
                "trade_date": "2025-08-14",
                "close": 12.2,
                "volume": 1234567,
                "open": 12.1,
                "high": 12.3,
                "low": 12.0
            },
            "historical_records": [
                {"trade_date": "2025-08-14", "close": 12.2, "volume": 1234567},
                {"trade_date": "2025-08-13", "close": 12.26, "volume": 1234567},
                {"trade_date": "2025-08-12", "close": 12.33, "volume": 1234567},
            ]
        }
        
        # 使用第一个可用的服务商进行测试
        provider = providers[0]
        print(f"使用 {provider} 进行测试分析...")
        
        service = llm_manager.get_service(provider)
        
        # 注意：这里只是测试服务创建，不实际调用API（避免消耗token）
        print(f"✅ {provider} 服务准备就绪，可以进行股票分析")
        print("💡 实际分析需要有效的API密钥")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")

def check_env_file():
    """检查环境变量文件"""
    print("\n🧪 检查环境变量配置")
    print("-" * 40)
    
    env_files = ['.env', '.env.example', '.env.template']
    
    for env_file in env_files:
        env_path = f'/home/shuyolin/workbench/own/quantFinance/{env_file}'
        if os.path.exists(env_path):
            print(f"✅ 找到 {env_file}")
        else:
            print(f"❌ 未找到 {env_file}")
    
    # 检查关键环境变量
    env_vars = ['OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'DEFAULT_LLM_PROVIDER']
    
    print("\n环境变量状态:")
    for var in env_vars:
        value = os.getenv(var)
        if value:
            # 隐藏API密钥的大部分内容
            if 'API_KEY' in var and len(value) > 8:
                display_value = value[:4] + '*' * 8 + value[-4:]
            else:
                display_value = value
            print(f"  {var}: {display_value}")
        else:
            print(f"  {var}: 未设置")

async def main():
    """主测试函数"""
    print("🧪 LLM服务测试套件")
    print("=" * 60)
    
    check_env_file()
    providers_ok = await test_llm_providers()
    
    if providers_ok:
        await test_stock_analysis()
    
    print("\n" + "=" * 60)
    print("✅ 测试完成")
    
    if not providers_ok:
        print("\n💡 配置说明:")
        print("1. 复制 .env.template 为 .env")
        print("2. 在 .env 文件中填入真实的API密钥")
        print("3. 重新启动FastAPI服务器")

API_URL = "http://10.70.50.68:8080/v1/chat/completions"
API_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6ImM3MjhjMWI1LWNlMDQtNGFjMS05MDA2LWNkODFlYjFlYmFjMCJ9.0eL_ukYP4flldwj5OhKFCi2s1ErXFQ1lNOjhIhoQ9NA"
payload = {
    "model": "qwen3-30b",
    "messages": [
        {"role": "user", "content": "你好，请简单介绍一下A股市场。"}
    ],
    "temperature": 0.3,
    "max_tokens": 2000
}
headers = {
    "Content-Type": "application/json"
}
if API_TOKEN:
    headers["Authorization"] = f"Bearer {API_TOKEN}"

def test_llm_api():
    print("开始测试 LLM 服务...")
    start = time.time()
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        elapsed = time.time() - start
        print(f"响应状态码: {response.status_code}")
        print(f"耗时: {elapsed:.2f} 秒")
        print("响应内容:")
        print(response.text)
    except Exception as e:
        print(f"请求失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())
    test_llm_api()
