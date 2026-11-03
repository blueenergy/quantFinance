"""National-team ETF monitoring endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import data_db
from services.etf_national_team import compute_curve, compute_snapshot

router = APIRouter(prefix="/etf/national-team", tags=["etf-national-team"])


@router.get("/snapshot", summary="国家队 ETF 盘中信号快照")
def get_national_team_snapshot(
    trade_date: Optional[str] = Query(
        None, description="YYYYMMDD; default latest minute_bars date"
    ),
    as_of: Optional[str] = Query(
        None, description="HHMM; default latest bar (e.g. 1030, 1430, 1500)"
    ),
    current_user: dict = Depends(get_current_active_user),
):
    """Same-time volume ratio, breadth, tail concentration and share confirmation."""
    try:
        return compute_snapshot(data_db, trade_date=trade_date, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # pragma: no cover - defensive surface for the UI
        raise HTTPException(status_code=500, detail=f"national-team snapshot failed: {exc}")


@router.get("/curve", summary="国家队 ETF 分时累计成交额 vs 20 日基线")
def get_national_team_curve(
    code: str = Query(..., description="ETF ts_code, e.g. 510300.SH"),
    trade_date: Optional[str] = Query(None, description="YYYYMMDD"),
    as_of: Optional[str] = Query(None, description="HHMM; default latest bar"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return compute_curve(data_db, code=code, trade_date=trade_date, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"national-team curve failed: {exc}")
