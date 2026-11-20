"""Unit tests for the national-team entry ladder (pure helpers, no Mongo)."""

from services import etf_national_team as nt


def _series(start=20260101, n=30, share0=1_000_000.0, growth=0.01):
    dates = [str(start + i) for i in range(n)]
    values = [share0 * (1 + growth) ** i for i in range(n)]
    return dates, values


def test_daily_flow_is_one_percent_when_shares_grow_one_percent():
    dates, values = _series()
    flows = nt._ladder_daily_flows(
        {"510300.SH": (dates, values)},
        {"510300.SH": {d: 1.0 for d in dates}},
        {"510300.SH": {d: 1.0 for d in dates}},
    )
    # The first 20 rows are skipped as listing flow; afterwards each day is
    # exactly +1% of the previous AUM.
    for day in dates[20:]:
        assert flows[day] is not None
        assert abs(flows[day] - 1.0) < 1e-9


def test_split_is_not_counted_as_a_redemption():
    dates = [str(20260101 + i) for i in range(25)]
    values = [1_000_000.0] * 25
    closes = {d: 1.0 for d in dates}
    factors = {d: 1.0 for d in dates}
    # 1:2 split: shares halve on day 22, the adj factor halves on day 23 and
    # the raw price doubles on day 23 (Tushare dates differ by one session).
    values[22:] = [500_000.0] * 3
    closes[dates[23]] = 2.0
    closes[dates[24]] = 2.0
    factors[dates[23]] = 0.5
    factors[dates[24]] = 0.5
    flows = nt._ladder_daily_flows(
        {"510300.SH": (dates, values)},
        {"510300.SH": closes},
        {"510300.SH": factors},
    )
    # Both sides of the split are dropped instead of showing -50% / +100%.
    assert flows.get(dates[22]) is None
    assert flows.get(dates[23]) is None
    assert flows.get(dates[21]) is not None
    assert abs(flows[dates[21]]) < 1e-9


def test_rolling_sum_respects_min_periods():
    values = [1.0] * 10
    r5 = nt._rolling_sum(values, 5, 3)
    assert r5[1] is None
    assert r5[2] == 3.0
    assert r5[9] == 5.0


def test_episode_confirms_after_five_sessions_and_ends_after_ten():
    dates = [str(20260101 + i) for i in range(20)]
    hs = {d: 100.0 + i for i, d in enumerate(dates)}
    flow60 = [6.0] * 6 + [4.0] * 14
    episode = nt._ladder_episode(dates, flow60, hs, dates[9])
    assert episode["active"] is True
    assert episode["start"] == dates[0]
    assert episode["p0"] == 100.0
    assert episode["days"] == 10

    flow60 = [6.0] * 4 + [4.0] * 16
    episode = nt._ladder_episode(dates, flow60, hs, dates[9])
    assert episode["active"] is False
    assert episode["start"] is None

    flow60 = [6.0] * 6 + [4.0] * 14
    episode = nt._ladder_episode(dates, flow60, hs, dates[-1])
    assert episode["active"] is False
    assert episode["ended"] is True
    assert episode["start"] == dates[0]


def test_tiers_trigger_at_drawdown_levels_and_keep_t4():
    tiers = nt._ladder_tiers(p0=100.0, price=88.0, active=True, ended=False)
    by_id = {t["id"]: t for t in tiers}
    assert by_id["T1"]["price"] == 90.0
    assert by_id["T1"]["status"] == "triggered"
    assert by_id["T2"]["status"] == "pending"
    assert by_id["T2"]["distance_pct"] < 0
    assert by_id["T4"]["status"] == "pending"

    tiers = nt._ladder_tiers(p0=100.0, price=80.0, active=True, ended=False)
    assert [t["status"] for t in tiers[:3]] == ["triggered"] * 3

    tiers = nt._ladder_tiers(
        p0=100.0, price=88.0, active=False, ended=True, t4_recent=True
    )
    assert tiers[-1]["id"] == "T4"
    assert tiers[-1]["status"] == "triggered"

    # An episode that ended more than 60 sessions ago is not a live T4 trigger.
    tiers = nt._ladder_tiers(
        p0=100.0, price=88.0, active=False, ended=True, t4_recent=False
    )
    assert tiers[-1]["status"] == "pending"
