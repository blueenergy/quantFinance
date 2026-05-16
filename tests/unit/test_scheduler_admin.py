from datetime import datetime

import pytest

from routers import scheduler_admin


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updated = []

    def find(self, *_args, **_kwargs):
        return list(self.docs)

    def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return dict(doc)
        return None

    def update_one(self, query, update, upsert=False):
        self.updated.append({"query": query, "update": update, "upsert": upsert})
        return None


@pytest.mark.unit
def test_scheduler_jobs_use_runner_status(monkeypatch):
    now_ts = scheduler_admin.time.time()
    config = FakeCollection([
        {
            "job_id": "market_analysis_1130",
            "is_active": True,
            "updated_at": datetime(2026, 5, 17, 12, 0, 0),
        }
    ])
    status = FakeCollection([
        {
            "_id": "primary",
            "heartbeat_ts": now_ts,
            "heartbeat_at": datetime(2026, 5, 17, 12, 1, 0),
            "instance_id": "scheduler-1",
            "jobs": {
                "market_analysis_1130": {
                    "id": "market_analysis_1130",
                    "next_run_time": "2026-05-18T11:30:00+08:00",
                }
            },
        }
    ])
    monkeypatch.setattr(
        scheduler_admin,
        "get_db_collections",
        lambda: {"schedule_config": config, "scheduler_status": status},
    )

    result = scheduler_admin.get_scheduler_jobs(admin_user={"username": "admin"})
    row = next(item for item in result["data"] if item["job_id"] == "market_analysis_1130")

    assert row["is_running"] is True
    assert row["scheduler_online"] is True
    assert row["scheduler_instance"] == "scheduler-1"
    assert row["next_run_time"] == "2026-05-18T11:30:00+08:00"


@pytest.mark.unit
def test_update_scheduler_job_only_persists_config(monkeypatch):
    config = FakeCollection([
        {
            "job_id": "hot_stock_poll_dc",
            "type": "interval",
            "seconds": 1800,
            "is_active": False,
        }
    ])
    monkeypatch.setattr(
        scheduler_admin,
        "get_db_collections",
        lambda: {"schedule_config": config},
    )
    monkeypatch.setattr(scheduler_admin, "log_admin_action", lambda **_kwargs: None)

    result = scheduler_admin.update_scheduler_job(
        "hot_stock_poll_dc",
        {"is_active": True, "config": {"seconds": 900}},
        admin_user={"username": "admin"},
    )

    assert result["success"] is True
    assert config.updated
    update = config.updated[0]["update"]["$set"]
    assert update["is_active"] is True
    assert update["seconds"] == 900
