"""日内 T+0 止损回放单测（合成 bar 路径，不碰 DB）。

最强的一条是 `test_huge_stop_reproduces_baseline_exactly`：止损设到 99% 时
止损腿必须**逐条等于** baseline。任何偏差都是模拟器的 bug —— 这条比逐项断言
更能兜住成交价、方向、下标三处都出错的情况。
"""

from __future__ import annotations

import pytest

from services import intraday_t0_stop_replay as replay


def _labels(first: int, count: int):
    """从 HHMM 起每分钟递增的标签；跨小时正确进位（0959 → 1000）。"""
    total = (first // 100) * 60 + (first % 100)
    return [f"{(total + i) // 60:02d}{(total + i) % 60:02d}" for i in range(count)]


def bars_from(rows, first: int = 931, labels=None):
    """`[(o, h, l, c), ...]` → bar 列表。"""
    tags = labels if labels is not None else _labels(first, len(rows))
    assert len(tags) == len(rows)
    return [
        {"label": t, "open": o, "high": h, "low": lo, "close": c}
        for t, (o, h, lo, c) in zip(tags, rows)
    ]


def flat(n: int, price: float = 10.0):
    return [(price, price, price, price)] * n


# --------------------------------------------------------------------------
# 判据与定位
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_directional_pnl_folds_sell_direction():
    # SELL 跌了才赚 → 价格空间 +1% 对 SELL 是亏 1%
    assert replay.directional_pnl_pct(1.0, "BUY_WATCH") == pytest.approx(1.0)
    assert replay.directional_pnl_pct(-1.0, "BUY_WATCH") == pytest.approx(-1.0)
    assert replay.directional_pnl_pct(1.0, "SELL_WATCH") == pytest.approx(-1.0)
    assert replay.directional_pnl_pct(-1.0, "SELL_WATCH") == pytest.approx(1.0)


@pytest.mark.unit
def test_net_pnl_is_profit_minus_cost_for_both_styles():
    # 两种风格的净口径都应读作「毛利 − 成本」
    assert replay._net_pnl(1.0, "BUY_WATCH", 0.07) == pytest.approx(0.93)
    # SELL：价格跌 1% = 毛利 +1，净 +0.93
    assert replay._net_pnl(-1.0, "SELL_WATCH", 0.07) == pytest.approx(0.93)


@pytest.mark.unit
def test_locate_entry_takes_last_bar_at_or_before_snapshot():
    bars = bars_from(flat(6), first=931)  # 0931..0936
    assert replay.locate_entry(bars, "202609180932") == 1
    assert replay.locate_entry(bars, "202609180933") == 2
    # 落在两根之间 → 取前一根，不取后一根（不用未来信息）
    assert replay.locate_entry(bars, "202609180931") == 0


@pytest.mark.unit
def test_locate_entry_rejects_before_first_bar_and_bad_input():
    bars = bars_from(flat(3), first=931)
    assert replay.locate_entry(bars, "202609180930") is None
    assert replay.locate_entry(bars, "20260918") is None
    assert replay.locate_entry([], "202609180931") is None


@pytest.mark.unit
def test_exit_index_returns_none_when_bars_run_out():
    bars = bars_from(flat(10))
    assert replay.exit_index(bars, 0, "5m") == 5
    # 不是夹到最后一根 —— 否则「60m」会被静默算成「4m」
    assert replay.exit_index(bars, 0, "60m") is None
    assert replay.exit_index(bars, 9, "5m") is None


@pytest.mark.unit
def test_exit_index_close_means_last_bar_of_day():
    bars = bars_from(flat(10))
    assert replay.exit_index(bars, 0, "close") == 9
    assert replay.exit_index(bars, 9, "close") == 9


@pytest.mark.unit
def test_exit_index_rejects_unknown_horizon():
    with pytest.raises(ValueError):
        replay.exit_index(bars_from(flat(3)), 0, "45m")


@pytest.mark.unit
def test_index_advance_skips_lunch_break():
    """下标推进天然跳过午休 —— 墙上时钟算术会造出不存在的分钟。"""
    tags = ["1128", "1129", "1130", "1301", "1302", "1303", "1304"]
    bars = bars_from(flat(7), labels=tags)
    entry = replay.locate_entry(bars, "202609181129")
    assert entry == 1
    exit_i = replay.exit_index(bars, entry, "5m")
    assert exit_i == 6
    # 关键：落在 1304，而不是不存在的「1134」
    assert bars[exit_i]["label"] == "1304"


# --------------------------------------------------------------------------
# 成交模型
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_entry_bar_never_triggers():
    """入场那根 bar 的振幅再大也不判定 —— 同一分钟内没有可交易的前视空间。"""
    rows = [(10.0, 10.90, 9.10, 10.0)] + flat(5, 10.0)
    bars = bars_from(rows)
    for style in ("BUY_WATCH", "SELL_WATCH"):
        case = replay.build_case(bars, 0, "5m", style)
        res = replay.apply_stop(case, bars, style, 1.0, "fixed")
        assert res["triggered"] is False


@pytest.mark.unit
def test_short_stop_triggers_on_high_breach():
    rows = [(10.0, 10.0, 10.0, 10.0)] + [(10.04, 10.06, 10.02, 10.04)] + flat(4, 10.04)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    res = replay.apply_stop(case, bars, "SELL_WATCH", 0.5, "fixed")
    assert res["triggered"] is True
    # 未跳空 → 恰在止损位成交（10.0 × 1.005）
    assert res["stop_exit_price"] == pytest.approx(10.05)


@pytest.mark.unit
def test_gap_through_stop_fills_at_open_not_at_level():
    """跳空越过止损位时拿开盘价 —— 这是「止损必然在止损价成交」的乐观假设的修正。"""
    rows = [(10.0, 10.0, 10.0, 10.0), (10.50, 10.60, 10.45, 10.55)] + flat(4, 10.55)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    res = replay.apply_stop(case, bars, "SELL_WATCH", 1.0, "fixed")
    assert res["triggered"] is True
    # 止损位是 10.10，但开盘已 10.50 → 成交 10.50
    assert res["stop_exit_price"] == pytest.approx(10.50)


@pytest.mark.unit
def test_long_gap_through_fills_at_open():
    rows = [(10.0, 10.0, 10.0, 10.0), (9.50, 9.55, 9.40, 9.45)] + flat(4, 9.45)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "BUY_WATCH")
    res = replay.apply_stop(case, bars, "BUY_WATCH", 1.0, "fixed")
    assert res["triggered"] is True
    # 止损位 9.90，但开盘已 9.50 → 成交 9.50（更差）
    assert res["stop_exit_price"] == pytest.approx(9.50)


