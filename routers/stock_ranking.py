# routers/stock_scoring.py
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from db import get_db_collections, get_data_db
from utils.score_utils import (
    parse_score_weights,
    safe_round,
    safe_float,
    extract_strategy_score,
    weighted_dimension_score,
)
from typing import Optional, List, Any, cast, Dict, Tuple, Set
from pydantic import BaseModel
from datetime import datetime, timedelta
from calendar import monthrange
from bson import ObjectId

router = APIRouter(tags=["股票排名"])

# 指数/批量「指定股票」请求时：按批查 stock_scores / stock_info，volume_price 另分段；峰值更低、往返略多。
SELECTED_SYMBOLS_QUERY_CHUNK = 50
STOCK_INFO_IN_CHUNK = 160
VOLUME_PRICE_IN_CHUNK = 80
# 超过该数量时默认不算「评分以来涨跌」（volume_price 聚合很重）；传 skip_return_since=false 可强制计算。
SELECTED_RETURN_SINCE_AUTO_THRESHOLD = 100

_STOCK_SCORE_LIST_PROJECTION = {
    "_id": 1,
    "symbol": 1,
    "score_date": 1,
    "composite_score": 1,
    "cycle_score": 1,
    "value_score": 1,
    "fundamental_score": 1,
    "growth_score": 1,
    "technical_score": 1,
    "money_flow_score": 1,
    # Read-only industry relative-strength (cycle_v2); reference only, not part
    # of composite. Absent on docs not yet backfilled -> rendered as "—".
    "industry_rs_score": 1,
}

# 与 stock-scoring-system data_manager.SCORE_INDEX_LABEL_CODES 一致；按 index_codes 查榜
STOCK_RANKING_INDEX_CODES: Tuple[str, ...] = (
    "hs300",
    "a500",
    "csi500",
    "csi1000",
    "star50",
)

SCORE_SORT_FIELDS: Dict[str, str] = {
    "cycle": "cycle_score",
    "growth": "growth_score",
    "fundamental": "fundamental_score",
    "value": "value_score",
    "technical": "technical_score",
    "money_flow": "money_flow_score",
    "industry_rs": "industry_rs_score",
}


