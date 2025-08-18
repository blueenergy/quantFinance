#!/usr/bin/env python3
"""
管理员功能测试脚本
测试所有管理员API端点
"""

import requests
import json
import time
from datetime import datetime

# 配置
BASE_URL = "http://localhost:3001"
TEST_ADMIN = {
    "username": "test_admin",
    "password": "test123",
    "email": "test_admin@quantfinance.com",
    "full_name": "测试管理员"
}

TEST_USER = {
    "username": "test_user",
    "password": "user123",
    "email": "test_user@quantfinance.com",
    "full_name": "测试用户"
}

class AdminTester:
    def __init__(self):
        self.admin_token = None
        self.user_token = None
        self.test_user_id = None
        
    def test_admin_init(self):
        """测试管理员初始化"""
        print("🔧 测试管理员初始化...")
        
        response = requests.post(f"{BASE_URL}/api/admin/init")
        if response.status_code == 200:
            print("✅ 管理员初始化成功")
            data = response.json()
            print(f"📝 {data.get('message', '')}")
            if 'credentials' in data:
                print(f"👤 用户名: {data['credentials']['username']}")
                print(f"🔑 密码: {data['credentials']['password']}")
        else:
            print(f"⚠️  管理员初始化响应: {response.status_code}")
            print(f"📄 响应内容: {response.text}")
    
    def create_test_users(self):
        """创建测试用户"""
        print("\n👥 创建测试用户...")
        
        # 创建测试管理员
        response = requests.post(f"{BASE_URL}/api/register", json=TEST_ADMIN)
        if response.status_code == 200:
            print("✅ 测试管理员创建成功")
            # 手动设置为管理员
            from auth import users_collection
            users_collection.update_one(
                {"username": TEST_ADMIN["username"]},
                {"$set": {"is_admin": True}}
            )
        
        # 创建测试普通用户
        response = requests.post(f"{BASE_URL}/api/register", json=TEST_USER)
        if response.status_code == 200:
            print("✅ 测试用户创建成功")
            self.test_user_id = response.json()["id"]
        
    def login_admin(self):
        """管理员登录"""
        print("\n🔑 管理员登录...")
        
        response = requests.post(f"{BASE_URL}/api/login", json={
            "username": TEST_ADMIN["username"],
            "password": TEST_ADMIN["password"]
        })
        
        if response.status_code == 200:
            data = response.json()
            self.admin_token = data["access_token"]
            print("✅ 管理员登录成功")
            print(f"👤 用户: {data['user']['username']}")
            print(f"🔐 管理员权限: {data['user']['is_admin']}")
        else:
            print(f"❌ 管理员登录失败: {response.status_code}")
            print(f"📄 {response.text}")
            
    def login_user(self):
        """普通用户登录"""
        print("\n🔑 普通用户登录...")
        
        response = requests.post(f"{BASE_URL}/api/login", json={
            "username": TEST_USER["username"],
            "password": TEST_USER["password"]
        })
        
        if response.status_code == 200:
            data = response.json()
            self.user_token = data["access_token"]
            print("✅ 普通用户登录成功")
        else:
            print(f"❌ 普通用户登录失败: {response.status_code}")
    
    def test_admin_statistics(self):
        """测试管理员统计"""
        print("\n📊 测试系统统计...")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/statistics", headers=headers)
        
        if response.status_code == 200:
            data = response.json()["data"]
            print("✅ 统计信息获取成功")
            print(f"👥 总用户数: {data['users']['total']}")
            print(f"🔐 管理员数: {data['users']['admin']}")
            print(f"🔑 总登录数: {data['logins']['total']}")
            print(f"✅ 成功登录: {data['logins']['successful']}")
        else:
            print(f"❌ 统计信息获取失败: {response.status_code}")
    
    def test_user_management(self):
        """测试用户管理"""
        print("\n👥 测试用户管理...")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        # 获取用户列表
        response = requests.get(f"{BASE_URL}/api/admin/users", headers=headers)
        if response.status_code == 200:
            data = response.json()["data"]
            print(f"✅ 用户列表获取成功: {len(data['users'])} 个用户")
            
            # 获取用户详情
            for user in data['users']:
                if user['username'] == TEST_USER['username']:
                    self.test_user_id = user['id']
                    break
            
            if self.test_user_id:
                response = requests.get(f"{BASE_URL}/api/admin/users/{self.test_user_id}", headers=headers)
                if response.status_code == 200:
                    print("✅ 用户详情获取成功")
                else:
                    print(f"❌ 用户详情获取失败: {response.status_code}")
        else:
            print(f"❌ 用户列表获取失败: {response.status_code}")
    
    def test_user_status_toggle(self):
        """测试用户状态切换"""
        print("\n🔄 测试用户状态切换...")
        
        if not self.test_user_id:
            print("❌ 无法测试：测试用户ID不存在")
            return
            
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        # 禁用用户
        response = requests.put(
            f"{BASE_URL}/api/admin/users/{self.test_user_id}/status",
            headers=headers,
            json={"is_active": False}
        )
        
        if response.status_code == 200:
            print("✅ 用户禁用成功")
            
            # 重新激活用户
            response = requests.put(
                f"{BASE_URL}/api/admin/users/{self.test_user_id}/status",
                headers=headers,
                json={"is_active": True}
            )
            
            if response.status_code == 200:
                print("✅ 用户激活成功")
            else:
                print(f"❌ 用户激活失败: {response.status_code}")
        else:
            print(f"❌ 用户禁用失败: {response.status_code}")
    
    def test_login_logs(self):
        """测试登录日志"""
        print("\n📋 测试登录日志...")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/login-logs", headers=headers)
        
        if response.status_code == 200:
            data = response.json()["data"]
            print(f"✅ 登录日志获取成功: {len(data['logs'])} 条记录")
            
            # 显示最近的几条日志
            for log in data['logs'][:3]:
                status = "成功" if log['success'] else "失败"
                print(f"📝 {log['username']} - {status} - {log['ip_address']}")
        else:
            print(f"❌ 登录日志获取失败: {response.status_code}")
    
    def test_admin_logs(self):
        """测试管理员操作日志"""
        print("\n⚙️ 测试管理员操作日志...")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.get(f"{BASE_URL}/api/admin/actions", headers=headers)
        
        if response.status_code == 200:
            data = response.json()["data"]
            print(f"✅ 管理员日志获取成功: {len(data['actions'])} 条记录")
        else:
            print(f"❌ 管理员日志获取失败: {response.status_code}")
    
    def test_permission_control(self):
        """测试权限控制"""
        print("\n🔒 测试权限控制...")
        
        # 普通用户尝试访问管理员API
        if self.user_token:
            headers = {"Authorization": f"Bearer {self.user_token}"}
            response = requests.get(f"{BASE_URL}/api/admin/statistics", headers=headers)
            
            if response.status_code == 403:
                print("✅ 权限控制正常：普通用户无法访问管理员API")
            else:
                print(f"❌ 权限控制异常: {response.status_code}")
        else:
            print("⚠️  无法测试权限控制：普通用户token不存在")
    
    def cleanup(self):
        """清理测试数据"""
        print("\n🧹 清理测试数据...")
        
        try:
            from auth import users_collection
            # 删除测试用户
            users_collection.delete_many({
                "username": {"$in": [TEST_ADMIN["username"], TEST_USER["username"]]}
            })
            print("✅ 测试用户清理完成")
        except Exception as e:
            print(f"⚠️  清理测试数据失败: {e}")
    
    def run_all_tests(self):
        """运行所有测试"""
        print("🧪 悟空量化金融智能助手 - 管理员功能测试")
        print("=" * 60)
        
        try:
            # 基础设置
            self.test_admin_init()
            self.create_test_users()
            
            # 登录测试
            self.login_admin()
            self.login_user()
            
            if not self.admin_token:
                print("❌ 无法继续测试：管理员登录失败")
                return
            
            # 功能测试
            self.test_admin_statistics()
            self.test_user_management()
            self.test_user_status_toggle()
            self.test_login_logs()
            self.test_admin_logs()
            self.test_permission_control()
            
            print("\n🎉 所有测试完成!")
            
        except Exception as e:
            print(f"\n❌ 测试过程中出现错误: {e}")
        finally:
            # 清理测试数据
            self.cleanup()

if __name__ == "__main__":
    tester = AdminTester()
    tester.run_all_tests()
