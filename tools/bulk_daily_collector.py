#!/usr/bin/env python3
"""
批量日线数据采集脚本 (基于 Tushare daily 接口)

目标：
  - 一次性按日期区间抓取所有股票的日线数据，而不是逐股循环 API
  - 支持 start_date, end_date (YYYYMMDD)，若未提供则使用当天（若不是交易日直接退出）
  - 数据按 trade_date 分批写入 MongoDB，使用 bulk_write 提升性能
  - 可配置批大小、并行写线程数（API 获取是单调用，不做并发；写入可并发）
  - 避免重复写：以 (symbol, trade_date) upsert
  - 提供简单指标输出（抓取条数、写入耗时等）

依赖：
  - 已在环境变量中配置 TUSHARE_TOKEN
  - MongoDB 连接 MONGO_URL

注意：Tushare daily 接口支持按 ts_code 筛选；这里利用不传 ts_code 直接返回全部当天数据。
      为了区间请求，需要对每个交易日调用一次 daily；Tushare 没有直接区间全市场批量接口，只能循环交易日。

后续扩展建议：
  - 缓存交易日历，跳过非交易日请求
  - 增加 --only-missing 模式，根据数据库已有最新日期自动衔接
  - 增加 Prometheus 指标或写入 stats 集合
"""
import os
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from pymongo import MongoClient, UpdateOne
import tushare as ts
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

DEFAULT_BATCH_SIZE = 1000
DEFAULT_WRITE_THREADS = 4

def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='批量日线数据采集 (全市场)')
    parser.add_argument('--start', type=str, help='开始日期 YYYYMMDD')
    parser.add_argument('--end', type=str, help='结束日期 YYYYMMDD')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE, help='单次 bulk 写入条数')
    parser.add_argument('--write-threads', type=int, default=DEFAULT_WRITE_THREADS, help='并行写线程数')
    parser.add_argument('--dry-run', action='store_true', help='仅查看将抓取的数据规模，不写库')
    parser.add_argument('--verbose', action='store_true', help='输出更多调试信息')
    parser.add_argument('--sleep', type=float, default=0.25, help='每个交易日调用之间休眠秒数(减压)')
    return parser.parse_args()


def init_tushare():
    token = os.getenv('TUSHARE_TOKEN')
    if not token:
        print('❌ 缺少环境变量 TUSHARE_TOKEN')
        sys.exit(1)
    ts.set_token(token)
    return ts.pro_api()


def init_mongo():
    mongo_url = os.getenv('MONGO_URL', 'mongodb://localhost:27017')
    client = MongoClient(mongo_url)
    db = client['finance']
    coll = db['volume_price']
    # 索引：symbol + trade_date (字符串)
    try:
        coll.create_index([('symbol', 1), ('trade_date', 1)], background=True)
    except Exception as e:
        print(f'⚠️ 创建索引异常: {e}')
    return coll


def normalize_date_str(s: str) -> str:
    if not s:
        return ''
    if len(s) == 8 and s.isdigit():
        return s
    # 尝试解析常见格式
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y%m%d')
        except Exception:
            pass
    return s


def is_trading_day(pro, date_str: str) -> bool:
    cal = pro.trade_cal(start_date=date_str, end_date=date_str)
    if cal.empty:
        return False
    return int(cal.iloc[0]['is_open']) == 1


def get_trading_days(pro, start_date: str, end_date: str) -> List[str]:
    cal = pro.trade_cal(start_date=start_date, end_date=end_date)
    if cal.empty:
        return []
    open_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
    return open_days


def fetch_daily_for_day(pro, date_str: str, verbose=False) -> pd.DataFrame:
    try:
        df = pro.daily(trade_date=date_str)
        if df is None or df.empty:
            if verbose:
                print(f'⚠️ {date_str} 无数据返回')
            return pd.DataFrame()
        # 统一字段 subset
        keep = ['ts_code','trade_date','open','high','low','close','pre_close','change','pct_chg','vol','amount']
        df = df[keep].copy()
        df.rename(columns={'vol':'volume'}, inplace=True)
        # 字段转基本类型
        for col in ['open','high','low','close','pre_close','change','pct_chg','amount']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0).astype(int)
        df['symbol'] = df['ts_code'].str.split('.').str[0]
        df['update_time'] = datetime.now().strftime('%Y%m%d %H:%M:%S')
        return df
    except Exception as e:
        print(f'❌ 获取 {date_str} 日线失败: {e}')
        return pd.DataFrame()


