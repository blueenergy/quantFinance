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


def test_pick_best_intraday_prefers_miniqmt_over_eastmoney():
    eastmoney = {"source": "eastmoney_snapshot", "close": 10.2}
    miniqmt = {"source": "miniqmt", "close": 10.1}
    tushare = {"source": "tushare_daily", "close": 10.0}

    picked = user_watchlist._pick_best_intraday_from_today_docs(
        [eastmoney, tushare, miniqmt]
    )

    assert picked is miniqmt


def test_pick_latest_vp_doc_same_date_prefers_miniqmt():
    eastmoney = {
        "source": "eastmoney_snapshot",
        "trade_date": "20260904",
        "close": 10.2,
    }
    tushare = {"source": "tushare_daily", "trade_date": "20260904", "close": 10.0}
    miniqmt = {"source": "miniqmt", "trade_date": "20260904", "close": 10.1}

    picked = user_watchlist._pick_latest_vp_doc([eastmoney, tushare, miniqmt])

    assert picked is miniqmt


def test_watchlist_realtime_stock_prefers_miniqmt_over_eastmoney(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260904")
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
                        "trade_date": "20260904",
                        "source": "eastmoney_snapshot",
                        "open": 9.9,
                        "high": 10.0,
                        "low": 9.8,
                        "close": 9.95,
                        "pre_close": 9.9,
                        "change": 0.05,
                        "pct_chg": 0.51,
                        "volume": 1,
                        "update_time": "20260904 09:35:00",
                    },
                    {
                        "symbol": "000001.SZ",
                        "ts_code": "000001.SZ",
                        "trade_date": "20260904",
                        "source": "miniqmt",
                        "open": 10.1,
                        "high": 10.8,
                        "low": 10.0,
                        "close": 10.5,
                        "pre_close": 10.0,
                        "change": 0.5,
                        "pct_chg": 5.0,
                        "vol": 12345,
                        "update_time": "20260904 14:59:00",
                    },
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
    row = response["data"][0]
    assert row["price"] == 10.5
    assert row["open"] == 10.1
    assert row["high"] == 10.8
    assert row["volume"] == 12345
    assert row["update_time"] == "2026-09-04 14:59:00"


class _CountingCollection(_Collection):
    def __init__(self, docs=None):
        super().__init__(docs)
        self.find_calls = 0
        self.find_one_calls = 0

    def find(self, query=None, projection=None):
        self.find_calls += 1
        return super().find(query, projection)

    def find_one(self, query, sort=None):
        self.find_one_calls += 1
        return super().find_one(query, sort)


def test_batch_latest_etf_daily_close_map_uses_bounded_find_not_full_history():
    docs = [
        {"ts_code": "510300.SH", "trade_date": f"2020{month:02d}01", "close": float(month)}
        for month in range(1, 13)
    ]
    docs.extend(
        {"ts_code": "510300.SH", "trade_date": f"2021{month:02d}01", "close": float(month)}
        for month in range(1, 13)
    )
    docs.append({"ts_code": "510300.SH", "trade_date": "20260701", "close": 4.25})
    col = _CountingCollection(docs)

    result = user_watchlist._batch_latest_etf_daily_close_map(
        col,
        ["510300.SH"],
        {"510300.SH": {"etf_ts": "510300.SH"}},
        today="20260701",
    )

    assert result == {"510300.SH": 4.25}
    assert col.find_calls == 1
    assert col.find_one_calls == 0


