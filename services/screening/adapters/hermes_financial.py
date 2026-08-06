"""Adapter keeping the legacy ``stock_screen`` tool contract on ScreeningService."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from services.screening.schemas import FilterSpec, ScreenSpec, SortSpec, UniverseSpec
from services.screening.service import get_screening_service


def legacy_financial_screen_payload(
    *,
    period: str,
    filters: str,
    report_type: int = 1,
    include_sw_industry: bool = True,
    exclude_st: bool = True,
    exclude_negative_base: bool = False,
    sort_by: str = "auto",
    top_n: int = 100,
    include_express: bool = True,
    include_forecast: bool = True,
) -> Dict[str, Any]:
    """Run a financial screen and return the original hermes response shape."""
    try:
        raw_filters = json.loads(filters)
        if not isinstance(raw_filters, list) or not raw_filters:
            return {"success": False, "error": "filters 必须是非空 JSON 数组"}
        parsed_filters: List[FilterSpec] = [FilterSpec(**f) for f in raw_filters]
    except Exception as e:
        return {"success": False, "error": f"filters 解析失败: {e}"}

    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period=period,
        report_type=report_type,
        include_sw_industry=include_sw_industry,
        exclude_st=exclude_st,
        exclude_negative_base=exclude_negative_base,
        include_express=include_express,
        include_forecast=include_forecast,
        filters=parsed_filters,
        sort=[SortSpec(field=sort_by, order="desc")] if sort_by and sort_by != "auto" else [],
        top_n=top_n,
    )

    result = get_screening_service().run(spec)
    if not result.success:
        return {"success": False, "error": result.error}

    resolved_period = (result.universe_resolved or {}).get("period") or period

    return {
        "success": True,
        "period": resolved_period,
        "report_type": report_type,
        "total_screened": result.total_screened,
        "matched": result.matched,
        "returned": result.returned,
        "filters_applied": [f.model_dump() for f in parsed_filters],
        "summary": result.summary,
        "results": result.results,
    }
