import pytest

from datetime import datetime
from types import SimpleNamespace

from services.analysis_tasks import (
    build_task_doc,
    can_access_task,
    find_or_create_deep_analysis_task,
    infer_task_data_date,
    infer_task_type,
    normalize_task_status,
    summarize_task_result,
)


@pytest.mark.unit
def test_build_task_doc_includes_normalized_owner_fields():
    doc = build_task_doc(
        current_user={"id": "u1", "username": "alice"},
        task_type="deep_analysis",
        priority=2,
        symbol="600000",
        params={"k": "v"},
        feedback="improve reasons",
    )
    assert doc["type"] == "deep_analysis"
    assert doc["task_type"] == "deep_analysis"
    assert doc["user_id"] == "u1"
    assert doc["created_by_user_id"] == "u1"
    assert doc["created_by_username"] == "alice"
    assert doc["feedback"] == "improve reasons"
    assert doc["params"]["k"] == "v"
    assert doc["symbol"] == "600000.SH"


class _FakeTasks:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = 0

    @staticmethod
    def _match(doc, query):
        for key, expected in query.items():
            value = (doc.get("params") or {}).get(key.split(".", 1)[1]) if key.startswith("params.") else doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if value not in expected["$in"]:
                    return False
            elif isinstance(expected, dict) and "$gte" in expected:
                if not value or value < expected["$gte"]:
                    return False
            elif value != expected:
                return False
        return True

    def find_one(self, query, sort=None):
        for doc in self.docs:
            if self._match(doc, query):
                return doc
        return None

    def insert_one(self, doc):
        self.inserted += 1
        doc["_id"] = f"id{self.inserted}"
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])


def _deep_task(status, mode="classic", symbol="300196.SZ", day=None):
    return {
        "type": "deep_analysis",
        "symbol": symbol,
        "status": status,
        "params": {"analysis_mode": mode, "date": day or datetime.now().strftime("%Y%m%d")},
        "created_at": datetime.now(),
        "completed_at": datetime.now() if status == "completed" else None,
    }


@pytest.mark.unit
def test_find_or_create_reuses_completed_task():
    tasks = _FakeTasks([_deep_task("completed")])
    doc, reused = find_or_create_deep_analysis_task(
        {"analysis_tasks": tasks},
        current_user={"id": "u1", "username": "alice"},
        symbol="300196",
        analysis_mode="classic",
    )
    assert reused is True
    assert tasks.inserted == 0


@pytest.mark.unit
def test_find_or_create_force_creates_new_task():
    tasks = _FakeTasks([_deep_task("completed")])
    doc, reused = find_or_create_deep_analysis_task(
        {"analysis_tasks": tasks},
        current_user={"id": "u1", "username": "alice"},
        symbol="300196",
        analysis_mode="classic",
        force=True,
    )
    assert reused is False
    assert tasks.inserted == 1


@pytest.mark.unit
def test_failed_task_does_not_block_a_new_run():
    tasks = _FakeTasks([_deep_task("failed")])
    doc, reused = find_or_create_deep_analysis_task(
        {"analysis_tasks": tasks},
        current_user={"id": "u1", "username": "alice"},
        symbol="300196",
        analysis_mode="classic",
    )
    assert reused is False
    assert tasks.inserted == 1
    assert doc["symbol"] == "300196.SZ"


@pytest.mark.unit
def test_can_access_task_accepts_id_or_username_match():
    task = {"created_by_user_id": "u1", "created_by_username": "alice", "user_id": "u1"}
    assert can_access_task(task, {"id": "u1", "username": "alice"}) is True
    assert can_access_task(task, {"id": "u2", "username": "alice"}) is True
    assert can_access_task(task, {"id": "u2", "username": "bob"}) is False


@pytest.mark.unit
def test_task_type_status_and_summary_helpers():
    assert infer_task_type({"task_type": "hot_stock_analysis"}) == "hot_stock_analysis"
    assert infer_task_type({"type": "portfolio_analysis"}) == "portfolio_analysis"
    assert infer_task_type({}) == "deep_analysis"

    assert normalize_task_status("TaskStatus.PENDING") == "pending"
    assert normalize_task_status("FAILED") == "failed"

    summary = summarize_task_result({"status": "completed", "result": {"analysis": "abcde"}})
    assert "abcde" in summary
    fail_summary = summarize_task_result({"status": "failed", "error": "oops"})
    assert fail_summary == "oops"

    parse_summary = summarize_task_result(
        {"status": "completed_with_parse_error", "error": "bad json"}
    )
    assert parse_summary == "bad json"


@pytest.mark.unit
def test_infer_task_data_date_market_prefers_effective_trade_day():
    task = {
        "type": "market_analysis",
        "params": {
            "requested_date": "2026-04-11",
            "effective_analysis_date": "2026-04-10",
            "date": "2026-04-10",
        },
        "result": {"summary": "x"},
    }
    assert infer_task_data_date(task) == "2026-04-10"


@pytest.mark.unit
def test_infer_task_data_date_hot_stock_from_rank_time():
    task = {
        "type": "hot_stock_analysis",
        "params": {"source": "ths"},
        "result": {"rank_time": "2026-04-10 15:00:01", "trade_date": "20260410"},
    }
    assert infer_task_data_date(task) == "2026-04-10"


@pytest.mark.unit
def test_infer_task_data_date_portfolio_from_ladder():
    task = {
        "type": "portfolio_analysis",
        "params": {
            "ladder_data": {"date": "20260410"},
            "analysis_type": "combined",
        },
        "result": {},
    }
    assert infer_task_data_date(task) == "2026-04-10"
