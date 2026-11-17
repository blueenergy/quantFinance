"""Daily model-score panel: rank view of quant_ml.model_predictions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_db_collections, get_data_db, get_ml_db_collections, get_trading_db_collections
from services.model_scores_daily import (
    DEFAULT_LIMIT,
    DEFAULT_TOP_N,
    build_daily_model_scores,
    list_model_catalog,
    normalize_model_ids,
)

router = APIRouter(prefix="/model-scores", tags=["模型评分"])
logger = logging.getLogger(__name__)


def _watchlist_symbols(user_id: str) -> List[str]:
    if not user_id:
        return []
    try:
        doc = get_db_collections().get("user_watchlists").find_one(
            {"user_id": user_id}, {"_id": 0, "symbols": 1}
        )
    except Exception:
        logger.warning("watchlist lookup failed for %s", user_id, exc_info=True)
        return []
    return [str(symbol) for symbol in ((doc or {}).get("symbols") or []) if symbol]


def _position_symbols(user_id: str) -> List[str]:
    if not user_id:
        return []
    try:
        docs = get_db_collections().get("trader_positions").find(
            {"user_id": user_id, "qty": {"$gt": 0}},
            {"_id": 0, "symbol": 1},
        )
        return sorted({str(doc.get("symbol")) for doc in docs if doc.get("symbol")})
    except Exception:
        logger.warning("positions lookup failed for %s", user_id, exc_info=True)
        return []


@router.get("/models")
def list_model_score_models(
    current_user: dict = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Model ids in ``model_predictions`` with coverage and recent dates."""
    _ = current_user
    ml = get_ml_db_collections()
    models = list_model_catalog(
        ml["model_predictions"],
        shadow_profiles=ml.get("model_shadow_profiles"),
        activations=get_trading_db_collections().get("portfolio_strategy_activations"),
        registry=ml.get("models"),
    )
    return {"success": True, "data": {"models": models}}


@router.get("/daily")
def get_daily_model_scores(
    model_ids: str = Query(..., description="Comma-separated model ids (max 6, one universe)"),
    date: Optional[str] = Query(None, description="Trade date YYYYMMDD; default = latest of the first model"),
    top_n: int = Query(DEFAULT_TOP_N, ge=1, le=200, description="Top-N size used for overlap/disagreement stats"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=1000, description="Table rows returned (ranked by average rank)"),
    include_prev: bool = Query(True, description="Compute rank change vs each model's previous trade date"),
    current_user: dict = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Ranked cross-section for one trade date, optionally comparing models."""
    try:
        model_ids_parsed = normalize_model_ids(model_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not model_ids_parsed:
        raise HTTPException(status_code=400, detail="model_ids is required")

    user_id = str(current_user.get("id") or "")
    try:
        payload = build_daily_model_scores(
            get_ml_db_collections()["model_predictions"],
            get_data_db()["stock_info"],
            model_ids=model_ids_parsed,
            trade_date=date,
            top_n=top_n,
            limit=limit,
            watchlist=_watchlist_symbols(user_id),
            positions=_position_symbols(user_id),
            include_prev=include_prev,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": payload}
