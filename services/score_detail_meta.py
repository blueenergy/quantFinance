"""Score detail provenance helpers for /stock-score-detail and workbench APIs.

Synthesizes ``score_meta`` from stored documents when the scoring pipeline has not
yet written an explicit block (details_schema_version 0 fallback).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SCORE_DIMENSION_KEYS = (
    "technical",
    "fundamental",
    "value",
    "growth",
    "money_flow",
    "cycle",
)

DEFAULT_LOOKAHEAD_RULE = "ann_date/f_ann_date <= score_date"

# History lists only plot score trends, so never pull the per-dimension *_details
# blobs — a full stock_scores document is orders of magnitude larger.
SCORE_HISTORY_PROJECTION: Dict[str, int] = {
    "_id": 0,
    "symbol": 1,
    "score_date": 1,
    "composite_score": 1,
    **{f"{key}_score": 1 for key in SCORE_DIMENSION_KEYS},
}


def _dimensions_computed(doc: Dict[str, Any]) -> List[str]:
    return [key for key in SCORE_DIMENSION_KEYS if doc.get(f"{key}_score") is not None]


def _infer_reduced_mode(doc: Dict[str, Any], dimensions: List[str]) -> bool:
    composite = doc.get("composite_score")
    if composite is None:
        return len(dimensions) < len(SCORE_DIMENSION_KEYS)
    return False


def build_score_meta(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return provenance metadata, preferring stored ``score_meta`` when present."""
    stored = doc.get("score_meta")
    dimensions = _dimensions_computed(doc)
    fallback: Dict[str, Any] = {
        "score_date": doc.get("score_date"),
        "algorithm_version": doc.get("algorithm_version") or "v0.1",
        "details_schema_version": 0,
        "lookahead_rule": DEFAULT_LOOKAHEAD_RULE,
        "reduced_mode": _infer_reduced_mode(doc, dimensions),
        "dimensions_computed": dimensions,
    }
    if not stored or not isinstance(stored, dict):
        return fallback
    merged = {**fallback, **stored}
    if merged.get("details_schema_version") is None:
        merged["details_schema_version"] = 0
    if not merged.get("dimensions_computed"):
        merged["dimensions_computed"] = dimensions
    return merged


def extract_express_flags(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Scan category details for express / provisional data markers."""
    if not details or not isinstance(details, dict):
        return {}
    flags: Dict[str, Any] = {}
    if details.get("express_source"):
        flags["express_source"] = details["express_source"]
    if details.get("数据来源"):
        flags["data_source"] = details["数据来源"]
    discount = details.get("express_discount")
    if discount is not None:
        try:
            flags["express_discount"] = float(discount)
        except (TypeError, ValueError):
            flags["express_discount"] = discount
    return flags


def get_strategy_recommendations(
    doc: Dict[str, Any],
    strategy: str = "balanced",
) -> Optional[Dict[str, Any]]:
    """Return strategy recommendations blob when stored on the score document."""
    strategy_scores = doc.get("strategy_scores") or {}
    if not isinstance(strategy_scores, dict):
        return None
    strat_obj = strategy_scores.get(strategy) or strategy_scores.get("balanced")
    if not isinstance(strat_obj, dict):
        return None
    recommendations = strat_obj.get("recommendations")
    if not isinstance(recommendations, dict):
        return None
    return {
        "strategy": strategy if strategy in strategy_scores else "balanced",
        "comp_score": strat_obj.get("comp_score"),
        "weight": strat_obj.get("weight") or strat_obj.get("weights"),
        **recommendations,
    }


def build_score_detail_payload(
    doc: Dict[str, Any],
    *,
    symbol: str,
    category: str,
    details: Any,
    weights: Optional[Dict[str, float]] = None,
    dimensions: Optional[List[Dict[str, Any]]] = None,
    strategy: str = "balanced",
) -> Dict[str, Any]:
    """Standard envelope for /stock-score-detail responses."""
    meta = build_score_meta(doc)
    express = extract_express_flags(details if isinstance(details, dict) else None)
    if express:
        meta = {**meta, "express": express}
    payload: Dict[str, Any] = {
        "symbol": symbol,
        "score_date": doc.get("score_date"),
        "category": category,
        "details": details,
        "meta": meta,
    }
    if weights is not None:
        payload["weights"] = weights
    if dimensions is not None:
        payload["dimensions"] = dimensions
    if category == "composite":
        recommendations = get_strategy_recommendations(doc, strategy=strategy)
        if recommendations:
            payload["recommendations"] = recommendations
    return payload
