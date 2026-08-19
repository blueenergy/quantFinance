"""Unit tests for services.etf_heatmap pure functions."""
import pytest

from services.etf_heatmap import (
    ETF_HEATMAP_BUCKETS,
    build_bucket_series_payload,
    build_heatmap_payload,
    compute_bucket_window_cell,
    coverage_as_of_date,
    daily_net_inflow_wan,
    parse_window,
    pick_top_members,
    price_for_inflow,
    rows_on_or_before,
    to_float,
    WindowSpec,
)

pytestmark = pytest.mark.unit


class FakeShareStore:
    def __init__(self, rows, names=None):
        self._rows = [dict(r) for r in rows]
        self._names = names or {}

    def latest_trade_date(self):
        if not self._rows:
            return None
        return max(str(r["trade_date"]) for r in self._rows)

    def coverage_as_of(self, codes):
        latest = {}
        wanted = set(codes)
        for r in self._rows:
            code = r.get("ts_code")
            if code not in wanted:
                continue
            td = str(r["trade_date"])
            if code not in latest or td > latest[code]:
                latest[code] = td
        return coverage_as_of_date(latest)

    def load_rows(self, codes, start_date, end_date):
        wanted = set(codes)
        return [
            dict(r)
            for r in self._rows
            if r.get("ts_code") in wanted
            and str(start_date) <= str(r.get("trade_date", "")) <= str(end_date)
        ]

    def names_for(self, codes):
        return {c: self._names.get(c, c) for c in codes}


def test_parse_window_valid():
    assert parse_window("5d").sessions == 5
    assert parse_window("1D").key == "1d"
    assert parse_window("60d").sessions == 60
    assert parse_window("120d").long is True
    assert parse_window("ytd").kind == "ytd"
    assert parse_window("ytd").sessions is None


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
    dates_by_code = {"A.SH": dates}
    out = compute_bucket_window_cell(
        members,
        WindowSpec("2d", "sessions", 2),
        share_by_key,
        dates_by_code,
        "20250410",
    )
    # day 20250410: +10*2=20; day 20250409: +5*2=10 => 30
    assert out["net_inflow"] == pytest.approx(30.0)
    assert out["size_start"] == pytest.approx(190.0)
    assert out["inflow_rate"] == pytest.approx(30.0 / 190.0, rel=1e-4)
    assert out["share_chg_pct"] == pytest.approx((110.0 - 95.0) / 95.0, rel=1e-4)
    assert out["coverage_days"] == 2


def test_bucket_candidates_exclude_feeder_names_by_curation():
    """Curated lists must not include explicit 联接 codes (manual universe rule)."""
    all_codes = [c for b in ETF_HEATMAP_BUCKETS for c in b["candidates"]]
    for code in all_codes:
        assert code.endswith((".SH", ".SZ")), code
    assert "161725.SZ" not in all_codes


def test_theme_buckets_do_not_share_candidate_codes():
    seen = {}
    for bucket in ETF_HEATMAP_BUCKETS:
        if bucket["kind"] != "theme":
            continue
        for code in bucket["candidates"]:
            assert code not in seen, f"{code} in {seen[code]} and {bucket['id']}"
            seen[code] = bucket["id"]


def test_price_for_inflow_uses_implied_nav_when_nav_and_close_missing():
    assert price_for_inflow({"total_share": 100.0, "total_size": 250.0}) == pytest.approx(2.5)


def test_to_float_decimal128():
    from bson.decimal128 import Decimal128

    assert to_float(Decimal128("1.25")) == pytest.approx(1.25)


def test_window_uses_per_code_consecutive_rows_not_union_calendar():
    """A missing session on one fund must pair with that fund's previous row."""
    members = ["A.SH"]
    share_by_key = {
        ("A.SH", "20250410"): {"total_share": 110.0, "total_size": 220.0, "nav": 2.0},
        ("A.SH", "20250408"): {"total_share": 100.0, "total_size": 200.0, "nav": 2.0},
        ("A.SH", "20250407"): {"total_share": 90.0, "total_size": 180.0, "nav": 2.0},
        ("B.SH", "20250409"): {"total_share": 1.0, "total_size": 1.0, "nav": 1.0},
    }
    dates_by_code = {
        "A.SH": ["20250410", "20250408", "20250407"],
        "B.SH": ["20250409"],
    }
    out = compute_bucket_window_cell(
        members, parse_window("1d"), share_by_key, dates_by_code, "20250410"
    )
    # 10 vs A's own previous obs 08: +10 万份 * 2 = 20 万元 (union 09 would have dropped this)
    assert out["net_inflow"] == pytest.approx(20.0)
    assert out["size_start"] == pytest.approx(200.0)


