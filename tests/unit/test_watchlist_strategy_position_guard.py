"""Tests for the strategy-disable position guard.

The manager owns the sleeve/state semantics; these tests pin the quantFinance
side: 409 before any write, keep/clear semantics, T+1 capping, and the
fail-safe fallback when the manager is unreachable.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.all_models import WatchlistStrategySetRequest
from routers import user_watchlist


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updates = []
        self.inserted = []

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            if isinstance(expected, dict):
                if "$in" in expected and doc.get(key) not in expected["$in"]:
                    return False
                if "$ne" in expected and doc.get(key) == expected["$ne"]:
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True

    def find(self, query):
        return [doc for doc in self.docs if self._matches(doc, query)]

    def find_one(self, query, sort=None):
        rows = self.find(query)
        return rows[0] if rows else None

    def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))

    def insert_one(self, doc):
        self.inserted.append(doc)


def _install(
    monkeypatch,
    *,
    strategies,
    signals=None,
    accounts=None,
    positions=None,
):
    collections = {
        "user_watchlists": _Collection([{"user_id": "u1", "symbols": []}]),
        "watchlist_strategies": _Collection(strategies),
        "trade_signals": _Collection(signals or []),
        "securities_accounts": _Collection(
            accounts
            if accounts is not None
            else [
                {
                    "_id": "acct-1",
                    "user_id": "u1",
                    "account_id": "A-0001",
                    "broker": "BrokerA",
                }
            ]
        ),
        "trader_positions": _Collection(positions or []),
    }
    monkeypatch.setattr(user_watchlist, "get_db_collections", lambda: collections)
    monkeypatch.setattr(
        user_watchlist,
        "reset_strategy_state",
        lambda *a, **k: {"ok": True, "reset": True},
    )
    monkeypatch.setattr(
        user_watchlist,
        "trigger_worker_reload",
        lambda *a, **k: {"ok": True, "started": 0, "stopped": 0},
    )
    return collections


def _guard(
    *,
    managed=1000,
    can_use=1000,
    clear_qty=None,
    can_clear=True,
    broker=1000,
    source="state_file",
    core_qty=0,
    pending_buy_count=0,
    pending_buy_qty=0,
    pending_sell_qty=0,
):
    if clear_qty is None:
        clear_qty = min(managed, can_use) if can_clear else 0
    return {
        "ok": True,
        "symbol": "600000.SH",
        "strategy_key": "hidden_dragon",
        "strategy_name": "潜龙低吸",
        "enabled": True,
        "account": {
            "bound": True,
            "securities_account_id": "acct-1",
            "account_id": "A-0001",
            "broker": "BrokerA",
        },
        "position": {
            "source": source,
            "broker_qty": broker,
            "can_use_volume": can_use,
            "managed_qty": managed,
            "core_qty": core_qty,
            "avg_price": 10.0,
            "last_price": 10.5,
            "market_value": 12000,
            "unrealized_pnl": 500.0,
            "unrealized_pnl_pct": 5.0,
            "entry_price": 9.5,
            "entry_date": "20260101",
            "snapshot_at": 1.0,
        },
        "pending": {
            "buy": {
                "count": pending_buy_count,
                "qty": pending_buy_qty,
                "order_ids": ["buy-1"] if pending_buy_count else [],
            },
            "sell": {
                "count": 1 if pending_sell_qty else 0,
                "qty": pending_sell_qty,
                "order_ids": ["sell-1"] if pending_sell_qty else [],
            },
        },
        "session": {"is_open": False, "next_open_at": "2026-11-10T09:25:00+08:00"},
        "can_auto_clear": can_clear,
        "clear_qty": clear_qty,
        "warnings": [],
    }


def _existing_600150(enabled=True):
    return {
        "user_id": "u1",
        "symbol": "600000.SH",
        "strategy_key": "hidden_dragon",
        "enabled": enabled,
        "params": {},
        "securities_account_id": "acct-1",
    }


def _req(**overrides):
    payload = {
        "symbol": "600000.SH",
        "strategy": "hidden_dragon",
        "enabled": False,
    }
    payload.update(overrides)
    return WatchlistStrategySetRequest(**payload)


def _user():
    return {"id": "u1", "username": "blue", "service_level": "vip"}


def test_explicit_securities_account_binding_is_written(monkeypatch):
    collections = _install(monkeypatch, strategies=[])

    user_watchlist.set_watchlist_strategy(
        WatchlistStrategySetRequest(
            symbol="600114.SH",
            strategy="daily_sma_cross",
            params={},
            enabled=False,
            securities_account_id="acct-1",
        ),
        _user(),
    )

    update_doc = collections["watchlist_strategies"].updates[-1][1]["$set"]
    assert update_doc["securities_account_id"] == "acct-1"


def test_unknown_securities_account_binding_returns_404(monkeypatch):
    _install(monkeypatch, strategies=[])

    with pytest.raises(HTTPException) as exc:
        user_watchlist.set_watchlist_strategy(
            WatchlistStrategySetRequest(
                symbol="600114.SH",
                strategy="daily_sma_cross",
                params={},
                enabled=False,
                securities_account_id="acct-OTHER",
            ),
            _user(),
        )

    assert exc.value.status_code == 404


def test_disable_without_position_writes_without_signal(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard(managed=0))

    result = user_watchlist.set_watchlist_strategy(_req(), _user())

    assert result["success"] is True
    assert result["disabled"] is True
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["enabled"] is False
    assert collections["trade_signals"].inserted == []


def test_disable_with_position_requires_explicit_action(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())

    with pytest.raises(HTTPException) as exc:
        user_watchlist.set_watchlist_strategy(_req(), _user())

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STRATEGY_POSITION_GUARD"
    assert exc.value.detail["guard"]["position"]["managed_qty"] == 1000
    assert collections["watchlist_strategies"].updates == []


def test_disable_keep_writes_without_signal(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())

    result = user_watchlist.set_watchlist_strategy(_req(position_action="keep"), _user())

    assert result["success"] is True
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["enabled"] is False
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["last_disable_action"] == "keep"
    assert collections["trade_signals"].inserted == []


def test_disable_clear_inserts_sell_for_managed_sleeve(monkeypatch):
    existing = {
        "user_id": "u1",
        "symbol": "600002.SH",
        "strategy_key": "daily_sma_cross",
        "enabled": True,
        "params": {"managed_shares": 2000, "core_shares": 8000},
        "securities_account_id": "acct-1",
    }
    collections = _install(monkeypatch, strategies=[existing])
    guard = _guard(
        managed=2000,
        broker=10000,
        can_use=10000,
        clear_qty=2000,
        source="broker_sleeve",
        core_qty=8000,
    )
    guard["symbol"] = "600002.SH"
    guard["strategy_key"] = "daily_sma_cross"
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: guard)

    req = WatchlistStrategySetRequest(
        symbol="600002.SH",
        strategy="daily_sma_cross",
        enabled=False,
        position_action="clear",
    )
    result = user_watchlist.set_watchlist_strategy(req, _user())

    assert result["flatten_qty"] == 2000
    assert len(collections["trade_signals"].inserted) == 1
    signal = collections["trade_signals"].inserted[0]
    assert signal["action"] == "sell"
    assert signal["size"] == 2000
    assert signal["order_type"] == "market"
    assert signal["source"] == "strategy_disable"
    assert signal["securities_account_id"] == "acct-1"
    assert signal["meta"]["core_qty"] == 8000
    assert signal["activate_after"] is not None
    assert result["state_reset"] is True
    assert result["worker_reload"] is True


def test_clear_is_capped_by_t1_available(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(
        user_watchlist, "fetch_position_guard",
        lambda *a, **k: _guard(managed=1000, can_use=900, clear_qty=900),
    )

    result = user_watchlist.set_watchlist_strategy(_req(position_action="clear"), _user())

    assert result["flatten_qty"] == 900
    assert collections["trade_signals"].inserted[0]["size"] == 900


def test_clear_still_succeeds_when_state_reset_fails(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())
    monkeypatch.setattr(user_watchlist, "reset_strategy_state", lambda *a, **k: None)

    result = user_watchlist.set_watchlist_strategy(_req(position_action="clear"), _user())

    assert result["success"] is True
    assert result["state_reset"] is False
    assert len(collections["trade_signals"].inserted) == 1


def test_reload_failure_still_writes_and_reports_false(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())
    monkeypatch.setattr(user_watchlist, "trigger_worker_reload", lambda *a, **k: None)

    result = user_watchlist.set_watchlist_strategy(_req(position_action="keep"), _user())

    assert result["success"] is True
    assert result["worker_reload"] is False
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["enabled"] is False


def test_clear_with_zero_sellable_returns_400(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(
        user_watchlist, "fetch_position_guard",
        lambda *a, **k: _guard(managed=1000, can_use=0, can_clear=False, clear_qty=0),
    )

    with pytest.raises(HTTPException) as exc:
        user_watchlist.set_watchlist_strategy(_req(position_action="clear"), _user())

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "POSITION_CLEAR_UNAVAILABLE"
    assert collections["watchlist_strategies"].updates == []


def test_manager_unavailable_with_broker_position_requires_keep(monkeypatch):
    collections = _install(
        monkeypatch,
        strategies=[_existing_600150()],
        positions=[{
            "user_id": "u1",
            "symbol": "600000.SH",
            "qty": 1000,
            "can_use_volume": 1000,
            "securities_account_id": "acct-1",
            "updated_at": 1,
            "avg_price": 10.0,
            "last_price": 10.5,
        }],
    )
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: None)

    with pytest.raises(HTTPException) as exc:
        user_watchlist.set_watchlist_strategy(_req(), _user())

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STRATEGY_POSITION_GUARD_UNAVAILABLE"
    assert exc.value.detail["guard"]["position"]["managed_qty"] is None
    assert exc.value.detail["guard"]["position"]["broker_qty"] == 1000


def test_manager_unavailable_without_position_disables(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150()])
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: None)

    result = user_watchlist.set_watchlist_strategy(_req(), _user())

    assert result["success"] is True
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["enabled"] is False


def test_retry_reuses_existing_disable_sell(monkeypatch):
    existing = _existing_600150(enabled=False)
    signals = [{
        "order_id": "strategy-disable-old",
        "user_id": "u1",
        "symbol": "600000.SH",
        "mode": "live",
        "action": "sell",
        "source": "strategy_disable",
        "status": "pending",
        "size": 1000,
        "securities_account_id": "acct-1",
    }]
    collections = _install(monkeypatch, strategies=[existing], signals=signals)
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())

    result = user_watchlist.set_watchlist_strategy(_req(position_action="clear"), _user())

    assert result["flatten_order_id"] == "strategy-disable-old"
    assert result["flatten_qty"] == 1000
    assert collections["trade_signals"].inserted == []


def test_already_disabled_without_action_skips_guard(monkeypatch):
    collections = _install(monkeypatch, strategies=[_existing_600150(enabled=False)])

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("guard should not run for an already-disabled config")

    monkeypatch.setattr(user_watchlist, "fetch_position_guard", _must_not_be_called)

    result = user_watchlist.set_watchlist_strategy(_req(), _user())

    assert result["success"] is True
    assert collections["watchlist_strategies"].updates[-1][1]["$set"]["enabled"] is False


def test_clear_can_request_pending_buy_cancellation(monkeypatch):
    buy = {
        "order_id": "buy-1",
        "user_id": "u1",
        "symbol": "600000.SH",
        "mode": "live",
        "action": "BUY",
        "status": "pending",
        "size": 1000,
        "securities_account_id": "acct-1",
    }
    collections = _install(
        monkeypatch, strategies=[_existing_600150()], signals=[buy]
    )
    monkeypatch.setattr(user_watchlist, "fetch_position_guard", lambda *a, **k: _guard())

    user_watchlist.set_watchlist_strategy(
        _req(position_action="clear", cancel_pending_buys=True), _user()
    )

    cancelled = [
        update for _, update, _ in collections["trade_signals"].updates
        if update.get("$set", {}).get("status") == "cancelled"
    ]
    assert len(cancelled) == 1
