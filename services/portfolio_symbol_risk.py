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
from utils.symbol_utils import normalize_symbol

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def legacy_ledger_read_enabled() -> bool:
    """Opt-in fallback to archived plan-scoped LLM results."""
    return os.getenv("SYMBOL_RISK_LEDGER_LEGACY_READ", "false").lower() in {"1", "true", "yes"}


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


def _review_to_display(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "checked_at": _dt_to_iso(row.get("checked_at")),
        "analyzed_at": _dt_to_iso(row.get("analyzed_at")),
        "last_run_status": row.get("last_run_status") or ("analyzed" if row.get("analyzed_at") else None),
        "evidence_count": row.get("evidence_count"),
        "latest_evidence_at": _dt_to_iso(row.get("latest_evidence_at")),
        "prompt_version": row.get("prompt_version"),
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


def attach_signal_reviews_to_items(
    items: List[Dict[str, Any]],
    reviews_by_symbol: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "")
        review = reviews_by_symbol.get(symbol)
        if not review:
            enriched.append(item)
            continue
        enriched.append({**item, "signal_review": _review_to_display(review)})
    return enriched


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


def _dedupe_active_findings(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for row in rows:
        key = str(row.get("finding_key") or row.get("summary") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _symbol_candidates_for_ledger(symbol: str) -> List[str]:
    from routers.stock import _symbol_candidates

    canonical = normalize_symbol(symbol)
    return list(dict.fromkeys([canonical, *_symbol_candidates(symbol)]))


def build_workbench_swot_section(
    symbol: str,
    db=None,
    *,
    industry: Optional[str] = None,
) -> Dict[str, Any]:
    """Canonical per-symbol SWOT read model for the stock workbench."""
    candidates = _symbol_candidates_for_ledger(symbol)
    canonical = candidates[0] if candidates else str(symbol or "").strip().upper()
    industry_name = str(industry or "").strip()

    risk_rows: List[Dict[str, Any]] = []
    opportunity_rows: List[Dict[str, Any]] = []
    risk_ledger = get_symbol_risk_ledger(db)
    grouped_risks = risk_ledger.get_active_for_symbols(candidates)
    for cand in candidates:
        risk_rows.extend(grouped_risks.get(cand, []))
    opp_ledger = None
    if opportunity_ledger_read_enabled():
        opp_ledger = get_symbol_opportunity_ledger(db)
        grouped_opportunities = opp_ledger.get_active_for_symbols(candidates)
        for cand in candidates:
            opportunity_rows.extend(grouped_opportunities.get(cand, []))

    risk_rows = _dedupe_active_findings(risk_rows)
    opportunity_rows = _dedupe_active_findings(opportunity_rows)

    industry_risk_rows: List[Dict[str, Any]] = []
    industry_opportunity_rows: List[Dict[str, Any]] = []
    if industry_name:
        industry_risk_rows = _dedupe_active_findings(
            risk_ledger.get_active_for_industries([industry_name]).get(industry_name, [])
        )
        if opp_ledger is not None:
            industry_opportunity_rows = _dedupe_active_findings(
                opp_ledger.get_active_for_industries([industry_name]).get(industry_name, [])
            )

    reviews = get_symbol_signal_reviews(db).get_latest_reviews(candidates)
    signal_review = None
    for cand in candidates:
        row = reviews.get(cand)
        if row:
            signal_review = _review_to_display(row)
            break

    threat = build_llm_payload_from_ledger_findings(risk_rows)
    opportunity = build_opportunity_payload_from_ledger_findings(opportunity_rows)
    industry_threat = build_llm_payload_from_ledger_findings(industry_risk_rows)
    industry_opportunity = build_opportunity_payload_from_ledger_findings(industry_opportunity_rows)
    empty_quadrant = {"status": "planned", "summary": "", "findings": []}
    found = bool(risk_rows or opportunity_rows or signal_review)
    latest_as_of = None
    for row in risk_rows + opportunity_rows:
        for key in ("last_confirmed_at", "first_detected_at"):
            value = row.get(key)
            if value and (latest_as_of is None or str(value) > str(latest_as_of)):
                latest_as_of = value

    return {
        "symbol": canonical,
        "signal_review": signal_review,
        "swot": {
            "strength": dict(empty_quadrant),
            "weakness": dict(empty_quadrant),
            "opportunity": opportunity,
            "threat": threat,
        },
        "industry_signal_reference": {
            "industry": industry_name,
            "opportunity": industry_opportunity,
            "threat": industry_threat,
            "data_status": {
                "found": bool(industry_risk_rows or industry_opportunity_rows),
                "threat_count": len(industry_threat.get("findings") or []),
                "opportunity_count": len(industry_opportunity.get("findings") or []),
                "source": "quant_analyzer.industry_risk_findings + industry_opportunity_findings",
            },
        },
        "data_status": {
            "found": found,
            "as_of": _dt_to_iso(latest_as_of) if latest_as_of else (signal_review or {}).get("analyzed_at"),
            "expected_freshness": "随组合 AI 风险/机会分析更新",
            "source": "quant_analyzer.symbol_risk_findings + symbol_opportunity_findings",
            "threat_count": len(threat.get("findings") or []),
            "opportunity_count": len(opportunity.get("findings") or []),
        },
    }


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
    """Return plan/bench items to enqueue.

    Fingerprint-aware skip now happens in the analyzer worker after evidence is
    collected.  Keeping a time-only gate here would miss same-day announcements,
    so this layer only filters invalid rows.
    """
    actionable = [item for item in items if item.get("symbol")]
    return list(actionable)
