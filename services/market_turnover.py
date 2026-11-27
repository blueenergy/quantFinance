"""Volume waterline: turnover relative to its own 20-day mean.

Two series feed the panel:

* daily  — ``market_turnover_daily``: one row per trading day with the five
  mutually exclusive boards plus the ETF split by fund type.
* intraday — ``market_turnover_intraday``: 2-minute snapshots of today's
  cumulative stock turnover per board. Rows backfilled from the Shenwan sector
  aggregate carry ``coverage="total_only"`` and a market-wide ``total`` only.

The intraday comparison has to normalise for the time of day: by 10:00 only
about a third of the session's turnover has happened, so comparing a 10:00
cumulative against a full-day mean would call every morning a shrink. The
observed cumulative fraction of the day at each clock time is what
``build_intraday_profile`` returns.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

MA_WINDOW = 20
#: Trading minutes grid used for the time-of-day profile: 09:30-11:30, 13:00-15:00.
PROFILE_GRID: List[str] = [
    f"{hour:02d}{minute:02d}"
    for hour, start, end in ((9, 30, 59), (10, 0, 59), (11, 0, 30), (13, 0, 59), (14, 0, 59))
    for minute in range(start, end + 1, 2)
] + ["1500"]

BOARDS: List[Dict[str, str]] = [
    {"key": "sh", "name": "沪市主板"},
    {"key": "sz", "name": "深市主板"},
    {"key": "cx", "name": "创业板"},
    {"key": "star", "name": "科创板"},
    {"key": "bse", "name": "北交所"},
]

ETF_TYPES: List[Dict[str, str]] = [
    {"key": "equity", "name": "股票型"},
    {"key": "bond", "name": "债券型"},
    {"key": "money", "name": "货币型"},
]


def to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = (len(sorted_values) - 1) * q
    lower = int(idx)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (idx - lower)


def attach_ma_and_ratio(rows: List[Dict[str, Any]], keys: Iterable[str], window: int = MA_WINDOW) -> List[Dict[str, Any]]:
    """Add ``{key}_ma{window}`` and ``{key}_ratio`` to each row, oldest first.

    ``ratio`` is the row's value divided by the mean of the *previous* ``window``
    rows, so a row is never compared against a mean that contains itself.
    """
    keys = list(keys)
    for key in keys:
        values = [to_float(row.get(key)) for row in rows]
        for index, row in enumerate(rows):
            if index < window:
                row[f"{key}_ma{window}"] = None
                row[f"{key}_ratio"] = None
                continue
            window_values = [value for value in values[index - window : index] if value is not None]
            if len(window_values) < window:
                row[f"{key}_ma{window}"] = None
                row[f"{key}_ratio"] = None
                continue
            mean = sum(window_values) / len(window_values)
            row[f"{key}_ma{window}"] = round(mean, 2)
            current = values[index]
            row[f"{key}_ratio"] = round(current / mean, 4) if mean and current is not None else None
    return rows


def serialize_daily_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "trade_date": doc.get("trade_date"),
        "stock_total": to_float(doc.get("stock_total")),
        "stock_count": doc.get("stock_count"),
        "etf_total": to_float(doc.get("etf_total")),
        "etf_count": doc.get("etf_count"),
        "etf_available": bool(doc.get("etf_available", False)),
    }
    for board in BOARDS:
        row[board["key"]] = to_float(doc.get(board["key"]))
    for etf in ETF_TYPES:
        row[f"etf_{etf['key']}"] = to_float(doc.get(f"etf_{etf['key']}"))
    return row


def cumulative_at_or_before(times: Sequence[str], values: Sequence[Optional[float]], bucket: str) -> Optional[float]:
    """Last cumulative value at or before *bucket* (times are ``HHMM`` strings)."""
    found: Optional[float] = None
    for moment, value in zip(times, values):
        if moment <= bucket:
            if value is not None:
                found = value
        else:
            break
    return found


def build_intraday_profile(
    day_series: Dict[str, List[Dict[str, Any]]],
    grid: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Distribution of the cumulative fraction of the day at each clock time.

    ``day_series`` maps ``trade_date`` -> rows sorted by ``snapshot_time``, each
    row carrying ``t`` (HHMM) and ``total``. Days whose last snapshot is far
    short of the day's final total would distort the fractions; the caller
    passes only complete days.
    """
    grid = list(grid or PROFILE_GRID)
    profile: List[Dict[str, Any]] = []
    for bucket in grid:
        fractions: List[float] = []
        for rows in day_series.values():
            if not rows:
                continue
            end_of_day = to_float(rows[-1].get("total"))
            if not end_of_day or end_of_day <= 0:
                continue
            cumulative = cumulative_at_or_before([r["t"] for r in rows], [r.get("total") for r in rows], bucket)
            if cumulative is None:
                continue
            fractions.append(cumulative / end_of_day)
        if len(fractions) < 3:
            continue
        fractions.sort()
        profile.append(
            {
                "t": bucket,
                "p10": round(_percentile(fractions, 0.10), 4),
                "p50": round(_percentile(fractions, 0.50), 4),
                "p90": round(_percentile(fractions, 0.90), 4),
                "n": len(fractions),
            }
        )
    return profile


