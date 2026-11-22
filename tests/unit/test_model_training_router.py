import asyncio

import pytest
from fastapi import HTTPException

from routers import model_training


class _Collection:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)

    def find(self, query, projection=None):
        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def sort(self, key, direction=None):
                reverse = direction is not None and direction < 0
                if key == "created_at":
                    self._rows = sorted(self._rows, key=lambda d: d.get("created_at") or "", reverse=reverse)
                return self

            def limit(self, n):
                self._rows = self._rows[:n]
                return self

            def __iter__(self):
                return iter(self._rows)

        rows = list(self.docs)
        for key, value in query.items():
            if isinstance(value, dict):
                if "$exists" in value:
                    rows = [d for d in rows if (key in d) == bool(value["$exists"])]
                if "$ne" in value:
                    rows = [d for d in rows if d.get(key) != value["$ne"]]
            else:
                rows = [d for d in rows if d.get(key) == value]
        return _Cursor(rows)

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None


def test_create_model_training_job(monkeypatch):
    jobs = _Collection()
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_training_jobs": jobs, "model_training_results": _Collection()},
    )
    body = model_training.CreateModelTrainingJobRequest(name="test", index_code="csi1000")
    resp = model_training.create_model_training_job(body, current_user={"id": "u1"})
    assert resp["data"]["job_id"]
    assert jobs.docs[0]["status"] == "pending"


def test_list_dataset_snapshots(monkeypatch):
    from datetime import datetime

    manifests = _Collection()
    manifests.docs = [
        {
            "snapshot_id": "a",
            "index_code": "hs300",
            "parquet_path": "s3://b/hs300/a.parquet",
            "start_date": "20230101",
            "end_date": "20251231",
            "row_count": 100,
            "created_at": datetime(2026, 1, 2),
        },
        {
            "snapshot_id": "b",
            "index_code": "csi1000",
            "parquet_uri": "s3://b/csi1000/b.parquet",
            "created_at": datetime(2026, 1, 3),
        },
    ]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {
            "model_training_jobs": _Collection(),
            "model_training_results": _Collection(),
            "dataset_manifests": manifests,
        },
    )
    resp = model_training.list_dataset_snapshots(index_code="hs300", limit=10, current_user={"id": "u1"})
    assert len(resp["data"]) == 1
    assert resp["data"][0]["snapshot_id"] == "a"
    assert resp["data"][0]["parquet_path"].endswith("a.parquet")


def test_promotion_context_endpoint(monkeypatch):
    jobs = _Collection()
    results = _Collection()
    params = {
        "index_code": "hs300",
        "model_kind": "ridge",
        "train_days": 60,
        "test_days": 20,
        "purge_days": 60,
    }
    jobs.docs = [
        {
            "job_id": "j1",
            "user_id": "u1",
            "status": "completed",
            "result_id": "r1",
            "params": params,
        }
    ]
    fold = [{"fold_id": i, "model": {"mean_ic": 0.05}, "baseline": {"mean_ic": 0.01}} for i in range(4)]
    results.docs = [
        {
            "result_id": "r1",
            "code_version": "0.1.0",
            "fold_reports": fold,
            "baseline_ic": {"mean_ic": 0.03},
            "permutation_p": 0.02,
            "promotion": {"passed": False, "reason": "insufficient_universes"},
        }
    ]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {
            "model_training_jobs": jobs,
            "model_training_results": results,
            "dataset_manifests": _Collection(),
        },
    )
    resp = model_training.get_model_training_promotion_context("j1", current_user={"id": "u1"})
    assert resp["data"]["single_pool"]["passed"] is True
    assert resp["data"]["group"]["passed_pool_count"] == 1


def _matches(doc, query):
    for key, value in query.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in value):
                return False
            continue
        if isinstance(value, dict):
            if "$exists" in value:
                if (key in doc) != bool(value["$exists"]):
                    return False
            if "$ne" in value:
                if doc.get(key) == value["$ne"]:
                    return False
            if "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
            if "$nin" in value:
                if doc.get(key) in value["$nin"]:
                    return False
            if not set(value) & {"$exists", "$ne", "$in", "$nin"}:
                return False
        elif doc.get(key) != value:
            return False
    return True


class _AggregateCollection:
    """Aggregate rows plus the raw docs ``_prediction_sets`` reads.

    The endpoint's aggregate returns model-level rows, while the per-kind
    prediction sets come from ``distinct``/``find_one`` over raw rows. Tests
    that do not care about sets leave ``docs`` empty.
    """

    def __init__(self, rows=None, docs=None):
        self.rows = list(rows or [])
        self.docs = list(docs or [])
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return iter(self.rows)

    def distinct(self, field, query=None):
        values = {doc.get(field) for doc in self.docs if _matches(doc, query or {})}
        return sorted(value for value in values if value)

    def find_one(self, query, projection=None, sort=None):
        rows = [doc for doc in self.docs if _matches(doc, query or {})]
        for key, direction in reversed(list(sort or [])):
            rows.sort(key=lambda doc: doc.get(key) or "", reverse=direction < 0)
        return rows[0] if rows else None


