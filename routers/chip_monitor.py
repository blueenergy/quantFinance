"""筹码监控：自选 + 持仓 A 股的筹码结构指标。

Data sources:
- quant_data.cyq_chips — 逐票筹码分布（自选+持仓，2026-11-14 起每日 21:45 增量）
- quant_data.cyq_perf  — 全市场获利盘/成本分位
- quant_data.volume_price — 当日收盘（同尺度比较）

口径与 ``stock-scoring-system/reports/chip_monitor`` 一致：获利盘优先用
cyq_perf.winner_rate（Tushare 官方），集中度=分布标准差/加权均价，
主峰偏离=最大筹码峰价/收盘-1，熵=归一化 Shannon。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from db import get_data_db, get_db_collections


router = APIRouter(tags=["筹码监控"])

A_SHARE_PREFIXES = ("60", "68", "00", "30", "43", "83", "87", "92")
A_SHARE_PATTERN = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def _is_a_share(symbol: str) -> bool:
    match = A_SHARE_PATTERN.match(str(symbol or "").strip().upper())
    return bool(match) and match.group(1)[:2] in A_SHARE_PREFIXES


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _universe() -> List[str]:
    cols = get_db_collections()
    symbols: set[str] = set()
    for doc in cols["user_watchlists"].find({}, {"symbols": 1}):
        symbols.update(str(s) for s in doc.get("symbols") or [] if s)
    symbols.update(str(s) for s in cols["trader_positions"].distinct("symbol") if s)
    return sorted(s for s in symbols if _is_a_share(s))


def _chip_entropy(chips: List[Dict[str, Any]]) -> Optional[float]:
    weights: List[float] = []
    for chip in chips or []:
        value = _to_float(chip.get("percent"))
        if value is not None and value > 0 and math.isfinite(value):
            weights.append(value)
    if not weights:
        return None
    if len(weights) == 1:
        return 0.0
    total = sum(weights)
    entropy = 0.0
    for value in weights:
        p = value / total
        entropy -= p * math.log(p)
    return entropy / math.log(len(weights))


def _chip_metrics(chips: List[Dict[str, Any]], close: Optional[float]) -> Optional[Dict[str, Any]]:
    pairs = []
    for chip in chips or []:
        price = _to_float(chip.get("price"))
        percent = _to_float(chip.get("percent"))
        if price is not None and percent is not None and percent > 0:
            pairs.append((price, percent))
    if not pairs or close is None or close <= 0:
        return None
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    wavg = sum(p * w for p, w in pairs) / total
    variance = sum(w * (p - wavg) ** 2 for p, w in pairs) / total
    dispersion = math.sqrt(variance) / wavg if wavg > 0 else None
    peak_price, peak_weight = max(pairs, key=lambda item: item[1])
    winner_chips = sum(w for p, w in pairs if p <= close) / total * 100.0
    return {
        "close": close,
        "wavg": wavg,
        "dispersion": dispersion,
        "peak_dev": peak_price / close - 1.0,
        "peak_share": peak_weight / total,
        "winner_chips": winner_chips,
        "s_chip": _chip_entropy(chips),
    }


def _load_history(symbol: str, limit: int) -> List[Dict[str, Any]]:
    data = get_data_db()
    docs = list(
        data["cyq_chips"]
        .find({"symbol": symbol}, {"_id": 0})
        .sort("trade_date", -1)
        .limit(limit)
    )
    docs.reverse()
    if not docs:
        return []

    dates = [str(doc.get("trade_date")) for doc in docs]
    closes = {
        str(row.get("trade_date")): _to_float(row.get("close"))
        for row in data["volume_price"].find(
            {"symbol": symbol, "trade_date": {"$in": dates}},
            {"_id": 0, "trade_date": 1, "close": 1},
        )
    }
    perf = {
        str(row.get("trade_date")): row
        for row in data["cyq_perf"].find(
            {"symbol": symbol, "trade_date": {"$in": dates}},
            {"_id": 0, "trade_date": 1, "winner_rate": 1, "cost_5pct": 1, "cost_95pct": 1, "weight_avg": 1},
        )
    }

    series: List[Dict[str, Any]] = []
    for doc in docs:
        date = str(doc.get("trade_date"))
        metrics = _chip_metrics(doc.get("chips") or [], closes.get(date))
        if metrics is None:
            continue
        row = perf.get(date) or {}
        winner = _to_float(row.get("winner_rate"))
        metrics["date"] = date
        metrics["winner"] = winner if winner is not None else metrics["winner_chips"]
        low = _to_float(row.get("cost_5pct"))
        high = _to_float(row.get("cost_95pct"))
        weight_avg = _to_float(row.get("weight_avg"))
        metrics["concentration_perf"] = (
            (high - low) / weight_avg if low is not None and high is not None and weight_avg not in (None, 0) else None
        )
        series.append(metrics)
    return series


def _risk_flags(last: Dict[str, Any], entropy_change: Optional[float]) -> List[str]:
    flags: List[str] = []
    winner = last.get("winner")
    if winner is not None and winner >= 90:
        flags.append("获利盘高位")
    if winner is not None and winner <= 10:
        flags.append("深度套牢")
    peak_dev = last.get("peak_dev")
    if peak_dev is not None and peak_dev > 0.10:
        flags.append("上方套牢峰")
    if peak_dev is not None and peak_dev < -0.10:
        flags.append("下方支撑峰")
    dispersion = last.get("dispersion")
    if dispersion is not None and dispersion <= 0.10:
        flags.append("筹码集中")
    if dispersion is not None and dispersion >= 0.25:
        flags.append("筹码发散")
    if entropy_change is not None and entropy_change <= -0.02:
        flags.append("熵减")
    if entropy_change is not None and entropy_change >= 0.02:
        flags.append("熵增")
    return flags


def _stock_info(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    cols = get_db_collections()
    out: Dict[str, Dict[str, Any]] = {}
    for doc in cols["stock_info"].find(
        {"symbol": {"$in": symbols}},
        {"_id": 0, "symbol": 1, "name": 1, "industry": 1, "sw_l1": 1},
    ):
        out[str(doc.get("symbol"))] = doc
    return out


@router.get("/chip-monitor/latest")
def chip_monitor_latest(history: int = Query(120, ge=20, le=500)):
    universe = _universe()
    if not universe:
        return {"success": True, "data": {"date": None, "rows": [], "history": {}}}

    info = _stock_info(universe)
    rows: List[Dict[str, Any]] = []
    histories: Dict[str, Any] = {}
    latest_date: Optional[str] = None

    for symbol in universe:
        series = _load_history(symbol, history)
        if not series:
            continue
        last = series[-1]
        entropy_change = None
        if len(series) >= 21 and last.get("s_chip") is not None and series[-21].get("s_chip") is not None:
            entropy_change = last["s_chip"] - series[-21]["s_chip"]
        rows.append(
            {
                "symbol": symbol,
                "name": info.get(symbol, {}).get("name", ""),
                "industry": info.get(symbol, {}).get("industry", ""),
                "date": last["date"],
                "close": last["close"],
                "wavg": last["wavg"],
                "winner": last["winner"],
                "dispersion": last["dispersion"],
                "peak_dev": last["peak_dev"],
                "peak_share": last["peak_share"],
                "s_chip": last["s_chip"],
                "entropy_change_20": entropy_change,
                "concentration_perf": last.get("concentration_perf"),
                "flags": _risk_flags(last, entropy_change),
            }
        )
        histories[symbol] = [
            {
                "date": item["date"],
                "close": item["close"],
                "winner": item["winner"],
                "dispersion": item["dispersion"],
                "s_chip": item["s_chip"],
                "peak_dev": item["peak_dev"],
            }
            for item in series
        ]
        if latest_date is None or last["date"] > latest_date:
            latest_date = last["date"]

    return {"success": True, "data": {"date": latest_date, "rows": rows, "history": histories}}
