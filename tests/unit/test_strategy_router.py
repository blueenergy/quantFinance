"""Unit tests for strategy metadata endpoints."""
import pytest

from main import app
from auth import get_current_user


@pytest.mark.unit
def test_list_strategies_fallback_respects_vip_user(client, monkeypatch):
    """Fallback strategy metadata must still use the authenticated user tier."""
    from routers import strategy

    client.app.dependency_overrides[get_current_user] = lambda: {
        "id": "vip-user",
        "username": "vip",
        "service_level": "vip",
    }
    monkeypatch.setattr(strategy, "get_db_collections", lambda: {})

    response = client.get("/api/strategy/strategies")

    assert response.status_code == 200
    body = response.json()
    by_key = {item["key"]: item for item in body["strategies"]}
    assert by_key["hidden_dragon"]["can_use"] is True
    assert by_key["single_yang"]["can_use"] is True


@pytest.mark.unit
def test_list_strategies_empty_collection_fallback_respects_vip_user(client, monkeypatch):
    """Empty strategy_metadata collections should not downgrade VIP users to free."""
    from routers import strategy

    class EmptyCollection:
        def find(self, *_args, **_kwargs):
            return []

    client.app.dependency_overrides[get_current_user] = lambda: {
        "id": "vip-user",
        "username": "vip",
        "service_level": "vip",
    }
    monkeypatch.setattr(strategy, "get_db_collections", lambda: {"strategy_metadata": EmptyCollection()})

    response = client.get("/api/strategy/strategies")

    assert response.status_code == 200
    by_key = {item["key"]: item for item in response.json()["strategies"]}
    assert by_key["hidden_dragon"]["can_use"] is True
