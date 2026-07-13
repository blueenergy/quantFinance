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
from stock_data_access.symbol_signal_review import SymbolSignalReviewAccess
from stock_data_access.symbol_strength import SymbolStrengthLedgerAccess
from stock_data_access.symbol_weakness import SymbolWeaknessLedgerAccess
from utils.symbol_utils import normalize_symbol

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def legacy_ledger_read_enabled() -> bool:
    """Opt-in fallback to archived plan-scoped LLM results."""
    return os.getenv("SYMBOL_RISK_LEDGER_LEGACY_READ", "false").lower() in {"1", "true", "yes"}


def opportunity_ledger_read_enabled() -> bool:
    return os.getenv("OPPORTUNITY_LEDGER_READ_ENABLED", "true").lower() in {"1", "true", "yes"}


def internal_signal_enabled() -> bool:
    return os.getenv("INTERNAL_SIGNAL_ENABLED", "true").lower() in {"1", "true", "yes"}


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


def get_symbol_signal_reviews(db=None) -> SymbolSignalReviewAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolSignalReviewAccess(db=db)


def get_symbol_strength_ledger(db=None) -> SymbolStrengthLedgerAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolStrengthLedgerAccess(db=db)


def get_symbol_weakness_ledger(db=None) -> SymbolWeaknessLedgerAccess:
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    return SymbolWeaknessLedgerAccess(db=db)


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
        "engine_type": row.get("engine_type"),
        "engine_version": row.get("engine_version"),
        "rule_config_hash": row.get("rule_config_hash"),
        "input_completeness": row.get("input_completeness"),
        "result_counts": row.get("result_counts") or {},
        "dimensions_reviewed": row.get("dimensions_reviewed") or [],
        "last_skip_reason": row.get("last_skip_reason"),
    }


