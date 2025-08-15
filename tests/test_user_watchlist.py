#!/usr/bin/env python3
"""
测试用户自选股功能
"""

import requests
import json

API_BASE = "http://localhost:3001"

def test_user_watchlist():
    """测试用户自选股功能"""
    print("🧪 测试用户自选股功能")
    print("=" * 50)
    
    # 1. 测试用户注册
    print("\n1️⃣ 测试用户注册...")
    register_data = {
        "username": "testuser_watchlist",
        "email": "testuser_watchlist@example.com",
        "password": "testpass123",
        "full_name": "Test User Watchlist"
    }
    
    try:
        response = requests.post(f"{API_BASE}/api/register", json=register_data)
        if response.status_code == 200:
            print("   ✅ 用户注册成功")
        else:
            print(f"   ⚠️ 用户可能已存在: {response.json()}")
    except Exception as e:
        print(f"   ❌ 注册失败: {e}")
    
    # 2. 测试用户登录
    print("\n2️⃣ 测试用户登录...")
    login_data = {
        "username": "testuser_watchlist",
        "password": "testpass123"
    }
    
    response = requests.post(f"{API_BASE}/api/login", json=login_data)
    if response.status_code != 200:
        print(f"   ❌ 登录失败: {response.json()}")
        return
    
    login_result = response.json()
    token = login_result["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print(f"   ✅ 登录成功，获得token")
    
    # 3. 测试获取空的自选股列表
    print("\n3️⃣ 测试获取空的自选股列表...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    if response.status_code == 200:
        result = response.json()
        print(f"   ✅ 获取成功: {result}")
    else:
        print(f"   ❌ 获取失败: {response.json()}")
        return
    
    # 4. 测试添加股票到自选股
    print("\n4️⃣ 测试添加股票到自选股...")
    test_symbols = ["000001", "000002", "300347"]
    
    for symbol in test_symbols:
        response = requests.post(f"{API_BASE}/api/user/watchlist/add", 
                               json={"symbol": symbol}, headers=headers)
        if response.status_code == 200:
            print(f"   ✅ 添加 {symbol} 成功")
        else:
            print(f"   ❌ 添加 {symbol} 失败: {response.json()}")
    
    # 5. 测试获取自选股列表
    print("\n5️⃣ 测试获取自选股列表...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    if response.status_code == 200:
        result = response.json()
        print(f"   ✅ 获取成功: {result}")
        symbols = result.get("data", {}).get("symbols", [])
        print(f"   自选股列表: {symbols}")
    else:
        print(f"   ❌ 获取失败: {response.json()}")
        return
    
    # 6. 测试获取自选股详细信息
    print("\n6️⃣ 测试获取自选股详细信息...")
    response = requests.get(f"{API_BASE}/api/user/watchlist-stocks", headers=headers)
    if response.status_code == 200:
        result = response.json()
        print(f"   ✅ 获取成功，共 {len(result.get('data', []))} 只股票")
        for stock in result.get("data", [])[:3]:  # 只显示前3只
            print(f"      {stock['symbol']} - {stock['name']}: {stock['close']}")
    else:
        print(f"   ❌ 获取失败: {response.json()}")
    
    # 7. 测试移除股票
    print("\n7️⃣ 测试移除股票...")
    remove_symbol = "000002"
    response = requests.delete(f"{API_BASE}/api/user/watchlist/remove", 
                              json={"symbol": remove_symbol}, headers=headers)
    if response.status_code == 200:
        print(f"   ✅ 移除 {remove_symbol} 成功")
    else:
        print(f"   ❌ 移除 {remove_symbol} 失败: {response.json()}")
    
    # 8. 验证移除结果
    print("\n8️⃣ 验证移除结果...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    if response.status_code == 200:
        result = response.json()
        symbols = result.get("data", {}).get("symbols", [])
        print(f"   当前自选股: {symbols}")
        if remove_symbol not in symbols:
            print("   ✅ 移除验证成功")
        else:
            print("   ❌ 移除验证失败")
    
    # 9. 测试批量更新
    print("\n9️⃣ 测试批量更新...")
    new_symbols = ["000001", "600000", "300347"]
    response = requests.put(f"{API_BASE}/api/user/watchlist", 
                           json={"symbols": new_symbols}, headers=headers)
    if response.status_code == 200:
        print(f"   ✅ 批量更新成功")
        result = response.json()
        print(f"   结果: {result}")
    else:
        print(f"   ❌ 批量更新失败: {response.json()}")
    
    print(f"\n✅ 用户自选股功能测试完成！")

if __name__ == "__main__":
    test_user_watchlist()
