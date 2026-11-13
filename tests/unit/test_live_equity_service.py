from datetime import datetime

from services.live_equity import (
    SHANGHAI_TZ,
    build_account_equity_live_point,
    compute_live_plan_equity,
    snapshot_account_equity,
    snapshot_live_plan_equity,
)


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return
        if upsert:
            row = dict(query)
            row.update(update.get("$set", {}))
            self.docs.append(row)

    def create_index(self, *args, **kwargs):
        return None

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$exists" in expected and (key in doc) is not bool(expected["$exists"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_snapshot_account_equity_upserts_latest_account_point():
    collections = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "total_asset": 100000,
                    "cash": 20000,
                    "available_cash": 19000,
                    "market_value": 80000,
                    "synced_at": 1,
                }
            ]
        ),
        "trader_account_equity": _Collection(),
    }

    first = snapshot_account_equity(collections, date="20250604", stale_seconds=0)
    second = snapshot_account_equity(collections, date="20250604", stale_seconds=0)

    assert first["written"] == 1
    assert second["written"] == 1
    assert len(collections["trader_account_equity"].docs) == 1
    row = collections["trader_account_equity"].docs[0]
    assert row["date"] == "20250604"
    assert row["total_asset"] == 100000
    assert row["market_value"] == 80000
    assert row["is_stale"] is True


def test_snapshot_account_equity_falls_back_when_total_asset_is_zero():
    collections = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "total_asset": 0,
                    "cash": 56347.14,
                    "available_cash": 56347.14,
                    "market_value": 411110.40,
                    "synced_at": 1,
                }
            ]
        ),
        "trader_account_equity": _Collection(),
    }

    snapshot_account_equity(collections, date="20261106", stale_seconds=0)
    row = collections["trader_account_equity"].docs[0]
    assert row["total_asset"] == 467457.54
    assert row["market_value"] == 411110.40
    assert row["cash"] == 56347.14


def test_compute_live_plan_equity_uses_plan_executions_and_fees():
    plan = {
        "user_id": "user-1",
        "plan_id": "plan-1",
        "strategy_template_id": "s1",
        "params_snapshot": {"initial_capital": 100000},
    }
    executions = [
        {
            "order_id": "buy-1",
            "symbol": "000001",
            "action": "buy",
            "filled_size": 100,
            "filled_price": 10,
            "total_fee": 5,
            "timestamp": 1,
        },
        {
            "order_id": "sell-1",
            "symbol": "000001",
            "action": "sell",
            "filled_size": 40,
            "filled_price": 12,
            "total_fee": 3,
            "timestamp": 2,
        },
    ]

    point = compute_live_plan_equity(plan, executions, {"000001": 11}, date="20250604")

    assert point["date"] == "20250604"
    assert point["initial_capital"] == 100000
    assert point["position_count"] == 1
    assert point["market_value"] == 660
    assert round(point["realized_pnl"], 2) == 75.0
    assert round(point["unrealized_pnl"], 2) == 57.0
    assert round(point["equity"], 2) == 100132.0
    assert round(point["cash"], 2) == 99472.0


def test_snapshot_live_plan_equity_writes_when_plan_has_paper_conflict():
    trading_collections = {
        "portfolio_trade_plans": _Collection(
            [
                {
                    "user_id": "user-1",
                    "plan_id": "plan-1",
                    "strategy_template_id": "s1",
                    "status": "executed_paper",
                    "paper_executed_at": 1,
                    "params_snapshot": {"initial_capital": 100000},
                }
            ]
        ),
        "portfolio_paper_executions": _Collection([{"user_id": "user-1", "plan_id": "plan-1"}]),
        "portfolio_live_equity": _Collection(),
    }
    finance_collections = {
        "trade_signals": _Collection(
            [
                {
                    "user_id": "user-1",
                    "plan_id": "plan-1",
                    "mode": "live",
                    "source": "portfolio_plan",
                    "order_id": "live-order-1",
                }
            ]
        ),
        "trade_executions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "order_id": "live-order-1",
                    "symbol": "000001",
                    "action": "buy",
                    "filled_size": 100,
                    "filled_price": 10,
                    "total_fee": 5,
                    "timestamp": 1,
                }
            ]
        ),
    }
    data_collections = {"volume_price": _Collection([{"symbol": "000001", "close": 11, "trade_date": "20250604"}])}

    result = snapshot_live_plan_equity(trading_collections, finance_collections, data_collections, date="20250604")

    assert result["written"] == 1
    assert result["skipped"] == 0
    row = trading_collections["portfolio_live_equity"].docs[0]
    assert row["plan_id"] == "plan-1"
    assert row["has_paper_conflict"] is True
    assert row["execution_count"] == 1


def _live_account_collections(synced_at, total_asset=100000):
    return {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "total_asset": total_asset,
                    "cash": 20000,
                    "available_cash": 19000,
                    "market_value": 80000,
                    "synced_at": synced_at,
                }
            ]
        )
    }


