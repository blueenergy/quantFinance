from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers import market_insights
from auth import get_current_active_user


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_insights.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
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


@pytest.mark.unit
def test_event_types_returns_usage_metadata():
    res = _client().get("/api/market-insights/event-types")

    assert res.status_code == 200
    data = res.json()["data"]
    assert "EARLY_SECTOR_DIFFUSION" in data
    assert data["EARLY_SECTOR_DIFFUSION"]["label"] == "早期扩散"
    assert data["SECTOR_FALSE_STRENGTH"]["category"] == "去妄存真类"
    assert data["COUNTER_TREND_SECTOR_STRENGTH"]["meaning"]


@pytest.mark.unit
def test_focus_passes_trade_date_and_limit_to_engine(monkeypatch):
    captured = {}

    def fake_focus(**kwargs):
        captured.update(kwargs)
        return {"success": True, "trade_date": kwargs["trade_date"], "count": 1, "data": [{"title": "焦点"}]}

    monkeypatch.setattr(market_insights, "get_market_insight_focus", fake_focus)

    res = _client().get("/api/market-insights/focus", params={"trade_date": "2026-05-15", "limit": 3})

    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert captured == {"trade_date": "2026-05-15", "limit": 3, "user_id": "u1"}


@pytest.mark.unit
def test_focus_feedback_passes_payload_to_engine(monkeypatch):
    captured = {}

    def fake_save(**kwargs):
        captured.update(kwargs)
        return {"success": True, "data": {"action": kwargs["action"], "label": "有价值"}}

    monkeypatch.setattr(market_insights, "save_market_insight_focus_feedback", fake_save)

    res = _client().post(
        "/api/market-insights/focus/feedback",
        json={
            "focus_key": "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
            "trade_date": "20260515",
            "action": "valuable",
            "note": "值得看",
        },
    )

    assert res.status_code == 200
    assert res.json()["data"]["label"] == "有价值"
    assert captured == {
        "user_id": "u1",
        "focus_key": "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
        "trade_date": "20260515",
        "action": "valuable",
        "note": "值得看",
    }


@pytest.mark.unit
def test_focus_feedback_rejects_unknown_action(monkeypatch):
    def fake_save(**kwargs):
        raise ValueError("unsupported feedback action")

    monkeypatch.setattr(market_insights, "save_market_insight_focus_feedback", fake_save)

    res = _client().post(
        "/api/market-insights/focus/feedback",
        json={"focus_key": "key", "action": "bad"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "unsupported feedback action"


@pytest.mark.unit
def test_focus_review_passes_params_to_engine(monkeypatch):
    captured = {}

    def fake_review(**kwargs):
        captured.update(kwargs)
        return {"success": True, "trade_date": kwargs["trade_date"], "written": 2, "data": []}

    monkeypatch.setattr(market_insights, "review_market_insight_focus", fake_review)

    res = _client().post(
        "/api/market-insights/focus/review",
        params={"trade_date": "2026-05-15", "horizon_minutes": 60},
    )

    assert res.status_code == 200
    assert res.json()["written"] == 2
    assert captured == {"trade_date": "2026-05-15", "horizon_minutes": 60}


@pytest.mark.unit
def test_events_passes_filter_params_to_engine(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return [{"event_type": "SECTOR_FALSE_STRENGTH", "confidence": 82.0}]

    monkeypatch.setattr(market_insights, "list_market_insight_events", fake_list)

    res = _client().get(
        "/api/market-insights/events",
        params={
            "trade_date": "2026-05-15",
            "category": "去妄存真类",
            "severity": "medium",
            "min_confidence": 80,
            "level": "L2",
            "limit": 20,
        },
    )

    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert captured == {
        "trade_date": "2026-05-15",
        "event_type": None,
        "category": "去妄存真类",
        "severity": "medium",
        "min_confidence": 80.0,
        "level": "L2",
        "limit": 20,
    }


@pytest.mark.unit
def test_events_rejects_unknown_category():
    res = _client().get("/api/market-insights/events", params={"category": "不存在"})

    assert res.status_code == 400
    assert res.json()["detail"] == "unsupported category"
