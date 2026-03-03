"""
用户自选股管理相关路由
处理自选股列表、策略、实时数据等功能
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from models.all_models import (
    WatchlistRequest, WatchlistUpdateRequest, WatchlistStrategySetRequest, 
    WatchlistStrategyResponse
)
from auth import get_current_active_user
from db import get_db_collections
from datetime import datetime

router = APIRouter()


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
        user_id = current_user["id"]
        
        # 获取用户自选股列表
        collections = get_db_collections()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]
        result = []

        volume_price_col = collections["volume_price"]
        etf_basic_col = collections.get("etf_basic")
        etf_daily_col = collections.get("etf_daily")
        
        for symbol in symbols:
            # Check Stock first
            stock_info = collections["stock_info"].find_one({"symbol": symbol})
            if stock_info:
                name = stock_info["name"] if "name" in stock_info else symbol
                latest_record = volume_price_col.find_one(
                    {"symbol": symbol},
                    sort=[("trade_date", -1)]
                )
            else:
                # Check ETF
                etf_info = etf_basic_col.find_one({"ts_code": symbol}) if etf_basic_col is not None else None
                if etf_info:
                    name = etf_info["name"] if "name" in etf_info else symbol
                    latest_record = etf_daily_col.find_one(
                        {"ts_code": symbol},
                        sort=[("trade_date", -1)]
                    ) if etf_daily_col is not None else None
                else:
                    name = symbol
                    latest_record = None

            if latest_record:
                # etf_daily uses 'amount' instead of 'turnover' natively in tushare sometimes
                # but our ingest script maps it to amount. Let's handle both.
                stock_data = {
                    "symbol": symbol,
                    "name": name,
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
    return symbol.replace(".SH", "").replace(".SZ", "")


# ==================== 实时数据 ====================

@router.get("/watchlist-stocks/realtime")
def get_user_watchlist_realtime(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的实时数据（基于分钟K线）
    
    Returns:
        {
            "success": True,
            "data": [
                {
                    "symbol": "600990",
                    "name": "四创电子",
                    "price": 29.61,        # 最新价格（最新分钟K线的收盘价）
                    "open": 27.61,         # 今日开盘价
                    "high": 31.0,          # 今日最高价
                    "low": 27.45,          # 今日最低价
                    "volume": 278479.0,    # 今日累计成交量
                    "change": 0.50,        # 涨跌额
                    "change_pct": 1.72,    # 涨跌幅%
                    "update_time": "2025-12-16 14:30:00"  # 最后更新时间
                },
                ...
            ]
        }
    """
    try:
        user_id = current_user["id"]
        
        # 1. 获取用户自选股列表
        collections = get_db_collections()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]  # e.g., ["600990", "300722"]
        
        # 2. 获取昨日收盘价（用于计算涨跌幅）
        yesterday_close_map = {}  # symbol -> close_price
        volume_price_col = collections["volume_price"]
        etf_daily_col = collections.get("etf_daily")
        
        for symbol in symbols:
            latest_record = volume_price_col.find_one(
                {"symbol": symbol},
                sort=[("trade_date", -1)]
            )
            if latest_record:
                yesterday_close_map[symbol] = latest_record.get("close")
            elif etf_daily_col is not None:
                # Fallback to ETF daily
                etf_record = etf_daily_col.find_one(
                    {"ts_code": symbol},
                    sort=[("trade_date", -1)]
                )
                if etf_record:
                    yesterday_close_map[symbol] = etf_record.get("close")
        
        # 3. 准备查询条件（minute_bars 中 symbol 没有交易所后缀）
        # symbols 已经是不带后缀的格式，如 ["600990", "300722"]
        
        # 4. 聚合今日分钟K线数据
        minute_bars_col = collections["minute_bars"]
        today = datetime.now().strftime("%Y%m%d")  # e.g., "20251216"
        
        # minute_bars 的 trade_date 格式是 YYYYMMDDHHmm，需要按日期前缀匹配
        pipeline = [
            {
                "$match": {
                    "symbol": {"$in": symbols},  # 不需要后缀
                    "trade_date": {"$regex": f"^{today}"}  # 匹配今天的所有分钟数据
                }
            },
            {"$sort": {"trade_date": 1}},  # 按时间排序
            {
                "$group": {
                    "_id": "$symbol",
                    "open": {"$first": "$open"},      # 第一根K线的开盘价
                    "close": {"$last": "$close"},     # 最新K线的收盘价
                    "high": {"$max": "$high"},        # 最高价
                    "low": {"$min": "$low"},          # 最低价
                    "volume": {"$sum": "$volume"},    # 累计成交量
                    "latest_time": {"$last": "$trade_date"}  # 最后更新时间
                }
            }
        ]
        
        realtime_data = list(minute_bars_col.aggregate(pipeline))
        
        # 5. 构建结果并计算涨跌幅
        result = []
        realtime_map = {item["_id"]: item for item in realtime_data}  # symbol -> data
        
        stock_info_col = collections["stock_info"]
        etf_basic_col = collections.get("etf_basic")
        
        for symbol in symbols:
            # 获取股票/ETF名称
            stock_info = stock_info_col.find_one({"symbol": symbol})
            if stock_info:
                name = stock_info["name"] if "name" in stock_info else symbol
            else:
                etf_info = etf_basic_col.find_one({"ts_code": symbol}) if etf_basic_col is not None else None
                if etf_info:
                    name = etf_info["name"] if "name" in etf_info else symbol
                else:
                    name = symbol
            
            if symbol in realtime_map:
                # 有今日实时数据
                item = realtime_map[symbol]
                current_price = item["close"]
                yesterday_close = yesterday_close_map.get(symbol, item["open"])  # 如果没有昨日数据，用今日开盘价
                
                # 计算涨跌额和涨跌幅
                change = current_price - yesterday_close if yesterday_close else 0
                change_pct = (change / yesterday_close * 100) if yesterday_close else 0
                
                # 格式化更新时间（YYYYMMDDHHmm -> YYYY-MM-DD HH:MM:SS）
                latest_time_str = item["latest_time"]
                if latest_time_str and len(latest_time_str) >= 12:
                    formatted_time = f"{latest_time_str[0:4]}-{latest_time_str[4:6]}-{latest_time_str[6:8]} {latest_time_str[8:10]}:{latest_time_str[10:12]}:00"
                else:
                    formatted_time = str(latest_time_str)
                
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "price": round(current_price, 2),
                    "open": round(item["open"], 2),
                    "high": round(item["high"], 2),
                    "low": round(item["low"], 2),
                    "volume": round(item["volume"], 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "update_time": formatted_time
                })
            else:
                # 无今日实时数据，返回昨日数据或空值
                yesterday_close = yesterday_close_map.get(symbol)
                
                if yesterday_close:
                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": round(yesterday_close, 2),
                        "open": None,
                        "high": None,
                        "low": None,
                        "volume": None,
                        "change": 0.0,
                        "change_pct": 0.0,
                        "update_time": None
                    })
                else:
                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": None,
                        "open": None,
                        "high": None,
                        "low": None,
                        "volume": None,
                        "change": None,
                        "change_pct": None,
                        "update_time": None
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
