import asyncio
import json
from datetime import datetime

import pytest
from fastapi import HTTPException

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

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for key, value in update.get("$push", {}).items():
                    doc.setdefault(key, []).append(value)
                return
        if upsert:
            new_doc = {**query, **update.get("$setOnInsert", {}), **update.get("$set", {})}
            self.docs.append(new_doc)

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
                if "$gte" in expected and str(value or "") < str(expected["$gte"]):
                    return False
                if "$lte" in expected and str(value or "") > str(expected["$lte"]):
                    return False
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
        "portfolio_plan_generation_tasks": _Collection(),
        "portfolio_lineage_controls": _Collection(),
        "portfolio_strategy_activations": _Collection(),
        "portfolio_trade_plans": _Collection(),
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
    assert job["params"]["selection_mode"] == "fixed_top_n"
    assert job["params"]["threshold_lookback_days"] == 10
    assert job["params"]["max_positions"] == 20


@pytest.mark.unit
def test_research_selection_models_defaults_and_dynamic_validation():
    create_defaults = portfolio_research.CreateResearchJobRequest(
        start_date="20250101",
        end_date="20250131",
    )
    rerun_defaults = portfolio_research.RerunResearchJobRequest()

    assert create_defaults.selection_mode == "fixed_top_n"
    assert create_defaults.threshold_lookback_days == 10
    assert create_defaults.max_positions == 20
    # Rerun overrides stay unset so the source job's own values survive.
    assert rerun_defaults.selection_mode is None
    assert rerun_defaults.threshold_lookback_days is None
    assert rerun_defaults.max_positions is None

    dynamic_create = portfolio_research.CreateResearchJobRequest(
        start_date="20250101",
        end_date="20250131",
        selection_mode="dynamic_score_threshold",
        top_n_values=[10],
        threshold_lookback_days=15,
        max_positions=12,
    )
    dynamic_rerun = portfolio_research.RerunResearchJobRequest(
        selection_mode="dynamic_score_threshold",
        top_n_values=[8],
        threshold_lookback_days=5,
        max_positions=8,
    )
    assert dynamic_create.top_n_values == [10]
    assert dynamic_rerun.top_n_values == [8]

    for top_n_values, max_positions in (([10, 20], 20), ([0], 20), ([10], 9)):
        with pytest.raises(ValueError, match="top_n_values|positive integer|max_positions"):
            portfolio_research.CreateResearchJobRequest(
                start_date="20250101",
                end_date="20250131",
                selection_mode="dynamic_score_threshold",
                top_n_values=top_n_values,
                max_positions=max_positions,
            )

    # Switching mode alone is allowed: top_n_values is inherited from the source job
    # and the merged payload is revalidated as a create request.
    assert portfolio_research.RerunResearchJobRequest(
        selection_mode="dynamic_score_threshold"
    ).top_n_values is None
    with pytest.raises(ValueError, match="top_n_values"):
        portfolio_research.RerunResearchJobRequest(
            selection_mode="dynamic_score_threshold",
            top_n_values=[10, 20],
        )
    with pytest.raises(ValueError, match="max_positions"):
        portfolio_research.RerunResearchJobRequest(
            selection_mode="dynamic_score_threshold",
            top_n_values=[10],
            max_positions=9,
        )
    with pytest.raises(ValueError):
        portfolio_research.CreateResearchJobRequest(
            start_date="20250101",
            end_date="20250131",
            threshold_lookback_days=0,
        )
    with pytest.raises(ValueError):
        portfolio_research.RerunResearchJobRequest(max_positions=0)


@pytest.mark.unit
def test_create_dynamic_research_job_persists_selection_params(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                start_date="2025-01-01",
                end_date="2025-01-31",
                selection_mode="dynamic_score_threshold",
                top_n_values=[10],
                threshold_lookback_days=15,
                max_positions=16,
            ),
            current_user={"id": "user-1"},
        )
    )

    params = response["data"]["params"]
    assert params["selection_mode"] == "dynamic_score_threshold"
    assert params["top_n_values"] == [10]
    assert params["threshold_lookback_days"] == 15
    assert params["max_positions"] == 16


