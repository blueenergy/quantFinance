"""Single source for broker-synced sellable quantity.

``trader_positions`` is a periodic snapshot; several live paths used to each
derive "how many shares can I sell now" with different field fallbacks. Two of
those fallbacks were wrong in direction (``or`` truthiness lets zero pierce
through; missing sellable fields fell back to total position).

Phase 1 keeps legacy sizing for actual orders and surfaces strict vs legacy
side by side so we can tighten once real data shows it is safe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

# quantTrader execution_tracker writes ``can_use_volume``; secondary sync paths
# may write ``available_qty``. Order reflects write-path trust, not UI labels.
SELLABLE_FIELDS = ("can_use_volume", "available_qty", "available_shares", "available_volume")

TOTAL_POSITION_FIELDS = ("qty", "quantity", "volume", "shares", "current_volume", "total_volume")

LEGACY_MODE_ACCOUNT_FALLBACK = "account_fallback"
LEGACY_MODE_OR_CHAIN = "or_chain"
LEGACY_MODE_T0_CHAIN = "t0_chain"

# quantTrader sync_interval defaults to 60s; warn when the snapshot is much older.
STALE_SNAPSHOT_WARNING_SECONDS = 120.0


@dataclass(frozen=True)
class SellableQuantity:
    """Sellable shares from one broker position row."""

    shares: int
    legacy_shares: int
    source_field: str
    reserved: int
    snapshot_age: Optional[float]

    @property
    def diverges(self) -> bool:
        return self.shares != self.legacy_shares

    def observability(self) -> Dict[str, Any]:
        return {
            "sellable_shares_strict": self.shares,
            "sellable_shares_legacy": self.legacy_shares,
            "sellable_source_field": self.source_field,
            "sellable_snapshot_age_seconds": self.snapshot_age,
            "sellable_reserved": self.reserved,
            "sellable_diverges": self.diverges,
        }


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def account_position_qty(position: Mapping[str, Any]) -> int:
    """Total position size from a broker row (not sellable quantity)."""
    for field in TOTAL_POSITION_FIELDS:
        if field in position and position[field] is not None:
            qty = _to_int(position[field])
            if qty >= 0:
                return qty
    return 0


def _strict_raw(position: Mapping[str, Any]) -> Tuple[int, str]:
    """Strict sellable: first present sellable field wins; zero is valid."""
    for field in SELLABLE_FIELDS:
        if field in position and position[field] is not None:
            return max(0, _to_int(position[field])), field
    return 0, ""


def _legacy_account_fallback_raw(position: Mapping[str, Any]) -> int:
    """Matches ``_account_available_qty`` before centralization."""
    for field in SELLABLE_FIELDS:
        if position.get(field) is not None:
            return max(0, _to_int(position[field]))
    return account_position_qty(position)


def _legacy_or_chain_raw(position: Mapping[str, Any]) -> int:
    """Matches publish risk ``available_qty or can_use_volume or qty``."""
    return max(
        0,
        _to_int(
            position.get("available_qty")
            or position.get("can_use_volume")
            or position.get("qty")
            or 0
        ),
    )


def _legacy_t0_chain_raw(position: Mapping[str, Any]) -> int:
    """Matches T+0 ``available_qty`` default chain before centralization."""
    return max(
        0,
        _to_int(
            position.get(
                "available_qty",
                position.get("can_use_volume", position.get("qty", 0)),
            )
        ),
    )


def _legacy_raw(position: Mapping[str, Any], legacy_mode: str) -> int:
    if legacy_mode == LEGACY_MODE_OR_CHAIN:
        return _legacy_or_chain_raw(position)
    if legacy_mode == LEGACY_MODE_T0_CHAIN:
        return _legacy_t0_chain_raw(position)
    return _legacy_account_fallback_raw(position)


def _snapshot_age(position: Mapping[str, Any], *, now: Optional[float] = None) -> Optional[float]:
    synced_at = position.get("synced_at")
    if synced_at is None:
        return None
    try:
        age = float(now if now is not None else time.time()) - float(synced_at)
    except (TypeError, ValueError):
        return None
    return max(0.0, age)


def sellable_shares(
    position: Mapping[str, Any],
    *,
    reserved: int = 0,
    legacy_mode: str = LEGACY_MODE_ACCOUNT_FALLBACK,
    now: Optional[float] = None,
    log_context: Optional[str] = None,
) -> SellableQuantity:
    """Return strict and legacy sellable quantity for one position row.

    Callers must keep using ``legacy_shares`` for order sizing in phase 1.
    ``reserved`` subtracts in-flight sell commitments (T+0 yields to strategy).
    """
    reserved_qty = max(0, _to_int(reserved))
    strict_raw, source_field = _strict_raw(position)
    legacy_raw = _legacy_raw(position, legacy_mode)
    shares = max(0, strict_raw - reserved_qty)
    legacy_shares = max(0, legacy_raw - reserved_qty)
    snapshot_age = _snapshot_age(position, now=now)
    result = SellableQuantity(
        shares=shares,
        legacy_shares=legacy_shares,
        source_field=source_field,
        reserved=reserved_qty,
        snapshot_age=snapshot_age,
    )
    prefix = f"{log_context} " if log_context else ""
    if snapshot_age is not None and snapshot_age > STALE_SNAPSHOT_WARNING_SECONDS:
        log.warning(
            "%ssellable snapshot stale age=%.1fs symbol=%s account=%s",
            prefix,
            snapshot_age,
            position.get("symbol"),
            position.get("securities_account_id"),
        )
    if result.diverges:
        log.warning(
            "%ssellable quantity diverges strict=%s legacy=%s source_field=%r symbol=%s",
            prefix,
            result.shares,
            result.legacy_shares,
            result.source_field or None,
            position.get("symbol"),
        )
    return result


def merge_sellable_bucket(
    bucket: Dict[str, Any],
    sellable: SellableQuantity,
) -> None:
    """Accumulate sellable quantities into an account_positions_by_symbol bucket."""
    bucket["available"] = bucket.get("available", 0) + sellable.legacy_shares
    bucket["sellable_strict"] = bucket.get("sellable_strict", 0) + sellable.shares
    bucket["sellable_legacy"] = bucket.get("sellable_legacy", 0) + sellable.legacy_shares
    bucket["sellable_diverges"] = bool(bucket.get("sellable_diverges")) or sellable.diverges
    if sellable.source_field:
        bucket["sellable_source_field"] = sellable.source_field
    age = sellable.snapshot_age
    if age is not None:
        prior = bucket.get("sellable_snapshot_age")
        bucket["sellable_snapshot_age"] = age if prior is None else max(float(prior), age)
