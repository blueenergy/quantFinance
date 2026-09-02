import time

import pytest

from services.account_sellable import (
  LEGACY_MODE_ACCOUNT_FALLBACK,
  LEGACY_MODE_OR_CHAIN,
  LEGACY_MODE_T0_CHAIN,
  account_position_qty,
  sellable_shares,
)


@pytest.mark.unit
def test_strict_reads_can_use_volume():
  sq = sellable_shares({"can_use_volume": 400, "qty": 900})
  assert sq.shares == 400
  assert sq.legacy_shares == 400
  assert sq.source_field == "can_use_volume"
  assert not sq.diverges


@pytest.mark.unit
def test_strict_reads_available_qty_when_present():
  sq = sellable_shares({"available_qty": 250, "can_use_volume": 400})
  assert sq.shares == 400
  assert sq.source_field == "can_use_volume"


@pytest.mark.unit
def test_or_chain_legacy_pierces_zero_available_qty():
    position = {"available_qty": 0, "qty": 500}
    sq = sellable_shares(position, legacy_mode=LEGACY_MODE_OR_CHAIN)
    assert sq.shares == 0
    assert sq.legacy_shares == 500
    assert sq.source_field == "available_qty"
    assert sq.diverges


@pytest.mark.unit
def test_account_fallback_legacy_uses_total_when_fields_missing():
  position = {"qty": 800}
  sq = sellable_shares(position, legacy_mode=LEGACY_MODE_ACCOUNT_FALLBACK)
  assert sq.shares == 0
  assert sq.legacy_shares == 800
  assert sq.source_field == ""
  assert sq.diverges


@pytest.mark.unit
def test_reserved_subtracts_from_both_paths():
  sq = sellable_shares({"can_use_volume": 500}, reserved=300)
  assert sq.shares == 200
  assert sq.legacy_shares == 200
  assert sq.reserved == 300


@pytest.mark.unit
def test_t0_chain_prefers_available_qty_key():
  position = {"available_qty": 100, "can_use_volume": 400, "qty": 900}
  sq = sellable_shares(position, legacy_mode=LEGACY_MODE_T0_CHAIN)
  assert sq.legacy_shares == 100
  assert sq.shares == 400


@pytest.mark.unit
def test_snapshot_age_computed_from_synced_at():
  now = 1_700_000_000.0
  sq = sellable_shares({"can_use_volume": 100, "synced_at": now - 45}, now=now)
  assert sq.snapshot_age == pytest.approx(45.0)


@pytest.mark.unit
def test_account_position_qty_ignores_sellable_fields():
  assert account_position_qty({"available_qty": 0, "qty": 600}) == 600


@pytest.mark.unit
def test_observability_payload():
  sq = sellable_shares({"available_qty": 0, "qty": 100}, legacy_mode=LEGACY_MODE_OR_CHAIN)
  payload = sq.observability()
  assert payload["sellable_shares_strict"] == 0
  assert payload["sellable_shares_legacy"] == 100
  assert payload["sellable_diverges"] is True
