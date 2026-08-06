"""Financial statement metric provider.

Values come from three sources, in descending order of reliability:

``formal``    ``financial_income`` — the audited/filed income statement.
``express``   ``earnings_express`` — 业绩快报, preliminary but complete enough to
              rank on; only exists early in a reporting season.
``forecast``  ``earnings_forecast`` — 业绩预告, a net-profit range only.

A stock is served by exactly one source per screen: the best one that has data
for the target period. Year-on-year bases always come from ``formal``, so the
growth denominator has a single definition regardless of the numerator's source.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from ..registry import (
    EXPRESS_FIELD_MAP,
    FINANCIAL_FIELDS,
    FORECAST_FIELDS,
    FORECAST_UNIT_SCALE,
    SOURCE_EXPRESS,
    SOURCE_FORECAST,
    SOURCE_FORMAL,
    YOY_LOSS,
    YOY_LOW_BASE,
    YOY_NORMAL,
    YOY_TURNAROUND,
)
from ..schemas import FilterSpec, ScreenSpec

_YOY_SUFFIX = "_yoy"
_ALLOWED_METRICS = {"yoy", "abs"}


def classify_yoy(cur: float, prev: float, yoy: float, low_base_threshold: float) -> str:
    """Label how much a growth rate can actually be read as growth."""
    if prev <= 0:
        return YOY_TURNAROUND if cur > 0 else YOY_LOSS
    if abs(yoy) >= low_base_threshold:
        return YOY_LOW_BASE
    return YOY_NORMAL


def normalize_period(period: str) -> str:
    return str(period).replace("-", "")[:8]


def _prev_year_period(period: str) -> str:
    return str(int(period[:4]) - 1) + period[4:]


def resolve_field_and_metric(filter_spec: FilterSpec) -> Tuple[str, str]:
    """Map a filter onto its ``financial_income`` field and metric kind."""
    field = filter_spec.field
    if field.endswith(_YOY_SUFFIX):
        return field[: -len(_YOY_SUFFIX)], "yoy"
    if filter_spec.metric_type in _ALLOWED_METRICS:
        return field, filter_spec.metric_type
    return field, "abs"


def filter_row_key(filter_spec: FilterSpec) -> str:
    """Row key holding the value this filter compares against."""
    field, metric_type = resolve_field_and_metric(filter_spec)
    if metric_type == "yoy":
        return f"{field}{_YOY_SUFFIX}"
    if filter_spec.base_period:
        return f"{field}@{normalize_period(filter_spec.base_period)}"
    return field


class FinancialMetricProvider:
    provider_id = "financial"

    def __init__(self, data_db):
        self.data_db = data_db
        self.fin = data_db["financial_income"]
        self.express = data_db["earnings_express"]
        self.forecast = data_db["earnings_forecast"]

    def _fallback_sources(self, spec: ScreenSpec) -> List[str]:
        # 快报/预告只有累计口径，单季筛选(report_type=2)不能混用。
        if spec.report_type != 1:
            return []
        sources = []
        if spec.include_express:
            sources.append(SOURCE_EXPRESS)
        if spec.include_forecast:
            sources.append(SOURCE_FORECAST)
        return sources

    def resolve_period(self, spec: ScreenSpec) -> str:
        period = (spec.period or "").strip()
        if not period:
            raise ValueError("financial provider 需要 period (YYYYMMDD 或 latest)")
        if period.lower() != "latest":
            return normalize_period(period)

        candidates: List[str] = []
        latest = self.fin.find_one(
            {"report_type": spec.report_type}, sort=[("end_date", -1)]
        )
        if latest and latest.get("end_date"):
            candidates.append(latest["end_date"])
        # 预告的 end_date 会排到远未披露的未来期，故不参与 latest 解析。
        if SOURCE_EXPRESS in self._fallback_sources(spec):
            latest_ex = self.express.find_one({}, sort=[("end_date", -1)])
            if latest_ex and latest_ex.get("end_date"):
                candidates.append(latest_ex["end_date"])
        if not candidates:
            raise ValueError("financial_income 无数据")
        return max(candidates)

    def _load_formal(
        self,
        symbols: Optional[List[str]],
        periods: Set[str],
        fields: Set[str],
        report_type: int,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        proj: Dict[str, int] = {"_id": 0, "ts_code": 1, "end_date": 1, "ann_date": 1}
        for name in fields:
            proj[name] = 1
        query: Dict[str, Any] = {
            "end_date": {"$in": sorted(periods)},
            "report_type": report_type,
        }
        if symbols is not None:
            query["ts_code"] = {"$in": symbols}

        by_code: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for d in self.fin.find(query, proj):
            ts, end_date = d.get("ts_code"), d.get("end_date")
            if ts and end_date:
                by_code.setdefault(ts, {})[end_date] = d
        return by_code

    def _load_express(
        self, symbols: Optional[List[str]], period: str
    ) -> Dict[str, Dict[str, Any]]:
        query: Dict[str, Any] = {"end_date": period}
        if symbols is not None:
            query["ts_code"] = {"$in": symbols}
        proj = {"_id": 0, "ts_code": 1, "ann_date": 1}
        for src_field in EXPRESS_FIELD_MAP.values():
            proj[src_field] = 1

        out: Dict[str, Dict[str, Any]] = {}
        for d in self.express.find(query, proj):
            ts = d.get("ts_code")
            if not ts:
                continue
            values = {
                target: d.get(src)
                for target, src in EXPRESS_FIELD_MAP.items()
                if d.get(src) is not None
            }
            if values:
                out[ts] = {"values": values, "ann_date": d.get("ann_date")}
        return out

    def _load_forecast(
        self, symbols: Optional[List[str]], period: str
    ) -> Dict[str, Dict[str, Any]]:
        query: Dict[str, Any] = {"end_date": period}
        if symbols is not None:
            query["ts_code"] = {"$in": symbols}
        proj = {
            "_id": 0,
            "ts_code": 1,
            "ann_date": 1,
            "net_profit_min": 1,
            "net_profit_max": 1,
            "type": 1,
        }

        out: Dict[str, Dict[str, Any]] = {}
        for d in self.forecast.find(query, proj):
            ts = d.get("ts_code")
            lo, hi = d.get("net_profit_min"), d.get("net_profit_max")
            if not ts:
                continue
            bounds = [v for v in (lo, hi) if v is not None]
            if not bounds:
                continue
            midpoint = sum(bounds) / len(bounds) * FORECAST_UNIT_SCALE
            out[ts] = {
                "values": {"n_income_attr_p": midpoint},
                "ann_date": d.get("ann_date"),
                "forecast_type": d.get("type"),
            }
        return out

    def enrich(
        self,
        symbols: Optional[List[str]],
        spec: ScreenSpec,
        *,
        target_period: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Compute financial metrics.

        ``symbols=None`` screens every stock reporting in the period, which
        keeps the query off a market-sized ``$in`` list.
        """
        target = target_period or self.resolve_period(spec)

        needed_periods = {target}
        has_yoy = any(
            resolve_field_and_metric(f)[1] == "yoy" for f in spec.filters
        ) or any(s.field.endswith(_YOY_SUFFIX) for s in spec.sort)
        prev_period = _prev_year_period(target) if has_yoy else None
        if prev_period:
            needed_periods.add(prev_period)
        for f in spec.filters:
            _, metric_type = resolve_field_and_metric(f)
            if metric_type == "abs" and f.base_period:
                needed_periods.add(normalize_period(f.base_period))

        needed_fields: Set[str] = set()
        for f in spec.filters:
            field, _ = resolve_field_and_metric(f)
            if field in FINANCIAL_FIELDS:
                needed_fields.add(field)
        for s in spec.sort:
            field = (
                s.field[: -len(_YOY_SUFFIX)] if s.field.endswith(_YOY_SUFFIX) else s.field
            )
            if field in FINANCIAL_FIELDS:
                needed_fields.add(field)
        if not needed_fields:
            needed_fields = set(FINANCIAL_FIELDS)

        formal = self._load_formal(symbols, needed_periods, needed_fields, spec.report_type)

        fallbacks = self._fallback_sources(spec)
        express = (
            self._load_express(symbols, target) if SOURCE_EXPRESS in fallbacks else {}
        )
        forecast = (
            self._load_forecast(symbols, target) if SOURCE_FORECAST in fallbacks else {}
        )

        if symbols is None:
            candidates: List[str] = sorted(
                set(formal) | set(express) | set(forecast)
            )
        else:
            candidates = symbols

        out: Dict[str, Dict[str, Any]] = {}
        for ts in candidates:
            periods = formal.get(ts) or {}
            current = self._pick_current(ts, target, periods, express, forecast, needed_fields)
            if current is None:
                continue
            values, source, ann_date, extra = current

            metrics: Dict[str, Any] = {
                "period": target,
                "data_source": source,
                "data_source_ann_date": ann_date,
            }
            metrics.update(extra)
            prev = periods.get(prev_period) if prev_period else None

            for name in needed_fields:
                cur_v = values.get(name)
                if cur_v is not None:
                    metrics[name] = cur_v
                if prev is None or cur_v is None:
                    continue
                prev_v = prev.get(name)
                if prev_v is None or prev_v == 0:
                    continue
                if spec.exclude_negative_base and prev_v <= 0:
                    continue
                yoy = (cur_v - prev_v) / abs(prev_v)
                metrics[f"{name}{_YOY_SUFFIX}"] = yoy
                # 扭亏和低基数股本身是有价值的信号，所以保留并标注，而不是丢弃：
                # 同时给出基数，读数才有解释的余地。
                metrics[f"{name}{_YOY_SUFFIX}_base"] = prev_v
                metrics[f"{name}{_YOY_SUFFIX}_quality"] = classify_yoy(
                    cur_v, prev_v, yoy, spec.low_base_yoy_threshold
                )

            for f in spec.filters:
                field, metric_type = resolve_field_and_metric(f)
                if metric_type != "abs" or not f.base_period:
                    continue
                base_period = normalize_period(f.base_period)
                base_doc = periods.get(base_period)
                if not base_doc:
                    continue
                base_v = base_doc.get(field)
                if base_v is not None:
                    metrics[f"{field}@{base_period}"] = base_v

            out[ts] = metrics
        return out

    @staticmethod
    def _pick_current(
        ts: str,
        target: str,
        formal_periods: Dict[str, Dict[str, Any]],
        express: Dict[str, Dict[str, Any]],
        forecast: Dict[str, Dict[str, Any]],
        needed_fields: Set[str],
    ) -> Optional[Tuple[Dict[str, Any], str, Optional[str], Dict[str, Any]]]:
        """Return ``(values, source, ann_date, extra)`` from the best source."""
        formal_doc = formal_periods.get(target)
        if formal_doc:
            return formal_doc, SOURCE_FORMAL, formal_doc.get("ann_date"), {}

        express_doc = express.get(ts)
        if express_doc and needed_fields & set(express_doc["values"]):
            return (
                express_doc["values"],
                SOURCE_EXPRESS,
                express_doc.get("ann_date"),
                {},
            )

        forecast_doc = forecast.get(ts)
        if forecast_doc and needed_fields & set(forecast_doc["values"]):
            extra = {}
            if forecast_doc.get("forecast_type"):
                extra["forecast_type"] = forecast_doc["forecast_type"]
            return (
                forecast_doc["values"],
                SOURCE_FORECAST,
                forecast_doc.get("ann_date"),
                extra,
            )
        return None
