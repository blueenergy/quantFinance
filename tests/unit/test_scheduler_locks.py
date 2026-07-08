from unittest.mock import Mock

import db
import scheduler


class _LockCollection:
    def __init__(self):
        self.calls = []

    def update_one(self, query, update, upsert=False):
        self.calls.append({"query": query, "update": update, "upsert": upsert})
        result = Mock()
        result.upserted_id = query["_id"]
        result.modified_count = 0
        return result


def test_scheduler_job_lock_sets_unique_index_fields(monkeypatch):
    locks = _LockCollection()
    monkeypatch.setattr(db, "get_db_collections", lambda: {"job_status": locks})

    assert scheduler._acquire_scheduler_job_lock("live_snapshot_execution_backfill", ttl_seconds=120)

    update = locks.calls[0]["update"]["$set"]
    assert update["job_id"] == "scheduler_lock:live_snapshot_execution_backfill"
    assert update["job_type"] == "scheduler_lock"
    assert update["date"] == "scheduler_lock:live_snapshot_execution_backfill"
