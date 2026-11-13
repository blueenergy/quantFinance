import datetime as _dt
import time
from types import SimpleNamespace

from bson import ObjectId

from routers import trader
from services.live_equity import SHANGHAI_TZ


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))


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
        row = dict(doc)
        row.setdefault("_id", ObjectId())
        self.docs.append(row)
        return SimpleNamespace(inserted_id=row["_id"])

    def delete_one(self, query):
        for index, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[index]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$gte" in expected and value < expected["$gte"]:
                    return False
                if "$lte" in expected and value > expected["$lte"]:
                    return False
                continue
            if value != expected:
                return False
        return True


def test_get_account_equity_filters_account_and_dates(monkeypatch):
    collections = {
        "trader_account_equity": _Collection(
            [
                {"user_id": "user-1", "securities_account_id": "acct-1", "date": "20250603", "total_asset": 90000},
                {"user_id": "user-1", "securities_account_id": "acct-1", "date": "20250604", "total_asset": 100000},
                {"user_id": "user-1", "securities_account_id": "acct-2", "date": "20250604", "total_asset": 200000},
            ]
        )
    }
    monkeypatch.setattr(trader, "get_db_collections", lambda: collections)
    monkeypatch.setattr(
        trader,
        "get_data_db",
        lambda: {"trade_calendar": _Collection()},
    )

    response = trader.get_account_equity(
        securities_account_id="acct-1",
        start_date="2025-06-04",
        end_date=None,
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    assert len(response["data"]) == 1
    row = response["data"][0]
    assert row["date"] == "20250604"
    assert row["total_asset"] == 100000
    assert row["flow_adjusted_equity"] == 100000.0
    assert row["external_flow"] == 0.0
    assert row["daily_pnl"] is None


def _live_equity_collections():
    today = _dt.datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
    return (
        {
            "trader_account_equity": _Collection(
                [
                    {
                        "user_id": "user-1",
                        "securities_account_id": "acct-1",
                        "date": "20261106",
                        "total_asset": 90000,
                    }
                ]
            ),
            "trader_accounts": _Collection(
                [
                    {
                        "user_id": "user-1",
                        "securities_account_id": "acct-1",
                        "total_asset": 95000,
                        "cash": 10000,
                        "available_cash": 9000,
                        "market_value": 85000,
                        "synced_at": time.time() - 30,
                    }
                ]
            ),
        },
        {"trade_calendar": _Collection([{"exchange": "SSE", "trade_date": today, "is_open": 1}])},
        today,
    )


def test_get_account_equity_appends_live_intraday_point(monkeypatch):
    collections, data_collections, today = _live_equity_collections()
    monkeypatch.setattr(trader, "get_db_collections", lambda: collections)
    monkeypatch.setattr(trader, "get_data_db", lambda: data_collections)

    response = trader.get_account_equity(
        securities_account_id="acct-1",
        start_date=None,
        end_date=None,
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    assert response["data"][-1]["date"] == today
    assert response["data"][-1]["is_intraday"] is True
    assert response["data"][-1]["total_asset"] == 95000


def test_get_account_equity_skips_live_point_outside_date_range(monkeypatch):
    collections, data_collections, today = _live_equity_collections()
    monkeypatch.setattr(trader, "get_db_collections", lambda: collections)
    monkeypatch.setattr(trader, "get_data_db", lambda: data_collections)

    response = trader.get_account_equity(
        securities_account_id="acct-1",
        start_date=None,
        end_date="2026-11-06",
        current_user={"id": "user-1"},
    )

    assert all(row.get("is_intraday") is not True for row in response["data"])
    assert response["data"][-1]["date"] == "20261106"


def test_create_list_delete_cash_flow(monkeypatch):
    collections = {
        "trader_accounts": _Collection(
            [{"user_id": "user-1", "securities_account_id": "acct-1"}]
        ),
        "trader_cash_flows": _Collection(),
    }
    monkeypatch.setattr(trader, "get_db_collections", lambda: collections)

    created = trader.create_cash_flow(
        trader.CashFlowCreateRequest(
            securities_account_id="acct-1",
            date="2026-11-12",
            amount=30000,
            note="入金",
        ),
        current_user={"id": "user-1"},
    )
    assert created["success"] is True
    assert created["data"]["date"] == "20261112"
    assert created["data"]["amount"] == 30000
    assert created["data"]["source"] == "manual"

    listed = trader.list_cash_flows(
        securities_account_id="acct-1",
        start_date=None,
        end_date=None,
        current_user={"id": "user-1"},
    )
    assert len(listed["data"]) == 1
    assert listed["data"][0]["note"] == "入金"

    deleted = trader.delete_cash_flow(
        flow_id=created["data"]["_id"],
        current_user={"id": "user-1"},
    )
    assert deleted["success"] is True
    assert collections["trader_cash_flows"].docs == []