def profile_fraction(profile: Sequence[Dict[str, Any]], bucket: str) -> Optional[Dict[str, Any]]:
    """Profile entry whose clock time is at or before *bucket*."""
    found = None
    for entry in profile:
        if entry["t"] <= bucket:
            found = entry
        else:
            break
    return found


def project_full_day(cumulative: Optional[float], fraction: Optional[float]) -> Optional[float]:
    """Scale today's cumulative to a full-day estimate using the observed fraction."""
    if cumulative is None or not fraction or fraction <= 0:
        return None
    return round(cumulative / fraction, 2)


# ── 历史同类（放量之后会怎样）────────────────────────────────────────────────

OUTLOOK_INDICES: List[Dict[str, str]] = [
    {"key": "csi1000", "code": "000852.SH", "name": "中证1000"},
    {"key": "gem", "code": "399006.SZ", "name": "创业板指"},
    {"key": "hs300", "code": "000300.SH", "name": "沪深300"},
]

#: 放量档位；当前倍数满足几档就返回几档。
RATIO_THRESHOLDS = (1.0, 1.1, 1.2, 1.3)
#: 回撤档位（相对近 250 日高点）。
DRAWDOWN_THRESHOLDS = (0.15, 0.25, 0.35)
FORWARD_WINDOW = 20
POST_2018 = "20180101"

_OUTLOOK_CACHE: Dict[tuple, Dict[str, Any]] = {}


def _episodes(dates: List[str], gap_days: int = 20) -> int:
    """Number of separate bursts: event days within *gap_days* of each other count once."""
    if not dates:
        return 0
    stamps = [int(d) for d in dates]
    return 1 + sum(1 for a, b in zip(stamps, stamps[1:]) if b - a > gap_days)


