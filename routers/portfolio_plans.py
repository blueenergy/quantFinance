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
from services.live_trade_signals import (
    ACTIVE_SIGNAL_STATUSES,
    PLAN_PHASE_APPROVED_PUBLISHED,
    PLAN_PHASE_DONE,
    PLAN_PHASE_SELLING,
    TERMINAL_SIGNAL_STATUSES,
    active_signal_query_for_order_ids,
    build_portfolio_plan_live_signal,
    derive_execution_phase_state,
    summarize_signal_statuses,
)
from services.live_equity import (
    _load_latest_close_prices,
    compute_live_plan_realtime_equity,
    list_live_plan_equity,
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
    overlay_account_positions,
    overlay_realtime,
    probe_stale,
    rebuild_live_snapshot_for_plan,
)
from services.portfolio_lineage_paper import (
    build_lineage_paper_trades,
    merge_lineage_paper_executions,
)
from services.portfolio_lineage_state import (
    EXECUTION_VENUE_LIVE,
    VALID_EXECUTION_VENUES,
    mark_lineage_live,
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
from services.execution_policy import estimate_buy_order_cash_exposure
from services.trade_ledger import group_executions_by_order
from services.portfolio_plan_remainder import (
    LIVE_SIGNAL_SOURCE_PLAN_TOPUP,
    build_remainder_specs,
    build_topup_signal_doc,
)
from services.trader_signal_reprice import resolve_market_reference_price
from services.portfolio_plan_market import (
    PRICE_SOURCE_DAILY_CLOSE,
    PRICE_SOURCE_NONE,
    attach_plan_item_overlay,
    market_price_quotes,
    quotes_to_prices_and_realtime_docs,
    reprice_plan_items_for_publish,
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
RISK_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
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
# Mirrors stock-scoring-system/tools/portfolio_trading.py PLAN_PARAM_KEYS — keep in sync
# when filtering params_override for force-rebalance so lineage params_hash is preserved.
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


class ReplanRemainderRequest(BaseModel):
    securities_account_id: str
    dry_run: bool = Field(default=True)
    reason: str = Field(default="")


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


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
            trading_collections, finance_collections, plan, user_id
        )
    elif schedule_rebuild_if_stale and probe_stale(finance_collections, snapshot):

        def _rebuild() -> None:
            try:
                rebuild_live_snapshot_for_plan(
                    trading_collections, finance_collections, plan, user_id
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "async snapshot rebuild failed for %s/%s",
                    strategy_template_id,
                    params_hash[:8] if params_hash else "",
                )

        threading.Thread(target=_rebuild, daemon=True).start()

    if not snapshot:
        return None, {}

    symbols = sorted(
        {
            str(row.get("symbol"))
            for row in (snapshot.get("positions") or [])
            if row.get("symbol")
        }
    )
    prices, realtime_docs = _load_fresh_market_prices(data_collections, symbols)
    overlay = overlay_realtime(snapshot, prices, realtime_docs=realtime_docs)
    return snapshot, overlay


def _build_lineage_live_equity_chart(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
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

    account_equity_rows: List[Dict[str, Any]] = []
    if account_ids:
        account_query: Dict[str, Any] = {
            "user_id": user_id,
            "securities_account_id": {"$in": account_ids},
        }
        if date_query:
            account_query["date"] = date_query
        raw_account_rows = list(
            finance_collections["trader_account_equity"].find(account_query).sort("date", 1)
        )
        latest_by_account_date: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in raw_account_rows:
            date = str(row.get("date") or "")
            account_id = str(row.get("securities_account_id") or "")
            if not date or not account_id:
                continue
            key = (date, account_id)
            existing = latest_by_account_date.get(key)
            if existing is None or str(row.get("updated_at") or "") >= str(existing.get("updated_at") or ""):
                latest_by_account_date[key] = row

        daily_totals: Dict[str, Dict[str, Any]] = {}
        for (date, account_id), row in latest_by_account_date.items():
            bucket = daily_totals.setdefault(
                date,
                {
                    "date": date,
                    "source": "trader_account_equity",
                    "account_ids": [],
                    "equity": 0.0,
                    "cash": 0.0,
                    "market_value": 0.0,
                    "available_cash": 0.0,
                    "updated_at": "",
                },
            )
            bucket["account_ids"].append(account_id)
            bucket["equity"] += _numeric(row.get("total_asset", row.get("total_assets")))
            bucket["cash"] += _numeric(row.get("cash"))
            bucket["market_value"] += _numeric(row.get("market_value"))
            bucket["available_cash"] += _numeric(row.get("available_cash"))
            bucket["updated_at"] = max(str(bucket.get("updated_at") or ""), str(row.get("updated_at") or ""))

        account_equity_rows = [
            {
                **row,
                "account_ids": sorted(set(row.get("account_ids") or [])),
                "equity": round(_numeric(row.get("equity")), 2),
                "cash": round(_numeric(row.get("cash")), 2),
                "market_value": round(_numeric(row.get("market_value")), 2),
                "available_cash": round(_numeric(row.get("available_cash")), 2),
            }
            for row in sorted(daily_totals.values(), key=lambda r: str(r.get("date") or ""))
        ]

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

    if account_equity_rows:
        rows_for_chart = account_equity_rows
        row_source = "trader_account_equity"
        caveat = (
            "净值曲线使用该组合关联证券账户的每日总资产(total_asset)，按日期聚合；"
            "横轴为交易日/自然日快照日期，纵轴为账户整体权益。"
        )
    else:
        rows_for_chart = enriched
        row_source = "portfolio_live_equity_legacy"
        caveat = (
            "未找到该组合关联账户的 trader_account_equity 日快照，暂回退到旧的 "
            "portfolio_live_equity plan 级快照；建议先生成账户级每日权益快照。"
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


def _normalize_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    return None


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


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


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
    return {key: params_snapshot[key] for key in sorted(PLAN_PARAM_KEYS) if key in params_snapshot}


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
    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": {"$in": _symbol_query_values(symbol)}},
            sort=[("trade_date", -1)],
            projection=projection,
        )
        if row:
            bars[symbol] = row
    return bars


def _build_ranked_score_universe(
    stock_scores,
    score_date: str,
    universe_index: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows = list(
        stock_scores.find(
            {"score_date": score_date, "index_codes": universe_index},
            {
                "_id": 0,
                "symbol": 1,
                "name": 1,
                "industry": 1,
                "growth_score": 1,
                "cycle_score": 1,
                "composite_score": 1,
            },
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
                "name": row.get("name") or "",
                "industry": row.get("industry") or "",
                "score_value": score_value,
            }
        )
    ranked.sort(key=lambda item: (-item["score_value"], item["symbol"]))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
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


def _risk_severity(value: Any) -> str:
    severity = str(value or "none").lower()
    return severity if severity in RISK_SEVERITY_ORDER else "none"


def _risk_severity_at_least(value: Any, minimum: str) -> bool:
    return RISK_SEVERITY_ORDER[_risk_severity(value)] >= RISK_SEVERITY_ORDER[_risk_severity(minimum)]


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


def _chunked(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


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
    plan_id = str(plan.get("plan_id") or "")
    run_id = _latest_completed_portfolio_llm_run_id(user_id=user_id, plan_id=plan_id)
    if not run_id:
        return plan, items

    risk_coll = get_analyzer_db()["portfolio_risk_llm"]
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
                "name": pos.get("name") or score_row.get("name") or "",
                "industry": score_row.get("industry") or "",
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
    starters_missing_price = [s["symbol"] for s in starters if s.get("estimated_price") is None]
    latest_bars = _load_latest_daily_bars_loose(
        data_collections["volume_price"], list({*bench_symbols, *starters_missing_price})
    )

    bench: List[Dict[str, Any]] = []
    for symbol in bench_symbols:
        score_row = rank_by_key.get(_symbol_key(symbol)) or {}
        latest_bar = latest_bars.get(symbol) or {}
        bench.append(
            {
                "symbol": symbol,
                "name": score_row.get("name") or "",
                "industry": score_row.get("industry") or "",
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
    plan_id = str(plan.get("plan_id") or payload.get("plan_id") or "")
    run_id = _latest_completed_portfolio_llm_run_id(
        user_id=user_id,
        plan_id=plan_id,
        target_scope="bench",
        eligible_statuses={"completed", "completed_with_failures", "running", "partial"},
    )
    if not run_id:
        return payload

    risk_coll = get_analyzer_db()["portfolio_risk_llm"]
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


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _symbol_key(value: Any) -> str:
    return str(value or "").split(".", 1)[0]


def _symbol_query_values(value: Any) -> List[str]:
    raw = str(value or "")
    base = _symbol_key(raw)
    values = {raw, base}
    if base:
        values.update({f"{base}.SH", f"{base}.SZ"})
    return [item for item in values if item]


def _signal_filled_qty(row: Dict[str, Any]) -> int:
    filled = _int_value(row.get("filled_qty", row.get("filled_size", 0)))
    if filled == 0 and str(row.get("status") or "").lower() == "filled":
        return abs(_int_value(row.get("size")))
    return abs(filled)


def _signal_signed_filled_qty(row: Dict[str, Any]) -> int:
    qty = _signal_filled_qty(row)
    action = str(row.get("action") or row.get("side") or "").lower()
    if action in {"sell", "s"}:
        return -qty
    return qty


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


def _daily_live_signal_amount(finance_collections: Dict[str, Any], account: Dict[str, Any], user_id: str) -> float:
    today_prefix = datetime.utcnow().date().isoformat()
    rows = finance_collections["trade_signals"].find(
        {"user_id": user_id, "securities_account_id": str(account.get("_id")), "mode": "live"}
    )
    total = 0.0
    for row in rows:
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            is_today = created_at.date() == datetime.utcnow().date()
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
        from stock_data_access.indicators.volatility import compute_volatility_features
    except ImportError:
        return {}

    if not symbols:
        return {}
    as_of = (base_date or datetime.utcnow().strftime("%Y%m%d")).replace("-", "")[:8]
    coll = data_collections["volume_price"]
    out: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        if not symbol:
            continue
        try:
            rows = list(
                coll.find(
                    {"symbol": symbol, "trade_date": {"$lte": as_of}},
                    {"_id": 0, "trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1},
                )
                .sort("trade_date", 1)
                .limit(320)
            )
            if rows:
                out[symbol] = compute_volatility_features(rows)
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


def _build_live_publish_risk_report(
    *,
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    account: Dict[str, Any],
    signals: List[Dict[str, Any]],
    user_id: str,
) -> Dict[str, Any]:
    account_snapshot = _latest_account_snapshot(finance_collections, account, user_id)
    available_cash = _numeric(account_snapshot.get("available_cash") or account_snapshot.get("cash"))
    total_assets = _numeric(account_snapshot.get("total_assets") or account_snapshot.get("total_asset"))
    risk_limits = account.get("risk_limits") or {}
    max_order_amount = _numeric(risk_limits.get("max_order_amount") or account.get("max_order_amount"), float("inf"))
    max_daily_amount = _numeric(risk_limits.get("max_daily_amount") or account.get("max_daily_amount"), float("inf"))
    max_position_pct = _numeric(risk_limits.get("max_position_pct") or account.get("max_position_pct"), 0.0)
    daily_used_amount = _daily_live_signal_amount(finance_collections, account, user_id)
    market_docs = _load_stock_guard_docs(data_collections, [signal["symbol"] for signal in signals])
    buy_symbols = [signal["symbol"] for signal in signals if signal.get("action") == "buy" and signal.get("symbol")]
    vol_features_by_symbol = _load_volatility_features_by_symbol(data_collections, buy_symbols)

    cash_reserved = 0.0
    day_reserved = 0.0
    items = []
    for signal in signals:
        amount = abs(int(signal.get("size", 0) or 0)) * _numeric(signal.get("price"))
        blockers: List[str] = []
        if not _account_live_enabled(account):
            blockers.append("live_trading_disabled")
        if not account.get("broker") or not account.get("account_id"):
            blockers.append("account_binding_missing")
        if max_order_amount != float("inf") and amount > max_order_amount:
            blockers.append("max_order_amount_exceeded")
        if max_daily_amount != float("inf") and daily_used_amount + day_reserved + amount > max_daily_amount:
            blockers.append("max_daily_amount_exceeded")
        if signal.get("action") == "buy":
            blockers.extend(_buy_blockers_from_market_doc(signal, market_docs.get(signal["symbol"], {})))
            blockers.extend(_volatility_buy_blockers(vol_features_by_symbol.get(signal.get("symbol"))))
            if available_cash and cash_reserved + amount > available_cash:
                blockers.append("insufficient_cash")
            if total_assets > 0 and max_position_pct > 0:
                position = _position_snapshot(finance_collections, signal, user_id)
                current_market_value = _numeric(position.get("market_value"))
                if (current_market_value + amount) / total_assets * 100 > max_position_pct:
                    blockers.append("max_position_pct_exceeded")
            cash_reserved += amount
        elif signal.get("action") == "sell":
            position = _position_snapshot(finance_collections, signal, user_id)
            available_qty = int(position.get("available_qty") or position.get("can_use_volume") or position.get("qty") or 0)
            if int(signal.get("size", 0) or 0) > available_qty:
                blockers.append("insufficient_available_position")
        day_reserved += amount
        items.append(
            {
                "order_id": signal.get("order_id"),
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "size": signal.get("size"),
                "price": signal.get("price"),
                "execution_phase": signal.get("execution_phase"),
                "execution_priority": signal.get("execution_priority"),
                "plan_item_rank": signal.get("plan_item_rank"),
                "reference_price": signal.get("reference_price"),
                "reference_price_source": signal.get("reference_price_source"),
                "max_slippage_bps": signal.get("max_slippage_bps"),
                "price_floor": signal.get("price_floor"),
                "price_ceiling": signal.get("price_ceiling"),
                "effective_limit_price": signal.get("effective_limit_price"),
                "valid_until": signal.get("valid_until"),
                "estimated_amount": amount,
                "blockers": sorted(set(blockers)),
                "warnings": sorted(
                    {
                        warning
                        for warning in (
                            ["priced_from_stale_close"]
                            if str(signal.get("reference_price_source") or "") == PRICE_SOURCE_DAILY_CLOSE
                            else []
                        )
                    }
                ),
            }
        )

    return {
        "account": {
            "securities_account_id": str(account.get("_id")),
            "broker": account.get("broker"),
            "account_id": account.get("account_id"),
            "live_trading_enabled": _account_live_enabled(account),
            "available_cash": available_cash,
        },
        "limits": {
            "max_order_amount": None if max_order_amount == float("inf") else max_order_amount,
            "max_daily_amount": None if max_daily_amount == float("inf") else max_daily_amount,
            "max_position_pct": max_position_pct or None,
            "daily_used_amount": daily_used_amount,
            "new_signal_amount": day_reserved,
        },
        "items": items,
        "blocked_count": sum(1 for item in items if item["blockers"]),
    }


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

    base_date = _normalize_yyyymmdd(request.base_date) or datetime.now().strftime("%Y%m%d")
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
LIVE_ACTION_HARD_BLOCKERS = {"live_trading_disabled", "account_binding_missing"}


def _normalize_live_target_shares(current_shares: int, requested_target: int, lot_size: int) -> Tuple[int, List[str]]:
    """Snap a manual target to tradable lots; clearing to zero keeps a full exit.

    Native mirror of tools.portfolio_trading.normalize_manual_target_shares so the
    API does not import stock-scoring-system.
    """
    current = max(0, int(current_shares or 0))
    requested = max(0, int(requested_target or 0))
    lot = max(1, int(lot_size or 1))
    blockers: List[str] = []
    if requested == current:
        return current, blockers
    if requested == 0:
        return 0, blockers
    delta = requested - current
    if delta > 0:
        rounded_delta = (delta // lot) * lot
        if rounded_delta <= 0:
            blockers.append("below_lot_size")
            return current, blockers
        return current + rounded_delta, blockers
    sell_qty = (abs(delta) // lot) * lot
    if sell_qty <= 0:
        blockers.append("below_lot_size")
        return current, blockers
    return current - sell_qty, blockers


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


def _account_level_live_holdings(
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
) -> List[Dict[str, Any]]:
    """Net live positions for the whole securities account (all live plans)."""
    execution_projection = {
        "_id": 0,
        "order_id": 1,
        "user_id": 1,
        "securities_account_id": 1,
        "symbol": 1,
        "name": 1,
        "stock_name": 1,
        "side": 1,
        "action": 1,
        "status": 1,
        "execution_status": 1,
        "filled_size": 1,
        "filled_qty": 1,
        "quantity": 1,
        "size": 1,
        "filled_price": 1,
        "traded_price": 1,
        "avg_price": 1,
        "price": 1,
        "target_price": 1,
        "total_fee": 1,
        "commission": 1,
        "fee": 1,
        "timestamp": 1,
        "created_at": 1,
    }
    executions = list(
        finance_collections["trade_executions"]
        .find(
            {"user_id": user_id, "securities_account_id": securities_account_id},
            execution_projection,
        )
        .sort("timestamp", 1)
    )
    if executions:
        return _holdings_from_executions(executions, data_collections)

    # Legacy execution rows may lack securities_account_id. Fall back to the
    # signal join only when the direct account-scoped execution query is empty.
    signals = list(
        finance_collections["trade_signals"].find(
            {
                "user_id": user_id,
                "mode": "live",
                "securities_account_id": securities_account_id,
            },
            {"_id": 0, "order_id": 1},
        )
    )
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    if not order_ids:
        return []
    executions = list(
        finance_collections["trade_executions"]
        .find({"user_id": user_id, "order_id": {"$in": order_ids}}, execution_projection)
        .sort("timestamp", 1)
    )
    return _holdings_from_executions(executions, data_collections)


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


# Live signal statuses that mean an order is still working at the broker and has
# not finished settling. A divergence on such a symbol is expected (in-flight),
# not a manual broker-side drift, so it must not raise the reconcile banner.
_INFLIGHT_SIGNAL_STATUSES = frozenset(
    {"pending", "retry_pending", "submitted", "partial_filled", "cancel_requested"}
)


def _settling_symbol_keys(
    finance_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
    account_synced_at: Optional[float],
) -> set:
    """Symbol keys whose ledger/broker gap is explained by in-flight settlement.

    A divergence is "settling" (not a real manual drift) when either:
      * a live signal for the symbol is still working at the broker, or
      * the symbol has a fill newer than the broker's last position sync, so the
        broker snapshot is simply stale and will catch up on the next sync.

    These should be hidden from the reconcile banner to avoid confusing users
    right after they trigger an in-app add/reduce.
    """
    signals_col = finance_collections.get("trade_signals")
    if signals_col is None:
        return set()

    signals = list(
        signals_col.find(
            {
                "user_id": user_id,
                "mode": "live",
                "securities_account_id": securities_account_id,
            },
            {"_id": 0, "order_id": 1, "symbol": 1, "status": 1},
        )
    )
    if not signals:
        return set()

    settling: set = set()
    order_ids: List[Any] = []
    for sig in signals:
        oid = sig.get("order_id")
        if oid:
            order_ids.append(oid)
        status = str(sig.get("status") or "").strip().lower()
        if status in _INFLIGHT_SIGNAL_STATUSES:
            key = _symbol_key(sig.get("symbol"))
            if key:
                settling.add(key)

    executions_col = finance_collections.get("trade_executions")
    if executions_col is not None and isinstance(account_synced_at, (int, float)):
        direct_query = {
            "user_id": user_id,
            "securities_account_id": securities_account_id,
            "timestamp": {"$gt": account_synced_at},
        }
        direct_rows = list(executions_col.find(direct_query, {"_id": 0, "symbol": 1, "timestamp": 1}))
        fallback_rows = []
        if not direct_rows and order_ids:
            fallback_rows = list(
                executions_col.find(
                    {"user_id": user_id, "order_id": {"$in": order_ids}},
                    {"_id": 0, "symbol": 1, "timestamp": 1},
                )
            )
        for ex in [*direct_rows, *fallback_rows]:
            ts = ex.get("timestamp")
            if isinstance(ts, (int, float)) and ts > account_synced_at:
                key = _symbol_key(ex.get("symbol"))
                if key:
                    settling.add(key)

    return settling


def _reconcile_live_holdings(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Diff the account-level system ledger against broker-synced positions.

    The ledger aggregates every live fill under the plan's ``securities_account_id``
    (not just this plan's lineage) so shared accounts with multiple strategies do
    not produce false mismatches. Divergence usually means a manual broker-side
    trade was not recorded, so we surface it for immediate re-sync.

    Symbols whose gap is still settling (working orders or fills newer than the
    broker's last position sync) are reported separately and excluded from the
    ``in_sync`` verdict, so an in-app add/reduce does not flash a false mismatch
    during the fill -> position-sync window.
    """
    lineage_control = _get_lineage_control_doc(
        trading_collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    account_id = str(lineage_control.get("securities_account_id") or "")
    if not account_id and finance_collections.get("trade_signals") is not None:
        account_id = _lineage_live_account_id_from_signals(
            trading_collections, finance_collections, plan, user_id
        )
    if not account_id:
        account_id = _lineage_live_account_id_from_fills(
            trading_collections, finance_collections, plan, user_id
        )
    if not account_id:
        return {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}

    if snapshot and (snapshot.get("account_positions") or snapshot.get("positions")):
        account_symbols = sorted(
            {
                str(row.get("symbol"))
                for row in (snapshot.get("account_positions") or snapshot.get("positions") or [])
                if row.get("symbol")
            }
        )
        prices, realtime_docs = _load_fresh_market_prices(data_collections, account_symbols)
        ledger = overlay_account_positions(snapshot, prices, realtime_docs=realtime_docs)
    else:
        ledger = _account_level_live_holdings(
            finance_collections,
            data_collections,
            user_id=user_id,
            securities_account_id=account_id,
        )

    ledger_by_key: Dict[str, Dict[str, Any]] = {
        _symbol_key(row.get("symbol")): row for row in ledger if row.get("symbol")
    }

    account_rows = list(
        finance_collections["trader_positions"].find(
            {"user_id": user_id, "securities_account_id": account_id},
            {"_id": 0},
        )
    )
    account_by_key: Dict[str, Dict[str, Any]] = {}
    synced_at: Optional[float] = None
    for row in account_rows:
        key = _symbol_key(row.get("symbol"))
        if not key:
            continue
        account_by_key[key] = row
        ts = row.get("synced_at")
        if isinstance(ts, (int, float)):
            synced_at = ts if synced_at is None else max(synced_at, ts)

    settling_keys = _settling_symbol_keys(
        finance_collections,
        user_id=user_id,
        securities_account_id=account_id,
        account_synced_at=synced_at,
    )

    diffs: List[Dict[str, Any]] = []
    settling_diffs: List[Dict[str, Any]] = []
    for key in sorted({*ledger_by_key, *account_by_key}):
        ledger_row = ledger_by_key.get(key) or {}
        account_row = account_by_key.get(key)
        ledger_shares = _int_value(ledger_row.get("shares"))
        # Missing from the broker sync entirely -> treat as unknown, not zero, so
        # we don't raise a false mismatch before the account has ever synced.
        if account_row is None and not account_rows:
            continue
        account_shares = _account_position_qty(account_row) if account_row is not None else 0
        if ledger_shares == account_shares:
            continue
        symbol = ledger_row.get("symbol") or (account_row or {}).get("symbol") or key
        entry = {
            "symbol": symbol,
            "name": ledger_row.get("name") or (account_row or {}).get("name") or "",
            "ledger_shares": ledger_shares,
            "account_shares": account_shares,
            "diff": account_shares - ledger_shares,
        }
        if key in settling_keys:
            entry["settling"] = True
            settling_diffs.append(entry)
        else:
            diffs.append(entry)

    return {
        "applicable": True,
        "in_sync": not diffs,
        "securities_account_id": account_id,
        "account_synced_at": synced_at,
        "diffs": diffs,
        "settling_diffs": settling_diffs,
    }


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
            return _lineage_live_holdings(
                trading_collections, finance_collections, data_collections, plan, user_id
            )
        snapshot = _latest_lineage_paper_snapshot(trading_collections, plan, user_id)
        return [
            row
            for row in (snapshot.get("positions") or [])
            if _int_value(row.get("shares")) > 0
        ]
    except Exception:
        return []


def _live_signal_is_blocked(item: Dict[str, Any]) -> bool:
    """Whether a discretionary live order should abort publishing.

    Sells abort only on hard account-config blockers; buys abort on any blocker.
    """
    blockers = item.get("blockers") or []
    if not blockers:
        return False
    if str(item.get("action")) == "sell":
        return any(blocker in LIVE_ACTION_HARD_BLOCKERS for blocker in blockers)
    return True


@router.post("/plans/{plan_id}/live-rebalance")
async def live_rebalance_portfolio(
    plan_id: str,
    request: LiveRebalanceRequest,
    current_user: dict = Depends(get_current_user),
):
    """One-click intraday LIVE position change (reduce / clear / add) for a lineage.

    A discretionary, human-driven rebalance: reads the lineage's live holdings
    (not the paper snapshot), computes per-symbol deltas from ``targets``, creates
    a manual (origin=manual) plan for audit, and publishes buy/sell live signals
    straight into ``trade_signals`` synchronously. The quantTrader loop (poll ~1s)
    picks them up and submits to the broker within the session, so a discretionary
    "trade now" no longer waits for the next-day open cron. Liquidation is just
    every held symbol mapped to 0.
    """
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)

    plan = _require_plan(trading_collections, plan_id, current_user)
    if not request.targets:
        raise HTTPException(status_code=400, detail="targets must include at least one symbol")
    account = _require_securities_account(finance_collections, request.securities_account_id, user_id)

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    holdings = _lineage_live_holdings(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    current_by_key: Dict[str, Dict[str, Any]] = {}
    for row in holdings:
        current_by_key[_symbol_key(row.get("symbol"))] = row
    securities_account_id = str(account.get("_id") or request.securities_account_id)
    account_positions_by_key, account_positions_synced = _account_positions_by_symbol(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
    )

    # Resolve a price for every symbol we may trade (held + newly-added targets).
    target_symbols = {str(s) for s in request.targets.keys() if s}
    all_symbols = sorted(target_symbols | {str(row.get("symbol")) for row in holdings})
    price_map, _realtime_docs = _load_fresh_market_prices(data_collections, all_symbols)

    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100

    now = datetime.utcnow()
    base_date = datetime.now().strftime("%Y%m%d")
    stamp = now.strftime("%Y%m%d%H%M%S")
    new_plan_id = f"{strategy_template_id}_{(params_hash or 'na')[:8]}_{user_id}_liverb_{stamp}{uuid.uuid4().hex[:6]}"

    items: List[Dict[str, Any]] = []
    manual_exclude_symbols: List[str] = []
    estimated_buy = 0.0
    estimated_sell = 0.0
    rank = 0
    for symbol in sorted(target_symbols):
        symbol_key = _symbol_key(symbol)
        held = current_by_key.get(symbol_key) or {}
        current_shares = _int_value(held.get("shares"))
        target_shares, blockers = _normalize_live_target_shares(current_shares, request.targets[symbol], lot_size)
        delta = target_shares - current_shares
        account_pos = account_positions_by_key.get(symbol_key) or {"current": 0, "available": 0}
        account_current_shares = account_pos["current"] if account_positions_synced else None
        account_available_shares = account_pos["available"] if account_positions_synced else None
        if delta < 0 and account_positions_synced:
            if account_current_shares <= 0 or account_available_shares <= 0:
                # The broker account has synced and says this symbol is not
                # sellable. The lineage ledger may be stale after external
                # clean-up, so never generate a sell for shares the account
                # does not currently hold.
                continue
            sell_qty = abs(delta)
            capped_sell_qty = min(sell_qty, account_available_shares)
            if capped_sell_qty <= 0:
                continue
            if capped_sell_qty < sell_qty:
                delta = -capped_sell_qty
                target_shares = current_shares + delta
                blockers.append("account_position_capped")
        if delta == 0:
            continue
        rank += 1
        price = (
            _numeric(price_map.get(symbol))
            or _numeric(price_map.get(_symbol_key(symbol)))
            or _numeric(held.get("last_price"))
            or None
        )
        item_blockers = list(blockers)
        if not price:
            item_blockers.append("missing_price")
        if delta > 0:
            action = "buy"
            estimated_buy += abs(delta) * float(price or 0)
        else:
            action = "sell"
            estimated_sell += abs(delta) * float(price or 0)
        if target_shares < current_shares and request.exclude_after:
            manual_exclude_symbols.append(symbol)
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
                "name": held.get("name", ""),
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "drift_delta_shares": delta,
                "account_current_shares": account_current_shares,
                "account_available_shares": account_available_shares,
                "estimated_price": price,
                "estimated_amount": abs(delta) * float(price or 0) if price else None,
                "blockers": item_blockers,
                "created_at": now,
            }
        )

    if not items:
        raise HTTPException(status_code=400, detail="targets produced no executable changes after lot-size normalization")

    is_liquidation = all(item["action"] == "sell" and item["target_shares"] == 0 for item in items)
    manual_action = "liquidate" if is_liquidation else "rebalance"

    plan_doc = {
        "plan_id": new_plan_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "preset_id": plan.get("preset_id"),
        "params_hash": params_hash,
        "params_snapshot": params_snapshot,
        "params_override": plan.get("params_override") or {},
        "strategy_name": plan.get("strategy_name"),
        "base_date": base_date,
        "execute_date": base_date,
        "plan_type": "rebalance",
        "record_kind": RECORD_KIND_TRADE_PLAN,
        "origin": "manual",
        "manual_action": manual_action,
        "is_liquidation": is_liquidation,
        "is_rebalance_day": True,
        "monitor_can_trade": False,
        "trading_enabled": True,
        "executable": True,
        "non_executable_reason": None,
        "exclude_after": bool(request.exclude_after),
        "manual_exclude_symbols": sorted(set(manual_exclude_symbols)),
        "status": "approved",
        "approved_by": user_id,
        "approved_at": now,
        "anchor_plan_id": plan_id,
        "summary": {
            "buy_count": sum(1 for item in items if item["action"] == "buy"),
            "sell_count": sum(1 for item in items if item["action"] == "sell"),
            "hold_count": 0,
            "skip_count": 0,
            "estimated_buy_amount": estimated_buy,
            "estimated_sell_amount": estimated_sell,
            "manual_action": manual_action,
        },
        "created_at": now,
        "updated_at": now,
    }

    signals: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    for item in items:
        try:
            signal = build_portfolio_plan_live_signal(plan=plan_doc, item=item, account=account, now=now)
        except Exception as exc:  # noqa: BLE001
            skipped_items.append({"symbol": item.get("symbol"), "reason": str(exc)})
            continue
        if signal is not None:
            signals.append(signal.to_mongo())

    if not signals:
        raise HTTPException(
            status_code=409,
            detail={"message": "no live signals could be built", "skipped_items": skipped_items},
        )

    risk_report = _build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=signals,
        user_id=user_id,
    )
    blocked = [item for item in risk_report["items"] if _live_signal_is_blocked(item)]
    if blocked and not request.dry_run:
        raise HTTPException(
            status_code=409,
            detail={"message": "live rebalance blocked by risk check", "risk_report": risk_report},
        )

    order_ids = [signal["order_id"] for signal in signals]
    existing_signals = list(
        finance_collections["trade_signals"].find(active_signal_query_for_order_ids(user_id, order_ids))
    ) if order_ids else []
    existing_order_ids = {row.get("order_id") for row in existing_signals}
    new_signals = [signal for signal in signals if signal.get("order_id") not in existing_order_ids]

    changed_symbols = sorted({item["symbol"] for item in items})
    if request.dry_run:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "dry_run": True,
                    "plan_id": new_plan_id,
                    "plan": plan_doc,
                    "items": items,
                    "signals": signals,
                    "new_signals": new_signals,
                    "existing_signals": existing_signals,
                    "skipped_items": skipped_items,
                    "risk_report": risk_report,
                    "manual_action": manual_action,
                    "changed_symbols": changed_symbols,
                    "blocked": bool(blocked),
                }
            ),
        }

    trading_collections["portfolio_trade_plans"].insert_one(dict(plan_doc))
    trading_collections["portfolio_trade_plan_items"].insert_many([dict(item) for item in items])

    inserted_count = 0
    if new_signals:
        finance_collections["trade_signals"].insert_many(new_signals)
        inserted_count = len(new_signals)
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": new_plan_id, "user_id": user_id},
        {
            "$set": {
                "live_signals_published_at": now,
                "live_signal_count": len(signals),
                "live_signal_inserted_count": inserted_count,
                "live_signal_status_summary": {
                    "pending": inserted_count,
                    **summarize_signal_statuses(existing_signals),
                },
            }
        },
    )

    # Forward-looking exclusion only; pausing the lineage waits until the sells
    # actually settle (mirrors publish-live-signals, see Error 8 in design notes).
    final_shares_by_key = {_symbol_key(row.get("symbol")): _int_value(row.get("shares")) for row in holdings}
    for item in items:
        final_shares_by_key[_symbol_key(item["symbol"])] = _int_value(item["target_shares"])
    lineage_effects = _apply_manual_lineage_effects_native(
        trading_collections,
        plan_doc,
        final_positions=[{"symbol": key, "shares": shares} for key, shares in final_shares_by_key.items()],
        acted_by=user_id,
        pause_on_empty=False,
    )

    return {
        "success": True,
        "data": _jsonable(
            {
                "dry_run": False,
                "plan_id": new_plan_id,
                "manual_action": manual_action,
                "signals": signals,
                "new_signals": new_signals,
                "existing_signals": existing_signals,
                "skipped_items": skipped_items,
                "risk_report": risk_report,
                "inserted_count": inserted_count,
                "existing_count": len(existing_signals),
                "changed_symbols": changed_symbols,
                "blocked": bool(blocked),
                "lineage_effects": lineage_effects,
            }
        ),
    }


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
    base_date = datetime.now().strftime("%Y%m%d")
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


@router.post("/plans/{plan_id}/paper-rebalance")
async def paper_rebalance_portfolio(
    plan_id: str,
    request: PaperRebalanceRequest,
    current_user: dict = Depends(get_current_user),
):
    """One-click intraday PAPER position change (reduce / clear / add) for a lineage.

    The paper-book mirror of ``live-rebalance``: computes per-symbol deltas from
    ``targets`` against the latest lineage paper snapshot, fills them immediately
    at the realtime price (no next-day-open cron), and writes the new paper
    position snapshot / equity / executions plus a manual (origin=manual) plan for
    audit. Liquidation is just every held symbol mapped to 0.
    """
    trading_collections = get_trading_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)

    plan = _require_plan(trading_collections, plan_id, current_user)
    if not request.targets:
        raise HTTPException(status_code=400, detail="targets must include at least one symbol")

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100
    initial_capital = _numeric(params_snapshot.get("initial_capital"), 1_000_000.0)

    now = datetime.utcnow()
    base_date = datetime.now().strftime("%Y%m%d")

    inherited = _latest_lineage_position_snapshot(trading_collections, plan, user_id, base_date)
    positions: Dict[str, Dict[str, Any]] = {
        str(row.get("symbol")): dict(row)
        for row in (inherited.get("positions") or [])
        if row.get("symbol")
    }
    cash = _numeric(inherited.get("cash"), initial_capital)

    target_symbols = {str(s) for s in request.targets.keys() if s}
    all_symbols = sorted(target_symbols | set(positions.keys()))
    price_map, _realtime_docs = _load_fresh_market_prices(data_collections, all_symbols)

    def _price_for(symbol: str, pos: Dict[str, Any]) -> Optional[float]:
        return (
            _numeric(price_map.get(symbol))
            or _numeric(price_map.get(_symbol_key(symbol)))
            or _numeric(pos.get("last_price"))
            or _numeric(pos.get("mark_price"))
            or _numeric(pos.get("cost_basis"))
            or None
        )

    items: List[Dict[str, Any]] = []
    manual_exclude_symbols: List[str] = []
    estimated_buy = 0.0
    estimated_sell = 0.0
    rank = 0
    for symbol in sorted(target_symbols):
        pos = positions.get(symbol, {})
        current_shares = _int_value(pos.get("shares"))
        target_shares, blockers = _normalize_live_target_shares(current_shares, request.targets[symbol], lot_size)
        delta = target_shares - current_shares
        if delta == 0:
            continue
        rank += 1
        price = _price_for(symbol, pos)
        item_blockers = list(blockers)
        if not price:
            item_blockers.append("missing_price")
        action = "buy" if delta > 0 else "sell"
        if action == "buy":
            estimated_buy += abs(delta) * float(price or 0)
        else:
            estimated_sell += abs(delta) * float(price or 0)
        if target_shares < current_shares and request.exclude_after:
            manual_exclude_symbols.append(symbol)
        items.append(
            {
                "plan_id": None,  # filled after plan_id is built
                "strategy_template_id": strategy_template_id,
                "user_id": user_id,
                "params_hash": params_hash,
                "base_date": base_date,
                "execute_date": base_date,
                "rank": rank,
                "symbol": symbol,
                "name": pos.get("name", ""),
                "industry": pos.get("industry", ""),
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "drift_delta_shares": delta,
                "estimated_price": price,
                "estimated_amount": abs(delta) * float(price or 0) if price else None,
                "blockers": item_blockers,
                "created_at": now,
            }
        )

    if not items:
        raise HTTPException(status_code=400, detail="targets produced no executable changes after lot-size normalization")

    is_liquidation = all(item["action"] == "sell" and item["target_shares"] == 0 for item in items)
    manual_action = "liquidate" if is_liquidation else "rebalance"

    stamp = now.strftime("%Y%m%d%H%M%S")
    new_plan_id = f"{strategy_template_id}_{(params_hash or 'na')[:8]}_{user_id}_paperrb_{stamp}{uuid.uuid4().hex[:6]}"
    for item in items:
        item["plan_id"] = new_plan_id

    plan_doc = {
        "plan_id": new_plan_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "preset_id": plan.get("preset_id"),
        "params_hash": params_hash,
        "params_snapshot": params_snapshot,
        "params_override": plan.get("params_override") or {},
        "strategy_name": plan.get("strategy_name"),
        "base_date": base_date,
        "execute_date": base_date,
        "plan_type": "rebalance",
        "record_kind": RECORD_KIND_TRADE_PLAN,
        "origin": "manual",
        "manual_action": manual_action,
        "is_liquidation": is_liquidation,
        "is_rebalance_day": True,
        "monitor_can_trade": False,
        "trading_enabled": True,
        "executable": True,
        "non_executable_reason": None,
        "exclude_after": bool(request.exclude_after),
        "manual_exclude_symbols": sorted(set(manual_exclude_symbols)),
        "anchor_plan_id": plan_id,
        "created_at": now,
        "updated_at": now,
    }

    # Simulate fills immediately at the realtime price.
    work_positions = {sym: dict(pos) for sym, pos in positions.items()}
    work_cash = cash
    executions: List[Dict[str, Any]] = []
    partial = False
    # Sell first, then buy: credit same-day sell proceeds before buys so a
    # rank-leading buy is not starved of cash its funding sells would provide
    # (avoids spurious partial). sorted() is stable, preserving rank order
    # within each side.
    for item in sorted(items, key=lambda it: 0 if int(it.get("delta_shares", 0) or 0) < 0 else 1):
        symbol = item["symbol"]
        delta = int(item["delta_shares"])
        price = item.get("estimated_price")
        current = work_positions.get(
            symbol,
            {"symbol": symbol, "name": item.get("name", ""), "industry": item.get("industry", ""), "shares": 0, "cost_basis": 0.0},
        )
        if not price:
            executions.append(_execution_doc(plan_doc, item, "blocked", 0, 0.0, 0.0, "missing_price", now))
            partial = True
            continue
        if delta < 0:
            quantity = min(abs(delta), int(current.get("shares", 0) or 0))
            amount = quantity * price
            work_cash += amount
            current["shares"] = int(current.get("shares", 0) or 0) - quantity
            executions.append(_execution_doc(plan_doc, item, "sell", quantity, price, amount, None, now))
        else:
            quantity = delta
            amount = quantity * price
            if amount > work_cash:
                quantity = (int(work_cash // price) // max(1, lot_size)) * max(1, lot_size)
                amount = quantity * price
                partial = True
            if quantity <= 0:
                executions.append(_execution_doc(plan_doc, item, "blocked", 0, price, 0.0, "insufficient_cash", now))
                partial = True
                continue
            work_cash -= amount
            old_shares = int(current.get("shares", 0) or 0)
            old_cost = float(current.get("cost_basis", 0) or 0)
            current["cost_basis"] = ((old_shares * old_cost) + amount) / (old_shares + quantity) if old_shares + quantity else price
            current["shares"] = old_shares + quantity
            executions.append(_execution_doc(plan_doc, item, "buy", quantity, price, amount, None, now))
        if int(current.get("shares", 0) or 0) > 0:
            work_positions[symbol] = current
        elif symbol in work_positions:
            del work_positions[symbol]

    position_rows = []
    market_value = 0.0
    for symbol, pos in sorted(work_positions.items()):
        mark_price = _price_for(symbol, pos) or float(pos.get("cost_basis", 0) or 0)
        value = int(pos.get("shares", 0) or 0) * mark_price
        market_value += value
        position_rows.append({**pos, "mark_price": mark_price, "market_value": value})
    equity = work_cash + market_value
    status = "partially_executed" if partial else "executed_paper"

    snapshot = {
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "params_hash": params_hash,
        "as_of_date": base_date,
        "plan_id": new_plan_id,
        "inherited_from_plan_id": inherited.get("plan_id"),
        "inherited_from_as_of_date": inherited.get("as_of_date"),
        "cash": work_cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }

    changed_symbols = sorted({item["symbol"] for item in items})
    if request.dry_run:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "dry_run": True,
                    "plan_id": new_plan_id,
                    "plan": plan_doc,
                    "items": items,
                    "executions": executions,
                    "position_snapshot": snapshot,
                    "manual_action": manual_action,
                    "changed_symbols": changed_symbols,
                    "status": status,
                }
            ),
        }

    plan_doc["status"] = status
    plan_doc["paper_executed_at"] = now
    trading_collections["portfolio_trade_plans"].insert_one(dict(plan_doc))
    trading_collections["portfolio_trade_plan_items"].insert_many([dict(item) for item in items])
    if executions:
        trading_collections["portfolio_paper_executions"].insert_many([dict(ex) for ex in executions])
    trading_collections["portfolio_paper_positions"].replace_one(
        {"plan_id": new_plan_id, "as_of_date": base_date, "user_id": user_id}, snapshot, upsert=True
    )
    trading_collections["portfolio_paper_equity"].replace_one(
        {"plan_id": new_plan_id, "date": base_date, "user_id": user_id},
        {
            "strategy_template_id": strategy_template_id,
            "user_id": user_id,
            "params_hash": params_hash,
            "date": base_date,
            "plan_id": new_plan_id,
            "inherited_from_plan_id": inherited.get("plan_id"),
            "inherited_from_as_of_date": inherited.get("as_of_date"),
            "equity": equity,
            "cash": work_cash,
            "market_value": market_value,
            "updated_at": now,
        },
        upsert=True,
    )
    lineage_effects = _apply_manual_lineage_effects_native(
        trading_collections,
        plan_doc,
        final_positions=position_rows,
        acted_by=user_id,
    )

    return {
        "success": True,
        "data": _jsonable(
            {
                "dry_run": False,
                "plan_id": new_plan_id,
                "status": status,
                "manual_action": manual_action,
                "executions": executions,
                "position_snapshot": snapshot,
                "changed_symbols": changed_symbols,
                "lineage_effects": lineage_effects,
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
        base_date = datetime.now().strftime("%Y%m%d")
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
    actionable = _portfolio_llm_actionable_items(items, request)
    if not actionable:
        detail = (
            "no bench candidates matched LLM risk review filters"
            if request.target_scope == "bench"
            else "no buy/hold plan items matched LLM risk review filters"
        )
        raise HTTPException(status_code=400, detail=detail)

    groups = _portfolio_llm_industry_groups(actionable)
    industry_context = _portfolio_llm_industry_context(groups)
    run_id = str(uuid.uuid4())
    base_date = (
        _normalize_yyyymmdd(plan.get("base_date"))
        or _normalize_yyyymmdd(plan.get("score_date"))
        or datetime.now().strftime("%Y%m%d")
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
    # Starters mirror the actual current holdings (live if the lineage has any
    # live fills, otherwise the latest paper snapshot) so the lineup stays in
    # sync with 最新持仓 across the whole rebalance cadence.
    holdings = _current_lineage_holdings(
        collections, finance_collections, data_collections, plan, user_id
    )
    payload = _build_plan_bench_payload(
        plan,
        holdings,
        bench_multiplier=bench_multiplier,
        data_collections=data_collections,
    )
    payload = _enrich_bench_payload_with_llm_risk(plan=plan, payload=payload, user_id=user_id)
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
        or datetime.now().strftime("%Y%m%d")
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


@router.get("/plans/{plan_id}/holdings-reconcile")
@_offload
def reconcile_portfolio_holdings(
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Compare the system ledger holdings with the broker's synced positions.

    Live-only: returns per-symbol diffs so the overview can warn the moment the
    book drifts from the real miniQMT account.
    """
    collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(collections, plan_id, current_user)
    user_id = _current_user_id(current_user)
    try:
        payload = _reconcile_live_holdings(
            collections, finance_collections, data_collections, plan, user_id
        )
    except Exception:
        payload = {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}
    return {"success": True, "data": _jsonable(payload)}


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


@router.get("/plans/{plan_id}/executions")
@_offload
def get_portfolio_plan_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id, current_user)
    rows = list(collections["portfolio_paper_executions"].find({"plan_id": plan_id, "user_id": _current_user_id(current_user)}).sort("created_at", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/equity")
@_offload
def get_portfolio_plan_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id, current_user)
    query: Dict[str, Any] = {"plan_id": plan_id, "user_id": _current_user_id(current_user)}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/lineage-equity")
@_offload
def get_portfolio_plan_lineage_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    query = _lineage_query_for_plan(collections, plan, user_id)
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    equity_rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    position_query = dict(query)
    if "date" in position_query:
        position_query["as_of_date"] = position_query.pop("date")
    position_rows = list(collections["portfolio_paper_positions"].find(position_query).sort("as_of_date", 1))
    rows_by_key: Dict[tuple, Dict[str, Any]] = {}
    for row in equity_rows:
        key = (row.get("plan_id"), row.get("date"))
        rows_by_key[key] = dict(row)
    for row in position_rows:
        date = row.get("as_of_date")
        key = (row.get("plan_id"), date)
        if key in rows_by_key or not date:
            continue
        rows_by_key[key] = {
            "strategy_template_id": row.get("strategy_template_id"),
            "user_id": row.get("user_id"),
            "params_hash": row.get("params_hash"),
            "date": date,
            "plan_id": row.get("plan_id"),
            "inherited_from_plan_id": row.get("inherited_from_plan_id"),
            "inherited_from_as_of_date": row.get("inherited_from_as_of_date"),
            "equity": row.get("equity"),
            "cash": row.get("cash"),
            "market_value": row.get("market_value"),
            "updated_at": row.get("updated_at"),
            "source": "position_snapshot",
        }
    latest_by_date: Dict[str, Dict[str, Any]] = {}
    for row in rows_by_key.values():
        date = str(row.get("date") or "")
        if not date:
            continue
        existing = latest_by_date.get(date)
        if existing is None or str(row.get("updated_at") or "") >= str(existing.get("updated_at") or ""):
            latest_by_date[date] = row
    rows = sorted(latest_by_date.values(), key=lambda row: str(row.get("date") or ""))
    plan_ids = sorted({row.get("plan_id") for row in rows if row.get("plan_id")})
    plan_map = {
        row.get("plan_id"): row
        for row in collections["portfolio_trade_plans"].find(
            {"plan_id": {"$in": plan_ids}, "user_id": user_id},
            {"_id": 0, "plan_id": 1, "base_date": 1, "plan_type": 1, "status": 1},
        )
    } if plan_ids else {}
    enriched = []
    for row in rows:
        source_plan = plan_map.get(row.get("plan_id"), {})
        enriched.append(
            {
                **row,
                "source_plan_base_date": source_plan.get("base_date"),
                "source_plan_type": source_plan.get("plan_type"),
                "source_plan_status": source_plan.get("status"),
                "is_current_plan": row.get("plan_id") == plan_id,
            }
        )
    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "rows": enriched,
                "source_plan_ids": plan_ids,
                "initial_capital": _lineage_initial_capital(plan),
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-paper-positions")
@_offload
def get_lineage_paper_positions(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Latest paper position snapshot across the plan's params_hash lineage."""

    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(collections, plan_id, current_user)
    snapshot = _latest_lineage_paper_snapshot(collections, plan, user_id)
    positions: List[Dict[str, Any]] = []
    total_market_value = 0.0
    for row in sorted(snapshot.get("positions") or [], key=lambda item: str(item.get("symbol") or "")):
        shares = _int_value(row.get("shares"))
        if shares <= 0:
            continue
        market_value = _numeric(row.get("market_value"))
        total_market_value += market_value
        avg_cost = _numeric(row.get("cost_basis") or row.get("mark_price"))
        last_price = _numeric(row.get("mark_price") or row.get("cost_basis"))
        positions.append(
            {
                "symbol": row.get("symbol"),
                "name": row.get("name", ""),
                "shares": shares,
                "avg_cost": avg_cost,
                "last_price": last_price,
                "market_value": market_value,
                "realized_pnl": 0.0,
                "unrealized_pnl": (last_price - avg_cost) * shares if last_price and avg_cost else 0.0,
                "unrealized_pnl_pct": last_price / avg_cost - 1 if last_price > 0 and avg_cost > 0 else None,
            }
        )
    equity = _numeric(snapshot.get("equity"))
    cash = _numeric(snapshot.get("cash"))
    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "as_of_date": snapshot.get("as_of_date"),
                "source_plan_id": snapshot.get("plan_id"),
                "positions": positions,
                "summary": {
                    "holding_count": len(positions),
                    "total_market_value": total_market_value,
                    "total_realized_pnl": 0.0,
                    "total_unrealized_pnl": sum(_numeric(row.get("unrealized_pnl")) for row in positions),
                    "equity": equity,
                    "cash": cash,
                },
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-paper-executions")
@_offload
def get_lineage_paper_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    """FIFO round-trip trade rows from paper executions across the plan lineage."""

    trading_collections = get_trading_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    lineage_ids = _lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "executions": [],
                    "trades": [],
                    "execution_count": 0,
                    "trade_count": 0,
                }
            ),
        }

    # Source of truth is ``portfolio_paper_executions`` (the actual paper fills):
    # its prices match each holding's cost basis and its quantities reflect
    # partial/blocked fills. Inheritance is now scoped by params_hash and the
    # rebuild tool repaired historical lineages, so executions once again carry
    # the full buy/sell history rather than a stale all-buy establishment.
    execution_rows = list(
        trading_collections["portfolio_paper_executions"]
        .find({"user_id": user_id, "plan_id": {"$in": lineage_ids}})
        .sort([("execute_date", 1), ("created_at", 1)])
    )
    plan_rows = list(
        trading_collections["portfolio_trade_plans"].find(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}},
            {"_id": 0, "plan_id": 1, "base_date": 1},
        )
    )
    plan_meta = {str(row.get("plan_id")): dict(row) for row in plan_rows if row.get("plan_id")}
    merged = merge_lineage_paper_executions(execution_rows, plan_meta)
    symbols = sorted({str(row.get("symbol")) for row in merged if row.get("symbol")})
    prices, _realtime_docs = _load_fresh_market_prices(data_collections, symbols)
    name_map = {
        str(row.get("symbol")): str(row.get("name") or "")
        for row in merged
        if row.get("symbol") and row.get("name")
    }
    for symbol, name in list(name_map.items()):
        name_map.setdefault(symbol.split(".", 1)[0], name)
    trades = build_lineage_paper_trades(merged, prices, name_map=name_map)

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": lineage_ids,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "trade_source": "paper_executions",
                "executions": merged,
                "trades": trades,
                "execution_count": len(merged),
                "trade_count": len(trades),
            }
        ),
    }


@router.get("/plans/{plan_id}/live-executions")
@_offload
def get_portfolio_plan_live_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    _require_plan(trading_collections, plan_id, current_user)
    user_id = _current_user_id(current_user)

    signal_query = {
        "user_id": user_id,
        "plan_id": plan_id,
        "mode": "live",
    }
    signals = _attach_signal_names(data_collections, [add_display_time_fields(row) for row in finance_collections["trade_signals"].find(signal_query).sort("created_at", 1)])
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    execution_query = {"user_id": user_id, "order_id": {"$in": order_ids}} if order_ids else None
    executions = (
        [add_display_time_fields(row) for row in finance_collections["trade_executions"].find(execution_query).sort("timestamp", -1)]
        if execution_query
        else []
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "signals": signals,
                "executions": executions,
                "signal_count": len(signals),
                "execution_count": len(executions),
            }
        ),
    }


@router.get("/plans/{plan_id}/live-equity")
@_offload
def get_portfolio_plan_live_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id, current_user)
    rows = list_live_plan_equity(
        collections,
        user_id=_current_user_id(current_user),
        plan_id=plan_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/live-realtime-equity")
@_offload
def get_portfolio_plan_live_realtime_equity(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(trading_collections, plan_id, current_user)
    row = compute_live_plan_realtime_equity(
        trading_collections,
        finance_collections,
        data_collections,
        user_id=_current_user_id(current_user),
        plan_id=plan_id,
        plan=plan,
    )
    return {"success": True, "data": _jsonable(row)}


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

    total = len(plans)
    page_plans = plans[offset : offset + limit]
    page_plan_ids = [str(row.get("plan_id")) for row in page_plans if row.get("plan_id")]
    page_items_by_plan = _timeline_items_by_plan(trading_collections, page_plan_ids, user_id)
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
        "current_cycle": _compute_current_cycle(
            cadence_plan, latest_rebalance, latest_monitor, data_collections
        ),
        "today_state": _plan_today_state_label(headline_plan),
        "action_required": _plan_action_required(headline_plan),
        "latest_drift_summary": latest_drift_summary,
        "timeline": timeline,
        "timeline_pagination": {"total": total, "limit": limit, "offset": offset},
    }


@router.get("/portfolios")
@_offload
def list_portfolios(current_user: dict = Depends(get_current_user)):
    """Enumerate live and paper portfolio lineages (strategy_template_id + params_hash)."""

    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    user_id = _current_user_id(current_user)
    portfolios = _list_portfolios_payload(trading_collections, finance_collections, user_id)
    return {"success": True, "data": _jsonable({"portfolios": portfolios})}


@router.get("/plans/{plan_id}/portfolio-summary")
@_offload
def get_portfolio_plan_summary(
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Deferred portfolio summary: paper snapshot, holdings, equity, approval policy."""

    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    summary = _build_portfolio_summary_payload(
        trading_collections, finance_collections, plan, user_id
    )
    return {"success": True, "data": _jsonable(summary)}


@router.get("/live-portfolios")
@_offload
def list_live_portfolios(current_user: dict = Depends(get_current_user)):
    """Enumerate live-traded plan lineages (strategy_template_id + params_hash) for the user."""

    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    user_id = _current_user_id(current_user)
    portfolios = [
        row for row in _list_portfolios_payload(trading_collections, finance_collections, user_id)
        if row.get("execution_venue") == "live"
    ]
    return {"success": True, "data": _jsonable({"portfolios": portfolios})}


@router.get("/plans/{plan_id}/lineage-timeline")
@_offload
def get_portfolio_plan_lineage_timeline(
    plan_id: str,
    limit: int = Query(default=60, ge=1, le=240),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    payload = _build_lineage_timeline_payload(
        trading_collections,
        finance_collections,
        data_collections,
        plan,
        user_id,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "data": _jsonable(payload)}


@router.get("/plans/{plan_id}/lineage-live-executions")
@_offload
def get_lineage_live_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)

    snapshot, overlay = _load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if not snapshot:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "signals": [],
                    "executions": [],
                    "trades": [],
                    "signal_count": 0,
                    "execution_count": 0,
                    "trade_count": 0,
                }
            ),
        }

    trades = overlay.get("trades") or []
    watermark = snapshot.get("watermark") or {}
    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") or [],
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "signals": [],
                "executions": [],
                "trades": trades,
                "signal_count": int(watermark.get("signal_count") or 0),
                "execution_count": int(watermark.get("execution_count") or 0),
                "trade_count": len(trades),
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-live-positions")
@_offload
def get_lineage_live_positions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)

    snapshot, overlay = _load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if not snapshot:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "positions": [],
                    "summary": {
                        "total_market_value": 0.0,
                        "total_realized_pnl": 0.0,
                        "total_unrealized_pnl": 0.0,
                        "total_cost_basis": 0.0,
                        "holding_count": 0,
                        "position_row_count": 0,
                    },
                    "latest_price_update": "",
                }
            ),
        }

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") or [],
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "positions": overlay.get("positions") or [],
                "summary": overlay.get("summary") or {},
                "latest_price_update": overlay.get("latest_price_update") or "",
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-live-equity")
@_offload
def get_lineage_live_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Return daily account equity for the live account(s) behind this lineage."""

    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)

    chart_payload = _build_lineage_live_equity_chart(
        trading_collections,
        finance_collections,
        plan,
        user_id,
        plan_id,
        start_date=start_date,
        end_date=end_date,
    )
    snapshot, overlay = _load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if snapshot and overlay.get("current_book_equity"):
        current_book_equity = overlay["current_book_equity"]
    else:
        rows_for_chart = chart_payload.get("rows") or []
        row_source = chart_payload.get("row_source") or ""
        latest_equity_row = rows_for_chart[-1] if rows_for_chart else {}
        initial_capital = _lineage_initial_capital(plan)
        current_equity = _numeric(latest_equity_row.get("equity"))
        current_cash = _numeric(latest_equity_row.get("cash"))
        current_market_value = _numeric(latest_equity_row.get("market_value"))
        if row_source == "portfolio_live_equity_legacy":
            current_equity = initial_capital
            current_cash = initial_capital
            current_market_value = 0.0
        execution_count = 0 if row_source == "portfolio_live_equity_legacy" else None
        current_book_equity = {
            "equity": current_equity,
            "cash": current_cash,
            "market_value": current_market_value,
            "initial_capital": initial_capital,
            "lineage_plan_ids": chart_payload.get("lineage_plan_ids") or [],
            "execution_count": execution_count,
            "latest_price_update": latest_equity_row.get("updated_at"),
            "source": row_source,
        }

    return {
        "success": True,
        "data": _jsonable({**chart_payload, "current_book_equity": current_book_equity}),
    }


@router.get("/plans/{plan_id}/live-summary")
@_offload
def get_portfolio_plan_live_summary(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Consolidated live lineage snapshot for overview (positions, trades, equity book)."""

    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)

    chart_payload = _build_lineage_live_equity_chart(
        trading_collections, finance_collections, plan, user_id, plan_id
    )
    snapshot, overlay = _load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    reconcile_payload: Dict[str, Any]
    try:
        reconcile_payload = _reconcile_live_holdings(
            trading_collections, finance_collections, data_collections, plan, user_id, snapshot=snapshot
        )
    except Exception:
        reconcile_payload = {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}

    current_book_equity = overlay.get("current_book_equity")
    if not current_book_equity:
        rows_for_chart = chart_payload.get("rows") or []
        latest_equity_row = rows_for_chart[-1] if rows_for_chart else {}
        initial_capital = _lineage_initial_capital(plan)
        current_book_equity = {
            "equity": _numeric(latest_equity_row.get("equity"), initial_capital),
            "cash": _numeric(latest_equity_row.get("cash"), initial_capital),
            "market_value": _numeric(latest_equity_row.get("market_value")),
            "initial_capital": initial_capital,
            "lineage_plan_ids": chart_payload.get("lineage_plan_ids") or [],
            "execution_count": 0,
            "latest_price_update": latest_equity_row.get("updated_at"),
            "source": chart_payload.get("row_source") or "portfolio_live_equity_legacy",
        }

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") if snapshot else chart_payload.get("lineage_plan_ids"),
                "equity": {
                    "rows": chart_payload.get("rows") or [],
                    "caveat": chart_payload.get("caveat") or "",
                    "current_book_equity": current_book_equity,
                },
                "positions": {
                    "positions": overlay.get("positions") or [],
                    "summary": overlay.get("summary") or {},
                    "latest_price_update": overlay.get("latest_price_update") or "",
                },
                "trades": overlay.get("trades") or [],
                "reconcile": reconcile_payload,
            }
        ),
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


@router.post("/plans/{plan_id}/execute-paper")
async def execute_portfolio_plan_paper(
    plan_id: str,
    request: PaperExecuteRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    _require_plan(collections, plan_id, current_user)
    result = _apply_paper_execution(collections, data_collections, plan_id, force=request.force, user_id=_current_user_id(current_user))
    return {"success": True, "data": _jsonable(result)}


@router.post("/plans/{plan_id}/publish-live-signals")
async def publish_portfolio_plan_live_signals(
    plan_id: str,
    request: PublishLiveSignalsRequest,
    current_user: dict = Depends(get_current_user),
):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    if plan.get("status") != "approved":
        raise HTTPException(status_code=400, detail="plan must be approved before publishing live signals")
    if plan.get("paper_executed_at") or trading_collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id}):
        raise HTTPException(status_code=400, detail="plan already has paper executions; cannot publish live signals")
    account = _require_securities_account(finance_collections, request.securities_account_id, user_id)
    items = list(
        trading_collections["portfolio_trade_plan_items"]
        .find({"plan_id": plan_id, "user_id": user_id})
        .sort("rank", 1)
    )
    # Carry the plan/lineage book holdings onto each item (generation snapshot plus
    # this lineage's own live fills) so repricing sizes deltas against what THIS
    # plan owns, not the shared broker account aggregate.
    items = _attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )
    now = datetime.utcnow()
    account_snapshot = _latest_account_snapshot(finance_collections, account, user_id)
    account_equity = _numeric(account_snapshot.get("total_assets") or account_snapshot.get("total_asset"))
    account_available_cash = _numeric(account_snapshot.get("available_cash") or account_snapshot.get("cash"))
    # Sizing budget comes from the plan's own book (see _resolve_publish_plan_capital);
    # the account snapshot above is kept only for broker-side execution risk checks.
    plan_capital = _resolve_publish_plan_capital(plan)
    plan_equity = plan_capital.get("equity")
    plan_cash = plan_capital.get("cash")
    capital_blockers: List[str] = []
    if plan_equity is None:
        capital_blockers.append("missing_plan_equity")
        plan_equity = 0.0
    if plan_cash is None:
        capital_blockers.append("missing_plan_cash")
        plan_cash = 0.0
    securities_account_id = str(account.get("_id") or request.securities_account_id)
    # NOTE (phase-1 basis seam): equity/cash are plan-book scoped, but current_shares
    # below still come from broker account positions. They coincide when one account
    # serves a single lineage; if the account also holds the same symbol for other
    # plans, delta/sell-proceeds may diverge from the plan book. Switching
    # current_shares to lineage-book positions is deferred to phase 2.
    account_positions_by_key, account_positions_synced = _account_positions_by_symbol(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
    )
    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100
    symbols = sorted({str(item.get("symbol")) for item in items if item.get("symbol")})
    quotes = _market_price_quotes(data_collections, symbols)
    repriced_items, reprice_summary = reprice_plan_items_for_publish(
        plan=plan,
        items=items,
        quotes=quotes,
        equity=plan_equity,
        lot_size=lot_size,
        account_positions_by_key=account_positions_by_key,
        account_positions_synced=account_positions_synced,
        normalize_live_target_shares=_normalize_live_target_shares,
        int_value=_int_value,
        numeric=_numeric,
        symbol_key=_symbol_key,
        # plan_cash is always a number (fallback to initial_capital), so the buy
        # cash cap is always active here -- even when the broker account is not
        # synced. This soft-caps buys to the plan budget and tags them with an
        # item-level ``insufficient_cash`` blocker. The broker account's real cash
        # is enforced separately (hard 409) in _build_live_publish_risk_report;
        # the two share the ``insufficient_cash`` label today (TODO: split into
        # plan_budget_capped vs account_cash_insufficient).
        available_cash=plan_cash,
    )
    blockers = set(reprice_summary.get("blockers") or [])
    blockers.update(capital_blockers)
    reprice_summary["blockers"] = sorted(blockers)
    reprice_summary["equity_used"] = float(plan_equity)
    reprice_summary["plan_equity_used"] = float(plan_equity)
    reprice_summary["plan_cash_used"] = float(plan_cash)
    reprice_summary["plan_equity_source"] = plan_capital.get("equity_source")
    reprice_summary["plan_cash_source"] = plan_capital.get("cash_source")
    reprice_summary["account_equity"] = account_equity
    reprice_summary["account_available_cash"] = account_available_cash
    reprice_summary["capital_basis"] = plan_capital.get("capital_basis")
    if reprice_summary.get("blockers") and not request.dry_run:
        message = "publish repricing blocked"
        blocker_set = set(reprice_summary.get("blockers") or [])
        if blocker_set == {"missing_price"}:
            message = "publish repricing blocked: missing usable prices"
        elif blocker_set.intersection({"missing_plan_equity", "missing_plan_cash"}):
            message = "publish repricing blocked: missing plan capital baseline"
        raise HTTPException(
            status_code=409,
            detail={
                "message": message,
                "reprice_summary": reprice_summary,
            },
        )
    items = repriced_items
    signals = []
    skipped_items = []
    for item in items:
        try:
            signal = build_portfolio_plan_live_signal(plan=plan, item=item, account=account, now=now)
        except Exception as exc:
            skipped_items.append({"symbol": item.get("symbol"), "reason": str(exc)})
            continue
        if signal is not None:
            signals.append(signal.to_mongo())

    if not signals:
        if request.dry_run:
            return {
                "success": True,
                "data": _jsonable(
                    {
                        "plan_id": plan_id,
                        "dry_run": True,
                        "signals": [],
                        "new_signals": [],
                        "existing_signals": [],
                        "skipped_items": skipped_items,
                        "risk_report": {"items": [], "blocked_count": len(reprice_summary.get("blockers") or [])},
                        "reprice_summary": reprice_summary,
                        "inserted_count": 0,
                        "existing_count": 0,
                        "blocked": bool(reprice_summary.get("blockers")),
                    }
                ),
            }
        raise HTTPException(
            status_code=409,
            detail={"message": "no live signals could be built", "skipped_items": skipped_items},
        )

    order_ids = [signal["order_id"] for signal in signals]
    existing_signals = list(
        finance_collections["trade_signals"].find(active_signal_query_for_order_ids(user_id, order_ids))
    ) if order_ids else []
    existing_order_ids = {row.get("order_id") for row in existing_signals}
    new_signals = [signal for signal in signals if signal.get("order_id") not in existing_order_ids]
    risk_report = _build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=new_signals,
        user_id=user_id,
    )
    risk_blocked = [item for item in risk_report["items"] if item["blockers"]]
    if risk_blocked and not request.dry_run:
        raise HTTPException(status_code=409, detail={"message": "live signal risk check failed", "risk_report": risk_report})
    # The response-level ``blocked`` flag must reflect every reason a confirm
    # would 409 (risk items AND reprice/capital blockers), so a dry-run preview
    # never shows "ok" for a publish that would actually fail.
    response_blocked = bool(risk_blocked) or bool(reprice_summary.get("blockers"))

    inserted_count = 0
    if not request.dry_run and new_signals:
        _persist_repriced_plan_items(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            items=repriced_items,
            reprice_summary=reprice_summary,
            now=now,
        )
        _append_plan_operation_log(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            operation_type="publish_reprice",
            status="success",
            message="plan repriced with latest market quotes before live publish",
            payload={
                "equity_used": reprice_summary.get("equity_used"),
                "plan_equity_used": reprice_summary.get("plan_equity_used"),
                "plan_cash_used": reprice_summary.get("plan_cash_used"),
                "account_available_cash": reprice_summary.get("account_available_cash"),
                "turnover_drift_pct": reprice_summary.get("turnover_drift_pct"),
                "stale_price_symbols": reprice_summary.get("stale_price_symbols"),
            },
        )
        finance_collections["trade_signals"].insert_many(new_signals)
        inserted_count = len(new_signals)
        mark_lineage_live(
            trading_collections.get("portfolio_lineage_controls"),
            user_id=user_id,
            strategy_template_id=str(plan.get("strategy_template_id") or ""),
            params_hash=str(plan.get("params_hash") or ""),
            securities_account_id=securities_account_id,
        )
        trading_collections["portfolio_trade_plans"].update_one(
            {"plan_id": plan_id, "user_id": user_id},
            {
                "$set": {
                    "live_signals_published_at": now,
                    "live_signal_count": len(signals),
                    "live_signal_inserted_count": inserted_count,
                    "live_signal_status_summary": {"pending": inserted_count, **summarize_signal_statuses(existing_signals)},
                }
            },
        )
        if plan.get("origin") == "manual":
            # Publishing live signals does NOT mean the orders filled. Only apply the
            # forward-looking exclusion here; pausing the lineage (which would stop
            # daily maintenance of a real book) must wait until fills are confirmed.
            _apply_manual_lineage_effects_native(
                trading_collections,
                plan,
                final_positions=[{"symbol": item.get("symbol"), "shares": _int_value(item.get("target_shares"))} for item in items],
                acted_by=user_id,
                pause_on_empty=False,
            )
        threading.Thread(
            target=lambda: rebuild_live_snapshot_for_plan(
                trading_collections, finance_collections, plan, user_id
            ),
            daemon=True,
        ).start()

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "dry_run": request.dry_run,
                "signals": signals,
                "new_signals": new_signals,
                "existing_signals": existing_signals,
                "skipped_items": skipped_items,
                "risk_report": risk_report,
                "reprice_summary": reprice_summary,
                "inserted_count": inserted_count,
                "existing_count": len(existing_signals),
                "blocked": response_blocked,
            }
        ),
    }


@router.post("/plans/{plan_id}/replan-remainder")
async def replan_portfolio_plan_remainder(
    plan_id: str,
    request: ReplanRemainderRequest,
    current_user: dict = Depends(get_current_user),
):
    """Gap-fill a live plan whose execution fell short (cancelled / partial).

    Computes, against this plan's **lineage book** baseline, the remaining shares
    needed to finish what the plan intended to trade — capped by the planned
    per-position budget and plan-book available cash — and (after dry-run
    confirmation) publishes ``plan_topup`` signals. It never re-selects stocks;
    it only finishes the original selection.
    """
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)

    plan = _require_plan(trading_collections, plan_id, current_user)
    if plan.get("status") != "approved":
        raise HTTPException(status_code=400, detail="plan must be approved to reconcile remainder")
    if not plan.get("live_signals_published_at") and _int_value(plan.get("live_signal_count")) <= 0:
        raise HTTPException(status_code=400, detail="plan has no live signals to reconcile")

    account = _require_securities_account(finance_collections, request.securities_account_id, user_id)
    account_snapshot = _latest_account_snapshot(finance_collections, account, user_id)
    account_synced = bool(account_snapshot)
    broker_available_cash = _numeric(
        account_snapshot.get("available_cash") or account_snapshot.get("cash")
    ) if account_snapshot else None

    cash_snapshot = _resolve_lineage_available_cash_for_remainder(
        trading_collections=trading_collections,
        finance_collections=finance_collections,
        data_collections=data_collections,
        plan=plan,
        user_id=user_id,
    )
    cash_snapshot["broker_available_cash"] = broker_available_cash
    plan_available_cash = cash_snapshot.get("plan_available_cash")
    cash_blockers = list(cash_snapshot.get("blockers") or [])

    items = list(
        trading_collections["portfolio_trade_plan_items"]
        .find({"plan_id": plan_id, "user_id": user_id})
        .sort("rank", 1)
    )
    enriched_items = _attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )

    # Symbols with an in-flight (non-terminal) live signal for this plan: never
    # double-send while an order is still working.
    active_rows = finance_collections["trade_signals"].find(
        {
            "user_id": user_id,
            "plan_id": plan_id,
            "mode": "live",
            "status": {"$in": sorted(ACTIVE_SIGNAL_STATUSES)},
        },
        {"_id": 0, "symbol": 1},
    )
    active_symbols = {_symbol_key(row.get("symbol")) for row in active_rows if row.get("symbol")}

    # Cancelled / terminal originals per symbol, surfaced as replaces_order_ids.
    replaced_rows = finance_collections["trade_signals"].find(
        {
            "user_id": user_id,
            "plan_id": plan_id,
            "mode": "live",
            "status": {"$in": ["cancelled", "canceled", "partial_cancelled", "failed", "rejected"]},
        },
        {"_id": 0, "order_id": 1, "symbol": 1},
    )
    replaces_by_symbol: Dict[str, List[str]] = {}
    for row in replaced_rows:
        sym = _symbol_key(row.get("symbol"))
        if sym and row.get("order_id"):
            replaces_by_symbol.setdefault(sym, []).append(str(row.get("order_id")))

    specs = build_remainder_specs(
        plan=plan,
        enriched_items=enriched_items,
        available_cash=plan_available_cash,
        account_synced=account_synced,
        active_symbols=active_symbols,
        price_resolver=resolve_market_reference_price,
    )

    now = datetime.utcnow()
    signals: List[Dict[str, Any]] = []
    for spec in specs:
        if not spec.actionable:
            continue
        try:
            signals.append(
                build_topup_signal_doc(
                    plan=plan,
                    account=account,
                    spec=spec,
                    now=now,
                    user_id=user_id,
                    reason=request.reason,
                    replaces_order_ids=replaces_by_symbol.get(_symbol_key(spec.symbol), []),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Skipping remainder signal for %s: %s", spec.symbol, exc)

    risk_report = _build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=signals,
        user_id=user_id,
    )
    blocked = [item for item in risk_report["items"] if item["blockers"]]
    response_blocked = bool(blocked) or bool(cash_blockers)
    if response_blocked and not request.dry_run:
        # cash_blockers (e.g. missing_frozen_price) mean plan_available_cash may be
        # overestimated, so confirming could over-budget the top-up. Block confirm
        # consistently with the dry-run preview rather than relying on the shared
        # broker-account cash check, which cannot bound this plan's own budget.
        message = "remainder risk check failed" if blocked else "remainder cash check failed"
        raise HTTPException(
            status_code=409,
            detail={
                "message": message,
                "risk_report": risk_report,
                "cash_blockers": cash_blockers,
            },
        )

    inserted_count = 0
    if not request.dry_run and signals:
        finance_collections["trade_signals"].insert_many(signals)
        inserted_count = len(signals)
        _append_remainder_audit(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            reviewer=str(current_user.get("username") or current_user.get("id") or ""),
            reason=request.reason,
            signals=signals,
        )

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "dry_run": request.dry_run,
                "account_synced": account_synced,
                "available_cash": plan_available_cash,
                "plan_available_cash": plan_available_cash,
                "lineage_book_cash": cash_snapshot.get("lineage_book_cash"),
                "frozen_buy_cash": cash_snapshot.get("frozen_buy_cash"),
                "broker_available_cash": broker_available_cash,
                "cash_basis": cash_snapshot.get("cash_basis"),
                "missing_frozen_price_symbols": cash_snapshot.get("missing_frozen_price_symbols") or [],
                "cash_blockers": cash_blockers,
                "remainder": [spec.to_public() for spec in specs],
                "actionable_count": sum(1 for spec in specs if spec.actionable),
                "signals": signals,
                "risk_report": risk_report,
                "inserted_count": inserted_count,
                "blocked": response_blocked,
            }
        ),
    }


def _append_remainder_audit(
    trading_collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    reviewer: str,
    reason: str,
    signals: List[Dict[str, Any]],
) -> None:
    logs = trading_collections.get("portfolio_plan_operation_logs")
    if logs is None:
        return
    now = datetime.utcnow()
    logs.insert_one(
        {
            "log_id": str(uuid.uuid4()),
            "plan_id": plan_id or "",
            "user_id": user_id,
            "task_id": None,
            "operation_type": "plan_remainder_topup",
            "operation_symbol": None,
            "status": "success",
            "message": f"published {len(signals)} top-up signals",
            "payload": {
                "reviewer": reviewer,
                "reason": (reason or "")[:2000],
                "order_ids": [s.get("order_id") for s in signals],
                "symbols": [s.get("symbol") for s in signals],
            },
            "created_at": now,
            "updated_at": now,
        }
    )


@router.get("/strategies/{strategy_template_id}/equity")
@_offload
def get_portfolio_strategy_equity(
    strategy_template_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _require_strategy(collections, strategy_template_id)
    query: Dict[str, Any] = {"strategy_template_id": strategy_template_id, "user_id": _current_user_id(current_user)}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/strategies/{strategy_template_id}/realtime-equity")
@_offload
def get_portfolio_strategy_realtime_equity(
    strategy_template_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    _require_strategy(collections, strategy_template_id)
    snapshot = collections["portfolio_paper_positions"].find_one(
        {"strategy_template_id": strategy_template_id, "user_id": _current_user_id(current_user)},
        sort=[("as_of_date", -1), ("updated_at", -1)],
    )
    if not snapshot:
        return {"success": True, "data": None}

    positions = snapshot.get("positions", []) or []
    symbols = [row.get("symbol") for row in positions if row.get("symbol")]
    realtime_prices = _load_prices(data_collections["volume_price_realtime_latest"], symbols, "", "close", latest=True)
    realtime_docs = _load_realtime_docs(data_collections["volume_price_realtime_latest"], symbols)
    latest_close_prices = _load_latest_close_prices(data_collections["volume_price"], symbols)

    position_rows = []
    market_value = 0.0
    for pos in positions:
        symbol = pos.get("symbol")
        shares = int(pos.get("shares", 0) or 0)
        realtime_doc = realtime_docs.get(symbol, {})
        price_source = "realtime"
        price = realtime_prices.get(symbol)
        if price is None:
            price = latest_close_prices.get(symbol)
            price_source = "latest_close" if price is not None else "paper_mark"
        if price is None:
            price = pos.get("mark_price") or pos.get("cost_basis") or 0
        price = float(price or 0)
        value = shares * price
        market_value += value
        position_rows.append(
            {
                **pos,
                "realtime_price": price,
                "realtime_market_value": value,
                "realtime_trade_time": realtime_doc.get("realtime_trade_time"),
                "snapshot_time": realtime_doc.get("snapshot_time"),
                "updated_at": realtime_doc.get("updated_at"),
                "price_source": price_source,
            }
        )

    cash = float(snapshot.get("cash", 0) or 0)
    realtime_equity = cash + market_value
    latest_equity = collections["portfolio_paper_equity"].find_one(
        {"strategy_template_id": strategy_template_id, "user_id": _current_user_id(current_user)},
        sort=[("date", -1)],
    ) or {}
    base_equity = float(latest_equity.get("equity", snapshot.get("equity", 0)) or 0)
    change = realtime_equity - base_equity if base_equity else None
    change_pct = change / base_equity if base_equity else None
    latest_update = max(
        [str(row.get("updated_at") or row.get("realtime_trade_time") or row.get("snapshot_time") or "") for row in position_rows],
        default="",
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "strategy_template_id": strategy_template_id,
                "as_of_date": snapshot.get("as_of_date"),
                "cash": cash,
                "market_value": market_value,
                "equity": realtime_equity,
                "base_equity": base_equity,
                "change": change,
                "change_pct": change_pct,
                "latest_update": latest_update,
                "positions": position_rows,
            }
        ),
    }


def _apply_paper_execution(collections: Dict[str, Any], data_collections: Dict[str, Any], plan_id: str, force: bool = False, user_id: Optional[str] = None) -> Dict[str, Any]:
    plan = _require_plan(collections, plan_id, {"id": user_id} if user_id else None)
    if plan.get("status") != "approved" and not force:
        raise HTTPException(status_code=400, detail="plan must be approved before paper execution")
    if user_id and _plan_has_live_signals(get_db_collections(), plan_id, user_id):
        raise HTTPException(status_code=400, detail="plan already has live signals; cannot execute paper trading")
    execution_query = {"plan_id": plan_id}
    if user_id:
        execution_query["user_id"] = user_id
    if collections["portfolio_paper_executions"].find_one(execution_query) and not force:
        raise HTTPException(status_code=400, detail="plan already has paper executions")

    strategy_template_id = plan.get("strategy_template_id")
    user_id = plan.get("user_id")
    execute_date = str(plan.get("execute_date") or "").replace("-", "")
    if not execute_date:
        raise HTTPException(
            status_code=400,
            detail="缺少 execute_date，请等待自动执行或先补齐下一交易日 execute_date",
        )
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    params_snapshot = plan.get("params_snapshot") or {}
    inherited_snapshot = _latest_lineage_position_snapshot(collections, plan, user_id, execute_date)
    positions: Dict[str, Dict[str, Any]] = {
        row.get("symbol"): dict(row)
        for row in (inherited_snapshot.get("positions") or [])
        if row.get("symbol")
    }
    cash = float(inherited_snapshot.get("cash", params_snapshot.get("initial_capital", 1_000_000.0)) or 0)
    inherited_equity = float(inherited_snapshot.get("equity", params_snapshot.get("initial_capital", 1_000_000.0)) or 0)
    item_query = {"plan_id": plan_id}
    if user_id:
        item_query["user_id"] = user_id
    items = list(collections["portfolio_trade_plan_items"].find(item_query).sort("rank", 1))
    symbols = [item.get("symbol") for item in items if item.get("symbol")]
    execution_symbols = sorted(
        {
            item.get("symbol")
            for item in items
            if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
        }
    )
    open_prices = _load_prices(data_collections["volume_price"], symbols, execute_date, "open")
    missing_open_prices = [symbol for symbol in execution_symbols if symbol not in open_prices]
    if missing_open_prices:
        raise HTTPException(
            status_code=409,
            detail=(
                f"开盘价未就绪：execute_date={execute_date}, "
                f"missing_count={len(missing_open_prices)}, "
                f"examples={', '.join(missing_open_prices[:10])}"
            ),
        )
    close_prices = _load_prices(data_collections["volume_price"], symbols, execute_date, "close")
    now = datetime.utcnow()
    executions = []
    partial = False

    if force:
        collections["portfolio_paper_executions"].delete_many(execution_query)

    # Sell first, then buy: A-share sell proceeds are usable for same-day buys,
    # so process all sells before any buy to credit cash up front. A single rank
    # ordered pass would otherwise starve a buy that precedes its funding sells
    # and report a spurious partial. sorted() is stable, preserving rank order
    # within each side.
    for item in sorted(items, key=lambda it: 0 if int(it.get("delta_shares", 0) or 0) < 0 else 1):
        symbol = item.get("symbol")
        delta = int(item.get("delta_shares", 0) or 0)
        if not symbol or delta == 0:
            continue
        price = open_prices.get(symbol)
        if not price:
            raise HTTPException(status_code=409, detail=f"开盘价未就绪：{symbol} {execute_date}")
        current = positions.get(symbol, {"symbol": symbol, "name": item.get("name", ""), "industry": item.get("industry", ""), "shares": 0, "cost_basis": 0.0})
        if delta < 0:
            quantity = min(abs(delta), int(current.get("shares", 0) or 0))
            amount = quantity * price
            cash += amount
            current["shares"] = int(current.get("shares", 0) or 0) - quantity
            executions.append(_execution_doc(plan, item, "sell", quantity, price, amount, None, now))
        else:
            quantity = delta
            amount = quantity * price
            if amount > cash:
                lot_size = int(params_snapshot.get("lot_size", 100))
                quantity = (int(cash // price) // max(1, lot_size)) * max(1, lot_size)
                amount = quantity * price
                partial = True
            if quantity <= 0:
                executions.append(_execution_doc(plan, item, "blocked", 0, price, 0.0, "insufficient_cash", now))
                partial = True
                continue
            cash -= amount
            old_shares = int(current.get("shares", 0) or 0)
            old_cost = float(current.get("cost_basis", 0) or 0)
            current["cost_basis"] = ((old_shares * old_cost) + amount) / (old_shares + quantity) if old_shares + quantity else price
            current["shares"] = old_shares + quantity
            executions.append(_execution_doc(plan, item, "buy", quantity, price, amount, None, now))
        if int(current.get("shares", 0) or 0) > 0:
            positions[symbol] = current
        elif symbol in positions:
            del positions[symbol]

    position_rows = []
    market_value = 0.0
    for symbol, pos in sorted(positions.items()):
        mark_price = close_prices.get(symbol) or open_prices.get(symbol) or float(pos.get("cost_basis", 0) or 0)
        value = int(pos.get("shares", 0) or 0) * mark_price
        market_value += value
        position_rows.append({**pos, "mark_price": mark_price, "market_value": value})
    equity = cash + market_value
    status = "partially_executed" if partial else "executed_paper"

    if executions:
        collections["portfolio_paper_executions"].insert_many(executions)
    snapshot = {
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "params_hash": plan.get("params_hash"),
        "as_of_date": execute_date,
        "plan_id": plan_id,
        "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
        "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }
    position_query = {"plan_id": plan_id, "as_of_date": execute_date}
    equity_query = {"plan_id": plan_id, "date": execute_date}
    if user_id:
        position_query["user_id"] = user_id
        equity_query["user_id"] = user_id
    collections["portfolio_paper_positions"].replace_one(position_query, snapshot, upsert=True)
    collections["portfolio_paper_equity"].replace_one(
        equity_query,
        {
            "strategy_template_id": strategy_template_id,
            "user_id": user_id,
            "params_hash": plan.get("params_hash"),
            "date": execute_date,
            "plan_id": plan_id,
            "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
            "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
            "equity": equity,
            "cash": cash,
            "market_value": market_value,
            "updated_at": now,
        },
        upsert=True,
    )
    plan_update_query = {"plan_id": plan_id}
    if user_id:
        plan_update_query["user_id"] = user_id
    collections["portfolio_trade_plans"].update_one(plan_update_query, {"$set": {"status": status, "paper_executed_at": now}})
    lineage_effects = _apply_manual_lineage_effects_native(
        collections,
        plan,
        final_positions=position_rows,
        acted_by=user_id,
    )
    return {
        "plan_id": plan_id,
        "status": status,
        "executions": executions,
        "position_snapshot": snapshot,
        "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
        "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
        "inherited_equity": inherited_equity,
        "lineage_effects": lineage_effects,
    }


def _paper_execution_status(collections: Dict[str, Any], data_collections: Dict[str, Any], plan: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    execution_query = {"plan_id": plan_id}
    if plan.get("user_id"):
        execution_query["user_id"] = plan.get("user_id")
    execute_date = plan.get("execute_date")
    effective_execute_date = execute_date or None
    plan_status = str(plan.get("status") or "")

    missing_symbols: List[str] = []
    missing_execute_date = plan_status == "approved" and not bool(execute_date)
    if plan_status == "approved" and execute_date:
        execution_symbols = sorted(
            {
                item.get("symbol")
                for item in items
                if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
            }
        )
        open_prices = _load_prices(data_collections["volume_price"], execution_symbols, effective_execute_date, "open") if execution_symbols else {}
        missing_symbols = [symbol for symbol in execution_symbols if symbol not in open_prices]

    should_load_executions = plan_status in {"approved", "executed_paper", "partially_executed"} or bool(plan.get("paper_executed_at"))
    latest_execution = None
    execution_count = 0
    if should_load_executions:
        latest_execution = collections["portfolio_paper_executions"].find_one(
            execution_query,
            sort=[("created_at", -1)],
        )
        execution_count = collections["portfolio_paper_executions"].count_documents(execution_query)

    if missing_execute_date:
        backend_status = "missing_execute_date"
    elif plan_status == "approved":
        backend_status = "waiting_for_open_prices" if missing_symbols else "ready_for_background_execution"
    elif plan_status in {"executed_paper", "partially_executed"}:
        backend_status = plan_status
    else:
        backend_status = "not_approved"

    return {
        "execute_date": execute_date,
        "effective_execute_date": effective_execute_date,
        "uses_runtime_execute_date": False,
        "open_price_ready": bool(execute_date) and not missing_symbols,
        "missing_execute_date": missing_execute_date,
        "missing_open_price_count": len(missing_symbols),
        "missing_open_price_examples": missing_symbols[:10],
        "backend_execution_status": backend_status,
        "execution_count": execution_count,
        "latest_execution_result": latest_execution,
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


def _market_price_quotes(data_collections: Dict[str, Any], symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per-symbol price quotes with source metadata for overlay and publish repricing."""
    return market_price_quotes(data_collections, symbols, symbol_query_values=_symbol_query_values)


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
    )


def _persist_repriced_plan_items(
    trading_collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    items: List[Dict[str, Any]],
    reprice_summary: Dict[str, Any],
    now: datetime,
) -> None:
    for item in items:
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        query = {"plan_id": plan_id, "user_id": user_id, "symbol": symbol}
        existing = trading_collections["portfolio_trade_plan_items"].find_one(query)
        set_fields = {
            "current_shares": _int_value(item.get("current_shares")),
            "target_shares": _int_value(item.get("target_shares")),
            "delta_shares": _int_value(item.get("delta_shares")),
            "action": item.get("action"),
            "estimated_price": item.get("estimated_price"),
            "estimated_amount": item.get("estimated_amount"),
            "price_source": item.get("price_source"),
            "price_as_of": item.get("price_as_of"),
            "blockers": item.get("blockers") or [],
            "warnings": item.get("warnings") or [],
            "estimated_price_at_approval": item.get("estimated_price"),
            "updated_at": now,
        }
        if existing and not existing.get("estimated_price_at_generation"):
            set_fields["estimated_price_at_generation"] = existing.get("estimated_price")
        trading_collections["portfolio_trade_plan_items"].update_one(query, {"$set": set_fields})
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {
            "$set": {
                "repriced_at": now,
                "reprice_summary": reprice_summary,
                "updated_at": now,
            }
        },
    )


def _execution_doc(plan: Dict[str, Any], item: Dict[str, Any], action: str, quantity: int, price: Optional[float], amount: float, blocker: Optional[str], now: datetime) -> Dict[str, Any]:
    return {
        "execution_id": str(uuid.uuid4()),
        "plan_id": plan["plan_id"],
        "strategy_template_id": plan["strategy_template_id"],
        "user_id": plan.get("user_id"),
        "symbol": item.get("symbol"),
        "name": item.get("name", ""),
        "action": action,
        "quantity": int(quantity),
        "price": price,
        "amount": amount,
        "blocker": blocker,
        "execute_date": plan.get("execute_date"),
        "created_at": now,
    }
