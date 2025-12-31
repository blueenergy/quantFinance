"""Backtesting Task Management API

This module provides APIs for users to create, manage, and retrieve backtesting tasks.
Workers can poll for pending tasks, claim them, and report results.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from bson import ObjectId

from auth import get_current_user
from db import get_db_collections

router = APIRouter(prefix="/backtest", tags=["backtest"])


# ============================================================================
# Request/Response Models
# ============================================================================

class BacktestTaskCreate(BaseModel):
    """Request model for creating a backtest task"""
    symbol: str = Field(..., description="Stock symbol (e.g., 000858)")
    strategy_key: str = Field(..., description="Strategy key from STRATEGY_MAP")
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Strategy parameters")
    start_date: str = Field(..., description="Backtest start date (YYYYMMDD)")
    end_date: str = Field(..., description="Backtest end date (YYYYMMDD)")
    initial_cash: float = Field(default=1000000.0, description="Initial cash for backtest")
    
    class Config:
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


class BacktestTaskResponse(BaseModel):
    """Response model for backtest task"""
    task_id: str
    user_id: str
    symbol: str
    strategy_key: str
    strategy_params: Dict[str, Any]
    start_date: str
    end_date: str
    initial_cash: float
    status: str  # pending, claimed, running, completed, failed, cancelled
    worker_id: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


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
    strategy_key: str
    strategy_params: Optional[Dict[str, Any]] = None  # 添加策略参数
    metrics: BacktestResultMetrics
    trades: List[BacktestTrade]
    equity_curve: List[BacktestEquityPoint]
    created_at: datetime


# ============================================================================
# User APIs - Task Management
# ============================================================================

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
    
    # Generate task ID
    task_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task.symbol}"
    
    # Create task document
    task_doc = {
        "task_id": task_id,
        "user_id": current_user["id"],
        "symbol": task.symbol,
        "strategy_key": task.strategy_key,
        "strategy_params": task.strategy_params,
        "start_date": task.start_date,
        "end_date": task.end_date,
        "initial_cash": task.initial_cash,
        "status": "pending",
        "worker_id": None,
        "created_at": datetime.now(),
        "started_at": None,
        "completed_at": None,
        "error_message": None
    }
    
    result = backtest_tasks_col.insert_one(task_doc)
    
    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to create backtest task")
    
    # Return response
    return BacktestTaskResponse(
        task_id=task_doc["task_id"],
        user_id=str(task_doc["user_id"]),
        symbol=task_doc["symbol"],
        strategy_key=task_doc["strategy_key"],
        strategy_params=task_doc["strategy_params"],
        start_date=task_doc["start_date"],
        end_date=task_doc["end_date"],
        initial_cash=task_doc["initial_cash"],
        status=task_doc["status"],
        worker_id=task_doc["worker_id"],
        created_at=task_doc["created_at"]
    )


@router.get("/tasks", response_model=List[BacktestTaskResponse])
async def list_backtest_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """List user's backtest tasks
    
    Returns tasks created by the current user, optionally filtered by status.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    
    query = {"user_id": current_user["id"]}
    if status:
        query["status"] = status
    
    tasks = list(backtest_tasks_col.find(query).sort("created_at", -1).limit(limit))
    
    return [
        BacktestTaskResponse(
            task_id=task["task_id"],
            user_id=str(task["user_id"]),
            symbol=task["symbol"],
            strategy_key=task["strategy_key"],
            strategy_params=task.get("strategy_params", {}),
            start_date=task["start_date"],
            end_date=task["end_date"],
            initial_cash=task.get("initial_cash", 1000000.0),
            status=task["status"],
            worker_id=task.get("worker_id"),
            created_at=task["created_at"],
            started_at=task.get("started_at"),
            completed_at=task.get("completed_at"),
            error_message=task.get("error_message")
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
    
    return BacktestTaskResponse(
        task_id=task["task_id"],
        user_id=str(task["user_id"]),
        symbol=task["symbol"],
        strategy_key=task["strategy_key"],
        strategy_params=task.get("strategy_params", {}),
        start_date=task["start_date"],
        end_date=task["end_date"],
        initial_cash=task.get("initial_cash", 1000000.0),
        status=task["status"],
        worker_id=task.get("worker_id"),
        created_at=task["created_at"],
        started_at=task.get("started_at"),
        completed_at=task.get("completed_at"),
        error_message=task.get("error_message")
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
    current_user: dict = Depends(get_current_user)
):
    """Poll for pending backtest tasks (Worker endpoint)
    
    Workers call this endpoint to get tasks waiting to be executed.
    Returns tasks in 'pending' status.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    
    tasks = list(
        backtest_tasks_col
        .find({"status": "pending"})
        .sort("created_at", 1)  # FIFO
        .limit(limit)
    )
    
    return [
        {
            "task_id": task["task_id"],
            "user_id": str(task["user_id"]),
            "symbol": task["symbol"],
            "strategy_key": task["strategy_key"],
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
    result_doc = {
        "task_id": task_id,
        "user_id": task["user_id"],
        "symbol": task["symbol"],
        "strategy_key": task["strategy_key"],
        "metrics": result.metrics.model_dump(),
        "trades": [trade.model_dump() for trade in result.trades],
        "equity_curve": [point.model_dump() for point in result.equity_curve],
        "created_at": datetime.now()
    }
    
    # Upsert results
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
    
    return {"message": "Results reported successfully", "task_id": task_id}


@router.post("/tasks/{task_id}/fail")
async def report_backtest_failure(
    task_id: str,
    error_message: str,
    current_user: dict = Depends(get_current_user)
):
    """Report backtest failure (Worker endpoint)
    
    Worker reports task execution failure with error details.
    """
    collections = get_db_collections()
    backtest_tasks_col = collections["backtest_tasks"]
    
    result = backtest_tasks_col.update_one(
        {"task_id": task_id},
        {
            "$set": {
                "status": "failed",
                "error_message": error_message,
                "completed_at": datetime.now()
            }
        }
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Backtest task not found")
    
    return {"message": "Failure reported", "task_id": task_id}