def test_create_portfolio_research_job_accepts_generic_score_specs(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    request = portfolio_research.CreateResearchJobRequest(
        name="generic blend",
        start_date="2025-01-01",
        end_date="2025-01-31",
        score_specs=[
            {
                "mode": "weighted",
                "weights": {"fundamental": 0.6, "value": 0.4},
            }
        ],
    )
    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            request,
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    job = collections["portfolio_research_jobs"].docs[0]
    assert job["strategy_family"] == "weighted_score"
    assert "growth_cycle_weights" not in job["params"]
    assert job["params"]["score_specs"][0]["weights"] == {
        "fundamental": 0.6,
        "value": 0.4,
    }


def test_score_specs_validate_dimensions_and_reject_legacy_conflict():
    with pytest.raises(ValueError, match="unsupported score dimensions"):
        portfolio_research.CreateResearchJobRequest(
            start_date="20250101",
            end_date="20250131",
            score_specs=[{"mode": "weighted", "weights": {"unknown": 1}}],
        )

    with pytest.raises(ValueError, match="mutually exclusive"):
        portfolio_research.CreateResearchJobRequest(
            start_date="20250101",
            end_date="20250131",
            score_specs=[{"mode": "column", "column": "fundamental_score"}],
            growth_cycle_weights=["30:70"],
        )


@pytest.mark.unit
def test_create_portfolio_research_job_accepts_trailing_stop_pcts(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi1000",
                start_date="2025-01-01",
                end_date="2025-01-31",
                trailing_stop_pcts=[0.0, 0.1, 0.15, 0.2],
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert collections["portfolio_research_jobs"].docs[0]["params"]["trailing_stop_pcts"] == [0.0, 0.1, 0.15, 0.2]


@pytest.mark.unit
def test_create_portfolio_research_job_accepts_regime_cash_on_any_universe(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi500",
                start_date="2025-01-01",
                end_date="2025-01-31",
                regime_cash=True,
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    params = collections["portfolio_research_jobs"].docs[0]["params"]
    assert params["universe_index"] == "csi500"
    assert params["regime_cash"] is True


@pytest.mark.unit
def test_create_portfolio_research_job_accepts_cash_only_regime_modes(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi1000",
                start_date="2025-01-01",
                end_date="2025-01-31",
                regime_modes=["bull_g60_else_cash"],
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    params = collections["portfolio_research_jobs"].docs[0]["params"]
    assert params["regime_modes"] == ["bull_g60_else_cash"]
    assert params["regime_cash"] is False


@pytest.mark.unit
def test_create_portfolio_research_job_accepts_volume_price_regime_rule(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi1000",
                start_date="2025-01-01",
                end_date="2025-01-31",
                regime_modes=["bull_g60_else_cash"],
                regime_rule="k_slow_anchor_with_vol_and_atr",
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    params = collections["portfolio_research_jobs"].docs[0]["params"]
    assert params["regime_rule"] == "k_slow_anchor_with_vol_and_atr"


@pytest.mark.unit
def test_create_portfolio_research_job_accepts_multiple_regime_rules(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="research",
                universe_index="csi1000",
                start_date="2025-01-01",
                end_date="2025-01-31",
                regime_modes=["bull_g60_else_cash"],
                regime_rules=["k_slow_anchor", "k_slow_anchor_with_vol_and_atr"],
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    params = collections["portfolio_research_jobs"].docs[0]["params"]
    assert params["regime_rules"] == ["k_slow_anchor", "k_slow_anchor_with_vol_and_atr"]
    assert "regime_rule" not in params


@pytest.mark.unit
def test_research_job_requests_reject_invalid_or_empty_regime_modes():
    common = {
        "name": "research",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    }

    with pytest.raises(ValueError):
        portfolio_research.CreateResearchJobRequest(**common, regime_modes=["bogus"])
    with pytest.raises(ValueError):
        portfolio_research.CreateResearchJobRequest(**common, regime_modes=[])
    with pytest.raises(ValueError):
        portfolio_research.RerunResearchJobRequest(regime_modes=["bogus"])
    with pytest.raises(ValueError):
        portfolio_research.CreateResearchJobRequest(**common, regime_rule="not_a_rule")
    with pytest.raises(ValueError):
        portfolio_research.RerunResearchJobRequest(regime_rule="not_a_rule")


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
    assert cloned["params"]["selection_mode"] == "fixed_top_n"
    assert cloned["params"]["threshold_lookback_days"] == 10
    assert cloned["params"]["max_positions"] == 20
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
def test_rerun_portfolio_research_job_accepts_dynamic_selection(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-source",
            "user_id": "user-1",
            "name": "fixed research",
            "universe_index": "csi1000",
            "start_date": "20250101",
            "end_date": "20250131",
            "params": {
                "universe_index": "csi1000",
                "start_date": "20250101",
                "end_date": "20250131",
                "top_n_values": [10, 20],
            },
            "status": "completed",
        }
    )

    response = asyncio.run(
        portfolio_research.rerun_portfolio_research_job(
            "job-source",
            request=portfolio_research.RerunResearchJobRequest(
                selection_mode="dynamic_score_threshold",
                top_n_values=[8],
                threshold_lookback_days=12,
                max_positions=15,
            ),
            current_user={"id": "user-1"},
        )
    )

    params = response["data"]["params"]
    assert params["selection_mode"] == "dynamic_score_threshold"
    assert params["top_n_values"] == [8]
    assert params["threshold_lookback_days"] == 12
    assert params["max_positions"] == 15


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
            "best_row": {
                "risk_adjusted_score": 1.2,
                "regime_mode": "bull_g60_else_cash",
            },
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
    assert preset["params"]["regime_cash"] is True
    assert collections["portfolio_research_reviews"].docs[0]["decision"] == "preset_published"


def test_publish_portfolio_research_result_rejects_dynamic_selection(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-dynamic",
            "user_id": "user-1",
            "status": "completed",
            "params": {
                "selection_mode": "dynamic_score_threshold",
                "top_n_values": [10],
                "threshold_lookback_days": 10,
                "max_positions": 20,
            },
        }
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-dynamic",
            "job_id": "job-dynamic",
            "user_id": "user-1",
            # Simulate a candidate emitted before the scoring side knows the new fields.
            "candidate_strategy_config": {
                "preset_id": "dynamic-research",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "top_n": 10,
            },
        }
    )

    with pytest.raises(portfolio_research.HTTPException) as exc:
        asyncio.run(
            portfolio_research.publish_portfolio_research_result(
                "result-dynamic",
                portfolio_research.PublishResearchResultRequest(status="draft"),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert "live portfolio construction" in exc.value.detail
    assert collections["portfolio_strategy_param_presets"].docs == []


def test_publish_portfolio_research_result_publishes_weighted_score(monkeypatch):
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
            "candidate_strategy_config": {
                "preset_id": "generic-score",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "name": "Generic Weighted",
                "score_type": "weighted_score",
                "score_weights": {"fundamental": 0.6, "value": 0.4},
                "top_n": 10,
                "rebalance_days": 20,
            },
        }
    )

    response = asyncio.run(
        portfolio_research.publish_portfolio_research_result(
            "result-1",
            portfolio_research.PublishResearchResultRequest(status="draft"),
            current_user={"id": "user-1", "username": "alice"},
        )
    )

    assert response["success"] is True
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": "generic-score"})
    assert preset["params"]["score_type"] == "weighted_score"
    assert preset["params"]["score_weights"] == {"fundamental": 0.6, "value": 0.4}


def test_publish_portfolio_research_result_rejects_unknown_weighted_dimension(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {"job_id": "job-1", "user_id": "user-1", "status": "completed"}
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "candidate_strategy_config": {
                "preset_id": "generic-score",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "score_type": "weighted_score",
                "score_weights": {"fundamental": 0.5, "sentiment": 0.5},
            },
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

    assert exc.value.status_code == 400
    assert "sentiment" in exc.value.detail


def test_publish_portfolio_research_result_rejects_score_column(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one(
        {"job_id": "job-1", "user_id": "user-1", "status": "completed"}
    )
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "candidate_strategy_config": {
                "preset_id": "generic-score",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "score_type": "score_column",
                "score_column": "composite_score.value_oriented",
            },
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
    assert "not productized" in exc.value.detail


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


@pytest.mark.unit
def test_resolve_combo_file_uses_basename_combo_dir_and_file(monkeypatch, tmp_path):
    report_dir = tmp_path / "portfolio-research"
    combo_dir = report_dir / "job-1_result_combos"
    combo_dir.mkdir(parents=True)
    detail = combo_dir / "combo-key.json"
    detail.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_REPORT_DIR", str(report_dir))

    path = portfolio_research._resolve_combo_file(
        "job-1_result_combos",
        "combo-key.json",
    )

    assert path == detail


@pytest.mark.unit
def test_list_portfolio_research_result_rows_paginates(monkeypatch, tmp_path):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    json_path = tmp_path / "result.json"
    json_path.write_text(
        json.dumps({"rows": [{"top_n": i, "risk_adjusted_score": i} for i in range(5)]}),
        encoding="utf-8",
    )
    collections["portfolio_research_jobs"].insert_one({"job_id": "job-rows", "user_id": "user-1", "status": "completed"})
    collections["portfolio_research_results"].insert_one(
        {
            "job_id": "job-rows",
            "user_id": "user-1",
            "rows": [{"top_n": 0}],
            "row_count_total": 5,
            "artifact": {"json_path": str(json_path)},
        }
    )

    response = asyncio.run(
        portfolio_research.list_portfolio_research_result_rows(
            "job-rows",
            page=2,
            page_size=2,
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert response["data"]["total"] == 5
    assert response["data"]["page"] == 2
    assert len(response["data"]["rows"]) == 2
    assert response["data"]["rows"][0]["top_n"] == 2


@pytest.mark.unit
def test_load_full_result_rows_prefers_artifact_without_row_count_total(tmp_path):
    json_path = tmp_path / "result.json"
    json_path.write_text(
        json.dumps({"rows": [{"top_n": i, "risk_adjusted_score": i} for i in range(72)]}),
        encoding="utf-8",
    )
    preview = [{"top_n": 0, "risk_adjusted_score": 0.53}]

    rows = portfolio_research._load_full_result_rows(
        {
            "rows": preview,
            "artifact": {"json_path": str(json_path)},
        }
    )

    assert len(rows) == 72
    assert rows[0]["top_n"] == 0


@pytest.mark.unit
def test_publish_portfolio_research_result_uses_selected_row(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_research_jobs"].insert_one({"job_id": "job-1", "user_id": "user-1", "status": "completed"})
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "candidate_strategy_config": {
                "preset_id": "research_old",
                "strategy_template_id": "growth_cycle_index_enhanced",
                "top_n": 10,
                "rebalance_days": 20,
            },
            "best_row": {"top_n": 10},
        }
    )

    response = asyncio.run(
        portfolio_research.publish_portfolio_research_result(
            "result-1",
            portfolio_research.PublishResearchResultRequest(
                status="draft",
                selected_row={
                    "top_n": 20,
                    "selection_mode": "fixed_top_n",
                    "threshold_lookback_days": 15,
                    "max_positions": 30,
                    "variant": "growth_cycle_topn",
                    "score_variant": "growth_cycle_g30_c70",
                    "rebalance_interval_days": 20,
                    "trailing_stop_pct": 0.15,
                    "construction_mode": "top_n",
                    "regime_mode": "bull_g60_else_cash",
                },
            ),
            current_user={"id": "user-1", "username": "alice"},
        )
    )

    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": response["data"]["preset"]["preset_id"]})
    assert preset["params"]["top_n"] == 20
    assert preset["params"]["trailing_stop_pct"] == 0.15
    assert preset["params"]["regime_cash"] is True
    # Selection-mode keys stay out of preset params so params_hash keeps matching
    # presets published before dynamic thresholds existed.
    assert "selection_mode" not in preset["params"]
    assert "threshold_lookback_days" not in preset["params"]
    assert "max_positions" not in preset["params"]


@pytest.mark.unit
def test_list_sw_l1_catalog_returns_sorted_l1_rows(monkeypatch):
    class _DataColl:
        def find(self, query, projection=None):
            return [
                {"index_code": "801080.SI", "industry_name": "电子"},
                {"index_code": "801730.SI", "industry_name": "电力设备"},
            ]

    import db as db_module

    monkeypatch.setattr(db_module, "data_db", {"sw_index_classify": _DataColl()})

    response = asyncio.run(
        portfolio_research.list_sw_l1_catalog(current_user={"id": "user-1"})
    )
    assert response["success"] is True
    codes = [row["code"] for row in response["data"]]
    assert codes == ["801080.SI", "801730.SI"]
    assert response["data"][1]["name"] == "电力设备"


@pytest.mark.unit
def test_create_portfolio_research_job_with_industry_l1_sets_benchmark(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="industry smoke",
                universe_index="csi1000",
                start_date="2024-01-01",
                end_date="2024-02-29",
                industry_l1="801730.SI",
                top_n_values=[8],
                active_caps=[0.3],
                index_benchmark_symbol="000852.SH",
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    job = collections["portfolio_research_jobs"].docs[0]
    assert job["params"]["industry_l1"] == "801730.SI"
    assert job["params"]["index_benchmark_symbol"] == "801730.SI"


def _ml_predictions(rows):
    return {"model_predictions": _Collection(rows)}


@pytest.mark.unit
def test_create_model_score_job_forces_model_column(monkeypatch):
    collections = _collections()
    predictions = _ml_predictions([
        {"model_id": "research-test-hs300", "trade_date": "20250102", "ts_code": "000001.SZ", "score": 0.1},
        {"model_id": "research-test-hs300", "trade_date": "20250131", "ts_code": "000001.SZ", "score": 0.2},
    ])
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_research, "get_ml_db_collections", lambda: predictions)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                name="model smoke",
                universe_index="hs300",
                start_date="2025-01-01",
                end_date="2025-01-31",
                model_id="research-test-hs300",
            ),
            current_user={"id": "user-1"},
        )
    )

    job = collections["portfolio_research_jobs"].docs[0]
    assert response["success"] is True
    assert job["strategy_family"] == "model_score"
    assert job["params"]["model_id"] == "research-test-hs300"
    # model_id wins even though score_column kept its rule-score default.
    assert job["params"]["score_column"] == "model_score"
    assert "score_specs" not in job["params"]
    assert "growth_cycle_weights" not in job["params"]


@pytest.mark.unit
def test_model_id_rejects_conflicting_score_inputs():
    base = {
        "start_date": "20250101",
        "end_date": "20250131",
        "model_id": "research-test-hs300",
    }
    with pytest.raises(ValueError, match="model_id cannot be combined with score_specs"):
        portfolio_research.CreateResearchJobRequest(
            **base,
            score_specs=[{"mode": "column", "column": "value_score"}],
        )
    with pytest.raises(ValueError, match="model_id cannot be combined with growth_cycle_weights"):
        portfolio_research.CreateResearchJobRequest(**base, growth_cycle_weights=["30:70"])
    with pytest.raises(ValueError, match="model_id requires score_column"):
        portfolio_research.CreateResearchJobRequest(**base, score_column="value_score")
    with pytest.raises(ValueError, match="requires model_id"):
        portfolio_research.CreateResearchJobRequest(
            start_date="20250101",
            end_date="20250131",
            score_column="model_score",
        )


@pytest.mark.unit
def test_create_model_score_job_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(portfolio_research, "get_ml_db_collections", lambda: _ml_predictions([]))

    with pytest.raises(HTTPException) as exc:
        portfolio_research._build_research_job_document(
            "user-1",
            portfolio_research.CreateResearchJobRequest(
                start_date="20250101",
                end_date="20250131",
                model_id="missing-model",
            ),
        )

    assert exc.value.status_code == 422
    assert "unknown model_id" in exc.value.detail


@pytest.mark.unit
def test_create_model_score_job_rejects_no_overlap(monkeypatch):
    predictions = _ml_predictions([
        {"model_id": "research-test-hs300", "trade_date": "20240102", "ts_code": "000001.SZ", "score": 0.1},
        {"model_id": "research-test-hs300", "trade_date": "20241231", "ts_code": "000001.SZ", "score": 0.2},
    ])
    monkeypatch.setattr(portfolio_research, "get_ml_db_collections", lambda: predictions)

    with pytest.raises(HTTPException) as exc:
        portfolio_research._build_research_job_document(
            "user-1",
            portfolio_research.CreateResearchJobRequest(
                start_date="20250101",
                end_date="20250131",
                model_id="research-test-hs300",
            ),
        )

    assert exc.value.status_code == 422
    assert "no overlap" in exc.value.detail


@pytest.mark.unit
def test_rerun_model_job_keeps_model_id_and_switches_modes(monkeypatch):
    source = {
        "name": "model job",
        "universe_index": "hs300",
        "start_date": "20250101",
        "end_date": "20250131",
        "params": {
            "model_id": "research-test-hs300",
            "score_column": "model_score",
            "start_date": "20250101",
            "end_date": "20250131",
        },
    }

    same_model = portfolio_research._create_request_from_job(source)
    assert same_model.model_id == "research-test-hs300"
    assert same_model.score_column == "model_score"

    # Switching to a rule column must clear model_id instead of tripping validation.
    rule_request = portfolio_research._create_request_from_job(
        source,
        options=portfolio_research.RerunResearchJobRequest(score_column="value_score"),
    )
    assert rule_request.model_id is None
    assert rule_request.score_column == "value_score"

    # Switching a rule job onto a model must drop the source's rule inputs.
    rule_source = {
        "name": "rule job",
        "universe_index": "hs300",
        "start_date": "20250101",
        "end_date": "20250131",
        "params": {
            "score_column": "composite_growth_cycle_score",
            "growth_cycle_weights": ["30:70"],
            "start_date": "20250101",
            "end_date": "20250131",
        },
    }
    monkeypatch.setattr(
        portfolio_research,
        "get_ml_db_collections",
        lambda: _ml_predictions([
            {"model_id": "research-test-hs300", "trade_date": "20250102"},
            {"model_id": "research-test-hs300", "trade_date": "20250131"},
        ]),
    )
    model_request = portfolio_research._create_request_from_job(
        rule_source,
        options=portfolio_research.RerunResearchJobRequest(model_id="research-test-hs300"),
    )
    job = portfolio_research._build_research_job_document("user-1", model_request)
    assert job["strategy_family"] == "model_score"
    assert job["params"]["score_column"] == "model_score"
    assert "growth_cycle_weights" not in job["params"]


@pytest.mark.unit
def test_rerun_rejects_model_id_with_rule_inputs():
    source = {
        "name": "rule job",
        "universe_index": "hs300",
        "start_date": "20250101",
        "end_date": "20250131",
        "params": {"start_date": "20250101", "end_date": "20250131"},
    }
    with pytest.raises(HTTPException) as exc:
        portfolio_research._create_request_from_job(
            source,
            options=portfolio_research.RerunResearchJobRequest(
                model_id="research-test-hs300",
                growth_cycle_weights=["30:70"],
            ),
        )
    assert exc.value.status_code == 422
    assert "model_id cannot be combined" in exc.value.detail


@pytest.mark.unit
def test_create_job_persists_volatility_sweep_params(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_research.create_portfolio_research_job(
            portfolio_research.CreateResearchJobRequest(
                start_date="2025-01-01",
                end_date="2025-01-31",
                volatility_modes=["off", "renormalize"],
                target_vols=[0.15, 0.2],
                volatility_column="vol_20d_annual",
            ),
            current_user={"id": "user-1"},
        )
    )

    params = response["data"]["params"]
    assert params["volatility_modes"] == ["off", "renormalize"]
    assert params["target_vols"] == [0.15, 0.2]
    assert params["volatility_column"] == "vol_20d_annual"

    source = {
        "name": "vol job",
        "universe_index": "csi1000",
        "start_date": "20250101",
        "end_date": "20250131",
        "params": {
            "start_date": "20250101",
            "end_date": "20250131",
            "volatility_modes": ["off", "renormalize"],
            "target_vols": [0.15, 0.2],
            "volatility_column": "vol_20d_annual",
        },
    }
    rerun = portfolio_research._create_request_from_job(source)
    assert rerun.volatility_modes == ["off", "renormalize"]
    assert rerun.target_vols == [0.15, 0.2]
    assert rerun.volatility_column == "vol_20d_annual"


@pytest.mark.unit
def test_target_vols_are_validated():
    for bad in ([0], [-0.1], [2.5]):
        with pytest.raises(ValueError, match="target_vols"):
            portfolio_research.CreateResearchJobRequest(
                start_date="20250101",
                end_date="20250131",
                target_vols=bad,
            )


@pytest.mark.unit
def test_create_live_preview_enqueues_preview_task(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)

    job = {
        "job_id": "job-1",
        "user_id": "user-1",
        "params": {
            "universe_index": "csi500",
            "top_n_values": [20],
            "horizon": 20,
            "score_column": "growth_cycle_g60_c40_score",
            "transaction_cost": 0.0027,
            "index_benchmark_symbol": "000905.SH",
        },
    }
    row = {
        "combo_key": "combo-1",
        "score_variant": "growth_cycle_g60_c40",
        "variant": "top_n",
        "top_n": 20,
        "rebalance_interval_days": 20,
        "construction_mode": "top_n",
        "max_industry_weight": 30,
        "regime_mode": "off",
        "trailing_stop_pct": None,
    }
    collections["portfolio_research_jobs"].insert_one(job)
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "res-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "rows": [row],
            "row_count_total": 1,
        }
    )

    response = asyncio.run(
        portfolio_research.create_portfolio_research_live_preview(
            "res-1",
            portfolio_research.LivePreviewRequest(
                combo_key="combo-1",
                as_of_date="20250106",
            ),
            current_user={"id": "user-1"},
        )
    )

    task = collections["portfolio_plan_generation_tasks"].docs[0]
    assert task["task_type"] == "preview_portfolio_plan"
    assert task["base_date"] == "20250106"
    assert task["combo_key"] == "combo-1"
    assert task["params_override"]["top_n"] == 20
    assert task["params_override"]["universe_index"] == "csi500"
    assert response["data"]["task_id"] == task["task_id"]


@pytest.mark.unit
def test_get_live_preview_returns_task_result(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_plan_generation_tasks"].insert_one(
        {
            "task_id": "task-1",
            "user_id": "user-1",
            "task_type": "preview_portfolio_plan",
            "status": "completed",
            "result": {"plan": {"plan_id": "preview"}, "items": []},
        }
    )

    response = asyncio.run(
        portfolio_research.get_portfolio_research_live_preview(
            "task-1",
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["status"] == "completed"
    assert response["data"]["result"]["plan"]["plan_id"] == "preview"


def _activation_candidate():
    return {
        "strategy_template_id": "growth_cycle_index_enhanced",
        "preset_id": "research_combo_preset",
        "name": "combo preset",
        "universe_index": "csi500",
        "index_benchmark_symbol": "000905.SH",
        "score_type": "growth_cycle_weighted",
        "score_column": "growth_cycle_g60_c40_score",
        "growth_weight": 0.6,
        "cycle_weight": 0.4,
        "score_weights": None,
        "top_n": 20,
        "selection_mode": "fixed_top_n",
        "threshold_lookback_days": 10,
        "max_positions": 20,
        "rebalance_days": 20,
        "construction_mode": "top_n",
        "max_industry_weight": 30,
        "lot_size": 100,
        "transaction_cost": 0.0027,
        "execution_price": "next_open",
        "cash_buffer": 0.03,
        "initial_capital": 1_000_000.0,
        "trailing_stop_pct": None,
        "candidate_pool_enabled": True,
        "regime_cash": True,
        "regime_rule": "k_slow_anchor",
    }


def _preview_task(status="completed", parity_status="clean", suggested="20991231"):
    return {
        "task_id": "task-activate",
        "user_id": "user-1",
        "task_type": "preview_portfolio_plan",
        "base_date": "20250106",
        "status": status,
        "result_id": "res-1",
        "research_job_id": "job-1",
        "combo_key": "combo-1",
        "params_override": _activation_candidate(),
        "selected_row": {"combo_key": "combo-1", "top_n": 20},
        "result": {
            "plan": {"plan_id": "preview-plan", "next_rebalance_date": "20250203"},
            "items": [],
            "suggested_effective_date": suggested,
            "parity": {
                "status": parity_status,
                "checked_dates": 1,
                "matched_dates": 0 if parity_status != "clean" else 1,
                "backfilled_dates": 0,
                "unexplained_dates": 0 if parity_status == "clean" else 1,
                "unexplained": [] if parity_status == "clean" else [{"score_date": "20250106"}],
            },
        },
    }


def test_create_live_preview_attaches_compact_parity_inputs(monkeypatch, tmp_path):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    combos_dir = tmp_path / "combos"
    combos_dir.mkdir()
    (combos_dir / "combo.json").write_text(
        json.dumps(
            {
                "periods": [
                    {"score_date": "20250103", "regime_action": "invest"},
                    {"score_date": "20250106", "regime_action": "invest"},
                    {"score_date": "20250107", "regime_action": "invest"},
                ],
                "trades": [
                    {"score_date": "20250103", "symbol": "000001"},
                    {"score_date": "20250106", "symbol": "000002"},
                    {"score_date": "20250106", "symbol": "000003", "quantity": 100},
                ],
            }
        ),
        encoding="utf-8",
    )
    collections["portfolio_research_jobs"].insert_one(
        {
            "job_id": "job-1",
            "user_id": "user-1",
            "params": {
                "universe_index": "csi500",
                "top_n_values": [20],
                "horizon": 20,
                "score_column": "growth_cycle_g60_c40_score",
            },
        }
    )
    row = {
        "combo_key": "combo-1",
        "score_variant": "growth_cycle_g60_c40",
        "variant": "top_n",
        "top_n": 20,
        "rebalance_interval_days": 20,
        "construction_mode": "top_n",
        "regime_mode": "off",
    }
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "res-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "rows": [row],
            "row_count_total": 1,
            "artifact": {
                "combos_dir": str(combos_dir),
                "combos": [{"combo_key": "combo-1", "file": "combo.json"}],
            },
        }
    )

    asyncio.run(
        portfolio_research.create_portfolio_research_live_preview(
            "res-1",
            portfolio_research.LivePreviewRequest(combo_key="combo-1", as_of_date="20250106"),
            current_user={"id": "user-1"},
        )
    )

    task = collections["portfolio_plan_generation_tasks"].docs[0]
    assert [period["score_date"] for period in task["research_periods"]] == [
        "20250103",
        "20250106",
    ]
    assert task["research_trades"] == [
        {"score_date": "20250103", "symbol": "000001"},
        {"score_date": "20250106", "symbol": "000002"},
        {"score_date": "20250106", "symbol": "000003"},
    ]
    assert task["selected_row"]["combo_key"] == "combo-1"


def test_activate_live_preview_blocks_unexplained_parity(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_plan_generation_tasks"].insert_one(
        _preview_task(parity_status="diff")
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            portfolio_research.activate_portfolio_research_live_preview(
                "task-activate",
                portfolio_research.ActivateResearchComboRequest(
                    execution_venue="paper",
                    effective_date="20991231",
                ),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["parity"]["status"] == "diff"
    assert collections["portfolio_strategy_activations"].docs == []


def test_activate_live_preview_schedules_clean_combo(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_plan_generation_tasks"].insert_one(_preview_task())

    response = asyncio.run(
        portfolio_research.activate_portfolio_research_live_preview(
            "task-activate",
            portfolio_research.ActivateResearchComboRequest(
                execution_venue="paper",
                effective_date="20991231",
                comment="rollout",
            ),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["applied_now"] is False
    assert data["activation"]["status"] == "scheduled"
    assert data["activation"]["effective_date"] == "20991231"
    assert data["activation"]["params_hash"] == portfolio_research._lineage_params_hash(
        _activation_candidate()
    )
    assert len(data["preset"]["spec_hash"]) == 12
    assert data["preset"]["params"]["top_n"] == 20
    assert data["advisory_plan_id"] == "preview-plan"
    assert data["activation"]["advisory_plan_snapshot"]["plan"]["plan_id"] == "preview-plan"
    assert collections["portfolio_strategy_activations"].docs[0]["status"] == "scheduled"
    assert collections["portfolio_plan_generation_tasks"].docs[0]["activation_status"] == "scheduled"


def test_activate_live_preview_admin_override_applies_now(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_plan_generation_tasks"].insert_one(
        _preview_task(parity_status="diff", suggested="20250106")
    )
    collections["portfolio_trade_plans"].insert_one(
        {
            "plan_id": "old-plan",
            "user_id": "user-1",
            "strategy_template_id": "growth_cycle_index_enhanced",
            "params_hash": "oldhash",
            "base_date": "20261020",
            "status": "generated",
            "live_signals_published_at": datetime.utcnow(),
        }
    )

    response = asyncio.run(
        portfolio_research.activate_portfolio_research_live_preview(
            "task-activate",
            portfolio_research.ActivateResearchComboRequest(
                execution_venue="live",
                admin_override=True,
            ),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["applied_now"] is True
    assert data["activation"]["status"] == "active"
    assert data["paused_lineages"] == [
        {"strategy_template_id": "growth_cycle_index_enhanced", "params_hash": "oldhash"}
    ]
    lineage = collections["portfolio_lineage_controls"].find_one(
        {"strategy_template_id": "growth_cycle_index_enhanced", "params_hash": "oldhash"}
    )
    assert lineage["paused"] is True
    activation_task = next(
        task
        for task in collections["portfolio_plan_generation_tasks"].docs
        if task.get("source") == "strategy_activation"
    )
    assert activation_task["mode"] == "rebalance"
    assert activation_task["params_override"]["capital_basis"] == "account_equity"


def test_activate_live_preview_blocks_unsupported_research_features(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    task = _preview_task()
    task["research_features"] = {
        "intra_period_cash": True,
        "variant": "vol70",
        "volatility_mode": "off",
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            portfolio_research.activate_portfolio_research_live_preview(
                "task-activate",
                portfolio_research.ActivateResearchComboRequest(
                    execution_venue="paper",
                    effective_date="20991231",
                ),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert "intra_period_cash" in exc.value.detail["unsupported_features"]
    assert any(
        feature.startswith("entry_filter:") for feature in exc.value.detail["unsupported_features"]
    )
    assert collections["portfolio_strategy_activations"].docs == []