@pytest.mark.unit
def test_slippage_makes_the_stop_fill_worse_only():
    rows = [(10.0, 10.0, 10.0, 10.0)] + [(10.04, 10.06, 10.02, 10.04)] + flat(4, 10.04)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    clean = replay.apply_stop(case, bars, "SELL_WATCH", 0.5, "fixed", 0.0)
    slipped = replay.apply_stop(case, bars, "SELL_WATCH", 0.5, "fixed", 10.0)
    # 做空回补，滑点让它买得更贵
    assert slipped["stop_exit_price"] > clean["stop_exit_price"]
    assert slipped["delta_pct"] < clean["delta_pct"]


# --------------------------------------------------------------------------
# 移动止损
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_trailing_stop_level_uses_prior_bars_peak_not_the_current_bar():
    """第 i 根的止损位只依赖第 i−1 根之前已知的信息 —— 真实挂单就是这样摆着的。

    构造一根「自身上冲又自下落」的宽 bar：用上一根的极值判定成交在 10.10，
    若改成先用本根更新极值再判定，成交会变成 9.09。两者差别就是前视。
    """
    rows = [
        (10.0, 10.0, 10.0, 10.0),   # idx0 入场（做空）
        (9.00, 10.15, 9.00, 10.10),  # idx1 同时创出新低与上冲
    ] + flat(4, 10.10)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    res = replay.apply_stop(case, bars, "SELL_WATCH", 1.0, "trailing")
    assert res["triggered"] is True
    # peak 此刻仍是入场价 10.0 → 止损位 10.10
    assert res["stop_exit_price"] == pytest.approx(10.10)


