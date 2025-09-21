#!/usr/bin/env python3
"""
完整测试移除功能的前端和后端流程
"""

import requests
import time

API_BASE = "http://localhost:3001"

def test_complete_remove_flow():
    """测试完整的移除流程"""
    print("🔧 完整移除功能测试")
    print("=" * 40)
    
    # 1. 登录
    print("1️⃣ 用户登录...")
    response = requests.post(f"{API_BASE}/api/login", json={
        "username": "testuser_watchlist", 
        "password": "testpass123"
    })
    
    if response.status_code != 200:
        print("❌ 登录失败，请先运行 test_user_watchlist.py 创建用户")
        return
    
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("✅ 登录成功")
    
    # 2. 确保有股票可以移除
    print("\n2️⃣ 添加测试股票...")
    test_stocks = ["000001", "000002", "300347"]
    
    for stock in test_stocks:
        requests.post(f"{API_BASE}/api/user/watchlist/add", 
                     json={"symbol": stock}, headers=headers)
    
    # 查看当前列表
    response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    current_list = response.json()["data"]["symbols"]
    print(f"当前自选股: {current_list}")
    
    # 3. 测试移除各种股票
    print("\n3️⃣ 测试移除功能...")
    
    for stock_to_remove in ["000001", "000002"]:
        if stock_to_remove in current_list:
            print(f"\n移除股票: {stock_to_remove}")
            
            # 执行移除 - 使用新的API路径
            remove_response = requests.delete(f"{API_BASE}/api/user/watchlist/remove/{stock_to_remove}",
                                            headers=headers)
            
            print(f"移除响应: {remove_response.status_code}")
            print(f"移除结果: {remove_response.json()}")
            
            # 验证移除
            verify_response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
            updated_list = verify_response.json()["data"]["symbols"]
            
            if stock_to_remove not in updated_list:
                print(f"✅ {stock_to_remove} 移除成功")
            else:
                print(f"❌ {stock_to_remove} 移除失败")
            
            time.sleep(0.5)
    
    # 4. 最终状态
    print("\n4️⃣ 最终状态检查...")
    final_response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
    final_list = final_response.json()["data"]["symbols"]
    print(f"最终自选股列表: {final_list}")
    
    print("\n✅ 移除功能测试完成！")
    print("\n💡 如果前端移除仍有问题，请：")
    print("   1. 检查浏览器控制台错误信息")
    print("   2. 确认前端服务正在运行")
    print("   3. 检查网络请求是否成功")

if __name__ == "__main__":
    test_complete_remove_flow()
