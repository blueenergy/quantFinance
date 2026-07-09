"""Persist decision-point risk snapshots for portfolio plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from services.portfolio_symbol_risk import build_llm_payload_from_ledger_findings, get_symbol_risk_ledger

SNAPSHOTS_COL = "portfolio_plan_risk_snapshots"


def capture_plan_risk_snapshot(
    collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    decision_point: str,
    symbols: Sequence[str],
    analyzer_db=None,
) -> Dict[str, Any]:
    """Freeze active ledger findings at a decision point (append-only)."""
    ledger = get_symbol_risk_ledger(analyzer_db)
    grouped = ledger.get_active_for_symbols(list(symbols))
    items: List[Dict[str, Any]] = []
    for symbol in symbols:
        sym = str(symbol or "").strip()
        if not sym:
            continue
        findings = grouped.get(sym) or []
        if not findings:
            continue
        items.append(
            {
                "symbol": sym,
                "llm": build_llm_payload_from_ledger_findings(findings),
                "active_findings": findings,
            }
        )
    doc = {
        "plan_id": plan_id,
        "user_id": user_id,
        "decision_point": decision_point,
        "captured_at": datetime.utcnow(),
        "items": items,
        "summary": {
            "symbol_count": len(items),
            "source": "ledger",
        },
    }
    collections[SNAPSHOTS_COL].insert_one(doc)
    return doc


def latest_plan_risk_snapshot(
    collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    return collections[SNAPSHOTS_COL].find_one(
        {"plan_id": plan_id, "user_id": user_id},
        sort=[("captured_at", -1)],
        projection={"_id": 0},
    )
