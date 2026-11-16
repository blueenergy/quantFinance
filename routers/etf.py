from fastapi import APIRouter, Query, HTTPException, Depends
from typing import Any, Dict, List, Optional
from db import db, data_db, analyzer_db
from auth import get_current_active_user
from services.etf_kline import map_etf_daily_row
from services.kline_resample import (
    VALID_TF,
    aggregate_rows,
    fill_pre_close,
    normalize_tf,
)
from routers.records import _apply_one_symbol

router = APIRouter(prefix="/etf", tags=["etf"])

# 核心推荐ETF（若DB未同步全量，至少保证这些显示）
FEATURED_ETF_CODES = [
    # ----- 宽基指数 -----
    "510300.SH",  # 沪淳300ETF
    "510500.SH",  # 中证500ETF
    "588000.SH",  # 科创50ETF
    "159949.SZ",  # 创业板100ETF
    "510050.SH",  # 上证50ETF
    "159919.SZ",  # 氪淳300ETF（易方达）
    "159922.SZ",  # 中证500ETF（易方达）
    "512100.SH",  # 中证1000ETF
    "159852.SZ",  # A500ETF
    # ----- 主题/行业 -----
    "512760.SH",  # 半导体ETF
    "159995.SZ",  # 芯片ETF
    "515790.SH",  # 新能源车ETF
    "512880.SH",  # 证券ETF
    "512800.SH",  # 銀行ETF
    "512690.SH",  # 白酒ETF
    "159928.SZ",  # 消费ETF
    "516160.SH",  # 光伏ETF
    "512980.SH",  # 传媒ETF
    "159941.SZ",  # 纳斯达ETF
    "513500.SH",  # 标普500ETF
    "513100.SH",  # 纳斯达ETF（华安）
]

ETF_SEARCH_ALIASES = {
    "300": ["510300.SH", "159919.SZ"],
    "沪深300": ["510300.SH", "159919.SZ"],
    "500": ["510500.SH", "159922.SZ"],
    "中证500": ["510500.SH", "159922.SZ"],
    "1000": ["512100.SH"],
    "中证1000": ["512100.SH"],
    "a500": ["159852.SZ"],
    "中证a500": ["159852.SZ"],
}

