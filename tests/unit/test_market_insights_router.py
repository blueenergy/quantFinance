from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers import market_insights


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_insights.router, prefix="/api")
    return TestClient(app)


@pytest.mark.unit
def test_generate_internal_rejects_missing_internal_key(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "shared-secret")

    res = _client().post(
        "/api/market-insights/generate-internal",
        json={"trade_date": "20260515", "snapshot_time": "202605151030"},
    )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid or missing X-Hermes-Quant-Key"


@pytest.mark.unit
def test_generate_internal_passes_trade_date_to_engine(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "shared-secret")
    captured = {}

    def fake_generate(trade_date=None):
        captured["trade_date"] = trade_date
        return {"success": True, "trade_date": trade_date, "written": 2}

    monkeypatch.setattr(market_insights, "generate_market_insight_events", fake_generate)

    res = _client().post(
        "/api/market-insights/generate-internal",
        headers={"X-Hermes-Quant-Key": "shared-secret"},
        json={
            "trade_date": "20260515",
            "snapshot_time": "202605151030",
            "source": "auto",
            "sync_status": "complete",
        },
    )

    assert res.status_code == 200
    assert res.json() == {"success": True, "trade_date": "20260515", "written": 2}
    assert captured["trade_date"] == "20260515"


@pytest.mark.unit
def test_generate_internal_accepts_empty_payload(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "shared-secret")
    captured = {}

    def fake_generate(trade_date=None):
        captured["trade_date"] = trade_date
        return {"success": True, "trade_date": trade_date, "written": 0}

    monkeypatch.setattr(market_insights, "generate_market_insight_events", fake_generate)

    res = _client().post(
        "/api/market-insights/generate-internal",
        headers={"X-Hermes-Quant-Key": "shared-secret"},
    )

    assert res.status_code == 200
    assert res.json() == {"success": True, "trade_date": None, "written": 0}
    assert captured["trade_date"] is None
