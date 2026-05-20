"""
用户自选股管理相关路由
处理自选股列表、策略、实时数据等功能
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from models.all_models import (
    WatchlistRequest, WatchlistUpdateRequest, WatchlistStrategySetRequest, 
    WatchlistStrategyResponse
)
from auth import get_current_active_user
from db import get_db_collections, get_data_db
from datetime import datetime
from zoneinfo import ZoneInfo

router = APIRouter()

# 与 quant-data-engine 东财自选轮询一致：按上海日历日匹配 ``trade_date``，避免 API 容器为 UTC 时错位。
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _cn_calendar_today_yyyymmdd() -> str:
    return datetime.now(_CN_TZ).strftime("%Y%m%d")


def _trade_date_match_today(today_yyyymmdd: str) -> Any:
    """``volume_price`` / ``etf_daily`` 的 ``trade_date`` 在库中可能是 str / int / ``YYYY-MM-DD`` 等。"""
    vals: List[Any] = [today_yyyymmdd]
    try:
        vals.append(int(today_yyyymmdd))
    except (TypeError, ValueError):
        pass
    if len(today_yyyymmdd) == 8 and today_yyyymmdd.isdigit():
        vals.append(f"{today_yyyymmdd[:4]}-{today_yyyymmdd[4:6]}-{today_yyyymmdd[6:8]}")
    return {"$in": vals}


def _norm_trade_date_to_yyyymmdd(td: Any) -> Optional[str]:
    """将 Mongo 中的 trade_date 规范为 8 位 YYYYMMDD，便于与「上海今日」比较。"""
    if td is None:
        return None
    if isinstance(td, int):
        s = str(td)
        return s[:8] if len(s) >= 8 else None
    if isinstance(td, str):
        s = td.replace("-", "").replace("/", "")[:8]
        return s if len(s) == 8 and s.isdigit() else None
    if hasattr(td, "strftime"):
        try:
            return td.strftime("%Y%m%d")
        except Exception:
            return None
    return None


def _eastmoney_trade_date_within_lookback(doc_td: str, today: str, max_days: int = 15) -> bool:
    """东财快照的 ``trade_date`` 可早于上海日历「今日」（周末/长假），在间隔内仍用于展示 OHLC。"""
    try:
        d_doc = datetime.strptime(doc_td, "%Y%m%d").date()
        d_today = datetime.strptime(today, "%Y%m%d").date()
    except ValueError:
        return False
    delta = (d_today - d_doc).days
    if delta < 0:
        return False
    return delta <= max_days


def _volume_price_symbol_filter(variants: List[str]) -> Dict[str, Any]:
    """``volume_price`` 有的历史数据用 ``ts_code``、有的用 ``symbol``。"""
    return {"$or": [{"symbol": {"$in": variants}}, {"ts_code": {"$in": variants}}]}


def _safe_round_price(val: Any, ndigits: int = 2) -> Optional[float]:
    if val is None:
        return None
    try:
        x = float(val)
        if x != x:  # NaN
            return None
        return round(x, ndigits)
    except (TypeError, ValueError):
        return None


# 辅助函数
def get_user_id(current_user: dict):
    """统一获取用户唯一标识（主键 _id），并转换为字符串"""
    return str(current_user["id"])


# ==================== 自选股列表管理 ====================

@router.get("/watchlist")
def get_user_watchlist(current_user: dict = Depends(get_current_active_user)):
    """获取用户自选股列表"""
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    doc = watchlist_col.find_one({"user_id": user_id})
    symbols = doc["symbols"] if doc and "symbols" in doc else []
    return {"success": True, "data": {"symbols": symbols}}


@router.post("/watchlist/add")
def add_to_watchlist(request: WatchlistRequest, current_user: dict = Depends(get_current_active_user)):
    """添加股票到自选股"""
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    symbol = request.symbol
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$addToSet": {"symbols": symbol}},
        upsert=True
    )
    return {"success": True}


@router.delete("/watchlist/remove/{symbol}")
def remove_from_watchlist(symbol: str, current_user: dict = Depends(get_current_active_user)):
    """从自选股中删除股票"""
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$pull": {"symbols": symbol}}
    )
    return {"success": True}


@router.put("/watchlist")
def update_watchlist(request: WatchlistUpdateRequest, current_user: dict = Depends(get_current_active_user)):
    """更新整个自选股列表"""
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$set": {"symbols": request.symbols}},
        upsert=True
    )
    return {"success": True}


# ==================== 自选股策略管理 ====================

@router.get("/watchlist/strategies")
def get_watchlist_strategies(current_user: dict = Depends(get_current_active_user)):
    """获取用户自选股的所有策略"""
    collections = get_db_collections()
    strat_col = collections["watchlist_strategies"]
    user_id = get_user_id(current_user)
    docs = strat_col.find({"user_id": user_id})
    data = []
    for d in docs:
        data.append({
            "symbol": d["symbol"],
            "strategy_key": d.get("strategy_key"),
            "params": d.get("params", {}),
            "updated_at": d.get("updated_at"),
            "enabled": d.get("enabled", True),
        })
    return {"success": True, "data": data}


@router.post("/watchlist/strategy")
def set_watchlist_strategy(req: WatchlistStrategySetRequest, current_user: dict = Depends(get_current_active_user)):
    """设置或更新自选股的策略
    
    CRITICAL: Query must include strategy_key to ensure precise match
    This prevents updating wrong strategy when user has multiple strategies for same symbol
    
    NOTE: 自动设置 engine="vnpy"，因为实盘策略目前只支持 vnpy 引擎
    """
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    strat_col = collections["watchlist_strategies"]
    user_id = get_user_id(current_user)
    
    # VIP-only live deployment restriction for Hidden Dragon strategy
    if req.strategy == "hidden_dragon":
        service_level = current_user.get("service_level", "free")
        if service_level != "vip":
            # 用户可见错误信息使用中文
            raise HTTPException(status_code=403, detail="潜龙低吸策略仅对VIP用户开放")
    
    # Auto-add symbol to watchlist if not present
    doc = watchlist_col.find_one({"user_id": user_id})
    symbols = doc["symbols"] if doc and "symbols" in doc else []
    if req.symbol not in symbols:
        # Automatically add symbol to watchlist
        symbols.append(req.symbol)
        watchlist_col.update_one(
            {"user_id": user_id},
            {"$set": {"symbols": symbols}},
            upsert=True
        )
    
    import time
    enabled = getattr(req, "enabled", True)
    
    # 构建更新文档，自动添加 engine 字段
    update_doc = {
        "strategy_key": req.strategy,
        "params": req.params,
        "updated_at": time.time(),
        "enabled": enabled,
        "engine": "vnpy",  # 🔥 自动设置 engine 为 vnpy（实盘策略仅支持 vnpy）
    }
    
    strat_col.update_one(
        {"user_id": user_id, "symbol": req.symbol, "strategy_key": req.strategy},
        {"$set": update_doc},
        upsert=True
    )
    return {"success": True}


# ==================== 自选股详细信息 ====================

@router.get("/watchlist-stocks")
def get_user_watchlist_stocks(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的详细信息（日K线数据）"""
    try:
        user_id = get_user_id(current_user)
        
        # 获取用户自选股列表
        collections = get_db_collections()
        data_collections = get_data_db()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]
        result = []

        volume_price_col = data_collections["volume_price"]
        etf_basic_col = data_collections.get("etf_basic")
        etf_daily_col = data_collections.get("etf_daily")
        sw_industry_map = _load_sw_industry_map(
            data_collections.get("sw_index_member"),
            symbols,
        )
        
        for symbol in symbols:
            industry = ""
            sw_industry = sw_industry_map.get(remove_exchange_suffix(symbol))
            # Check Stock first
            stock_info = data_collections["stock_info"].find_one({"symbol": symbol})
            if stock_info:
                name = stock_info["name"] if "name" in stock_info else symbol
                industry = stock_info.get("industry", "")
                asset_type = "stock"
                latest_record = volume_price_col.find_one(
                    {"symbol": symbol},
                    sort=[("trade_date", -1)]
                )
            else:
                # Check ETF
                etf_ts = _resolve_etf_ts_code(etf_basic_col, symbol)
                etf_info = (
                    etf_basic_col.find_one({"ts_code": etf_ts})
                    if etf_basic_col is not None and etf_ts
                    else None
                )
                if etf_info:
                    name = etf_info["name"] if "name" in etf_info else symbol
                    asset_type = "etf"
                    latest_record = etf_daily_col.find_one(
                        {"ts_code": etf_ts},
                        sort=[("trade_date", -1)]
                    ) if etf_daily_col is not None else None
                else:
                    name = symbol
                    asset_type = "unknown"
                    latest_record = None

            if latest_record:
                # etf_daily uses 'amount' instead of 'turnover' natively in tushare sometimes
                # but our ingest script maps it to amount. Let's handle both.
                stock_data = {
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "close": latest_record.get("close"),
                    "trade_date": latest_record.get("trade_date"),
                    "volume": latest_record.get("vol"),
                    "turnover": latest_record.get("amount"),
                    "high": latest_record.get("high"),
                    "low": latest_record.get("low"),
                    "open": latest_record.get("open"),
                    "change": latest_record.get("change"),
                    "change_percent": latest_record.get("pct_chg"),
                    "turnover_rate": latest_record.get("turnover_rate"),
                    "pe": latest_record.get("pe"),
                    "market_cap": latest_record.get("market_cap"),
                    "circ_market_cap": latest_record.get("circ_market_cap")
                }
                result.append(stock_data)
            else:
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "close": None,
                    "trade_date": None,
                    "volume": None,
                    "turnover": None,
                    "high": None,
                    "low": None,
                    "open": None,
                    "change": None,
                    "change_percent": None,
                    "turnover_rate": None,
                    "pe": None,
                    "market_cap": None,
                    "circ_market_cap": None
                })
        
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取用户自选股信息失败: {str(e)}"
        }


