#!/usr/bin/env python3
"""
测试自选股API的CORS功能
"""

import requests
import json

API_BASE_URL = "http://localhost:3001"

def test_options_requests():
    """测试OPTIONS预检请求"""
    
    endpoints = [
        "/api/user/watchlist",
        "/api/user/watchlist/add", 
        "/api/user/watchlist/remove/000001",
        "/api/user/watchlist-stocks"
    ]
    
    print("=== 测试OPTIONS预检请求 ===")
    
    for endpoint in endpoints:
        url = f"{API_BASE_URL}{endpoint}"
        print(f"\n测试 OPTIONS {endpoint}")
        
        try:
            response = requests.options(url, headers={
                'Origin': 'http://localhost:5173',
                'Access-Control-Request-Method': 'GET' if 'add' not in endpoint and 'remove' not in endpoint else ('POST' if 'add' in endpoint else 'DELETE'),
                'Access-Control-Request-Headers': 'authorization,content-type'
            })
            
            print(f"状态码: {response.status_code}")
            print(f"响应头: {dict(response.headers)}")
            
            if response.status_code == 200:
                print("✅ OPTIONS请求成功")
            else:
                print(f"❌ OPTIONS请求失败: {response.text}")
                
        except Exception as e:
            print(f"❌ 请求异常: {str(e)}")

def test_cors_headers():
    """测试CORS响应头"""
    print("\n=== 测试CORS响应头 ===")
    
    try:
        # 测试一个简单的GET请求
        response = requests.get(f"{API_BASE_URL}/api/user/watchlist", headers={
            'Origin': 'http://localhost:5173'
        })
        
        print(f"GET /api/user/watchlist 状态码: {response.status_code}")
        print("CORS相关响应头:")
        cors_headers = {k: v for k, v in response.headers.items() if 'access-control' in k.lower()}
        for header, value in cors_headers.items():
            print(f"  {header}: {value}")
            
    except Exception as e:
        print(f"❌ 请求异常: {str(e)}")

def main():
    print("测试自选股API的CORS功能")
    print("=" * 50)
    
    test_options_requests()
    test_cors_headers()
    
    print("\n" + "=" * 50)
    print("测试完成")

if __name__ == "__main__":
    main()
