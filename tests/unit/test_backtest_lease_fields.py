"""Lease field defaults for backtest task queue workers."""

from services.backtest_batch_service import (
    LEASE_RESET_FIELDS,
    build_backtest_task_doc,
    lease_field_defaults,
)


def test_lease_field_defaults():
    defaults = lease_field_defaults()
    assert defaults["attempts"] == 0
    assert defaults["lease_token"] is None
    assert defaults["lease_expires_at"] is None
    assert defaults["heartbeat_at"] is None


def test_build_backtest_task_doc_includes_lease_fields():
    doc = build_backtest_task_doc(
        current_user={"id": "user-1"},
        symbol="000001.SZ",
        asset_type="stock",
        strategy_key="turtle",
        preset="turtle_conservative",
        strategy_params={},
        start_date="20230101",
        end_date="20231231",
        initial_cash=1000000.0,
    )
    assert doc["attempts"] == 0
    assert doc["lease_token"] is None
    assert doc["lease_expires_at"] is None
    assert doc["heartbeat_at"] is None


def test_lease_reset_fields_clears_owner_state():
    assert LEASE_RESET_FIELDS["lease_token"] is None
    assert LEASE_RESET_FIELDS["heartbeat_at"] is None
    assert LEASE_RESET_FIELDS["lease_expires_at"] is None
