from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional, Dict, Any
from datetime import datetime
from db import get_db_collections
from utils.stock_utils import serialize_doc

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

@router.get("/trade-executions/", summary="获取所有交易执行记录")
def get_all_trade_executions(
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天")
):
    """获取所有交易执行记录，按时间倒序排列，支持日期范围筛选"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        stock_info_collection = collections["stock_info"]
        
        # 构建查询条件
        query = {}
        
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
            date_query = {}
            if start_date:
                date_query["$gte"] = start_date
            if end_date:
                date_query["$lte"] = end_date
            if date_query:
                or_conditions.append({"date": date_query})
            
            # timestamp 字段条件
            timestamp_query = {}
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

@router.get("/trade-executions/symbol/{symbol}", summary="获取特定股票的交易执行记录")
def get_trade_executions_by_symbol(
    symbol: str,
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量")
):
    """获取特定股票的交易执行记录，按时间倒序排列"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        stock_info_collection = collections["stock_info"]
        
        # 查询并排序
        cursor = collection.find({"symbol": symbol}).sort("timestamp", -1).skip(skip).limit(limit)
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

@router.get("/trade-executions/date/{date}", summary="获取特定日期的交易执行记录")
def get_trade_executions_by_date(
    date: str,
    limit: int = Query(100, description="返回记录数量限制"),
    skip: int = Query(0, description="跳过的记录数量")
):
    """获取特定日期的交易执行记录，按时间倒序排列"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        stock_info_collection = collections["stock_info"]
        
        # 查询并排序
        cursor = collection.find({"date": date}).sort("timestamp", -1).skip(skip).limit(limit)
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

@router.get("/trade-executions/stats", summary="获取交易统计数据")
def get_trade_execution_stats():
    """获取交易统计数据"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        
        # 获取所有交易记录
        trades = list(collection.find())
        
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
        buy_trades = sum(1 for t in trades if t.get("side", "").upper() == "BUY")
        sell_trades = sum(1 for t in trades if t.get("side", "").upper() == "SELL")
        
        # 按股票分组统计
        symbols = {}
        for trade in trades:
            symbol = trade.get("symbol", "UNKNOWN")
            if symbol not in symbols:
                symbols[symbol] = {
                    "buy_count": 0,
                    "sell_count": 0,
                    "total_quantity": 0,
                    "strategies": set()
                }
            
            side = trade.get("side", "").upper()
            if side == "BUY":
                symbols[symbol]["buy_count"] += 1
            elif side == "SELL":
                symbols[symbol]["sell_count"] += 1
                
            symbols[symbol]["total_quantity"] += trade.get("quantity", 0)
            symbols[symbol]["strategies"].add(trade.get("strategy_name", "UNKNOWN"))
        
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

@router.get("/trade-executions/strategies", summary="获取所有策略名称")
def get_trade_strategies():
    """获取所有出现过的策略名称"""
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        
        # 获取所有不同的策略名称
        strategies = list(collection.distinct("strategy_name"))
        
        return {
            "success": True,
            "strategies": [s for s in strategies if s]  # 过滤掉空值
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")