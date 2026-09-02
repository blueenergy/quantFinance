"""Unit tests for intraday T+0 manual trade ledger."""

from __future__ import annotations

import pytest
from bson import ObjectId

from services import intraday_t0_trades as trades
from tests.unit.test_market_insight_engine import FakeCollection


@pytest.mark.unit
def test_record_sell_and_buy_pair(monkeypatch):
    trade_rows = []
    trades_coll = FakeCollection(trade_rows)

    class _InsertCollection(FakeCollection):
        def insert_one(self, doc):
            doc = dict(doc)
            doc["_id"] = ObjectId()
            self.rows.append(doc)
            class _Result:
                inserted_id = doc["_id"]

            return _Result()

    trades_coll = _InsertCollection(trade_rows)
    signals = FakeCollection(
        [
            {
                "_id": ObjectId("64bfa1ec12aa6c4e3b123456"),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "signal_score": 72,
                "snapshot_time": "202608201030",
            }
        ]
    )
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "qty": 500, "available_qty": 400}]
    )

    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": trades_coll,
            "intraday_t0_signals": signals,
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)

    sell = trades.record_trade(
        "u1",
        {
            "symbol": "300750.SZ",
            "leg": "sell",
            "quantity": 100,
            "price": 200.0,
            "trade_date": "20260820",
            "signal_id": "64bfa1ec12aa6c4e3b123456",
        },
    )
    pair_id = sell["data"]["pair_id"]
    trades.record_trade(
        "u1",
        {
            "symbol": "300750.SZ",
            "leg": "buy",
            "quantity": 100,
            "price": 198.5,
            "trade_date": "20260820",
            "pair_id": pair_id,
            "fees": 10.0,
        },
    )
    summary = trades.day_summary("u1", trade_date="20260820")
    assert summary["closed_pairs"] == 1
    assert summary["open_pairs"] == 0
    assert summary["total_gross_pnl"] == pytest.approx(150.0)
    assert summary["total_net_pnl"] == pytest.approx(140.0)


@pytest.mark.unit
def test_buy_exceeds_uncovered_rejected(monkeypatch):
    trades_coll = FakeCollection(
        [
            {
                "_id": ObjectId(),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "pair_id": "pair-1",
                "leg": "sell",
                "quantity": 100,
                "price": 10.0,
                "created_at": "2026-08-20T10:00:00",
            }
        ]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": trades_coll,
            "intraday_t0_signals": FakeCollection([]),
            "trader_positions": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    with pytest.raises(ValueError, match="uncovered"):
        trades.record_trade(
            "u1",
            {
                "symbol": "300750.SZ",
                "leg": "buy",
                "quantity": 200,
                "price": 9.8,
                "pair_id": "pair-1",
            },
        )


@pytest.mark.unit
def test_day_close_check_alerts_open_pair(monkeypatch):
    trades_coll = FakeCollection(
        [
            {
                "_id": ObjectId(),
                "user_id": "u1",
                "trade_date": "20260820",
                "symbol": "300750.SZ",
                "pair_id": "pair-1",
                "leg": "sell",
                "quantity": 100,
                "price": 10.0,
                "fees": 0,
                "created_at": "2026-08-20T10:00:00",
            }
        ]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {"intraday_t0_trades": trades_coll},
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    result = trades.day_close_check("u1", trade_date="20260820")
    assert result["requires_action"] is True
    assert result["alert_count"] == 1
    assert result["alerts"][0]["code"] == "uncovered_sell"
    assert result["alerts"][0]["style"] == "sell_first"


@pytest.mark.unit
def test_record_buy_first_and_sell_pair(monkeypatch):
    trade_rows = []

    class _InsertCollection(FakeCollection):
        def insert_one(self, doc):
            doc = dict(doc)
            doc["_id"] = ObjectId()
            self.rows.append(doc)

            class _Result:
                inserted_id = doc["_id"]

            return _Result()

    trades_coll = _InsertCollection(trade_rows)
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "qty": 500, "available_qty": 400}]
    )

    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": trades_coll,
            "intraday_t0_signals": FakeCollection([]),
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)

    buy = trades.record_trade(
        "u1",
        {
            "symbol": "300750.SZ",
            "leg": "buy",
            "quantity": 100,
            "price": 198.0,
            "trade_date": "20260820",
        },
    )
    pair_id = buy["data"]["pair_id"]
    trades.record_trade(
        "u1",
        {
            "symbol": "300750.SZ",
            "leg": "sell",
            "quantity": 100,
            "price": 200.0,
            "trade_date": "20260820",
            "pair_id": pair_id,
            "fees": 5.0,
        },
    )
    summary = trades.day_summary("u1", trade_date="20260820")
    assert summary["closed_pairs"] == 1
    assert summary["open_pairs"] == 0
    assert summary["naked_buy_qty"] == 0
    assert summary["total_gross_pnl"] == pytest.approx(200.0)
    assert summary["total_net_pnl"] == pytest.approx(195.0)


