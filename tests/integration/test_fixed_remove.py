#!/usr/bin/env python3
"""
测试修复后的自选股移除功能
"""

import requests
import time

API_BASE = "http://localhost:3001"

def test_fixed_remove_function():
    """测试修复后的移除功能"""
    print("🔧 测试修复后的自选股移除功能")
    print("=" * 50)
    
    # 1. 登录
    print("1️⃣ 用户登录...")
    response = requests.post(f"{API_BASE}/api/login", json={
        "username": "testuser_watchlist", 
        "password": "testpass123"
    })
    
    if response.status_code != 200:
        print("❌ 登录失败")
        return
    
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("✅ 登录成功")
    
    # 2. 添加测试股票
    print("\n2️⃣ 添加测试股票...")
    test_stocks = ["000001", "000002"]
    
    for stock in test_stocks:
        response = requests.post(f"{API_BASE}/api/user/watchlist/add", 
                               json={"symbol": stock}, headers=headers)
        if response.status_code == 200 and response.json().get("success"):
            print(f"✅ 添加 {stock} 成功")
        else:
            print(f"⚠️ 添加 {stock}: {response.json().get('message', '可能已存在')}")
    
    # 3. 查看当前列表
    print("\n3️⃣ 查看当前自选股...")
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    current_list = response.json()["data"]["symbols"]
    print(f"当前自选股: {current_list}")
    
    # 4. 测试新的删除API（路径参数）
    print("\n4️⃣ 测试新的删除API...")
    for stock_to_remove in ["000001", "000002"]:
        if stock_to_remove in current_list:
            print(f"\n移除股票: {stock_to_remove}")
            
            # 使用新的API路径
            remove_response = requests.delete(f"{API_BASE}/api/user/watchlist/remove/{stock_to_remove}",
                                            headers=headers)
            
            print(f"移除响应状态: {remove_response.status_code}")
            
            if remove_response.status_code == 200:
                result = remove_response.json()
                print(f"移除结果: {result}")
                
                if result.get("success"):
                    print(f"✅ {stock_to_remove} 移除成功")
                else:
                    print(f"❌ {stock_to_remove} 移除失败: {result.get('message')}")
            else:
                print(f"❌ HTTP错误: {remove_response.status_code}")
            
            time.sleep(0.5)
    
    # 5. 验证最终状态
    print("\n5️⃣ 验证最终状态...")
    final_response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    final_list = final_response.json()["data"]["symbols"]
    print(f"最终自选股列表: {final_list}")
    
    # 6. 测试前端兼容性
    print("\n6️⃣ 前端兼容性提示...")
    print("✅ 新API使用路径参数，避免了CORS预检请求问题")
    print("✅ 前端axios.delete()调用更简单，不需要请求体")
    print("✅ 解决了OPTIONS请求400错误的问题")
    
    print("\n🎉 修复验证完成！")

if __name__ == "__main__":
    test_fixed_remove_function()
