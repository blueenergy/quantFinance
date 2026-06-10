import pytest

from routers.stock_workbench import _attach_display_pct_change


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query=None, projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
        return rows[0] if rows else None

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$lt" in expected and not (value < expected["$lt"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_display_pct_change_uses_close_against_daily_pre_close():
    data = {
        "volume_price": _Collection(
            [{"symbol": "000001.SZ", "trade_date": "20260610", "pre_close": 10.0}]
        )
    }
    quote = {
        "symbol": "000001.SZ",
        "trade_date": "20260610",
        "close": 10.04113,
        "pct_chg": 9.99,
    }

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(0.4113)
    assert normalized["display_pre_close"] == pytest.approx(10.0)
    assert normalized["display_pct_chg_source"] == "daily.pre_close"


def test_display_pct_change_falls_back_to_previous_daily_close():
    data = {
        "volume_price": _Collection(
            [{"symbol": "000001.SZ", "trade_date": "20260609", "close": 10.0}]
        )
    }
    quote = {
        "symbol": "000001.SZ",
        "trade_date": "20260610",
        "close": 10.5,
        "pct_chg": 0.1,
    }

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(5.0)
    assert normalized["display_pct_chg_source"] == "previous_daily.close"


def test_display_pct_change_uses_pct_field_when_pre_close_unavailable():
    data = {"volume_price": _Collection([])}
    quote = {"symbol": "000001.SZ", "trade_date": "20260610", "close": 10.5, "pct_chg": 0.41}

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(0.41)
    assert normalized["display_pct_chg_source"] == "quote_pct_field"
