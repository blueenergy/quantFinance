"""Persist decision-point risk snapshots for portfolio plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from services.portfolio_symbol_risk import (
    build_llm_payload_from_ledger_findings,
    build_opportunity_payload_from_ledger_findings,
    get_symbol_opportunity_ledger,
    get_symbol_risk_ledger,
    opportunity_ledger_read_enabled,
)

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
    opportunity_grouped: Dict[str, List[Dict[str, Any]]] = {}
    if opportunity_ledger_read_enabled():
        opportunity_ledger = get_symbol_opportunity_ledger(analyzer_db)
        opportunity_grouped = opportunity_ledger.get_active_for_symbols(list(symbols))
    items: List[Dict[str, Any]] = []
    for symbol in symbols:
        sym = str(symbol or "").strip()
        if not sym:
            continue
        findings = grouped.get(sym) or []
        opportunity_findings = opportunity_grouped.get(sym) or []
        if not findings and not opportunity_findings:
            continue
        item = {"symbol": sym}
        if findings:
            item["llm"] = build_llm_payload_from_ledger_findings(findings)
            item["active_findings"] = findings
        if opportunity_findings:
            item["opportunity"] = build_opportunity_payload_from_ledger_findings(opportunity_findings)
            item["active_opportunity_findings"] = opportunity_findings
        items.append(item)
    doc = {
        "plan_id": plan_id,
        "user_id": user_id,
        "decision_point": decision_point,
        "captured_at": datetime.utcnow(),
        "items": items,
        "summary": {
            "symbol_count": len(items),
            "risk_symbol_count": sum(1 for item in items if item.get("active_findings")),
            "opportunity_symbol_count": sum(1 for item in items if item.get("active_opportunity_findings")),
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
