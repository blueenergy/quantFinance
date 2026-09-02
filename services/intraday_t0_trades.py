"""Intraday T+0 manual trade ledger (Phase 2 — semi-automated follow)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from db import get_db_collections
from services.intraday_t0_signals import (
    compact_trade_date,
    ensure_intraday_t0_indexes,
    latest_market_spectrum,
    now_ts,
    to_float,
    to_int,
)
from services.live_trade_signals import in_flight_sell_qty
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol

log = logging.getLogger(__name__)

LEG_TYPES = {"sell", "buy"}
PAIR_STYLES = {"sell_first", "buy_first"}
DEFAULT_SELL_QTY_RATIO = 0.5
DEFAULT_BUY_QTY_RATIO = 0.5
_TRADE_INDEXES_READY = False


def ensure_intraday_t0_trade_indexes() -> None:
    global _TRADE_INDEXES_READY
    if _TRADE_INDEXES_READY:
        return
    ensure_intraday_t0_indexes()
    coll = get_db_collections()["intraday_t0_trades"]
    coll.create_index(
        [("user_id", ASCENDING), ("trade_date", DESCENDING), ("created_at", DESCENDING)],
        name="user_trade_date_idx",
    )
    coll.create_index(
        [("user_id", ASCENDING), ("pair_id", ASCENDING)],
        name="user_pair_idx",
    )
    coll.create_index(
        [("user_id", ASCENDING), ("symbol", ASCENDING), ("trade_date", DESCENDING)],
        name="user_symbol_trade_date_idx",
    )
    _TRADE_INDEXES_READY = True


def _trades_coll():
    return get_db_collections()["intraday_t0_trades"]


def _load_signal_meta(signal_id: Optional[str], user_id: str) -> Dict[str, Any]:
    if not signal_id:
        return {}
    try:
        oid = ObjectId(signal_id)
    except Exception:
        return {}
    doc = get_db_collections()["intraday_t0_signals"].find_one(
        {"_id": oid, "user_id": user_id},
        {"signal_type": 1, "signal_score": 1, "snapshot_time": 1, "symbol": 1},
    )
    if not doc:
        return {}
    return {
        "signal_type": doc.get("signal_type"),
        "signal_score": doc.get("signal_score"),
        "signal_snapshot_time": doc.get("snapshot_time"),
        "signal_symbol": doc.get("symbol"),
    }


def _broker_available_qty(user_id: str, symbol: str) -> int:
    normalized = normalize_symbol(symbol)
    doc = get_db_collections()["trader_positions"].find_one(
        {"user_id": user_id, "symbol": normalized},
        {"available_qty": 1, "can_use_volume": 1, "qty": 1},
    )
    if not doc:
        for cand in {normalized, normalized.split(".")[0]}:
            doc = get_db_collections()["trader_positions"].find_one(
                {"user_id": user_id, "symbol": cand},
                {"available_qty": 1, "can_use_volume": 1, "qty": 1},
            )
            if doc:
                break
    if not doc:
        return 0
    snapshot_qty = to_int(doc.get("available_qty", doc.get("can_use_volume", doc.get("qty", 0))))
    # Same-day sellable shares are non-renewable under T+1 settlement, and the
    # main rebalance draws on the same pool. Yield to sell orders already in
    # flight — a missed T+0 leg is cheaper than an unfilled strategy reduction.
    reserved = in_flight_sell_qty(
        get_db_collections(),
        user_id,
        {normalized, normalized.split(".")[0]},
    )
    if reserved:
        log.info(
            "T+0 available qty for %s reduced from %s to %s by %s shares of in-flight sells",
            normalized,
            snapshot_qty,
            max(0, snapshot_qty - reserved),
            reserved,
        )
    return max(0, snapshot_qty - reserved)


def _round_lot(qty: int) -> int:
    if qty <= 0:
        return 0
    rounded = qty // 100 * 100
    return rounded if rounded >= 100 else (100 if qty >= 100 else 0)


def _pair_legs_from_rows(rows: List[Dict[str, Any]]) -> Tuple[str, int, int]:
    """Return (opening_leg, sell_qty, buy_qty) for a pair's trade rows."""
    if not rows:
        return "sell", 0, 0
    ordered = sorted(rows, key=lambda r: (r.get("created_at") or "", str(r.get("_id") or "")))
    opening_leg = str(ordered[0].get("leg") or "sell")
    sell_qty = sum(to_int(r.get("quantity")) for r in rows if r.get("leg") == "sell")
    buy_qty = sum(to_int(r.get("quantity")) for r in rows if r.get("leg") == "buy")
    return opening_leg, sell_qty, buy_qty