def _aggregate_index_ranking_score_docs(
    stock_scores: Any, index_code: str, score_date: Optional[str]
) -> List[dict]:
    """按 index_codes 拉取评分行：有 date 则当日截面；否则每 symbol 取 score_date 最新一条再筛指数。"""
    if score_date:
        pipeline = [
            {"$match": {"score_date": score_date, "index_codes": index_code}},
            {"$project": _STOCK_SCORE_LIST_PROJECTION},
        ]
    else:
        pipeline = [
            {"$sort": {"score_date": -1}},
            {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$doc"}},
            {"$match": {"index_codes": index_code}},
            {"$project": _STOCK_SCORE_LIST_PROJECTION},
        ]
    try:
        return list(stock_scores.aggregate(pipeline))
    except Exception:
        return []


def _normalize_sort_by(sort_by: Optional[str]) -> str:
    key = (sort_by or "composite").strip().lower()
    if key in ("composite", "total", "score"):
        return "composite"
    if key in ("weighted", "custom_weighted", "custom"):
        return "weighted"
    if key not in SCORE_SORT_FIELDS:
        raise HTTPException(
            status_code=400,
            detail="不支持的 sort_by: %s；允许: composite, weighted, %s"
            % (sort_by, ", ".join(SCORE_SORT_FIELDS.keys())),
        )
    return key


def _parse_weighted_sort_weights(weights: Optional[str]) -> Dict[str, float]:
    try:
        return parse_score_weights(
            weights,
            allowed_dimensions=(
                "cycle",
                "growth",
                "fundamental",
                "value",
                "technical",
                "money_flow",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"weights 参数无效: {exc}") from exc


def _score_sort_value(
    row: dict,
    sort_by: str,
    strategy: str,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    if sort_by == "composite":
        return extract_strategy_score(row.get("composite_score", 0), strategy)
    if sort_by == "weighted":
        return weighted_dimension_score(row, weights or {})
    return safe_float(row.get(SCORE_SORT_FIELDS[sort_by]), 0.0)


def _weighted_score_or_none(row: dict, sort_by: str, weights: Dict[str, float]) -> Optional[float]:
    if sort_by != "weighted":
        return None
    return safe_round(weighted_dimension_score(row, weights), 2)


def _weighted_sort_value_from_per_date_fields(
    row: dict,
    weights: Dict[str, float],
) -> float:
    return max(
        (
            weighted_dimension_score(fields, weights)
            for fields in (row.get("per_date_fields") or {}).values()
        ),
        default=0.0,
    )


# using helpers from stock-scoring-system/utils/score_utils.py


def get_db_collections_from_request(request: Request):
    # keep a small shim for compatibility with other routers
    return get_db_collections()


RETURN_SINCE_EMPTY: Dict[str, Any] = {
    "return_since_score_pct": None,
    "price_base_trade_date": None,
    "price_latest_trade_date": None,
}

PRIOR_3M_EMPTY: Dict[str, Any] = {
    "prior_3m_return_pct": None,
    "prior_3m_base_trade_date": None,
    "prior_3m_end_trade_date": None,
}


def _symbol_key_variants(sym: str) -> List[str]:
    """同一标的在库中可能存 ``600519``、``600519.SH`` 等，用于查询与匹配。"""
    s = (sym or "").strip()
    if not s:
        return []
    u = s.upper()
    out: List[str] = []
    seen: Set[str] = set()

    def add(x: str) -> None:
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(s)
    if u.endswith((".SH", ".SZ", ".BJ")):
        dot = s.rfind(".")
        if dot > 0:
            add(s[:dot].strip())
    else:
        digits = "".join(ch for ch in s if ch.isdigit())
        bare = digits[:6] if len(digits) >= 6 else s
        add(bare)
        for suf in (".SH", ".SZ", ".BJ"):
            add(bare + suf)
    return out


def _expanded_query_symbols(symbols: List[str]) -> List[str]:
    keys: List[str] = []
    seen: Set[str] = set()
    for sym in symbols:
        for v in _symbol_key_variants(sym):
            if v not in seen:
                seen.add(v)
                keys.append(v)
    return keys


def _chunk_list(items: List[str], chunk_size: int) -> List[List[str]]:
    if chunk_size <= 0:
        return [items] if items else []
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def _aggregate_latest_score_doc_per_symbol(stock_scores: Any, symbols_in: List[str]) -> List[dict]:
    """按 symbol 取 score_date 最新的一条；避免 $push 全历史；$project 减少网卡与 BSON 解码量。"""
    if not symbols_in:
        return []
    pipeline = [
        {"$match": {"symbol": {"$in": symbols_in}}},
        {"$sort": {"symbol": 1, "score_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": _STOCK_SCORE_LIST_PROJECTION},
    ]
    try:
        return list(stock_scores.aggregate(pipeline))
    except Exception:
        return []


def _find_latest_per_symbol_fallback(stock_scores: Any, symbols_in: List[str]) -> List[dict]:
    """主聚合异常时的备用路径：仍用 $group+$first，禁止无 projection 的 find 拉回全历史。"""
    if not symbols_in:
        return []
    pipeline = [
        {"$match": {"symbol": {"$in": symbols_in}}},
        {"$sort": {"score_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": _STOCK_SCORE_LIST_PROJECTION},
    ]
    try:
        return list(stock_scores.aggregate(pipeline))
    except Exception:
        return []


def _stock_info_find_in_chunks(
    stock_info: Any, score_query_symbols: List[str], projection: Optional[dict]
) -> List[dict]:
    out: List[dict] = []
    for chunk in _chunk_list(score_query_symbols, STOCK_INFO_IN_CHUNK):
        if not chunk:
            continue
        cur = stock_info.find({"symbol": {"$in": chunk}}, projection or None)
        out.extend(list(cur))
    return out


def _requested_symbol_for_db_row(db_symbol: Optional[str], requested: List[str]) -> str:
    """将库里的 symbol 映射回调用方传入的代码（优先保留 ts_code 写法）。"""
    dbs = (db_symbol or "").strip()
    if not dbs:
        return dbs or ""
    for req in requested:
        if dbs in _symbol_key_variants(req):
            return req.strip()
    return dbs


def _find_one_stock_score(
    stock_scores: Any, symbol: str, score_date: str
) -> Optional[dict]:
    for k in _symbol_key_variants(symbol):
        doc = stock_scores.find_one({"symbol": k, "score_date": score_date})
        if doc:
            return doc
    return None


def _find_one_stock_info(stock_info: Any, symbol: str) -> Optional[dict]:
    for k in _symbol_key_variants(symbol):
        doc = stock_info.find_one({"symbol": k})
        if doc:
            return doc
    return None


def _close_from_volume_price_doc(doc: Optional[dict]) -> Optional[float]:
    if not doc:
        return None
    try:
        c = float(doc.get("close"))
        return c if c > 0 else None
    except (TypeError, ValueError):
        return None


def _trade_date_str(doc: Optional[dict]) -> Optional[str]:
    if not doc:
        return None
    td = doc.get("trade_date")
    if td is None:
        return None
    if hasattr(td, "strftime"):
        return td.strftime("%Y%m%d")
    if isinstance(td, int):
        s = str(td)
        return s[:8] if len(s) >= 8 else s
    s = str(td)
    return s.replace("-", "")[:8] if s else None


def _fetch_base_doc_single(volume_price_col: Any, symbol: str, sd: str) -> Optional[dict]:
    """单只股票基准 K：字符串 / BSON date / int 三种 trade_date 存法依次尝试。"""
    base_doc = volume_price_col.find_one(
        {"symbol": symbol, "trade_date": {"$lte": sd}},
        sort=[("trade_date", -1)],
    )
    if not base_doc:
        try:
            y, mo, day = int(sd[:4]), int(sd[4:6]), int(sd[6:8])
            dt_end = datetime(y, mo, day)
            base_doc = volume_price_col.find_one(
                {"symbol": symbol, "trade_date": {"$lte": dt_end}},
                sort=[("trade_date", -1)],
            )
        except (ValueError, IndexError):
            base_doc = None
    if not base_doc:
        try:
            as_int = int(sd)
            base_doc = volume_price_col.find_one(
                {"symbol": symbol, "trade_date": {"$lte": as_int}},
                sort=[("trade_date", -1)],
            )
        except ValueError:
            base_doc = None
    return base_doc


def _return_since_from_docs(latest_doc: Optional[dict], base_doc: Optional[dict]) -> dict:
    out = dict(RETURN_SINCE_EMPTY)
    latest_close = _close_from_volume_price_doc(latest_doc)
    base_close = _close_from_volume_price_doc(base_doc)
    if latest_close is None or base_close is None:
        return out
    pct = (latest_close / base_close - 1.0) * 100.0
    out["return_since_score_pct"] = round(pct, 2)
    out["price_base_trade_date"] = _trade_date_str(base_doc)
    out["price_latest_trade_date"] = _trade_date_str(latest_doc)
    return out


def _yyyymmdd_months_before(date_str: str, months: int) -> Optional[str]:
    """Return YYYYMMDD roughly ``months`` calendar months before ``date_str``."""
    s = str(date_str or "").strip().replace("-", "")[:8]
    if len(s) < 8:
        return None
    try:
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    except ValueError:
        return None
    m -= months
    while m <= 0:
        y -= 1
        m += 12
    d = min(d, monthrange(y, m)[1])
    return f"{y:04d}{m:02d}{d:02d}"


def _prior_3m_return_from_docs(end_doc: Optional[dict], base_doc: Optional[dict]) -> dict:
    out = dict(PRIOR_3M_EMPTY)
    end_close = _close_from_volume_price_doc(end_doc)
    base_close = _close_from_volume_price_doc(base_doc)
    if end_close is None or base_close is None:
        return out
    pct = (end_close / base_close - 1.0) * 100.0
    out["prior_3m_return_pct"] = round(pct, 2)
    out["prior_3m_base_trade_date"] = _trade_date_str(base_doc)
    out["prior_3m_end_trade_date"] = _trade_date_str(end_doc)
    return out


def _pick_vp_doc(vp_by_symbol: Dict[str, Any], sym: str) -> Optional[dict]:
    """从 bulk 结果中取文档：``volume_price.symbol`` 可能与 ``stock_scores`` 后缀不一致。"""
    for k in _symbol_key_variants(sym):
        d = vp_by_symbol.get(k)
        if d:
            return d
    return None


def _bulk_latest_by_symbol(volume_price_col: Any, symbols: List[str]) -> Dict[str, Any]:
    """每只股票最新一根日线；symbol 较多时分段 $match 聚合后在内存合并。"""
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    merged: Dict[str, Any] = {}
    for chunk in _chunk_list(uniq, VOLUME_PRICE_IN_CHUNK):
        if not chunk:
            continue
        try:
            cur = volume_price_col.aggregate(
                [
                    {"$match": {"symbol": {"$in": chunk}}},
                    {"$sort": {"trade_date": -1}},
                    {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
                ]
            )
            for row in cur:
                if row.get("_id") and row.get("doc"):
                    merged[row["_id"]] = row["doc"]
        except Exception:
            continue
    return merged


def _bulk_base_by_symbol_string(
    volume_price_col: Any, symbols: List[str], sd: str
) -> Dict[str, Any]:
    """不晚于 score_date 的最近一根 K；symbol 较多时分段聚合后合并。"""
    if not symbols or len(sd) < 8:
        return {}
    uniq = list(dict.fromkeys(symbols))
    merged: Dict[str, Any] = {}
    for chunk in _chunk_list(uniq, VOLUME_PRICE_IN_CHUNK):
        if not chunk:
            continue
        try:
            cur = volume_price_col.aggregate(
                [
                    {"$match": {"symbol": {"$in": chunk}, "trade_date": {"$lte": sd}}},
                    {"$sort": {"symbol": 1, "trade_date": -1}},
                    {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
                ]
            )
            for row in cur:
                if row.get("_id") and row.get("doc"):
                    merged[row["_id"]] = row["doc"]
        except Exception:
            continue
    return merged


def compute_return_since_score(
    volume_price_col: Any,
    symbol: str,
    score_date: str,
    latest_by_symbol: Dict[str, Any],
    base_by_key: Dict[Tuple[str, str], Any],
) -> dict:
    """
    Baseline: last trading day's close on or before score_date.
    Latest: most recent close in volume_price for symbol.
    """
    if not symbol or not score_date or len(str(score_date).strip()) < 8:
        return dict(RETURN_SINCE_EMPTY)
    sd = str(score_date).strip()[:8]

    if symbol not in latest_by_symbol:
        latest_by_symbol[symbol] = volume_price_col.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
        )
    latest_doc = latest_by_symbol[symbol]

    bkey = (symbol, sd)
    if bkey not in base_by_key:
        base_by_key[bkey] = _fetch_base_doc_single(volume_price_col, symbol, sd)
    base_doc = base_by_key[bkey]

    return _return_since_from_docs(latest_doc, base_doc)


def compute_prior_3m_return(
    volume_price_col: Any,
    symbol: str,
    score_date: str,
    end_by_key: Dict[Tuple[str, str], Any],
    base_by_key: Dict[Tuple[str, str], Any],
) -> dict:
    """
    Baseline: last trading day's close on or before score_date minus 3 calendar months.
    End: last trading day's close on or before score_date.
    """
    if not symbol or not score_date or len(str(score_date).strip()) < 8:
        return dict(PRIOR_3M_EMPTY)
    sd = str(score_date).strip()[:8]
    base_sd = _yyyymmdd_months_before(sd, 3)
    if not base_sd:
        return dict(PRIOR_3M_EMPTY)

    end_key = (symbol, sd)
    if end_key not in end_by_key:
        end_by_key[end_key] = _fetch_base_doc_single(volume_price_col, symbol, sd)
    base_key = (symbol, base_sd)
    if base_key not in base_by_key:
        base_by_key[base_key] = _fetch_base_doc_single(volume_price_col, symbol, base_sd)

    return _prior_3m_return_from_docs(end_by_key[end_key], base_by_key[base_key])


def enrich_rows_return_since_score(rows: List[dict], volume_price_col: Any) -> None:
    """批量写涨跌字段：Mongo 往返约 1 + 不同 score_date 种数 + 少量回退查询。"""
    if not rows:
        return
    if not hasattr(volume_price_col, "aggregate"):
        latest_by_symbol: Dict[str, Any] = {}
        base_by_key: Dict[Tuple[str, str], Any] = {}
        for row in rows:
            row.update(
                compute_return_since_score(
                    volume_price_col,
                    row.get("symbol") or "",
                    str(row.get("score_date") or ""),
                    latest_by_symbol,
                    base_by_key,
                )
            )
        return

    symbols: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        sym = row.get("symbol") or ""
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    symbols_vp = _expanded_query_symbols(symbols)
    latest_map = _bulk_latest_by_symbol(volume_price_col, symbols_vp)

    by_sd: Dict[str, List[str]] = {}
    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        if len(sd) < 8 or not sym:
            continue
        by_sd.setdefault(sd, []).append(sym)

    base_map: Dict[Tuple[str, str], Any] = {}
    for sd, syms in by_sd.items():
        uniq_syms = list(dict.fromkeys(syms))
        uniq_vp = _expanded_query_symbols(uniq_syms)
        bulk = _bulk_base_by_symbol_string(volume_price_col, uniq_vp, sd)
        for sym in uniq_syms:
            doc = _pick_vp_doc(bulk, sym)
            if doc and _close_from_volume_price_doc(doc) is not None:
                base_map[(sym, sd)] = doc
        for sym in uniq_syms:
            if (sym, sd) in base_map:
                continue
            doc = None
            for k in _symbol_key_variants(sym):
                fd = _fetch_base_doc_single(volume_price_col, k, sd)
                if fd and _close_from_volume_price_doc(fd) is not None:
                    doc = fd
                    break
            base_map[(sym, sd)] = doc

    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        if not sym or len(sd) < 8:
            row.update(dict(RETURN_SINCE_EMPTY))
            continue
        latest_doc = _pick_vp_doc(latest_map, sym)
        base_doc = base_map.get((sym, sd))
        row.update(_return_since_from_docs(latest_doc, base_doc))


def enrich_rows_prior_3m_return(rows: List[dict], volume_price_col: Any) -> None:
    """批量写评分日前 3 个月涨跌字段。"""
    if not rows:
        return
    if not hasattr(volume_price_col, "aggregate"):
        end_by_key: Dict[Tuple[str, str], Any] = {}
        base_by_key: Dict[Tuple[str, str], Any] = {}
        for row in rows:
            row.update(
                compute_prior_3m_return(
                    volume_price_col,
                    row.get("symbol") or "",
                    str(row.get("score_date") or ""),
                    end_by_key,
                    base_by_key,
                )
            )
        return

    by_sd: Dict[str, List[str]] = {}
    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        if len(sd) < 8 or not sym:
            row.update(dict(PRIOR_3M_EMPTY))
            continue
        by_sd.setdefault(sd, []).append(sym)

    end_map: Dict[Tuple[str, str], Any] = {}
    base_map: Dict[Tuple[str, str], Any] = {}
    for sd, syms in by_sd.items():
        base_sd = _yyyymmdd_months_before(sd, 3)
        if not base_sd:
            continue
        uniq_syms = list(dict.fromkeys(syms))
        uniq_vp = _expanded_query_symbols(uniq_syms)
        end_bulk = _bulk_base_by_symbol_string(volume_price_col, uniq_vp, sd)
        base_bulk = _bulk_base_by_symbol_string(volume_price_col, uniq_vp, base_sd)
        for sym in uniq_syms:
            end_doc = _pick_vp_doc(end_bulk, sym)
            if end_doc and _close_from_volume_price_doc(end_doc) is not None:
                end_map[(sym, sd)] = end_doc
            base_doc = _pick_vp_doc(base_bulk, sym)
            if base_doc and _close_from_volume_price_doc(base_doc) is not None:
                base_map[(sym, base_sd)] = base_doc
        for sym in uniq_syms:
            if (sym, sd) not in end_map:
                doc = None
                for k in _symbol_key_variants(sym):
                    fd = _fetch_base_doc_single(volume_price_col, k, sd)
                    if fd and _close_from_volume_price_doc(fd) is not None:
                        doc = fd
                        break
                end_map[(sym, sd)] = doc
            if (sym, base_sd) not in base_map:
                doc = None
                for k in _symbol_key_variants(sym):
                    fd = _fetch_base_doc_single(volume_price_col, k, base_sd)
                    if fd and _close_from_volume_price_doc(fd) is not None:
                        doc = fd
                        break
                base_map[(sym, base_sd)] = doc

    for row in rows:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "").strip()[:8]
        base_sd = _yyyymmdd_months_before(sd, 3)
        if not sym or len(sd) < 8 or not base_sd:
            row.update(dict(PRIOR_3M_EMPTY))
            continue
        row.update(
            _prior_3m_return_from_docs(
                end_map.get((sym, sd)),
                base_map.get((sym, base_sd)),
            )
        )


@router.get("/stock-rankings/by-index/{index_code}")
async def get_stock_rankings_by_index(
    request: Request,
    index_code: str,
    strategy: str = Query(
        "balanced",
        description="与全市场榜一致：balanced/aggressive/conservative 等",
    ),
    sort_by: str = Query(
        "composite",
        description="排序维度：composite/weighted/cycle/growth/fundamental/value/technical/money_flow",
    ),
    weights: Optional[str] = Query(
        None,
        description="sort_by=weighted 时使用，如 growth:0.6,cycle:0.4；权重自动归一化",
    ),
    date: Optional[str] = Query(
        None,
        description="YYYYMMDD；不传则每只股票取最新 score_date 后再筛 index_codes",
    ),
    exclude_st: bool = Query(True, description="是否排除名称含 ST 的股票"),
    offset: int = Query(0, ge=0, description="分页起点"),
    page_size: int = Query(30, ge=1, le=500, description="本页条数"),
):
    """
    按 ``stock_scores.index_codes`` 拉指数成分榜（不在库内回退 POST 全量 symbols）。
    无 ``index_codes`` 或未回填的历史数据时结果为空列表。
    """
    ic = (index_code or "").strip().lower()
    if ic not in STOCK_RANKING_INDEX_CODES:
        raise HTTPException(
            status_code=400,
            detail="不支持的 index_code: %s；允许: %s"
            % (index_code, ", ".join(STOCK_RANKING_INDEX_CODES)),
        )
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db["stock_scores"]
        stock_info = db["stock_info"]
        effective_strategy = (strategy or "balanced").strip() or "balanced"
        sort_key = _normalize_sort_by(sort_by)
        weighted_weights = _parse_weighted_sort_weights(weights) if sort_key == "weighted" else {}
        raw = _aggregate_index_ranking_score_docs(stock_scores, ic, date)
        raw.sort(
            key=lambda d: _score_sort_value(d, sort_key, effective_strategy, weighted_weights),
            reverse=True,
        )
        symbols = [s.get("symbol") for s in raw if s.get("symbol")]
        info_map: Dict[str, Any] = {}
        if symbols:
            for info in stock_info.find(
                {"symbol": {"$in": symbols}},
                {"symbol": 1, "name": 1, "industry": 1},
            ):
                sy = info.get("symbol")
                if sy:
                    info_map[str(sy)] = info
        ranked: List[dict] = []
        for stock in raw:
            sym = stock.get("symbol") or ""
            info = info_map.get(sym)
            name = (info or {}).get("name", "") if info else ""
            if exclude_st and "ST" in (name or ""):
                continue
            ranked.append(stock)
        total = len(ranked)
        page = ranked[offset : offset + page_size]
        formatted: List[dict] = []
        for stock in page:
            sym = stock.get("symbol") or ""
            inf = info_map.get(sym)
            cs = stock.get("composite_score", 0)
            if not isinstance(cs, dict):
                cs = {"balanced": cs}
            row = {
                "symbol": sym,
                "name": (inf or {}).get("name", "") if inf else "",
                "industry": (inf or {}).get("industry", "") if inf else "",
                "composite_score": cs,
                "cycle_score": safe_round(stock.get("cycle_score", 0), 2),
                "value_score": safe_round(stock.get("value_score", 0), 2),
                "fundamental_score": safe_round(stock.get("fundamental_score", 0), 2),
                "growth_score": safe_round(stock.get("growth_score", 0), 2),
                "technical_score": safe_round(stock.get("technical_score", 0), 2),
                "money_flow_score": safe_round(stock.get("money_flow_score", 0), 2),
                "industry_rs_score": stock.get("industry_rs_score"),
                "score_date": stock.get("score_date", ""),
            }
            if sort_key == "weighted":
                row["weighted_score"] = _weighted_score_or_none(row, sort_key, weighted_weights)
                row["weighted_score_weights"] = weighted_weights
            row.update(dict(RETURN_SINCE_EMPTY))
            row.update(dict(PRIOR_3M_EMPTY))
            formatted.append(row)
        try:
            vp_col = get_data_db()["volume_price"]
            # Index rankings are paged; enrich only the visible page to keep
            # large constituent universes cheap while preserving table parity.
            enrich_rows_return_since_score(formatted, vp_col)
            enrich_rows_prior_3m_return(formatted, vp_col)
        except Exception as ex:
            print(f"[stock-rankings/by-index] enrich price returns failed: {ex}")
        return {
            "success": True,
            "data": formatted,
            "total": total,
            "offset": offset,
            "page_size": page_size,
            "date": date or "",
            "index_code": ic,
            "message": "按指数 %s 获取评分成功" % ic,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print("按指数获取股票评分失败: %s\n%s" % (str(e), traceback.format_exc()))
        raise HTTPException(
            status_code=500, detail="按指数获取股票评分失败: %s" % str(e)
        )


@router.get("/stock-rankings")
async def get_stock_rankings(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    min_score: Optional[float] = Query(None, description="最低分数过滤"),
    exclude_st: bool = Query(True, description="是否排除ST股票"),
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为最新日期"),
    strategy: str = Query('balanced', description="指定排行榜策略，balanced/aggressive/conservative"),
    sort_by: str = Query(
        "composite",
        description="排序维度：composite/weighted/cycle/growth/fundamental/value/technical/money_flow",
    ),
    weights: Optional[str] = Query(
        None,
        description="sort_by=weighted 时使用，如 growth:0.6,cycle:0.4；权重自动归一化",
    ),
):
    """
    获取股票评分排行榜 - 每只股票只显示最新一天的评分（简洁实现）
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']
        effective_strategy = (strategy or "balanced").strip() or "balanced"
        sort_key = _normalize_sort_by(sort_by)
        weighted_weights = _parse_weighted_sort_weights(weights) if sort_key == "weighted" else {}

        # Efficient aggregation: $sort -> $group (first doc) -> project -> sort
        if date:
            match_stage = {"$match": {"score_date": date}}
            if min_score is not None:
                match_stage["$match"].update({"composite_score": {"$gte": min_score}})
            pipeline = [
                match_stage,
                {"$project": {
                    "symbol": 1,
                    "composite_score": 1,
                    "cycle_score": 1,
                    "value_score": 1,
                    "fundamental_score": 1,
                    "growth_score": 1,
                    "technical_score": 1,
                    "money_flow_score": 1,
                    "industry_rs_score": 1,
                    "score_date": 1
                }},
                {"$sort": {"composite_score": -1}},
            ]
        else:
            # sort by score_date desc, then group by symbol taking first
            pipeline = [
                {"$sort": {"score_date": -1}},
                {"$group": {"_id": "$symbol", "first": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$first"}},
                {"$project": {
                    "symbol": 1,
                    "composite_score": 1,
                    "cycle_score": 1,
                    "value_score": 1,
                    "fundamental_score": 1,
                    "growth_score": 1,
                    "technical_score": 1,
                    "money_flow_score": 1,
                    "industry_rs_score": 1,
                    "score_date": 1
                }},
            ]
            if min_score is not None:
                pipeline.append({"$match": {"composite_score": {"$gte": min_score}}})
            pipeline.extend([
                {"$sort": {"composite_score": -1}},
            ])

        try:
            all_stocks = list(stock_scores.aggregate(pipeline))
        except Exception:
            # Fallback: simple find with sorting if aggregation fails
            base_query = {"score_date": date} if date else {}
            if min_score is not None:
                base_query["composite_score"] = {"$gte": min_score}
            all_stocks = list(stock_scores.find(base_query).sort("composite_score", -1).limit(limit * 2))

        # Batch attach basic info to avoid N+1 queries
        symbols = [s.get('symbol') for s in all_stocks if s.get('symbol')]
        info_map = {}
        if symbols:
            # Use positional projection arg for broader compatibility with test fakes
            for info in stock_info.find({"symbol": {"$in": symbols}}, {"symbol": 1, "name": 1, "industry": 1}):
                info_map[info.get('symbol')] = info
        for stock in all_stocks:
            info = info_map.get(stock.get('symbol'))
            stock['name'] = info.get('name', '') if info else ''
            stock['industry'] = info.get('industry', '') if info else ''

        # filter ST if requested (simple heuristic: name contains 'ST')
        if exclude_st:
            top_stocks = [s for s in all_stocks if 'ST' not in (s.get('name') or '')]
        else:
            top_stocks = all_stocks

        # use centralized helper to extract strategy-specific score
        top_stocks.sort(
            key=lambda s: _score_sort_value(s, sort_key, effective_strategy, weighted_weights),
            reverse=True,
        )

        formatted = []
        for stock in top_stocks[:limit]:
            cs = stock.get('composite_score', 0)
            # ensure composite_score is a dict for consistent responses
            if not isinstance(cs, dict):
                cs = {'balanced': cs}

            row = {
                'symbol': stock.get('symbol', ''),
                'name': stock.get('name', ''),
                'industry': stock.get('industry', ''),
                'composite_score': cs,
                'cycle_score': safe_round(stock.get('cycle_score', 0), 2),
                'value_score': safe_round(stock.get('value_score', 0), 2),
                'fundamental_score': safe_round(stock.get('fundamental_score', 0), 2),
                'growth_score': safe_round(stock.get('growth_score', 0), 2),
                'technical_score': safe_round(stock.get('technical_score', 0), 2),
                'money_flow_score': safe_round(stock.get('money_flow_score', 0), 2),
                'industry_rs_score': stock.get('industry_rs_score'),
                'score_date': stock.get('score_date', '')
            }
            if sort_key == "weighted":
                row["weighted_score"] = _weighted_score_or_none(row, sort_key, weighted_weights)
                row["weighted_score_weights"] = weighted_weights
            row.update(dict(RETURN_SINCE_EMPTY))
            row.update(dict(PRIOR_3M_EMPTY))
            formatted.append(row)

        try:
            vp_col = get_data_db()["volume_price"]
            enrich_rows_return_since_score(formatted, vp_col)
            enrich_rows_prior_3m_return(formatted, vp_col)
        except Exception as ex:
            print(f"[stock-rankings] enrich price returns failed: {ex}")

        return {
            "success": True,
            "data": formatted,
            "total": len(formatted),
            "message": "获取排行榜成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取股票评分失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票评分失败: {str(e)}")

@router.get("/stock-score-detail")
async def get_single_score_detail(
    request: Request,
    symbol: str = Query(..., description="股票代码"),
    category: str = Query(..., description="评分类别: composite|cycle|value|fundamental|growth|technical|money_flow"),
    score_date: Optional[str] = Query(None, description="评分日期 YYYYMMDD, 为空则取最新"),
    strategy: str = Query('balanced', description="策略: balanced|aggressive|conservative 等，与文档中 strategy_scores 对应")
):
    """返回指定股票某一评分类别的详情字段。

    category 到字段映射:
    composite -> 综合得分拆解（含各子项及策略权重）
    cycle -> cycle_details
    value -> value_details
    fundamental -> fundamental_details
    growth -> growth_details
    technical -> technical_details
    money_flow -> money_flow_details
    """
    try:
        cat_map = {
            'cycle': 'cycle_details',
            'value': 'value_details',
            'fundamental': 'fundamental_details',
            'growth': 'growth_details',
            'technical': 'technical_details',
            'money_flow': 'money_flow_details'
        }
        supported_categories = set(list(cat_map.keys()) + ['composite'])
        if category not in supported_categories:
            raise HTTPException(status_code=400, detail=f"不支持的评分类别: {category}")
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        query = {"symbol": symbol}
        if score_date:
            query["score_date"] = score_date
            doc = stock_scores.find_one(query)
        else:
            doc = stock_scores.find_one(query, sort=[('score_date', -1)])
        if not doc:
            raise HTTPException(status_code=404, detail="未找到对应评分记录")
        # 处理综合得分拆解
        if category == 'composite':
            composite_score = doc.get('composite_score') or {}
            # 确保 composite_score 统一为 dict
            if not isinstance(composite_score, dict):
                composite_score = {'balanced': composite_score}
            strategy_scores = doc.get('strategy_scores') or {}
            strat_obj = strategy_scores.get(strategy) or strategy_scores.get('balanced') or {}
            weight_map = strat_obj.get('weight') or strat_obj.get('weights') or {}
            # 如果 weight_map 为空，尝试 fallback 通用默认权重（以 balanced 为例）
            if not weight_map:
                weight_map = {'cycle': 0.25, 'growth': 0.25, 'fundamental': 0.35, 'value': 0.35, 'technical': 0.25, 'money_flow': 0.15}
            # 构造子项得分（直接使用文档中各独立字段）
            sub_scores = {
                'cycle': doc.get('cycle_score'),
                'growth': doc.get('growth_score'),
                'fundamental': doc.get('fundamental_score'),
                'value': doc.get('value_score'),
                'technical': doc.get('technical_score'),
                'money_flow': doc.get('money_flow_score')
            }
            # 组装列表展示友好字段
            details_payload = {}
            name_map = {
                'cycle': '动量评分',
                'growth': '成长评分',
                'fundamental': '基本面评分',
                'value': '价值评分',
                'technical': '技术面评分',
                'money_flow': '资金流评分'
            }
            for key, score_val in sub_scores.items():
                if score_val is None:
                    continue
                w = weight_map.get(key)
                w_display = f"{round(w*100, 2)}%" if isinstance(w, (int, float)) else '-' 
                details_payload[f"{name_map.get(key, key)} (权重: {w_display})"] = round(score_val, 2) if isinstance(score_val, (int, float)) else score_val
            details_payload['当前策略'] = strategy
            details_payload['综合得分(当前策略)'] = composite_score.get(strategy)
            return {
                'success': True,
                'data': {
                    'symbol': symbol,
                    'score_date': doc.get('score_date'),
                    'category': category,
                    'details': details_payload
                },
                'message': '获取综合得分拆解成功'
            }
        # 其它分类处理
        detail_field = cat_map[category]
        details = doc.get('details') or {}
        # 兼容旧结构: 直接平铺字段
        if not details and any(k.endswith('_details') for k in doc.keys()):
            details = {k: v for k, v in doc.items() if k.endswith('_details')}
        category_details = details.get(detail_field, doc.get(detail_field, {})) or {}
        return {
            'success': True,
            'data': {
                'symbol': symbol,
                'score_date': doc.get('score_date'),
                'category': category,
                'details': category_details
            },
            'message': '获取评分详情成功'
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"获取评分详情失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取评分详情失败: {str(e)}")

@router.get("/score-history/{symbol}")
async def get_stock_score_history(
    request: Request,
    symbol: str,
    days: int = Query(30, ge=1, le=365),
    date: Optional[str] = Query(None, description="截止日期 YYYYMMDD,默认为昨天")
):
    """获取股票评分历史记录（直接查数据库）"""
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 构建查询条件
        query = {"symbol": symbol}
        if date:
            score_date_filter = {"$lte": date}
            query["score_date"] = cast(Any, score_date_filter)
        # 查询最近days天的评分，按score_date降序
        cursor = stock_scores.find(query).sort("score_date", -1).limit(days)
        score_history = []
        for score in cursor:
            if '_id' in score:
                score["_id"] = str(score["_id"])
            score_history.append(score)
        # 按日期升序返回
        score_history = sorted(score_history, key=lambda x: x.get("score_date", ""))
        return {
            "success": True,
            "data": score_history,
            "total": len(score_history),
            "message": f"获取 {symbol} 最近{days}天评分历史"
        }
    except Exception as e:
        import traceback
        print(f"获取评分历史失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取评分历史失败: {str(e)}")

@router.get("/categories")
async def get_score_categories():
    """
    获取评分维度说明
    """
    categories = {
        "cycle_score": {
            "name": "动量评分",
            "weight": 0.25,
            "description": "基于多周期价量趋势(均线斜率/价格位置/波动率)评估个股动量强弱",
            "factors": ["价格相对历史高低点位置", "近期价格趋势", "波动率分析"]
        },
        "fundamental_score": {
            "name": "基本面评分", 
            "weight": 0.35,
            "description": "基于公司基本面数据的评估",
            "factors": ["市值规模", "行业热度", "历史价格稳定性", "财务指标"]
        },
        "technical_score": {
            "name": "技术面评分",
            "weight": 0.25, 
            "description": "基于技术分析指标的评估",
            "factors": ["价格趋势", "成交量趋势", "技术指标(RSI, MACD等)"]
        },
        "money_flow_score": {
            "name": "资金流向评分",
            "weight": 0.15,
            "description": "基于大资金进出情况的评估", 
            "factors": ["大单净买入", "资金流入趋势", "连续净买入天数"]
        }
    }
    
    return {
        "success": True,
        "data": categories,
        "message": "获取评分维度说明成功"
    }

@router.get("/statistics")
async def get_scoring_statistics(
    request: Request,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD, 默认为昨天")
):
    """获取评分统计信息（直接查数据库）"""
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 构建查询条件
        match_query = {}
        if date:
            match_query["score_date"] = date
        # 统计各分数段的股票数量
        pipeline = [
            {"$match": match_query},
            {
                "$group": {
                    "_id": {
                        "$switch": {
                            "branches": [
                                {"case": {"$gte": ["$composite_score", 80]}, "then": "优秀(80-100)"},
                                {"case": {"$gte": ["$composite_score", 70]}, "then": "良好(70-79)"},
                                {"case": {"$gte": ["$composite_score", 60]}, "then": "一般(60-69)"},
                                {"case": {"$gte": ["$composite_score", 50]}, "then": "较差(50-59)"}
                            ],
                            "default": "很差(0-49)"
                        }
                    },
                    "count": {"$sum": 1},
                    "avg_score": {"$avg": "$composite_score"}
                }
            },
            {"$sort": {"avg_score": -1}}
        ]
        score_distribution = list(stock_scores.aggregate(pipeline))
        # 总体统计
        total_stocks = stock_scores.count_documents(match_query)
        avg_pipeline = [{"$match": match_query}, {"$group": {"_id": None, "avg": {"$avg": "$composite_score"}}}]
        avg_result = list(stock_scores.aggregate(avg_pipeline))
        avg_score = avg_result[0]["avg"] if avg_result else 0
        return {
            "success": True,
            "data": {
                "total_stocks": total_stocks,
                "average_score": round(avg_score, 2),
                "score_distribution": score_distribution,
                "query_date": date if date else "",
                "last_update": datetime.now().isoformat()
            },
            "message": "获取评分统计成功"
        }
    except Exception as e:
        import traceback
        print(f"获取统计信息失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")

@router.get("/current-date")
async def get_current_date():
    """获取当前系统使用的默认日期（昨天）"""
    yesterday = datetime.now() - timedelta(days=1)
    return {
        "success": True,
        "data": {
            "default_date": yesterday.strftime('%Y%m%d'),
            "formatted_date": yesterday.strftime('%Y年%m月%d日'),
            "description": "系统默认使用前一天作为股票评分基准日期"
        },
        "message": "获取默认日期成功"
    }

@router.get("/stock-dates")
async def get_stock_dates(request: Request, symbol: str = Query(..., description="股票代码，例如 000001")):
    """
    返回指定股票历史存在评分的所有日期（降序）
    """
    try:
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        # 使用聚合获取该 symbol 的所有 distinct score_date 并按降序排序
        pipeline = [
            {"$match": {"symbol": symbol}},
            {"$group": {"_id": "$score_date"}},
            {"$sort": {"_id": -1}}
        ]
        results = list(stock_scores.aggregate(pipeline))
        dates = [r['_id'] for r in results if r and '_id' in r]
        return {"success": True, "data": dates, "total": len(dates), "message": f"找到 {len(dates)} 个评分日期"}
    except Exception as e:
        import traceback
        print(f"获取股票可用评分日期失败: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"获取股票可用评分日期失败: {str(e)}")

# ✅ 添加请求模型
class SelectedStocksRequest(BaseModel):
    symbols: List[str]
    dates: Optional[List[str]] = None
    strategy: Optional[str] = None
    # 指数视图（沪深300/中证500 等）批量拉榜：为 true 时不访问 volume_price，只返回评分表字段
    index_mode: bool = False
    # True: 不算评分以来涨跌；False: 强制算；None: 由 index_mode / 只数阈值决定
    skip_return_since: Optional[bool] = None


def _should_enrich_return_since(body: SelectedStocksRequest, n_symbols: int) -> bool:
    if body.index_mode or body.skip_return_since is True:
        return False
    if body.skip_return_since is False:
        return True
    return n_symbols <= SELECTED_RETURN_SINCE_AUTO_THRESHOLD

# ✅ 修复 selected 端点 - 确保每只股票只显示最新评分

@router.post("/stock-rankings/selected")
async def get_selected_stock_rankings(
    request: Request,
    body: SelectedStocksRequest,
    date: Optional[str] = Query(None, description="指定日期 YYYYMMDD,默认为每只股票的最新日期"),
    strategy: Optional[str] = Query(
        None,
        description="与 /stock-rankings 一致，如 balanced/aggressive/conservative；优先于 body.strategy",
    ),
    sort_by: str = Query(
        "composite",
        description="排序维度：composite/weighted/cycle/growth/fundamental/value/technical/money_flow",
    ),
    weights: Optional[str] = Query(
        None,
        description="sort_by=weighted 时使用，如 growth:0.6,cycle:0.4；权重自动归一化",
    ),
    offset: int = Query(0, ge=0, description="分页：排序后截取起点"),
    page_size: Optional[int] = Query(
        None,
        ge=1,
        le=500,
        description="分页：本页条数；不传则返回全部（兼容旧客户端）",
    ),
):
    """
    获取指定股票的评分 - 每只股票只显示最新一天的评分（直接查数据库）
    """
    try:
        symbols = body.symbols
        if not symbols:
            raise HTTPException(status_code=400, detail="股票代码列表不能为空")
        db = get_db_collections_from_request(request)
        stock_scores = db['stock_scores']
        stock_info = db['stock_info']
        score_query_symbols = _expanded_query_symbols(symbols)
        qs = (strategy or "").strip()
        bs = (body.strategy or "").strip() if body.strategy else ""
        effective_strategy = qs or bs or "balanced"
        sort_key = _normalize_sort_by(sort_by)
        weighted_weights = _parse_weighted_sort_weights(weights) if sort_key == "weighted" else {}
        # support optional multi-date per-symbol queries via body.dates
        dates = getattr(body, 'dates', None) or None
        if dates:
            # build a lightweight per-date structure for each requested symbol
            info_docs = _stock_info_find_in_chunks(
                stock_info, score_query_symbols, {"symbol": 1, "name": 1, "industry": 1}
            )
            info_cache: Dict[str, Any] = {}
            for doc in info_docs:
                sy = doc.get("symbol")
                if not sy:
                    continue
                for k in _symbol_key_variants(str(sy)):
                    info_cache[k] = doc
            formatted_stocks = []
            try:
                vp_col = get_data_db()["volume_price"]
            except Exception:
                vp_col = None
            for sym in symbols:
                per_date_scores = {}
                per_date_fields = {}
                per_date_return_since: Dict[str, dict] = {}
                per_date_prior_3m_return: Dict[str, dict] = {}
                latest_by_symbol: Dict[str, Any] = {}
                base_by_key: Dict[Tuple[str, str], Any] = {}
                prior_end_by_key: Dict[Tuple[str, str], Any] = {}
                prior_base_by_key: Dict[Tuple[str, str], Any] = {}
                latest_any = None
                for d in dates:
                    if vp_col is not None:
                        per_date_return_since[d] = compute_return_since_score(
                            vp_col, sym, d, latest_by_symbol, base_by_key
                        )
                        per_date_prior_3m_return[d] = compute_prior_3m_return(
                            vp_col, sym, d, prior_end_by_key, prior_base_by_key
                        )
                    else:
                        per_date_return_since[d] = {
                            "return_since_score_pct": None,
                            "price_base_trade_date": None,
                            "price_latest_trade_date": None,
                        }
                        per_date_prior_3m_return[d] = dict(PRIOR_3M_EMPTY)
                    doc = _find_one_stock_score(stock_scores, sym, d)
                    if doc:
                        latest_any = latest_any or doc
                        cs = doc.get('composite_score', 0)
                        if not isinstance(cs, dict):
                            cs = {'balanced': cs}
                        per_date_scores[d] = cs
                        per_date_fields[d] = {
                            'cycle_score': safe_round(doc.get('cycle_score', 0), 2),
                            'growth_score': safe_round(doc.get('growth_score', 0), 2),
                            'fundamental_score': safe_round(doc.get('fundamental_score', 0), 2),
                            'value_score': safe_round(doc.get('value_score', 0), 2),
                            'technical_score': safe_round(doc.get('technical_score', 0), 2),
                            'money_flow_score': safe_round(doc.get('money_flow_score', 0), 2),
                            'industry_rs_score': doc.get('industry_rs_score')
                        }
                info = None
                for k in _symbol_key_variants(sym):
                    if k in info_cache:
                        info = info_cache[k]
                        break
                if not info:
                    info = _find_one_stock_info(stock_info, sym)
                formatted_stocks.append({
                    'symbol': sym,
                    'name': info.get('name', '') if info else '',
                    'industry': info.get('industry', '') if info else '',
                    'composite_score': latest_any.get('composite_score', {}) if latest_any else {},
                    'per_date_scores': per_date_scores,
                    'per_date_fields': per_date_fields,
                    'per_date_weighted_scores': (
                        {
                            d: safe_round(weighted_dimension_score(fields, weighted_weights), 2)
                            for d, fields in per_date_fields.items()
                        }
                        if sort_key == "weighted"
                        else {}
                    ),
                    'per_date_return_since': per_date_return_since,
                    'per_date_prior_3m_return': per_date_prior_3m_return,
                    'score_date': latest_any.get('score_date') if latest_any else '',
                    **(
                        {
                            'weighted_score': safe_round(
                                _weighted_sort_value_from_per_date_fields(
                                    {'per_date_fields': per_date_fields},
                                    weighted_weights,
                                ),
                                2,
                            ),
                            'weighted_score_weights': weighted_weights,
                        }
                        if sort_key == "weighted"
                        else {}
                    ),
                })
            formatted_stocks.sort(
                key=lambda row: (
                    _score_sort_value(row, sort_key, effective_strategy, weighted_weights)
                    if sort_key == "composite"
                    else _weighted_sort_value_from_per_date_fields(row, weighted_weights)
                    if sort_key == "weighted"
                    else max(
                        (
                            safe_float(fields.get(SCORE_SORT_FIELDS[sort_key]), 0.0)
                            for fields in (row.get("per_date_fields") or {}).values()
                        ),
                        default=0.0,
                    )
                ),
                reverse=True,
            )
            return {
                'success': True,
                'data': formatted_stocks,
                'total': len(formatted_stocks),
                'dates': dates,
                'message': f'成功获取 {len(formatted_stocks)} 只指定股票的多日期评分'
            }

        else:
            selected_stocks = []
            if date:
                for batch in _chunk_list(symbols, SELECTED_SYMBOLS_QUERY_CHUNK):
                    exp = _expanded_query_symbols(batch)
                    if not exp:
                        continue
                    q = {"symbol": {"$in": exp}, "score_date": date}
                    selected_stocks.extend(
                        list(stock_scores.find(q, _STOCK_SCORE_LIST_PROJECTION))
                    )
            else:
                for batch in _chunk_list(symbols, SELECTED_SYMBOLS_QUERY_CHUNK):
                    exp = _expanded_query_symbols(batch)
                    if not exp:
                        continue
                    part = _aggregate_latest_score_doc_per_symbol(stock_scores, exp)
                    if not part:
                        part = _find_latest_per_symbol_fallback(stock_scores, exp)
                    selected_stocks.extend(part)
        # 同一标的若库中多条键（极少见），按请求侧 symbol 去重并保留当前策略下分更高的一条
        if selected_stocks:
            by_req: Dict[str, dict] = {}
            for st in selected_stocks:
                rk = _requested_symbol_for_db_row(st.get("symbol", ""), symbols)
                cur = by_req.get(rk)
                if cur is None or extract_strategy_score(
                    st.get("composite_score", 0), effective_strategy
                ) > extract_strategy_score(cur.get("composite_score", 0), effective_strategy):
                    by_req[rk] = st
            selected_stocks = list(by_req.values())
        # 批量补全股票名称和行业（分段 $in 后在内存建索引）
        info_docs = _stock_info_find_in_chunks(
            stock_info, score_query_symbols, {"symbol": 1, "name": 1, "industry": 1}
        )
        info_by_key: Dict[str, Any] = {}
        for doc in info_docs:
            sy = doc.get("symbol")
            if not sy:
                continue
            for k in _symbol_key_variants(str(sy)):
                info_by_key[k] = doc
        for stock in selected_stocks:
            inf = _pick_vp_doc(info_by_key, stock.get("symbol", ""))
            stock["name"] = inf.get("name", "") if inf else ""
            stock["industry"] = inf.get("industry", "") if inf else ""
        # 按当前策略降序（兼容 composite_score 为 dict 或数值；聚合 $sort 对 dict 不可靠，统一在 Python 侧排序）
        selected_stocks.sort(
            key=lambda x: _score_sort_value(x, sort_key, effective_strategy, weighted_weights),
            reverse=True,
        )
        # 格式化返回数据
        formatted_stocks = []
        for stock in selected_stocks:
            if '_id' in stock:
                stock['_id'] = str(stock['_id'])
            cs = stock.get('composite_score', 0)
            if not isinstance(cs, dict):
                cs = {'balanced': cs}
            formatted_stock = {
                'symbol': stock.get('symbol', ''),
                'name': stock.get('name', ''),
                'industry': stock.get('industry', ''),
                'composite_score': cs,
                'cycle_score': safe_round(stock.get('cycle_score', 0), 2),
                'value_score': safe_round(stock.get('value_score', 0), 2),
                'fundamental_score': safe_round(stock.get('fundamental_score', 0), 2),
                'growth_score': safe_round(stock.get('growth_score', 0), 2),
                'technical_score': safe_round(stock.get('technical_score', 0), 2),
                'money_flow_score': safe_round(stock.get('money_flow_score', 0), 2),
                'industry_rs_score': stock.get('industry_rs_score'),
                'score_date': stock.get('score_date', date if date else '')
            }
            if sort_key == "weighted":
                formatted_stock["weighted_score"] = _weighted_score_or_none(
                    formatted_stock,
                    sort_key,
                    weighted_weights,
                )
                formatted_stock["weighted_score_weights"] = weighted_weights
            formatted_stock.update(dict(RETURN_SINCE_EMPTY))
            formatted_stock.update(dict(PRIOR_3M_EMPTY))
            formatted_stocks.append(formatted_stock)
        total_all = len(formatted_stocks)
        if page_size is not None:
            page_rows = formatted_stocks[offset : offset + page_size]
        else:
            page_rows = formatted_stocks
        if _should_enrich_return_since(body, len(symbols)):
            try:
                vp_col = get_data_db()["volume_price"]
                enrich_rows_return_since_score(
                    page_rows, vp_col
                )
                enrich_rows_prior_3m_return(page_rows, vp_col)
            except Exception as ex:
                print(f"[stock-rankings/selected] enrich price returns failed: {ex}")
        else:
            for fs in page_rows:
                fs.update(RETURN_SINCE_EMPTY)
                fs.update(PRIOR_3M_EMPTY)
        for fs in page_rows:
            fs["symbol"] = _requested_symbol_for_db_row(fs.get("symbol", ""), symbols)
        eff_page_size = page_size if page_size is not None else total_all
        return {
            "success": True,
            "data": page_rows,
            "total": total_all,
            "offset": offset if page_size is not None else 0,
            "page_size": eff_page_size,
            "date": date if date else "",
            "message": (
                f"成功获取 {len(page_rows)} 只指定股票评分 (共 {total_all} 只；offset={offset})"
                if page_size is not None
                else f"成功获取 {len(page_rows)} 只指定股票评分 (每只股票显示最新评分)"
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f"获取指定股票评分失败: {str(e)}\n{traceback.format_exc()}"
        print(error_detail)
        raise HTTPException(status_code=500, detail=f"获取指定股票评分失败: {str(e)}")