def test_list_prediction_models_serializes_coverage(monkeypatch):
    predictions = _AggregateCollection([
        {
            "_id": "research-rolling500-hs300",
            "index_code": "hs300",
            "first_trade_date": "20170424",
            "last_trade_date": "20260909",
            "date_count": 115,
            "prediction_count": 34500,
            "research": True,
            "shadow": False,
            "source": "factor_incremental_experiment",
            "window": "rolling_500",
        },
        {
            "_id": "legacy",
            "first_trade_date": "20200102",
            "last_trade_date": "20200131",
            "date_count": 20,
            "prediction_count": 3000,
        },
    ])
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_predictions": predictions},
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": _Collection()},
    )

    response = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        limit=100,
        current_user={"id": "u1"},
    )

    assert response["data"][0] == {
        "model_id": "research-rolling500-hs300",
        "index_code": "hs300",
        "first_trade_date": "20170424",
        "last_trade_date": "20260909",
        "date_count": 115,
        "prediction_count": 34500,
        "research": True,
        "shadow": False,
        "source": "factor_incremental_experiment",
        "window": "rolling_500",
        "accepted": False,
        "accepted_reasons": [],
        "state": None,
        "validated": False,
        "validation": None,
        "prediction_sets": [],
    }
    # Missing flags mean neither research nor shadow, not None.
    assert response["data"][1]["research"] is False
    assert response["data"][1]["shadow"] is False
    assert response["data"][1]["prediction_sets"] == []

    pipeline = predictions.pipelines[0]
    assert pipeline[0]["$match"] == {"model_id": {"$exists": True, "$nin": [None, ""]}}
    assert pipeline[-1] == {"$limit": 100}


def test_list_prediction_models_applies_kind_and_index_filters(monkeypatch):
    predictions = _AggregateCollection([])
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_predictions": predictions},
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": _Collection()},
    )

    model_training.list_prediction_models(
        index_code="hs300",
        kind="research",
        limit=5,
        current_user={"id": "u1"},
    )
    research_match = predictions.pipelines[0][0]["$match"]
    assert research_match["index_code"] == "hs300"
    assert research_match["research"] is True
    assert "shadow" not in research_match

    model_training.list_prediction_models(
        index_code=None,
        kind="shadow",
        limit=5,
        current_user={"id": "u1"},
    )
    shadow_match = predictions.pipelines[1][0]["$match"]
    assert shadow_match["shadow"] is True
    assert "research" not in shadow_match
    assert "index_code" not in shadow_match


def test_list_prediction_models_returns_per_kind_prediction_sets(monkeypatch):
    """A model with both streams must not report the union as one set."""
    model_id = "research-csi1000-clean-raw5"
    predictions = _AggregateCollection(
        [
            {
                "_id": model_id,
                "index_code": "csi1000",
                "first_trade_date": "20160413",
                "last_trade_date": "20261120",
                "date_count": 130,
                "prediction_count": 120565,
                "research": True,
                "shadow": True,
                "source": "factor_incremental_experiment",
                "window": "expanding",
            },
        ],
        docs=[
            {
                "model_id": model_id,
                "trade_date": "20160413",
                "research": True,
                "source": "factor_incremental_experiment",
                "window": "expanding",
            },
            {
                "model_id": model_id,
                "trade_date": "20260923",
                "research": True,
                "source": "factor_incremental_experiment",
                "window": "expanding",
            },
            {
                "model_id": model_id,
                "trade_date": "20261106",
                "shadow": True,
                "source": "factor_incremental_experiment",
                "window": "full_sample_expanding",
            },
            {
                "model_id": model_id,
                "trade_date": "20261120",
                "shadow": True,
                "source": "factor_incremental_experiment",
                "window": "full_sample_expanding",
            },
        ],
    )
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_predictions": predictions},
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": _Collection()},
    )

    response = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        limit=100,
        current_user={"id": "u1"},
    )

    assert response["data"][0]["prediction_sets"] == [
        {
            "kind": "research",
            "first_trade_date": "20160413",
            "last_trade_date": "20260923",
            "date_count": 2,
            "source": "factor_incremental_experiment",
            "window": "expanding",
        },
        {
            "kind": "shadow",
            "first_trade_date": "20261106",
            "last_trade_date": "20261120",
            "date_count": 2,
            "source": "factor_incremental_experiment",
            "window": "full_sample_expanding",
        },
    ]


