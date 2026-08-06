"""ScreeningService — single deterministic engine for REST and MCP."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from db import data_db

from .metrics import compare_value
from .providers.financial import (
    FinancialMetricProvider,
    filter_row_key,
    resolve_field_and_metric,
)
from .providers.price import PriceMetricProvider
from .registry import (
    ALL_METRICS,
    SCHEMA_VERSION,
    SOURCE_FORMAL,
    YOY_QUALITY_SEVERITY,
    get_schema_response,
    resolve_provider_needs,
    validate_spec_fields,
)
from .schemas import ScreenResult, ScreenSpec, SortSpec
from .universes import get_preset_l2_names, list_universe_presets


class ScreeningService:
    def __init__(self, db=None, price_provider: Optional[PriceMetricProvider] = None):
        self.db = db if db is not None else data_db
        self._price: Optional[PriceMetricProvider] = price_provider
        self._financial = FinancialMetricProvider(self.db)

    def _get_price_provider(self) -> PriceMetricProvider:
        if self._price is None:
            self._price = PriceMetricProvider(data_db=self.db)
        return self._price

    def get_presets(self) -> dict:
        return {
            "success": True,
            "presets": [p.model_dump() for p in list_universe_presets()],
        }

    def get_schema(self) -> dict:
        return get_schema_response().model_dump()

    def resolve_universe(
        self,
        spec: ScreenSpec,
        *,
        resolved_period: Optional[str] = None,
    ) -> Tuple[Optional[List[str]], dict]:
        """Resolve the candidate symbols.

        Returns ``(symbols, meta)`` where ``symbols is None`` means the universe
        is unbounded and must be produced by a provider (``all_financial``),
        which avoids sending every A-share ts_code back as an ``$in`` filter.
        """
        u = spec.universe
        meta: Dict[str, Any] = {"type": u.type}

        if u.type == "preset":
            preset_id = str(u.value)
            meta["preset"] = preset_id
            l2_names = get_preset_l2_names(preset_id)
            meta["sw_l2_names"] = l2_names
            symbols = self._symbols_for_sw_l2(l2_names, meta)
        elif u.type == "sw_l2":
            l2_names = u.value if isinstance(u.value, list) else [str(u.value)]
            meta["sw_l2_names"] = l2_names
            symbols = self._symbols_for_sw_l2(l2_names, meta)
        elif u.type == "manual":
            symbols = u.value if isinstance(u.value, list) else [str(u.value)]
            meta["symbols"] = symbols
        elif u.type == "all_financial":
            if not resolved_period:
                raise ValueError("all_financial universe requires period")
            meta["period"] = resolved_period
            meta["count"] = None
            return None, meta
        else:
            raise ValueError(f"unsupported universe type: {u.type}")

        symbols = sorted({s for s in symbols if s})
        meta["count"] = len(symbols)
        return symbols, meta

    def _symbols_for_sw_l2(self, l2_names: List[str], meta: Dict[str, Any]) -> List[str]:
        if not l2_names:
            return []
        cursor = self.db["sw_index_member"].find(
            {
                "src": "SW2021",
                "is_new": "Y",
                "l2_name": {"$in": l2_names},
            },
            {"_id": 0, "con_code": 1, "l2_name": 1},
        )
        symbols: List[str] = []
        matched: Set[str] = set()
        for d in cursor:
            code = d.get("con_code")
            if not code:
                continue
            symbols.append(str(code))
            matched.add(d.get("l2_name"))
        # A misspelled L2 name silently contributes zero members, so report it
        # rather than letting the universe quietly shrink.
        unmatched = [n for n in l2_names if n not in matched]
        if unmatched:
            meta["sw_l2_unmatched"] = unmatched
        return symbols

    def _attach_names_and_st(
        self, rows: List[Dict[str, Any]], *, exclude_st: bool
    ) -> List[Dict[str, Any]]:
        if not rows:
            return []
        codes = [r["ts_code"] for r in rows]
        info_map = {
            d["ts_code"]: d
            for d in self.db["stock_info"].find(
                {"ts_code": {"$in": codes}},
                {"_id": 0, "ts_code": 1, "name": 1},
            )
        }
        out: List[Dict[str, Any]] = []
        for r in rows:
            name = (info_map.get(r["ts_code"], {}) or {}).get("name", "")
            if exclude_st and name and "ST" in name.upper():
                continue
            r["name"] = name
            out.append(r)
        return out

    def _attach_sw(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        codes = [r["ts_code"] for r in rows]
        sw_map = {
            d["con_code"]: d
            for d in self.db["sw_index_member"].find(
                {"con_code": {"$in": codes}, "is_new": "Y", "src": "SW2021"},
                {
                    "_id": 0,
                    "con_code": 1,
                    "l1_name": 1,
                    "l2_name": 1,
                    "l3_name": 1,
                },
            )
        }
        for r in rows:
            sw = sw_map.get(r["ts_code"], {}) or {}
            r["sw_l1"] = sw.get("l1_name", "")
            r["sw_l2"] = sw.get("l2_name", "")
            r["sw_l3"] = sw.get("l3_name", "")

    @staticmethod
    def _worst_yoy_quality(row: Dict[str, Any]) -> Optional[str]:
        labels = [v for k, v in row.items() if k.endswith("_yoy_quality")]
        if not labels:
            return None
        return max(labels, key=lambda x: YOY_QUALITY_SEVERITY.get(x, 0))

    @staticmethod
    def _filter_row_key(f) -> str:
        """Row key holding the value a filter compares against."""
        meta = ALL_METRICS.get(f.field)
        if meta is not None and meta.provider == "financial":
            return filter_row_key(f)
        return f.field

    def _apply_filters(self, row: Dict[str, Any], spec: ScreenSpec) -> bool:
        for f in spec.filters:
            val = row.get(self._filter_row_key(f))
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return False
            if not compare_value(float(val), f.op, f.value):
                return False
        return True

    def _sort_key(self, sort_specs: List[SortSpec]):
        def key(row: Dict[str, Any]):
            keys = []
            for s in sort_specs:
                v = row.get(s.field)
                missing = not isinstance(v, (int, float)) or isinstance(v, bool)
                num = 0.0 if missing else -float(v)
                if s.order == "asc":
                    num = -num
                keys.append((missing, num))
            return tuple(keys)

        return key

    def _default_sort(self, spec: ScreenSpec, needs) -> List[SortSpec]:
        """Mirror the legacy ``sort_by="auto"`` behaviour: first YoY filter wins."""
        if spec.sort:
            return spec.sort
        if needs.price:
            return [SortSpec(field="range_amplitude", order="desc")]
        for f in spec.filters:
            if resolve_field_and_metric(f)[1] == "yoy":
                return [SortSpec(field=filter_row_key(f), order="desc")]
        if spec.filters:
            return [SortSpec(field=filter_row_key(spec.filters[0]), order="desc")]
        return []

    def run(self, spec: ScreenSpec) -> ScreenResult:
        err = validate_spec_fields(spec)
        if err:
            return ScreenResult(success=False, error=err)

        needs = resolve_provider_needs(spec)
        if needs.price and not spec.start:
            return ScreenResult(success=False, error="price metrics require start date")

        resolved_period: Optional[str] = None
        if needs.financial:
            try:
                resolved_period = self._financial.resolve_period(spec)
            except ValueError as e:
                return ScreenResult(success=False, error=str(e))

        try:
            symbols, universe_meta = self.resolve_universe(
                spec, resolved_period=resolved_period
            )
        except (KeyError, ValueError) as e:
            return ScreenResult(success=False, error=str(e))

        unbounded = symbols is None
        if unbounded and needs.price:
            return ScreenResult(
                success=False,
                error="price metrics require an explicit universe (preset/sw_l2/manual)",
            )

        def _empty(total_universe: int) -> ScreenResult:
            return ScreenResult(
                success=True,
                schema_version=SCHEMA_VERSION,
                spec_applied=spec.model_dump(),
                universe_resolved=universe_meta,
                total_universe=total_universe,
                total_screened=0,
                matched=0,
                returned=0,
                summary={"industries_distribution": []},
                results=[],
            )

        if not unbounded and not symbols:
            return _empty(0)

        price_metrics: Dict[str, Dict[str, Any]] = {}
        financial_metrics: Dict[str, Dict[str, Any]] = {}
        window_meta: Dict[str, Any] = {}

        if needs.price:
            try:
                end_date = self._get_price_provider().resolve_end_date(
                    spec, self.db["volume_price"]
                )
                price_metrics = self._get_price_provider().enrich(
                    symbols, spec, end_trade_date=end_date
                )
            except ValueError as e:
                return ScreenResult(success=False, error=str(e))
            if price_metrics:
                sample = next(iter(price_metrics.values()))
                window_meta = {
                    "start": sample.get("start_trade_date"),
                    "end": sample.get("end_trade_date"),
                    "adjust": spec.adjust,
                    "as_of": end_date,
                }

        if needs.financial:
            try:
                financial_metrics = self._financial.enrich(
                    symbols, spec, target_period=resolved_period
                )
            except ValueError as e:
                return ScreenResult(success=False, error=str(e))

        # A required provider returning nothing means no candidate qualifies.
        # Narrowing unconditionally (rather than only when the provider produced
        # rows) keeps a data gap from silently yielding metric-less rows.
        if unbounded:
            active_symbols: Set[str] = set(financial_metrics.keys())
            total_universe = len(active_symbols)
            universe_meta["count"] = total_universe
        else:
            total_universe = len(symbols)
            active_symbols = set(symbols)
            if needs.price:
                active_symbols &= set(price_metrics.keys())
            if needs.financial:
                active_symbols &= set(financial_metrics.keys())

        if not active_symbols:
            return _empty(total_universe)

        rows: List[Dict[str, Any]] = []
        for ts in sorted(active_symbols):
            row: Dict[str, Any] = {"ts_code": ts}
            if ts in price_metrics:
                row.update(price_metrics[ts])
            if ts in financial_metrics:
                row.update(financial_metrics[ts])
            if self._apply_filters(row, spec):
                rows.append(row)

        total_screened = len(active_symbols)
        rows = self._attach_names_and_st(rows, exclude_st=spec.exclude_st)
        if spec.include_sw_industry:
            self._attach_sw(rows)

        sort_specs = self._default_sort(spec, needs)
        if sort_specs:
            rows.sort(key=self._sort_key(sort_specs))

        results = rows[: spec.top_n]

        industries_dist: List[Dict[str, Any]] = []
        if spec.include_sw_industry:
            cnt = Counter(r.get("sw_l1") or "未知" for r in results)
            industries_dist = [{"sw_l1": k, "count": v} for k, v in cnt.most_common()]

        summary: Dict[str, Any] = {"industries_distribution": industries_dist}
        if needs.financial:
            src = Counter(r.get("data_source") or SOURCE_FORMAL for r in results)
            summary["sources_distribution"] = [
                {"data_source": k, "count": v} for k, v in src.most_common()
            ]
            quality = Counter(self._worst_yoy_quality(r) for r in results)
            quality.pop(None, None)
            if quality:
                # Worst first: the caveats are the part a reader must not miss,
                # and counts tie too often for frequency order to be stable.
                summary["yoy_quality_distribution"] = [
                    {"yoy_quality": k, "count": quality[k]}
                    for k in sorted(
                        quality, key=lambda x: -YOY_QUALITY_SEVERITY.get(x, 0)
                    )
                ]

        # Projection runs last so trimming output columns cannot distort the
        # summaries computed above. Provenance and growth-rate caveats are never
        # dropped: a caller must not mistake a 快报 estimate or a 扭亏 ratio for a
        # filed report or for real growth.
        if spec.fields:
            keep = set(spec.fields) | {"ts_code", "name"}
            if needs.financial:
                keep |= {"data_source", "data_source_ann_date"}
                keep |= {
                    f"{name}{suffix}"
                    for name in spec.fields
                    if name.endswith("_yoy")
                    for suffix in ("_base", "_quality")
                }
            results = [{k: v for k, v in r.items() if k in keep} for r in results]

        return ScreenResult(
            success=True,
            schema_version=SCHEMA_VERSION,
            spec_applied=spec.model_dump(),
            universe_resolved=universe_meta,
            window=window_meta or None,
            total_universe=total_universe,
            total_screened=total_screened,
            matched=len(rows),
            returned=len(results),
            summary=summary,
            results=results,
        )


_default_service: Optional[ScreeningService] = None


def get_screening_service(db=None) -> ScreeningService:
    global _default_service
    if db is not None:
        return ScreeningService(db=db)
    if _default_service is None:
        _default_service = ScreeningService()
    return _default_service