@router.get("/list")
async def get_etf_list(
    q: Optional[str] = Query(None, description="Search by symbol or name"),
    featured: bool = Query(False, description="Return curated featured ETFs when no search query"),
    limit: int = Query(50, ge=1, le=10000, description="Max rows; use with offset when sort is set for full-market lists"),
    offset: int = Query(0, ge=0, description="Skip rows (only when sort=total_size|total_share)"),
    sort: Optional[str] = Query(
        None,
        description="Scale sort: total_size or total_share (reads etf_share_size; ignored when q is set)",
    ),
    trade_date: Optional[str] = Query(
        None,
        description="YYYYMMDD for scale sort; default latest trade_date in etf_share_size",
    ),
):
    """
    Get a list of ETFs.
    - No params: returns first `limit` ETFs
    - ?featured=true: returns curated core ETFs (宽基+主要行业)
    - ?q=xxx: full-text search across all ETFs
    - ?sort=total_size|total_share: sort by Tushare etf_share_size field for one trade_date (no q);
      supports offset/limit pagination; response includes total for that trade_date.
    """
    import re, math

    sort_scale_trade_date: Optional[str] = None
    sort_key = (sort or "").strip().lower() if sort and not q else None
    if sort_key and sort_key not in ("total_size", "total_share"):
        raise HTTPException(status_code=400, detail="sort must be total_size or total_share")

    if sort_key:
        td_raw = (trade_date or "").strip().replace("-", "")[:8]
        if not td_raw:
            tip = data_db["etf_share_size"].find_one(
                {}, sort=[("trade_date", -1)], projection={"trade_date": 1}
            )
            if not tip or not tip.get("trade_date"):
                return {
                    "success": True,
                    "count": 0,
                    "featured": False,
                    "sort": sort_key,
                    "trade_date": None,
                    "data": [],
                    "message": "etf_share_size 集合无数据，请先运行 quant-data-engine 同步任务",
                }
            td = str(tip["trade_date"])
        else:
            td = td_raw
        sort_scale_trade_date = td
        total_for_td = data_db["etf_share_size"].count_documents({"trade_date": td})
        cursor = (
            data_db["etf_share_size"]
            .find({"trade_date": td}, {"_id": 0})
            .sort(sort_key, -1)
            .skip(offset)
            .limit(limit)
        )
        scale_rows = list(cursor)
        codes = [r["ts_code"] for r in scale_rows if r.get("ts_code")]
        basics = {
            b["ts_code"]: b
            for b in data_db["etf_basic"].find({"ts_code": {"$in": codes}}, {"_id": 0})
        }
        merged = []
        for r in scale_rows:
            ts = r.get("ts_code")
            base = dict(basics.get(ts, {}))
            base.update(r)
            merged.append(base)
        results = merged
    elif q:
        # Full-text search mode
        q_clean = q.strip()
        pattern = re.compile(re.escape(q_clean), re.IGNORECASE)
        alias_codes = ETF_SEARCH_ALIASES.get(q_clean.lower(), [])
        query = {
            "$or": [
                {"ts_code": pattern},
                {"name": pattern},
                {"benchmark": pattern},
                {"management": pattern},
                {"index_name": pattern},
                {"ts_code": {"$in": alias_codes}},
            ]
        }
        cursor = data_db["etf_basic"].find(query, {"_id": 0}).limit(limit)
        results = list(cursor)
    elif featured:
        # Featured mode: return curated list in defined order
        cursor = data_db["etf_basic"].find(
            {"ts_code": {"$in": FEATURED_ETF_CODES}}, {"_id": 0}
        )
        results = list(cursor)
    else:
        cursor = data_db["etf_basic"].find({}, {"_id": 0}).limit(limit)
        results = list(cursor)

    # Maintain featured order when in featured mode
    if featured and not q and not sort_key:
        order_map = {code: i for i, code in enumerate(FEATURED_ETF_CODES)}
        results.sort(key=lambda r: order_map.get(r.get("ts_code", ""), 999))

    clean_results = []
    for row in results:
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean_row[k] = None
            else:
                try:
                    from bson.decimal128 import Decimal128

                    if isinstance(v, Decimal128):
                        clean_row[k] = float(str(v))
                        continue
                except Exception:
                    pass
                clean_row[k] = v.item() if hasattr(v, "item") else v
        clean_results.append(clean_row)

    out = {
        "success": True,
        "count": len(clean_results),
        "featured": featured and not q and not sort_key,
        "data": clean_results,
    }
    if sort_key:
        out["sort"] = sort_key
        out["trade_date"] = sort_scale_trade_date
        out["offset"] = offset
        out["limit"] = limit
        out["total"] = total_for_td if sort_scale_trade_date is not None else None
    return out


@router.get("/watchlist")
async def get_etf_watchlist(
    current_user: dict = Depends(get_current_active_user)
):
    """
    Get the current user's ETF watchlist with latest price data.
    Returns only ETFs (ts_codes with .SH/.SZ) from the user's watchlist.
    """
    from db import get_db_collections
    collections = get_db_collections()
    user_id = str(current_user["id"])
    doc = collections["user_watchlists"].find_one({"user_id": user_id})
    all_symbols = doc["symbols"] if doc and "symbols" in doc else []

    # Filter only ETF codes (contain a dot, e.g. 510300.SH)
    etf_symbols = [s for s in all_symbols if "." in s]

    result = []
    for ts_code in etf_symbols:
        basic = data_db["etf_basic"].find_one({"ts_code": ts_code}, {"_id": 0})
        latest = data_db["etf_daily"].find_one({"ts_code": ts_code}, sort=[("trade_date", -1)])
        result.append({
            "ts_code": ts_code,
            "name": (basic or {}).get("name", ts_code),
            "close": (latest or {}).get("close"),
            "trade_date": (latest or {}).get("trade_date"),
            "vol": (latest or {}).get("vol"),
            "amount": (latest or {}).get("amount"),
        })

    return {"success": True, "count": len(result), "data": result}


