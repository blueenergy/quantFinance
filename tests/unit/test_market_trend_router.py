"""market_trend 路由：评分口径与无数据/参数路径（Mongo 用假集合，不连真库）。"""
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_active_user
from routers import market_trend as mt


class FakeCursor(list):
    def sort(self, key, direction=1):
        return FakeCursor(sorted(self, key=lambda d: d.get(key) or "", reverse=direction == -1))


class FakeCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    @staticmethod
    def _match(doc, query):
        for key, want in (query or {}).items():
            got = doc.get(key)
            if isinstance(want, dict):
                if "$in" in want and got not in want["$in"]:
                    return False
                if "$gte" in want and str(got or "") < str(want["$gte"]):
                    return False
            elif got != want:
                return False
        return True

    def find(self, query=None, projection=None):
        return FakeCursor({k: v for k, v in doc.items() if not projection or projection.get(k)} for doc in self.docs if self._match(doc, query))

    def distinct(self, field, query=None):
        return list({doc.get(field) for doc in self.docs if self._match(doc, query) and doc.get(field) is not None})


def _seed(monkeypatch, n_days=520):
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2020-01-01", periods=n_days)]
    broad_codes = [code for code, _ in mt.BROAD_INDICES]
    index_rows = []
    for code in broad_codes:
        for i, date in enumerate(dates):
            index_rows.append({"ts_code": code, "trade_date": date, "close": 100.0 + i * 0.1})

    classify_rows = [
        {"index_code": "801780.SI", "industry_name": "银行", "level": "L1", "src": "SW2021"},
        {"index_code": "801081.SI", "industry_name": "半导体", "level": "L2", "src": "SW2021"},
        {"index_code": "801737.SI", "industry_name": "电池", "level": "L2", "src": "SW2021"},
    ]
    sw_rows = []
    for code in ("801780.SI", "801081.SI", "801737.SI"):
        for i, date in enumerate(dates):
            sw_rows.append({"ts_code": code, "trade_date": date, "close": 100.0 + i * 0.05})

    monkeypatch.setattr(mt, "_INDEX_PRICES", FakeCollection(index_rows))
    monkeypatch.setattr(mt, "_SW_DAILY", FakeCollection(sw_rows))
    monkeypatch.setattr(mt, "_SW_CLASSIFY", FakeCollection(classify_rows))
    monkeypatch.setattr(mt, "_PAYLOAD_CACHE", {})
    return dates


def _client():
    app = FastAPI()
    app.include_router(mt.router, prefix="/api/market-trend")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1"}
    return TestClient(app)


@pytest.mark.unit
def test_score_series_uses_only_past_data():
    """追加未来数据不能改变历史分数。"""
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.arange(500) * 0.02 + rng.normal(0, 0.5, 500))
    full = mt._score_series(close)
    prefix = mt._score_series(close.iloc[:400])
    pd.testing.assert_series_equal(full.iloc[:400], prefix)


@pytest.mark.parametrize(
    "close, expected_side",
    [
        # 日收益率递增（加速上涨）：近期趋势强于历史 → 高分
        (pd.Series(100 * np.exp(np.cumsum(np.linspace(0, 0.01, 400)))), "high"),
        # 日收益率递减（上涨减速）：近期趋势弱于历史 → 低分
        (pd.Series(100 * np.exp(np.cumsum(np.linspace(0.01, 0, 400)))), "low"),
    ],
)
@pytest.mark.unit
def test_score_series_direction(close, expected_side):
    score = mt._score_series(close).dropna()
    assert len(score) > 0
    if expected_side == "high":
        assert score.iloc[-1] > 95
    else:
        assert score.iloc[-1] < 5


@pytest.mark.unit
def test_panorama_payload_shape(monkeypatch):
    dates = _seed(monkeypatch)
    payload = mt._build_payload(30, include_themes=True)
    assert payload["success"] is True
    assert payload["as_of"] == dates[-1]
    assert len(payload["dates"]) == 30
    assert [g["key"] for g in payload["groups"]] == ["broad", "sw1", "theme"]
    broad = payload["groups"][0]["items"]
    assert len(broad) == len(mt.BROAD_INDICES)
    for item in broad:
        assert len(item["scores"]) == 30
        assert item["latest"] is not None
        assert 0 <= item["latest"] <= 100
    assert payload["groups"][1]["items"][0]["name"] == "银行"


@pytest.mark.unit
def test_panorama_endpoint_and_param_validation(monkeypatch):
    _seed(monkeypatch)
    client = _client()
    ok = client.get("/api/market-trend/panorama", params={"days": 30})
    assert ok.status_code == 200
    body = ok.json()
    assert body["success"] is True
    assert len(body["dates"]) == 30

    assert client.get("/api/market-trend/panorama", params={"days": 5}).status_code == 422
    assert client.get("/api/market-trend/panorama", params={"days": 300}).status_code == 422


@pytest.mark.unit
def test_panorama_empty_data_returns_message(monkeypatch):
    monkeypatch.setattr(mt, "_INDEX_PRICES", FakeCollection([]))
    monkeypatch.setattr(mt, "_SW_DAILY", FakeCollection([]))
    monkeypatch.setattr(mt, "_SW_CLASSIFY", FakeCollection([]))
    monkeypatch.setattr(mt, "_PAYLOAD_CACHE", {})
    payload = mt._build_payload(30, include_themes=True)
    assert payload["success"] is True
    assert payload["dates"] == []
    assert "暂无数据" in payload["message"]