def test_coverage_as_of_date_ignores_early_sync_outlier():
    latest = {
        "510300.SH": "20250410",
        "159919.SZ": "20250410",
        "510330.SH": "20250410",
        "512760.SH": "20250411",
    }
    assert coverage_as_of_date(latest) == "20250410"


def test_coverage_as_of_date_moves_when_pack_arrives():
    latest = {
        "510300.SH": "20250411",
        "159919.SZ": "20250411",
        "510330.SH": "20250411",
        "512760.SH": "20250410",
    }
    assert coverage_as_of_date(latest) == "20250411"


def test_coverage_as_of_date_tie_breaks_earlier():
    assert coverage_as_of_date({"A": "20250411", "B": "20250410"}) == "20250410"


def test_rows_on_or_before_uses_latest_not_exact_as_of():
    share_by_key = {
        ("A.SH", "20250409"): {"total_size": 100.0},
        ("B.SH", "20250410"): {"total_size": 200.0},
    }
    dates_by_code = {"A.SH": ["20250409"], "B.SH": ["20250410"]}
    rows = rows_on_or_before(["A.SH", "B.SH"], "20250410", share_by_key, dates_by_code)
    assert rows["A.SH"]["total_size"] == pytest.approx(100.0)
    assert rows["B.SH"]["total_size"] == pytest.approx(200.0)


def test_heatmap_payload_does_not_empty_when_one_fund_syncs_ahead():
    rows = [
        {
            "ts_code": "999999.SH",
            "trade_date": "20250412",
            "total_share": 1.0,
            "total_size": 1.0,
            "nav": 1.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250411",
            "total_share": 1010.0,
            "total_size": 2020.0,
            "nav": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250410",
            "total_share": 1000.0,
            "total_size": 2000.0,
            "nav": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250409",
            "total_share": 990.0,
            "total_size": 1980.0,
            "nav": 2.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250410",
            "total_share": 500.0,
            "total_size": 1500.0,
            "nav": 3.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250409",
            "total_share": 490.0,
            "total_size": 1470.0,
            "nav": 3.0,
        },
        {
            "ts_code": "510300.SH",
            "trade_date": "20250410",
            "total_share": 100.0,
            "total_size": 100.0,
            "nav": 1.0,
        },
        {
            "ts_code": "159919.SZ",
            "trade_date": "20250410",
            "total_share": 100.0,
            "total_size": 100.0,
            "nav": 1.0,
        },
        {
            "ts_code": "510330.SH",
            "trade_date": "20250410",
            "total_share": 100.0,
            "total_size": 100.0,
            "nav": 1.0,
        },
    ]
    payload = build_heatmap_payload(FakeShareStore(rows), window="1d")
    assert payload["trade_date"] == "20250410"
    semi = next(c for c in payload["cells"] if c["bucket_id"] == "semiconductor")
    assert {m["ts_code"] for m in semi["members"]} >= {"512760.SH", "159995.SZ"}
    assert semi["net_inflow"] == pytest.approx(50.0)


def test_heatmap_payload_on_or_before_keeps_lagging_members():
    rows = [
        {
            "ts_code": "512760.SH",
            "trade_date": "20250411",
            "total_share": 1010.0,
            "total_size": 2020.0,
            "nav": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250410",
            "total_share": 1000.0,
            "total_size": 2000.0,
            "nav": 2.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250410",
            "total_share": 500.0,
            "total_size": 1500.0,
            "nav": 3.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250409",
            "total_share": 490.0,
            "total_size": 1470.0,
            "nav": 3.0,
        },
    ]
    payload = build_heatmap_payload(
        FakeShareStore(rows), window="1d", trade_date="20250411"
    )
    semi = next(c for c in payload["cells"] if c["bucket_id"] == "semiconductor")
    assert {m["ts_code"] for m in semi["members"]} >= {"512760.SH", "159995.SZ"}


