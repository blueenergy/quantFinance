"""
用户自选股管理相关路由
处理自选股列表、策略、实时数据等功能
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from models.all_models import (
    WatchlistRequest, WatchlistUpdateRequest, WatchlistStrategySetRequest, 
    WatchlistStrategyResponse
)
from auth import get_current_active_user
from db import get_db_collections, get_data_db
from datetime import datetime, timedelta
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


def _trade_date_match_recent(today_yyyymmdd: str, max_days: int = 15) -> Any:
    """返回最近若干自然日的常见 ``trade_date`` 写法，用于约束最新日线扫描范围。"""
    vals: List[Any] = []
    try:
        today = datetime.strptime(today_yyyymmdd, "%Y%m%d").date()
    except ValueError:
        return _trade_date_match_today(today_yyyymmdd)

    for i in range(max_days + 1):
        d = today - timedelta(days=i)
        compact = d.strftime("%Y%m%d")
        vals.append(compact)
        vals.append(int(compact))
        vals.append(d.strftime("%Y-%m-%d"))
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


# volume_price.source：盘中 miniQMT（及旧 Tushare rt_k），再日终 Tushare；东财快照仅兜底。
_VP_SOURCE_RANK = {
    "miniqmt": 0,
    "tushare_rt_k": 1,
    "tushare_daily": 2,
    "eastmoney_snapshot": 90,
}


def _vp_source_rank(doc: Optional[Dict[str, Any]]) -> int:
    src = str((doc or {}).get("source") or "").strip().lower()
    if src in _VP_SOURCE_RANK:
        return _VP_SOURCE_RANK[src]
    if src.startswith("tushare"):
        return 2
    return 50


def _volume_price_symbol_filter(variants: List[str]) -> Dict[str, Any]:
    """``volume_price`` 有的历史数据用 ``ts_code``、有的用 ``symbol``。"""
    return {"$or": [{"symbol": {"$in": variants}}, {"ts_code": {"$in": variants}}]}


def _unique_variants_for_symbols(symbols: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for symbol in symbols:
        for variant in _symbol_variants(symbol):
            if variant and variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


def _etf_symbol_variants(symbol: str) -> List[str]:
    """ETF 代码常有裸代码输入，批量查 etf_basic 时同时尝试 SH/SZ 写法。"""
    variants = list(_symbol_variants(symbol))
    raw = remove_exchange_suffix(symbol or "")
    if len(raw) == 6 and raw.isdigit():
        for suffix in (".SH", ".SZ", ".BJ"):
            candidate = f"{raw}{suffix}"
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _unique_etf_variants_for_symbols(symbols: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for symbol in symbols:
        for variant in _etf_symbol_variants(symbol):
            if variant and variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


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


def _price_ndigits(asset_type: Any) -> int:
    """A-share stocks tick 0.01; listed ETFs tick 0.001."""
    return 3 if str(asset_type or "").strip().lower() == "etf" else 2


def _bar_volume(doc: Optional[Dict[str, Any]]) -> Any:
    """Daily bars often store ``vol``; eastmoney snapshots use ``volume``."""
    if not doc:
        return None
    vol = doc.get("volume")
    if vol is None:
        vol = doc.get("vol")
    return vol


def _bar_update_time(doc: Optional[Dict[str, Any]]) -> Optional[str]:
    formatted = _format_vp_update_time(doc)
    if formatted:
        return formatted
    if not doc:
        return None
    td = _norm_trade_date_to_yyyymmdd(doc.get("trade_date"))
    if not td:
        return None
    return f"{td[0:4]}-{td[4:6]}-{td[6:8]}"


def _realtime_row_from_bar(
    *,
    symbol: str,
    name: str,
    industry: str,
    sw_industry: Any,
    asset_type: str,
    bar: Dict[str, Any],
) -> Dict[str, Any]:
    close = bar.get("close")
    pre = bar.get("pre_close")
    ch = bar.get("change")
    pct = bar.get("pct_chg")
    if ch is None and close is not None and pre is not None:
        try:
            ch = float(close) - float(pre)
        except (TypeError, ValueError):
            ch = None
    if pct is None and ch is not None:
        try:
            pre_f = float(pre) if pre is not None else 0.0
            pct = (float(ch) / pre_f * 100.0) if pre_f else 0.0
        except (TypeError, ValueError):
            pct = None
    nd = _price_ndigits(asset_type)
    vol = _bar_volume(bar)
    return {
        "symbol": symbol,
        "name": name,
        "industry": industry,
        "sw_industry": sw_industry,
        "asset_type": asset_type,
        "price": _safe_round_price(close, nd),
        "open": _safe_round_price(bar.get("open"), nd),
        "high": _safe_round_price(bar.get("high"), nd),
        "low": _safe_round_price(bar.get("low"), nd),
        "volume": _safe_round_price(vol, 2) if vol is not None else None,
        "change": _safe_round_price(ch, nd),
        "change_pct": _safe_round_price(pct, 2),
        "update_time": _bar_update_time(bar),
    }


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


def _pick_latest_vp_doc(docs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按规范化 trade_date 取最新 ``volume_price`` 文档；同日优先 miniQMT / Tushare。"""
    if not docs:
        return None

    def _td_sort_key(d: Dict[str, Any]) -> str:
        t = _norm_trade_date_to_yyyymmdd(d.get("trade_date"))
        return t or ""

    return sorted(
        docs,
        key=lambda d: (_td_sort_key(d), -_vp_source_rank(d)),
        reverse=True,
    )[0]


