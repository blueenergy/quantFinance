"""Portfolio trade plan and paper-trading API."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import functools
import logging
import math
import os
import threading
import time
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import get_current_user
from db import db as finance_db
from db import get_analyzer_db, get_data_db, get_db_collections, get_trading_db_collections
from services.analysis_tasks import build_task_doc, normalize_task_status
from utils.time_display import add_display_time_fields
from services.execution_policy import estimate_buy_order_cash_exposure
from services.live_trade_signals import (
    ACTIVE_SIGNAL_STATUSES,
    PLAN_PHASE_APPROVED_PUBLISHED,
    PLAN_PHASE_DONE,
    PLAN_PHASE_SELLING,
    TERMINAL_SIGNAL_STATUSES,
    derive_execution_phase_state,
    summarize_signal_statuses,
)
from services.live_equity import (
    _load_latest_close_prices,
)
from services.portfolio_lineage_live import (
    build_lineage_live_positions,
)
from services.portfolio_lineage_live_bundle import (
    fetch_lineage_live_bundle,
    lineage_plan_ids_for_plan as _lineage_plan_ids_for_plan,
)
from services.portfolio_live_snapshot import (
    get_live_snapshot,
    overlay_realtime,
    probe_stale,
    rebuild_live_snapshot_for_plan,
    schedule_snapshot_rebuild,
)
from services.portfolio_lineage_state import (
    EXECUTION_VENUE_LIVE,
    VALID_EXECUTION_VENUES,
    resolve_execution_venue,
)
from services.portfolio_record_kind import (
    RECORD_KIND_LEDGER_EVENT,
    RECORD_KIND_OBSERVATION,
    RECORD_KIND_TRADE_PLAN,
    VALID_RECORD_KINDS,
    classify_record,
    require_record_kind,
)

# Timeline node_type is a display label derived from the authoritative
# record_kind (trade_plans render as "rebalance" nodes, monitor snapshots as
# "monitor", external ledger facts as "manual").
_NODE_TYPE_BY_RECORD_KIND = {
    RECORD_KIND_TRADE_PLAN: "rebalance",
    RECORD_KIND_OBSERVATION: "monitor",
    RECORD_KIND_LEDGER_EVENT: "manual",
}
from services.portfolio_risk_snapshots import capture_plan_risk_snapshot, latest_plan_risk_snapshot
from services.portfolio_symbol_risk import (
    attach_ledger_opportunity_to_items,
    attach_ledger_llm_to_items,
    attach_signal_reviews_to_items,
    build_ledger_opportunity_summary,
    build_ledger_llm_summary,
    filter_items_for_llm_enqueue,
    get_symbol_opportunity_ledger,
    get_symbol_risk_ledger,
    get_symbol_signal_reviews,
    legacy_ledger_read_enabled,
    opportunity_ledger_read_enabled,
)
from services.trade_ledger import group_executions_by_order
# Retained as a package-level symbol: publishing.replan_portfolio_plan_remainder
# resolves it via _pp.resolve_market_reference_price (so tests can monkeypatch it
# on the package namespace); it is no longer called directly in this module.
from services.trader_signal_reprice import resolve_market_reference_price  # noqa: F401
from services.portfolio_plan_market import (
    PRICE_SOURCE_NONE,
    attach_plan_item_overlay,
    market_price_quotes,
    quotes_to_prices_and_realtime_docs,
)
from ._shared import (
    _business_today_date,
    _business_today_yyyymmdd,
    _chunked,
    _datetime_as_beijing_date,
    _int_value,
    _jsonable,
    _normalize_yyyymmdd,
    _numeric,
    _optional_float,
    _risk_severity_at_least,
    _signal_filled_qty,
    _signal_signed_filled_qty,
    _symbol_key,
    _symbol_query_values,
)


router = APIRouter(prefix="/portfolio-plans", tags=["portfolio-plans"])
log = logging.getLogger(__name__)


def _offload(func):
    """Run a synchronous (blocking pymongo) endpoint body in the threadpool.

    Read endpoints here do blocking pymongo I/O. Declared as plain ``def`` and
    wrapped with this, FastAPI still sees a coroutine (so unit tests that call
    the handler via ``asyncio.run`` keep working) but the blocking work runs off
    the event loop, so one slow query no longer stalls the whole worker and
    blocks concurrent dashboard requests.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await run_in_threadpool(func, *args, **kwargs)

    return wrapper


ALLOWED_UNIVERSE_INDICES = {"hs300", "a500", "csi500", "csi1000", "csi2000", "star50"}
PORTFOLIO_RISK_LLM_TASK_TYPE = "portfolio_risk_llm"
PORTFOLIO_RISK_LLM_PRIORITY = 10
PORTFOLIO_RISK_LLM_RUNNING_STATUSES = {"pending", "processing"}
LEGACY_PORTFOLIO_RISK_LLM_COLLECTION = "legacy_portfolio_risk_llm"
GROWTH_CYCLE_PARAMETER_SCHEMA = {
    "universe_index": {"type": "enum", "options": sorted(ALLOWED_UNIVERSE_INDICES)},
    "index_benchmark_symbol": {"type": "string"},
    "growth_weight": {"type": "number", "min": 0, "max": 1},
    "cycle_weight": {"type": "number", "min": 0, "max": 1},
    "top_n": {"type": "integer", "min": 1},
    "rebalance_days": {"type": "integer", "min": 1},
    "construction_mode": {"type": "enum", "options": ["top_n", "industry_capped", "industry_neutral"]},
    "max_industry_weight": {"type": "number", "min": 0, "max": 1, "nullable": True},
    "lot_size": {"type": "integer", "min": 1},
    "transaction_cost": {"type": "number", "min": 0},
    "buy_commission_rate": {"type": "number", "min": 0, "nullable": True},
    "sell_commission_rate": {"type": "number", "min": 0, "nullable": True},
    "min_commission": {"type": "number", "min": 0, "nullable": True},
    "stamp_tax_rate": {"type": "number", "min": 0, "nullable": True},
    "transfer_fee_rate": {"type": "number", "min": 0, "nullable": True},
    "execution_price": {"type": "enum", "options": ["next_open"]},
    "cash_buffer": {"type": "number", "min": 0, "max": 1},
    "initial_capital": {"type": "number", "min": 0},
    "candidate_pool_enabled": {"type": "boolean"},
    "candidate_pool_multiplier": {"type": "integer", "min": 1},
    "min_buy_lots": {"type": "integer", "min": 1},
    # When false (default) non-rebalance days only show portfolio drift and never
    # produce executable trades; the portfolio rebalances strictly every
    # rebalance_days. Set true to allow intra-cycle drift trading.
    "monitor_can_trade": {"type": "boolean"},
    # Capital baseline for rebalance sizing. Defaults auto-derive (live -> rolling
    # real account equity, paper -> paper snapshot). Not part of params_hash.
    "capital_basis": {"type": "enum", "options": ["account_equity", "rolling_paper", "fixed_notional"], "nullable": True},
}
# Mirrors stock-scoring-system/tools/portfolio_trading.py PLAN_PARAM_KEYS.
# These keys define the strategy lineage params_hash.
PLAN_PARAM_KEYS = {
    "universe_index",
    "index_benchmark_symbol",
    "score_type",
    "score_column",
    "growth_weight",
    "cycle_weight",
    "top_n",
    "rebalance_days",
    "construction_mode",
    "max_industry_weight",
}
# Mirrors stock-scoring-system/tools/portfolio_trading.py PLAN_SNAPSHOT_PARAM_KEYS.
# Keep this broader set when carrying params_override forward for force-rebalance:
# the scorer records these values in params_snapshot, but only PLAN_PARAM_KEYS
# participates in params_hash.
PLAN_SNAPSHOT_PARAM_KEYS = PLAN_PARAM_KEYS | {
    "lot_size",
    "transaction_cost",
    "buy_commission_rate",
    "sell_commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "execution_price",
    "cash_buffer",
    "initial_capital",
    "candidate_pool_enabled",
    "candidate_pool_multiplier",
    "min_buy_lots",
}
DEFAULT_STRATEGY_TEMPLATES = (
    {
        "strategy_template_id": "growth_cycle_index_enhanced",
        "strategy_type": "template",
        "name": "成长周期指数增强",
        "algorithm": "growth_cycle_index_enhanced",
        "score_type": "growth_cycle_weighted",
        "parameter_schema": GROWTH_CYCLE_PARAMETER_SCHEMA,
        "status": "enabled",
        "version": 1,
    },
)
class ReviewRequest(BaseModel):
    comment: str = Field(default="")


class PaperExecuteRequest(BaseModel):
    force: bool = Field(default=False)


class PublishLiveSignalsRequest(BaseModel):
    securities_account_id: str
    dry_run: bool = Field(default=True)
    # When true, publish the signals that pass risk and skip only the blocked
    # symbols instead of failing the whole plan. Account-level and plan-capital
    # blockers still hard-block (nothing can go out). Defaults to all-or-nothing.
    allow_partial: bool = Field(default=False)


class ReplanRemainderRequest(BaseModel):
    securities_account_id: str
    dry_run: bool = Field(default=True)
    reason: str = Field(default="")
    # See PublishLiveSignalsRequest.allow_partial. Cash blockers still hard-block
    # here because an unreliable plan-cash baseline can over-budget the top-up.
    allow_partial: bool = Field(default=False)


class BulkRejectPlanItemsRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)


class PortfolioLlmRiskReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_scope: str = Field(default="plan", pattern="^(plan|bench)$")
    min_rules_severity: str = Field(default="none", pattern="^(none|low|medium|high)$")
    taxonomy_level: str = Field(default="sw1", pattern="^sw1$")
    include_actions: List[str] = Field(default_factory=lambda: ["buy", "hold"])
    max_symbols_per_call: int = Field(default=8, ge=1, le=20)
    prompt_version: str = Field(default="v1")
    bench_multiplier: float = Field(default=1.5, ge=1.0, le=3.0)
    force: bool = Field(default=False)


class GeneratePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_template_id: str
    base_date: str
    mode: str = Field(default="auto", pattern="^(auto|monitor|rebalance)$")
    force: bool = Field(default=False)
    preset_id: Optional[str] = None
    params_override: Dict[str, Any] = Field(default_factory=dict)


class ManualRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_plan_id: str
    base_date: Optional[str] = None
    targets: Dict[str, int] = Field(default_factory=dict)
    exclude_after: bool = Field(default=False)


class ForceRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_date: Optional[str] = None


class LiveRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    securities_account_id: str
    # Per-symbol target shares. Symbols not present keep their current live
    # position untouched. target=0 clears a holding; target>current adds; a full
    # liquidation is simply every held symbol mapped to 0.
    targets: Dict[str, int] = Field(default_factory=dict)
    # Add reduced/cleared symbols to the lineage exclusion list so the next
    # strategy rebalance does not buy them back.
    exclude_after: bool = Field(default=False)
    dry_run: bool = Field(default=False)


class ExternalManualFill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: str = Field(default="sell")
    filled_size: int = Field(..., gt=0)
    filled_price: float = Field(..., gt=0)
    timestamp: Optional[float] = Field(default=None)
    commission: float = Field(default=0.0, ge=0)
    total_fee: Optional[float] = Field(default=None, ge=0)
    broker_order_id: Optional[str] = None
    name: str = Field(default="")

    @field_validator("action")
    def _normalize_action(cls, value: str) -> str:
        normalized = str(value or "").lower()
        if normalized not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")
        return normalized


class ExternalManualRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    securities_account_id: str
    fills: List[ExternalManualFill] = Field(default_factory=list)
    # Manual broker-side fills are ledger facts, not strategy decisions. Keep
    # automated cadence intact unless the caller explicitly asks to pause.
    pause_lineage: bool = Field(default=False)
    exclude_after: bool = Field(default=False)
    reason: str = Field(default="miniQMT manual operation")
    external_batch_id: Optional[str] = None


class PaperRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Per-symbol target shares against the current paper book; see
    # LiveRebalanceRequest. Liquidation is every held symbol mapped to 0.
    targets: Dict[str, int] = Field(default_factory=dict)
    exclude_after: bool = Field(default=False)
    dry_run: bool = Field(default=False)


class ParamPresetRequest(BaseModel):
    strategy_template_id: str
    name: str
    params: Dict[str, Any]
    status: str = Field(default="draft", pattern="^(draft|enabled|recommended|disabled)$")
    description: str = Field(default="")
    is_default: bool = Field(default=False)
    priority: int = Field(default=100)
    backtest_summary: Dict[str, Any] = Field(default_factory=dict)
    data_window: Dict[str, Any] = Field(default_factory=dict)


def _with_preset_evidence_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    evidence = row.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    latest = row.get("latest_evidence") or (evidence[-1] if evidence else None)
    return {
        **row,
        "evidence_count": len(evidence),
        "latest_evidence": latest,
    }


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="current user id is missing")
    return str(user_id)


def _ensure_default_strategy_templates(collections: Dict[str, Any]) -> None:
    strategy_configs = collections["portfolio_strategy_configs"]
    now = datetime.utcnow()
    for template in DEFAULT_STRATEGY_TEMPLATES:
        template_id = template["strategy_template_id"]
        if strategy_configs.find_one({"strategy_template_id": template_id}):
            strategy_configs.update_one(
                {"strategy_template_id": template_id},
                {
                    "$set": {**template, "updated_at": now},
                    "$unset": {
                        "universe_index": "",
                        "index_benchmark_symbol": "",
                        "growth_weight": "",
                        "cycle_weight": "",
                        "top_n": "",
                        "rebalance_days": "",
                        "construction_mode": "",
                        "max_industry_weight": "",
                        "lot_size": "",
                        "transaction_cost": "",
                        "buy_commission_rate": "",
                        "sell_commission_rate": "",
                        "min_commission": "",
                        "stamp_tax_rate": "",
                        "transfer_fee_rate": "",
                        "execution_price": "",
                        "cash_buffer": "",
                        "initial_capital": "",
                        "candidate_pool_enabled": "",
                        "candidate_pool_multiplier": "",
                        "min_buy_lots": "",
                        "monitor_can_trade": "",
                    },
                },
            )
        else:
            strategy_configs.insert_one({**template, "created_at": now, "updated_at": now})


def _require_plan(collections: Dict[str, Any], plan_id: str, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {"plan_id": plan_id}
    if current_user is not None:
        query["user_id"] = _current_user_id(current_user)
    plan = collections["portfolio_trade_plans"].find_one(query)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


def _lineage_live_signal_execution_bundle(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Live signals + raw executions for all plans in the same params_hash lineage."""

    bundle = fetch_lineage_live_bundle(trading_collections, finance_collections, plan, user_id)
    return bundle["lineage_ids"], bundle["signals"], bundle["executions"]


def _load_snapshot_with_realtime(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    schedule_rebuild_if_stale: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Read materialized snapshot and overlay realtime prices."""

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    snapshot = get_live_snapshot(
        trading_collections,
        user_id=user_id,
        strategy_template_id=strategy_template_id,
        params_hash=params_hash,
    )
    if snapshot is None:
        snapshot = rebuild_live_snapshot_for_plan(
            trading_collections,
            finance_collections,
            plan,
            user_id,
            reason="build_on_miss",
        )
    elif schedule_rebuild_if_stale and probe_stale(finance_collections, snapshot):
        schedule_snapshot_rebuild(
            trading_collections,
            finance_collections,
            plan,
            user_id,
            reason="stale_read",
        )

    if not snapshot:
        return None, {}

    symbols = sorted(
        {
            str(row.get("symbol"))
            for row in (snapshot.get("positions") or [])
            if row.get("symbol")
        }
    )
    quotes = _market_price_quotes(data_collections, symbols, day_pnl_date=_business_today_yyyymmdd())
    prices, realtime_docs = quotes_to_prices_and_realtime_docs(quotes)
    overlay = overlay_realtime(snapshot, prices, realtime_docs=realtime_docs, quote_map=quotes)
    return snapshot, overlay


def _lineage_execution_yyyymmdd(row: Dict[str, Any]) -> str:
    value = row.get("created_at") or row.get("timestamp") or row.get("updated_at") or ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 1_000_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds).strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        if len(text) == 8:
            return text
        if len(text) >= 13:
            return datetime.fromtimestamp(float(text[:13]) / 1000).strftime("%Y%m%d")
        if len(text) == 10:
            return datetime.fromtimestamp(float(text)).strftime("%Y%m%d")
    return text[:10].replace("-", "")


def _load_lineage_close_prices_by_date(
    data_collections: Dict[str, Any],
    symbols: List[str],
    start_date: str,
    end_date: str,
) -> Dict[str, Dict[str, float]]:
    symbol_set = {str(symbol) for symbol in symbols if symbol}
    symbol_set.update(symbol.split(".", 1)[0] for symbol in list(symbol_set))
    if not symbol_set or not start_date or not end_date:
        return {}

    rows = data_collections["volume_price"].find(
        {
            "symbol": {"$in": sorted(symbol_set)},
            "trade_date": {"$gte": start_date, "$lte": end_date},
        },
        {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1},
    )
    prices_by_date: Dict[str, Dict[str, float]] = {}
    for row in rows:
        date = str(row.get("trade_date") or "")
        symbol = str(row.get("symbol") or "")
        price = _numeric(row.get("close"))
        if not date or not symbol or price <= 0:
            continue
        prices_by_date.setdefault(date, {})[symbol] = price
    return prices_by_date


def _build_lineage_live_book_daily_rows(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    lineage_ids: List[str],
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not lineage_ids:
        return []

    plan_rows = list(
        trading_collections["portfolio_trade_plans"].find(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}},
            {"_id": 0, "plan_id": 1, "base_date": 1},
        )
    )
    first_base_date = min(
        (
            normalized
            for normalized in (_normalize_yyyymmdd(row.get("base_date")) for row in plan_rows)
            if normalized
        ),
        default=_normalize_yyyymmdd(plan.get("base_date")) or "",
    )

    _bundle_ids, _signals, executions = _lineage_live_signal_execution_bundle(
        trading_collections, finance_collections, plan, user_id
    )
    if not executions:
        return []
    execution_dates = {
        date
        for date in (_normalize_yyyymmdd(_lineage_execution_yyyymmdd(row)) for row in executions)
        if date
    }
    earliest_execution_date = min(execution_dates, default="")
    today = datetime.now().strftime("%Y%m%d")
    default_lower = min(
        (date for date in (first_base_date, earliest_execution_date) if date),
        default="",
    )
    lower = _normalize_yyyymmdd(start_date) or default_lower
    upper = _normalize_yyyymmdd(end_date) or today
    if not lower or not upper or lower > upper:
        return []

    symbols = sorted({str(row.get("symbol")) for row in executions if row.get("symbol")})
    prices_by_date = _load_lineage_close_prices_by_date(data_collections, symbols, lower, upper)
    market_dates = _trading_dates_between(data_collections, lower, upper)
    date_set = {date for date in market_dates if lower <= date <= upper}
    date_set.update(date for date in prices_by_date.keys() if lower <= date <= upper)
    date_set.update(date for date in execution_dates if lower <= date <= upper)
    if first_base_date and lower <= first_base_date <= upper:
        date_set.add(first_base_date)
    dates = sorted(date_set)
    if not dates:
        return []

    initial_capital = _lineage_initial_capital(plan)
    rows: List[Dict[str, Any]] = []
    for date in dates:
        executions_to_date = [
            row
            for row in executions
            if (_normalize_yyyymmdd(_lineage_execution_yyyymmdd(row)) or "99999999") <= date
        ]
        grouped = group_executions_by_order(executions_to_date)
        price_map = prices_by_date.get(date, {})
        payload = build_lineage_live_positions(grouped, price_map, realtime_docs={})
        summary = payload.get("summary") or {}
        realized = _numeric(summary.get("total_realized_pnl"))
        unrealized = _numeric(summary.get("total_unrealized_pnl"))
        market_value = _numeric(summary.get("total_market_value"))
        equity = initial_capital + realized + unrealized
        rows.append(
            {
                "date": date,
                "source": "lineage_live_book_daily",
                "equity": round(equity, 2),
                "initial_capital": initial_capital,
                "cash": round(equity - market_value, 2),
                "market_value": round(market_value, 2),
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "execution_count": len(grouped),
                "lineage_plan_ids": lineage_ids,
            }
        )
    return rows


