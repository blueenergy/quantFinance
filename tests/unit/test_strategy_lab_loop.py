"""Unit tests for Strategy Lab auto-iteration loop service."""

from datetime import datetime

import pytest

from services import strategy_lab_loop as loop_svc


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc.copy()
        return None

    def find(self, query=None):
        class Cursor(list):
            def sort(self, *args, **kwargs):
                return self

            def limit(self, n):
                return Cursor(self[:n])

        if not query:
            return Cursor([d.copy() for d in self.docs])
        out = []
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                out.append(doc.copy())
        return Cursor(out)

    def insert_one(self, doc):
        doc = doc.copy()
        doc.setdefault("created_at", datetime.now())
        self.docs.append(doc)

        class R:
            inserted_id = "fake_id"

        return R()

    def insert_many(self, docs):
        docs = [doc.copy() for doc in docs]
        self.docs.extend(docs)

        class R:
            inserted_ids = list(range(len(docs)))

        return R()

    def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                if "$set" in update:
                    doc.update(update["$set"])
                if "$push" in update:
                    key = update["$push"]
                    if isinstance(key, dict):
                        for field, val in key.items():
                            doc.setdefault(field, [])
                            doc[field].append(val)
                    else:
                        doc.setdefault(key, [])
                        doc[key].append(update["$push"])
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    def update_many(self, query, update):
        count = 0
        for doc in self.docs:
            ok = True
            for key, value in query.items():
                if isinstance(value, dict) and "$in" in value:
                    ok = doc.get(key) in value["$in"]
                else:
                    ok = doc.get(key) == value
                if not ok:
                    break
            if ok:
                if "$set" in update:
                    doc.update(update["$set"])
                count += 1
        return type("R", (), {"modified_count": count})()

    def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                del self.docs[i]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()


def test_normalize_suggestion_params_strategy_params():
    item = {"name": "foo", "strategy_params": {"period": 30}}
    assert loop_svc.normalize_suggestion_params(item) == {"period": 30}


def test_normalize_suggestion_params_suggested_values():
    item = {"name": "period", "suggested_values": [25, 30]}
    assert loop_svc.normalize_suggestion_params(item) == {"period": 25}


def test_normalize_suggestion_params_rejects_combo_label_names():
    item = {
        "name": "entry_ma_period + ma_proximity_pct",
        "suggested_values": [20, 0.03],
    }
    assert loop_svc.normalize_suggestion_params(item) == {}


def test_normalize_suggestion_params_filters_invalid_keys_in_dict():
    item = {
        "strategy_params": {
            "entry_ma_period": 20,
            "entry_ma_period + ma_proximity_pct": 0.03,
        }
    }
    assert loop_svc.normalize_suggestion_params(item) == {"entry_ma_period": 20}


def test_normalize_suggestion_params_filters_batch_metadata_keys():
    item = {
        "strategy_params": {
            "entry_ma_period": 20,
            "universe_value": "csi1000",
        }
    }
    assert loop_svc.normalize_suggestion_params(item) == {"entry_ma_period": 20}


