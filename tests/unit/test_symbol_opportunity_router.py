import pytest

from routers import symbol_opportunity
from tests.unit.test_symbol_risk_router import _Collection


def _analyzer_db():
    return {
        "symbol_opportunity_findings": _Collection(),
        "industry_opportunity_findings": _Collection(),
        "symbol_risk_findings": _Collection(),
        "industry_risk_findings": _Collection(),
    }


def _trading_collections():
    return {"symbol_opportunity_operation_logs": _Collection()}


def test_add_manual_symbol_opportunity(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    monkeypatch.setattr(symbol_opportunity, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_opportunity, "get_trading_db_collections", lambda: trading)

    response = symbol_opportunity.add_manual_symbol_opportunity(
        "688001.SH",
        symbol_opportunity.ManualOpportunityFindingRequest(
            summary="国产替代机会",
            strength="high",
            category="policy_tailwind",
        ),
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    row = analyzer["symbol_opportunity_findings"].docs[0]
    assert row["status"] == "active"
    assert row["strength"] == "high"
    assert trading["symbol_opportunity_operation_logs"].docs[0]["operation_type"] == "manual_add"


def test_realize_symbol_opportunity(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    analyzer["symbol_opportunity_findings"].insert_one(
        {
            "symbol": "688001.SH",
            "finding_key": "policy_tailwind:国产替代",
            "status": "active",
            "category": "policy_tailwind",
            "strength": "medium",
            "summary": "国产替代机会",
            "resolution_mode": "event",
        }
    )
    monkeypatch.setattr(symbol_opportunity, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_opportunity, "get_trading_db_collections", lambda: trading)

    response = symbol_opportunity.realize_symbol_opportunity(
        "688001.SH",
        "policy_tailwind:国产替代",
        symbol_opportunity.CloseOpportunityFindingRequest(reason="订单兑现"),
        current_user={"id": "user-1"},
    )

    assert response["data"]["outcome"] == "realized"
    row = analyzer["symbol_opportunity_findings"].docs[0]
    assert row["status"] == "closed"
    assert row["outcome"] == "realized"
    assert trading["symbol_opportunity_operation_logs"].docs[0]["operation_type"] == "realize"


def test_invalidate_symbol_opportunity_can_spawn_risk(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    analyzer["symbol_opportunity_findings"].insert_one(
        {
            "symbol": "688001.SH",
            "finding_key": "demand_growth:ai订单",
            "status": "active",
            "category": "demand_growth",
            "subject": "ai订单",
            "strength": "high",
            "summary": "AI订单机会",
            "resolution_mode": "event",
        }
    )
    monkeypatch.setattr(symbol_opportunity, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_opportunity, "get_trading_db_collections", lambda: trading)

    response = symbol_opportunity.invalidate_symbol_opportunity(
        "688001.SH",
        "demand_growth:ai订单",
        symbol_opportunity.CloseOpportunityFindingRequest(reason="订单证伪", spawn_risk=True),
        current_user={"id": "user-1"},
    )

    assert response["data"]["outcome"] == "invalidated"
    assert response["data"]["spawned_risk_finding_key"]
    risk = analyzer["symbol_risk_findings"].docs[0]
    assert risk["severity"] == "low"
    assert risk["origin_opportunity_finding_key"] == "demand_growth:ai订单"


def test_reopen_symbol_opportunity_not_found(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    monkeypatch.setattr(symbol_opportunity, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_opportunity, "get_trading_db_collections", lambda: trading)

    with pytest.raises(symbol_opportunity.HTTPException) as exc:
        symbol_opportunity.reopen_symbol_opportunity(
            "688001.SH",
            "missing:key",
            symbol_opportunity.CloseOpportunityFindingRequest(),
            current_user={"id": "user-1"},
        )

    assert exc.value.status_code == 404
