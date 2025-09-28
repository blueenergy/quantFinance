"""Tushare cached access layer.

Goals:
- Reduce repeated API calls (rate limit & latency)
- Provide simple in-memory (per-process) daily caches for relatively static datasets
- Offer helper functions used by service layer to batch fetch instead of per-symbol calls

NOTE: This is a lightweight cache. For multi-process / container scale-out, consider
adding a persistent cache (Mongo / Redis) keyed by (resource, date).
"""
from __future__ import annotations
import os
import time
from typing import Dict, Any, Optional, Tuple
from functools import lru_cache

# Simple module-level caches with (data, timestamp)
_CACHE: Dict[str, Tuple[Any, float]] = {}
_DEFAULT_TTL = 60 * 60  # 1 hour default for intraday queries
_DAY_TTL = 60 * 60 * 6  # 6 hours for full universe data

class TushareNotConfigured(Exception):
    pass

# -----------------------------
# Core helpers
# -----------------------------

def _get_token() -> str:
    ts_token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not ts_token:
        raise TushareNotConfigured("Missing TUSHARE_TOKEN env variable")
    return ts_token

@lru_cache(maxsize=1)
def _get_pro():  # type: ignore
    import tushare as ts
    token = _get_token()
    ts.set_token(token)
    return ts.pro_api()

def _cache_get(key: str, ttl: int):
    now = time.time()
    entry = _CACHE.get(key)
    if not entry:
        return None
    data, ts_saved = entry
    if now - ts_saved > ttl:
        return None
    return data

def _cache_set(key: str, data: Any):
    _CACHE[key] = (data, time.time())

# -----------------------------
# Public cached fetchers
# -----------------------------

def get_latest_trade_date() -> Optional[str]:
    """Return latest open trade date (YYYYMMDD). Cached for 1 hour."""
    key = "trade_calendar_latest"
    cached = _cache_get(key, _DEFAULT_TTL)
    if cached:
        return cached
    try:
        pro = _get_pro()
        from datetime import datetime, timedelta
        today = datetime.now().strftime('%Y%m%d')
        # Fetch last 30 calendar entries to find recent open days
        start = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=today)
        if cal is None or cal.empty:
            return None
        open_days = cal[cal['is_open'] == 1].sort_values('cal_date')
        if open_days.empty:
            return None
        latest = open_days.iloc[-1]['cal_date']
        _cache_set(key, latest)
        return latest
    except Exception:
        return None

def get_index_weight(real_code: str, trade_date: str) -> Any:
    """Fetch index weight for a specific date; if empty, attempt fallback to latest trade date.
    Cached per (index_code, trade_date) for 1 hour."""
    key = f"index_weight:{real_code}:{trade_date}"
    cached = _cache_get(key, _DEFAULT_TTL)
    if cached is not None:
        return cached
    pro = _get_pro()
    df = pro.index_weight(index_code=real_code, trade_date=trade_date)
    if df is None or df.empty:
        # fallback to latest
        latest = get_latest_trade_date()
        if latest and latest != trade_date:
            df2 = pro.index_weight(index_code=real_code, trade_date=latest)
            if df2 is not None and not df2.empty:
                df = df2
    _cache_set(key, df)
    return df

def get_stock_basic_map() -> Dict[str, Dict[str, Any]]:
    """Return mapping ts_code -> { name, industry } (industry may be blank).
    Cached for 6 hours.
    """
    key = "stock_basic_all"
    cached = _cache_get(key, _DAY_TTL)
    if cached is not None:
        return cached
    pro = _get_pro()
    df = pro.stock_basic(exchange='', list_status='L')
    mapping: Dict[str, Dict[str, Any]] = {}
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            ts_code = row.get('ts_code')
            if not ts_code:
                continue
            mapping[ts_code] = {
                'name': row.get('name', ''),
                'industry': row.get('industry', '')
            }
    _cache_set(key, mapping)
    return mapping

def get_daily_basic_map(trade_date: str) -> Dict[str, Dict[str, Any]]:
    """Return mapping ts_code -> row data for a single trade_date.
    Cached per date for 1 hour.
    """
    key = f"daily_basic:{trade_date}"
    cached = _cache_get(key, _DEFAULT_TTL)
    if cached is not None:
        return cached
    pro = _get_pro()
    df = pro.daily_basic(trade_date=trade_date, fields='ts_code,total_mv')
    mapping: Dict[str, Dict[str, Any]] = {}
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            ts_code = row.get('ts_code')
            if not ts_code:
                continue
            mapping[ts_code] = {
                'total_mv': row.get('total_mv', 0)
            }
    _cache_set(key, mapping)
    return mapping

# Convenience util to clear cache (e.g. in tests)

def clear_cache():
    _CACHE.clear()
    _get_pro.cache_clear()  # type: ignore
