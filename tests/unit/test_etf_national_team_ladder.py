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


def test_tiers_use_the_selected_index_stats():
    spec = nt._LADDER_INDEX_SPECS["000905.SH"]
    tiers = nt._ladder_tiers(100.0, 88.0, True, False, index_spec=spec)
    by_id = {t["id"]: t for t in tiers}
    # CSI500's own forward path, not the HS300 row.
    assert by_id["T1"]["hist_reach"] == "4/6"
    assert by_id["T1"]["hist_f60"] == 1.5
    assert by_id["T2"]["hist_f60"] == 11.3
    assert by_id["T3"]["hist_post_dd"] == -5.1
    assert by_id["T4"]["hist_f250"] == 4.2


def test_every_index_spec_has_full_tier_stats():
    for code, spec in nt._LADDER_INDEX_SPECS.items():
        assert spec["episodes"] >= 4, code
        assert spec["regime_a"] + spec["regime_b"] == spec["episodes"], code
        for tier_id in ("T1", "T2", "T3"):
            keys = set(spec["tiers"][tier_id])
            assert {"reach", "f60", "f120", "post_dd"} <= keys, (code, tier_id)
        assert {"reach", "f60", "f120", "f250"} <= set(spec["tiers"]["T4"]), code


def test_t4_requires_the_study_size_filter():
    # A real episode: cumulative >= 10% and >= 15 sessions.
    assert nt._t4_qualified(10.0, 15) is True
    assert nt._t4_qualified(80.0, 60) is True
    # The 2025-04 run: flow60 was elevated but the run netted ~0.
    assert nt._t4_qualified(-0.1, 69) is False
    assert nt._t4_qualified(9.9, 69) is False
    assert nt._t4_qualified(50.0, 14) is False
    assert nt._t4_qualified(None, 69) is False

    # Ended and recent, but not qualified: T4 stays pending instead of firing.
    tiers = nt._ladder_tiers(
        p0=100.0,
        price=88.0,
        active=False,
        ended=True,
        t4_recent=True,
        t4_qualified=False,
    )
    assert tiers[-1]["id"] == "T4"
    assert tiers[-1]["status"] == "pending"

    tiers = nt._ladder_tiers(
        p0=100.0,
        price=88.0,
        active=False,
        ended=True,
        t4_recent=True,
        t4_qualified=True,
    )
    assert tiers[-1]["status"] == "triggered"


def test_tier_triggers_carry_the_index_session_date():
    dates = ["20260101", "20260102", "20260105", "20260106"]
    closes = {dates[0]: 100.0, dates[1]: 89.0, dates[2]: 84.0, dates[3]: 79.0}
    tiers = nt._ladder_tiers(100.0, 79.0, True, False)
    nt._annotate_tier_triggers(tiers, dates, closes, dates[0])
    by_id = {t["id"]: t for t in tiers}
    assert by_id["T1"]["trigger_date"] == dates[1]
    assert by_id["T1"]["trigger_days"] == 2
    assert by_id["T2"]["trigger_date"] == dates[2]
    assert by_id["T2"]["trigger_days"] == 3
    assert by_id["T3"]["trigger_date"] == dates[3]
    assert by_id["T3"]["trigger_days"] == 4
    assert by_id["T4"]["trigger_date"] is None

    # Pending tiers are never dated.
    tiers = nt._ladder_tiers(100.0, 95.0, True, False)
    nt._annotate_tier_triggers(tiers, dates, {d: 95.0 for d in dates}, dates[0])
    assert all(t["trigger_date"] is None for t in tiers)

    # T4 (buy after accumulation ends) uses the episode end date.
    tiers = nt._ladder_tiers(100.0, 80.0, False, True, t4_recent=True)
    nt._annotate_tier_triggers(tiers, dates, closes, dates[0], {"end": dates[2]})
    assert tiers[-1]["id"] == "T4"
    assert tiers[-1]["trigger_date"] == dates[2]
    assert tiers[-1]["trigger_days"] == 3


def test_ladder_rejects_unknown_index():
    import pytest

    with pytest.raises(ValueError, match="unsupported index"):
        nt.compute_entry_ladder(object(), index="000000.SH")
