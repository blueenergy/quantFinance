"""Metric and universe registry for the screening engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .schemas import (
    FinancialSourceMeta,
    MetricMeta,
    ScreenSchemaResponse,
    ScreenSpec,
    YoyQualityMeta,
)
from .universes import list_universe_presets

SCHEMA_VERSION = "1.0"

IDENTITY_FIELDS = frozenset({"ts_code", "name", "sw_l1", "sw_l2", "sw_l3"})

PRICE_METRICS: Dict[str, MetricMeta] = {
    "range_amplitude": MetricMeta(
        name="range_amplitude",
        label="区间振幅",
        provider="price",
        unit="ratio",
        requires_date_range=True,
    ),
    "peak_gain": MetricMeta(
        name="peak_gain",
        label="前期最大涨幅",
        provider="price",
        unit="ratio",
        requires_date_range=True,
    ),
    "drawdown_from_peak": MetricMeta(
        name="drawdown_from_peak",
        label="高点至今回撤",
        provider="price",
        unit="ratio",
        requires_date_range=True,
    ),
    "return_pct": MetricMeta(
        name="return_pct",
        label="区间涨跌幅",
        provider="price",
        unit="ratio",
        requires_date_range=True,
    ),
}

FINANCIAL_FIELDS = {
    "operate_profit": "营业利润",
    "revenue": "主营业务收入",
    "total_revenue": "营业总收入",
    "n_income": "净利润",
    "n_income_attr_p": "归母净利润",
    "total_profit": "利润总额",
    "oper_cost": "营业成本",
}


SOURCE_FORMAL = "formal"
SOURCE_EXPRESS = "express"
SOURCE_FORECAST = "forecast"

# 快报的 n_income 字段名为「净利润」，但实测口径是归母净利润，故映射到 n_income_attr_p。
EXPRESS_FIELD_MAP: Dict[str, str] = {
    "revenue": "revenue",
    "operate_profit": "operate_profit",
    "total_profit": "total_profit",
    "n_income_attr_p": "n_income",
}

# 预告只给出归母净利润区间，取中值；其金额单位是万元，financial_income 是元。
FORECAST_FIELDS: Tuple[str, ...] = ("n_income_attr_p",)
FORECAST_UNIT_SCALE = 10_000


YOY_NORMAL = "normal"
YOY_TURNAROUND = "turnaround"
YOY_LOSS = "loss"
YOY_LOW_BASE = "low_base"

# Worst-first: a row is summarised by its most caveated growth rate.
YOY_QUALITY_SEVERITY: Dict[str, int] = {
    YOY_NORMAL: 0,
    YOY_LOW_BASE: 1,
    YOY_LOSS: 2,
    YOY_TURNAROUND: 3,
}


def _build_yoy_quality_labels() -> List[YoyQualityMeta]:
    return [
        YoyQualityMeta(
            id=YOY_NORMAL,
            label="正常",
            meaning="去年同期为正且增速在合理量级，同比可直接解读",
        ),
        YoyQualityMeta(
            id=YOY_TURNAROUND,
            label="扭亏为盈",
            meaning="去年同期<=0、今年为正。百分比由负基数算出，不代表真实增速，应说「扭亏为盈」并给出两期金额",
        ),
        YoyQualityMeta(
            id=YOY_LOSS,
            label="两期均亏",
            meaning="去年同期<=0 且今年仍<=0。正的同比只表示亏损收窄，不是盈利增长",
        ),
        YoyQualityMeta(
            id=YOY_LOW_BASE,
            label="低基数",
            meaning="去年同期为正但极小，导致同比畸高（|同比| >= low_base_yoy_threshold，默认 1000%）",
        ),
    ]


def _build_financial_sources() -> List[FinancialSourceMeta]:
    return [
        FinancialSourceMeta(
            id=SOURCE_FORMAL,
            label="正式财报",
            priority=1,
            reliability="high",
            supported_fields=sorted(FINANCIAL_FIELDS),
            notes="financial_income，已披露的利润表，字段最全",
        ),
        FinancialSourceMeta(
            id=SOURCE_EXPRESS,
            label="业绩快报",
            priority=2,
            reliability="medium",
            supported_fields=sorted(EXPRESS_FIELD_MAP),
            notes=(
                "earnings_express，未审计预披露值，与最终财报可有数个百分点出入；"
                "仅在该股当期无正式财报时启用（include_express）"
            ),
        ),
        FinancialSourceMeta(
            id=SOURCE_FORECAST,
            label="业绩预告",
            priority=3,
            reliability="low",
            supported_fields=sorted(FORECAST_FIELDS),
            notes=(
                "earnings_forecast，仅归母净利润区间的中值，无营收；"
                "仅在无正式财报且无快报时启用（include_forecast）"
            ),
        ),
    ]


def _build_financial_metrics() -> Dict[str, MetricMeta]:
    metrics: Dict[str, MetricMeta] = {}
    for name, label in FINANCIAL_FIELDS.items():
        metrics[name] = MetricMeta(
            name=name,
            label=f"{label}（绝对值）",
            provider="financial",
            unit="yuan",
            requires_period=True,
            metric_types=["abs"],
        )
        metrics[f"{name}_yoy"] = MetricMeta(
            name=f"{name}_yoy",
            label=f"{label}（同比）",
            provider="financial",
            unit="ratio",
            requires_period=True,
            metric_types=["yoy"],
        )
    return metrics


FINANCIAL_METRICS: Dict[str, MetricMeta] = _build_financial_metrics()

ALL_METRICS: Dict[str, MetricMeta] = {**PRICE_METRICS, **FINANCIAL_METRICS}


@dataclass
class ProviderNeeds:
    price: bool = False
    financial: bool = False
    price_fields: Set[str] = field(default_factory=set)
    financial_fields: Set[str] = field(default_factory=set)


def _referenced_fields(spec: ScreenSpec) -> Set[str]:
    referenced: Set[str] = {f.field for f in spec.filters}
    referenced.update(s.field for s in spec.sort)
    if spec.fields:
        referenced.update(spec.fields)
    return referenced


def resolve_provider_needs(spec: ScreenSpec) -> ProviderNeeds:
    needs = ProviderNeeds()

    for name in _referenced_fields(spec):
        meta = ALL_METRICS.get(name)
        if meta is None:
            continue
        if meta.provider == "price":
            needs.price = True
            needs.price_fields.add(name)
        elif meta.provider == "financial":
            needs.financial = True
            needs.financial_fields.add(name)

    # A date range means the caller wants the price columns even without filters.
    if spec.start:
        needs.price = True
        if not needs.price_fields:
            needs.price_fields.update(PRICE_METRICS.keys())

    # `period` alone must not pull the whole income statement; only an explicit
    # financial reference or a financial universe requires that provider.
    if spec.universe.type == "all_financial":
        needs.financial = True

    return needs


def get_schema_response() -> ScreenSchemaResponse:
    return ScreenSchemaResponse(
        schema_version=SCHEMA_VERSION,
        metrics=list(ALL_METRICS.values()),
        universe_presets=list_universe_presets(),
        financial_sources=_build_financial_sources(),
        yoy_quality_labels=_build_yoy_quality_labels(),
    )


def validate_spec_fields(spec: ScreenSpec) -> Optional[str]:
    """Return an error message if the spec references unregistered fields."""
    unknown = sorted(
        name
        for name in _referenced_fields(spec)
        if name not in ALL_METRICS and name not in IDENTITY_FIELDS
    )
    if unknown:
        return f"unsupported field(s): {', '.join(unknown)}"
    return None
