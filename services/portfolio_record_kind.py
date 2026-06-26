"""Authoritative record-kind taxonomy for ``portfolio_trade_plans``.

The ``portfolio_trade_plans`` collection historically stored three
semantically distinct entities together, with the "kind" only inferable
from a scatter of implicit fields (``plan_type`` / ``origin`` /
``manual_action`` / ``is_rebalance_day`` / the ``_extman_`` id suffix),
recomputed independently in the router, the worker and the frontend.

``record_kind`` is the single authoritative type field. It is stamped at
write time and trusted on read. The three kinds are:

- ``trade_plan``    -- an executable / reviewable rebalance decision
                       (cadence plans *and* manual live/paper rebalances).
                       Only this kind participates in the plan lifecycle
                       state machine.
- ``observation``   -- a daily monitor snapshot. Never executed, never
                       reviewed, not part of the state machine.
- ``ledger_event``  -- an external manual broker-side fact already settled
                       outside the system. Not executable.

``origin`` (``strategy`` / ``manual``) is an orthogonal *provenance*
dimension and must NOT be used to decide the kind: a manual rebalance is
still a ``trade_plan``.

Classification priority (validated against real data in two environments,
50 records total) -- order matters because legacy external-manual ledger
rows carry ``plan_type == "rebalance"``::

    1. manual_action in {external_manual, external_liquidation} -> ledger_event
    2. plan_type == "monitor"                                   -> observation
    3. plan_type == "rebalance"                                 -> trade_plan
    4. otherwise                                                -> ValueError
"""

from __future__ import annotations

from typing import Any, Mapping

RECORD_KIND_TRADE_PLAN = "trade_plan"
RECORD_KIND_OBSERVATION = "observation"
RECORD_KIND_LEDGER_EVENT = "ledger_event"

VALID_RECORD_KINDS = frozenset(
    {
        RECORD_KIND_TRADE_PLAN,
        RECORD_KIND_OBSERVATION,
        RECORD_KIND_LEDGER_EVENT,
    }
)

# manual_action values that denote an already-settled external broker fact.
LEDGER_MANUAL_ACTIONS = frozenset({"external_manual", "external_liquidation"})


def classify_record(doc: Mapping[str, Any]) -> str:
    """Derive the authoritative ``record_kind`` for a plan-like document.

    Pure function: depends only on ``doc``. Raises ``ValueError`` for
    documents that match none of the known shapes, so dirty data fails
    loud instead of being silently mis-bucketed.
    """
    manual_action = doc.get("manual_action")
    if manual_action in LEDGER_MANUAL_ACTIONS:
        return RECORD_KIND_LEDGER_EVENT

    plan_type = doc.get("plan_type")
    if plan_type == "monitor":
        return RECORD_KIND_OBSERVATION
    if plan_type == "rebalance":
        return RECORD_KIND_TRADE_PLAN

    # Legacy ledger rows were written with plan_type == "manual"; treat any
    # remaining "manual" plan_type as a ledger event for completeness.
    if plan_type == "manual":
        return RECORD_KIND_LEDGER_EVENT

    raise ValueError(
        "cannot classify record_kind: "
        f"plan_id={doc.get('plan_id')!r} plan_type={plan_type!r} "
        f"manual_action={manual_action!r}"
    )


def require_record_kind(doc: Mapping[str, Any]) -> str:
    """Return the stamped ``record_kind``, failing loud if missing/invalid.

    Use on read paths after the one-time normalization migration: every
    document must carry a valid ``record_kind``; absence indicates a write
    path that forgot to stamp it (a bug) rather than something to paper
    over with a fallback.
    """
    kind = doc.get("record_kind")
    if kind not in VALID_RECORD_KINDS:
        raise ValueError(
            "missing/invalid record_kind: "
            f"plan_id={doc.get('plan_id')!r} record_kind={kind!r}"
        )
    return kind
