import pytest

from routers import user_watchlist


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self


def _matches_value(value, expected):
    if isinstance(expected, dict):
        if "$in" in expected:
            return value in expected["$in"]
        if "$lt" in expected:
            return value < expected["$lt"]
        return False
    return value == expected


def _matches(doc, query):
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in expected):
                return False
            continue
        if not _matches_value(doc.get(key), expected):
            return False
    return True


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find_one(self, query, sort=None):
        rows = [doc for doc in self.docs if _matches(doc, query)]
        if sort:
            for key, direction in reversed(sort):
                rows.sort(key=lambda doc: doc.get(key), reverse=direction < 0)
        return rows[0] if rows else None

    def find(self, query=None, projection=None):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])


class _ExplodingCollection(_Collection):
    def aggregate(self, pipeline):
        raise AssertionError("minute_bars should not be queried")


def test_watchlist_realtime_uses_volume_price_snapshot(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260701")
    monkeypatch.setattr(
        user_watchlist,
        "get_db_collections",
        lambda: {
            "user_watchlists": _Collection(
                [{"user_id": "u1", "symbols": ["000001.SZ"]}]
            )
        },
    )
    monkeypatch.setattr(
        user_watchlist,
        "get_data_db",
        lambda: {
            "volume_price": _Collection(
                [
                    {
                        "symbol": "000001.SZ",
                        "ts_code": "000001.SZ",
                        "trade_date": "20260701",
                        "source": "eastmoney_snapshot",
                        "open": 10.0,
                        "high": 10.8,
                        "low": 9.9,
                        "close": 10.5,
                        "pre_close": 10.0,
                        "change": 0.5,
                        "pct_chg": 5.0,
                        "volume": 12345,
                        "update_time": "20260701 10:15:00",
                    }
                ]
            ),
            "stock_info": _Collection(
                [{"symbol": "000001.SZ", "name": "平安银行", "industry": "银行"}]
            ),
            "etf_basic": _Collection([]),
            "etf_daily": _Collection([]),
            "sw_index_member": _Collection([]),
            "minute_bars": _ExplodingCollection([]),
        },
    )

    response = user_watchlist.get_user_watchlist_realtime({"id": "u1"})

    assert response["success"] is True
    assert response["data"] == [
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "industry": "银行",
            "sw_industry": None,
            "asset_type": "stock",
            "price": 10.5,
            "open": 10.0,
            "high": 10.8,
            "low": 9.9,
            "volume": 12345,
            "change": 0.5,
            "change_pct": 5.0,
            "update_time": "2026-07-01 10:15:00",
        }
    ]
