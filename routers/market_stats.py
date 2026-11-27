"""Market statistics gallery API — 两融余额, index PE and ETF penetration."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_data_db
from services.etf_penetration import DEFAULT_DAYS, DEFAULT_INDEX, compute_penetration
from services.market_turnover import (
    BOARDS,
    ETF_TYPES,
    MA_WINDOW,
    attach_ma_and_ratio,
    build_intraday_profile,
    profile_fraction,
    project_full_day,
    serialize_daily_row,
    to_float,
)
from services.market_stats import (
    ALLA_CODE,
    MARGIN_ALL_EXCHANGE,
    MARGIN_DELTA_WINDOW,
    PE_INDEX_LABELS,
    attach_window_delta,
    lookback_start,
    merge_pe_by_code,
    pe_index_sort_key,
    pick_pe_index,
    serialize_margin_row,
    serialize_pe_row,
)

router = APIRouter(prefix="/market-stats", tags=["市场统计"])


def _empty_overview() -> Dict[str, Any]:
    return {
        "pe": {
            "index_code": None,
            "index_name": None,
            "available_indices": [],
            "latest": None,
            "series": [],
        },
        "margin": {
            "latest": None,
            "series": [],
        },
    }


@router.get("/overview", summary="市场统计图数据（两融 + 指数市盈率）")
async def get_market_stats_overview(
    days: int = Query(750, ge=60, le=2500, description="返回最近 N 个交易日"),
    pe_index: Optional[str] = Query(None, description="指数代码，如 ALLA.WI / 000001.SH；缺省按全A自算→上证→沪深300→创业板 回退"),
    current_user: dict = Depends(get_current_active_user),
):
    data = get_data_db()
    today = datetime.today().strftime("%Y%m%d")
    start = lookback_start(today, days + MARGIN_DELTA_WINDOW)
    payload = _empty_overview()

    pe_coll = data.get("index_daily_basic")
    alla_coll = data.get("market_pe_daily")
    index_docs: List[Dict[str, Any]] = []
    alla_docs: List[Dict[str, Any]] = []
    pe_projection = {
        "_id": 0,
        "ts_code": 1,
        "trade_date": 1,
        "pe": 1,
        "pe_ttm": 1,
        "pb": 1,
        "close": 1,
        "total_mv": 1,
        "n_universe": 1,
        "n_used": 1,
        "n_loss": 1,
        "n_missing": 1,
        "method": 1,
        "universe": 1,
    }
    if pe_coll is not None:
        index_docs = list(
            pe_coll.find({"trade_date": {"$gte": start}}, pe_projection).sort("trade_date", 1)
        )
    if alla_coll is not None:
        alla_docs = list(
            alla_coll.find(
                {"ts_code": ALLA_CODE, "trade_date": {"$gte": start}},
                pe_projection,
            ).sort("trade_date", 1)
        )
    by_code = merge_pe_by_code(index_docs, alla_docs)
    if by_code:
        available = [
            {
                "ts_code": code,
                "name": PE_INDEX_LABELS.get(code, code),
                "points": len(rows),
            }
            for code, rows in sorted(by_code.items(), key=lambda item: (pe_index_sort_key(item[0]), item[0]))
        ]
        chosen, name = pick_pe_index(by_code.keys(), pe_index)
        series_docs = by_code.get(chosen or "", [])[-days:]
        series = [serialize_pe_row(doc) for doc in series_docs]
        payload["pe"] = {
            "index_code": chosen,
            "index_name": name,
            "available_indices": available,
            "latest": series[-1] if series else None,
            "series": series,
        }

    margin_coll = data.get("margin_market_daily")
    if margin_coll is not None:
        margin_docs = list(
            margin_coll.find(
                {"exchange_id": MARGIN_ALL_EXCHANGE, "trade_date": {"$gte": start}},
                {"_id": 0, "trade_date": 1, "rzye": 1, "rqye": 1, "rzrqye": 1},
            ).sort("trade_date", 1)
        )
        with_delta = attach_window_delta(margin_docs, field="rzrqye", window=MARGIN_DELTA_WINDOW)
        series_docs = with_delta[-days:]
        series = [serialize_margin_row(doc) for doc in series_docs]
        payload["margin"] = {
            "latest": series[-1] if series else None,
            "series": series,
        }

    return payload


@router.get("/etf-penetration", summary="头部宽基 ETF 渗透率（ETF 规模 / 指数流通市值）")
async def get_etf_penetration(
    index: str = Query(
        DEFAULT_INDEX,
        description="宽基指数 key：hs300 / sse50 / csi500 / csi1000 / gem / star50",
    ),
    days: int = Query(
        DEFAULT_DAYS,
        ge=0,
        le=10000,
        description="返回最近 N 个有效交易日；0 表示全部历史",
    ),
    current_user: dict = Depends(get_current_active_user),
):
    """Head broad-index ETF AUM as a share of the index float market cap.

    Both sides scale with the index level, so the ratio tracks share
    creation/redemption rather than price.  Only dates where every ETF in the
    curated head pool has shares and a close are emitted; the latest point is
    labelled by its own trade date (share data is T+1).
    """
    try:
        payload = compute_penetration(get_data_db(), index_key=index, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload


def _turnover_daily_payload(docs: List[Dict[str, Any]], days: int, window: int) -> Dict[str, Any]:
    rows = [serialize_daily_row(doc) for doc in docs]
    attach_ma_and_ratio(rows, ["stock_total"] + [b["key"] for b in BOARDS], window=window)
    if days > 0:
        rows = rows[-days:]
    return {"rows": rows, "latest": rows[-1] if rows else None}


@router.get("/turnover", summary="分板块成交额与量能倍数（股票五板块 + ETF 三类）")
async def get_market_turnover(
    days: int = Query(250, ge=0, le=2500, description="返回最近 N 个交易日；0 表示全部"),
    window: int = Query(MA_WINDOW, ge=5, le=120, description="均线窗口（交易日）"),
    current_user: dict = Depends(get_current_active_user),
):
    """Daily turnover by board, with each series divided by its own trailing mean.

    Boards are mutually exclusive: 沪市 = 沪市主板 + 科创板, 深市 = 深市主板 +
    创业板. ETF is a separate instrument family and is never folded into the
    stock total — it is carried in its own fields so the two can be compared.
    """
    data = get_data_db()
    query: Dict[str, Any] = {}
    cursor = data["market_turnover_daily"].find(query, {"_id": 0}).sort("trade_date", 1)
    docs = list(cursor)
    if days > 0:
        docs = docs[-(days + window) :]
    payload = _turnover_daily_payload(docs, days, window)
    payload["boards"] = BOARDS
    payload["etf_types"] = ETF_TYPES
    payload["window"] = window
    return payload


@router.get("/turnover-intraday", summary="盘中分板块成交额与实时量能倍数")
async def get_market_turnover_intraday(
    days: int = Query(60, ge=5, le=250, description="构造同时点基准所用的历史交易日数"),
    window: int = Query(MA_WINDOW, ge=5, le=120, description="日频均线窗口"),
    date: Optional[str] = Query(
        None,
        description="指定交易日 YYYYMMDD；缺省为最近一个有盘中数据的交易日。历史日只有全市场合计，没有分板块",
    ),
    current_user: dict = Depends(get_current_active_user),
):
    """Today's cumulative turnover against the same clock time on prior days.

    The baseline is the distribution of ``cumulative / end_of_day`` observed at
    each clock time, so a 10:00 reading is compared with what 10:00 normally
    shows — not with a full-day mean.
    """
    data = get_data_db()
    intraday_coll = data["market_turnover_intraday"]
    daily_coll = data["market_turnover_daily"]

    # distinct, not a document cursor: one day holds ~120 snapshots, so taking the
    # first N documents of a descending sort yields a single date.
    all_dates = sorted(intraday_coll.distinct("trade_date"))
    if not all_dates:
        return {
            "session_date": None,
            "available_dates": [],
            "is_live": False,
            "profile": [],
            "cumulative": [],
            "boards": [],
            "projected": None,
        }

    session_date = date if date in all_dates else all_dates[-1]
    history_dates = [d for d in all_dates if d < session_date][-days:]

    day_series: Dict[str, List[Dict[str, Any]]] = {}
    for doc in intraday_coll.find(
        {"trade_date": {"$in": history_dates}}, {"_id": 0, "trade_date": 1, "snapshot_time": 1, "total": 1}
    ).sort("snapshot_time", 1):
        day_series.setdefault(doc["trade_date"], []).append(
            {"t": str(doc["snapshot_time"])[-4:], "total": to_float(doc.get("total"))}
        )
    # A day that stopped early (container restart, bridge stall) would report a
    # tiny end-of-day total and inflate every fraction. Keep days that reached
    # at least 14:30.
    day_series = {
        date: rows
        for date, rows in day_series.items()
        if rows and rows[-1]["t"] >= "1430" and to_float(rows[-1].get("total"))
    }
    profile = build_intraday_profile(day_series)

    today_rows = list(
        intraday_coll.find({"trade_date": session_date}, {"_id": 0}).sort("snapshot_time", 1)
    )
    cumulative = []
    for doc in today_rows:
        entry: Dict[str, Any] = {
            "t": str(doc["snapshot_time"])[-4:],
            "total": to_float(doc.get("total")),
            "coverage": doc.get("coverage"),
        }
        for board in BOARDS:
            entry[board["key"]] = to_float(doc.get(board["key"]))
        cumulative.append(entry)

    snapshot_time = str(today_rows[-1]["snapshot_time"]) if today_rows else None
    last_t = cumulative[-1]["t"] if cumulative else None
    fraction_entry = profile_fraction(profile, last_t) if last_t else None
    p50 = (fraction_entry or {}).get("p50")

    # Baseline: trailing daily mean, excluding today's own row.
    daily_docs = list(
        daily_coll.find({"trade_date": {"$lt": session_date}}, {"_id": 0}).sort("trade_date", -1).limit(window)
    )
    daily_docs.reverse()
    baseline_ma: Dict[str, Optional[float]] = {}
    for key in ["stock_total"] + [b["key"] for b in BOARDS]:
        values = [to_float(doc.get(key)) for doc in daily_docs]
        values = [value for value in values if value is not None]
        baseline_ma[key] = round(sum(values) / len(values), 2) if len(values) == window else None

    cumulative_total = cumulative[-1]["total"] if cumulative else None
    projected_total = project_full_day(cumulative_total, p50)

    def _ratio(cum_value: Optional[float], mean: Optional[float], fraction: Optional[float]) -> Optional[float]:
        if not cum_value or not mean or not fraction:
            return None
        return round(cum_value / (mean * fraction), 4)

    boards = []
    for board in BOARDS:
        board_cum = cumulative[-1].get(board["key"]) if cumulative else None
        if board_cum is None:
            continue
        boards.append(
            {
                "key": board["key"],
                "name": board["name"],
                "cum": board_cum,
                "ma20": baseline_ma.get(board["key"]),
                "ratio": _ratio(board_cum, baseline_ma.get(board["key"]), p50),
            }
        )

    return {
        "session_date": session_date,
        "available_dates": list(reversed(all_dates[-60:])),
        "is_live": session_date == datetime.today().strftime("%Y%m%d"),
        "snapshot_time": snapshot_time,
        "as_of_daily": daily_docs[-1]["trade_date"] if daily_docs else None,
        "is_final": bool(last_t and last_t >= "1500"),
        "cumulative": cumulative,
        "profile": profile,
        "baseline": {
            "ma20": baseline_ma.get("stock_total"),
            "window": window,
            "days": len(daily_docs),
            "profile_days": len(day_series),
        },
        "projected": {
            "total": projected_total,
            "fraction": p50,
            "ratio": _ratio(cumulative_total, baseline_ma.get("stock_total"), p50),
            "ratio_low": _ratio(cumulative_total, baseline_ma.get("stock_total"), (fraction_entry or {}).get("p90")),
            "ratio_high": _ratio(cumulative_total, baseline_ma.get("stock_total"), (fraction_entry or {}).get("p10")),
        },
        "boards": boards,
    }


__all__ = ["router"]
