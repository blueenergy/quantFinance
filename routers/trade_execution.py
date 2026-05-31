from fastapi import APIRouter, Query, HTTPException, Depends
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from db import get_db_collections, get_data_db
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol, strip_exchange_suffix
from utils.time_display import add_display_time_fields
from auth import get_current_active_user
import time

router = APIRouter()


@router.get("/minute-bars/", summary="获取股票分钟K线数据")
def get_minute_bars(
    symbol: str = Query(..., description="股票代码"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYYMMDD或YYYYMMDDHHMM)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYYMMDD或YYYYMMDDHHMM)"),
    limit: int = Query(500, description="返回记录数量限制", ge=1, le=2000),
    current_user: dict = Depends(get_current_active_user),
):
    """获取股票分钟K线数据，用于绘制分钟图
    
    Args:
        symbol: 股票代码（如 600391 或 600391.SH）
        start_date: 开始日期，支持 YYYYMMDD 或 YYYYMMDDHHMM 格式
        end_date: 结束日期，支持 YYYYMMDD 或 YYYYMMDDHHMM 格式
        limit: 返回记录数量
    
    Returns:
        {
            "success": True,
            "data": [
                {
                    "trade_date": "202412201030",
                    "open": 26.35,
                    "high": 26.50,
                    "low": 26.30,
                    "close": 26.45,
                    "volume": 12500
                },
                ...
            ],
            "count": 240
        }
    """
    try:
        data_collections = get_data_db()
        minute_bars_coll = data_collections["minute_bars"]
        
        # 规范化symbol（处理带后缀和不带后缀的情况）
        symbol_normalized = normalize_symbol(symbol)
        symbol_stripped = strip_exchange_suffix(symbol)
        
        # 构建查询条件
        query: Dict[str, Any] = {
            "$or": [
                {"symbol": symbol_normalized},
                {"symbol": symbol_stripped}
            ]
        }
        
        # 添加日期范围筛选
        if start_date or end_date:
            date_query: Dict[str, Any] = {}
            if start_date:
                # 支持 YYYYMMDD 或 YYYYMMDDHHMM 格式
                if len(start_date) == 8:  # YYYYMMDD
                    date_query["$gte"] = start_date + "0000"  # 转为当天00:00
                else:
                    date_query["$gte"] = start_date
            
            if end_date:
                if len(end_date) == 8:  # YYYYMMDD
                    date_query["$lte"] = end_date + "2359"  # 转为当天23:59
                else:
                    date_query["$lte"] = end_date
            
            if date_query:
                query["trade_date"] = date_query
        
        # 查询并排序（时间升序，方便前端绘制）
        cursor = minute_bars_coll.find(query).sort("trade_date", 1).limit(limit)
        bars = [serialize_doc(doc) for doc in cursor]
        
        return {
            "success": True,
            "data": bars,
            "count": len(bars)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询分钟K线失败: {str(e)}")


@router.get("/trade-signals/", summary="获取交易信号记录")
def get_trade_signals(
    symbol: Optional[str] = Query(None, description="股票代码"),
    strategy: Optional[str] = Query(None, description="策略名称"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    limit: int = Query(200, description="返回记录数量限制", ge=1, le=500),
    current_user: dict = Depends(get_current_active_user),
):
    """获取交易信号记录，用于在K线图上标记买卖点
    
    Args:
        symbol: 股票代码（可选，不传则返回所有）
        strategy: 策略名称（可选）
        start_date: 开始日期
        end_date: 结束日期
        limit: 返回记录数量
    
    Returns:
        {
            "success": True,
            "data": [
                {
                    "symbol": "600391",
                    "strategy": "GridTradingStrategy",
                    "action": "buy",
                    "price": 26.35,
                    "timestamp": 1703039400.0,
                    "order_id": "strategy_xxx",
                    "status": "pending"
                },
                ...
            ],
            "count": 15
        }
    """
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 构建查询条件
        query: Dict[str, Any] = {"user_id": user_id}
        
        # 添加股票代码筛选
        if symbol:
            symbol_normalized = normalize_symbol(symbol)
            symbol_stripped = strip_exchange_suffix(symbol)
            query["$or"] = [
                {"symbol": symbol_normalized},
                {"symbol": symbol_stripped}
            ]
        
        # 添加策略筛选
        if strategy:
            query["strategy"] = strategy
        
        # 添加日期范围筛选
        if start_date or end_date:
            timestamp_query: Dict[str, Any] = {}
            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
                timestamp_query["$gte"] = start_dt.timestamp()
            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                timestamp_query["$lte"] = end_dt.timestamp()
            
            if timestamp_query:
                query["timestamp"] = timestamp_query
        
        # 查询并排序（时间升序）
        cursor = signals_coll.find(query).sort("timestamp", 1).limit(limit)
        signals = [serialize_doc(doc) for doc in cursor]
        
        return {
            "success": True,
            "data": signals,
            "count": len(signals)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询交易信号失败: {str(e)}")


def _merge_stock_info_symbol_keys(docs: List[Dict[str, Any]]) -> Dict[str, str]:
    """把 ``stock_info`` 文档的 ``symbol`` / 规范化形式都映射到 ``name``，便于与无后缀成交代码对齐。"""
    symbol_to_name: Dict[str, str] = {}
    for doc in docs:
        raw_name = doc.get("name")
        name = raw_name if isinstance(raw_name, str) else (str(raw_name) if raw_name is not None else "")
        sym = str(doc.get("symbol", "")).strip()
        if not sym:
            continue
        symbol_to_name[sym] = name
        symbol_to_name[normalize_symbol(sym)] = name
        bare = strip_exchange_suffix(sym)
        if bare:
            symbol_to_name[bare] = name
    return symbol_to_name


def _resolve_stock_name_from_map(symbol: Any, symbol_to_name: Dict[str, str]) -> str:
    if symbol is None:
        return ""
    s = str(symbol).strip()
    if not s:
        return ""
    for key in (s, normalize_symbol(s), strip_exchange_suffix(s)):
        val = symbol_to_name.get(key, "")
        if val:
            return val
    return ""


def enrich_trades_with_stock_names(trades: List[Dict], stock_info_collection) -> List[Dict]:
    """为交易记录添加股票名称（兼容 ``stock_info.symbol`` 带后缀与成交里仅 6 位代码）。"""
    if not trades:
        return trades

    variants: set[str] = set()
    for trade in trades:
        sym = trade.get("symbol")
        if not sym:
            continue
        s = str(sym).strip()
        for cand in (s, normalize_symbol(s), strip_exchange_suffix(s)):
            if cand:
                variants.add(cand)

    if not variants:
        for trade in trades:
            trade.setdefault("stock_name", "")
        return trades

    stock_info_docs = list(
        stock_info_collection.find(
            {"symbol": {"$in": list(variants)}},
            {"symbol": 1, "name": 1},
        )
    )
    symbol_to_name = _merge_stock_info_symbol_keys(stock_info_docs)

    for trade in trades:
        trade["stock_name"] = _resolve_stock_name_from_map(trade.get("symbol"), symbol_to_name)

    return trades


def _compute_realized_pnl_for_group(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """基于一组同一股票+策略的执行记录，按时间顺序计算持仓与已实现盈亏.

    使用简单的加权平均成本法:
      - BUY: 更新持仓和平均成本
      - SELL: 以当前平均成本计算本次卖出的已实现盈亏

    返回字典包含:
      - net_quantity: 最终净持仓数量
      - realized_pnl: 已实现盈亏
      - avg_cost: 当前持仓的平均成本价(如果 net_quantity 为 0 则为 0)

    注意: 这里只计算已实现盈亏和平均成本，不直接计算未平仓浮动盈亏。
    """
    if not trades:
        return {"net_quantity": 0, "realized_pnl": 0.0, "avg_cost": 0.0}

    # 按时间顺序排序
    sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

    position_qty = 0
    avg_cost = 0.0
    realized_pnl = 0.0

    for t in sorted_trades:
        side_raw = (t.get("side") or t.get("action", "")).upper()
        if side_raw not in ("BUY", "SELL"):
            continue

        qty = (
            t.get("filled_size")
            or t.get("quantity")
            or t.get("size")
            or 0
        )
        price = (
            t.get("filled_price")
            or t.get("price")
            or t.get("target_price")
            or 0.0
        )
        commission = t.get("commission", 0.0) or 0.0

        if not qty or not price:
            continue

        if side_raw == "BUY":
            # 买入: 更新平均成本和持仓, 手续费计入成本
            total_cost = avg_cost * position_qty + price * qty + commission
            position_qty += qty
            if position_qty > 0:
                avg_cost = total_cost / position_qty
        else:  # SELL
            if position_qty <= 0:
                # 当前系统视角下没有可用持仓: 不计算已实现盈亏，也不引入负持仓
                sell_qty = 0
            else:
                sell_qty = min(qty, position_qty)
                # 卖出: 以当前平均成本计算已实现盈亏, 并扣除本次卖出的手续费
                realized_pnl += (price - avg_cost) * sell_qty - commission
                position_qty -= sell_qty

            if position_qty <= 0:
                position_qty = 0
                avg_cost = 0.0

    return {
        "net_quantity": position_qty,
        "realized_pnl": round(realized_pnl, 2),
        "avg_cost": round(avg_cost, 4) if position_qty > 0 else 0.0,
    }


def _group_executions_by_order(executions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 order_id 聚合执行记录，避免同一订单被重复统计。

    当前实现: 对于每个 order_id，保留 timestamp 最大的一条记录作为该订单的代表快照。
    这样可以在券商重复回放相同执行回报时，防止在统计和视图中重复计算。
    """
    if not executions:
        return []

    grouped: Dict[str, Dict[str, Any]] = {}
    for doc in executions:
        order_id = doc.get("order_id") or str(doc.get("_id"))
        current = grouped.get(order_id)
        if (not current) or (doc.get("timestamp", 0) > current.get("timestamp", 0)):
            grouped[order_id] = doc

    return list(grouped.values())


@router.get("/trade-executions/", summary="获取当前用户的交易执行记录")
def get_all_trade_executions(
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天"),
    plan_id: Optional[str] = Query(None, description="只返回指定 portfolio plan 发布的实盘成交"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前登录用户的交易执行记录，按时间倒序排列，支持日期范围筛选"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        data_collections = get_data_db()
        stock_info_collection = data_collections["stock_info"]
        
        # 构建查询条件（按用户 + 日期）
        user_id = current_user["id"]
        query: Dict[str, Any] = {"user_id": user_id}
        if plan_id:
            signals = list(
                collections["trade_signals"].find(
                    {"user_id": user_id, "plan_id": plan_id, "mode": "live"},
                    {"_id": 0, "order_id": 1},
                )
            )
            order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
            if not order_ids:
                return {
                    "success": True,
                    "data": [],
                    "count": 0
                }
            query["order_id"] = {"$in": order_ids}
        
        # 处理日期筛选
        if days:
            # 最近N天（包含今天）
            from datetime import datetime, timedelta
            end = datetime.now().replace(hour=23, minute=59, second=59)
            start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
            # 尝试使用 date 字段，如果不存在则使用 timestamp
            query["$or"] = [
                {
                    "date": {
                        "$gte": start.strftime("%Y-%m-%d"),
                        "$lte": end.strftime("%Y-%m-%d")
                    }
                },
                {
                    "timestamp": {
                        "$gte": start.timestamp(),
                        "$lte": end.timestamp()
                    }
                }
            ]
        elif start_date or end_date:
            # 日期范围
            from datetime import datetime
            or_conditions = []
            
            # date 字段条件
            date_query: Dict[str, Any] = {}
            if start_date:
                date_query["$gte"] = start_date
            if end_date:
                date_query["$lte"] = end_date
            if date_query:
                or_conditions.append({"date": date_query})
            
            # timestamp 字段条件
            timestamp_query: Dict[str, Any] = {}
            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
                timestamp_query["$gte"] = start_dt.timestamp()
            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                timestamp_query["$lte"] = end_dt.timestamp()
            if timestamp_query:
                or_conditions.append({"timestamp": timestamp_query})
            
            if or_conditions:
                query["$or"] = or_conditions
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = [add_display_time_fields(serialize_doc(doc)) for doc in cursor]
        
        # 添加股票名称
        trades = enrich_trades_with_stock_names(trades, stock_info_collection)
        
        return {
            "success": True,
            "data": trades,
            "count": len(trades)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

@router.get("/trade-executions/symbol/{symbol}", summary="获取当前用户特定股票的交易执行记录")
def get_trade_executions_by_symbol(
    symbol: str,
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前用户特定股票的交易执行记录，按时间倒序排列"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        data_collections = get_data_db()
        stock_info_collection = data_collections["stock_info"]
        
        user_id = current_user["id"]
        
        # 构建查询条件，支持多种 symbol 格式
        symbol_normalized = normalize_symbol(symbol)
        symbol_stripped = strip_exchange_suffix(symbol)
        
        query = {
            "user_id": user_id,
            "$or": [
                {"symbol": symbol},
                {"symbol": symbol_normalized},
                {"symbol": symbol_stripped}
            ]
        }
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = _group_executions_by_order([add_display_time_fields(serialize_doc(doc)) for doc in cursor])
        # 过滤掉未成交的订单（filled_size 为 0 的废单）
        trades = [t for t in trades if t.get("filled_size", 0) != 0]
        
        # 添加股票名称
        trades = enrich_trades_with_stock_names(trades, stock_info_collection)
        
        return {
            "success": True,
            "data": trades,
            "count": len(trades),
            "symbol": symbol
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

@router.get("/trade-executions/date/{date}", summary="获取当前用户特定日期的交易执行记录")
def get_trade_executions_by_date(
    date: str,
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前用户特定日期的交易执行记录，按时间倒序排列"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        data_collections = get_data_db()
        stock_info_collection = data_collections["stock_info"]
        
        user_id = current_user["id"]
        query = {"date": date, "user_id": user_id}
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = [add_display_time_fields(serialize_doc(doc)) for doc in cursor]
        
        # 添加股票名称
        trades = enrich_trades_with_stock_names(trades, stock_info_collection)
        
        return {
            "success": True,
            "data": trades,
            "count": len(trades),
            "date": date
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

@router.get("/trade-executions/stats", summary="获取当前用户的交易统计数据")
def get_trade_execution_stats(current_user: dict = Depends(get_current_active_user)):
    """获取当前登录用户的交易统计数据"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        
        user_id = current_user["id"]
        trades = _group_executions_by_order(list(collection.find({"user_id": user_id})))
        # 过滤掉完全未成交的订单（filled_size 为 0），避免统计中计入作废单
        trades = [t for t in trades if t.get("filled_size", 0) != 0]
        
        if not trades:
            return {
                "success": True,
                "stats": {
                    "total_trades": 0,
                    "buy_trades": 0,
                    "sell_trades": 0,
                    "total_symbols": 0,
                    "symbols": []
                }
            }
        
        # 统计数据
        total_trades = len(trades)
        buy_trades = sum(1 for t in trades if t.get("side", t.get("action", "")).upper() == "BUY")
        sell_trades = sum(1 for t in trades if t.get("side", t.get("action", "")).upper() == "SELL")
        
        # 按股票分组统计
        symbols: Dict[str, Any] = {}
        for trade in trades:
            symbol = trade.get("symbol", "UNKNOWN")
            if symbol not in symbols:
                symbols[symbol] = {
                    "buy_count": 0,
                    "sell_count": 0,
                    "total_quantity": 0,
                    "strategies": set()
                }
            
            side = (trade.get("side") or trade.get("action", "")).upper()
            if side == "BUY":
                symbols[symbol]["buy_count"] += 1
            elif side == "SELL":
                symbols[symbol]["sell_count"] += 1
                
            symbols[symbol]["total_quantity"] += trade.get("filled_size", trade.get("quantity", trade.get("size", 0)))
            symbols[symbol]["strategies"].add(trade.get("strategy_name", trade.get("strategy", "UNKNOWN")))
        
        # 转换策略集合为列表
        for symbol_data in symbols.values():
            symbol_data["strategies"] = list(symbol_data["strategies"])
        
        return {
            "success": True,
            "stats": {
                "total_trades": total_trades,
                "buy_trades": buy_trades,
                "sell_trades": sell_trades,
                "total_symbols": len(symbols),
                "symbols": symbols
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")


@router.get("/trade-executions/pnl-summary", summary="获取当前用户按股票和策略分组的盈亏统计")
def get_trade_pnl_summary(
    days: Optional[int] = Query(None, description="最近N天"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    current_user: dict = Depends(get_current_active_user),
):
    """按股票和策略分组汇总当前用户的已实现盈亏和净持仓.

    主要用于前端展示：
      - 每个股票 + 策略的交易笔数、净持仓数量、已实现盈亏
      - 支持按日期范围或最近N天筛选，便于回溯查看
    """
    try:
        collections = get_db_collections()
        executions_coll = collections["trade_executions"]
        data_collections = get_data_db()
        stock_info_coll = data_collections["stock_info"]
        minute_bars_coll = data_collections["minute_bars"]
        user_id = current_user["id"]

        # FastAPI 在实际请求中会注入已解析的值，但直接调用函数时参数可能仍然是 Query 对象
        # 为了兼容单元测试和直接调用，这里将非预期类型归一化为 None
        if not isinstance(days, (int, type(None))):
            days = None
        if not isinstance(start_date, (str, type(None))):
            start_date = None
        if not isinstance(end_date, (str, type(None))):
            end_date = None

        # 基于 get_all_trade_executions 的日期筛选逻辑构建查询
        query: Dict[str, Any] = {"user_id": user_id}

        if days:
            end = datetime.now().replace(hour=23, minute=59, second=59)
            start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
            query["$or"] = [
                {
                    "date": {
                        "$gte": start.strftime("%Y-%m-%d"),
                        "$lte": end.strftime("%Y-%m-%d"),
                    }
                },
                {
                    "timestamp": {
                        "$gte": start.timestamp(),
                        "$lte": end.timestamp(),
                    }
                },
            ]
        elif start_date or end_date:
            or_conditions = []

            date_query: Dict[str, Any] = {}
            if start_date:
                date_query["$gte"] = start_date
            if end_date:
                date_query["$lte"] = end_date
            if date_query:
                or_conditions.append({"date": date_query})

            timestamp_query: Dict[str, Any] = {}
            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
                timestamp_query["$gte"] = start_dt.timestamp()
            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                timestamp_query["$lte"] = end_dt.timestamp()
            if timestamp_query:
                or_conditions.append({"timestamp": timestamp_query})

            if or_conditions:
                query["$or"] = or_conditions

        # 查询当前用户的所有执行记录
        trades = _group_executions_by_order(list(executions_coll.find(query)))
        # 过滤掉完全未成交的订单（filled_size 为 0），避免统计中计入作废单
        trades = [t for t in trades if t.get("filled_size", 0) != 0]
        if not trades:
            return {"success": True, "data": [], "total_realized_pnl": 0.0, "total_unrealized_pnl": 0.0}

        # 分组: (symbol, strategy) -> trades
        groups: Dict[tuple, Dict[str, Any]] = {}
        for t in trades:
            symbol = t.get("symbol", "")
            strategy = t.get("strategy_name", t.get("strategy", "")) or "UNKNOWN"
            key = (symbol, strategy)
            if key not in groups:
                groups[key] = {"trades": [], "trade_count": 0}
            groups[key]["trades"].append(t)
            groups[key]["trade_count"] += 1

        # 计算每个分组的净持仓和已实现盈亏
        summary_rows = []
        # 收集所有需要查询的 symbol
        all_symbols: set = set()
        for (symbol, _), info in groups.items():
            all_symbols.add(symbol)

        # 使用 minute_bars 获取每个 symbol 的最新收盘价（近实时价）
        latest_prices: Dict[str, float] = {}
        if all_symbols:
            today_prefix = datetime.now().strftime("%Y%m%d")
            pipeline = [
                {
                    "$match": {
                        "symbol": {"$in": list(all_symbols)},
                        "trade_date": {"$regex": f"^{today_prefix}"},
                    }
                },
                {"$sort": {"trade_date": 1}},
                {
                    "$group": {
                        "_id": "$symbol",
                        "close": {"$last": "$close"},
                    }
                },
            ]
            try:
                realtime_docs = list(minute_bars_coll.aggregate(pipeline))
                for doc in realtime_docs:
                    s = doc.get("_id")
                    close = doc.get("close")
                    if s and close is not None:
                        latest_prices[s] = float(close)
            except Exception:
                latest_prices = {}

        total_unrealized_pnl = 0.0
        for (symbol, strategy), info in groups.items():
            pnl_info = _compute_realized_pnl_for_group(info["trades"])
            net_qty = pnl_info["net_quantity"]
            realized_pnl = pnl_info["realized_pnl"]
            avg_cost = pnl_info.get("avg_cost", 0.0)

            current_price = latest_prices.get(symbol)
            unrealized_pnl = 0.0

            # 只有在有 current_price 且 net_qty != 0 时才计算浮盈
            if current_price is not None and net_qty:
                unrealized_pnl = (current_price - avg_cost) * net_qty

            total_unrealized_pnl += unrealized_pnl

            summary_rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "total_trades": info["trade_count"],
                    "net_quantity": net_qty,
                    "realized_pnl": realized_pnl,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                }
            )

        # 批量补全股票名称（与 enrich_trades_with_stock_names 同口径：兼容无后缀 symbol）
        symbols = list({row["symbol"] for row in summary_rows if row.get("symbol")})
        variants: set[str] = set()
        for sym in symbols:
            s = str(sym).strip()
            for cand in (s, normalize_symbol(s), strip_exchange_suffix(s)):
                if cand:
                    variants.add(cand)
        stock_docs = list(
            stock_info_coll.find({"symbol": {"$in": list(variants)}}, {"symbol": 1, "name": 1})
        )
        symbol_to_name = _merge_stock_info_symbol_keys(stock_docs)
        for row in summary_rows:
            row["stock_name"] = _resolve_stock_name_from_map(row.get("symbol"), symbol_to_name)

        total_realized_pnl = round(sum(r["realized_pnl"] for r in summary_rows), 2)
        total_unrealized_pnl = round(total_unrealized_pnl, 2)

        return {
            "success": True,
            "data": summary_rows,
            "total_realized_pnl": total_realized_pnl,
            "total_unrealized_pnl": total_unrealized_pnl,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")

@router.get("/trade-executions/strategies", summary="获取当前用户出现过的策略名称")
def get_trade_strategies(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户出现过的策略名称"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        user_id = current_user["id"]
        
        # 获取所有不同的策略名称（兼容 strategy_name / strategy 字段）
        strategies_name = list(collection.distinct("strategy_name", {"user_id": user_id}))
        strategies_key = list(collection.distinct("strategy", {"user_id": user_id}))
        strategies = set([s for s in strategies_name if s] + [s for s in strategies_key if s])
        
        return {
            "success": True,
            "strategies": list(strategies)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

@router.get("/trade-activities/stats", include_in_schema=False)
def get_trade_activities_stats(current_user: dict = Depends(get_current_active_user)):
    """[Deprecated] 该接口已废弃，请使用 /trade-executions/stats 或前端本地统计.

    保留占位以避免旧客户端报 404，但不再返回实际统计数据。
    """
    raise HTTPException(status_code=410, detail="/trade-activities/stats has been removed; use /trade-executions/stats instead")

@router.get("/trade-activities/", summary="获取当前用户的所有交易活动（包含信号和执行记录）")
def get_all_trade_activities(
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天"),
    status_filter: Optional[str] = Query(None, description="状态筛选: executed, pending, all"),
    current_user: dict = Depends(get_current_active_user),
):
    """融合 trade_signals 和 trade_executions，提供当前用户的完整交易活动视图"""
    try:
        collections = get_db_collections()
        executions_coll = collections["trade_executions"]
        signals_coll = collections["trade_signals"]
        data_collections = get_data_db()
        stock_info_coll = data_collections["stock_info"]
        user_id = current_user["id"]
        
        # 构建日期查询条件
        date_query = {}
        if days:
            from datetime import datetime, timedelta
            end = datetime.now().replace(hour=23, minute=59, second=59)
            start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
            date_query = {
                "$or": [
                    {
                        "date": {
                            "$gte": start.strftime("%Y-%m-%d"),
                            "$lte": end.strftime("%Y-%m-%d")
                        }
                    },
                    {
                        "timestamp": {
                            "$gte": start.timestamp(),
                            "$lte": end.timestamp()
                        }
                    }
                ]
            }
        elif start_date or end_date:
            from datetime import datetime
            or_conditions = []
            date_filter = {}
            timestamp_filter = {}
            
            if start_date:
                date_filter["$gte"] = start_date
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                timestamp_filter["$gte"] = start_dt.timestamp()
            if end_date:
                date_filter["$lte"] = end_date
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                timestamp_filter["$lte"] = end_dt.timestamp()
            
            if date_filter:
                or_conditions.append({"date": date_filter})
            if timestamp_filter:
                or_conditions.append({"timestamp": timestamp_filter})
            if or_conditions:
                date_query = {"$or": or_conditions}
        
        merged_activities = []
        
        # 1. 获取已执行的交易记录（如果不是仅显示 pending）
        if status_filter != "pending":
            exec_query = {"worker_mode": {"$ne": "backtest"}, "user_id": user_id}  # 排除回测数据，按用户过滤
            if date_query:
                exec_query.update(date_query)
            
            executions = _group_executions_by_order(list(executions_coll.find(exec_query).sort("timestamp", -1)))
            # 排除完全未成交的订单，避免在用户视图中显示作废单
            executions = [e for e in executions if e.get("filled_size", 0) != 0]
            
            for exec_doc in executions:
                exec_time = add_display_time_fields(dict(exec_doc))
                raw_status = str(exec_doc.get("status", "filled")).lower()
                if raw_status in ("filled",):
                    status = "FILLED"
                    status_category = "executed"
                elif raw_status in ("partial_filled",):
                    status = "PARTIAL_FILLED"
                    status_category = "executed"
                elif raw_status in ("rejected", "failed"):
                    status = "FAILED"
                    status_category = "failed"
                else:
                    status = raw_status.upper()
                    status_category = "executed"
                merged_activities.append({
                    "_id": str(exec_doc.get("_id")),
                    "symbol": exec_doc.get("symbol"),
                    "direction": (exec_doc.get("side") or exec_doc.get("action", "")).upper(),
                    "status": status,
                    "status_category": status_category,
                    "price": exec_doc.get("filled_price", exec_doc.get("price", 0)),
                    "target_price": exec_doc.get("target_price"),
                    "filled_price": exec_doc.get("filled_price"),
                    "quantity": exec_doc.get("filled_size", exec_doc.get("size", exec_doc.get("quantity", 0))),
                    "strategy": exec_doc.get("strategy_name", exec_doc.get("strategy", "")),
                    "timestamp": exec_doc.get("timestamp", 0),
                    "execution_time": exec_time.get("execution_time"),
                    "timestamp_iso": exec_time.get("timestamp_iso"),
                    "order_id": exec_doc.get("order_id", ""),
                    "source": "trade_execution",
                    "date": exec_doc.get("date", "")
                })
        
        # 2. 获取待执行的交易信号（如果不是仅显示 executed）
        if status_filter != "executed":
            signal_query = {
                "$or": [
                    {"worker_mode": "live"},
                    {"mode": "live"}
                ],
                "user_id": user_id,
            }
            if date_query:
                signal_query.update(date_query)
            
            pending_signal_statuses = {"pending", "retry_pending", "submitted", ""}
            signals = list(signals_coll.find(signal_query).sort("timestamp", -1))
            
            for signal_doc in signals:
                signal_time = add_display_time_fields(dict(signal_doc))
                raw_status = str(signal_doc.get("status", "pending")).lower()
                if status_filter == "pending" and raw_status not in pending_signal_statuses:
                    continue
                status_map = {
                    "pending": "PENDING",
                    "retry_pending": "RETRY",
                    "submitted": "SUBMITTED",
                    "executed": "FILLED",
                    "filled": "FILLED",
                    "partial_filled": "PARTIAL_FILLED",
                    "failed": "FAILED",
                    "rejected": "FAILED",
                }
                # 粗分为待执行 vs 已执行/失败，便于前端理解
                if raw_status in ("pending", "retry_pending", "submitted"):
                    status_category = "pending"
                elif raw_status in ("executed", "filled", "partial_filled"):
                    status_category = "executed"
                elif raw_status in ("failed", "rejected"):
                    status_category = "failed"
                else:
                    status_category = "other"
                
                merged_activities.append({
                    "_id": str(signal_doc.get("_id")),
                    "symbol": signal_doc.get("symbol"),
                    "direction": (signal_doc.get("action", "")).upper(),
                    "status": status_map.get(raw_status, raw_status.upper()),
                    "status_category": status_category,
                    "price": signal_doc.get("price", 0),
                    "target_price": signal_doc.get("price"),
                    "filled_price": None,
                    "quantity": signal_doc.get("size", 0),
                    "strategy": signal_doc.get("strategy", ""),
                    "timestamp": signal_doc.get("timestamp", 0),
                    "execution_time": signal_time.get("execution_time"),
                    "timestamp_iso": signal_time.get("timestamp_iso"),
                    "order_id": signal_doc.get("order_id", ""),
                    "source": "trade_signal",
                    "error_message": signal_doc.get("error_message"),
                    "retry_count": signal_doc.get("retry_count", 0),
                    "date": signal_doc.get("date", "")
                })
        
        # 3. 按时间倒序排序
        merged_activities.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        
        # 4. 分页
        total = len(merged_activities)
        merged_activities = merged_activities[skip:skip + limit]
        
        # 5. 添加股票名称
        merged_activities = enrich_trades_with_stock_names(merged_activities, stock_info_coll)
        
        return {
            "success": True,
            "data": merged_activities,
            "count": len(merged_activities),
            "total": total
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/trader/signals", summary="Trader: 获取当前用户的待执行交易信号（供本地 Trader 使用）")
def get_trader_pending_signals(
    limit: int = Query(50, ge=1, le=500, description="返回记录数量限制"),
    include_submitted: bool = Query(False, description="是否包含已提交但未完成的订单"),
    current_user: dict = Depends(get_current_active_user),
):
    """供本地 Trader 使用的信号获取接口.

    - 只返回当前用户的实盘可执行信号
    - 默认只包含 `pending` 和 `retry_pending` 状态
    - 可选包含 `submitted` 状态，便于断线重连后恢复
    """
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]

        user_id = current_user["id"]
        query: Dict[str, Any] = {
            "user_id": user_id,
            "is_executable": True,
            "mode": "live",
        }

        status_list = ["pending", "retry_pending"]
        if include_submitted:
            status_list.append("submitted")
        query["status"] = {"$in": status_list}

        cursor = (
            signals_coll.find(query)
            .sort("timestamp", 1)
            .limit(limit)
        )
        signals = [serialize_doc(doc) for doc in cursor]

        return {
            "success": True,
            "data": signals,
            "count": len(signals),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/trader/signals/{order_id}/status", summary="Trader: 更新交易信号状态（供本地 Trader 使用）")
def update_trader_signal_status(
    order_id: str,
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user),
):
    """供本地 Trader 使用的信号状态更新接口.

    典型用法：
    - 下单成功后将信号标记为 `submitted`，并写入 `qmt_order_id`
    - 下单失败或需要重试时，将信号标记为 `retry_pending` 并累加 `retry_count`
    - 超过重试次数后标记为 `failed` 并写入错误信息
    """
    status = payload.get("status")
    if not status:
        raise HTTPException(status_code=400, detail="status 字段为必填")

    allowed_fields = {
        "status",
        "qmt_order_id",
        "retry_count",
        "last_error",
        "error",
        "submitted_at",
        "executed_at",
        "filled_qty",
        "avg_price",
    }

    update_doc: Dict[str, Any] = {k: v for k, v in payload.items() if k in allowed_fields}
    update_doc["updated_at"] = time.time()

    collections = get_db_collections()
    signals_coll = collections["trade_signals"]
    user_id = current_user["id"]

    result = signals_coll.update_one(
        {"order_id": order_id, "user_id": user_id},
        {"$set": update_doc},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="对应订单不存在或不属于当前用户")

    return {
        "success": True,
        "matched": result.matched_count,
        "modified": result.modified_count,
    }


@router.post("/trader/executions", summary="Trader: 上报交易执行结果（供本地 Trader 使用）")
def create_trader_execution(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user),
):
    """供本地 Trader 使用的执行结果上报接口.

    Trader 侧可以直接将原来写入 `trade_executions` 的字典作为 payload 发送：

    - 服务器会自动补充/覆盖 `user_id` 字段
    - 如未提供时间字段，会自动填充 `timestamp`/`date`/`datetime`
    - 同时会根据执行结果更新对应 `trade_signals` 的状态
    """
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id 字段为必填")

    collections = get_db_collections()
    executions_coll = collections["trade_executions"]
    signals_coll = collections["trade_signals"]

    user_id = current_user["id"]

    # 构建执行记录
    execution_record: Dict[str, Any] = dict(payload)
    execution_record["user_id"] = user_id

    ts = execution_record.get("timestamp")
    if not isinstance(ts, (int, float)):
        ts = time.time()
        execution_record["timestamp"] = ts

    # 补充日期相关字段
    dt = datetime.fromtimestamp(ts)
    execution_record.setdefault("date", dt.strftime("%Y%m%d"))
    execution_record.setdefault("datetime", dt.strftime("%Y-%m-%d %H:%M:%S"))

    # 默认实盘模式
    execution_record.setdefault("mode", "live")
    signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
    if signal:
        for field in ("plan_id", "strategy_template_id", "source", "securities_account_id", "account_id", "broker"):
            if signal.get(field) is not None:
                execution_record.setdefault(field, signal.get(field))

    insert_result = executions_coll.insert_one(execution_record)

    # 同步更新对应信号状态（如果存在）
    signal_update: Dict[str, Any] = {}
    status = execution_record.get("status")
    if status:
        signal_update["status"] = status

    filled_size = execution_record.get("filled_size")
    if filled_size is not None:
        signal_update["filled_qty"] = filled_size

    filled_price = execution_record.get("filled_price")
    if filled_price is not None:
        signal_update["avg_price"] = filled_price

    if signal_update:
        signal_update["executed_at"] = time.time()
        signal_update["updated_at"] = time.time()
        signals_coll.update_one(
            {"order_id": order_id, "user_id": user_id},
            {"$set": signal_update},
        )

    return {
        "success": True,
        "inserted_id": str(insert_result.inserted_id),
    }
