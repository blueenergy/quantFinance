"""ScreenSpec and response models for the universal screening engine."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

ALLOWED_OPS = frozenset({"gt", "gte", "lt", "lte"})
ALLOWED_SORT_ORDERS = frozenset({"asc", "desc"})
ALLOWED_UNIVERSE_TYPES = frozenset({"preset", "sw_l2", "manual", "all_financial"})
ALLOWED_ADJUST = frozenset({"hfq", "qfq", "none"})


class UniverseSpec(BaseModel):
    type: Literal["preset", "sw_l2", "manual", "all_financial"] = "preset"
    value: Union[str, List[str]] = Field(
        ...,
        description="preset id, list of sw L2 names, or list of ts_codes",
    )


class FilterSpec(BaseModel):
    field: str
    op: Literal["gt", "gte", "lt", "lte"]
    value: float
    metric_type: Optional[Literal["yoy", "abs"]] = None
    base_period: Optional[str] = None

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str) -> str:
        if v not in ALLOWED_OPS:
            raise ValueError(f"unsupported op: {v}")
        return v


class SortSpec(BaseModel):
    field: str
    order: Literal["asc", "desc"] = "desc"

    @field_validator("order")
    @classmethod
    def _validate_order(cls, v: str) -> str:
        if v not in ALLOWED_SORT_ORDERS:
            raise ValueError(f"unsupported order: {v}")
        return v


class ScreenSpec(BaseModel):
    """Deterministic screening request consumed by REST and MCP adapters."""

    universe: UniverseSpec
    start: Optional[str] = Field(
        None,
        description="Price window start YYYYMMDD; required for price metrics",
    )
    end: str = Field("latest", description="Price window end YYYYMMDD or 'latest'")
    adjust: Literal["hfq", "qfq", "none"] = "hfq"
    period: Optional[str] = Field(
        None,
        description="Financial report period YYYYMMDD or 'latest'",
    )
    report_type: int = Field(1, description="1=cumulative, 2=single quarter")
    include_express: bool = Field(
        True,
        description="Fall back to 业绩快报 when a stock has no formal report for the period",
    )
    include_forecast: bool = Field(
        True,
        description="Fall back to 业绩预告 (net profit only) when neither formal nor express exists",
    )
    exclude_st: bool = True
    exclude_adj_degraded: bool = False
    exclude_negative_base: bool = Field(
        False,
        description="Drop stocks whose prior-year base is <=0 instead of labelling them 扭亏/减亏",
    )
    low_base_yoy_threshold: float = Field(
        10.0,
        gt=0,
        description="|YoY| at or above this marks the growth rate as low_base (10.0 = 1000%)",
    )
    include_sw_industry: bool = True
    filters: List[FilterSpec] = Field(default_factory=list)
    sort: List[SortSpec] = Field(default_factory=list)
    top_n: int = Field(100, ge=1, le=500)
    fields: Optional[List[str]] = None


class MetricMeta(BaseModel):
    name: str
    label: str
    provider: str
    unit: str = "ratio"
    sortable: bool = True
    filterable: bool = True
    ops: List[str] = Field(default_factory=lambda: sorted(ALLOWED_OPS))
    requires_date_range: bool = False
    requires_period: bool = False
    metric_types: Optional[List[str]] = None


class YoyQualityMeta(BaseModel):
    id: str
    label: str
    meaning: str


class FinancialSourceMeta(BaseModel):
    id: str
    label: str
    priority: int
    reliability: str
    supported_fields: List[str]
    notes: Optional[str] = None


class UniversePresetMeta(BaseModel):
    id: str
    label: str
    version: str
    description: str
    universe_type: str = "sw_l2"
    members_hint: Optional[str] = None


class ScreenSchemaResponse(BaseModel):
    schema_version: str
    metrics: List[MetricMeta]
    universe_presets: List[UniversePresetMeta]
    financial_sources: List[FinancialSourceMeta] = Field(default_factory=list)
    yoy_quality_labels: List[YoyQualityMeta] = Field(default_factory=list)
    allowed_ops: List[str] = Field(default_factory=lambda: sorted(ALLOWED_OPS))
    allowed_adjust: List[str] = Field(default_factory=lambda: sorted(ALLOWED_ADJUST))


class ScreenResult(BaseModel):
    success: bool = True
    error: Optional[str] = None
    schema_version: str = "1.0"
    spec_applied: Optional[Dict[str, Any]] = None
    universe_resolved: Optional[Dict[str, Any]] = None
    window: Optional[Dict[str, Any]] = None
    total_universe: int = 0
    total_screened: int = 0
    matched: int = 0
    returned: int = 0
    summary: Dict[str, Any] = Field(default_factory=dict)
    results: List[Dict[str, Any]] = Field(default_factory=list)
