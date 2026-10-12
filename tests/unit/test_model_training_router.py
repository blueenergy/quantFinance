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

            def sort(self, *args, **kwargs):
                return self

            def limit(self, n):
                self._rows = self._rows[:n]
                return self

            def __iter__(self):
                return iter(self._rows)

        rows = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
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
