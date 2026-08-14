"""Unit tests for services.etf_heatmap pure functions."""
import pytest

from services.etf_heatmap import (
    ETF_HEATMAP_BUCKETS,
    compute_bucket_window_cell,
    daily_net_inflow_wan,
    parse_window,
    pick_top_members,
    to_float,
)


def test_parse_window_valid():
    assert parse_window("5d") == 5
    assert parse_window("1D") == 1


def test_parse_window_invalid():
    with pytest.raises(ValueError):
        parse_window("7d")


def test_daily_net_inflow_wan_uses_nav():
    row_t = {"total_share": 110.0, "nav": 2.0}
    row_prev = {"total_share": 100.0}
    # delta 10 万份 * 2 元 = 20 万元
    assert daily_net_inflow_wan(row_t, row_prev) == pytest.approx(20.0)


def test_daily_net_inflow_falls_back_to_close():
    row_t = {"total_share": 105.0, "close": 1.5}
    row_prev = {"total_share": 100.0}
    assert daily_net_inflow_wan(row_t, row_prev) == pytest.approx(7.5)


def test_daily_net_inflow_falls_back_to_implied_nav_from_size():
    row_t = {"total_share": 105.0, "total_size": 210.0}
    row_prev = {"total_share": 100.0}
    # implied NAV = 210 / 105 = 2 元/份
    assert daily_net_inflow_wan(row_t, row_prev) == pytest.approx(10.0)


def test_pick_top_members_by_total_size():
    as_of = "20250410"
    rows = {
        "512760.SH": {"total_size": 500.0},
        "159995.SZ": {"total_size": 800.0},
        "588200.SH": {"total_size": 300.0},
        "512480.SH": {"total_size": 600.0},
    }
    top = pick_top_members(
        ["512760.SH", "159995.SZ", "588200.SH", "512480.SH"],
        as_of,
        rows,
        limit=3,
    )
    assert top == ["159995.SZ", "512480.SH", "512760.SH"]


def test_compute_bucket_window_cell_sums_window():
    members = ["A.SH"]
    dates = ["20250410", "20250409", "20250408", "20250407"]
    share_by_key = {
        ("A.SH", "20250410"): {"total_share": 110.0, "total_size": 220.0, "nav": 2.0},
        ("A.SH", "20250409"): {"total_share": 100.0, "total_size": 200.0, "nav": 2.0},
        ("A.SH", "20250408"): {"total_share": 95.0, "total_size": 190.0, "nav": 2.0},
        ("A.SH", "20250407"): {"total_share": 90.0, "total_size": 180.0, "nav": 2.0},
    }
    out = compute_bucket_window_cell(members, dates, 2, share_by_key)
    # day 20250410: +10*2=20; day 20250409: +5*2=10 => 30
    assert out["net_inflow"] == pytest.approx(30.0)
    assert out["size_start"] == pytest.approx(190.0)
    assert out["inflow_rate"] == pytest.approx(30.0 / 190.0, rel=1e-4)
    assert out["share_chg_pct"] == pytest.approx((110.0 - 95.0) / 95.0, rel=1e-4)


def test_bucket_candidates_exclude_feeder_names_by_curation():
    """Curated lists must not include explicit 联接 codes (manual universe rule)."""
    all_codes = [c for b in ETF_HEATMAP_BUCKETS for c in b["candidates"]]
    for code in all_codes:
        assert code.endswith((".SH", ".SZ")), code


def test_to_float_decimal128():
    from bson.decimal128 import Decimal128

    assert to_float(Decimal128("1.25")) == pytest.approx(1.25)
