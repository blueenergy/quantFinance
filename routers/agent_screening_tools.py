"""MCP-exposed screening tools (Agent-framework agnostic)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import require_hermes_quant_internal_key
from routers.hermes_quant_tools import MCP_READ_TAG
from services.screening import ScreenSpec
from services.screening.service import get_screening_service

router = APIRouter(prefix="/agent-tools/screening")


@router.get(
    "/presets",
    operation_id="stock_screen_presets",
    tags=[MCP_READ_TAG],
)
async def agent_stock_screen_presets(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """List versioned universe presets for stock screening."""
    return get_screening_service().get_presets()


@router.get(
    "/schema",
    operation_id="stock_screen_schema",
    tags=[MCP_READ_TAG],
)
async def agent_stock_screen_schema(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Return allowed metrics, operators, and universe presets for ScreenSpec."""
    return get_screening_service().get_schema()


@router.post(
    "/run",
    operation_id="run_stock_screen",
    tags=[MCP_READ_TAG],
)
async def agent_run_stock_screen(
    spec: ScreenSpec,
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Run a deterministic ScreenSpec (price and/or financial metrics)."""
    result = get_screening_service().run(spec)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error)
    return result.model_dump()
