"""Cross-plan (lineage) paper execution aggregation - pure helpers, no I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List


def _symbol_key(value: Any) -> str:
    return str(value or "").split(".", 1)[0]


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit() and len(text) == 8:
        return text
    return text[:10].replace("-", "")


def _sort_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw / 1000 if raw > 1_000_000_000_000 else raw
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.isdigit():
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").timestamp()
        raw = float(text[:13] if len(text) >= 13 else text)
        return raw / 1000 if raw > 1_000_000_000_000 else raw
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _execution_ts(row: Dict[str, Any]) -> float:
    return max(
        _sort_ts(row.get("created_at")),
        _sort_ts(row.get("execute_date")),
        _sort_ts(row.get("timestamp")),
    )


def _normalized_action(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"buy", "b", "买", "买入"}:
        return "buy"
    if raw in {"sell", "s", "卖", "卖出"}:
        return "sell"
    return raw


def merge_lineage_paper_executions(
    executions: List[Dict[str, Any]],
    plan_meta: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize paper executions for FIFO trade pairing across lineage plans."""

    merged: List[Dict[str, Any]] = []
    for row in executions:
        action = _normalized_action(row.get("action"))
        if action not in {"buy", "sell"}:
            continue
        quantity = int(row.get("quantity") or 0)
        if quantity <= 0:
            continue
        plan_id = str(row.get("plan_id") or "")
        meta = plan_meta.get(plan_id) or {}
        execute_date = row.get("execute_date") or meta.get("base_date")
        price = row.get("price")
        execution_id = str(row.get("execution_id") or row.get("_id") or "")
        merged.append(
            {
                **row,
                "execution_id": execution_id or row.get("execution_id"),
                "order_id": execution_id or row.get("order_id") or row.get("execution_id"),
                "side": action.upper(),
                "action": action,
                "filled_size": quantity,
                "filled_price": price,
                "quantity": quantity,
                "price": price,
                "fee": 0.0,
                "total_fee": 0.0,
                "timestamp": _execution_ts({**row, "execute_date": execute_date}),
                "display_time": execute_date,
                "source_plan_id": plan_id or None,
                "source_plan_base_date": meta.get("base_date") or execute_date,
                "name": row.get("name") or "",
            }
        )
    merged.sort(key=_execution_ts)
    return merged


def build_lineage_paper_trades(
    executions: List[Dict[str, Any]],
    price_map: Dict[str, float] | None = None,
    name_map: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    """Build FIFO rows from paper executions.

    Paper plans have historically used both full A-share symbols and bare
    six-digit symbols, so matching uses the base symbol while display keeps the
    buy-lot symbol.
    """

    latest_prices = price_map or {}
    names = name_map or {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in executions:
        key = _symbol_key(row.get("symbol"))
        if key:
            grouped.setdefault(key, []).append(row)

    out: List[Dict[str, Any]] = []
    seq = 0
    for key in sorted(grouped.keys()):
        lots: List[Dict[str, Any]] = []
        for row in sorted(grouped[key], key=_execution_ts):
            action = _normalized_action(row.get("action") or row.get("side"))
            qty = _numeric(row.get("quantity") or row.get("filled_size"))
            price = _numeric(row.get("price") or row.get("filled_price"))
            if action not in {"buy", "sell"} or qty <= 0 or price <= 0:
                continue
            symbol = str(row.get("symbol") or key)
            name = row.get("name") or names.get(symbol) or names.get(key) or ""
            if action == "buy":
                lots.append(
                    {
                        "row": row,
                        "symbol": symbol,
                        "name": name,
                        "quantity": qty,
                        "remaining": qty,
                        "price": price,
                        "date": _date_text(row.get("execute_date") or row.get("display_time")),
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
                buy_price = _numeric(lot.get("price"))
                holding_return = price / buy_price - 1 if buy_price > 0 else None
                net_pnl = (price - buy_price) * match_qty
                seq += 1
                out.append(
                    {
                        "trade_id": f"{key}-{seq}",
                        "status": "closed",
                        "rebalance_date": lot.get("rebalance_date"),
                        "symbol": lot.get("symbol") or symbol,
                        "name": lot.get("name") or name,
                        "buy_date": lot.get("date") or "",
                        "buy_price": round(buy_price, 4),
                        "sell_date": _date_text(row.get("execute_date") or row.get("display_time")),
                        "sell_price": round(price, 4),
                        "quantity": int(match_qty) if float(match_qty).is_integer() else round(match_qty, 4),
                        "buy_amount": round(buy_price * match_qty, 2),
                        "holding_return": round(holding_return, 6) if holding_return is not None else None,
                        "net_pnl": round(net_pnl, 2),
                        "fee": 0.0,
                        "source_plan_id": lot["row"].get("source_plan_id"),
                        "sell_source_plan_id": row.get("source_plan_id"),
                        "buy_order_id": lot["row"].get("execution_id"),
                        "sell_order_id": row.get("execution_id"),
                    }
                )
                lot["remaining"] = _numeric(lot.get("remaining")) - match_qty
                sell_remaining -= match_qty
                if _numeric(lot.get("remaining")) <= 0:
                    lots.pop(0)

        for lot in lots:
            remaining = _numeric(lot.get("remaining"))
            if remaining <= 0:
                continue
            symbol = str(lot.get("symbol") or key)
            buy_price = _numeric(lot.get("price"))
            latest_price = _numeric(latest_prices.get(symbol) or latest_prices.get(key))
            holding_return = latest_price / buy_price - 1 if latest_price > 0 and buy_price > 0 else None
            net_pnl = (latest_price - buy_price) * remaining if latest_price > 0 else None
            seq += 1
            out.append(
                {
                    "trade_id": f"{key}-{seq}",
                    "status": "open",
                    "rebalance_date": lot.get("rebalance_date"),
                    "symbol": symbol,
                    "name": lot.get("name") or names.get(symbol) or names.get(key) or "",
                    "buy_date": lot.get("date") or "",
                    "buy_price": round(buy_price, 4),
                    "sell_date": "",
                    "sell_price": None,
                    "quantity": int(remaining) if float(remaining).is_integer() else round(remaining, 4),
                    "buy_amount": round(buy_price * remaining, 2),
                    "holding_return": round(holding_return, 6) if holding_return is not None else None,
                    "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
                    "fee": 0.0,
                    "source_plan_id": lot["row"].get("source_plan_id"),
                    "sell_source_plan_id": None,
                    "buy_order_id": lot["row"].get("execution_id"),
                    "sell_order_id": None,
                }
            )
    return out
