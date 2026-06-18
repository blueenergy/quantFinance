"""Live equity curve snapshots for real accounts and portfolio plans."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from services.trade_ledger import (
    compute_realized_pnl_for_group,
    group_executions_by_order,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_INITIAL_CAPITAL = 1_000_000.0
ACCOUNT_SNAPSHOT_STALE_SECONDS = 6 * 60 * 60


def _today_yyyymmdd(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ).strftime("%Y%m%d")


def _utc_now() -> datetime:
    return datetime.utcnow()


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    return None


def _symbol_key(value: Any) -> str:
    return str(value or "").split(".", 1)[0]


def _query_date_range(start_date: Optional[str], end_date: Optional[str]) -> Dict[str, str]:
    query: Dict[str, str] = {}
    if start_date:
        query["$gte"] = start_date.replace("-", "")
    if end_date:
        query["$lte"] = end_date.replace("-", "")
    return query


def ensure_live_equity_indexes(finance_collections: Dict[str, Any], trading_collections: Dict[str, Any]) -> None:
    """Create idempotent indexes for live equity collections."""
    finance_collections["trader_account_equity"].create_index(
        [("user_id", 1), ("securities_account_id", 1), ("date", 1)],
        unique=True,
        background=True,
        name="user_account_date_unique",
    )
    trading_collections["portfolio_live_equity"].create_index(
        [("user_id", 1), ("plan_id", 1), ("date", 1)],
        unique=True,
        background=True,
        name="user_plan_date_unique",
    )
    trading_collections["portfolio_live_equity"].create_index(
        [("user_id", 1), ("plan_id", 1), ("date", -1)],
        background=True,
        name="user_plan_date_desc",
    )


def snapshot_account_equity(
    finance_collections: Dict[str, Any],
    *,
    date: Optional[str] = None,
    now: Optional[datetime] = None,
    stale_seconds: int = ACCOUNT_SNAPSHOT_STALE_SECONDS,
) -> Dict[str, Any]:
    """Persist one account-level equity point per account for the given date."""
    snapshot_date = date or _today_yyyymmdd(now)
    updated_at = _utc_now()
    written = 0
    skipped = 0
    rows = list(finance_collections["trader_accounts"].find({}))

    for account in rows:
        user_id = account.get("user_id")
        securities_account_id = account.get("securities_account_id")
        if not user_id or not securities_account_id:
            skipped += 1
            continue

        source_synced_at = account.get("synced_at")
        source_ts = _timestamp(source_synced_at)
        age_seconds = (time.time() - source_ts) if source_ts is not None else None
        is_stale = bool(age_seconds is None or age_seconds > stale_seconds)
        row = {
            "user_id": user_id,
            "securities_account_id": str(securities_account_id),
            "date": snapshot_date,
            "total_asset": _numeric(account.get("total_asset", account.get("total_assets"))),
            "cash": _numeric(account.get("cash")),
            "available_cash": _numeric(account.get("available_cash")),
            "market_value": _numeric(account.get("market_value")),
            "account_id": account.get("account_id"),
            "broker": account.get("broker"),
            "source_synced_at": source_synced_at,
            "source_synced_age_seconds": age_seconds,
            "is_stale": is_stale,
            "updated_at": updated_at,
        }
        finance_collections["trader_account_equity"].update_one(
            {"user_id": user_id, "securities_account_id": str(securities_account_id), "date": snapshot_date},
            {"$set": row},
            upsert=True,
        )
        written += 1

    return {"date": snapshot_date, "written": written, "skipped": skipped}


def list_account_equity(
    finance_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if securities_account_id:
        query["securities_account_id"] = securities_account_id
    date_range = _query_date_range(start_date, end_date)
    if date_range:
        query["date"] = date_range
    return list(finance_collections["trader_account_equity"].find(query).sort("date", 1))


def _plan_initial_capital(plan: Dict[str, Any]) -> float:
    params = plan.get("params_snapshot") or {}
    summary = plan.get("summary") or {}
    return _numeric(
        params.get("initial_capital", plan.get("initial_capital", summary.get("equity"))),
        DEFAULT_INITIAL_CAPITAL,
    )


def _load_live_plan_inputs(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    signal_query = {
        "user_id": plan.get("user_id"),
        "plan_id": plan.get("plan_id"),
        "mode": "live",
    }
    signals = list(finance_collections["trade_signals"].find(signal_query))
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    if not order_ids:
        return signals, []
    executions = list(
        finance_collections["trade_executions"].find(
            {
                "user_id": plan.get("user_id"),
                "order_id": {"$in": order_ids},
            }
        )
    )
    return signals, group_executions_by_order(executions)


def _load_latest_close_prices(volume_price: Any, symbols: Iterable[str]) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
            projection={"_id": 0, "symbol": 1, "close": 1},
        )
        if not row:
            row = volume_price.find_one(
                {"symbol": _symbol_key(symbol)},
                sort=[("trade_date", -1)],
                projection={"_id": 0, "symbol": 1, "close": 1},
            )
        price = _numeric(row.get("close") if row else None)
        if price > 0:
            prices[symbol] = price
    return prices


def _load_realtime_prices(volume_price_realtime: Any, symbols: Iterable[str]) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]]]:
    symbol_list = list(symbols)
    base_symbols = [_symbol_key(symbol) for symbol in symbol_list]
    rows = list(
        volume_price_realtime.find(
            {"symbol": {"$in": list(set(symbol_list + base_symbols))}},
            {"_id": 0, "symbol": 1, "close": 1, "realtime_trade_time": 1, "snapshot_time": 1, "updated_at": 1},
        )
    )
    docs = {row.get("symbol"): row for row in rows if row.get("symbol")}
    prices: Dict[str, float] = {}
    resolved_docs: Dict[str, Dict[str, Any]] = {}
    for symbol in symbol_list:
        doc = docs.get(symbol) or docs.get(_symbol_key(symbol)) or {}
        price = _numeric(doc.get("close"))
        if price > 0:
            prices[symbol] = price
            resolved_docs[symbol] = doc
    return prices, resolved_docs


def compute_live_plan_equity(
    plan: Dict[str, Any],
    executions: List[Dict[str, Any]],
    price_map: Dict[str, float],
    *,
    date: Optional[str] = None,
    realtime_docs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compute a per-plan equity point from this plan's own executions."""
    initial_capital = _plan_initial_capital(plan)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in executions:
        qty = row.get("filled_size") or row.get("filled_qty") or row.get("quantity") or row.get("size") or 0
        if not qty:
            continue
        symbol = row.get("symbol")
        if symbol:
            grouped[str(symbol)].append(row)

    positions = []
    market_value = 0.0
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    latest_update = ""

    for symbol, trades in sorted(grouped.items()):
        position = compute_realized_pnl_for_group(trades)
        qty = int(position.get("net_quantity") or 0)
        avg_cost = _numeric(position.get("avg_cost"))
        realized_pnl += _numeric(position.get("realized_pnl"))
        price = price_map.get(symbol) or price_map.get(_symbol_key(symbol)) or avg_cost
        value = qty * price
        unrealized = (price - avg_cost) * qty if qty > 0 else 0.0
        market_value += value
        unrealized_pnl += unrealized
        realtime_doc = (realtime_docs or {}).get(symbol, {})
        update_time = str(
            realtime_doc.get("updated_at")
            or realtime_doc.get("realtime_trade_time")
            or realtime_doc.get("snapshot_time")
            or ""
        )
        latest_update = max(latest_update, update_time)
        if qty > 0:
            positions.append(
                {
                    "symbol": symbol,
                    "shares": qty,
                    "avg_cost": round(avg_cost, 4),
                    "price": price,
                    "market_value": value,
                    "unrealized_pnl": unrealized,
                    "price_source": "market" if symbol in price_map or _symbol_key(symbol) in price_map else "avg_cost",
                    **({"realtime_doc": realtime_doc} if realtime_doc else {}),
                }
            )

    equity = initial_capital + realized_pnl + unrealized_pnl
    return {
        "user_id": plan.get("user_id"),
        "plan_id": plan.get("plan_id"),
        "strategy_template_id": plan.get("strategy_template_id"),
        "date": date,
        "initial_capital": initial_capital,
        "equity": equity,
        "cash": equity - market_value,
        "market_value": market_value,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "position_count": len(positions),
        "positions": positions,
        "latest_update": latest_update,
    }


