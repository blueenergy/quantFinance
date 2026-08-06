"""REST API for universal stock screening (scripts and future product clients)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import require_hermes_quant_internal_key
from services.screening import ScreenSpec
from services.screening.service import get_screening_service

router = APIRouter(prefix="/screeners", tags=["选股筛选"])


@router.get("/presets")
async def list_presets(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """List versioned universe presets (internal key or via MCP)."""
    return get_screening_service().get_presets()


@router.get("/schema")
async def get_schema(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Return ScreenSpec schema: metrics, ops, universe presets."""
    return get_screening_service().get_schema()


@router.post("/run")
async def run_screen(
    spec: ScreenSpec,
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Execute a deterministic ScreenSpec and return ranked results."""
    result = get_screening_service().run(spec)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error)
    return result.model_dump()
