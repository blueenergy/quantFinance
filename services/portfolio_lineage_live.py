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


def _execution_status(row: Dict[str, Any]) -> str:
    return str(row.get("status") or row.get("execution_status") or "").lower()


def _first_present(row: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row.get(key)
    return None


def _execution_qty(row: Dict[str, Any]) -> float:
    explicit_fill = _first_present(row, ("filled_size", "filled_qty"))
    if explicit_fill is not None:
        return _numeric(explicit_fill)

    status = _execution_status(row)
    unfilled_terminal = {"cancelled", "canceled", "rejected", "failed", "expired"}
    if status in unfilled_terminal:
        return 0.0

    qty = row.get("quantity") or row.get("size") or 0
    if status and status not in {"filled", "partial_filled", "partial_cancelled"}:
        return 0.0
    return _numeric(qty)


def _execution_price(row: Dict[str, Any]) -> float:
    return _numeric(
        row.get("filled_price")
        or row.get("traded_price")
        or row.get("avg_price")
        or row.get("price")
        or row.get("target_price")
        or 0.0
    )


def _execution_fee(row: Dict[str, Any]) -> float:
    if row.get("total_fee") is not None:
        return _numeric(row.get("total_fee"))
    return _numeric(row.get("commission") if row.get("commission") is not None else row.get("fee"))


def _execution_date(row: Dict[str, Any]) -> str:
    value = (
        row.get("display_time")
        or row.get("created_at_display")
        or row.get("created_at")
        or row.get("timestamp")
        or ""
    )
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


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _trades_for_ledger(executions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize execution docs for ``compute_realized_pnl_for_group``."""
    out: List[Dict[str, Any]] = []
    for row in executions:
        side = _execution_side(row)
        if side not in ("BUY", "SELL"):
            continue
        qty = _execution_qty(row)
        if not qty:
            continue
        doc = dict(row)
        doc["filled_size"] = qty
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


def build_lineage_live_trades(
    executions: List[Dict[str, Any]],
    price_map: Optional[Dict[str, float]] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Build FIFO round-trip rows from lineage live executions.

    One output row is one FIFO match between a buy lot and a sell execution.
    Remaining buy lots are emitted as open rows using ``price_map`` for floating
    P&L when a latest price is available.
    """

    latest_prices = price_map or {}
    names = name_map or {}
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in executions:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        by_symbol.setdefault(symbol, []).append(row)

    out: List[Dict[str, Any]] = []
    seq = 0
    for symbol in sorted(by_symbol.keys()):
        lots: List[Dict[str, Any]] = []
        rows = sorted(by_symbol[symbol], key=_execution_ts)
        for row in rows:
            side = _execution_side(row)
            qty = _execution_qty(row)
            price = _execution_price(row)
            if side not in ("BUY", "SELL") or qty <= 0 or price <= 0:
                continue

            fee = _execution_fee(row)
            name = row.get("name") or row.get("stock_name") or names.get(symbol) or names.get(_symbol_key(symbol)) or ""
            if side == "BUY":
                lots.append(
                    {
                        "row": row,
                        "symbol": symbol,
                        "name": name,
                        "quantity": qty,
                        "remaining": qty,
                        "price": price,
                        "fee": fee,
                        "date": _execution_date(row),
                        "rebalance_date": row.get("source_plan_base_date"),
                    }
                )
                continue

            sell_remaining = qty
            while sell_remaining > 0 and lots:
                lot = lots[0]
                match_qty = min(_numeric(lot.get("remaining")), sell_remaining)
                if match_qty <= 0:
                    lots.pop(0)
                    continue

                buy_qty = _numeric(lot.get("quantity"))
                buy_fee = _numeric(lot.get("fee")) * match_qty / buy_qty if buy_qty > 0 else 0.0
                sell_fee = fee * match_qty / qty if qty > 0 else 0.0
                total_fee = buy_fee + sell_fee
                buy_price = _numeric(lot.get("price"))
                holding_return = price / buy_price - 1 if buy_price > 0 else None
                net_pnl = (price - buy_price) * match_qty - total_fee
                seq += 1
                out.append(
                    {
                        "trade_id": f"{symbol}-{seq}",
                        "status": "closed",
                        "rebalance_date": lot.get("rebalance_date"),
                        "symbol": symbol,
                        "name": lot.get("name") or name,
                        "buy_date": lot.get("date") or "",
                        "buy_price": round(buy_price, 4),
                        "sell_date": _execution_date(row),
                        "sell_price": round(price, 4),
                        "quantity": int(match_qty) if float(match_qty).is_integer() else round(match_qty, 4),
                        "buy_amount": round(buy_price * match_qty, 2),
                        "holding_return": _round_or_none(holding_return, 6),
                        "net_pnl": round(net_pnl, 2),
                        "fee": round(total_fee, 2),
                        "source_plan_id": lot["row"].get("source_plan_id"),
                        "sell_source_plan_id": row.get("source_plan_id"),
                        "buy_order_id": lot["row"].get("order_id"),
                        "sell_order_id": row.get("order_id"),
                    }
                )

                lot["remaining"] = _numeric(lot.get("remaining")) - match_qty
                sell_remaining -= match_qty
                if _numeric(lot.get("remaining")) <= 0:
                    lots.pop(0)

        latest_price = latest_prices.get(symbol) or latest_prices.get(_symbol_key(symbol))
        for lot in lots:
            remaining = _numeric(lot.get("remaining"))
            if remaining <= 0:
                continue
            buy_price = _numeric(lot.get("price"))
            buy_qty = _numeric(lot.get("quantity"))
            buy_fee = _numeric(lot.get("fee")) * remaining / buy_qty if buy_qty > 0 else 0.0
            mark_price = _numeric(latest_price)
            holding_return = mark_price / buy_price - 1 if mark_price > 0 and buy_price > 0 else None
            net_pnl = (mark_price - buy_price) * remaining - buy_fee if mark_price > 0 else None
            seq += 1
            out.append(
                {
                    "trade_id": f"{symbol}-{seq}",
                    "status": "open",
                    "rebalance_date": lot.get("rebalance_date"),
                    "symbol": symbol,
                    "name": lot.get("name") or names.get(symbol) or names.get(_symbol_key(symbol)) or "",
                    "buy_date": lot.get("date") or "",
                    "buy_price": round(buy_price, 4),
                    "sell_date": "",
                    "sell_price": None,
                    "quantity": int(remaining) if float(remaining).is_integer() else round(remaining, 4),
                    "buy_amount": round(buy_price * remaining, 2),
                    "holding_return": _round_or_none(holding_return, 6),
                    "net_pnl": _round_or_none(net_pnl, 2),
                    "fee": round(buy_fee, 2),
                    "source_plan_id": lot["row"].get("source_plan_id"),
                    "sell_source_plan_id": None,
                    "buy_order_id": lot["row"].get("order_id"),
                    "sell_order_id": None,
                }
            )

    return out


def build_lineage_live_positions(
    executions: List[Dict[str, Any]],
    price_map: Dict[str, float],
    realtime_docs: Optional[Dict[str, Dict[str, Any]]] = None,
    name_map: Optional[Dict[str, str]] = None,
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
            names = name_map or {}
            first_name = next(
                (
                    row.get("name") or row.get("stock_name") or ""
                    for row in grouped[symbol]
                    if row.get("name") or row.get("stock_name")
                ),
                "",
            )
            positions.append(
                {
                    "symbol": symbol,
                    "name": first_name or names.get(symbol) or names.get(_symbol_key(symbol)) or "",
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