def chunk_iter(df: pd.DataFrame, size: int):
    if df.empty:
        return
    total = len(df)
    for start in range(0, total, size):
        yield df.iloc[start:start+size]


def build_bulk_ops(df: pd.DataFrame):
    ops = []
    for _, row in df.iterrows():
        doc = row.to_dict()
        ops.append(UpdateOne(
            {'symbol': doc['symbol'], 'trade_date': doc['trade_date']},
            {'$set': doc},
            upsert=True
        ))
    return ops


def parallel_bulk_write(coll, dataframes: List[pd.DataFrame], batch_size: int, write_threads: int, verbose=False):
    total_docs = 0
    start_time = time.time()
    def submit_df(df_part: pd.DataFrame):
        nonlocal total_docs
        count = 0
        for chunk in chunk_iter(df_part, batch_size):
            ops = build_bulk_ops(chunk)
            if ops:
                coll.bulk_write(ops, ordered=False)
                count += len(ops)
        return count
    with ThreadPoolExecutor(max_workers=write_threads) as ex:
        futures = [ex.submit(submit_df, df) for df in dataframes if not df.empty]
        for fut in as_completed(futures):
            try:
                c = fut.result()
                total_docs += c
            except Exception as e:
                print(f'⚠️ bulk 写入子任务失败: {e}')
    dur = time.time() - start_time
    if verbose:
        print(f'🧾 写入完成: {total_docs} 条，用时 {dur:.2f}s  (平均 {total_docs/dur if dur>0 else 0:.1f} 条/秒)')
    return total_docs, dur


def main():
    args = parse_args()
    pro = init_tushare()
    coll = init_mongo()

    today = datetime.now().strftime('%Y%m%d')
    start_date = normalize_date_str(args.start) if args.start else today
    end_date = normalize_date_str(args.end) if args.end else today

    # 单日模式验证
    if not args.start and not args.end:
        if not is_trading_day(pro, today):
            print(f'📅 {today} 不是交易日，退出')
            return
    # 区间合法性
    if start_date > end_date:
        print(f'❌ 日期区间非法: {start_date}>{end_date}')
        return

    trading_days = get_trading_days(pro, start_date, end_date)
    if not trading_days:
        print('⚠️ 区间内无交易日，退出')
        return

    print(f'📆 交易日数量: {len(trading_days)}  ({trading_days[0]} -> {trading_days[-1]})')
    print(f'🛠️ 参数: batch_size={args.batch_size} write_threads={args.write_threads} dry_run={args.dry_run}')

    all_day_dfs = []
    total_rows = 0
    fetch_start = time.time()
    for d in trading_days:
        df = fetch_daily_for_day(pro, d, verbose=args.verbose)
        if not df.empty:
            all_day_dfs.append(df)
            total_rows += len(df)
        if args.sleep>0:
            time.sleep(args.sleep)
    fetch_dur = time.time() - fetch_start

    print(f'📥 抓取完成: {len(all_day_dfs)} 天, {total_rows} 行, 用时 {fetch_dur:.2f}s (平均 {total_rows/(fetch_dur or 1):.1f} 行/秒)')
    if args.dry_run:
        print('💡 dry-run 模式，不写入数据库')
        return

    # 写入阶段
    write_total, write_dur = parallel_bulk_write(
        coll,
        all_day_dfs,
        batch_size=args.batch_size,
        write_threads=args.write_threads,
        verbose=True
    )
    print(f'✅ 全流程完成: 获取 {total_rows} 行, 写入 {write_total} 条 (去重后 upsert)，总耗时 {(fetch_dur+write_dur):.2f}s')

if __name__ == '__main__':
    main()
