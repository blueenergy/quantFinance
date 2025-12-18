from fastapi import APIRouter, Query, HTTPException, Depends
from typing import List, Optional, Dict, Any
from datetime import datetime
from db import get_db_collections
from utils.stock_utils import serialize_doc
from auth import get_current_active_user

router = APIRouter()

def enrich_trades_with_stock_names(trades: List[Dict], stock_info_collection) -> List[Dict]:
    """为交易记录添加股票名称"""
    if not trades:
        return trades
    
    # 收集所有唯一的股票代码
    symbols = list(set(trade.get("symbol") for trade in trades if trade.get("symbol")))
    
    # 批量查询股票信息
    stock_info_docs = list(stock_info_collection.find(
        {"symbol": {"$in": symbols}},
        {"symbol": 1, "name": 1}
    ))
    
    # 创建代码到名称的映射
    symbol_to_name = {doc["symbol"]: doc.get("name", "") for doc in stock_info_docs}
    
    # 为每条交易记录添加股票名称
    for trade in trades:
        symbol = trade.get("symbol")
        if symbol:
            trade["stock_name"] = symbol_to_name.get(symbol, "")
    
    return trades


def _compute_realized_pnl_for_group(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """基于一组同一股票+策略的执行记录，按时间顺序计算持仓与已实现盈亏.

    使用简单的加权平均成本法:
      - BUY: 更新持仓和平均成本
      - SELL: 以当前平均成本计算本次卖出的已实现盈亏

    注意: 这里只计算已实现盈亏，不考虑未平仓浮动盈亏。
    """
    if not trades:
        return {"net_quantity": 0, "realized_pnl": 0.0}

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
            t.get("quantity")
            or t.get("size")
            or t.get("filled_size")
            or 0
        )
        price = (
            t.get("filled_price")
            or t.get("price")
            or t.get("target_price")
            or 0.0
        )

        if not qty or not price:
            continue

        if side_raw == "BUY":
            # 买入: 更新平均成本和持仓
            total_cost = avg_cost * position_qty + price * qty
            position_qty += qty
            if position_qty > 0:
                avg_cost = total_cost / position_qty
        else:  # SELL
            sell_qty = min(qty, position_qty) if position_qty > 0 else qty
            if position_qty <= 0:
                # 没有持仓时卖出，简单视为从0成本卖出
                realized_pnl += price * sell_qty
            else:
                realized_pnl += (price - avg_cost) * sell_qty
            position_qty -= sell_qty
            if position_qty <= 0:
                position_qty = 0
                avg_cost = 0.0

    return {"net_quantity": position_qty, "realized_pnl": round(realized_pnl, 2)}

