"""Live-signal permission is gated by the broker's QMT support flag."""

from __future__ import annotations

import pytest
from bson import ObjectId
from fastapi import HTTPException

from models.all_models import SecuritiesAccountCreate, SecuritiesAccountUpdate
from routers import securities_account as sa
from tests.unit.test_market_insight_engine import FakeCollection


class _InsertCollection(FakeCollection):
    def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = ObjectId()
        self.rows.append(doc)

        class _Result:
            inserted_id = doc["_id"]

        return _Result()


def _collections(brokers):
    return {
        "brokers": FakeCollection(brokers),
        "securities_accounts": _InsertCollection([]),
        "trader_accounts": FakeCollection([]),
        "trader_positions": FakeCollection([]),
    }


def _user():
    return {"id": "u1", "username": "blue"}


@pytest.mark.unit
def test_broker_options_expose_qmt_supported(monkeypatch):
    collections = _collections(
        [
            {"name": "国金证券", "enabled": True, "qmt_supported": True, "sort_order": 10},
            {"name": "财通证券", "enabled": True, "qmt_supported": False, "sort_order": 20},
        ]
    )
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)
    result = sa.list_broker_options(_user())
    assert result["success"] is True
    assert result["data"] == [
        {"name": "国金证券", "qmt_supported": True},
        {"name": "财通证券", "qmt_supported": False},
    ]


@pytest.mark.unit
def test_create_live_account_rejects_non_qmt_broker(monkeypatch):
    collections = _collections(
        [{"name": "财通证券", "enabled": True, "qmt_supported": False}]
    )
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        sa.create_account(
            SecuritiesAccountCreate(
                broker="财通证券",
                account_id="12345678",
                account_type="real",
                live_trading_enabled=True,
            ),
            _user(),
        )

    assert exc.value.status_code == 400
    assert "未接入 QMT" in exc.value.detail


@pytest.mark.unit
def test_create_live_account_allows_qmt_broker(monkeypatch):
    collections = _collections(
        [{"name": "国金证券", "enabled": True, "qmt_supported": True}]
    )
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)

    result = sa.create_account(
        SecuritiesAccountCreate(
            broker="国金证券",
            account_id="8885156549",
            account_type="real",
            live_trading_enabled=True,
        ),
        _user(),
    )

    assert result.account_id == "8885156549"
    saved = collections["securities_accounts"].rows[0]
    assert saved["broker"] == "国金证券"
    assert saved["live_trading_enabled"] is True


@pytest.mark.unit
def test_create_account_fills_executor_defaults(monkeypatch):
    collections = _collections(
        [{"name": "国金证券", "enabled": True, "qmt_supported": True}]
    )
    collections["qmt_clients"] = FakeCollection(
        [
            {
                "name": "国金证券-实盘",
                "broker": "国金证券",
                "mailbox_root": "/mnt/d/qmt/bqbox",
                "enabled": True,
            }
        ]
    )
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)

    sa.create_account(
        SecuritiesAccountCreate(
            broker="国金证券",
            account_id="8885156549",
            account_type="real",
            live_trading_enabled=False,
        ),
        _user(),
    )

    saved = collections["securities_accounts"].rows[0]
    assert saved["qmt_account_id"] == "8885156549"
    assert saved["trader_client_id"] == "quant-trader-8885156549"
    assert saved["trader_container_name"] == "quant-trader-8885156549"
    assert saved["auto_manage"] is False
    # Exactly one QMT client for this broker -> mailbox pre-filled.
    assert saved["qmt_mailbox_root"] == "/mnt/d/qmt/bqbox"


@pytest.mark.unit
def test_create_account_leaves_mailbox_blank_with_multiple_clients(monkeypatch):
    collections = _collections(
        [{"name": "国金证券", "enabled": True, "qmt_supported": True}]
    )
    collections["qmt_clients"] = FakeCollection(
        [
            {"name": "实盘", "broker": "国金证券", "mailbox_root": "/mnt/d/a", "enabled": True},
            {"name": "模拟", "broker": "国金证券", "mailbox_root": "/mnt/e/b", "enabled": True},
        ]
    )
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)

    sa.create_account(
        SecuritiesAccountCreate(
            broker="国金证券",
            account_id="8885156549",
            account_type="real",
            live_trading_enabled=False,
        ),
        _user(),
    )

    saved = collections["securities_accounts"].rows[0]
    assert "qmt_mailbox_root" not in saved


@pytest.mark.unit
def test_update_rejects_enabling_live_for_non_qmt_broker(monkeypatch):
    account_id = ObjectId()
    collections = {
        "brokers": FakeCollection(
            [{"name": "财通证券", "enabled": True, "qmt_supported": False}]
        ),
        "securities_accounts": FakeCollection(
            [
                {
                    "_id": account_id,
                    "user_id": "u1",
                    "broker": "财通证券",
                    "account_id": "12345678",
                    "account_type": "real",
                    "live_trading_enabled": False,
                }
            ]
        ),
    }
    monkeypatch.setattr(sa, "get_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        sa.update_account(
            str(account_id),
            SecuritiesAccountUpdate(live_trading_enabled=True),
            _user(),
        )

    assert exc.value.status_code == 400
    assert "未接入 QMT" in exc.value.detail
