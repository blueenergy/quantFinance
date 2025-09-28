"""Utility script: print full detailed stock info for a given index.

Usage examples:
  python tests/test_index.py hs300
  python tests/test_index.py star50
  python tests/test_index.py a500
  python tests/test_index.py 000905.SH 20    # limit first 20 constituents

Alias mapping:
  hs300  -> 000300.SH
  star50 -> 000688.SH
  a500   -> 000905.SH (这里将 A500 映射为中证500，如需自定义请修改 ALIAS_MAP)

If no argument is provided, it lists indices whose names contain '500'.

NOTE: Remove the hardcoded token below and export TUSHARE_TOKEN in production.
"""

import os
import sys
import time
from typing import List, Dict, Optional

import pandas as pd  # type: ignore
import tushare as ts

TOKEN = os.getenv('TUSHARE_TOKEN') or '1e703f9c93103a9266af23e98e606fc5cfec17649184f04e78e2226d'  # TODO: remove hardcoded token
if not TOKEN:
    print('Missing TUSHARE_TOKEN env variable.')
    sys.exit(1)

ts.set_token(TOKEN)
pro = ts.pro_api()

ALIAS_MAP = {
    'hs300': '000300.SH',
    '399300.sz': '000300.SH',
    'star50': '000688.SH',
    'kcb50': '000688.SH',
    'a500': '000510.SH',  # treat A500 as CSI500 unless custom definition supplied
    'csi500': '000905.SH',
    'zz500': '000905.SH',
    'a1000': '000852.SH',
}


def resolve_index_code(code: str) -> str:
    return ALIAS_MAP.get(code.lower(), code.upper())


def get_latest_trade_date(pro) -> str:
    from datetime import datetime, timedelta
    today = datetime.now().strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'), end_date=today)
    cal = cal[cal['is_open'] == 1].sort_values('cal_date', ascending=False)
    if cal.empty:
        return today
    return cal.iloc[0]['cal_date']


def fetch_index_constituents(pro, index_code: str, trade_date: Optional[str] = None) -> pd.DataFrame:
    if not trade_date:
        trade_date = get_latest_trade_date(pro)
    dfw = pro.index_weight(index_code=index_code, trade_date=trade_date)
    if dfw.empty:
        # fallback a few past open days
        from datetime import datetime, timedelta
        base = pd.to_datetime(trade_date)
        for i in range(1, 8):
            alt = (base - pd.Timedelta(days=i)).strftime('%Y%m%d')
            temp = pro.index_weight(index_code=index_code, trade_date=alt)
            if not temp.empty:
                print(f"Fallback trade_date={alt} for {index_code}")
                return temp
    return dfw


def batch_fetch_basic_info(pro, symbols: List[str]) -> pd.DataFrame:
    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,area,industry,list_date,market')
    return basic[basic['ts_code'].isin(symbols)].copy()


DAILY_BASIC_CACHE: Dict[str, pd.DataFrame] = {}

def fetch_daily_basic_map(pro, symbols: List[str], trade_date: str) -> Dict[str, Dict]:
    """Batch fetch daily_basic data for all symbols in one call to avoid rate limits.

    优化点:
      1. 尝试使用单次 pro.daily_basic(trade_date=...) 获取当日全部，再过滤 (最少 1 次调用)
      2. 结果缓存到内存，重复调用同一 trade_date 不再打 API
      3. 如果接口返回空或失败，回退到逐个请求（仍限速）
      4. 支持通过环境变量 LIMIT_DAILY_SYMBOLS=N 只拉前 N 个（调试用）
      5. 支持通过 SKIP_DAILY_BASIC=1 完全跳过（节省 quota）
    """
    if os.getenv('SKIP_DAILY_BASIC') == '1':
        return {}

    # 可选限制（调试或快速跑）
    limit_env = os.getenv('LIMIT_DAILY_SYMBOLS')
    if limit_env and limit_env.isdigit():
        n = int(limit_env)
        if n < len(symbols):
            symbols = symbols[:n]

    symbols_set = set(symbols)
    out: Dict[str, Dict] = {}

    # 1) 批量模式
    try:
        if trade_date not in DAILY_BASIC_CACHE:
            # 只要 trade_date，不带 ts_code，返回全量（官方限速更低冲击）
            daily_df = pro.daily_basic(trade_date=trade_date,
                                       fields='ts_code,close,pe_ttm,pb,total_mv,circ_mv,turnover_rate,free_share')
            DAILY_BASIC_CACHE[trade_date] = daily_df if not daily_df.empty else pd.DataFrame()
        cached_df = DAILY_BASIC_CACHE[trade_date]
        if not cached_df.empty:
            subset = cached_df[cached_df['ts_code'].isin(symbols_set)]
            for _, r in subset.iterrows():
                out[r['ts_code']] = {
                    'close': r.get('close'),
                    'pe_ttm': r.get('pe_ttm'),
                    'pb': r.get('pb'),
                    'total_mv': r.get('total_mv'),
                    'circ_mv': r.get('circ_mv'),
                    'turnover_rate': r.get('turnover_rate'),
                    'free_share': r.get('free_share'),
                }
            # 如果已经覆盖大部分，直接返回
            if len(out) >= len(symbols_set) * 0.8:
                return out
            else:
                missing = symbols_set - set(out.keys())
                print(f"[daily_basic] batch covered {len(out)} / {len(symbols_set)}, missing={len(missing)} -> fallback single fetch")
        else:
            print(f"[daily_basic] batch call returned empty for {trade_date}, fallback to per-symbol.")
    except Exception as e:
        print(f"[daily_basic] batch fetch failed: {e} -> fallback per-symbol")

    # 2) 回退逐个模式（保留限速）
    missing_symbols = [s for s in symbols if s not in out]
    for ts_code in missing_symbols:
        try:
            ddf = pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if not ddf.empty:
                r = ddf.iloc[0]
                out[ts_code] = {
                    'close': r.get('close'),
                    'pe_ttm': r.get('pe_ttm'),
                    'pb': r.get('pb'),
                    'total_mv': r.get('total_mv'),
                    'circ_mv': r.get('circ_mv'),
                    'turnover_rate': r.get('turnover_rate'),
                    'free_share': r.get('free_share'),
                }
        except Exception as e:
            print(f"daily_basic error {ts_code}: {e}")
        time.sleep(0.05)  # gentle pacing
    return out


