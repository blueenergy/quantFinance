import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import tushare as ts
from pymongo import MongoClient, UpdateOne
from datetime import date
from utils import get_ts_code
from dotenv import load_dotenv
load_dotenv()


ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token
pro = ts.pro_api()

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
stock_info_collection = db["stock_info"]
money_flow_collection = db["money_flow"]
state_collection = db["collect_state"]


# df = pro.moneyflow(ts_code= '000002.SZ', start_date='20250101', end_date='20250103')
# records = df.to_dict(orient='records')
# for record in records:
#     money_flow_collection.insert_one({"symbol": '000002', **record})

# for doc in money_flow_collection.find():
#     print(doc)
# for doc in stock_info_collection.find():
#     ts_code = get_ts_code(doc['symbol'])
#     df = pro.moneyflow(ts_code= ts_code, start_date='20250903', end_date='20250905')
#     records = df.to_dict(orient='records')
#     for record in records:
#         money_flow_collection.insert_one({"symbol": doc['symbol'], **record})

import argparse
from datetime import datetime, timedelta


def build_ts_code_map():
    """Build a mapping from tushare ts_code (e.g. '000002.SZ') to local symbol (e.g. '000002').

    Reads `stock_info_collection` and calls `get_ts_code(doc['symbol'])` for each doc.
    This keeps the existing collection lookup logic but lets us query moneyflow by date
    once and then map each returned ts_code back to our local symbol.
    """
    mapping = {}
    for doc in stock_info_collection.find():
        try:
            ts_code = get_ts_code(doc['symbol'])
            mapping[ts_code] = doc['symbol']
        except Exception:
            # skip entries that can't be converted
            continue
    return mapping


def collect_range_by_date(start_date: str, end_date: str):
    """Collect moneyflow data for each date in [start_date, end_date].

    Dates must be strings in YYYYMMDD format. For each trade_date we call
    `pro.moneyflow(trade_date=trade_date)` which returns data for all symbols on that date.
    We then map the returned `ts_code` to our local `symbol` using `build_ts_code_map()`
    and bulk upsert into `money_flow_collection`.
    """
    ts_map = build_ts_code_map()

    # prefer using tushare trading calendar to skip non-trading days
    try:
        cal = pro.trade_cal(start_date=start_date, end_date=end_date)
        # trade_cal returns a DataFrame with 'cal_date' and 'is_open' flags
        trade_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
        trade_days = [d.replace('-', '') if isinstance(d, str) else d.strftime('%Y%m%d') for d in trade_days]
    except Exception as exc:
        print(f"Failed to fetch trade calendar, falling back to daily iteration: {exc}")
        s = datetime.strptime(start_date, "%Y%m%d").date()
        e = datetime.strptime(end_date, "%Y%m%d").date()
        trade_days = []
        day = s
        while day <= e:
            trade_days.append(day.strftime('%Y%m%d'))
            day += timedelta(days=1)

    for trade_date in trade_days:
        try:
            df = pro.moneyflow(trade_date=trade_date)
        except Exception as exc:
            print(f"Failed to fetch moneyflow for {trade_date}: {exc}")
            continue

        if df is None or df.empty:
            print(f"No data for {trade_date}")
            continue

        records = df.to_dict(orient='records')
        operations = []
        for record in records:
            ts_code = record.get('ts_code') or record.get('ts_code')
            # map to our local symbol; if missing, keep ts_code as symbol to avoid data loss
            symbol = ts_map.get(ts_code)
            if not symbol:
                # try fallback: if ts_code like '000002.SZ', extract numeric prefix
                if isinstance(ts_code, str) and '.' in ts_code:
                    symbol = ts_code.split('.')[0]
                else:
                    symbol = ts_code

            record['symbol'] = symbol
            # ensure trade_date exists as YYYYMMDD
            td = record.get('trade_date') or trade_date
            record['trade_date'] = td

            operations.append(
                UpdateOne(
                    {"symbol": symbol, "trade_date": td},
                    {"$set": record},
                    upsert=True,
                )
            )

        if operations:
            try:
                result = money_flow_collection.bulk_write(operations, ordered=False)
                upserted_count = 0
                if hasattr(result, 'upserted_ids') and result.upserted_ids:
                    upserted_count = len(result.upserted_ids)
                elif hasattr(result, 'upserted_count'):
                    upserted_count = result.upserted_count or 0

                modified_count = getattr(result, 'modified_count', 0)
                print(f"Date {trade_date}: upserted {upserted_count} / modified {modified_count}")

                #update last successful date in state collection
                try:
                    state_collection.update_one({'_id': 'money_flow_last_date'}, {'$set': {'last_date': trade_date}}, upsert=True)
                except Exception as exc:
                    print(f"Failed to update last-success state for {trade_date}: {exc}")
            except Exception as exc:
                print(f"Bulk write failed for {trade_date}: {exc}")


