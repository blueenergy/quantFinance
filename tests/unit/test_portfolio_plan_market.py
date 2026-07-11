import pytest

from services.portfolio_plan_market import (
    PRICE_SOURCE_DAILY_CLOSE,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_REALTIME,
    _quote_from_docs,
    attach_plan_item_overlay,
    market_price_quotes,
    reprice_plan_items_for_publish,
)


class _Collection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, sort=None, projection=None):
        rows = self.docs
        if query.get("symbol", {}).get("$in"):
            allowed = set(query["symbol"]["$in"])
            rows = [row for row in rows if row.get("symbol") in allowed]
        if sort:
            key = sort[0][0]
            reverse = sort[0][1] == -1
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=reverse)
        return dict(rows[0]) if rows else None


class _AggCollection(_Collection):
    """Collection stub that also supports the batched aggregation pipeline."""

    def __init__(self, docs):
        super().__init__(docs)
        self.find_one_called = 0

    def find_one(self, query, sort=None, projection=None):
        self.find_one_called += 1
        return super().find_one(query, sort=sort, projection=projection)

    def aggregate(self, pipeline):
        allowed = set(pipeline[0]["$match"]["symbol"]["$in"])
        grouped = {}
        for row in self.docs:
            symbol = row.get("symbol")
            if symbol in allowed:
                grouped.setdefault(str(symbol), []).append(dict(row))
        out = []
        for symbol, rows in grouped.items():
            # $sort symbol asc, trade_date desc + $group $first => newest per symbol
            newest = max(rows, key=lambda r: str(r.get("trade_date") or ""))
            out.append({"_id": symbol, "doc": newest})
        return out


def _symbol_query_values(symbol):
    return [symbol]


def test_quote_prefers_realtime_when_fresher():
    quote = _quote_from_docs(
        symbol="600000",
        daily_doc={"trade_date": "20260625", "close": 9.5, "pre_close": 9.0},
        realtime_doc={
            "trade_date": "20260627",
            "close": 10.2,
            "pre_close": 10.0,
            "snapshot_time": "2026-06-27T14:58:00",
        },
    )
    assert quote["price_source"] == PRICE_SOURCE_REALTIME
    assert quote["price"] == 10.2
    assert quote["previous_close"] is None
    assert quote["daily_change_pct"] == pytest.approx(5.5555555556)
    assert quote["is_stale"] is False


def test_quote_exposes_previous_close_only_when_daily_bar_matches_realtime_date():
    quote = _quote_from_docs(
        symbol="600000",
        daily_doc={"trade_date": "20260627", "close": 10.1, "pre_close": 9.8},
        realtime_doc={
            "trade_date": "20260627",
            "close": 10.2,
            "snapshot_time": "2026-06-27T14:58:00",
        },
        day_pnl_date="20260627",
    )
    assert quote["price_source"] == PRICE_SOURCE_REALTIME
    assert quote["price"] == 10.2
    assert quote["previous_close"] == 9.8


def test_quote_falls_back_to_daily_close():
    quote = _quote_from_docs(
        symbol="600000",
        daily_doc={"trade_date": "20260627", "close": 9.8},
        realtime_doc={"trade_date": "20260625", "close": 9.5},
    )
    assert quote["price_source"] == PRICE_SOURCE_DAILY_CLOSE
    assert quote["price"] == 9.8
    assert quote["is_stale"] is True


def test_quote_none_when_no_prices():
    quote = _quote_from_docs(symbol="600000", daily_doc=None, realtime_doc=None)
    assert quote["price_source"] == PRICE_SOURCE_NONE
    assert quote["price"] is None


def test_market_price_quotes_collection_lookup():
    data = {
        "volume_price": _Collection([{"symbol": "600000", "trade_date": "20260626", "close": 11.0}]),
        "volume_price_realtime_latest": _Collection([{"symbol": "600000", "close": 11.5}]),
    }
    quotes = market_price_quotes(data, ["600000"], symbol_query_values=_symbol_query_values)
    assert quotes["600000"]["price_source"] == PRICE_SOURCE_REALTIME