def _calendar_collections(trade_date="20261109", is_open=1):
    return {"trade_calendar": _Collection([{"exchange": "SSE", "trade_date": trade_date, "is_open": is_open}])}


def test_build_account_equity_live_point_on_trading_day():
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)
    source_synced_at = now.timestamp() - 30

    point = build_account_equity_live_point(
        _live_account_collections(source_synced_at),
        _calendar_collections(),
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point["date"] == "20261109"
    assert point["total_asset"] == 100000
    assert point["is_intraday"] is True
    assert point["is_stale"] is False
    assert point["live_synced_at"] == source_synced_at


def test_build_account_equity_live_point_accepts_string_calendar_flag():
    """2026-11-10 起 trade_calendar.is_open 存成字符串 "1"，门禁必须兼容。"""
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)

    point = build_account_equity_live_point(
        _live_account_collections(now.timestamp() - 30),
        _calendar_collections(is_open="1"),
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point["is_intraday"] is True


def test_build_account_equity_live_point_skips_closed_calendar_day():
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)

    point = build_account_equity_live_point(
        _live_account_collections(now.timestamp() - 30),
        _calendar_collections(is_open="0"),
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point is None


def test_build_account_equity_live_point_skips_non_trading_day():
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)

    point = build_account_equity_live_point(
        _live_account_collections(now.timestamp() - 30),
        {"trade_calendar": _Collection()},
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point is None


def test_build_account_equity_live_point_skips_stale_snapshot():
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)

    point = build_account_equity_live_point(
        _live_account_collections(now.timestamp() - 3600),
        _calendar_collections(),
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point is None


def test_build_account_equity_live_point_skips_zero_total_asset():
    now = datetime(2026, 11, 9, 15, 0, tzinfo=SHANGHAI_TZ)

    point = build_account_equity_live_point(
        _live_account_collections(now.timestamp() - 30, total_asset=0),
        _calendar_collections(),
        user_id="user-1",
        securities_account_id="acct-1",
        now=now,
    )

    assert point is None


def _account_pnl_collections():
    return {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "total_asset": 100000,
                    "cash": 20000,
                    "available_cash": 19000,
                    "market_value": 80000,
                    "synced_at": 1,
                }
            ]
        ),
        "trader_positions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "symbol": "000001.SZ",
                    "qty": 1000,
                }
            ]
        ),
        "trader_account_equity": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "date": "20261111",
                    "total_asset": 100000,
                }
            ]
        ),
    }


def test_snapshot_account_equity_stores_position_daily_pnl_estimate():
    collections = _account_pnl_collections()
    data_collections = {
        "volume_price": _Collection(
            [
                {"symbol": "000001.SZ", "trade_date": "20261111", "close": 10.0},
                {"symbol": "000001.SZ", "trade_date": "20261112", "close": 9.5},
            ]
        )
    }

    snapshot_account_equity(collections, data_collections, date="20261112", stale_seconds=0)

    row = next(doc for doc in collections["trader_account_equity"].docs if doc["date"] == "20261112")
    assert row["daily_pnl"] == -500.0
    assert round(row["daily_pnl_ratio"], 6) == -0.005
    assert row["daily_pnl_method"] == "positions_close_estimate"
    assert row["positions_snapshot"] == [
        {"symbol": "000001.SZ", "qty": 1000.0, "close": 9.5, "prev_close": 10.0}
    ]


def test_snapshot_account_equity_marks_daily_pnl_unknown_without_close_pair():
    collections = _account_pnl_collections()
    data_collections = {
        "volume_price": _Collection(
            [{"symbol": "000001.SZ", "trade_date": "20261111", "close": 10.0}]
        )
    }

    snapshot_account_equity(collections, data_collections, date="20261112", stale_seconds=0)

    row = next(doc for doc in collections["trader_account_equity"].docs if doc["date"] == "20261112")
    assert row["daily_pnl"] is None
    assert row["daily_pnl_ratio"] is None
    assert row["daily_pnl_unpriced_symbols"] == ["000001.SZ"]


def test_snapshot_account_equity_uses_etf_daily_for_etf_positions():
    collections = _account_pnl_collections()
    collections["trader_positions"] = _Collection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "510300.SH",
                "qty": 100,
            }
        ]
    )
    data_collections = {
        "etf_daily": _Collection(
            [
                {"ts_code": "510300.SH", "trade_date": "20261111", "close": 4.15},
                {"ts_code": "510300.SH", "trade_date": "20261112", "close": 4.114},
            ]
        )
    }

    snapshot_account_equity(collections, data_collections, date="20261112", stale_seconds=0)

    row = next(doc for doc in collections["trader_account_equity"].docs if doc["date"] == "20261112")
    assert row["daily_pnl"] == -3.6
    assert row["positions_snapshot"][0]["symbol"] == "510300.SH"

