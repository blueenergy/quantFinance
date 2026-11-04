"""market_intraday 路由：横截面分、强弱切换、空数据与参数校验（假集合，不连真库）。"""
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import DESCENDING

from auth import get_current_active_user
from routers import market_intraday as mi


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
            if key == "$or":
                if not any(FakeCollection._match(doc, branch) for branch in want):
                    return False
            elif isinstance(want, dict):
                if "$in" in want and got not in want["$in"]:
                    return False
                if "$exists" in want:
                    if bool(want["$exists"]) != (key in doc):
                        return False
                if "$gte" in want and str(got or "") < str(want["$gte"]):
                    return False
                if "$lte" in want and str(got or "") > str(want["$lte"]):
                    return False
            elif got != want:
                return False
        return True

    def find(self, query=None, projection=None):
        return FakeCursor(
            {k: v for k, v in doc.items() if not projection or projection.get(k)}
            for doc in self.docs
            if self._match(doc, query)
        )

    def find_one(self, query=None, projection=None, sort=None):
        rows = list(self.find(query, projection))
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda d: d.get(key) or "", reverse=direction == -1)
        return rows[0] if rows else None

    def distinct(self, field, query=None):
        return list({doc.get(field) for doc in self.docs if self._match(doc, query) and doc.get(field) is not None})


def _times():
    # 09:30 -> 10:02，每 2 分钟一条，共 17 条
    out = []
    for i in range(17):
        hh, mm = divmod(570 + 2 * i, 60)
        out.append(f"20261104{hh:02d}{mm:02d}")
    return out


def _sector_docs():
    sectors = {
        "801780.SI": ("银行", 0, 3),
        "801080.SI": ("电子", 2, 2),
        "801050.SI": ("有色金属", 3, 0),
        "801120.SI": ("食品饮料", 1, 1),
    }
    docs = []
    for snap in _times():
        # 09:50 之前有色强、银行弱；之后反转
        before = snap < "202611040950"
        for code, (name, early, late) in sectors.items():
            strength = early if before else late
            docs.append({
                "snapshot_time": snap,
                "trade_date": "20261104",
                "level": "L1",
                "ma_period": 5,
                "sector_code": code,
                "sector_name": name,
                "positive_ratio": [0.1, 0.4, 0.7, 0.9][strength],
                "above_ma_ratio": [0.1, 0.4, 0.7, 0.9][strength],
                "relative_positive_ratio": [-0.5, -0.15, 0.15, 0.5][strength],
                "avg_pct_chg": [-3.0, -1.0, 1.0, 3.0][strength],
                "median_pct_chg": [-3.0, -1.0, 1.0, 3.0][strength],
                "positive_count": [10, 40, 70, 100][strength],
                "negative_count": [100, 70, 40, 10][strength],
                "flat_count": 1,
                "stock_count": 111,
            })
    return docs


def _index_docs():
    names = [
        ("000001.SH", "上证指数"),
        ("000016.SH", "上证50"),
        ("000300.SH", "沪深300"),
        ("000905.SH", "中证500"),
        ("000852.SH", "中证1000"),
        ("399006.SZ", "创业板指"),
        ("000688.SH", "科创50"),
        ("399673.SZ", "创业板50"),
        ("399303.SZ", "国证2000"),
    ]
    docs = []
    for rank, (code, name) in enumerate(names, start=1):
        for snap in _times():
            docs.append({
                "ts_code": code,
                "name": name,
                "snapshot_time": snap,
                "pct_chg": float(rank),
                "intraday_move": float(rank),
                "range_pos": rank / 10.0,
            })
    return docs


def _breadth_docs():
    docs = []
    for snap in _times():
        for period, yang in ((5, 0.7), (30, 0.3)):
            docs.append({
                "snapshot_time": snap,
                "ma_period": period,
                "yang_spectrum": yang,
                "above_ma_count": int(5000 * yang),
                "below_ma_count": int(5000 * (1 - yang)),
                "total_stocks": 5000,
                "coverage_ratio": 0.99,
            })
    return docs


def _seed(monkeypatch):
    monkeypatch.setattr(mi, "_SECTOR_STRENGTH", FakeCollection(_sector_docs()))
    monkeypatch.setattr(mi, "_MARKET_SPECTRUM", FakeCollection(_breadth_docs()))
    monkeypatch.setattr(mi, "_INDEX_SNAPSHOTS", FakeCollection(_index_docs()))
    monkeypatch.setattr(mi, "_CACHE", {})
    monkeypatch.setattr(mi, "_DATES_CACHE", {})


def _client():
    app = FastAPI()
    app.include_router(mi.router, prefix="/api/market-trend")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1"}
    return TestClient(app)


