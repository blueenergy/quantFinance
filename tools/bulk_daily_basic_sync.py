#!/usr/bin/env python3
"""
bulk_daily_basic_sync.py

目的:
  按交易日区间批量抓取全市场 daily_basic (估值/基础行情指标) 数据, 一天一次 API 调用, 避免逐 ts_code 循环。
  与现有 sync_financial_data.py 并行存在, 不修改原脚本。

特性:
  - 支持 --start, --end (YYYYMMDD), 缺省=当天 (若非交易日则退出)
  - 获取交易日历, 只对开市日调用 daily_basic(trade_date=...) 一次
  - DataFrame 扩展: 添加 symbol=ts_code 前缀去除后的部分
  - Upsert 写入 Mongo 集合 financial_daily_basic (与现有同步器相同集合)
  - Bulk 写入, 可调批大小/写线程
  - Dry-run 支持
  - 输出抓取与写入统计

后续可扩展:
  - 增加 only-missing 模式 (自动从库最新 trade_date+1 补齐)
  - 增加 Prometheus 指标
  - 将 daily 与 daily_basic 合并管线
"""
import os
import sys
import time
from datetime import datetime
from typing import List
from pymongo import MongoClient, UpdateOne
import pandas as pd
import tushare as ts
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv()

DEFAULT_BATCH_SIZE = 1000
DEFAULT_WRITE_THREADS = 4


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description='按日批量同步 daily_basic (全市场)')
    p.add_argument('--start', type=str, help='开始日期 YYYYMMDD')
    p.add_argument('--end', type=str, help='结束日期 YYYYMMDD')
    p.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE, help='bulk 写入批大小')
    p.add_argument('--write-threads', type=int, default=DEFAULT_WRITE_THREADS, help='并行写线程数')
    p.add_argument('--dry-run', action='store_true', help='仅抓取不写库')
    p.add_argument('--sleep', type=float, default=0.25, help='交易日间调用间隔')
    p.add_argument('--verbose', action='store_true', help='输出更多日志')
    return p.parse_args()


def normalize_date(s: str) -> str:
    if not s: return ''
    if len(s) == 8 and s.isdigit():
        return s
    for fmt in ('%Y-%m-%d','%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y%m%d')
        except Exception:
            pass
    return s


def init_tushare():
    token = os.getenv('TUSHARE_TOKEN')
    if not token:
        print('❌ 缺少 TUSHARE_TOKEN')
        sys.exit(1)
    ts.set_token(token)
    return ts.pro_api()


def init_mongo():
    mongo_url = os.getenv('MONGO_URL','mongodb://localhost:27017')
    client = MongoClient(mongo_url)
    db = client['finance']
    coll = db['financial_daily_basic']
    try:
        coll.create_index([('ts_code',1),('trade_date',-1)])
    except Exception as e:
        print(f'⚠️ 索引创建失败: {e}')
    return coll


def get_trading_days(pro, start_date: str, end_date: str) -> List[str]:
    cal = pro.trade_cal(start_date=start_date, end_date=end_date)
    if cal is None or cal.empty:
        return []
    return cal[cal['is_open']==1]['cal_date'].tolist()


def fetch_daily_basic_for_day(pro, day: str, verbose=False) -> pd.DataFrame:
    try:
        df = pro.daily_basic(trade_date=day,
                             fields='ts_code,trade_date,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv,turnover_rate,volume_ratio')
        if df is None or df.empty:
            if verbose:
                print(f'⚠️ {day} daily_basic 空返回')
            return pd.DataFrame()
        df['symbol'] = df['ts_code'].str.split('.').str[0]
        df['update_time'] = datetime.now().strftime('%Y%m%d %H:%M:%S')
        # 类型处理 (避免 NaN 直接进入 Mongo, 统一设 None)
        for col in ['pe','pe_ttm','pb','ps','ps_ttm','total_mv','circ_mv','turnover_rate','volume_ratio']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        print(f'❌ 获取 {day} daily_basic 失败: {e}')
        return pd.DataFrame()


def chunk_iter(df: pd.DataFrame, size: int):
    if df.empty:
        return
    n = len(df)
    for start in range(0, n, size):
        yield df.iloc[start:start+size]


def build_ops(df: pd.DataFrame):
    ops = []
    for _, row in df.iterrows():
        doc = row.to_dict()
        # 清理 NaN
        clean = {k: (None if pd.isna(v) else v) for k,v in doc.items()}
        clean['synced_at'] = datetime.now()
        clean['data_source'] = 'tushare'
        ops.append(UpdateOne({'ts_code': clean['ts_code'], 'end_date': clean['trade_date']}, {'$set': clean}, upsert=True))
    return ops


def parallel_write(coll, dfs: List[pd.DataFrame], batch_size: int, write_threads: int, verbose=False):
    total = 0
    start = time.time()
    def process_df(df):
        c = 0
        for chunk in chunk_iter(df, batch_size):
            ops = build_ops(chunk)
            if ops:
                coll.bulk_write(ops, ordered=False)
                c += len(ops)
        return c
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=write_threads) as ex:
        futures = [ex.submit(process_df, d) for d in dfs if not d.empty]
        for f in as_completed(futures):
            try:
                total += f.result()
            except Exception as e:
                print(f'⚠️ 写入任务失败: {e}')
    dur = time.time() - start
    if verbose:
        print(f'🧾 写入完成: {total} 条, 用时 {dur:.2f}s, 速度 {total/dur if dur>0 else 0:.1f}/s')
    return total, dur


def main():
    args = parse_args()
    pro = init_tushare()
    coll = init_mongo()

    today = datetime.now().strftime('%Y%m%d')
    start = normalize_date(args.start) if args.start else today
    end = normalize_date(args.end) if args.end else today

    if start > end:
        print(f'❌ 日期区间非法: {start}>{end}')
        return

    trading_days = get_trading_days(pro, start, end)
    if not trading_days:
        print('⚠️ 区间无交易日, 退出')
        return

    # 单日且用户未指定, 需确认是交易日
    if not args.start and not args.end and trading_days[0] != today:
        print(f'📅 {today} 非交易日, 退出')
        return

    print(f'📆 交易日数量: {len(trading_days)} ({trading_days[0]} -> {trading_days[-1]})')
    print(f'🛠️ 参数: batch={args.batch_size} threads={args.write_threads} dry_run={args.dry_run}')

    dfs = []
    total_rows = 0
    fetch_start = time.time()
    for d in trading_days:
        df = fetch_daily_basic_for_day(pro, d, verbose=args.verbose)
        if not df.empty:
            dfs.append(df)
            total_rows += len(df)
        if args.sleep:
            time.sleep(args.sleep)
    fetch_dur = time.time() - fetch_start
    print(f'📥 抓取完成: {len(dfs)} 天, {total_rows} 行, 用时 {fetch_dur:.2f}s (均值 {total_rows/(fetch_dur or 1):.1f}/s)')

    if args.dry_run:
        print('💡 dry-run 模式，不写库')
        return

    wrote, write_dur = parallel_write(coll, dfs, args.batch_size, args.write_threads, verbose=True)
    print(f'✅ 完成: 抓取 {total_rows} 行, 写入 upsert {wrote} 条, 总耗时 {(fetch_dur+write_dur):.2f}s')

if __name__ == '__main__':
    main()