def test_list_prediction_models_marks_and_filters_accepted(monkeypatch):
    predictions = _AggregateCollection(
        [
            {
                "_id": "live-model",
                "index_code": "csi1000",
                "first_trade_date": "20160413",
                "last_trade_date": "20261113",
                "date_count": 128,
                "prediction_count": 118565,
                "research": True,
                "shadow": True,
            },
            {
                "_id": "shadow-only",
                "index_code": "hs300",
                "first_trade_date": "20261014",
                "last_trade_date": "20261113",
                "date_count": 20,
                "prediction_count": 6000,
                "research": False,
                "shadow": True,
            },
            {
                "_id": "retired",
                "index_code": "csi500",
                "first_trade_date": "20261019",
                "last_trade_date": "20261113",
                "date_count": 19,
                "prediction_count": 9489,
                "research": False,
                "shadow": True,
            },
        ]
    )
    shadow_profiles = _Collection()
    shadow_profiles.docs = [
        {"model_id": "live-model", "enabled": True},
        {"profile_id": "p2", "params": {"model_id": "shadow-only"}, "enabled": True},
        {"model_id": "retired", "enabled": False},
    ]
    activations = _Collection()
    activations.docs = [{"status": "active", "params_override": {"model_id": "live-model"}}]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {
            "model_predictions": predictions,
            "model_shadow_profiles": shadow_profiles,
        },
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": activations},
    )

    response = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        limit=100,
        current_user={"id": "u1"},
    )
    rows = {row["model_id"]: row for row in response["data"]}
    assert rows["live-model"]["accepted"] is True
    assert rows["live-model"]["accepted_reasons"] == ["live", "shadow"]
    assert rows["shadow-only"]["accepted"] is True
    assert rows["shadow-only"]["accepted_reasons"] == ["shadow"]
    assert rows["retired"]["accepted"] is False
    assert rows["retired"]["accepted_reasons"] == []

    accepted_response = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        accepted_only=True,
        limit=100,
        current_user={"id": "u1"},
    )
    assert {row["model_id"] for row in accepted_response["data"]} == {
        "live-model",
        "shadow-only",
    }
    assert predictions.pipelines[-1][0]["$match"]["model_id"] == {
        "$in": ["live-model", "shadow-only"]
    }


def test_list_prediction_models_accepted_only_returns_empty_without_sources(monkeypatch):
    predictions = _AggregateCollection([])
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_predictions": predictions},
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": _Collection()},
    )

    response = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        accepted_only=True,
        limit=100,
        current_user={"id": "u1"},
    )
    assert response["data"] == []
    assert predictions.pipelines == []


def test_list_prediction_models_include_validated_from_registry(monkeypatch):
    predictions = _AggregateCollection(
        [
            {
                "_id": "live-model",
                "index_code": "csi1000",
                "first_trade_date": "20250101",
                "last_trade_date": "20261116",
                "date_count": 100,
                "prediction_count": 10000,
                "research": True,
                "shadow": True,
            },
            {
                "_id": "validated-model",
                "index_code": "csi1000",
                "first_trade_date": "20250101",
                "last_trade_date": "20261113",
                "date_count": 451,
                "prediction_count": 449413,
                "research": True,
                "shadow": False,
            },
            {
                "_id": "raw-model",
                "index_code": "csi1000",
                "first_trade_date": "20250101",
                "last_trade_date": "20261113",
                "date_count": 20,
                "prediction_count": 20000,
                "research": True,
                "shadow": False,
            },
        ]
    )
    registry = _Collection()
    registry.docs = [
        {
            "_id": "live-model",
            "model_id": "live-model",
            "state": "live",
            "accepted": True,
            "accepted_reasons": ["live"],
            "latest_validation": {"protocol": "portfolio_oos", "verdict": "weak"},
        },
        {
            "_id": "validated-model",
            "model_id": "validated-model",
            "state": "validated",
            "accepted": False,
            "accepted_reasons": [],
            "latest_validation": {"protocol": "portfolio_oos", "verdict": "weak"},
        },
        {
            "_id": "raw-model",
            "model_id": "raw-model",
            "state": "research",
            "accepted": False,
            "accepted_reasons": [],
            "latest_validation": None,
        },
    ]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {
            "model_predictions": predictions,
            "models": registry,
        },
    )
    monkeypatch.setattr(
        model_training,
        "get_trading_db_collections",
        lambda: {"portfolio_strategy_activations": _Collection()},
    )

    accepted = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        accepted_only=True,
        limit=100,
        current_user={"id": "u1"},
    )
    assert {row["model_id"] for row in accepted["data"]} == {"live-model"}
    assert accepted["data"][0]["state"] == "live"
    assert accepted["data"][0]["validation"] == {"protocol": "portfolio_oos", "verdict": "weak"}

    with_validated = model_training.list_prediction_models(
        index_code=None,
        kind="all",
        accepted_only=True,
        include_validated=True,
        limit=100,
        current_user={"id": "u1"},
    )
    rows = {row["model_id"]: row for row in with_validated["data"]}
    assert set(rows) == {"live-model", "validated-model"}
    assert rows["validated-model"]["validated"] is True
    assert rows["validated-model"]["state"] == "validated"
    assert rows["validated-model"]["accepted"] is False
    assert "raw-model" not in rows


