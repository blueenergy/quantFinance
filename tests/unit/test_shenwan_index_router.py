"""shenwan_index 路由：参数校验与无数据路径（Mongo 用假集合，不连真库）。"""
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_active_user
from routers import shenwan_index as sm

app = FastAPI()
app.include_router(sm.router, prefix="/api/shenwan-index")


def _authed_client() -> TestClient:
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "service_level": "vip", "is_admin": False}
    return TestClient(app)


def teardown_dep():
    app.dependency_overrides.clear()


@pytest.mark.unit
def test_kline_invalid_tf():
    c = _authed_client()
    try:
        r = c.get("/api/shenwan-index/kline", params={"ts_code": "801012.SI", "tf": "1h"})
        assert r.status_code == 400
    finally:
        teardown_dep()


@pytest.mark.unit
def test_kline_no_data_returns_message(monkeypatch):
    """全库无该 ts 时 success + message（提示回补）。"""

    class Empty:
        def find_one(self, *a, **k):
            return None

        def find(self, *a, **k):
            return _EmptyCur()

    class _EmptyCur:
        def sort(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __iter__(self):
            return iter([])

    c = _authed_client()
    try:
        monkeypatch.setattr(sm, "_DAILY", Empty())
        r = c.get("/api/shenwan-index/kline", params={"ts_code": "999999.SI", "tf": "1d", "limit": 200})
        assert r.status_code == 200
        j = r.json()
        assert j.get("success") is True
        assert j.get("data") == []
        assert "run_sw_industry_backfill" in (j.get("message") or "")
    finally:
        teardown_dep()


@pytest.mark.unit
def test_kline_monthly_rebuilds_tail_from_daily(monkeypatch):
    """预计算月线尾部落后时，用 sw_daily 重算最后周期并追加后续月份。"""

    class Curs:
        def __init__(self, docs):
            self._docs = list(docs)

        def sort(self, field, direction):
            self._docs.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
            return self

        def limit(self, n):
            self._docs = self._docs[:n]
            return self

        def __iter__(self):
            return iter(self._docs)

    class FakeCollection:
        def __init__(self, docs):
            self.docs = list(docs)

        def find_one(self, query=None, projection=None, sort=None):
            rows = list(self.find(query, projection))
            if sort:
                for field, direction in reversed(sort):
                    rows.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
            return rows[0] if rows else None

        def find(self, query=None, projection=None):
            rows = [dict(doc) for doc in self.docs if self._match(doc, query or {})]
            if projection:
                include = {k for k, v in projection.items() if v}
                if include:
                    rows = [{k: row.get(k) for k in include if k in row} for row in rows]
            return Curs(rows)

        def _match(self, doc, query):
            for key, expected in query.items():
                value = doc.get(key)
                if isinstance(expected, dict):
                    gte = expected.get("$gte")
                    lte = expected.get("$lte")
                    if gte and value < gte:
                        return False
                    if lte and value > lte:
                        return False
                    continue
                if value != expected:
                    return False
            return True

    stale_monthly = [
        {
            "ts_code": "801010.SI",
            "trade_date": "20260424",
            "open": 100,
            "high": 110,
            "low": 90,
            "close": 105,
            "vol": 100,
            "amount": 1000,
        }
    ]
    daily_rows = [
        {"ts_code": "801010.SI", "trade_date": "20260401", "open": 100, "high": 110, "low": 90, "close": 102, "vol": 10, "amount": 100},
        {"ts_code": "801010.SI", "trade_date": "20260424", "open": 103, "high": 112, "low": 99, "close": 105, "vol": 20, "amount": 200},
        {"ts_code": "801010.SI", "trade_date": "20260430", "open": 106, "high": 120, "low": 104, "close": 118, "vol": 30, "amount": 300},
        {"ts_code": "801010.SI", "trade_date": "20260529", "open": 119, "high": 130, "low": 115, "close": 128, "vol": 40, "amount": 400},
        {"ts_code": "801010.SI", "trade_date": "20260612", "open": 129, "high": 140, "low": 125, "close": 136, "vol": 50, "amount": 500},
    ]

    c = _authed_client()
    try:
        monkeypatch.setattr(sm, "_MONTHLY", FakeCollection(stale_monthly))
        monkeypatch.setattr(sm, "_DAILY", FakeCollection(daily_rows))
        r = c.get(
            "/api/shenwan-index/kline",
            params={
                "ts_code": "801010.SI",
                "tf": "1m",
                "start": "20260401",
                "end": "20260614",
                "limit": 50,
            },
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert [row["trade_date"] for row in data] == ["20260430", "20260529", "20260612"]
        assert data[0]["close"] == pytest.approx(118)
        assert data[0]["vol"] == pytest.approx(60)
    finally:
        teardown_dep()


@pytest.mark.unit
def test_get_children_uses_industry_code_link(monkeypatch):
    """Tushare 子级 parent_code=父行 industry_code；用父 index 801050.SI 能查到 L2。"""

    l1 = {
        "index_code": "801050.SI",
        "industry_code": 240000,
        "level": "L1",
        "src": "SW2021",
        "industry_name": "有色金属",
    }
    l2 = {
        "index_code": "801074.SI",
        "industry_code": 240600,
        "parent_code": 240000,
        "level": "L2",
        "src": "SW2021",
        "industry_name": "能源金属",
    }
    all_docs = [l1, l2]

    class Curs:
        def __init__(self, docs):
            self._docs = list(docs)

        def sort(self, *a, **k):
            return self

        def __iter__(self):
            return iter(self._docs)

    class FakeClassify:
        def find_one(self, query, projection=None):
            if not query:
                return None
            want_src = query.get("src")
            for d in all_docs:
                if want_src is not None and d.get("src") != want_src:
                    continue
                if "index_code" in query and query.get("index_code") == d.get("index_code"):
                    return dict(d)
                if "industry_code" in query and isinstance(query["industry_code"], dict):
                    inn = query["industry_code"].get("$in", [])
                    if d.get("industry_code") in inn or str(d.get("industry_code")) in inn:
                        return dict(d)
            return None

        def _match_flt(self, q, d):
            if d.get("src") != q.get("src", "SW2021") or d.get("level") != q.get("level"):
                return False
            p = q.get("parent_code")
            if p is None:
                return True
            dp = d.get("parent_code")
            if isinstance(p, dict) and "$in" in p:
                for x in p["$in"]:
                    if dp == x or str(dp) == str(x):
                        return True
                return False
            if dp == p or str(dp) == str(p):
                return True
            return False

        def count_documents(self, flt, **k):
            return len([d for d in all_docs if self._match_flt(flt, d)])

        def find(self, flt, projection=None):
            m = [dict(d) for d in all_docs if self._match_flt(flt, d)]
            return Curs(m)

    c = _authed_client()
    try:
        monkeypatch.setattr(sm, "_CLASSIFY", FakeClassify())
        r = c.get("/api/shenwan-index/children", params={"parent_code": "801050.SI"})
        assert r.status_code == 200
        j = r.json()
        assert j.get("success") is True
        assert j.get("count") == 1
        data = j.get("data") or []
        assert len(data) == 1
        assert data[0].get("index_code") == "801074.SI"
        assert data[0].get("industry_code") in (240600, "240600")
        r2 = c.get(
            "/api/shenwan-index/children", params={"parent_code": "801050.SI", "src": "WRONG"}
        )
        assert (r2.json() or {}).get("count", 0) == 0
    finally:
        teardown_dep()


@pytest.mark.unit
def test_tree_no_classify_returns_message(monkeypatch):
    class Empty:
        def count_documents(self, *a, **k):
            return 0

    c = _authed_client()
    try:
        monkeypatch.setattr(sm, "_CLASSIFY", Empty())
        r = c.get("/api/shenwan-index/tree", params={"level": "L1"})
        assert r.status_code == 200
        j = r.json()
        assert j.get("data") == []
        assert "run_sw_industry_backfill" in (j.get("message") or "")
    finally:
        teardown_dep()


@pytest.mark.unit
def test_industry_money_flow_aggregates_members(monkeypatch):
    class Curs:
        def __init__(self, docs):
            self._docs = list(docs)

        def sort(self, field, direction):
            self._docs.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
            return self

        def limit(self, n):
            self._docs = self._docs[:n]
            return self

        def __iter__(self):
            return iter(self._docs)

    class FakeCollection:
        def __init__(self, docs):
            self.docs = list(docs)

        def find(self, query=None, projection=None):
            rows = [dict(doc) for doc in self.docs if self._match(doc, query or {})]
            if projection:
                include = {k for k, v in projection.items() if v}
                if include:
                    rows = [{k: row.get(k) for k in include if k in row} for row in rows]
            return Curs(rows)

        def _match(self, doc, query):
            for key, expected in query.items():
                if key == "$or":
                    if not any(self._match(doc, clause) for clause in expected):
                        return False
                    continue
                value = doc.get(key)
                if isinstance(expected, dict):
                    if "$in" in expected and value not in expected["$in"]:
                        return False
                    continue
                if value != expected:
                    return False
            return True

    members = [
        {"src": "SW2021", "l1_code": "801010.SI", "is_new": "Y", "con_code": "000001.SZ", "name": "平安银行"},
        {"src": "SW2021", "l1_code": "801010.SI", "is_new": "Y", "con_code": "000002.SZ", "name": "万科A"},
    ]
    flows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260610",
            "buy_elg_amount": 1000.0,
            "sell_elg_amount": 400.0,
            "buy_lg_amount": 500.0,
            "sell_lg_amount": 200.0,
            "amount": 3000.0,
        },
        {
            "ts_code": "000002.SZ",
            "trade_date": "20260610",
            "buy_elg_amount": 100.0,
            "sell_elg_amount": 300.0,
            "buy_lg_amount": 200.0,
            "sell_lg_amount": 400.0,
            "amount": 2000.0,
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260609",
            "net_mf_amount": -50.0,
            "amount": 1000.0,
        },
    ]

    c = _authed_client()
    try:
        monkeypatch.setattr(sm, "_MEMBER", FakeCollection(members))
        monkeypatch.setattr(sm, "_MONEY_FLOW", FakeCollection(flows))
        r = c.get("/api/shenwan-index/money-flow", params={"index_code": "801010.SI", "days": 5})
        assert r.status_code == 200
        j = r.json()
        assert j["success"] is True
        assert j["member_count"] == 2
        assert j["count"] == 2
        latest = j["data"][-1]
        assert latest["trade_date"] == "20260610"
        assert latest["main_net"] == pytest.approx(500.0)
        assert latest["covered_count"] == 2
        assert latest["coverage_level"] == "ok"
        assert latest["top_inflow"][0]["con_code"] == "000001.SZ"
        assert latest["top_outflow"][0]["con_code"] == "000002.SZ"
        assert j["summary"]["main_net_today"] == pytest.approx(500.0)
        assert j["summary"]["main_net_5d"] == pytest.approx(450.0)
    finally:
        teardown_dep()


