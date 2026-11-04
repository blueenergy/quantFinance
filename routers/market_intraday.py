"""盘中强弱全景：申万行业 2 分钟快照 + 全市场广度。

分数口径（intraday-v1）：
    每个快照上，对 31 个申万 L1 做横截面百分位排名，5 个指标取均值 ×100：
      positive_ratio / above_ma_ratio / relative_positive_ratio /
      avg_pct_chg / median_pct_chg
    分数是「同一时刻谁更强」的相对强弱，不是涨跌幅，也不是买卖信号。

数据源：quant_realtime.realtime_sector_strength（QMT 全市场日 K 快照，2 分钟）
       quant_realtime.market_spectrum_realtime_daily（全市场 MA5/MA30 阳谱）
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query
from pymongo import DESCENDING

from auth import get_current_active_user
from db import realtime_db

router = APIRouter()

SCORE_VERSION = "intraday-v1"
MAX_DAYS = 60
CACHE_TTL_SECONDS = 30
MIN_METRICS = 3

_METRICS = [
    "positive_ratio",
    "above_ma_ratio",
    "relative_positive_ratio",
    "avg_pct_chg",
    "median_pct_chg",
]

_SECTOR_STRENGTH = realtime_db["realtime_sector_strength"]
_MARKET_SPECTRUM = realtime_db["market_spectrum_realtime_daily"]

_SECTOR_PROJ = {
    "_id": 0,
    "snapshot_time": 1,
    "sector_code": 1,
    "sector_name": 1,
    "positive_ratio": 1,
    "above_ma_ratio": 1,
    "above_ma5_ratio": 1,
    "relative_positive_ratio": 1,
    "avg_pct_chg": 1,
    "median_pct_chg": 1,
    "positive_count": 1,
    "negative_count": 1,
    "flat_count": 1,
    "stock_count": 1,
}
_SPECTRUM_PROJ = {
    "_id": 0,
    "snapshot_time": 1,
    "ma_period": 1,
    "yang_spectrum": 1,
    "above_ma_count": 1,
    "below_ma_count": 1,
    "total_stocks": 1,
    "coverage_ratio": 1,
}

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_DATES_CACHE: Dict[str, Tuple[float, List[str]]] = {}
DATES_TTL_SECONDS = 600


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def _round_or_none(value: Any, digits: int = 1) -> Optional[float]:
    num = _num(value)
    return None if num is None else round(num, digits)


def _latest_date(level: str) -> str:
    doc = _SECTOR_STRENGTH.find_one(
        {"level": level, "trade_date": {"$exists": True}},
        {"_id": 0, "trade_date": 1},
        sort=[("snapshot_time", DESCENDING)],
    )
    raw = str((doc or {}).get("trade_date") or "")
    return raw[:8] if len(raw) >= 8 else ""


def _available_dates(level: str) -> List[str]:
    now = time.monotonic()
    hit = _DATES_CACHE.get(level)
    if hit and now - hit[0] < DATES_TTL_SECONDS:
        return hit[1]
    values = _SECTOR_STRENGTH.distinct("trade_date", {"level": level})
    clean = sorted({str(v)[:8] for v in values if v and len(str(v)) >= 8})
    clean = clean[-MAX_DAYS:]
    _DATES_CACHE[level] = (now, clean)
    return clean


def _load_sector_rows(date: str, level: str, ma_period: int) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {
        "level": level,
        "snapshot_time": {"$gte": date + "0900", "$lte": date + "1500"},
    }
    if ma_period == 5:
        # 兼容早期没有 ma_period 字段的文档，按 MA5 处理
        query["$or"] = [{"ma_period": 5}, {"ma_period": {"$exists": False}}]
    else:
        query["ma_period"] = ma_period
    return list(_SECTOR_STRENGTH.find(query, _SECTOR_PROJ))


def _load_breadth_rows(date: str) -> List[Dict[str, Any]]:
    query = {
        "snapshot_time": {"$gte": date + "0900", "$lte": date + "1500"},
        "ma_period": {"$in": [5, 30]},
    }
    return list(_MARKET_SPECTRUM.find(query, _SPECTRUM_PROJ))


def _time_minutes(snapshot: str) -> int:
    raw = str(snapshot)
    return int(raw[8:10]) * 60 + int(raw[10:12])


def _pivot(df: pd.DataFrame, metric: str, times: Sequence[str], sectors: Sequence[str]) -> pd.DataFrame:
    matrix = df.pivot_table(index="snapshot_time", columns="sector_code", values=metric, aggfunc="last")
    return matrix.reindex(index=list(times), columns=list(sectors))


def _score_matrix(df: pd.DataFrame, times: Sequence[str], sectors: Sequence[str]) -> pd.DataFrame:
    total = pd.DataFrame(0.0, index=list(times), columns=list(sectors))
    count = pd.DataFrame(0, index=list(times), columns=list(sectors))
    for metric in _METRICS:
        raw = _pivot(df, metric, times, sectors)
        ranked = raw.rank(axis=1, pct=True)
        total = total.add(ranked.fillna(0.0), fill_value=0.0)
        count = count.add(ranked.notna().astype(int), fill_value=0)
    score = (total / count.replace(0, np.nan)) * 100.0
    return score.where(count >= MIN_METRICS)


def _change_matrix(score: pd.DataFrame, times: Sequence[str], minutes: int = 30) -> pd.DataFrame:
    """当前分数 - 30 分钟前分数；找不到对应快照时留空。"""
    mins = np.array([_time_minutes(t) for t in times])
    target = mins - minutes
    idx = np.searchsorted(mins, target, side="right") - 1
    out = pd.DataFrame(np.nan, index=list(times), columns=score.columns)
    for i, j in enumerate(idx):
        if 0 <= j < i:
            out.iloc[i] = score.iloc[i].to_numpy() - score.iloc[j].to_numpy()
    return out


def _series_or_empty(frame: pd.DataFrame, column: Optional[str], times: Sequence[str]) -> List[Optional[float]]:
    if not column or column not in frame.columns:
        return [None] * len(times)
    values = frame.reindex(times)[column].tolist()
    return [_round_or_none(v) for v in values]


def _sector_payload(
    code: str,
    name: str,
    times: Sequence[str],
    score: pd.DataFrame,
    chg30: pd.DataFrame,
    positive: pd.DataFrame,
    above_ma: pd.DataFrame,
    avg_pct: pd.DataFrame,
) -> Dict[str, Any]:
    scores = _series_or_empty(score, code, times)
    latest = next((v for v in reversed(scores) if v is not None), None)
    return {
        "code": code,
        "name": name,
        "scores": scores,
        "chg30m": _series_or_empty(chg30, code, times),
        "positive_ratio": _series_or_empty(positive, code, times),
        "above_ma_ratio": _series_or_empty(above_ma, code, times),
        "avg_pct_chg": _series_or_empty(avg_pct, code, times),
        "latest": latest,
    }


def _summary(
    df: pd.DataFrame,
    score: pd.DataFrame,
    chg30: pd.DataFrame,
    names: Dict[str, str],
    times: Sequence[str],
) -> Dict[str, Any]:
    latest_time = times[-1]
    latest_scores = score.loc[latest_time].dropna().sort_values(ascending=False)
    top = [
        {"code": code, "name": names.get(code, code), "score": _round_or_none(value)}
        for code, value in latest_scores.head(5).items()
    ]
    bottom = [
        {"code": code, "name": names.get(code, code), "score": _round_or_none(value)}
        for code, value in latest_scores.nsmallest(5).items()
    ]

    latest_rows = df[df["snapshot_time"] == latest_time]

    def _sum_count(column: str) -> int:
        if column not in latest_rows.columns:
            return 0
        return int(pd.to_numeric(latest_rows[column], errors="coerce").fillna(0).sum())

    up = _sum_count("positive_count")
    down = _sum_count("negative_count")
    flat = _sum_count("flat_count")

    base_time = next((t for t in times if t[8:] >= "0945"), times[0])
    base_scores = score.loc[base_time]

    weak_to_strong = []
    strong_to_weak = []
    for code in score.columns:
        before = base_scores.get(code)
        now = latest_scores.get(code)
        if pd.isna(before) or pd.isna(now):
            continue
        if before < 30 and now >= 60:
            weak_to_strong.append({
                "code": code,
                "name": names.get(code, code),
                "from": _round_or_none(before),
                "to": _round_or_none(now),
            })
        elif before > 70 and now <= 40:
            strong_to_weak.append({
                "code": code,
                "name": names.get(code, code),
                "from": _round_or_none(before),
                "to": _round_or_none(now),
            })

    latest_chg = chg30.loc[latest_time].dropna().sort_values(ascending=False)
    chg_top = [
        {"code": code, "name": names.get(code, code), "chg": _round_or_none(value)}
        for code, value in latest_chg.head(3).items()
    ]
    chg_bottom = [
        {"code": code, "name": names.get(code, code), "chg": _round_or_none(value)}
        for code, value in latest_chg.nsmallest(3).items()
    ]

    return {
        "latest_time": latest_time,
        "market_score": _round_or_none(latest_scores.mean()),
        "up": up,
        "down": down,
        "flat": flat,
        "top": top,
        "bottom": bottom,
        "weak_to_strong": weak_to_strong[:8],
        "strong_to_weak": strong_to_weak[:8],
        "chg30_top": chg_top,
        "chg30_bottom": chg_bottom,
    }


def _build_payload(date: str, level: str, ma_period: int) -> Dict[str, Any]:
    available = _available_dates(level)
    if not date:
        date = _latest_date(level)
    if not date:
        return {
            "success": True,
            "date": "",
            "as_of": None,
            "times": [],
            "sectors": [],
            "breadth": {},
            "summary": {},
            "available_dates": available,
            "message": "realtime_sector_strength 暂无数据",
            "meta": _meta(level, ma_period),
        }

    rows = _load_sector_rows(date, level, ma_period)
    if not rows:
        return {
            "success": True,
            "date": date,
            "as_of": None,
            "times": [],
            "sectors": [],
            "breadth": {},
            "summary": {},
            "available_dates": available,
            "message": f"{date} 无盘中快照（数据从 {available[0] if available else '2026-08-25'} 起）",
            "meta": _meta(level, ma_period),
        }

    df = pd.DataFrame(rows)
    df["snapshot_time"] = df["snapshot_time"].astype(str)
    df = df.drop_duplicates(["snapshot_time", "sector_code"], keep="last")
    # 早期文档可能只有 above_ma5_ratio
    if "above_ma_ratio" not in df.columns:
        df["above_ma_ratio"] = df.get("above_ma5_ratio")
    else:
        df["above_ma_ratio"] = df["above_ma_ratio"].fillna(df.get("above_ma5_ratio"))
    for metric in _METRICS:
        df[metric] = pd.to_numeric(df.get(metric), errors="coerce")

    times = sorted(df["snapshot_time"].unique())
    sectors = sorted(df["sector_code"].unique())
    names = (
        df.drop_duplicates("sector_code", keep="last")
        .set_index("sector_code")["sector_name"]
        .astype(str)
        .to_dict()
    )

    score = _score_matrix(df, times, sectors)
    chg30 = _change_matrix(score, times, minutes=30)
    positive = _pivot(df, "positive_ratio", times, sectors)
    above_ma = _pivot(df, "above_ma_ratio", times, sectors)
    avg_pct = _pivot(df, "avg_pct_chg", times, sectors)

    breadth_rows = _load_breadth_rows(date)
    breadth: Dict[str, Any] = {}
    if breadth_rows:
        bdf = pd.DataFrame(breadth_rows)
        bdf["snapshot_time"] = bdf["snapshot_time"].astype(str)
        bdf = bdf.drop_duplicates(["snapshot_time", "ma_period"], keep="last")
        bdf["yang_spectrum"] = pd.to_numeric(bdf.get("yang_spectrum"), errors="coerce")
        bdf["above_ma_count"] = pd.to_numeric(bdf.get("above_ma_count"), errors="coerce")
        bdf["below_ma_count"] = pd.to_numeric(bdf.get("below_ma_count"), errors="coerce")
        bdf["total_stocks"] = pd.to_numeric(bdf.get("total_stocks"), errors="coerce")

        def breadth_series(period: int, column: str) -> List[Optional[float]]:
            sub = bdf[bdf["ma_period"] == period].set_index("snapshot_time")[column]
            return [_round_or_none(v) for v in sub.reindex(times).tolist()]

        breadth = {
            "times": list(times),
            "yang5": breadth_series(5, "yang_spectrum"),
            "yang30": breadth_series(30, "yang_spectrum"),
            "above5": breadth_series(5, "above_ma_count"),
            "above30": breadth_series(30, "above_ma_count"),
            "below5": breadth_series(5, "below_ma_count"),
            "below30": breadth_series(30, "below_ma_count"),
            "total5": breadth_series(5, "total_stocks"),
            "total30": breadth_series(30, "total_stocks"),
        }

    sectors_payload = [
        _sector_payload(code, names.get(code, code), times, score, chg30, positive, above_ma, avg_pct)
        for code in sectors
    ]
    # 前端默认按最新分排序；这里保留代码顺序作为「固定顺序」
    summary = _summary(df, score, chg30, names, times)

    return {
        "success": True,
        "date": date,
        "as_of": times[-1],
        "times": list(times),
        "sectors": sectors_payload,
        "breadth": breadth,
        "summary": summary,
        "available_dates": available,
        "meta": _meta(level, ma_period),
    }


def _meta(level: str, ma_period: int) -> Dict[str, Any]:
    return {
        "score_version": SCORE_VERSION,
        "level": level,
        "ma_period": ma_period,
        "metrics": _METRICS,
        "min_metrics": MIN_METRICS,
        "cadence": "2min",
        "source": "realtime_sector_strength + market_spectrum_realtime_daily",
        "score_note": "每个快照上跨行业横截面百分位均值；100=当前最强，0=当前最弱。",
    }


def _cached_payload(date: str, level: str, ma_period: int) -> Dict[str, Any]:
    resolved = date or _latest_date(level)
    key = f"{resolved}:{level}:{ma_period}"
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    payload = _build_payload(resolved, level, ma_period)
    _CACHE[key] = (now, payload)
    return payload


@router.get("/intraday")
def get_intraday(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    date: str = Query("", max_length=8, description="YYYYMMDD；留空取最新有快照的交易日"),
    level: str = Query("L1", pattern="^L[12]$", description="申万层级"),
    ma_period: int = Query(5, ge=2, le=30, description="均线周期；当前数据有 5 / 30"),
):
    return _cached_payload(date, level, ma_period)
