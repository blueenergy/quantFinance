"""shenwan_index 路由：参数校验与无数据路径（Mongo 用假集合，不连真库）。"""
import pytest
from fastapi.testclient import TestClient

from auth import get_current_active_user
from main import app
from routers import shenwan_index as sm


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
