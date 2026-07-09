"""Read-model helpers for the global symbol/industry risk finding ledger."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stock_data_access.symbol_risk import (
    ENGINE_VERSION_LLM,
    SymbolRiskLedgerAccess,
)
from stock_data_access.symbol_opportunity import SymbolOpportunityLedgerAccess
from stock_data_access.symbol_signal_review import SymbolLlmSignalReviewAccess

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def legacy_ledger_read_enabled() -> bool:
    """During bake, fall back to plan-scoped ``portfolio_risk_llm`` when ledger is empty."""
    return os.getenv("SYMBOL_RISK_LEDGER_LEGACY_READ", "true").lower() in {"1", "true", "yes"}


def opportunity_ledger_read_enabled() -> bool:
    return os.getenv("OPPORTUNITY_LEDGER_READ_ENABLED", "true").lower() in {"1", "true", "yes"}


def _max_severity(values: Sequence[str]) -> str:
    result = "none"
    for value in values:
        sev = str(value or "none").strip().lower()
        if sev not in _SEVERITY_ORDER:
            sev = "none"
        if _SEVERITY_ORDER[sev] > _SEVERITY_ORDER[result]:
            result = sev
    return result


def get_symbol_risk_ledger(db=None) -> SymbolRiskLedgerAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolRiskLedgerAccess(db=db)


def get_symbol_opportunity_ledger(db=None) -> SymbolOpportunityLedgerAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolOpportunityLedgerAccess(db=db)


def get_symbol_signal_reviews(db=None) -> SymbolLlmSignalReviewAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolLlmSignalReviewAccess(db=db)


def _dt_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _ledger_finding_to_display(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "finding_key": row.get("finding_key"),
        "category": row.get("category"),
        "category_raw": row.get("category_raw"),
        "subject": row.get("subject"),
        "severity": row.get("severity"),
        "summary": row.get("summary"),
        "detail": row.get("detail"),
        "resolution_mode": row.get("resolution_mode"),
        "expires_as_of": row.get("expires_as_of"),
        "discovered_by": row.get("discovered_by"),
        "first_detected_as_of": row.get("first_detected_as_of"),
        "first_detected_at": _dt_to_iso(row.get("first_detected_at")),
        "last_confirmed_as_of": row.get("last_confirmed_as_of"),
        "last_confirmed_at": _dt_to_iso(row.get("last_confirmed_at")),
        "suggested_resolution": row.get("suggested_resolution"),
        "evidence": row.get("evidence") or [],
    }


def _opportunity_finding_to_display(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "finding_key": row.get("finding_key"),
        "category": row.get("category"),
        "category_raw": row.get("category_raw"),
        "subject": row.get("subject"),
        "strength": row.get("strength"),
        "summary": row.get("summary"),
        "detail": row.get("detail"),
        "resolution_mode": row.get("resolution_mode"),
        "expires_as_of": row.get("expires_as_of"),
        "discovered_by": row.get("discovered_by"),
        "first_detected_as_of": row.get("first_detected_as_of"),
        "first_detected_at": _dt_to_iso(row.get("first_detected_at")),
        "last_confirmed_as_of": row.get("last_confirmed_as_of"),
        "last_confirmed_at": _dt_to_iso(row.get("last_confirmed_at")),
        "suggested_closure": row.get("suggested_closure"),
        "evidence": row.get("evidence") or [],
    }


def build_llm_payload_from_ledger_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape active ledger findings into ``ai_risk.llm`` for the dashboard."""
    display = [_ledger_finding_to_display(row) for row in findings]
    severities = [str(row.get("severity") or "none") for row in display]
    summaries = [str(row.get("summary") or "").strip() for row in display if row.get("summary")]
    return {
        "engine_version": ENGINE_VERSION_LLM,
        "severity": _max_severity(severities) if display else "none",
        "summary": "；".join(summaries[:3]) if summaries else "未发现明确 LLM 事件风险",
        "findings": display,
        "source": "ledger",
        "finding_count": len(display),
        "has_suggested_resolution": any(row.get("suggested_resolution") for row in display),
    }


def build_opportunity_payload_from_ledger_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    display = [_opportunity_finding_to_display(row) for row in findings]
    strengths = [str(row.get("strength") or "none") for row in display]
    summaries = [str(row.get("summary") or "").strip() for row in display if row.get("summary")]
    return {
        "engine_version": ENGINE_VERSION_LLM,
        "strength": _max_severity(strengths) if display else "none",
        "summary": "；".join(summaries[:3]) if summaries else "未发现明确 LLM 机会",
        "findings": display,
        "source": "ledger",
        "finding_count": len(display),
        "has_suggested_closure": any(row.get("suggested_closure") for row in display),
    }