def test_series_pairs_each_code_with_own_previous_row():
    rows = [
        {
            "ts_code": "512760.SH",
            "trade_date": "20250410",
            "total_share": 110.0,
            "total_size": 220.0,
            "nav": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250408",
            "total_share": 100.0,
            "total_size": 200.0,
            "nav": 2.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250410",
            "total_share": 50.0,
            "total_size": 150.0,
            "nav": 3.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250409",
            "total_share": 40.0,
            "total_size": 120.0,
            "nav": 3.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250408",
            "total_share": 40.0,
            "total_size": 120.0,
            "nav": 3.0,
        },
    ]
    payload = build_bucket_series_payload(
        FakeShareStore(rows, names={"512760.SH": "半导体ETF", "159995.SZ": "芯片ETF"}),
        "semiconductor",
        limit=10,
        trade_date="20250410",
    )
    by_td = {p["trade_date"]: p for p in payload["series"]}
    # 0410: 512760 vs 08 (+10*2=20) + 159995 vs 09 (+10*3=30)
    assert by_td["20250410"]["net_inflow"] == pytest.approx(50.0)
    # 0409: 512760 has no row; 159995 vs 08 is flat
    assert by_td["20250409"]["net_inflow"] == pytest.approx(0.0)


def test_coverage_days_counts_pairs_not_nominal_window():
    """A 60d request with only 4 sessions reports coverage_days=3, not 60."""
    members = ["A.SH"]
    dates = ["20250410", "20250409", "20250408", "20250407"]
    share_by_key = {
        ("A.SH", td): {
            "total_share": 100.0 + i,
            "total_size": 200.0 + 2 * i,
            "nav": 2.0,
        }
        for i, td in enumerate(reversed(dates))
    }
    dates_by_code = {"A.SH": dates}
    out = compute_bucket_window_cell(
        members, parse_window("60d"), share_by_key, dates_by_code, "20250410"
    )
    assert out["coverage_days"] == 3
    # share 103 vs 100 over the 3 pairs
    assert out["share_chg_pct"] == pytest.approx(3.0 / 100.0, rel=1e-4)


def test_ytd_window_includes_pair_from_prior_year_end():
    members = ["A.SH"]
    share_by_key = {
        ("A.SH", "20250410"): {"total_share": 120.0, "total_size": 240.0, "nav": 2.0},
        ("A.SH", "20250409"): {"total_share": 110.0, "total_size": 220.0, "nav": 2.0},
        ("A.SH", "20241231"): {"total_share": 100.0, "total_size": 200.0, "nav": 2.0},
        ("A.SH", "20241230"): {"total_share": 90.0, "total_size": 180.0, "nav": 2.0},
    }
    dates_by_code = {
        "A.SH": ["20250410", "20250409", "20241231", "20241230"],
    }
    out = compute_bucket_window_cell(
        members, parse_window("ytd"), share_by_key, dates_by_code, "20250410"
    )
    # 0410 vs 0409: +10*2=20; 0409 vs 1231: +10*2=20; 1231 is before year start so not a YTD later-date
    assert out["net_inflow"] == pytest.approx(40.0)
    assert out["share_start"] == pytest.approx(100.0)
    assert out["share_chg_pct"] == pytest.approx(20.0 / 100.0, rel=1e-4)
    assert out["coverage_days"] == 2


def test_heatmap_payload_long_window_meta():
    rows = [
        {
            "ts_code": "512760.SH",
            "trade_date": "20250410",
            "total_share": 1000.0,
            "total_size": 2000.0,
            "nav": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250409",
            "total_share": 990.0,
            "total_size": 1980.0,
            "nav": 2.0,
        },
    ]
    payload = build_heatmap_payload(
        FakeShareStore(rows), window="60d", trade_date="20250410"
    )
    assert payload["window"] == "60d"
    assert payload["window_target_days"] == 60
    assert payload["window_long"] is True
    semi = next(c for c in payload["cells"] if c["bucket_id"] == "semiconductor")
    assert semi["coverage_days"] == 1
    assert semi["share_chg_pct"] == pytest.approx((1000.0 - 990.0) / 990.0, rel=1e-4)
