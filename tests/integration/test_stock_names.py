#!/usr/bin/env python3
"""
测试获取股票名称的方法
"""

import efinance as ef
import pandas as pd

def test_stock_name_methods():
    """测试不同的股票名称获取方法"""
    print("🧪 测试股票名称获取方法")
    print("="*50)
    
    test_symbol = "000001"
    
    # 方法1: 尝试从历史数据中获取名称
    try:
        print(f"\n1️⃣ 测试从历史数据获取名称:")
        df = ef.stock.get_quote_history(test_symbol)
        print(f"   历史数据列: {df.columns.tolist()}")
        if '股票名称' in df.columns:
            stock_name = df['股票名称'].iloc[0]
            print(f"   股票名称: {stock_name}")
        else:
            print("   历史数据中没有股票名称字段")
    except Exception as e:
        print(f"   ❌ 获取历史数据失败: {e}")
    
    # 方法2: 尝试获取股票基本信息
    try:
        print(f"\n2️⃣ 测试获取股票基本信息:")
        # 这个可能不存在，但试试看
        info = ef.stock.get_base_info(test_symbol)
        print(f"   基本信息: {info}")
    except Exception as e:
        print(f"   ❌ 获取基本信息失败: {e}")
    
    # 方法3: 尝试获取所有股票列表
    try:
        print(f"\n3️⃣ 测试获取股票列表:")
        stocks = ef.stock.get_realtime_quotes()
        print(f"   股票列表前5行:")
        print(stocks.head())
        print(f"   列名: {stocks.columns.tolist()}")
        
        # 查找测试股票
        if '股票代码' in stocks.columns:
            test_stock = stocks[stocks['股票代码'] == test_symbol]
            if not test_stock.empty and '股票名称' in stocks.columns:
                stock_name = test_stock['股票名称'].iloc[0]
                print(f"   找到股票名称: {stock_name}")
            else:
                print(f"   未找到股票 {test_symbol} 的名称")
    except Exception as e:
        print(f"   ❌ 获取股票列表失败: {e}")
    
    # 方法4: 尝试其他可能的方法
    try:
        print(f"\n4️⃣ 测试其他方法:")
        # 查看efinance所有可用方法
        stock_methods = [method for method in dir(ef.stock) if not method.startswith('_')]
        print(f"   efinance.stock 可用方法: {stock_methods}")
    except Exception as e:
        print(f"   ❌ 查看方法失败: {e}")

if __name__ == "__main__":
    test_stock_name_methods()
