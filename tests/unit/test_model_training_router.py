import asyncio

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