@router.get("/trade-executions/", summary="获取当前用户的交易执行记录")
def get_all_trade_executions(
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前登录用户的交易执行记录，按时间倒序排列，支持日期范围筛选"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        stock_info_collection = collections["stock_info"]
        
        # 构建查询条件（按用户 + 日期）
        user_id = current_user["id"]
        query: Dict[str, Any] = {"user_id": user_id}
        
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
        trades = [serialize_doc(doc) for doc in cursor]
        
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
        stock_info_collection = collections["stock_info"]
        
        user_id = current_user["id"]
        query = {"symbol": symbol, "user_id": user_id}
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = [serialize_doc(doc) for doc in cursor]
        
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
        stock_info_collection = collections["stock_info"]
        
        user_id = current_user["id"]
        query = {"date": date, "user_id": user_id}
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = [serialize_doc(doc) for doc in cursor]
        
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
        trades = list(collection.find({"user_id": user_id}))
        
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
                
            symbols[symbol]["total_quantity"] += trade.get("quantity", trade.get("size", 0))
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
        stock_info_coll = collections["stock_info"]
        user_id = current_user["id"]

        # 基于 get_all_trade_executions 的日期筛选逻辑构建查询
        query: Dict[str, Any] = {"user_id": user_id}

        if days:
            from datetime import datetime, timedelta
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
            from datetime import datetime

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
        trades = list(executions_coll.find(query))
        if not trades:
            return {"success": True, "data": [], "total_realized_pnl": 0.0}

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
        for (symbol, strategy), info in groups.items():
            pnl_info = _compute_realized_pnl_for_group(info["trades"])
            summary_rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "total_trades": info["trade_count"],
                    "net_quantity": pnl_info["net_quantity"],
                    "realized_pnl": pnl_info["realized_pnl"],
                }
            )

        # 批量补全股票名称
        symbols = list({row["symbol"] for row in summary_rows if row.get("symbol")})
        stock_docs = list(
            stock_info_coll.find({"symbol": {"$in": symbols}}, {"symbol": 1, "name": 1})
        )
        symbol_to_name = {doc["symbol"]: doc.get("name", "") for doc in stock_docs}
        for row in summary_rows:
            row["stock_name"] = symbol_to_name.get(row["symbol"], "")

        total_realized_pnl = round(sum(r["realized_pnl"] for r in summary_rows), 2)

        return {
            "success": True,
            "data": summary_rows,
            "total_realized_pnl": total_realized_pnl,
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

@router.get("/trade-activities/stats", summary="获取当前用户的所有交易活动统计")
def get_trade_activities_stats(current_user: dict = Depends(get_current_active_user)):
    """统计当前用户的所有交易活动（包括信号和执行记录）"""
    try:
        collections = get_db_collections()
        executions_coll = collections["trade_executions"]
        signals_coll = collections["trade_signals"]
        user_id = current_user["id"]
        
        # 获取执行记录（排除回测，按用户）
        executions = list(executions_coll.find({
            "worker_mode": {"$ne": "backtest"},
            "user_id": user_id
        }))
        
        # 获取交易信号（live 模式，按用户）
        signals = list(signals_coll.find({
            "$or": [
                {"worker_mode": "live"},
                {"mode": "live"}
            ],
            "user_id": user_id
        }))
        
        # 统计执行记录
        exec_buy = sum(1 for t in executions if (t.get("side") or t.get("action", "")).upper() == "BUY")
        exec_sell = sum(1 for t in executions if (t.get("side") or t.get("action", "")).upper() == "SELL")
        
        # 统计信号
        signal_buy = sum(1 for s in signals if s.get("action", "").lower() == "buy")
        signal_sell = sum(1 for s in signals if s.get("action", "").lower() == "sell")
        
        # 收集所有股票
        symbols = set()
        for t in executions:
            if t.get("symbol"):
                symbols.add(t.get("symbol"))
        for s in signals:
            if s.get("symbol"):
                symbols.add(s.get("symbol"))
        
        return {
            "success": True,
            "stats": {
                "total_trades": len(executions) + len(signals),
                "executed_trades": len(executions),
                "pending_signals": len(signals),
                "buy_trades": exec_buy + signal_buy,
                "sell_trades": exec_sell + signal_sell,
                "total_symbols": len(symbols),
                "symbols": list(symbols)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")

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
        stock_info_coll = collections["stock_info"]
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
            
            executions = list(executions_coll.find(exec_query).sort("timestamp", -1))
            
            for exec_doc in executions:
                merged_activities.append({
                    "_id": str(exec_doc.get("_id")),
                    "symbol": exec_doc.get("symbol"),
                    "direction": (exec_doc.get("side") or exec_doc.get("action", "")).upper(),
                    "status": "FILLED",
                    "status_category": "executed",
                    "price": exec_doc.get("filled_price", exec_doc.get("price", 0)),
                    "target_price": exec_doc.get("target_price"),
                    "filled_price": exec_doc.get("filled_price"),
                    "quantity": exec_doc.get("quantity", exec_doc.get("size", 0)),
                    "strategy": exec_doc.get("strategy_name", exec_doc.get("strategy", "")),
                    "timestamp": exec_doc.get("timestamp", 0),
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
            
            signals = list(signals_coll.find(signal_query).sort("timestamp", -1))
            
            for signal_doc in signals:
                raw_status = str(signal_doc.get("status", "pending")).lower()
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