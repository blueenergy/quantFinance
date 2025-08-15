#!/usr/bin/env python3
"""
用户自选股功能完整演示
展示整个用户自选股系统的工作流程
"""

import requests
import json
import time

API_BASE = "http://localhost:3001"

def demo_complete_workflow():
    """演示完整的用户自选股工作流程"""
    print("🎭 用户自选股功能完整演示")
    print("=" * 60)
    
    print("\n🌟 功能特点：")
    print("   ✅ 用户自选股与账户绑定")
    print("   ✅ 不同用户看到不同的自选股列表")
    print("   ✅ 登录时自动迁移本地数据到服务器")
    print("   ✅ 支持添加/删除/批量更新操作")
    print("   ✅ 实时获取股票价格和名称信息")
    
    # 创建两个测试用户来演示多用户隔离
    users = [
        {
            "username": "investor_alice",
            "email": "alice@investor.com",
            "password": "alice123",
            "full_name": "Alice Wang",
            "stocks": ["000001", "000002"]  # Alice关注银行股
        },
        {
            "username": "investor_bob", 
            "email": "bob@investor.com",
            "password": "bob123",
            "full_name": "Bob Li",
            "stocks": ["300347", "600000"]  # Bob关注医药和银行
        }
    ]
    
    for i, user_data in enumerate(users, 1):
        print(f"\n{'='*20} 用户 {i}: {user_data['full_name']} {'='*20}")
        
        # 1. 注册用户
        print(f"\n1️⃣ 注册用户 {user_data['username']}...")
        register_response = requests.post(f"{API_BASE}/api/register", json={
            "username": user_data["username"],
            "email": user_data["email"], 
            "password": user_data["password"],
            "full_name": user_data["full_name"]
        })
        
        if register_response.status_code == 200:
            print(f"   ✅ 用户 {user_data['username']} 注册成功")
        else:
            print(f"   ⚠️ 用户可能已存在，继续测试...")
        
        # 2. 用户登录
        print(f"\n2️⃣ 用户 {user_data['username']} 登录...")
        login_response = requests.post(f"{API_BASE}/api/login", json={
            "username": user_data["username"],
            "password": user_data["password"]
        })
        
        if login_response.status_code != 200:
            print(f"   ❌ 登录失败: {login_response.json()}")
            continue
            
        token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"   ✅ 登录成功")
        
        # 3. 添加自选股
        print(f"\n3️⃣ 为 {user_data['username']} 添加自选股...")
        for stock in user_data["stocks"]:
            response = requests.post(f"{API_BASE}/api/user/watchlist/add",
                                   json={"symbol": stock}, headers=headers)
            if response.status_code == 200:
                print(f"   ✅ 添加 {stock} 成功")
            else:
                print(f"   ⚠️ 添加 {stock} 失败或已存在")
        
        # 4. 查看自选股列表
        print(f"\n4️⃣ 查看 {user_data['username']} 的自选股列表...")
        response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
        if response.status_code == 200:
            watchlist = response.json()["data"]["symbols"]
            print(f"   📋 自选股列表: {watchlist}")
        else:
            print(f"   ❌ 获取失败")
            continue
        
        # 5. 获取详细股票信息
        print(f"\n5️⃣ 获取 {user_data['username']} 自选股的详细信息...")
        response = requests.get(f"{API_BASE}/api/user/watchlist-stocks", headers=headers)
        if response.status_code == 200:
            stocks_data = response.json()["data"]
            print(f"   📊 股票详情:")
            for stock in stocks_data:
                change_str = f"+{stock['change']}" if stock['change'] >= 0 else str(stock['change'])
                change_pct_str = f"+{stock['change_percent']:.2f}%" if stock['change_percent'] >= 0 else f"{stock['change_percent']:.2f}%"
                print(f"      {stock['symbol']} {stock['name']}: ¥{stock['close']} ({change_str}, {change_pct_str})")
        else:
            print(f"   ❌ 获取详情失败")
        
        time.sleep(1)  # 稍微延迟，让演示更清晰
    
    # 6. 展示用户隔离效果
    print(f"\n{'='*25} 用户隔离验证 {'='*25}")
    print("\n6️⃣ 验证不同用户看到不同的自选股...")
    
    for user_data in users:
        # 重新登录获取token
        login_response = requests.post(f"{API_BASE}/api/login", json={
            "username": user_data["username"],
            "password": user_data["password"]
        })
        
        if login_response.status_code == 200:
            token = login_response.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            
            response = requests.get(f"{API_BASE}/api/user/watchlist", headers=headers)
            if response.status_code == 200:
                watchlist = response.json()["data"]["symbols"]
                print(f"   👤 {user_data['username']}: {watchlist}")
            else:
                print(f"   ❌ 获取 {user_data['username']} 自选股失败")
    
    print(f"\n✨ 演示完成！")
    print(f"\n🎯 重要特性总结:")
    print(f"   🔐 每个用户都有独立的自选股列表")
    print(f"   💾 数据安全保存在服务器端MongoDB数据库")
    print(f"   🔄 支持实时添加、删除、批量更新操作")
    print(f"   📈 自动获取股票名称和最新价格信息")
    print(f"   🌐 前端页面有友好的状态提示和用户体验")
    
    print(f"\n🚀 现在你可以：")
    print(f"   1. 访问 http://localhost:5173 体验前端界面")
    print(f"   2. 注册/登录不同用户账户测试")
    print(f"   3. 在自选股管理页面添加你关注的股票")
    print(f"   4. 享受个性化的股票投资追踪服务！")

if __name__ == "__main__":
    demo_complete_workflow()
