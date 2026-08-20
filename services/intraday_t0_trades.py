"""Intraday T+0 manual trade ledger (Phase 2 — semi-automated follow)."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

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
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol

LEG_TYPES = {"sell", "buy"}
DEFAULT_SELL_QTY_RATIO = 0.5
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
    return to_int(doc.get("available_qty", doc.get("can_use_volume", doc.get("qty", 0))))


def preview_trade_from_signal(
    user_id: str,
    signal_id: str,
    sell_qty_ratio: float = DEFAULT_SELL_QTY_RATIO,
) -> Dict[str, Any]:
    ensure_intraday_t0_trade_indexes()
    meta = _load_signal_meta(signal_id, user_id)
    if not meta:
        raise ValueError("signal not found")
    symbol = normalize_symbol(str(meta.get("signal_symbol") or ""))
    if not symbol:
        raise ValueError("signal has no symbol")
    available = _broker_available_qty(user_id, symbol)
    suggested_qty = int(available * sell_qty_ratio) // 100 * 100
    if suggested_qty < 100 and available >= 100:
        suggested_qty = 100
    warnings: List[str] = []
    if meta.get("signal_type") not in {"SELL_WATCH", "BUY_WATCH"}:
        warnings.append("signal is not a WATCH type")
    market = latest_market_spectrum(compact_trade_date())
    yang = to_float(market.get("yang_spectrum"))
    if yang is not None and yang < 0.35:
        warnings.append("market_yang_spectrum below 0.35")
    if meta.get("signal_type") == "SELL_WATCH" and suggested_qty <= 0:
        warnings.append("no available_qty for sell preview")
    return {
        "success": True,
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_type": meta.get("signal_type"),
        "signal_score": meta.get("signal_score"),
        "available_qty": available,
        "suggested_qty": max(0, suggested_qty),
        "sell_qty_ratio": sell_qty_ratio,
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
        if not pair_id:
            pair_id = str(uuid.uuid4())
        available = _broker_available_qty(user_id, symbol)
        if available > 0 and quantity > available:
            raise ValueError(f"quantity exceeds available_qty ({available})")
    else:
        if not pair_id:
            raise ValueError("pair_id required for buy leg")
        uncovered = _uncovered_qty_for_pair(user_id, pair_id)
        if quantity > uncovered:
            raise ValueError(f"buy quantity exceeds uncovered sell qty ({uncovered})")

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


def _uncovered_qty_for_pair(user_id: str, pair_id: str) -> int:
    sells = 0
    buys = 0
    for row in _trades_coll().find({"user_id": user_id, "pair_id": pair_id}):
        qty = to_int(row.get("quantity"))
        if row.get("leg") == "sell":
            sells += qty
        elif row.get("leg") == "buy":
            buys += qty
    return max(0, sells - buys)


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
                "sell_qty": 0,
                "buy_qty": 0,
                "sell_notional": 0.0,
                "buy_notional": 0.0,
                "fees": 0.0,
                "sell_trades": [],
                "buy_trades": [],
            },
        )
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
        uncovered = max(0, sell_qty - buy_qty)
        matched_qty = min(sell_qty, buy_qty)
        avg_sell = bucket["sell_notional"] / sell_qty if sell_qty else None
        avg_buy = bucket["buy_notional"] / buy_qty if buy_qty else None
        gross_pnl = None
        net_pnl = None
        if matched_qty > 0 and avg_sell is not None and avg_buy is not None:
            gross_pnl = (avg_sell - avg_buy) * matched_qty
            net_pnl = gross_pnl - bucket["fees"]
        if sell_qty == 0:
            status = "invalid"
        elif uncovered > 0:
            status = "open"
        else:
            status = "closed"
        pairs.append(
            {
                **bucket,
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
    return {
        "success": True,
        "trade_date": td,
        "pairs_count": len(pairs),
        "closed_pairs": len(closed),
        "open_pairs": len(open_pairs),
        "naked_qty": sum(to_int(p.get("uncovered_qty")) for p in open_pairs),
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
        if uncovered > 0:
            alerts.append(
                {
                    "level": "warning",
                    "code": "uncovered_sell",
                    "pair_id": pair.get("pair_id"),
                    "symbol": pair.get("symbol"),
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
