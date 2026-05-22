"""Backtesting Task Management API

This module provides APIs for users to create, manage, and retrieve backtesting tasks.
Workers can poll for pending tasks, claim them, and report results.
"""

import os
import uuid
import math

from fastapi import APIRouter, HTTPException, Depends, Header, status, Body
from datetime import datetime, date
from enum import Enum
from pydantic import BaseModel, Field,ConfigDict,field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from bson import ObjectId

from auth import get_current_user
from db import get_analyzer_db, get_db_collections
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/backtest", tags=["backtest"])


# ============================================================================
# Request/Response Models
# ============================================================================

class BacktestTaskCreate(BaseModel):
    """Request model for creating a backtest task"""
    symbol: str = Field(..., description="Trading symbol (e.g., 000858.SZ or 510300.SH)")
    asset_type: str = Field(default="stock", description="Asset type: stock or etf")
    strategy_key: str = Field(..., description="Strategy key from STRATEGY_MAP")
    preset: Optional[str] = Field(default=None, description="Optional preset name (e.g., turtle_conservative)")
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Strategy parameters")
    start_date: str = Field(..., description="Backtest start date (YYYYMMDD)")
    end_date: str = Field(..., description="Backtest end date (YYYYMMDD)")
    initial_cash: float = Field(default=1000000.0, description="Initial cash for backtest")

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_required_date(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("start_date/end_date are required")
        return value.replace("-", "")

    model_config = ConfigDict(
        json_schema_extra = {
            "example": {
                "symbol": "000858",
                "strategy_key": "turtle",
                "strategy_params": {"period": 20, "atr_period": 20},
                "start_date": "20240101",
                "end_date": "20241231",
                "initial_cash": 1000000.0
            }
        }
    )


class BacktestBatchCreate(BaseModel):
    """Request model for creating a batch backtest experiment."""
    name: Optional[str] = Field(default=None, description="Human-readable experiment name")
    symbols: List[str] = Field(default_factory=list, description="Symbols for manual universe")
    asset_type: str = Field(default="stock", description="Asset type for child tasks: stock or etf")
    universe_type: str = Field(default="manual", description="manual, index, watchlist, or strategy_pool")
    universe_value: Optional[str] = Field(default=None, description="Universe key, e.g. csi1000")
    strategy_key: str = Field(..., description="Strategy key from STRATEGY_MAP")
    preset: Optional[str] = Field(default=None, description="Optional preset name")
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    start_date: str = Field(..., description="Backtest start date (YYYYMMDD)")
    end_date: str = Field(..., description="Backtest end date (YYYYMMDD)")
    initial_cash: float = Field(default=1000000.0)
    limit_symbols: int = Field(default=0, ge=0, description="Optional cap for testing. 0 means no limit.")

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_required_date(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("start_date/end_date are required")
        return value.replace("-", "")


class BacktestBatchRerunCreate(BaseModel):
    name: Optional[str] = None
    strategy_key: Optional[str] = None
    preset: Optional[str] = None
    strategy_params: Optional[Dict[str, Any]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_cash: Optional[float] = None
    only_statuses: List[str] = Field(default_factory=list)


class BacktestBatchReviewCreate(BaseModel):
    objective: str = Field(default="提高收益", description="Review objective")
    include_top_n: int = Field(default=20, ge=1, le=100)
    include_bottom_n: int = Field(default=20, ge=1, le=100)
    include_failures: bool = True

class BacktestStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class BacktestTaskResponse(BaseModel):
    """Response model for backtest task"""
    task_id: str = Field(..., description="Unique identifier for the backtest task")
    user_id: str = Field(..., description="Identifier of the user who initiated the task")
    symbol: str = Field(..., description="Trading symbol for the backtest (e.g., 'AAPL')")
    asset_type: str = Field(default="stock", description="Asset type: stock or etf")
    stock_name: Optional[str] = Field(default=None, description="Human-readable stock name")
    strategy_key: str = Field(..., description="Key identifying the trading strategy used")
    preset: Optional[str] = Field(default=None, description="Preset name used when creating the task")
    # 4. Use Field for default_factory for mutable types
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Parameters for the trading strategy")
    # 2. Use date objects for date fields
    start_date: Optional[date] = Field(None, description="Start date of the backtest period (YYYY-MM-DD)")
    end_date: Optional[date] = Field(None, description="End date of the backtest period (YYYY-MM-DD)")
    initial_cash: float = Field(..., gt=0, description="Initial capital for the backtest")
    # 1. Use Enum for status
    status: BacktestStatus = Field(..., description="Current status of the backtest task")
    worker_id: Optional[str] = Field(None, description="Identifier of the worker processing the task, if claimed")
    # 5. Default created_at to now if always set on creation
    created_at: datetime = Field(default_factory=datetime.now, description="Timestamp when the task was created")
    started_at: Optional[datetime] = Field(None, description="Timestamp when the task started processing")
    completed_at: Optional[datetime] = Field(None, description="Timestamp when the task completed (successfully or failed)")
    error_message: Optional[str] = Field(None, description="Error message if the task failed")

    @field_validator('start_date', 'end_date', mode='before')
    @classmethod
    def parse_yyyymmdd_to_date(cls, v: Any) -> Optional[date]:
        """
        Custom validator to parse YYYYMMDD strings into datetime.date objects.
        """
        if v is None:
            return None
        if isinstance(v, str):
            if not v.strip():
                return None
            try:
                # Attempt to parse as YYYYMMDD
                return datetime.strptime(v, '%Y%m%d').date()
            except ValueError:
                # If it's not YYYYMMDD, let Pydantic's default date parsing handle it
                # (e.g., for YYYY-MM-DD or other standard formats)
                pass
        return v # Return the value as is for Pydantic to process (e.g., if it's already a date object)

class BacktestResultMetrics(BaseModel):
    """Backtest result metrics"""
    total_return: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    total_trades: Optional[int] = None


class BacktestTrade(BaseModel):
    """Single trade record"""
    datetime: str
    action: str  # buy, sell
    price: float
    quantity: int
    commission: float
    pnl: float
    cumulative_pnl: float


class BacktestEquityPoint(BaseModel):
    """Single point in equity curve"""
    date: str
    value: float


class BacktestResultReport(BaseModel):
    """Request model for worker to report backtest results"""
    metrics: BacktestResultMetrics
    trades: List[BacktestTrade]
    equity_curve: List[BacktestEquityPoint]


class BacktestResultResponse(BaseModel):
    """Response model for backtest results"""
    task_id: str
    user_id: str
    symbol: str
    asset_type: str = "stock"
    strategy_key: str
    strategy_params: Optional[Dict[str, Any]] = None  # 添加策略参数
    metrics: BacktestResultMetrics
    trades: List[BacktestTrade]
    equity_curve: List[BacktestEquityPoint]
    created_at: datetime


# ============================================================================
# User APIs - Task Management
# ============================================================================

def _normalize_asset_type(asset_type: Optional[str]) -> str:
    value = (asset_type or "stock").strip().lower()
    if value not in {"stock", "etf"}:
        raise HTTPException(status_code=400, detail="asset_type must be 'stock' or 'etf'")
    return value


def _lookup_symbol_name(collections: Dict[str, Any], symbol: Optional[str], asset_type: str) -> Optional[str]:
    if not symbol:
        return None
    if asset_type == "etf":
        etf_basic_col = collections.get("etf_basic")
        if etf_basic_col is None:
            return None
        info = etf_basic_col.find_one({"ts_code": symbol})
    else:
        stock_info_col = collections.get("stock_info")
        if stock_info_col is None:
            return None
        info = stock_info_col.find_one({"symbol": symbol})
    if info and isinstance(info, dict):
        name = info.get("name")
        return name if isinstance(name, str) and name else None
    return None


def _new_task_id(symbol: str) -> str:
    clean_symbol = str(symbol or "symbol").replace(".", "_")
    return f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{clean_symbol}_{uuid.uuid4().hex[:6]}"


def _new_batch_id() -> str:
    return f"bb_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _normalize_symbol(symbol: str, asset_type: str) -> str:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return ""
    if "." in symbol:
        return symbol
    if asset_type == "etf":
        if symbol.startswith(("15", "16")):
            return f"{symbol}.SZ"
        if symbol.startswith("5"):
            return f"{symbol}.SH"
        return symbol
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    if symbol.startswith(("8", "4")):
        return f"{symbol}.BJ"
    return symbol


def _dedupe_symbols(symbols: List[str], asset_type: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        sym = _normalize_symbol(raw, asset_type)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _load_index_symbols(collections: Dict[str, Any], index_code: str) -> List[str]:
    coll = collections.get("index_constituents")
    code = str(index_code or "").strip()
    if coll is None or not code:
        return []
    latest = coll.find_one(
        {"index_code": code},
        {"_id": 0, "weight_trade_date": 1, "update_date": 1},
        sort=[("weight_trade_date", -1), ("update_date", -1)],
    )
    if not latest:
        return []
    query = {"index_code": code}
    if latest.get("weight_trade_date"):
        query["weight_trade_date"] = latest["weight_trade_date"]
    if latest.get("update_date"):
        query["update_date"] = latest["update_date"]
    return _dedupe_symbols(
        [d.get("symbol") for d in coll.find(query, {"_id": 0, "symbol": 1}).sort("symbol", 1)],
        "stock",
    )


def _resolve_batch_symbols(collections: Dict[str, Any], task: BacktestBatchCreate) -> List[str]:
    universe_type = (task.universe_type or "manual").strip().lower()
    asset_type = _normalize_asset_type(task.asset_type)
    if universe_type == "manual":
        symbols = _dedupe_symbols(task.symbols, asset_type)
    elif universe_type == "index":
        symbols = _load_index_symbols(collections, task.universe_value or "")
    elif universe_type == "watchlist":
        watchlist_col = collections.get("user_watchlists")
        doc = watchlist_col.find_one({"_id": task.universe_value}) if watchlist_col is not None and task.universe_value else None
        symbols = _dedupe_symbols((doc or {}).get("symbols", []), asset_type)
    elif universe_type == "strategy_pool":
        pool_col = collections.get("strategy_stock_pool")
        query: Dict[str, Any] = {}
        if task.universe_value:
            query["preset"] = task.universe_value
        rows = list(pool_col.find(query, {"_id": 0, "symbol": 1}).sort("date", -1).limit(2000)) if pool_col is not None else []
        symbols = _dedupe_symbols([r.get("symbol") for r in rows], asset_type)
    else:
        raise HTTPException(status_code=400, detail="universe_type must be manual, index, watchlist, or strategy_pool")
    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols resolved for batch backtest")
    return symbols


def _build_backtest_task_doc(
    *,
    current_user: dict,
    symbol: str,
    asset_type: str,
    strategy_key: str,
    preset: Optional[str],
    strategy_params: Dict[str, Any],
    start_date: str,
    end_date: str,
    initial_cash: float,
    batch_id: Optional[str] = None,
    batch_name: Optional[str] = None,
    batch_index: Optional[int] = None,
) -> Dict[str, Any]:
    task_doc = {
        "task_id": _new_task_id(symbol),
        "user_id": current_user["id"],
        "symbol": symbol,
        "asset_type": asset_type,
        "strategy_key": strategy_key,
        "preset": preset,
        "strategy_params": strategy_params or {},
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": initial_cash,
        "status": "pending",
        "worker_id": None,
        "created_at": datetime.now(),
        "started_at": None,
        "completed_at": None,
        "error_message": None,
    }
    if batch_id:
        task_doc["batch_id"] = batch_id
        task_doc["batch_name"] = batch_name
        task_doc["batch_index"] = batch_index
    return task_doc


def _metric_value(metrics: Dict[str, Any], key: str) -> Optional[float]:
    value = metrics.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_for_mongo(value: Any) -> Any:
    """Normalize worker result values before writing to MongoDB."""
    if value is None or isinstance(value, (str, bool, datetime, date, ObjectId)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        if -(2 ** 63) <= value <= (2 ** 63 - 1):
            return value
        return str(value)
    if isinstance(value, dict):
        return {str(k): _clean_for_mongo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_for_mongo(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _clean_for_mongo(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _clean_for_response(value: Any) -> Any:
    """Normalize Mongo documents for FastAPI JSON responses."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        if -(2 ** 63) <= value <= (2 ** 63 - 1):
            return value
        return str(value)
    if isinstance(value, dict):
        return {str(k): _clean_for_response(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_for_response(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _clean_for_response(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _batch_status_from_counts(total: int, counts: Dict[str, int]) -> str:
    if total <= 0:
        return "failed"
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    running = counts.get("pending", 0) + counts.get("claimed", 0) + counts.get("running", 0)
    if running:
        return "running" if completed or failed or cancelled else "pending"
    if completed == total:
        return "completed"
    if completed > 0 and (failed or cancelled):
        return "partial_failed"
    if cancelled == total:
        return "cancelled"
    return "failed"


def _summarize_batch(tasks: List[Dict[str, Any]], results_by_task: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    returns: List[float] = []
    drawdowns: List[float] = []
    win_rates: List[float] = []
    for task in tasks:
        status_value = str(task.get("status") or "unknown")
        counts[status_value] = counts.get(status_value, 0) + 1
        result = results_by_task.get(task.get("task_id"))
        metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
        for target, key in ((returns, "total_return"), (drawdowns, "max_drawdown"), (win_rates, "win_rate")):
            val = _metric_value(metrics, key)
            if val is not None:
                target.append(val)

    def avg(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    def median(values: List[float]) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    total = len(tasks)
    return {
        "total": total,
        "status_counts": counts,
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "running": counts.get("pending", 0) + counts.get("claimed", 0) + counts.get("running", 0),
        "avg_total_return": avg(returns),
        "median_total_return": median(returns),
        "avg_max_drawdown": avg(drawdowns),
        "median_win_rate": median(win_rates),
        "status": _batch_status_from_counts(total, counts),
    }


def _serialize_batch(batch: Dict[str, Any], summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {k: v for k, v in batch.items() if k != "_id"}
    out["id"] = str(batch.get("_id")) if batch.get("_id") else None
    if summary is not None:
        out["summary"] = summary
        out["status"] = summary.get("status", out.get("status"))
    return _clean_for_response(out)


def require_backtest_worker_token(
    x_backtest_worker_token: Optional[str] = Header(None, alias="X-Backtest-Worker-Token"),
) -> bool:
    """Shared secret for backtest-worker internal APIs."""
    expected = os.getenv("BACKTEST_WORKER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BACKTEST_WORKER_TOKEN is not configured",
        )
    if not x_backtest_worker_token or x_backtest_worker_token.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Backtest-Worker-Token",
        )
    return True

@router.post("/tasks", response_model=BacktestTaskResponse)
async def create_backtest_task(
    task: BacktestTaskCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new backtest task
    
    User submits a backtest request with symbol, strategy, date range.
    Task is created in 'pending' status waiting for worker to pick up.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    asset_type = _normalize_asset_type(task.asset_type)
    
    symbol = _normalize_symbol(task.symbol, asset_type)
    task_doc = _build_backtest_task_doc(
        current_user=current_user,
        symbol=symbol,
        asset_type=asset_type,
        strategy_key=task.strategy_key,
        preset=task.preset,
        strategy_params=task.strategy_params,
        start_date=task.start_date,
        end_date=task.end_date,
        initial_cash=task.initial_cash,
    )
    
    result = backtest_tasks_col.insert_one(task_doc)
    
    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to create backtest task")
    
    stock_name = _lookup_symbol_name(collections, task_doc["symbol"], asset_type)

    # Return response
    return BacktestTaskResponse(
        task_id=task_doc["task_id"],
        user_id=str(task_doc["user_id"]),
        symbol=task_doc["symbol"],
        asset_type=asset_type,
        stock_name=stock_name,
        strategy_key=task_doc["strategy_key"],
        preset=task_doc.get("preset"),
        strategy_params=task_doc["strategy_params"],
        start_date=task_doc["start_date"],
        end_date=task_doc["end_date"],
        initial_cash=task_doc["initial_cash"],
        status=task_doc["status"],
        worker_id=task_doc["worker_id"],
        created_at=task_doc["created_at"]
    )


@router.post("/batches")
async def create_backtest_batch(
    task: BacktestBatchCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a batch experiment by expanding one request into child backtest tasks."""
    collections = get_db_collections()
    backtest_batches_col = collections["backtest_batches"]
    backtest_tasks_col = collections["backtest_tasks"]
    asset_type = _normalize_asset_type(task.asset_type)
    symbols = _resolve_batch_symbols(collections, task)
    if task.limit_symbols and task.limit_symbols > 0:
        symbols = symbols[:task.limit_symbols]
    batch_id = _new_batch_id()
    name = task.name or f"{task.strategy_key} batch {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    now = datetime.now()

    batch_doc = {
        "batch_id": batch_id,
        "user_id": current_user["id"],
        "name": name,
        "universe_type": (task.universe_type or "manual").strip().lower(),
        "universe_value": task.universe_value,
        "symbols_count": len(symbols),
        "limit_symbols": task.limit_symbols,
        "symbols_preview": symbols[:20],
        "asset_type": asset_type,
        "strategy_key": task.strategy_key,
        "preset": task.preset,
        "strategy_params": task.strategy_params or {},
        "start_date": task.start_date,
        "end_date": task.end_date,
        "initial_cash": task.initial_cash,
        "status": "pending",
        "summary": {"total": len(symbols), "status_counts": {"pending": len(symbols)}},
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }

    child_tasks = [
        _build_backtest_task_doc(
            current_user=current_user,
            symbol=symbol,
            asset_type=asset_type,
            strategy_key=task.strategy_key,
            preset=task.preset,
            strategy_params=task.strategy_params,
            start_date=task.start_date,
            end_date=task.end_date,
            initial_cash=task.initial_cash,
            batch_id=batch_id,
            batch_name=name,
            batch_index=i,
        )
        for i, symbol in enumerate(symbols)
    ]
    backtest_batches_col.insert_one(batch_doc)
    if child_tasks:
        backtest_tasks_col.insert_many(child_tasks)

    return {
        **_serialize_batch(batch_doc),
        "created_tasks": len(child_tasks),
        "task_ids": [t["task_id"] for t in child_tasks],
    }


@router.get("/batches")
async def list_backtest_batches(
    status: Optional[str] = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    query = {"user_id": current_user["id"]}
    if status:
        query["status"] = status
    batches = list(collections["backtest_batches"].find(query).sort("created_at", -1).limit(limit))
    return [_serialize_batch(b) for b in batches]


@router.get("/batches/{batch_id}")
async def get_backtest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    tasks = list(collections["backtest_tasks"].find({"batch_id": batch_id, "user_id": current_user["id"]}))
    results = list(collections["backtest_results"].find({"task_id": {"$in": [t["task_id"] for t in tasks]}}))
    results_by_task = {r.get("task_id"): r for r in results}
    summary = _summarize_batch(tasks, results_by_task)
    collections["backtest_batches"].update_one(
        {"batch_id": batch_id},
        {"$set": {"summary": summary, "status": summary["status"], "updated_at": datetime.now()}},
    )
    return {**_serialize_batch(batch, summary), "tasks_count": len(tasks)}


@router.get("/batches/{batch_id}/results")
async def get_backtest_batch_results(
    batch_id: str,
    status: Optional[str] = None,
    sort_by: str = "total_return",
    order: str = "desc",
    limit: int = 200,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    query = {"batch_id": batch_id, "user_id": current_user["id"]}
    if status:
        query["status"] = status
    tasks = list(collections["backtest_tasks"].find(query).sort("batch_index", 1))
    task_ids = [t["task_id"] for t in tasks]
    results = list(collections["backtest_results"].find({"task_id": {"$in": task_ids}}))
    results_by_task = {r.get("task_id"): r for r in results}
    rows = []
    for task in tasks:
        result = results_by_task.get(task["task_id"]) or {}
        metrics = result.get("metrics", {}) or {}
        rows.append({
            "task_id": task["task_id"],
            "batch_id": batch_id,
            "symbol": task.get("symbol"),
            "asset_type": _normalize_asset_type(task.get("asset_type")),
            "stock_name": _lookup_symbol_name(collections, task.get("symbol"), _normalize_asset_type(task.get("asset_type"))),
            "status": task.get("status"),
            "error_message": task.get("error_message"),
            "strategy_key": task.get("strategy_key"),
            "preset": task.get("preset"),
            "metrics": metrics,
            "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "win_rate": metrics.get("win_rate"),
            "total_trades": metrics.get("total_trades"),
        })
    reverse = order.lower() != "asc"
    present = [r for r in rows if r.get(sort_by) is not None]
    missing = [r for r in rows if r.get(sort_by) is None]
    present.sort(key=lambda r: r.get(sort_by), reverse=reverse)
    rows = present + missing
    total = len(rows)
    return _clean_for_response({"batch_id": batch_id, "total": total, "offset": offset, "limit": limit, "rows": rows[offset:offset + limit]})


@router.delete("/batches/{batch_id}")
async def delete_backtest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a batch experiment and all child tasks/results for the current user."""
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")

    cancelled_active = collections["backtest_tasks"].update_many(
        {
            "batch_id": batch_id,
            "user_id": current_user["id"],
            "status": {"$in": ["pending", "claimed", "running"]},
        },
        {"$set": {"status": "cancelled", "completed_at": datetime.now()}},
    ).modified_count

    tasks = list(collections["backtest_tasks"].find({"batch_id": batch_id, "user_id": current_user["id"]}))
    task_ids = [t["task_id"] for t in tasks if t.get("task_id")]
    if task_ids:
        collections["backtest_results"].delete_many({"task_id": {"$in": task_ids}})
    deleted_tasks = collections["backtest_tasks"].delete_many(
        {"batch_id": batch_id, "user_id": current_user["id"]}
    ).deleted_count
    collections["backtest_batches"].delete_one({"batch_id": batch_id, "user_id": current_user["id"]})

    try:
        get_analyzer_db()["backtest_batch_reviews"].delete_many({"batch_id": batch_id})
    except Exception:
        pass

    return _clean_for_response(
        {
            "message": "Backtest batch deleted",
            "batch_id": batch_id,
            "deleted_tasks": deleted_tasks,
            "cancelled_active": cancelled_active,
        }
    )


@router.post("/batches/{batch_id}/cancel")
async def cancel_backtest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    result = collections["backtest_tasks"].update_many(
        {"batch_id": batch_id, "user_id": current_user["id"], "status": {"$in": ["pending", "claimed", "running"]}},
        {"$set": {"status": "cancelled", "completed_at": datetime.now()}},
    )
    if result.modified_count <= 0:
        return {
            "message": "No active tasks to cancel (batch may already be completed)",
            "batch_id": batch_id,
            "cancelled_tasks": 0,
        }
    collections["backtest_batches"].update_one(
        {"batch_id": batch_id},
        {"$set": {"status": "cancelled", "updated_at": datetime.now()}},
    )
    return {"message": "Batch cancelled", "batch_id": batch_id, "cancelled_tasks": result.modified_count}


@router.post("/batches/{batch_id}/retry-failed")
async def retry_failed_backtest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    result = collections["backtest_tasks"].update_many(
        {"batch_id": batch_id, "user_id": current_user["id"], "status": {"$in": ["failed", "cancelled"]}},
        {"$set": {"status": "pending", "worker_id": None, "started_at": None, "completed_at": None, "error_message": None}},
    )
    collections["backtest_batches"].update_one({"batch_id": batch_id}, {"$set": {"status": "running", "updated_at": datetime.now()}})
    return {"message": "Failed tasks reset", "batch_id": batch_id, "reset_tasks": result.modified_count}


@router.post("/batches/{batch_id}/rerun")
async def rerun_backtest_batch(
    batch_id: str,
    payload: BacktestBatchRerunCreate,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    query: Dict[str, Any] = {"batch_id": batch_id, "user_id": current_user["id"]}
    if payload.only_statuses:
        query["status"] = {"$in": payload.only_statuses}
    source_tasks = list(collections["backtest_tasks"].find(query).sort("batch_index", 1))
    symbols = [t["symbol"] for t in source_tasks]
    new_payload = BacktestBatchCreate(
        name=payload.name or f"{batch.get('name', batch_id)} rerun",
        symbols=symbols,
        asset_type=batch.get("asset_type", "stock"),
        universe_type="manual",
        universe_value=f"rerun:{batch_id}",
        strategy_key=payload.strategy_key or batch["strategy_key"],
        preset=payload.preset if payload.preset is not None else batch.get("preset"),
        strategy_params=payload.strategy_params if payload.strategy_params is not None else batch.get("strategy_params", {}),
        start_date=payload.start_date or batch["start_date"],
        end_date=payload.end_date or batch["end_date"],
        initial_cash=payload.initial_cash or batch.get("initial_cash", 1000000.0),
        limit_symbols=0,
    )
    return await create_backtest_batch(new_payload, current_user)


@router.post("/batches/{batch_id}/review")
async def create_backtest_batch_review(
    batch_id: str,
    payload: BacktestBatchReviewCreate,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="backtest_batch_review",
        priority=20,
        params={
            "batch_id": batch_id,
            "objective": payload.objective,
            "include_top_n": payload.include_top_n,
            "include_bottom_n": payload.include_bottom_n,
            "include_failures": payload.include_failures,
        },
        source="strategy_lab",
    )
    result = collections["analysis_tasks"].insert_one(task_doc)
    return {"message": "Backtest batch review queued", "analysis_task_id": str(result.inserted_id), "batch_id": batch_id}


@router.get("/batches/{batch_id}/reviews")
async def list_backtest_batch_reviews(
    batch_id: str,
    limit: int = 10,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")
    analyzer_collections = get_analyzer_db()
    reviews = list(
        analyzer_collections["backtest_batch_reviews"]
        .find({"batch_id": batch_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_clean_for_response({k: v for k, v in review.items() if k != "_id"}) for review in reviews]


@router.get("/tasks", response_model=List[BacktestTaskResponse])
async def list_backtest_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    include_batch: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """List user's backtest tasks
    
    Returns tasks created by the current user, optionally filtered by status.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]

    query = {"user_id": current_user["id"]}
    if not include_batch:
        query["batch_id"] = {"$exists": False}
    if status:
        query["status"] = status

    tasks = list(backtest_tasks_col.find(query).sort("created_at", -1).limit(limit))

    symbol_to_name: Dict[str, str] = {}
    for t in tasks:
        sym = t.get("symbol")
        asset_type = _normalize_asset_type(t.get("asset_type"))
        cache_key = f"{asset_type}:{sym}"
        if not sym or cache_key in symbol_to_name:
            continue
        name = _lookup_symbol_name(collections, sym, asset_type)
        if name:
            symbol_to_name[cache_key] = name

    return [
        BacktestTaskResponse(
            task_id=task["task_id"],
            user_id=str(task.get("user_id", "")),
            symbol=task["symbol"],
            asset_type=_normalize_asset_type(task.get("asset_type")),
            stock_name=symbol_to_name.get(f"{_normalize_asset_type(task.get('asset_type'))}:{task['symbol']}"),
            strategy_key=task["strategy_key"],
            preset=task.get("preset"),
            strategy_params=task.get("strategy_params", {}) or {},
            start_date=task["start_date"],
            end_date=task["end_date"],
            initial_cash=task.get("initial_cash", 1000000.0),
            status=task["status"],
            worker_id=task.get("worker_id"),
            created_at=task.get("created_at"),
            started_at=task.get("started_at"),
            completed_at=task.get("completed_at"),
            error_message=task.get("error_message"),
        )
        for task in tasks
    ]


@router.get("/tasks/{task_id}", response_model=BacktestTaskResponse)
async def get_backtest_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get backtest task details"""
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]

    task = backtest_tasks_col.find_one({
        "task_id": task_id,
        "user_id": current_user["id"]
    })

    if not task:
        raise HTTPException(status_code=404, detail="Backtest task not found")

    asset_type = _normalize_asset_type(task.get("asset_type"))
    stock_name = _lookup_symbol_name(collections, task.get("symbol"), asset_type)

    return BacktestTaskResponse(
        task_id=task.get("task_id") or task_id,
        user_id=str(task.get("user_id", "")),
        symbol=task.get("symbol"),
        asset_type=asset_type,
        stock_name=stock_name,
        strategy_key=task.get("strategy_key"),
        preset=task.get("preset"),
        strategy_params=task.get("strategy_params", {}) or {},
        start_date=task.get("start_date"),
        end_date=task.get("end_date"),
        initial_cash=task.get("initial_cash", 1000000.0),
        status=task.get("status"),
        worker_id=task.get("worker_id"),
        created_at=task.get("created_at"),
        started_at=task.get("started_at"),
        completed_at=task.get("completed_at"),
        error_message=task.get("error_message"),
    )


@router.delete("/tasks/{task_id}")
async def delete_backtest_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Cancel or delete a backtest task
    
    Can only delete tasks in 'pending', 'completed', or 'failed' status.
    Running tasks should be cancelled first.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    backtest_results_col = collections["backtest_results"]
    
    task = backtest_tasks_col.find_one({
        "task_id": task_id,
        "user_id": current_user["id"]
    })
    
    if not task:
        raise HTTPException(status_code=404, detail="Backtest task not found")
    
    # Check if task can be deleted
    if task["status"] in ["claimed", "running"]:
        # Mark as cancelled instead of deleting
        backtest_tasks_col.update_one(
            {"task_id": task_id},
            {"$set": {"status": "cancelled"}}
        )
        return {"message": "Backtest task cancelled"}
    
    # Delete task and its results
    backtest_tasks_col.delete_one({"task_id": task_id})
    backtest_results_col.delete_one({"task_id": task_id})
    
    return {"message": "Backtest task deleted"}


@router.post("/tasks/{task_id}/reset")
async def reset_backtest_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Reset a cancelled/failed task back to pending for re-execution.

    Clears worker info and timing fields, removes stale results, and sets status=pending.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    backtest_results_col = collections["backtest_results"]

    task = backtest_tasks_col.find_one({
        "task_id": task_id,
        "user_id": current_user["id"]
    })

    if not task:
        raise HTTPException(status_code=404, detail="Backtest task not found")

    if task.get("status") not in ["cancelled", "failed"]:
        raise HTTPException(status_code=400, detail=f"Only cancelled/failed tasks can be reset (current: {task.get('status')})")

    backtest_tasks_col.update_one(
        {"task_id": task_id},
        {"$set": {
            "status": "pending",
            "worker_id": None,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
        }}
    )

    # Remove stale results so rerun writes fresh ones
    backtest_results_col.delete_one({"task_id": task_id})

    return {"message": "Backtest task reset to pending", "task_id": task_id}


# ============================================================================
# User APIs - Results
# ============================================================================

@router.get("/results/{task_id}", response_model=BacktestResultResponse)
async def get_backtest_result(
    task_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get backtest results
    
    Returns metrics, trades, and equity curve data for rendering.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    backtest_results_col = collections["backtest_results"]
    
    # Verify task ownership
    task = backtest_tasks_col.find_one({
        "task_id": task_id,
        "user_id": current_user["id"]
    })
    
    if not task:
        raise HTTPException(status_code=404, detail="Backtest task not found")
    
    if task["status"] != "completed":
        raise HTTPException(
            status_code=400, 
            detail=f"Backtest not completed yet. Current status: {task['status']}"
        )
    
    # Get results
    result = backtest_results_col.find_one({"task_id": task_id})
    
    if not result:
        raise HTTPException(status_code=404, detail="Backtest results not found")
    
    return BacktestResultResponse(
        task_id=result["task_id"],
        user_id=str(result["user_id"]),
        symbol=result["symbol"],
        asset_type=_normalize_asset_type(result.get("asset_type") or task.get("asset_type")),
        strategy_key=result["strategy_key"],
        strategy_params=result.get("strategy_params") or task.get("strategy_params"),  # 从 result 或 task 获取参数
        metrics=BacktestResultMetrics(**result.get("metrics", {})),
        trades=[BacktestTrade(**trade) for trade in result.get("trades", [])],
        equity_curve=[BacktestEquityPoint(**point) for point in result.get("equity_curve", [])],
        created_at=result["created_at"]
    )


# ============================================================================
# Worker APIs - Task Execution
# ============================================================================

@router.get("/tasks/pending/poll")
async def poll_pending_tasks(
    limit: int = 10,
    _worker_auth: bool = Depends(require_backtest_worker_token)
):
    """Poll for pending backtest tasks (Worker endpoint)
    
    Workers call this endpoint to get tasks waiting to be executed.
    Returns tasks in 'pending' status.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]

    # Poll is a hot path: only read valid pending tasks. Legacy malformed rows are
    # excluded here; run tools/cleanup_invalid_backtest_tasks.py to quarantine them.
    tasks = list(
        backtest_tasks_col
        .find({
            "status": "pending",
            "start_date": {"$nin": ["", None]},
            "end_date": {"$nin": ["", None]},
        })
        .sort("created_at", 1)  # FIFO
        .limit(limit)
    )
    
    return [
        {
            "task_id": task["task_id"],
            "user_id": str(task["user_id"]),
            "symbol": task["symbol"],
            "asset_type": _normalize_asset_type(task.get("asset_type")),
            "strategy_key": task["strategy_key"],
            "preset": task.get("preset"),
            "strategy_params": task.get("strategy_params", {}),
            "start_date": task["start_date"],
            "end_date": task["end_date"],
            "initial_cash": task.get("initial_cash", 1000000.0),
            "created_at": task["created_at"].isoformat()
        }
        for task in tasks
    ]


@router.post("/tasks/{task_id}/claim")
async def claim_backtest_task(
    task_id: str,
    worker_id: str,
    _worker_auth: bool = Depends(require_backtest_worker_token)
):
    """Claim a backtest task (Worker endpoint)
    
    Worker claims a task before execution to prevent duplicate processing.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    
    # Try to claim task atomically
    result = backtest_tasks_col.update_one(
        {"task_id": task_id, "status": "pending"},
        {
            "$set": {
                "status": "claimed",
                "worker_id": worker_id,
                "started_at": datetime.now()
            }
        }
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=409, 
            detail="Task already claimed or not in pending status"
        )
    
    return {"message": "Task claimed successfully", "task_id": task_id}


@router.post("/tasks/{task_id}/report")
async def report_backtest_result(
    task_id: str,
    result: BacktestResultReport,
    _worker_auth: bool = Depends(require_backtest_worker_token)
):
    """Report backtest results (Worker endpoint)
    
    Worker reports execution results including metrics, trades, and equity curve.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    backtest_results_col = collections["backtest_results"]
    
    # Verify task exists and is claimed
    task = backtest_tasks_col.find_one({"task_id": task_id})
    
    if not task:
        raise HTTPException(status_code=404, detail="Backtest task not found")
    
    if task["status"] not in ["claimed", "running"]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot report results for task in status: {task['status']}"
        )
    
    # Store results
    result_doc = _clean_for_mongo({
        "task_id": task_id,
        "user_id": task["user_id"],
        "symbol": task["symbol"],
        "asset_type": _normalize_asset_type(task.get("asset_type")),
        "strategy_key": task["strategy_key"],
        "preset": task.get("preset"),
        "strategy_params": task.get("strategy_params", {}),
        "batch_id": task.get("batch_id"),
        "metrics": result.metrics.model_dump(),
        "trades": [trade.model_dump() for trade in result.trades],
        "equity_curve": [point.model_dump() for point in result.equity_curve],
        "created_at": datetime.now()
    })
    
    # Upsert results
    try:
        backtest_results_col.update_one(
            {"task_id": task_id},
            {"$set": result_doc},
            upsert=True
        )

        # Update task status to completed
        backtest_tasks_col.update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "completed",
                    "completed_at": datetime.now()
                }
            }
        )
    except Exception as exc:
        backtest_tasks_col.update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "failed",
                    "error_message": f"Failed to persist backtest result: {exc}",
                    "completed_at": datetime.now(),
                }
            },
        )
        raise HTTPException(status_code=500, detail=f"Failed to persist backtest result: {exc}") from exc
    
    return {"message": "Results reported successfully", "task_id": task_id}


@router.post("/tasks/{task_id}/fail")
async def report_backtest_failure(
    task_id: str,
    error_message: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _worker_auth: bool = Depends(require_backtest_worker_token)
):
    """Report backtest failure (Worker endpoint)
    
    Worker reports task execution failure with error details.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    message = error_message or (payload or {}).get("error_message") or (payload or {}).get("error") or "Backtest failed"
    
    result = backtest_tasks_col.update_one(
        {"task_id": task_id},
        {
            "$set": {
                "status": "failed",
                "error_message": message,
                "completed_at": datetime.now()
            }
        }
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Backtest task not found")
    
    return {"message": "Failure reported", "task_id": task_id}


from routers.strategy_lab_loops import router as strategy_lab_loops_router

router.include_router(strategy_lab_loops_router)