@pytest.mark.unit
def test_agg_ohlc_weekly_sums_vol_and_ohlc():
    """W-FRI: open=first, high=max, low=min, close=last, vol/amount=sum。"""
    rows = [
        {
            "trade_date": d,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 2.0 if d == "20250404" else 1.0,
            "vol": 10.0,
            "amount": 100.0,
        }
        for d in ["20250331", "20250401", "20250402", "20250403", "20250404"]
    ]
    rows += [
        {
            "trade_date": d,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 2.5 if d == "20250411" else 1.0,
            "vol": 10.0,
            "amount": 100.0,
        }
        for d in ["20250407", "20250408", "20250409", "20250410", "20250411"]
    ]
    df = pd.DataFrame(rows)
    out = sm._agg_ohlc(df, "1w")
    assert len(out) == 2
    w_old, w_new = out[0], out[1]
    assert w_new["trade_date"] == "20250411"
    assert w_new["close"] == pytest.approx(2.5)
    assert w_new["vol"] == pytest.approx(50.0)
    assert w_old["trade_date"] == "20250404"
    assert w_old["close"] == pytest.approx(2.0)
    assert w_old["vol"] == pytest.approx(50.0)
    assert w_new["amount"] == pytest.approx(500.0)


@pytest.mark.unit
def test_agg_ohlc_monthly_two_months():
    """两自然月：量求和、收盘取各月最后一根日。"""
    df = pd.DataFrame(
        [
            {"trade_date": "20250102", "open": 1, "high": 1.1, "low": 0.9, "close": 1.0, "vol": 1, "amount": 10},
            {"trade_date": "20250110", "open": 1, "high": 1.1, "low": 0.9, "close": 1.0, "vol": 1, "amount": 10},
            {"trade_date": "20250120", "open": 1, "high": 1.1, "low": 0.9, "close": 1.0, "vol": 1, "amount": 10},
            {"trade_date": "20250210", "open": 2, "high": 2.1, "low": 1.9, "close": 2.0, "vol": 2, "amount": 20},
        ]
    )
    out = sm._agg_ohlc(df, "1m")
    assert len(out) == 2
    assert out[0]["vol"] == pytest.approx(3.0)
    assert out[0]["close"] == pytest.approx(1.0)
    assert out[1]["vol"] == pytest.approx(2.0)
    assert out[1]["close"] == pytest.approx(2.0)
    assert out[0]["trade_date"][:6] == "202501"
    assert out[1]["trade_date"][:6] == "202502"
