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
from services.trading_calendar import is_sse_open_day


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_INITIAL_CAPITAL = 1_000_000.0
ACCOUNT_SNAPSHOT_STALE_SECONDS = 6 * 60 * 60
# Trader pushes account data every ~60s; five minutes tolerates a missed cycle
# without letting a dead QMT connection masquerade as today's live point.
LIVE_EQUITY_POINT_MAX_AGE_SECONDS = 5 * 60


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


def _date_key(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


def load_account_cash_flows(
    finance_collections: Dict[str, Any],
    *,
    user_id: Any,
    securities_account_id: Any,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load manually recorded external cash flows for one account."""
    collection = finance_collections.get("trader_cash_flows")
    if collection is None:
        return []
    query: Dict[str, Any] = {
        "user_id": user_id,
        "securities_account_id": str(securities_account_id),
    }
    date_query: Dict[str, str] = {}
    if start_date:
        date_query["$gte"] = _date_key(start_date)
    if end_date:
        date_query["$lte"] = _date_key(end_date)
    if date_query:
        query["date"] = date_query
    return list(collection.find(query).sort("date", 1))


def _external_flow_for_date(
    finance_collections: Dict[str, Any],
    *,
    user_id: Any,
    securities_account_id: Any,
    date_key: str,
) -> float:
    collection = finance_collections.get("trader_cash_flows")
    if collection is None:
        return 0.0
    rows = collection.find(
        {
            "user_id": user_id,
            "securities_account_id": str(securities_account_id),
            "date": date_key,
        }
    )
    return round(sum(_numeric(row.get("amount")) for row in rows), 2)


def _previous_account_equity_row(
    finance_collections: Dict[str, Any],
    *,
    user_id: Any,
    securities_account_id: Any,
    snapshot_date: str,
) -> Optional[Dict[str, Any]]:
    rows = [
        row
        for row in finance_collections["trader_account_equity"].find(
            {"user_id": user_id, "securities_account_id": str(securities_account_id)}
        )
        if _date_key(row.get("date")) < snapshot_date
    ]
    return max(rows, key=lambda row: _date_key(row.get("date"))) if rows else None


def enrich_account_equity_with_flows(
    finance_collections: Dict[str, Any],
    rows: List[Dict[str, Any]],
    *,
    user_id: Any,
    securities_account_id: Any,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Attach flow-adjusted equity and flow-adjusted daily P&L to equity rows.

    A recorded cash flow is removed from the total-asset change before it is
    called P&L. If a flow was not recorded, it will still look like P&L; the
    manual cash-flow ledger is the source of truth for external flows.
    """
    if not rows:
        return rows
    flows = load_account_cash_flows(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
        start_date=start_date,
        end_date=end_date,
    )
    flow_by_date: Dict[str, float] = defaultdict(float)
    for flow in flows:
        flow_by_date[_date_key(flow.get("date"))] += _numeric(flow.get("amount"))
    flow_dates = sorted(flow_by_date)

    enriched_rows: List[Dict[str, Any]] = []
    previous_equity: Optional[float] = None
    first_adjusted_equity: Optional[float] = None
    previous_date = ""
    for row in rows:
        date_key = _date_key(row.get("date"))
        equity = _numeric(row.get("total_asset", row.get("equity")))
        external_flow = round(flow_by_date.get(date_key, 0.0), 2)
        cumulative_flow = round(
            sum(flow_by_date[flow_date] for flow_date in flow_dates if flow_date <= date_key),
            2,
        )
        adjusted_equity = round(equity - cumulative_flow, 2)
        if first_adjusted_equity is None:
            first_adjusted_equity = adjusted_equity
        daily_pnl = None
        daily_pnl_ratio = None
        if previous_equity is not None:
            daily_pnl = round(equity - previous_equity - external_flow, 2)
            daily_pnl_ratio = (daily_pnl / previous_equity) if previous_equity > 0 else None
        enriched = {
            **row,
            "external_flow": external_flow,
            "cumulative_external_flow": cumulative_flow,
            "flow_adjusted_equity": adjusted_equity,
            "flow_adjusted_change_from_start": round(
                adjusted_equity - first_adjusted_equity, 2
            ),
            "daily_pnl": daily_pnl if daily_pnl is not None else row.get("daily_pnl"),
            "daily_pnl_ratio": (
                daily_pnl_ratio if daily_pnl_ratio is not None else row.get("daily_pnl_ratio")
            ),
            "daily_pnl_method": (
                "total_asset_delta_minus_external_flow"
                if daily_pnl is not None
                else row.get("daily_pnl_method")
            ),
            "flow_adjusted_previous_date": previous_date or None,
        }
        enriched_rows.append(enriched)
        previous_equity = equity
        previous_date = date_key
    return enriched_rows


def snapshot_account_equity(
    finance_collections: Dict[str, Any],
    data_collections: Optional[Dict[str, Any]] = None,
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
        market_value = _numeric(account.get("market_value"))
        cash = _numeric(account.get("cash"))
        total_asset_source = _numeric(
            account.get("total_asset", account.get("total_assets"))
        )
        # QMT occasionally reports total_asset=0 while cash/market_value are
        # valid. Fall back like portfolio_snapshot does, otherwise the equity
        # curve silently records a zero point for that day.
        total_asset = round(total_asset_source or (market_value + cash), 2)
        row = {
            "user_id": user_id,
            "securities_account_id": str(securities_account_id),
            "date": snapshot_date,
            "total_asset": total_asset,
            "cash": cash,
            "available_cash": _numeric(account.get("available_cash")),
            "market_value": market_value,
            "account_id": account.get("account_id"),
            "broker": account.get("broker"),
            "source_synced_at": source_synced_at,
            "source_synced_age_seconds": age_seconds,
            "is_stale": is_stale,
            "updated_at": updated_at,
        }
        previous_row = _previous_account_equity_row(
            finance_collections,
            user_id=user_id,
            securities_account_id=securities_account_id,
            snapshot_date=snapshot_date,
        )
        if previous_row is not None:
            prior_asset = _numeric(previous_row.get("total_asset"))
            external_flow = _external_flow_for_date(
                finance_collections,
                user_id=user_id,
                securities_account_id=securities_account_id,
                date_key=snapshot_date,
            )
            daily_pnl = round(total_asset - prior_asset - external_flow, 2)
            row.update({
                "external_flow": external_flow,
                "daily_pnl": daily_pnl,
                "daily_pnl_ratio": (daily_pnl / prior_asset) if prior_asset > 0 else None,
                "daily_pnl_method": "total_asset_delta_minus_external_flow",
            })
        elif data_collections:
            row.update(
                compute_account_position_daily_pnl(
                    finance_collections,
                    data_collections,
                    user_id=user_id,
                    securities_account_id=securities_account_id,
                    snapshot_date=snapshot_date,
                )
            )
        finance_collections["trader_account_equity"].update_one(
            {"user_id": user_id, "securities_account_id": str(securities_account_id), "date": snapshot_date},
            {"$set": row},
            upsert=True,
        )
        written += 1

    return {"date": snapshot_date, "written": written, "skipped": skipped}


def build_account_equity_live_point(
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: Optional[str] = None,
    now: Optional[datetime] = None,
    max_age_seconds: int = LIVE_EQUITY_POINT_MAX_AGE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Build a synthetic intraday equity point from the latest account snapshot.

    Returns ``None`` unless every condition holds:
    - today (Asia/Shanghai) is an SSE trading day;
    - the account snapshot was synced within ``max_age_seconds``;
    - the broker reported a positive ``total_asset``.

    The positive ``total_asset`` requirement is deliberate: QMT rolls
    ``market_value`` back to the previous close after hours, so the
    ``market_value + cash`` fallback used for the EOD frame can be wrong
    intraday. A missing point is better than a fabricated one.
    """
    if not user_id or not securities_account_id:
        return None

    snapshot_date = _today_yyyymmdd(now)
    calendar = data_collections["trade_calendar"]
    if not is_sse_open_day(calendar, snapshot_date):
        return None

    account = finance_collections["trader_accounts"].find_one(
        {
            "user_id": user_id,
            "securities_account_id": str(securities_account_id),
        }
    )
    if not account:
        return None

    source_synced_at = account.get("synced_at")
    source_ts = _timestamp(source_synced_at)
    if source_ts is None:
        return None
    current_ts = _timestamp(now) if now is not None else time.time()
    age_seconds = current_ts - source_ts
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return None

    total_asset = _numeric(account.get("total_asset"))
    if total_asset <= 0:
        return None

    return {
        "user_id": user_id,
        "securities_account_id": str(securities_account_id),
        "date": snapshot_date,
        "total_asset": round(total_asset, 2),
        "cash": _numeric(account.get("cash")),
        "available_cash": _numeric(account.get("available_cash")),
        "market_value": _numeric(account.get("market_value")),
        "account_id": account.get("account_id"),
        "broker": account.get("broker"),
        "source_synced_at": source_synced_at,
        "source_synced_age_seconds": age_seconds,
        "is_stale": False,
        "is_intraday": True,
        "live_synced_at": source_synced_at,
    }


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


def _load_close_pair(
    data_collections: Dict[str, Any],
    symbol: str,
    snapshot_date: str,
) -> Optional[Tuple[float, float]]:
    """Return (snapshot-date close, previous trading-day close).

    Only returns a pair when the latest row is exactly ``snapshot_date``. A
    stale latest row would make a "daily" P&L silently describe an older day.
    """
    for collection_name, key in (("volume_price", "symbol"), ("etf_daily", "ts_code")):
        collection = data_collections.get(collection_name)
        if collection is None:
            continue
        rows = list(
            collection.find(
                {key: {"$in": [symbol, _symbol_key(symbol)]}},
                {"_id": 0, key: 1, "trade_date": 1, "close": 1},
            ).sort("trade_date", -1)
        )
        rows = [row for row in rows if str(row.get("trade_date") or "") <= snapshot_date]
        if len(rows) < 2:
            continue
        latest, previous = rows[0], rows[1]
        if str(latest.get("trade_date") or "") != snapshot_date:
            continue
        today_close = _numeric(latest.get("close"))
        prev_close = _numeric(previous.get("close"))
        if today_close > 0 and prev_close > 0:
            return today_close, prev_close
    return None


def compute_account_position_daily_pnl(
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    user_id: Any,
    securities_account_id: Any,
    snapshot_date: str,
) -> Dict[str, Any]:
    """Estimate one account's daily P&L from closing positions and closes.

    The estimate is deterministic price change: ``qty * (close - prev_close)``
    per position. It excludes fees, dividends and intraday trading effects, so
    it is marked as an estimate rather than broker-reported daily P&L.
    """
    positions = list(
        finance_collections["trader_positions"].find(
            {"securities_account_id": str(securities_account_id)}
        )
    )
    positions_snapshot: List[Dict[str, Any]] = []
    unpriced: List[str] = []
    daily_pnl = 0.0
    priced_count = 0

    for position in positions:
        if user_id and position.get("user_id") and str(position["user_id"]) != str(user_id):
            continue
        qty = _numeric(
            position.get("qty", position.get("quantity", position.get("volume")))
        )
        if qty <= 0:
            continue
        symbol = str(position.get("symbol") or "")
        if not symbol:
            continue
        closes = _load_close_pair(data_collections, symbol, snapshot_date)
        if closes is None:
            unpriced.append(symbol)
            continue
        today_close, prev_close = closes
        daily_pnl += qty * (today_close - prev_close)
        priced_count += 1
        positions_snapshot.append({
            "symbol": symbol,
            "qty": qty,
            "close": today_close,
            "prev_close": prev_close,
        })

    if not priced_count:
        return {
            "daily_pnl": None,
            "daily_pnl_ratio": None,
            "daily_pnl_method": "positions_close_estimate",
            "positions_snapshot": positions_snapshot,
            "daily_pnl_priced_symbols": priced_count,
            "daily_pnl_unpriced_symbols": unpriced,
        }

    equity_col = finance_collections["trader_account_equity"]
    prior_rows = [
        row for row in equity_col.find({"securities_account_id": str(securities_account_id)})
        if str(row.get("date") or "") < snapshot_date
    ]
    prior = max(prior_rows, key=lambda row: str(row.get("date") or "")) if prior_rows else None
    prior_asset = _numeric(prior.get("total_asset")) if prior else 0.0
    return {
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_ratio": (daily_pnl / prior_asset) if prior_asset > 0 else None,
        "daily_pnl_method": "positions_close_estimate",
        "positions_snapshot": positions_snapshot,
        "daily_pnl_priced_symbols": priced_count,
        "daily_pnl_unpriced_symbols": unpriced,
    }


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
