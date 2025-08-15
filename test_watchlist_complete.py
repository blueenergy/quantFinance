#!/usr/bin/env python3
"""
测试完整的自选股功能流程
"""

import requests
import json

API_BASE_URL = "http://localhost:3001"

def login_user(username="testuser", password="testpass123"):
    """登录用户并获取token"""
    try:
        response = requests.post(f"{API_BASE_URL}/api/login", json={
            "username": username,
            "password": password
        })
        
        if response.status_code == 200:
            data = response.json()
            return data.get("access_token")
        else:
            print(f"登录失败: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"登录异常: {str(e)}")
        return None

def test_watchlist_operations():
    """测试自选股操作"""
    
    # 1. 登录获取token
    print("=== 步骤1: 用户登录 ===")
    token = login_user()
    if not token:
        print("❌ 登录失败，无法继续测试")
        return
    
    print("✅ 登录成功")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # 2. 获取当前自选股
    print("\n=== 步骤2: 获取当前自选股 ===")
    try:
        response = requests.get(f"{API_BASE_URL}/api/user/watchlist", headers=headers)
        print(f"状态码: {response.status_code}")
        if response.status_code == 200:
            current_watchlist = response.json()
            print(f"当前自选股: {current_watchlist}")
        else:
            print(f"获取自选股失败: {response.text}")
    except Exception as e:
        print(f"获取自选股异常: {str(e)}")
    
    # 3. 添加测试股票
    print("\n=== 步骤3: 添加测试股票 ===")
    test_symbols = ["000001", "000002", "600519"]
    
    for symbol in test_symbols:
        try:
            response = requests.post(f"{API_BASE_URL}/api/user/watchlist/add", 
                                   json={"symbol": symbol}, 
                                   headers=headers)
            print(f"添加 {symbol}: {response.status_code}")
            if response.status_code == 200:
                result = response.json()
                print(f"  ✅ {result.get('message', '添加成功')}")
            else:
                print(f"  ❌ 添加失败: {response.text}")
        except Exception as e:
            print(f"  ❌ 添加异常: {str(e)}")
    
    # 4. 再次获取自选股确认添加
    print("\n=== 步骤4: 确认添加结果 ===")
    try:
        response = requests.get(f"{API_BASE_URL}/api/user/watchlist", headers=headers)
        if response.status_code == 200:
            watchlist = response.json()
            symbols = watchlist.get('data', {}).get('symbols', [])
            print(f"当前自选股: {symbols}")
        else:
            print(f"获取自选股失败: {response.text}")
    except Exception as e:
        print(f"获取自选股异常: {str(e)}")
    
    # 5. 测试移除功能
    print("\n=== 步骤5: 测试移除功能 ===")
    remove_symbol = "000001"
    
    try:
        response = requests.delete(f"{API_BASE_URL}/api/user/watchlist/remove/{remove_symbol}", 
                                 headers=headers)
        print(f"移除 {remove_symbol}: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            print(f"  ✅ {result.get('message', '移除成功')}")
        else:
            print(f"  ❌ 移除失败: {response.text}")
    except Exception as e:
        print(f"  ❌ 移除异常: {str(e)}")
    
    # 6. 最终确认
    print("\n=== 步骤6: 最终确认 ===")
    try:
        response = requests.get(f"{API_BASE_URL}/api/user/watchlist", headers=headers)
        if response.status_code == 200:
            watchlist = response.json()
            symbols = watchlist.get('data', {}).get('symbols', [])
            print(f"最终自选股: {symbols}")
            
            if remove_symbol not in symbols:
                print(f"✅ 移除功能正常：{remove_symbol} 已从自选股中移除")
            else:
                print(f"❌ 移除功能异常：{remove_symbol} 仍在自选股中")
        else:
            print(f"获取自选股失败: {response.text}")
    except Exception as e:
        print(f"获取自选股异常: {str(e)}")

def main():
    print("测试完整的自选股功能流程")
    print("=" * 50)
    
    test_watchlist_operations()
    
    print("\n" + "=" * 50)
    print("测试完成")

if __name__ == "__main__":
    main()
