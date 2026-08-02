"""Strategy Lab auto-iteration loop APIs (mounted under /backtest/loops)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import get_current_user
from services.backtest_batch_schemas import BacktestBatchCreate
from services import strategy_lab_loop as loop_service

router = APIRouter(prefix="/loops", tags=["strategy-lab-loops"])


class LoopStopRules(BaseModel):
    min_improvement: float = Field(default=0.0, ge=0)
    no_improvement_rounds: int = Field(default=2, ge=1, le=10)
    max_drawdown_limit: Optional[float] = Field(default=None, ge=0, le=1)
    min_completion_rate: float = Field(default=0.5, ge=0, le=1)


class LoopReviewOptions(BaseModel):
    objective: str = Field(default="提高收益并控制回撤")
    include_top_n: int = Field(default=20, ge=1, le=100)
    include_bottom_n: int = Field(default=20, ge=1, le=100)
    include_failures: bool = True


class StrategyLabLoopCreate(BaseModel):
    name: Optional[str] = None
    max_iterations: int = Field(default=3, ge=1, le=20)
    objective: Optional[str] = None
    stop_rules: Optional[LoopStopRules] = None
    review_options: Optional[LoopReviewOptions] = None
    batch_config: Optional[BacktestBatchCreate] = None
    source_batch_id: Optional[str] = Field(
        default=None,
        description="Start loop from an existing batch instead of creating a new one",
    )


def _loop_create_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        return str(detail)
    return str(exc)


def _raise_loop_status_error(exc: ValueError) -> None:
    detail = str(exc)
    if detail.startswith("Invalid loop status transition:"):
        raise HTTPException(status_code=409, detail=detail) from exc
    raise HTTPException(status_code=400, detail=detail) from exc


@router.post("")
async def create_strategy_lab_loop(
    request: Request,
    payload: StrategyLabLoopCreate,
    current_user: dict = Depends(get_current_user),
):
    if payload.batch_config is None and not (payload.source_batch_id or "").strip():
        raise HTTPException(status_code=400, detail="batch_config or source_batch_id is required")
    try:
        doc = loop_service.create_loop(
            user_id=current_user["id"],
            batch_config=payload.batch_config.model_dump() if payload.batch_config else None,
            source_batch_id=(payload.source_batch_id or "").strip() or None,
            max_iterations=payload.max_iterations,
            objective=payload.objective,
            stop_rules=payload.stop_rules.model_dump() if payload.stop_rules else None,
            review_options=payload.review_options.model_dump() if payload.review_options else None,
            name=payload.name,
        )
    except (ValueError, HTTPException) as exc:
        raise HTTPException(status_code=400, detail=_loop_create_error_detail(exc)) from exc
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception(
            "create_strategy_lab_loop failed user=%s path=%s",
            current_user.get("id"),
            request.url.path,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return doc


@router.get("")
async def list_strategy_lab_loops(
    status: Optional[str] = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    return loop_service.list_loops(current_user["id"], status=status, limit=limit)


@router.get("/health")
async def get_strategy_lab_loop_health(
    current_user: dict = Depends(get_current_user),
):
    """Scheduler/tick status for Strategy Lab auto-iteration UI."""
    return loop_service.get_loop_runtime_health(current_user["id"])


@router.get("/{loop_id}/summary")
async def get_strategy_lab_loop_summary(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Cross-iteration metrics and per-round AI review summaries for a completed or running loop."""
    report = loop_service.build_loop_summary_report(loop_id, current_user["id"])
    if not report:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return report


@router.get("/{loop_id}")
async def get_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    doc = loop_service.get_loop(loop_id, current_user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return doc


@router.post("/{loop_id}/pause")
async def pause_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        changed = loop_service.set_loop_status(loop_id, current_user["id"], loop_service.LOOP_STATUS_PAUSED)
    except ValueError as exc:
        _raise_loop_status_error(exc)
    if not changed:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return {"message": "Loop paused", "loop_id": loop_id}


@router.post("/{loop_id}/resume")
async def resume_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        changed = loop_service.set_loop_status(loop_id, current_user["id"], loop_service.LOOP_STATUS_RUNNING)
    except ValueError as exc:
        _raise_loop_status_error(exc)
    if not changed:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return {"message": "Loop resumed", "loop_id": loop_id}


@router.post("/{loop_id}/cancel")
async def cancel_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        changed = loop_service.set_loop_status(loop_id, current_user["id"], loop_service.LOOP_STATUS_CANCELLED)
    except ValueError as exc:
        _raise_loop_status_error(exc)
    if not changed:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return {"message": "Loop cancelled", "loop_id": loop_id}


@router.delete("/{loop_id}")
async def delete_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a historical loop record. Linked batch experiments are kept."""
    try:
        return loop_service.delete_loop(loop_id, current_user["id"])
    except ValueError as exc:
        detail = str(exc)
        if detail.startswith("Strategy lab loop not found:"):
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/{loop_id}/advance")
async def advance_strategy_lab_loop(
    loop_id: str,
    current_user: dict = Depends(get_current_user),
):
    doc = loop_service.get_loop(loop_id, current_user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    return loop_service.advance_loop(loop_id)


@router.post("/{loop_id}/final-report")
async def queue_strategy_lab_loop_final_report(
    loop_id: str,
    force: bool = Query(False, description="Re-queue even if a report is pending or completed"),
    current_user: dict = Depends(get_current_user),
):
    """Queue (or re-queue) cross-iteration AI conclusion for a completed loop."""
    from db import get_db_collections

    collections = get_db_collections()
    loop = collections["strategy_lab_loops"].find_one(
        {"loop_id": loop_id, "user_id": current_user["id"]},
    )
    if not loop:
        raise HTTPException(status_code=404, detail="Strategy lab loop not found")
    if loop.get("status") != loop_service.LOOP_STATUS_COMPLETED:
        raise HTTPException(
            status_code=400,
            detail="Final report is only available for completed loops",
        )
    existing_task_id = loop.get("final_report_task_id")
    try:
        task_id = loop_service.queue_loop_final_report_sync(
            collections,
            current_user,
            loop_id,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    created = str(task_id) != str(existing_task_id or "")
    return {
        "message": "Loop final report queued" if created else "Loop final report already pending",
        "loop_id": loop_id,
        "analysis_task_id": task_id,
        "created": created,
        "final_report_status": loop_service.LOOP_FINAL_REPORT_STATUS_PENDING,
    }
