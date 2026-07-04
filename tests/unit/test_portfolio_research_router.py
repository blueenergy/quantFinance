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
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if any(key.startswith("$") for key in expected):
                    continue
                if value != expected:
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
