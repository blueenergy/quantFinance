"""Cross-plan (lineage) live portfolio aggregation — pure helpers, no I/O.

Used by portfolio plan routers to merge live executions across rebalance
``plan_id`` values that share the same ``(strategy_template_id, params_hash)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services.trade_ledger import compute_realized_pnl_for_group


def _symbol_key(value: Any) -> str:
    return str(value or "").split(".", 1)[0]


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _execution_ts(row: Dict[str, Any]) -> float:
    ts = row.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    return 0.0


def _execution_side(row: Dict[str, Any]) -> str:
    raw = (row.get("side") or row.get("action") or "").upper()
    if raw in ("BUY", "SELL"):
        return raw
    return ""


def _trades_for_ledger(executions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize execution docs for ``compute_realized_pnl_for_group``."""
    out: List[Dict[str, Any]] = []
    for row in executions:
        side = _execution_side(row)
        if side not in ("BUY", "SELL"):
            continue
        qty = (
            row.get("filled_size")
            or row.get("filled_qty")
            or row.get("quantity")
            or row.get("size")
            or 0
        )
        if not qty:
            continue
        doc = dict(row)
        doc["side"] = side
        doc["action"] = side
        out.append(doc)
    return out


def merge_lineage_live_executions(
    signals: List[Dict[str, Any]],
    executions: List[Dict[str, Any]],
    plan_meta: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach ``source_plan_*`` to each execution and sort chronologically (oldest first)."""

    order_to_signal: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        oid = sig.get("order_id")
        if oid:
            order_to_signal[str(oid)] = sig

    merged: List[Dict[str, Any]] = []
    for row in executions:
        oid = str(row.get("order_id") or row.get("_id") or "")
        sig = order_to_signal.get(oid) or {}
        plan_id = str(sig.get("plan_id") or row.get("plan_id") or "")
        meta = plan_meta.get(plan_id) or {}
        base_date = meta.get("base_date")
        enriched = {
            **row,
            "plan_id": plan_id or row.get("plan_id"),
            "source_plan_id": plan_id or None,
            "source_plan_base_date": base_date,
            "name": row.get("name") or sig.get("name") or sig.get("stock_name") or "",
            "signal_action": sig.get("action"),
            "signal_size": sig.get("size"),
        }
        merged.append(enriched)

    merged.sort(key=_execution_ts)
    return merged


def build_lineage_live_positions(
    executions: List[Dict[str, Any]],
    price_map: Dict[str, float],
    realtime_docs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Combine executions across plans; per-symbol weighted avg cost + PnL.

    ``executions`` should already be one row per ``order_id`` (e.g. after
    ``group_executions_by_order``). ``price_map`` keys may be full symbol or
    exchange suffix form; we resolve via ``_symbol_key``.
    """

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in executions:
        sym = row.get("symbol")
        if not sym:
            continue
        sym_s = str(sym)
        grouped.setdefault(sym_s, []).append(row)

    positions: List[Dict[str, Any]] = []
    total_market_value = 0.0
    total_realized_pnl = 0.0
    total_unrealized_pnl = 0.0
    total_cost_basis = 0.0
    latest_update = ""

    for symbol in sorted(grouped.keys()):
        trades = _trades_for_ledger(grouped[symbol])
        if not trades:
            continue
        pos = compute_realized_pnl_for_group(trades)
        qty = int(pos.get("net_quantity") or 0)
        avg_cost = _numeric(pos.get("avg_cost"))
        realized = _numeric(pos.get("realized_pnl"))
        total_realized_pnl += realized

        price = price_map.get(symbol) or price_map.get(_symbol_key(symbol)) or avg_cost
        if qty > 0 and price <= 0:
            price = avg_cost
        value = qty * price
        unrealized = (price - avg_cost) * qty if qty > 0 else 0.0
        total_market_value += value
        total_unrealized_pnl += unrealized
        if qty > 0:
            total_cost_basis += avg_cost * qty

        rt_doc = (realtime_docs or {}).get(symbol) or (realtime_docs or {}).get(_symbol_key(symbol)) or {}
        update_time = str(
            rt_doc.get("updated_at")
            or rt_doc.get("realtime_trade_time")
            or rt_doc.get("snapshot_time")
            or ""
        )
        if update_time:
            latest_update = max(latest_update, update_time)

        in_market = symbol in price_map or _symbol_key(symbol) in price_map
        price_source = "market" if in_market and price > 0 else "avg_cost"

        if qty != 0 or realized != 0:
            positions.append(
                {
                    "symbol": symbol,
                    "shares": qty,
                    "avg_cost": round(avg_cost, 4) if qty > 0 else round(avg_cost, 4),
                    "last_price": round(price, 4) if price else 0.0,
                    "last_price_source": price_source,
                    "market_value": round(value, 2),
                    "realized_pnl": round(realized, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    **({"realtime_doc": rt_doc} if rt_doc else {}),
                }
            )

    holding_count = sum(1 for p in positions if (p.get("shares") or 0) > 0)

    return {
        "positions": positions,
        "summary": {
            "total_market_value": round(total_market_value, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
            "total_cost_basis": round(total_cost_basis, 2),
            "holding_count": holding_count,
            "position_row_count": len(positions),
        },
        "latest_price_update": latest_update,
    }
