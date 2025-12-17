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
                "status": {"$in": ["pending", "retry_pending", "submitted"]},
                "user_id": user_id,
            }
            if date_query:
                signal_query.update(date_query)
            
            signals = list(signals_coll.find(signal_query).sort("timestamp", -1))
            
            for signal_doc in signals:
                status_map = {
                    "pending": "PENDING",
                    "retry_pending": "RETRY",
                    "submitted": "SUBMITTED"
                }
                merged_activities.append({
                    "_id": str(signal_doc.get("_id")),
                    "symbol": signal_doc.get("symbol"),
                    "direction": (signal_doc.get("action", "")).upper(),
                    "status": status_map.get(signal_doc.get("status", "pending"), "PENDING"),
                    "status_category": "pending",
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