def _pair_style_from_opening_leg(opening_leg: str) -> str:
    return "buy_first" if opening_leg == "buy" else "sell_first"


def _uncovered_for_style(style: str, sell_qty: int, buy_qty: int) -> int:
    if style == "buy_first":
        return max(0, buy_qty - sell_qty)
    return max(0, sell_qty - buy_qty)


def _pair_state_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    opening_leg, sell_qty, buy_qty = _pair_legs_from_rows(rows)
    style = _pair_style_from_opening_leg(opening_leg)
    uncovered = _uncovered_for_style(style, sell_qty, buy_qty)
    matched_qty = min(sell_qty, buy_qty)
    if uncovered > 0:
        status = "open"
    elif matched_qty > 0:
        status = "closed"
    else:
        status = "open"
    return {
        "style": style,
        "opening_leg": opening_leg,
        "sell_qty": sell_qty,
        "buy_qty": buy_qty,
        "uncovered_qty": uncovered,
        "matched_qty": matched_qty,
        "status": status,
    }


def _load_pair_rows(user_id: str, pair_id: str) -> List[Dict[str, Any]]:
    return list(_trades_coll().find({"user_id": user_id, "pair_id": pair_id}))


def _pair_info(user_id: str, pair_id: str) -> Dict[str, Any]:
    rows = _load_pair_rows(user_id, pair_id)
    if not rows:
        raise ValueError("pair_id not found")
    state = _pair_state_from_rows(rows)
    return {
        **state,
        "pair_id": pair_id,
        "symbol": normalize_symbol(str(rows[0].get("symbol") or "")),
        "trade_date": rows[0].get("trade_date"),
    }


def _try_pair_info(user_id: str, pair_id: str) -> Optional[Dict[str, Any]]:
    try:
        return _pair_info(user_id, pair_id)
    except ValueError:
        return None


def _uncovered_qty_for_pair(user_id: str, pair_id: str) -> int:
    rows = _load_pair_rows(user_id, pair_id)
    if not rows:
        return 0
    state = _pair_state_from_rows(rows)
    return to_int(state.get("uncovered_qty"))


def _buy_first_reserved_qty(user_id: str, symbol: str, *, exclude_pair_id: Optional[str] = None) -> int:
    normalized = normalize_symbol(symbol)
    reserved = 0
    for pair in list_open_pairs(user_id):
        if exclude_pair_id and pair.get("pair_id") == exclude_pair_id:
            continue
        if pair.get("style") != "buy_first":
            continue
        if normalize_symbol(str(pair.get("symbol") or "")) != normalized:
            continue
        reserved += to_int(pair.get("uncovered_qty"))
    return reserved


def _buy_first_open_capacity(user_id: str, symbol: str, *, exclude_pair_id: Optional[str] = None) -> int:
    available = _broker_available_qty(user_id, symbol)
    reserved = _buy_first_reserved_qty(user_id, symbol, exclude_pair_id=exclude_pair_id)
    return max(0, available - reserved)