def main():
    parser = argparse.ArgumentParser(description='Money flow collector')
    parser.add_argument('--mode', choices=['date', 'symbol'], default='date', help='collection mode')
    parser.add_argument('--start', help='start date YYYYMMDD')
    parser.add_argument('--end', help='end date YYYYMMDD')
    args = parser.parse_args()

    if args.mode == 'date':
        # Default end: if omitted, pick latest trading date <= today
        end = args.end
        if not end:
            try:
                today = datetime.today().date()
                # ask tushare for recent trade days (last 14 days should be enough)
                cal = pro.trade_cal(start_date=(today - timedelta(days=14)).strftime('%Y%m%d'), end_date=today.strftime('%Y%m%d'))
                opens = cal[cal['is_open'] == 1]['cal_date'].tolist()
                print(opens)
                if opens:
                    latest = opens[0]
                    end = latest.replace('-', '') if isinstance(latest, str) else latest.strftime('%Y%m%d')
                    print(f"Auto-selected end date (latest trading day): {end}")
                else:
                    end = today.strftime('%Y%m%d')
                    print(f"No trading days found in calendar; defaulting end to today: {end}")
            except Exception as exc:
                end = datetime.today().strftime('%Y%m%d')
                print(f"Failed to fetch trade calendar to determine end date, defaulting to today: {end} (error: {exc})")

        # If start is omitted, try to resume from last successful date + 1 day
        start = args.start
        if not start:
            doc = state_collection.find_one({'_id': 'money_flow_last_date'})
            if doc and 'last_date' in doc:
                # advance one day
                last = datetime.strptime(doc['last_date'], '%Y%m%d').date()
                start_date_obj = last + timedelta(days=1)
                start = start_date_obj.strftime('%Y%m%d')
                print(f"Resuming from last successful date: {start}")
            else:
                # default to 7 days before end if no state present
                end_obj = datetime.strptime(end, '%Y%m%d').date()
                start_date_obj = end_obj - timedelta(days=1)
                start = start_date_obj.strftime('%Y%m%d')
                print(f"No last-successful-date found, defaulting start to {start}")
        print(f"Collecting data from {start} to {end}")
        collect_range_by_date(start, end)
    else:
        # symbol mode: preserve backward compatible behavior - single symbol range from hardcoded dates
        for doc in stock_info_collection.find():
            ts_code = get_ts_code(doc['symbol'])
            df = pro.moneyflow(ts_code=ts_code, start_date='20250906', end_date='20250913')
            records = df.to_dict(orient='records')

            # 给每条记录加 symbol 字段
            for record in records:
                record["symbol"] = doc["symbol"]

            if records:
                # ✅ 使用 bulk_write 批量 upsert
                operations = []
                for record in records:
                    operations.append(
                        UpdateOne(
                            {"symbol": record["symbol"], "trade_date": record["trade_date"]},
                            {"$set": record},
                            upsert=True
                        )
                    )

                if operations:
                    result = money_flow_collection.bulk_write(operations)
                    print(f"股票 {doc['symbol']}: 插入 {result.upserted_count} 条，更新 {result.modified_count} 条")


if __name__ == '__main__':
    main()


#获取单日全部股票数据
#df = pro.moneyflow(trade_date='20190315')

#获取单个股票数据
#df = pro.moneyflow(ts_code='002149.SZ', start_date='20190115', end_date='20190315')
# for doc in money_flow_collection.find({"symbol": '000002'}):
#     print(doc)