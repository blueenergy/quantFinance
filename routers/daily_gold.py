"""Daily gold picks read-only API."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_data_db
from services.daily_gold_panel import build_daily_gold_panel

router = APIRouter(prefix="/daily-gold", tags=["DailyGold"])


@router.get(
    "/panel",
    summary="每日金股观察池 + 纸面战绩",
)
def get_daily_gold_panel(
    recipe_id: Optional[str] = Query(None, description="配方 ID，默认中证1000"),
    score_date: Optional[str] = Query(None, description="评分日 YYYYMMDD，默认该配方最新"),
    current_user: dict = Depends(get_current_active_user),
):
    _ = current_user
    data_db = get_data_db()
    try:
        return build_daily_gold_panel(
            data_db=data_db,
            recipe_id=recipe_id,
            score_date=score_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
