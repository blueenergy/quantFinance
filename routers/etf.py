from fastapi import APIRouter, Query, HTTPException, Depends
from typing import Any, Dict, List, Optional
from db import db, data_db, analyzer_db
import pandas as pd
from auth import get_current_active_user

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
    For weekly/monthly bars we need enough daily points before resampling.
    With an explicit date range, cap raw daily rows; otherwise use heuristics.
    """
    if has_date_filter:
        return min(ETF_KLINE_DAILY_CAP_WITH_DATE_RANGE, max(int(user_limit) * 35, 800))
    if tf in ("1w", "1m", "w", "m"):
        return min(5_000, max(int(user_limit) * 35, 800))
    return int(user_limit)


def _etf_kline_date_truncation_message(
    has_date_filter: bool, n_daily: int, cap: int
) -> Optional[str]:
    """有起止且拉满 cap 时提示，避免用户以为区间內日级别数据完整。"""
    if not has_date_filter or n_daily < cap:
        return None
    return (
        f"日期区间内已按最多 {cap} 条日线拉取；若区间极宽，K 线可能未覆盖全部交易日"
    )


def _agg_etf_ohlc_to_df(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample pre-adjusted daily rows to week/month.
    - OHLC + pre_adj_*: first|max|min|last; vol/amount: sum; fd_share: last.
    - Same as shenwan_index _agg: W-FRI / month-end. Input df: ascending trade_date.
    """
    if df is None or df.empty or "trade_date" not in df.columns:
        return pd.DataFrame()
    d = df.copy()
    d["td"] = pd.to_datetime(d["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    d = d.dropna(subset=["td"]).set_index("td").sort_index()
    ag: Dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last"}
    ohlc_cols: Dict[str, str] = {}
    for c, how in ag.items():
        if c in d.columns:
            ohlc_cols[c] = how
    for base in ("open", "high", "low", "close"):
        pac = f"pre_adj_{base}"
        if pac in d.columns and pac not in ohlc_cols:
            ohlc_cols[pac] = ag[base]
    if not ohlc_cols:
        return pd.DataFrame()
    if rule not in ("1w", "1m"):
        return pd.DataFrame()
    rule_s = "W-FRI" if rule == "1w" else "ME"
    rs_kw = {"label": "right", "closed": "right"}
    ohlc = d.resample(rule_s, **rs_kw).agg(ohlc_cols)
    for col, method in (("vol", "sum"), ("amount", "sum"), ("fd_share", "last")):
        if col not in d.columns:
            continue
        s = d[col].resample(rule_s, **rs_kw)
        ohlc[col] = s.sum() if method == "sum" else s.last()
    _sub = [c for c in ("close", "pre_adj_close") if c in ohlc.columns]
    if _sub:
        ohlc = ohlc.dropna(how="all", subset=_sub)
    return ohlc


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
    window: str = Query("5d", description="1d, 5d, or 20d net inflow window"),
    trade_date: Optional[str] = Query(
        None,
        description="As-of trade_date YYYYMMDD; default latest in etf_share_size",
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
        description="As-of trade_date YYYYMMDD; default latest in etf_share_size",
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
):
    """
    Get ETF K-line: daily rows, or weekly/monthly resampled from pre-adjusted dailies.
    """
    tfc = (tf or "1d").strip().lower()
    if tfc in ("d",):
        tfc = "1d"
    if tfc in ("w",):
        tfc = "1w"
    if tfc in ("m",):
        tfc = "1m"
    if tfc not in ("1d", "1w", "1m"):
        raise HTTPException(400, "tf must be 1d, 1w, or 1m")

    # Base query for the specific ETF
    query = {"ts_code": symbol}
    
    # Add date filters if provided
    date_filter: Dict[str, str] = {}
    if start_date:
        date_filter["$gte"] = start_date.replace('-', '')
    if end_date:
        date_filter["$lte"] = end_date.replace('-', '')
    has_date_filter = bool(date_filter)        
    if date_filter:
        query["trade_date"] = date_filter

    fetch_limit = _etf_kline_fetch_limit(tfc, int(limit), has_date_filter)
        
    # Execute queries synchronously (pymongo client is sync)
    daily_results = list(data_db["etf_daily"].find(query, {"_id": 0}).sort("trade_date", -1).limit(fetch_limit))
    adj_results = list(data_db["etf_adj_factor"].find(query, {"_id": 0}).sort("trade_date", -1).limit(fetch_limit))
    share_results = list(data_db["etf_shares"].find(query, {"_id": 0}).sort("trade_date", -1).limit(fetch_limit))

    n_daily = len(daily_results)
    date_trunc_msg = _etf_kline_date_truncation_message(has_date_filter, n_daily, fetch_limit)
    
    if not daily_results:
        out0: Dict[str, Any] = {"success": True, "symbol": symbol, "tf": tfc, "count": 0, "data": []}
        if date_trunc_msg:
            out0["message"] = date_trunc_msg
        return out0
        
    # Convert to pandas DataFrames for easy merging and calculation
    df_daily = pd.DataFrame(daily_results)
    # Mongo 可能存 Decimal128 / 字符串，与 float 运算会 TypeError → 接口 500
    for col in ("open", "high", "low", "close", "vol", "amount"):
        if col in df_daily.columns:
            df_daily[col] = pd.to_numeric(df_daily[col], errors="coerce")

    # --- 1. Process Adjustment Factors (Pre-adjustment) ---
    if adj_results:
        df_adj = pd.DataFrame(adj_results)
        if "adj_factor" in df_adj.columns:
            df_adj["adj_factor"] = pd.to_numeric(df_adj["adj_factor"], errors="coerce")
        # Merge on trade_date
        df = pd.merge(df_daily, df_adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
        
        # Forward fill adj_factors if some days are missing but earlier days exist
        df["adj_factor"] = df["adj_factor"].ffill().bfill().fillna(1.0)
        
        # Calculate pre-adjusted prices
        # Pre-adjusted price = Current Price * (Current Adj Factor / Latest Adj Factor)
        # Since we sorted by trade_date descending, the latest available is at index 0
        latest_adj_factor = df["adj_factor"].iloc[0]
        if pd.isna(latest_adj_factor) or float(latest_adj_factor) == 0.0:
            latest_adj_factor = 1.0
        
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col] * (df['adj_factor'] / latest_adj_factor)
            # Round to 3 decimal places for ETFs
            df[f'pre_adj_{col}'] = df[f'pre_adj_{col}'].round(3)
    else:
        df = df_daily
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col]

    # --- 2. Process Share Scale ---
    if share_results:
        df_share = pd.DataFrame(share_results)
        if "fd_share" in df_share.columns:
            df_share["fd_share"] = pd.to_numeric(df_share["fd_share"], errors="coerce")
        # Tushare fund_share might not be updated daily for all ETFs, typically week/month ends for some, daily for others.
        # We merge and forward fill the share scale so user always sees the 'latest known' scale.
        df = pd.merge(df, df_share[["trade_date", "fd_share"]], on="trade_date", how="left")
        
        # Need to sort by date ascending to safely forward fill shares into the future
        df = df.sort_values('trade_date', ascending=True)
        df["fd_share"] = df["fd_share"].ffill()
        
        # Sort back to descending for API output (1d)
        df = df.sort_values('trade_date', ascending=False)
    else:
        df['fd_share'] = None

    import math

    def _rows_to_json(df_in: pd.DataFrame) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in df_in.to_dict("records"):
            clean_row: Dict[str, Any] = {}
            for k, v in row.items():
                if k == "td":
                    continue
                if pd.isna(v) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                    clean_row[k] = None
                else:
                    if hasattr(v, "item"):
                        v = v.item()
                    clean_row[k] = v
            out.append(clean_row)
        return out

    if tfc in ("1w", "1m"):
        df_asc = df.sort_values("trade_date", ascending=True)
        ohlc_df = _agg_etf_ohlc_to_df(df_asc, tfc)
        if ohlc_df is None or ohlc_df.empty:
            out_emp: Dict[str, Any] = {
                "success": True,
                "symbol": symbol,
                "tf": tfc,
                "count": 0,
                "data": [],
            }
            if date_trunc_msg:
                out_emp["message"] = date_trunc_msg
            return out_emp
        ohlc_df = ohlc_df.reset_index()
        time_col = ohlc_df.columns[0]
        ohlc_df["trade_date"] = pd.to_datetime(ohlc_df[time_col], errors="coerce").map(
            lambda x: x.strftime("%Y%m%d") if pd.notna(x) else None
        )
        ohlc_df = ohlc_df.drop(columns=[time_col], errors="ignore")
        for _c in ohlc_df.columns:
            if str(_c).startswith("pre_adj_"):
                ohlc_df[_c] = ohlc_df[_c].round(3)
        ohlc_df = ohlc_df.sort_values("trade_date", ascending=True)
        ohlc_df = ohlc_df.tail(int(limit))
        ohlc_df = ohlc_df.sort_values("trade_date", ascending=False)
        merged_data = _rows_to_json(ohlc_df)
        out_wm: Dict[str, Any] = {
            "success": True,
            "symbol": symbol,
            "tf": tfc,
            "count": len(merged_data),
            "data": merged_data,
        }
        if date_trunc_msg:
            out_wm["message"] = date_trunc_msg
        return out_wm

    merged_data = []
    for row in df.to_dict('records'):
        clean_row = {}
        for k, v in row.items():
            if pd.isna(v) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                clean_row[k] = None
            else:
                # specifically convert NumPy floats to regular Python floats
                if hasattr(v, 'item'):
                    v = v.item()
                clean_row[k] = v
        merged_data.append(clean_row)

    out_d: Dict[str, Any] = {
        "success": True,
        "symbol": symbol,
        "tf": tfc,
        "count": len(merged_data),
        "data": merged_data,
    }
    if date_trunc_msg:
        out_d["message"] = date_trunc_msg
    return out_d

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
