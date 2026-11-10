"""Trading-calendar helpers must tolerate is_open int/string and closed rows."""

from services.trading_calendar import is_sse_open_day, latest_sse_open_day


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query=None, projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            rows = _Cursor(rows).sort(sort)
        return rows[0] if rows else None

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$lte" in expected and not (value <= expected["$lte"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_is_sse_open_day_accepts_int_and_string_flags():
    calendar = _Collection(
        [
            {"exchange": "SSE", "trade_date": "20261110", "is_open": 1},
            {"exchange": "SSE", "trade_date": "20261111", "is_open": "1"},
            {"exchange": "SSE", "trade_date": "20261112", "is_open": "0"},
        ]
    )

    assert is_sse_open_day(calendar, "20261110") is True
    assert is_sse_open_day(calendar, "20261111") is True
    assert is_sse_open_day(calendar, "20261112") is False
    assert is_sse_open_day(calendar, "20261113") is False


def test_latest_sse_open_day_ignores_closed_rows():
    calendar = _Collection(
        [
            {"exchange": "SSE", "trade_date": "20261106", "is_open": "1"},
            {"exchange": "SSE", "trade_date": "20261107", "is_open": "0"},
            {"exchange": "SSE", "trade_date": "20261108", "is_open": "0"},
            {"exchange": "SSE", "trade_date": "20261110", "is_open": "1"},
        ]
    )

    assert latest_sse_open_day(calendar, "20261110") == "20261110"
    assert latest_sse_open_day(calendar, "20261109") == "20261106"
    assert latest_sse_open_day(calendar, "20260101") is None
