from fastapi import APIRouter, Query, HTTPException, Depends
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from db import get_db_collections, get_data_db
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol, strip_exchange_suffix
from utils.time_display import add_display_time_fields
from auth import get_current_active_user
from services.trade_ledger import (
    compute_realized_pnl_for_group,
    group_executions_by_order,
)
import logging
import os
import time

router = APIRouter()
log = logging.getLogger(__name__)

DEFAULT_TRADE_QUERY_DAYS = 30
MAX_TRADE_ACTIVITY_LIMIT = 200
MAX_TRADE_SKIP = 5000


def _valid_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) else None


def _trade_time_window_query(
    *,
    days: Optional[int],
    start_date: Optional[str],
    end_date: Optional[str],
    all_dates: bool = False,
    default_days: int = DEFAULT_TRADE_QUERY_DAYS,
) -> Dict[str, Any]:
    """Return a bounded date/timestamp query for trade read paths."""
    if all_dates:
        return {}

    days = _valid_int(days)
    start_date = start_date if isinstance(start_date, str) else None
    end_date = end_date if isinstance(end_date, str) else None

    if days is None and not (start_date or end_date):
        days = default_days

    if days:
        days = max(1, min(days, 366))
        end = datetime.now().replace(hour=23, minute=59, second=59)
        start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    else:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0) if start_date else None
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if end_date else None

    or_conditions = []
    if start or end:
        timestamp_query: Dict[str, Any] = {}
        date_query: Dict[str, Any] = {}
        compact_date_query: Dict[str, Any] = {}
        if start:
            timestamp_query["$gte"] = start.timestamp()
            date_query["$gte"] = start.strftime("%Y-%m-%d")
            compact_date_query["$gte"] = start.strftime("%Y%m%d")
        if end:
            timestamp_query["$lte"] = end.timestamp()
            date_query["$lte"] = end.strftime("%Y-%m-%d")
            compact_date_query["$lte"] = end.strftime("%Y%m%d")
        or_conditions.extend(
            [
                {"timestamp": timestamp_query},
                {"date": date_query},
                {"date": compact_date_query},
            ]
        )
    return {"$or": or_conditions} if or_conditions else {}


def _signal_time_window_query(
    *,
    days: Optional[int],
    start_date: Optional[str],
    end_date: Optional[str],
    all_dates: bool = False,
    default_days: int = DEFAULT_TRADE_QUERY_DAYS,
) -> Dict[str, Any]:
    """Return a bounded time query for trade signal activity.

    Unlike trade executions, a signal may be created earlier and later updated
    to filled. For activity views, updated_at/executed_at are part of the signal
    lifecycle contract and should participate in recent-window filtering.
    """
    if all_dates:
        return {}

    days = _valid_int(days)
    start_date = start_date if isinstance(start_date, str) else None
    end_date = end_date if isinstance(end_date, str) else None

    if days is None and not (start_date or end_date):
        days = default_days

    if days:
        days = max(1, min(days, 366))
        end = datetime.now().replace(hour=23, minute=59, second=59)
        start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    else:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0) if start_date else None
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if end_date else None

    if not (start or end):
        return {}

    numeric_query: Dict[str, Any] = {}
    date_query: Dict[str, Any] = {}
    compact_date_query: Dict[str, Any] = {}
    if start:
        numeric_query["$gte"] = start.timestamp()
        date_query["$gte"] = start.strftime("%Y-%m-%d")
        compact_date_query["$gte"] = start.strftime("%Y%m%d")
    if end:
        numeric_query["$lte"] = end.timestamp()
        date_query["$lte"] = end.strftime("%Y-%m-%d")
        compact_date_query["$lte"] = end.strftime("%Y%m%d")

    return {
        "$or": [
            {"timestamp": numeric_query},
            {"updated_at": numeric_query},
            {"executed_at": numeric_query},
            {"submitted_at": numeric_query},
            {"date": date_query},
            {"date": compact_date_query},
        ]
    }


