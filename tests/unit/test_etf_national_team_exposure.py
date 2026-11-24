"""Unit tests for the national-team exposure series (pure helpers, no Mongo)."""

import pytest

from services import etf_national_team as nt


@pytest.fixture(autouse=True)
def _clear_exposure_cache():
    """compute_exposure 有 5 分钟模块级缓存，key 只含 (codes, days)。

    不同测试用同样的 pool + days 会命中上一次的 payload，必须每个用例前清空。
    """
    nt._EXPOSURE_CACHE.clear()
    yield
    nt._EXPOSURE_CACHE.clear()


# ---------------------------------------------------------------- phase
def test_phase_bottom_refill_requires_low_percentile():
    # 20 日 +5% 但分位只有 20% => 底部回补；分位 60% => 只是持续流入
    assert nt._exposure_phase(5.0, 20.0) == "bottom_refill"
    assert nt._exposure_phase(5.0, 60.0) == "inflow"


def test_phase_top_exit_requires_high_percentile():
    assert nt._exposure_phase(-5.0, 80.0) == "top_exit"
    assert nt._exposure_phase(-5.0, 20.0) == "outflow"


def test_phase_flat_inside_threshold_band():
    # ±2%/20 日是分界，不能把噪声判成方向
    assert nt._exposure_phase(1.9, 10.0) == "flat"
    assert nt._exposure_phase(-1.9, 90.0) == "flat"
    assert nt._exposure_phase(0.0, 50.0) == "flat"


def test_phase_unknown_when_inputs_missing():
    assert nt._exposure_phase(None, 50.0) == "unknown"
    assert nt._exposure_phase(5.0, None) == "unknown"


# ---------------------------------------------------------------- percentile
def test_pctile_rank_bounds():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert nt._pctile_rank(vals, 1.0) == 10.0     # 最低值
    assert nt._pctile_rank(vals, 5.0) == 90.0     # 最高值
    assert nt._pctile_rank(vals, 3.0) == 50.0     # 中位


def test_pctile_rank_handles_ties():
    # 三个相同值的并列：应落在区间的中点，而不是全算最高或最低
    vals = [1.0, 2.0, 2.0, 2.0, 5.0]
    assert nt._pctile_rank(vals, 2.0) == 50.0


def test_pctile_rank_empty_is_none():
    assert nt._pctile_rank([], 1.0) is None


# ---------------------------------------------------------------- exposure series
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, *_args, **_kwargs):
        return _FakeCursor(self._docs)


class _FakeDb:
    """Minimal stand-in: only etf_shares and the pool collection are read."""

    def __init__(self, pool_docs, share_docs):
        self._pool = pool_docs
        self._shares = share_docs

    def __getitem__(self, name):
        if name == "etf_national_team_pool":
            return _FakeCollection(self._pool)
        return _FakeCollection(self._shares)


def _shares(codes, dates, value):
    out = []
    for d in dates:
        for c in codes:
            out.append({"ts_code": c, "trade_date": d, "fd_share": value})
    return out


def test_exposure_skips_dates_missing_any_pool_etf():
    """覆盖不全的日子必须整日剔除，否则分母漂移到「部分 ETF 之和」。"""
    codes = ["510300.SH", "510310.SH"]
    pool = [
        {"ts_code": "510300.SH", "name": "A", "holder_pct": 80.0, "enabled": True},
        {"ts_code": "510310.SH", "name": "B", "holder_pct": 60.0, "enabled": True},
    ]
    dates = ["20260101", "20260102", "20260105"]
    docs = _shares(codes, dates, 1_000_000.0)
    docs = [d for d in docs if not (d["trade_date"] == "20260102" and d["ts_code"] == "510310.SH")]
    payload = nt.compute_exposure(_FakeDb(pool, docs), days=50)

    assert "error" not in payload
    assert [r["trade_date"] for r in payload["series"]] == ["20260101", "20260105"]
    assert payload["as_of"] == "20260105"


def test_exposure_reports_50_pct_growth_over_two_days():
    codes = ["510300.SH"]
    pool = [{"ts_code": "510300.SH", "name": "A", "holder_pct": 80.0, "enabled": True}]
    dates = [f"2026010{i}" for i in range(1, 10)] + ["20260110"]
    docs = []
    for i, d in enumerate(dates):
        # 万分: 第 1 天 1e6，往后每天 ×1.05
        docs.append({"ts_code": "510300.SH", "trade_date": d, "fd_share": 1_000_000.0 * (1.05 ** i)})
    payload = nt.compute_exposure(_FakeDb(pool, docs), days=50)

    series = payload["series"]
    # 第 2 天相对第 1 天 +5%
    assert abs(series[1]["chg_1d"] - 5.0) < 0.05
    # 估算国家队 = 合计 × 80%（两个字段都 round 到 1 位小数，留 0.1 容差）
    assert abs(series[-1]["est_national_yi"] - series[-1]["shares_yi"] * 0.8) < 0.1


def test_exposure_returns_error_when_no_full_coverage():
    pool = [
        {"ts_code": "510300.SH", "name": "A", "holder_pct": 80.0, "enabled": True},
        {"ts_code": "510310.SH", "name": "B", "holder_pct": 60.0, "enabled": True},
    ]
    docs = _shares(["510300.SH"], ["20260101"], 1_000_000.0)
    payload = nt.compute_exposure(_FakeDb(pool, docs), days=50)
    assert payload.get("error") == "no_full_coverage_dates"
