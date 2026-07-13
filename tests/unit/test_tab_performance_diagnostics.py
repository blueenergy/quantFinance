"""Unit tests for tab performance diagnostics helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(_TOOLS.parent))

from tools import tab_performance_diagnostics as diag  # noqa: E402


@pytest.mark.unit
def test_extract_execution_stats_from_find_plan():
    plan = {
        "executionStats": {
            "nReturned": 50,
            "totalDocsExamined": 55,
            "executionTimeMillis": 3,
        },
        "queryPlanner": {"winningPlan": {"stage": "FETCH"}},
    }
    stats = diag._extract_execution_stats(plan)
    assert stats["nReturned"] == 50
    assert diag._extract_stage(plan) == "FETCH"


@pytest.mark.unit
def test_extract_execution_stats_from_aggregate_plan():
    plan = {
        "stages": [
            {
                "$cursor": {
                    "executionStats": {
                        "nReturned": 100,
                        "totalDocsExamined": 120,
                        "executionTimeMillis": 8,
                    },
                    "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
                }
            }
        ]
    }
    stats = diag._extract_execution_stats(plan)
    assert stats["totalDocsExamined"] == 120
    assert diag._extract_stage(plan) == "IXSCAN"


@pytest.mark.unit
def test_format_ratio_handles_none():
    assert diag._format_ratio(None, 10) == "n/a"
    assert diag._format_ratio(100, 50) == "2.0"


@pytest.mark.unit
def test_aggregate_stats_supplements_match_find_explain():
    coll = MagicMock()
    coll.aggregate.side_effect = Exception("no aggregate explain")
    coll.name = "entropy_scores"
    coll.database.command.return_value = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "FETCH"}}}}]
    }
    coll.find.return_value.explain.return_value = {
        "executionStats": {"nReturned": 100, "totalDocsExamined": 150, "executionTimeMillis": 2},
        "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
    }

    pipeline = [
        {"$match": {"score_date": "20260101", "index_codes": "csi1000"}},
        {"$sort": {"final_entropy_score": 1}},
        {"$limit": 100},
    ]
    row = diag._aggregate_stats(coll, pipeline, "entropy_rankings")

    assert row["nReturned"] == 100
    assert row["totalDocsExamined"] == 150
    assert row["stage"] == "FETCH"


@pytest.mark.unit
def test_explain_aggregate_falls_back_to_database_command():
    from pymongo.errors import ConfigurationError

    coll = MagicMock()
    coll.aggregate.side_effect = ConfigurationError(
        "The explain option is not supported. Use Database.command instead."
    )
    coll.name = "entropy_scores"
    coll.database.command.return_value = {
        "stages": [
            {
                "$cursor": {
                    "executionStats": {"nReturned": 1, "totalDocsExamined": 2},
                }
            }
        ]
    }

    plan = diag._explain_aggregate(coll, [{"$match": {"score_date": "20260101"}}])

    coll.database.command.assert_called_once()
    assert diag._extract_execution_stats(plan)["totalDocsExamined"] == 2
