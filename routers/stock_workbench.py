from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db, get_db_collections
from routers.stock import _json_safe, _symbol_candidates
from services.analysis_history import build_user_analysis_history_payload
from services.analysis_tasks import build_task_doc
from services.portfolio_symbol_risk import (
    build_workbench_swot_section,
    internal_signal_enabled,
)
from services.valuation import VALUATION_SOURCE, build_stock_valuation
from services.score_detail_meta import (
    SCORE_HISTORY_PROJECTION,
    build_score_meta,
    get_strategy_recommendations,
)
try:
    from stock_data_access.evidence_fingerprint import normalize_url
    from stock_data_access.indicators.nineturn import enrich_nineturn_signals, latest_signal_summary
except ModuleNotFoundError:
    _stock_data_access_src = Path(__file__).resolve().parents[1] / "data-access-lib" / "src"
    if str(_stock_data_access_src) not in sys.path:
        sys.path.insert(0, str(_stock_data_access_src))
    try:
        from stock_data_access.evidence_fingerprint import normalize_url
        from stock_data_access.indicators.nineturn import enrich_nineturn_signals, latest_signal_summary
    except ModuleNotFoundError:
        log = logging.getLogger(__name__)
        log.warning("stock_data_access is unavailable; nineturn signal enrichment is disabled")

        def enrich_nineturn_signals(daily_quotes: List[Dict[str, Any]], nineturn_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return []

        def latest_signal_summary(signals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            return None

        def normalize_url(value: Any) -> str:
            return str(value or "").strip()

router = APIRouter()
log = logging.getLogger(__name__)

WORKBENCH_DAILY_KLINE_LIMIT = 360
WORKBENCH_MONEY_FLOW_HISTORY_LIMIT = 120
WORKBENCH_WEEKLY_MONEY_FLOW_HISTORY_LIMIT = 520
WORKBENCH_MONTHLY_MONEY_FLOW_HISTORY_LIMIT = 1500
WORKBENCH_WEEKLY_KLINE_LIMIT = 260
WORKBENCH_MONTHLY_KLINE_LIMIT = 260
WORKBENCH_PROFILE_ENABLED = os.getenv("WORKBENCH_PROFILE", "").strip().lower() in {"1", "true", "yes", "on"}
NEWS_URL_ANALYSIS_TASK_TYPE = "news_url_risk_analysis"
MAX_NEWS_URL_LENGTH = 2048
MAX_FINDING_KEY_LENGTH = 256


class AnalyzeNewsUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=MAX_NEWS_URL_LENGTH)
    finding_key: Optional[str] = Field(default=None, max_length=MAX_FINDING_KEY_LENGTH)


class RefreshInternalSignalsRequest(BaseModel):
    force: bool = False


def _validate_public_http_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="url is required")
    if len(text) > MAX_NEWS_URL_LENGTH:
        raise HTTPException(status_code=400, detail="url is too long")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid url") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="only http/https urls are supported")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="url host is required")
    normalized = normalize_url(text)
    if not normalized:
        raise HTTPException(status_code=400, detail="invalid url")
    return normalized


def _news_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

ENTRY_HIGH_WINDOW = 120
ENTRY_FLOW_WINDOW = 20
ENTRY_MIN_HIGH_SAMPLES = 60
ENTRY_MIN_FLOW_SAMPLES = 10
ENTRY_HIPOS_LOW = 0.90
ENTRY_HIPOS_MEDIUM = 0.95
ENTRY_HIPOS_HIGH = 0.97
ENTRY_DISTRIB_LOW = 0.005
ENTRY_DISTRIB_MEDIUM = 0.02
ENTRY_DISTRIB_HIGH = 0.05
ENTRY_MAINFLOW_HIGH_OUTFLOW = -0.05

KLINE_PROJECTION = {
    "_id": 0,
    "symbol": 1,
    "ts_code": 1,
    "trade_date": 1,
    "open": 1,
    "high": 1,
    "low": 1,
    "close": 1,
    "pre_close": 1,
    "pct_chg": 1,
    "pct_change": 1,
    "vol": 1,
    "volume": 1,
    "amount": 1,
}

MONEY_FLOW_PROJECTION = {
    "_id": 0,
    "symbol": 1,
    "ts_code": 1,
    "trade_date": 1,
    "amount": 1,
    "buy_sm_vol": 1,
    "buy_sm_amount": 1,
    "sell_sm_vol": 1,
    "sell_sm_amount": 1,
    "buy_md_vol": 1,
    "buy_md_amount": 1,
    "sell_md_vol": 1,
    "sell_md_amount": 1,
    "buy_lg_vol": 1,
    "buy_lg_amount": 1,
    "sell_lg_vol": 1,
    "sell_lg_amount": 1,
    "buy_elg_vol": 1,
    "buy_elg_amount": 1,
    "sell_elg_vol": 1,
    "sell_elg_amount": 1,
    "net_mf_vol": 1,
    "net_mf_amount": 1,
}


class _WorkbenchProfiler:
    def __init__(self, section: str, symbol: str):
        self.enabled = WORKBENCH_PROFILE_ENABLED
        self.section = section
        self.symbol = symbol
        self.started = time.perf_counter()
        self.last = self.started
        self.parts: List[str] = []

    def lap(self, name: str, **extra: Any) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        elapsed_ms = (now - self.last) * 1000
        self.last = now
        suffix = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
        self.parts.append(f"{name}={elapsed_ms:.1f}ms{(' ' + suffix) if suffix else ''}")

    def finish(self, **extra: Any) -> None:
        if not self.enabled:
            return
        total_ms = (time.perf_counter() - self.started) * 1000
        suffix = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
        log.info(
            "workbench_profile section=%s symbol=%s total=%.1fms parts=[%s]%s%s",
            self.section,
            self.symbol,
            total_ms,
            "; ".join(self.parts),
            " " if suffix else "",
            suffix,
        )


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        number = _to_float(value)
        if number is not None:
            return number
    return None