def test_market_price_quotes_batches_via_aggregation():
    daily = _AggCollection(
        [
            {"symbol": "600000", "trade_date": "20260625", "close": 9.0},
            {"symbol": "600000", "trade_date": "20260626", "close": 11.0},
            {"symbol": "000001", "trade_date": "20260626", "close": 5.0},
        ]
    )
    realtime = _AggCollection(
        [{"symbol": "600000", "trade_date": "20260626", "close": 11.5}]
    )
    data = {"volume_price": daily, "volume_price_realtime_latest": realtime}
    quotes = market_price_quotes(
        data, ["600000", "000001"], symbol_query_values=_symbol_query_values
    )
    # newest daily bar wins for the pure-daily symbol; fresher realtime wins otherwise
    assert quotes["600000"]["price_source"] == PRICE_SOURCE_REALTIME
    assert quotes["600000"]["price"] == 11.5
    assert quotes["000001"]["price_source"] == PRICE_SOURCE_DAILY_CLOSE
    assert quotes["000001"]["price"] == 5.0
    # batched path must not fall back to per-symbol round-trips
    assert daily.find_one_called == 0
    assert realtime.find_one_called == 0


def test_reprice_preserves_plan_delta_instead_of_resizing_by_weight():
    items = [
        {
            "symbol": "600000",
            "target_weight": 0.1,
            "target_shares": 100,
            "delta_shares": 100,
            "current_shares": 0,
            "estimated_price": 10.0,
        }
    ]
    quotes = {
        "600000": {
            "price": 20.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-06-27T14:58:00",
            "is_stale": False,
        }
    }

    def normalize(current, requested, lot):
        lot = max(1, lot)
        if requested == 0:
            return 0, []
        delta = requested - current
        rounded = (abs(delta) // lot) * lot
        if rounded <= 0:
            return current, ["below_lot_size"]
        return current + (rounded if delta > 0 else -rounded), []

    repriced, summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={},
        account_positions_synced=False,
        normalize_live_target_shares=normalize,
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
        available_cash=500_000,
    )
    assert repriced[0]["target_shares"] == 100
    assert repriced[0]["delta_shares"] == 100
    assert repriced[0]["estimated_price"] == 20.0
    assert repriced[0]["estimated_amount"] == 2000.0
    assert summary["blockers"] == []


def test_reprice_keeps_unchanged_star_hold_without_min_order_blocker():
    items = [
        {
            "symbol": "688629.SH",
            "target_weight": 0.12,
            "target_shares": 600,
            "delta_shares": 0,
            "current_shares": 600,
            "estimated_price": 200.0,
        }
    ]
    quotes = {
        "688629.SH": {
            "price": 220.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-07-10T15:00:00",
            "is_stale": False,
        }
    }

    repriced, summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={},
        account_positions_synced=False,
        normalize_live_target_shares=lambda current, requested, lot: (requested, []),
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
        available_cash=500_000,
    )

    assert repriced[0]["current_shares"] == 600
    assert repriced[0]["target_shares"] == 600
    assert repriced[0]["delta_shares"] == 0
    assert repriced[0]["action"] == "hold"
    assert repriced[0]["blockers"] == []
    assert summary["blockers"] == []


def test_reprice_uses_plan_book_not_shared_account_positions():
    # Shared account already holds 2000 of the symbol for OTHER plans, but this
    # plan's book holds 0. Sizing a buy to target must size off the plan book
    # (current=0 -> buy), never off the account aggregate (which would compute a
    # negative delta and SELL shares this plan does not own).
    items = [
        {
            "symbol": "600000",
            "target_weight": 0,
            "target_shares": 500,
            "delta_shares": 500,
            "current_shares": 0,
            "estimated_price": 10.0,
        }
    ]
    quotes = {
        "600000": {
            "price": 10.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-06-27T14:58:00",
            "is_stale": False,
        }
    }

    def normalize(current, requested, lot):
        lot = max(1, lot)
        if requested == 0:
            return 0, []
        delta = requested - current
        rounded = (abs(delta) // lot) * lot
        if rounded <= 0:
            return current, ["below_lot_size"]
        return current + (rounded if delta > 0 else -rounded), []

    repriced, summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={"600000": {"current": 2000, "available": 2000}},
        account_positions_synced=True,
        normalize_live_target_shares=normalize,
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
        available_cash=1_000_000,
    )
    assert repriced[0]["current_shares"] == 0
    assert repriced[0]["target_shares"] == 500
    assert repriced[0]["delta_shares"] == 500
    assert summary["blockers"] == []