def test_watchlist_realtime_etf_keeps_three_price_decimals(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260701")
    monkeypatch.setattr(
        user_watchlist,
        "get_db_collections",
        lambda: {
            "user_watchlists": _Collection(
                [{"user_id": "u1", "symbols": ["510300.SH"]}]
            )
        },
    )
    monkeypatch.setattr(
        user_watchlist,
        "get_data_db",
        lambda: {
            "volume_price": _Collection([]),
            "stock_info": _Collection([]),
            "etf_basic": _Collection(
                [{"ts_code": "510300.SH", "name": "沪深300ETF"}]
            ),
            "etf_daily": _Collection(
                [
                    {
                        "ts_code": "510300.SH",
                        "trade_date": "20260630",
                        "open": 1.230,
                        "high": 1.240,
                        "low": 1.228,
                        "close": 1.229,
                        "vol": 100,
                    },
                    {
                        "ts_code": "510300.SH",
                        "trade_date": "20260701",
                        "open": 1.231,
                        "high": 1.238,
                        "low": 1.229,
                        "close": 1.234,
                        "vol": 200,
                    },
                ]
            ),
            "sw_index_member": _Collection([]),
            "minute_bars": _ExplodingCollection([]),
        },
    )

    response = user_watchlist.get_user_watchlist_realtime({"id": "u1"})

    assert response["success"] is True
    row = response["data"][0]
    assert row["asset_type"] == "etf"
    assert row["price"] == 1.234
    assert row["open"] == 1.231
    assert row["high"] == 1.238
    assert row["low"] == 1.229
    assert row["change"] == 0.005
    assert row["change_pct"] == 0.41


def test_watchlist_realtime_stock_uses_latest_daily_bar_on_weekend(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260905")
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
                        "trade_date": "20260904",
                        "source": "tushare_daily",
                        "open": 10.1,
                        "high": 10.8,
                        "low": 10.0,
                        "close": 10.5,
                        "pre_close": 10.0,
                        "change": 0.5,
                        "pct_chg": 5.0,
                        "vol": 12345,
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
            "open": 10.1,
            "high": 10.8,
            "low": 10.0,
            "volume": 12345,
            "change": 0.5,
            "change_pct": 5.0,
            "update_time": "2026-09-04",
        }
    ]


def test_watchlist_realtime_mixed_list_uses_one_find_per_collection(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260701")
    volume_price = _CountingCollection(
        [
            {
                "symbol": "000001.SZ",
                "ts_code": "000001.SZ",
                "trade_date": "20260701",
                "source": "miniqmt",
                "open": 10.1,
                "high": 10.8,
                "low": 10.0,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "pct_chg": 5.0,
                "vol": 12345,
                "update_time": "20260701 14:59:00",
            }
        ]
    )
    etf_basic = _CountingCollection(
        [{"ts_code": "510300.SH", "name": "沪深300ETF"}]
    )
    etf_daily = _CountingCollection(
        [
            {
                "ts_code": "510300.SH",
                "trade_date": "20260630",
                "open": 1.230,
                "high": 1.240,
                "low": 1.228,
                "close": 1.229,
                "vol": 100,
            },
            {
                "ts_code": "510300.SH",
                "trade_date": "20260701",
                "open": 1.231,
                "high": 1.238,
                "low": 1.229,
                "close": 1.234,
                "vol": 200,
            },
        ]
    )
    stock_info = _CountingCollection(
        [{"symbol": "000001.SZ", "name": "平安银行", "industry": "银行"}]
    )
    sw_index_member = _CountingCollection([])

    monkeypatch.setattr(
        user_watchlist,
        "get_db_collections",
        lambda: {
            "user_watchlists": _Collection(
                [{"user_id": "u1", "symbols": ["000001.SZ", "510300.SH"]}]
            )
        },
    )
    monkeypatch.setattr(
        user_watchlist,
        "get_data_db",
        lambda: {
            "volume_price": volume_price,
            "stock_info": stock_info,
            "etf_basic": etf_basic,
            "etf_daily": etf_daily,
            "sw_index_member": sw_index_member,
            "minute_bars": _ExplodingCollection([]),
        },
    )

    response = user_watchlist.get_user_watchlist_realtime({"id": "u1"})

    assert response["success"] is True
    assert [row["symbol"] for row in response["data"]] == ["000001.SZ", "510300.SH"]
    assert response["data"][0]["price"] == 10.5
    assert response["data"][1]["price"] == 1.234
    assert volume_price.find_calls == 1
    assert volume_price.find_one_calls == 0
    assert etf_basic.find_calls == 1
    assert etf_daily.find_calls == 1
    assert etf_daily.find_one_calls == 0
    assert stock_info.find_calls == 1
    assert stock_info.find_one_calls == 0

