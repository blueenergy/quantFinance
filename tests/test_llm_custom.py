#!/usr/bin/env python3
"""
测试qwen3-30b模型调用
"""

import os
import sys
import asyncio
import httpx
import json
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

async def test_qwen_model():
    """测试qwen-plus模型"""
    print("🧪 测试qwen-plus模型")
    print("-" * 40)
    
    api_key = os.getenv('CUSTOM_LLM_API_KEY')
    base_url = os.getenv('CUSTOM_LLM_BASE_URL', 'https://api.openai.com/v1')
    
    print(f"API Base URL: {base_url}")
    print(f"API Key: {api_key[:10]}..." if api_key else "API Key: 未设置")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 简单的测试请求
    payload = {
        "model": "qwen-plus",
        "messages": [
            {
                "role": "system",
                "content": "你是一个股票分析师。"
            },
            {
                "role": "user", 
                "content": "请简单分析一下股票000001的情况。"
            }
        ],
        "temperature": 0.3,
        "max_tokens": 500
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            print("正在发送API请求...")
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            print(f"响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and result['choices']:
                    content = result['choices'][0]['message']['content']
                    print("✅ API调用成功!")
                    print(f"响应内容: {content[:200]}...")
                else:
                    print("❌ 响应格式异常")
                    print(f"完整响应: {result}")
            else:
                print(f"❌ API调用失败: {response.status_code}")
                print(f"错误内容: {response.text}")
                
    except Exception as e:
        print(f"❌ 请求异常: {e}")

async def test_model_list():
    """测试模型列表"""
    print("\n🧪 测试可用模型列表")
    print("-" * 40)
    
    api_key = os.getenv('CUSTOM_LLM_API_KEY')
    base_url = os.getenv('CUSTOM_LLM_BASE_URL', 'https://api.openai.com/v1')
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/models",
                headers=headers
            )
            
            if response.status_code == 200:
                models = response.json()
                print("✅ 可用模型列表:")
                if 'data' in models:
                    for model in models['data'][:10]:  # 只显示前10个
                        print(f"  - {model.get('id', 'unknown')}")
                else:
                    print(f"响应格式: {models}")
            else:
                print(f"❌ 获取模型列表失败: {response.status_code}")
                print(f"错误: {response.text}")
                
    except Exception as e:
        print(f"❌ 请求异常: {e}")

async def main():
    """主函数"""
    await test_model_list()
    await test_qwen_model()

if __name__ == "__main__":
    asyncio.run(main())
