"""First-board execution planner (Phase A: shadow only).

Reads ``quant_data.first_board_candidates``, computes configurable target
shares from the account's equity/cash, and writes shadow intents to
``quant_trading.first_board_order_intents``.

This module deliberately does **not** write ``trade_signals``.  Live/paper
publishing reuses the portfolio-plan/live-rebalance path in a later phase.

Sizing modes:
  fixed_amount | fixed_shares | pct_equity | pct_cash | equal_weight

Every order is rounded down to ``lot_size`` and capped by:
  - ``max_positions``
  - ``max_position_pct`` of total equity (existing position included)
  - ``max_order_amount``
  - ``min_cash_reserve_pct`` of equity
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import json
import logging
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

SETTINGS_COLLECTION = "first_board_execution_settings"
INTENTS_COLLECTION = "first_board_order_intents"
RUNS_COLLECTION = "first_board_execution_runs"
DEFAULT_CANDIDATES_COLLECTION = "first_board_candidates"
MODEL_VERSION = "first_board_exec_v0_shadow"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "mode": "shadow",
    "sizing": {
        "mode": "fixed_amount",
        "value": 50_000.0,
        "max_positions": 3,
        "max_position_pct": 0.20,
        "min_cash_reserve_pct": 0.10,
        "lot_size": 100,
        "max_order_amount": 50_000.0,
    },
    "filters": {
        "min_score": 0.0,
        "max_candidates": 3,
        "exclude_bj": True,
        "exclude_st": True,
        "skip_existing_positions": True,
        "skip_if_limit_up": True,
    },
    "gates": {
        "sector": {
            "enabled": True,
            "level": "L1",
            "ma_period": 5,
            "min_above_ma5_ratio": 0.60,
            "confirm_level": "L3",
            "min_median_pct_chg": 0.0,
            "on_missing": "skip",
        }
    },
    "entry": {
        "order_type": "limit",
        "max_slippage_bps": 100,
        "activate_after": "09:31:00",
        "valid_until": "09:35:00",
    },
}


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(settings_collection: Any, account_key: str) -> Dict[str, Any]:
    doc = settings_collection.find_one(
        {
            "$or": [
                {"securities_account_id": account_key},
                {"account_id": account_key},
            ]
        }
    )
    if not doc:
        return dict(DEFAULT_SETTINGS)
    return _merge(DEFAULT_SETTINGS, doc)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _int(value: Any, default: int = 0) -> int:
    number = _num(value)
    return int(number) if number is not None else default


def _board_limit_pct(symbol: str, name: str = "") -> float:
    text = str(name or "").upper()
    if "ST" in text:
        return 0.05
    if symbol.endswith(".BJ"):
        return 0.30
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def _limit_price(pre_close: float, pct: float) -> float:
    return float(
        (Decimal(str(pre_close)) * (Decimal("1") + Decimal(str(pct)))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def _buy_price_ceiling(reference_price: float, max_slippage_bps: int) -> float:
    multiplier = Decimal("1") + Decimal(int(max_slippage_bps)) / Decimal("10000")
    return float(
        (Decimal(str(reference_price)) * multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
    )


def _reference_price(candidate: Mapping[str, Any], price_map: Mapping[str, float]) -> Optional[float]:
    symbol = str(candidate.get("symbol") or "")
    for value in (
        (price_map or {}).get(symbol),
        candidate.get("price_0925"),
        candidate.get("price_0920"),
        candidate.get("price_0915"),
    ):
        number = _num(value)
        if number is not None and number > 0:
            return number
    return None


def _pre_close(candidate: Mapping[str, Any], current_positions: Mapping[str, int], pre_close_map: Mapping[str, float]) -> Optional[float]:
    symbol = str(candidate.get("symbol") or "")
    direct = _num((pre_close_map or {}).get(symbol))
    if direct and direct > 0:
        return direct
    price = _num(candidate.get("price_0925"))
    gap = _num(candidate.get("gap_pct"))
    if price and price > 0 and gap is not None and gap > -100:
        return price / (1.0 + gap / 100.0)
    return None


def _current_shares(positions: Mapping[str, Mapping[str, Any]], symbol: str) -> int:
    row = positions.get(symbol) or {}
    for key in ("qty", "quantity", "volume", "shares", "current_volume", "total_volume"):
        value = _num(row.get(key))
        if value is not None:
            return int(value)
    return 0


def _position_map(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            out[symbol] = row
    return out


def load_sector_strength(
    data_db: Any,
    realtime_db: Any,
    symbols: Sequence[str],
    snapshot_time: Optional[str] = None,
    ma_period: int = 5,
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Attach SW L1 breadth + L3 median to each symbol.

    Gate chosen 2026-11-05: L1 ``above_ma5_ratio >= 0.60`` and L3
    ``median_pct_chg > 0``.  The L3 median is the confirmation leg; the L1
    breadth is the primary leg.
    """
    cleaned = sorted({str(symbol) for symbol in symbols if symbol})
    if not cleaned:
        return {}, None
    strength = realtime_db["realtime_sector_strength"]
    if snapshot_time is None:
        latest = strength.find_one(sort=[("snapshot_time", -1)])
        if not latest:
            return {}, None
        snapshot_time = str(latest.get("snapshot_time") or "")
    members = {
        str(doc.get("con_code")): doc
        for doc in data_db["sw_index_member"].find(
            {"con_code": {"$in": cleaned}, "src": "SW2021", "is_new": "Y"},
            {
                "_id": 0,
                "con_code": 1,
                "l1_code": 1,
                "l1_name": 1,
                "l3_code": 1,
                "l3_name": 1,
            },
        )
    }
    codes = {
        str(value)
        for doc in members.values()
        for value in (doc.get("l1_code"), doc.get("l3_code"))
        if value
    }
    by_code: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    if codes:
        for doc in strength.find(
            {
                "snapshot_time": snapshot_time,
                "ma_period": int(ma_period),
                "sector_code": {"$in": sorted(codes)},
            }
        ):
            by_code[(str(doc.get("level")), str(doc.get("sector_code")))] = doc
    out: Dict[str, Dict[str, Any]] = {}
    for symbol, member in members.items():
        l1 = by_code.get(("L1", str(member.get("l1_code")))) or {}
        l3 = by_code.get(("L3", str(member.get("l3_code")))) or {}
        out[symbol] = {
            "sector_l1_code": member.get("l1_code"),
            "sector_l1_name": member.get("l1_name"),
            "sector_l1_above_ma5_ratio": _num(l1.get("above_ma5_ratio")),
            "sector_l3_code": member.get("l3_code"),
            "sector_l3_name": member.get("l3_name"),
            "sector_l3_median_pct_chg": _num(l3.get("median_pct_chg")),
            "sector_snapshot_time": snapshot_time,
        }
    return out, snapshot_time