def find_open_pair_for_symbol(user_id: str, symbol: str, trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    normalized = normalize_symbol(symbol)
    candidates = [
        p
        for p in list_open_pairs(user_id, trade_date=trade_date)
        if normalize_symbol(str(p.get("symbol") or "")) == normalized
    ]
    return candidates[-1] if candidates else None


def preview_trade_from_signal(
    user_id: str,
    signal_id: str,
    sell_qty_ratio: float = DEFAULT_SELL_QTY_RATIO,
    buy_qty_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    ensure_intraday_t0_trade_indexes()
    meta = _load_signal_meta(signal_id, user_id)
    if not meta:
        raise ValueError("signal not found")
    symbol = normalize_symbol(str(meta.get("signal_symbol") or ""))
    if not symbol:
        raise ValueError("signal has no symbol")
    available = _broker_available_qty(user_id, symbol)
    ratio = float(buy_qty_ratio if buy_qty_ratio is not None else sell_qty_ratio)
    signal_type = str(meta.get("signal_type") or "")
    open_pair = find_open_pair_for_symbol(user_id, symbol)
    warnings: List[str] = []
    suggested_qty = 0
    pair_style: Optional[str] = None

    if signal_type == "SELL_WATCH":
        if open_pair and open_pair.get("style") == "buy_first":
            pair_style = "buy_first"
            uncovered = to_int(open_pair.get("uncovered_qty"))
            suggested_qty = min(_round_lot(uncovered), _round_lot(available)) if uncovered > 0 else 0
            if uncovered > 0 and available <= 0:
                warnings.append("no available_qty to close buy_first pair")
        else:
            pair_style = "sell_first"
            suggested_qty = _round_lot(int(available * sell_qty_ratio))
            if suggested_qty < 100 and available >= 100:
                suggested_qty = 100
            if suggested_qty <= 0:
                warnings.append("no available_qty for sell preview")
    elif signal_type == "BUY_WATCH":
        if open_pair and open_pair.get("style") == "sell_first":
            pair_style = "sell_first"
            uncovered = to_int(open_pair.get("uncovered_qty"))
            suggested_qty = _round_lot(uncovered)
            if uncovered <= 0:
                warnings.append("open sell_first pair has no uncovered quantity")
        else:
            pair_style = "buy_first"
            capacity = _buy_first_open_capacity(user_id, symbol)
            suggested_qty = _round_lot(int(capacity * ratio))
            if suggested_qty < 100 and capacity >= 100:
                suggested_qty = 100
            if capacity <= 0:
                warnings.append("no buy_first capacity — available_qty exhausted or zero")
            elif suggested_qty <= 0:
                warnings.append("buy_first capacity below one lot after reservation")

    if signal_type not in {"SELL_WATCH", "BUY_WATCH"}:
        warnings.append("signal is not a WATCH type")
    market = latest_market_spectrum(compact_trade_date())
    yang = to_float(market.get("yang_spectrum"))
    if yang is not None and yang < 0.35:
        warnings.append("market_yang_spectrum below 0.35")

    return {
        "success": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_type": signal_type,
        "signal_score": meta.get("signal_score"),
        "available_qty": available,
        "suggested_qty": max(0, suggested_qty),
        "sell_qty_ratio": sell_qty_ratio,
        "buy_qty_ratio": ratio,
        "pair_style": pair_style,
        "open_pair_id": open_pair.get("pair_id") if open_pair else None,
        "warnings": warnings,
    }


def record_trade(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_intraday_t0_trade_indexes()
    leg = str(payload.get("leg") or "").lower().strip()
    if leg not in LEG_TYPES:
        raise ValueError("leg must be sell or buy")
    symbol = normalize_symbol(str(payload.get("symbol") or "").strip())
    if not symbol:
        raise ValueError("symbol required")
    quantity = to_int(payload.get("quantity"))
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    price = to_float(payload.get("price"))
    if price is None or price <= 0:
        raise ValueError("price must be positive")
    trade_date = compact_trade_date(payload.get("trade_date"))
    fees = to_float(payload.get("fees"), 0.0) or 0.0
    signal_id = str(payload.get("signal_id") or "").strip() or None
    pair_id = str(payload.get("pair_id") or "").strip() or None
    notes = str(payload.get("notes") or "").strip()

    signal_meta = _load_signal_meta(signal_id, user_id)
    if signal_id and not signal_meta:
        raise ValueError("signal_id not found")

    if leg == "sell":
        if pair_id:
            pair = _try_pair_info(user_id, pair_id)
            if pair:
                if pair.get("style") != "buy_first":
                    raise ValueError("sell with pair_id only closes buy_first pairs")
                if normalize_symbol(str(pair.get("symbol") or "")) != symbol:
                    raise ValueError("pair_id symbol mismatch")
                uncovered = to_int(pair.get("uncovered_qty"))
                if uncovered <= 0:
                    raise ValueError("open pair has no uncovered quantity")
                available = _broker_available_qty(user_id, symbol)
                max_qty = min(uncovered, available) if available > 0 else uncovered
                if quantity > max_qty:
                    raise ValueError(f"sell quantity exceeds close limit ({max_qty})")
            else:
                open_pair = find_open_pair_for_symbol(user_id, symbol, trade_date=trade_date)
                if open_pair:
                    raise ValueError("open pair exists — close it before opening a new sell_first pair")
                available = _broker_available_qty(user_id, symbol)
                if available > 0 and quantity > available:
                    raise ValueError(f"quantity exceeds available_qty ({available})")
        else:
            open_pair = find_open_pair_for_symbol(user_id, symbol, trade_date=trade_date)
            if open_pair:
                raise ValueError("open pair exists — close it before opening a new sell_first pair")
            pair_id = str(uuid.uuid4())
            available = _broker_available_qty(user_id, symbol)
            if available > 0 and quantity > available:
                raise ValueError(f"quantity exceeds available_qty ({available})")
    else:
        if pair_id:
            pair = _try_pair_info(user_id, pair_id)
            if pair:
                if pair.get("style") == "buy_first":
                    raise ValueError("cannot add buy leg to an open buy_first pair")
                if normalize_symbol(str(pair.get("symbol") or "")) != symbol:
                    raise ValueError("pair_id symbol mismatch")
                uncovered = to_int(pair.get("uncovered_qty"))
                if quantity > uncovered:
                    raise ValueError(f"buy quantity exceeds uncovered sell qty ({uncovered})")
            else:
                open_pair = find_open_pair_for_symbol(user_id, symbol, trade_date=trade_date)
                if open_pair:
                    raise ValueError("open pair exists — close it before opening a new buy_first pair")
                capacity = _buy_first_open_capacity(user_id, symbol)
                if capacity <= 0:
                    raise ValueError("no buy_first capacity — available_qty exhausted or zero")
                if quantity > capacity:
                    raise ValueError(f"buy quantity exceeds buy_first capacity ({capacity})")
        else:
            open_pair = find_open_pair_for_symbol(user_id, symbol, trade_date=trade_date)
            if open_pair:
                raise ValueError("open pair exists — close it before opening a new buy_first pair")
            capacity = _buy_first_open_capacity(user_id, symbol)
            if capacity <= 0:
                raise ValueError("no buy_first capacity — available_qty exhausted or zero")
            if quantity > capacity:
                raise ValueError(f"buy quantity exceeds buy_first capacity ({capacity})")
            pair_id = str(uuid.uuid4())

    doc = {
        "user_id": user_id,
        "trade_date": trade_date,
        "symbol": symbol,
        "leg": leg,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "signal_id": signal_id,
        "pair_id": pair_id,
        "notes": notes,
        "created_at": now_ts(),
        **signal_meta,
    }
    result = _trades_coll().insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"success": True, "data": serialize_doc(doc)}


def trade_exists_for_signal(user_id: str, signal_id: str) -> bool:
    ensure_intraday_t0_trade_indexes()
    return (
        _trades_coll().find_one(
            {"user_id": user_id, "signal_id": signal_id},
            {"_id": 1},
        )
        is not None
    )


def list_trades(
    user_id: str,
    trade_date: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    ensure_intraday_t0_trade_indexes()
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    if symbol:
        query["symbol"] = normalize_symbol(symbol)
    rows = (
        _trades_coll()
        .find(query)
        .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        .limit(limit)
    )
    return [serialize_doc(doc) for doc in rows]


def _aggregate_pairs(user_id: str, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in _trades_coll().find(query).sort([("created_at", ASCENDING)]):
        pair_id = str(row.get("pair_id") or "")
        if not pair_id:
            continue
        bucket = buckets.setdefault(
            pair_id,
            {
                "pair_id": pair_id,
                "user_id": user_id,
                "trade_date": row.get("trade_date"),
                "symbol": row.get("symbol"),
                "opening_leg": None,
                "sell_qty": 0,
                "buy_qty": 0,
                "sell_notional": 0.0,
                "buy_notional": 0.0,
                "fees": 0.0,
                "sell_trades": [],
                "buy_trades": [],
                "trade_rows": [],
            },
        )
        if bucket["opening_leg"] is None:
            bucket["opening_leg"] = row.get("leg")
        bucket["trade_rows"].append(row)
        qty = to_int(row.get("quantity"))
        price = to_float(row.get("price"), 0.0) or 0.0
        fees = to_float(row.get("fees"), 0.0) or 0.0
        bucket["fees"] += fees
        trade_ref = {
            "id": str(row.get("_id")),
            "quantity": qty,
            "price": price,
            "fees": fees,
            "signal_id": row.get("signal_id"),
            "created_at": row.get("created_at"),
        }
        if row.get("leg") == "sell":
            bucket["sell_qty"] += qty
            bucket["sell_notional"] += qty * price
            bucket["sell_trades"].append(trade_ref)
        elif row.get("leg") == "buy":
            bucket["buy_qty"] += qty
            bucket["buy_notional"] += qty * price
            bucket["buy_trades"].append(trade_ref)

    pairs: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        sell_qty = bucket["sell_qty"]
        buy_qty = bucket["buy_qty"]
        opening_leg = str(bucket.get("opening_leg") or "sell")
        style = _pair_style_from_opening_leg(opening_leg)
        uncovered = _uncovered_for_style(style, sell_qty, buy_qty)
        matched_qty = min(sell_qty, buy_qty)
        avg_sell = bucket["sell_notional"] / sell_qty if sell_qty else None
        avg_buy = bucket["buy_notional"] / buy_qty if buy_qty else None
        gross_pnl = None
        net_pnl = None
        if matched_qty > 0 and avg_sell is not None and avg_buy is not None:
            gross_pnl = (avg_sell - avg_buy) * matched_qty
            net_pnl = gross_pnl - bucket["fees"]
        if uncovered > 0:
            status = "open"
        elif matched_qty > 0:
            status = "closed"
        else:
            status = "open"
        pairs.append(
            {
                **{k: v for k, v in bucket.items() if k != "trade_rows"},
                "style": style,
                "opening_leg": opening_leg,
                "uncovered_qty": uncovered,
                "matched_qty": matched_qty,
                "avg_sell_price": avg_sell,
                "avg_buy_price": avg_buy,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "status": status,
            }
        )
    pairs.sort(key=lambda p: (p.get("trade_date", ""), p.get("symbol", ""), p.get("pair_id", "")), reverse=True)
    return pairs


def list_open_pairs(user_id: str, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
    return [p for p in _aggregate_pairs(user_id, trade_date) if p.get("status") == "open"]


def day_summary(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    td = compact_trade_date(trade_date)
    pairs = _aggregate_pairs(user_id, td)
    closed = [p for p in pairs if p.get("status") == "closed"]
    open_pairs = [p for p in pairs if p.get("status") == "open"]
    net_values = [to_float(p.get("net_pnl")) for p in closed]
    net_values = [v for v in net_values if v is not None]
    gross_values = [to_float(p.get("gross_pnl")) for p in closed]
    gross_values = [v for v in gross_values if v is not None]
    naked_sell_qty = sum(
        to_int(p.get("uncovered_qty"))
        for p in open_pairs
        if p.get("style") == "sell_first"
    )
    naked_buy_qty = sum(
        to_int(p.get("uncovered_qty"))
        for p in open_pairs
        if p.get("style") == "buy_first"
    )
    return {
        "success": True,
        "trade_date": td,
        "pairs_count": len(pairs),
        "closed_pairs": len(closed),
        "open_pairs": len(open_pairs),
        "naked_sell_qty": naked_sell_qty,
        "naked_buy_qty": naked_buy_qty,
        "naked_qty": naked_sell_qty + naked_buy_qty,
        "total_gross_pnl": sum(gross_values) if gross_values else 0.0,
        "total_net_pnl": sum(net_values) if net_values else 0.0,
        "total_fees": sum(to_float(p.get("fees"), 0.0) or 0.0 for p in pairs),
        "open_pair_details": open_pairs,
        "closed_pair_details": closed,
    }


def day_close_check(user_id: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
    summary = day_summary(user_id, trade_date)
    alerts: List[Dict[str, Any]] = []
    for pair in summary.get("open_pair_details") or []:
        uncovered = to_int(pair.get("uncovered_qty"))
        if uncovered <= 0:
            continue
        style = str(pair.get("style") or "sell_first")
        if style == "buy_first":
            alerts.append(
                {
                    "level": "warning",
                    "code": "uncovered_buy",
                    "pair_id": pair.get("pair_id"),
                    "symbol": pair.get("symbol"),
                    "style": style,
                    "uncovered_qty": uncovered,
                    "message": (
                        f"{pair.get('symbol')} has {uncovered} shares bought but not sold back "
                        "(will hold overnight and tie up cash)"
                    ),
                }
            )
        else:
            alerts.append(
                {
                    "level": "warning",
                    "code": "uncovered_sell",
                    "pair_id": pair.get("pair_id"),
                    "symbol": pair.get("symbol"),
                    "style": style,
                    "uncovered_qty": uncovered,
                    "message": f"{pair.get('symbol')} has {uncovered} shares sold but not bought back",
                }
            )
    return {
        "success": True,
        "trade_date": summary.get("trade_date"),
        "alerts": alerts,
        "alert_count": len(alerts),
        "requires_action": len(alerts) > 0,
        "summary": summary,
    }
