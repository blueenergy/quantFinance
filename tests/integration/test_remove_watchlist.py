#!/usr/bin/env python3
"""
测试自选股移除功能
"""

import requests
import json

API_BASE = "http://localhost:3001"

def test_remove_functionality():
    """专门测试移除功能"""
    print("🧪 测试自选股移除功能")
    print("=" * 40)
    
    # 1. 登录获取token
    print("1️⃣ 用户登录...")
    login_data = {
        "username": "testuser_watchlist",
        "password": "testpass123"
    }
    
    response = requests.post(f"{API_BASE}/api/login", json=login_data)
    if response.status_code != 200:
        print(f"   ❌ 登录失败: {response.json()}")
        return
    
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print(f"   ✅ 登录成功")
    
    # 2. 添加测试股票
    print("\n2️⃣ 添加测试股票...")
    test_symbols = ["000001", "000002", "300347"]
    
    for symbol in test_symbols:
        response = requests.post(f"{API_BASE}/api/user/watchlist/add", 
                               json={"symbol": symbol}, headers=headers)
        if response.status_code == 200:
            print(f"   ✅ 添加 {symbol} 成功")
        else:
            print(f"   ⚠️ 添加 {symbol}: {response.json().get('message', '已存在')}")
    
    # 3. 查看当前自选股
    print("\n3️⃣ 查看当前自选股...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    if response.status_code == 200:
        current_symbols = response.json()["data"]["symbols"]
        print(f"   当前自选股: {current_symbols}")
    else:
        print(f"   ❌ 获取失败")
        return
    
    # 4. 测试移除功能
    print("\n4️⃣ 测试移除股票...")
    remove_symbol = "000002"
    
    # 使用新的API路径
    print(f"   尝试移除 {remove_symbol}...")
    response = requests.delete(f"{API_BASE}/api/user/watchlist/remove/{remove_symbol}", 
                              headers=headers)
    
    print(f"   响应状态码: {response.status_code}")
    print(f"   响应内容: {response.json()}")
    
    if response.status_code == 200:
        print(f"   ✅ 移除成功")
    else:
        print(f"   ❌ 移除失败")
    
    # 5. 验证移除结果
    print("\n5️⃣ 验证移除结果...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    if response.status_code == 200:
        updated_symbols = response.json()["data"]["symbols"]
        print(f"   更新后自选股: {updated_symbols}")
        
        if remove_symbol not in updated_symbols:
            print(f"   ✅ {remove_symbol} 已成功移除")
        else:
            print(f"   ❌ {remove_symbol} 仍在列表中")
    else:
        print(f"   ❌ 获取失败")

if __name__ == "__main__":
    test_remove_functionality()
