import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import tushare as ts
from pymongo import MongoClient, UpdateOne
from datetime import date
from utils.stock_utils import get_ts_code
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
import time
from pymongo.errors import BulkWriteError, PyMongoError


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


def bulk_write_in_batches(collection, operations, batch_size=1000, max_retries=3, backoff_factor=1.5):
    """Perform unordered bulk writes in batches with retry/backoff.

    Returns tuple (total_upserted, total_modified).
    """
    total_upserted = 0
    total_modified = 0

    for i in range(0, len(operations), batch_size):
        batch = operations[i:i + batch_size]
        attempt = 0
        while True:
            try:
                res = collection.bulk_write(batch, ordered=False)
                # compute upserted count in a version-compatible way
                upserted = 0
                if hasattr(res, 'upserted_ids') and res.upserted_ids:
                    upserted = len(res.upserted_ids)
                else:
                    upserted = getattr(res, 'upserted_count', 0) or 0

                modified = getattr(res, 'modified_count', 0) or 0
                total_upserted += upserted
                total_modified += modified
                break
            except BulkWriteError as bwe:
                attempt += 1
                print(f"BulkWriteError on batch {i} attempt {attempt}: {getattr(bwe, 'details', bwe)}")
                if attempt > max_retries:
                    print("Max retries exceeded for batch starting at", i)
                    # Re-raise to let caller decide
                    raise
                sleep_time = backoff_factor ** attempt
                time.sleep(sleep_time)
            except PyMongoError as e:
                attempt += 1
                print(f"PyMongoError on batch {i} attempt {attempt}: {e}")
                if attempt > max_retries:
                    raise
                time.sleep(backoff_factor ** attempt)

    return total_upserted, total_modified


def resolve_date_range(arg_start: str, arg_end: str, state_collection, pro_client, lookback_days: int = 14):
    """Resolve (start, end) date strings (YYYYMMDD) based on user args, existing state and trade calendar.

    Logic:
      1. End date precedence: arg_end > detected latest trading day > today.
      2. Start date precedence: arg_start > (state last_date + 1) > (end - 1 day).
    This mirrors previous inline logic and is isolated for testing.
    """
    # Determine end
    end = arg_end
    if not end:
        try:
            today = datetime.today().date()
            cal = pro_client.trade_cal(start_date=(today - timedelta(days=lookback_days)).strftime('%Y%m%d'),
                                        end_date=today.strftime('%Y%m%d'))
            opens = cal[cal['is_open'] == 1]['cal_date'].tolist()
            if opens:
                latest = opens[0]
                end = latest.replace('-', '') if isinstance(latest, str) else latest.strftime('%Y%m%d')
                print(f"Auto-selected end date (latest trading day): {end}")
            else:
                end = today.strftime('%Y%m%d')
                print(f"No trading days found; default end=today {end}")
        except Exception as exc:
            end = datetime.today().strftime('%Y%m%d')
            print(f"Failed to compute end from calendar, fallback to today {end}: {exc}")

    # Determine start
    start = arg_start
    if not start:
        try:
            doc = state_collection.find_one({'_id': 'money_flow_last_date'})
        except Exception:
            doc = None
        if doc and 'last_date' in doc:
            try:
                last = datetime.strptime(doc['last_date'], '%Y%m%d').date()
                start = (last + timedelta(days=1)).strftime('%Y%m%d')
                print(f"Resuming from last successful date: {start}")
            except Exception:
                start = None
        if not start:
            try:
                end_obj = datetime.strptime(end, '%Y%m%d').date()
                start = (end_obj - timedelta(days=1)).strftime('%Y%m%d')
                print(f"Defaulting start to {start} (end - 1 day)")
            except Exception:
                start = end
                print(f"Fallback: set start=end={start}")

    # Guard: if start > end, swap or clamp
    try:
        if datetime.strptime(start, '%Y%m%d') > datetime.strptime(end, '%Y%m%d'):
            print(f"Warning: start {start} > end {end}, adjusting start=end")
            start = end
    except Exception:
        pass
    return start, end


def get_trade_days(start_date: str, end_date: str, pro_client):
    """Return list of trading days (YYYYMMDD). Falls back to sequential days if calendar fetch fails."""
    try:
        cal = pro_client.trade_cal(start_date=start_date, end_date=end_date)
        trade_days = cal[cal['is_open'] == 1]['cal_date'].tolist()
        return [d.replace('-', '') if isinstance(d, str) else d.strftime('%Y%m%d') for d in trade_days]
    except Exception as exc:
        print(f"Failed to fetch trade calendar, fallback iteration: {exc}")
        s = datetime.strptime(start_date, "%Y%m%d").date()
        e = datetime.strptime(end_date, "%Y%m%d").date()
        out = []
        day = s
        while day <= e:
            out.append(day.strftime('%Y%m%d'))
            day += timedelta(days=1)
        return out


def collect_range_by_date(start_date: str, end_date: str):
    """Collect moneyflow data for each date in [start_date, end_date].

    Dates must be strings in YYYYMMDD format. For each trade_date we call
    `pro.moneyflow(trade_date=trade_date)` which returns data for all symbols on that date.
    We then map the returned `ts_code` to our local `symbol` using `build_ts_code_map()`
    and bulk upsert into `money_flow_collection`.
    """
    ts_map = build_ts_code_map()

    trade_days = get_trade_days(start_date, end_date, pro)

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
                upserted_count, modified_count = bulk_write_in_batches(money_flow_collection, operations)
                print(f"Date {trade_date}: upserted {upserted_count} / modified {modified_count}")

                # update last successful date in state collection
                try:
                    state_collection.update_one({'_id': 'money_flow_last_date'}, {'$set': {'last_date': trade_date}}, upsert=True)
                except Exception as exc:
                    print(f"Failed to update last-success state for {trade_date}: {exc}")
            except Exception as exc:
                print(f"Bulk write failed for {trade_date}: {exc}")


def main():
    parser = argparse.ArgumentParser(description='Money flow collector')
    parser.add_argument('--start', help='start date YYYYMMDD')
    parser.add_argument('--end', help='end date YYYYMMDD')
    args = parser.parse_args()


    start, end = resolve_date_range(args.start, args.end, state_collection, pro)
    print(f"Collecting data from {start} to {end}")
    collect_range_by_date(start, end)


if __name__ == '__main__':
    main()


#获取单日全部股票数据
#df = pro.moneyflow(trade_date='20190315')

#获取单个股票数据
#df = pro.moneyflow(ts_code='002149.SZ', start_date='20190115', end_date='20190315')
# for doc in money_flow_collection.find({"symbol": '000002'}):
#     print(doc)