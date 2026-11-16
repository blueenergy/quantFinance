"""ETF OHLC helpers for watchlist / workbench / kline-series.

ETF bars live in ``etf_daily`` (``ts_code``) with factors in ``etf_adj_factor``.
They must not be read from ``volume_price``: that collection is A-share daily
and may contain a stale one-bar Eastmoney leftover for a few ETF codes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def etf_symbol_variants(symbol: str) -> List[str]:
    s = (symbol or "").strip().upper()
    if not s:
        return []
    raw = s.split(".", 1)[0]
    out: List[str] = []
    for item in (s, raw, f"{raw}.SH", f"{raw}.SZ"):
        if item and item not in out:
            out.append(item)
    return out


def _find_one(coll: Any, query: Dict[str, Any], projection: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    if coll is None:
        return None
    try:
        if projection is not None:
            return coll.find_one(query, projection)
        return coll.find_one(query)
    except TypeError:
        return coll.find_one(query)


def resolve_etf_basic(etf_basic_col: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """Return ``{ts_code, name}`` when ``etf_basic`` recognizes the symbol."""
    variants = etf_symbol_variants(symbol)
    if etf_basic_col is None or not variants:
        return None
    proj = {"_id": 0, "ts_code": 1, "name": 1}
    doc = _find_one(etf_basic_col, {"ts_code": {"$in": variants}}, proj)
    if not doc or not doc.get("ts_code"):
        for variant in variants:
            doc = _find_one(etf_basic_col, {"ts_code": variant}, proj)
            if doc and doc.get("ts_code"):
                break
        else:
            doc = None
    if not doc or not doc.get("ts_code"):
        return None
    ts_code = str(doc["ts_code"]).strip().upper()
    name = str(doc.get("name") or "").strip()
    return {"ts_code": ts_code, "name": name}


def resolve_etf_ts_code(etf_basic_col: Any, symbol: str) -> Optional[str]:
    basic = resolve_etf_basic(etf_basic_col, symbol)
    return basic["ts_code"] if basic else None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        from bson.decimal128 import Decimal128

        if isinstance(value, Decimal128):
            value = float(str(value))
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _ymd(value: Any) -> str:
    text = str(value or "").replace("-", "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else text


def map_etf_daily_row(
    doc: Dict[str, Any],
    *,
    symbol: Optional[str] = None,
    name: str = "",
) -> Dict[str, Any]:
    """Normalize an ``etf_daily`` document to the stock K-line row shape."""
    ts_code = str(doc.get("ts_code") or symbol or "").strip().upper()
    volume = _to_float(doc.get("volume"))
    if volume is None:
        volume = _to_float(doc.get("vol"))
    row = {
        "symbol": ts_code,
        "ts_code": ts_code,
        "trade_date": _ymd(doc.get("trade_date")),
        "open": _to_float(doc.get("open")),
        "high": _to_float(doc.get("high")),
        "low": _to_float(doc.get("low")),
        "close": _to_float(doc.get("close")),
        "pre_close": _to_float(doc.get("pre_close")),
        "vol": volume,
        "volume": volume,
        "amount": _to_float(doc.get("amount")),
        "pct_chg": _to_float(doc.get("pct_chg") if doc.get("pct_chg") is not None else doc.get("pct_change")),
        "name": name or str(doc.get("name") or ""),
    }
    pct = row["pct_chg"]
    if pct is not None:
        row["pct_change"] = pct
    return row


def load_etf_daily_docs(
    etf_daily_col: Any,
    ts_code: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Newest-first ``etf_daily`` rows for ``ts_code``."""
    if etf_daily_col is None or not ts_code:
        return []
    query: Dict[str, Any] = {"ts_code": ts_code}
    date_flt: Dict[str, str] = {}
    if start:
        date_flt["$gte"] = str(start).replace("-", "")[:8]
    if end:
        date_flt["$lte"] = str(end).replace("-", "")[:8]
    if date_flt:
        query["trade_date"] = date_flt
    cap = max(1, int(limit))
    try:
        cursor = etf_daily_col.find(query, {"_id": 0}, sort=[("trade_date", -1)], limit=cap)
        return list(cursor)
    except TypeError:
        cursor = etf_daily_col.find(query, {"_id": 0})
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("trade_date", -1)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(cap)
        docs = list(cursor)
        docs.sort(key=lambda row: str(row.get("trade_date") or ""), reverse=True)
        return docs[:cap]




def load_etf_kline_rows(
    etf_basic_col: Any,
    etf_daily_col: Any,
    symbol: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 500,
) -> Optional[List[Dict[str, Any]]]:
    """Return mapped daily rows if ``symbol`` is an ETF, else ``None``.

    An empty list means it is an ETF but ``etf_daily`` has no bars — callers
    must not fall through to ``volume_price``.
    """
    basic = resolve_etf_basic(etf_basic_col, symbol)
    if not basic:
        return None
    ts_code = basic["ts_code"]
    docs = load_etf_daily_docs(
        etf_daily_col,
        ts_code,
        start=start,
        end=end,
        limit=limit,
    )
    name = basic.get("name") or ""
    return [map_etf_daily_row(doc, symbol=ts_code, name=name) for doc in docs]