@pytest.mark.unit
def test_payload_shape_and_cross_section(monkeypatch):
    _seed(monkeypatch)
    payload = mi._build_payload("20261104", "L1", 5)
    assert payload["success"] is True
    assert payload["as_of"] == "202611041002"
    assert len(payload["times"]) == 17
    assert len(payload["sectors"]) == 4

    by_name = {s["name"]: s for s in payload["sectors"]}
    # 09:50 之后银行最强、有色最弱
    assert by_name["银行"]["latest"] > by_name["电子"]["latest"] > by_name["有色金属"]["latest"]
    assert by_name["银行"]["latest"] == 100.0
    assert by_name["有色金属"]["latest"] == 25.0

    summary = payload["summary"]
    assert summary["top"][0]["name"] == "银行"
    assert summary["bottom"][0]["name"] == "有色金属"
    assert summary["up"] == 220
    assert summary["down"] == 220
    assert summary["flat"] == 4
    assert summary["weak_to_strong"][0]["name"] == "银行"
    assert summary["strong_to_weak"][0]["name"] == "有色金属"
    assert payload["breadth"]["yang5"][-1] == 0.7
    assert payload["breadth"]["yang30"][-1] == 0.3

    indices = payload["indices"]
    assert len(indices) == 9
    by_code = {item["code"]: item for item in indices}
    assert by_code["399303.SZ"]["latest"] == 100.0
    assert by_code["000001.SH"]["latest"] == pytest.approx(100.0 / 9.0, abs=0.1)
    assert by_code["000688.SH"]["latest_pct_chg"] == 7.0
    assert len(by_code["000300.SH"]["scores"]) == 17


@pytest.mark.unit
def test_score_matrix_has_no_lookahead():
    times = ["202611040930", "202611040932", "202611040934"]
    sectors = ["A", "B"]
    rows = []
    for i, snap in enumerate(times):
        rows.append({"snapshot_time": snap, "sector_code": "A", "m": i + 1})
        rows.append({"snapshot_time": snap, "sector_code": "B", "m": 10 - i})
    df = pd.DataFrame(rows).rename(columns={"m": "positive_ratio"})
    for col in ["above_ma_ratio", "relative_positive_ratio", "avg_pct_chg", "median_pct_chg"]:
        df[col] = df["positive_ratio"]
    full = mi._score_matrix(df, times, sectors)
    prefix = mi._score_matrix(df[df["snapshot_time"].isin(times[:2])], times[:2], sectors)
    pd.testing.assert_frame_equal(full.loc[times[:2]], prefix)


@pytest.mark.unit
def test_change_matrix_uses_30_minutes_ago():
    times = ["202611040930", "202611041000", "202611041030"]
    score = pd.DataFrame({"A": [10.0, 20.0, 50.0], "B": [90.0, 80.0, 40.0]}, index=times)
    chg = mi._change_matrix(score, times, minutes=30)
    assert pd.isna(chg.loc[times[0], "A"])
    assert chg.loc[times[1], "A"] == 10.0
    assert chg.loc[times[1], "B"] == -10.0
    assert chg.loc[times[2], "A"] == 30.0
    assert chg.loc[times[2], "B"] == -40.0


@pytest.mark.unit
def test_endpoint_and_empty_date(monkeypatch):
    _seed(monkeypatch)
    client = _client()
    ok = client.get("/api/market-trend/intraday", params={"date": "20261104"})
    assert ok.status_code == 200
    assert len(ok.json()["times"]) == 17

    missing = client.get("/api/market-trend/intraday", params={"date": "20260101"})
    assert missing.status_code == 200
    body = missing.json()
    assert body["times"] == []
    assert "无盘中快照" in body["message"]

    assert client.get("/api/market-trend/intraday", params={"level": "L3"}).status_code == 422
    assert client.get("/api/market-trend/intraday", params={"ma_period": 1}).status_code == 422


@pytest.mark.unit
def test_latest_date_is_default(monkeypatch):
    _seed(monkeypatch)
    payload = mi._build_payload("", "L1", 5)
    assert payload["date"] == "20261104"
    assert len(payload["times"]) == 17


@pytest.mark.unit
def test_empty_collection_returns_message(monkeypatch):
    monkeypatch.setattr(mi, "_SECTOR_STRENGTH", FakeCollection([]))
    monkeypatch.setattr(mi, "_MARKET_SPECTRUM", FakeCollection([]))
    monkeypatch.setattr(mi, "_INDEX_SNAPSHOTS", FakeCollection([]))
    monkeypatch.setattr(mi, "_CACHE", {})
    monkeypatch.setattr(mi, "_DATES_CACHE", {})
    payload = mi._build_payload("", "L1", 5)
    assert payload["success"] is True
    assert payload["times"] == []
    assert payload["indices"] == []
    assert "暂无数据" in payload["message"]
