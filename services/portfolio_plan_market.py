"""Market quotes, live overlay, and publish-time repricing for portfolio plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from stock_data_access.board_lots import is_star_market, normalize_board_target_shares

PRICE_SOURCE_REALTIME = "realtime"
PRICE_SOURCE_DAILY_CLOSE = "daily_close"
PRICE_SOURCE_NONE = "none"


def _first_positive_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
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


def _price_as_of_from_doc(doc: Optional[Dict[str, Any]], *, source: str) -> Optional[str]:
    if not doc:
        return None
    if source == PRICE_SOURCE_REALTIME:
        for key in ("snapshot_time", "realtime_trade_time", "trade_date", "date", "updated_at", "created_at"):
            value = doc.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value)
    if source == PRICE_SOURCE_DAILY_CLOSE:
        date_text = _market_doc_yyyymmdd(doc)
        return date_text or None
    return None


def _quote_from_docs(
    *,
    symbol: str,
    daily_doc: Optional[Dict[str, Any]],
    realtime_doc: Optional[Dict[str, Any]],
    day_pnl_date: Optional[str] = None,
) -> Dict[str, Any]:
    daily_price = _first_positive_float((daily_doc or {}).get("close"))
    daily_pre_close = _first_positive_float(
        (daily_doc or {}).get("pre_close"),
        (daily_doc or {}).get("prev_close"),
        (daily_doc or {}).get("previous_close"),
    )
    realtime_price = _first_positive_float(
        (realtime_doc or {}).get("close"),
        (realtime_doc or {}).get("price"),
        (realtime_doc or {}).get("latest_price"),
    )
    realtime_pre_close = _first_positive_float(
        (realtime_doc or {}).get("pre_close"),
        (realtime_doc or {}).get("prev_close"),
        (realtime_doc or {}).get("previous_close"),
    )
    daily_date = _market_doc_yyyymmdd(daily_doc)
    realtime_date = _market_doc_yyyymmdd(realtime_doc)
    daily_change = daily_price - daily_pre_close if daily_price is not None and daily_pre_close else None
    daily_change_pct = (
        daily_change / daily_pre_close * 100.0
        if daily_change is not None and daily_pre_close
        else _first_float((daily_doc or {}).get("pct_chg"), (daily_doc or {}).get("pct_change"))
    )
    use_realtime = bool(
        realtime_doc
        and realtime_price
        and (not daily_date or not realtime_date or realtime_date >= daily_date)
    )
    # Only expose a day-P&L baseline for the requested trading date. If the
    # newest daily bar is older than "today", yesterday's intraday P&L must not
    # be shown as today's P&L.
    daily_matches_day_pnl_date = bool(day_pnl_date and daily_date == day_pnl_date)
    realtime_previous_close = (
        (realtime_pre_close or daily_pre_close)
        if daily_matches_day_pnl_date and realtime_date == daily_date
        else None
    )
    if use_realtime:
        return {
            "symbol": symbol,
            "price": realtime_price,
            "previous_close": realtime_previous_close,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": _price_as_of_from_doc(realtime_doc, source=PRICE_SOURCE_REALTIME),
            "is_stale": False,
            "daily_change": daily_change,
            "daily_change_pct": daily_change_pct,
            "realtime_doc": realtime_doc,
            "daily_doc": daily_doc,
        }
    if daily_price:
        return {
            "symbol": symbol,
            "price": daily_price,
            "previous_close": daily_pre_close if daily_matches_day_pnl_date else None,
            "price_source": PRICE_SOURCE_DAILY_CLOSE,
            "price_as_of": _price_as_of_from_doc(daily_doc, source=PRICE_SOURCE_DAILY_CLOSE),
            "is_stale": True,
            "daily_change": daily_change,
            "daily_change_pct": daily_change_pct,
            "realtime_doc": realtime_doc,
            "daily_doc": daily_doc,
        }
    return {
        "symbol": symbol,
        "price": None,
        "previous_close": None,
        "price_source": PRICE_SOURCE_NONE,
        "price_as_of": None,
        "is_stale": True,
        "daily_change": None,
        "daily_change_pct": None,
        "realtime_doc": realtime_doc,
        "daily_doc": daily_doc,
    }


_DAILY_QUOTE_FIELDS = (
    "symbol",
    "trade_date",
    "date",
    "close",
    "pre_close",
    "prev_close",
    "previous_close",
    "change",
    "pct_chg",
    "pct_change",
)
_REALTIME_QUOTE_FIELDS = (
    "symbol",
    "trade_date",
    "date",
    "close",
    "price",
    "latest_price",
    "pre_close",
    "prev_close",
    "previous_close",
    "realtime_trade_time",
    "snapshot_time",
    "updated_at",
    "created_at",
)


def _latest_variant_docs(
    coll: Any,
    candidates: List[str],
    fields: Tuple[str, ...],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Latest doc per stored ``symbol`` for a candidate set, via one aggregation.

    Returns ``None`` when the collection cannot aggregate (e.g. a test stub), so
    the caller can fall back to per-symbol ``find_one`` lookups. The ``$sort`` on
    ``(symbol, trade_date desc)`` is served by the ``(symbol, trade_date)`` index,
    and ``$first`` keeps the newest bar per stored symbol.
    """
    if not candidates:
        return {}
    aggregate = getattr(coll, "aggregate", None)
    if aggregate is None:
        return None
    pipeline = [
        {"$match": {"symbol": {"$in": candidates}}},
        {"$sort": {"symbol": 1, "trade_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
    ]
    try:
        result: Dict[str, Dict[str, Any]] = {}
        for row in aggregate(pipeline):
            doc = row.get("doc") or {}
            sym = doc.get("symbol")
            if not sym:
                continue
            result[str(sym)] = {key: doc.get(key) for key in fields if key in doc}
        return result
    except Exception:
        return None


def _pick_latest_variant(
    docs_by_symbol: Dict[str, Dict[str, Any]],
    candidates: List[str],
) -> Optional[Dict[str, Any]]:
    """Pick the newest doc among a symbol's suffix variants (mirrors find_one sort)."""
    best: Optional[Dict[str, Any]] = None
    best_key = ""
    for candidate in candidates:
        doc = docs_by_symbol.get(candidate)
        if not doc:
            continue
        key = _market_doc_yyyymmdd(doc)
        if best is None or key > best_key:
            best, best_key = doc, key
    return best


def _market_price_quotes_per_symbol(
    daily_coll: Any,
    realtime_coll: Any,
    symbols: List[str],
    candidates_by_symbol: Dict[str, List[str]],
    *,
    day_pnl_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fallback path: one daily + one realtime ``find_one`` per symbol."""
    quotes: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        candidates = candidates_by_symbol[symbol]
        daily_doc = daily_coll.find_one(
            {"symbol": {"$in": candidates}},
            sort=[("trade_date", -1)],
            projection={key: 1 for key in _DAILY_QUOTE_FIELDS} | {"_id": 0},
        )
        realtime_doc = realtime_coll.find_one(
            {"symbol": {"$in": candidates}},
            sort=[("trade_date", -1)],
            projection={key: 1 for key in _REALTIME_QUOTE_FIELDS} | {"_id": 0},
        )
        quotes[symbol] = _quote_from_docs(
            symbol=symbol,
            daily_doc=daily_doc,
            realtime_doc=realtime_doc,
            day_pnl_date=day_pnl_date,
        )
    return quotes


def market_price_quotes(
    data_collections: Dict[str, Any],
    symbols: List[str],
    *,
    symbol_query_values: Callable[[str], List[str]],
    day_pnl_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return per-symbol price quotes with source metadata.

    Previously this issued two ``find_one`` calls per symbol (daily + realtime),
    i.e. ``2N`` sequential round-trips. It now fetches the latest daily and
    realtime docs for every requested symbol in two batched aggregations and
    resolves per-symbol in memory, falling back to the per-symbol path when the
    collection cannot aggregate.
    """
    if not symbols:
        return {}
    daily_coll = data_collections["volume_price"]
    realtime_coll = data_collections["volume_price_realtime_latest"]
    candidates_by_symbol = {symbol: list(symbol_query_values(symbol)) for symbol in symbols}
    all_candidates = sorted({c for cands in candidates_by_symbol.values() for c in cands})

    daily_by_symbol = _latest_variant_docs(daily_coll, all_candidates, _DAILY_QUOTE_FIELDS)
    realtime_by_symbol = _latest_variant_docs(realtime_coll, all_candidates, _REALTIME_QUOTE_FIELDS)
    if daily_by_symbol is None or realtime_by_symbol is None:
        return _market_price_quotes_per_symbol(
            daily_coll,
            realtime_coll,
            symbols,
            candidates_by_symbol,
            day_pnl_date=day_pnl_date,
        )

    quotes: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        candidates = candidates_by_symbol[symbol]
        quotes[symbol] = _quote_from_docs(
            symbol=symbol,
            daily_doc=_pick_latest_variant(daily_by_symbol, candidates),
            realtime_doc=_pick_latest_variant(realtime_by_symbol, candidates),
            day_pnl_date=day_pnl_date,
        )
    return quotes


def quotes_to_prices_and_realtime_docs(
    quotes: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]]]:
    prices: Dict[str, float] = {}
    realtime_docs: Dict[str, Dict[str, Any]] = {}
    for symbol, quote in quotes.items():
        price = quote.get("price")
        if price is not None and float(price) > 0:
            prices[symbol] = float(price)
        if quote.get("price_source") == PRICE_SOURCE_REALTIME and quote.get("realtime_doc"):
            realtime_docs[symbol] = quote["realtime_doc"]
    return prices, realtime_docs


def _item_action(current_shares: int, target_shares: int, delta_shares: int) -> str:
    if delta_shares > 0:
        return "buy"
    if delta_shares < 0:
        return "sell"
    if target_shares > 0:
        return "hold"
    return "hold"


def reprice_plan_items_for_publish(
    *,
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    equity: float,
    lot_size: int,
    account_positions_by_key: Dict[str, Dict[str, int]],
    account_positions_synced: bool,
    normalize_live_target_shares: Callable[[int, int, int], Tuple[int, List[str]]],
    int_value: Callable[[Any, int], int],
    numeric: Callable[[Any], float],
    symbol_key: Callable[[Any], str],
    available_cash: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Recompute plan item prices/shares at publish time without changing lineup/weights."""
    lot = max(1, int(lot_size or 100))
    equity_value = max(0.0, float(equity or 0.0))
    original_items = [dict(item) for item in items]
    working_items = [dict(item) for item in items]
    diff_rows: List[Dict[str, Any]] = []
    missing_price_symbols: List[str] = []
    stale_price_symbols: List[str] = []
    blockers: List[str] = []

    for item in working_items:
        symbol = str(item.get("symbol") or "")
        quote = quotes.get(symbol) or {}
        price_source = str(quote.get("price_source") or PRICE_SOURCE_NONE)
        live_price = quote.get("price")
        planned_delta = int_value(item.get("delta_shares"))
        account_pos = account_positions_by_key.get(symbol_key(symbol)) or {"current": 0, "available": 0}
        # current_shares is the plan/lineage book holding (carried on the item by
        # the caller), never the broker account aggregate. The securities account
        # can be shared by several plans / manual positions, so using the account
        # total would distort this plan's delta -- and could make it sell shares it
        # does not own. account positions are consulted only to cap sells to the
        # broker-available quantity (see ``account_positions_synced`` blocks below).
        current_shares = int_value(item.get("current_shares"))
        planned_target = int_value(item.get("target_shares"))
        if live_price is None or float(live_price) <= 0:
            # A pure sell-to-zero can still be published as a market order when
            # the latest price is unavailable. Other planned trades need a price
            # for amount/cash checks and must be blocked before publish.
            can_publish_market_sell = planned_delta < 0 and planned_target == 0
            needs_price = not can_publish_market_sell and (
                planned_delta != 0 or planned_target != current_shares
            )
            if needs_price:
                missing_price_symbols.append(symbol)
                item.update(
                    {
                        "current_shares": current_shares,
                        "target_shares": current_shares,
                        "delta_shares": 0,
                        "action": "hold",
                        "estimated_price": None,
                        "price_source": PRICE_SOURCE_NONE,
                        "price_as_of": None,
                        "estimated_amount": None,
                        "blockers": sorted(set([*(item.get("blockers") or []), "missing_price"])),
                        "warnings": item.get("warnings") or [],
                    }
                )
                continue
            target_shares = planned_target
            delta = target_shares - current_shares
            item_blockers: List[str] = []
            if delta < 0 and account_positions_synced:
                available = int_value(account_pos.get("available"))
                if available <= 0:
                    delta = 0
                    target_shares = current_shares
                    item_blockers.append("account_position_capped")
                else:
                    sell_qty = abs(delta)
                    capped = min(sell_qty, available)
                    if capped <= 0:
                        delta = 0
                        target_shares = current_shares
                        item_blockers.append("account_position_capped")
                    elif capped < sell_qty:
                        delta = -capped
                        target_shares = current_shares + delta
                        item_blockers.append("account_position_capped")
            item.update(
                {
                    "current_shares": current_shares,
                    "target_shares": target_shares,
                    "delta_shares": delta,
                    "action": _item_action(current_shares, target_shares, delta),
                    "estimated_price": None,
                    "price_source": PRICE_SOURCE_NONE,
                    "price_as_of": None,
                    "estimated_amount": None,
                    "blockers": sorted(set(item_blockers)),
                    "warnings": ["missing_price_market_sell"] if delta < 0 else [],
                }
            )
            continue
        if price_source == PRICE_SOURCE_DAILY_CLOSE:
            stale_price_symbols.append(symbol)
        requested_target = planned_target
        if is_star_market(symbol):
            # STAR (科创板) single orders must be >= 200 shares; the injected
            # normalize callback only round-lots, so enforce the board rule here.
            target_shares, lot_blockers = normalize_board_target_shares(
                current_shares,
                requested_target,
                float(live_price) if live_price else None,
                lot,
                symbol,
                available_cash=None,
            )
        else:
            target_shares, lot_blockers = normalize_live_target_shares(current_shares, requested_target, lot)
        delta = target_shares - current_shares
        item_blockers = list(lot_blockers)
        item_warnings: List[str] = []
        if delta < 0 and account_positions_synced:
            available = int_value(account_pos.get("available"))
            if available <= 0:
                delta = 0
                target_shares = current_shares
                item_blockers.append("account_position_capped")
            else:
                sell_qty = abs(delta)
                capped = min(sell_qty, available)
                if capped <= 0:
                    delta = 0
                    target_shares = current_shares
                    item_blockers.append("account_position_capped")
                elif capped < sell_qty:
                    delta = -capped
                    target_shares = current_shares + delta
                    item_blockers.append("account_position_capped")
        if price_source == PRICE_SOURCE_DAILY_CLOSE:
            item_warnings.append("priced_from_stale_close")
        action = _item_action(current_shares, target_shares, delta)
        estimated_amount = abs(delta) * float(live_price) if delta else None
        item.update(
            {
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "action": action,
                "estimated_price": float(live_price),
                "price_source": price_source,
                "price_as_of": quote.get("price_as_of"),
                "estimated_amount": estimated_amount,
                "blockers": sorted(set(item_blockers)),
                "warnings": sorted(set(item_warnings)),
            }
        )

    if missing_price_symbols:
        blockers.append("missing_price")

    # Sell-first cash check for buys.
    work_cash = float(available_cash) if available_cash is not None else None
    trade_items = [item for item in working_items if int_value(item.get("delta_shares")) != 0]
    sells = [item for item in trade_items if int_value(item.get("delta_shares")) < 0]
    buys = [item for item in trade_items if int_value(item.get("delta_shares")) > 0]
    sell_proceeds = 0.0
    for item in sells:
        delta = abs(int_value(item.get("delta_shares")))
        price = float(item.get("estimated_price") or 0)
        sell_proceeds += delta * price
    if work_cash is not None:
        work_cash += sell_proceeds
        for item in buys:
            delta = int_value(item.get("delta_shares"))
            price = float(item.get("estimated_price") or 0)
            amount = delta * price
            if amount > work_cash:
                affordable = (int(work_cash // price) // lot) * lot if price > 0 else 0
                if affordable <= 0:
                    item["delta_shares"] = 0
                    item["target_shares"] = int_value(item.get("current_shares"))
                    item.setdefault("blockers", []).append("insufficient_cash")
                else:
                    item["delta_shares"] = affordable
                    item["target_shares"] = int_value(item.get("current_shares")) + affordable
                    item.setdefault("blockers", []).append("insufficient_cash")
                item["estimated_amount"] = abs(int_value(item.get("delta_shares"))) * price
                item["action"] = _item_action(
                    int_value(item.get("current_shares")),
                    int_value(item.get("target_shares")),
                    int_value(item.get("delta_shares")),
                )
                work_cash -= affordable * price

    for before, after in zip(original_items, working_items):
        diff_rows.append(
            {
                "symbol": after.get("symbol"),
                "old_estimated_price": before.get("estimated_price"),
                "new_estimated_price": after.get("estimated_price"),
                "old_target_shares": before.get("target_shares"),
                "new_target_shares": after.get("target_shares"),
                "old_delta_shares": before.get("delta_shares"),
                "new_delta_shares": after.get("delta_shares"),
                "price_source": after.get("price_source"),
                "price_as_of": after.get("price_as_of"),
                "blockers": after.get("blockers") or [],
                "warnings": after.get("warnings") or [],
            }
        )

    old_turnover = sum(abs(float(row.get("estimated_amount") or 0)) for row in original_items)
    new_turnover = sum(abs(float(row.get("estimated_amount") or 0)) for row in working_items)
    turnover_drift_pct = None
    if old_turnover > 0:
        turnover_drift_pct = (new_turnover - old_turnover) / old_turnover * 100.0

    summary = {
        "equity_used": equity_value,
        "lot_size": lot,
        "missing_price_symbols": sorted(set(missing_price_symbols)),
        "stale_price_symbols": sorted(set(stale_price_symbols)),
        "blockers": sorted(set(blockers)),
        "turnover_drift_pct": turnover_drift_pct,
        "items": diff_rows,
    }
    return working_items, summary


def _target_slot_amount(plan: Dict[str, Any], top_n: int, numeric: Callable[[Any], float]) -> Optional[float]:
    summary = plan.get("summary") or {}
    equity = numeric(summary.get("equity"))
    if equity <= 0:
        return None
    params = dict(plan.get("params_snapshot") or {})
    cash_buffer = numeric(params.get("cash_buffer", 0.03))
    return equity * (1.0 - cash_buffer) / max(1, top_n)


def _selection_exclusion_reason(
    item: Dict[str, Any],
    *,
    target_slot_amount: Optional[float],
    lot_size: int,
    min_buy_lots: int,
    numeric: Callable[[Any], float],
) -> Optional[str]:
    """Why a held symbol was not kept in the new target portfolio."""
    if item.get("rank") is not None:
        return None
    if int(item.get("target_shares") or 0) > 0:
        return None
    blockers = list(item.get("blockers") or [])
    if "missing_price" in blockers:
        return "missing_price"
    price = numeric(item.get("estimated_price"))
    min_shares = max(1, lot_size) * max(1, min_buy_lots)
    if (
        target_slot_amount
        and price > 0
        and price * min_shares > float(target_slot_amount) + 1e-9
    ):
        return "over_slot_budget"
    return "dropped_from_target_pool"


def attach_plan_item_overlay(
    *,
    plan: Dict[str, Any],
    items: List[Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    plan_score_date: Optional[str],
    latest_score_date: Optional[str],
    plan_ranked: Optional[List[Dict[str, Any]]] = None,
    latest_ranked: List[Dict[str, Any]],
    top_n: int,
    numeric: Callable[[Any], float],
    int_value: Callable[[Any, int], int],
    symbol_key: Callable[[Any], str],
    score_context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    params = dict(plan.get("params_snapshot") or {})
    lot_size = max(1, int_value(params.get("lot_size"), 100))
    min_buy_lots = max(1, int_value(params.get("min_buy_lots"), 1))
    target_slot_amount = _target_slot_amount(plan, top_n, numeric)
    plan_rank_by_key = {symbol_key(row["symbol"]): row for row in (plan_ranked or [])}
    latest_rank_by_key = {symbol_key(row["symbol"]): row for row in latest_ranked}
    enriched: List[Dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "")
        quote = quotes.get(symbol) or {}
        live_price = quote.get("price")
        plan_price = numeric(item.get("estimated_price"))
        price_drift_pct = None
        if live_price and plan_price > 0:
            price_drift_pct = (float(live_price) - plan_price) / plan_price * 100.0
        target_shares = int_value(item.get("target_shares"))
        live_market_value = float(live_price) * target_shares if live_price and target_shares else None
        plan_market_value = plan_price * target_shares if plan_price > 0 and target_shares else None
        latest_row = latest_rank_by_key.get(symbol_key(symbol))
        latest_rank = latest_row.get("rank") if latest_row else None
        latest_score = latest_row.get("score_value") if latest_row else None
        plan_row = plan_rank_by_key.get(symbol_key(symbol))
        snapshot_rank = plan_row.get("rank") if plan_row else None
        rank_delta = None
        if latest_rank is not None and snapshot_rank is not None:
            rank_delta = int(snapshot_rank) - int(latest_rank)
        dropped_out = bool(
            latest_ranked
            and target_shares > 0
            and (latest_row is None or int(latest_row.get("rank") or 0) > max(1, top_n))
        )
        selection_exclusion_reason = _selection_exclusion_reason(
            item,
            target_slot_amount=target_slot_amount,
            lot_size=lot_size,
            min_buy_lots=min_buy_lots,
            numeric=numeric,
        )
        enriched.append(
            {
                **item,
                "live_price": live_price,
                "live_price_source": quote.get("price_source"),
                "live_price_as_of": quote.get("price_as_of"),
                "live_price_is_stale": bool(quote.get("is_stale")),
                "price_drift_pct": price_drift_pct,
                "daily_change": quote.get("daily_change"),
                "daily_change_pct": quote.get("daily_change_pct"),
                "live_market_value": live_market_value,
                "plan_market_value": plan_market_value,
                "latest_score": latest_score,
                "latest_rank": latest_rank,
                "snapshot_rank": snapshot_rank,
                "rank_delta": rank_delta,
                "dropped_out_of_top_n": dropped_out,
                "selection_exclusion_reason": selection_exclusion_reason,
            }
        )

    score_stale = bool(
        plan_score_date
        and latest_score_date
        and str(latest_score_date) > str(plan_score_date)
    )
    plan_overlay = {
        "score_snapshot_date": plan_score_date,
        "latest_score_date": latest_score_date,
        "score_snapshot_stale": score_stale,
        "score_snapshot_age_days": _score_age_days(plan_score_date, latest_score_date),
        "top_n": top_n,
        "target_slot_amount": target_slot_amount,
        "score_context": dict(score_context or {}),
    }
    return enriched, plan_overlay


def _score_age_days(plan_score_date: Optional[str], latest_score_date: Optional[str]) -> Optional[int]:
    if not plan_score_date or not latest_score_date:
        return None
    try:
        plan_dt = datetime.strptime(str(plan_score_date)[:8], "%Y%m%d")
        latest_dt = datetime.strptime(str(latest_score_date)[:8], "%Y%m%d")
        return max(0, (latest_dt - plan_dt).days)
    except (TypeError, ValueError):
        return None