def _target_amount(
    *,
    mode: str,
    value: float,
    price: float,
    equity: float,
    available_cash: float,
    max_positions: int,
    max_position_pct: float,
) -> float:
    if mode == "fixed_shares":
        return value * price
    if mode == "pct_equity":
        return equity * value
    if mode == "pct_cash":
        return available_cash * value
    if mode == "equal_weight":
        per_slot = (available_cash / max(1, max_positions)) if max_positions > 0 else available_cash
        return min(equity * max_position_pct, per_slot)
    # fixed_amount or unknown
    return value


def build_intents(
    candidates: Sequence[Mapping[str, Any]],
    *,
    settings: Mapping[str, Any],
    equity: float,
    available_cash: float,
    current_positions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    price_map: Optional[Mapping[str, float]] = None,
    pre_close_map: Optional[Mapping[str, float]] = None,
    trade_date: str = "",
    user_id: str = "",
    account_id: str = "",
    securities_account_id: str = "",
    sector_strength: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sizing = settings.get("sizing") or {}
    filters = settings.get("filters") or {}
    entry = settings.get("entry") or {}
    positions = current_positions or {}
    price_map = price_map or {}
    pre_close_map = pre_close_map or {}

    mode = str(sizing.get("mode") or "fixed_amount").lower()
    value = float(sizing.get("value") or 0.0)
    max_positions = int(sizing.get("max_positions") or 0)
    max_candidates = int(filters.get("max_candidates") or 0)
    if max_candidates > 0:
        max_positions = min(max_positions, max_candidates) if max_positions > 0 else max_candidates
    max_position_pct = float(sizing.get("max_position_pct") or 0.0)
    min_cash_reserve_pct = float(sizing.get("min_cash_reserve_pct") or 0.0)
    lot_size = max(1, int(sizing.get("lot_size") or 100))
    max_order_amount = float(sizing.get("max_order_amount") or 0.0)
    min_score = float(filters.get("min_score") or 0.0)
    exclude_bj = bool(filters.get("exclude_bj", True))
    exclude_st = bool(filters.get("exclude_st", True))
    skip_existing = bool(filters.get("skip_existing_positions", True))
    skip_if_limit_up = bool(filters.get("skip_if_limit_up", True))
    order_type = str(entry.get("order_type") or "limit").lower()
    max_slippage_bps = max(0, int(entry.get("max_slippage_bps") or 0))
    gates = settings.get("gates") or {}
    sector_gate = gates.get("sector") or {}
    sector_gate_enabled = bool(sector_gate.get("enabled", True))
    sector_min_ratio = float(sector_gate.get("min_above_ma5_ratio", 0.60))
    sector_min_median = float(sector_gate.get("min_median_pct_chg", 0.0))
    sector_on_missing = str(sector_gate.get("on_missing", "skip")).lower()
    sector_map = sector_strength or {}

    investable_cash = max(0.0, available_cash - equity * min_cash_reserve_pct)
    remaining_cash = investable_cash
    intents: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    ordered = sorted(
        candidates,
        key=lambda row: (-(_num(row.get("score"), 0.0) or 0.0), str(row.get("symbol") or "")),
    )
    for candidate in ordered:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        name = str(candidate.get("name") or "")
        if not symbol:
            continue
        sector = sector_map.get(symbol) or {}
        if sector_gate_enabled:
            l1_ratio = _num(sector.get("sector_l1_above_ma5_ratio"))
            l3_median = _num(sector.get("sector_l3_median_pct_chg"))
            if l1_ratio is None or l3_median is None:
                if sector_on_missing != "pass":
                    dropped.append({"symbol": symbol, "reason": "sector_data_missing"})
                    continue
            else:
                if l1_ratio < sector_min_ratio:
                    dropped.append(
                        {
                            "symbol": symbol,
                            "reason": "sector_l1_breadth_weak",
                            "sector_l1_above_ma5_ratio": l1_ratio,
                        }
                    )
                    continue
                if l3_median <= sector_min_median:
                    dropped.append(
                        {
                            "symbol": symbol,
                            "reason": "sector_l3_median_weak",
                            "sector_l3_median_pct_chg": l3_median,
                        }
                    )
                    continue
        score = _num(candidate.get("score"), 0.0) or 0.0
        if exclude_bj and symbol.endswith(".BJ"):
            dropped.append({"symbol": symbol, "reason": "excluded_bj"})
            continue
        if exclude_st and "ST" in name.upper():
            dropped.append({"symbol": symbol, "reason": "excluded_st"})
            continue
        if score < min_score:
            dropped.append({"symbol": symbol, "reason": "below_min_score"})
            continue
        if len(intents) >= max_positions:
            dropped.append({"symbol": symbol, "reason": "max_positions"})
            continue
        price = _reference_price(candidate, price_map)
        if price is None:
            dropped.append({"symbol": symbol, "reason": "missing_price"})
            continue
        current_shares = _current_shares(positions, symbol)
        if skip_existing and current_shares > 0:
            dropped.append({"symbol": symbol, "reason": "existing_position", "current_shares": current_shares})
            continue
        pre_close = _pre_close(candidate, positions, pre_close_map)
        if skip_if_limit_up and pre_close:
            limit = _limit_price(pre_close, _board_limit_pct(symbol, name))
            if price >= limit - 0.005:
                dropped.append({"symbol": symbol, "reason": "limit_up_at_auction", "limit_price": limit})
                continue

        target = _target_amount(
            mode=mode,
            value=value,
            price=price,
            equity=equity,
            available_cash=available_cash,
            max_positions=max_positions,
            max_position_pct=max_position_pct,
        )
        caps: list[str] = []
        if max_order_amount > 0 and target > max_order_amount:
            target = max_order_amount
            caps.append("max_order_amount")
        if max_position_pct > 0:
            max_position_value = equity * max_position_pct
            additional = max(0.0, max_position_value - current_shares * price)
            if target > additional:
                target = additional
                caps.append("max_position_pct")
        if target > remaining_cash:
            target = remaining_cash
            caps.append("cash_reserve")

        shares = int(
            (Decimal(str(target)) / Decimal(str(price)) / Decimal(str(lot_size)))
            .quantize(Decimal("1"), rounding=ROUND_FLOOR)
        ) * lot_size
        if shares <= 0:
            dropped.append({"symbol": symbol, "reason": "below_one_lot", "target_amount": round(target, 2)})
            continue
        order_amount = shares * price
        remaining_cash -= order_amount
        limit_price = (
            _buy_price_ceiling(price, max_slippage_bps)
            if order_type == "limit"
            else None
        )
        intents.append(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "name": name,
                "action": "buy",
                "mode": "shadow",
                "score": round(score, 4),
                "gap_pct": _num(candidate.get("gap_pct")),
                "auction_volume_ratio": _num(candidate.get("auction_volume_ratio")),
                "seal_ratio": _num(candidate.get("seal_ratio")),
                "reference_price": round(price, 4),
                "reference_price_source": "auction_0925" if price_map.get(symbol) is None else "latest",
                "target_amount": round(target, 2),
                "target_shares": shares,
                "order_amount": round(order_amount, 2),
                "order_type": order_type,
                "limit_price": limit_price,
                "max_slippage_bps": max_slippage_bps,
                "current_shares": current_shares,
                "max_position_value": round(equity * max_position_pct, 2) if max_position_pct > 0 else None,
                "sizing_mode": mode,
                "sizing_value": value,
                "capped_by": caps,
                "sector_l1_name": sector.get("sector_l1_name"),
                "sector_l1_above_ma5_ratio": _num(sector.get("sector_l1_above_ma5_ratio")),
                "sector_l3_name": sector.get("sector_l3_name"),
                "sector_l3_median_pct_chg": _num(sector.get("sector_l3_median_pct_chg")),
                "sector_snapshot_time": sector.get("sector_snapshot_time"),
                "user_id": user_id,
                "account_id": account_id,
                "securities_account_id": securities_account_id,
                "model_version": MODEL_VERSION,
                "status": "shadow",
                "created_at": datetime.now(),
            }
        )
    return intents, dropped


