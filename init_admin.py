#!/usr/bin/env python3
"""
管理员初始化脚本
用于创建初始管理员账户
"""

import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from auth import create_admin_user, get_user

def init_admin():
    """初始化管理员账户"""
    print("🔧 悟空量化金融智能助手 - 管理员初始化工具")
    print("=" * 50)
    
    # 检查是否已有管理员
    try:
        from auth import users_collection
        existing_admin = users_collection.find_one({"is_admin": True})
        if existing_admin:
            print(f"⚠️  管理员账户已存在: {existing_admin['username']}")
            confirm = input("是否要创建新的管理员账户? (y/N): ")
            if confirm.lower() != 'y':
                print("❌ 操作已取消")
                return
    except Exception as e:
        print(f"❌ 检查现有管理员失败: {e}")
        return
    
    print("\n请输入管理员账户信息:")
    
    # 获取管理员信息
    while True:
        username = input("用户名: ").strip()
        if not username:
            print("❌ 用户名不能为空")
            continue
        
        # 检查用户名是否已存在
        if get_user(username):
            print(f"⚠️  用户名 '{username}' 已存在")
            confirm = input("是否将现有用户提升为管理员? (y/N): ")
            if confirm.lower() == 'y':
                break
            else:
                continue
        break
    
    email = input("邮箱: ").strip()
    if not email:
        email = f"{username}@quantfinance.com"
        print(f"使用默认邮箱: {email}")
    
    full_name = input("姓名 (可选): ").strip()
    if not full_name:
        full_name = "系统管理员"
    
    # 密码设置
    import getpass
    while True:
        password = getpass.getpass("密码: ")
        if len(password) < 6:
            print("❌ 密码长度至少6位")
            continue
        
        confirm_password = getpass.getpass("确认密码: ")
        if password != confirm_password:
            print("❌ 两次密码不一致")
            continue
        break
    
    # 创建管理员
    try:
        result = create_admin_user(
            username=username,
            password=password,
            email=email,
            full_name=full_name
        )
        
        print("\n✅ 管理员账户创建成功!")
        print(f"📝 {result['message']}")
        print(f"👤 用户名: {username}")
        print(f"📧 邮箱: {email}")
        print(f"👨‍💼 姓名: {full_name}")
        print("\n🎯 您现在可以使用管理员账户登录系统了!")
        
        # 提示如何访问管理员功能
        print("\n" + "=" * 50)
        print("📋 管理员功能说明:")
        print("1. 使用管理员账户登录系统")
        print("2. 在主界面点击 '管理员' 标签")
        print("3. 可以查看:")
        print("   - 📊 系统统计信息")
        print("   - 👥 用户管理")
        print("   - 📋 登录日志")
        print("   - ⚙️  管理员操作日志")
        print("4. 可以执行:")
        print("   - ✅ 激活/禁用用户")
        print("   - 👀 查看用户详情")
        print("   - 📊 查看系统统计")
        
    except Exception as e:
        print(f"❌ 创建管理员失败: {e}")

if __name__ == "__main__":
    init_admin()