def build_internal_payload_from_ledger_findings(
    findings: List[Dict[str, Any]],
    *,
    dimension: str,
    review: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    level_field = "strength" if dimension == "strength" else "severity"
    display = [
        {
            "finding_key": row.get("finding_key"),
            "rule_id": row.get("rule_id"),
            "category": row.get("category"),
            level_field: row.get(level_field),
            "summary": row.get("summary"),
            "detail": row.get("detail"),
            "discovered_by": row.get("discovered_by"),
            "engine_type": row.get("engine_type"),
            "engine_version": row.get("engine_version"),
            "rule_config_hash": row.get("rule_config_hash"),
            "evidence_version": row.get("evidence_version"),
            "first_detected_as_of": row.get("first_detected_as_of"),
            "first_detected_at": _dt_to_iso(row.get("first_detected_at")),
            "last_confirmed_as_of": row.get("last_confirmed_as_of"),
            "last_confirmed_at": _dt_to_iso(row.get("last_confirmed_at")),
            "evidence": row.get("evidence") or [],
            "lifecycle_policy": row.get("lifecycle_policy") or {},
            "rule_condition": row.get("rule_condition") or {},
        }
        for row in findings
    ]
    summaries = [
        str(row.get("summary") or "").strip()
        for row in display
        if row.get("summary")
    ]
    levels = [str(row.get(level_field) or "none") for row in display]
    analyzed = bool(review and review.get("analyzed_at"))
    return {
        "status": "completed" if findings or analyzed else "not_analyzed",
        level_field: _max_severity(levels) if display else "none",
        "summary": "；".join(summaries[:3]) if summaries else "",
        "findings": display,
        "source": "ledger",
        "finding_count": len(display),
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


def attach_ledger_internal_swot_to_items(
    items: List[Dict[str, Any]],
    grouped_strength: Dict[str, List[Dict[str, Any]]],
    grouped_weakness: Dict[str, List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], int]:
    enriched: List[Dict[str, Any]] = []
    attached = 0
    for item in items:
        symbol = str(item.get("symbol") or "")
        strength_rows = grouped_strength.get(symbol) or []
        weakness_rows = grouped_weakness.get(symbol) or []
        if not strength_rows and not weakness_rows:
            enriched.append(item)
            continue
        strength = build_internal_payload_from_ledger_findings(
            strength_rows,
            dimension="strength",
            review=None,
        )
        weakness = build_internal_payload_from_ledger_findings(
            weakness_rows,
            dimension="weakness",
            review=None,
        )
        enriched.append(
            {
                **item,
                "internal_swot": {
                    "strength": strength,
                    "weakness": weakness,
                },
            }
        )
        attached += 1
    return enriched, attached


def enrich_items_with_internal_swot(
    items: List[Dict[str, Any]],
    db=None,
) -> List[Dict[str, Any]]:
    if not internal_signal_enabled() or not items:
        return items
    symbols = [str(item.get("symbol") or "") for item in items if item.get("symbol")]
    if not symbols:
        return items
    if db is None:
        from db import get_analyzer_db

        db = get_analyzer_db()
    grouped_strength = get_symbol_strength_ledger(db).get_active_for_symbols(symbols)
    grouped_weakness = get_symbol_weakness_ledger(db).get_active_for_symbols(symbols)
    if not any(grouped_strength.values()) and not any(grouped_weakness.values()):
        return items
    enriched, _ = attach_ledger_internal_swot_to_items(
        items,
        grouped_strength,
        grouped_weakness,
    )
    return enriched


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

    strength_rows: List[Dict[str, Any]] = []
    weakness_rows: List[Dict[str, Any]] = []
    if internal_signal_enabled():
        grouped_strengths = get_symbol_strength_ledger(db).get_active_for_symbols(candidates)
        grouped_weaknesses = get_symbol_weakness_ledger(db).get_active_for_symbols(candidates)
        for cand in candidates:
            strength_rows.extend(grouped_strengths.get(cand, []))
            weakness_rows.extend(grouped_weaknesses.get(cand, []))
        strength_rows = _dedupe_active_findings(strength_rows)
        weakness_rows = _dedupe_active_findings(weakness_rows)

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

    review_access = get_symbol_signal_reviews(db)
    reviews = review_access.get_latest_reviews(candidates)
    internal_reviews = review_access.get_latest_reviews(
        candidates,
        review_scope="internal_fundamental",
    )
    signal_review = None
    internal_signal_review = None
    for cand in candidates:
        row = reviews.get(cand)
        if row:
            signal_review = _review_to_display(row)
            break
    for cand in candidates:
        row = internal_reviews.get(cand)
        if row:
            internal_signal_review = _review_to_display(row)
            break

    threat = build_llm_payload_from_ledger_findings(risk_rows)
    opportunity = build_opportunity_payload_from_ledger_findings(opportunity_rows)
    strength = build_internal_payload_from_ledger_findings(
        strength_rows,
        dimension="strength",
        review=internal_signal_review,
    )
    weakness = build_internal_payload_from_ledger_findings(
        weakness_rows,
        dimension="weakness",
        review=internal_signal_review,
    )
    industry_threat = build_llm_payload_from_ledger_findings(industry_risk_rows)
    industry_opportunity = build_opportunity_payload_from_ledger_findings(industry_opportunity_rows)
    found = bool(
        risk_rows
        or opportunity_rows
        or strength_rows
        or weakness_rows
        or signal_review
        or internal_signal_review
    )
    latest_as_of = None
    for row in risk_rows + opportunity_rows + strength_rows + weakness_rows:
        for key in ("last_confirmed_at", "first_detected_at"):
            value = row.get(key)
            if value and (latest_as_of is None or str(value) > str(latest_as_of)):
                latest_as_of = value

    return {
        "symbol": canonical,
        "signal_review": signal_review,
        "internal_signal_review": internal_signal_review,
        "swot": {
            "strength": strength,
            "weakness": weakness,
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
            "as_of": (
                _dt_to_iso(latest_as_of)
                if latest_as_of
                else (internal_signal_review or signal_review or {}).get("analyzed_at")
            ),
            "expected_freshness": "O/T 随外部事件分析更新；S/W 随内部财务信号刷新",
            "source": (
                "quant_analyzer.symbol_risk_findings + symbol_opportunity_findings + "
                "symbol_strength_findings + symbol_weakness_findings"
            ),
            "strength_count": len(strength.get("findings") or []),
            "weakness_count": len(weakness.get("findings") or []),
            "threat_count": len(threat.get("findings") or []),
            "opportunity_count": len(opportunity.get("findings") or []),
            "internal_signal_enabled": internal_signal_enabled(),
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