def _find_first(collection, candidates: List[str], projection: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    for field in ("symbol", "ts_code", "con_code"):
        doc = collection.find_one({field: {"$in": candidates}}, projection)
        if doc:
            return doc
    return None


def _latest_score(symbol: str) -> Optional[Dict[str, Any]]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    return collections["stock_scores"].find_one(
        {"symbol": {"$in": candidates}},
        # Keep the sort covered by (symbol, score_date). Adding _id here forces
        # MongoDB to fetch and sort every score document for a cold symbol.
        sort=[("score_date", -1)],
    )


def _score_history(symbol: str, limit: int = 60) -> List[Dict[str, Any]]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    cursor = collections["stock_scores"].find(
        {"symbol": {"$in": candidates}},
        SCORE_HISTORY_PROJECTION,
        # See _latest_score: the deployed compound index does not include _id.
        sort=[("score_date", -1)],
        limit=limit,
    )
    return list(cursor)


def _latest_daily_quote(data: Any, candidates: List[str], trade_date: Any = None) -> Optional[Dict[str, Any]]:
    volume_price = data["volume_price"]
    # Prefer symbol first: most deployments index volume_price by symbol; ts_code-only first can full-scan.
    for field in ("symbol", "ts_code"):
        base: Dict[str, Any] = {field: {"$in": candidates}}
        if trade_date:
            same_day = volume_price.find_one(
                {**base, "trade_date": trade_date},
                sort=[("trade_date", -1)],
            )
            if same_day:
                return same_day
            doc = volume_price.find_one(
                {**base, "trade_date": {"$lt": trade_date}},
                sort=[("trade_date", -1)],
            )
            if doc:
                return doc
        else:
            # volume_price is unique per symbol/trade_date. Restricting the
            # sort to trade_date lets MongoDB stop after one index entry.
            doc = volume_price.find_one(base, sort=[("trade_date", -1)])
            if doc:
                return doc
    return None


def _trade_date_key(value: Any) -> str:
    """YYYYMMDD prefix for comparing calendar trade dates (string compare safe)."""
    s = str(value or "").strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _quote_date_key(quote: Optional[Dict[str, Any]]) -> str:
    """Best available market date for comparing quote freshness."""
    if not quote:
        return ""
    keys = [
        _trade_date_key(quote.get("trade_date")),
        _trade_date_key(quote.get("datetime")),
        _trade_date_key(quote.get("trade_time")),
        _trade_date_key(quote.get("realtime_trade_time")),
    ]
    return max((key for key in keys if key), default="")


def _quote_as_of(quote: Optional[Dict[str, Any]]) -> Any:
    key = _quote_date_key(quote)
    return key or (quote or {}).get("trade_date") or (quote or {}).get("datetime") or (quote or {}).get("trade_time") or (quote or {}).get("realtime_trade_time")


def _recent_docs_for_symbol(
    collection: Any,
    candidates: List[str],
    *,
    date_field: str,
    limit: int,
    projection: Optional[Dict[str, int]] = None,
    fields: tuple[str, ...] = ("ts_code", "symbol"),
) -> List[Dict[str, Any]]:
    for field in fields:
        cursor = collection.find(
            {field: {"$in": candidates}},
            projection,
            sort=[(date_field, -1)],
            limit=limit,
        )
        docs = list(cursor)
        if docs:
            return docs
    return []


def _data_collection(data: Any, name: str) -> Any:
    if isinstance(data, dict):
        return data.get(name)
    try:
        return data[name]
    except Exception:
        return None


def _normalize_timeframe(tf: str) -> str:
    tfc = (tf or "1d").strip().lower()
    if tfc in ("d",):
        tfc = "1d"
    elif tfc in ("w",):
        tfc = "1w"
    elif tfc in ("m",):
        tfc = "1m"
    if tfc not in ("1d", "1w", "1m"):
        raise HTTPException(status_code=400, detail="tf must be 1d, 1w, or 1m")
    return tfc


def _recent_kline_docs(
    data: Any,
    collection_name: str,
    candidates: List[str],
    *,
    limit: int,
    fields: tuple[str, ...],
) -> List[Dict[str, Any]]:
    collection = _data_collection(data, collection_name)
    if collection is None:
        return []
    return _recent_docs_for_symbol(
        collection,
        candidates,
        date_field="trade_date",
        limit=limit,
        projection=KLINE_PROJECTION,
        fields=fields,
    )


def _recent_money_flow_docs(
    data: Any,
    candidates: List[str],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    collection = _data_collection(data, "money_flow")
    if collection is None:
        return []
    return _recent_docs_for_symbol(
        collection,
        candidates,
        date_field="trade_date",
        limit=limit,
        projection=MONEY_FLOW_PROJECTION,
        fields=("symbol", "ts_code"),
    )


def _period_key(trade_date: Any, tf: str) -> str:
    s = str(trade_date or "")
    if len(s) < 8:
        return s
    if tf == "1m":
        return s[:6]
    try:
        dt = datetime.strptime(s[:8], "%Y%m%d")
    except ValueError:
        return s[:6]
    iso = dt.isocalendar()
    return f"{iso.year}W{iso.week:02d}"


def _aggregate_partial_kline(rows: List[Dict[str, Any]], tf: str) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
    first = ordered[0]
    last = ordered[-1]
    open_price = _to_float(first.get("open"))
    close_price = _to_float(last.get("close"))
    highs = [_to_float(row.get("high")) for row in ordered]
    lows = [_to_float(row.get("low")) for row in ordered]
    volumes = [_to_float(row.get("vol") if row.get("vol") is not None else row.get("volume")) for row in ordered]
    amounts = [_to_float(row.get("amount")) for row in ordered]
    pre_close = _to_float(first.get("pre_close"))
    change = (close_price - pre_close) if close_price is not None and pre_close not in (None, 0) else None
    pct_chg = (change / pre_close * 100) if change is not None and pre_close not in (None, 0) else None
    volume = sum(value for value in volumes if value is not None) if any(value is not None for value in volumes) else None
    amount = sum(value for value in amounts if value is not None) if any(value is not None for value in amounts) else None
    amount_unit = "qian_yuan"
    if tf in ("1w", "1m") and amount is not None:
        amount *= 1000
        amount_unit = "yuan"
    result = {
        "symbol": last.get("symbol") or last.get("ts_code"),
        "ts_code": last.get("ts_code") or last.get("symbol"),
        "trade_date": last.get("trade_date"),
        "open": open_price,
        "high": max((value for value in highs if value is not None), default=None),
        "low": min((value for value in lows if value is not None), default=None),
        "close": close_price,
        "pre_close": pre_close,
        "change": change,
        "pct_chg": pct_chg,
        "pct_change": pct_chg,
        "vol": volume,
        "volume": volume,
        "volume_unit": "hands",
        "amount": amount,
        "amount_unit": amount_unit,
        "is_partial": True,
        "partial": True,
        "period": tf,
        "period_start": first.get("trade_date"),
        "period_end": last.get("trade_date"),
        "sample_count": len(ordered),
        "source": "dynamic_daily_aggregate",
    }
    return result


def _annotate_kline_amount_unit(rows: List[Dict[str, Any]], tf: str) -> List[Dict[str, Any]]:
    amount_unit = "qian_yuan" if tf == "1d" else "yuan"
    volume_unit = "hands" if tf == "1d" else "shares"
    return [
        {
            **row,
            "amount_unit": row.get("amount_unit") or amount_unit,
            "volume_unit": row.get("volume_unit") or volume_unit,
        }
        for row in rows
    ]


def _with_partial_period_kline(
    data: Any,
    candidates: List[str],
    rows: List[Dict[str, Any]],
    tf: str,
) -> List[Dict[str, Any]]:
    if tf not in ("1w", "1m"):
        return rows
    daily_rows = _recent_kline_docs(
        data,
        "volume_price",
        candidates,
        limit=80 if tf == "1w" else 120,
        fields=("symbol", "ts_code"),
    )
    if not daily_rows:
        return rows
    latest_daily = daily_rows[0]
    latest_date = str(latest_daily.get("trade_date") or "")
    latest_key = _period_key(latest_date, tf)
    if not latest_key:
        return rows
    bucket = [
        row for row in daily_rows
        if _period_key(row.get("trade_date"), tf) == latest_key
    ]
    partial = _aggregate_partial_kline(bucket, tf)
    if not partial:
        return rows

    if not rows:
        return [partial]

    latest_row_date = str(rows[0].get("trade_date") or "")
    latest_row_key = _period_key(latest_row_date, tf)
    if latest_row_key == latest_key:
        if latest_row_date >= latest_date:
            return rows
        return [partial, *rows[1:]]
    if latest_row_date > latest_date:
        return rows
    return [partial, *rows]


def _amount_diff(row: Dict[str, Any], buy_key: str, sell_key: str) -> Optional[float]:
    buy = _to_float(row.get(buy_key))
    sell = _to_float(row.get(sell_key))
    if buy is None and sell is None:
        return None
    return (buy or 0.0) - (sell or 0.0)


def _money_flow_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    elg_net = _amount_diff(row, "buy_elg_amount", "sell_elg_amount")
    lg_net = _amount_diff(row, "buy_lg_amount", "sell_lg_amount")
    md_net = _amount_diff(row, "buy_md_amount", "sell_md_amount")
    sm_net = _amount_diff(row, "buy_sm_amount", "sell_sm_amount")
    main_net = (
        (elg_net or 0.0) + (lg_net or 0.0)
        if elg_net is not None or lg_net is not None
        else _to_float(row.get("net_mf_amount"))
    )
    return {
        "symbol": row.get("symbol") or row.get("ts_code"),
        "ts_code": row.get("ts_code"),
        "trade_date": row.get("trade_date"),
        "elg_net": elg_net,
        "lg_net": lg_net,
        "main_net": main_net,
        "md_net": md_net,
        "sm_net": sm_net,
        "net_mf_amount": _to_float(row.get("net_mf_amount")),
        "amount": _to_float(row.get("amount")),
        "unit": "wan_yuan",
    }


def _rolling_sum(rows: List[Dict[str, Any]], key: str, limit: int) -> Optional[float]:
    values = [_to_float(row.get(key)) for row in rows[:limit]]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values)


def _money_flow_streak(rows_desc: List[Dict[str, Any]]) -> int:
    first = _to_float((rows_desc[0] if rows_desc else {}).get("main_net"))
    if first is None or first == 0:
        return 0
    sign = 1 if first > 0 else -1
    count = 0
    for row in rows_desc:
        value = _to_float(row.get("main_net"))
        if value is None or value == 0 or (value > 0) != (sign > 0):
            break
        count += 1
    return count * sign


def _money_flow_summary(history_desc: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = history_desc[0] if history_desc else {}
    return {
        "main_net_today": _to_float(latest.get("main_net")),
        "main_net_5d": _rolling_sum(history_desc, "main_net", 5),
        "main_net_20d": _rolling_sum(history_desc, "main_net", 20),
        "inflow_streak": _money_flow_streak(history_desc),
        "sample_count": len(history_desc),
        "as_of": latest.get("trade_date"),
        "unit": "wan_yuan",
    }


def _quote_amount_wan(row: Dict[str, Any]) -> Optional[float]:
    amount = _to_float(row.get("amount"))
    if amount is None:
        volume = _to_float(row.get("vol") if row.get("vol") is not None else row.get("volume"))
        close = _to_float(row.get("close"))
        if volume is not None and close is not None:
            # Tushare daily vol is in hands; convert shares * price to 万元.
            return volume * 100 * close / 10000
    if amount is None or amount <= 0:
        return None
    # Tushare daily amount is 千元, while money_flow amounts are 万元.
    return amount / 10


def _return_pct_from_closes(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) <= lookback:
        return None
    latest = closes[-1]
    past = closes[-1 - lookback]
    if latest <= 0 or past <= 0:
        return None
    return (latest / past - 1.0) * 100.0


def _entry_distribution_severity(metrics: Dict[str, Any]) -> str:
    hipos = _to_float(metrics.get("hipos120"))
    mainflow = _to_float(metrics.get("mainflow20"))
    distrib = _to_float(metrics.get("distrib"))
    if hipos is None or mainflow is None or distrib is None:
        return "none"
    if mainflow >= 0 or distrib <= 0:
        return "none"
    ret20 = _to_float(metrics.get("ret_20d"))
    ret60 = _to_float(metrics.get("ret_60d"))
    surged = (
        (ret20 is not None and ret20 >= 30.0)
        or (ret60 is not None and ret60 >= 50.0)
    )
    if (
        hipos >= ENTRY_HIPOS_HIGH
        and distrib >= ENTRY_DISTRIB_HIGH
        and (mainflow <= ENTRY_MAINFLOW_HIGH_OUTFLOW or surged)
    ):
        return "high"
    if hipos >= ENTRY_HIPOS_MEDIUM and distrib >= ENTRY_DISTRIB_MEDIUM:
        return "medium"
    if hipos >= ENTRY_HIPOS_LOW and distrib >= ENTRY_DISTRIB_LOW:
        return "low"
    return "none"


def _entry_risk_snapshot(
    daily_rows_desc: List[Dict[str, Any]],
    money_flow_rows_desc: List[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered_prices = []
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in reversed(daily_rows_desc or []):
        trade_date = _trade_date_key(row.get("trade_date"))
        close = _to_float(row.get("close"))
        if not trade_date or close is None or close <= 0:
            continue
        by_date[trade_date] = {**row, "trade_date": trade_date, "close": close}
    ordered_prices = list(by_date.values())
    closes = [_to_float(row.get("close")) for row in ordered_prices]
    closes = [value for value in closes if value is not None and value > 0]
    latest = closes[-1] if closes else None
    if latest is None or len(closes) < ENTRY_MIN_HIGH_SAMPLES:
        return {
            "status": "insufficient_data",
            "severity": "none",
            "as_of": (ordered_prices[-1] if ordered_prices else {}).get("trade_date"),
            "sample_count": len(closes),
            "summary": "暂无足够日线样本计算 120 日高位指标。",
        }

    high_window = closes[-ENTRY_HIGH_WINDOW:]
    trailing_high = max(high_window) if high_window else None
    hipos = latest / trailing_high if trailing_high and trailing_high > 0 else None

    flow_by_date: Dict[str, float] = {}
    for row in money_flow_rows_desc or []:
        trade_date = _trade_date_key(row.get("trade_date"))
        main_net = _to_float(row.get("main_net"))
        if trade_date and main_net is not None:
            flow_by_date[trade_date] = main_net

    recent_prices = ordered_prices[-ENTRY_FLOW_WINDOW:]
    main_values = [
        flow_by_date[str(row.get("trade_date"))]
        for row in recent_prices
        if str(row.get("trade_date")) in flow_by_date
    ]
    amount_values = [_quote_amount_wan(row) for row in recent_prices]
    amount_den = sum(value for value in amount_values if value is not None and value > 0)
    mainflow = None
    if len(main_values) >= ENTRY_MIN_FLOW_SAMPLES and amount_den > 0:
        mainflow = sum(main_values) / amount_den
    distrib = hipos * (-mainflow) if hipos is not None and mainflow is not None else None

    volumes = [
        _to_float(row.get("vol") if row.get("vol") is not None else row.get("volume"))
        for row in ordered_prices
    ]
    volumes = [value for value in volumes if value is not None and value > 0]
    volratio = None
    if len(volumes) >= ENTRY_FLOW_WINDOW:
        slow = sum(volumes[-ENTRY_FLOW_WINDOW:]) / ENTRY_FLOW_WINDOW
        fast = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else None
        if slow > 0 and fast is not None:
            volratio = fast / slow

    metrics = {
        "as_of": ordered_prices[-1].get("trade_date") if ordered_prices else None,
        "sample_count": len(closes),
        "flow_sample_count": len(main_values),
        "latest_close": latest,
        "trailing_high_120d": trailing_high,
        "hipos120": hipos,
        "distance_to_120d_high_pct": (hipos - 1.0) * 100.0 if hipos is not None else None,
        "mainflow20": mainflow,
        "main_net_20d": sum(main_values) if main_values else None,
        "amount_20d_wan": amount_den if amount_den > 0 else None,
        "volratio": volratio,
        "distrib": distrib,
        "ret_20d": _return_pct_from_closes(closes, 20),
        "ret_60d": _return_pct_from_closes(closes, 60),
    }
    severity = _entry_distribution_severity(metrics)
    if mainflow is None:
        summary = "资金流样本不足，暂只展示高位位置指标。"
    elif severity == "high":
        summary = "股价贴近 120 日高点，且 20 日主力净流出明显，进场前建议重点复核。"
    elif severity == "medium":
        summary = "股价处于阶段高位，同时出现 20 日主力净流出，建议谨慎追高。"
    elif severity == "low":
        summary = "存在轻微高位资金流出迹象，作为观察项处理。"
    elif hipos is not None and hipos >= ENTRY_HIPOS_MEDIUM:
        summary = "股价接近 120 日高点，但暂未形成高位派发信号。"
    else:
        summary = "暂未发现高位派发信号。"

    return {
        "status": "ok",
        "severity": severity,
        "summary": summary,
        "metrics": metrics,
        "as_of": metrics.get("as_of"),
    }


def _quote_section(symbol: str) -> Dict[str, Any]:
    profiler = _WorkbenchProfiler("quote", symbol)
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    profiler.lap("setup", candidates=len(candidates))
    quote = _latest_quote(symbol)
    profiler.lap("latest_quote", found=bool(quote))
    daily_quotes = _recent_kline_docs(
        data,
        "volume_price",
        candidates,
        limit=WORKBENCH_DAILY_KLINE_LIMIT,
        fields=("symbol", "ts_code"),
    )
    profiler.lap("daily_kline", rows=len(daily_quotes))
    daily_basic = _recent_docs_for_symbol(
        data["financial_daily_basic"],
        candidates,
        date_field="trade_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "close": 1,
            "turnover_rate": 1,
            "turnover_rate_f": 1,
            "volume_ratio": 1,
            "pe": 1,
            "pe_ttm": 1,
            "pb": 1,
            "ps_ttm": 1,
            "dv_ttm": 1,
            "total_mv": 1,
            "circ_mv": 1,
        },
    )
    profiler.lap("daily_basic", rows=len(daily_basic))
    money_flow = _recent_money_flow_docs(
        data,
        candidates,
        limit=WORKBENCH_MONEY_FLOW_HISTORY_LIMIT,
    )
    profiler.lap("money_flow", rows=len(money_flow))
    money_flow_history_desc = [_money_flow_history_row(row) for row in money_flow]
    money_flow_history = sorted(money_flow_history_desc, key=lambda row: str(row.get("trade_date") or ""))
    money_flow_summary = _money_flow_summary(money_flow_history_desc)
    entry_risk = _entry_risk_snapshot(daily_quotes, money_flow_history_desc)
    profiler.lap("money_flow_transform", rows=len(money_flow_history))
    status = _quote_status(quote)
    if not status["as_of"]:
        status["as_of"] = (daily_quotes[0] if daily_quotes else {}).get("trade_date")
    status["found"] = bool(quote or daily_quotes or daily_basic or money_flow)
    status["source"] = "quant_realtime.volume_price_realtime_latest + quant_data.volume_price/money_flow/financial_daily_basic"
    result = {
        "symbol": symbol,
        "quote": quote,
        "daily_quotes": daily_quotes,
        "daily_basic": daily_basic[0] if daily_basic else None,
        "money_flow": money_flow[0] if money_flow else None,
        "money_flow_history": money_flow_history,
        "money_flow_summary": money_flow_summary,
        "entry_risk": entry_risk,
        "data_status": status,
    }
    profiler.finish(
        found=status["found"],
        daily_rows=len(daily_quotes),
        money_flow_rows=len(money_flow),
    )
    return result


def _kline_section(symbol: str, tf: str) -> Dict[str, Any]:
    profiler = _WorkbenchProfiler("kline", symbol)
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    tfc = _normalize_timeframe(tf)

    collection_name = {
        "1d": "volume_price",
        "1w": "stock_weekly",
        "1m": "stock_monthly",
    }[tfc]
    limit = {
        "1d": WORKBENCH_DAILY_KLINE_LIMIT,
        "1w": WORKBENCH_WEEKLY_KLINE_LIMIT,
        "1m": WORKBENCH_MONTHLY_KLINE_LIMIT,
    }[tfc]
    fields = ("symbol", "ts_code")
    rows = _recent_kline_docs(
        data,
        collection_name,
        candidates,
        limit=limit,
        fields=fields,
    )
    rows = _annotate_kline_amount_unit(rows, tfc)
    rows = _with_partial_period_kline(data, candidates, rows, tfc)
    profiler.lap("kline_query", tf=tfc, collection=collection_name, rows=len(rows), limit=limit)
    result = {
        "symbol": symbol,
        "tf": tfc,
        "rows": rows,
        "data_status": _section_status(
            found=bool(rows),
            as_of=(rows[0] if rows else {}).get("trade_date"),
            expected_freshness="按周期切换时加载",
            source=f"quant_data.{collection_name}",
        ),
    }
    profiler.finish(tf=tfc, rows=len(rows))
    return result


def _money_flow_section(symbol: str, tf: str) -> Dict[str, Any]:
    profiler = _WorkbenchProfiler("money_flow", symbol)
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    tfc = _normalize_timeframe(tf)
    limit = {
        "1d": WORKBENCH_MONEY_FLOW_HISTORY_LIMIT,
        "1w": WORKBENCH_WEEKLY_MONEY_FLOW_HISTORY_LIMIT,
        "1m": WORKBENCH_MONTHLY_MONEY_FLOW_HISTORY_LIMIT,
    }[tfc]
    money_flow = _recent_money_flow_docs(data, candidates, limit=limit)
    profiler.lap("money_flow_query", tf=tfc, rows=len(money_flow), limit=limit)
    money_flow_history_desc = [_money_flow_history_row(row) for row in money_flow]
    money_flow_history = sorted(money_flow_history_desc, key=lambda row: str(row.get("trade_date") or ""))
    money_flow_summary = _money_flow_summary(money_flow_history_desc)
    profiler.lap("money_flow_transform", rows=len(money_flow_history))
    result = {
        "symbol": symbol,
        "tf": tfc,
        "history": money_flow_history,
        "summary": money_flow_summary,
        "latest": money_flow[0] if money_flow else None,
        "data_status": _section_status(
            found=bool(money_flow_history),
            as_of=(money_flow_history_desc[0] if money_flow_history_desc else {}).get("trade_date"),
            expected_freshness="按资金流周期切换时加载",
            source="quant_data.money_flow",
        ),
    }
    profiler.finish(tf=tfc, rows=len(money_flow_history), limit=limit)
    return result


def _nineturn_section(symbol: str) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    daily_quotes = _recent_docs_for_symbol(
        data["volume_price"],
        candidates,
        date_field="trade_date",
        limit=WORKBENCH_DAILY_KLINE_LIMIT,
        projection={
            "_id": 0,
            "symbol": 1,
            "trade_date": 1,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "pre_close": 1,
            "pct_chg": 1,
            "vol": 1,
            "volume": 1,
            "amount": 1,
        },
        fields=("symbol",),
    )
    nineturn_rows = _recent_docs_for_symbol(
        data["stk_nineturn"],
        candidates,
        date_field="trade_date",
        limit=WORKBENCH_DAILY_KLINE_LIMIT,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "freq": 1,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "vol": 1,
            "amount": 1,
            "up_count": 1,
            "down_count": 1,
            "nine_up_turn": 1,
            "nine_down_turn": 1,
        },
    )
    daily_quotes_asc = sorted(daily_quotes, key=lambda row: str(row.get("trade_date") or ""))
    nineturn_rows_asc = sorted(nineturn_rows, key=lambda row: str(row.get("trade_date") or ""))
    signals = enrich_nineturn_signals(daily_quotes_asc, nineturn_rows_asc)
    latest_signal = latest_signal_summary(signals)
    as_of = (
        (nineturn_rows[0] if nineturn_rows else {}).get("trade_date")
        or (daily_quotes[0] if daily_quotes else {}).get("trade_date")
    )
    return {
        "symbol": symbol,
        "freq": "daily",
        "daily_quotes": daily_quotes,
        "nine_turn_rows": nineturn_rows,
        "signals": signals,
        "latest_signal": latest_signal,
        "data_status": _section_status(
            found=bool(nineturn_rows or signals),
            as_of=as_of,
            expected_freshness="T+1 21:30 后",
            source="quant_data.stk_nineturn + quant_data.volume_price",
        ),
    }


def _attach_display_pct_change(data: Any, quote: Optional[Dict[str, Any]], candidates: List[str]) -> Optional[Dict[str, Any]]:
    """Normalize workbench quote change to the common A-share close/pre-close basis."""
    if not quote:
        return quote

    close = _first_float(quote.get("close"), quote.get("price"), quote.get("latest_price"))
    quote_trade_date = _quote_date_key(quote) or quote.get("trade_date")
    previous_close = _first_float(
        quote.get("pre_close"),
        quote.get("prev_close"),
        quote.get("previous_close"),
        quote.get("yesterday_close"),
    )
    previous_close_source = "quote.pre_close" if previous_close is not None else None

    daily_quote = None
    if previous_close is None:
        try:
            daily_quote = _latest_daily_quote(data, candidates, quote_trade_date)
            if daily_quote:
                if quote_trade_date and _trade_date_key(daily_quote.get("trade_date")) == quote_trade_date:
                    previous_close = _first_float(
                        daily_quote.get("pre_close"),
                        daily_quote.get("prev_close"),
                        daily_quote.get("previous_close"),
                    )
                    previous_close_source = "daily.pre_close"
                else:
                    previous_close = _first_float(daily_quote.get("close"))
                    previous_close_source = "previous_daily.close"
        except Exception:
            previous_close = None
            previous_close_source = None

    if close is not None and previous_close and previous_close > 0:
        quote["display_pct_chg"] = (close - previous_close) / previous_close * 100.0
        quote["display_pct_chg_source"] = previous_close_source or "computed_from_pre_close"
        quote["display_pre_close"] = previous_close
        return quote

    fallback_pct = _first_float(quote.get("pct_chg"), quote.get("pct_change"), quote.get("change_percent"))
    if fallback_pct is not None:
        quote["display_pct_chg"] = fallback_pct
        quote["display_pct_chg_source"] = "quote_pct_field"
    else:
        quote["display_pct_chg"] = None
        quote["display_pct_chg_source"] = "unavailable"
    return quote


def _latest_quote(symbol: str) -> Optional[Dict[str, Any]]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    daily_latest = _latest_daily_quote(data, candidates)
    rt = None
    for field in ("symbol", "ts_code"):
        doc = data["volume_price_realtime_latest"].find_one(
            {field: {"$in": candidates}},
            sort=[("trade_time", -1), ("realtime_trade_time", -1), ("trade_date", -1), ("_id", -1)],
        )
        if doc:
            rt = doc
            break
    dy_key = _trade_date_key((daily_latest or {}).get("trade_date"))
    if rt:
        rt["source"] = "realtime"
    rt_key = _quote_date_key(rt)

    # Realtime snapshot is often ETL-delayed; if quant_data.volume_price already has a newer
    # completed session, prefer that bar so refresh matches the K-line tail.
    if daily_latest and dy_key and rt and (not rt_key or dy_key > rt_key):
        quote = dict(daily_latest)
        quote["source"] = "daily"
        if quote.get("display_pct_chg") is None:
            quote["display_pct_chg"] = _first_float(quote.get("pct_chg"), quote.get("pct_change"))
            quote["display_pct_chg_source"] = "daily.pct_chg"
        return _attach_display_pct_change(data, quote, candidates)

    if rt:
        quote = rt
        as_of = _quote_date_key(quote)
        if as_of and _trade_date_key(quote.get("trade_date")) != as_of:
            quote["display_trade_date"] = as_of
        return _attach_display_pct_change(data, quote, candidates)

    if daily_latest:
        quote = daily_latest
        quote["source"] = "daily"
        if quote.get("display_pct_chg") is None:
            quote["display_pct_chg"] = _first_float(quote.get("pct_chg"), quote.get("pct_change"))
            quote["display_pct_chg_source"] = "daily.pct_chg"
        return quote
    return None


def _stock_identity(symbol: str) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    stock = _find_first(data["stock_info"], candidates) or {}
    if not stock:
        stock = _find_first(data["stock_basic"], candidates) or {}
    if not stock:
        stock = {"symbol": candidates[0]}

    industry = None
    member = None
    sw_member = data.get("sw_index_member")
    if sw_member is not None:
        for c in candidates:
            member = sw_member.find_one(
                {"con_code": c, "src": "SW2021", "is_new": "Y"},
                {
                    "_id": 0,
                    "con_code": 1,
                    "name": 1,
                    "l1_code": 1,
                    "l1_name": 1,
                    "l2_code": 1,
                    "l2_name": 1,
                    "l3_code": 1,
                    "l3_name": 1,
                    "src": 1,
                },
            )
            if member:
                break
        if not member:
            for c in candidates:
                member = sw_member.find_one(
                    {"con_code": c, "src": "SW2021"},
                    {
                        "_id": 0,
                        "con_code": 1,
                        "name": 1,
                        "l1_code": 1,
                        "l1_name": 1,
                        "l2_code": 1,
                        "l2_name": 1,
                        "l3_code": 1,
                        "l3_name": 1,
                        "src": 1,
                    },
                    sort=[("in_date", -1), ("_id", -1)],
                )
                if member:
                    break
    if member:
        industry = member

    return {
        **stock,
        "symbol": stock.get("symbol") or stock.get("ts_code") or stock.get("con_code") or candidates[0],
        "name": stock.get("name") or stock.get("stock_name") or (member or {}).get("name", ""),
        "industry": stock.get("industry") or (member or {}).get("l1_name", ""),
        "shenwan_industry": industry,
    }


def _latest_deep_analysis(user_id: Any, symbol: str) -> Optional[Dict[str, Any]]:
    payload = build_user_analysis_history_payload(
        user_id, symbol=symbol, page=1, limit=1, include_full=True
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def _analysis_history(user_id: Any, symbol: str, limit: int = 5) -> Dict[str, Any]:
    payload = build_user_analysis_history_payload(user_id, symbol=symbol, page=1, limit=limit)
    rows = payload.get("data") if isinstance(payload, dict) else []
    return {
        "rows": rows if isinstance(rows, list) else [],
        "total": payload.get("total") if isinstance(payload, dict) else 0,
    }


def _accuracy_label(value: Any) -> str:
    return {
        "accurate": "准确",
        "mixed": "部分准确",
        "inaccurate": "不准确",
    }.get(str(value or ""), str(value or "未评估"))


def _analysis_history_title(row: Dict[str, Any]) -> str:
    analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
    return (
        analysis.get("final_conclusion")
        or analysis.get("investment_advice")
        or analysis.get("long_term_forecast")
        or analysis.get("mid_term_forecast")
        or analysis.get("short_term_forecast")
        or row.get("stock_name")
        or "历史分析"
    )


def _analysis_evaluation_summary(history: Dict[str, Any]) -> Dict[str, Any]:
    rows = history.get("rows") if isinstance(history, dict) else []
    rows = rows if isinstance(rows, list) else []
    history_ids = [str(row.get("id")) for row in rows if row.get("id")]
    if not history_ids:
        return {
            "total_analyses": history.get("total") if isinstance(history, dict) else 0,
            "recent_analyses": 0,
            "evaluated_count": 0,
            "pending_count": 0,
            "accuracy_counts": {},
            "accuracy_rate": None,
            "latest_evaluations": [],
            "data_status": _section_status(
                found=False,
                expected_freshness="用户手动触发评估后更新",
                source="quant_analyzer.analysis_evaluations",
            ),
        }

    eval_rows = list(
        get_analyzer_db()["analysis_evaluations"].find(
            {"history_id": {"$in": history_ids}},
            {"_id": 0},
        )
    )
    evaluations_by_history_id = {
        str(doc.get("history_id")): doc for doc in eval_rows if doc.get("history_id")
    }
    accuracy_counts: Dict[str, int] = {}
    latest_evaluations: List[Dict[str, Any]] = []
    latest_as_of = None

    for row in rows:
        history_id = str(row.get("id") or "")
        doc = evaluations_by_history_id.get(history_id)
        if not doc:
            continue
        review = doc.get("llm_review") if isinstance(doc.get("llm_review"), dict) else {}
        accuracy = review.get("outcome_accuracy")
        if accuracy:
            accuracy_counts[accuracy] = accuracy_counts.get(accuracy, 0) + 1
        as_of = doc.get("evaluated_at") or doc.get("updated_at") or doc.get("created_at")
        if as_of and (latest_as_of is None or str(as_of) > str(latest_as_of)):
            latest_as_of = as_of
        latest_evaluations.append(
            {
                "history_id": history_id,
                "analysis_created_at": row.get("created_at"),
                "analysis_mode": row.get("analysis_mode"),
                "title": _analysis_history_title(row),
                "evaluation_status": doc.get("evaluation_status"),
                "evaluated_at": as_of,
                "outcome_accuracy": accuracy,
                "outcome_accuracy_label": _accuracy_label(accuracy),
                "reasoning_quality": review.get("reasoning_quality"),
                "risk_calibration": review.get("risk_calibration"),
                "summary": review.get("summary"),
                "short_review": review.get("short_review"),
                "price_windows": doc.get("price_windows") or {},
            }
        )

    evaluated_count = len(evaluations_by_history_id)
    accurate_count = accuracy_counts.get("accurate", 0)
    mixed_count = accuracy_counts.get("mixed", 0)
    mature_count = sum(accuracy_counts.values())
    return {
        "total_analyses": history.get("total") if isinstance(history, dict) else len(rows),
        "recent_analyses": len(rows),
        "evaluated_count": evaluated_count,
        "pending_count": max(0, len(history_ids) - evaluated_count),
        "accuracy_counts": accuracy_counts,
        "accuracy_rate": round(accurate_count / mature_count * 100, 2) if mature_count else None,
        "hit_rate": round((accurate_count + mixed_count * 0.5) / mature_count * 100, 2) if mature_count else None,
        "latest_evaluations": latest_evaluations,
        "data_status": _section_status(
            found=bool(evaluated_count),
            as_of=latest_as_of,
            expected_freshness="用户手动触发评估后更新",
            source="quant_analyzer.analysis_evaluations",
        ),
    }


def _user_id_candidates(user_id: Any) -> List[Any]:
    values = [user_id, str(user_id)]
    return list(dict.fromkeys(value for value in values if value is not None))


def _symbol_matches_any(value: Any, candidates: List[str]) -> bool:
    norm = str(value or "").upper()
    if not norm:
        return False
    candidate_set = {str(item).upper() for item in candidates}
    candidate_set.update(item.split(".", 1)[0] for item in candidate_set)
    return norm in candidate_set or norm.split(".", 1)[0] in candidate_set


def _trading_status(signals: List[Dict[str, Any]], positions: List[Dict[str, Any]], in_watchlist: bool) -> Dict[str, Any]:
    latest = signals[0] if signals else None
    return _section_status(
        found=bool(in_watchlist or signals or positions),
        as_of=(latest or {}).get("created_at") or (latest or {}).get("timestamp") or (latest or {}).get("updated_at"),
        expected_freshness="随用户自选、持仓和交易信号更新",
        source="finance.user_watchlists/watchlist_strategies/trader_positions/trade_signals",
    )


def _trading_section(user_id: Any, symbol: str) -> Dict[str, Any]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    user_ids = _user_id_candidates(user_id)

    watchlist_doc = collections["user_watchlists"].find_one({"user_id": {"$in": user_ids}}) or {}
    watchlist_symbols = watchlist_doc.get("symbols") or []
    in_watchlist = any(_symbol_matches_any(item, candidates) for item in watchlist_symbols)

    strategies = list(
        collections["watchlist_strategies"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("updated_at", -1)],
            limit=20,
        )
    )
    positions = list(
        collections["trader_positions"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("updated_at", -1), ("timestamp", -1)],
            limit=10,
        )
    )
    signals = list(
        collections["trade_signals"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("created_at", -1), ("timestamp", -1)],
            limit=10,
        )
    )

    return {
        "symbol": symbol,
        "watchlist": {
            "in_watchlist": in_watchlist,
            "symbol_count": len(watchlist_symbols),
        },
        "watchlist_strategies": strategies,
        "positions": positions,
        "recent_signals": signals,
        "data_status": _trading_status(signals, positions, in_watchlist),
    }


