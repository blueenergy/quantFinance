import pandas as pd
import akshare as ak
from tqdm import tqdm
import efinance as ef
from datetime import datetime, date
stock_df = ak.stock_info_a_code_name()
total_stocks = len(stock_df)
print(f"获取到 {total_stocks} 只A股代码")
print(dir(stock_df))

print("查看所有列名")
print(stock_df.columns)

print("访问某一列（如股票代码")
codes = stock_df['code']
print(codes)

print("访问某一行")
row = stock_df.iloc[0]
print(row)

print("访问某个单元格")
code = stock_df.loc[0, 'code']
name = stock_df.loc[0, 'name']
print(code,name)
for symbol in tqdm(stock_df['code'], desc="📊 数据采集进度"):
    print(symbol)
    df = ef.stock.get_quote_history(symbol)
    print(df)
    print(dir(df))
    df = df[['日期', '开盘', '最高', '最低', '收盘', '成交量']].copy()
    df.columns = ['trade_date', 'open', 'high', 'low', 'close', 'volume']
    # 转换日期格式
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    # 添加元数据
    df['symbol'] = symbol
    df['update_time'] = datetime.now()
    # 类型转换
    df['open'] = df['open'].astype(float)
    df['volume'] = df['volume'].astype(int)