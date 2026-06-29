from bson import ObjectId

from routers import securities_account
from models.all_models import SecuritiesAccountCreate, SecuritiesAccountUpdate


class _Result:
    def __init__(self, inserted_id=None, matched_count=0):
        self.inserted_id = inserted_id
        self.matched_count = matched_count
        self.deleted_count = 0


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None):
        return [doc for doc in self.docs if self._matches(doc, query or {})]

    def find_one(self, query=None):
        rows = self.find(query or {})
        return rows[0] if rows else None

    def count_documents(self, query=None):
        return len(self.find(query or {}))

    def insert_one(self, doc):
        inserted_id = ObjectId()
        row = dict(doc)
        row["_id"] = inserted_id
        self.docs.append(row)
        return _Result(inserted_id=inserted_id)

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return _Result(matched_count=1)
        return _Result(matched_count=0)

    def _matches(self, doc, query):
        for key, expected in query.items():
            if doc.get(key) != expected:
                return False
        return True


def test_create_simulated_account_forces_live_trading_off(monkeypatch):
    accounts = _Collection()
    trader_accounts = _Collection()
    monkeypatch.setattr(
        securities_account,
        "get_db_collections",
        lambda: {"securities_accounts": accounts, "trader_accounts": trader_accounts},
    )

    response = securities_account.create_account(
        SecuritiesAccountCreate(
            account_type="simulated",
            initial_cash=1000000,
            live_trading_enabled=True,
            risk_limits={"max_order_amount": 1000},
        ),
        current_user={"id": "user-1", "username": "alice"},
    )

    saved = accounts.docs[0]
    assert response.live_trading_enabled is False
    assert saved["live_trading_enabled"] is False
    assert saved["risk_limits"] == {"max_order_amount": 1000.0}


def test_create_allows_multiple_distinct_accounts_for_user(monkeypatch):
    accounts = _Collection(
        [
            {
                "_id": ObjectId(),
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(
        securities_account,
        "get_db_collections",
        lambda: {"securities_accounts": accounts},
    )

    response = securities_account.create_account(
        SecuritiesAccountCreate(
            account_type="real",
            broker="miniQMT",
            account_id="B456",
        ),
        current_user={"id": "user-1", "username": "alice"},
    )

    assert response.account_id == "B456"
    assert len(accounts.docs) == 2


def test_create_rejects_duplicate_account_for_user(monkeypatch):
    accounts = _Collection(
        [
            {
                "_id": ObjectId(),
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(
        securities_account,
        "get_db_collections",
        lambda: {"securities_accounts": accounts},
    )

    try:
        securities_account.create_account(
            SecuritiesAccountCreate(
                account_type="real",
                broker="miniQMT",
                account_id="A123",
            ),
            current_user={"id": "user-1", "username": "alice"},
        )
    except securities_account.HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("expected duplicate account to be rejected")


def test_update_real_account_saves_live_switch_and_risk_limits(monkeypatch):
    account_id = ObjectId()
    accounts = _Collection(
        [
            {
                "_id": account_id,
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(securities_account, "get_db_collections", lambda: {"securities_accounts": accounts})

    response = securities_account.update_account(
        str(account_id),
        SecuritiesAccountUpdate(
            live_trading_enabled=True,
            risk_limits={
                "max_order_amount": 5000,
                "max_daily_amount": "20000",
                "max_position_pct": 20,
                "ignored": 1,
            },
        ),
        current_user={"id": "user-1"},
    )

    saved = accounts.docs[0]
    assert response == {"success": True}
    assert saved["live_trading_enabled"] is True
    assert saved["risk_limits"] == {
        "max_order_amount": 5000.0,
        "max_daily_amount": 20000.0,
        "max_position_pct": 20.0,
    }


def test_update_simulated_account_forces_live_switch_off(monkeypatch):
    account_id = ObjectId()
    accounts = _Collection(
        [
            {
                "_id": account_id,
                "user_id": "user-1",
                "broker": "模拟交易",
                "account_id": "sim_1",
                "account_type": "simulated",
                "live_trading_enabled": True,
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(securities_account, "get_db_collections", lambda: {"securities_accounts": accounts})

    securities_account.update_account(
        str(account_id),
        SecuritiesAccountUpdate(live_trading_enabled=True),
        current_user={"id": "user-1"},
    )

    assert accounts.docs[0]["live_trading_enabled"] is False


def test_update_account_rejects_negative_risk_limits(monkeypatch):
    account_id = ObjectId()
    accounts = _Collection(
        [
            {
                "_id": account_id,
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(securities_account, "get_db_collections", lambda: {"securities_accounts": accounts})

    try:
        securities_account.update_account(
            str(account_id),
            SecuritiesAccountUpdate(risk_limits={"max_order_amount": -1}),
            current_user={"id": "user-1"},
        )
    except securities_account.HTTPException as exc:
        assert exc.status_code == 400
        assert "max_order_amount" in exc.detail
    else:
        raise AssertionError("expected negative risk limit to be rejected")


def test_update_account_saves_max_slippage_bps(monkeypatch):
    account_id = ObjectId()
    accounts = _Collection(
        [
            {
                "_id": account_id,
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(securities_account, "get_db_collections", lambda: {"securities_accounts": accounts})

    securities_account.update_account(
        str(account_id),
        SecuritiesAccountUpdate(risk_limits={"max_slippage_bps": 50}),
        current_user={"id": "user-1"},
    )

    assert accounts.docs[0]["risk_limits"] == {"max_slippage_bps": 50}


def test_update_account_rejects_invalid_max_slippage_bps(monkeypatch):
    account_id = ObjectId()
    accounts = _Collection(
        [
            {
                "_id": account_id,
                "user_id": "user-1",
                "broker": "miniQMT",
                "account_id": "A123",
                "account_type": "real",
                "created_at": securities_account.datetime.utcnow(),
            }
        ]
    )
    monkeypatch.setattr(securities_account, "get_db_collections", lambda: {"securities_accounts": accounts})

    try:
        securities_account.update_account(
            str(account_id),
            SecuritiesAccountUpdate(risk_limits={"max_slippage_bps": 2000}),
            current_user={"id": "user-1"},
        )
    except securities_account.HTTPException as exc:
        assert exc.status_code == 400
        assert "max_slippage_bps" in exc.detail
    else:
        raise AssertionError("expected oversized max_slippage_bps to be rejected")
