"""End-to-end readiness for live trading (permission + mapping + heartbeats)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.trading_readiness import (
    account_trading_readiness,
    require_trading_ready,
)
from tests.unit.test_market_insight_engine import FakeCollection

NOW = 1_000_000


def _account(**overrides):
    row = {
        "_id": "sec-a",
        "user_id": "u1",
        "live_trading_enabled": True,
        "qmt_account_id": "8885156549",
        "qmt_mailbox_root": "/mnt/d/qmt/bqbox",
        "trader_client_id": "quant-trader-live",
    }
    row.update(overrides)
    return row


def _collections(*, worker_age=30, sync_age=60, worker=True, account=True):
    return {
        "worker_status": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "worker_id": "quantTrader:sec-a",
                    "last_seen_at": NOW - worker_age,
                }
            ]
            if worker
            else []
        ),
        "trader_accounts": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "securities_account_id": "sec-a",
                    "synced_at": NOW - sync_age,
                }
            ]
            if account
            else []
        ),
    }


@pytest.mark.unit
def test_not_live_is_not_ready():
    result = account_trading_readiness(
        _collections(), _account(live_trading_enabled=False), now_ts=NOW
    )
    assert result["trading_ready"] is False
    assert result["trading_status"] == "not_live"


@pytest.mark.unit
def test_missing_mapping_blocks_readiness():
    result = account_trading_readiness(
        _collections(), _account(qmt_mailbox_root=None), now_ts=NOW
    )
    assert result["trading_ready"] is False
    assert result["trading_status"] == "no_mapping"


@pytest.mark.unit
def test_missing_instance_blocks_readiness():
    result = account_trading_readiness(
        _collections(worker=False), _account(), now_ts=NOW
    )
    assert result["trading_ready"] is False
    assert result["trading_status"] == "instance_down"


@pytest.mark.unit
def test_stale_sync_blocks_readiness():
    result = account_trading_readiness(
        _collections(sync_age=600), _account(), now_ts=NOW
    )
    assert result["trading_ready"] is False
    assert result["trading_status"] == "sync_stale"


@pytest.mark.unit
def test_ready_when_permission_mapping_instance_and_sync_are_fresh():
    result = account_trading_readiness(_collections(), _account(), now_ts=NOW)
    assert result["trading_ready"] is True
    assert result["trading_status"] == "ready"
    assert result["mapping_complete"] is True


@pytest.mark.unit
def test_require_raises_with_reason():
    with pytest.raises(HTTPException) as exc:
        require_trading_ready(_collections(), _account(qmt_account_id=None))
    assert exc.value.status_code == 400
    assert "未就绪" in exc.value.detail
