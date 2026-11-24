from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import get_current_active_user
from routers import market_stats
from services import etf_penetration


class _Coll:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, _projection=None):
        filtered = []
        for doc in self.docs:
            ok = True
            for key, cond in (query or {}).items():
                value = doc.get(key)
                if isinstance(cond, dict):
                    if "$in" in cond and value not in cond["$in"]:
                        ok = False
                        break
                    if "$gte" in cond and str(value) < str(cond["$gte"]):
                        ok = False
                        break
                elif value != cond:
                    ok = False
                    break
            if ok:
                filtered.append(dict(doc))
        return filtered


TEST_POOLS = {
    "test": {
        "name": "测试指数",
        "index_code": "TEST.IDX",
        "funds": [
            {"ts_code": "AAA.SH", "name": "A ETF"},
            {"ts_code": "BBB.SH", "name": "B ETF"},
        ],
    }
}


def _collections():
    shares = [
        {"ts_code": "AAA.SH", "trade_date": "20260105", "fd_share": 10000.0},
        {"ts_code": "AAA.SH", "trade_date": "20260106", "fd_share": 11000.0},
        {"ts_code": "AAA.SH", "trade_date": "20260107", "fd_share": 12000.0},
        {"ts_code": "BBB.SH", "trade_date": "20260105", "fd_share": 5000.0},
        # 20260106 missing BBB on purpose: the point must be dropped, not summed
        # with partial coverage.
        {"ts_code": "BBB.SH", "trade_date": "20260107", "fd_share": 6000.0},
    ]
    closes = [
        {"ts_code": "AAA.SH", "trade_date": "20260105", "close": 2.0},
        {"ts_code": "AAA.SH", "trade_date": "20260106", "close": 2.0},
        {"ts_code": "AAA.SH", "trade_date": "20260107", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260105", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260106", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260107", "close": 2.0},
    ]
    basic = [
        {"ts_code": "TEST.IDX", "trade_date": "20260105", "float_mv": 1.0e11},
        {"ts_code": "TEST.IDX", "trade_date": "20260106", "float_mv": 1.1e11},
        {"ts_code": "TEST.IDX", "trade_date": "20260107", "float_mv": 1.2e11},
    ]
    prices = [
        {"ts_code": "TEST.IDX", "trade_date": "20260105", "close": 4000.0},
        {"ts_code": "TEST.IDX", "trade_date": "20260106", "close": 4100.0},
        {"ts_code": "TEST.IDX", "trade_date": "20260107", "close": 4200.0},
    ]
    return {
        "etf_shares": _Coll(shares),
        "etf_daily": _Coll(closes),
        "index_daily_basic": _Coll(basic),
        "index_prices": _Coll(prices),
    }


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_stats.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    return TestClient(app)


@pytest.mark.unit
def test_compute_penetration_requires_full_pool_coverage(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    payload = etf_penetration.compute_penetration(_collections(), index_key="test", days=0)

    assert payload["success"] is True
    assert payload["index"]["name"] == "测试指数"
    assert payload["coverage"]["fund_count"] == 2
    # 20260106 dropped: BBB has no share row that day.
    assert [row["trade_date"] for row in payload["series"]] == ["20260105", "20260107"]
    first = payload["series"][0]
    # (10000 + 5000) 万份 * 2 元 = 30000 万元 = 3 亿元 / 1000 亿元 = 0.3%
    assert first["aum_yi"] == pytest.approx(3.0)
    assert first["float_mv_yi"] == pytest.approx(1000.0)
    assert first["ratio_pct"] == pytest.approx(0.3)
    assert first["index_close"] == pytest.approx(4000.0)
    assert payload["latest"]["trade_date"] == "20260107"
    # (12000 + 6000) * 2 = 3.6 亿元 / 1200 亿元 = 0.3%
    assert payload["latest"]["ratio_pct"] == pytest.approx(0.3)
    assert payload["latest"]["index_close"] == pytest.approx(4200.0)


@pytest.mark.unit
def test_compute_penetration_rejects_unknown_index(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    with pytest.raises(ValueError):
        etf_penetration.compute_penetration(_collections(), index_key="nope", days=0)


@pytest.mark.unit
def test_penetration_router_returns_series_and_indices(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    monkeypatch.setattr(market_stats, "get_data_db", _collections)

    res = _client().get("/api/market-stats/etf-penetration", params={"index": "test", "days": 0})
    assert res.status_code == 200
    body = res.json()
    assert body["index"]["key"] == "test"
    assert body["available_indices"][0]["key"] == "test"
    assert len(body["series"]) == 2
    assert body["latest"]["ratio_pct"] == pytest.approx(0.3)


@pytest.mark.unit
def test_penetration_router_rejects_unknown_index(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    monkeypatch.setattr(market_stats, "get_data_db", _collections)

    res = _client().get("/api/market-stats/etf-penetration", params={"index": "nope"})
    assert res.status_code == 400


# --------------------------------------------------------------- 阶段判定
def _series_collections(ratios, index_close=None):
    """按给定的渗透率序列反推份额，指数市值固定为 1000 亿。

    ratio_pct = aum_yi / 1000 * 100  =>  aum_yi = ratio_pct * 10
    aum_wan = shares * close，取 close=2，两只有 ETF，故 shares = aum_wan / 4
    """

    shares, closes, basic, prices = [], [], [], []
    for i, r in enumerate(ratios):
        day = f"2026{1 + i // 28:02d}{1 + i % 28:02d}"
        aum_yi = r * 10.0                 # 亿元
        total_wan = aum_yi * 1e4          # 万元
        per_fund_wan = total_wan / 4.0    # close=2, 两只合计 = shares*2*2
        for code in ("AAA.SH", "BBB.SH"):
            shares.append({"ts_code": code, "trade_date": day, "fd_share": per_fund_wan / 2.0})
            closes.append({"ts_code": code, "trade_date": day, "close": 2.0})
        basic.append({"ts_code": "TEST.IDX", "trade_date": day, "float_mv": 1.0e11})
        prices.append({"ts_code": "TEST.IDX", "trade_date": day, "close": 4000.0})
    return {
        "etf_shares": _Coll(shares),
        "etf_daily": _Coll(closes),
        "index_daily_basic": _Coll(basic),
        "index_prices": _Coll(prices),
    }


@pytest.mark.unit
def test_phase_helpers_band_and_labels():
    # ±2% / 20 日是分界；分位 35/65 决定「底部」还是「高位」
    assert etf_penetration._penetration_phase(5.0, 20.0) == "bottom_refill"
    assert etf_penetration._penetration_phase(5.0, 60.0) == "inflow"
    assert etf_penetration._penetration_phase(-5.0, 80.0) == "top_exit"
    assert etf_penetration._penetration_phase(-5.0, 20.0) == "outflow"
    assert etf_penetration._penetration_phase(1.0, 50.0) == "flat"
    assert etf_penetration._penetration_phase(None, 50.0) == "unknown"


@pytest.mark.unit
def test_pctile_rank_averages_ties():
    assert etf_penetration._pctile_rank([1.0, 2.0, 2.0, 2.0, 5.0], 2.0) == 50.0
    assert etf_penetration._pctile_rank([], 1.0) is None


@pytest.mark.unit
def test_summary_reports_chg_and_phase_for_rising_series(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    # 连续上升：第 i 天 = 1.0 * 1.01^i，20 日约 +22%
    ratios = [1.0 * (1.01 ** i) for i in range(60)]
    payload = etf_penetration.compute_penetration(_series_collections(ratios), index_key="test", days=0)

    summary = payload["summary"]
    assert summary is not None
    assert summary["chg_20d"] == pytest.approx((1.01 ** 20 - 1) * 100, abs=0.05)
    # 序列单调上升 => 当前值就是最高点，分位接近 100
    assert summary["pctile_all"] > 95
    assert summary["phase"] == "inflow"
    assert summary["phase_label"] == "持续流入"
    assert summary["peaks"]["max_date"] == payload["series"][-1]["trade_date"]


@pytest.mark.unit
def test_percentiles_use_full_history_not_the_truncated_window(monkeypatch):
    """days 截断只影响返回的 series，不能影响分位与阶段。

    否则「1 年分位」会退化成「当前窗口内的分位」，换个回看区间结论就变。
    """
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    # 前 250 天在 1.0 附近横盘，最后 20 天拉到 3.0
    ratios = [1.0] * 250 + [1.0 + 0.1 * i for i in range(1, 21)]
    full = etf_penetration.compute_penetration(_series_collections(ratios), index_key="test", days=0)
    short = etf_penetration.compute_penetration(_series_collections(ratios), index_key="test", days=20)

    assert len(short["series"]) == 20
    assert len(full["series"]) == len(ratios)
    # 关键：截断后 summary 与全量完全一致
    assert short["summary"]["pctile_all"] == full["summary"]["pctile_all"]
    assert short["summary"]["pctile_1y"] == full["summary"]["pctile_1y"]
    assert short["summary"]["phase"] == full["summary"]["phase"]
    # 拉高之后应处在全样本高位
    assert short["summary"]["pctile_all"] > 80