def test_merge_strategy_params():
    assert loop_svc.merge_strategy_params({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert loop_svc.merge_strategy_params({"a": 1}, {"a": 9}) == {"a": 9}


def test_evaluate_stop_rules_max_iterations():
    stop, reason = loop_svc.evaluate_stop_rules(
        stop_rules=loop_svc.default_stop_rules(),
        current_iteration=3,
        max_iterations=3,
        summary={"total": 10, "completed": 10, "median_total_return": 0.1},
        previous_summaries=[],
    )
    assert stop is True
    assert "最大迭代" in reason


def test_final_iteration_can_be_reviewed_before_max_stop(monkeypatch):
    loop = {
        "loop_id": "sl_final_review",
        "user_id": "u1",
        "status": loop_svc.LOOP_STATUS_RUNNING,
        "current_step": loop_svc.STEP_WAIT_REVIEW,
        "current_iteration": 3,
        "max_iterations": 3,
        "current_batch_id": "bb_final",
        "stop_rules": {"min_completion_rate": 0.0, "min_improvement": 0, "no_improvement_rounds": 2},
        "review_options": {},
        "iterations": [
            {
                "iteration": 3,
                "batch_id": "bb_final",
                "batch_summary": {"total": 1, "completed": 1},
                "review_task_id": "task_final",
            }
        ],
    }
    loops = FakeCollection([loop])
    tasks = FakeCollection(
        [
            {
                "_id": __import__("bson").ObjectId("507f1f77bcf86cd799439012"),
                "status": "completed",
                "task_type": "backtest_batch_review",
                "params": {"batch_id": "bb_final"},
            }
        ]
    )

    def fake_collections():
        return {
            "strategy_lab_loops": loops,
            "backtest_batches": FakeCollection([{"batch_id": "bb_final", "user_id": "u1"}]),
            "backtest_tasks": FakeCollection([
                {"batch_id": "bb_final", "user_id": "u1", "task_id": "bt_final", "status": "completed"}
            ]),
            "backtest_results": FakeCollection([]),
            "analysis_tasks": tasks,
        }

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    monkeypatch.setattr(loop_svc, "get_analyzer_db", lambda: {
        "backtest_batch_reviews": FakeCollection([
            {
                "batch_id": "bb_final",
                "review_id": "review_final",
                "review": {"summary": "final reviewed", "next_experiments": []},
            }
        ])
    })
    monkeypatch.setattr(loop_svc, "queue_loop_final_report_sync", lambda *args, **kwargs: "final_report_task")

    result = loop_svc.advance_loop("sl_final_review")
    assert result["action"] == "completed"
    assert loops.docs[0]["status"] == loop_svc.LOOP_STATUS_COMPLETED
    assert loops.docs[0]["iterations"][0]["review_id"] is not None
    assert loops.docs[0]["iterations"][0]["decision"] == "stop"


def test_final_iteration_batch_completion_queues_review_before_stop(monkeypatch):
    loop = {
        "loop_id": "sl_final_batch",
        "user_id": "u1",
        "status": loop_svc.LOOP_STATUS_RUNNING,
        "current_step": loop_svc.STEP_WAIT_BATCH,
        "current_iteration": 3,
        "max_iterations": 3,
        "current_batch_id": "bb_final",
        "stop_rules": {"min_completion_rate": 0.0},
        "review_options": {},
        "iterations": [
            {"iteration": 3, "batch_id": "bb_final", "batch_summary": {"status": "pending"}}
        ],
    }
    loops = FakeCollection([loop])
    tasks = FakeCollection([
        {"batch_id": "bb_final", "user_id": "u1", "task_id": "bt_final", "status": "completed"}
    ])
    analysis_tasks = FakeCollection()

    monkeypatch.setattr(loop_svc, "get_db_collections", lambda: {
        "strategy_lab_loops": loops,
        "backtest_batches": FakeCollection([{"batch_id": "bb_final", "user_id": "u1"}]),
        "backtest_tasks": tasks,
        "backtest_results": FakeCollection([]),
        "analysis_tasks": analysis_tasks,
    })

    result = loop_svc.advance_loop("sl_final_batch")
    assert result["action"] == "queued_review"
    assert loops.docs[0]["current_step"] == loop_svc.STEP_WAIT_REVIEW
    assert loops.docs[0]["status"] == loop_svc.LOOP_STATUS_RUNNING


def test_review_task_unknown_status_waits_instead_of_failing(monkeypatch):
    loop = {
        "loop_id": "sl_wait_review",
        "user_id": "u1",
        "status": loop_svc.LOOP_STATUS_RUNNING,
        "current_step": loop_svc.STEP_WAIT_REVIEW,
        "current_iteration": 1,
        "max_iterations": 3,
        "current_batch_id": "bb_review",
        "stop_rules": {"min_completion_rate": 0.0},
        "review_options": {},
        "iterations": [
            {"iteration": 1, "batch_id": "bb_review", "review_task_id": "task_unknown"}
        ],
    }
    loops = FakeCollection([loop])

    monkeypatch.setattr(loop_svc, "get_db_collections", lambda: {
        "strategy_lab_loops": loops,
        "backtest_batches": FakeCollection([{"batch_id": "bb_review", "user_id": "u1"}]),
        "backtest_tasks": FakeCollection([
            {"batch_id": "bb_review", "user_id": "u1", "task_id": "bt_review", "status": "completed"}
        ]),
        "backtest_results": FakeCollection([]),
        "analysis_tasks": FakeCollection(),
    })
    monkeypatch.setattr(loop_svc, "_find_review_task", lambda *args, **kwargs: {"status": "claimed"})

    result = loop_svc.advance_loop("sl_wait_review")

    assert result["action"] == "waiting_review"
    assert result["task_status"] == "claimed"
    assert loops.docs[0]["status"] == loop_svc.LOOP_STATUS_RUNNING


def test_evaluate_stop_rules_low_completion():
    stop, reason = loop_svc.evaluate_stop_rules(
        stop_rules={"min_completion_rate": 0.8, "min_improvement": 0, "no_improvement_rounds": 2},
        current_iteration=1,
        max_iterations=5,
        summary={"total": 10, "completed": 2},
        previous_summaries=[],
    )
    assert stop is True
    assert "完成率" in reason


def test_pick_next_experiment():
    review = {
        "next_experiments": [
            {"name": "empty", "strategy_params": {}},
            {"name": "tune", "strategy_params": {"period": 40}},
        ]
    }
    item, params = loop_svc._pick_next_experiment(review)
    assert item["name"] == "tune"
    assert params == {"period": 40}


def test_create_loop_from_source_batch(monkeypatch):
    batches = FakeCollection(
        [
            {
                "batch_id": "bb_test",
                "user_id": "u1",
                "strategy_key": "turtle",
                "strategy_params": {"period": 20},
                "start_date": "20240101",
                "end_date": "20241231",
                "asset_type": "stock",
            }
        ]
    )
    loops = FakeCollection()

    def fake_collections():
        return {
            "backtest_batches": batches,
            "strategy_lab_loops": loops,
            "backtest_tasks": FakeCollection(),
            "backtest_results": FakeCollection(),
        }

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    doc = loop_svc.create_loop(user_id="u1", source_batch_id="bb_test", max_iterations=2)
    assert doc["loop_id"].startswith("sl_")
    assert doc["status"] == "running"
    assert doc["current_batch_id"] == "bb_test"
    assert batches.docs[0]["loop_id"] == doc["loop_id"]


def test_create_loop_rejects_source_batch_used_by_active_loop(monkeypatch):
    batches = FakeCollection(
        [
            {
                "batch_id": "bb_test",
                "user_id": "u1",
                "strategy_key": "turtle",
                "strategy_params": {"period": 20},
                "start_date": "20240101",
                "end_date": "20241231",
                "asset_type": "stock",
            }
        ]
    )
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_active",
                "user_id": "u1",
                "status": loop_svc.LOOP_STATUS_RUNNING,
                "batch_chain": ["bb_test"],
            }
        ]
    )

    monkeypatch.setattr(loop_svc, "get_db_collections", lambda: {
        "backtest_batches": batches,
        "strategy_lab_loops": loops,
        "backtest_tasks": FakeCollection(),
        "backtest_results": FakeCollection(),
    })

    with pytest.raises(ValueError, match="already used by active loop"):
        loop_svc.create_loop(user_id="u1", source_batch_id="bb_test", max_iterations=2)


