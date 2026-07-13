"""Unit tests for analysis history list projection and detail lookup."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import analysis_history as ah


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def skip(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def __iter__(self):
        return iter(self._docs)


def _heavy_doc():
    return {
        "_id": "abc123",
        "user_id": "user-1",
        "symbol": "600519.SH",
        "stock_name": "贵州茅台",
        "timestamp": "2026-07-01T10:00:00Z",
        "analysis_mode": "classic",
        "provider": "openai",
        "analysis_result": {
            "provider": "openai",
            "analysis_mode": "classic",
            "analysis": {
                "investment_advice": "buy",
                "risk_level": "medium",
                "technical_analysis": "x" * 500,
                "final_conclusion": "看好",
                "module_reports": {"technical": {"content": "huge"}},
                "financial_chart_data": [{"period": "2024", "revenue": 1}],
            },
        },
    }


@patch("services.analysis_history.get_analyzer_db")
def test_list_payload_uses_projection_and_summary(mock_get_db):
    col = MagicMock()
    col.count_documents.return_value = 1
    col.find.return_value = _FakeCursor([_heavy_doc()])
    mock_get_db.return_value = {"analysis_history": col}

    payload = ah.build_user_analysis_history_payload("user-1", page=1, limit=50, include_full=False)

    assert payload["success"] is True
    assert payload["has_more"] is False
    assert payload["total"] == 1
    row = payload["data"][0]
    assert row["symbol"] == "600519.SH"
    assert row["analysis"]["investment_advice"] == "buy"
    assert "module_reports" not in row["analysis"]
    assert row["analysis_result"] is None
    assert len(row["analysis"]["technical_analysis"]) <= ah._TECH_PREVIEW_CHARS

    _, projection = col.find.call_args[0]
    assert projection is ah._LIST_PROJECTION


@patch("services.analysis_history.get_analyzer_db")
def test_list_payload_include_full_returns_body(mock_get_db):
    col = MagicMock()
    col.count_documents.return_value = 1
    col.find.return_value = _FakeCursor([_heavy_doc()])
    mock_get_db.return_value = {"analysis_history": col}

    payload = ah.build_user_analysis_history_payload("user-1", include_full=True)
    row = payload["data"][0]
    assert "module_reports" in row["analysis"]
    assert row["analysis_result"] is not None
    _, projection = col.find.call_args[0]
    assert projection is None


@patch("services.analysis_history.get_analyzer_db")
def test_get_detail_requires_user_scope(mock_get_db):
    col = MagicMock()
    col.find_one.return_value = _heavy_doc()
    mock_get_db.return_value = {"analysis_history": col}

    detail = ah.get_user_analysis_history_detail("user-1", "507f1f77bcf86cd799439011")
    assert detail is not None
    assert detail["data"]["analysis"]["module_reports"]

    col.find_one.assert_called_once()
    query = col.find_one.call_args[0][0]
    assert query["user_id"] == "user-1"


@patch("services.analysis_history.get_analyzer_db")
def test_get_detail_invalid_id_returns_none(mock_get_db):
    mock_get_db.return_value = {"analysis_history": MagicMock()}
    assert ah.get_user_analysis_history_detail("user-1", "not-an-object-id") is None