def _apply_query_filter(query: Dict[str, Any], extra_filter: Dict[str, Any]) -> None:
    if not extra_filter:
        return

    existing_or = query.pop("$or", None)
    extra_or = extra_filter.get("$or")
    if existing_or and extra_or:
        query.setdefault("$and", [])
        query["$and"].extend([{"$or": existing_or}, {"$or": extra_or}])

    for key, value in extra_filter.items():
        if key == "$or" and existing_or and extra_or:
            continue
        query[key] = value


def _effective_window_days(
    days: Optional[int],
    start_date: Optional[str],
    end_date: Optional[str],
    all_dates: bool = False,
) -> Optional[int]:
    if all_dates:
        return None
    days = _valid_int(days)
    has_range = isinstance(start_date, str) or isinstance(end_date, str)
    if days:
        return max(1, min(days, 366))
    return None if has_range else DEFAULT_TRADE_QUERY_DAYS


def _bounded_trade_limit(limit: int) -> int:
    return max(1, min(_valid_int(limit) or 100, MAX_TRADE_ACTIVITY_LIMIT))


def _bounded_trade_skip(skip: int) -> int:
    return max(0, min(_valid_int(skip) or 0, MAX_TRADE_SKIP))


def _execution_filled_size(row: Dict[str, Any]) -> float:
    value = row.get("filled_size")
    if value in (None, ""):
        return 0.0
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return 0.0


def _is_filled_execution(row: Dict[str, Any]) -> bool:
    return _execution_filled_size(row) != 0


def _safe_count_documents(collection: Any, query: Dict[str, Any]) -> Optional[int]:
    try:
        return collection.count_documents(query, maxTimeMS=1000)
    except TypeError:
        try:
            return collection.count_documents(query)
        except Exception:
            return None
    except Exception:
        return None


