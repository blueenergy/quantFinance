"""ETF industry/theme heatmap: share-change net inflow aggregated by curated buckets."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from bson.decimal128 import Decimal128
except ImportError:  # pragma: no cover
    Decimal128 = None  # type: ignore


# Only on-exchange stock ETFs; no feeder/联接, money, bond, inverse/leveraged, FOF.
ETF_HEATMAP_BUCKETS: List[Dict[str, Any]] = [
    {
        "id": "semiconductor",
        "name": "半导体",
        "kind": "theme",
        "candidates": [
            "512760.SH",
            "159995.SZ",
            "588200.SH",
            "512480.SH",
            "159813.SZ",
            "516350.SH",
        ],
    },
    {
        "id": "new_energy_vehicle",
        "name": "新能源车",
        "kind": "theme",
        "candidates": [
            "515790.SH",
            "516660.SH",
            "159806.SZ",
            "159637.SZ",
            "561160.SH",
        ],
    },
    {
        "id": "solar_pv",
        "name": "光伏",
        "kind": "theme",
        "candidates": [
            "516160.SH",
            "159857.SZ",
            "561010.SH",
        ],
    },
    {
        "id": "pharma",
        "name": "医药",
        "kind": "theme",
        "candidates": [
            "512010.SH",
            "159929.SZ",
            "512170.SH",
            "159938.SZ",
            "515120.SH",
        ],
    },
    {
        "id": "liquor",
        "name": "白酒",
        "kind": "theme",
        "candidates": [
            "512690.SH",
            "159993.SZ",
        ],
    },
    {
        "id": "consumer",
        "name": "消费",
        "kind": "theme",
        "candidates": [
            "159928.SZ",
            "515650.SH",
            "159736.SZ",
            "512600.SH",
            "159996.SZ",
        ],
    },
    {
        "id": "broker",
        "name": "证券",
        "kind": "theme",
        "candidates": [
            "512880.SH",
            "512000.SH",
            "159842.SZ",
            "515010.SH",
        ],
    },
    {
        "id": "bank",
        "name": "银行",
        "kind": "theme",
        "candidates": [
            "512800.SH",
            "515290.SH",
            "159887.SZ",
            "512730.SH",
        ],
    },
    {
        "id": "military",
        "name": "军工",
        "kind": "theme",
        "candidates": [
            "512660.SH",
            "512810.SH",
            "159638.SZ",
            "512680.SH",
        ],
    },
    {
        "id": "nonferrous",
        "name": "有色",
        "kind": "theme",
        "candidates": [
            "512400.SH",
            "159881.SZ",
            "516650.SH",
            "159871.SZ",
        ],
    },
    {
        "id": "coal",
        "name": "煤炭",
        "kind": "theme",
        "candidates": [
            "515220.SH",
            "159930.SZ",
            "561570.SH",
        ],
    },
    {
        "id": "real_estate",
        "name": "地产",
        "kind": "theme",
        "candidates": [
            "512200.SH",
            "159768.SZ",
            "515060.SH",
        ],
    },
    {
        "id": "media",
        "name": "传媒",
        "kind": "theme",
        "candidates": [
            "512980.SH",
            "159805.SZ",
            "516010.SH",
        ],
    },
    {
        "id": "computer",
        "name": "计算机",
        "kind": "theme",
        "candidates": [
            "512720.SH",
            "159998.SZ",
            "516760.SH",
            "159890.SZ",
        ],
    },
    {
        "id": "telecom",
        "name": "通信",
        "kind": "theme",
        "candidates": [
            "515880.SH",
            "159695.SZ",
            "561800.SH",
        ],
    },
    {
        "id": "power",
        "name": "电力",
        "kind": "theme",
        "candidates": [
            "561560.SH",
            "159611.SZ",
            "562350.SH",
        ],
    },
    {
        "id": "hs300",
        "name": "沪深300",
        "kind": "broad",
        "candidates": ["510300.SH", "159919.SZ", "510330.SH", "515380.SH"],
    },
    {
        "id": "zz500",
        "name": "中证500",
        "kind": "broad",
        "candidates": ["510500.SH", "159922.SZ", "512500.SH"],
    },
    {
        "id": "zz1000",
        "name": "中证1000",
        "kind": "broad",
        "candidates": ["512100.SH", "159845.SZ", "560010.SH"],
    },
    {
        "id": "sz50",
        "name": "上证50",
        "kind": "broad",
        "candidates": ["510050.SH", "510100.SH", "159601.SZ"],
    },
    {
        "id": "kcb50",
        "name": "科创50",
        "kind": "broad",
        "candidates": ["588000.SH", "588080.SH", "588050.SH"],
    },
    {
        "id": "cyb100",
        "name": "创业板",
        "kind": "broad",
        "candidates": ["159949.SZ", "159915.SZ", "159952.SZ"],
    },
    {
        "id": "a500",
        "name": "A500",
        "kind": "broad",
        "candidates": ["159852.SZ", "563360.SH", "560610.SH"],
    },
]

_BUCKET_BY_ID = {b["id"]: b for b in ETF_HEATMAP_BUCKETS}

WINDOW_DAYS = {"1d": 1, "5d": 5, "20d": 20}


def parse_window(window: str) -> int:
    key = (window or "5d").strip().lower()
    if key not in WINDOW_DAYS:
        raise ValueError(f"window must be one of {list(WINDOW_DAYS)}")
    return WINDOW_DAYS[key]


def to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if Decimal128 is not None and isinstance(v, Decimal128):
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception:
            pass
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def price_for_inflow(row: Mapping[str, Any]) -> Optional[float]:
    nav = to_float(row.get("nav"))
    if nav is not None and nav > 0:
        return nav
    close = to_float(row.get("close"))
    if close is not None and close > 0:
        return close
    # Tushare etf_share_size commonly returns only total_share (万份) and
    # total_size (万元). Their ratio is the same-day implied NAV (元/份), so
    # retain the share-flow calculation without requiring a second price join.
    total_share = to_float(row.get("total_share"))
    total_size = to_float(row.get("total_size"))
    if (
        total_share is not None
        and total_share > 0
        and total_size is not None
        and total_size > 0
    ):
        return total_size / total_share
    return None


def daily_net_inflow_wan(row_t: Mapping[str, Any], row_prev: Mapping[str, Any]) -> Optional[float]:
    """Net subscription in 万元: delta total_share (万份) * nav/close (元/份)."""
    share_t = to_float(row_t.get("total_share"))
    share_prev = to_float(row_prev.get("total_share"))
    if share_t is None or share_prev is None:
        return None
    px = price_for_inflow(row_t)
    if px is None:
        return None
    return (share_t - share_prev) * px


def pick_top_members(
    candidates: Sequence[str],
    as_of: str,
    rows_by_code: Mapping[str, Mapping[str, Any]],
    limit: int = 3,
) -> List[str]:
    scored: List[Tuple[str, float]] = []
    for code in candidates:
        row = rows_by_code.get(code)
        if not row:
            continue
        size = to_float(row.get("total_size"))
        if size is None:
            continue
        scored.append((code, size))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [code for code, _ in scored[:limit]]


def aggregate_member_metrics(
    members: Sequence[str],
    trade_date: str,
    prev_date: Optional[str],
    share_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
) -> Dict[str, Any]:
    net = 0.0
    has_net = False
    total_share = 0.0
    total_size = 0.0
    has_share = False
    has_size = False
    member_details: List[Dict[str, Any]] = []

    for code in members:
        row_t = share_by_key.get((code, trade_date)) or {}
        row_prev = share_by_key.get((code, prev_date)) if prev_date else None
        m_net: Optional[float] = None
        if row_prev:
            m_net = daily_net_inflow_wan(row_t, row_prev)
        if m_net is not None:
            net += m_net
            has_net = True
        sh = to_float(row_t.get("total_share"))
        sz = to_float(row_t.get("total_size"))
        if sh is not None:
            total_share += sh
            has_share = True
        if sz is not None:
            total_size += sz
            has_size = True
        member_details.append(
            {
                "ts_code": code,
                "total_share": sh,
                "total_size": sz,
                "net_inflow": round(m_net, 4) if m_net is not None else None,
            }
        )

    return {
        "net_inflow": round(net, 4) if has_net else None,
        "total_share": round(total_share, 4) if has_share else None,
        "total_size": round(total_size, 4) if has_size else None,
        "members": member_details,
    }


def compute_bucket_window_cell(
    members: Sequence[str],
    trade_dates_desc: Sequence[str],
    window_days: int,
    share_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
) -> Dict[str, Any]:
    if not trade_dates_desc or not members:
        return {
            "net_inflow": None,
            "inflow_rate": None,
            "size_start": None,
            "share_start": None,
            "share_end": None,
            "share_chg_pct": None,
        }

    dates = list(trade_dates_desc[: window_days + 1])
    if len(dates) < 2:
        return {
            "net_inflow": None,
            "inflow_rate": None,
            "size_start": None,
            "share_start": None,
            "share_end": None,
            "share_chg_pct": None,
        }

    window_trade_dates = dates[:window_days]
    net_sum = 0.0
    has_any = False
    for i, td in enumerate(window_trade_dates):
        prev_td = dates[i + 1]
        agg = aggregate_member_metrics(members, td, prev_td, share_by_key)
        if agg["net_inflow"] is not None:
            net_sum += float(agg["net_inflow"])
            has_any = True

    end_td = window_trade_dates[0]
    start_td = dates[min(window_days, len(dates) - 1)]
    start_agg = aggregate_member_metrics(members, start_td, None, share_by_key)
    end_agg = aggregate_member_metrics(members, end_td, None, share_by_key)

    size_start = start_agg.get("total_size")
    share_start = start_agg.get("total_share")
    share_end = end_agg.get("total_share")

    inflow_rate: Optional[float] = None
    if has_any and size_start and size_start > 0:
        inflow_rate = net_sum / float(size_start)

    share_chg_pct: Optional[float] = None
    if share_start and share_end is not None and share_start > 0:
        share_chg_pct = (float(share_end) - float(share_start)) / float(share_start)

    return {
        "net_inflow": round(net_sum, 4) if has_any else None,
        "inflow_rate": round(inflow_rate, 6) if inflow_rate is not None else None,
        "size_start": size_start,
        "share_start": share_start,
        "share_end": share_end,
        "share_chg_pct": round(share_chg_pct, 6) if share_chg_pct is not None else None,
    }


@dataclass
class ShareSizeStore:
    """Thin wrapper over Mongo etf_share_size for heatmap queries."""

    coll: Any
    basic_coll: Any

    def latest_trade_date(self) -> Optional[str]:
        tip = self.coll.find_one({}, sort=[("trade_date", -1)], projection={"trade_date": 1})
        if not tip or not tip.get("trade_date"):
            return None
        return str(tip["trade_date"])

    def load_rows(self, codes: Iterable[str], start_date: str, end_date: str) -> List[Dict[str, Any]]:
        code_list = list(dict.fromkeys(codes))
        if not code_list:
            return []
        cursor = self.coll.find(
            {
                "ts_code": {"$in": code_list},
                "trade_date": {"$gte": start_date, "$lte": end_date},
            },
            {"_id": 0},
        )
        return list(cursor)

    def names_for(self, codes: Sequence[str]) -> Dict[str, str]:
        if not codes:
            return {}
        cursor = self.basic_coll.find({"ts_code": {"$in": list(codes)}}, {"_id": 0, "ts_code": 1, "name": 1})
        return {str(d["ts_code"]): str(d.get("name") or d["ts_code"]) for d in cursor if d.get("ts_code")}


def _index_share_rows(rows: Sequence[Mapping[str, Any]]) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], List[str]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    dates: set[str] = set()
    for raw in rows:
        code = raw.get("ts_code")
        td = raw.get("trade_date")
        if not code or not td:
            continue
        td_s = str(td)
        by_key[(str(code), td_s)] = dict(raw)
        dates.add(td_s)
    return by_key, sorted(dates, reverse=True)


def _lookback_start(as_of: str, window_days: int) -> str:
    """Calendar lookback wide enough for window trading days (share_size is sparse-safe)."""
    y, m, d = int(as_of[:4]), int(as_of[4:6]), int(as_of[6:8])
    # ~2 calendar days per trading day + buffer
    back = max(window_days * 3 + 10, 30)
    from datetime import date, timedelta

    dt = date(y, m, d) - timedelta(days=back)
    return dt.strftime("%Y%m%d")


def build_heatmap_payload(
    store: ShareSizeStore,
    *,
    window: str = "5d",
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    window_days = parse_window(window)
    as_of = (trade_date or "").strip().replace("-", "")[:8] or store.latest_trade_date()
    if not as_of:
        return {
            "success": True,
            "trade_date": None,
            "share_as_of": None,
            "window": window,
            "cells": [],
            "message": "etf_share_size 集合无数据，请先运行 quant-data-engine 同步任务",
        }

    all_candidates: List[str] = []
    for bucket in ETF_HEATMAP_BUCKETS:
        all_candidates.extend(bucket["candidates"])
    all_candidates = list(dict.fromkeys(all_candidates))

    start = _lookback_start(as_of, window_days)
    rows = store.load_rows(all_candidates, start, as_of)
    share_by_key, all_dates_desc = _index_share_rows(rows)
    trade_dates_desc = [d for d in all_dates_desc if d <= as_of]
    if not trade_dates_desc:
        return {
            "success": True,
            "trade_date": as_of,
            "share_as_of": as_of,
            "window": window,
            "cells": [],
            "message": f"trade_date={as_of} 无份额数据",
        }

    as_of_rows = {code: share_by_key.get((code, as_of), {}) for code in all_candidates}

    cells: List[Dict[str, Any]] = []
    for bucket in ETF_HEATMAP_BUCKETS:
        members = pick_top_members(bucket["candidates"], as_of, as_of_rows)
        if not members:
            cells.append(
                {
                    "bucket_id": bucket["id"],
                    "name": bucket["name"],
                    "kind": bucket["kind"],
                    "net_inflow": None,
                    "inflow_rate": None,
                    "size_start": None,
                    "share_chg_pct": None,
                    "members": [],
                    "flagship_ts_code": None,
                }
            )
            continue

        window_metrics = compute_bucket_window_cell(members, trade_dates_desc, window_days, share_by_key)
        end_td = trade_dates_desc[0]
        member_rows: List[Dict[str, Any]] = []
        for code in members:
            row = share_by_key.get((code, end_td), {})
            member_net = None
            if len(trade_dates_desc) >= 2:
                prev = trade_dates_desc[1]
                m = aggregate_member_metrics([code], end_td, prev, share_by_key)
                member_net = m.get("net_inflow")
            member_rows.append(
                {
                    "ts_code": code,
                    "name": None,
                    "total_size": to_float(row.get("total_size")),
                    "net_inflow": member_net,
                }
            )

        cells.append(
            {
                "bucket_id": bucket["id"],
                "name": bucket["name"],
                "kind": bucket["kind"],
                "net_inflow": window_metrics["net_inflow"],
                "inflow_rate": window_metrics["inflow_rate"],
                "size_start": window_metrics["size_start"],
                "share_chg_pct": window_metrics["share_chg_pct"],
                "members": member_rows,
                "flagship_ts_code": members[0],
            }
        )

    names = store.names_for([m["ts_code"] for c in cells for m in c.get("members", []) if m.get("ts_code")])
    for cell in cells:
        for m in cell.get("members", []):
            code = m.get("ts_code")
            if code:
                m["name"] = names.get(code, code)

    return {
        "success": True,
        "trade_date": as_of,
        "share_as_of": as_of,
        "window": window,
        "cells": cells,
    }


def build_bucket_series_payload(
    store: ShareSizeStore,
    bucket_id: str,
    *,
    limit: int = 250,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    bucket = _BUCKET_BY_ID.get(bucket_id)
    if not bucket:
        raise KeyError(bucket_id)

    as_of = (trade_date or "").strip().replace("-", "")[:8] or store.latest_trade_date()
    if not as_of:
        return {
            "success": True,
            "bucket_id": bucket_id,
            "name": bucket["name"],
            "kind": bucket["kind"],
            "trade_date": None,
            "members": [],
            "series": [],
            "message": "etf_share_size 集合无数据",
        }

    start = _lookback_start(as_of, max(limit, 60))
    rows = store.load_rows(bucket["candidates"], start, as_of)
    share_by_key, all_dates_desc = _index_share_rows(rows)
    trade_dates_desc = [d for d in all_dates_desc if d <= as_of]
    as_of_rows = {code: share_by_key.get((code, as_of), {}) for code in bucket["candidates"]}
    members = pick_top_members(bucket["candidates"], as_of, as_of_rows)
    if not members:
        return {
            "success": True,
            "bucket_id": bucket_id,
            "name": bucket["name"],
            "kind": bucket["kind"],
            "trade_date": as_of,
            "members": [],
            "series": [],
            "message": "该主题在 as-of 日无可用 ETF 份额数据",
        }

    flagship = members[0]
    series_dates = trade_dates_desc[: limit + 1]
    if len(series_dates) < 2:
        return {
            "success": True,
            "bucket_id": bucket_id,
            "name": bucket["name"],
            "kind": bucket["kind"],
            "trade_date": as_of,
            "members": members,
            "flagship_ts_code": flagship,
            "series": [],
        }

    out_series: List[Dict[str, Any]] = []
    for i in range(min(limit, len(series_dates) - 1)):
        td = series_dates[i]
        prev_td = series_dates[i + 1]
        agg = aggregate_member_metrics(members, td, prev_td, share_by_key)
        flagship_row = share_by_key.get((flagship, td), {})
        out_series.append(
            {
                "trade_date": td,
                "net_inflow": agg.get("net_inflow"),
                "total_share": agg.get("total_share"),
                "total_size": agg.get("total_size"),
                "flagship_close": price_for_inflow(flagship_row),
            }
        )

    out_series.reverse()
    names = store.names_for(members)
    member_meta = [{"ts_code": c, "name": names.get(c, c)} for c in members]

    return {
        "success": True,
        "bucket_id": bucket_id,
        "name": bucket["name"],
        "kind": bucket["kind"],
        "trade_date": as_of,
        "share_as_of": as_of,
        "members": member_meta,
        "flagship_ts_code": flagship,
        "member_lock_note": "成员按 as-of 日规模取前 3 锁定，历史序列不按日换马",
        "series": out_series,
    }


__all__ = [
    "ETF_HEATMAP_BUCKETS",
    "ShareSizeStore",
    "build_bucket_series_payload",
    "build_heatmap_payload",
    "compute_bucket_window_cell",
    "daily_net_inflow_wan",
    "parse_window",
    "pick_top_members",
    "to_float",
]
