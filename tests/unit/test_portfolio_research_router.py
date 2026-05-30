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
                return

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                continue
            if value != expected:
                return False
        return True


def _collections():
    return {
        "portfolio_strategy_configs": _Collection(),
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


def test_publish_portfolio_research_result_creates_strategy(monkeypatch):
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
                "strategy_id": "research_strategy",
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
    strategy = collections["portfolio_strategy_configs"].find_one({"strategy_id": "research_strategy"})
    assert strategy["status"] == "enabled"
    assert strategy["source"] == "portfolio_research"
    assert strategy["research_job_id"] == "job-1"
    assert collections["portfolio_research_reviews"].docs[0]["decision"] == "published"


def test_publish_portfolio_research_result_rejects_duplicate_strategy(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_research, "get_trading_db_collections", lambda: collections)
    collections["portfolio_strategy_configs"].insert_one({"strategy_id": "research_strategy"})
    collections["portfolio_research_jobs"].insert_one({"job_id": "job-1", "user_id": "user-1", "status": "completed"})
    collections["portfolio_research_results"].insert_one(
        {
            "result_id": "result-1",
            "job_id": "job-1",
            "user_id": "user-1",
            "candidate_strategy_config": {"strategy_id": "research_strategy"},
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