def snapshot_live_plan_equity(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    date: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    snapshot_date = date or _today_yyyymmdd(now)
    updated_at = _utc_now()
    live_signals = list(
        finance_collections["trade_signals"].find(
            {"mode": "live", "plan_id": {"$exists": True}}
        )
    )
    plan_keys = sorted({(row.get("user_id"), row.get("plan_id")) for row in live_signals if row.get("user_id") and row.get("plan_id")})
    written = 0
    skipped = 0

    for user_id, plan_id in plan_keys:
        plan = trading_collections["portfolio_trade_plans"].find_one({"user_id": user_id, "plan_id": plan_id})
        if not plan:
            skipped += 1
            continue
        _signals, executions = _load_live_plan_inputs(trading_collections, finance_collections, plan)
        if not executions:
            skipped += 1
            continue
        symbols = sorted({str(row.get("symbol")) for row in executions if row.get("symbol")})
        prices = _load_latest_close_prices(data_collections["volume_price"], symbols)
        point = compute_live_plan_equity(plan, executions, prices, date=snapshot_date)
        point["has_paper_conflict"] = bool(
            plan.get("paper_executed_at")
            or plan.get("status") in {"executed_paper", "partially_executed"}
            or trading_collections["portfolio_paper_executions"].find_one({"user_id": user_id, "plan_id": plan_id})
        )
        point["updated_at"] = updated_at
        point["execution_count"] = len(executions)
        trading_collections["portfolio_live_equity"].update_one(
            {"user_id": user_id, "plan_id": plan_id, "date": snapshot_date},
            {"$set": point},
            upsert=True,
        )
        written += 1

    return {"date": snapshot_date, "written": written, "skipped": skipped}


def list_live_plan_equity(
    trading_collections: Dict[str, Any],
    *,
    user_id: str,
    plan_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id, "plan_id": plan_id}
    date_range = _query_date_range(start_date, end_date)
    if date_range:
        query["date"] = date_range
    return list(trading_collections["portfolio_live_equity"].find(query).sort("date", 1))


def compute_live_plan_realtime_equity(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    user_id: str,
    plan_id: str,
    plan: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if plan is None:
        plan = trading_collections["portfolio_trade_plans"].find_one({"user_id": user_id, "plan_id": plan_id})
    if not plan:
        return None
    _signals, executions = _load_live_plan_inputs(trading_collections, finance_collections, plan)
    if not executions:
        return None
    symbols = sorted({str(row.get("symbol")) for row in executions if row.get("symbol")})
    realtime_prices, realtime_docs = _load_realtime_prices(data_collections["volume_price_realtime_latest"], symbols)
    close_prices = _load_latest_close_prices(data_collections["volume_price"], symbols)
    prices = {**close_prices, **realtime_prices}
    point = compute_live_plan_equity(plan, executions, prices, date=_today_yyyymmdd(), realtime_docs=realtime_docs)
    latest_equity = trading_collections["portfolio_live_equity"].find_one(
        {"user_id": user_id, "plan_id": plan_id},
        sort=[("date", -1)],
    ) or {}
    base_equity = _numeric(latest_equity.get("equity"), point["initial_capital"])
    change = point["equity"] - base_equity if base_equity else None
    change_pct = change / base_equity if base_equity and change is not None else None
    return {
        **point,
        "base_equity": base_equity,
        "change": change,
        "change_pct": change_pct,
    }
