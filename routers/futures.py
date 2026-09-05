"""Futures K-line API routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_active_user
from services.futures_varieties import list_futures_varieties

router = APIRouter()


@router.get("/futures/varieties")
def get_futures_varieties(current_user: dict = Depends(get_current_active_user)):
    payload = list_futures_varieties()
    return {
        "success": True,
        "data": payload,
        "count": len(payload["all"]),
    }
