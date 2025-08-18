#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
管理员界面测试脚本
测试管理员专用界面的显示效果
"""

import asyncio
import sys
from pymongo import MongoClient
from auth import create_user

async def ensure_admin_user():
    """确保系统中有管理员账户"""
    try:
        # 连接数据库
        client = MongoClient('mongodb://localhost:27017/')
        db = client.quantfinance
        
        # 检查是否已有admin用户
        admin_user = db.users.find_one({'username': 'admin'})
        
        if admin_user:
            print('✅ 管理员账户已存在')
            print(f'   用户名: {admin_user["username"]}')
            print(f'   邮箱: {admin_user.get("email", "N/A")}')
            print(f'   管理员权限: {admin_user.get("is_admin", False)}')
            return True
        
        # 创建管理员账户
        print('🔧 创建管理员账户...')
        
        admin_data = {
            'username': 'admin',
            'email': 'admin@quantfinance.com',
            'full_name': '系统管理员',
            'password': 'admin123'
        }
        
        result = await create_user(
            username=admin_data['username'],
            email=admin_data['email'],
            full_name=admin_data['full_name'],
            password=admin_data['password'],
            is_admin=True
        )
        
        if result:
            print('✅ 管理员账户创建成功!')
            print(f'   用户名: {admin_data["username"]}')
            print(f'   密码: {admin_data["password"]}')
            print(f'   邮箱: {admin_data["email"]}')
            print('   管理员权限: ✅')
            return True
        else:
            print('❌ 创建管理员账户失败')
            return False
            
    except Exception as e:
        print(f'❌ 操作失败: {e}')
        return False

def print_test_instructions():
    """打印测试说明"""
    print('\n' + '='*60)
    print('🧪 管理员界面测试指南')
    print('='*60)
    print()
    print('📋 测试步骤:')
    print('1. 确保后端服务运行在 http://localhost:3001')
    print('2. 确保前端服务运行在 http://localhost:5173')
    print('3. 使用以下管理员账户登录:')
    print('   用户名: admin')
    print('   密码: admin123')
    print()
    print('🎯 预期效果:')
    print('✅ 管理员登录后只显示"系统管理"标签页')
    print('✅ 界面采用专业冷静的蓝灰色调设计')
    print('✅ 不显示股票分析、K线图等业务功能')
    print('✅ 显示系统监控、用户管理、日志审计等管理功能')
    print()
    print('🎨 UI风格特点:')
    print('• 冷静专业的深色主题 (蓝灰色调)')
    print('• 企业级数据表格设计')
    print('• 简洁的功能标签和状态指示器')
    print('• 专业的管理仪表板布局')
    print()
    print('🔍 普通用户对比:')
    print('• 普通用户: 紫色渐变主题 + 股票分析功能')
    print('• 管理员: 蓝灰色专业主题 + 系统管理功能')
    print()
    print('='*60)

def check_services():
    """检查服务状态"""
    import requests
    
    print('\n🔍 检查服务状态...')
    
    # 检查后端服务
    try:
        response = requests.get('http://localhost:3001/api/health', timeout=3)
        if response.status_code == 200:
            print('✅ 后端服务 (端口 3001) 运行正常')
        else:
            print(f'⚠️  后端服务响应异常: {response.status_code}')
    except requests.RequestException:
        print('❌ 后端服务 (端口 3001) 未运行')
        print('   请先启动: cd /path/to/project && python -m uvicorn main:app --host 0.0.0.0 --port 3001 --reload')
    
    # 检查前端服务
    try:
        response = requests.get('http://localhost:5173', timeout=3)
        if response.status_code == 200:
            print('✅ 前端服务 (端口 5173) 运行正常')
        else:
            print(f'⚠️  前端服务响应异常: {response.status_code}')
    except requests.RequestException:
        print('❌ 前端服务 (端口 5173) 未运行')
        print('   请先启动: cd dashboard && npm run dev')

async def main():
    """主函数"""
    print('🛠️  管理员界面测试工具')
    print('='*40)
    
    # 检查服务状态
    check_services()
    
    # 确保管理员账户存在
    print('\n👤 检查管理员账户...')
    admin_ready = await ensure_admin_user()
    
    if admin_ready:
        print_test_instructions()
        
        print('\n🚀 现在可以开始测试管理员界面!')
        print('   访问: http://localhost:5173')
        print('   登录管理员账户体验专业界面设计')
    else:
        print('\n❌ 管理员账户准备失败，请检查数据库连接')
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