def test_create_loop_keeps_structured_batch_name_separate_from_loop_name(monkeypatch):
    batches = FakeCollection()
    loops = FakeCollection()
    tasks = FakeCollection()

    monkeypatch.setattr(loop_svc, "get_db_collections", lambda: {
        "backtest_batches": batches,
        "strategy_lab_loops": loops,
        "backtest_tasks": tasks,
        "backtest_results": FakeCollection(),
    })

    doc = loop_svc.create_loop(
        user_id="u1",
        name="turtle_short_entry_csi1000 自动迭代",
        batch_config={
            "name": "Strategy Lab loop 自动迭代",
            "symbols": ["000001.SZ"],
            "asset_type": "stock",
            "universe_type": "manual",
            "universe_value": "csi1000",
            "strategy_key": "turtle",
            "preset": "turtle_short_entry",
            "strategy_params": {"period": 20},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000,
        },
        max_iterations=2,
    )

    assert doc["name"] == "turtle_short_entry_csi1000 自动迭代"
    assert batches.docs[0]["name"] == "turtle_short_entry_csi1000"


def test_structured_name_prefers_machine_label_and_source_universe():
    batch = {
        "name": "turtle_conservative_csi1000",
        "strategy_key": "turtle",
        "preset": "turtle_conservative",
        "universe_type": "manual",
        "universe_value": "rerun:bb_20260526_090222_61199afe",
        "source_universe_value": "csi1000",
    }

    assert (
        loop_svc.build_structured_batch_name(batch, {"label": "turtle_short_entry"})
        == "turtle_short_entry_csi1000"
    )
    assert (
        loop_svc.build_structured_batch_name(batch, {"label": "扩大止盈止损区间实验"})
        == "turtle_conservative_csi1000"
    )


