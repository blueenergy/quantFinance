import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_active_user
from db import get_data_db
from services.etf_kline import load_etf_kline_rows
from utils.stock_utils import serialize_doc

try:
    from stock_data_access.indicators.decision_gs import attach_decision_gs
    from stock_data_access.prices import VALID_ADJUST, symbol_variants
except ModuleNotFoundError:
    _stock_data_access_src = Path(__file__).resolve().parents[1] / "data-access-lib" / "src"
    if str(_stock_data_access_src) not in sys.path:
        sys.path.insert(0, str(_stock_data_access_src))
    try:
        from stock_data_access.indicators.decision_gs import attach_decision_gs
        from stock_data_access.prices import VALID_ADJUST, symbol_variants
    except ModuleNotFoundError:
        VALID_ADJUST = ("hfq", "qfq", "none")

        def attach_decision_gs(rows: List[Dict[str, Any]], **_params: Any) -> List[Dict[str, Any]]:
            return list(rows)

        def symbol_variants(symbol: str) -> List[str]:
            if not symbol:
                return []
            variants = [symbol]
            if "." in symbol:
                base = symbol.split(".", 1)[0]
                if base not in variants:
                    variants.append(base)
            else:
                for suffix in (".SH", ".SZ", ".BJ"):
                    variants.append(f"{symbol}{suffix}")
            return variants

router = APIRouter()
log = logging.getLogger(__name__)

data_collections = get_data_db()
collection = data_collections["volume_price"] # type: ignore
adj_collection = data_collections["stock_adj_factor"]  # type: ignore
etf_basic_collection = data_collections.get("etf_basic")
etf_daily_collection = data_collections.get("etf_daily")
etf_adj_collection = data_collections.get("etf_adj_factor")

