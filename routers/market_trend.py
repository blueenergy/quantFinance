"""市场趋势全景图：宽基指数 + 申万行业指数的 0-100 趋势强度分。

分数口径（trend-v1，冻结后再改要升版本号）：

    对每个指数，取 5 个趋势特征：
      close/MA20-1、close/MA60-1、MA20 的 10 日斜率、20 日收益、60 日收益
    每个特征做 750 个交易日滚动窗口的百分位（min_periods=250），
    5 个百分位取均值 → 0-100。

分数是「相对自身历史」的强弱，不是涨跌幅，也不是跨资产可直接比较的绝对强弱。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from fastapi import APIRouter, Depends, Query
from pymongo import ASCENDING

from auth import get_current_active_user
from db import data_db

router = APIRouter()

SCORE_VERSION = "trend-v1"
PCT_WINDOW = 750
PCT_MIN_PERIODS = 250
MAX_DAYS = 250
CACHE_TTL_SECONDS = 300

_INDEX_PRICES = data_db["index_prices"]
_SW_DAILY = data_db["sw_daily"]
_SW_CLASSIFY = data_db["sw_index_classify"]

# 与 quant_data.index_prices 的覆盖一致
BROAD_INDICES: List[Tuple[str, str]] = [
    ("000001.SH", "上证指数"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("932000.CSI", "中证2000"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]
_BROAD_CODES = {code for code, _ in BROAD_INDICES}

# 图上的主题列：用申万 L2/L3 里最接近的行业指数，固定顺序
THEME_INDICES: List[Tuple[str, str]] = [
    ("801081.SI", "半导体"),
    ("850814.SI", "数字芯片设计"),
    ("801085.SI", "消费电子"),
    ("801102.SI", "通信设备"),
    ("801104.SI", "软件开发"),
    ("801111.SI", "白色家电"),
    ("801125.SI", "白酒"),
    ("801151.SI", "化学制药"),
    ("801153.SI", "医疗器械"),
    ("801193.SI", "证券"),
    ("801194.SI", "保险"),
    ("801735.SI", "光伏设备"),
    ("801737.SI", "电池"),
    ("801742.SI", "航空装备"),
    ("801745.SI", "军工电子"),
    ("850711.SI", "机床工具"),
    ("801992.SI", "航运港口"),
]

_PAYLOAD_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

_PROJ = {"_id": 0, "ts_code": 1, "trade_date": 1, "close": 1}


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def _round_or_none(value: Any, digits: int = 1) -> Optional[float]:
    num = _num(value)
    return None if num is None else round(num, digits)


def _score_series(close: pd.Series) -> pd.Series:
    """单条指数收盘价的 0-100 趋势分；只用当日及以前的数据。"""
    s = pd.to_numeric(close, errors="coerce").astype(float)
    ma20 = s.rolling(20, min_periods=20).mean()
    ma60 = s.rolling(60, min_periods=60).mean()
    feats = pd.DataFrame(
        {
            "dev20": s / ma20 - 1.0,
            "dev60": s / ma60 - 1.0,
            "slope20": ma20 / ma20.shift(10) - 1.0,
            "r20": s / s.shift(20) - 1.0,
            "r60": s / s.shift(60) - 1.0,
        },
        index=s.index,
    )
    pct = feats.rolling(PCT_WINDOW, min_periods=PCT_MIN_PERIODS).rank(pct=True) * 100.0
    # 5 个特征都可用才给分，避免早期只用部分特征造成口径漂移
    return pct.mean(axis=1, skipna=False)


def _score_frame(px: pd.DataFrame) -> pd.DataFrame:
    """px: index=trade_date, columns=ts_code, values=close -> 同形状的分数。"""
    if px.empty:
        return px.copy()
    return pd.DataFrame({code: _score_series(px[code]) for code in px.columns}, index=px.index)


def _trade_dates(lookback: int) -> List[str]:
    """用沪深300的交易日作为日历；缺失时回退到申万日线。"""
    dates = _INDEX_PRICES.distinct("trade_date", {"ts_code": "000300.SH"})
    if not dates:
        dates = _SW_DAILY.distinct("trade_date", {})
    clean = sorted(str(d) for d in dates if d)
    return clean[-lookback:] if lookback > 0 else clean


def _sw_l1_items() -> List[Tuple[str, str]]:
    rows = list(
        _SW_CLASSIFY.find(
            {"level": "L1", "src": "SW2021"},
            {"_id": 0, "index_code": 1, "industry_name": 1},
        ).sort("index_code", ASCENDING)
    )
    items = [(str(r.get("index_code") or "").strip(), str(r.get("industry_name") or "").strip()) for r in rows]
    return [(code, name) for code, name in items if code]


def _theme_items() -> List[Tuple[str, str]]:
    """固定主题清单；名称优先用 sw_index_classify 里的最新叫法。"""
    rows = list(
        _SW_CLASSIFY.find(
            {"index_code": {"$in": [code for code, _ in THEME_INDICES]}},
            {"_id": 0, "index_code": 1, "industry_name": 1},
        )
    )
    names = {str(r.get("index_code")): str(r.get("industry_name") or "") for r in rows}
    return [(code, names.get(code) or fallback) for code, fallback in THEME_INDICES]


def _load_closes(codes: Sequence[str], start_date: str) -> pd.DataFrame:
    """从 index_prices + sw_daily 拼出 close 矩阵，返回 index=日期、columns=代码。"""
    frames: List[pd.DataFrame] = []
    broad_codes = [c for c in codes if c in _BROAD_CODES]
    sw_codes = [c for c in codes if c not in _BROAD_CODES]
    if broad_codes:
        rows = list(_INDEX_PRICES.find({"ts_code": {"$in": broad_codes}, "trade_date": {"$gte": start_date}}, _PROJ))
        if rows:
            frames.append(pd.DataFrame(rows))
    if sw_codes:
        rows = list(_SW_DAILY.find({"ts_code": {"$in": sw_codes}, "trade_date": {"$gte": start_date}}, _PROJ))
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["trade_date"] = df["trade_date"].astype(str)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    px = (
        df.pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")
        .sort_index()
        .reindex(columns=list(codes))
    )
    # 指数/行业指数不会有停牌，但申万个别代码可能缺日；前向填充避免单日缺口把分数打空
    return px.ffill()


def _item_payload(
    code: str,
    name: str,
    display_dates: Sequence[str],
    scores: pd.DataFrame,
    changes_5: pd.DataFrame,
    changes_20: pd.DataFrame,
) -> Dict[str, Any]:
    def series(frame: pd.DataFrame) -> List[Optional[float]]:
        if code not in frame.columns:
            return [None] * len(display_dates)
        values = frame.reindex(display_dates)[code].tolist()
        return [_round_or_none(v) for v in values]

    score_values = series(scores)
    latest = next((v for v in reversed(score_values) if v is not None), None)
    return {
        "code": code,
        "name": name,
        "scores": score_values,
        "chg5": series(changes_5),
        "chg20": series(changes_20),
        "latest": latest,
    }


def _build_payload(days: int, include_themes: bool) -> Dict[str, Any]:
    lookback = days + PCT_WINDOW + 100
    dates = _trade_dates(lookback)
    if not dates:
        return {
            "success": True,
            "as_of": None,
            "dates": [],
            "groups": [],
            "message": "index_prices / sw_daily 暂无数据",
            "meta": _meta(),
        }
    if len(dates) < days:
        days = len(dates)
    display_dates = dates[-days:]

    groups_spec: List[Tuple[str, str, List[Tuple[str, str]]]] = [
        ("broad", "宽基", BROAD_INDICES),
        ("sw1", "申万一级", _sw_l1_items()),
    ]
    if include_themes:
        groups_spec.append(("theme", "主题", _theme_items()))

    codes = [code for _, _, items in groups_spec for code, _ in items if code]
    px = _load_closes(codes, dates[0])
    scores = _score_frame(px)
    changes_5 = scores - scores.shift(5)
    changes_20 = scores - scores.shift(20)

    groups = []
    for key, group_name, items in groups_spec:
        groups.append(
            {
                "key": key,
                "name": group_name,
                "items": [
                    _item_payload(code, name, display_dates, scores, changes_5, changes_20)
                    for code, name in items
                    if code
                ],
            }
        )

    return {
        "success": True,
        "as_of": display_dates[-1] if display_dates else None,
        "dates": list(display_dates),
        "groups": groups,
        "meta": _meta(),
    }


def _meta() -> Dict[str, Any]:
    return {
        "score_version": SCORE_VERSION,
        "percentile_window": PCT_WINDOW,
        "min_periods": PCT_MIN_PERIODS,
        "score_note": "0=自身历史最弱，100=自身历史最强；分数是趋势强度分位，不是涨跌幅。",
        "groups_note": "宽基来自 index_prices；申万一级/主题来自 sw_daily（2016-07 起）。",
    }


def _cached_payload(days: int, include_themes: bool) -> Dict[str, Any]:
    key = f"{days}:{int(include_themes)}"
    now = time.monotonic()
    hit = _PAYLOAD_CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    payload = _build_payload(days, include_themes)
    _PAYLOAD_CACHE[key] = (now, payload)
    return payload


@router.get("/panorama")
def get_panorama(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    days: int = Query(60, ge=10, le=MAX_DAYS, description="返回最近 N 个交易日"),
    include_themes: bool = Query(True, description="是否包含申万 L2/L3 主题列"),
):
    return _cached_payload(days, include_themes)
