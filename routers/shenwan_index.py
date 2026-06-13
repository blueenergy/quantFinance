"""
申万行业指数：从 quant_data 只读 sw_index_classify、sw_daily、sw_weekly、sw_monthly。
周/月优先查预计算集合，回退至日线 pandas 聚合。
无数据时返回 success+空数组与 message 提示回填补跑。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pymongo import ASCENDING, DESCENDING

from pydantic import BaseModel
from auth import get_current_active_user
from db import data_db, get_db_collections
from services.analysis_tasks import build_task_doc

router = APIRouter()

_CLASSIFY = data_db["sw_index_classify"]
_DAILY = data_db["sw_daily"]
_WEEKLY = data_db["sw_weekly"]
_MONTHLY = data_db["sw_monthly"]

MAX_KLINE_LIMIT = 1000
MAX_DAILY_LOOKBACK_DAYS = 4000  # ~10y+ 自然日
# Tushare sw_daily：涨跌幅/市值等字段在部分版本或旧回补中可能缺失
PROJ_KLINE = {
    "_id": 0,
    "ts_code": 1,
    "trade_date": 1,
    "name": 1,
    "open": 1,
    "close": 1,
    "high": 1,
    "low": 1,
    "vol": 1,
    "amount": 1,
    "change": 1,
    "pct_change": 1,
    "float_mv": 1,
    "total_mv": 1,
    "pe": 1,
    "pb": 1,
}


def _sw_daily_flt_ts_date_range(ts_code: str, start_s: str, end_s: str) -> Dict[str, Any]:
    """按 ts_code + 交易日起止查询 sw_daily；与入库一致，trade_date 为 YYYYMMDD 字符串比较。"""
    return {
        "ts_code": ts_code.strip(),
        "trade_date": {"$gte": start_s, "$lte": end_s},
    }


def _name_from_classify(d: Dict[str, Any]) -> str:
    return (d.get("industry_name") or d.get("name") or d.get("index_name") or "").strip() or d.get("index_code", "")


def _value_variants(x: Any) -> List[Any]:
    """Tushare/Mongo 中 industry_code 与 parent_code 可能为 int 或 str，统一为 $in 候选。"""
    if x is None or (isinstance(x, str) and not x.strip()):
        return []
    s = str(x).strip()
    if not s:
        return []
    out: List[Any] = [s]
    if s.lstrip("-").isdigit() or (s.startswith("-") and s[1:].isdigit()):
        try:
            out.append(int(s))
        except ValueError:
            pass
    elif isinstance(x, int) and not isinstance(x, bool):
        out = [x, s]
    return list(dict.fromkeys([v for v in out if v is not None and v != ""]))


def _looks_like_sw_index_code(p: str) -> bool:
    p = p.strip()
    if not p:
        return False
    u = p.upper()
    return u.endswith((".SI",)) or (".SH" in u) or (".BJ" in u) or (".SZ" in u) or p.endswith((".sh", ".sz", ".si"))


def _classify_row_item(d: Dict[str, Any], default_level: str) -> Dict[str, Any]:
    return {
        "index_code": d.get("index_code", ""),
        "industry_code": d.get("industry_code", ""),
        "name": _name_from_classify(d),
        "level": d.get("level", default_level),
        "parent_code": d.get("parent_code", ""),
    }


def _find_classify_parent(src: str, parent_param: str) -> Optional[Dict[str, Any]]:
    """先按父级 index_code 再按 industry_code 查父级行（Tushare 以 industry_code 链子级）。"""
    p = parent_param.strip()
    if not p:
        return None
    q = _CLASSIFY.find_one({"index_code": p, "src": src})
    if q:
        return q
    iv = _value_variants(p)
    if not iv:
        return None
    return _CLASSIFY.find_one({"src": src, "industry_code": {"$in": iv}})


def _parent_key_variants_for_children(fath: Dict[str, Any]) -> List[Any]:
    """
    子行 sw_index_classify.parent_code 对父的 industry_code（Tushare）；若无 industry 则回退为 index_code。
    为兼容错旧数据，两种键的变体都入 $in。
    """
    parts: List[Any] = []
    for k in ("industry_code", "index_code"):
        v = fath.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        for x in _value_variants(v):
            if x not in parts:
                parts.append(x)
    return parts


def _build_parent_code_child_filter(fath: Dict[str, Any], child_level: str, src: str) -> Dict[str, Any]:
    keys = _parent_key_variants_for_children(fath)
    if not keys:
        return {"src": src, "level": child_level, "parent_code": "__no_such_hierarchy__"}
    if len(keys) == 1:
        return {"src": src, "level": child_level, "parent_code": keys[0]}
    return {"src": src, "level": child_level, "parent_code": {"$in": keys}}


def _build_tree_level_filter(
    src: str,
    level: str,
    parent_param: str,
) -> Optional[Dict[str, Any]]:
    """
    L2/L3 列表：按父的 industry 链过滤；parent_param 可为 8010xx.SI 或行业码。
    失败时返回 None（调用方作空集）。
    """
    p = parent_param.strip()
    if not p:
        return None
    if _looks_like_sw_index_code(p):
        fath = _find_classify_parent(src, p)
        if not fath:
            return None
        return _build_parent_code_child_filter(fath, level, src)
    iv = _value_variants(p)
    if not iv:
        return None
    if len(iv) == 1:
        return {"src": src, "level": level, "parent_code": iv[0]}
    return {"src": src, "level": level, "parent_code": {"$in": iv}}


@router.get("/tree")
async def get_tree(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    src: str = Query("SW2021", description="申万版本 SW2014|SW2021 等，与入库一致"),
    level: str = Query("L1", pattern="^(L1|L2|L3)$"),
    parent_code: str = Query(
        "", description="L2/L3 时填父级：指数 index_code(如801050.SI) 或 行业码 industry_code"
    ),
):
    flt: Dict[str, Any] = {"src": src, "level": level}
    p = parent_code.strip()
    if p and level in ("L2", "L3"):
        built = _build_tree_level_filter(src, level, p)
        if built is None:
            return {
                "success": True,
                "count": 0,
                "data": [],
                "message": "未找到父级行业，请检查 index_code 或 industry_code",
            }
        flt = built
    elif p:
        flt["parent_code"] = p
    # L1 无 parent 时仅按 level 过滤（顶层仅申万 L1 行业）
    n = _CLASSIFY.count_documents(flt)
    if n == 0:
        return {
            "success": True,
            "count": 0,
            "data": [],
            "message": "无分类数据。请先执行 quant_data_engine: run_sw_industry_backfill --sync classify",
        }
    cur = _CLASSIFY.find(flt, {"_id": 0}).sort("index_code", ASCENDING)
    out = [_classify_row_item(d, level) for d in cur]
    return {"success": True, "count": len(out), "data": out}


@router.get("/children")
async def get_children(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    src: str = Query("SW2021"),
    parent_code: str = Query(
        ..., min_length=1, description="父级行：index_code(如801050.SI) 或 industry_code（与 classify 落库一致）"
    ),
):
    if not parent_code.strip():
        raise HTTPException(400, "parent_code required")
    fath = _find_classify_parent(src, parent_code)
    if not fath:
        return {"success": True, "count": 0, "data": [], "message": "未找到父级行业"}
    lv = fath.get("level", "L1")
    child_level = {"L1": "L2", "L2": "L3"}.get(str(lv), "")
    if not child_level:
        return {"success": True, "count": 0, "data": []}
    flt = _build_parent_code_child_filter(fath, child_level, src)
    n = _CLASSIFY.count_documents(flt)
    if n == 0:
        return {"success": True, "count": 0, "data": []}
    cur = _CLASSIFY.find(flt, {"_id": 0}).sort("index_code", ASCENDING)
    return {
        "success": True,
        "count": n,
        "data": [_classify_row_item(d, child_level) for d in cur],
    }


def _load_daily_ohlc(
    ts_code: str, start: str, end: str, *, last_n: Optional[int] = None
) -> pd.DataFrame:
    """
    加载 sw_daily 在 [start,end] 内的行。
    - last_n: 仅取该区间内按日期最近的 last_n 条（Mongo 侧 sort desc + limit），降低日线量。
    - 周/月聚合不传 last_n，需区间内全部日线。
    """
    flt = _sw_daily_flt_ts_date_range(ts_code, start, end)
    if last_n is not None and int(last_n) > 0:
        cur = _DAILY.find(flt, PROJ_KLINE).sort("trade_date", DESCENDING).limit(int(last_n))
        rows = list(cur)
        rows.reverse()  # ascending by trade_date，等同于全量后 tail(n)
    else:
        cur = _DAILY.find(flt, PROJ_KLINE).sort("trade_date", ASCENDING)
        rows = list(cur)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for c in ("open", "high", "low", "close", "vol", "amount", "change", "pct_change", "float_mv", "total_mv", "pe", "pb"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _period_key(trade_date: Any, tfc: str) -> str:
    s = str(trade_date or "").replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        return s
    if tfc == "1m":
        return s[:6]
    if tfc == "1w":
        dt = datetime.strptime(s, "%Y%m%d")
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return s


def _period_start(trade_date: Any, tfc: str) -> str:
    s = str(trade_date or "").replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        return s
    dt = datetime.strptime(s, "%Y%m%d")
    if tfc == "1m":
        return dt.replace(day=1).strftime("%Y%m%d")
    if tfc == "1w":
        return (dt - timedelta(days=dt.weekday())).strftime("%Y%m%d")
    return s


def _kline_row_from_doc(raw: Dict[str, Any]) -> Dict[str, Any]:
    tdr = raw.get("trade_date", "")
    rec: Dict[str, Any] = {"trade_date": str(tdr)[:8] if tdr else ""}
    for c in ("name", "open", "high", "low", "close", "vol", "amount",
              "change", "pct_change", "float_mv", "total_mv", "pe", "pb"):
        v = raw.get(c)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        try:
            rec[c] = float(v) if c != "name" else str(v)
        except Exception:  # pragma: no cover
            rec[c] = v
    return rec


def _agg_ohlc(df: pd.DataFrame, rule: str) -> List[Dict[str, Any]]:
    if df is None or df.empty or "trade_date" not in df.columns:
        return []
    df = df.copy()
    df["td"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["td"]).set_index("td").sort_index()
    df["_period_trade_date"] = df.index
    if rule == "1w":
        ohlc = df.resample("W-FRI", label="right", closed="right").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "_period_trade_date": "last",
            }
        )
        if "vol" in df.columns:
            ohlc["vol"] = df["vol"].resample("W-FRI", label="right", closed="right").sum()
        if "amount" in df.columns:
            ohlc["amount"] = df["amount"].resample("W-FRI", label="right", closed="right").sum()
        for mcol in ("float_mv", "total_mv", "pct_change", "change"):
            if mcol in df.columns:
                ohlc[mcol] = df[mcol].resample("W-FRI", label="right", closed="right").last()
    elif rule == "1m":
        ohlc = df.resample("ME", label="right", closed="right").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "_period_trade_date": "last",
            }
        )
        if "vol" in df.columns:
            ohlc["vol"] = df["vol"].resample("ME", label="right", closed="right").sum()
        if "amount" in df.columns:
            ohlc["amount"] = df["amount"].resample("ME", label="right", closed="right").sum()
        for mcol in ("float_mv", "total_mv", "pct_change", "change"):
            if mcol in df.columns:
                ohlc[mcol] = df[mcol].resample("ME", label="right", closed="right").last()
    else:
        return []
    ohlc = ohlc.dropna(how="all")
    out: List[Dict[str, Any]] = []
    for idx, r in ohlc.iterrows():
        td = (
            pd.to_datetime(r.get("_period_trade_date")).strftime("%Y%m%d")
            if "_period_trade_date" in ohlc.columns and pd.notna(r.get("_period_trade_date"))
            else idx.strftime("%Y%m%d")
        )
        if pd.isna(r.get("close")):
            continue
        rec: Dict[str, Any] = {
            "trade_date": td,
            "open": float(r["open"]) if "open" in ohlc.columns and pd.notna(r.get("open")) else None,
            "high": float(r["high"]) if "high" in ohlc.columns and pd.notna(r.get("high")) else None,
            "low": float(r["low"]) if "low" in ohlc.columns and pd.notna(r.get("low")) else None,
            "close": float(r["close"]) if pd.notna(r.get("close")) else None,
            "vol": float(r["vol"]) if "vol" in ohlc.columns and pd.notna(r.get("vol")) else None,
        }
        for opt in ("amount", "change", "pct_change", "float_mv", "total_mv"):
            if opt in ohlc.columns and pd.notna(r.get(opt)):
                rec[opt] = float(r[opt])
        out.append(rec)
    return out


@router.get("/kline")
async def get_kline(
    response: Response,
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    ts_code: str = Query(..., min_length=4, description="申万行业指数 ts_code，如 801012.SI 或 850531.SI"),
    tf: str = Query("1d", description="1d=日线, 1w=周线(自然周聚合到周五), 1m=月线(月末)"),
    start: str = Query("", max_length=8, description="YYYYMMDD 可选"),
    end: str = Query("", max_length=8, description="默认今天"),
    limit: int = Query(800, ge=50, le=MAX_KLINE_LIMIT),
):
    # 与 db.data_db 实际一致，便于和 Mongo Shell / Compass 核对（疑连错库时看响应头）
    response.headers["X-Quant-Data-DB"] = str(data_db.name)
    response.headers["X-Quant-Data-Collection"] = "sw_daily"  # updated later for weekly/monthly

    ts_code = (ts_code or "").strip()
    tfc = (tf or "1d").strip().lower()
    if tfc not in ("1d", "1w", "1m", "d", "w", "m"):
        raise HTTPException(400, "tf must be 1d, 1w, or 1m")
    if tfc in ("d",):
        tfc = "1d"
    if tfc in ("w",):
        tfc = "1w"
    if tfc in ("m",):
        tfc = "1m"

    start = str(start or "").strip()
    end = str(end or "").strip()
    end_s = end.replace("-", "")[:8] or datetime.now().strftime("%Y%m%d")
    if len(end_s) != 8 or not end_s.isdigit():
        raise HTTPException(400, "invalid end")

    if not start or len(str(start)) < 8:
        e_dt = datetime.strptime(end_s, "%Y%m%d")
        s_dt = e_dt - timedelta(days=MAX_DAILY_LOOKBACK_DAYS)
        start_s = s_dt.strftime("%Y%m%d")
    else:
        start_s = str(start).replace("-", "")[:8]
    if start_s > end_s:
        raise HTTPException(400, "start must be <= end")

    # ── 周/月：优先查预计算集合，空结果则回退 pandas resample ──────────────────
    if tfc in ("1w", "1m"):
        col_name = "sw_weekly" if tfc == "1w" else "sw_monthly"
        pre_col = _WEEKLY if tfc == "1w" else _MONTHLY
        flt_pre = _sw_daily_flt_ts_date_range(ts_code, start_s, end_s)
        pre_rows = list(
            pre_col.find(flt_pre, PROJ_KLINE)
            .sort("trade_date", DESCENDING)
            .limit(int(limit))
        )
        if pre_rows:
            pre_rows.reverse()  # ascending by trade_date
            response.headers["X-Quant-Data-Collection"] = col_name
            ohlc_list = [_kline_row_from_doc(raw) for raw in pre_rows]
            latest_daily = _DAILY.find_one(
                _sw_daily_flt_ts_date_range(ts_code, start_s, end_s),
                {"_id": 0, "trade_date": 1},
                sort=[("trade_date", DESCENDING)],
            )
            latest_daily_date = str((latest_daily or {}).get("trade_date") or "")[:8]
            last_pre_date = str(ohlc_list[-1].get("trade_date") or "")[:8] if ohlc_list else ""
            if latest_daily_date and last_pre_date and latest_daily_date > last_pre_date:
                rebuild_start = _period_start(last_pre_date, tfc)
                tail_df = _load_daily_ohlc(ts_code, rebuild_start, end_s)
                tail_rows = _agg_ohlc(tail_df, tfc)
                if tail_rows:
                    tail_keys = {_period_key(row.get("trade_date"), tfc) for row in tail_rows}
                    ohlc_list = [
                        row for row in ohlc_list
                        if _period_key(row.get("trade_date"), tfc) not in tail_keys
                    ]
                    ohlc_list.extend(tail_rows)
                    ohlc_list.sort(key=lambda row: str(row.get("trade_date") or ""))
                    if len(ohlc_list) > int(limit):
                        ohlc_list = ohlc_list[-int(limit):]
            return {
                "success": True,
                "tf": tfc,
                "ts_code": ts_code,
                "start": ohlc_list[0]["trade_date"] if ohlc_list else start_s,
                "end": ohlc_list[-1]["trade_date"] if ohlc_list else end_s,
                "data": ohlc_list,
                "count": len(ohlc_list),
            }
        # pre-computed collection empty → fall back to daily resample

    # 日线在 Mongo 侧按 limit 截取；周/月需全区间日线再 resample
    if tfc == "1d":
        df = _load_daily_ohlc(ts_code, start_s, end_s, last_n=int(limit))
    else:
        df = _load_daily_ohlc(ts_code, start_s, end_s)

    if df is None or df.empty:
        n_any = _DAILY.find_one({"ts_code": ts_code}, {"_id": 1})
        if n_any is None:
            return {
                "success": True,
                "tf": tfc,
                "ts_code": ts_code,
                "data": [],
                "count": 0,
                "message": f"无 {ts_code} 的 sw_daily 数据。请回填补跑: run_sw_industry_backfill --sync daily",
            }
        return {
            "success": True,
            "tf": tfc,
            "ts_code": ts_code,
            "data": [],
            "count": 0,
            "message": f"未查询到 {start_s}~{end_s} 的日线; 请扩大范围或先回补日行情。",
        }

    if tfc == "1d":
        _cols = [
            c
            for c in (
                "trade_date",
                "name",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "amount",
                "change",
                "pct_change",
                "float_mv",
                "total_mv",
                "pe",
                "pb",
            )
            if c in df.columns
        ]
        if "open" in df.columns and _cols:
            df2 = df[_cols].copy()
        else:
            df2 = df.sort_values("trade_date").copy()

        out: List[Dict[str, Any]] = []
        for row in df2.to_dict(orient="records"):
            tdr = row.get("trade_date")
            rec: Dict[str, Any] = {
                "trade_date": (str(int(tdr)) if tdr is not None and not isinstance(tdr, str) else (tdr[:8] if isinstance(tdr, str) else "")),
            }
            for c in ("name", "open", "high", "low", "close", "vol", "amount", "change", "pct_change", "float_mv", "total_mv", "pe", "pb"):
                if c not in row or row.get(c) is None or (isinstance(row.get(c), float) and pd.isna(row.get(c))):
                    continue
                try:
                    v = row[c]
                    rec[c] = float(v) if c != "name" else str(v)
                except Exception:  # pragma: no cover
                    rec[c] = v
            out.append(rec)
        return {
            "success": True,
            "tf": "1d",
            "ts_code": ts_code,
            "start": out[0]["trade_date"] if out else start_s,
            "end": out[-1]["trade_date"] if out else end_s,
            "data": out,
            "count": len(out),
        }

    ohlc_list = _agg_ohlc(df, tfc)
    if not ohlc_list:
        return {
            "success": True,
            "tf": tfc,
            "ts_code": ts_code,
            "data": [],
            "count": 0,
            "message": f"周/月聚合后无数据（日线共 {len(df)} 行），请拉更长区间。",
        }
    if len(ohlc_list) > int(limit):
        ohlc_list = ohlc_list[-int(limit) :]
    return {
        "success": True,
        "tf": tfc,
        "ts_code": ts_code,
        "start": ohlc_list[0]["trade_date"] if ohlc_list else start_s,
        "end": ohlc_list[-1]["trade_date"] if ohlc_list else end_s,
        "data": ohlc_list,
        "count": len(ohlc_list),
    }


_MEMBER = data_db["sw_index_member"]
_DAILY_BASIC = data_db["financial_daily_basic"]
_MONEY_FLOW = data_db["money_flow"]

_MEMBER_LEVEL_KEYS = ("l1_code", "l2_code", "l3_code")
_MEMBER_PROJ = {"_id": 0, "con_code": 1, "name": 1, "is_new": 1, "l1_name": 1, "l2_name": 1, "l3_name": 1}


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _amount_diff(row: Dict[str, Any], buy_key: str, sell_key: str) -> Optional[float]:
    buy = _to_float(row.get(buy_key))
    sell = _to_float(row.get(sell_key))
    if buy is None and sell is None:
        return None
    return (buy or 0.0) - (sell or 0.0)


def _main_money_flow(row: Dict[str, Any]) -> Dict[str, Any]:
    elg_net = _amount_diff(row, "buy_elg_amount", "sell_elg_amount")
    lg_net = _amount_diff(row, "buy_lg_amount", "sell_lg_amount")
    main_net = (
        (elg_net or 0.0) + (lg_net or 0.0)
        if elg_net is not None or lg_net is not None
        else _to_float(row.get("net_mf_amount"))
    )
    return {
        "elg_net": elg_net,
        "lg_net": lg_net,
        "main_net": main_net,
        "md_net": _amount_diff(row, "buy_md_amount", "sell_md_amount"),
        "sm_net": _amount_diff(row, "buy_sm_amount", "sell_sm_amount"),
    }


def _rolling_sum(rows_desc: List[Dict[str, Any]], key: str, limit: int) -> Optional[float]:
    values = [_to_float(row.get(key)) for row in rows_desc[:limit]]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values)


def _money_flow_streak(rows_desc: List[Dict[str, Any]]) -> int:
    first = _to_float((rows_desc[0] if rows_desc else {}).get("main_net"))
    if first is None or first == 0:
        return 0
    sign = 1 if first > 0 else -1
    count = 0
    for row in rows_desc:
        value = _to_float(row.get("main_net"))
        if value is None or value == 0 or (value > 0) != (sign > 0):
            break
        count += 1
    return count * sign


def _industry_money_flow_summary(rows_desc: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = rows_desc[0] if rows_desc else {}
    return {
        "main_net_today": _to_float(latest.get("main_net")),
        "main_net_5d": _rolling_sum(rows_desc, "main_net", 5),
        "main_net_20d": _rolling_sum(rows_desc, "main_net", 20),
        "inflow_streak": _money_flow_streak(rows_desc),
        "sample_count": len(rows_desc),
        "as_of": latest.get("trade_date"),
        "unit": "wan_yuan",
    }


def _coverage_level(covered_count: int, member_count: int) -> str:
    if member_count <= 0:
        return "none"
    ratio = covered_count / member_count
    if ratio >= 0.8:
        return "ok"
    if ratio >= 0.5:
        return "partial"
    return "low"


@router.get("/members")
async def get_members(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    index_code: str = Query(..., min_length=4, description="行业指数 index_code，如 801010.SI"),
    src: str = Query("SW2021"),
    current_only: bool = Query(True, description="True 仅返回当前成分 is_new=Y"),
):
    """返回某行业指数（L1/L2/L3 均可）的成分股列表，含最新市值和涨跌幅。"""
    code = index_code.strip()
    if not code:
        raise HTTPException(400, "index_code required")

    flt: Dict[str, Any] = {
        "src": src,
        "$or": [{k: code} for k in _MEMBER_LEVEL_KEYS],
    }
    if current_only:
        flt["is_new"] = "Y"

    cur = _MEMBER.find(flt, _MEMBER_PROJ).sort("con_code", ASCENDING)
    members = list(cur)

    # 补充最新行情：total_mv（万元）、pct_chg（%）
    # con_code 已带交易所后缀（如 600519.SH），可直接作为 ts_code 查询
    if members:
        latest = _DAILY_BASIC.find_one({}, {"trade_date": 1}, sort=[("trade_date", -1)])
        if latest:
            trade_date = latest["trade_date"]
            ts_codes = [m["con_code"] for m in members]
            rows = _DAILY_BASIC.find(
                {"trade_date": trade_date, "ts_code": {"$in": ts_codes}},
                {"_id": 0, "ts_code": 1, "total_mv": 1, "pe_ttm": 1, "pb": 1},
            )
            mkt: Dict[str, Any] = {r["ts_code"]: r for r in rows}
            for m in members:
                row = mkt.get(m["con_code"], {})
                m["total_mv"] = row.get("total_mv")   # 万元
                m["pe_ttm"]   = row.get("pe_ttm")
                m["pb"]       = row.get("pb")

    return {
        "success": True,
        "index_code": code,
        "count": len(members),
        "data": members,
    }


@router.get("/money-flow")
async def get_industry_money_flow(
    response: Response,
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    index_code: str = Query(..., min_length=4, description="行业指数 index_code，如 801010.SI"),
    src: str = Query("SW2021"),
    days: int = Query(60, ge=5, le=1500),
    current_only: bool = Query(True, description="True 仅聚合当前成分 is_new=Y"),
):
    """按行业成分股 on-demand 聚合 money_flow，作为预聚合表落地前的轻量 MVP。"""
    code = index_code.strip()
    if not code:
        raise HTTPException(400, "index_code required")

    member_filter: Dict[str, Any] = {
        "src": src,
        "$or": [{k: code} for k in _MEMBER_LEVEL_KEYS],
    }
    if current_only:
        member_filter["is_new"] = "Y"
    members = list(_MEMBER.find(member_filter, _MEMBER_PROJ).sort("con_code", ASCENDING))
    member_count = len(members)
    if not members:
        return {
            "success": True,
            "index_code": code,
            "src": src,
            "days": days,
            "member_count": 0,
            "data": [],
            "summary": _industry_money_flow_summary([]),
            "message": "暂无该行业成分股，无法聚合资金流。",
        }

    member_names = {row.get("con_code"): row.get("name") for row in members if row.get("con_code")}
    codes = list(member_names.keys())
    row_limit = min(max(days * max(member_count, 1) * 2, days), 800000)
    projection = {
        "_id": 0,
        "ts_code": 1,
        "symbol": 1,
        "trade_date": 1,
        "amount": 1,
        "buy_sm_amount": 1,
        "sell_sm_amount": 1,
        "buy_md_amount": 1,
        "sell_md_amount": 1,
        "buy_lg_amount": 1,
        "sell_lg_amount": 1,
        "buy_elg_amount": 1,
        "sell_elg_amount": 1,
        "net_mf_amount": 1,
    }
    cursor = (
        _MONEY_FLOW.find(
            {"$or": [{"ts_code": {"$in": codes}}, {"symbol": {"$in": codes}}]},
            projection,
        )
        .sort("trade_date", DESCENDING)
        .limit(row_limit)
    )
    raw_rows = list(cursor)
    dates_desc = sorted(
        {str(row.get("trade_date") or "") for row in raw_rows if row.get("trade_date")},
        reverse=True,
    )[:days]
    date_set = set(dates_desc)
    grouped: Dict[str, List[Dict[str, Any]]] = {date: [] for date in dates_desc}
    for row in raw_rows:
        trade_date = str(row.get("trade_date") or "")
        if trade_date in date_set:
            grouped.setdefault(trade_date, []).append(row)

    rows_desc: List[Dict[str, Any]] = []
    for trade_date in dates_desc:
        day_rows = grouped.get(trade_date, [])
        seen_codes = set()
        totals = {"elg_net": 0.0, "lg_net": 0.0, "main_net": 0.0, "md_net": 0.0, "sm_net": 0.0}
        amount_sum = 0.0
        stock_flows: List[Dict[str, Any]] = []
        for raw in day_rows:
            ts_code = raw.get("ts_code") or raw.get("symbol")
            if not ts_code or ts_code in seen_codes:
                continue
            flow = _main_money_flow(raw)
            if flow["main_net"] is None:
                continue
            seen_codes.add(ts_code)
            amount = _to_float(raw.get("amount"))
            amount_sum += amount or 0.0
            for key in totals:
                value = _to_float(flow.get(key))
                if value is not None:
                    totals[key] += value
            stock_flows.append({
                "con_code": ts_code,
                "name": member_names.get(ts_code, ""),
                "main_net": flow["main_net"],
            })

        top_inflow = sorted(stock_flows, key=lambda row: row.get("main_net") or 0, reverse=True)[:5]
        top_outflow = sorted(stock_flows, key=lambda row: row.get("main_net") or 0)[:5]
        covered_count = len(seen_codes)
        rows_desc.append({
            "index_code": code,
            "src": src,
            "trade_date": trade_date,
            "member_count": member_count,
            "covered_count": covered_count,
            "coverage_ratio": covered_count / member_count if member_count else None,
            "coverage_level": _coverage_level(covered_count, member_count),
            "amount": amount_sum,
            "main_net_ratio": totals["main_net"] / amount_sum if amount_sum else None,
            "top_inflow": top_inflow,
            "top_outflow": top_outflow,
            "unit": "wan_yuan",
            **totals,
        })

    rows = sorted(rows_desc, key=lambda row: str(row.get("trade_date") or ""))
    response.headers["Cache-Control"] = "private, max-age=21600"
    return {
        "success": True,
        "index_code": code,
        "src": src,
        "days": days,
        "member_count": member_count,
        "count": len(rows),
        "data": rows,
        "summary": _industry_money_flow_summary(rows_desc),
        "unit": "wan_yuan",
    }


_STOCK_INDUSTRY_PROJ = {
    "_id": 0, "con_code": 1, "name": 1,
    "l1_code": 1, "l1_name": 1,
    "l2_code": 1, "l2_name": 1,
    "l3_code": 1, "l3_name": 1,
}


@router.get("/stock-industry")
async def get_stock_industry(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    symbol: str = Query(..., min_length=1, description="股票代码，如 600519 或 600519.SH"),
    src: str = Query("SW2021"),
):
    """查询某股票所属申万行业 L1/L2/L3。"""
    sym = symbol.strip()
    cands: List[str] = [sym]
    if "." not in sym:
        cands += [f"{sym}.SH", f"{sym}.SZ", f"{sym}.BJ"]
    else:
        cands.append(sym.split(".")[0])

    doc = None
    for c in cands:
        doc = _MEMBER.find_one({"con_code": c, "src": src, "is_new": "Y"}, _STOCK_INDUSTRY_PROJ)
        if doc:
            break
    if not doc:
        for c in cands:
            doc = _MEMBER.find_one(
                {"con_code": c, "src": src},
                _STOCK_INDUSTRY_PROJ,
                sort=[("in_date", -1)],
            )
            if doc:
                break

    if not doc:
        return {"success": False, "message": f"未找到 {symbol} 的申万行业归属"}

    return {
        "success": True,
        "symbol": doc.get("con_code", sym),
        "name": doc.get("name", ""),
        "l1_code": doc.get("l1_code", ""),
        "l1_name": doc.get("l1_name", ""),
        "l2_code": doc.get("l2_code", ""),
        "l2_name": doc.get("l2_name", ""),
        "l3_code": doc.get("l3_code", ""),
        "l3_name": doc.get("l3_name", ""),
        "src": src,
    }


# ---------------------------------------------------------------------------
# Industry analysis task — POST to submit, GET latest for auto-restore
# ---------------------------------------------------------------------------

class IndustryAnalysisRequest(BaseModel):
    index_code: str
    model: Optional[str] = None
    src: str = "SW2021"


@router.post("/industry-analysis")
async def submit_industry_analysis(
    body: IndustryAnalysisRequest,
    current_user: Dict = Depends(get_current_active_user),
):
    """提交行业分析任务（异步队列）。"""
    index_code = (body.index_code or "").strip().upper()
    if not index_code:
        raise HTTPException(400, "index_code required")

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="industry_analysis",
        priority=20,
        params={
            "index_code": index_code,
            "src": body.src,
            **({"model": body.model} if body.model else {}),
        },
        source="manual_trigger",
    )
    # store index_code at top level for easy querying
    task_doc["index_code"] = index_code

    try:
        result = get_db_collections()["analysis_tasks"].insert_one(task_doc)
        return {
            "success": True,
            "task_id": str(result.inserted_id),
            "status": "pending",
            "index_code": index_code,
        }
    except Exception as e:
        raise HTTPException(500, f"任务创建失败: {e}")


@router.get("/industry-analysis/latest")
async def get_latest_industry_analysis(
    current_user: Dict = Depends(get_current_active_user),  # noqa: ARG001
    index_code: str = Query(..., min_length=1),
):
    """查询该行业最近一次已完成的分析结果（供页面打开时自动回显）。"""
    ic = (index_code or "").strip().upper()
    doc = get_db_collections()["analysis_tasks"].find_one(
        {"type": "industry_analysis", "index_code": ic, "status": "completed"},
        sort=[("created_at", -1)],
        projection={"_id": 1, "result": 1, "created_at": 1, "updated_at": 1},
    )
    if not doc:
        return {"success": False, "message": "暂无该行业的分析记录"}

    analysis = (doc.get("result") or {}).get("analysis") or {}
    return {
        "success": True,
        "task_id": str(doc["_id"]),
        "index_code": ic,
        "analysis": analysis,
        "created_at": doc.get("created_at"),
    }