def test_rerun_preserves_source_universe_for_future_names():
    batches = FakeCollection([
        {
            "batch_id": "bb_parent",
            "user_id": "u1",
            "name": "turtle_short_entry_csi1000",
            "asset_type": "stock",
            "universe_type": "manual",
            "universe_value": "rerun:bb_grandparent",
            "source_universe_type": "index",
            "source_universe_value": "csi1000",
            "strategy_key": "turtle",
            "preset": "turtle_conservative",
            "strategy_params": {"period": 20},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000,
        }
    ])
    tasks = FakeCollection([
        {
            "task_id": "bt_1",
            "batch_id": "bb_parent",
            "user_id": "u1",
            "symbol": "000001.SZ",
            "batch_index": 0,
            "status": "completed",
        }
    ])
    collections = {
        "backtest_batches": batches,
        "backtest_tasks": tasks,
        "backtest_results": FakeCollection(),
    }

    result = loop_svc.rerun_backtest_batch_sync(
        collections,
        {"id": "u1"},
        "bb_parent",
        loop_svc.BacktestBatchRerunCreate(name="turtle_short_exit_csi1000"),
    )
    child = batches.find_one({"batch_id": result["batch_id"], "user_id": "u1"})

    assert child["name"] == "turtle_short_exit_csi1000"
    assert child["universe_value"] == "csi1000"
    assert child["source_universe_value"] == "csi1000"
    assert child["source_universe_type"] == "index"


def test_build_loop_summary_report_dedupes_batches(monkeypatch):
    batches = FakeCollection(
        [
            {
                "batch_id": "bb_a",
                "user_id": "u1",
                "strategy_key": "turtle",
                "strategy_params": {"period": 20},
                "start_date": "20240101",
                "end_date": "20241231",
                "asset_type": "stock",
            }
        ]
    )
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_test",
                "user_id": "u1",
                "status": "completed",
                "max_iterations": 3,
                "current_iteration": 3,
                "stop_reason": "达到最大迭代次数 3",
                "batch_chain": ["bb_a"],
                "final_summary": {"median_total_return": 0.05},
                "iterations": [
                    {
                        "iteration": 1,
                        "batch_id": "bb_a",
                        "batch_summary": {"total": 2, "completed": 2, "median_total_return": 0.01},
                        "decision": "continue",
                    },
                    {
                        "iteration": 1,
                        "batch_id": "bb_a",
                        "batch_summary": {"total": 2, "completed": 2, "median_total_return": 0.01},
                    },
                ],
            }
        ]
    )
    tasks = FakeCollection(
        [
            {"batch_id": "bb_a", "user_id": "u1", "task_id": "t1", "status": "completed"},
            {"batch_id": "bb_a", "user_id": "u1", "task_id": "t2", "status": "completed"},
        ]
    )
    results = FakeCollection(
        [
            {"task_id": "t1", "metrics": {"total_return": 0.01}},
            {"task_id": "t2", "metrics": {"total_return": 0.09}},
        ]
    )

    def fake_collections():
        return {
            "backtest_batches": batches,
            "strategy_lab_loops": loops,
            "backtest_tasks": tasks,
            "backtest_results": results,
        }

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    monkeypatch.setattr(loop_svc, "get_analyzer_db", lambda: {"backtest_batch_reviews": FakeCollection([])})

    report = loop_svc.build_loop_summary_report("sl_test", "u1")
    assert report is not None
    assert len(report["iterations"]) == 1
    assert report["iterations"][0]["batch_id"] == "bb_a"
    assert report["is_terminal"] is True