def test_reprice_plan_cash_cap_applies_even_without_account_sync():
    # The plan cash budget is always passed at publish (fallback to initial
    # capital), so the buy cash cap must bind even when the broker account has
    # not synced any positions -- it soft-caps to the plan budget, not silently
    # letting an unsynced account allow an unbounded buy.
    items = [
        {
            "symbol": "600000",
            "target_weight": 0,
            "target_shares": 1000,
            "delta_shares": 1000,
            "current_shares": 0,
            "estimated_price": 10.0,
        }
    ]
    quotes = {
        "600000": {
            "price": 10.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-06-27T14:58:00",
            "is_stale": False,
        }
    }

    def normalize(current, requested, lot):
        lot = max(1, lot)
        if requested == 0:
            return 0, []
        delta = requested - current
        rounded = (abs(delta) // lot) * lot
        if rounded <= 0:
            return current, ["below_lot_size"]
        return current + (rounded if delta > 0 else -rounded), []

    repriced, _summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={},
        account_positions_synced=False,
        normalize_live_target_shares=normalize,
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
        available_cash=3000,
    )
    assert repriced[0]["delta_shares"] == 300
    assert repriced[0]["target_shares"] == 300
    assert "insufficient_cash" in (repriced[0]["blockers"] or [])


def test_reprice_blocks_missing_price():
    items = [{"symbol": "600000", "target_weight": 0.1, "target_shares": 100, "delta_shares": 100, "current_shares": 0}]
    quotes = {"600000": {"price": None, "price_source": PRICE_SOURCE_NONE, "price_as_of": None, "is_stale": True}}
    _, summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={},
        account_positions_synced=False,
        normalize_live_target_shares=lambda c, r, l: (r, []),
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
    )
    assert "missing_price" in summary["blockers"]


def test_reprice_allows_missing_price_for_sell_to_zero():
    items = [
        {
            "symbol": "600000",
            "target_weight": 0,
            "target_shares": 0,
            "delta_shares": -100,
            "current_shares": 100,
            "estimated_price": 10.0,
        }
    ]
    quotes = {"600000": {"price": None, "price_source": PRICE_SOURCE_NONE, "price_as_of": None, "is_stale": True}}
    repriced, summary = reprice_plan_items_for_publish(
        plan={},
        items=items,
        quotes=quotes,
        equity=1_000_000,
        lot_size=100,
        account_positions_by_key={},
        account_positions_synced=False,
        normalize_live_target_shares=lambda c, r, l: (r, []),
        int_value=lambda value, default=0: int(value or default),
        numeric=lambda value: float(value or 0),
        symbol_key=lambda value: str(value),
    )
    assert summary["blockers"] == []
    assert summary["missing_price_symbols"] == []
    assert repriced[0]["action"] == "sell"
    assert repriced[0]["estimated_price"] is None
    assert repriced[0]["warnings"] == ["missing_price_market_sell"]


def test_overlay_attaches_price_and_rank_drift():
    items = [{"symbol": "600000", "rank": 3, "target_shares": 100, "estimated_price": 10.0}]
    quotes = {
        "600000": {
            "price": 11.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-06-27T14:58:00",
            "is_stale": False,
        }
    }
    latest_ranked = [{"symbol": "600000", "rank": 5, "score_value": 80.0}]
    plan_ranked = [{"symbol": "600000", "rank": 3, "score_value": 82.0}]
    enriched, overlay = attach_plan_item_overlay(
        plan={},
        items=items,
        quotes=quotes,
        plan_score_date="20260625",
        latest_score_date="20260627",
        plan_ranked=plan_ranked,
        latest_ranked=latest_ranked,
        top_n=10,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value),
        score_context={
            "score_type": "growth_cycle_weighted",
            "growth_weight": 0.3,
            "cycle_weight": 0.7,
            "universe_index": "csi500",
        },
    )
    assert enriched[0]["price_drift_pct"] == 10.0
    assert enriched[0]["daily_change_pct"] is None
    assert enriched[0]["latest_rank"] == 5
    assert enriched[0]["snapshot_rank"] == 3
    assert enriched[0]["rank_delta"] == -2
    assert overlay["score_snapshot_stale"] is True
    assert overlay["score_context"]["universe_index"] == "csi500"
    assert overlay["score_context"]["growth_weight"] == 0.3


