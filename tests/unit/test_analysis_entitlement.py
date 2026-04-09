import pytest
import asyncio

from routers import analysis


class _DummyQuotaManager:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.mark.unit
def test_watchlist_opportunities_returns_json_when_blocked(monkeypatch):
    monkeypatch.setattr(analysis, "QuotaManager", _DummyQuotaManager)
    monkeypatch.setattr(
        analysis,
        "resolve_action_entitlement",
        lambda **_kwargs: {"type": "blocked", "detail": "blocked by matrix"},
    )
    monkeypatch.setattr(analysis, "get_db_collections", lambda: {})

    out = asyncio.run(
        analysis.analyze_watchlist_opportunities(
            current_user={"id": "u1", "service_level": "free"}
        )
    )
    assert out["success"] is False
    assert "blocked by matrix" in out["error"]


@pytest.mark.unit
def test_watchlist_opportunities_success_contains_quota_remaining(monkeypatch):
    monkeypatch.setattr(analysis, "QuotaManager", _DummyQuotaManager)
    monkeypatch.setattr(
        analysis,
        "resolve_action_entitlement",
        lambda **_kwargs: {"type": "inherit"},
    )
    monkeypatch.setattr(
        analysis,
        "_enforce_entitlement_and_quota",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(analysis, "get_db_collections", lambda: {})
    monkeypatch.setattr(analysis, "_fetch_user_watchlist", lambda *_args, **_kwargs: [{"symbol": "600000"}])
    monkeypatch.setattr(analysis, "_fetch_ladder_data_for_portfolio", lambda: {"date": "20260101"})
    monkeypatch.setattr(analysis, "_submit_portfolio_task", lambda **_kwargs: "task-123")
    monkeypatch.setattr(analysis, "_consume_quota_after_submit", lambda **_kwargs: 7)

    out = asyncio.run(
        analysis.analyze_watchlist_opportunities(
            current_user={"id": "u1", "service_level": "free"}
        )
    )
    assert out["success"] is True
    assert out["task_id"] == "task-123"
    assert out["quota_remaining"] == 7


@pytest.mark.unit
def test_enforce_entitlement_and_quota_json_denied(monkeypatch):
    class _Quota:
        def check_feature_weekly_quota(self, *_args, **_kwargs):
            return False

        def get_remaining_feature_weekly_quota(self, *_args, **_kwargs):
            return 0

    res = analysis._enforce_entitlement_and_quota(
        current_user={"id": "u1", "service_level": "free"},
        entitlement={"type": "matrix", "feature_key": "action.watchlist.opportunities_analysis", "weekly_limit": 1},
        quota_manager=_Quota(),
        feature_label="机会分析",
        on_denied="json",
    )
    assert isinstance(res, dict)
    assert res["success"] is False
    assert "次数已用完" in res["error"]
