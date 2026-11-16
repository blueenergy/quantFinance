"""Unit tests for routers.etf (Mongo faked via monkeypatch; no real DB)."""
import pytest
from fastapi.testclient import TestClient
from bson.decimal128 import Decimal128

from main import app
from routers import etf as etf_mod


class ChainCursor(list):
    """Mimics PyMongo cursor chain: find → sort → skip → limit."""

    def sort(self, field, direction=-1):
        rev = direction == -1
        keyf = lambda r: r.get(field, 0)
        data = sorted(self, key=keyf, reverse=rev)
        self.clear()
        self.extend(data)
        return self

    def skip(self, n):
        rest = list(self)[n:]
        self.clear()
        self.extend(rest)
        return self

    def limit(self, n):
        head = list(self)[:n]
        self.clear()
        self.extend(head)
        return self


class EmptyCollection:
    def find_one(self, query=None, sort=None, projection=None):
        return None

    def find(self, query=None, projection=None):
        return ChainCursor([])

    def count_documents(self, query=None):
        return 0


class ShareSizeCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query=None, sort=None, projection=None):
        if not self.docs:
            return None
        if not query:
            best = max(self.docs, key=lambda d: str(d.get("trade_date", "")))
            return {"trade_date": best.get("trade_date")}
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    def count_documents(self, query):
        return len([d for d in self.docs if all(d.get(k) == v for k, v in query.items())])

    def find(self, query, projection=None):
        filtered = [dict(d) for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return ChainCursor(filtered)


class BasicCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    def find(self, query=None, projection=None):
        if not query:
            return ChainCursor([dict(d) for d in self.docs])
        if "ts_code" in query and isinstance(query["ts_code"], dict) and "$in" in query["ts_code"]:
            inc = set(query["ts_code"]["$in"])
            return ChainCursor([dict(d) for d in self.docs if d.get("ts_code") in inc])
        if "$or" in query:
            def matches_clause(doc, clause):
                for key, expected in clause.items():
                    value = doc.get(key)
                    if hasattr(expected, "search"):
                        return bool(expected.search(str(value or "")))
                    if isinstance(expected, dict) and "$in" in expected:
                        return value in expected["$in"]
                    return value == expected
                return False

            return ChainCursor([
                dict(d)
                for d in self.docs
                if any(matches_clause(d, clause) for clause in query["$or"])
            ])
        filtered = [dict(d) for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return ChainCursor(filtered)


class DailyCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query=None, projection=None):
        q = query or {}
        rows = [dict(d) for d in self.docs if all(d.get(k) == v for k, v in q.items() if k != "trade_date")]
        if "trade_date" in q and isinstance(q["trade_date"], dict):
            # minimal range support not needed for unit test path
            rows = [dict(d) for d in self.docs if d.get("ts_code") == q.get("ts_code")]
        return ChainCursor(rows)


def _make_data_db(mapping):
    class _DB(dict):
        def __missing__(self, key):
            return EmptyCollection()

    return _DB(mapping)


@pytest.mark.unit
def test_etf_list_invalid_sort_returns_400():
    client = TestClient(app)
    r = client.get("/api/etf/list", params={"sort": "not_a_sort"})
    assert r.status_code == 400
    detail = str(r.json().get("detail", "")).lower()
    assert "sort" in detail and ("total_size" in detail or "total_share" in detail)


@pytest.mark.unit
def test_etf_list_sort_empty_share_size_returns_message(monkeypatch):
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db({"etf_share_size": ShareSizeCollection([]), "etf_basic": BasicCollection([])}),
    )
    client = TestClient(app)
    r = client.get("/api/etf/list", params={"sort": "total_share", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == []
    assert "message" in body
    assert "etf_share_size" in body["message"]


@pytest.mark.unit
def test_etf_list_sort_merges_basic_and_pagination_fields(monkeypatch):
    td = "20250401"
    share_docs = [
        {"ts_code": "510300.SH", "trade_date": td, "total_share": 100.0, "total_size": 50.0},
        {"ts_code": "159919.SZ", "trade_date": td, "total_share": 80.0, "total_size": 40.0},
    ]
    basic_docs = [
        {"ts_code": "510300.SH", "name": "沪深300ETF"},
        {"ts_code": "159919.SZ", "name": "沪深300ETF易方达"},
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": ShareSizeCollection(share_docs),
                "etf_basic": BasicCollection(basic_docs),
            }
        ),
    )
    client = TestClient(app)
    r = client.get(
        "/api/etf/list",
        params={"sort": "total_share", "trade_date": td, "limit": 1, "offset": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sort"] == "total_share"
    assert body["trade_date"] == td
    assert body["total"] == 2
    assert body["offset"] == 0
    assert body["limit"] == 1
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["ts_code"] == "510300.SH"
    assert row["name"] == "沪深300ETF"
    assert row["total_share"] == 100.0


@pytest.mark.unit
def test_etf_list_sort_serializes_decimal128(monkeypatch):
    td = "20250402"
    share_docs = [
        {"ts_code": "510330.SH", "trade_date": td, "total_share": Decimal128("1.5"), "total_size": Decimal128("2.25")},
    ]
    basic_docs = [{"ts_code": "510330.SH", "name": "上证ETF"}]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": ShareSizeCollection(share_docs),
                "etf_basic": BasicCollection(basic_docs),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/list", params={"sort": "total_share", "trade_date": td})
    assert r.status_code == 200
    row = r.json()["data"][0]
    assert isinstance(row["total_share"], float)
    assert row["total_share"] == pytest.approx(1.5)
    assert isinstance(row["total_size"], float)


@pytest.mark.unit
def test_etf_list_search_matches_benchmark_and_alias(monkeypatch):
    basic_docs = [
        {"ts_code": "512100.SH", "name": "南方中证1000ETF", "benchmark": "中证1000指数"},
        {"ts_code": "510300.SH", "name": "沪深300ETF", "benchmark": "沪深300指数"},
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db({"etf_basic": BasicCollection(basic_docs)}),
    )
    client = TestClient(app)

    r = client.get("/api/etf/list", params={"q": "1000"})

    assert r.status_code == 200
    codes = [row["ts_code"] for row in r.json()["data"]]
    assert "512100.SH" in codes


@pytest.mark.unit
def test_etf_kline_coerces_string_ohlc(monkeypatch):
    daily = [
        {
            "ts_code": "510300.SH",
            "trade_date": "20250410",
            "open": "3.1",
            "high": "3.2",
            "low": "3.0",
            "close": "3.15",
            "vol": "1000",
            "amount": "3150",
        }
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_daily": DailyCollection(daily),
                "etf_adj_factor": EmptyCollection(),
                "etf_shares": EmptyCollection(),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/510300.SH/kline", params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["tf"] == "1d"
    assert body["count"] == 1
    bar = body["data"][0]
    assert bar["close"] == pytest.approx(3.15)
    # Shaped like /records/: pre_close/pct_chg exist even when there is no
    # older row to anchor the change.
    assert bar["adjust"] == "qfq"
    assert bar["pre_close"] is None
    assert bar["pct_chg"] is None


@pytest.mark.unit
def test_etf_kline_invalid_tf_returns_400():
    client = TestClient(app)
    r = client.get("/api/etf/510300.SH/kline", params={"tf": "1h"})
    assert r.status_code == 400


@pytest.mark.unit
def test_etf_kline_weekly_sums_vol_and_labels_friday(monkeypatch):
    """Two trading weeks: W-FRI resample, vol sum per week, pre_adj close = last in period."""
    daily = []
    w1 = ["20250331", "20250401", "20250402", "20250403", "20250404"]
    w2 = ["20250407", "20250408", "20250409", "20250410", "20250411"]
    for td in w1:
        daily.append(
            {
                "ts_code": "510300.SH",
                "trade_date": td,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 2.0 if td == "20250404" else 1.0,
                "vol": 10.0,
                "amount": 100.0,
            }
        )
    for td in w2:
        daily.append(
            {
                "ts_code": "510300.SH",
                "trade_date": td,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 2.5 if td == "20250411" else 1.0,
                "vol": 10.0,
                "amount": 100.0,
            }
        )
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_daily": DailyCollection(daily),
                "etf_adj_factor": EmptyCollection(),
                "etf_shares": EmptyCollection(),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/510300.SH/kline", params={"tf": "1w", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["tf"] == "1w"
    assert body["count"] == 2
    # Newest first; trade_date is the last trading day inside the bucket.
    w_new, w_old = body["data"][0], body["data"][1]
    assert w_new["trade_date"] == "20250411"
    assert w_new["close"] == pytest.approx(2.5)
    assert w_new["pre_close"] == pytest.approx(2.0)
    assert w_new["pct_chg"] == pytest.approx(25.0)
    assert w_new["vol"] == pytest.approx(50.0)
    assert w_old["trade_date"] == "20250404"
    assert w_old["close"] == pytest.approx(2.0)
    assert w_old["vol"] == pytest.approx(50.0)


@pytest.mark.unit
def test_etf_kline_weekly_uses_last_trading_day_not_period_label(monkeypatch):
    """周/月 bar 的 trade_date 必须是组内最后一个交易日（不能是周期末标签）。"""
    daily = [
        {"ts_code": "510300.SH", "trade_date": td, "open": 1.0, "high": 1.1, "low": 0.9,
         "close": 1.0, "vol": 10.0, "amount": 100.0, "fd_share": 100.0}
        for td in ["20250331", "20250401", "20250402", "20250403"]
    ]
    daily += [
        {"ts_code": "510300.SH", "trade_date": td, "open": 1.0, "high": 1.1, "low": 0.9,
         "close": 1.0, "vol": 10.0, "amount": 100.0, "fd_share": 100.0}
        for td in ["20250407", "20250408", "20250409", "20250410"]
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_daily": DailyCollection(daily),
                "etf_adj_factor": EmptyCollection(),
                "etf_shares": EmptyCollection(),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/510300.SH/kline", params={"tf": "1w", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    newest = body["data"][0]
    # Friday 2025-04-04 was a holiday; the week's last trading day is Thursday.
    assert newest["trade_date"] == "20250410"
    assert newest["open"] == pytest.approx(1.0)
    assert newest["high"] == pytest.approx(1.1)
    assert newest["low"] == pytest.approx(0.9)
    assert newest["close"] == pytest.approx(1.0)
    assert newest["pre_close"] == pytest.approx(1.0)
    assert newest["pct_chg"] == pytest.approx(0.0)


@pytest.mark.unit
def test_etf_kline_adjust_none_returns_raw_and_hfq_scales(monkeypatch):
    daily = [
        {"ts_code": "510300.SH", "trade_date": "20250410", "open": 1.0, "high": 1.1,
         "low": 0.9, "close": 1.0, "vol": 10.0, "amount": 100.0},
        {"ts_code": "510300.SH", "trade_date": "20250411", "open": 1.0, "high": 1.1,
         "low": 0.9, "close": 1.0, "vol": 10.0, "amount": 100.0},
    ]
    adj_docs = [
        {"ts_code": "510300.SH", "trade_date": "20250410", "adj_factor": 1.2},
        {"ts_code": "510300.SH", "trade_date": "20250411", "adj_factor": 1.5},
    ]

    class AdjCollection:
        def find(self, query=None, projection=None):
            return ChainCursor([dict(d) for d in adj_docs])

        def find_one(self, query=None, sort=None, projection=None):
            return dict(max(adj_docs, key=lambda d: d["trade_date"]))

    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_daily": DailyCollection(daily),
                "etf_adj_factor": AdjCollection(),
                "etf_shares": EmptyCollection(),
            }
        ),
    )
    client = TestClient(app)
    raw = client.get("/api/etf/510300.SH/kline", params={"adjust": "none"}).json()["data"][0]
    qfq = client.get("/api/etf/510300.SH/kline", params={"adjust": "qfq"}).json()["data"][0]
    hfq = client.get("/api/etf/510300.SH/kline", params={"adjust": "hfq"}).json()["data"][0]
    assert raw["close"] == pytest.approx(1.0)
    assert raw["adjust"] == "none"
    # qfq anchors on the latest factor (1.5), so the newest bar stays at 1.0
    assert qfq["close"] == pytest.approx(1.0)
    assert qfq["adjust"] == "qfq"
    assert hfq["close"] == pytest.approx(1.5)  # factor × price
    assert hfq["adjust"] == "hfq"
