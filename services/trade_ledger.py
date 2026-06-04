"""Pure helpers to rebuild positions and realized P&L from trade executions.

These functions are intentionally free of any FastAPI/router or DB dependency
so they can be reused by both routers and background services (scheduler,
equity snapshots, etc.).
"""

from __future__ import annotations

from typing import Any, Dict, List


def compute_realized_pnl_for_group(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute net position, realized P&L and average cost for one symbol group.

    Uses a simple weighted-average-cost method over time-ordered trades:
      - BUY: fold commission into the cost basis
      - SELL: realize P&L against the current average cost, net of commission

    Returns a dict with ``net_quantity``, ``realized_pnl`` and ``avg_cost``
    (``avg_cost`` is 0 when ``net_quantity`` is 0). Floating P&L on open
    positions is not computed here.
    """
    if not trades:
        return {"net_quantity": 0, "realized_pnl": 0.0, "avg_cost": 0.0}

    sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

    position_qty = 0
    avg_cost = 0.0
    realized_pnl = 0.0

    for t in sorted_trades:
        side_raw = (t.get("side") or t.get("action", "")).upper()
        if side_raw not in ("BUY", "SELL"):
            continue

        qty = (
            t.get("filled_size")
            or t.get("quantity")
            or t.get("size")
            or 0
        )
        price = (
            t.get("filled_price")
            or t.get("traded_price")
            or t.get("avg_price")
            or t.get("price")
            or t.get("target_price")
            or 0.0
        )
        commission = (
            t.get("total_fee")
            if t.get("total_fee") is not None
            else (t.get("commission", 0.0) or 0.0)
        )

        if not qty or not price:
            continue

        if side_raw == "BUY":
            total_cost = avg_cost * position_qty + price * qty + commission
            position_qty += qty
            if position_qty > 0:
                avg_cost = total_cost / position_qty
        else:  # SELL
            if position_qty <= 0:
                sell_qty = 0
            else:
                sell_qty = min(qty, position_qty)
                realized_pnl += (price - avg_cost) * sell_qty - commission
                position_qty -= sell_qty

            if position_qty <= 0:
                position_qty = 0
                avg_cost = 0.0

    return {
        "net_quantity": position_qty,
        "realized_pnl": round(realized_pnl, 2),
        "avg_cost": round(avg_cost, 4) if position_qty > 0 else 0.0,
    }


def group_executions_by_order(executions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse executions to one representative doc per ``order_id``.

    Keeps the doc with the largest ``timestamp`` per order so replayed broker
    fill reports do not get double-counted in statistics or views.
    """
    if not executions:
        return []

    grouped: Dict[str, Dict[str, Any]] = {}
    for doc in executions:
        order_id = doc.get("order_id") or str(doc.get("_id"))
        current = grouped.get(order_id)
        if (not current) or (doc.get("timestamp", 0) > current.get("timestamp", 0)):
            grouped[order_id] = doc

    return list(grouped.values())