_PRICE_COLS = ("open", "high", "low", "close")


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _normalize_trade_date(value: Any) -> str:
    text = str(value or "").replace("-", "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else text


def _stamp_adjust(row: Dict[str, Any], adjust: str, degraded: bool) -> Dict[str, Any]:
    merged = dict(row)
    merged["adjust"] = adjust
    merged["adj_degraded"] = degraded
    return merged


def _lookup_latest_factor(adj_coll: Any, symbols: Sequence[str]) -> Optional[float]:
    """Most recent adj_factor for the symbol (qfq anchor, independent of the K window)."""
    if adj_coll is None or not symbols:
        return None
    query = {"$or": [{"symbol": {"$in": list(symbols)}}, {"ts_code": {"$in": list(symbols)}}]}
    doc = None
    try:
        doc = adj_coll.find_one(
            query,
            {"_id": 0, "adj_factor": 1, "trade_date": 1},
            sort=[("trade_date", -1)],
        )
    except TypeError:
        doc = None
    if doc:
        value = _to_float(doc.get("adj_factor"))
        if value is not None and value > 0:
            return value
    try:
        docs = list(adj_coll.find(query, {"_id": 0, "adj_factor": 1, "trade_date": 1}))
    except TypeError:
        try:
            docs = list(adj_coll.find(query) or [])
        except Exception:
            return None
    best_date = ""
    best: Optional[float] = None
    for item in docs:
        day = _normalize_trade_date(item.get("trade_date"))
        value = _to_float(item.get("adj_factor"))
        if value is None or value <= 0:
            continue
        if day >= best_date:
            best_date = day
            best = value
    return best


def _apply_one_symbol(
    rows: List[Dict[str, Any]],
    symbol: str,
    adjust: str,
    adj_coll: Any,
) -> List[Dict[str, Any]]:
    if not rows:
        return rows
    if adjust == "none":
        return [_stamp_adjust(row, "none", False) for row in rows]
    if adj_coll is None:
        return [_stamp_adjust(row, adjust, True) for row in rows]

    variants = symbol_variants(symbol) if symbol else []
    dates = [_normalize_trade_date(row.get("trade_date")) for row in rows]
    dates_ok = [item for item in dates if item]
    if not variants or not dates_ok:
        return [_stamp_adjust(row, adjust, True) for row in rows]

    start, end = min(dates_ok), max(dates_ok)
    query = {
        "$or": [{"symbol": {"$in": variants}}, {"ts_code": {"$in": variants}}],
        "trade_date": {"$gte": start, "$lte": end},
    }
    try:
        factor_docs = list(
            adj_coll.find(query, {"_id": 0, "trade_date": 1, "adj_factor": 1})
        )
    except TypeError:
        factor_docs = list(adj_coll.find(query) or [])
    except Exception:
        log.exception("adj_factor lookup failed for %s", symbol)
        return [_stamp_adjust(row, adjust, True) for row in rows]

    factor_by_date: Dict[str, float] = {}
    for doc in factor_docs:
        key = _normalize_trade_date(doc.get("trade_date"))
        factor = _to_float(doc.get("adj_factor"))
        if key and factor is not None and factor > 0:
            factor_by_date[key] = factor
    if not factor_by_date:
        return [_stamp_adjust(row, adjust, True) for row in rows]

    filled: Dict[str, float] = {}
    last: Optional[float] = None
    for day in sorted(set(dates_ok)):
        if day in factor_by_date:
            last = factor_by_date[day]
        if last is not None:
            filled[day] = last
    if not filled:
        return [_stamp_adjust(row, adjust, True) for row in rows]

    latest_factor = _lookup_latest_factor(adj_coll, variants) if adjust == "qfq" else None
    if adjust == "qfq" and (latest_factor is None or latest_factor <= 0):
        latest_factor = filled[max(filled)]

    degraded = False
    out: List[Dict[str, Any]] = []
    for row in rows:
        merged = dict(row)
        day = _normalize_trade_date(row.get("trade_date"))
        factor = filled.get(day)
        if factor is None:
            degraded = True
            out.append(_stamp_adjust(merged, adjust, True))
            continue
        if adjust == "hfq":
            multiplier = factor
        else:
            multiplier = factor / latest_factor if latest_factor else 1.0
        for col in _PRICE_COLS:
            value = _to_float(merged.get(col))
            if value is not None:
                merged[col] = value * multiplier
        out.append(_stamp_adjust(merged, adjust, False))
    if degraded:
        for item in out:
            item["adj_degraded"] = True
    return out


def _apply_records_adjustment(
    docs: List[Dict[str, Any]],
    adjust: str,
    adj_coll: Any = None,
) -> List[Dict[str, Any]]:
    """Scale OHLC at read time. K-line default is qfq; hfq/none are explicit."""
    mode = str(adjust or "qfq").strip().lower()
    if mode not in VALID_ADJUST:
        mode = "qfq"
    if not docs:
        return docs
    coll = adj_coll if adj_coll is not None else adj_collection
    grouped: Dict[str, List[int]] = defaultdict(list)
    for idx, row in enumerate(docs):
        grouped[str(row.get("symbol") or "")].append(idx)
    out = list(docs)
    for symbol, indexes in grouped.items():
        subset = [docs[idx] for idx in indexes]
        adjusted = _apply_one_symbol(subset, symbol, mode, coll)
        for idx, row in zip(indexes, adjusted):
            out[idx] = row
    return out


class RecordResponseModel(BaseModel):
    symbol: Optional[str]
    trade_date: Optional[Any]
    name: Optional[str] = None
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    composite_score: Optional[float] = None
    cycle_score: Optional[float] = None
    growth_score: Optional[float] = None
    fundamental_score: Optional[float] = None
    value_score: Optional[float] = None
    technical_score: Optional[float] = None
    money_flow_score: Optional[float] = None
    score_date: Optional[str] = None
    mj20: Optional[float] = None
    mj30: Optional[float] = None
    gs_is_bull: Optional[bool] = None
    gs_signal: Optional[str] = None
    gs_watch: Optional[str] = None
    gs_warmup: Optional[bool] = None
    gs_formula_id: Optional[str] = None
    adjust: Optional[str] = None
    adj_degraded: Optional[bool] = None

_STOCK_SCORE_LOOKUP_STAGES = [
    {
        "$lookup": {
            "from": "stock_scores",
            "let": {"symbol": "$symbol", "trade_date_str": "$trade_date_str"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$symbol", "$$symbol"]},
                    {"$eq": ["$score_date", "$$trade_date_str"]}
                ]}}}
            ],
            "as": "score_docs"
        }
    },
    {"$addFields": {
        "score_doc": {"$arrayElemAt": ["$score_docs", 0]}
    }},
    {"$addFields": {
        "composite_score": {"$ifNull": ["$score_doc.composite_score", None]},
        "cycle_score": {"$ifNull": ["$score_doc.cycle_score", None]},
        "growth_score": {"$ifNull": ["$score_doc.growth_score", None]},
        "fundamental_score": {"$ifNull": ["$score_doc.fundamental_score", None]},
        "value_score": {"$ifNull": ["$score_doc.value_score", None]},
        "technical_score": {"$ifNull": ["$score_doc.technical_score", None]},
        "money_flow_score": {"$ifNull": ["$score_doc.money_flow_score", None]},
        "score_date": {"$ifNull": ["$score_doc.score_date", None]}
    }},
    {
        "$addFields": {
            "composite_score": {
                "$cond": [
                    {"$eq": [{"$type": "$composite_score"}, "object"]},
                    "$composite_score.balanced",
                    "$composite_score"
                ]
            }
        }
    },
]

_STOCK_INFO_LOOKUP_STAGES = [
    {
        "$lookup": {
            "from": "stock_info",
            "localField": "symbol",
            "foreignField": "symbol",
            "as": "stock_info_docs"
        }
    },
    {"$addFields": {
        "stock_info": {"$arrayElemAt": ["$stock_info_docs", 0]}
    }},
    {"$addFields": {
        "name": {"$ifNull": ["$stock_info.name", ""]}
    }},
]

_RECORDS_PROJECT_KLINE = {
    "$project": {
        "symbol": 1,
        "trade_date": 1,
        "open": 1,
        "close": 1,
        "high": 1,
        "low": 1,
        "volume": 1,
        "name": 1,
    }
}