def _retrain_source():
    return {
        "job_id": "source-job",
        "user_id": "u1",
        "name": "clean-csi1000-raw5-live-20261108",
        "status": "completed",
        "result_id": "source-result",
        "params": {
            "model_id": "old-model",
            "index_code": "csi1000",
            "model_kind": "lightgbm",
            "data_source": "production_scores",
            "start_date": "20150130",
            "end_date": "20261106",
            "parquet_path": "/data/old-panel.parquet",
            "feature_names": ["momentum_20d", "momentum_60d", "volatility_20d", "turnover_20d", "log_market_cap"],
            "model_params": {"num_leaves": 15, "learning_rate": 0.05},
            "label_col": "label_fwd_20d",
            "train_days": 500,
            "test_days": 20,
            "purge_days": 60,
            "universe_count": 1,
        },
    }


def test_retrain_copies_full_recipe_and_overrides_version(monkeypatch):
    jobs = _Collection()
    jobs.docs = [_retrain_source()]
    models = _Collection()
    models.docs = [{"_id": "live-model", "model_id": "live-model", "state": "live"}]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_training_jobs": jobs, "models": models},
    )

    response = model_training.retrain_model_training_job(
        "source-job",
        model_training.RetrainModelTrainingJobRequest(
            model_id="csi1000-clean-raw5-202612",
            end_date="20261231",
        ),
        current_user={"id": "u1"},
    )
    new_job = jobs.docs[-1]
    assert response["data"]["model_id"] == "csi1000-clean-raw5-202612"
    assert new_job["status"] == "pending"
    assert new_job["retrain_of"] == "source-job"
    assert new_job["params"]["model_id"] == "csi1000-clean-raw5-202612"
    assert new_job["params"]["end_date"] == "20261231"
    assert new_job["params"]["feature_names"] == [
        "momentum_20d",
        "momentum_60d",
        "volatility_20d",
        "turnover_20d",
        "log_market_cap",
    ]
    assert new_job["params"]["model_params"] == {"num_leaves": 15, "learning_rate": 0.05}
    assert new_job["params"]["data_source"] == "production_scores"
    assert "parquet_path" not in new_job["params"]


def test_retrain_rejects_existing_managed_model_id(monkeypatch):
    jobs = _Collection()
    jobs.docs = [_retrain_source()]
    models = _Collection()
    models.docs = [{"_id": "live-model", "model_id": "live-model", "state": "live"}]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_training_jobs": jobs, "models": models},
    )
    with pytest.raises(HTTPException) as exc_info:
        model_training.retrain_model_training_job(
            "source-job",
            model_training.RetrainModelTrainingJobRequest(model_id="live-model", end_date="20261231"),
            current_user={"id": "u1"},
        )
    assert exc_info.value.status_code == 409


def test_retrain_requires_completed_source(monkeypatch):
    jobs = _Collection()
    jobs.docs = [{**_retrain_source(), "status": "running", "result_id": None}]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_training_jobs": jobs, "models": _Collection()},
    )
    with pytest.raises(HTTPException) as exc_info:
        model_training.retrain_model_training_job(
            "source-job",
            model_training.RetrainModelTrainingJobRequest(model_id="new-model", end_date="20261231"),
            current_user={"id": "u1"},
        )
    assert exc_info.value.status_code == 409


def test_retrain_rejects_synthetic_source_without_panel(monkeypatch):
    source = _retrain_source()
    source["params"]["data_source"] = "synthetic"
    source["params"].pop("parquet_path", None)
    jobs = _Collection()
    jobs.docs = [source]
    monkeypatch.setattr(
        model_training,
        "get_ml_db_collections",
        lambda: {"model_training_jobs": jobs, "models": _Collection()},
    )
    with pytest.raises(HTTPException) as exc_info:
        model_training.retrain_model_training_job(
            "source-job",
            model_training.RetrainModelTrainingJobRequest(model_id="new-model", end_date="20261231"),
            current_user={"id": "u1"},
        )
    assert exc_info.value.status_code == 422
