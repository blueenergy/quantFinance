"""T+0 books are account-scoped: same symbol held in two accounts must not mix."""

from __future__ import annotations

import pytest
from bson import ObjectId

from services import intraday_t0_signals as t0
from services import intraday_t0_trades as trades
from tests.unit.test_market_insight_engine import FakeCollection


class _InsertCollection(FakeCollection):
    def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = doc.get("_id") or ObjectId()
        self.rows.append(doc)

        class _Result:
            inserted_id = doc["_id"]

        return _Result()


def _positions():
    return FakeCollection(
        [
            {
                "user_id": "u1",
                "securities_account_id": "sec-a",
                "symbol": "600114.SH",
                "qty": 13700,
                "available_qty": 13700,
                "avg_price": 30.2562,
            },
            {
                "user_id": "u1",
                "securities_account_id": "sec-b",
                "symbol": "600114.SH",
                "qty": 144100,
                "available_qty": 144100,
                "avg_price": 21.9164,
            },
        ]
    )


@pytest.mark.unit
def test_iter_t0_books_returns_each_account(monkeypatch):
    accounts = FakeCollection(
        [
            {"user_id": "u1", "securities_account_id": "sec-a"},
            {"user_id": "u1", "securities_account_id": "sec-b"},
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "trader_accounts": accounts,
            "intraday_t0_mock_positions": FakeCollection([]),
        },
    )

    assert t0.iter_t0_books() == [("u1", "sec-a"), ("u1", "sec-b")]


@pytest.mark.unit
def test_get_user_positions_filters_by_account(monkeypatch):
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(t0, "resolve_stock_info", lambda symbol: {})
    monkeypatch.setattr(t0, "lookup_sw_industry", lambda symbol: {})
    monkeypatch.setattr(t0, "enrich_position", lambda position, trade_date=None: position)
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "trader_positions": _positions(),
            "intraday_t0_mock_positions": FakeCollection([]),
            "user_watchlists": FakeCollection([]),
        },
    )

    _, rows = t0.get_user_positions("u1", source="real", securities_account_id="sec-a")

    assert [row["symbol"] for row in rows] == ["600114.SH"]
    assert rows[0]["securities_account_id"] == "sec-a"


@pytest.mark.unit
def test_generated_signals_and_snapshots_carry_account(monkeypatch):
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(t0, "resolve_stock_info", lambda symbol: {})
    monkeypatch.setattr(t0, "lookup_sw_industry", lambda symbol: {})
    monkeypatch.setattr(t0, "enrich_position", lambda position, trade_date=None: position)
    monkeypatch.setattr(t0, "build_features", lambda position, td, st: {"marker": 1})
    monkeypatch.setattr(
        t0, "classify_signal", lambda position, features, st: ("SELL_WATCH", 70.0, ["test"])
    )
    monkeypatch.setattr(t0, "cooldown_suppressed", lambda *a, **k: False)
    signals = _InsertCollection([])
    snapshots = FakeCollection([])
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "trader_positions": _positions(),
            "intraday_t0_signals": signals,
            "intraday_t0_position_snapshots": snapshots,
            "intraday_t0_mock_positions": FakeCollection([]),
            "user_watchlists": FakeCollection([]),
        },
    )

    result = t0.generate_signals_for_user(
        "u1", trade_date="20260820", snapshot_time="202608201030", source="real",
        securities_account_id="sec-a",
    )

    assert result["securities_account_id"] == "sec-a"
    assert len(signals.rows) == 1
    assert signals.rows[0]["securities_account_id"] == "sec-a"
    assert signals.rows[0]["symbol"] == "600114.SH"
    assert snapshots.upserts
    assert snapshots.upserts[0][0]["securities_account_id"] == "sec-a"


@pytest.mark.unit
def test_preview_uses_the_signals_own_account(monkeypatch):
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(trades, "_TRADE_INDEXES_READY", True)
    monkeypatch.setattr(trades, "latest_market_spectrum", lambda td: {})
    signal = {
        "_id": ObjectId("64bfa1ec12aa6c4e3b123456"),
        "user_id": "u1",
        "securities_account_id": "sec-a",
        "symbol": "600114.SH",
        "signal_type": "SELL_WATCH",
        "signal_score": 72,
        "snapshot_time": "202608201030",
        "price": 20.0,
    }
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "trader_positions": _positions(),
            "intraday_t0_signals": FakeCollection([signal]),
            "intraday_t0_trades": FakeCollection([]),
            "trade_signals": FakeCollection([]),
        },
    )

    preview = trades.preview_trade_from_signal("u1", str(signal["_id"]))

    assert preview["securities_account_id"] == "sec-a"
    assert preview["available_qty"] == 13700
    assert preview["suggested_qty"] <= 13700


@pytest.mark.unit
def test_record_trade_rejects_account_mismatch(monkeypatch):
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(trades, "_TRADE_INDEXES_READY", True)
    signal = {
        "_id": ObjectId("64bfa1ec12aa6c4e3b123456"),
        "user_id": "u1",
        "securities_account_id": "sec-a",
        "symbol": "600114.SH",
        "signal_type": "SELL_WATCH",
        "signal_score": 72,
        "snapshot_time": "202608201030",
        "price": 20.0,
    }
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "trader_positions": _positions(),
            "intraday_t0_signals": FakeCollection([signal]),
            "intraday_t0_trades": _InsertCollection([]),
            "trade_signals": FakeCollection([]),
        },
    )

    with pytest.raises(ValueError, match="does not match the signal's account"):
        trades.record_trade(
            "u1",
            {
                "symbol": "600114.SH",
                "leg": "sell",
                "quantity": 100,
                "price": 20.0,
                "fees": 0.0,
                "signal_id": str(signal["_id"]),
                "securities_account_id": "sec-b",
            },
        )


@pytest.mark.unit
def test_open_pairs_are_scoped_by_account(monkeypatch):
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(trades, "_TRADE_INDEXES_READY", True)
    rows = _InsertCollection(
        [
            {
                "user_id": "u1",
                "securities_account_id": "sec-a",
                "pair_id": "p-a",
                "trade_date": "20260820",
                "symbol": "600114.SH",
                "leg": "sell",
                "quantity": 100,
                "price": 20.0,
                "fees": 0.0,
                "created_at": 1.0,
            },
            {
                "user_id": "u1",
                "securities_account_id": "sec-b",
                "pair_id": "p-b",
                "trade_date": "20260820",
                "symbol": "600114.SH",
                "leg": "sell",
                "quantity": 200,
                "price": 20.0,
                "fees": 0.0,
                "created_at": 2.0,
            },
        ]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {"intraday_t0_trades": rows},
    )

    open_a = trades.list_open_pairs("u1", "20260820", securities_account_id="sec-a")

    assert [pair["pair_id"] for pair in open_a] == ["p-a"]