def test_build_loop_summary_report_uses_batch_chain_when_iteration_missing(monkeypatch):
    batches = FakeCollection(
        [
            {"batch_id": "bb_1", "user_id": "u1", "strategy_key": "turtle", "name": "turtle_initial"},
            {"batch_id": "bb_2", "user_id": "u1", "strategy_key": "turtle", "name": "turtle_long_entry_csi1000"},
            {"batch_id": "bb_3", "user_id": "u1", "strategy_key": "turtle", "name": "turtle_short_entry_csi1000"},
        ]
    )
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_chain",
                "user_id": "u1",
                "status": "completed",
                "max_iterations": 3,
                "current_iteration": 3,
                "current_step": "completed",
                "batch_chain": ["bb_1", "bb_2", "bb_3"],
                "iterations": [
                    {"iteration": 1, "batch_id": "bb_1", "batch_summary": {"status": "completed"}},
                    {"iteration": 2, "batch_id": "bb_2", "batch_summary": {"status": "completed"}},
                ],
            }
        ]
    )

    def fake_collections():
        return {
            "backtest_batches": batches,
            "strategy_lab_loops": loops,
            "backtest_tasks": FakeCollection(),
            "backtest_results": FakeCollection(),
        }

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    monkeypatch.setattr(loop_svc, "get_analyzer_db", lambda: {"backtest_batch_reviews": FakeCollection([])})

    report = loop_svc.build_loop_summary_report("sl_chain", "u1")
    assert [row["batch_id"] for row in report["iterations"]] == ["bb_1", "bb_2", "bb_3"]
    assert [row["batch_name"] for row in report["iterations"]] == [
        "turtle_initial",
        "turtle_long_entry_csi1000",
        "turtle_short_entry_csi1000",
    ]
    assert report["iterations"][2]["iteration"] == 3


def test_queue_loop_final_report_creates_new_when_completed_but_empty(monkeypatch):
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_empty",
                "user_id": "u1",
                "batch_chain": ["bb_a"],
                "final_report_status": "completed",
                "final_report": {},
                "final_report_task_id": "old_task",
                "review_options": {"objective": "test"},
            }
        ]
    )
    tasks = FakeCollection()

    def fake_collections():
        return {"strategy_lab_loops": loops, "analysis_tasks": tasks}

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    tid = loop_svc.queue_loop_final_report_sync(
        fake_collections(), {"id": "u1"}, "sl_empty", force=False
    )
    assert tid != "old_task"
    assert len(tasks.docs) == 1


def test_queue_loop_final_report_skips_when_pending(monkeypatch):
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_pend",
                "user_id": "u1",
                "batch_chain": ["bb_a"],
                "final_report_status": "pending",
                "final_report_task_id": "task_existing",
                "review_options": {"objective": "test"},
            }
        ]
    )
    tasks = FakeCollection()

    def fake_collections():
        return {
            "strategy_lab_loops": loops,
            "analysis_tasks": tasks,
        }

    monkeypatch.setattr(loop_svc, "get_db_collections", fake_collections)
    tid = loop_svc.queue_loop_final_report_sync(
        fake_collections(), {"id": "u1"}, "sl_pend", force=False
    )
    assert tid == "task_existing"
    assert len(tasks.docs) == 0


def test_finish_loop_queues_final_report_on_completed(monkeypatch):
    loops = FakeCollection(
        [{"loop_id": "sl_done", "user_id": "u1", "batch_chain": ["bb_a"]}]
    )
    tasks = FakeCollection()
    queued = []

    def fake_queue(collections, user, loop_id, **kwargs):
        queued.append(loop_id)
        return "task_new"

    monkeypatch.setattr(loop_svc, "get_db_collections", lambda: {
        "strategy_lab_loops": loops,
        "analysis_tasks": tasks,
    })
    monkeypatch.setattr(loop_svc, "queue_loop_final_report_sync", fake_queue)

    loop_svc._finish_loop(
        loops,
        {"loop_id": "sl_done", "user_id": "u1"},
        status=loop_svc.LOOP_STATUS_COMPLETED,
        step=loop_svc.STEP_COMPLETED,
        stop_reason="达到最大迭代次数 3",
        final_summary={"median_total_return": 0.1},
    )
    assert queued == ["sl_done"]


