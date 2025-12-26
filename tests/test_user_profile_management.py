"""
用户配置管理功能测试
测试新实现的用户配置管理功能，包括邮箱验证、资料更新等
"""

import pytest
import requests
import os
from datetime import datetime, timedelta
import jwt

# 从环境变量获取测试配置
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:3001")

def test_user_registration_with_email_verification():
    """测试用户注册及邮箱验证流程"""
    # 生成测试用户名和邮箱
    import uuid
    test_username = f"testuser_{uuid.uuid4().hex[:8]}"
    test_email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    test_password = "testpassword123"
    
    # 1. 注册用户
    register_payload = {
        "username": test_username,
        "email": test_email,
        "password": test_password,
        "full_name": "Test User"
    }
    
    response = requests.post(f"{BASE_URL}/api/user/register", json=register_payload)
    assert response.status_code == 200, f"注册失败: {response.text}"
    
    # 2. 尝试登录（应该失败，因为邮箱未验证）
    login_payload = {
        "username": test_username,
        "password": test_password
    }
    
    response = requests.post(f"{BASE_URL}/api/user/login", json=login_payload)
    assert response.status_code == 401, "未验证用户不应该能够登录"
    assert "账户未激活" in response.json().get("detail", "") or "email not verified" in response.json().get("detail", "")
    
    print("✓ 用户注册成功，未验证用户无法登录")

def test_email_verification():
    """测试邮箱验证功能（需要手动测试或使用测试令牌）"""
    # 注意：这需要一个有效的验证令牌，通常在真实环境中通过邮件获取
    # 在测试环境中，我们可以手动创建一个验证令牌
    
    # 这里需要模拟验证令牌的生成和验证过程
    # 由于验证令牌是通过邮件发送的，实际测试可能需要特殊处理
    print("⚠ 邮箱验证功能需要在真实环境中测试，因为需要发送邮件")

def test_user_profile_update():
    """测试用户资料更新功能"""
    # 首先需要一个已登录的用户
    # 这里我们使用现有的测试用户或创建一个
    pass

def test_llm_config_management():
    """测试LLM配置管理功能"""
    # 需要一个已认证的用户token
    # 测试更新和获取LLM配置
    pass

def test_password_change():
    """测试密码修改功能"""
    # 需要一个已认证的用户
    # 测试密码修改流程
    pass

def test_securities_account_management():
    """测试证券账户管理功能"""
    # 需要一个已认证的用户
    # 测试证券账户的增删改查
    pass

if __name__ == "__main__":
    # 运行测试
    test_user_registration_with_email_verification()
    test_email_verification()
    print("用户配置管理功能测试完成")