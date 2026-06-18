"""Unit tests for manual signal repricing (preview + confirm)."""

import pytest
from bson import ObjectId

from services import trader_signal_reprice as sr


class _SignalsColl:
    def __init__(self, by_order_id):
        self._by = dict(by_order_id)
        self.inserted = []

    def find_one(self, query):
        return self._by.get(query.get("order_id"))

    def insert_one(self, doc):
        self.inserted.append(doc)


class _LogsColl:
    def __init__(self):
        self.rows = []

    def insert_one(self, doc):
        self.rows.append(doc)


class _MarketColl:
    def __init__(self, rows):
        self.rows = list(rows)

    def find_one(self, query, sort=None):
        symbols = set((query.get("symbol") or {}).get("$in") or [])
        rows = [row for row in self.rows if row.get("symbol") in symbols]
        if not rows:
            return None
        if sort:
            for key, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(key) or "", reverse=direction < 0)
        return rows[0]


def _oid() -> str:
    return str(ObjectId())


def test_resolve_market_reference_price_prefers_fresh_realtime(monkeypatch):
    monkeypatch.setattr(
        sr,
        "get_data_db",
        lambda: {
            "volume_price": _MarketColl([{"symbol": "000001.SZ", "trade_date": "2026-06-18", "close": 10.2}]),
            "volume_price_realtime_latest": _MarketColl(
                [{"symbol": "000001.SZ", "trade_date": "2026-06-18", "trade_time": "14:30:00", "close": 10.5}]
            ),
        },
    )

    assert sr.resolve_market_reference_price("000001.SZ") == (10.5, "realtime")


def test_resolve_market_reference_price_skips_stale_realtime(monkeypatch):
    monkeypatch.setattr(
        sr,
        "get_data_db",
        lambda: {
            "volume_price": _MarketColl([{"symbol": "000001.SZ", "trade_date": "2026-06-18", "close": 10.2}]),
            "volume_price_realtime_latest": _MarketColl(
                [{"symbol": "000001.SZ", "trade_date": "2026-06-13", "trade_time": "15:00:00", "close": 9.8}]
            ),
        },
    )

    assert sr.resolve_market_reference_price("000001.SZ") == (10.2, "daily_close")


def test_resolve_market_reference_price_falls_back_to_recent_realtime(monkeypatch):
    today = sr.datetime.utcnow().date().isoformat()
    monkeypatch.setattr(
        sr,
        "get_data_db",
        lambda: {
            "volume_price": _MarketColl([]),
            "volume_price_realtime_latest": _MarketColl(
                [{"symbol": "000001.SZ", "trade_date": today, "trade_time": "15:00:00", "price": 9.8}]
            ),
        },
    )

    assert sr.resolve_market_reference_price("000001.SZ") == (9.8, "realtime")


def test_preview_rejects_non_cancelled(monkeypatch):
    oid = _oid()
    signals = _SignalsColl(
        {
            "o1": {
                "order_id": "o1",
                "user_id": "u1",
                "action": "buy",
                "status": "submitted",
                "symbol": "000001.SZ",
                "securities_account_id": oid,
                "size": 100,
            }
        }
    )
    monkeypatch.setattr(sr, "resolve_market_reference_price", lambda sym: (10.0, "daily_close"))
    with pytest.raises(sr.RepriceError) as exc:
        sr.build_reprice_preview({"trade_signals": signals}, user_id="u1", order_id="o1")
    assert exc.value.status_code == 400


def test_preview_success(monkeypatch):
    oid = _oid()
    signals = _SignalsColl(
        {
            "o2": {
                "order_id": "o2",
                "user_id": "u1",
                "action": "buy",
                "status": "cancelled",
                "symbol": "000001.SZ",
                "securities_account_id": oid,
                "size": 200,
                "max_slippage_bps": 100,
                "effective_limit_price": 9.5,
                "reference_price": 9.4,
            }
        }
    )
    monkeypatch.setattr(sr, "resolve_market_reference_price", lambda sym: (10.0, "daily_close"))
    out = sr.build_reprice_preview({"trade_signals": signals}, user_id="u1", order_id="o2")
    assert out["success"] is True
    assert out["size"] == 200
    assert out["market_reference_price"] == 10.0
    assert out["suggested_effective_limit_price"] is not None


def test_confirm_rejects_out_of_band(monkeypatch):
    oid = _oid()
    signals = _SignalsColl(
        {
            "o3": {
                "order_id": "o3",
                "user_id": "u1",
                "action": "buy",
                "status": "cancelled",
                "symbol": "000001.SZ",
                "securities_account_id": oid,
                "size": 100,
                "plan_id": "p1",
                "strategy_template_id": "st1",
                "account_id": "acc",
                "broker": "miniQMT",
                "fee_model": {},
            }
        }
    )
    logs = _LogsColl()
    cols = {"trade_signals": signals, "portfolio_plan_operation_logs": logs, "securities_accounts": _SignalsColl({})}
    monkeypatch.setattr(sr, "resolve_market_reference_price", lambda sym: (10.0, "daily_close"))
    with pytest.raises(sr.RepriceError):
        sr.confirm_reprice_and_insert_signal(
            cols,
            user_id="u1",
            order_id="o3",
            effective_limit_price=50.0,
            size_override=None,
            reason="test",
            reviewer="tester",
        )


def test_confirm_inserts_signal(monkeypatch):
    oid = _oid()
    old = {
        "order_id": "o4",
        "user_id": "u1",
        "action": "buy",
        "status": "cancelled",
        "symbol": "000001.SZ",
        "securities_account_id": oid,
        "size": 100,
        "plan_id": "p1",
        "strategy_template_id": "st1",
        "account_id": "acc",
        "broker": "miniQMT",
        "fee_model": {},
        "max_slippage_bps": 100,
    }
    signals = _SignalsColl({"o4": old})
    logs = _LogsColl()
    cols = {"trade_signals": signals, "portfolio_plan_operation_logs": logs, "securities_accounts": _SignalsColl({})}
    monkeypatch.setattr(sr, "resolve_market_reference_price", lambda sym: (10.0, "daily_close"))
    out = sr.confirm_reprice_and_insert_signal(
        cols,
        user_id="u1",
        order_id="o4",
        effective_limit_price=10.5,
        size_override=80,
        reason="chase",
        reviewer="tester",
    )
    assert out["success"] is True
    assert len(signals.inserted) == 1
    doc = signals.inserted[0]
    assert doc["replaces_order_id"] == "o4"
    assert doc["size"] == 80
    assert doc["status"] == "pending"
    assert len(logs.rows) == 1