def attach_ledger_llm_to_items(
    items: List[Dict[str, Any]],
    grouped_findings: Dict[str, List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], int]:
    enriched: List[Dict[str, Any]] = []
    attached = 0
    for item in items:
        symbol = str(item.get("symbol") or "")
        findings = grouped_findings.get(symbol) or []
        if not findings:
            enriched.append(item)
            continue
        risk = dict(item.get("ai_risk") or {})
        risk["llm"] = build_llm_payload_from_ledger_findings(findings)
        enriched.append({**item, "ai_risk": risk})
        attached += 1
    return enriched, attached


def attach_ledger_opportunity_to_items(
    items: List[Dict[str, Any]],
    grouped_findings: Dict[str, List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], int]:
    enriched: List[Dict[str, Any]] = []
    attached = 0
    for item in items:
        symbol = str(item.get("symbol") or "")
        findings = grouped_findings.get(symbol) or []
        if not findings:
            enriched.append(item)
            continue
        opportunity = dict(item.get("ai_opportunity") or {})
        opportunity["llm"] = build_opportunity_payload_from_ledger_findings(findings)
        enriched.append({**item, "ai_opportunity": opportunity})
        attached += 1
    return enriched, attached


def build_ledger_llm_summary(
    *,
    grouped_findings: Dict[str, List[Dict[str, Any]]],
    industry_grouped: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    symbol_count = sum(1 for rows in grouped_findings.values() if rows)
    industries_payload = []
    for industry, rows in sorted((industry_grouped or {}).items()):
        if not rows:
            continue
        payload = build_llm_payload_from_ledger_findings(rows)
        industries_payload.append(
            {
                "industry": industry,
                "severity": payload.get("severity"),
                "summary": payload.get("summary"),
                "findings": payload.get("findings") or [],
            }
        )
    return {
        "source": "ledger",
        "status": "completed",
        "symbol_count": symbol_count,
        "industry_count": len(industries_payload),
        "industries": industries_payload,
    }


def build_ledger_opportunity_summary(
    *,
    grouped_findings: Dict[str, List[Dict[str, Any]]],
    industry_grouped: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    symbol_count = sum(1 for rows in grouped_findings.values() if rows)
    industries_payload = []
    for industry, rows in sorted((industry_grouped or {}).items()):
        if not rows:
            continue
        payload = build_opportunity_payload_from_ledger_findings(rows)
        industries_payload.append(
            {
                "industry": industry,
                "strength": payload.get("strength"),
                "summary": payload.get("summary"),
                "findings": payload.get("findings") or [],
            }
        )
    return {
        "source": "ledger",
        "status": "completed",
        "symbol_count": symbol_count,
        "industry_count": len(industries_payload),
        "industries": industries_payload,
    }


def symbols_with_ledger_coverage(symbols: Sequence[str], db=None) -> bool:
    ledger = get_symbol_risk_ledger(db)
    grouped = ledger.get_active_for_symbols(symbols)
    return any(grouped.get(str(sym)) for sym in symbols if sym)


def _parse_utc_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def filter_items_for_llm_enqueue(
    items: Sequence[Dict[str, Any]],
    *,
    force: bool = False,
    cooldown_hours: Optional[int] = None,
    db=None,
) -> List[Dict[str, Any]]:
    """Return plan/bench items that should be enqueued for LLM reassessment."""
    actionable = [item for item in items if item.get("symbol")]
    if force or not actionable:
        return list(actionable)

    hours = cooldown_hours
    if hours is None:
        hours = int(os.getenv("SYMBOL_LLM_REVIEW_COOLDOWN_HOURS", "24"))
    symbols = [str(item.get("symbol")) for item in actionable]
    reviews = get_symbol_signal_reviews(db).get_latest_reviews(symbols)
    legacy_grouped = get_symbol_risk_ledger(db).get_active_for_symbols(symbols)
    now = datetime.utcnow()
    selected: List[Dict[str, Any]] = []

    for item in actionable:
        sym = str(item.get("symbol"))
        review = reviews.get(sym) or {}
        reviewed_at = _parse_utc_datetime(review.get("reviewed_at"))
        if reviewed_at is not None:
            if now - reviewed_at >= timedelta(hours=hours):
                selected.append(item)
            continue

        # Migration fallback for ledgers written before review-stamps existed.
        findings = legacy_grouped.get(sym) or []
        if not findings:
            selected.append(item)
            continue
        confirmed_times = [
            _parse_utc_datetime(row.get("last_confirmed_at"))
            for row in findings
            if row.get("last_confirmed_at")
        ]
        confirmed_times = [dt for dt in confirmed_times if dt is not None]
        latest = max(confirmed_times) if confirmed_times else None
        if latest is None:
            selected.append(item)
            continue
        if now - latest >= timedelta(hours=hours):
            selected.append(item)
    return selected
