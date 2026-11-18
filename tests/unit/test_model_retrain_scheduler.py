from types import SimpleNamespace

from services import model_retrain_scheduler as scheduler


class _Schedules:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, projection=None):
        return [doc for doc in self.docs if doc.get("enabled") is not False]

    def update_one(self, query, update):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                doc.update(update.get("$set") or {})
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


class _Jobs:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, projection=None):
        model_id = query.get("params.model_id")
        if model_id:
            for doc in self.docs:
                if (doc.get("params") or {}).get("model_id") == model_id:
                    return doc
            return None
        for doc in self.docs:
            if doc.get("job_id") == query.get("job_id"):
                return doc
        return None

    def insert_one(self, doc):
        self.docs.append(doc)


class _Models:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if doc.get("model_id") == query.get("model_id"):
                return doc
        return None


def _source_job():
    return {
        "job_id": "source-job",
        "user_id": "u1",
        "name": "live source",
        "status": "completed",
        "result_id": "result-1",
        "params": {
            "index_code": "csi1000",
            "model_kind": "lightgbm",
            "data_source": "production_scores",
            "start_date": "20150130",
            "end_date": "20261106",
            "feature_names": ["momentum_20d"],
            "model_params": {"num_leaves": 15},
            "label_col": "label_fwd_20d",
        },
    }


def _patch(monkeypatch, schedule):
    schedules = _Schedules([schedule])
    jobs = _Jobs([_source_job()])
    models = _Models()
    monkeypatch.setattr(
        scheduler,
        "get_ml_db_collections",
        lambda: {
            "model_retrain_schedules": schedules,
            "model_training_jobs": jobs,
            "models": models,
        },
    )
    monkeypatch.setattr(scheduler, "get_data_db", lambda: object())
    monkeypatch.setattr(scheduler, "_today", lambda: "20261118")
    monkeypatch.setattr(scheduler, "_is_trading_day", lambda db, day: True)
    monkeypatch.setattr(scheduler, "_first_trading_day", lambda db, start: "20261201")
    monkeypatch.setattr(scheduler, "latest_volume_price_date", lambda: "20261118")
    return schedules, jobs


def test_due_monthly_schedule_enqueues_versioned_job(monkeypatch):
    schedule = {
        "_id": "s1",
        "schedule_id": "csi1000-monthly",
        "family_id": "csi1000-clean-raw5",
        "model_id_prefix": "csi1000-clean-raw5",
        "source_job_id": "source-job",
        "cadence": "monthly",
        "enabled": True,
        "next_run_date": "20261118",
        "user_id": "u1",
    }
    schedules, jobs = _patch(monkeypatch, schedule)
    result = scheduler.run_due_retrains()
    assert result["results"][0]["status"] == "enqueued"
    assert result["results"][0]["model_id"] == "csi1000-clean-raw5-202611"
    job = jobs.docs[-1]
    assert job["params"]["model_id"] == "csi1000-clean-raw5-202611"
    assert job["params"]["end_date"] == "20261118"
    assert job["schedule_id"] == "csi1000-monthly"
    assert schedules.docs[0]["next_run_date"] == "20261201"


def test_scheduler_is_idempotent_for_same_period(monkeypatch):
    schedule = {
        "_id": "s1",
        "schedule_id": "csi1000-monthly",
        "family_id": "csi1000-clean-raw5",
        "model_id_prefix": "csi1000-clean-raw5",
        "source_job_id": "source-job",
        "cadence": "monthly",
        "enabled": True,
        "next_run_date": "20261118",
        "user_id": "u1",
    }
    schedules, jobs = _patch(monkeypatch, schedule)
    scheduler.run_due_retrains()
    # Simulate a second scheduler tick in the same due window (the first run
    # advanced next_run_date, so reset it to the due date).
    schedules.docs[0]["next_run_date"] = "20261118"
    result = scheduler.run_due_retrains()
    assert result["results"][0]["status"] == "skipped"
    assert len(jobs.docs) == 2  # source + one retrain; no duplicate


def test_scheduler_skips_before_next_run(monkeypatch):
    schedule = {
        "_id": "s1",
        "schedule_id": "csi1000-quarterly",
        "family_id": "csi1000-clean-raw5",
        "model_id_prefix": "csi1000-clean-raw5",
        "source_job_id": "source-job",
        "cadence": "quarterly",
        "enabled": True,
        "next_run_date": "20270104",
        "user_id": "u1",
    }
    _patch(monkeypatch, schedule)
    result = scheduler.run_due_retrains()
    assert result["results"] == []


def test_scheduler_dry_run_does_not_insert(monkeypatch):
    schedule = {
        "_id": "s1",
        "schedule_id": "csi1000-monthly",
        "family_id": "csi1000-clean-raw5",
        "model_id_prefix": "csi1000-clean-raw5",
        "source_job_id": "source-job",
        "cadence": "monthly",
        "enabled": True,
        "next_run_date": "20261118",
        "user_id": "u1",
    }
    schedules, jobs = _patch(monkeypatch, schedule)
    result = scheduler.run_due_retrains(dry_run=True)
    assert result["results"][0]["status"] == "would_enqueue"
    assert len(jobs.docs) == 1
    assert schedules.docs[0]["next_run_date"] == "20261118"