# ==================== 辅助函数 ====================

def add_exchange_suffix(symbol: str) -> str:
    """Add exchange suffix for stock symbol.
    
    Rules:
    - 6xxxxx (Shanghai) -> .SH
    - 0xxxxx, 3xxxxx (Shenzhen) -> .SZ
    - Already has suffix -> return as-is
    
    Args:
        symbol: Stock symbol (e.g., "600990" or "600990.SH")
        
    Returns:
        str: Symbol with exchange suffix (e.g., "600990.SH")
    """
    if '.' in symbol:
        return symbol  # Already has suffix
    
    if symbol.startswith('6'):
        return f"{symbol}.SH"
    elif symbol.startswith(('0', '3')):
        return f"{symbol}.SZ"
    else:
        return f"{symbol}.SZ"  # Default to Shenzhen


def remove_exchange_suffix(symbol: str) -> str:
    """Remove exchange suffix from stock symbol.
    
    Args:
        symbol: Symbol with suffix (e.g., "600990.SH")
        
    Returns:
        str: Symbol without suffix (e.g., "600990")
    """
    return symbol.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")


def _symbol_variants(symbol: str) -> List[str]:
    """同一标的在库中可能出现的 symbol / ts_code 写法（无后缀、.SH/.SZ 等）。"""
    s = (symbol or "").strip()
    if not s:
        return []
    raw = remove_exchange_suffix(s)
    suffixed = add_exchange_suffix(raw) if raw else s
    out: List[str] = []
    for x in (s, raw, suffixed):
        if x and x not in out:
            out.append(x)
    return out


