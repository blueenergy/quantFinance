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
    """列出可用的股票池预设（universe），用于构造 ScreenSpec 的 universe 字段。

    何时用：不确定该用哪个股票池，或想确认预设名称/成分数量时先调用本工具。
    何时不用：已知预设名称时直接 run_stock_screen。
    返回：预设名称、成分数量、版本与更新时间；纯静态配置，不触发筛选计算。
    """
    return get_screening_service().get_presets()


@router.get(
    "/schema",
    operation_id="stock_screen_schema",
    tags=[MCP_READ_TAG],
)
async def agent_stock_screen_schema(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """返回筛选引擎支持的指标、运算符与参数约束（ScreenSpec 的 schema）。

    何时用：第一次构造 ScreenSpec，或 run_stock_screen 返回 422 参数错误时，
    先读本工具确认指标名、可用运算符、取值范围与股票池预设。
    何时不用：已有合法 ScreenSpec 时直接 run_stock_screen。
    返回：价量与财务两类指标清单、运算符、universe 预设、示例请求。
    """
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
    """按 ScreenSpec 执行确定性股票筛选（价量/财务指标），返回命中股票列表。

    何时用：需要按明确条件筛股（如 PE<20 且 20 日涨幅>5%），结果可复现、可对比。
    何时不用：需要主观分析/归因时用 deep_analysis；不确定指标名时先调 stock_screen_schema。
    返回：命中数量、股票代码/名称/行业与命中指标值；基于最近交易日收盘，不含盘中实时。
    """
    result = get_screening_service().run(spec)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error)
    return result.model_dump()