@pytest.mark.unit
def test_buy_first_open_exceeds_capacity_rejected(monkeypatch):
    trades_coll = FakeCollection([])
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 100}]
    )

    class _InsertCollection(FakeCollection):
        def insert_one(self, doc):
            doc = dict(doc)
            doc["_id"] = ObjectId()
            self.rows.append(doc)

            class _Result:
                inserted_id = doc["_id"]

            return _Result()

    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": _InsertCollection(trade_rows := []),
            "intraday_t0_signals": FakeCollection([]),
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    with pytest.raises(ValueError, match="buy_first capacity"):
        trades.record_trade(
            "u1",
            {
                "symbol": "300750.SZ",
                "leg": "buy",
                "quantity": 200,
                "price": 10.0,
            },
        )


@pytest.mark.unit
def test_buy_first_open_capacity_reserves_other_open_pairs(monkeypatch):
    monkeypatch.setattr(trades, "_broker_available_qty", lambda *_a, **_k: 300)
    monkeypatch.setattr(
        trades,
        "list_open_pairs",
        lambda *_a, **_k: [
            {
                "symbol": "300750.SZ",
                "style": "buy_first",
                "uncovered_qty": 200,
                "status": "open",
            }
        ],
    )
    assert trades._buy_first_open_capacity("u1", "300750.SZ") == 100


@pytest.mark.unit
def test_buy_first_open_rejected_when_existing_open_pair(monkeypatch):
    trades_coll = FakeCollection(
        [
            {
                "_id": ObjectId(),
                "user_id": "u1",
                "trade_date": "20260820",
                "symbol": "300750.SZ",
                "pair_id": "pair-open",
                "leg": "buy",
                "quantity": 200,
                "price": 10.0,
                "fees": 0,
                "created_at": "2026-08-20T10:00:00",
            }
        ]
    )
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 300}]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": trades_coll,
            "intraday_t0_signals": FakeCollection([]),
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    with pytest.raises(ValueError, match="open pair exists"):
        trades.record_trade(
            "u1",
            {
                "symbol": "300750.SZ",
                "leg": "buy",
                "quantity": 100,
                "price": 10.0,
                "trade_date": "20260820",
            },
        )


@pytest.mark.unit
def test_day_close_check_alerts_uncovered_buy(monkeypatch):
    trades_coll = FakeCollection(
        [
            {
                "_id": ObjectId(),
                "user_id": "u1",
                "trade_date": "20260820",
                "symbol": "300750.SZ",
                "pair_id": "pair-1",
                "leg": "buy",
                "quantity": 100,
                "price": 10.0,
                "fees": 0,
                "created_at": "2026-08-20T10:00:00",
            }
        ]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {"intraday_t0_trades": trades_coll},
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    result = trades.day_close_check("u1", trade_date="20260820")
    assert result["requires_action"] is True
    assert result["alerts"][0]["code"] == "uncovered_buy"
    assert result["alerts"][0]["style"] == "buy_first"


@pytest.mark.unit
def test_preview_trade_buy_first_without_open_pair(monkeypatch):
    signal_id = str(ObjectId())
    oid = ObjectId(signal_id)
    signals = FakeCollection(
        [
            {
                "_id": oid,
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "signal_score": 80,
            }
        ]
    )
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 400}]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    monkeypatch.setattr("services.intraday_t0_trades.latest_market_spectrum", lambda _td: {"yang_spectrum": 0.5})
    preview = trades.preview_trade_from_signal("u1", signal_id, buy_qty_ratio=0.5)
    assert preview["pair_style"] == "buy_first"
    assert preview["suggested_qty"] == 200
    assert preview["available_qty"] == 400


@pytest.mark.unit
def test_preview_trade_from_signal(monkeypatch):
    signal_id = str(ObjectId())
    oid = ObjectId(signal_id)
    signals = FakeCollection(
        [
            {
                "_id": oid,
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "signal_score": 80,
            }
        ]
    )
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 500}]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "trader_positions": positions,
            "trade_signals": FakeCollection([]),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    monkeypatch.setattr("services.intraday_t0_trades.latest_market_spectrum", lambda _td: {"yang_spectrum": 0.5})
    preview = trades.preview_trade_from_signal("u1", signal_id)
    assert preview["suggested_qty"] == 200
    assert preview["available_qty"] == 500
    assert preview["pair_style"] == "sell_first"


@pytest.mark.unit
def test_available_qty_yields_to_in_flight_sells(monkeypatch):
    """A pending strategy reduction must not be preempted by a T+0 leg."""
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "signal_score": 80,
            }
        ]
    )
    positions = FakeCollection(
        [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 500}]
    )
    live_signals = FakeCollection(
        [
            {
                "user_id": "u1",
                "symbol": "300750.SZ",
                "mode": "live",
                "action": "sell",
                "status": "submitted",
                "size": 300,
            },
            # Filled orders are already reflected in the broker snapshot, and a
            # buy never competes for sellable shares.
            {
                "user_id": "u1",
                "symbol": "300750.SZ",
                "mode": "live",
                "action": "sell",
                "status": "filled",
                "size": 100,
            },
            {
                "user_id": "u1",
                "symbol": "300750.SZ",
                "mode": "live",
                "action": "buy",
                "status": "submitted",
                "size": 400,
            },
        ]
    )
    monkeypatch.setattr(
        trades,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "trader_positions": positions,
            "trade_signals": live_signals,
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    monkeypatch.setattr("services.intraday_t0_trades.latest_market_spectrum", lambda _td: {"yang_spectrum": 0.5})

    preview = trades.preview_trade_from_signal("u1", signal_id)
    assert preview["available_qty"] == 200
    assert preview["suggested_qty"] == 100