def _block_bootstrap_excess(
    drawdown, ratio, forward, dd_threshold: float, ratio_threshold: float,
    *, block: int = 60, iters: int = 400, seed: int = 11,
) -> Optional[List[float]]:
    """90% interval of the excess 20-day return, resampling 60-day blocks.

    Overlapping forward windows and clustered events make the naive standard
    error meaningless; block resampling keeps the local dependence intact.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    n = len(ratio)
    if n <= block + 1:
        return None
    nb = int(np.ceil(n / block))
    samples = []
    for _ in range(iters):
        starts = rng.integers(0, n - block, nb)
        idx = np.concatenate([np.arange(start, start + block) for start in starts])[:n]
        mask = (drawdown[idx] < -dd_threshold) & (ratio[idx] > ratio_threshold)
        if mask.sum() < 5:
            continue
        samples.append(forward[idx][mask].mean() - forward[idx].mean())
    if len(samples) < 50:
        return None
    return [round(float(x) * 100, 2) for x in np.percentile(np.array(samples), [5, 95])]


def _summarize(rows, baseline_mean: Optional[float]) -> Dict[str, Any]:
    if rows is None or len(rows) == 0:
        return {"n": 0, "episodes": 0, "mean": None, "median": None, "win_rate": None, "excess": None}
    mean = float(rows["fwd"].mean())
    return {
        "n": int(len(rows)),
        "episodes": _episodes(rows["trade_date"].tolist()),
        "mean": round(mean * 100, 2),
        "median": round(float(rows["fwd"].median()) * 100, 2),
        "win_rate": round(float((rows["fwd"] > 0).mean()) * 100, 1),
        "excess": None if baseline_mean is None else round((mean - baseline_mean) * 100, 2),
    }


def compute_turnover_outlook(
    turnover_rows: List[Dict[str, Any]],
    price_rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Forward-return ladders for the current volume and drawdown state.

    Two ladders are returned rather than one cell: the current reading usually
    satisfies several thresholds, and the shallow ones carry far more samples.
    Every row reports its own sample size, number of separate bursts and year
    concentration, because the deep-drawdown cells rest on a handful of
    bear-market bottoms.
    """
    import pandas as pd

    if len(turnover_rows) < 60 or len(price_rows) < 60:
        return None

    turn = pd.DataFrame(turnover_rows)[["trade_date", "stock_total"]]
    prices = pd.DataFrame(price_rows)[["trade_date", "close"]]
    frame = turn.merge(prices, on="trade_date", how="inner").reset_index(drop=True)
    if len(frame) < 60:
        return None

    # shift(1): 均线只用「前 20 个交易日」，不含当天 —— 与面板顶部和日频接口同口径。
    frame["ma20"] = frame["stock_total"].rolling(MA_WINDOW).mean().shift(1)
    frame["ratio"] = frame["stock_total"] / frame["ma20"]
    frame["peak250"] = frame["close"].rolling(250, min_periods=120).max()
    frame["drawdown"] = frame["close"] / frame["peak250"] - 1
    frame["fwd"] = frame["close"].shift(-FORWARD_WINDOW) / frame["close"] - 1
    completed = frame.dropna(subset=["ratio", "drawdown", "fwd"]).reset_index(drop=True)
    if completed.empty:
        return None
    baseline = float(completed["fwd"].mean())

    live = frame.dropna(subset=["ratio"]).iloc[-1]
    live_ratio = to_float(live["ratio"])
    live_dd = to_float(completed["drawdown"].iloc[-1]) if len(completed) else None
    # 当日回撤要用包含最新收盘的窗口算，completed 少了最后 20 天。
    dd_series = (frame["close"] / frame["peak250"] - 1).dropna()
    if len(dd_series):
        live_dd = to_float(dd_series.iloc[-1])

    payload: Dict[str, Any] = {
        "as_of": str(completed.iloc[-1]["trade_date"]),
        "current": {
            "ratio": None if live_ratio is None else round(live_ratio, 4),
            "drawdown": None if live_dd is None else round(live_dd, 4),
        },
        "baseline": {"mean": round(baseline * 100, 2), "window": FORWARD_WINDOW},
        "ratio_ladder": [],
        "drawdown_ladder": [],
    }
    if live_ratio is None:
        return payload

    for threshold in RATIO_THRESHOLDS:
        if live_ratio <= threshold:
            continue
        row = _summarize(completed[completed["ratio"] > threshold], baseline)
        row["threshold"] = threshold
        payload["ratio_ladder"].append(row)

    if not payload["ratio_ladder"] or live_dd is None:
        return payload
    ratio_threshold = payload["ratio_ladder"][-1]["threshold"]
    for threshold in DRAWDOWN_THRESHOLDS:
        if -live_dd <= threshold:
            continue
        mask = (completed["ratio"] > ratio_threshold) & (completed["drawdown"] < -threshold)
        rows = completed[mask]
        row = _summarize(rows, baseline)
        row["threshold"] = -threshold
        row["post2018"] = _summarize(rows[rows["trade_date"] >= POST_2018], None)
        years = rows["trade_date"].str[:4].value_counts().head(3)
        row["years"] = [{"year": str(year), "n": int(count)} for year, count in years.items()]
        row["bootstrap_ci"] = _block_bootstrap_excess(
            completed["drawdown"].to_numpy(),
            completed["ratio"].to_numpy(),
            completed["fwd"].to_numpy(),
            threshold,
            ratio_threshold,
        )
        payload["drawdown_ladder"].append(row)
    payload["ratio_threshold"] = ratio_threshold
    return payload


def get_cached_outlook(cache_key: tuple, builder) -> Optional[Dict[str, Any]]:
    """One computation per index per trading day — the data only moves nightly."""
    if cache_key in _OUTLOOK_CACHE:
        return _OUTLOOK_CACHE[cache_key]
    if len(_OUTLOOK_CACHE) > 32:
        _OUTLOOK_CACHE.clear()
    payload = builder()
    _OUTLOOK_CACHE[cache_key] = payload
    return payload