@pytest.mark.unit
def test_trailing_stop_tightens_after_a_favourable_move():
    """做空时价格先跌，止损位应随之下移 —— 这是 trailing 与 fixed 的分野。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),      # idx0 入场
        (9.50, 9.55, 9.00, 9.05),      # idx1 跌到 9.00
        (9.60, 9.95, 9.58, 9.90),      # idx2 反弹到 9.95
    ] + flat(3, 9.90)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    fixed = replay.apply_stop(case, bars, "SELL_WATCH", 1.0, "fixed")
    trailing = replay.apply_stop(case, bars, "SELL_WATCH", 1.0, "trailing")
    assert fixed["triggered"] is False            # 10.10 没碰到（最高 9.95）
    assert trailing["triggered"] is True          # 9.00 × 1.01 = 9.09，被 9.95 击穿
    # 该 bar 开在 9.60、已越过 9.09 → 成交取开盘价 9.60（跳空规则）
    assert trailing["stop_exit_price"] == pytest.approx(9.60)


# --------------------------------------------------------------------------
# 最强的不变量
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("style", ["BUY_WATCH", "SELL_WATCH"])
@pytest.mark.parametrize("mode", ["fixed", "trailing"])
def test_huge_stop_reproduces_baseline_exactly(style, mode):
    """止损设到 99% 必然不触发 → 止损腿必须逐条等于 baseline。

    这是整个模块的正确性锚点：入场价、方向、下标、成交价任何一处写错都会在这里露出来。
    """
    rows = [
        (10.0, 10.2, 9.8, 10.1),
        (10.1, 10.9, 9.2, 10.6),
        (10.6, 10.7, 9.9, 10.0),
        (10.0, 11.5, 9.95, 11.2),
        (11.2, 11.3, 10.1, 10.3),
        (10.3, 10.4, 9.7, 9.8),
    ]
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", style, cost_pct=0.0)
    res = replay.apply_stop(case, bars, style, 99.0, mode, 0.0, 0.0)

    assert res["triggered"] is False
    assert res["stop_exit_price"] == case["base_exit_price"]
    assert res["stop_return_pct"] == pytest.approx(case["base_return_pct"])
    assert res["stop_pnl_pct"] == pytest.approx(case["base_pnl_pct"])
    assert res["delta_pct"] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Δ 的读法：正 = 止损有帮助，两种风格一致
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_stop_cutting_a_loss_shows_positive_delta_for_a_short():
    """SELL：价格一路涨（逆势），止损 1% 把大亏截成小亏 → Δ > 0。"""
    rows = [(10.0, 10.0, 10.0, 10.0), (10.15, 10.20, 10.10, 10.15)] + flat(4, 10.50)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    res = replay.apply_stop(case, bars, "SELL_WATCH", 1.0, "fixed")
    assert res["triggered"] is True
    assert res["delta_pct"] > 0


@pytest.mark.unit
def test_stop_cutting_a_loss_shows_positive_delta_for_a_long():
    """BUY：价格一路跌，止损 1% 把大亏截成小亏 → Δ > 0（与 SELL 同号）。"""
    rows = [(10.0, 10.0, 10.0, 10.0), (9.85, 9.90, 9.80, 9.85)] + flat(4, 9.50)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "BUY_WATCH")
    res = replay.apply_stop(case, bars, "BUY_WATCH", 1.0, "fixed")
    assert res["triggered"] is True
    assert res["delta_pct"] > 0


@pytest.mark.unit
def test_stop_on_a_winner_can_show_negative_delta():
    """反例：先被扫到止损、随后价格才大跌 → Δ < 0。

    说明 Δ 可正可负，不是恒正的产物；也说明止损的真实代价是「本来能赚的被扫掉」。
    """
    rows = [(10.0, 10.0, 10.0, 10.0), (10.00, 10.09, 9.95, 10.05)] + flat(4, 9.00)
    bars = bars_from(rows)
    case = replay.build_case(bars, 0, "5m", "SELL_WATCH")
    res = replay.apply_stop(case, bars, "SELL_WATCH", 0.8, "fixed")
    assert res["triggered"] is True          # 10.09 击穿 10.08
    assert res["stop_exit_price"] == pytest.approx(10.08)
    assert res["delta_pct"] < 0              # 基线本来能赚 10%（跌到 9.00）


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_summarize_counts_clusters_not_rows():
    samples = [
        {"cluster": "A|20260918", "base_pnl_pct": 0.0, "stop_pnl_pct": 1.0,
         "delta_pct": 1.0, "base_net_pnl_pct": -0.07, "stop_net_pnl_pct": 0.93,
         "triggered": True},
        {"cluster": "A|20260918", "base_pnl_pct": 0.0, "stop_pnl_pct": 1.0,
         "delta_pct": 1.0, "base_net_pnl_pct": -0.07, "stop_net_pnl_pct": 0.93,
         "triggered": True},
        {"cluster": "B|20260918", "base_pnl_pct": 0.0, "stop_pnl_pct": -1.0,
         "delta_pct": -1.0, "base_net_pnl_pct": -0.07, "stop_net_pnl_pct": -1.07,
         "triggered": False},
    ]
    out = replay.summarize(samples)
    assert out["n"] == 3          # 行数
    assert out["clusters"] == 2   # 簇数
    assert out["trigger_rate"] == pytest.approx(2 / 3)
    # 簇等权：每簇各占一半，与「行数多的簇」主导的池化口径不同
    assert out["delta_mean"] == pytest.approx(0.0)


@pytest.mark.unit
def test_summarize_omits_ci_keys_for_a_single_cluster():
    out = replay.summarize([
        {"cluster": "A|20260918", "base_pnl_pct": 0.0, "stop_pnl_pct": 1.0,
         "delta_pct": 1.0, "base_net_pnl_pct": 0.0, "stop_net_pnl_pct": 0.0,
         "triggered": True},
    ])
    assert "delta_ci_low" not in out  # 键缺失而非 None
    assert out["clusters"] == 1


@pytest.mark.unit
def test_normalize_bars_drops_unusable_rows():
    rows = [
        {"trade_date": "202609180931", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
        {"trade_date": "20260918", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},   # 8 位
        {"trade_date": "202609180932", "open": 10.0, "high": None, "low": 9.9, "close": 10.0},  # 缺 high
        {"trade_date": "202609180933", "open": 0.0, "high": 10.1, "low": 9.9, "close": 10.0},   # 零价
        {"trade_date": "202609180934", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1},
    ]
    bars = replay.normalize_bars(rows)
    assert [b["label"] for b in bars] == ["0931", "0934"]


@pytest.mark.unit
def test_normalize_bars_sorts_by_time():
    rows = [
        {"trade_date": "202609181000", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        {"trade_date": "202609180932", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
    ]
    assert [b["label"] for b in replay.normalize_bars(rows)] == ["0932", "1000"]


# --------------------------------------------------------------------------
# 分桶统计（选择偏差诊断的判据）
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_bucket_stats_reports_tail_size_not_just_tail_mean():
    """n 小时 tail_n = 1 —— 那是**一条**，不是「最差 10% 的平均」。

    这正是本诊断差点被我读错的地方：只报 −6.41 会看着像一群亏损的均值，
    实际是一条孤例。故 tail_n 必须一起返回。
    """
    out = replay.bucket_stats([1.0, -1.0, 2.0, -6.41, 0.5])  # n=5 → ⌊5/10⌋=0 → 兜底 1
    assert out["n"] == 5
    assert out["tail_n"] == 1
    assert out["tail_pct"] == pytest.approx(-6.41)


@pytest.mark.unit
def test_bucket_stats_tail_is_worst_tenth():
    out = replay.bucket_stats([float(i) for i in range(100)])  # 0..99
    assert out["tail_n"] == 10
    assert out["tail_pct"] == pytest.approx(4.5)   # 最差 10 条 = 0..9
    assert out["mean_pct"] == pytest.approx(49.5)


@pytest.mark.unit
def test_bucket_stats_flags_session_impossible_returns():
    """沪主板单日 ±10% —— 超过就是伪影，不是收益。"""
    out = replay.bucket_stats([1.0, -0.5, 26.99, -29.02])
    assert out["implausible_n"] == 2
    assert replay.bucket_stats([1.0, -9.9])["implausible_n"] == 0


@pytest.mark.unit
def test_bucket_stats_empty_is_not_a_zero_mean():
    out = replay.bucket_stats([])
    assert out["n"] == 0
    assert out["mean_pct"] is None      # None 而非 0.0：0 会被读成「持平」
    assert out["tail_pct"] is None
