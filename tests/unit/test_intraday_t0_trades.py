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
                "pair_id": "pair-1",
                "leg": "sell",
                "quantity": 100,
                "price": 10.0,
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
        },
    )
    monkeypatch.setattr(trades, "ensure_intraday_t0_trade_indexes", lambda: None)
    monkeypatch.setattr("services.intraday_t0_trades.latest_market_spectrum", lambda _td: {"yang_spectrum": 0.5})
    preview = trades.preview_trade_from_signal("u1", signal_id)
    assert preview["suggested_qty"] == 200
    assert preview["available_qty"] == 500