def _build_lineage_live_equity_chart(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    plan_id: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return {
            "anchor_plan_id": plan_id,
            "lineage_plan_ids": [],
            "rows": [],
            "caveat": "No lineage plan ids; equity curve unavailable.",
        }

    date_query: Dict[str, str] = {}
    if start_date:
        date_query["$gte"] = start_date.replace("-", "")
    if end_date:
        date_query["$lte"] = end_date.replace("-", "")

    signal_rows = list(
        finance_collections["trade_signals"].find(
            {"user_id": user_id, "mode": "live", "plan_id": {"$in": lineage_ids}},
            {"_id": 0, "securities_account_id": 1},
        )
    )
    account_ids = sorted(
        {
            str(row.get("securities_account_id") or "")
            for row in signal_rows
            if str(row.get("securities_account_id") or "")
        }
    )

    query: Dict[str, Any] = {"user_id": user_id, "plan_id": {"$in": lineage_ids}}
    if date_query:
        query["date"] = date_query

    equity_rows = list(trading_collections["portfolio_live_equity"].find(query).sort("date", 1))
    latest_by_date: Dict[str, Dict[str, Any]] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not date:
            continue
        existing = latest_by_date.get(date)
        if existing is None or str(row.get("updated_at") or "") >= str(existing.get("updated_at") or ""):
            latest_by_date[date] = row

    rows_sorted = sorted(latest_by_date.values(), key=lambda r: str(r.get("date") or ""))
    plan_ids_in_data = sorted({row.get("plan_id") for row in rows_sorted if row.get("plan_id")})
    plan_map = {
        row.get("plan_id"): row
        for row in trading_collections["portfolio_trade_plans"].find(
            {"plan_id": {"$in": plan_ids_in_data}, "user_id": user_id},
            {"_id": 0, "plan_id": 1, "base_date": 1, "plan_type": 1, "status": 1},
        )
    } if plan_ids_in_data else {}

    enriched: List[Dict[str, Any]] = []
    for row in rows_sorted:
        src = plan_map.get(row.get("plan_id"), {})
        enriched.append(
            {
                **row,
                "source_plan_base_date": src.get("base_date"),
                "source_plan_type": src.get("plan_type"),
                "source_plan_status": src.get("status"),
                "is_current_plan": row.get("plan_id") == plan_id,
            }
        )

    daily_rows = _build_lineage_live_book_daily_rows(
        trading_collections,
        finance_collections,
        data_collections,
        plan,
        user_id,
        lineage_ids,
        start_date=start_date,
        end_date=end_date,
    )
    if daily_rows:
        rows_for_chart = daily_rows
        row_source = "lineage_live_book_daily"
        caveat = (
            "净值曲线按当前组合血缘的一本账重建：用同血缘所有 live 成交逐日滚动持仓，"
            "并按当日收盘价估值；横轴为交易日，纵轴为当前血缘权益，不使用券商账户整体权益。"
        )
    else:
        rows_for_chart = enriched
        row_source = "portfolio_live_equity_legacy"
        caveat = (
            "当前血缘缺少可重建日线所需的成交或行情数据，暂回退到旧的 "
            "portfolio_live_equity plan 级快照；不使用券商账户整体权益。"
        )

    return {
        "anchor_plan_id": plan_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "params_hash": plan.get("params_hash"),
        "lineage_plan_ids": lineage_ids,
        "rows": rows_for_chart,
        "row_source": row_source,
        "securities_account_ids": account_ids,
        "source_plan_ids": plan_ids_in_data,
        "legacy_plan_rows": enriched,
        "caveat": caveat,
    }


def _lineage_query_for_plan(
    collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    include_plan_id_fallback: bool = True,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "strategy_template_id": plan.get("strategy_template_id"),
        "user_id": user_id,
    }
    params_hash = plan.get("params_hash")
    if params_hash and not include_plan_id_fallback:
        query["params_hash"] = params_hash
        return query

    plan_ids = _lineage_plan_ids_for_plan(collections, plan, user_id)
    branches = []
    if params_hash:
        branches.append({"params_hash": params_hash})
    if plan_ids:
        branches.append({"plan_id": {"$in": plan_ids}})
    if len(branches) == 1:
        query.update(branches[0])
    elif branches:
        query["$or"] = branches
    return query


def _lineage_open_trade_plan(
    collections: Dict[str, Any], plan: Dict[str, Any], user_id: str
) -> Optional[Dict[str, Any]]:
    query = _lineage_query_for_plan(collections, plan, user_id)
    query["status"] = {"$in": ["needs_review", "approved"]}
    rows = (
        collections["portfolio_trade_plans"]
        .find(
            query,
            {
                "_id": 0,
                "plan_id": 1,
                "base_date": 1,
                "created_at": 1,
                "status": 1,
                "plan_type": 1,
                "record_kind": 1,
                "manual_action": 1,
            },
        )
        .sort([("base_date", -1), ("created_at", -1)])
    )
    for row in rows:
        if _resilient_record_kind(row) == RECORD_KIND_TRADE_PLAN:
            return row
    return None


def _lineage_initial_capital(plan: Dict[str, Any]) -> float:
    params = plan.get("params_snapshot") or {}
    summary = plan.get("summary") or {}
    return _numeric(
        params.get("initial_capital", plan.get("initial_capital", summary.get("baseline_equity"))),
        1_000_000.0,
    )


def _first_valid_numeric(candidates: List[Tuple[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    """Return the first parseable finite float and its source label."""
    for source, raw in candidates:
        value = _optional_float(raw)
        if value is not None:
            return value, source
    return None, None


def _resolve_publish_plan_capital(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve plan-scoped equity/cash baseline for live publish repricing.

    Sizing at publish must use the plan's *own* book equity/cash (so unrelated
    funds parked in the same brokerage account never inflate this plan), not the
    broker account total asset. The fallback chain is fixed and deliberately
    never falls back to the account snapshot:

        equity: summary.baseline_equity -> summary.equity
                -> params_snapshot.initial_capital -> plan.initial_capital
        cash:   summary.baseline_cash   -> summary.cash
                -> params_snapshot.initial_capital -> plan.initial_capital

    Unlike ``_lineage_initial_capital`` (which silently defaults to 1,000,000),
    a fully missing baseline returns ``None`` so the caller can block the publish
    instead of guessing a budget.
    """
    params = plan.get("params_snapshot") or {}
    summary = plan.get("summary") or {}
    equity, equity_source = _first_valid_numeric(
        [
            ("summary.baseline_equity", summary.get("baseline_equity")),
            ("summary.equity", summary.get("equity")),
            ("params_snapshot.initial_capital", params.get("initial_capital")),
            ("plan.initial_capital", plan.get("initial_capital")),
        ]
    )
    cash, cash_source = _first_valid_numeric(
        [
            ("summary.baseline_cash", summary.get("baseline_cash")),
            ("summary.cash", summary.get("cash")),
            ("params_snapshot.initial_capital", params.get("initial_capital")),
            ("plan.initial_capital", plan.get("initial_capital")),
        ]
    )
    return {
        "equity": equity,
        "cash": cash,
        "equity_source": equity_source,
        "cash_source": cash_source,
        "capital_basis": summary.get("capital_basis"),
    }


def _compute_lineage_book_equity(
    *,
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    """Rolling one-book equity over all live executions in the plan's lineage.

    equity = initial_capital + total_realized_pnl + total_unrealized_pnl, where the
    positions are rebuilt from every live fill across the lineage's rebalance
    plan_ids (deduped per order). This is the continuous combo-scoped equity, as
    opposed to the per-plan points which each restart from ``initial_capital``.
    """
    lineage_ids, _signals, executions = _lineage_live_signal_execution_bundle(
        trading_collections, finance_collections, plan, user_id
    )
    initial_capital = _lineage_initial_capital(plan)
    if not executions:
        return {
            "equity": initial_capital,
            "initial_capital": initial_capital,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "market_value": 0.0,
            "cash": initial_capital,
            "lineage_plan_ids": lineage_ids,
            "execution_count": 0,
        }
    grouped = group_executions_by_order(executions)
    symbols = sorted({str(row.get("symbol")) for row in grouped if row.get("symbol")})
    prices, realtime_docs = _load_fresh_market_prices(data_collections, symbols)
    payload = build_lineage_live_positions(grouped, prices, realtime_docs=realtime_docs)
    summary = payload.get("summary") or {}
    realized = _numeric(summary.get("total_realized_pnl"))
    unrealized = _numeric(summary.get("total_unrealized_pnl"))
    market_value = _numeric(summary.get("total_market_value"))
    equity = initial_capital + realized + unrealized
    return {
        "equity": equity,
        "initial_capital": initial_capital,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "market_value": market_value,
        "cash": equity - market_value,
        "lineage_plan_ids": lineage_ids,
        "execution_count": len(grouped),
        "latest_price_update": payload.get("latest_price_update"),
    }


def _resolve_lineage_available_cash_for_remainder(
    *,
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    """Plan-book available cash for remainder top-up budgeting.

    ``plan_available_cash = lineage_book_cash - active_buy_order_frozen_cash``

    ``lineage_book_cash`` is rebuilt from executed fills at trade prices via
    ``_compute_lineage_book_equity``. Active buy signals in this lineage reserve
    cash at their persisted protected limit prices (see
    ``execution_policy.estimate_buy_order_cash_exposure``). Broker account cash
    is *not* used here; callers keep it for execution risk hard checks.
    """
    book = _compute_lineage_book_equity(
        trading_collections=trading_collections,
        finance_collections=finance_collections,
        data_collections=data_collections,
        plan=plan,
        user_id=user_id,
    )
    lineage_book_cash = _numeric(book.get("cash"))
    lineage_ids = list(book.get("lineage_plan_ids") or [])
    if not lineage_ids and plan.get("plan_id"):
        lineage_ids = [str(plan.get("plan_id"))]

    active_signals: List[Dict[str, Any]] = []
    if lineage_ids:
        active_signals = list(
            finance_collections["trade_signals"].find(
                {
                    "user_id": user_id,
                    "plan_id": {"$in": lineage_ids},
                    "mode": "live",
                    "status": {"$in": sorted(ACTIVE_SIGNAL_STATUSES)},
                },
                {
                    "_id": 0,
                    "symbol": 1,
                    "action": 1,
                    "side": 1,
                    "status": 1,
                    "size": 1,
                    "filled_qty": 1,
                    "filled_size": 1,
                    "effective_limit_price": 1,
                    "price_ceiling": 1,
                    "price": 1,
                },
            )
        )

    frozen_buy_cash = 0.0
    missing_frozen_price_symbols: List[str] = []
    for signal in active_signals:
        exposure = estimate_buy_order_cash_exposure(signal)
        if exposure is None:
            symbol = str(signal.get("symbol") or "")
            if symbol:
                missing_frozen_price_symbols.append(symbol)
            continue
        frozen_buy_cash += exposure

    plan_available_cash = max(0.0, lineage_book_cash - frozen_buy_cash)
    blockers: List[str] = []
    if missing_frozen_price_symbols:
        blockers.append("missing_frozen_price")

    return {
        "cash_basis": "lineage_book",
        "lineage_book_cash": lineage_book_cash,
        "frozen_buy_cash": frozen_buy_cash,
        "plan_available_cash": plan_available_cash,
        "broker_available_cash": None,
        "missing_frozen_price_symbols": sorted(set(missing_frozen_price_symbols)),
        "blockers": blockers,
        "lineage_plan_ids": lineage_ids,
    }


def _latest_lineage_position_snapshot(
    collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    before_or_on: Optional[str] = None,
) -> Dict[str, Any]:
    query = _lineage_query_for_plan(collections, plan, user_id)
    if plan.get("plan_id"):
        query["plan_id"] = {"$ne": plan.get("plan_id")}
    if before_or_on:
        query["as_of_date"] = {"$lte": before_or_on}
    return (
        collections["portfolio_paper_positions"].find_one(
            query, sort=[("as_of_date", -1), ("updated_at", -1)]
        )
        or {}
    )


PLAN_LIST_PROJECTION = {
    "_id": 0,
    "plan_id": 1,
    "strategy_template_id": 1,
    "user_id": 1,
    "preset_id": 1,
    "params_hash": 1,
    "params_snapshot": 1,
    "strategy_name": 1,
    "base_date": 1,
    "score_date": 1,
    "execute_date": 1,
    "plan_type": 1,
    "record_kind": 1,
    "origin": 1,
    "manual_action": 1,
    "is_liquidation": 1,
    "is_rebalance_day": 1,
    "previous_rebalance_date": 1,
    "previous_rebalance_plan_id": 1,
    "monitor_can_trade": 1,
    "trading_enabled": 1,
    "executable": 1,
    "non_executable_reason": 1,
    "status": 1,
    "summary": 1,
    "excluded_symbols": 1,
    "created_at": 1,
    "updated_at": 1,
    "reviewed_at": 1,
    "paper_executed_at": 1,
    "live_signals_published_at": 1,
    "live_signal_count": 1,
    "live_signal_status_summary": 1,
}


def _plan_has_live_signals(finance_collections: Dict[str, Any], plan_id: str, user_id: str) -> bool:
    return bool(
        finance_collections["trade_signals"].find_one(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "mode": "live",
            }
        )
    )


def _require_strategy(collections: Dict[str, Any], strategy_template_id: str) -> Dict[str, Any]:
    _ensure_default_strategy_templates(collections)
    strategy = collections["portfolio_strategy_configs"].find_one({"strategy_template_id": strategy_template_id})
    if not strategy:
        raise HTTPException(status_code=404, detail="strategy template not found")
    return strategy


def _require_param_preset(collections: Dict[str, Any], preset_id: str, user_id: str) -> Dict[str, Any]:
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": preset_id})
    if not preset:
        raise HTTPException(status_code=404, detail="parameter preset not found")
    if preset.get("user_id") not in {None, user_id}:
        raise HTTPException(status_code=404, detail="parameter preset not found")
    if preset.get("status") == "disabled":
        raise HTTPException(status_code=400, detail="parameter preset is disabled")
    return preset


def _validate_params_override(params: Dict[str, Any]) -> None:
    universe = params.get("universe_index")
    if universe is not None and universe not in ALLOWED_UNIVERSE_INDICES:
        raise HTTPException(status_code=400, detail="unsupported universe_index")
    has_growth = params.get("growth_weight") is not None
    has_cycle = params.get("cycle_weight") is not None
    if has_growth or has_cycle:
        try:
            growth = float(params.get("growth_weight", 0))
            cycle = float(params.get("cycle_weight", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="growth_weight and cycle_weight must be numbers")
        if growth < 0 or cycle < 0 or growth > 1 or cycle > 1:
            raise HTTPException(status_code=400, detail="growth_weight and cycle_weight must be between 0 and 1")
        if abs((growth + cycle) - 1) > 1e-6:
            raise HTTPException(status_code=400, detail="growth_weight and cycle_weight must add up to 1")
    if params.get("initial_capital") is not None:
        try:
            initial_capital = float(params["initial_capital"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="initial_capital must be a number")
        if initial_capital <= 0:
            raise HTTPException(status_code=400, detail="initial_capital must be greater than 0")


def _append_plan_operation_log(
    collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    operation_type: str,
    status: str,
    message: str = "",
    task_id: Optional[str] = None,
    symbol: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = datetime.utcnow()
    doc = {
        "log_id": str(uuid.uuid4()),
        "plan_id": plan_id,
        "user_id": user_id,
        "task_id": task_id,
        "operation_type": operation_type,
        "operation_symbol": symbol,
        "status": status,
        "message": message,
        "payload": payload or {},
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_operation_logs"].insert_one(doc)
    return doc


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


def _latest_available_score_watermark(stock_scores, universe_index: str) -> Dict[str, Any]:
    query: Dict[str, Any] = {"index_codes": universe_index}
    row = stock_scores.find_one(query, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
    source_scope = "index_codes"
    if not row:
        query = {}
        row = stock_scores.find_one(query, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
        source_scope = "global"
    score_date = str(row.get("score_date")) if row and row.get("score_date") else None
    count = stock_scores.count_documents({**query, "score_date": score_date}) if score_date else 0
    return {
        "latest_available_score_date": score_date,
        "latest_available_score_count": count,
        "latest_available_score_source": "stock_scores",
        "latest_available_score_scope": source_scope,
    }


def _weighted_growth_cycle_score(
    growth: Optional[float],
    cycle: Optional[float],
    growth_weight: Any,
    cycle_weight: Any,
) -> Optional[float]:
    """Mirror stock-scoring-system compute_score_series (growth_cycle_weighted)."""
    gw = float(growth_weight if growth_weight is not None else 0.3)
    cw = float(cycle_weight if cycle_weight is not None else 0.7)
    has_growth = growth is not None
    has_cycle = cycle is not None
    available_weight = (gw if has_growth else 0.0) + (cw if has_cycle else 0.0)
    if available_weight <= 0:
        return None
    weighted_sum = (float(growth or 0) * gw) + (float(cycle or 0) * cw)
    return weighted_sum / available_weight


def _score_value_from_stock_row(row: Dict[str, Any], config: Dict[str, Any]) -> Optional[float]:
    score_type = str(config.get("score_type") or "growth_cycle_weighted")
    if score_type == "growth_cycle_weighted":
        return _weighted_growth_cycle_score(
            _optional_float(row.get("growth_score")),
            _optional_float(row.get("cycle_score")),
            config.get("growth_weight", 0.3),
            config.get("cycle_weight", 0.7),
        )
    score_column = str(config.get("score_column") or "")
    if score_column.startswith("composite_score."):
        key = score_column.split(".", 1)[1]
        composite = row.get("composite_score") or {}
        if isinstance(composite, dict):
            return _optional_float(composite.get(key))
    return None


def _resolve_plan_score_date(plan: Dict[str, Any], universe_index: str) -> Optional[str]:
    score_date = _normalize_yyyymmdd(plan.get("score_date"))
    if score_date:
        return score_date
    base_date = _normalize_yyyymmdd(plan.get("base_date"))
    if not base_date:
        return None
    row = finance_db["stock_scores"].find_one(
        {"score_date": {"$lte": base_date}, "index_codes": universe_index},
        sort=[("score_date", -1)],
        projection={"_id": 0, "score_date": 1},
    )
    if row and row.get("score_date"):
        return str(row["score_date"])
    row = finance_db["stock_scores"].find_one(
        {"score_date": {"$lte": base_date}},
        sort=[("score_date", -1)],
        projection={"_id": 0, "score_date": 1},
    )
    return str(row["score_date"]) if row and row.get("score_date") else None


def _normalized_plan_params_from_snapshot(params_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {key: params_snapshot[key] for key in sorted(PLAN_SNAPSHOT_PARAM_KEYS) if key in params_snapshot}


def _load_latest_close_prices_loose(volume_price, symbols: List[str]) -> Dict[str, float]:
    """Latest close keyed by the *requested* symbol, tolerating suffix mismatch.

    stock_scores and volume_price may store a symbol with or without the
    exchange suffix (600000 vs 600000.SH); match on all known variants so the
    bench reference price is not silently blank.
    """
    prices: Dict[str, float] = {}
    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": {"$in": _symbol_query_values(symbol)}},
            sort=[("trade_date", -1)],
            projection={"_id": 0, "close": 1},
        )
        if not row:
            continue
        try:
            price = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def _load_latest_daily_bars_loose(volume_price, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Latest daily bar keyed by the requested symbol, tolerating suffix mismatch."""
    bars: Dict[str, Dict[str, Any]] = {}
    projection = {
        "_id": 0,
        "trade_date": 1,
        "symbol": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "pre_close": 1,
        "change": 1,
        "pct_chg": 1,
        "volume": 1,
        "amount": 1,
        "update_time": 1,
        "source": 1,
    }
    if not symbols:
        return bars
    candidates_by_symbol = {symbol: list(_symbol_query_values(symbol)) for symbol in symbols}
    all_candidates = sorted({c for cands in candidates_by_symbol.values() for c in cands})

    # Batch the latest bar for every requested symbol in one aggregation instead
    # of one find_one per symbol (bench previously issued ~15-25 sequential
    # round-trips here). The (symbol, trade_date) index serves the sort so $first
    # returns each symbol's newest bar; fall back to per-symbol on any failure.
    aggregate = getattr(volume_price, "aggregate", None)
    if aggregate is not None:
        try:
            latest_by_stored: Dict[str, Dict[str, Any]] = {}
            for row in aggregate(
                [
                    {"$match": {"symbol": {"$in": all_candidates}}},
                    {"$sort": {"symbol": 1, "trade_date": -1}},
                    {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
                ]
            ):
                doc = row.get("doc") or {}
                sym = doc.get("symbol")
                if sym:
                    latest_by_stored[str(sym)] = {k: doc.get(k) for k in projection if k in doc}
            for symbol, candidates in candidates_by_symbol.items():
                best = None
                best_date = ""
                for candidate in candidates:
                    candidate_row = latest_by_stored.get(candidate)
                    if not candidate_row:
                        continue
                    row_date = str(candidate_row.get("trade_date") or "")
                    if best is None or row_date > best_date:
                        best, best_date = candidate_row, row_date
                if best:
                    bars[symbol] = best
            return bars
        except Exception:
            bars = {}

    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": {"$in": candidates_by_symbol[symbol]}},
            sort=[("trade_date", -1)],
            projection=projection,
        )
        if row:
            bars[symbol] = row
    return bars


def _ranked_score_projection(config: Dict[str, Any]) -> Dict[str, int]:
    """Only the score inputs needed to rank -- never name/industry/composite_score.

    In prod, transferring the full ~1100-row universe with the ``composite_score``
    dict and the name/industry strings cost ~3s per call, while the scalar score
    fields alone come back in ~40ms. name/industry for the handful of displayed
    symbols are fetched separately by the caller.
    """
    projection = {"_id": 0, "symbol": 1}
    score_type = str(config.get("score_type") or "growth_cycle_weighted")
    if score_type == "growth_cycle_weighted":
        projection["growth_score"] = 1
        projection["cycle_score"] = 1
    else:
        projection["composite_score"] = 1
    return projection


def _score_symbol_meta(
    stock_scores,
    score_date: str,
    symbols,
) -> Dict[str, Dict[str, Any]]:
    """name/industry for a small display set, keyed by suffix-insensitive symbol."""
    meta: Dict[str, Dict[str, Any]] = {}
    variants = sorted({v for sym in symbols for v in _symbol_query_values(sym) if v})
    if not variants:
        return meta
    for row in stock_scores.find(
        {"score_date": score_date, "symbol": {"$in": variants}},
        {"_id": 0, "symbol": 1, "name": 1, "industry": 1},
    ):
        meta[_symbol_key(row.get("symbol"))] = row
    return meta


_RANKED_UNIVERSE_CACHE: Dict[tuple, tuple] = {}
_RANKED_UNIVERSE_CACHE_LOCK = threading.Lock()
_RANKED_UNIVERSE_TTL_SECONDS = float(os.getenv("RANKED_UNIVERSE_CACHE_TTL_SECONDS", "600"))


def _ranked_universe_cache_key(score_date: str, universe_index: str, config: Dict[str, Any]) -> tuple:
    return (
        str(score_date),
        str(universe_index),
        str(config.get("score_type") or "growth_cycle_weighted"),
        str(config.get("growth_weight", 0.3)),
        str(config.get("cycle_weight", 0.7)),
        str(config.get("score_column") or ""),
    )


def _build_ranked_score_universe(
    stock_scores,
    score_date: str,
    universe_index: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Ranked universe for (score_date, universe, score config).

    Fetching + ranking the full ~1100-row universe costs a variable 0.5-0.8s in
    prod (the row transfer, not the 5ms server-side query), and it is recomputed
    on every bench / live-overlay / plan-detail call even though the result is
    identical for a given score_date and config. Cache it in-process with a short
    TTL so only the first call per key pays the transfer. Callers only read the
    returned rows, so sharing the cached list is safe.
    """
    key = _ranked_universe_cache_key(score_date, universe_index, config)
    now = time.monotonic()
    with _RANKED_UNIVERSE_CACHE_LOCK:
        cached = _RANKED_UNIVERSE_CACHE.get(key)
        if cached is not None and now - cached[0] < _RANKED_UNIVERSE_TTL_SECONDS:
            return cached[1]

    rows = list(
        stock_scores.find(
            {"score_date": score_date, "index_codes": universe_index},
            _ranked_score_projection(config),
        )
    )
    ranked: List[Dict[str, Any]] = []
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        score_value = _score_value_from_stock_row(row, config)
        if score_value is None:
            continue
        ranked.append(
            {
                "symbol": str(symbol),
                "score_value": score_value,
            }
        )
    ranked.sort(key=lambda item: (-item["score_value"], item["symbol"]))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx

    with _RANKED_UNIVERSE_CACHE_LOCK:
        _RANKED_UNIVERSE_CACHE[key] = (now, ranked)
        if len(_RANKED_UNIVERSE_CACHE) > 64:
            for stale_key in [
                k for k, (ts, _) in _RANKED_UNIVERSE_CACHE.items()
                if now - ts >= _RANKED_UNIVERSE_TTL_SECONDS
            ]:
                _RANKED_UNIVERSE_CACHE.pop(stale_key, None)
    return ranked


def _enrich_plan_items_with_score_snapshot(
    *,
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fill display metadata from the plan score snapshot without mutating DB."""
    if not items:
        return items
    if not plan.get("score_date") and not plan.get("params_snapshot"):
        return items
    params = dict(plan.get("params_snapshot") or {})
    universe_index = str(params.get("universe_index") or "csi1000")
    score_date = _resolve_plan_score_date(plan, universe_index)
    symbols = sorted({str(item.get("symbol")) for item in items if item.get("symbol")})
    if not score_date or not symbols:
        return items

    # Resolve all symbols in two batched `$in` queries instead of 2-3 find_one
    # round-trips per symbol. Prefer rows scoped to the plan's universe index,
    # then fall back (any index) for symbols still unresolved.
    score_projection = {
        "_id": 0,
        "symbol": 1,
        "name": 1,
        "industry": 1,
        "growth_score": 1,
        "cycle_score": 1,
        "composite_score": 1,
    }
    all_variants = sorted({value for symbol in symbols for value in _symbol_query_values(symbol)})
    stock_scores = finance_db["stock_scores"]
    score_rows: Dict[str, Dict[str, Any]] = {}
    if all_variants:
        for row in stock_scores.find(
            {"score_date": score_date, "symbol": {"$in": all_variants}, "index_codes": universe_index},
            score_projection,
        ):
            key = _symbol_key(row.get("symbol"))
            if key and key not in score_rows:
                score_rows[key] = row
        missing_variants = sorted(
            {
                value
                for symbol in symbols
                if _symbol_key(symbol) not in score_rows
                for value in _symbol_query_values(symbol)
            }
        )
        if missing_variants:
            for row in stock_scores.find(
                {"score_date": score_date, "symbol": {"$in": missing_variants}},
                score_projection,
            ):
                key = _symbol_key(row.get("symbol"))
                if key and key not in score_rows:
                    score_rows[key] = row

    stock_info_rows: Dict[str, Dict[str, Any]] = {}
    stock_info = data_collections.get("stock_info") if data_collections else None
    if stock_info is not None and all_variants:
        for row in stock_info.find(
            {"symbol": {"$in": all_variants}},
            {"_id": 0, "symbol": 1, "name": 1, "industry": 1},
        ):
            key = _symbol_key(row.get("symbol"))
            if key and key not in stock_info_rows:
                stock_info_rows[key] = row

    enriched: List[Dict[str, Any]] = []
    for item in items:
        symbol = item.get("symbol")
        key = _symbol_key(symbol)
        score_row = score_rows.get(key) or {}
        info_row = stock_info_rows.get(key) or {}
        patch: Dict[str, Any] = {}
        if not item.get("name"):
            patch["name"] = score_row.get("name") or info_row.get("name") or ""
        if not item.get("industry"):
            patch["industry"] = score_row.get("industry") or info_row.get("industry") or ""
        if item.get("score_value") is None and score_row:
            patch["score_value"] = _score_value_from_stock_row(score_row, params)
        enriched.append({**item, **patch} if patch else item)
    return enriched


def _enrich_plan_items_with_estimated_amount(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill missing trade notional from delta and estimated price for display."""
    enriched: List[Dict[str, Any]] = []
    for item in items:
        if item.get("estimated_amount") is not None:
            enriched.append(item)
            continue
        delta = _optional_float(item.get("delta_shares"))
        price = _optional_float(item.get("estimated_price"))
        if delta is None or delta == 0 or price is None or price <= 0:
            enriched.append(item)
            continue
        enriched.append({**item, "estimated_amount": abs(delta) * price})
    return enriched


def _plan_item_weight(item: Dict[str, Any]) -> Optional[float]:
    for key in ("target_weight", "weight", "estimated_weight", "current_weight"):
        value = _optional_float(item.get(key))
        if value is not None:
            return value
    return None


def _sw1_industry_for_item(item: Dict[str, Any]) -> str:
    """Best-effort SW1 bucket. Score snapshot industry is the current source."""
    industry = str(item.get("industry") or "").strip()
    if not industry:
        return "未分类"
    # Some upstream values are formatted as "一级/二级" or "一级-二级".
    for sep in ("/", "／", "-", "—", ">", "：", ":"):
        if sep in industry:
            head = industry.split(sep, 1)[0].strip()
            return head or "未分类"
    return industry


def _portfolio_llm_actionable_items(
    items: List[Dict[str, Any]],
    request: PortfolioLlmRiskReviewRequest,
) -> List[Dict[str, Any]]:
    include_actions = {str(action or "").lower() for action in request.include_actions}
    selected: List[Dict[str, Any]] = []
    for item in items:
        symbol = item.get("symbol")
        action = str(item.get("action") or "").lower()
        if not symbol:
            continue
        if request.target_scope != "bench" and action not in include_actions:
            continue
        rules_risk = item.get("ai_risk") if isinstance(item.get("ai_risk"), dict) else {}
        if not _risk_severity_at_least(rules_risk.get("severity"), request.min_rules_severity):
            continue
        selected.append(item)
    return selected


def _portfolio_llm_industry_groups(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[_sw1_industry_for_item(item)].append(item)
    return dict(groups)


def _portfolio_llm_item_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "name": item.get("name") or item.get("symbol"),
        "industry": _sw1_industry_for_item(item),
        "action": str(item.get("action") or "").lower(),
        "weight": _plan_item_weight(item),
        "score_value": item.get("score_value"),
        "rules_risk": item.get("ai_risk") if isinstance(item.get("ai_risk"), dict) else None,
    }


def _portfolio_llm_industry_context(groups: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    total_weight = 0.0
    for industry, industry_items in groups.items():
        weight = sum(abs(_plan_item_weight(item) or 0.0) for item in industry_items)
        total_weight += weight
        rows.append(
            {
                "industry": industry,
                "symbol_count": len(industry_items),
                "weight": weight,
                "symbols": [str(item.get("symbol")) for item in industry_items if item.get("symbol")],
            }
        )
    rows.sort(key=lambda row: (row.get("weight") or 0.0, row.get("symbol_count") or 0), reverse=True)
    return {
        "total_weight": total_weight,
        "industries": rows,
    }


def _portfolio_llm_task_query(
    *,
    user_id: str,
    plan_id: str,
    run_id: Optional[str] = None,
    target_scope: str = "plan",
    statuses: Optional[set] = None,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "type": PORTFOLIO_RISK_LLM_TASK_TYPE,
        "user_id": user_id,
        "params.plan_id": plan_id,
    }
    if target_scope == "bench":
        query["params.target_scope"] = "bench"
    else:
        query["$or"] = [
            {"params.target_scope": {"$in": [None, "plan"]}},
            {"params.target_scope": {"$exists": False}},
        ]
    if run_id:
        query["params.run_id"] = run_id
    if statuses:
        query["status"] = {"$in": sorted(statuses)}
    return query


def _analysis_tasks_collection():
    try:
        return finance_db["analysis_tasks"]
    except Exception:
        return None


def _portfolio_llm_run_status(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(tasks)
    completed = 0
    failed = 0
    pending = 0
    processing = 0
    parse_error = 0
    for task in tasks:
        status = normalize_task_status(task.get("status"))
        if status == "completed":
            completed += 1
        elif status == "completed_with_parse_error":
            parse_error += 1
            failed += 1
        elif status == "failed":
            failed += 1
        elif status == "processing":
            processing += 1
        elif status == "pending":
            pending += 1
    if total == 0:
        run_status = "not_found"
    elif completed == total:
        run_status = "completed"
    elif failed and completed:
        run_status = "completed_with_failures" if pending + processing == 0 else "partial"
    elif failed == total:
        run_status = "failed"
    else:
        run_status = "running"
    return {
        "status": run_status,
        "total": total,
        "completed": completed,
        "failed": failed,
        "parse_error": parse_error,
        "pending": pending,
        "processing": processing,
        "partial_summary": f"{completed}/{total} 行业任务完成，{failed} 个失败" if total else "",
    }


def _portfolio_llm_run_tasks(
    *,
    user_id: str,
    plan_id: str,
    run_id: str,
    target_scope: str = "plan",
) -> List[Dict[str, Any]]:
    tasks_col = _analysis_tasks_collection()
    if tasks_col is None:
        return []
    try:
        return list(
            tasks_col.find(
                _portfolio_llm_task_query(
                    user_id=user_id,
                    plan_id=plan_id,
                    run_id=run_id,
                    target_scope=target_scope,
                )
            )
        )
    except Exception:
        return []


def _latest_completed_portfolio_llm_run_id(
    *,
    user_id: str,
    plan_id: str,
    target_scope: str = "plan",
    eligible_statuses: Optional[set] = None,
) -> Optional[str]:
    statuses = eligible_statuses or {"completed", "completed_with_failures"}
    tasks_col = _analysis_tasks_collection()
    if tasks_col is None:
        return None
    try:
        tasks = list(
            tasks_col.find(
                _portfolio_llm_task_query(
                    user_id=user_id,
                    plan_id=plan_id,
                    target_scope=target_scope,
                )
            )
        )
    except Exception:
        return None
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        run_id = ((task.get("params") or {}).get("run_id"))
        if run_id:
            grouped[str(run_id)].append(task)
    candidates: List[Tuple[datetime, str]] = []
    for run_id, run_tasks in grouped.items():
        summary = _portfolio_llm_run_status(run_tasks)
        if summary["status"] not in statuses:
            continue
        latest_created = max(
            (task.get("created_at") for task in run_tasks if isinstance(task.get("created_at"), datetime)),
            default=datetime.min,
        )
        candidates.append((latest_created, run_id))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _attach_llm_risk_to_items(
    *,
    items: List[Dict[str, Any]],
    docs: List[Dict[str, Any]],
    run_id: str,
) -> Tuple[List[Dict[str, Any]], int]:
    symbol_docs = {
        _symbol_key(doc.get("symbol")): doc
        for doc in docs
        if doc.get("scope") == "symbol" and doc.get("symbol")
    }
    enriched: List[Dict[str, Any]] = []
    attached_symbol_count = 0
    for item in items:
        key = _symbol_key(item.get("symbol"))
        llm_doc = symbol_docs.get(key)
        if not llm_doc:
            enriched.append(item)
            continue
        risk = dict(item.get("ai_risk") or {})
        risk["llm"] = {
            "engine_version": llm_doc.get("engine_version"),
            "severity": llm_doc.get("severity"),
            "summary": llm_doc.get("summary"),
            "findings": llm_doc.get("findings") or [],
            "model": llm_doc.get("model"),
            "analyzed_at": llm_doc.get("analyzed_at"),
            "run_id": run_id,
            "industry": llm_doc.get("industry"),
        }
        enriched.append({**item, "ai_risk": risk})
        attached_symbol_count += 1
    return enriched, attached_symbol_count


def _enrich_plan_items_with_llm_risk(
    *,
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
    user_id: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not items:
        return plan, items

    symbols = [str(item.get("symbol") or "") for item in items if item.get("symbol")]
    reviews_by_symbol = get_symbol_signal_reviews(get_analyzer_db()).get_latest_reviews(symbols) if symbols else {}
    items = attach_signal_reviews_to_items(items, reviews_by_symbol)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    grouped = ledger.get_active_for_symbols(symbols) if symbols else {}
    opportunity_grouped: Dict[str, List[Dict[str, Any]]] = {}
    if opportunity_ledger_read_enabled() and symbols:
        opportunity_ledger = get_symbol_opportunity_ledger(get_analyzer_db())
        opportunity_grouped = opportunity_ledger.get_active_for_symbols(symbols)
    if any(rows for rows in grouped.values()) or any(rows for rows in opportunity_grouped.values()):
        industries = sorted(
            {
                str(item.get("industry") or "").strip()
                for item in items
                if item.get("industry")
            }
        )
        industry_grouped = ledger.get_active_for_industries(industries) if industries else {}
        opportunity_industry_grouped = (
            opportunity_ledger.get_active_for_industries(industries)
            if industries and opportunity_grouped
            else {}
        )
        enriched, _attached = attach_ledger_llm_to_items(items, grouped)
        enriched, _opp_attached = attach_ledger_opportunity_to_items(enriched, opportunity_grouped)
        plan_summary = dict(plan.get("summary") or {})
        if any(rows for rows in grouped.values()):
            plan_summary["ai_risk_llm_summary"] = build_ledger_llm_summary(
                grouped_findings=grouped,
                industry_grouped=industry_grouped,
            )
        if any(rows for rows in opportunity_grouped.values()):
            plan_summary["ai_opportunity_llm_summary"] = build_ledger_opportunity_summary(
                grouped_findings=opportunity_grouped,
                industry_grouped=opportunity_industry_grouped,
            )
        return {**plan, "summary": plan_summary}, enriched

    if not legacy_ledger_read_enabled():
        return plan, items

    plan_id = str(plan.get("plan_id") or "")
    run_id = _latest_completed_portfolio_llm_run_id(user_id=user_id, plan_id=plan_id)
    if not run_id:
        return plan, items

    risk_coll = get_analyzer_db()[LEGACY_PORTFOLIO_RISK_LLM_COLLECTION]
    docs = list(risk_coll.find({"plan_id": plan_id, "run_id": run_id}, {"_id": 0}))
    if not docs:
        return plan, items

    industry_docs = [doc for doc in docs if doc.get("scope") == "industry"]
    enriched, attached_symbol_count = _attach_llm_risk_to_items(items=items, docs=docs, run_id=run_id)
    run_summary = _portfolio_llm_run_status(
        _portfolio_llm_run_tasks(user_id=user_id, plan_id=plan_id, run_id=run_id)
    )

    summary = {
        "run_id": run_id,
        "status": run_summary.get("status") or "completed",
        "symbol_count": attached_symbol_count,
        "industry_count": len(industry_docs),
        "industries": [
            {
                "industry": doc.get("industry"),
                "severity": doc.get("severity"),
                "summary": doc.get("summary"),
                "findings": doc.get("findings") or [],
                "analyzed_at": doc.get("analyzed_at"),
                "model": doc.get("model"),
            }
            for doc in sorted(industry_docs, key=lambda row: str(row.get("industry") or ""))
        ],
        "source": "legacy_plan_run",
    }
    plan_summary = dict(plan.get("summary") or {})
    plan_summary["ai_risk_llm_summary"] = summary
    return {**plan, "summary": plan_summary}, enriched


def _build_plan_bench_payload(
    plan: Dict[str, Any],
    holdings: List[Dict[str, Any]],
    *,
    bench_multiplier: float,
    data_collections: Dict[str, Any],
) -> Dict[str, Any]:
    """Football-style lineup for a lineage.

    Starters are the *actual current holdings* (live or paper), not the plan
    items frozen at generation time: after a 10-day cadence with manual swaps the
    real lineup drifts from the original recommendation, so "今日阵容" must always
    mirror "最新持仓". The bench is the freshly-scored candidate pool minus anyone
    already on the field (held), matched on a suffix-insensitive key.
    """
    params = dict(plan.get("params_snapshot") or {})
    universe_index = str(params.get("universe_index") or "csi1000")
    top_n = max(1, _int_value(params.get("top_n"), 10))
    multiplier = max(1.0, float(bench_multiplier or 1.5))
    pool_size = max(top_n, math.ceil(top_n * multiplier))
    score_date = _resolve_plan_score_date(plan, universe_index)
    if not score_date:
        raise HTTPException(status_code=404, detail="score snapshot not found for plan bench")

    ranked = _build_ranked_score_universe(finance_db["stock_scores"], score_date, universe_index, params)
    ranked_pool = ranked[:pool_size]
    rank_by_key = {_symbol_key(row["symbol"]): row for row in ranked}

    starters: List[Dict[str, Any]] = []
    on_field_keys: set = set()
    for pos in sorted(
        holdings, key=lambda row: _optional_float(row.get("market_value")) or 0.0, reverse=True
    ):
        shares = _int_value(pos.get("shares"))
        if shares <= 0:
            continue
        symbol = str(pos.get("symbol") or "")
        if not symbol:
            continue
        key = _symbol_key(symbol)
        on_field_keys.add(key)
        score_row = rank_by_key.get(key) or {}
        price = _optional_float(pos.get("last_price")) or _optional_float(pos.get("price"))
        starters.append(
            {
                "symbol": symbol,
                "name": pos.get("name") or "",
                "industry": "",
                "rank": score_row.get("rank"),
                "score_value": score_row.get("score_value"),
                "target_shares": shares,
                "current_shares": shares,
                "estimated_price": price,
                "market_value": _optional_float(pos.get("market_value")),
                "in_universe": bool(score_row),
            }
        )

    bench_symbols = [
        row["symbol"]
        for row in ranked_pool
        if _symbol_key(row["symbol"]) not in on_field_keys
    ]

    # name/industry are not carried on the ranked universe (fetching them for all
    # ~1100 rows cost seconds in prod); resolve them only for displayed symbols.
    display_symbols = {starter["symbol"] for starter in starters} | set(bench_symbols)
    score_meta_by_key = _score_symbol_meta(finance_db["stock_scores"], score_date, display_symbols)
    for starter in starters:
        meta = score_meta_by_key.get(_symbol_key(starter["symbol"])) or {}
        if not starter["name"]:
            starter["name"] = meta.get("name") or ""
        if not starter["industry"]:
            starter["industry"] = meta.get("industry") or ""

    starters_missing_price = [s["symbol"] for s in starters if s.get("estimated_price") is None]
    latest_bars = _load_latest_daily_bars_loose(
        data_collections["volume_price"], list({*bench_symbols, *starters_missing_price})
    )

    bench: List[Dict[str, Any]] = []
    for symbol in bench_symbols:
        score_row = rank_by_key.get(_symbol_key(symbol)) or {}
        meta = score_meta_by_key.get(_symbol_key(symbol)) or {}
        latest_bar = latest_bars.get(symbol) or {}
        bench.append(
            {
                "symbol": symbol,
                "name": meta.get("name") or "",
                "industry": meta.get("industry") or "",
                "rank": score_row.get("rank"),
                "score_value": score_row.get("score_value"),
                "latest_close": _optional_float(latest_bar.get("close")),
                "latest_trade_date": latest_bar.get("trade_date"),
                "latest_open": _optional_float(latest_bar.get("open")),
                "latest_high": _optional_float(latest_bar.get("high")),
                "latest_low": _optional_float(latest_bar.get("low")),
                "latest_pre_close": _optional_float(latest_bar.get("pre_close")),
                "latest_change": _optional_float(latest_bar.get("change")),
                "latest_pct_chg": _optional_float(latest_bar.get("pct_chg")),
                "latest_volume": _optional_float(latest_bar.get("volume")),
                "latest_amount": _optional_float(latest_bar.get("amount")),
                "latest_update_time": latest_bar.get("update_time"),
                "latest_price_source": latest_bar.get("source"),
            }
        )

    for starter in starters:
        if starter.get("estimated_price") is None:
            latest_bar = latest_bars.get(starter["symbol"]) or {}
            starter["estimated_price"] = _optional_float(latest_bar.get("close"))

    return {
        "plan_id": plan.get("plan_id"),
        "base_date": plan.get("base_date"),
        "score_date": score_date,
        "universe_index": universe_index,
        "top_n": top_n,
        "lot_size": max(1, _int_value(params.get("lot_size"), 100)),
        "bench_multiplier": multiplier,
        "pool_size": pool_size,
        "starters": starters,
        "bench": bench,
    }


def _enrich_bench_payload_with_llm_risk(
    *,
    plan: Dict[str, Any],
    payload: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    bench_items = list(payload.get("bench") or [])
    if not bench_items:
        return payload

    symbols = [str(item.get("symbol") or "") for item in bench_items if item.get("symbol")]
    reviews_by_symbol = get_symbol_signal_reviews(get_analyzer_db()).get_latest_reviews(symbols) if symbols else {}
    bench_items = attach_signal_reviews_to_items(bench_items, reviews_by_symbol)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    grouped = ledger.get_active_for_symbols(symbols) if symbols else {}
    opportunity_grouped: Dict[str, List[Dict[str, Any]]] = {}
    if opportunity_ledger_read_enabled() and symbols:
        opportunity_ledger = get_symbol_opportunity_ledger(get_analyzer_db())
        opportunity_grouped = opportunity_ledger.get_active_for_symbols(symbols)
    if any(rows for rows in grouped.values()) or any(rows for rows in opportunity_grouped.values()):
        enriched_bench, _attached_symbol_count = attach_ledger_llm_to_items(bench_items, grouped)
        enriched_bench, _opp_attached = attach_ledger_opportunity_to_items(enriched_bench, opportunity_grouped)
        enriched_payload = {
            **payload,
            "bench": enriched_bench,
        }
        if any(rows for rows in grouped.values()):
            enriched_payload["ai_risk_llm_summary"] = build_ledger_llm_summary(grouped_findings=grouped)
        if any(rows for rows in opportunity_grouped.values()):
            enriched_payload["ai_opportunity_llm_summary"] = build_ledger_opportunity_summary(
                grouped_findings=opportunity_grouped
            )
        return enriched_payload

    if not legacy_ledger_read_enabled():
        return payload

    plan_id = str(plan.get("plan_id") or payload.get("plan_id") or "")
    run_id = _latest_completed_portfolio_llm_run_id(
        user_id=user_id,
        plan_id=plan_id,
        target_scope="bench",
        eligible_statuses={"completed", "completed_with_failures", "running", "partial"},
    )
    if not run_id:
        return payload

    risk_coll = get_analyzer_db()[LEGACY_PORTFOLIO_RISK_LLM_COLLECTION]
    docs = list(risk_coll.find({"plan_id": plan_id, "run_id": run_id}, {"_id": 0}))
    if not docs:
        return payload

    enriched_bench, attached_symbol_count = _attach_llm_risk_to_items(
        items=bench_items,
        docs=docs,
        run_id=run_id,
    )
    industry_docs = [doc for doc in docs if doc.get("scope") == "industry"]
    run_summary = _portfolio_llm_run_status(
        _portfolio_llm_run_tasks(
            user_id=user_id,
            plan_id=plan_id,
            run_id=run_id,
            target_scope="bench",
        )
    )
    return {
        **payload,
        "bench": enriched_bench,
        "ai_risk_llm_summary": {
            "run_id": run_id,
            "status": run_summary.get("status") or "completed",
            "symbol_count": attached_symbol_count,
            "industry_count": len(industry_docs),
            "industries": [
                {
                    "industry": doc.get("industry"),
                    "severity": doc.get("severity"),
                    "summary": doc.get("summary"),
                    "findings": doc.get("findings") or [],
                    "analyzed_at": doc.get("analyzed_at"),
                    "model": doc.get("model"),
                }
                for doc in sorted(industry_docs, key=lambda row: str(row.get("industry") or ""))
            ],
            "source": "legacy_plan_run",
        },
    }


def _require_securities_account(collections: Dict[str, Any], securities_account_id: str, user_id: str) -> Dict[str, Any]:
    query_ids: List[Any] = [securities_account_id]
    try:
        query_ids.insert(0, ObjectId(securities_account_id))
    except Exception:
        pass
    account = None
    for query_id in query_ids:
        account = collections["securities_accounts"].find_one({"_id": query_id, "user_id": user_id})
        if account:
            break
    if not account:
        raise HTTPException(status_code=404, detail="securities account not found")
    account["_id"] = str(account.get("_id"))
    return account


def _account_live_enabled(account: Dict[str, Any]) -> bool:
    return bool(
        account.get("live_trading_enabled")
        or account.get("live_enabled")
        or account.get("allow_live_trading")
    )


def _build_live_plan_item_context(
    *,
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
    user_id: str,
) -> Dict[str, Dict[str, Any]]:
    if not plan.get("live_signals_published_at") and _int_value(plan.get("live_signal_count")) <= 0:
        return {}

    started_at = time.perf_counter()
    marks: List[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter() - started_at))

    symbols = {_symbol_key(item.get("symbol")) for item in items if item.get("symbol")}
    if not symbols:
        return {}
    symbol_values = sorted(
        {
            value
            for item in items
            for value in _symbol_query_values(item.get("symbol"))
            if value
        }
    )

    signal_query = {
        "user_id": user_id,
        "plan_id": plan.get("plan_id"),
        "mode": "live",
        "symbol": {"$in": symbol_values},
    }
    plan_signals = list(
        finance_collections["trade_signals"].find(
            signal_query,
            {
                "_id": 0,
                "order_id": 1,
                "symbol": 1,
                "action": 1,
                "side": 1,
                "status": 1,
                "size": 1,
                "filled_qty": 1,
                "filled_size": 1,
                "securities_account_id": 1,
            },
        )
    )
    mark("signals")
    order_ids = [row.get("order_id") for row in plan_signals if row.get("order_id")]
    account_ids = {str(row.get("securities_account_id")) for row in plan_signals if row.get("securities_account_id")}

    account_positions: List[Dict[str, Any]] = []
    if account_ids:
        account_positions = list(
            finance_collections["trader_positions"].find(
                {
                    "user_id": user_id,
                    "securities_account_id": {"$in": sorted(account_ids)},
                    "symbol": {"$in": symbol_values},
                },
                {"_id": 0, "symbol": 1, "qty": 1, "quantity": 1, "volume": 1, "shares": 1, "can_use_volume": 1, "available_qty": 1},
            )
        )
    mark("account_positions")

    context: Dict[str, Dict[str, Any]] = {
        symbol: {
            "live_signed_delta": 0,
            "live_filled_qty": 0,
            "live_remaining_qty": 0,
            "live_status": None,
            "account_current_shares": None,
            "account_available_shares": None,
        }
        for symbol in symbols
    }

    signal_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for signal in plan_signals:
        symbol = _symbol_key(signal.get("symbol"))
        if symbol in context:
            signal_by_symbol.setdefault(symbol, []).append(signal)
            context[symbol]["live_filled_qty"] += _signal_filled_qty(signal)
            context[symbol]["live_signed_delta"] += _signal_signed_filled_qty(signal)

    terminal_statuses = {"filled", "cancelled", "canceled", "rejected", "failed", "partial_cancelled"}
    for symbol, signals in signal_by_symbol.items():
        target_qty = sum(_int_value(signal.get("size")) for signal in signals)
        filled_qty = context[symbol]["live_filled_qty"]
        context[symbol]["live_remaining_qty"] = max(0, target_qty - filled_qty)
        statuses = [str(signal.get("status") or "") for signal in signals if signal.get("status")]
        if statuses:
            if all(status == "filled" for status in statuses):
                live_status = "filled"
            elif any(status in {"partial_filled", "partial_cancelled"} for status in statuses) or filled_qty:
                live_status = "partial_filled"
            elif any(status not in terminal_statuses for status in statuses):
                live_status = "active"
            else:
                live_status = statuses[-1]
            context[symbol]["live_status"] = live_status

    for pos in account_positions:
        symbol = _symbol_key(pos.get("symbol"))
        if symbol not in context:
            continue
        qty = _int_value(pos.get("qty", pos.get("quantity", pos.get("volume", pos.get("shares", 0)))))
        available = _int_value(pos.get("can_use_volume", pos.get("available_qty", qty)))
        current = context[symbol]
        current["account_current_shares"] = (current["account_current_shares"] or 0) + qty
        current["account_available_shares"] = (current["account_available_shares"] or 0) + available

    total_elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("PORTFOLIO_PLAN_DETAIL_TIMING_SECONDS", "1"))
    if total_elapsed >= threshold:
        previous = 0.0
        parts = []
        for label, elapsed in marks:
            parts.append(f"{label}={elapsed - previous:.3f}s")
            previous = elapsed
        log.warning(
            "Portfolio plan live context timing total=%.3fs plan_id=%s signals=%d order_ids=%d account_ids=%d symbols=%d %s",
            total_elapsed,
            plan.get("plan_id"),
            len(plan_signals),
            len(order_ids),
            len(account_ids),
            len(symbol_values),
            " ".join(parts),
        )

    return context


def _attach_live_plan_item_context(
    *,
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
    user_id: str,
) -> List[Dict[str, Any]]:
    context = _build_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )
    has_live_context = any(
        row.get("live_status") is not None
        or _int_value(row.get("live_filled_qty")) != 0
        or _int_value(row.get("live_remaining_qty")) != 0
        or row.get("account_current_shares") is not None
        for row in context.values()
    )
    enriched = []
    for item in items:
        symbol_context = context.get(_symbol_key(item.get("symbol"))) or {}
        original_current = _int_value(item.get("current_shares"))
        strategy_current = original_current + _int_value(symbol_context.get("live_signed_delta")) if has_live_context else original_current
        enriched.append(
            {
                **item,
                "plan_current_shares": original_current,
                "current_shares": strategy_current,
                "strategy_current_shares": strategy_current,
                "account_current_shares": symbol_context.get("account_current_shares"),
                "account_available_shares": symbol_context.get("account_available_shares"),
                "live_filled_qty": _int_value(symbol_context.get("live_filled_qty")),
                "live_remaining_qty": _int_value(symbol_context.get("live_remaining_qty")),
                "live_status": symbol_context.get("live_status"),
            }
        )
    return enriched


def _stamp_sell_settled_at(
    trading_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    phase_state: str,
) -> None:
    """Idempotently record when the plan's sell barrier first becomes satisfied.

    ``sell_settled_at`` is a non-drifting event fact (design doc D1); it is only
    written once and never rolled back. ``execution_phase_state`` itself stays a
    derived value and is not stored.
    """

    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        return
    if phase_state in {PLAN_PHASE_APPROVED_PUBLISHED, PLAN_PHASE_SELLING}:
        return
    if plan.get("sell_settled_at"):
        return
    now = datetime.utcnow()
    plan["sell_settled_at"] = now
    try:
        trading_collections["portfolio_trade_plans"].update_one(
            {"plan_id": plan_id, "user_id": user_id, "sell_settled_at": {"$exists": False}},
            {"$set": {"sell_settled_at": now}},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Failed to stamp sell_settled_at for plan %s: %s", plan_id, exc)


def _maybe_stamp_plan_executed_live(
    trading_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    active_signal_count: int,
    phase_state: str,
    has_live_fills: bool,
) -> bool:
    """Close out a live trade_plan once it has actually executed.

    Computed-then-persisted (same pattern as ``_stamp_sell_settled_at``): a
    published live plan sits at ``approved`` until its signals settle. When all
    signals are terminal (no active in-flight), the plan phase is ``done`` and at
    least one signal actually filled, flip ``approved -> executed_live`` so it no
    longer looks like it is awaiting execution. Only applies to
    ``record_kind == trade_plan``; mixed terminal outcomes (some filled, some
    cancelled) still count as executed_live — the per-signal breakdown lives in
    ``live_signal_status_summary``.

    The fill requirement is essential: a plan whose live signals were *all*
    cancelled/rejected/failed (zero fills) never executed, so it must NOT be
    marked executed_live just because every signal reached a terminal status.
    """
    if plan.get("record_kind") != RECORD_KIND_TRADE_PLAN:
        return False
    if str(plan.get("status") or "") != "approved":
        return False
    if active_signal_count > 0 or phase_state != PLAN_PHASE_DONE:
        return False
    if not has_live_fills:
        return False
    now = datetime.utcnow()
    plan_id = str(plan.get("plan_id") or "")
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id, "status": "approved"},
        {"$set": {"status": "executed_live", "live_executed_at": now}},
    )
    plan["status"] = "executed_live"
    plan["live_executed_at"] = now
    return True


def _maybe_revert_unfilled_executed_live(
    trading_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    has_live_fills: bool,
) -> bool:
    """Undo an invalid executed_live stamp when no live fill exists.

    ``executed_live`` must mean "at least one live signal filled". If a legacy
    bad stamp, manual edit, or transient bug left a plan at executed_live while
    there are no live signals / no fills, normalize it back to approved on read
    so the UI never presents an unexecuted plan as completed.
    """
    if str(plan.get("status") or "") != "executed_live":
        return False
    if has_live_fills:
        return False
    plan_id = str(plan.get("plan_id") or "")
    try:
        record_kind = classify_record(plan)
    except ValueError:
        record_kind = None
    if record_kind == RECORD_KIND_LEDGER_EVENT:
        trading_collections["portfolio_trade_plans"].update_one(
            {"plan_id": plan_id, "user_id": user_id, "status": "executed_live"},
            {
                "$set": {
                    "status": "filled",
                    "record_kind": RECORD_KIND_LEDGER_EVENT,
                    "plan_type": "manual",
                    "origin": "manual",
                    "manual_action": plan.get("manual_action") or "external_manual",
                },
                "$unset": {"live_executed_at": ""},
            },
        )
        plan["status"] = "filled"
        plan["record_kind"] = RECORD_KIND_LEDGER_EVENT
        plan["plan_type"] = "manual"
        plan["origin"] = "manual"
        plan["manual_action"] = plan.get("manual_action") or "external_manual"
        plan.pop("live_executed_at", None)
        return True
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id, "status": "executed_live"},
        {"$set": {"status": "approved"}, "$unset": {"live_executed_at": ""}},
    )
    plan["status"] = "approved"
    plan.pop("live_executed_at", None)
    return True


def _plan_execution_context(finance_collections: Dict[str, Any], plan: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Single source of truth for how a plan is being executed.

    Returns an ``execution_mode`` of ``paper``, ``live`` or ``not_executed``
    so the UI never has to infer the layout from scattered fields.
    """
    plan_id = str(plan.get("plan_id") or "")
    trading_collections = get_trading_db_collections()
    paper_execution = (
        trading_collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id})
        if plan_id
        else None
    )
    has_paper_state = bool(plan.get("status") in {"executed_paper", "partially_executed"} or plan.get("paper_executed_at") or paper_execution)
    live_signals = (
        list(
            finance_collections["trade_signals"].find(
                {"user_id": user_id, "plan_id": plan_id, "mode": "live"},
                {
                    "_id": 0,
                    "execution_phase": 1,
                    "action": 1,
                    "status": 1,
                    "mode": 1,
                    "filled_qty": 1,
                    "filled_size": 1,
                    "size": 1,
                },
            )
        )
        if plan_id
        else []
    )
    signal_count = len(live_signals)
    has_live_fills = any(_signal_filled_qty(signal) > 0 for signal in live_signals)
    _maybe_revert_unfilled_executed_live(
        trading_collections,
        plan,
        user_id,
        has_live_fills=has_live_fills,
    )
    if signal_count > 0:
        status_summary = summarize_signal_statuses(live_signals)
        active_signal_count = sum(
            1 for signal in live_signals if str(signal.get("status") or "").lower() in ACTIVE_SIGNAL_STATUSES
        )
        phase_state = derive_execution_phase_state(live_signals)
        _stamp_sell_settled_at(trading_collections, plan, user_id, phase_state)
        _maybe_stamp_plan_executed_live(
            trading_collections,
            plan,
            user_id,
            active_signal_count=active_signal_count,
            phase_state=phase_state,
            has_live_fills=has_live_fills,
        )
        return {
            "execution_mode": "live",
            "has_live_signals": True,
            "signal_count": signal_count,
            "active_signal_count": active_signal_count,
            "status_summary": status_summary,
            "has_paper_conflict": has_paper_state,
            "execution_phase_state": phase_state,
        }
    if has_paper_state:
        return {
            "execution_mode": "paper",
            "has_live_signals": False,
            "signal_count": 0,
            "active_signal_count": 0,
            "status_summary": {},
            "has_paper_conflict": False,
        }
    return {
        "execution_mode": "not_executed",
        "has_live_signals": False,
        "signal_count": 0,
        "active_signal_count": 0,
        "status_summary": {},
        "has_paper_conflict": False,
    }


def _latest_account_snapshot(finance_collections: Dict[str, Any], account: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return finance_collections["trader_accounts"].find_one(
        {"user_id": user_id, "securities_account_id": str(account.get("_id"))},
        sort=[("synced_at", -1)],
    ) or {}


def _position_snapshot(finance_collections: Dict[str, Any], signal: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return finance_collections["trader_positions"].find_one(
        {
            "user_id": user_id,
            "securities_account_id": signal.get("securities_account_id"),
            "symbol": signal.get("symbol"),
        },
        sort=[("synced_at", -1)],
    ) or {}


def _position_snapshots_by_symbol(
    finance_collections: Dict[str, Any],
    *,
    signals: List[Dict[str, Any]],
    user_id: str,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Load trader_positions for all risk-check signals in one query."""
    account_ids = {
        str(signal.get("securities_account_id") or "")
        for signal in signals
        if signal.get("securities_account_id")
    }
    symbols = {str(signal.get("symbol") or "") for signal in signals if signal.get("symbol")}
    if not account_ids or not symbols:
        return {}

    rows = list(
        finance_collections["trader_positions"].find(
            {
                "user_id": user_id,
                "securities_account_id": {"$in": list(account_ids)},
                "symbol": {"$in": list(symbols)},
            }
        ).sort("synced_at", -1)
    )
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("securities_account_id") or ""), str(row.get("symbol") or ""))
        out.setdefault(key, row)
    return out


def _live_sell_sizing_basis() -> str:
    basis = str(os.getenv("LIVE_SELL_SIZING_BASIS", "broker") or "broker").strip().lower()
    return "lineage" if basis == "lineage" else "broker"


def _lineage_positions_by_symbol(
    trading_collections: Optional[Dict[str, Any]],
    *,
    plan: Optional[Dict[str, Any]],
    user_id: str,
) -> Tuple[Dict[str, int], bool]:
    """Return this lineage's structural shares by symbol from the live snapshot."""

    if not trading_collections or not plan:
        return {}, False
    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id or not params_hash:
        return {}, False
    try:
        snapshot = get_live_snapshot(
            trading_collections,
            user_id=user_id,
            strategy_template_id=strategy_template_id,
            params_hash=params_hash,
        )
    except Exception:  # noqa: BLE001 - fall back to broker checks on snapshot read issues.
        return {}, False
    if not snapshot:
        return {}, False
    positions: Dict[str, int] = {}
    for row in snapshot.get("positions") or []:
        key = _symbol_key(row.get("symbol"))
        if key:
            positions[key] = positions.get(key, 0) + max(0, _int_value(row.get("shares")))
    return positions, True


def _daily_live_signal_amount(finance_collections: Dict[str, Any], account: Dict[str, Any], user_id: str) -> float:
    today = _business_today_date()
    today_prefix = today.isoformat()
    rows = finance_collections["trade_signals"].find(
        {"user_id": user_id, "securities_account_id": str(account.get("_id")), "mode": "live"}
    )
    total = 0.0
    for row in rows:
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            is_today = _datetime_as_beijing_date(created_at) == today
        else:
            is_today = str(created_at or "").startswith(today_prefix)
        if is_today and str(row.get("status") or "") not in {"cancelled", "canceled", "rejected", "failed"}:
            total += abs(int(row.get("size", 0) or 0)) * _numeric(row.get("price"))
    return total


def _load_stock_guard_docs(data_collections: Dict[str, Any], symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    symbol_set = {str(symbol) for symbol in symbols if symbol}
    base_symbol_set = {symbol.split(".", 1)[0] for symbol in symbol_set}
    lookup_symbols = list(symbol_set | base_symbol_set)
    stock_info_rows = data_collections["stock_info"].find({"symbol": {"$in": lookup_symbols}})
    realtime_rows = data_collections["volume_price_realtime_latest"].find({"symbol": {"$in": lookup_symbols}})
    docs = {row.get("symbol"): dict(row) for row in stock_info_rows if row.get("symbol")}
    for row in realtime_rows:
        symbol = row.get("symbol")
        if symbol:
            docs.setdefault(symbol, {}).update(row)
    for symbol in symbol_set:
        base_symbol = symbol.split(".", 1)[0]
        if symbol not in docs and base_symbol in docs:
            docs[symbol] = docs[base_symbol]
    return docs


# Live publish hard blocks for extreme single-stock volatility (annualized fraction).
VOL_LIVE_BLOCK_ANNUAL = 1.0
VOL_LIVE_BLOCK_ATR_PCT = 0.15


def _load_volatility_features_by_symbol(
    data_collections: Dict[str, Any],
    symbols: List[str],
    base_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    try:
        from stock_data_access.indicators.volatility import (  # pyright: ignore[reportMissingImports]
            compute_volatility_features,
        )
    except ImportError:
        return {}

    if not symbols:
        return {}
    as_of = (base_date or _business_today_yyyymmdd()).replace("-", "")[:8]
    coll = data_collections["volume_price"]
    symbol_set = {str(symbol) for symbol in symbols if symbol}
    if not symbol_set:
        return {}

    try:
        rows = list(
            coll.find(
                {"symbol": {"$in": list(symbol_set)}, "trade_date": {"$lte": as_of}},
                {"_id": 0, "symbol": 1, "trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1},
            ).sort([("symbol", 1), ("trade_date", 1)])
        )
    except Exception:
        return {}

    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol in symbol_set:
            rows_by_symbol[symbol].append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for symbol, symbol_rows in rows_by_symbol.items():
        try:
            window = sorted(symbol_rows, key=lambda row: str(row.get("trade_date") or ""))[-320:]
            if window:
                out[symbol] = compute_volatility_features(window)
        except Exception:
            continue
    return out


def _volatility_buy_blockers(features: Optional[Dict[str, Any]]) -> List[str]:
    if not features:
        return []
    blockers: List[str] = []
    vol_20 = features.get("vol_20d_annual")
    atr_pct_20 = features.get("atr_pct_20d")
    if isinstance(vol_20, (int, float)) and float(vol_20) >= VOL_LIVE_BLOCK_ANNUAL:
        blockers.append("extreme_volatility")
    if isinstance(atr_pct_20, (int, float)) and float(atr_pct_20) >= VOL_LIVE_BLOCK_ATR_PCT:
        blockers.append("extreme_atr_volatility")
    if features.get("abnormal_range_flag"):
        blockers.append("abnormal_intraday_range")
    return blockers


def _buy_blockers_from_market_doc(signal: Dict[str, Any], market_doc: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    price = _numeric(signal.get("price"))
    if price <= 0:
        blockers.append("missing_price")
    name = str(market_doc.get("name") or market_doc.get("stock_name") or "")
    if market_doc.get("is_st") or "ST" in name.upper():
        blockers.append("st_stock")
    if market_doc.get("is_suspended") or market_doc.get("suspended") or market_doc.get("paused"):
        blockers.append("suspended")
    limit_up = _numeric(market_doc.get("limit_up") or market_doc.get("up_limit"))
    last_price = _numeric(market_doc.get("close") or market_doc.get("last_price"))
    if limit_up > 0 and last_price >= limit_up:
        blockers.append("limit_up")
    return blockers


def _attach_signal_names(data_collections: Dict[str, Any], signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    missing = [signal for signal in signals if signal.get("symbol") and not (signal.get("name") or signal.get("stock_name"))]
    if not missing:
        return signals
    market_docs = _load_stock_guard_docs(data_collections, [signal["symbol"] for signal in missing])
    for signal in missing:
        market_doc = market_docs.get(signal.get("symbol")) or {}
        name = market_doc.get("name") or market_doc.get("stock_name")
        if name:
            signal["name"] = name
    return signals


@router.get("/strategies")
@_offload
def list_portfolio_strategies(
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _ensure_default_strategy_templates(collections)
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    rows = list(collections["portfolio_strategy_configs"].find(query).sort("strategy_template_id", 1))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.get("/parameter-presets")
@_offload
def list_portfolio_parameter_presets(
    strategy_template_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _ensure_default_strategy_templates(collections)
    user_id = _current_user_id(current_user)
    query: Dict[str, Any] = {"$or": [{"user_id": user_id}, {"user_id": None}]}
    if strategy_template_id:
        query["strategy_template_id"] = strategy_template_id
    if status:
        query["status"] = status
    else:
        query["status"] = {"$in": ["draft", "enabled", "recommended"]}
    rows = [_with_preset_evidence_summary(row) for row in collections["portfolio_strategy_param_presets"].find(query)]
    rows.sort(key=lambda row: (0 if row.get("is_default") else 1, int(row.get("priority") or 9999), str(row.get("created_at") or "")))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.post("/parameter-presets")
async def create_portfolio_parameter_preset(request: ParamPresetRequest, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    _require_strategy(collections, request.strategy_template_id)
    _validate_params_override(request.params)
    now = datetime.utcnow()
    doc = {
        "preset_id": str(uuid.uuid4()),
        "strategy_template_id": request.strategy_template_id,
        "user_id": user_id,
        "name": request.name,
        "description": request.description,
        "params": request.params,
        "status": request.status,
        "is_default": request.is_default,
        "priority": request.priority,
        "source": "manual",
        "backtest_summary": request.backtest_summary,
        "data_window": request.data_window,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_strategy_param_presets"].insert_one(doc)
    return {"success": True, "data": _jsonable(doc)}


@router.get("/plans")
@_offload
def list_portfolio_plans(
    strategy_template_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    plan_type: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"user_id": _current_user_id(current_user)}
    if strategy_template_id:
        query["strategy_template_id"] = strategy_template_id
    if status:
        query["status"] = status
    if plan_type:
        query["plan_type"] = plan_type
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["base_date"] = date_query
    rows = list(collections["portfolio_trade_plans"].find(query, PLAN_LIST_PROJECTION).sort("base_date", -1).limit(limit))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.post("/plans/generate")
async def generate_portfolio_plan(request: GeneratePlanRequest, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    strategy = _require_strategy(collections, request.strategy_template_id)
    if strategy.get("status") == "disabled":
        raise HTTPException(status_code=400, detail="strategy is disabled")
    params_override = dict(request.params_override or {})
    if request.preset_id:
        preset = _require_param_preset(collections, request.preset_id, user_id)
        if preset.get("strategy_template_id") != request.strategy_template_id:
            raise HTTPException(status_code=400, detail="parameter preset does not belong to selected strategy")
        params_override = {**dict(preset.get("params") or {}), **params_override}
    _validate_params_override(params_override)
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "generate_portfolio_plan",
        "user_id": user_id,
        "strategy_template_id": request.strategy_template_id,
        "base_date": request.base_date,
        "mode": request.mode,
        "force": request.force,
        "preset_id": request.preset_id,
        "params_override": params_override,
        "status": "pending",
        "attempts": 0,
        "plan_id": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {"success": True, "data": _jsonable(task)}


@router.post("/plans/manual-rebalance")
async def create_manual_rebalance_plan(request: ManualRebalanceRequest, current_user: dict = Depends(get_current_user)):
    user_id = _current_user_id(current_user)
    collections = get_trading_db_collections()
    anchor = _require_plan(collections, request.anchor_plan_id, current_user)
    if not request.targets:
        raise HTTPException(status_code=400, detail="targets must include at least one symbol")
    # Heavy plan generation (capital baseline, pricing, lot-size, risk review) lives
    # in stock-scoring-system; enqueue a task for the plan-generation worker instead
    # of importing it into the API process.
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "generate_manual_rebalance_plan",
        "user_id": user_id,
        "strategy_template_id": anchor.get("strategy_template_id"),
        "params_hash": anchor.get("params_hash"),
        "base_date": request.base_date or anchor.get("base_date"),
        "anchor_plan_id": request.anchor_plan_id,
        "targets": {str(symbol): int(shares) for symbol, shares in request.targets.items()},
        "exclude_after": bool(request.exclude_after),
        "operation_type": "manual_rebalance",
        "status": "pending",
        "attempts": 0,
        "plan_id": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {"success": True, "data": _jsonable(task)}


@router.post("/plans/{plan_id}/force-rebalance")
async def force_rebalance_portfolio_plan(
    plan_id: str,
    request: ForceRebalanceRequest = ForceRebalanceRequest(),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue an off-cycle strategy rebalance plan (slow path: worker generates lineup)."""
    user_id = _current_user_id(current_user)
    collections = get_trading_db_collections()
    anchor = _require_plan(collections, plan_id, current_user)
    strategy_template_id = str(anchor.get("strategy_template_id") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")
    strategy = _require_strategy(collections, strategy_template_id)
    if strategy.get("status") == "disabled":
        raise HTTPException(status_code=400, detail="strategy is disabled")
    open_plan = _lineage_open_trade_plan(collections, anchor, user_id)
    if open_plan:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "lineage already has a pending or approved trade plan",
                "plan_id": open_plan.get("plan_id"),
                "status": open_plan.get("status"),
                "base_date": open_plan.get("base_date"),
            },
        )

    params_override = _normalized_plan_params_from_snapshot(anchor.get("params_snapshot") or {})
    if anchor.get("params_override"):
        params_override = {**params_override, **dict(anchor.get("params_override") or {})}
        params_override = _normalized_plan_params_from_snapshot(params_override)
    _validate_params_override(params_override)

    base_date = _normalize_yyyymmdd(request.base_date) or _business_today_yyyymmdd()
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "generate_portfolio_plan",
        "user_id": user_id,
        "strategy_template_id": strategy_template_id,
        "base_date": base_date,
        "mode": "rebalance",
        "force": True,
        "preset_id": anchor.get("preset_id"),
        "params_override": params_override,
        "status": "pending",
        "attempts": 0,
        "plan_id": None,
        "error_message": None,
        "anchor_plan_id": plan_id,
        "operation_type": "force_rebalance",
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {
        "success": True,
        "data": _jsonable(
            {
                **task,
                "params_hash": anchor.get("params_hash"),
            }
        ),
    }


# Config-integrity blockers that still abort a discretionary live action: there is
# no point publishing orders the broker can never accept. For SELLS, only these
# hard blockers abort — market/position blockers (limit-down, position mismatch,
# daily-amount caps) are reported but allowed, because a discretionary exit must
# reach the broker and let the market decide whether it fills. BUYS additionally
# respect their own blockers (insufficient cash, limit-up, position caps): adding
# an unfundable/over-limit position is surfaced immediately rather than published.
def _account_id_from_filled_signals(
    signals: List[Dict[str, Any]], executions: List[Dict[str, Any]]
) -> str:
    """Securities account tied to the signals that actually received fills."""
    filled_order_ids = {
        str(row.get("order_id"))
        for row in executions
        if row.get("order_id") and _execution_fill_quantity(row) > 0
    }
    for signal in reversed(signals):
        order_id = str(signal.get("order_id") or "")
        if order_id not in filled_order_ids:
            continue
        account_id = str(signal.get("securities_account_id") or "")
        if account_id:
            return account_id
    return ""


def _holdings_from_executions(
    executions: List[Dict[str, Any]], data_collections: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Net live positions (shares > 0) rebuilt from raw executions."""
    grouped = group_executions_by_order(executions)
    symbols = sorted({str(row.get("symbol")) for row in grouped if row.get("symbol")})
    if not symbols:
        return []
    prices, realtime_docs = _load_fresh_market_prices(data_collections, symbols)
    payload = build_lineage_live_positions(grouped, prices, realtime_docs=realtime_docs)
    return [row for row in (payload.get("positions") or []) if _int_value(row.get("shares")) > 0]


def _lineage_live_account_and_holdings(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """One pass over the lineage's live signals/executions: account id + holdings.

    Both the broker account id and the ledger holdings derive from the same
    signal/execution bundle, so callers that need both (e.g. reconciliation) get
    them in a single fetch instead of scanning the lineage twice.
    """
    _ids, signals, executions = _lineage_live_signal_execution_bundle(
        trading_collections, finance_collections, plan, user_id
    )
    account_id = _account_id_from_filled_signals(signals, executions)
    holdings = _holdings_from_executions(executions, data_collections)
    return account_id, holdings


def _lineage_live_holdings(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> List[Dict[str, Any]]:
    """Current live holdings (shares > 0) for the plan's params_hash lineage."""
    return _lineage_live_account_and_holdings(
        trading_collections, finance_collections, data_collections, plan, user_id
    )[1]


def _account_position_qty(pos: Dict[str, Any]) -> int:
    for field in ("qty", "quantity", "volume", "shares", "current_volume", "total_volume"):
        if pos.get(field) is not None:
            return _int_value(pos.get(field))
    return 0


def _account_available_qty(pos: Dict[str, Any]) -> int:
    for field in ("can_use_volume", "available_qty", "available_shares", "available_volume"):
        if pos.get(field) is not None:
            return _int_value(pos.get(field))
    return _account_position_qty(pos)


def _account_positions_by_symbol(
    finance_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
) -> Tuple[Dict[str, Dict[str, int]], bool]:
    """Return broker-synced current/available shares by normalized symbol key.

    The boolean says whether the account has any synced position rows at all.
    When true, a missing symbol should be treated as zero for sell protection.
    When false, the account has not synced yet, so callers should avoid forcing
    zero and fall back to the system ledger.
    """
    rows = list(
        finance_collections["trader_positions"].find(
            {"user_id": user_id, "securities_account_id": securities_account_id},
            {
                "_id": 0,
                "symbol": 1,
                "qty": 1,
                "quantity": 1,
                "volume": 1,
                "shares": 1,
                "current_volume": 1,
                "total_volume": 1,
                "can_use_volume": 1,
                "available_qty": 1,
                "available_shares": 1,
                "available_volume": 1,
            },
        )
    )
    positions: Dict[str, Dict[str, int]] = {}
    for row in rows:
        key = _symbol_key(row.get("symbol"))
        if not key:
            continue
        bucket = positions.setdefault(key, {"current": 0, "available": 0})
        bucket["current"] += max(0, _account_position_qty(row))
        bucket["available"] += max(0, _account_available_qty(row))
    return positions, bool(rows)


def _current_lineage_holdings(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> List[Dict[str, Any]]:
    """Current real holdings (shares > 0) for the lineage.

    Uses the lineage's execution venue as the source of truth. A live lineage
    with no current broker-ledger holdings should show an empty lineup; it must
    not fall back to historical paper snapshots.
    """
    try:
        if _portfolio_execution_venue(trading_collections, finance_collections, plan, user_id) == "live":
            # Serve live holdings from the materialized snapshot (build-on-miss)
            # instead of replaying the whole lineage signals/executions bundle.
            _snapshot, overlay = _load_snapshot_with_realtime(
                trading_collections, finance_collections, data_collections, plan, user_id
            )
            return [
                row for row in (overlay.get("positions") or []) if _int_value(row.get("shares")) > 0
            ]
        snapshot = _latest_lineage_paper_snapshot(trading_collections, plan, user_id)
        return [
            row
            for row in (snapshot.get("positions") or [])
            if _int_value(row.get("shares")) > 0
        ]
    except Exception:
        return []


@router.post("/plans/{plan_id}/external-manual-record")
async def record_external_manual_record(
    plan_id: str,
    request: ExternalManualRecordRequest,
    current_user: dict = Depends(get_current_user),
):
    """Record broker-side manual fills (e.g. miniQMT direct operation) into the
    plan lineage book.

    This does not publish executable orders; it writes already-filled buy/sell
    signals + executions so the live lineage ledger reflects what was done in
    miniQMT. Supports partial reduce / add as well as a full liquidation; the
    resulting holdings are simply current + manual buys - manual sells. The
    lineage is paused only when ``pause_lineage`` is set and the resulting book
    becomes empty.
    """
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)

    if not request.fills:
        raise HTTPException(status_code=400, detail="fills must include at least one fill")

    plan = _require_plan(trading_collections, plan_id, current_user)
    account = _require_securities_account(finance_collections, request.securities_account_id, user_id)

    if request.external_batch_id:
        existing = trading_collections["portfolio_trade_plans"].find_one(
            {
                "user_id": user_id,
                "anchor_plan_id": plan_id,
                "manual_action": "external_manual",
                "external_batch_id": request.external_batch_id,
            }
        )
        if existing:
            return {
                "success": True,
                "data": _jsonable(
                    {
                        "plan_id": existing.get("plan_id"),
                        "external_batch_id": request.external_batch_id,
                        "already_recorded": True,
                        "inserted_signal_count": 0,
                        "inserted_execution_count": 0,
                    }
                ),
            }

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    holdings = _lineage_live_holdings(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    holdings_by_key = {_symbol_key(row.get("symbol")): row for row in holdings}

    # Aggregate signed deltas per symbol to validate sells and to build a
    # per-symbol audit view + the resulting holdings used for lineage effects.
    delta_by_key: Dict[str, int] = defaultdict(int)
    name_by_key: Dict[str, str] = {}
    symbol_by_key: Dict[str, str] = {}
    for fill in request.fills:
        key = _symbol_key(fill.symbol)
        signed = int(fill.filled_size) if fill.action == "buy" else -int(fill.filled_size)
        delta_by_key[key] += signed
        if fill.name:
            name_by_key.setdefault(key, fill.name)
        symbol_by_key.setdefault(key, str(fill.symbol))

    oversells = []
    for key, delta in delta_by_key.items():
        current_shares = _int_value((holdings_by_key.get(key) or {}).get("shares"))
        if current_shares + delta < 0:
            oversells.append(
                {
                    "symbol": (holdings_by_key.get(key) or {}).get("symbol") or symbol_by_key.get(key),
                    "current_shares": current_shares,
                    "net_delta_shares": delta,
                }
            )
    if oversells:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "manual sells exceed current lineage holdings",
                "oversells": oversells,
            },
        )

    now = datetime.utcnow()
    base_date = _business_today_yyyymmdd()
    stamp = now.strftime("%Y%m%d%H%M%S")
    new_plan_id = f"{strategy_template_id}_{(params_hash or 'na')[:8]}_{user_id}_extman_{stamp}{uuid.uuid4().hex[:6]}"

    # Resulting holdings over the union of held + touched symbols (untouched
    # holdings keep their current shares, so a full liquidation is detected
    # correctly only when every held symbol nets to zero).
    final_by_key: Dict[str, int] = {
        key: _int_value(row.get("shares")) for key, row in holdings_by_key.items()
    }
    for key, delta in delta_by_key.items():
        final_by_key[key] = final_by_key.get(key, 0) + delta

    final_positions = [
        {
            "symbol": str((holdings_by_key.get(key) or {}).get("symbol") or symbol_by_key.get(key) or key),
            "shares": shares,
        }
        for key, shares in sorted(final_by_key.items())
    ]
    cleared_symbols = sorted(
        str((holdings_by_key.get(key) or {}).get("symbol") or symbol_by_key.get(key) or key)
        for key, delta in delta_by_key.items()
        if delta < 0 and final_by_key.get(key, 0) == 0
    )
    is_liquidation = bool(final_by_key) and all(shares == 0 for shares in final_by_key.values())

    items: List[Dict[str, Any]] = []
    for rank, key in enumerate(sorted(delta_by_key.keys()), start=1):
        delta = delta_by_key[key]
        if delta == 0:
            continue
        held = holdings_by_key.get(key) or {}
        symbol = held.get("symbol") or symbol_by_key.get(key) or key
        current_shares = _int_value(held.get("shares"))
        items.append(
            {
                "plan_id": new_plan_id,
                "strategy_template_id": strategy_template_id,
                "user_id": user_id,
                "params_hash": params_hash,
                "base_date": base_date,
                "execute_date": base_date,
                "rank": rank,
                "symbol": symbol,
                "name": held.get("name") or name_by_key.get(key, ""),
                "action": "buy" if delta > 0 else "sell",
                "current_shares": current_shares,
                "target_shares": current_shares + delta,
                "delta_shares": delta,
                "drift_delta_shares": delta,
                "estimated_price": held.get("last_price"),
                "estimated_amount": abs(delta) * _numeric(held.get("last_price")),
                "blockers": [],
                "created_at": now,
            }
        )

    changed_symbols = sorted({str(it["symbol"]) for it in items})
    estimated_buy_amount = sum(
        float(f.filled_size) * float(f.filled_price) for f in request.fills if f.action == "buy"
    )
    estimated_sell_amount = sum(
        float(f.filled_size) * float(f.filled_price) for f in request.fills if f.action == "sell"
    )
    manual_action = "external_liquidation" if is_liquidation else "external_manual"

    plan_doc = {
        "plan_id": new_plan_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "preset_id": plan.get("preset_id"),
        "params_hash": params_hash,
        "params_snapshot": plan.get("params_snapshot") or {},
        "params_override": plan.get("params_override") or {},
        "strategy_name": plan.get("strategy_name"),
        "base_date": base_date,
        "execute_date": base_date,
        "plan_type": "manual",
        "record_kind": RECORD_KIND_LEDGER_EVENT,
        "origin": "manual",
        # manual_action stays "external_manual" for idempotency/query keying;
        # is_liquidation flags the full-clear case for downstream display.
        "manual_action": "external_manual",
        "manual_action_detail": manual_action,
        "is_liquidation": is_liquidation,
        "is_rebalance_day": False,
        "monitor_can_trade": False,
        "trading_enabled": False,
        "executable": False,
        "non_executable_reason": "external_manual_fills_already_completed",
        "exclude_after": bool(request.exclude_after),
        "manual_exclude_symbols": cleared_symbols if request.exclude_after else [],
        "status": "filled",
        "approved_by": user_id,
        "approved_at": now,
        "anchor_plan_id": plan_id,
        "securities_account_id": str(account.get("_id") or request.securities_account_id),
        "account_id": account.get("account_id"),
        "broker": account.get("broker"),
        "external_batch_id": request.external_batch_id,
        "external_reason": request.reason,
        "summary": {
            "buy_count": sum(1 for it in items if it["action"] == "buy"),
            "sell_count": sum(1 for it in items if it["action"] == "sell"),
            "hold_count": 0,
            "skip_count": 0,
            "estimated_buy_amount": estimated_buy_amount,
            "estimated_sell_amount": estimated_sell_amount,
            "manual_action": manual_action,
        },
        "created_at": now,
        "updated_at": now,
        "live_signals_published_at": now,
        "live_signal_count": len(request.fills),
        "live_signal_inserted_count": len(request.fills),
        "live_signal_status_summary": {"filled": len(request.fills)},
    }

    signals: List[Dict[str, Any]] = []
    executions: List[Dict[str, Any]] = []
    for idx, fill in enumerate(request.fills, start=1):
        key = _symbol_key(fill.symbol)
        held = holdings_by_key.get(key) or {}
        ts = float(fill.timestamp) if fill.timestamp is not None else now.timestamp()
        dt = datetime.fromtimestamp(ts)
        order_id = f"external-manual-{new_plan_id}-{idx:03d}"
        broker_order_id = fill.broker_order_id or order_id
        fee = fill.total_fee if fill.total_fee is not None else fill.commission
        symbol = str(held.get("symbol") or fill.symbol)
        name = fill.name or held.get("name") or ""
        action = fill.action
        side = action.upper()
        signal = {
            "order_id": order_id,
            "user_id": user_id,
            "securities_account_id": str(account.get("_id") or request.securities_account_id),
            "account_id": str(account.get("account_id") or ""),
            "broker": str(account.get("broker") or ""),
            "mode": "live",
            "is_executable": False,
            "source": "external_manual_record",
            "plan_id": new_plan_id,
            "strategy_template_id": strategy_template_id,
            "symbol": symbol,
            "name": name,
            "action": action,
            "size": int(fill.filled_size),
            "price": float(fill.filled_price),
            "order_type": "external_manual",
            "execution_phase": action,
            "execution_priority": (-10000 if action == "sell" else 10000) + idx,
            "plan_item_rank": idx,
            "reference_price": float(fill.filled_price),
            "reference_price_source": "external_manual_fill",
            "effective_limit_price": float(fill.filled_price),
            "status": "filled",
            "created_at": now,
            "updated_at": ts,
            "submitted_at": ts,
            "executed_at": ts,
            "timestamp": now.timestamp(),
            "datetime": now,
            "filled_qty": int(fill.filled_size),
            "avg_price": float(fill.filled_price),
        }
        execution = {
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "qmt_order_id": broker_order_id,
            "user_id": user_id,
            "plan_id": new_plan_id,
            "strategy_template_id": strategy_template_id,
            "source": "external_manual_record",
            "securities_account_id": str(account.get("_id") or request.securities_account_id),
            "account_id": str(account.get("account_id") or ""),
            "broker": str(account.get("broker") or ""),
            "mode": "live",
            "symbol": symbol,
            "name": name,
            "side": side,
            "action": side,
            "status": "filled",
            "quantity": int(fill.filled_size),
            "filled_size": int(fill.filled_size),
            "filled_qty": int(fill.filled_size),
            "filled_price": float(fill.filled_price),
            "avg_price": float(fill.filled_price),
            "commission": float(fill.commission),
            "total_fee": float(fee or 0.0),
            "timestamp": ts,
            "date": dt.strftime("%Y%m%d"),
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "external_batch_id": request.external_batch_id,
            "external_reason": request.reason,
        }
        signals.append(signal)
        executions.append(execution)

    trading_collections["portfolio_trade_plans"].insert_one(dict(plan_doc))
    if items:
        trading_collections["portfolio_trade_plan_items"].insert_many([dict(item) for item in items])
    finance_collections["trade_signals"].insert_many(signals)
    finance_collections["trade_executions"].insert_many(executions)

    lineage_effects = _apply_manual_lineage_effects_native(
        trading_collections,
        plan_doc,
        final_positions=final_positions,
        acted_by=user_id,
        pause_on_empty=bool(request.pause_lineage),
    )

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": new_plan_id,
                "anchor_plan_id": plan_id,
                "manual_action": manual_action,
                "is_liquidation": is_liquidation,
                "changed_symbols": changed_symbols,
                "cleared_symbols": cleared_symbols,
                "final_positions": [p for p in final_positions if p["shares"] > 0],
                "inserted_signal_count": len(signals),
                "inserted_execution_count": len(executions),
                "signals": signals,
                "executions": executions,
                "lineage_effects": lineage_effects,
                "already_recorded": False,
            }
        ),
    }


@router.post("/plans/{plan_id}/holdings-risk")
async def enqueue_portfolio_holdings_risk(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Enqueue a holdings-level AI risk review for the worker.

    Position sourcing (paper snapshot or live fills) stays in the API; the actual
    rule evaluation (ai_risk_review) runs in the plan-generation worker and writes
    its result onto the task, which the client polls.
    """
    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    snapshot = _latest_lineage_paper_snapshot(collections, plan, user_id)
    positions = [row for row in (snapshot.get("positions") or []) if _int_value(row.get("shares")) > 0]
    base_date = _normalize_yyyymmdd(snapshot.get("as_of_date")) or _normalize_yyyymmdd(plan.get("base_date"))
    if not positions:
        # Serve live holdings from the materialized snapshot (build-on-miss)
        # instead of replaying the lineage signals/executions bundle here.
        _snapshot, overlay = _load_snapshot_with_realtime(
            collections, finance_collections, data_collections, plan, user_id
        )
        positions = [
            row for row in (overlay.get("positions") or []) if _int_value(row.get("shares")) > 0
        ]
    if not base_date:
        base_date = _business_today_yyyymmdd()
    control = _get_lineage_control_doc(
        collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    review_positions = [
        {"symbol": row.get("symbol"), "shares": _int_value(row.get("shares")), "name": row.get("name", "")}
        for row in positions
    ]
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "holdings_risk_review",
        "user_id": user_id,
        "plan_id": plan_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "params_hash": plan.get("params_hash"),
        "base_date": base_date,
        "positions": review_positions,
        "status": "pending",
        "attempts": 0,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {
        "success": True,
        "data": _jsonable(
            {
                "task_id": task["task_id"],
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "base_date": base_date,
                "position_count": len(review_positions),
                "paused": bool(control.get("paused")),
                "excluded_symbols": control.get("excluded_symbols") or [],
            }
        ),
    }


@router.post("/plans/{plan_id}/resume-lineage")
async def resume_portfolio_lineage(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    control = _resume_lineage_native(
        collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
        resumed_by=user_id,
    )
    return {"success": True, "data": _jsonable(control)}


PLAN_RESELECT_STATUSES = {"draft", "generated", "needs_review"}


def _enqueue_plan_regeneration(
    collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    excluded_symbols: List[str],
    *,
    operation_type: str,
    operation_symbol: Optional[str] = None,
    operation_symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Re-run plan generation in place with an updated exclusion set.

    Reuses the plan's own base_date / preset / params so params_hash (hence
    plan_id) is unchanged and the worker replaces the same plan. The exclusion
    set is threaded separately and intentionally does not affect params_hash.
    """
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "generate_portfolio_plan",
        "user_id": user_id,
        "strategy_template_id": plan["strategy_template_id"],
        "base_date": plan["base_date"],
        "mode": "auto",
        "force": True,
        "preset_id": plan.get("preset_id"),
        "params_override": plan.get("params_override") or {},
        "excluded_symbols": excluded_symbols,
        "operation_type": operation_type,
        "operation_symbol": operation_symbol,
        "status": "pending",
        "attempts": 0,
        "plan_id": plan["plan_id"],
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    # Reflect the intent immediately so the UI shows the pending exclusion before
    # the worker rewrites the plan.
    collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan["plan_id"], "user_id": user_id},
        {"$set": {"excluded_symbols": excluded_symbols, "updated_at": now}},
    )
    _append_plan_operation_log(
        collections,
        plan_id=plan["plan_id"],
        user_id=user_id,
        operation_type=operation_type,
        status="enqueued",
        message=f"operation enqueued: {operation_type}",
        task_id=task["task_id"],
        symbol=operation_symbol,
        payload={
            "excluded_symbols": excluded_symbols,
            "operation_symbols": operation_symbols or ([operation_symbol] if operation_symbol else []),
        },
    )
    return task


@router.post("/plans/{plan_id}/items/{symbol}/reject")
async def reject_portfolio_plan_item(plan_id: str, symbol: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    if plan.get("status") not in PLAN_RESELECT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"plan is {plan.get('status')}; can only reselect while in {sorted(PLAN_RESELECT_STATUSES)}",
        )
    excluded = sorted({*(plan.get("excluded_symbols") or []), str(symbol)})
    task = _enqueue_plan_regeneration(
        collections,
        plan,
        user_id,
        excluded,
        operation_type="reject_symbol",
        operation_symbol=str(symbol),
    )
    return {"success": True, "data": _jsonable({"task": task, "excluded_symbols": excluded})}


@router.post("/plans/{plan_id}/items/bulk-reject")
async def bulk_reject_portfolio_plan_items(
    plan_id: str,
    request: BulkRejectPlanItemsRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    if plan.get("status") not in PLAN_RESELECT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"plan is {plan.get('status')}; can only reselect while in {sorted(PLAN_RESELECT_STATUSES)}",
        )
    symbols = sorted({str(symbol).strip() for symbol in request.symbols if str(symbol).strip()})
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols is empty")

    excluded = sorted({*(plan.get("excluded_symbols") or []), *symbols})
    task = _enqueue_plan_regeneration(
        collections,
        plan,
        user_id,
        excluded,
        operation_type="bulk_reject_symbols",
        operation_symbols=symbols,
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "task": task,
                "excluded_symbols": excluded,
                "rejected_symbols": symbols,
            }
        ),
    }


@router.post("/plans/{plan_id}/items/{symbol}/restore")
async def restore_portfolio_plan_item(plan_id: str, symbol: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    if plan.get("status") not in PLAN_RESELECT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"plan is {plan.get('status')}; can only reselect while in {sorted(PLAN_RESELECT_STATUSES)}",
        )
    symbol = str(symbol)
    if symbol not in set(plan.get("excluded_symbols") or []):
        log = _append_plan_operation_log(
            collections,
            plan_id=plan_id,
            user_id=user_id,
            operation_type="restore_symbol",
            status="noop",
            message=f"symbol {symbol} is not in excluded list",
            symbol=symbol,
            payload={"excluded_symbols": plan.get("excluded_symbols") or []},
        )
        return {"success": True, "data": _jsonable({"task": None, "excluded_symbols": plan.get("excluded_symbols") or [], "operation_log": log})}
    excluded = sorted(s for s in (plan.get("excluded_symbols") or []) if s != str(symbol))
    task = _enqueue_plan_regeneration(
        collections,
        plan,
        user_id,
        excluded,
        operation_type="restore_symbol",
        operation_symbol=symbol,
    )
    return {"success": True, "data": _jsonable({"task": task, "excluded_symbols": excluded})}


@router.post("/plans/{plan_id}/ai-risk-review")
async def rerun_portfolio_plan_ai_risk(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Manually re-run the rule-based AI risk pass on an existing plan.

    Enqueues an ``ai_risk_review`` task processed by the plan worker, which
    recomputes ``ai_risk`` on each buy/hold item in place. Advisory only — it
    never changes selection or status.
    """
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "ai_risk_review",
        "user_id": user_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "base_date": plan.get("base_date"),
        "plan_id": plan_id,
        "operation_type": "ai_risk_review",
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    _append_plan_operation_log(
        collections,
        plan_id=plan_id,
        user_id=user_id,
        operation_type="ai_risk_review",
        status="enqueued",
        message="ai risk review enqueued",
        task_id=task["task_id"],
    )
    return {"success": True, "data": _jsonable({"task": task})}


@router.post("/plans/{plan_id}/llm-risk-review")
async def enqueue_portfolio_plan_llm_risk(
    plan_id: str,
    request: Optional[PortfolioLlmRiskReviewRequest] = None,
    current_user: dict = Depends(get_current_user),
):
    """Fan out one portfolio LLM risk review run by SW1 industry bucket."""
    request = request or PortfolioLlmRiskReviewRequest()
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    analysis_tasks = _analysis_tasks_collection()
    if analysis_tasks is None:
        raise HTTPException(status_code=503, detail="analysis task queue unavailable")
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)

    if not request.force:
        existing = analysis_tasks.find_one(
            _portfolio_llm_task_query(
                user_id=user_id,
                plan_id=plan_id,
                target_scope=request.target_scope,
                statuses=PORTFOLIO_RISK_LLM_RUNNING_STATUSES,
            ),
            sort=[("created_at", -1)],
        )
        if existing:
            existing_run_id = str((existing.get("params") or {}).get("run_id") or "")
            tasks = _portfolio_llm_run_tasks(
                user_id=user_id,
                plan_id=plan_id,
                run_id=existing_run_id,
                target_scope=request.target_scope,
            )
            return {
                "success": True,
                "data": _jsonable(
                    {
                        "run_id": existing_run_id,
                        "reused": True,
                        "task_ids": [str(task.get("_id")) for task in tasks if task.get("_id")],
                        "summary": _portfolio_llm_run_status(tasks),
                    }
                ),
            }

    if request.target_scope == "bench":
        finance_collections = get_db_collections()
        holdings = _current_lineage_holdings(
            collections, finance_collections, data_collections, plan, user_id
        )
        bench_payload = _build_plan_bench_payload(
            plan,
            holdings,
            bench_multiplier=request.bench_multiplier,
            data_collections=data_collections,
        )
        items = [
            {
                **row,
                "action": "bench",
                "target_weight": None,
            }
            for row in bench_payload.get("bench") or []
        ]
    else:
        owner_query = {"plan_id": plan_id, "user_id": user_id}
        items = list(collections["portfolio_trade_plan_items"].find(owner_query).sort("rank", 1))
        items = _enrich_plan_items_with_score_snapshot(
            data_collections=data_collections,
            plan=plan,
            items=items,
        )
    raw_actionable = _portfolio_llm_actionable_items(items, request)
    if not raw_actionable:
        detail = (
            "no bench candidates matched LLM risk review filters"
            if request.target_scope == "bench"
            else "no buy/hold plan items matched LLM risk review filters"
        )
        raise HTTPException(status_code=400, detail=detail)
    actionable = filter_items_for_llm_enqueue(raw_actionable, force=request.force, db=get_analyzer_db())
    if not actionable:
        raise HTTPException(
            status_code=400,
            detail="all matched symbols were reviewed recently; use force=true to re-run",
        )

    groups = _portfolio_llm_industry_groups(actionable)
    industry_context = _portfolio_llm_industry_context(groups)
    run_id = str(uuid.uuid4())
    base_date = (
        _normalize_yyyymmdd(plan.get("base_date"))
        or _normalize_yyyymmdd(plan.get("score_date"))
        or _business_today_yyyymmdd()
    )
    score_date = _normalize_yyyymmdd(plan.get("score_date")) or base_date
    task_ids: List[str] = []

    for industry in sorted(groups):
        industry_items = groups[industry]
        chunks = _chunked(industry_items, request.max_symbols_per_call)
        for chunk_index, chunk in enumerate(chunks):
            params = {
                "plan_id": plan_id,
                "run_id": run_id,
                "target_scope": request.target_scope,
                "base_date": base_date,
                "score_date": score_date,
                "industry": industry,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "emit_industry_scope": len(chunks) == 1,
                "items": [_portfolio_llm_item_payload(item) for item in chunk],
                "industry_context": industry_context,
                "options": {
                    "min_rules_severity": request.min_rules_severity,
                    "taxonomy_level": request.taxonomy_level,
                    "include_actions": request.include_actions,
                    "max_symbols_per_call": request.max_symbols_per_call,
                    "prompt_version": request.prompt_version,
                    "bench_multiplier": request.bench_multiplier,
                    "force": request.force,
                },
            }
            task_doc = build_task_doc(
                current_user=current_user,
                task_type=PORTFOLIO_RISK_LLM_TASK_TYPE,
                priority=PORTFOLIO_RISK_LLM_PRIORITY,
                params=params,
                source="portfolio_plan_llm_risk",
                extra_fields={
                    "plan_id": plan_id,
                    "operation_type": f"{request.target_scope}_llm_risk_review",
                    "idempotency_key": f"{plan_id}:{request.target_scope}:{run_id}:{industry}:{chunk_index}",
                },
            )
            result = analysis_tasks.insert_one(task_doc)
            inserted_id = getattr(result, "inserted_id", None)
            if inserted_id:
                task_ids.append(str(inserted_id))

    _append_plan_operation_log(
        collections,
        plan_id=plan_id,
        user_id=user_id,
        operation_type=f"{request.target_scope}_llm_risk_review",
        status="enqueued",
        message=f"portfolio {request.target_scope} llm risk review enqueued",
        payload={
            "run_id": run_id,
            "target_scope": request.target_scope,
            "task_count": sum(len(_chunked(rows, request.max_symbols_per_call)) for rows in groups.values()),
            "industry_context": industry_context,
        },
    )
    tasks = _portfolio_llm_run_tasks(
        user_id=user_id,
        plan_id=plan_id,
        run_id=run_id,
        target_scope=request.target_scope,
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "run_id": run_id,
                "target_scope": request.target_scope,
                "reused": False,
                "task_ids": task_ids,
                "summary": _portfolio_llm_run_status(tasks),
                "industry_context": industry_context,
            }
        ),
    }


@router.get("/plans/{plan_id}/llm-risk-review/runs/{run_id}")
@_offload
def get_portfolio_plan_llm_risk_run(
    plan_id: str,
    run_id: str,
    target_scope: str = Query(default="plan", pattern="^(plan|bench)$"),
    current_user: dict = Depends(get_current_user),
):
    """Aggregate quantAnalyzer analysis_tasks status for one LLM risk run."""
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    _require_plan(collections, plan_id, current_user)
    tasks = _portfolio_llm_run_tasks(
        user_id=user_id,
        plan_id=plan_id,
        run_id=run_id,
        target_scope=target_scope,
    )
    if not tasks:
        raise HTTPException(status_code=404, detail="llm risk review run not found")
    summary = _portfolio_llm_run_status(tasks)
    return {
        "success": True,
        "data": _jsonable(
            {
                "run_id": run_id,
                "target_scope": target_scope,
                **summary,
                "tasks": tasks,
            }
        ),
    }


@router.get("/plans/generation-tasks")
@_offload
def list_portfolio_plan_generation_tasks(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"user_id": _current_user_id(current_user)}
    if status:
        query["status"] = status
    rows = list(
        collections["portfolio_plan_generation_tasks"]
        .find(query, GENERATION_TASK_LIST_PROJECTION)
        .sort("created_at", -1)
        .limit(limit)
    )
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/generation-tasks/{task_id}")
@_offload
def get_portfolio_plan_generation_task(task_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    task = collections["portfolio_plan_generation_tasks"].find_one(
        {"task_id": task_id, "user_id": _current_user_id(current_user)}
    )
    if not task:
        raise HTTPException(status_code=404, detail="generation task not found")
    return {"success": True, "data": _jsonable(task)}


@router.get("/workers/status")
@_offload
def list_portfolio_worker_status(
    worker_type: Optional[str] = Query(default="portfolio_plan_generator"),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if worker_type:
        query["worker_type"] = worker_type
    rows = list(collections["portfolio_worker_status"].find(query).sort("last_seen_at", -1).limit(limit))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/data-watermarks/plan-generation")
@_offload
def get_portfolio_plan_generation_watermark(
    strategy_template_id: Optional[str] = Query(default=None),
    base_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_trading_db_collections()
    strategy = _require_strategy(collections, strategy_template_id) if strategy_template_id else {}
    universe_index = str(strategy.get("universe_index") or "csi1000")
    normalized_base_date = _normalize_yyyymmdd(base_date)

    scoring_runs = finance_db["scoring_runs"]
    universe_query = {"universe_index": universe_index}
    target_query = dict(universe_query)
    if normalized_base_date:
        target_query["score_date"] = normalized_base_date
    target_run = scoring_runs.find_one(target_query, sort=[("updated_at", -1)]) if normalized_base_date else None
    latest_run = scoring_runs.find_one(universe_query, sort=[("score_date", -1), ("updated_at", -1)])
    latest_completed_run = scoring_runs.find_one(
        {**universe_query, "status": "completed"},
        sort=[("score_date", -1), ("updated_at", -1)],
    )
    available_score_watermark = _latest_available_score_watermark(finance_db["stock_scores"], universe_index)
    return {
        "success": True,
        "data": _jsonable(
            {
                "strategy_template_id": strategy_template_id,
                "base_date": normalized_base_date,
                "universe_index": universe_index,
                "target_scoring_run": target_run,
                "target_status": target_run.get("status") if target_run else None,
                "target_is_running": bool(target_run and target_run.get("status") == "running"),
                "latest_scoring_run": latest_run,
                "latest_completed_scoring_run": latest_completed_run,
                **available_score_watermark,
            }
        ),
    }


@router.get("/plans/{plan_id}")
@_offload
def get_portfolio_plan(plan_id: str, current_user: dict = Depends(get_current_user)):
    started_at = time.perf_counter()
    marks: List[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter() - started_at))

    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(collections, plan_id, current_user)
    mark("plan")
    owner_query = {"plan_id": plan_id, "user_id": _current_user_id(current_user)}
    items = list(collections["portfolio_trade_plan_items"].find(owner_query).sort("rank", 1))
    mark("items")
    user_id = _current_user_id(current_user)
    items = _attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )
    mark("live_context")
    items = _enrich_plan_items_with_score_snapshot(
        data_collections=data_collections,
        plan=plan,
        items=items,
    )
    items = _enrich_plan_items_with_estimated_amount(items)
    mark("score_snapshot")
    items, live_overlay = _enrich_plan_items_with_overlay(
        data_collections=data_collections,
        plan=plan,
        items=items,
    )
    mark("live_overlay")
    plan, items = _enrich_plan_items_with_llm_risk(plan=plan, items=items, user_id=user_id)
    mark("llm_risk")
    reviews = list(collections["portfolio_plan_reviews"].find(owner_query).sort("created_at", -1))
    mark("reviews")
    execution_status = _paper_execution_status(collections, data_collections, plan, items)
    mark("paper_status")
    execution_context = _plan_execution_context(finance_collections, plan, user_id)
    mark("execution_context")
    total_elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("PORTFOLIO_PLAN_DETAIL_TIMING_SECONDS", "1"))
    if total_elapsed >= threshold:
        previous = 0.0
        parts = []
        for label, elapsed in marks:
            parts.append(f"{label}={elapsed - previous:.3f}s")
            previous = elapsed
        log.warning(
            "Portfolio plan detail timing total=%.3fs plan_id=%s %s",
            total_elapsed,
            plan_id,
            " ".join(parts),
        )
    risk_snapshot = latest_plan_risk_snapshot(collections, plan_id=plan_id, user_id=user_id)
    return {
        "success": True,
        "data": {
            "plan": _jsonable(plan),
            "items": _jsonable(items),
            "live_overlay": _jsonable(live_overlay),
            "reviews": _jsonable(reviews),
            "execution_status": _jsonable(execution_status),
            "execution_mode": execution_context["execution_mode"],
            "live_execution_context": _jsonable(execution_context),
            "risk_snapshot": _jsonable(risk_snapshot),
        },
    }


@router.get("/plans/{plan_id}/operation-logs")
@_offload
def get_portfolio_plan_operation_logs(
    plan_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    _require_plan(collections, plan_id, current_user)
    rows = list(
        collections["portfolio_plan_operation_logs"]
        .find({"plan_id": plan_id, "user_id": user_id}, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/items")
@_offload
def get_portfolio_plan_items(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(collections, plan_id, current_user)
    user_id = _current_user_id(current_user)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id, "user_id": user_id}).sort("rank", 1))
    items = _attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )
    items = _enrich_plan_items_with_score_snapshot(
        data_collections=data_collections,
        plan=plan,
        items=items,
    )
    items = _enrich_plan_items_with_estimated_amount(items)
    items, live_overlay = _enrich_plan_items_with_overlay(
        data_collections=data_collections,
        plan=plan,
        items=items,
    )
    plan, items = _enrich_plan_items_with_llm_risk(plan=plan, items=items, user_id=user_id)
    return {"success": True, "data": _jsonable({"items": items, "live_overlay": live_overlay})}


@router.get("/plans/{plan_id}/bench")
@_offload
def get_portfolio_plan_bench(
    plan_id: str,
    bench_multiplier: float = Query(default=1.5, ge=1.0, le=3.0),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(collections, plan_id, current_user)
    user_id = _current_user_id(current_user)
    started_at = time.perf_counter()
    marks: List[tuple[str, float]] = []

    def _mark(label: str) -> None:
        marks.append((label, time.perf_counter() - started_at))

    # Starters mirror the actual current holdings (live if the lineage has any
    # live fills, otherwise the latest paper snapshot) so the lineup stays in
    # sync with 最新持仓 across the whole rebalance cadence.
    holdings = _current_lineage_holdings(
        collections, finance_collections, data_collections, plan, user_id
    )
    _mark("holdings")
    payload = _build_plan_bench_payload(
        plan,
        holdings,
        bench_multiplier=bench_multiplier,
        data_collections=data_collections,
    )
    _mark("bench_payload")
    payload = _enrich_bench_payload_with_llm_risk(plan=plan, payload=payload, user_id=user_id)
    _mark("llm_risk")

    total_elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("PORTFOLIO_PLAN_DETAIL_TIMING_SECONDS", "1"))
    if total_elapsed >= threshold:
        previous = 0.0
        parts = []
        for label, elapsed in marks:
            parts.append(f"{label}={elapsed - previous:.3f}s")
            previous = elapsed
        log.warning(
            "Portfolio plan bench timing total=%.3fs plan_id=%s holdings=%d %s",
            total_elapsed,
            plan_id,
            len(holdings),
            " ".join(parts),
        )
    return {"success": True, "data": _jsonable(payload)}


@router.post("/plans/{plan_id}/bench-risk")
async def enqueue_portfolio_bench_risk(
    plan_id: str,
    bench_multiplier: float = Query(default=1.5, ge=1.0, le=3.0),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue an AI risk review for the lineage's *bench* candidates.

    Mirror of holdings-risk but targeting the scored-but-unheld pool, so the
    coach can vet a substitute before swapping it onto the field. The cheap
    rule engine (ai_risk_review.review_symbol) runs in the plan worker, which
    writes its result onto the task for the client to poll. Advisory only — it
    never changes selection or status.
    """
    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    holdings = _current_lineage_holdings(
        collections, finance_collections, data_collections, plan, user_id
    )
    payload = _build_plan_bench_payload(
        plan, holdings, bench_multiplier=bench_multiplier, data_collections=data_collections
    )
    symbols = [str(row.get("symbol")) for row in (payload.get("bench") or []) if row.get("symbol")]
    base_date = (
        _normalize_yyyymmdd(payload.get("score_date"))
        or _normalize_yyyymmdd(plan.get("base_date"))
        or _business_today_yyyymmdd()
    )
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "bench_risk_review",
        "user_id": user_id,
        "plan_id": plan_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "params_hash": plan.get("params_hash"),
        "base_date": base_date,
        "symbols": symbols,
        "status": "pending",
        "attempts": 0,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {
        "success": True,
        "data": _jsonable(
            {
                "task_id": task["task_id"],
                "anchor_plan_id": plan_id,
                "base_date": base_date,
                "symbol_count": len(symbols),
            }
        ),
    }


@router.post("/plans/{plan_id}/approve")
async def approve_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "approved", request.comment, current_user)


@router.post("/plans/{plan_id}/reject")
async def reject_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "rejected", request.comment, current_user)


@router.post("/plans/{plan_id}/cancel")
async def cancel_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _cancel_plan(plan_id, request.comment, current_user)


def _cancel_open_live_signals_or_block(
    finance_collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    reviewer: str,
    reason: str,
    now: datetime,
    conflict_action: str,
) -> int:
    signal_query = {"plan_id": plan_id, "user_id": user_id, "mode": "live"}
    signals = list(finance_collections["trade_signals"].find(signal_query))
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    executions = (
        list(finance_collections["trade_executions"].find({"user_id": user_id, "order_id": {"$in": order_ids}}))
        if order_ids
        else []
    )
    if any(_signal_filled_qty(row) > 0 for row in signals) or any(_execution_fill_quantity(row) > 0 for row in executions):
        raise HTTPException(status_code=409, detail=f"plan already has live fills; cannot {conflict_action}")

    cancelled_signal_count = 0
    terminal_signal_statuses = TERMINAL_SIGNAL_STATUSES | {"cancelled", "canceled"}
    for signal in signals:
        order_id = signal.get("order_id")
        if not order_id or str(signal.get("status") or "") in terminal_signal_statuses:
            continue
        result = finance_collections["trade_signals"].update_one(
            {"user_id": user_id, "order_id": order_id},
            {
                "$set": {
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": reviewer,
                    "cancel_reason": reason,
                    "updated_at": now,
                }
            },
        )
        cancelled_signal_count += int(getattr(result, "modified_count", 0) or 0)
    return cancelled_signal_count


def _plan_has_paper_executions(collections: Dict[str, Any], plan: Dict[str, Any], plan_id: str, user_id: str) -> bool:
    return bool(
        plan.get("paper_executed_at")
        or collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id})
    )


def _plan_has_live_fills(finance_collections: Dict[str, Any], plan_id: str, user_id: str) -> bool:
    signal_query = {"plan_id": plan_id, "user_id": user_id, "mode": "live"}
    signals = list(finance_collections["trade_signals"].find(signal_query))
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    executions = (
        list(finance_collections["trade_executions"].find({"user_id": user_id, "order_id": {"$in": order_ids}}))
        if order_ids
        else []
    )
    return any(_signal_filled_qty(row) > 0 for row in signals) or any(_execution_fill_quantity(row) > 0 for row in executions)


def _review_plan(plan_id: str, decision: str, comment: str, current_user: Dict[str, Any]):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id, current_user)
    status = str(plan.get("status") or "")
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail=f"unsupported review decision {decision}")
    if status not in {"needs_review", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail=f"plan is not reviewable in status {status}")
    now = datetime.utcnow()
    user_id = _current_user_id(current_user)
    reviewer = _reviewer(current_user)
    finance_collections = get_db_collections()
    status_flipped = decision != status
    review_action = "approve" if decision == "approved" else "reject"
    cancelled_signal_count = 0
    if status_flipped and _plan_has_paper_executions(collections, plan, plan_id, user_id):
        raise HTTPException(status_code=409, detail=f"plan already has paper executions; cannot {review_action}")
    if status_flipped and decision == "approved" and _plan_has_live_signals(finance_collections, plan_id, user_id):
        raise HTTPException(status_code=409, detail="plan already has live signals; cannot approve")
    if status_flipped and not (decision == "rejected" and status == "approved") and _plan_has_live_fills(finance_collections, plan_id, user_id):
        raise HTTPException(status_code=409, detail=f"plan already has live fills; cannot {review_action}")
    if decision == "rejected" and status == "approved":
        cancelled_signal_count = _cancel_open_live_signals_or_block(
            finance_collections,
            plan_id=plan_id,
            user_id=user_id,
            reviewer=reviewer,
            reason=comment or "plan rejected by reviewer",
            now=now,
            conflict_action="reject",
        )
    review = {
        "plan_id": plan_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "user_id": user_id,
        "decision": decision,
        "reviewer": reviewer,
        "comment": comment,
        "created_at": now,
    }
    if cancelled_signal_count:
        review["cancelled_signal_count"] = cancelled_signal_count
    collections["portfolio_plan_reviews"].insert_one(review)
    collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {"$set": {"status": decision, "reviewer": review["reviewer"], "reviewed_at": now}},
    )
    if decision == "rejected":
        _append_plan_operation_log(
            collections,
            plan_id=plan_id,
            user_id=user_id,
            operation_type="reject_plan",
            status="success",
            message=comment or "plan rejected by reviewer",
            payload={"previous_status": status, "cancelled_signal_count": cancelled_signal_count},
        )
    if status_flipped and decision == "approved":
        items = list(
            collections["portfolio_trade_plan_items"].find(
                {"plan_id": plan_id, "user_id": user_id},
                {"symbol": 1, "_id": 0},
            )
        )
        symbols = [str(row.get("symbol") or "") for row in items if row.get("symbol")]
        if symbols:
            capture_plan_risk_snapshot(
                collections,
                plan_id=plan_id,
                user_id=user_id,
                decision_point="approved",
                symbols=symbols,
                analyzer_db=get_analyzer_db(),
            )
    return {"success": True, "data": _jsonable(review)}


def _cancel_plan(plan_id: str, comment: str, current_user: Dict[str, Any]):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    plan = _require_plan(trading_collections, plan_id, current_user)
    user_id = _current_user_id(current_user)
    status = str(plan.get("status") or "")
    # partially_executed is a paper shortfall outcome: it always carries paper
    # executions, so allow cancelling it to escape the dead-end. Live fills are
    # still blocked below via _cancel_open_live_signals_or_block.
    cancellable = ACTIVE_PLAN_STATUSES | {"partially_executed"}
    if status not in cancellable:
        raise HTTPException(status_code=400, detail=f"plan is not cancellable in status {status}")
    if status != "partially_executed" and (
        plan.get("paper_executed_at")
        or trading_collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id})
    ):
        raise HTTPException(status_code=409, detail="plan already has paper executions; cannot cancel")

    now = datetime.utcnow()
    reviewer = _reviewer(current_user)
    reason = comment or "plan cancelled by user"
    cancelled_signal_count = _cancel_open_live_signals_or_block(
        finance_collections,
        plan_id=plan_id,
        user_id=user_id,
        reviewer=reviewer,
        reason=reason,
        now=now,
        conflict_action="cancel",
    )

    review = {
        "plan_id": plan_id,
        "strategy_template_id": plan.get("strategy_template_id"),
        "user_id": user_id,
        "decision": "cancelled",
        "reviewer": reviewer,
        "comment": comment,
        "created_at": now,
    }
    trading_collections["portfolio_plan_reviews"].insert_one(review)
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancelled_by": reviewer,
                "cancel_comment": comment,
                "reviewer": reviewer,
                "reviewed_at": now,
            }
        },
    )
    _append_plan_operation_log(
        trading_collections,
        plan_id=plan_id,
        user_id=user_id,
        operation_type="cancel_plan",
        status="success",
        message=reason,
        payload={"previous_status": status, "cancelled_signal_count": cancelled_signal_count},
    )
    return {"success": True, "data": _jsonable({**review, "cancelled_signal_count": cancelled_signal_count})}


PAPER_ACTIVE_STATUSES = frozenset({"executed_paper", "partially_executed"})
ACTIVE_PLAN_STATUSES = frozenset({"needs_review", "approved"})


def _execution_fill_quantity(execution: Dict[str, Any]) -> float:
    for key in ("filled_size", "quantity", "filled_qty"):
        qty = _numeric(execution.get(key), default=-1.0)
        if qty > 0:
            return qty
    return 0.0


def _lineage_has_live_fills(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> bool:
    """True when the lineage has at least one live fill in trade_executions.

    Existence-only: resolve the lineage's live ``order_id`` set via a server-side
    ``distinct`` and probe ``trade_executions`` with a bounded ``find_one`` instead
    of replaying the full signals+executions bundle just to derive a boolean.
    """

    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return False
    signals_col = finance_collections.get("trade_signals")
    executions_col = finance_collections.get("trade_executions")
    if signals_col is None or executions_col is None:
        return False
    order_ids = [
        oid
        for oid in signals_col.distinct(
            "order_id",
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}, "mode": "live"},
        )
        if oid
    ]
    if not order_ids:
        return False
    return (
        executions_col.find_one(
            {
                "user_id": user_id,
                "order_id": {"$in": order_ids},
                "$or": [
                    {"filled_size": {"$gt": 0}},
                    {"quantity": {"$gt": 0}},
                    {"filled_qty": {"$gt": 0}},
                ],
            },
            {"_id": 1},
        )
        is not None
    )


def _lineage_live_signals(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> List[Dict[str, Any]]:
    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return []
    return list(
        finance_collections["trade_signals"]
        .find(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}, "mode": "live"},
            {
                "_id": 0,
                "plan_id": 1,
                "order_id": 1,
                "securities_account_id": 1,
                "created_at": 1,
            },
        )
        .sort("created_at", 1)
    )


