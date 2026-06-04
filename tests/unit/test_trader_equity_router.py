from routers import trader


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

    response = trader.get_account_equity(
        securities_account_id="acct-1",
        start_date="2025-06-04",
        end_date=None,
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    assert response["data"] == [
        {"user_id": "user-1", "securities_account_id": "acct-1", "date": "20250604", "total_asset": 100000}
    ]