def test_resolve_loop_final_report_backfills_from_task_result(monkeypatch):
    monkeypatch.setattr(
        loop_svc,
        "get_analyzer_db",
        lambda: {"strategy_lab_loop_reports": FakeCollection([])},
    )
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_bf",
                "user_id": "u1",
                "final_report_status": "pending",
                "final_report_task_id": "507f1f77bcf86cd799439011",
            }
        ]
    )
    from bson import ObjectId

    task_oid = ObjectId("507f1f77bcf86cd799439011")
    tasks = FakeCollection(
        [
            {
                "_id": task_oid,
                "status": "completed",
                "result": {
                    "report": {
                        "summary": "三轮后推荐 period=30",
                        "main_findings": ["收益改善"],
                    }
                },
            }
        ]
    )

    resolved = loop_svc.resolve_loop_final_report(
        {"strategy_lab_loops": loops, "analysis_tasks": tasks},
        loops.docs[0],
        persist=True,
    )
    assert resolved["final_report"]["summary"] == "三轮后推荐 period=30"
    assert loops.docs[0]["final_report_status"] == "completed"


def test_delete_loop_only_allows_terminal_and_detaches_batches(monkeypatch):
    loops = FakeCollection(
        [
            {
                "loop_id": "sl_old",
                "user_id": "u1",
                "status": "completed",
                "batch_chain": ["bb_a", "bb_b"],
            }
        ]
    )
    batches = FakeCollection(
        [
            {"batch_id": "bb_a", "user_id": "u1", "loop_id": "sl_old"},
            {"batch_id": "bb_b", "user_id": "u1", "loop_id": "sl_old"},
        ]
    )
    monkeypatch.setattr(
        loop_svc,
        "get_db_collections",
        lambda: {"strategy_lab_loops": loops, "backtest_batches": batches},
    )

    result = loop_svc.delete_loop("sl_old", "u1")

    assert result["deleted_loops"] == 1
    assert result["detached_batches"] == 2
    assert loops.docs == []
    assert all(batch["loop_id"] is None for batch in batches.docs)


def test_delete_loop_rejects_active_loop(monkeypatch):
    loops = FakeCollection(
        [{"loop_id": "sl_active", "user_id": "u1", "status": "running"}]
    )
    monkeypatch.setattr(
        loop_svc,
        "get_db_collections",
        lambda: {"strategy_lab_loops": loops, "backtest_batches": FakeCollection()},
    )

    with pytest.raises(ValueError, match="Only completed"):
        loop_svc.delete_loop("sl_active", "u1")


def test_set_loop_status_rejects_terminal_transition(monkeypatch):
    loops = FakeCollection(
        [{"loop_id": "sl_done", "user_id": "u1", "status": loop_svc.LOOP_STATUS_COMPLETED}]
    )
    monkeypatch.setattr(
        loop_svc,
        "get_db_collections",
        lambda: {"strategy_lab_loops": loops},
    )

    with pytest.raises(ValueError, match="Invalid loop status transition"):
        loop_svc.set_loop_status("sl_done", "u1", loop_svc.LOOP_STATUS_RUNNING)
    assert loops.docs[0]["status"] == loop_svc.LOOP_STATUS_COMPLETED


def test_set_loop_status_cancel_sets_reason_and_completed_at(monkeypatch):
    loops = FakeCollection(
        [{"loop_id": "sl_running", "user_id": "u1", "status": loop_svc.LOOP_STATUS_RUNNING}]
    )
    monkeypatch.setattr(
        loop_svc,
        "get_db_collections",
        lambda: {"strategy_lab_loops": loops},
    )

    assert loop_svc.set_loop_status("sl_running", "u1", loop_svc.LOOP_STATUS_CANCELLED)
    assert loops.docs[0]["status"] == loop_svc.LOOP_STATUS_CANCELLED
    assert loops.docs[0]["stop_reason"] == "用户取消"
    assert loops.docs[0]["completed_at"] is not None
