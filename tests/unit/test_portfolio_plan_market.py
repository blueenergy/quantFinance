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


def _symbol_query_values(symbol):
    return [symbol]


def test_quote_prefers_realtime_when_fresher():
    quote = _quote_from_docs(
        symbol="600000",
        daily_doc={"trade_date": "20260625", "close": 9.5},
        realtime_doc={"trade_date": "20260627", "close": 10.2, "snapshot_time": "2026-06-27T14:58:00"},
    )
    assert quote["price_source"] == PRICE_SOURCE_REALTIME
    assert quote["price"] == 10.2
    assert quote["is_stale"] is False


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


def test_reprice_uses_target_weight_and_equity():
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
    assert repriced[0]["target_shares"] == 5000
    assert repriced[0]["delta_shares"] == 5000
    assert repriced[0]["estimated_price"] == 20.0
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
    enriched, overlay = attach_plan_item_overlay(
        plan={},
        items=items,
        quotes=quotes,
        plan_score_date="20260625",
        latest_score_date="20260627",
        latest_ranked=latest_ranked,
        top_n=10,
        numeric=lambda value: float(value or 0),
        int_value=lambda value, default=0: int(value or default),
        symbol_key=lambda value: str(value),
    )
    assert enriched[0]["price_drift_pct"] == 10.0
    assert enriched[0]["latest_rank"] == 5
    assert enriched[0]["rank_delta"] == -2
    assert overlay["score_snapshot_stale"] is True


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