def test_overlay_does_not_mark_dropped_when_latest_rank_unavailable():
    items = [{"symbol": "600000", "rank": 3, "target_shares": 100, "estimated_price": 10.0}]
    quotes = {
        "600000": {
            "price": 11.0,
            "price_source": PRICE_SOURCE_REALTIME,
            "price_as_of": "2026-06-27T14:58:00",
            "is_stale": False,
        }
    }
    enriched, _overlay = attach_plan_item_overlay(
        plan={},
        items=items,
        quotes=quotes,
        plan_score_date="20260625",
        latest_score_date="20260627",
        latest_ranked=[],
        top_n=10,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value),
    )
    assert enriched[0]["latest_rank"] is None
    assert enriched[0]["dropped_out_of_top_n"] is False


def test_overlay_rank_delta_uses_universe_rank_not_portfolio_slot():
    items = [{"symbol": "002185.SZ", "rank": 1, "target_shares": 100, "estimated_price": 25.31}]
    quotes = {"002185.SZ": {"price": 25.5, "price_source": PRICE_SOURCE_REALTIME, "is_stale": False}}
    plan_ranked = [{"symbol": "002185.SZ", "rank": 13, "score_value": 84.25}]
    latest_ranked = [{"symbol": "002185.SZ", "rank": 13, "score_value": 84.25}]
    enriched, overlay = attach_plan_item_overlay(
        plan={},
        items=items,
        quotes=quotes,
        plan_score_date="20260710",
        latest_score_date="20260710",
        plan_ranked=plan_ranked,
        latest_ranked=latest_ranked,
        top_n=20,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value).split(".")[0] if "." in str(value) else str(value),
    )
    assert enriched[0]["rank"] == 1
    assert enriched[0]["snapshot_rank"] == 13
    assert enriched[0]["latest_rank"] == 13
    assert enriched[0]["rank_delta"] == 0
    assert overlay["score_snapshot_stale"] is False


def test_overlay_marks_liquidation_rows_dropped_from_target_pool():
    items = [{"symbol": "601958.SH", "rank": None, "target_shares": 0, "current_shares": 100, "action": "sell", "estimated_price": 12.0}]
    enriched, _overlay = attach_plan_item_overlay(
        plan={"summary": {"equity": 100000}, "params_snapshot": {"cash_buffer": 0.03, "lot_size": 100, "min_buy_lots": 1}},
        items=items,
        quotes={},
        plan_score_date="20260710",
        latest_score_date="20260710",
        plan_ranked=[{"symbol": "601958.SH", "rank": 73, "score_value": 74.0}],
        latest_ranked=[{"symbol": "601958.SH", "rank": 73, "score_value": 74.0}],
        top_n=20,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value).split(".")[0],
    )
    assert enriched[0]["selection_exclusion_reason"] == "dropped_from_target_pool"


def test_overlay_marks_missing_price_liquidation():
    items = [{"symbol": "600000", "rank": None, "target_shares": 0, "blockers": ["missing_price"], "estimated_price": None}]
    enriched, _overlay = attach_plan_item_overlay(
        plan={"summary": {"equity": 100000}, "params_snapshot": {"cash_buffer": 0.03}},
        items=items,
        quotes={},
        plan_score_date="20260710",
        latest_score_date="20260710",
        plan_ranked=[],
        latest_ranked=[],
        top_n=10,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value),
    )
    assert enriched[0]["selection_exclusion_reason"] == "missing_price"


def test_overlay_marks_over_slot_budget_liquidation():
    items = [{"symbol": "600000", "rank": None, "target_shares": 0, "estimated_price": 500.0}]
    enriched, overlay = attach_plan_item_overlay(
        plan={"summary": {"equity": 100000}, "params_snapshot": {"cash_buffer": 0.03, "lot_size": 100, "min_buy_lots": 1}},
        items=items,
        quotes={},
        plan_score_date="20260710",
        latest_score_date="20260710",
        plan_ranked=[],
        latest_ranked=[],
        top_n=10,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value),
    )
    assert overlay["target_slot_amount"] == 9700.0
    assert enriched[0]["selection_exclusion_reason"] == "over_slot_budget"