# 带起止日期时 Mongo 拉取日线的条数上限（与无日期时的 heuristics 区分，避免 min(2e4,1e4) 等困惑写法）
ETF_KLINE_DAILY_CAP_WITH_DATE_RANGE = 20_000


def _etf_kline_fetch_limit(tf: str, user_limit: int, has_date_filter: bool) -> int:
    """
    How many daily rows to pull.

    ``1d`` needs one extra older day to compute ``pre_close``; ``1w`` / ``1m``
    need enough days for the requested bars plus one extra bucket so the
    oldest returned bar also has a ``pre_close``.
    """
    limit = max(int(user_limit), 1)
    base = limit * 35 + 35 if tf in ("1w", "1m") else limit + 1
    if has_date_filter:
        return min(ETF_KLINE_DAILY_CAP_WITH_DATE_RANGE, max(base, 800))
    if tf in ("1w", "1m"):
        return min(5_000 + 35, max(base, 800))
    return base


def _etf_kline_date_truncation_message(
    has_date_filter: bool, n_daily: int, cap: int
) -> Optional[str]:
    """有起止且拉满 cap 时提示，避免用户以为区间內日级别数据完整。"""
    if not has_date_filter or n_daily < cap:
        return None
    return (
        f"日期区间内已按最多 {cap} 条日线拉取；若区间极宽，K 线可能未覆盖全部交易日"
    )


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        from bson.decimal128 import Decimal128

        if isinstance(value, Decimal128):
            value = float(str(value))
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _attach_fd_share(rows: List[Dict[str, Any]], fetch_limit: int, query: Dict[str, Any]) -> None:
    """Attach ``fd_share`` (万份) to daily rows, carried forward from older days.

    Tushare ``fund_share`` is not published for every ETF every day; showing
    the last known scale beats a hole in the third grid.
    """
    share_docs = list(
        data_db["etf_shares"]
        .find(query, {"_id": 0, "trade_date": 1, "fd_share": 1})
        .sort("trade_date", -1)
        .limit(fetch_limit)
    )
    share_by_date = {
        str(doc.get("trade_date")): _num(doc.get("fd_share")) for doc in share_docs
    }
    last_share: Optional[float] = None
    # rows are newest-first, so walking backwards goes forward in time
    for row in reversed(rows):
        value = share_by_date.get(str(row.get("trade_date")))
        if value is not None:
            last_share = value
        row["fd_share"] = last_share


def _sanitize_json_value(v: Any) -> Any:
    import math

    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    try:
        from bson.decimal128 import Decimal128

        if isinstance(v, Decimal128):
            return float(str(v))
    except Exception:
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v


def _sanitize_json_tree(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_json_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json_tree(x) for x in obj]
    return _sanitize_json_value(obj)


@router.get("/heatmap")
async def get_etf_heatmap(
    window: str = Query("5d", description="1d, 5d, 20d, 60d, 120d, or ytd net inflow window"),
    trade_date: Optional[str] = Query(
        None,
        description="As-of trade_date YYYYMMDD; default coverage date among heatmap ETFs",
    ),
):
    """Cross-section heatmap: themed ETF buckets ranked by net subscription inflow."""
    from services.etf_heatmap import ShareSizeStore, build_heatmap_payload, parse_window

    try:
        parse_window(window)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    store = ShareSizeStore(data_db["etf_share_size"], data_db["etf_basic"])
    payload = build_heatmap_payload(store, window=window, trade_date=trade_date)
    return _sanitize_json_tree(payload)


