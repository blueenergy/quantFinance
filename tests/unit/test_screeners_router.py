"""REST and MCP screening router contract tests."""

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def screening_client(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_INTERNAL_KEY", "test-internal-key")
    from main import app

    return TestClient(app)


_HEADERS = {"X-Hermes-Quant-Key": "test-internal-key"}


def test_screeners_schema_requires_key(screening_client):
    r = screening_client.get("/api/screeners/schema")
    assert r.status_code == 401


def test_screeners_schema_ok(screening_client):
    r = screening_client.get("/api/screeners/schema", headers=_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == "1.0"
    names = {m["name"] for m in body["metrics"]}
    assert "range_amplitude" in names
    assert "revenue" in names


def test_screeners_presets_ok(screening_client):
    r = screening_client.get("/api/screeners/presets", headers=_HEADERS)
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["presets"]}
    assert "tech_hardware_v1" in ids


def test_run_stock_screen_rejects_bad_spec(screening_client):
    r = screening_client.post(
        "/api/screeners/run",
        headers=_HEADERS,
        json={
            "universe": {"type": "manual", "value": ["688001.SH"]},
            "filters": [{"field": "not_a_metric", "op": "gt", "value": 1}],
        },
    )
    assert r.status_code == 422


def test_agent_screening_schema_mcp_path(screening_client):
    r = screening_client.get("/api/agent-tools/screening/schema", headers=_HEADERS)
    assert r.status_code == 200


def test_quant_client_key_alias(screening_client):
    r = screening_client.get(
        "/api/screeners/presets",
        headers={"X-Quant-Client-Key": "test-internal-key"},
    )
    assert r.status_code == 200
