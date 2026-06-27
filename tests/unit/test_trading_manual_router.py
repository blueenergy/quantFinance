from bson import ObjectId

from routers import trading_manual


class _InsertResult:
    def __init__(self, inserted_id=None):
        self.inserted_id = inserted_id or ObjectId()


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query=None):
        for doc in self.docs:
            if self._matches(doc, query or {}):
                return dict(doc)
        return None

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _InsertResult()

    def _matches(self, doc, query):
        for key, expected in query.items():
            if doc.get(key) != expected:
                return False
        return True


def test_manual_create_signal_requires_explicit_account(monkeypatch):
    monkeypatch.setattr(
        trading_manual,
        "get_db_collections",
        lambda: {
            "trade_signals": _Collection(),
            "securities_accounts": _Collection(),
        },
    )

    try:
        trading_manual.manual_create_signal(
            {"symbol": "000001.SZ", "action": "buy", "size": 100},
            current_user={"id": "user-1"},
        )
    except trading_manual.HTTPException as exc:
        assert exc.status_code == 400
        assert "证券账户" in exc.detail
    else:
        raise AssertionError("expected manual create to require securities_account_id")


def test_manual_create_signal_uses_selected_account(monkeypatch):
    account_a = ObjectId()
    account_b = ObjectId()
    signals = _Collection()
    accounts = _Collection(
        [
            {
                "_id": account_a,
                "user_id": "user-1",
                "broker": "BrokerA",
                "account_id": "A123",
            },
            {
                "_id": account_b,
                "user_id": "user-1",
                "broker": "BrokerB",
                "account_id": "B456",
            },
        ]
    )
    monkeypatch.setattr(
        trading_manual,
        "get_db_collections",
        lambda: {
            "trade_signals": signals,
            "securities_accounts": accounts,
        },
    )

    response = trading_manual.manual_create_signal(
        {
            "symbol": "000001.SZ",
            "action": "buy",
            "size": 100,
            "securities_account_id": str(account_b),
        },
        current_user={"id": "user-1"},
    )

    signal = signals.docs[0]
    assert response["success"] is True
    assert signal["securities_account_id"] == str(account_b)
    assert signal["broker"] == "BrokerB"
    assert signal["account_id"] == "B456"
