from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional


CHINA_TZ = timezone(timedelta(hours=8))


def _datetime_from_value(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.astimezone(CHINA_TZ) if value.tzinfo else value.replace(tzinfo=CHINA_TZ)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, CHINA_TZ)


def add_display_time_fields(doc: Dict[str, Any], *, timestamp_field: str = "timestamp") -> Dict[str, Any]:
    """Add human-friendly time fields without changing the raw timestamp."""

    display_dt = _datetime_from_value(doc.get(timestamp_field))
    if display_dt is None:
        datetime_value = doc.get("datetime")
        if isinstance(datetime_value, str) and datetime_value:
            doc.setdefault("execution_time", datetime_value)
            return doc
        display_dt = _datetime_from_value(datetime_value)

    if display_dt is None:
        return doc

    doc.setdefault("execution_time", display_dt.strftime("%Y-%m-%d %H:%M:%S"))
    doc.setdefault("timestamp_iso", display_dt.isoformat())
    doc.setdefault("date_display", display_dt.strftime("%Y-%m-%d"))
    return doc
