import pytest

from services.etf_kline import (
    etf_symbol_variants,
    load_etf_kline_rows,
    map_etf_daily_row,
    resample_ohlc_rows,
    resolve_etf_ts_code,
)


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query=None, projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None, limit=0):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            field, direction = sort[0]
            rows.sort(key=lambda row: str(row.get(field) or ""), reverse=direction < 0)
        if limit:
            rows = rows[:limit]
        return rows

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$gte" in expected and not (str(value) >= str(expected["$gte"])):
                    return False
                if "$lte" in expected and not (str(value) <= str(expected["$lte"])):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_etf_symbol_variants_include_exchange_suffixes():
    assert etf_symbol_variants("510300.SH") == ["510300.SH", "510300", "510300.SZ"]
    assert "510300.SH" in etf_symbol_variants("510300")


def test_resolve_etf_ts_code_hits_basic_and_misses_stock():
    basic = _Coll([{"ts_code": "510300.SH", "name": "沪深300ETF"}])
    assert resolve_etf_ts_code(basic, "510300") == "510300.SH"
    assert resolve_etf_ts_code(basic, "000001.SZ") is None
    assert resolve_etf_ts_code(None, "510300.SH") is None


def test_map_etf_daily_row_copies_vol_to_volume():
    row = map_etf_daily_row(
        {
            "ts_code": "510300.SH",
            "trade_date": "20260904",
            "open": "4.6",
            "high": 4.7,
            "low": 4.5,
            "close": 4.616,
            "vol": 1000,
        },
        name="沪深300ETF",
    )
    assert row["symbol"] == "510300.SH"
    assert row["volume"] == pytest.approx(1000)
    assert row["vol"] == pytest.approx(1000)
    assert row["name"] == "沪深300ETF"
    assert row["close"] == pytest.approx(4.616)


def test_load_etf_kline_rows_none_vs_empty():
    basic = _Coll([{"ts_code": "510300.SH", "name": "沪深300ETF"}])
    assert load_etf_kline_rows(basic, _Coll([]), "000001.SZ") is None
    rows = load_etf_kline_rows(basic, _Coll([]), "510300.SH")
    assert rows == []


def test_resample_ohlc_rows_weekly_sums_volume():
    w1 = ["20250331", "20250401", "20250402", "20250403", "20250404"]
    rows = [
        {
            "ts_code": "510300.SH",
            "symbol": "510300.SH",
            "trade_date": td,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 2.0 if td == "20250404" else 1.0,
            "vol": 10.0,
            "volume": 10.0,
        }
        for td in w1
    ]
    weekly = resample_ohlc_rows(rows, "1w")
    assert len(weekly) == 1
    assert weekly[0]["trade_date"] == "20250404"
    assert weekly[0]["close"] == pytest.approx(2.0)
    assert weekly[0]["volume"] == pytest.approx(50.0)