@router.get("/heatmap/{bucket_id}/series")
async def get_etf_heatmap_series(
    bucket_id: str,
    limit: int = Query(250, ge=1, le=500),
    trade_date: Optional[str] = Query(
        None,
        description="As-of trade_date YYYYMMDD; default coverage date among heatmap ETFs",
    ),
):
    """Time series for one bucket: daily net inflow bars + aggregate share line."""
    from services.etf_heatmap import ShareSizeStore, build_bucket_series_payload

    store = ShareSizeStore(data_db["etf_share_size"], data_db["etf_basic"])
    try:
        payload = build_bucket_series_payload(
            store, bucket_id, limit=limit, trade_date=trade_date
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown heatmap bucket: {bucket_id}")
    return _sanitize_json_tree(payload)


@router.get("/{symbol}/kline")
async def get_etf_kline(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 1000,
    tf: str = Query("1d", description="1d daily, 1w weekly, 1m monthly (from daily bars)"),
    adjust: str = Query("qfq", description="复权口径: qfq 前复权 / hfq 后复权 / none 不复权"),
):
    """
    ETF K-line rows shaped like ``/records/``: prices are already in ``adjust`` 口径.

    - ``1d``: ``pre_close`` / ``pct_chg`` vs the previous trading day.
    - ``1w`` / ``1m``: resampled from adjusted daily bars; the bar's
      ``trade_date`` is the last trading day inside the period (never a future
      or non-trading date such as pandas' W-FRI / ME labels).
    """
    tfc = normalize_tf(tf)
    if tfc not in VALID_TF:
        raise HTTPException(400, "tf must be 1d, 1w, or 1m")
    adj = str(adjust or "qfq").strip().lower()
    if adj not in ("qfq", "hfq", "none"):
        adj = "qfq"

    # Base query for the specific ETF
    query: Dict[str, Any] = {"ts_code": symbol}
    date_filter: Dict[str, str] = {}
    if start_date:
        date_filter["$gte"] = start_date.replace("-", "")
    if end_date:
        date_filter["$lte"] = end_date.replace("-", "")
    has_date_filter = bool(date_filter)
    if date_filter:
        query["trade_date"] = date_filter

    fetch_limit = _etf_kline_fetch_limit(tfc, int(limit), has_date_filter)

    # Execute queries synchronously (pymongo client is sync)
    daily_results = list(
        data_db["etf_daily"].find(query, {"_id": 0}).sort("trade_date", -1).limit(fetch_limit)
    )
    n_daily = len(daily_results)
    date_trunc_msg = _etf_kline_date_truncation_message(has_date_filter, n_daily, fetch_limit)

    result: Dict[str, Any] = {
        "success": True,
        "symbol": symbol,
        "tf": tfc,
        "adjust": adj,
        "count": 0,
        "data": [],
    }
    if date_trunc_msg:
        result["message"] = date_trunc_msg
    if not daily_results:
        return result

    rows = [map_etf_daily_row(doc, symbol=symbol) for doc in daily_results]
    _attach_fd_share(rows, fetch_limit, query)

    # qfq/hfq/none applied in place to open/high/low/close — the same rule and
    # the same anchor (latest adj_factor in the collection) as /records/.
    rows = _apply_one_symbol(rows, symbol, adj, data_db["etf_adj_factor"])

    if tfc == "1d":
        data = fill_pre_close(rows)[: int(limit)]
    else:
        data = aggregate_rows(rows, tfc)[: int(limit)]

    for bar in data:
        for col in ("open", "high", "low", "close", "pre_close"):
            value = bar.get(col)
            if value is not None:
                bar[col] = round(float(value), 3)
        if bar.get("pct_chg") is not None:
            bar["pct_change"] = bar["pct_chg"]

    result["count"] = len(data)
    result["data"] = _sanitize_json_tree(data)
    return result

from pydantic import BaseModel
from auth import get_current_active_user
from fastapi import Depends
from bson import ObjectId
from datetime import datetime

class AnalyzeRequest(BaseModel):
    symbol: str
    force: bool = False


def _etf_symbol_variants(symbol: str) -> List[str]:
    s = (symbol or "").strip().upper()
    if not s:
        return []
    raw = s.split(".", 1)[0]
    out: List[str] = []
    for item in (s, raw, f"{raw}.SH", f"{raw}.SZ"):
        if item and item not in out:
            out.append(item)
    return out


def _resolve_etf_ts_code(symbol: str) -> Optional[str]:
    variants = _etf_symbol_variants(symbol)
    if not variants:
        return None
    basic = data_db["etf_basic"].find_one({"ts_code": {"$in": variants}})
    if basic and basic.get("ts_code"):
        return str(basic["ts_code"]).strip().upper()
    raw = variants[1] if len(variants) > 1 else variants[0].split(".", 1)[0]
    if len(raw) == 6 and raw.isdigit():
        basic = data_db["etf_basic"].find_one({"ts_code": {"$regex": f"^{raw}\\."}})
        if basic and basic.get("ts_code"):
            return str(basic["ts_code"]).strip().upper()
    return None


@router.post("/analyze", summary="手动触发ETF智能分析")
async def submit_etf_analysis(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """Submit an ETF analysis task to the background worker (quantAnalyzer)."""
    symbol = _resolve_etf_ts_code(request.symbol)
    if not symbol:
        raise HTTPException(status_code=404, detail=f"ETF {request.symbol.upper()} not found")
    basic = data_db["etf_basic"].find_one({"ts_code": symbol})
    if not basic:
        raise HTTPException(status_code=404, detail=f"ETF {symbol} not found")

    # Check if a task is already running
    existing = db["analysis_tasks"].find_one({
        "type": "etf_analysis",
        "params.symbol": symbol,
        "status": {"$in": ["pending", "processing"]},
    })
    
    if existing:
        return {
            "success": True,
            "task_id": str(existing["_id"]),
            "message": "已有该ETF的分析任务正在处理中",
            "status": existing["status"],
        }

    task_doc = {
        "user_id": current_user.get("username", "unknown"),
        "type": "etf_analysis",
        "priority": 20,
        "status": "pending",
        "params": {
            "symbol": symbol,
            "force": request.force,
        },
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    
    result = db["analysis_tasks"].insert_one(task_doc)

    return {
        "success": True,
        "task_id": str(result.inserted_id),
        "message": f"ETF分析任务已提交 ({symbol})",
        "status": "pending",
    }


@router.get("/task/{task_id}", summary="查询分析任务状态")
async def get_etf_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Poll task status."""
    try:
        task = db["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(400, "Invalid task_id")

    if not task:
        raise HTTPException(404, "Task not found")

    response = {
        "success": True,
        "task_id": task_id,
        "status": task.get("status"),
        "created_at": task.get("created_at").isoformat() if task.get("created_at") else None,
    }

    if task["status"] == "completed":
        result = task.get("result", {})
        response["analysis"] = result.get("analysis")
        response["symbol"] = result.get("ts_code")
        response["trade_date"] = result.get("trade_date")
        response["completed_at"] = (
            task.get("completed_at").isoformat() if task.get("completed_at") else None
        )
    elif task["status"] == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


@router.get("/{symbol}/analysis", summary="获取ETF最新智能分析结果")
async def get_etf_latest_analysis(
    symbol: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get the most recent cached analysis for an ETF."""
    symbol = symbol.upper()
    doc = analyzer_db["etf_analysis"].find_one(
        {"ts_code": symbol}, 
        sort=[("created_at", -1)]
    )
    
    if not doc:
        return {
            "success": False,
            "error": f"暂无 {symbol} 的深度分析报告，请点击分析按钮生成。",
        }

    return {
        "success": True,
        "symbol": symbol,
        "trade_date": doc.get("trade_date"),
        "latest_kline_date": doc.get("latest_kline_date"),
        "analysis": doc.get("analysis"),
        "model": doc.get("model"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }
