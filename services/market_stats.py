"""Helpers for market-stats series (PE / 两融)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ALLA_CODE = "ALLA.WI"

PE_INDEX_LABELS = {
    ALLA_CODE: "全A（自算）",
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399006.SZ": "创业板指",
    "000985.SH": "中证全指",
}

# ALLA.WI 来自 market_pe_daily；其余来自 Tushare index_dailybasic（000985 通常无数据）。
PE_FALLBACK_CODES = (ALLA_CODE, "000001.SH", "000300.SH", "399006.SZ", "000985.SH")
MARGIN_ALL_EXCHANGE = "ALL"
MARGIN_DELTA_WINDOW = 30


def lookback_start(end: str, trading_days: int) -> str:
    """Calendar-day lookback padded for weekends/holidays."""
    end_dt = datetime.strptime(end, "%Y%m%d")
    pad = max(int(trading_days * 2.2), trading_days + 40)
    return (end_dt - timedelta(days=pad)).strftime("%Y%m%d")


def attach_window_delta(
    rows: Sequence[Dict[str, Any]],
    field: str = "rzrqye",
    window: int = MARGIN_DELTA_WINDOW,
    out_field: str = "delta_30d",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        item = dict(row)
        prev = rows[idx - window] if idx >= window else None
        cur_val = row.get(field)
        prev_val = prev.get(field) if prev is not None else None
        if cur_val is None or prev_val is None:
            item[out_field] = None
        else:
            item[out_field] = float(cur_val) - float(prev_val)
        out.append(item)
    return out


def pick_pe_index(
    available_codes: Iterable[str],
    requested: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    have = {str(code) for code in available_codes if code}
    if requested and requested in have:
        return requested, PE_INDEX_LABELS.get(requested, requested)
    for code in PE_FALLBACK_CODES:
        if code in have:
            return code, PE_INDEX_LABELS.get(code, code)
    return None, None


def pe_index_sort_key(code: str) -> int:
    try:
        return PE_FALLBACK_CODES.index(code)
    except ValueError:
        return len(PE_FALLBACK_CODES)


def merge_pe_by_code(
    index_docs: Sequence[Dict[str, Any]],
    alla_docs: Sequence[Dict[str, Any]] = (),
) -> Dict[str, List[Dict[str, Any]]]:
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for doc in index_docs:
        code = doc.get("ts_code")
        if not code or code == ALLA_CODE:
            continue
        by_code.setdefault(str(code), []).append(doc)
    if alla_docs:
        by_code[ALLA_CODE] = list(alla_docs)
    return by_code


def serialize_pe_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "trade_date": doc.get("trade_date"),
        "pe": _to_float(doc.get("pe")),
        "pe_ttm": _to_float(doc.get("pe_ttm")),
        "pb": _to_float(doc.get("pb")),
        "close": _to_float(doc.get("close")),
        "total_mv": _to_float(doc.get("total_mv")),
    }
    for field in ("n_universe", "n_used", "n_loss", "n_missing", "method", "universe"):
        if field in doc:
            row[field] = doc.get(field)
    return row


def serialize_margin_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trade_date": doc.get("trade_date"),
        "rzye": _to_float(doc.get("rzye")),
        "rqye": _to_float(doc.get("rqye")),
        "rzrqye": _to_float(doc.get("rzrqye")),
        "delta_30d": _to_float(doc.get("delta_30d")),
    }


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
