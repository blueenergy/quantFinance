import asyncio
from datetime import datetime

import pytest

from routers import portfolio_research


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def create_index(self, *args, **kwargs):
        return True

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def delete_one(self, query=None):
        query = query or {}
        for idx, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[idx]
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()

    def delete_many(self, query=None):
        query = query or {}
        keep = []
        deleted = 0
        for doc in self.docs:
            if self._matches(doc, query):
                deleted += 1
            else:
                keep.append(doc)
        self.docs = keep
        return type("DeleteResult", (), {"deleted_count": deleted})()

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for key, value in update.get("$push", {}).items():
                    doc.setdefault(key, []).append(value)
                return

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, item) for item in expected):
                    return False
                continue
            value = self._value(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected:
                    if isinstance(value, list):
                        if not any(item in expected["$in"] for item in value):
                            return False
                    elif value not in expected["$in"]:
                        return False
                    continue
                if any(op.startswith("$") for op in expected):
                    continue
                if value != expected:
                    return False
                continue
            # Mirror MongoDB: equality against an array field means membership.
            if isinstance(value, list) and not isinstance(expected, list):
                if expected not in value:
                    return False
                continue
            if value != expected:
                return False
        return True

    def _value(self, doc, key):
        value = doc
        for part in key.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value


def _collections():
    return {
        "portfolio_strategy_configs": _Collection(),
        "portfolio_strategy_param_presets": _Collection(),
        "portfolio_research_jobs": _Collection(),
        "portfolio_research_results": _Collection(),
        "portfolio_research_reviews": _Collection(),
    }


def test_create_portfolio_research_job(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi1000",
                start_date="2025-01-01",
                end_date="2025-01-31",
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    job = collections["portfolio_research_jobs"].docs[0]
    assert job["user_id"] == "user-1"
    assert job["status"] == "pending"
    assert job["params"]["start_date"] == "20250101"
    assert job["params"]["rebalance_interval_days"] == [20]


@pytest.mark.unit
def test_list_portfolio_research_jobs_includes_timing_and_progress_fields(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_research, "get_db_collections", lambda: {"stock_scores": _Collection()})
    started = datetime(2026, 7, 5, 3, 30, 0)
    heartbeat = datetime(2026, 7, 5, 4, 15, 0)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-running",
            "user_id": "user-1",
            "name": "running job",
            "status": "running",
            "created_at": started,
            "started_at": started,
            "heartbeat_at": heartbeat,
            "progress_stage": "loading_scores",
            "progress_message": "loading stock scores",
        }
    )

    response = asyncio.run(
        portfolio_research.list_portfolio_research_jobs(
            status=None,
            universe_index=None,
            growth_cycle_weight=None,
            limit=50,
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    job = response["data"][0]
    assert job["started_at"] == started.isoformat()
    assert job["heartbeat_at"] == heartbeat.isoformat()
    assert job["progress_stage"] == "loading_scores"
    assert job["progress_message"] == "loading stock scores"


@pytest.mark.unit
def test_list_portfolio_research_jobs_filters_by_universe_and_growth_cycle_weight(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_research, "get_db_collections", lambda: {"stock_scores": _Collection()})
    now = datetime(2026, 7, 5, 3, 30, 0)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-csi1000-3070",
            "user_id": "user-1",
            "name": "csi1000 30:70",
            "universe_index": "csi1000",
            "params": {"universe_index": "csi1000", "growth_cycle_weights": ["30:70", "40:60"]},
            "status": "completed",
            "created_at": now,
        }
    )
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-csi500-2080",
            "user_id": "user-1",
            "name": "csi500 20:80",
            "universe_index": "csi500",
            "params": {"universe_index": "csi500", "growth_cycle_weights": ["20:80"]},
            "status": "completed",
            "created_at": now,
        }
    )
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-other-user",
            "user_id": "user-2",
            "name": "other",
            "universe_index": "csi1000",
            "params": {"universe_index": "csi1000", "growth_cycle_weights": ["30:70"]},
            "status": "completed",
            "created_at": now,
        }
    )

    by_universe = asyncio.run(
        portfolio_research.list_portfolio_research_jobs(
            status=None,
            universe_index="csi1000",
            growth_cycle_weight=None,
            limit=50,
            current_user={"id": "user-1"},
        )
    )
    assert [job["job_id"] for job in by_universe["data"]] == ["job-csi1000-3070"]

    by_weight = asyncio.run(
        portfolio_research.list_portfolio_research_jobs(
            status=None,
            universe_index=None,
            growth_cycle_weight="30：70",  # fullwidth colon normalized
            limit=50,
            current_user={"id": "user-1"},
        )
    )
    assert [job["job_id"] for job in by_weight["data"]] == ["job-csi1000-3070"]

    by_both = asyncio.run(
        portfolio_research.list_portfolio_research_jobs(
            status="completed",
            universe_index="csi500",
            growth_cycle_weight="20:80",
            limit=50,
            current_user={"id": "user-1"},
        )
    )
    assert [job["job_id"] for job in by_both["data"]] == ["job-csi500-2080"]

    no_match = asyncio.run(
        portfolio_research.list_portfolio_research_jobs(
            status=None,
            universe_index="csi1000",
            growth_cycle_weight="20:80",
            limit=50,
            current_user={"id": "user-1"},
        )
    )
    assert no_match["data"] == []


