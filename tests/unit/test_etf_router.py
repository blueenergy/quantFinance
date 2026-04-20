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
    assert body["count"] == 1
    bar = body["data"][0]
    assert bar["close"] == pytest.approx(3.15)
    assert "pre_adj_close" in bar
