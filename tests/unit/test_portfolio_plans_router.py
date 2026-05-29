import asyncio

import pytest

from routers import portfolio_plans


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
                if "$gte" in expected and not (value >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (value <= expected["$lte"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def _collections():
    return {
        "portfolio_strategy_configs": _Collection(),
        "portfolio_trade_plans": _Collection(
            [
                {"plan_id": "plan-user-1", "strategy_id": "s1", "user_id": "user-1", "base_date": "20250102", "status": "needs_review"},
                {"plan_id": "plan-user-2", "strategy_id": "s2", "user_id": "user-2", "base_date": "20250103", "status": "needs_review"},
            ]
        ),
        "portfolio_trade_plan_items": _Collection(),
        "portfolio_plan_reviews": _Collection(),
        "portfolio_paper_positions": _Collection(),
        "portfolio_paper_executions": _Collection(),
        "portfolio_paper_equity": _Collection(),
    }


def test_list_portfolio_plans_is_scoped_to_current_user(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.list_portfolio_plans(
            strategy_id=None,
            status=None,
            plan_type=None,
            start_date=None,
            end_date=None,
            limit=50,
            current_user={"id": "user-1"},
        )
    )

    assert [row["plan_id"] for row in response["data"]] == ["plan-user-1"]


def test_review_plan_rejects_cross_user_access(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._review_plan("plan-user-2", "approved", "ok", {"id": "user-1"})

    assert exc.value.status_code == 404
    assert not collections["portfolio_plan_reviews"].docs


def test_review_plan_writes_user_id(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = portfolio_plans._review_plan("plan-user-1", "approved", "ok", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_plan_reviews"].docs[0]["user_id"] == "user-1"
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"