def load_candidates(data_db: Any, collection: str, trade_date: str) -> list[dict[str, Any]]:
    return list(data_db[collection].find({"trade_date": trade_date}))


def load_account(finance_collections: Mapping[str, Any], account_key: str) -> Optional[dict[str, Any]]:
    return finance_collections["trader_accounts"].find_one(
        {
            "$or": [
                {"securities_account_id": account_key},
                {"account_id": account_key},
            ]
        },
        sort=[("synced_at", -1)],
    )


def load_positions(finance_collections: Mapping[str, Any], account: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    query: Dict[str, Any] = {}
    if account.get("user_id"):
        query["user_id"] = account["user_id"]
    if account.get("securities_account_id"):
        query["securities_account_id"] = account["securities_account_id"]
    return _position_map(finance_collections["trader_positions"].find(query))


def run_shadow(
    *,
    finance_collections: Mapping[str, Any],
    trading_collections: Mapping[str, Any],
    data_db: Any,
    realtime_db: Any,
    trade_date: str,
    account_key: str,
    candidates_collection: str = DEFAULT_CANDIDATES_COLLECTION,
    intents_collection: str = INTENTS_COLLECTION,
    runs_collection: str = RUNS_COLLECTION,
    settings_collection: str = SETTINGS_COLLECTION,
    equity_override: Optional[float] = None,
    cash_override: Optional[float] = None,
    use_latest_prices: bool = False,
    sector_snapshot_time: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    account = load_account(finance_collections, account_key)
    if not account:
        raise ValueError(f"trader account not found: {account_key}")
    settings = load_settings(trading_collections[settings_collection], account_key)
    candidates = load_candidates(data_db, candidates_collection, trade_date)
    positions = load_positions(finance_collections, account)
    sector_gate = (settings.get("gates") or {}).get("sector") or {}
    sector_strength, sector_snapshot_time = load_sector_strength(
        data_db,
        realtime_db,
        [str(row.get("symbol")) for row in candidates if row.get("symbol")],
        snapshot_time=sector_snapshot_time,
        ma_period=int(sector_gate.get("ma_period", 5)),
    )

    equity = (
        equity_override
        if equity_override is not None
        else (_num(account.get("total_asset")) or (_num(account.get("market_value"), 0.0) or 0.0) + (_num(account.get("cash"), 0.0) or 0.0))
    )
    available_cash = (
        cash_override
        if cash_override is not None
        else (_num(account.get("available_cash")) or _num(account.get("cash")) or 0.0)
    )

    price_map: Dict[str, float] = {}
    pre_close_map: Dict[str, float] = {}
    if use_latest_prices:
        symbols = [str(row.get("symbol")) for row in candidates if row.get("symbol")]
        if symbols:
            for row in data_db["volume_price"].find({"trade_date": trade_date, "symbol": {"$in": symbols}}):
                symbol = str(row.get("symbol"))
                close = _num(row.get("close"))
                pre_close = _num(row.get("pre_close"))
                if close and close > 0:
                    price_map[symbol] = close
                if pre_close and pre_close > 0:
                    pre_close_map[symbol] = pre_close

    intents, dropped = build_intents(
        candidates,
        settings=settings,
        equity=equity or 0.0,
        available_cash=available_cash or 0.0,
        current_positions=positions,
        price_map=price_map,
        pre_close_map=pre_close_map,
        trade_date=trade_date,
        user_id=str(account.get("user_id") or ""),
        account_id=str(account.get("account_id") or ""),
        securities_account_id=str(account.get("securities_account_id") or ""),
        sector_strength=sector_strength,
    )
    for row in intents:
        row["candidates_collection"] = candidates_collection
    summary = {
        "trade_date": trade_date,
        "account_key": account_key,
        "candidates_collection": candidates_collection,
        "account_id": account.get("account_id"),
        "securities_account_id": account.get("securities_account_id"),
        "equity": round(equity or 0.0, 2),
        "available_cash": round(available_cash or 0.0, 2),
        "sector_snapshot_time": sector_snapshot_time,
        "candidates_seen": len(candidates),
        "intents": len(intents),
        "dropped": len(dropped),
        "target_amount": round(sum(row["order_amount"] for row in intents), 2),
        "settings": settings,
        "intent_rows": intents,
        "dropped_rows": dropped,
        "dry_run": dry_run,
        "model_version": MODEL_VERSION,
    }
    if not dry_run:
        intents_col = trading_collections[intents_collection]
        intents_col.create_index(
            [("trade_date", 1), ("account_id", 1), ("symbol", 1)], unique=True
        )
        for row in intents:
            intents_col.update_one(
                {
                    "trade_date": row["trade_date"],
                    "account_id": row["account_id"],
                    "symbol": row["symbol"],
                },
                {"$set": row},
                upsert=True,
            )
        runs = trading_collections[runs_collection]
        runs.create_index(
            [("trade_date", 1), ("account_id", 1), ("mode", 1)], unique=True
        )
        run_doc = {
            "trade_date": trade_date,
            "account_id": account.get("account_id"),
            "securities_account_id": account.get("securities_account_id"),
            "mode": "shadow",
            "candidates_collection": candidates_collection,
            "sector_snapshot_time": sector_snapshot_time,
            "equity": summary["equity"],
            "available_cash": summary["available_cash"],
            "candidates_seen": len(candidates),
            "intents": len(intents),
            "dropped": len(dropped),
            "target_amount": summary["target_amount"],
            "settings": settings,
            "dropped_rows": dropped,
            "model_version": MODEL_VERSION,
            "finished_at": datetime.now(),
        }
        runs.update_one(
            {
                "trade_date": trade_date,
                "account_id": account.get("account_id"),
                "mode": "shadow",
            },
            {"$set": run_doc, "$setOnInsert": {"started_at": datetime.now()}},
            upsert=True,
        )
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="First-board shadow execution planner")
    parser.add_argument("--trade-date", default="", help="YYYYMMDD (default: today CST)")
    parser.add_argument("--account-id", required=True, help="trader account_id or securities_account_id")
    parser.add_argument("--candidates-collection", default=DEFAULT_CANDIDATES_COLLECTION)
    parser.add_argument("--intents-collection", default=INTENTS_COLLECTION)
    parser.add_argument("--runs-collection", default=RUNS_COLLECTION)
    parser.add_argument("--settings-collection", default=SETTINGS_COLLECTION)
    parser.add_argument("--equity", type=float, default=None, help="replay override")
    parser.add_argument("--cash", type=float, default=None, help="replay override")
    parser.add_argument("--use-latest-prices", action="store_true")
    parser.add_argument("--sector-snapshot-time", default=None,
                        help="PIT replay: use this sector snapshot (YYYYMMDDHHMM) instead of latest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [first_board_exec] %(levelname)s %(message)s",
    )
    trade_date = args.trade_date or datetime.now(CN_TZ).strftime("%Y%m%d")
    from db import (
        data_db,
        get_db_collections,
        get_trading_db_collections,
        realtime_db,
        trading_db,
    )

    trading_collections = get_trading_db_collections()
    for collection_name in (
        args.settings_collection,
        args.intents_collection,
        args.runs_collection,
    ):
        trading_collections.setdefault(collection_name, trading_db[collection_name])

    summary = run_shadow(
        finance_collections=get_db_collections(),
        trading_collections=trading_collections,
        data_db=data_db,
        realtime_db=realtime_db,
        trade_date=trade_date,
        account_key=args.account_id,
        candidates_collection=args.candidates_collection,
        intents_collection=args.intents_collection,
        runs_collection=args.runs_collection,
        settings_collection=args.settings_collection,
        equity_override=args.equity,
        cash_override=args.cash,
        use_latest_prices=args.use_latest_prices,
        sector_snapshot_time=args.sector_snapshot_time,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