def _resolve_etf_ts_code(etf_basic_col, symbol: str) -> Optional[str]:
    """若在 ``etf_basic`` 中能识别为场内 ETF，返回规范 ``ts_code``（避免 51 开头被误判为深交所）。"""
    if etf_basic_col is None:
        return None
    s = (symbol or "").strip()
    if not s:
        return None
    base = _symbol_variants(s)
    doc = etf_basic_col.find_one({"ts_code": {"$in": base}})
    if doc and doc.get("ts_code"):
        return str(doc["ts_code"]).strip()
    raw = remove_exchange_suffix(s)
    if len(raw) == 6 and raw.isdigit():
        doc = etf_basic_col.find_one({"ts_code": {"$regex": f"^{raw}\\."}})
        if doc and doc.get("ts_code"):
            return str(doc["ts_code"]).strip()
    return None


_SW_INDUSTRY_PROJ = {
    "_id": 0,
    "con_code": 1,
    "name": 1,
    "l1_code": 1,
    "l1_name": 1,
    "l2_code": 1,
    "l2_name": 1,
    "l3_code": 1,
    "l3_name": 1,
    "src": 1,
}


def _format_sw_industry(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    return {
        "symbol": doc.get("con_code", ""),
        "name": doc.get("name", ""),
        "l1_code": doc.get("l1_code", ""),
        "l1_name": doc.get("l1_name", ""),
        "l2_code": doc.get("l2_code", ""),
        "l2_name": doc.get("l2_name", ""),
        "l3_code": doc.get("l3_code", ""),
        "l3_name": doc.get("l3_name", ""),
        "src": doc.get("src", "SW2021"),
    }


def _load_sw_industry_map(sw_member_col, symbols: List[str], src: str = "SW2021") -> Dict[str, Dict[str, Any]]:
    """批量读取自选股票的申万 L1/L2/L3 行业，key 为去后缀代码。"""
    if sw_member_col is None or not symbols:
        return {}

    variants = list(dict.fromkeys(v for symbol in symbols for v in _symbol_variants(symbol)))
    if not variants:
        return {}

    by_norm: Dict[str, Dict[str, Any]] = {}

    def put_doc(doc: Dict[str, Any]) -> None:
        con_code = str(doc.get("con_code") or "").strip()
        norm = remove_exchange_suffix(con_code)
        if norm and norm not in by_norm:
            by_norm[norm] = _format_sw_industry(doc) or {}

    try:
        for doc in sw_member_col.find(
            {"con_code": {"$in": variants}, "src": src, "is_new": "Y"},
            _SW_INDUSTRY_PROJ,
        ):
            put_doc(doc)

        missing_norms = {
            remove_exchange_suffix(symbol)
            for symbol in symbols
            if remove_exchange_suffix(symbol) not in by_norm
        }
        if missing_norms:
            cursor = sw_member_col.find(
                {"con_code": {"$in": variants}, "src": src},
                _SW_INDUSTRY_PROJ,
            )
            try:
                cursor = cursor.sort("in_date", -1)
            except Exception:
                pass
            for doc in cursor:
                norm = remove_exchange_suffix(str(doc.get("con_code") or ""))
                if norm in missing_norms:
                    put_doc(doc)
    except Exception:
        return {}

    return by_norm


def _merge_variants(*groups: List[str]) -> List[str]:
    out: List[str] = []
    for g in groups:
        for x in g:
            if x and x not in out:
                out.append(x)
    return out


def _etf_daily_intraday_pack(
    etf_daily_col, ts_code: str, today: str
) -> Optional[Dict[str, Any]]:
    """ETF：优先当日 Tushare 日线（盘后或已同步时才有），否则最新一根日线 + 前一交易日用于涨跌。"""
    td_key = _trade_date_match_today(today)
    row = etf_daily_col.find_one({"ts_code": ts_code, "trade_date": td_key})
    if not row:
        row = etf_daily_col.find_one({"ts_code": ts_code}, sort=[("trade_date", -1)])
    if not row or row.get("close") is None:
        return None
    td = row.get("trade_date")
    prev = None
    if td:
        prev = etf_daily_col.find_one(
            {"ts_code": ts_code, "trade_date": {"$lt": td}},
            sort=[("trade_date", -1)],
        )
    return {"row": row, "prev": prev}


def _norm_keys_from_vp_doc(doc: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for k in ("symbol", "ts_code"):
        v = doc.get(k)
        if not v:
            continue
        n = remove_exchange_suffix(str(v).strip())
        if n and n not in out:
            out.append(n)
    return out


def _vp_docs_index_by_norm(docs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按去掉后缀后的代码聚合；同一文档只出现一次。"""
    by_norm: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    def _did(d: Dict[str, Any]) -> Any:
        return d.get("_id") if d.get("_id") is not None else id(d)

    for doc in docs:
        did = _did(doc)
        for nid in _norm_keys_from_vp_doc(doc):
            lst = by_norm[nid]
            if any(_did(x) == did for x in lst):
                continue
            lst.append(doc)
    return dict(by_norm)


def _pick_best_intraday_from_today_docs(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同一标的当日多条：优先 eastmoney_snapshot。"""
    for c in cands:
        if c.get("source") == "eastmoney_snapshot":
            return c
    return cands[0] if cands else None


def _pick_fallback_eastmoney_doc(
    docs: List[Dict[str, Any]], today: str
) -> Optional[Dict[str, Any]]:
    """仅东财快照；按 trade_date 降序取第一条满足当日或 lookback 的文档。"""
    if not docs:
        return None

    def _td_sort_key(d: Dict[str, Any]) -> str:
        t = _norm_trade_date_to_yyyymmdd(d.get("trade_date"))
        return t or ""

    for d in sorted(docs, key=_td_sort_key, reverse=True):
        doc_td = _norm_trade_date_to_yyyymmdd(d.get("trade_date"))
        if doc_td is None:
            continue
        if doc_td == today or _eastmoney_trade_date_within_lookback(doc_td, today):
            return d
    return None


def _format_vp_update_time(vp: Optional[Dict[str, Any]]) -> Optional[str]:
    """volume_price 常见 ``update_time`` 为 ``%Y%m%d %H:%M:%S`` 字符串。"""
    if not vp:
        return None
    raw = vp.get("update_time")
    if raw is None:
        raw = vp.get("synced_at")
    if raw is None:
        return None
    if hasattr(raw, "strftime"):
        try:
            return raw.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    s = str(raw).strip()
    if len(s) >= 15 and s[:8].isdigit() and s[8] in (" ", "T"):
        d = s[:8]
        rest = s[8:].strip()
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {rest}"
    return s


def _batch_intraday_volume_price_map(
    volume_price_col,
    symbols: List[str],
    sym_ctx: Dict[str, Dict[str, Any]],
    today: str,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """一次（或两次）查询 Mongo，按自选 ``symbol`` 映射最佳当日 / 近日 ``volume_price`` 文档。"""
    all_variants: List[str] = []
    seen: set[str] = set()
    for s in symbols:
        for v in sym_ctx[s]["variants"]:
            if v and v not in seen:
                seen.add(v)
                all_variants.append(v)
    empty = {s: None for s in symbols}
    if not all_variants:
        return empty

    td_key = _trade_date_match_today(today)
    sym_f = _volume_price_symbol_filter(all_variants)
    today_docs = list(volume_price_col.find({**sym_f, "trade_date": td_key}))
    by_norm_today = _vp_docs_index_by_norm(today_docs)

    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for sym in symbols:
        nid = remove_exchange_suffix(sym)
        cands = by_norm_today.get(nid, [])
        out[sym] = _pick_best_intraday_from_today_docs(cands)

    need_fb = [sym for sym in symbols if out.get(sym) is None]
    if need_fb:
        limit = min(3000, 200 + 50 * len(all_variants))
        fb_docs = list(
            volume_price_col.find(
                {**sym_f, "source": "eastmoney_snapshot"},
            ).sort([("trade_date", -1)]).limit(limit)
        )
        by_norm_fb = _vp_docs_index_by_norm(fb_docs)
        for sym in need_fb:
            nid = remove_exchange_suffix(sym)
            out[sym] = _pick_fallback_eastmoney_doc(by_norm_fb.get(nid, []), today)

    return out


# ==================== 实时数据 ====================

@router.get("/watchlist-stocks/realtime")
def get_user_watchlist_realtime(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的实时数据。

    股票：① 当日 ``minute_bars``；② 当日 ``volume_price``（优先东财 ``eastmoney_snapshot``）；
    ③ 若日历「今日」尚无记录，可用 **最近若干自然日内** 的东财快照（休市/长假/数据仍停在上一交易日，如库中为 ``20260502`` 而上海已为 ``20260503``）。
    ④ 最近 ``volume_price`` 仅收盘价。``trade_date`` 与「今天」按 **Asia/Shanghai** 对齐，并兼容多种存法。

    ETF：一般无分钟线；东财 ulist 与自选轮询 **仅覆盖带交易所后缀的 ts_code**（裸代码不会进东财任务），
    故在 ①② 不可用时回退到 **``etf_daily``**（优先当日已同步的 Tushare 日线，否则最新一根日线，
    涨跌相对前一交易日收盘）。**非交易时段 / 日线未落地时 ETF 仍可能只有昨收级别数据。**
    """
    try:
        user_id = get_user_id(current_user)
        
        # 1. 获取用户自选股列表
        collections = get_db_collections()
        data_collections = get_data_db()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]  # e.g., ["600990", "300722"]
        
        # 2. 获取昨日收盘价（用于计算涨跌幅）
        etf_basic_col = data_collections.get("etf_basic")
        etf_daily_col = data_collections.get("etf_daily")

        sym_ctx: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            etf_ts = _resolve_etf_ts_code(etf_basic_col, symbol)
            eff_variants = _merge_variants(_symbol_variants(symbol), [etf_ts] if etf_ts else [])
            sym_ctx[symbol] = {"variants": eff_variants, "etf_ts": etf_ts}
        sw_industry_map = _load_sw_industry_map(
            data_collections.get("sw_index_member"),
            symbols,
        )

        yesterday_close_map = {}
        volume_price_col = data_collections["volume_price"]

        # Key 与下方主循环的 symbol 一致（user_watchlists 原文）；命中文档靠 variants，避免后缀不一致丢 close。
        for symbol in symbols:
            variants = sym_ctx[symbol]["variants"]
            latest_record = volume_price_col.find_one(
                _volume_price_symbol_filter(variants),
                sort=[("trade_date", -1)],
            )
            if latest_record:
                yesterday_close_map[symbol] = latest_record.get("close")
            elif etf_daily_col is not None:
                etf_ts = sym_ctx[symbol]["etf_ts"]
                q = {"ts_code": {"$in": variants}}
                if etf_ts:
                    q = {"ts_code": etf_ts}
                etf_record = etf_daily_col.find_one(q, sort=[("trade_date", -1)])
                if etf_record:
                    yesterday_close_map[symbol] = etf_record.get("close")

        minute_bars_col = data_collections["minute_bars"]
        today = _cn_calendar_today_yyyymmdd()

        minute_match_symbols = list(
            dict.fromkeys(v for symbol in symbols for v in sym_ctx[symbol]["variants"])
        )
        
        pipeline = [
            {
                "$match": {
                    "symbol": {"$in": minute_match_symbols},
                    "trade_date": {"$regex": f"^{today}"},
                }
            },
            {"$sort": {"trade_date": 1}},
            {
                "$group": {
                    "_id": "$symbol",
                    "open": {"$first": "$open"},
                    "close": {"$last": "$close"},
                    "high": {"$max": "$high"},
                    "low": {"$min": "$low"},
                    "volume": {"$sum": "$volume"},
                    "latest_time": {"$last": "$trade_date"},
                }
            },
        ]
        realtime_data = list(minute_bars_col.aggregate(pipeline))
        realtime_by_norm: Dict[str, Dict[str, Any]] = {}
        for item in realtime_data:
            nid = remove_exchange_suffix(str(item["_id"]))
            prev = realtime_by_norm.get(nid)
            if prev is None or str(item.get("latest_time") or "") > str(prev.get("latest_time") or ""):
                realtime_by_norm[nid] = item

        vp_batch = _batch_intraday_volume_price_map(
            volume_price_col, symbols, sym_ctx, today
        )

        result = []
        stock_info_col = data_collections["stock_info"]

        for symbol in symbols:
            variants = sym_ctx[symbol]["variants"]
            etf_ts = sym_ctx[symbol]["etf_ts"]
            industry = ""
            sw_industry = sw_industry_map.get(remove_exchange_suffix(symbol))
            stock_info = stock_info_col.find_one({"symbol": {"$in": variants}})
            if stock_info:
                name = stock_info["name"] if "name" in stock_info else symbol
                industry = stock_info.get("industry", "")
                asset_type = "stock"
            else:
                etf_info = (
                    etf_basic_col.find_one({"ts_code": {"$in": variants}})
                    if etf_basic_col is not None
                    else None
                )
                if etf_info:
                    name = etf_info["name"] if "name" in etf_info else symbol
                    asset_type = "etf"
                else:
                    name = symbol
                    asset_type = "unknown"
            
            norm = remove_exchange_suffix(symbol)
            item = realtime_by_norm.get(norm)
            
            if item is not None:
                vp_snap = vp_batch.get(symbol)
                o = (
                    item.get("open")
                    if item.get("open") is not None
                    else (vp_snap.get("open") if vp_snap else None)
                )
                h = (
                    item.get("high")
                    if item.get("high") is not None
                    else (vp_snap.get("high") if vp_snap else None)
                )
                lo = (
                    item.get("low")
                    if item.get("low") is not None
                    else (vp_snap.get("low") if vp_snap else None)
                )
                current_price = item["close"]
                yesterday_close = yesterday_close_map.get(symbol, item.get("open"))
                change = current_price - yesterday_close if yesterday_close else 0
                change_pct = (change / yesterday_close * 100) if yesterday_close else 0
                latest_time_str = item["latest_time"]
                if latest_time_str and len(str(latest_time_str)) >= 12:
                    lts = str(latest_time_str)
                    formatted_time = (
                        f"{lts[0:4]}-{lts[4:6]}-{lts[6:8]} {lts[8:10]}:{lts[10:12]}:00"
                    )
                else:
                    formatted_time = str(latest_time_str)
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "price": _safe_round_price(current_price),
                    "open": _safe_round_price(o),
                    "high": _safe_round_price(h),
                    "low": _safe_round_price(lo),
                    "volume": _safe_round_price(item.get("volume"), 2),
                    "change": _safe_round_price(change, 2),
                    "change_pct": _safe_round_price(change_pct, 2),
                    "update_time": formatted_time,
                })
                continue

            vp = vp_batch.get(symbol)
            if vp and vp.get("close") is not None:
                close = float(vp.get("close"))
                pre = vp.get("pre_close")
                if pre is not None:
                    pre = float(pre)
                ch = vp.get("change")
                if ch is not None:
                    ch = float(ch)
                elif pre is not None:
                    ch = close - pre
                else:
                    ch = 0.0
                pct = vp.get("pct_chg")
                if pct is not None:
                    pct = float(pct)
                elif pre:
                    pct = (ch / pre * 100.0) if pre else 0.0
                else:
                    pct = 0.0
                vol = vp.get("volume")
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "price": _safe_round_price(close),
                    "open": _safe_round_price(vp.get("open")),
                    "high": _safe_round_price(vp.get("high")),
                    "low": _safe_round_price(vp.get("low")),
                    "volume": _safe_round_price(vol, 2) if vol is not None else None,
                    "change": _safe_round_price(ch, 2),
                    "change_pct": _safe_round_price(pct, 2),
                    "update_time": _format_vp_update_time(vp),
                })
                continue

            if etf_ts and etf_daily_col is not None:
                pack = _etf_daily_intraday_pack(etf_daily_col, etf_ts, today)
                if pack:
                    row = pack["row"]
                    prev = pack.get("prev")
                    close = row.get("close")
                    if close is not None:
                        close = float(close)
                        prev_c = prev.get("close") if prev else None
                        if prev_c is not None:
                            prev_c = float(prev_c)
                        ch = (
                            (close - prev_c)
                            if prev_c is not None
                            else 0.0
                        )
                        pct = (
                            (ch / prev_c * 100.0)
                            if prev_c not in (None, 0)
                            else 0.0
                        )
                        vol = row.get("vol")
                        if vol is None:
                            vol = row.get("volume")
                        result.append(
                            {
                                "symbol": symbol,
                                "name": name,
                                "industry": industry,
                                "sw_industry": sw_industry,
                                "asset_type": asset_type,
                                "price": round(close, 2),
                                "open": round(row["open"], 2)
                                if row.get("open") is not None
                                else None,
                                "high": round(row["high"], 2)
                                if row.get("high") is not None
                                else None,
                                "low": round(row["low"], 2)
                                if row.get("low") is not None
                                else None,
                                "volume": round(float(vol), 2)
                                if vol is not None
                                else None,
                                "change": round(ch, 2),
                                "change_pct": round(pct, 2),
                                "update_time": None,
                            }
                        )
                        continue

            yesterday_close = yesterday_close_map.get(symbol)
            if yesterday_close:
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "price": round(yesterday_close, 2),
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "change": 0.0,
                    "change_pct": 0.0,
                    "update_time": None,
                })
            else:
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "price": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "change": None,
                    "change_pct": None,
                    "update_time": None,
                })
        
        return {
            "success": True,
            "data": result
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"获取实时自选股数据失败: {str(e)}"
        }
