"""Unit tests for intraday T+0 health checks."""

from __future__ import annotations

import pytest

from services import intraday_t0_health as health
from tests.unit.test_market_insight_engine import FakeCollection


class _FakeAdmin:
    def command(self, name):
        if name == "ping":
            return {"ok": 1}
        raise ValueError(name)


class _FakeDatabase:
    def __init__(self, names):
        self._names = names
        self.client = self
        self.admin = _FakeAdmin()

    def list_collection_names(self):
        return list(self._names)


@pytest.mark.unit
def test_engine_smoke_check_passes():
    result = health._engine_smoke_check()
    assert result.status == "pass"
    assert "SELL_WATCH" in result.detail


@pytest.mark.unit
def test_run_liveness_checks_passes_with_mocks(monkeypatch):
    finance_db = _FakeDatabase(list(health.FINANCE_COLLECTIONS))
    signals = FakeCollection([])
    signals.database = finance_db

    monkeypatch.setattr(health, "get_db_collections", lambda: {"intraday_t0_signals": signals})
    monkeypatch.setattr(health, "realtime_db", _FakeDatabase(list(health.REALTIME_COLLECTIONS)))
    monkeypatch.setattr(health, "ensure_intraday_t0_indexes", lambda: None)
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "test-secret-key")

    report = health.run_liveness_checks()
    assert report.mode == "liveness"
    assert report.overall == "pass"
    assert any(c.name == "engine_smoke" and c.status == "pass" for c in report.checks)


@pytest.mark.unit
def test_run_readiness_fails_without_realtime_data(monkeypatch):
    finance_db = _FakeDatabase(list(health.FINANCE_COLLECTIONS))
    signals = FakeCollection([])
    signals.database = finance_db
    empty = FakeCollection([])

    monkeypatch.setattr(
        health,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "trader_positions": empty,
            "intraday_t0_mock_positions": empty,
        },
    )
    monkeypatch.setattr(
        health,
        "get_data_db",
        lambda: {
            "volume_price_realtime_latest": empty,
            "market_spectrum_realtime_daily": empty,
        },
    )
    monkeypatch.setattr(health, "realtime_db", _FakeDatabase(list(health.REALTIME_COLLECTIONS)))
    monkeypatch.setattr(health, "ensure_intraday_t0_indexes", lambda: None)
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "test-secret-key")

    report = health.run_readiness_checks(trade_date="20260820")
    assert report.mode == "readiness"
    assert report.overall == "fail"
    failed = {c.name for c in report.checks if c.status == "fail" and c.required}
    assert "realtime_quotes" in failed
    assert "orderbook_fields" in failed
    assert "market_spectrum" in failed


@pytest.mark.unit
def test_health_report_to_dict():
    report = health.HealthReport(mode="liveness", trade_date="20260820", overall="pass", checks=[])
    body = report.to_dict()
    assert body["success"] is True
    assert body["mode"] == "liveness"
    assert "timestamp" in body
