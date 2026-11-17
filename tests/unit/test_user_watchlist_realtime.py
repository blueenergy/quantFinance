from datetime import datetime

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

    def aggregate(self, pipeline):
        """Same-time volume aggregation is stubbed out in router tests."""
        return _Cursor([])


def test_watchlist_realtime_uses_volume_price_snapshot(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260701")
    monkeypatch.setattr(
        user_watchlist,
        "_session_volume_clock",
        lambda now=None: {"phase": "pre", "elapsed": 0, "cutoff": None},
    )
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
            "minute_bars": _Collection([]),
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
            "volume_ratio": None,
            "volume_ratio_basis": None,
            "volume_ratio_same_time": None,
            "volume_ratio_traditional": None,
            "volume_ratio_prev_day": None,
            "volume_ratio_avg5": None,
            "volume_ratio_samples": 0,
            "volume_ratio_at": None,
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
            "minute_bars": _Collection([]),
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
            "minute_bars": _Collection([]),
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
        "_session_volume_clock",
        lambda now=None: {"phase": "pre", "elapsed": 0, "cutoff": None},
    )
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
            "minute_bars": _Collection([]),
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
            "volume_ratio": None,
            "volume_ratio_basis": None,
            "volume_ratio_same_time": None,
            "volume_ratio_traditional": None,
            "volume_ratio_prev_day": None,
            "volume_ratio_avg5": None,
            "volume_ratio_samples": 0,
            "volume_ratio_at": None,
        }
    ]


def test_watchlist_realtime_mixed_list_uses_bounded_finds_per_collection(monkeypatch):
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20260701")
    monkeypatch.setattr(
        user_watchlist,
        "_session_volume_clock",
        lambda now=None: {"phase": "am", "elapsed": 75, "cutoff": "1045"},
    )
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
            "minute_bars": _Collection([]),
        },
    )

    response = user_watchlist.get_user_watchlist_realtime({"id": "u1"})

    assert response["success"] is True
    assert [row["symbol"] for row in response["data"]] == ["000001.SZ", "510300.SH"]
    assert response["data"][0]["price"] == 10.5
    assert response["data"][1]["price"] == 1.234
    # One bounded recent-window find for the quote/latest rows, plus one for the
    # five completed daily volumes used by the traditional 量比 fallback.
    assert volume_price.find_calls == 2
    assert volume_price.find_one_calls == 0
    assert etf_basic.find_calls == 1
    assert etf_daily.find_calls == 2
    assert etf_daily.find_one_calls == 0
    assert stock_info.find_calls == 1
    assert stock_info.find_one_calls == 0


def test_session_volume_clock_phases():
    def clock(hour, minute):
        return user_watchlist._session_volume_clock(
            datetime(2026, 11, 17, hour, minute, tzinfo=user_watchlist._CN_TZ)
        )

    assert clock(9, 10) == {"phase": "pre", "elapsed": 0, "cutoff": None}
    assert clock(9, 35) == {"phase": "am", "elapsed": 5, "cutoff": "0935"}
    assert clock(11, 0) == {"phase": "am", "elapsed": 90, "cutoff": "1100"}
    assert clock(11, 45) == {"phase": "lunch", "elapsed": 120, "cutoff": "1130"}
    assert clock(13, 30) == {"phase": "pm", "elapsed": 150, "cutoff": "1330"}
    assert clock(15, 30) == {"phase": "closed", "elapsed": 240, "cutoff": "1500"}


def test_same_time_stats_median_prev_and_avg5():
    stats = user_watchlist._same_time_stats(
        {f"202610{i:02d}": float(i) for i in range(1, 21)}, "20261117"
    )
    assert stats == {"samples": 20, "median": 10.5, "prev": 20.0, "avg5": 18.0}


def test_same_time_stats_excludes_today_from_the_baseline():
    """Today's cumulative volume is the numerator, never part of the baseline."""
    stats = user_watchlist._same_time_stats(
        {f"202610{i:02d}": float(i) for i in range(1, 21)} | {"20261117": 999.0},
        "20261117",
    )
    assert stats["samples"] == 20
    assert stats["median"] == 10.5


def test_same_time_stats_needs_ten_samples_for_median():
    stats = user_watchlist._same_time_stats(
        {f"202610{i:02d}": float(i) for i in range(1, 10)}, "20261117"
    )
    assert stats["samples"] == 9
    assert stats["median"] is None
    assert stats["prev"] == 9.0
    assert stats["avg5"] == 7.0


def test_traditional_volume_ratio_matches_exchange_formula():
    # 600600 2026-11-17 10:45: today's cumulative 32,959 shares; the previous
    # five completed daily volumes.  Expected ~1.82x on a 240-minute day.
    ratio = user_watchlist._traditional_volume_ratio(
        32959.0,
        [59253.0, 73939.0, 55559.0, 48278.0, 52706.0],
        75,
    )
    assert ratio == pytest.approx(1.82, abs=0.02)