def _section_status(
    *,
    found: bool,
    as_of: Any = None,
    source: str,
    expected_freshness: str = "",
    stale: bool = False,
) -> Dict[str, Any]:
    return {
        "found": bool(found),
        "as_of": as_of,
        "stale": stale,
        "expected_freshness": expected_freshness,
        "source": source,
    }


def _quote_status(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(quote),
        as_of=_quote_as_of(quote),
        expected_freshness="盘中实时快照优先，缺失时使用最近交易日 T+1 日线",
        source=(quote or {}).get("source") or "quant_data.volume_price",
    )


def _score_status(score: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(score),
        as_of=(score or {}).get("score_date"),
        expected_freshness="随评分任务刷新",
        source="finance.stock_scores",
    )


def _financial_status(indicators: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = indicators[0] if indicators else {}
    return _section_status(
        found=bool(indicators),
        as_of=latest.get("end_date") or latest.get("period") or latest.get("ann_date"),
        expected_freshness="财报披露后同步，通常按季更新",
        source="quant_data.financial_* + quant_data.earnings_*",
    )


def _valuation_status(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and payload.get("data_status"):
        return payload["data_status"]
    return _section_status(
        found=False,
        expected_freshness="进入估值模型页签后按需加载",
        source=VALUATION_SOURCE,
    )


def _ai_status(deep_analysis: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(deep_analysis),
        as_of=(deep_analysis or {}).get("created_at"),
        expected_freshness="用户手动触发后更新",
        source="finance.analysis_history",
    )


def _financials_section(symbol: str, periods: int = 40) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    income = _recent_docs_for_symbol(
        data["financial_income"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    balance = _recent_docs_for_symbol(
        data["financial_balance"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    cashflow = _recent_docs_for_symbol(
        data["financial_cashflow"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    indicators = _recent_docs_for_symbol(
        data["financial_indicator"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    earnings_forecast = _recent_docs_for_symbol(
        data["earnings_forecast"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_express = _recent_docs_for_symbol(
        data["earnings_express"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_disclosure = _recent_docs_for_symbol(
        data["earnings_disclosure"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_report_rc = _recent_docs_for_symbol(
        data["earnings_report_rc"],
        candidates,
        date_field="report_date",
        limit=10,
        projection={"_id": 0},
    )
    return {
        "symbol": symbol,
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "indicators": indicators,
        "earnings": {
            "forecast": earnings_forecast,
            "express": earnings_express,
            "disclosure": earnings_disclosure,
            "report_rc": earnings_report_rc,
        },
        "data_status": _financial_status(indicators or income),
    }


def _valuation_section(symbol: str) -> Dict[str, Any]:
    return build_stock_valuation(
        symbol,
        data_db=get_data_db(),
        finance_db=get_db_collections(),
        candidates=_symbol_candidates(symbol),
    )


def _shareholder_status(
    holder_numbers: List[Dict[str, Any]],
    hk_hold: List[Dict[str, Any]],
    top10_float_change: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    as_of = (holder_numbers[0] if holder_numbers else {}).get("end_date") or (hk_hold[0] if hk_hold else {}).get("trade_date")
    top10_found = bool((top10_float_change or {}).get("period_curr"))
    return _section_status(
        found=bool(holder_numbers or hk_hold or top10_found),
        as_of=as_of or (top10_float_change or {}).get("period_curr"),
        expected_freshness="股东户数/港股通按季披露；增减持·解禁·回购为公告驱动",
        source="quant_data.stk_holdernumber/hk_hold/top10_floatholders/stk_holdertrade/share_float/repurchase",
    )


def _pct_change(curr: Any, prev: Any) -> Optional[float]:
    c, p = _to_float(curr), _to_float(prev)
    if c is None or p is None or p == 0:
        return None
    return (c - p) / abs(p) * 100.0


_INSTITUTION_PATTERNS = [
    ("MorganStanley", "摩根士丹利", re.compile(r"MORGAN\s*STANLEY|摩根士丹利", re.I)),
    ("JPMorgan", "摩根大通", re.compile(r"J\.?\s*P\.?\s*MORGAN|JP\s*MORGAN|摩根大通|JPM", re.I)),
    ("GoldmanSachs", "高盛", re.compile(r"GOLDMAN|高盛", re.I)),
    ("UBS", "瑞银/UBS", re.compile(r"\bUBS\s*AG\b|\bUBS\b|瑞银", re.I)),
    ("Citi", "花旗", re.compile(r"花旗|CITI(GROUP|BANK|CORP|GLOBAL|SECUR)|\bCITI\b", re.I)),
    ("BNPParibas", "法国巴黎银行", re.compile(r"BNP|PARIBAS|法巴|巴黎.*法巴", re.I)),
    ("Barclays", "巴克莱", re.compile(r"BARCLAYS?|巴克莱", re.I)),
    ("DeutscheBank", "德意志银行", re.compile(r"DEUTSCHE|德意志|德银", re.I)),
    ("BlackRock", "贝莱德", re.compile(r"BLACKROCK|贝莱德", re.I)),
    ("Fidelity", "富达", re.compile(r"FIDELITY|富达|FIL", re.I)),
    ("HKSC", "香港中央结算有限公司", re.compile(r"香港中央结算(有限公司)?|HONG\s*KONG.*CLEARING", re.I)),
]
_INTL_KEYS = {key for key, _, _ in _INSTITUTION_PATTERNS if key != "HKSC"}


def _normalize_holder_name(name: Any) -> Dict[str, str]:
    text = str(name or "").strip()
    for key, label, pattern in _INSTITUTION_PATTERNS:
        if pattern.search(text):
            return {"key": key, "label": label, "holder_name": text}
    return {"key": "OTHER", "label": "其他", "holder_name": text}


def _holding_amount(row: Dict[str, Any]) -> Optional[float]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return _first_float(
        row.get("hold_amount"), row.get("hold_num"), row.get("hold_share"), row.get("shares"),
        raw.get("hold_amount"), raw.get("hold_num"), raw.get("hold_share"), raw.get("shares"),
    )


def _holding_ratio(row: Dict[str, Any]) -> Optional[float]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return _first_float(
        row.get("hold_ratio"), row.get("hold_ratio_pct"), row.get("ratio"),
        raw.get("hold_ratio"), raw.get("hold_float_ratio"), raw.get("hold_ratio_pct"), raw.get("ratio"),
    )


def _top10_holder_change_type(current: Optional[Dict[str, Any]], previous: Optional[Dict[str, Any]]) -> str:
    if current and not previous:
        return "new"
    if previous and not current:
        return "exited"
    if not current or not previous:
        return "unknown"

    curr_ratio = current.get("hold_ratio")
    prev_ratio = previous.get("hold_ratio")
    if curr_ratio is not None and prev_ratio is not None:
        delta = curr_ratio - prev_ratio
    else:
        curr_amount = current.get("hold_amount")
        prev_amount = previous.get("hold_amount")
        if curr_amount is None or prev_amount is None:
            return "unchanged"
        delta = curr_amount - prev_amount

    if delta > 0:
        return "increased"
    if delta < 0:
        return "decreased"
    return "unchanged"


def _top10_holder_change(current: Optional[Dict[str, Any]], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = current or previous or {}
    amount_delta = None
    ratio_delta = None
    ratio_rel_delta = None
    if current and previous:
        curr_amount = current.get("hold_amount")
        prev_amount = previous.get("hold_amount")
        if curr_amount is not None and prev_amount is not None:
            amount_delta = curr_amount - prev_amount
        curr_ratio = current.get("hold_ratio")
        prev_ratio = previous.get("hold_ratio")
        if curr_ratio is not None and prev_ratio is not None:
            ratio_delta = curr_ratio - prev_ratio
            ratio_rel_delta = _pct_change(curr_ratio, prev_ratio)

    return {
        "norm_key": base.get("norm_key"),
        "norm_label": base.get("norm_label"),
        "holder_name": base.get("holder_name"),
        "change_type": _top10_holder_change_type(current, previous),
        "current": current,
        "previous": previous,
        "hold_amount_chg": amount_delta,
        "hold_ratio_chg": ratio_delta,
        "hold_ratio_rel_chg_pct": ratio_rel_delta,
    }


def _group_top10_by_period(rows: List[Dict[str, Any]]) -> List[tuple[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        period = str(row.get("end_date") or row.get("period") or "")
        if not period:
            continue
        grouped.setdefault(period, []).append(row)
    out = []
    for period in sorted(grouped.keys(), reverse=True):
        period_rows = grouped[period]
        period_rows.sort(
            key=lambda item: (
                -(_to_float(item.get("holder_rank")) or 999),
                _holding_amount(item) or 0,
            ),
            reverse=True,
        )
        out.append((period, period_rows))
    return out


def _top10_float_change(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    periods = _group_top10_by_period(rows)
    if not periods:
        return {"period_curr": None, "period_prev": None, "intl_new": [], "hksc": None, "current_holders": []}
    curr_period, curr_rows = periods[0]
    prev_period, prev_rows = periods[1] if len(periods) > 1 else (None, [])

    def enrich(row: Dict[str, Any]) -> Dict[str, Any]:
        norm = _normalize_holder_name(row.get("holder_name"))
        return {
            **row,
            "norm_key": norm["key"],
            "norm_label": norm["label"],
            "hold_amount": _holding_amount(row),
            "hold_ratio": _holding_ratio(row),
        }

    curr = [enrich(row) for row in curr_rows]
    prev = [enrich(row) for row in prev_rows]
    curr_intl = {row.get("norm_key"): row for row in curr if row.get("norm_key") in _INTL_KEYS}
    prev_intl = {row.get("norm_key"): row for row in prev if row.get("norm_key") in _INTL_KEYS}
    intl_changes = [
        _top10_holder_change(curr_intl.get(key), prev_intl.get(key))
        for key in sorted(set(curr_intl) | set(prev_intl))
    ]
    intl_change_summary: Dict[str, int] = {}
    for row in intl_changes:
        key = row.get("change_type") or "unknown"
        intl_change_summary[key] = intl_change_summary.get(key, 0) + 1
    intl_new = [
        row.get("current") for row in intl_changes
        if row.get("change_type") == "new" and row.get("current")
    ]

    hksc_curr = next((row for row in curr if row.get("norm_key") == "HKSC"), None)
    hksc_prev = next((row for row in prev if row.get("norm_key") == "HKSC"), None)
    hksc = None
    if hksc_curr or hksc_prev:
        hksc = _top10_holder_change(hksc_curr, hksc_prev)

    return {
        "period_curr": curr_period,
        "period_prev": prev_period,
        "intl_new": intl_new,
        "intl_new_count": len(intl_new),
        "intl_changes": intl_changes,
        "intl_change_count": len([row for row in intl_changes if row.get("change_type") != "unchanged"]),
        "intl_change_summary": intl_change_summary,
        "hksc": hksc,
        "current_holders": curr,
    }


def _shareholder_summary(
    holder_numbers: List[Dict[str, Any]],
    hk_hold: List[Dict[str, Any]],
    top10_float_change: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    latest_hn = holder_numbers[0] if holder_numbers else {}
    prev_hn = holder_numbers[1] if len(holder_numbers) > 1 else {}
    latest_hk = hk_hold[0] if hk_hold else {}
    prev_hk = hk_hold[1] if len(hk_hold) > 1 else {}
    top10_float_change = top10_float_change or {}
    hksc = top10_float_change.get("hksc") or {}
    return {
        "holder_num": _to_float(latest_hn.get("holder_num")),
        "holder_num_date": latest_hn.get("end_date"),
        # 户数下降通常意味着筹码集中（利多），故单独给出变化方向
        "holder_num_chg_pct": _pct_change(latest_hn.get("holder_num"), prev_hn.get("holder_num")),
        "northbound_ratio": _to_float(latest_hk.get("ratio")),
        "northbound_vol": _to_float(latest_hk.get("vol")),
        "northbound_date": latest_hk.get("trade_date"),
        "northbound_ratio_chg": (
            None if _to_float(latest_hk.get("ratio")) is None or _to_float(prev_hk.get("ratio")) is None
            else _to_float(latest_hk.get("ratio")) - _to_float(prev_hk.get("ratio"))
        ),
        "intl_new_count": int(top10_float_change.get("intl_new_count") or 0),
        "intl_change_count": int(top10_float_change.get("intl_change_count") or 0),
        "top10_float_period": top10_float_change.get("period_curr"),
        "hksc_hold_ratio_chg": hksc.get("hold_ratio_chg"),
        "hksc_hold_amount_chg": hksc.get("hold_amount_chg"),
    }


def _shareholder_section(symbol: str) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)

    holder_numbers = _recent_docs_for_symbol(
        data["stk_holdernumber"],
        candidates,
        date_field="end_date",
        limit=16,
        projection={"_id": 0, "ts_code": 1, "end_date": 1, "ann_date": 1, "holder_num": 1},
    )
    hk_hold = _recent_docs_for_symbol(
        data["hk_hold"],
        candidates,
        date_field="trade_date",
        limit=24,
        projection={"_id": 0, "ts_code": 1, "trade_date": 1, "vol": 1, "ratio": 1, "exchange": 1, "name": 1},
    )
    top10_floatholders = _recent_docs_for_symbol(
        data["top10_floatholders"],
        candidates,
        date_field="end_date",
        limit=80,
        projection={
            "_id": 0, "ts_code": 1, "ann_date": 1, "end_date": 1, "holder_name": 1,
            "hold_amount": 1, "hold_ratio": 1, "holder_rank": 1, "raw": 1,
        },
    )
    top10_float_change = _top10_float_change(top10_floatholders)
    holder_trades = _recent_docs_for_symbol(
        data["stk_holdertrade"],
        candidates,
        date_field="ann_date",
        limit=20,
        projection={
            "_id": 0, "ts_code": 1, "ann_date": 1, "holder_name": 1, "holder_type": 1,
            "in_de": 1, "change_vol": 1, "change_ratio": 1, "after_share": 1,
            "after_ratio": 1, "avg_price": 1,
        },
    )
    share_floats = _recent_docs_for_symbol(
        data["share_float"],
        candidates,
        date_field="float_date",
        limit=12,
        projection={
            "_id": 0, "ts_code": 1, "ann_date": 1, "float_date": 1, "holder_name": 1,
            "share_type": 1, "float_share": 1, "float_ratio": 1,
        },
    )
    repurchases = _recent_docs_for_symbol(
        data["repurchase"],
        candidates,
        date_field="ann_date",
        limit=12,
        projection={
            "_id": 0, "ts_code": 1, "ann_date": 1, "end_date": 1, "proc": 1, "exp_date": 1,
            "vol": 1, "amount": 1, "high_limit": 1, "low_limit": 1,
        },
    )

    return {
        "symbol": symbol,
        "summary": _shareholder_summary(holder_numbers, hk_hold, top10_float_change),
        "holder_numbers": holder_numbers,
        "hk_hold": hk_hold,
        "top10_floatholders": top10_floatholders,
        "top10_float_change": top10_float_change,
        "holder_trades": holder_trades,
        "share_floats": share_floats,
        "repurchases": repurchases,
        "data_status": _shareholder_status(holder_numbers, hk_hold, top10_float_change),
    }


@router.get("/{symbol}/workbench")
def get_stock_workbench(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Single-stock research workbench payload."""
    stock = _stock_identity(symbol)
    quote = _latest_quote(symbol)
    score = _latest_score(symbol)

    data_status = {
        "stock_found": bool(stock),
        "quote_found": bool(quote),
        "score_found": bool(score),
        "deep_analysis_found": False,
        "quote_date": _quote_as_of(quote),
        "score_date": (score or {}).get("score_date"),
        "analysis_created_at": None,
        "sections": {
            "quote": _quote_status(quote),
            "nine_turn": _section_status(
                found=False,
                expected_freshness="进入神奇九转页签后按需加载",
                source="quant_data.stk_nineturn + quant_data.volume_price",
            ),
            "scores": _score_status(score),
            "financials": _section_status(
                found=False,
                expected_freshness="进入财务业绩页签后按需加载",
                source="quant_data.financial_* + quant_data.earnings_*",
            ),
            "valuation": _valuation_status(),
            "ai": _section_status(
                found=False,
                expected_freshness="进入深度分析页签后按需加载",
                source="finance.analysis_history",
            ),
            "trading": _section_status(
                found=False,
                expected_freshness="进入 AI 与交易页签后按需加载",
                source="finance.user_watchlists/watchlist_strategies/trader_positions/trade_signals",
            ),
            "shareholders": _section_status(
                found=False,
                expected_freshness="进入股东筹码页签后按需加载",
                source="quant_data.stk_holdernumber/hk_hold/stk_holdertrade/share_float/repurchase",
            ),
            "signals": _section_status(
                found=False,
                expected_freshness="进入 SWOT 页签后按需加载",
                source="quant_analyzer.symbol_risk_findings + symbol_opportunity_findings",
            ),
        },
    }

    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "stock": stock,
                "quote": quote,
                "score": score,
                "deep_analysis": None,
                "data_status": data_status,
            }
        ),
    }


@router.get("/{symbol}/workbench/quote")
def get_stock_workbench_quote(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Market quote section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_quote_section(symbol)),
    }


@router.get("/{symbol}/workbench/kline")
def get_stock_workbench_kline(
    symbol: str,
    tf: str = Query("1d", description="1d=日线, 1w=周线, 1m=月线"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """K-line rows for a selected stock workbench timeframe."""
    return {
        "success": True,
        "data": _json_safe(_kline_section(symbol, tf)),
    }


@router.get("/{symbol}/workbench/money-flow")
def get_stock_workbench_money_flow(
    symbol: str,
    tf: str = Query("1d", description="1d=日资金流, 1w=周线视图资金流, 1m=月线视图资金流"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Money-flow history for a selected stock workbench timeframe."""
    return {
        "success": True,
        "data": _json_safe(_money_flow_section(symbol, tf)),
    }


@router.get("/{symbol}/workbench/nine-turn")
def get_stock_workbench_nine_turn(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Nine-turn signal section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_nineturn_section(symbol)),
    }


@router.get("/{symbol}/workbench/scores")
def get_stock_workbench_scores(
    symbol: str,
    strategy: str = Query("balanced", description="综合分策略，用于 recommendations"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Quant score section for the single-stock research workbench."""
    score = _latest_score(symbol)
    history = _score_history(symbol)
    recommendations = get_strategy_recommendations(score, strategy=strategy) if score else None
    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "score": score,
                "score_history": history,
                "score_meta": build_score_meta(score) if score else None,
                "recommendations": recommendations,
                "data_status": _score_status(score),
            }
        ),
    }


@router.get("/{symbol}/workbench/financials")
def get_stock_workbench_financials(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Financial performance section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_financials_section(symbol)),
    }


@router.get("/{symbol}/workbench/valuation")
def get_stock_workbench_valuation(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Valuation model section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_valuation_section(symbol)),
    }


@router.get("/{symbol}/workbench/ai")
def get_stock_workbench_ai(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """AI analysis section for the single-stock research workbench."""
    history = _analysis_history(current_user.get("id"), symbol)
    analysis_rows = history["rows"]
    deep_analysis = analysis_rows[0] if analysis_rows else None
    evaluation_summary = _analysis_evaluation_summary(history)
    status = _ai_status(deep_analysis)
    if evaluation_summary["data_status"]["found"]:
        status["found"] = True
        status["as_of"] = status["as_of"] or evaluation_summary["data_status"]["as_of"]
    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "deep_analysis": deep_analysis,
                "analysis_history": analysis_rows,
                "analysis_history_total": history["total"],
                "evaluation_summary": evaluation_summary,
                "data_status": status,
            }
        ),
    }


@router.get("/{symbol}/workbench/trading")
def get_stock_workbench_trading(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """User trading context section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_trading_section(current_user.get("id"), symbol)),
    }


@router.get("/{symbol}/workbench/shareholders")
def get_stock_workbench_shareholders(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Shareholder / chips section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_shareholder_section(symbol)),
    }


@router.get("/{symbol}/workbench/signals")
def get_stock_workbench_signals(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Canonical SWOT signals section (O/T from ledger; S/W reserved)."""
    stock = _stock_identity(symbol)
    payload = build_workbench_swot_section(
        stock.get("symbol") or symbol,
        industry=stock.get("industry"),
    )
    return {
        "success": True,
        "data": _json_safe(payload),
    }


@router.post("/{symbol}/workbench/signals/refresh-internal")
def refresh_stock_workbench_internal_signals(
    symbol: str,
    request: RefreshInternalSignalsRequest,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Enqueue deterministic Strength/Weakness analysis for one symbol."""
    if not internal_signal_enabled():
        raise HTTPException(status_code=503, detail="internal signal analysis is disabled")
    collections = get_db_collections()
    analysis_tasks = collections.get("analysis_tasks")
    if analysis_tasks is None:
        raise HTTPException(status_code=503, detail="analysis task queue unavailable")

    stock = _stock_identity(symbol)
    canonical = str(stock.get("symbol") or symbol).strip().upper()
    user_id = str(current_user.get("id") or current_user.get("username") or "")
    existing = analysis_tasks.find_one(
        {
            "type": "internal_signal_refresh",
            "user_id": user_id,
            "params.symbol": canonical,
            "status": {"$in": ["pending", "processing"]},
        },
        sort=[("created_at", -1)],
    )
    if existing:
        return {
            "success": True,
            "data": {
                "task_id": str(existing.get("_id") or ""),
                "status": existing.get("status") or "pending",
                "reused": True,
            },
        }

    score = _latest_score(canonical) or {}
    score_date = str(score.get("score_date") or datetime.utcnow().strftime("%Y%m%d"))
    run_id = str(uuid.uuid4())
    params = {
        "symbol": canonical,
        "score_date": score_date,
        "run_id": run_id,
        "force": request.force,
        "options": {"force": request.force},
    }
    task = build_task_doc(
        current_user=current_user,
        task_type="internal_signal_refresh",
        priority=11,
        symbol=canonical,
        stock_name=stock.get("name"),
        params=params,
        source="stock_workbench_swot",
        extra_fields={
            "operation_type": "workbench_internal_signal_refresh",
            "idempotency_key": f"internal-signal:{canonical}:{run_id}",
        },
    )
    result = analysis_tasks.insert_one(task)
    return {
        "success": True,
        "data": {
            "task_id": str(getattr(result, "inserted_id", "") or ""),
            "run_id": run_id,
            "status": "pending",
            "reused": False,
        },
    }


@router.post("/{symbol}/workbench/signals/collect")
def collect_stock_workbench_signals(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Enqueue one-symbol evidence collection and O/T ledger analysis."""
    collections = get_db_collections()
    analysis_tasks = collections.get("analysis_tasks")
    if analysis_tasks is None:
        raise HTTPException(status_code=503, detail="analysis task queue unavailable")

    stock = _stock_identity(symbol)
    canonical = str(stock.get("symbol") or symbol).strip().upper()
    user_id = str(current_user.get("id") or current_user.get("username") or "")
    existing = analysis_tasks.find_one(
        {
            "type": "portfolio_risk_llm",
            "user_id": user_id,
            "params.target_scope": "workbench",
            "params.items.symbol": canonical,
            "status": {"$in": ["pending", "processing"]},
        },
        sort=[("created_at", -1)],
    )
    if existing:
        return {
            "success": True,
            "data": {
                "task_id": str(existing.get("_id") or ""),
                "status": existing.get("status") or "pending",
                "reused": True,
            },
        }

    industry = str(stock.get("industry") or "未分类").strip() or "未分类"
    run_id = str(uuid.uuid4())
    as_of = datetime.utcnow().strftime("%Y%m%d")
    params = {
        "plan_id": f"workbench:{canonical}",
        "run_id": run_id,
        "target_scope": "workbench",
        "base_date": as_of,
        "score_date": as_of,
        "industry": industry,
        "chunk_index": 0,
        "chunk_count": 1,
        "emit_industry_scope": False,
        "items": [
            {
                "symbol": canonical,
                "name": stock.get("name") or canonical,
                "industry": industry,
                "action": "research",
                "weight": None,
                "score_value": None,
                "rules_risk": None,
            }
        ],
        "industry_context": {
            "total_weight": 0.0,
            "industries": [
                {
                    "industry": industry,
                    "symbol_count": 1,
                    "weight": 0.0,
                    "symbols": [canonical],
                }
            ],
        },
        "options": {
            "taxonomy_level": "sw1",
            "prompt_version": "v1",
            "force": False,
        },
    }
    task = build_task_doc(
        current_user=current_user,
        task_type="portfolio_risk_llm",
        priority=10,
        symbol=canonical,
        stock_name=stock.get("name"),
        params=params,
        source="stock_workbench_swot",
        extra_fields={
            "operation_type": "workbench_llm_risk_review",
            "idempotency_key": f"workbench:{canonical}:{run_id}",
        },
    )
    result = analysis_tasks.insert_one(task)
    task_id = str(getattr(result, "inserted_id", "") or "")
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "run_id": run_id,
            "status": "pending",
            "reused": False,
        },
    }


@router.post("/{symbol}/workbench/signals/analyze-url")
def analyze_stock_workbench_news_url(
    symbol: str,
    request: AnalyzeNewsUrlRequest,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Enqueue one news URL for safe fetch + LLM risk/opportunity assessment."""
    collections = get_db_collections()
    analysis_tasks = collections.get("analysis_tasks")
    if analysis_tasks is None:
        raise HTTPException(status_code=503, detail="analysis task queue unavailable")

    normalized_url = _validate_public_http_url(request.url)
    url_hash = _news_url_hash(normalized_url)
    target_finding_key = str(request.finding_key or "").strip() or None
    stock = _stock_identity(symbol)
    canonical = str(stock.get("symbol") or symbol).strip().upper()
    user_id = str(current_user.get("id") or current_user.get("username") or "")
    industry = str(stock.get("industry") or "未分类").strip() or "未分类"

    existing = analysis_tasks.find_one(
        {
            "type": NEWS_URL_ANALYSIS_TASK_TYPE,
            "user_id": user_id,
            "params.symbol": canonical,
            "params.url_hash": url_hash,
            "params.target_finding_key": target_finding_key,
            "status": {"$in": ["pending", "processing"]},
        },
        sort=[("created_at", -1)],
    )
    if existing:
        return {
            "success": True,
            "data": {
                "task_id": str(existing.get("_id") or ""),
                "status": existing.get("status") or "pending",
                "reused": True,
            },
        }

    run_id = str(uuid.uuid4())
    as_of = datetime.utcnow().strftime("%Y%m%d")
    params = {
        "symbol": canonical,
        "name": stock.get("name") or canonical,
        "industry": industry,
        "url": normalized_url,
        "url_hash": url_hash,
        "target_finding_key": target_finding_key,
        "run_id": run_id,
        "base_date": as_of,
        "score_date": as_of,
        "options": {
            "prompt_version": "v1",
            "force": False,
        },
    }
    idempotency_key = f"news_url:{canonical}:{url_hash}"
    if target_finding_key:
        idempotency_key = f"{idempotency_key}:{_news_url_hash(target_finding_key)}"
    task = build_task_doc(
        current_user=current_user,
        task_type=NEWS_URL_ANALYSIS_TASK_TYPE,
        priority=12,
        symbol=canonical,
        stock_name=stock.get("name"),
        params=params,
        source="stock_workbench_news_url",
        extra_fields={
            "operation_type": "workbench_news_url_analysis",
            "idempotency_key": idempotency_key,
        },
    )
    result = analysis_tasks.insert_one(task)
    task_id = str(getattr(result, "inserted_id", "") or "")
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "run_id": run_id,
            "status": "pending",
            "reused": False,
        },
    }
