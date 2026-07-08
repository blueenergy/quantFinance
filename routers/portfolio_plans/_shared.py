"""Shared pure helpers for portfolio plan routers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId


CN_TZ = ZoneInfo("Asia/Shanghai")
RISK_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _beijing_now() -> datetime:
    return datetime.now(CN_TZ)


def _business_today_yyyymmdd() -> str:
    return _beijing_now().strftime("%Y%m%d")


def _business_today_date():
    return _beijing_now().date()


def _datetime_as_beijing_date(value: datetime):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ).date()


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _normalize_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _risk_severity(value: Any) -> str:
    severity = str(value or "none").lower()
    return severity if severity in RISK_SEVERITY_ORDER else "none"


def _risk_severity_at_least(value: Any, minimum: str) -> bool:
    return RISK_SEVERITY_ORDER[_risk_severity(value)] >= RISK_SEVERITY_ORDER[_risk_severity(minimum)]


def _chunked(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _symbol_key(value: Any) -> str:
    return str(value or "").split(".", 1)[0]


def _symbol_query_values(value: Any) -> List[str]:
    raw = str(value or "")
    base = _symbol_key(raw)
    values = {raw, base}
    if base:
        values.update({f"{base}.SH", f"{base}.SZ"})
    return [item for item in values if item]


def _signal_filled_qty(row: Dict[str, Any]) -> int:
    filled = _int_value(row.get("filled_qty", row.get("filled_size", 0)))
    if filled == 0 and str(row.get("status") or "").lower() == "filled":
        return abs(_int_value(row.get("size")))
    return abs(filled)


def _signal_signed_filled_qty(row: Dict[str, Any]) -> int:
    qty = _signal_filled_qty(row)
    action = str(row.get("action") or row.get("side") or "").lower()
    if action in {"sell", "s"}:
        return -qty
    return qty