def _batch_etf_ts_code_map(etf_basic_col, symbols: List[str]) -> Dict[str, Optional[str]]:
    """批量识别 ETF 规范 ``ts_code``，key 为用户自选原始 symbol。"""
    out: Dict[str, Optional[str]] = {symbol: None for symbol in symbols}
    if etf_basic_col is None or not symbols:
        return out

    variants = _unique_etf_variants_for_symbols(symbols)
    if not variants:
        return out

    by_norm: Dict[str, str] = {}
    try:
        for doc in etf_basic_col.find(
            {"ts_code": {"$in": variants}},
            {"_id": 0, "ts_code": 1},
        ):
            ts_code = str(doc.get("ts_code") or "").strip()
            norm = remove_exchange_suffix(ts_code)
            if ts_code and norm and norm not in by_norm:
                by_norm[norm] = ts_code
    except Exception:
        return out

    for symbol in symbols:
        out[symbol] = by_norm.get(remove_exchange_suffix(symbol))
    return out


def _batch_stock_info_map(stock_info_col, all_variants: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量读取股票基础信息，key 为去后缀代码。"""
    if stock_info_col is None or not all_variants:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    try:
        for doc in stock_info_col.find({"symbol": {"$in": all_variants}}):
            symbol = str(doc.get("symbol") or "").strip()
            norm = remove_exchange_suffix(symbol)
            if norm and norm not in out:
                out[norm] = doc
    except Exception:
        return {}
    return out


def _batch_etf_info_map(etf_basic_col, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量读取 ETF 基础信息，key 为去后缀代码。"""
    if etf_basic_col is None or not symbols:
        return {}

    variants = _unique_etf_variants_for_symbols(symbols)
    if not variants:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    try:
        for doc in etf_basic_col.find({"ts_code": {"$in": variants}}):
            ts_code = str(doc.get("ts_code") or "").strip()
            norm = remove_exchange_suffix(ts_code)
            if norm and norm not in out:
                out[norm] = doc
    except Exception:
        return {}
    return out


def _batch_latest_volume_price_map(
    volume_price_col,
    symbols: List[str],
    sym_ctx: Dict[str, Dict[str, Any]],
    today: str,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """批量读取每只自选股近期最新 ``volume_price`` 文档，用于昨收/兜底。"""
    all_variants = list(
        dict.fromkeys(v for symbol in symbols for v in sym_ctx[symbol]["variants"])
    )
    out: Dict[str, Optional[Dict[str, Any]]] = {symbol: None for symbol in symbols}
    if volume_price_col is None or not all_variants:
        return out

    docs: List[Dict[str, Any]] = []
    recent_td_key = _trade_date_match_recent(today)
    projection = {
        "symbol": 1,
        "ts_code": 1,
        "trade_date": 1,
        "close": 1,
        "pre_close": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "change": 1,
        "pct_chg": 1,
        "vol": 1,
        "volume": 1,
        "update_time": 1,
        "synced_at": 1,
        "source": 1,
    }
    try:
        docs.extend(
            volume_price_col.find(
                {
                    "$or": [
                        {"symbol": {"$in": all_variants}, "trade_date": recent_td_key},
                        {"ts_code": {"$in": all_variants}, "trade_date": recent_td_key},
                    ]
                },
                projection,
            )
        )
    except Exception:
        return out

    by_norm = _vp_docs_index_by_norm(docs)
    for symbol in symbols:
        cands = by_norm.get(remove_exchange_suffix(symbol), [])
        out[symbol] = _pick_latest_vp_doc(cands)
    return out


def _batch_latest_etf_daily_close_map(
    etf_daily_col,
    symbols: List[str],
    sym_ctx: Dict[str, Dict[str, Any]],
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """批量读取 ETF 最新收盘价，补足 volume_price 无昨收的情况。

    用近期 ``trade_date`` 约束一次 ``find``，避免把该 ETF 的全部历史
    ``etf_daily`` 拉进内存，也不要对每只 ``find_one``。
    """
    out: Dict[str, Any] = {}
    ts_codes = list(
        dict.fromkeys(
            sym_ctx[symbol].get("etf_ts")
            for symbol in symbols
            if sym_ctx[symbol].get("etf_ts")
        )
    )
    packs = _batch_etf_daily_packs(etf_daily_col, ts_codes, today)
    for symbol in symbols:
        etf_ts = sym_ctx[symbol].get("etf_ts")
        pack = packs.get(str(etf_ts) if etf_ts else "")
        row = (pack or {}).get("row") if pack else None
        if row and row.get("close") is not None:
            out[symbol] = row.get("close")
    return out


def _batch_etf_daily_packs(
    etf_daily_col,
    ts_codes: List[str],
    today: Optional[str] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """一次取出自选 ETF 近期日线，在内存里挑最新一根 + 前一交易日。"""
    codes = [str(ts).strip() for ts in ts_codes if ts]
    out: Dict[str, Optional[Dict[str, Any]]] = {code: None for code in codes}
    if etf_daily_col is None or not codes:
        return out

    today_yyyymmdd = today or _cn_calendar_today_yyyymmdd()
    recent_td_key = _trade_date_match_recent(today_yyyymmdd)
    projection = {
        "ts_code": 1,
        "trade_date": 1,
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "pre_close": 1,
        "change": 1,
        "pct_chg": 1,
        "vol": 1,
        "volume": 1,
        "amount": 1,
    }
    try:
        docs = list(
            etf_daily_col.find(
                {"ts_code": {"$in": codes}, "trade_date": recent_td_key},
                projection,
            )
        )
    except Exception:
        return out

    by_ts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        ts_code = str(doc.get("ts_code") or "").strip()
        if ts_code:
            by_ts[ts_code].append(doc)

    for ts_code in codes:
        rows = by_ts.get(ts_code, [])
        rows.sort(
            key=lambda d: _norm_trade_date_to_yyyymmdd(d.get("trade_date")) or "",
            reverse=True,
        )
        if not rows or rows[0].get("close") is None:
            continue
        out[ts_code] = {"row": rows[0], "prev": rows[1] if len(rows) > 1 else None}
    return out


def _etf_daily_intraday_pack(
    etf_daily_col, ts_code: str, today: str
) -> Optional[Dict[str, Any]]:
    """ETF：优先当日 Tushare 日线（盘后或已同步时才有），否则最新一根日线 + 前一交易日用于涨跌。"""
    packs = _batch_etf_daily_packs(etf_daily_col, [ts_code], today)
    return packs.get(str(ts_code).strip()) if ts_code else None


def _today_vp_from_latest(
    latest_vp_map: Dict[str, Optional[Dict[str, Any]]],
    today: str,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """最新一根若就是上海今日，即可当盘中 bar，不必再查一遍 ``volume_price``。"""
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for symbol, doc in latest_vp_map.items():
        if doc and _norm_trade_date_to_yyyymmdd(doc.get("trade_date")) == today:
            out[symbol] = doc
        else:
            out[symbol] = None
    return out


def _etf_ts_from_info_map(
    etf_info_map: Dict[str, Dict[str, Any]],
    symbol: str,
) -> Optional[str]:
    info = etf_info_map.get(remove_exchange_suffix(symbol))
    if not info:
        return None
    ts_code = str(info.get("ts_code") or "").strip()
    return ts_code or None


def _run_watchlist_lookups(jobs: Dict[str, Any]) -> Dict[str, Any]:
    """彼此独立的 Mongo 读并行发出，总耗时接近最慢的那一次。"""
    if not jobs:
        return {}
    if len(jobs) == 1:
        name, fn = next(iter(jobs.items()))
        return {name: fn()}
    out: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {pool.submit(fn): name for name, fn in jobs.items()}
        for fut, name in futs.items():
            out[name] = fut.result()
    return out


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
    """同一标的当日多条：优先 miniQMT，其次 Tushare，东财快照仅作兜底。"""
    if not cands:
        return None
    return min(cands, key=_vp_source_rank)


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
    """查询当日 ``volume_price``，按自选 ``symbol`` 映射最佳文档（miniQMT / Tushare 优先）。"""
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
    return out


# ==================== 实时数据 ====================

@router.get("/watchlist-stocks/realtime")
def get_user_watchlist_realtime(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的实时数据。

    股票：读 ``volume_price``。当日有盘中 bar 时优先 ``source=miniqmt``（其次旧
    ``tushare_rt_k``、日终 ``tushare_daily``）；``eastmoney_snapshot`` 只在没有上述
    来源时才用。日历「今日」没有当日 bar（周末 / 长假 / 盘前）则回退最近一根完整日线
    （通常是 Tushare ``run_stock_daily``），不要只吐收盘价。
    ``trade_date`` 与「今天」按 **Asia/Shanghai** 对齐，并兼容多种存法。

    ETF：在快照不可用时回退到 **``etf_daily``**（优先当日已同步的 Tushare 日线，否则最新一根日线，
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

        etf_basic_col = data_collections.get("etf_basic")
        etf_daily_col = data_collections.get("etf_daily")
        volume_price_col = data_collections["volume_price"]
        stock_info_col = data_collections["stock_info"]
        today = _cn_calendar_today_yyyymmdd()
        # ETF 变体覆盖 SH/SZ/BJ，股票多几个后缀对 $in 无害，可与 etf_basic 并行。
        sym_ctx: Dict[str, Dict[str, Any]] = {
            symbol: {"variants": _etf_symbol_variants(symbol), "etf_ts": None}
            for symbol in symbols
        }
        etf_variants = _unique_etf_variants_for_symbols(symbols)

        lookups = _run_watchlist_lookups({
            "etf_info": lambda: _batch_etf_info_map(etf_basic_col, symbols),
            "sw": lambda: _load_sw_industry_map(
                data_collections.get("sw_index_member"),
                symbols,
            ),
            "stock_info": lambda: _batch_stock_info_map(stock_info_col, etf_variants),
            "latest_vp": lambda: _batch_latest_volume_price_map(
                volume_price_col, symbols, sym_ctx, today
            ),
            "etf_packs": lambda: _batch_etf_daily_packs(
                etf_daily_col, etf_variants, today
            ),
        })
        etf_info_map = lookups["etf_info"]
        sw_industry_map = lookups["sw"]
        stock_info_map = lookups["stock_info"]
        latest_vp_map = lookups["latest_vp"]
        etf_pack_by_ts = lookups["etf_packs"]

        for symbol in symbols:
            etf_ts = _etf_ts_from_info_map(etf_info_map, symbol)
            if etf_ts:
                sym_ctx[symbol]["etf_ts"] = etf_ts
                if etf_ts not in sym_ctx[symbol]["variants"]:
                    sym_ctx[symbol]["variants"].append(etf_ts)

        yesterday_close_map = {}
        etf_close_map = {}
        for symbol in symbols:
            pack = etf_pack_by_ts.get(str(sym_ctx[symbol].get("etf_ts") or ""))
            row = (pack or {}).get("row") if pack else None
            if row and row.get("close") is not None:
                etf_close_map[symbol] = row.get("close")
        for symbol, latest_record in latest_vp_map.items():
            if latest_record and latest_record.get("close") is not None:
                yesterday_close_map[symbol] = latest_record.get("close")
            elif symbol in etf_close_map:
                yesterday_close_map[symbol] = etf_close_map[symbol]

        vp_batch = _today_vp_from_latest(latest_vp_map, today)

        result = []
        for symbol in symbols:
            etf_ts = sym_ctx[symbol]["etf_ts"]
            industry = ""
            sw_industry = sw_industry_map.get(remove_exchange_suffix(symbol))
            norm = remove_exchange_suffix(symbol)
            stock_info = stock_info_map.get(norm)
            if stock_info:
                name = stock_info["name"] if "name" in stock_info else symbol
                industry = stock_info.get("industry", "")
                asset_type = "stock"
            else:
                etf_info = etf_info_map.get(norm)
                if etf_info:
                    name = etf_info["name"] if "name" in etf_info else symbol
                    asset_type = "etf"
                else:
                    name = symbol
                    asset_type = "unknown"

            vp = vp_batch.get(symbol)
            if vp and vp.get("close") is not None:
                result.append(
                    _realtime_row_from_bar(
                        symbol=symbol,
                        name=name,
                        industry=industry,
                        sw_industry=sw_industry,
                        asset_type=asset_type,
                        bar=vp,
                    )
                )
                continue

            pack = etf_pack_by_ts.get(str(etf_ts or "")) if etf_ts else None
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
                    bar = dict(row)
                    bar["close"] = close
                    bar["pre_close"] = prev_c
                    bar["change"] = ch
                    bar["pct_chg"] = pct
                    result.append(
                        _realtime_row_from_bar(
                            symbol=symbol,
                            name=name,
                            industry=industry,
                            sw_industry=sw_industry,
                            asset_type=asset_type,
                            bar=bar,
                        )
                    )
                    continue

            latest_record = latest_vp_map.get(symbol)
            if latest_record and latest_record.get("close") is not None:
                result.append(
                    _realtime_row_from_bar(
                        symbol=symbol,
                        name=name,
                        industry=industry,
                        sw_industry=sw_industry,
                        asset_type=asset_type,
                        bar=latest_record,
                    )
                )
                continue

            yesterday_close = yesterday_close_map.get(symbol)
            if yesterday_close:
                nd = _price_ndigits(asset_type)
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "industry": industry,
                    "sw_industry": sw_industry,
                    "asset_type": asset_type,
                    "price": _safe_round_price(yesterday_close, nd),
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