def _latest_trade_sample(collection: Any, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return collection.find_one(
            query,
            {
                "_id": 0,
                "user_id": 1,
                "order_id": 1,
                "timestamp": 1,
                "date": 1,
                "datetime": 1,
                "filled_size": 1,
                "status": 1,
                "worker_mode": 1,
                "mode": 1,
            },
            sort=[("timestamp", -1)],
        )
    except Exception:
        return None


def _log_empty_trade_activity_diagnostics(
    *,
    executions_coll: Any,
    signals_coll: Any,
    user_id: Any,
    exec_query: Optional[Dict[str, Any]],
    signal_query: Optional[Dict[str, Any]],
    status_filter: Optional[str],
    execution_source_count: int,
    execution_filled_count: int,
    signal_source_count: int,
    signal_included_count: int,
) -> None:
    if os.getenv("TRADE_ACTIVITY_EMPTY_DIAGNOSTICS", "1") == "0":
        return

    base_exec_query = {"user_id": user_id, "worker_mode": {"$ne": "backtest"}}
    base_signal_query = {"user_id": user_id}
    log.warning(
        "Trade activities empty diagnostics user_id=%s status_filter=%s "
        "exec_base=%s exec_window=%s exec_source=%s exec_filled=%s "
        "signal_base=%s signal_window=%s signal_source=%s signal_included=%s "
        "latest_exec=%s latest_signal=%s",
        user_id,
        status_filter,
        _safe_count_documents(executions_coll, base_exec_query),
        _safe_count_documents(executions_coll, exec_query) if exec_query else None,
        execution_source_count,
        execution_filled_count,
        _safe_count_documents(signals_coll, base_signal_query),
        _safe_count_documents(signals_coll, signal_query) if signal_query else None,
        signal_source_count,
        signal_included_count,
        _latest_trade_sample(executions_coll, base_exec_query),
        _latest_trade_sample(signals_coll, base_signal_query),
    )


def _display_trade_quantity(row: Dict[str, Any]) -> float:
    filled = _execution_filled_size(row)
    if filled:
        return filled
    for field in ("size", "quantity"):
        value = row.get(field)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _numeric_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _signal_activity_timestamp(row: Dict[str, Any]) -> float:
    for field in ("updated_at", "executed_at", "submitted_at", "timestamp", "created_at"):
        ts = _numeric_timestamp(row.get(field))
        if ts:
            return ts
    return 0.0


def _signal_display_quantity(row: Dict[str, Any]) -> float:
    for field in ("filled_qty", "size", "quantity"):
        value = row.get(field)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


_EXECUTION_ACTIVITY_FIELDS = (
    "_id",
    "symbol",
    "side",
    "action",
    "status",
    "filled_price",
    "price",
    "target_price",
    "filled_size",
    "quantity",
    "size",
    "strategy_name",
    "strategy",
    "timestamp",
    "order_id",
    "broker_order_id",
    "qmt_order_id",
    "date",
    "total_fee",
    "commission",
    "worker_mode",
    "user_id",
)
_SIGNAL_ACTIVITY_FIELDS = (
    "_id",
    "symbol",
    "action",
    "status",
    "avg_price",
    "price",
    "order_id",
    "broker_order_id",
    "qmt_order_id",
    "strategy",
    "timestamp",
    "updated_at",
    "executed_at",
    "submitted_at",
    "created_at",
    "mode",
    "worker_mode",
    "user_id",
    "date",
    "filled_qty",
    "size",
    "quantity",
    "target_size",
    "effective_limit_price",
    "last_market_price",
    "submitted_age_seconds",
    "broker_status",
    "broker_status_msg",
    "last_status_checked_at",
    "error_message",
    "retry_count",
)
_EXECUTION_ACTIVITY_PROJECTION = {field: 1 for field in _EXECUTION_ACTIVITY_FIELDS}
_SIGNAL_ACTIVITY_PROJECTION = {field: 1 for field in _SIGNAL_ACTIVITY_FIELDS}

_PENDING_SIGNAL_STATUSES = {"pending", "retry_pending", "submitted", ""}
_EXECUTED_SIGNAL_STATUSES = {"executed", "filled", "partial_filled"}
_PENDING_EXECUTION_STATUSES = {"pending", "submitted", "partial_filled", "cancel_requested", "retry_pending"}


def _normalize_activity_type(activity_type: Optional[str]) -> Optional[str]:
    if activity_type is None:
        return None
    normalized = str(activity_type).strip().lower()
    if normalized in ("executions", "signals"):
        return normalized
    raise HTTPException(
        status_code=400,
        detail="activity_type must be 'executions' or 'signals'",
    )


def _execution_activity_from_doc(exec_doc: Dict[str, Any]) -> Dict[str, Any]:
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
    return {
        "_id": str(exec_doc.get("_id")),
        "symbol": exec_doc.get("symbol"),
        "direction": (exec_doc.get("side") or exec_doc.get("action", "")).upper(),
        "status": status,
        "status_category": status_category,
        "price": exec_doc.get("filled_price", exec_doc.get("price", 0)),
        "target_price": exec_doc.get("target_price"),
        "filled_price": exec_doc.get("filled_price"),
        "quantity": _display_trade_quantity(exec_doc),
        "strategy": exec_doc.get("strategy_name", exec_doc.get("strategy", "")),
        "timestamp": exec_doc.get("timestamp", 0),
        "execution_time": exec_time.get("execution_time"),
        "timestamp_iso": exec_time.get("timestamp_iso"),
        "order_id": exec_doc.get("order_id", ""),
        "broker_order_id": exec_doc.get("broker_order_id") or exec_doc.get("qmt_order_id"),
        "qmt_order_id": exec_doc.get("qmt_order_id") or exec_doc.get("broker_order_id"),
        "source": "trade_execution",
        "date": exec_doc.get("date", ""),
    }


def _signal_activity_from_doc(signal_doc: Dict[str, Any]) -> Dict[str, Any]:
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
    if raw_status in ("pending", "retry_pending", "submitted"):
        status_category = "pending"
    elif raw_status in _EXECUTED_SIGNAL_STATUSES:
        status_category = "executed"
    elif raw_status in ("failed", "rejected"):
        status_category = "failed"
    else:
        status_category = "other"

    activity_ts = _signal_activity_timestamp(signal_doc)
    signal_display_doc = dict(signal_doc)
    if activity_ts:
        signal_display_doc["timestamp"] = activity_ts
    signal_time = add_display_time_fields(signal_display_doc)
    order_id = signal_doc.get("order_id", "")
    return {
        "_id": str(signal_doc.get("_id")),
        "symbol": signal_doc.get("symbol"),
        "direction": (signal_doc.get("action", "")).upper(),
        "action": signal_doc.get("action"),
        "status": status_map.get(raw_status, raw_status.upper()),
        "status_category": status_category,
        "price": signal_doc.get("avg_price", signal_doc.get("price", 0)),
        "target_price": signal_doc.get("price"),
        "filled_price": signal_doc.get("avg_price"),
        "quantity": _signal_display_quantity(signal_doc),
        "strategy": signal_doc.get("strategy", ""),
        "timestamp": activity_ts or signal_doc.get("timestamp", 0),
        "execution_time": signal_time.get("execution_time"),
        "timestamp_iso": signal_time.get("timestamp_iso"),
        "order_id": order_id,
        "broker_order_id": signal_doc.get("broker_order_id") or signal_doc.get("qmt_order_id"),
        "qmt_order_id": signal_doc.get("qmt_order_id") or signal_doc.get("broker_order_id"),
        "source": "trade_signal",
        "error_message": signal_doc.get("error_message"),
        "retry_count": signal_doc.get("retry_count", 0),
        "date": signal_doc.get("date", ""),
        "effective_limit_price": signal_doc.get("effective_limit_price"),
        "last_market_price": signal_doc.get("last_market_price"),
        "submitted_age_seconds": signal_doc.get("submitted_age_seconds"),
        "broker_status": signal_doc.get("broker_status"),
        "broker_status_msg": signal_doc.get("broker_status_msg"),
        "last_status_checked_at": signal_doc.get("last_status_checked_at"),
        "raw_signal_status": raw_status,
    }


def _execution_matches_status_filter(exec_doc: Dict[str, Any], status_filter: Optional[str]) -> bool:
    if not status_filter or status_filter == "all":
        return True
    raw_status = str(exec_doc.get("status", "filled")).lower()
    if status_filter == "executed":
        return _is_filled_execution(exec_doc)
    if status_filter == "pending":
        return raw_status in _PENDING_EXECUTION_STATUSES
    return True


def _signal_matches_status_filter(signal_doc: Dict[str, Any], status_filter: Optional[str]) -> bool:
    if not status_filter or status_filter == "all":
        return True
    raw_status = str(signal_doc.get("status", "pending")).lower()
    if status_filter == "pending":
        return raw_status in _PENDING_SIGNAL_STATUSES
    if status_filter == "executed":
        return raw_status in _EXECUTED_SIGNAL_STATUSES
    return True


def _collect_execution_activities(
    executions_coll: Any,
    *,
    user_id: Any,
    fetch_limit: int,
    date_query: Dict[str, Any],
    status_filter: Optional[str],
) -> tuple[List[Dict[str, Any]], Dict[str, Any], int, int]:
    exec_query: Dict[str, Any] = {"worker_mode": {"$ne": "backtest"}, "user_id": user_id}
    if date_query:
        _apply_query_filter(exec_query, date_query)

    execution_docs = list(
        executions_coll.find(exec_query, _EXECUTION_ACTIVITY_PROJECTION)
        .sort("timestamp", -1)
        .limit(fetch_limit)
    )
    execution_source_count = len(execution_docs)
    executions = _group_executions_by_order(execution_docs)
    activities: List[Dict[str, Any]] = []
    for exec_doc in executions:
        if not _execution_matches_status_filter(exec_doc, status_filter):
            continue
        activities.append(_execution_activity_from_doc(exec_doc))
    return activities, exec_query, execution_source_count, len(activities)


def _collect_signal_activities(
    signals_coll: Any,
    *,
    user_id: Any,
    fetch_limit: int,
    signal_date_query: Dict[str, Any],
    status_filter: Optional[str],
) -> tuple[List[Dict[str, Any]], Dict[str, Any], int, int]:
    signal_query: Dict[str, Any] = {
        "$or": [
            {"worker_mode": "live"},
            {"mode": "live"},
        ],
        "user_id": user_id,
    }
    if signal_date_query:
        _apply_query_filter(signal_query, signal_date_query)

    signals = list(
        signals_coll.find(signal_query, _SIGNAL_ACTIVITY_PROJECTION)
        .sort("timestamp", -1)
        .limit(fetch_limit)
    )
    signal_source_count = len(signals)
    activities: List[Dict[str, Any]] = []
    for signal_doc in signals:
        if not _signal_matches_status_filter(signal_doc, status_filter):
            continue
        activities.append(_signal_activity_from_doc(signal_doc))
    return activities, signal_query, signal_source_count, len(activities)


def _collect_legacy_merged_activities(
    executions_coll: Any,
    signals_coll: Any,
    *,
    user_id: Any,
    fetch_limit: int,
    date_query: Dict[str, Any],
    signal_date_query: Dict[str, Any],
    status_filter: Optional[str],
) -> tuple[
    List[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    int,
    int,
    int,
    int,
]:
    merged_activities: List[Dict[str, Any]] = []
    execution_source_count = 0
    execution_filled_count = 0
    signal_source_count = 0
    signal_included_count = 0
    execution_order_ids: set = set()
    exec_query = None
    signal_query = None

    if status_filter != "pending":
        # Legacy compat: the pre-refactor merge always dropped unfilled orders from
        # the execution side (via _is_filled_execution) whenever not in pending mode,
        # regardless of status_filter. Force "executed" semantics to preserve that.
        exec_activities, exec_query, execution_source_count, execution_filled_count = _collect_execution_activities(
            executions_coll,
            user_id=user_id,
            fetch_limit=fetch_limit,
            date_query=date_query,
            status_filter="executed",
        )
        merged_activities.extend(exec_activities)
        execution_order_ids = {row.get("order_id") for row in exec_activities if row.get("order_id")}

    signal_activities, signal_query, signal_source_count, signal_included_count = _collect_signal_activities(
        signals_coll,
        user_id=user_id,
        fetch_limit=fetch_limit,
        signal_date_query=signal_date_query,
        status_filter=status_filter,
    )
    for row in signal_activities:
        order_id = row.get("order_id", "")
        if order_id and order_id in execution_order_ids:
            continue
        merged_activities.append(row)

    return (
        merged_activities,
        exec_query,
        signal_query,
        execution_source_count,
        execution_filled_count,
        signal_source_count,
        signal_included_count,
    )


def _log_trade_timing(label: str, started_at: float, **parts: Any) -> None:
    elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("TRADE_API_TIMING_SECONDS", "1"))
    if elapsed < threshold:
        return
    suffix = " ".join(f"{key}={value}" for key, value in parts.items())
    log.warning("%s timing total=%.3fs %s", label, elapsed, suffix)


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


# 持仓重建与已实现盈亏计算已下沉到 services.trade_ledger，供 router 与后台服务复用。
# 保留下划线别名以兼容既有调用与单元测试。
_compute_realized_pnl_for_group = compute_realized_pnl_for_group
_group_executions_by_order = group_executions_by_order


@router.get("/trade-executions/", summary="获取当前用户的交易执行记录")
def get_all_trade_executions(
    limit: int = Query(100, description="返回记录数量限制", ge=1, le=500),
    skip: int = Query(0, description="跳过的记录数量", ge=0, le=5000),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天"),
    all_dates: bool = Query(False, description="显式查询所有日期"),
    plan_id: Optional[str] = Query(None, description="只返回指定 portfolio plan 发布的实盘成交"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前登录用户的交易执行记录，按时间倒序排列，支持日期范围筛选"""
    started_at = time.perf_counter()
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
        
        _apply_query_filter(
            query,
            _trade_time_window_query(days=days, start_date=start_date, end_date=end_date, all_dates=all_dates),
        )
        
        # 查询并排序
        cursor = collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
        trades = [add_display_time_fields(serialize_doc(doc)) for doc in cursor]
        
        # 添加股票名称
        trades = enrich_trades_with_stock_names(trades, stock_info_collection)
        _log_trade_timing("Trade executions", started_at, rows=len(trades), limit=limit, skip=skip)
        
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
        trades = [t for t in trades if _is_filled_execution(t)]
        
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
def get_trade_execution_stats(
    days: Optional[int] = Query(None, ge=1, le=366, description="最近N天，默认30天"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    all_dates: bool = Query(False, description="显式查询所有日期"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取当前登录用户的交易统计数据"""
    started_at = time.perf_counter()
    try:
        collections = get_db_collections()
        collection = collections["trade_executions"]
        
        user_id = current_user["id"]
        query: Dict[str, Any] = {"user_id": user_id}
        effective_days = _effective_window_days(days, start_date, end_date, all_dates=all_dates)
        _apply_query_filter(
            query,
            _trade_time_window_query(days=days, start_date=start_date, end_date=end_date, all_dates=all_dates),
        )
        trades = _group_executions_by_order(list(collection.find(query).sort("timestamp", -1).limit(2000)))
        # 过滤掉完全未成交的订单（filled_size 为 0），避免统计中计入作废单
        trades = [t for t in trades if _is_filled_execution(t)]
        
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
                
            symbols[symbol]["total_quantity"] += _execution_filled_size(trade)
            symbols[symbol]["strategies"].add(trade.get("strategy_name", trade.get("strategy", "UNKNOWN")))
        
        # 转换策略集合为列表
        for symbol_data in symbols.values():
            symbol_data["strategies"] = list(symbol_data["strategies"])
        
        response = {
            "success": True,
            "stats": {
                "total_trades": total_trades,
                "buy_trades": buy_trades,
                "sell_trades": sell_trades,
                "total_symbols": len(symbols),
                "symbols": symbols
            }
        }
        _log_trade_timing("Trade execution stats", started_at, rows=len(trades), days=effective_days)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")


@router.get("/trade-executions/pnl-summary", summary="获取当前用户按股票和策略分组的盈亏统计")
def get_trade_pnl_summary(
    days: Optional[int] = Query(None, ge=1, le=366, description="最近N天，默认30天"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    all_dates: bool = Query(False, description="显式查询所有日期"),
    current_user: dict = Depends(get_current_active_user),
):
    """按股票和策略分组汇总当前用户的已实现盈亏和净持仓.

    主要用于前端展示：
      - 每个股票 + 策略的交易笔数、净持仓数量、已实现盈亏
      - 支持按日期范围或最近N天筛选，便于回溯查看
    """
    started_at = time.perf_counter()
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
        if not isinstance(all_dates, bool):
            all_dates = False

        query: Dict[str, Any] = {"user_id": user_id}
        effective_days = _effective_window_days(days, start_date, end_date, all_dates=all_dates)
        _apply_query_filter(
            query,
            _trade_time_window_query(days=days, start_date=start_date, end_date=end_date, all_dates=all_dates),
        )

        trades = _group_executions_by_order(list(executions_coll.find(query).sort("timestamp", -1).limit(5000)))
        # 过滤掉完全未成交的订单（filled_size 为 0），避免统计中计入作废单
        trades = [t for t in trades if _is_filled_execution(t)]
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

        response = {
            "success": True,
            "data": summary_rows,
            "total_realized_pnl": total_realized_pnl,
            "total_unrealized_pnl": total_unrealized_pnl,
            "window_days": effective_days,
        }
        _log_trade_timing("Trade PnL summary", started_at, rows=len(trades), groups=len(summary_rows), days=effective_days)
        return response
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
    limit: int = Query(100, description="返回记录数量限制", ge=1),
    skip: int = Query(0, description="跳过的记录数量", ge=0, le=MAX_TRADE_SKIP),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    days: Optional[int] = Query(None, description="最近N天"),
    all_dates: bool = Query(False, description="显式查询所有日期"),
    status_filter: Optional[str] = Query(None, description="状态筛选: executed, pending, all"),
    activity_type: Optional[str] = Query(
        None,
        description="活动来源: executions 仅成交记录, signals 仅交易信号; 缺省时兼容旧版 merge",
    ),
    current_user: dict = Depends(get_current_active_user),
):
    """按 tab 查询交易活动; activity_type 指定单集合, status_filter=all 仅在当前 tab 内生效."""
    started_at = time.perf_counter()
    try:
        collections = get_db_collections()
        executions_coll = collections["trade_executions"]
        signals_coll = collections["trade_signals"]
        data_collections = get_data_db()
        stock_info_coll = data_collections["stock_info"]
        user_id = current_user["id"]

        limit = _bounded_trade_limit(limit)
        skip = _bounded_trade_skip(skip)
        fetch_limit = min(MAX_TRADE_SKIP + MAX_TRADE_ACTIVITY_LIMIT, skip + limit + 100)
        effective_days = _effective_window_days(days, start_date, end_date, all_dates=all_dates)
        date_query = _trade_time_window_query(days=days, start_date=start_date, end_date=end_date, all_dates=all_dates)
        signal_date_query = _signal_time_window_query(
            days=days,
            start_date=start_date,
            end_date=end_date,
            all_dates=all_dates,
        )

        normalized_activity_type = _normalize_activity_type(activity_type)
        execution_source_count = 0
        execution_filled_count = 0
        signal_source_count = 0
        signal_included_count = 0
        exec_query = None
        signal_query = None

        if normalized_activity_type == "executions":
            merged_activities, exec_query, execution_source_count, execution_filled_count = (
                _collect_execution_activities(
                    executions_coll,
                    user_id=user_id,
                    fetch_limit=fetch_limit,
                    date_query=date_query,
                    status_filter=status_filter,
                )
            )
        elif normalized_activity_type == "signals":
            merged_activities, signal_query, signal_source_count, signal_included_count = (
                _collect_signal_activities(
                    signals_coll,
                    user_id=user_id,
                    fetch_limit=fetch_limit,
                    signal_date_query=signal_date_query,
                    status_filter=status_filter,
                )
            )
        else:
            (
                merged_activities,
                exec_query,
                signal_query,
                execution_source_count,
                execution_filled_count,
                signal_source_count,
                signal_included_count,
            ) = _collect_legacy_merged_activities(
                executions_coll,
                signals_coll,
                user_id=user_id,
                fetch_limit=fetch_limit,
                date_query=date_query,
                signal_date_query=signal_date_query,
                status_filter=status_filter,
            )

        merged_activities.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

        total = len(merged_activities)
        if total == 0:
            _log_empty_trade_activity_diagnostics(
                executions_coll=executions_coll,
                signals_coll=signals_coll,
                user_id=user_id,
                exec_query=exec_query,
                signal_query=signal_query,
                status_filter=status_filter,
                execution_source_count=execution_source_count,
                execution_filled_count=execution_filled_count,
                signal_source_count=signal_source_count,
                signal_included_count=signal_included_count,
            )
        merged_activities = merged_activities[skip:skip + limit]

        merged_activities = enrich_trades_with_stock_names(merged_activities, stock_info_coll)

        response = {
            "success": True,
            "data": merged_activities,
            "count": len(merged_activities),
            "total": total,
            "window_days": effective_days,
            "activity_type": normalized_activity_type,
        }
        _log_trade_timing(
            "Trade activities",
            started_at,
            execution_docs=execution_source_count,
            signal_docs=signal_source_count,
            signal_included=signal_included_count,
            merged=total,
            returned=len(merged_activities),
            limit=limit,
            skip=skip,
            activity_type=normalized_activity_type or "legacy",
        )
        return response
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