def print_index_full_details(index_alias_or_code: str, limit: Optional[int] = None):
    code = resolve_index_code(index_alias_or_code)
    print(f"Resolving '{index_alias_or_code}' -> {code}")
    meta = pro.index_basic(ts_code=code)
    if meta.empty:
        print(f"No meta found for index {code}")
    else:
        m = meta.iloc[0]
        cols = set(meta.columns.tolist())
        # helper to safely extract a field
        def g(field: str, default: str = ''):
            if field in cols:
                val = m[field]
                return '' if (pd.isna(val)) else str(val)
            return default
        # Some environments / API versions may not return fullname/category/publisher
        ts_code = g('ts_code') or code
        name = g('name')
        fullname = g('fullname') or g('name')
        category = g('category')
        publisher = g('publisher')
        base_date = g('base_date') or g('base_point')
        print(f"Index: {ts_code} | {name} | {fullname} | category={category} publisher={publisher} base_date={base_date}")
        # Optional: show which meta columns are available when debugging missing fields
        missing = [f for f in ['fullname','category','publisher','base_date'] if f not in cols]
        if missing:
            print(f"(Info) Missing meta fields: {', '.join(missing)} | Available: {', '.join(sorted(cols))}")

    weights_df = fetch_index_constituents(pro, code,'20250630')
    if weights_df.empty:
        print(f"No constituents retrieved for {code}")
        return
    symbols = weights_df['con_code'].unique().tolist()
    if limit:
        symbols = symbols[:limit]
        weights_df = weights_df[weights_df['con_code'].isin(symbols)]
    trade_date_used = weights_df.iloc[0]['trade_date'] if not weights_df.empty else ''
    print(f"Constituents count: {len(symbols)} | trade_date={trade_date_used}")

    basic_df = batch_fetch_basic_info(pro, symbols)
    daily_map = fetch_daily_basic_map(pro, symbols, trade_date_used)
    weight_map = weights_df.set_index('con_code')['weight'].to_dict()

    print("ts_code      | name       | industry  | area | listed    | weight  | close    | pe_ttm   | pb    | total_mv    | circ_mv     | turn%")
    print("-" * 120)
    for ts_code in symbols:
        br = basic_df[basic_df['ts_code'] == ts_code]
        name = br.iloc[0]['name'] if not br.empty else ''
        industry = br.iloc[0]['industry'] if not br.empty else ''
        area = br.iloc[0]['area'] if not br.empty else ''
        list_date = br.iloc[0]['list_date'] if not br.empty else ''
        daily = daily_map.get(ts_code, {})
        print(
            f"{ts_code:>10} | {name:<10} | {industry:<9} | {area:<4} | {list_date:<8} | "
            f"{weight_map.get(ts_code, 0):>6.2f} | {str(daily.get('close')):<8} | {str(daily.get('pe_ttm')):<8} | {str(daily.get('pb')):<5} | "
            f"{str(daily.get('total_mv')):<11} | {str(daily.get('circ_mv')):<11} | {str(daily.get('turnover_rate')):<5}"
        )


def print_indices_with_500_keyword():
    df = pro.index_basic(market='CSI')
    subset = df[df['name'].str.contains('500', na=False)]
    if subset.empty:
        print("No indices containing '500' in name")
        return
    print(subset[['ts_code', 'name', 'fullname', 'publisher', 'category']].to_string(index=False))


if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("Usage: python tests/test_index.py <index_alias_or_code> [limit]")
        print("Examples: hs300 star50 a500 000905.SH 000300.SH 000688.SH")
        print("\nIndices containing '500':")
        print_indices_with_500_keyword()
        sys.exit(0)
    idx = sys.argv[1]
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print_index_full_details(idx, limit=lim)