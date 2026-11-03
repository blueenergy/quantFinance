"""Regression tests for watchlist strategy params merge semantics.

2026-11-01 18:25: a partial params payload (the three universal sizing keys
produced by the dashboard preset bug) replaced the whole params object and
silently dropped ``state_prefix`` / ``core_shares`` on the 600114 strategy.
The API now merges params over the existing document and only removes keys
explicitly listed in ``deleted_keys``.
"""

from models.all_models import WatchlistStrategySetRequest
from routers import user_watchlist


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.last_update = None

    def find_one(self, query, sort=None):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None

    def update_one(self, query, update, upsert=False):
        self.last_update = (query, update, upsert)
        return None


def _install(monkeypatch, strategy_docs):
    strategy_col = _Collection(strategy_docs)
    watchlist_col = _Collection([{"user_id": "u1", "symbols": ["600114.SH"]}])
    monkeypatch.setattr(
        user_watchlist,
        "get_db_collections",
        lambda: {
            "user_watchlists": watchlist_col,
            "watchlist_strategies": strategy_col,
        },
    )
    return strategy_col


def _existing_params():
    return {
        "state_prefix": "daily_single_stream",
        "account_capital": 2845000,
        "position_pct": 0.3,
        "managed_shares": 40000,
        "core_shares": 104100,
    }


def _existing_doc():
    return {
        "user_id": "u1",
        "symbol": "600114.SH",
        "strategy_key": "daily_sma_cross",
        "enabled": True,
        "params": _existing_params(),
    }


def test_partial_params_preserve_state_prefix_and_core_shares(monkeypatch):
    """The exact 2026-11-01 payload must no longer wipe the other keys."""
    strategy_col = _install(monkeypatch, [_existing_doc()])
    req = WatchlistStrategySetRequest(
        symbol="600114.SH",
        strategy="daily_sma_cross",
        params={
            "account_capital": 2840000,
            "position_pct": 0.3,
            "managed_shares": 40000,
        },
        enabled=True,
    )

    user_watchlist.set_watchlist_strategy(req, {"id": "u1"})

    params = strategy_col.last_update[1]["$set"]["params"]
    assert params["state_prefix"] == "daily_single_stream"
    assert params["core_shares"] == 104100
    # Sent keys still win.
    assert params["account_capital"] == 2840000
    assert params["position_pct"] == 0.3
    assert params["managed_shares"] == 40000


def test_deleted_keys_are_removed(monkeypatch):
    strategy_col = _install(monkeypatch, [_existing_doc()])
    req = WatchlistStrategySetRequest(
        symbol="600114.SH",
        strategy="daily_sma_cross",
        params={"account_capital": 2840000},
        deleted_keys=["managed_shares"],
        enabled=True,
    )

    user_watchlist.set_watchlist_strategy(req, {"id": "u1"})

    params = strategy_col.last_update[1]["$set"]["params"]
    assert "managed_shares" not in params
    assert params["state_prefix"] == "daily_single_stream"
    assert params["core_shares"] == 104100
    assert params["account_capital"] == 2840000


def test_omitted_params_leave_existing_params_untouched(monkeypatch):
    """Toggles can omit params instead of echoing a possibly stale snapshot."""
    strategy_col = _install(monkeypatch, [_existing_doc()])
    req = WatchlistStrategySetRequest(
        symbol="600114.SH",
        strategy="daily_sma_cross",
        enabled=False,
    )

    user_watchlist.set_watchlist_strategy(req, {"id": "u1"})

    params = strategy_col.last_update[1]["$set"]["params"]
    assert params == _existing_params()
    assert strategy_col.last_update[1]["$set"]["enabled"] is False


def test_new_strategy_with_omitted_params_starts_empty(monkeypatch):
    strategy_col = _install(monkeypatch, [])
    req = WatchlistStrategySetRequest(
        symbol="600114.SH",
        strategy="daily_sma_cross",
        enabled=False,
    )

    user_watchlist.set_watchlist_strategy(req, {"id": "u1"})

    params = strategy_col.last_update[1]["$set"]["params"]
    assert params == {}