@pytest.mark.unit
def test_rerun_portfolio_research_job_clones_params_without_overwriting_source(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_research, "get_db_collections", lambda: {"stock_scores": _Collection()})
    source_params = {
        "name": "csi1000 research",
        "universe_index": "csi1000",
        "start_date": "20250101",
        "end_date": "20250131",
        "score_column": "composite_growth_cycle_score",
        "growth_cycle_weights": ["30:70", "40:60"],
        "top_n_values": [10, 20],
        "horizon": 20,
        "rebalance_interval_days": [20],
        "active_caps": [0.2, 0.3],
        "transaction_cost": 0.001,
        "force": False,
    }
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-source",
            "user_id": "user-1",
            "name": "csi1000 research",
            "universe_index": "csi1000",
            "start_date": "20250101",
            "end_date": "20250131",
            "params": source_params,
            "status": "completed",
            "result_id": "result-source",
            "created_at": datetime(2026, 7, 5, 3, 30, 0),
        }
    )

    response = asyncio.run(
        portfolio_research.rerun_portfolio_research_job(
            "job-source",
            request=None,
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    cloned = response["data"]
    assert cloned["job_id"] != "job-source"
    assert cloned["status"] == "pending"
    assert cloned["result_id"] is None
    assert cloned["rerun_of_job_id"] == "job-source"
    assert cloned["name"] == "csi1000 research (rerun)"
    assert cloned["params"]["universe_index"] == "csi1000"
    assert cloned["params"]["growth_cycle_weights"] == ["30:70", "40:60"]
    assert cloned["params"]["top_n_values"] == [10, 20]
    assert cloned["params"]["force"] is True

    source = collections["portfolio_research_jobs"].find_one({"job_id": "job-source"})
    assert source["status"] == "completed"
    assert source["result_id"] == "result-source"
    assert len(collections["portfolio_research_jobs"].docs) == 2


@pytest.mark.unit
def test_rerun_portfolio_research_job_rejects_other_users_job(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-source",
            "user_id": "user-1",
            "name": "owned",
            "universe_index": "csi1000",
            "params": {
                "universe_index": "csi1000",
                "start_date": "20250101",
                "end_date": "20250131",
            },
            "status": "completed",
        }
    )

    with pytest.raises(Exception) as exc:
        asyncio.run(
            portfolio_research.rerun_portfolio_research_job(
                "job-source",
                request=None,
                current_user={"id": "user-2"},
            )
        )
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.unit
def test_rerun_portfolio_research_job_applies_param_overrides(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-source",
            "user_id": "user-1",
            "name": "csi1000 research",
            "universe_index": "csi1000",
            "start_date": "20250101",
            "end_date": "20250131",
            "params": {
                "name": "csi1000 research",
                "universe_index": "csi1000",
                "start_date": "20250101",
                "end_date": "20250131",
                "score_column": "composite_growth_cycle_score",
                "growth_cycle_weights": ["30:70"],
                "top_n_values": [10, 20],
                "horizon": 20,
                "rebalance_interval_days": [20],
                "active_caps": [0.2, 0.3],
                "transaction_cost": 0.001,
                "force": False,
            },
            "status": "completed",
            "result_id": "result-source",
        }
    )

    response = asyncio.run(
        portfolio_research.rerun_portfolio_research_job(
            "job-source",
            request=portfolio_research.RerunResearchJobRequest(
                growth_cycle_weights=["60:40", "40:60"],
                start_date="20240101",
                end_date="20241231",
                name="csi1000 research tuned",
            ),
            current_user={"id": "user-1"},
        )
    )

    cloned = response["data"]
    assert cloned["rerun_of_job_id"] == "job-source"
    assert cloned["name"] == "csi1000 research tuned (rerun)"
    assert cloned["params"]["growth_cycle_weights"] == ["60:40", "40:60"]
    assert cloned["params"]["start_date"] == "20240101"
    assert cloned["params"]["end_date"] == "20241231"
    assert cloned["params"]["universe_index"] == "csi1000"
    assert cloned["params"]["top_n_values"] == [10, 20]
    assert cloned["params"]["force"] is True
    source = collections["portfolio_research_jobs"].find_one({"job_id": "job-source"})
    assert source["params"]["growth_cycle_weights"] == ["30:70"]


