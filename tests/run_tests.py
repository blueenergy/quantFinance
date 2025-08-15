#!/usr/bin/env python3
"""
测试运行器 - 运行所有测试文件的工具脚本
"""

import os
import sys
import subprocess
from pathlib import Path

def run_test_file(test_file):
    """运行单个测试文件"""
    print(f"\n{'='*60}")
    print(f"🧪 运行测试: {test_file}")
    print(f"{'='*60}")
    
    try:
        # 从项目根目录运行测试
        result = subprocess.run([
            sys.executable, f"tests/{test_file}"
        ], capture_output=False, text=True, cwd=os.getcwd())
        
        if result.returncode == 0:
            print(f"✅ {test_file} 测试通过")
            return True
        else:
            print(f"❌ {test_file} 测试失败 (返回码: {result.returncode})")
            return False
    except Exception as e:
        print(f"❌ 运行 {test_file} 时出错: {str(e)}")
        return False

def main():
    """主函数"""
    print("🧪 量化金融系统测试运行器")
    print("="*60)
    
    # 获取tests目录中的所有测试文件
    tests_dir = Path("tests")
    if not tests_dir.exists():
        print("❌ tests目录不存在")
        return
    
    test_files = []
    for file in tests_dir.glob("test_*.py"):
        test_files.append(file.name)
    
    if not test_files:
        print("❌ 没有找到测试文件")
        return
    
    print(f"📊 找到 {len(test_files)} 个测试文件:")
    for i, test_file in enumerate(test_files, 1):
        print(f"   {i}. {test_file}")
    
    print(f"\n选择运行模式:")
    print(f"   1. 运行所有测试")
    print(f"   2. 选择特定测试")
    print(f"   3. 退出")
    
    choice = input(f"\n请输入选择 (1-3): ").strip()
    
    if choice == "1":
        # 运行所有测试
        print(f"\n🚀 运行所有测试...")
        passed = 0
        failed = 0
        
        for test_file in test_files:
            if run_test_file(test_file):
                passed += 1
            else:
                failed += 1
        
        print(f"\n{'='*60}")
        print(f"📊 测试总结")
        print(f"{'='*60}")
        print(f"✅ 通过: {passed} 个测试")
        print(f"❌ 失败: {failed} 个测试")
        print(f"📊 总计: {passed + failed} 个测试")
        
    elif choice == "2":
        # 选择特定测试
        print(f"\n请选择要运行的测试文件:")
        for i, test_file in enumerate(test_files, 1):
            print(f"   {i}. {test_file}")
        
        try:
            test_choice = int(input(f"\n请输入测试编号 (1-{len(test_files)}): "))
            if 1 <= test_choice <= len(test_files):
                selected_test = test_files[test_choice - 1]
                run_test_file(selected_test)
            else:
                print("❌ 无效的选择")
        except ValueError:
            print("❌ 请输入有效的数字")
    
    elif choice == "3":
        print("👋 退出测试运行器")
    else:
        print("❌ 无效的选择")

if __name__ == "__main__":
    # 确保在项目根目录运行
    script_dir = Path(__file__).parent.absolute()
    project_root = script_dir.parent if script_dir.name == "tests" else script_dir
    os.chdir(project_root)
    
    main()