def _lineage_has_live_signals(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> bool:
    """True when any plan in the lineage has been published to live trading."""
    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return False
    return bool(
        finance_collections["trade_signals"].find_one(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}, "mode": "live"},
            {"_id": 1},
        )
    )


def _lineage_live_account_id_from_signals(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> str:
    control = _get_lineage_control_doc(
        trading_collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    if control.get("securities_account_id"):
        return str(control.get("securities_account_id") or "")
    for signal in reversed(_lineage_live_signals(trading_collections, finance_collections, plan, user_id)):
        account_id = str(signal.get("securities_account_id") or "")
        if account_id:
            return account_id
    return ""


def _lineage_live_account_id_from_fills(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> str:
    """Prefer the securities account tied to signals that actually received fills.

    Read the materialized ``portfolio_lineage_controls.securities_account_id``
    first; only when it is missing do we probe the latest execution/signal that
    carries an account id (single ``find_one`` each), instead of replaying the
    whole lineage bundle just to pull one string.
    """

    control = _get_lineage_control_doc(
        trading_collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    if control.get("securities_account_id"):
        return str(control.get("securities_account_id") or "")

    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return ""
    signals_col = finance_collections.get("trade_signals")
    executions_col = finance_collections.get("trade_executions")
    order_ids = (
        [
            oid
            for oid in signals_col.distinct(
                "order_id",
                {"user_id": user_id, "plan_id": {"$in": lineage_ids}, "mode": "live"},
            )
            if oid
        ]
        if signals_col is not None
        else []
    )
    if executions_col is not None and order_ids:
        latest_exec = executions_col.find_one(
            {
                "user_id": user_id,
                "order_id": {"$in": order_ids},
                "securities_account_id": {"$nin": ["", None]},
            },
            {"_id": 0, "securities_account_id": 1},
            sort=[("timestamp", -1)],
        )
        if latest_exec and latest_exec.get("securities_account_id"):
            return str(latest_exec.get("securities_account_id"))
    if signals_col is not None:
        latest_signal = signals_col.find_one(
            {
                "user_id": user_id,
                "plan_id": {"$in": lineage_ids},
                "mode": "live",
                "securities_account_id": {"$nin": ["", None]},
            },
            {"_id": 0, "securities_account_id": 1},
            sort=[("created_at", -1)],
        )
        if latest_signal and latest_signal.get("securities_account_id"):
            return str(latest_signal.get("securities_account_id"))
    return ""


def _portfolio_execution_venue(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> str:
    """Where the lineage trades: 'live' (broker) vs 'paper' (paper book).

    A lineage counts as live once it has published any live signal, not only
    after a fill is reported back — otherwise a placed-but-unfilled live order
    would be misclassified as paper and vanish from the live overview.
    """
    control = _get_lineage_control_doc(
        trading_collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    stored = control.get("execution_venue")
    if stored in {"live", "paper"}:
        return str(stored)
    return "live" if _lineage_has_live_signals(trading_collections, finance_collections, plan, user_id) else "paper"


def _portfolio_param_summary(params_snapshot: Optional[Dict[str, Any]]) -> str:
    params = params_snapshot or {}
    parts: List[str] = []
    universe = params.get("universe_index")
    if universe:
        parts.append(str(universe))
    top_n = params.get("top_n")
    if top_n is not None:
        parts.append(f"Top{int(top_n)}")
    rebalance_days = params.get("rebalance_days")
    if rebalance_days is not None:
        parts.append(f"{int(rebalance_days)}d")
    mode = params.get("construction_mode")
    if mode:
        parts.append(str(mode))
    return " · ".join(parts)


def _get_lineage_control_doc(
    collections: Dict[str, Any],
    strategy_template_id: str,
    params_hash: str,
    user_id: str,
) -> Dict[str, Any]:
    coll = collections.get("portfolio_lineage_controls")
    if coll is None:
        return {"paused": False, "excluded_symbols": []}
    doc = coll.find_one(
        {
            "strategy_template_id": strategy_template_id,
            "params_hash": params_hash,
            "user_id": user_id,
        },
        {"_id": 0},
    )
    if doc:
        return doc
    return {"paused": False, "excluded_symbols": []}


def _lineage_control_key(strategy_template_id: str, params_hash: str, user_id: Optional[str]) -> Dict[str, Any]:
    return {
        "strategy_template_id": strategy_template_id,
        "params_hash": params_hash,
        "user_id": user_id,
    }


def _resume_lineage_native(
    collections: Dict[str, Any],
    strategy_template_id: str,
    params_hash: str,
    user_id: Optional[str],
    *,
    resumed_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Clear the pause flag on a lineage. Pure Mongo write; no cross-repo import."""
    coll = collections.get("portfolio_lineage_controls")
    key = _lineage_control_key(strategy_template_id, params_hash, user_id)
    now = datetime.utcnow()
    if coll is not None:
        coll.update_one(
            key,
            {
                "$set": {**key, "paused": False, "resumed_at": now, "resumed_by": resumed_by, "updated_at": now},
                "$unset": {"paused_at": "", "paused_by": "", "paused_reason": ""},
            },
            upsert=True,
        )
    return {**key, "paused": False}


def _apply_manual_lineage_effects_native(
    collections: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    final_positions: Optional[List[Dict[str, Any]]] = None,
    acted_by: Optional[str] = None,
    pause_on_empty: bool = True,
) -> Dict[str, Any]:
    """Mirror of tools.portfolio_trading.apply_manual_plan_lineage_effects.

    Kept native in quant-api (pure Mongo on portfolio_lineage_controls) so the API
    does not import stock-scoring-system. ``pause_on_empty`` must be False until
    fills are settled (e.g. live signal publishing only).
    """
    if str(plan.get("origin") or "") != "manual":
        return {}
    coll = collections.get("portfolio_lineage_controls")
    if coll is None:
        return {}
    key = _lineage_control_key(
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        plan.get("user_id"),
    )
    now = datetime.utcnow()
    effects: Dict[str, Any] = {}
    if plan.get("exclude_after"):
        symbols = sorted({str(s) for s in (plan.get("manual_exclude_symbols") or []) if s})
        if symbols:
            existing = (coll.find_one(key, {"_id": 0}) or {}).get("excluded_symbols") or []
            merged = sorted({str(s) for s in existing} | set(symbols))
            coll.update_one(
                key,
                {"$set": {**key, "excluded_symbols": merged, "updated_at": now}, "$setOnInsert": {"paused": False}},
                upsert=True,
            )
            effects["excluded"] = merged
    if not pause_on_empty:
        return effects
    positions = list(final_positions or [])
    if not any(_int_value(row.get("shares")) > 0 for row in positions):
        coll.update_one(
            key,
            {
                "$set": {
                    **key,
                    "paused": True,
                    "paused_at": now,
                    "paused_by": acted_by,
                    "paused_reason": "manual_liquidation",
                    "updated_at": now,
                },
                "$unset": {"resumed_at": ""},
            },
            upsert=True,
        )
        effects["paused"] = True
    return effects


def _latest_lineage_paper_snapshot(
    collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    query = _lineage_query_for_plan(collections, plan, user_id)
    return (
        collections["portfolio_paper_positions"].find_one(
            query, sort=[("as_of_date", -1), ("updated_at", -1)]
        )
        or {}
    )


def _resilient_record_kind(row: Dict[str, Any]) -> Optional[str]:
    """Best-effort record_kind for aggregate/list views.

    Prefers the stamped ``record_kind``; if a write path (e.g. a worker still
    running pre-stamp code) left it unset, derive it from the document shape via
    ``classify_record``. Returns ``None`` only when the row is genuinely
    unclassifiable. Unlike ``require_record_kind`` this never raises, so one
    unstamped plan can't 500 the whole portfolios list — strict per-plan reads
    (detail / timeline) still fail loud to surface the missing stamp.
    """
    try:
        classified = classify_record(row)
    except ValueError:
        classified = None
    if classified == RECORD_KIND_LEDGER_EVENT:
        return RECORD_KIND_LEDGER_EVENT
    kind = row.get("record_kind")
    if kind in VALID_RECORD_KINDS:
        return kind
    return classified


def _resolve_paper_execution_mode(
    plan: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Plan-level paper approval policy; paper defaults to auto_shadow."""
    params = params if params is not None else dict(plan.get("params_snapshot") or {})
    explicit = params.get("paper_execution_mode") or plan.get("paper_execution_mode")
    if explicit in ("auto_shadow", "manual_review"):
        return str(explicit)
    return "auto_shadow"


def _build_portfolio_summary_payload(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    lineage_control = _get_lineage_control_doc(
        trading_collections, strategy_template_id, params_hash, user_id
    )
    has_live = _lineage_has_live_signals(
        trading_collections, finance_collections, plan, user_id
    ) or _lineage_has_live_fills(trading_collections, finance_collections, plan, user_id)
    execution_venue = resolve_execution_venue(lineage_control, has_live_signals=has_live)
    params = dict(plan.get("params_snapshot") or {})
    paper_snapshot = _latest_lineage_paper_snapshot(trading_collections, plan, user_id)
    paper_positions = list(paper_snapshot.get("positions") or [])
    paper_holding_count = sum(1 for row in paper_positions if _int_value(row.get("shares")) > 0)
    sec_id = ""
    if execution_venue == "live":
        # Prefer the materialized account id; only replay the lineage
        # signals/executions bundle when the control has not captured it yet.
        sec_id = str(lineage_control.get("securities_account_id") or "")
        if not sec_id:
            sec_id = (
                _lineage_live_account_id_from_fills(trading_collections, finance_collections, plan, user_id)
                or _lineage_live_account_id_from_signals(trading_collections, finance_collections, plan, user_id)
            )
    return {
        "strategy_template_id": strategy_template_id,
        "params_hash": params_hash,
        "execution_venue": execution_venue,
        "paper_execution_mode": _resolve_paper_execution_mode(plan, params),
        "paper_snapshot_date": paper_snapshot.get("as_of_date"),
        "paper_holding_count": paper_holding_count,
        "paper_equity": paper_snapshot.get("equity"),
        "securities_account_id": sec_id,
        "paused": bool(lineage_control.get("paused")),
        "excluded_symbols": lineage_control.get("excluded_symbols") or [],
    }


def _list_portfolios_payload(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    user_id: str,
) -> List[Dict[str, Any]]:
    plans = list(
        trading_collections["portfolio_trade_plans"].find(
            {"user_id": user_id, "params_hash": {"$exists": True, "$nin": [None, ""]}},
            {
                "_id": 0,
                "plan_id": 1,
                "strategy_template_id": 1,
                "params_hash": 1,
                "base_date": 1,
                "strategy_name": 1,
                "status": 1,
                "plan_type": 1,
                "manual_action": 1,
                "record_kind": 1,
                "params_snapshot": 1,
                "paper_execution_mode": 1,
            },
        )
    )
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    plan_group_by_id: Dict[str, tuple] = {}
    for plan in plans:
        stid = plan.get("strategy_template_id")
        phash = plan.get("params_hash")
        if not stid or not phash:
            continue
        key = (str(stid), str(phash))
        groups[key].append(plan)
        if plan.get("plan_id"):
            plan_group_by_id[str(plan.get("plan_id"))] = key
    if not groups:
        return []

    strategy_template_ids = sorted({key[0] for key in groups})
    params_hashes = sorted({key[1] for key in groups})

    group_plan_ids: Dict[tuple, List[str]] = defaultdict(list)
    for pid, key in plan_group_by_id.items():
        group_plan_ids[key].append(pid)

    lineage_control_by_group: Dict[tuple, Dict[str, Any]] = {}
    controls_coll = trading_collections.get("portfolio_lineage_controls")
    if controls_coll is not None:
        controls = list(
            controls_coll.find(
                {
                    "user_id": user_id,
                    "strategy_template_id": {"$in": strategy_template_ids},
                    "params_hash": {"$in": params_hashes},
                },
                {"_id": 0},
            )
        )
        for control in controls:
            key = (
                str(control.get("strategy_template_id") or ""),
                str(control.get("params_hash") or ""),
            )
            if key in groups:
                lineage_control_by_group[key] = control

    # Live-ness comes from the materialized execution_venue; only lineages whose
    # control lacks a definitive venue (pre-backfill) need a probe, resolved for
    # all of them at once via a single server-side distinct instead of pulling
    # every live signal/execution just to compute a boolean + account id.
    signals_col = finance_collections.get("trade_signals")
    venue_by_group: Dict[tuple, str] = {}
    groups_needing_probe: List[tuple] = []
    for key in groups:
        stored = (lineage_control_by_group.get(key) or {}).get("execution_venue")
        if stored in VALID_EXECUTION_VENUES:
            venue_by_group[key] = str(stored)
        else:
            groups_needing_probe.append(key)

    live_plan_ids: set = set()
    if groups_needing_probe and signals_col is not None:
        probe_plan_ids = [pid for key in groups_needing_probe for pid in group_plan_ids.get(key, [])]
        if probe_plan_ids:
            live_plan_ids = {
                str(pid)
                for pid in signals_col.distinct(
                    "plan_id",
                    {"user_id": user_id, "plan_id": {"$in": probe_plan_ids}, "mode": "live"},
                )
                if pid
            }

    portfolios: List[Dict[str, Any]] = []
    for (strategy_template_id, params_hash), all_lineage in groups.items():
        key = (strategy_template_id, params_hash)
        if key in venue_by_group:
            has_live = venue_by_group[key] == EXECUTION_VENUE_LIVE
        else:
            has_live = any(pid in live_plan_ids for pid in group_plan_ids.get(key, []))
        has_paper = any(row.get("status") in PAPER_ACTIVE_STATUSES for row in all_lineage)
        if not has_live and not has_paper:
            continue

        latest = max(all_lineage, key=lambda row: str(row.get("base_date") or ""))
        earliest = min(all_lineage, key=lambda row: str(row.get("base_date") or ""))
        lineage_control = lineage_control_by_group.get(
            key,
            {"paused": False, "excluded_symbols": []},
        )
        sec_id = str(lineage_control.get("securities_account_id") or "")
        if not sec_id and has_live and signals_col is not None:
            latest_signal = signals_col.find_one(
                {
                    "user_id": user_id,
                    "plan_id": {"$in": group_plan_ids.get(key, [])},
                    "mode": "live",
                    "securities_account_id": {"$nin": ["", None]},
                },
                {"_id": 0, "securities_account_id": 1},
                sort=[("created_at", -1)],
            )
            if latest_signal:
                sec_id = str(latest_signal.get("securities_account_id") or "")

        params = dict(latest.get("params_snapshot") or {})
        execution_venue = resolve_execution_venue(lineage_control, has_live_signals=has_live)

        portfolios.append(
            {
                "strategy_template_id": strategy_template_id,
                "params_hash": params_hash,
                "params_hash_short": str(params_hash or "")[:8],
                "latest_plan_id": latest.get("plan_id"),
                "latest_plan_status": latest.get("status"),
                "latest_plan_type": latest.get("plan_type"),
                "param_summary": _portfolio_param_summary(params),
                "universe_index": params.get("universe_index"),
                "top_n": params.get("top_n"),
                "rebalance_days": params.get("rebalance_days"),
                "construction_mode": params.get("construction_mode"),
                "initial_capital": params.get("initial_capital"),
                "plan_count": sum(
                    1 for row in all_lineage if _resilient_record_kind(row) == RECORD_KIND_TRADE_PLAN
                ),
                "first_base_date": earliest.get("base_date"),
                "last_base_date": latest.get("base_date"),
                "strategy_name": latest.get("strategy_name") or earliest.get("strategy_name") or "",
                "securities_account_id": sec_id,
                "execution_venue": execution_venue,
                "paused": bool(lineage_control.get("paused")),
                "excluded_symbols": lineage_control.get("excluded_symbols") or [],
            }
        )
    portfolios.sort(key=lambda row: str(row.get("last_base_date") or ""), reverse=True)
    return portfolios


def _add_calendar_days(yyyymmdd: str, days: int) -> Optional[str]:
    try:
        return (datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return None


def _distinct_trade_dates(data_collections: Dict[str, Any], query: Dict[str, Any]) -> List[str]:
    """Server-side distinct trade dates.

    ``volume_price`` holds one row per (symbol, trade_date), so fetching rows to
    dedupe dates in Python pulls the whole market table. ``distinct`` returns
    only the distinct dates and lets MongoDB do the dedupe.
    """
    try:
        days = data_collections["volume_price"].distinct("trade_date", query)
    except Exception:
        cursor = data_collections["volume_price"].find(query, {"_id": 0, "trade_date": 1})
        days = [row.get("trade_date") for row in cursor]
    return sorted({str(d) for d in days if d})


def _trading_dates_between(data_collections: Dict[str, Any], start_date: str, end_date: str) -> List[str]:
    start = _normalize_yyyymmdd(start_date) or ""
    end = _normalize_yyyymmdd(end_date) or ""
    if not start or not end or start > end:
        return []
    return _distinct_trade_dates(data_collections, {"trade_date": {"$gte": start, "$lte": end}})


def _advance_trading_days(data_collections: Dict[str, Any], from_date: str, trading_day_offset: int) -> Optional[str]:
    if trading_day_offset <= 0:
        return _normalize_yyyymmdd(from_date)
    anchor = _normalize_yyyymmdd(from_date) or ""
    if not anchor:
        return None
    # Bound the forward scan instead of reading every future row of the whole
    # market table. Trading days are ~5/7 of calendar days, so these windows
    # comfortably contain ``trading_day_offset`` sessions; widen progressively
    # and only fall back to an unbounded scan in the pathological case.
    for span in (trading_day_offset * 2 + 15, trading_day_offset * 4 + 45):
        upper = _add_calendar_days(anchor, span)
        if not upper:
            break
        future_dates = _distinct_trade_dates(
            data_collections, {"trade_date": {"$gt": anchor, "$lte": upper}}
        )
        if len(future_dates) >= trading_day_offset:
            return future_dates[trading_day_offset - 1]
    future_dates = _distinct_trade_dates(data_collections, {"trade_date": {"$gt": anchor}})
    if len(future_dates) < trading_day_offset:
        return future_dates[-1] if future_dates else None
    return future_dates[trading_day_offset - 1]


def _drift_action_from_item(item: Dict[str, Any]) -> str:
    drift = _int_value(item.get("drift_delta_shares"))
    if drift > 0:
        return "buy"
    if drift < 0:
        return "sell"
    if _int_value(item.get("current_shares")) > 0:
        return "hold"
    return "skip"


def _executable_action_from_item(item: Dict[str, Any]) -> str:
    delta = _int_value(item.get("delta_shares"))
    if delta > 0:
        return "buy"
    if delta < 0:
        return "sell"
    if _int_value(item.get("current_shares")) > 0:
        return "hold"
    return "skip"


def _compute_drift_summary(items: List[Dict[str, Any]], equity: Optional[float] = None) -> Dict[str, Any]:
    buy_count = sell_count = hold_count = skip_count = 0
    buy_amount = sell_amount = 0.0
    new_entrants = dropped = 0
    for item in items:
        drift = _int_value(item.get("drift_delta_shares"))
        price = _numeric(item.get("estimated_price"), default=0.0)
        action = _drift_action_from_item(item)
        if action == "buy":
            buy_count += 1
        elif action == "sell":
            sell_count += 1
        elif action == "hold":
            hold_count += 1
        else:
            skip_count += 1
        if drift > 0 and price:
            buy_amount += abs(drift) * price
        elif drift < 0 and price:
            sell_amount += abs(drift) * price
        current_shares = _int_value(item.get("current_shares"))
        target_shares = _int_value(item.get("target_shares"))
        if current_shares == 0 and target_shares > 0:
            new_entrants += 1
        elif current_shares > 0 and target_shares == 0:
            dropped += 1
    resolved_equity = _numeric(equity) if equity is not None else 0.0
    turnover = (buy_amount + sell_amount) / resolved_equity if resolved_equity > 0 else None
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "hold_count": hold_count,
        "skip_count": skip_count,
        "estimated_buy_amount": buy_amount,
        "estimated_sell_amount": sell_amount,
        "estimated_turnover": turnover,
        "new_entrant_count": new_entrants,
        "dropped_count": dropped,
    }


def _plan_today_state_label(plan: Dict[str, Any]) -> str:
    status = str(plan.get("status") or "")
    plan_type = str(plan.get("plan_type") or "")
    non_executable_reason = str(plan.get("non_executable_reason") or "")
    monitor_can_trade = bool(plan.get("monitor_can_trade"))

    if plan.get("origin") == "manual" and plan.get("manual_action") == "external_manual":
        if plan.get("is_liquidation"):
            return "已补录手工清仓"
        return "已补录手工操作"
    if status == "needs_review":
        if plan_type == "monitor" and monitor_can_trade:
            return "观察日（已开启日内再平衡）：可交易，等待审核"
        return "调仓日：等待审核"
    if status == "approved":
        return "已批准，等待执行/发布"
    if status == "rejected":
        return "已拒绝，保留记录"
    if status == "executed_paper":
        return "纸面已执行"
    if status == "executed_live":
        return "实盘已执行"
    if status == "partially_executed":
        return "部分执行"
    if status == "filled":
        return "已成交"
    if status == "cancelled":
        return "计划已作废"
    if status == "superseded":
        return "已被新周期计划替代"
    if status == "expired":
        return "计划已过期"
    if plan.get("live_signals_published_at"):
        return "实盘信号已发布"
    if (
        status == "generated"
        and plan_type == "monitor"
        and non_executable_reason == "monitor_no_trade"
    ):
        return "观察日：不交易"
    if plan_type == "monitor" and plan.get("executable"):
        return "观察日（已开启日内再平衡）：可交易"
    if plan_type == "rebalance" or plan.get("is_rebalance_day"):
        return "调仓日"
    return status or "未知状态"


def _plan_action_required(plan: Dict[str, Any]) -> bool:
    status = str(plan.get("status") or "")
    if status in {"needs_review", "approved", "partially_executed"}:
        return True
    if bool(plan.get("executable")) and status in {"needs_review", "approved"}:
        return True
    return False


def _rebalance_days_from_plan(plan: Dict[str, Any]) -> int:
    params = plan.get("params_snapshot") or {}
    return max(1, _int_value(params.get("rebalance_days"), 20))


def _compute_current_cycle(
    latest_plan: Dict[str, Any],
    latest_rebalance_plan: Optional[Dict[str, Any]],
    latest_monitor_plan: Optional[Dict[str, Any]],
    data_collections: Dict[str, Any],
) -> Dict[str, Any]:
    previous_rebalance_date = latest_plan.get("previous_rebalance_date")
    if latest_rebalance_plan and latest_rebalance_plan.get("base_date"):
        previous_rebalance_date = latest_rebalance_plan.get("base_date")
    base_date = latest_plan.get("base_date")
    rebalance_days = _rebalance_days_from_plan(latest_plan)
    elapsed_trading_days = 0
    if previous_rebalance_date and base_date:
        trading_dates = _trading_dates_between(data_collections, str(previous_rebalance_date), str(base_date))
        elapsed_trading_days = max(0, len(trading_dates) - 1)
    remaining_days = max(0, rebalance_days - elapsed_trading_days)
    next_rebalance_estimate = None
    if base_date and remaining_days:
        next_rebalance_estimate = _advance_trading_days(data_collections, str(base_date), remaining_days)
    return {
        "previous_rebalance_date": previous_rebalance_date,
        "previous_rebalance_plan_id": (
            (latest_rebalance_plan or {}).get("plan_id")
            or latest_plan.get("previous_rebalance_plan_id")
        ),
        "rebalance_days": rebalance_days,
        "elapsed_trading_days": elapsed_trading_days,
        "next_rebalance_estimate": next_rebalance_estimate,
        "latest_monitor_plan_id": (latest_monitor_plan or {}).get("plan_id"),
        "latest_rebalance_plan_id": (latest_rebalance_plan or {}).get("plan_id"),
        "latest_base_date": base_date,
        "is_rebalance_day": bool(latest_plan.get("is_rebalance_day")),
        "plan_type": latest_plan.get("plan_type"),
    }


def _timeline_node_drift_brief(
    trading_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if items is None:
        items = list(
            trading_collections["portfolio_trade_plan_items"].find(
                {"plan_id": plan.get("plan_id"), "user_id": user_id},
                {
                    "_id": 0,
                    "symbol": 1,
                    "name": 1,
                    "rank": 1,
                    "action": 1,
                    "delta_shares": 1,
                    "drift_delta_shares": 1,
                    "estimated_price": 1,
                    "current_shares": 1,
                    "target_shares": 1,
                },
            )
        )
    equity = _numeric((plan.get("summary") or {}).get("equity"), default=0.0)
    summary = _compute_drift_summary(items, equity=equity or None)
    executable = bool(plan.get("executable"))
    trade_items: List[Dict[str, Any]] = []
    for item in sorted(
        items,
        key=lambda row: (
            row.get("rank") if row.get("rank") is not None else 999,
            str(row.get("symbol") or ""),
        ),
    ):
        if executable:
            delta = _int_value(item.get("delta_shares"))
            action = str(item.get("action") or _executable_action_from_item(item))
        else:
            delta = _int_value(item.get("drift_delta_shares"))
            action = _drift_action_from_item(item)
        if action not in {"buy", "sell"}:
            continue
        price = _numeric(item.get("estimated_price"), default=0.0)
        trade_items.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "action": action,
                "delta_shares": delta,
                "current_shares": _int_value(item.get("current_shares")),
                "target_shares": _int_value(item.get("target_shares")),
                "estimated_amount": abs(delta) * price if price and delta else None,
            }
        )
    return {
        "estimated_turnover": summary.get("estimated_turnover"),
        "buy_count": summary.get("buy_count"),
        "sell_count": summary.get("sell_count"),
        "hold_count": summary.get("hold_count"),
        "estimated_buy_amount": summary.get("estimated_buy_amount"),
        "estimated_sell_amount": summary.get("estimated_sell_amount"),
        "new_entrant_count": summary.get("new_entrant_count"),
        "dropped_count": summary.get("dropped_count"),
        "mode": "executable" if executable else "drift",
        "items": trade_items,
    }


def _timeline_items_by_plan(
    trading_collections: Dict[str, Any],
    plan_ids: List[str],
    user_id: str,
) -> Dict[str, List[Dict[str, Any]]]:
    if not plan_ids:
        return {}
    rows = list(
        trading_collections["portfolio_trade_plan_items"].find(
            {"plan_id": {"$in": plan_ids}, "user_id": user_id},
            {
                "_id": 0,
                "plan_id": 1,
                "symbol": 1,
                "name": 1,
                "rank": 1,
                "action": 1,
                "delta_shares": 1,
                "drift_delta_shares": 1,
                "estimated_price": 1,
                "current_shares": 1,
                "target_shares": 1,
            },
        )
    )
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("plan_id") or "")].append(row)
    return grouped


GENERATION_TASK_LIST_PROJECTION = {
    "_id": 0,
    "task_id": 1,
    "task_type": 1,
    "operation_type": 1,
    "operation_symbol": 1,
    "operation_symbols": 1,
    "user_id": 1,
    "plan_id": 1,
    "strategy_template_id": 1,
    "params_hash": 1,
    "base_date": 1,
    "mode": 1,
    "force": 1,
    "status": 1,
    "attempts": 1,
    "worker_id": 1,
    "error_message": 1,
    "created_at": 1,
    "updated_at": 1,
    "completed_at": 1,
    "symbol_count": 1,
}


def _build_lineage_timeline_payload(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    limit: int = 60,
    offset: int = 0,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    marks: List[tuple[str, float]] = []

    def _mark(label: str) -> None:
        marks.append((label, time.perf_counter() - started_at))

    lineage_query = _lineage_query_for_plan(
        trading_collections,
        plan,
        user_id,
        include_plan_id_fallback=False,
    )
    projection = dict(PLAN_LIST_PROJECTION)
    projection["live_signals_published_at"] = 1
    projection["origin"] = 1
    projection["manual_action"] = 1
    plans = list(
        trading_collections["portfolio_trade_plans"]
        .find(lineage_query, projection)
        # created_at breaks same-base_date ties so "latest" is deterministic
        # (a manual ledger record and a strategy plan can share base_date).
        .sort([("base_date", -1), ("created_at", -1)])
    )
    if not plans:
        raise HTTPException(status_code=404, detail="lineage plans not found")
    _mark("load_plans")

    latest_plan = plans[0]
    # record_kind is the authoritative type (stamped at write time). External
    # ledger facts (record_kind == ledger_event) are not strategy decisions, so
    # cadence / drift / headline look past them to the latest real plan. headline
    # prefers whatever still needs human action so a same-day ledger record never
    # hides a rebalance awaiting approval. require_record_kind fails loud if any
    # document is missing its kind (a write-path bug, not something to paper over).
    cadence_plan = next(
        (row for row in plans if require_record_kind(row) != RECORD_KIND_LEDGER_EVENT),
        latest_plan,
    )
    headline_plan = next((row for row in plans if _plan_action_required(row)), latest_plan)

    def is_strategy_rebalance(row: Dict[str, Any]) -> bool:
        # A scheduled (non-manual) trade plan. Manual off-cycle rebalances are
        # trade_plans too, but excluded here by origin (provenance dimension).
        return (
            require_record_kind(row) == RECORD_KIND_TRADE_PLAN
            and row.get("origin") != "manual"
        )

    latest_rebalance = next(
        (row for row in plans if is_strategy_rebalance(row)),
        None,
    )
    latest_monitor = next(
        (row for row in plans if require_record_kind(row) == RECORD_KIND_OBSERVATION),
        None,
    )
    _mark("picks")
    lineage_control = _get_lineage_control_doc(
        trading_collections,
        str(latest_plan.get("strategy_template_id") or ""),
        str(latest_plan.get("params_hash") or ""),
        user_id,
    )
    stored_venue = lineage_control.get("execution_venue")
    has_live_signals = False
    if stored_venue not in {"live", "paper"}:
        has_live_signals = _lineage_has_live_signals(
            trading_collections, finance_collections, latest_plan, user_id
        )
    execution_venue = resolve_execution_venue(
        lineage_control,
        has_live_signals=has_live_signals,
    )
    live_account_id = str(lineage_control.get("securities_account_id") or "")
    if execution_venue == "live" and not live_account_id:
        live_account_id = _lineage_live_account_id_from_signals(
            trading_collections, finance_collections, latest_plan, user_id
        )
    _mark("venue_account")

    drift_plan = cadence_plan
    drift_items = list(
        trading_collections["portfolio_trade_plan_items"].find(
            {"plan_id": drift_plan.get("plan_id"), "user_id": user_id},
            {
                "_id": 0,
                "drift_delta_shares": 1,
                "estimated_price": 1,
                "current_shares": 1,
                "target_shares": 1,
            },
        )
    )
    drift_equity = _numeric((drift_plan.get("summary") or {}).get("equity"), default=0.0)
    latest_drift_summary = _compute_drift_summary(drift_items, equity=drift_equity or None)
    latest_drift_summary.update(
        {
            "executable": bool(drift_plan.get("executable")),
            "non_executable_reason": drift_plan.get("non_executable_reason"),
            "plan_id": drift_plan.get("plan_id"),
            "base_date": drift_plan.get("base_date"),
        }
    )
    _mark("drift")

    total = len(plans)
    page_plans = plans[offset : offset + limit]
    page_plan_ids = [str(row.get("plan_id")) for row in page_plans if row.get("plan_id")]
    page_items_by_plan = _timeline_items_by_plan(trading_collections, page_plan_ids, user_id)
    _mark("page_items")
    timeline: List[Dict[str, Any]] = []
    for row in page_plans:
        record_kind = require_record_kind(row)
        node_type = _NODE_TYPE_BY_RECORD_KIND[record_kind]
        drift_brief = _timeline_node_drift_brief(
            trading_collections,
            row,
            user_id,
            page_items_by_plan.get(str(row.get("plan_id") or ""), []),
        )
        timeline.append(
            {
                "date": row.get("base_date"),
                "plan_id": row.get("plan_id"),
                "record_kind": record_kind,
                "node_type": node_type,
                "status": row.get("status"),
                "executable": bool(row.get("executable")),
                "non_executable_reason": row.get("non_executable_reason"),
                "plan_type": row.get("plan_type"),
                "is_rebalance_day": bool(row.get("is_rebalance_day")),
                "today_state": _plan_today_state_label(row),
                "action_required": _plan_action_required(row),
                "drift_brief": drift_brief,
            }
        )
    _mark("nodes")

    current_cycle = _compute_current_cycle(
        cadence_plan, latest_rebalance, latest_monitor, data_collections
    )
    _mark("current_cycle")

    total_elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("PORTFOLIO_PLAN_DETAIL_TIMING_SECONDS", "1"))
    if total_elapsed >= threshold:
        previous = 0.0
        parts = []
        for label, elapsed in marks:
            parts.append(f"{label}={elapsed - previous:.3f}s")
            previous = elapsed
        log.warning(
            "Lineage timeline timing total=%.3fs plan_id=%s plans=%d page=%d %s",
            total_elapsed,
            plan.get("plan_id"),
            total,
            len(page_plans),
            " ".join(parts),
        )

    return {
        "portfolio_identity": {
            "strategy_template_id": latest_plan.get("strategy_template_id"),
            "params_hash": latest_plan.get("params_hash"),
            "strategy_name": latest_plan.get("strategy_name"),
            "latest_plan_id": latest_plan.get("plan_id"),
            "execution_venue": execution_venue,
            "securities_account_id": live_account_id or None,
        },
        "operation_plan": {
            "plan_id": headline_plan.get("plan_id"),
            "status": headline_plan.get("status"),
            "base_date": headline_plan.get("base_date"),
            "record_kind": require_record_kind(headline_plan),
            "plan_type": headline_plan.get("plan_type"),
            "origin": headline_plan.get("origin"),
            "manual_action": headline_plan.get("manual_action"),
            "today_state": _plan_today_state_label(headline_plan),
            "action_required": _plan_action_required(headline_plan),
        },
        "current_cycle": current_cycle,
        "today_state": _plan_today_state_label(headline_plan),
        "action_required": _plan_action_required(headline_plan),
        "latest_drift_summary": latest_drift_summary,
        "timeline": timeline,
        "timeline_pagination": {"total": total, "limit": limit, "offset": offset},
    }


@router.post("/plans/{plan_id}/recalculate-live-snapshot")
@_offload
def recalculate_portfolio_plan_live_snapshot(
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    snapshot = rebuild_live_snapshot_for_plan(
        trading_collections, finance_collections, plan, user_id
    )
    if not snapshot:
        raise HTTPException(status_code=400, detail="unable to rebuild live snapshot for plan")
    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "updated_at": snapshot.get("updated_at"),
                "build_ms": snapshot.get("build_ms"),
                "watermark": snapshot.get("watermark"),
            }
        ),
    }


def _load_prices(volume_price, symbols: List[str], trade_date: str, field: str, latest: bool = False) -> Dict[str, float]:
    query: Dict[str, Any] = {"symbol": {"$in": symbols}}
    if not latest:
        query["trade_date"] = trade_date
    rows = volume_price.find(query, {"_id": 0, "symbol": 1, field: 1, "open": 1, "close": 1})
    prices = {}
    for row in rows:
        value = row.get(field) or row.get("close") or row.get("open")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[row["symbol"]] = price
    return prices


def _load_realtime_docs(volume_price_realtime, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    rows = volume_price_realtime.find(
        {"symbol": {"$in": symbols}},
        {
            "_id": 0,
            "symbol": 1,
            "close": 1,
            "realtime_trade_time": 1,
            "snapshot_time": 1,
            "updated_at": 1,
        },
    )
    return {row["symbol"]: row for row in rows if row.get("symbol")}


def _load_latest_close_prices(volume_price, symbols: List[str]) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
            projection={"_id": 0, "symbol": 1, "close": 1},
        )
        if not row:
            continue
        try:
            price = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def _first_positive_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _market_doc_yyyymmdd(doc: Optional[Dict[str, Any]]) -> str:
    if not doc:
        return ""
    value = (
        doc.get("trade_date")
        or doc.get("date")
        or doc.get("realtime_trade_time")
        or doc.get("snapshot_time")
        or doc.get("updated_at")
        or doc.get("created_at")
        or ""
    )
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt, length in (("%Y%m%d", 8), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:length], fmt).strftime("%Y%m%d")
        except (TypeError, ValueError):
            continue
    return ""


def _market_price_quotes(
    data_collections: Dict[str, Any],
    symbols: List[str],
    *,
    day_pnl_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Per-symbol price quotes with source metadata for overlay and publish repricing."""
    return market_price_quotes(
        data_collections,
        symbols,
        symbol_query_values=_symbol_query_values,
        day_pnl_date=day_pnl_date,
    )


def _load_fresh_market_prices(data_collections: Dict[str, Any], symbols: List[str]) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]]]:
    """Load latest usable market prices.

    Realtime prices are used only when their document date is at least as fresh
    as the latest daily bar. Otherwise the daily close is used so stale realtime
    snapshots do not distort live holdings.
    """
    quotes = _market_price_quotes(data_collections, symbols)
    return quotes_to_prices_and_realtime_docs(quotes)


def _enrich_plan_items_with_overlay(
    *,
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Attach read-only live price/score drift overlay; never writes to DB."""
    status = str(plan.get("status") or "")
    if status not in {"generated", "needs_review", "approved"}:
        return items, {
            "enabled": False,
            "reason": "plan_status_not_active",
            "score_snapshot_date": _normalize_yyyymmdd(plan.get("score_date")),
            "latest_score_date": None,
            "score_snapshot_stale": False,
            "score_snapshot_age_days": None,
        }
    if not plan.get("score_date") and not plan.get("params_snapshot"):
        return items, {
            "enabled": False,
            "reason": "score_context_missing",
            "score_snapshot_date": None,
            "latest_score_date": None,
            "score_snapshot_stale": False,
            "score_snapshot_age_days": None,
        }
    symbols = sorted({str(item.get("symbol")) for item in items if item.get("symbol")})
    quotes = _market_price_quotes(data_collections, symbols)
    params = dict(plan.get("params_snapshot") or {})
    universe_index = str(params.get("universe_index") or "csi1000")
    top_n = max(1, _int_value(params.get("top_n"), 10))
    plan_score_date = _resolve_plan_score_date(plan, universe_index)
    watermark = _latest_available_score_watermark(finance_db["stock_scores"], universe_index)
    latest_score_date = watermark.get("latest_available_score_date")
    latest_ranked: List[Dict[str, Any]] = []
    if latest_score_date:
        latest_ranked = _build_ranked_score_universe(
            finance_db["stock_scores"],
            str(latest_score_date),
            universe_index,
            params,
        )
    return attach_plan_item_overlay(
        plan=plan,
        items=items,
        quotes=quotes,
        plan_score_date=plan_score_date,
        latest_score_date=str(latest_score_date) if latest_score_date else None,
        latest_ranked=latest_ranked,
        top_n=top_n,
        numeric=_numeric,
        int_value=_int_value,
        symbol_key=_symbol_key,
        score_context={
            "score_type": params.get("score_type") or "growth_cycle_weighted",
            "score_column": params.get("score_column"),
            "growth_weight": params.get("growth_weight", 0.3),
            "cycle_weight": params.get("cycle_weight", 0.7),
            "universe_index": universe_index,
            "top_n": top_n,
        },
    )


# ---------------------------------------------------------------------------
# Domain sub-routers. Imported at the bottom so the submodules can reference the
# infra helpers defined above via the package namespace. Sub-routers use a
# prefix-less APIRouter and are aggregated here, so the public API paths and
# OpenAPI tags are identical to the pre-split inline routes.
# ---------------------------------------------------------------------------
from . import paper as _paper  # noqa: E402
from . import views as _views  # noqa: E402
from . import ledger as _ledger  # noqa: E402
from . import publishing as _publishing  # noqa: E402

router.include_router(_paper.router)
router.include_router(_views.router)
router.include_router(_ledger.router)
router.include_router(_publishing.router)

# Backward-compat re-exports: unit tests and tools reference these paper-domain
# symbols through the ``portfolio_plans`` package namespace.
paper_rebalance_portfolio = _paper.paper_rebalance_portfolio
execute_portfolio_plan_paper = _paper.execute_portfolio_plan_paper
get_portfolio_plan_executions = _paper.get_portfolio_plan_executions
get_portfolio_plan_equity = _paper.get_portfolio_plan_equity
_apply_paper_execution = _paper._apply_paper_execution
_paper_execution_status = _paper._paper_execution_status
_execution_doc = _paper._execution_doc

# Backward-compat re-exports for moved read-model/view endpoints.
get_portfolio_plan_lineage_equity = _views.get_portfolio_plan_lineage_equity
get_lineage_paper_positions = _views.get_lineage_paper_positions
get_lineage_paper_executions = _views.get_lineage_paper_executions
get_portfolio_plan_live_executions = _views.get_portfolio_plan_live_executions
get_portfolio_plan_live_equity = _views.get_portfolio_plan_live_equity
get_portfolio_plan_live_realtime_equity = _views.get_portfolio_plan_live_realtime_equity
list_portfolios = _views.list_portfolios
get_portfolio_plan_summary = _views.get_portfolio_plan_summary
list_live_portfolios = _views.list_live_portfolios
get_portfolio_plan_lineage_timeline = _views.get_portfolio_plan_lineage_timeline
get_lineage_live_executions = _views.get_lineage_live_executions
get_lineage_live_positions = _views.get_lineage_live_positions
get_lineage_live_equity = _views.get_lineage_live_equity
get_portfolio_plan_live_summary = _views.get_portfolio_plan_live_summary
get_portfolio_strategy_equity = _views.get_portfolio_strategy_equity
get_portfolio_strategy_realtime_equity = _views.get_portfolio_strategy_realtime_equity

# Backward-compat re-exports for the moved ledger/reconcile domain. The
# reconcile service is still monkeypatched and cross-called (views live-summary)
# through the package namespace, so keep the symbols resolvable here.
reconcile_portfolio_holdings = _ledger.reconcile_portfolio_holdings
_reconcile_live_holdings = _ledger._reconcile_live_holdings
_account_level_live_holdings = _ledger._account_level_live_holdings
_settling_symbol_keys = _ledger._settling_symbol_keys
_INFLIGHT_SIGNAL_STATUSES = _ledger._INFLIGHT_SIGNAL_STATUSES

# Backward-compat re-exports for the moved publishing/live-rebalance risk
# helpers. The publish risk report is monkeypatched (confirm-path guard) and
# cross-called by the still-inline publish/replan/live-rebalance endpoints
# through the package namespace, so keep these symbols resolvable here.
_build_live_publish_risk_report = _publishing._build_live_publish_risk_report
_partition_publishable_live_signals = _publishing._partition_publishable_live_signals
_risk_report_has_account_level_block = _publishing._risk_report_has_account_level_block
_SYMBOL_INTRINSIC_BLOCKERS = _publishing._SYMBOL_INTRINSIC_BLOCKERS
_BUDGET_BLOCKERS = _publishing._BUDGET_BLOCKERS
_ACCOUNT_LEVEL_BLOCKERS = _publishing._ACCOUNT_LEVEL_BLOCKERS

# R5b: the live-rebalance endpoint and its exclusive helpers now live in
# publishing.py. Re-exported so the still-inline publish endpoint (which reuses
# _normalize_live_target_shares) and unit tests keep resolving them through the
# package namespace.
live_rebalance_portfolio = _publishing.live_rebalance_portfolio
_normalize_live_target_shares = _publishing._normalize_live_target_shares
_live_signal_is_blocked = _publishing._live_signal_is_blocked
LIVE_ACTION_HARD_BLOCKERS = _publishing.LIVE_ACTION_HARD_BLOCKERS

# R5c: publish-live-signals endpoint + its reprice-persist helper now live in
# publishing.py. Re-exported for the package namespace (unit tests call the
# endpoint through it).
publish_portfolio_plan_live_signals = _publishing.publish_portfolio_plan_live_signals
_persist_repriced_plan_items = _publishing._persist_repriced_plan_items

# R5d: replan-remainder endpoint + its audit helper now live in publishing.py.
replan_portfolio_plan_remainder = _publishing.replan_portfolio_plan_remainder
_append_remainder_audit = _publishing._append_remainder_audit