@pytest.mark.unit
def test_delete_portfolio_research_job_cascades_and_keeps_preset(monkeypatch, tmp_path):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(tmp_path))

    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    combos_dir = tmp_path / "report_combos"
    combos_dir.mkdir()
    combo_file = combos_dir / "g30_c70.json"
    html_path.write_text("<html></html>", encoding="utf-8")
    json_path.write_text("{}", encoding="utf-8")
    combo_file.write_text("{}", encoding="utf-8")

    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-del",
            "user_id": "user-1",
            "name": "to delete",
            "status": "completed",
            "result_id": "result-del",
        }
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-del",
            "job_id": "job-del",
            "user_id": "user-1",
            "published_preset_id": "preset-keep",
            "artifact": {
                "html_path": str(html_path),
                "json_path": str(json_path),
                "combos_dir": str(combos_dir),
                "combos": [{"combo_key": "g30_c70", "file": str(combo_file)}],
            },
        }
    )
    collections["portfolio_research_reviews"].insert_one(
        {
            "job_id": "job-del",
            "user_id": "user-1",
            "research_result_id": "result-del",
            "action": "publish",
        }
    )
    collections["portfolio_strategy_param_presets"].insert_one(
        {"preset_id": "preset-keep", "user_id": "user-1", "status": "enabled"}
    )

    response = asyncio.run(
        portfolio_research.delete_portfolio_research_job(
            "job-del",
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert response["data"]["deleted_jobs"] == 1
    assert response["data"]["deleted_results"] == 1
    assert response["data"]["deleted_reviews"] == 1
    assert response["data"]["published_preset_kept"] == "preset-keep"
    assert collections["portfolio_research_jobs"].find_one({"job_id": "job-del"}) is None
    assert collections["portfolio_research_results"].find_one({"result_id": "result-del"}) is None
    assert collections["portfolio_research_reviews"].docs == []
    assert collections["portfolio_strategy_param_presets"].find_one({"preset_id": "preset-keep"}) is not None
    assert not html_path.exists()
    assert not json_path.exists()
    assert not combos_dir.exists()


@pytest.mark.unit
def test_delete_portfolio_research_job_rejects_running(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {"job_id": "job-run", "user_id": "user-1", "status": "running"}
    )

    with pytest.raises(Exception) as exc:
        asyncio.run(
            portfolio_research.delete_portfolio_research_job(
                "job-run",
                current_user={"id": "user-1"},
            )
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.unit
def test_delete_portfolio_research_job_scopes_reviews_by_user(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {"job_id": "job-del", "user_id": "user-1", "status": "completed", "result_id": "result-del"}
    )
    collections["portfolio_research_results"].insert_one(
        {"result_id": "result-del", "job_id": "job-del", "user_id": "user-1"}
    )
    collections["portfolio_research_reviews"].insert_one(
        {"job_id": "job-del", "user_id": "user-1", "research_result_id": "result-del", "action": "mine"}
    )
    collections["portfolio_research_reviews"].insert_one(
        {"job_id": "job-del", "user_id": "user-2", "research_result_id": "result-del", "action": "other"}
    )

    response = asyncio.run(
        portfolio_research.delete_portfolio_research_job(
            "job-del",
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["deleted_reviews"] == 1
    remaining = collections["portfolio_research_reviews"].docs
    assert len(remaining) == 1
    assert remaining[0]["user_id"] == "user-2"


@pytest.mark.unit
def test_delete_artifact_files_refuses_outside_report_root_and_root_itself(monkeypatch, tmp_path):
    report_root = tmp_path / "reports"
    report_root.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("x", encoding="utf-8")
    nested = report_root / "ok.html"
    nested.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(report_root))

    stats_outside = portfolio_research._delete_artifact_files({"html_path": str(outside)})
    assert outside.exists()
    assert stats_outside["removed"] == []
    assert any("refused" in err for err in stats_outside["errors"])

    stats_root = portfolio_research._delete_artifact_files({"combos_dir": str(report_root)})
    assert report_root.exists()
    assert stats_root["removed"] == []
    assert any("refused" in err for err in stats_root["errors"])

    stats_ok = portfolio_research._delete_artifact_files({"html_path": str(nested)})
    assert not nested.exists()
    assert str(nested.resolve()) in stats_ok["removed"] or any(str(nested) in item for item in stats_ok["removed"])


def test_publish_portfolio_research_result_creates_parameter_preset(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {"job_id": "job-1", "user_id": "user-1", "status": "completed", "created_at": datetime.utcnow()}
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "best_row": {"risk_adjusted_score": 1.2},
            "data_watermark": {"stock_scores_max_date": "20250131"},
            "candidate_strategy_config": {
                "preset_id": "research_strategy",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "name": "Research Strategy",
                "status": "draft",
                "top_n": 10,
                "rebalance_days": 20,
            },
        }
    )

    response = asyncio.run(
        portfolio_research.publish_portfolio_research_result(
            "result-1",
            portfolio_research.PublishResearchResultRequest(status="enabled"),
            current_user={"id": "user-1", "username": "alice"},
        )
    )

    assert response["success"] is True
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": "research_strategy"})
    assert preset["status"] == "enabled"
    assert preset["source"] == "portfolio_research"
    assert preset["research_job_id"] == "job-1"
    assert preset["params"]["top_n"] == 10
    assert collections["portfolio_research_reviews"].docs[0]["decision"] == "preset_published"


def test_publish_portfolio_research_result_rejects_duplicate_preset(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_strategy_param_presets"].insert_one({"preset_id": "research_strategy"})
    collections["portfolio_research_jobs"].insert_one({"job_id": "job-1", "user_id": "user-1", "status": "completed"})
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "candidate_strategy_config": {"preset_id": "research_strategy", "strategy_template_id": "growth_cycle_index_enhanced"},
        }
    )

    with pytest.raises(portfolio_research.HTTPException) as exc:
        asyncio.run(
            portfolio_research.publish_portfolio_research_result(
                "result-1",
                portfolio_research.PublishResearchResultRequest(status="draft"),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409


def test_publish_portfolio_research_result_rejects_already_published_result(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one({"job_id": "job-1", "user_id": "user-1", "status": "completed"})
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "published_preset_id": "published-preset",
            "candidate_strategy_config": {"preset_id": "research_strategy"},
        }
    )

    with pytest.raises(portfolio_research.HTTPException) as exc:
        asyncio.run(
            portfolio_research.publish_portfolio_research_result(
                "result-1",
                portfolio_research.PublishResearchResultRequest(status="enabled"),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409


def test_publish_portfolio_research_result_attaches_duplicate_params_as_evidence(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    params = {"top_n": 10, "rebalance_days": 20}
    collections["portfolio_strategy_param_presets"].insert_one(
        {
            "preset_id": "existing-preset",
            "user_id": "user-1",
            "strategy_template_id": "growth_cycle_index_enhanced",
            "status": "enabled",
            "params": params,
        }
    )
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-2",
            "user_id": "user-1",
            "status": "completed",
            "start_date": "20240101",
            "end_date": "20250131",
        }
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-2",
            "job_id": "job-2",
            "user_id": "user-1",
            "best_row": {"risk_adjusted_score": 1.3},
            "candidate_strategy_config": {
                "preset_id": "new-preset-id",
                "strategy_template_id": "growth_cycle_index_enhanced",
                **params,
            },
        }
    )

    response = asyncio.run(
        portfolio_research.publish_portfolio_research_result(
            "result-2",
            portfolio_research.PublishResearchResultRequest(status="enabled"),
            current_user={"id": "user-1", "username": "alice"},
        )
    )

    assert response["data"]["attached_to_existing"] is True
    assert len(collections["portfolio_strategy_param_presets"].docs) == 1
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": "existing-preset"})
    assert preset["latest_research_result_id"] == "result-2"
    assert preset["evidence"][0]["data_window"] == {"start_date": "20240101", "end_date": "20250131"}
    assert collections["portfolio_research_reviews"].docs[0]["decision"] == "evidence_attached"
    result = collections["portfolio_research_results"].find_one({"result_id": "result-2"})
    assert result["published_preset_id"] == "existing-preset"
    assert result["publish_action"] == "evidence_attached"


def test_resolve_artifact_path_falls_back_to_report_dir(monkeypatch, tmp_path):
    report_dir = tmp_path / "portfolio-research"
    report_dir.mkdir()
    report = report_dir / "job-1_result.html"
    report.write_text("<html>ok</html>", encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(report_dir))

    path = portfolio_research._resolve_artifact_path("reports/portfolio-research/job-1_result.html")

    assert path == report


def test_resolve_combo_file_translates_container_combo_dir(monkeypatch, tmp_path):
    report_dir = tmp_path / "portfolio-research"
    combo_dir = report_dir / "job-1_result_combos"
    combo_dir.mkdir(parents=True)
    detail = combo_dir / "combo-key.json"
    detail.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(report_dir))

    path = portfolio_research._resolve_combo_file(
        "/app/reports/portfolio-research/job-1_result_combos",
        "combo-key.json",
    )

    assert path == detail


def test_resolve_combo_file_uses_manifest_relative_path(monkeypatch, tmp_path):
    report_dir = tmp_path / "portfolio-research"
    combo_dir = report_dir / "job-1_result_combos"
    combo_dir.mkdir(parents=True)
    detail = combo_dir / "combo-key.json"
    detail.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(report_dir))

    path = portfolio_research._resolve_combo_file(
        None,
        "job-1_result_combos/combo-key.json",
    )

    assert path == detail