def _realtime_response(
    monkeypatch,
    *,
    symbol,
    volume,
    update_time,
    same_days,
    daily_volumes,
):
    user_watchlist._volume_ratio_cache.clear()
    monkeypatch.setattr(user_watchlist, "_cn_calendar_today_yyyymmdd", lambda: "20261117")
    monkeypatch.setattr(
        user_watchlist,
        "_session_volume_clock",
        lambda now=None: {"phase": "am", "elapsed": 75, "cutoff": "1045"},
    )
    monkeypatch.setattr(
        user_watchlist, "_query_same_time_volume_days", lambda *args, **kwargs: same_days
    )
    monkeypatch.setattr(
        user_watchlist, "_query_recent_daily_volumes", lambda *args, **kwargs: daily_volumes
    )
    monkeypatch.setattr(
        user_watchlist,
        "get_db_collections",
        lambda: {
            "user_watchlists": _Collection(
                [{"user_id": "u1", "symbols": [symbol]}]
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
                        "symbol": symbol,
                        "ts_code": symbol,
                        "trade_date": "20261117",
                        "source": "miniqmt",
                        "open": 53.2,
                        "high": 54.33,
                        "low": 53.02,
                        "close": 54.02,
                        "pre_close": 53.33,
                        "change": 0.69,
                        "pct_chg": 1.29,
                        "volume": volume,
                        "update_time": update_time,
                    }
                ]
            ),
            "stock_info": _Collection(
                [{"symbol": symbol, "name": "测试", "industry": "食品"}]
            ),
            "etf_basic": _Collection([]),
            "etf_daily": _Collection([]),
            "sw_index_member": _Collection([]),
            "minute_bars": _Collection([]),
        },
    )
    return user_watchlist.get_user_watchlist_realtime({"id": "u1"})


def test_watchlist_realtime_prefers_same_time_volume_ratio(monkeypatch):
    same_days = {
        "000001.SZ": {
            "20261117": 32959.0,
            "20261116": 24901.0,
            "20261113": 24000.0,
            "20261112": 25000.0,
            "20261111": 24500.0,
            "20261110": 24800.0,
            "20261109": 25200.0,
            "20261106": 24600.0,
            "20261105": 25050.0,
            "20261104": 24400.0,
            "20261103": 24950.0,
            "20261031": 25100.0,
            "20261030": 24700.0,
        }
    }
    response = _realtime_response(
        monkeypatch,
        symbol="000001.SZ",
        volume=32959.0,
        update_time="2026-11-17 10:45:00",
        same_days=same_days,
        daily_volumes={"000001.SZ": [59253.0, 73939.0, 55559.0, 48278.0, 52706.0]},
    )

    row = response["data"][0]
    assert row["volume_ratio_basis"] == "same_time"
    assert row["volume_ratio"] == pytest.approx(1.33, abs=0.01)
    assert row["volume_ratio_same_time"] == pytest.approx(1.33, abs=0.01)
    assert row["volume_ratio_traditional"] == pytest.approx(1.82, abs=0.02)
    assert row["volume_ratio_samples"] == 12
    assert row["volume_ratio_at"] == "10:45"


def test_watchlist_realtime_numerator_uses_minute_bars_not_snapshot(monkeypatch):
    """The numerator comes from minute bars at the cutoff, not the snapshot.

    The realtime snapshot lags the minute bridge by a few minutes; mixing the
    two would bias the ratio.
    """
    same_days = {
        "000001.SZ": {
            "20261117": 30000.0,
            "20261116": 20000.0,
            "20261113": 20000.0,
            "20261112": 20000.0,
            "20261111": 20000.0,
            "20261110": 20000.0,
            "20261109": 20000.0,
            "20261106": 20000.0,
            "20261105": 20000.0,
            "20261104": 20000.0,
            "20261103": 20000.0,
            "20261031": 20000.0,
        }
    }
    response = _realtime_response(
        monkeypatch,
        symbol="000001.SZ",
        volume=99999.0,
        update_time="2026-11-17 10:45:00",
        same_days=same_days,
        daily_volumes={"000001.SZ": [100000.0] * 5},
    )

    row = response["data"][0]
    assert row["volume_ratio"] == pytest.approx(1.5, abs=0.01)


def test_watchlist_realtime_falls_back_to_traditional_ratio(monkeypatch):
    same_days = {
        "000002.SZ": {
            "20261117": 20000.0,
            "20261116": 20000.0,
            "20261113": 21000.0,
            "20261112": 19000.0,
        }
    }
    response = _realtime_response(
        monkeypatch,
        symbol="000002.SZ",
        volume=20000.0,
        update_time="2026-11-17 10:45:00",
        same_days=same_days,
        daily_volumes={"000002.SZ": [100000.0] * 5},
    )

    row = response["data"][0]
    assert row["volume_ratio_basis"] == "traditional"
    assert row["volume_ratio"] == pytest.approx(0.64, abs=0.01)
    assert row["volume_ratio_same_time"] is None
    assert row["volume_ratio_samples"] == 3