_RECORDS_PROJECT_WITH_SCORES = {
    "$project": {
        **_RECORDS_PROJECT_KLINE["$project"],
        "composite_score": 1,
        "cycle_score": 1,
        "fundamental_score": 1,
        "growth_score": 1,
        "technical_score": 1,
        "money_flow_score": 1,
        "score_date": 1,
    }
}


def _etf_records_or_none(
    symbol: Optional[str],
    limit: int,
    start_date: Optional[str],
    end_date: Optional[str],
    adjust: str,
) -> Optional[List[Dict[str, Any]]]:
    """ETF K-lines from etf_daily. ``None`` = not an ETF; ``[]`` = ETF but no bars."""
    if not symbol:
        return None
    start = (start_date or "").replace("-", "")[:8] or None
    end = (end_date or "").replace("-", "")[:8] or None
    rows = load_etf_kline_rows(
        etf_basic_collection,
        etf_daily_collection,
        symbol,
        start=start,
        end=end,
        limit=limit,
    )
    if rows is None:
        return None
    return _attach_decision_gs_fields(
        _apply_records_adjustment(rows, adjust, etf_adj_collection)
    )


@router.get("/records/", response_model=List[RecordResponseModel])
def get_records(
    symbol: Optional[str] = Query(None),
    limit: int = 10,
    sort: Optional[str] = None,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    adjust: str = Query("qfq", description="复权口径: none/hfq/qfq，K线默认前复权"),
    include_scores: bool = Query(
        True,
        description="是否关联 stock_scores；自选股 K 线浏览应关以降低延迟",
    ),
):
    etf_docs = _etf_records_or_none(symbol, limit, start_date, end_date, adjust)
    if etf_docs is not None:
        return etf_docs

    query = {}
    if symbol:
        query["symbol"] = symbol
    
    # 构建日期范围查询
    date_query = {}
    if start_date:
        s_date = start_date.replace('-', '')
        date_query["$gte"] = s_date
    if end_date:
        e_date = end_date.replace('-', '')
        date_query["$lte"] = e_date
    
    # 在初始 match 阶段尝试过滤（针对字符串类型的 trade_date）
    if date_query:
        query["trade_date"] = date_query

    # 构建聚合管道
    pipeline = [
        {"$match": query},
        {"$addFields": {
            "trade_date_str": {
                "$cond": [
                    {"$eq": [{"$type": "$trade_date"}, "date"]},
                    {"$dateToString": {"format": "%Y%m%d", "date": "$trade_date"}},
                    "$trade_date"
                ]
            }
        }}
    ]

    # 如果存在过滤需求，在标准化日期后再次过滤，以防 trade_date 是日期对象
    if date_query:
        match_stage = {}
        if date_query.get("$gte"):
            match_stage["$gte"] = date_query["$gte"]
        if date_query.get("$lte"):
            match_stage["$lte"] = date_query["$lte"]
        if match_stage:
            pipeline.append({"$match": {"trade_date_str": match_stage}})
    if sort:
        # 支持 '-trade_date' 降序
        field = sort.lstrip('-')
        direction = -1 if sort.startswith('-') else 1
        pipeline.append({"$sort": {field: direction}})
    pipeline.append({"$limit": limit})
    if include_scores:
        # $expr lookup cannot use indexes well; skip this for K-line browsing.
        pipeline.extend(_STOCK_SCORE_LOOKUP_STAGES)

    pipeline.extend(_STOCK_INFO_LOOKUP_STAGES)
    pipeline.append(_RECORDS_PROJECT_WITH_SCORES if include_scores else _RECORDS_PROJECT_KLINE)
    
    docs = [serialize_doc(doc) for doc in collection.aggregate(pipeline)]
    docs = _apply_records_adjustment(docs, adjust, adj_collection)
    return _attach_decision_gs_fields(docs)


def _attach_decision_gs_fields(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stamp decision_gs_v1 overlays on /records K-line rows (watchlist chart)."""
    if not docs:
        return docs
    try:
        return attach_decision_gs(docs)
    except Exception:
        log.exception("decision_gs attach failed for /records/")
        return docs

@router.get("/record/{record_id}")
def get_record(record_id: str):
    doc = collection.find_one({"_id": ObjectId(record_id)})
    if doc:
        return serialize_doc(doc)
    return {"error": "Not found"}

@router.get("/money-flow-records")
def get_money_flow_records(
    symbol: str, 
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_active_user)
):
    """
    获取某只股票的资金流数据，按 trade_date 升序排列，支持日期范围。
    """
    data_col = get_data_db()
    money_flow_col = data_col["money_flow"]
    
    query = {"symbol": symbol}
    date_query = {}
    if start_date:
        date_query["$gte"] = start_date.replace('-', '')
    if end_date:
        date_query["$lte"] = end_date.replace('-', '')
    
    if date_query:
        query["trade_date"] = date_query

    # 查询资金流数据
    records = list(money_flow_col.find(query).sort("trade_date", 1))
    
    # 格式化 trade_date 为字符串
    for r in records:
        if isinstance(r["trade_date"], (datetime, date)):
            r["trade_date"] = r["trade_date"].strftime("%Y%m%d")
        r["_id"] = str(r["_id"])
    return {"success": True, "data": records}