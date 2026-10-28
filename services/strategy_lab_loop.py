"""Strategy Lab auto-iteration loop: batch → AI review → apply params → rerun.

State machine (current_step):
  wait_batch → wait_review → (apply_rerun) → wait_batch … until stop or max_iterations.

Advanced by quant-scheduler interval job and optional manual POST /advance.
"""
from __future__ import annotations

import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from db import get_analyzer_db, get_db_collections
from services.backtest_batch_schemas import BacktestBatchCreate, BacktestBatchRerunCreate
from services.backtest_batch_service import (
    create_batch,
    is_compare_batch,
    refresh_batch_summary,
)
from services.analysis_tasks import (
    RUNNING_STATUSES,
    TERMINAL_STATUSES,
    build_task_doc,
    normalize_task_status,
)

LOOP_FINAL_REPORT_STATUS_PENDING = "pending"
LOOP_FINAL_REPORT_STATUS_COMPLETED = "completed"
LOOP_FINAL_REPORT_STATUS_FAILED = "failed"


def report_has_content(report: Any) -> bool:
    if not report or not isinstance(report, dict):
        return False
    if str(report.get("summary") or "").strip():
        return True
    for key in (
        "main_findings",
        "iteration_comparison",
        "lessons_learned",
        "next_steps",
        "risk_warnings",
    ):
        value = report.get(key)
        if isinstance(value, list) and len(value) > 0:
            return True
    params = report.get("recommended_params")
    return isinstance(params, dict) and len(params) > 0


def _load_final_report_from_analyzer_store(loop_id: str) -> Optional[Dict[str, Any]]:
    try:
        reports_col = get_analyzer_db().get("strategy_lab_loop_reports")
        if reports_col is None:
            return None
        doc = reports_col.find_one({"loop_id": loop_id}, sort=[("created_at", -1)])
        if not doc:
            return None
        report = doc.get("report")
        return report if report_has_content(report) else None
    except Exception as exc:
        log.warning("[StrategyLabLoop] load report from analyzer store failed: %s", exc)
        return None


def _load_final_report_from_task(
    collections: Dict[str, Any],
    loop: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    task_id = loop.get("final_report_task_id")
    if not task_id:
        return None
    from bson import ObjectId

    try:
        oid = ObjectId(task_id)
    except Exception:
        return None
    task = collections["analysis_tasks"].find_one({"_id": oid})
    if not task:
        return None
    result = task.get("result")
    if not isinstance(result, dict):
        return None
    report = result.get("report")
    return report if report_has_content(report) else None


def resolve_loop_final_report(
    collections: Dict[str, Any],
    loop: Dict[str, Any],
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    """Backfill final_report on loop doc from analyzer store or analysis_tasks.result."""
    loop_id = loop.get("loop_id")
    report = loop.get("final_report")
    if not report_has_content(report):
        report = _load_final_report_from_analyzer_store(loop_id or "")
    if not report_has_content(report):
        report = _load_final_report_from_task(collections, loop)
    if not report_has_content(report):
        return loop

    loop = {**loop, "final_report": report, "final_report_status": LOOP_FINAL_REPORT_STATUS_COMPLETED}
    if persist and loop_id:
        collections["strategy_lab_loops"].update_one(
            {"loop_id": loop_id},
            {
                "$set": {
                    "final_report": report,
                    "final_report_status": LOOP_FINAL_REPORT_STATUS_COMPLETED,
                    "final_report_error": None,
                    "updated_at": datetime.now(),
                }
            },
        )
    return loop

log = logging.getLogger(__name__)

LOOP_STATUS_RUNNING = "running"
LOOP_STATUS_PAUSED = "paused"
LOOP_STATUS_COMPLETED = "completed"
LOOP_STATUS_FAILED = "failed"
LOOP_STATUS_CANCELLED = "cancelled"

STEP_WAIT_BATCH = "wait_batch"
STEP_WAIT_REVIEW = "wait_review"
STEP_COMPLETED = "completed"

TERMINAL_BATCH_STATUSES = {"completed", "partial_failed", "failed", "cancelled"}
ACTIVE_LOOP_STATUSES = {LOOP_STATUS_RUNNING}
TERMINAL_LOOP_STATUSES = {LOOP_STATUS_COMPLETED, LOOP_STATUS_FAILED, LOOP_STATUS_CANCELLED}


def _new_loop_id() -> str:
    return f"sl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


_PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_EXPERIMENT_NAME_PART_RE = re.compile(r"[^\w.-]+")
_MACHINE_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

_RESERVED_STRATEGY_PARAM_KEYS = frozenset({
    "universe_value",
    "universe_type",
    "symbols_count",
    "limit_symbols",
    "symbols_preview",
    "batch_id",
    "batch_name",
    "batch_index",
    "parent_batch_id",
    "loop_id",
    "asset_type",
    "strategy_key",
    "preset",
    "name",
    "symbols",
    "start_date",
    "end_date",
    "initial_cash",
    "symbol",
    "task_id",
    "user_id",
    "stock_name",
    "status",
    "worker_id",
    "created_at",
    "started_at",
    "completed_at",
    "error_message",
})


def is_valid_strategy_param_name(name: Any) -> bool:
    """Reject AI combo labels and batch/task metadata keys."""
    if not isinstance(name, str):
        return False
    key = name.strip()
    if not key or key in _RESERVED_STRATEGY_PARAM_KEYS:
        return False
    return bool(_PARAM_NAME_RE.match(key))


def _filter_valid_param_dict(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in params.items():
        if is_valid_strategy_param_name(key):
            out[str(key).strip()] = value
    return out


def _experiment_name_part(value: Any) -> str:
    part = str(value or "").strip()
    if part.startswith("rerun:"):
        part = part[len("rerun:") :]
    part = _EXPERIMENT_NAME_PART_RE.sub("_", part)
    return part.strip("_")


def _machine_experiment_label(value: Any) -> Optional[str]:
    label = _experiment_name_part(value)
    if not label or not _MACHINE_LABEL_RE.match(label):
        return None
    if label.lower() in {"next_experiment", "experiment", "batch", "rerun"}:
        return None
    return label


def _source_universe_value(batch: Dict[str, Any]) -> str:
    return _experiment_name_part(
        batch.get("source_universe_value")
        or batch.get("original_universe_value")
        or batch.get("universe_value")
    )


def _source_universe_type(batch: Dict[str, Any]) -> str:
    return _experiment_name_part(
        batch.get("source_universe_type")
        or batch.get("original_universe_type")
        or batch.get("universe_type")
    )


def _is_batch_ref(value: Any) -> bool:
    part = str(value or "").strip()
    return part.startswith("rerun:") or part.startswith("bb_")


def _resolve_source_universe(
    collections: Dict[str, Any],
    batch: Dict[str, Any],
    user_id: str,
) -> Dict[str, Optional[str]]:
    universe_type = batch.get("source_universe_type") or batch.get("original_universe_type") or batch.get("universe_type")
    universe_value = batch.get("source_universe_value") or batch.get("original_universe_value") or batch.get("universe_value")
    if universe_value and not _is_batch_ref(universe_value):
        return {"source_universe_type": universe_type, "source_universe_value": universe_value}

    parent_id = batch.get("parent_batch_id")
    if not parent_id and str(batch.get("universe_value") or "").startswith("rerun:"):
        parent_id = str(batch.get("universe_value"))[len("rerun:") :]

    seen = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = collections["backtest_batches"].find_one({"batch_id": parent_id, "user_id": user_id}) or {}
        if not parent:
            break
        universe_type = (
            parent.get("source_universe_type")
            or parent.get("original_universe_type")
            or parent.get("universe_type")
        )
        universe_value = (
            parent.get("source_universe_value")
            or parent.get("original_universe_value")
            or parent.get("universe_value")
        )
        if universe_value and not _is_batch_ref(universe_value):
            return {"source_universe_type": universe_type, "source_universe_value": universe_value}
        parent_id = parent.get("parent_batch_id")
        if not parent_id and str(parent.get("universe_value") or "").startswith("rerun:"):
            parent_id = str(parent.get("universe_value"))[len("rerun:") :]

    return {"source_universe_type": universe_type, "source_universe_value": universe_value}


def _loop_references_batch(loop: Dict[str, Any], batch_id: str) -> bool:
    return (
        loop.get("root_batch_id") == batch_id
        or loop.get("current_batch_id") == batch_id
        or batch_id in (loop.get("batch_chain") or [])
    )


def _find_active_loop_for_batch(collections: Dict[str, Any], user_id: str, batch_id: str) -> Optional[Dict[str, Any]]:
    loops_col = collections.get("strategy_lab_loops")
    if loops_col is None:
        return None
    for loop in loops_col.find({"user_id": user_id}):
        if loop.get("status") in {LOOP_STATUS_RUNNING, LOOP_STATUS_PAUSED} and _loop_references_batch(loop, batch_id):
            return loop
    return None


def build_structured_batch_name(batch: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> str:
    overrides = overrides or {}
    strategy_key = _experiment_name_part(overrides.get("strategy_key", batch.get("strategy_key")))
    preset = _experiment_name_part(overrides.get("preset", batch.get("preset")))
    label = _machine_experiment_label(overrides.get("label"))
    universe_value = _source_universe_value(batch)
    universe_type = _source_universe_type(batch)
    base = label or preset or strategy_key or _experiment_name_part(batch.get("name")) or "strategy_lab"
    suffix = universe_value or universe_type
    if not suffix or base.endswith(f"_{suffix}"):
        return base
    return f"{base}_{suffix}"


def normalize_suggestion_params(item: Any) -> Dict[str, Any]:
    if not item or not isinstance(item, dict):
        return {}
    for key in ("strategy_params", "params", "suggested_params"):
        val = item.get(key)
        if isinstance(val, dict):
            return _filter_valid_param_dict(val)
    name = item.get("name")
    if not is_valid_strategy_param_name(name):
        return {}
    param_name = str(name).strip()
    if isinstance(item.get("suggested_values"), list) and item["suggested_values"]:
        return {param_name: item["suggested_values"][0]}
    if "value" in item:
        return {param_name: item["value"]}
    return {}


def merge_strategy_params(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    for key, value in (patch or {}).items():
        out[key] = value
    return out


def default_stop_rules() -> Dict[str, Any]:
    return {
        "min_improvement": 0.0,
        "no_improvement_rounds": 2,
        "max_drawdown_limit": None,
        "min_completion_rate": 0.5,
    }


def default_review_options() -> Dict[str, Any]:
    return {
        "objective": "提高收益并控制回撤",
        "include_top_n": 20,
        "include_bottom_n": 20,
        "include_failures": True,
    }


def _completion_rate(summary: Dict[str, Any]) -> float:
    total = int(summary.get("total") or 0)
    if total <= 0:
        return 0.0
    return float(summary.get("completed") or 0) / total


def _median_drawdown(summary: Dict[str, Any]) -> Optional[float]:
    val = summary.get("avg_max_drawdown")
    if val is None:
        return None
    try:
        return abs(float(val))
    except (TypeError, ValueError):
        return None


def evaluate_stop_rules(
    *,
    stop_rules: Dict[str, Any],
    current_iteration: int,
    max_iterations: int,
    summary: Dict[str, Any],
    previous_summaries: List[Dict[str, Any]],
    review: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    if current_iteration >= max_iterations:
        return True, f"达到最大迭代次数 {max_iterations}"

    min_rate = float(stop_rules.get("min_completion_rate") or 0)
    rate = _completion_rate(summary)
    if rate < min_rate:
        return True, f"完成率 {rate:.1%} 低于阈值 {min_rate:.1%}"

    max_dd = stop_rules.get("max_drawdown_limit")
    if max_dd is not None:
        dd = _median_drawdown(summary)
        if dd is not None and dd > float(max_dd):
            return True, f"回撤 {dd:.2%} 超过上限 {float(max_dd):.2%}"

    if review:
        experiments = review.get("next_experiments") or []
        if isinstance(experiments, list) and len(experiments) == 0:
            if review.get("stop_iteration") or review.get("should_stop"):
                return True, "AI 建议停止迭代"

    median_return = summary.get("median_total_return")
    if median_return is not None and previous_summaries:
        prev_returns = [
            s.get("median_total_return")
            for s in previous_summaries
            if s.get("median_total_return") is not None
        ]
        if prev_returns:
            best_prev = max(prev_returns)
            min_improvement = float(stop_rules.get("min_improvement") or 0)
            try:
                if float(median_return) < float(best_prev) + min_improvement:
                    no_improve = int(stop_rules.get("no_improvement_rounds") or 2)
                    streak = 0
                    for prev in reversed(previous_summaries[-no_improve:]):
                        pr = prev.get("median_total_return")
                        if pr is None:
                            continue
                        if float(median_return) < float(pr) + min_improvement:
                            streak += 1
                    if streak >= no_improve:
                        return True, f"连续 {streak} 轮未达到最小提升 {min_improvement}"
            except (TypeError, ValueError):
                pass

    return False, ""


def create_backtest_batch_sync(
    collections: Dict[str, Any],
    current_user: dict,
    task: BacktestBatchCreate,
) -> Dict[str, Any]:
    return create_batch(
        collections,
        current_user,
        task,
        extra_batch_fields={"loop_id": None},
    )


def rerun_backtest_batch_sync(
    collections: Dict[str, Any],
    current_user: dict,
    batch_id: str,
    payload: BacktestBatchRerunCreate,
) -> Dict[str, Any]:
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": current_user["id"]})
    if not batch:
        raise ValueError(f"Backtest batch not found: {batch_id}")
    query: Dict[str, Any] = {"batch_id": batch_id, "user_id": current_user["id"]}
    if payload.only_statuses:
        query["status"] = {"$in": payload.only_statuses}
    source_tasks = list(collections["backtest_tasks"].find(query).sort("batch_index", 1))
    symbols = [t["symbol"] for t in source_tasks]
    source_universe = _resolve_source_universe(collections, batch, current_user["id"])
    source_universe_type = source_universe.get("source_universe_type")
    source_universe_value = source_universe.get("source_universe_value")
    new_payload = BacktestBatchCreate(
        name=payload.name or f"{batch.get('name', batch_id)} rerun",
        symbols=symbols,
        asset_type=batch.get("asset_type", "stock"),
        universe_type="manual",
        universe_value=source_universe_value,
        strategy_key=payload.strategy_key or batch["strategy_key"],
        preset=payload.preset if payload.preset is not None else batch.get("preset"),
        strategy_params=payload.strategy_params
        if payload.strategy_params is not None
        else batch.get("strategy_params", {}),
        start_date=payload.start_date or batch["start_date"],
        end_date=payload.end_date or batch["end_date"],
        initial_cash=payload.initial_cash or batch.get("initial_cash", 1000000.0),
        limit_symbols=0,
        regime_cash=(
            payload.regime_cash
            if payload.regime_cash is not None
            else bool(batch.get("regime_cash"))
        ),
        regime_index=(
            payload.regime_index
            if payload.regime_index is not None
            else batch.get("regime_index")
        ),
        regime_rule=(
            payload.regime_rule
            if payload.regime_rule is not None
            else batch.get("regime_rule")
        ),
    )
    result = create_backtest_batch_sync(collections, current_user, new_payload)
    new_batch_id = result.get("batch_id")
    if new_batch_id:
        collections["backtest_batches"].update_one(
            {"batch_id": new_batch_id},
            {
                "$set": {
                    "parent_batch_id": batch_id,
                    "source_universe_type": source_universe_type,
                    "source_universe_value": source_universe_value,
                }
            },
        )
    return result


def queue_loop_final_report_sync(
    collections: Dict[str, Any],
    current_user: dict,
    loop_id: str,
    *,
    force: bool = False,
) -> str:
    """Queue quantAnalyzer task to write cross-iteration AI conclusion on the loop doc."""
    loops_col = collections["strategy_lab_loops"]
    loop = loops_col.find_one({"loop_id": loop_id, "user_id": current_user["id"]})
    if not loop:
        raise ValueError(f"Strategy lab loop not found: {loop_id}")

    status = loop.get("final_report_status")
    if not force:
        existing = loop.get("final_report_task_id")
        if status == LOOP_FINAL_REPORT_STATUS_PENDING and existing:
            return str(existing)
        if (
            status == LOOP_FINAL_REPORT_STATUS_COMPLETED
            and report_has_content(loop.get("final_report"))
            and existing
        ):
            return str(existing)

    chain = loop.get("batch_chain") or []
    if len(chain) < 1:
        raise ValueError("Loop has no batch_chain; cannot build final report")

    objective = (loop.get("review_options") or {}).get("objective") or "提高收益并控制回撤"
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="strategy_lab_loop_report",
        priority=18,
        params={"loop_id": loop_id, "objective": objective},
        source="strategy_lab_loop",
    )
    result = collections["analysis_tasks"].insert_one(task_doc)
    task_id = str(result.inserted_id)
    loops_col.update_one(
        {"loop_id": loop_id},
        {
            "$set": {
                "final_report_status": LOOP_FINAL_REPORT_STATUS_PENDING,
                "final_report_task_id": task_id,
                "final_report": None,
                "final_report_error": None,
                "final_report_at": None,
                "updated_at": datetime.now(),
            }
        },
    )
    return task_id


def sync_loop_final_report_status(collections: Dict[str, Any], loop: Dict[str, Any]) -> Dict[str, Any]:
    """If final report task finished but loop doc stale, sync from analysis_tasks."""
    if loop.get("final_report_status") != LOOP_FINAL_REPORT_STATUS_PENDING:
        return loop
    task_id = loop.get("final_report_task_id")
    if not task_id:
        return loop
    from bson import ObjectId

    try:
        oid = ObjectId(task_id)
    except Exception:
        return loop

    task = collections["analysis_tasks"].find_one({"_id": oid})
    if not task:
        return loop

    status = normalize_task_status(task.get("status"))
    loops_col = collections["strategy_lab_loops"]
    loop_id = loop.get("loop_id")
    if status in RUNNING_STATUSES:
        return loop
    if status == "completed":
        refreshed = loops_col.find_one({"loop_id": loop_id}) or loop
        return resolve_loop_final_report(collections, refreshed, persist=True)
    if status in TERMINAL_STATUSES:
        loops_col.update_one(
            {"loop_id": loop_id},
            {
                "$set": {
                    "final_report_status": LOOP_FINAL_REPORT_STATUS_FAILED,
                    "final_report_error": str(task.get("error") or status),
                    "updated_at": datetime.now(),
                }
            },
        )
        return loops_col.find_one({"loop_id": loop_id}) or loop
    return loop


def queue_batch_review_sync(
    collections: Dict[str, Any],
    current_user: dict,
    batch_id: str,
    review_options: Dict[str, Any],
) -> str:
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="backtest_batch_review",
        priority=20,
        params={
            "batch_id": batch_id,
            "objective": review_options.get("objective", "提高收益"),
            "include_top_n": int(review_options.get("include_top_n") or 20),
            "include_bottom_n": int(review_options.get("include_bottom_n") or 20),
            "include_failures": bool(review_options.get("include_failures", True)),
        },
        source="strategy_lab_loop",
    )
    result = collections["analysis_tasks"].insert_one(task_doc)
    return str(result.inserted_id)


def _find_review_task(
    collections: Dict[str, Any],
    batch_id: str,
    analysis_task_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if analysis_task_id:
        from bson import ObjectId

        try:
            doc = collections["analysis_tasks"].find_one({"_id": ObjectId(analysis_task_id)})
            if doc:
                return doc
        except Exception:
            pass
    tasks = list(
        collections["analysis_tasks"]
        .find(
            {
                "task_type": "backtest_batch_review",
                "params.batch_id": batch_id,
            }
        )
        .sort("created_at", -1)
        .limit(5)
    )
    return tasks[0] if tasks else None


def _iso_dt(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _serialize_review_task(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    status = normalize_task_status(task.get("status"))
    return {
        "task_id": str(task.get("_id")) if task.get("_id") is not None else task.get("task_id"),
        "status": status,
        "raw_status": task.get("status"),
        "worker_id": task.get("worker_id"),
        "error": task.get("error") or task.get("error_message"),
        "created_at": _iso_dt(task.get("created_at")),
        "updated_at": _iso_dt(task.get("updated_at")),
        "started_at": _iso_dt(task.get("started_at")),
        "completed_at": _iso_dt(task.get("completed_at")),
        "has_result": isinstance(task.get("result"), dict),
    }


def _latest_review_doc(batch_id: str) -> Optional[Dict[str, Any]]:
    analyzer = get_analyzer_db()
    reviews = list(
        analyzer["backtest_batch_reviews"]
        .find({"batch_id": batch_id})
        .sort("created_at", -1)
        .limit(1)
    )
    return reviews[0] if reviews else None


def _pick_next_experiment(review_body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    experiments = review_body.get("next_experiments") or []
    if not isinstance(experiments, list):
        return None, {}
    for item in experiments:
        if not isinstance(item, dict):
            continue
        params = normalize_suggestion_params(item)
        if params:
            return item, params
    if experiments and isinstance(experiments[0], dict):
        return experiments[0], normalize_suggestion_params(experiments[0])
    return None, {}


def _iteration_record(
    iteration: int,
    batch_id: str,
    summary: Dict[str, Any],
    *,
    review_task_id: Optional[str] = None,
    review_id: Optional[str] = None,
    applied_change: Optional[Dict[str, Any]] = None,
    decision: Optional[str] = None,
    decision_reason: Optional[str] = None,
    child_batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "iteration": iteration,
        "batch_id": batch_id,
        "batch_summary": summary,
        "review_task_id": review_task_id,
        "review_id": review_id,
        "applied_change": applied_change,
        "decision": decision,
        "decision_reason": decision_reason,
        "child_batch_id": child_batch_id,
        "recorded_at": datetime.now(),
    }


def _finish_loop(
    loops_col,
    loop: Dict[str, Any],
    *,
    status: str,
    step: str,
    stop_reason: str,
    final_summary: Optional[Dict[str, Any]] = None,
) -> None:
    loops_col.update_one(
        {"loop_id": loop["loop_id"]},
        {
            "$set": {
                "status": status,
                "current_step": step,
                "stop_reason": stop_reason,
                "final_summary": final_summary,
                "updated_at": datetime.now(),
                "completed_at": datetime.now(),
            }
        },
    )
    if status == LOOP_STATUS_COMPLETED:
        try:
            collections = get_db_collections()
            queue_loop_final_report_sync(
                collections,
                {"id": loop.get("user_id")},
                loop["loop_id"],
            )
        except Exception as exc:
            log.warning(
                "[StrategyLabLoop] queue final report failed loop_id=%s: %s",
                loop.get("loop_id"),
                exc,
            )


def _update_iteration_entry(loops_col, loop_id: str, iteration: int, patch: Dict[str, Any]) -> None:
    loop = loops_col.find_one({"loop_id": loop_id})
    if not loop:
        return
    iterations = list(loop.get("iterations") or [])
    for entry in iterations:
        if entry.get("iteration") == iteration:
            entry.update(patch)
            break
    loops_col.update_one(
        {"loop_id": loop_id},
        {"$set": {"iterations": iterations, "updated_at": datetime.now()}},
    )


def advance_loop(loop_id: str) -> Dict[str, Any]:
    """Advance one loop by a single state transition. Returns action summary."""
    collections = get_db_collections()
    loops_col = collections["strategy_lab_loops"]
    loop = loops_col.find_one({"loop_id": loop_id})
    if not loop:
        return {"loop_id": loop_id, "action": "not_found"}
    if loop.get("status") not in ACTIVE_LOOP_STATUSES:
        return {"loop_id": loop_id, "action": "skipped", "status": loop.get("status")}

    user_id = loop["user_id"]
    current_user = {"id": user_id}
    batch_id = loop.get("current_batch_id")
    step = loop.get("current_step") or STEP_WAIT_BATCH
    iteration = int(loop.get("current_iteration") or 1)
    max_iterations = int(loop.get("max_iterations") or 3)
    stop_rules = loop.get("stop_rules") or default_stop_rules()
    review_options = loop.get("review_options") or default_review_options()

    if not batch_id:
        _finish_loop(loops_col, loop, status=LOOP_STATUS_FAILED, step=STEP_COMPLETED, stop_reason="缺少 current_batch_id")
        return {"loop_id": loop_id, "action": "failed", "reason": "missing batch"}

    summary = refresh_batch_summary(collections, batch_id, user_id)
    batch_status = str(summary.get("status") or "")

    if step == STEP_WAIT_BATCH:
        if batch_status not in TERMINAL_BATCH_STATUSES:
            loops_col.update_one(
                {"loop_id": loop_id},
                {"$set": {"last_batch_summary": summary, "updated_at": datetime.now()}},
            )
            return {"loop_id": loop_id, "action": "waiting_batch", "batch_status": batch_status}

        if batch_status == "cancelled":
            _finish_loop(loops_col, loop, status=LOOP_STATUS_FAILED, step=STEP_COMPLETED, stop_reason="批量实验已取消")
            return {"loop_id": loop_id, "action": "failed", "reason": "batch cancelled"}

        if batch_status == "failed":
            _finish_loop(loops_col, loop, status=LOOP_STATUS_FAILED, step=STEP_COMPLETED, stop_reason="批量实验全部失败")
            return {"loop_id": loop_id, "action": "failed", "reason": "batch failed"}

        iterations = list(loop.get("iterations") or [])
        existing = next((e for e in iterations if e.get("iteration") == iteration and e.get("batch_id") == batch_id), None)
        if not existing:
            iterations.append(_iteration_record(iteration, batch_id, summary))
        else:
            existing["batch_summary"] = summary

        review_task_id = existing.get("review_task_id") if existing else None
        if not review_task_id:
            review_task_id = queue_batch_review_sync(collections, current_user, batch_id, review_options)
            if existing:
                existing["review_task_id"] = review_task_id
            else:
                iterations[-1]["review_task_id"] = review_task_id

        loops_col.update_one(
            {"loop_id": loop_id},
            {
                "$set": {
                    "current_step": STEP_WAIT_REVIEW,
                    "iterations": iterations,
                    "last_batch_summary": summary,
                    "updated_at": datetime.now(),
                }
            },
        )
        return {"loop_id": loop_id, "action": "queued_review", "review_task_id": review_task_id}

    if step == STEP_WAIT_REVIEW:
        entry = next(
            (e for e in (loop.get("iterations") or []) if e.get("iteration") == iteration),
            None,
        )
        review_task_id = (entry or {}).get("review_task_id")
        review_task = _find_review_task(collections, batch_id, review_task_id)
        if review_task:
            status_norm = normalize_task_status(review_task.get("status"))
            if status_norm in RUNNING_STATUSES:
                return {"loop_id": loop_id, "action": "waiting_review", "task_status": status_norm}
            if status_norm in {"failed", "completed_with_parse_error"}:
                error = review_task.get("error") or review_task.get("error_detail") or status_norm
                _finish_loop(
                    loops_col,
                    loop,
                    status=LOOP_STATUS_FAILED,
                    step=STEP_COMPLETED,
                    stop_reason=f"复盘任务失败: {error}",
                )
                return {"loop_id": loop_id, "action": "failed", "reason": "review task failed"}
            if status_norm not in TERMINAL_STATUSES:
                return {"loop_id": loop_id, "action": "waiting_review", "task_status": status_norm}

        review_doc = _latest_review_doc(batch_id)
        if not review_doc:
            return {"loop_id": loop_id, "action": "waiting_review_doc"}

        review_body = review_doc.get("review") if isinstance(review_doc.get("review"), dict) else review_doc
        review_id = str(review_doc.get("_id")) if review_doc.get("_id") else review_doc.get("review_id")

        prev_summaries = [
            e.get("batch_summary")
            for e in (loop.get("iterations") or [])
            if e.get("batch_summary") and e.get("iteration") < iteration
        ]
        stop, stop_reason = evaluate_stop_rules(
            stop_rules=stop_rules,
            current_iteration=iteration,
            max_iterations=max_iterations,
            summary=summary,
            previous_summaries=prev_summaries,
            review=review_body,
        )
        if stop:
            _update_iteration_entry(
                loops_col,
                loop_id,
                iteration,
                {"review_id": review_id, "decision": "stop", "decision_reason": stop_reason},
            )
            _finish_loop(
                loops_col,
                loop,
                status=LOOP_STATUS_COMPLETED,
                step=STEP_COMPLETED,
                stop_reason=stop_reason,
                final_summary=summary,
            )
            return {"loop_id": loop_id, "action": "completed", "reason": stop_reason}

        if iteration >= max_iterations:
            _update_iteration_entry(
                loops_col,
                loop_id,
                iteration,
                {
                    "review_id": review_id,
                    "decision": "stop",
                    "decision_reason": f"达到最大迭代次数 {max_iterations}",
                },
            )
            _finish_loop(
                loops_col,
                loop,
                status=LOOP_STATUS_COMPLETED,
                step=STEP_COMPLETED,
                stop_reason=f"达到最大迭代次数 {max_iterations}",
                final_summary=summary,
            )
            return {"loop_id": loop_id, "action": "completed", "reason": "max iterations"}

        experiment_item, param_patch = _pick_next_experiment(review_body or {})
        if not param_patch:
            _finish_loop(
                loops_col,
                loop,
                status=LOOP_STATUS_COMPLETED,
                step=STEP_COMPLETED,
                stop_reason="AI 未给出可应用的参数建议",
                final_summary=summary,
            )
            return {"loop_id": loop_id, "action": "completed", "reason": "no params"}

        batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": user_id}) or {}
        before_params = dict(batch.get("strategy_params") or {})
        after_params = merge_strategy_params(before_params, param_patch)
        source_universe = _resolve_source_universe(collections, batch, user_id)
        batch_for_name = {**batch, **source_universe}
        exp_name = build_structured_batch_name(batch_for_name, {
            "strategy_key": batch.get("strategy_key"),
            "preset": batch.get("preset"),
            "label": (experiment_item or {}).get("name") if isinstance(experiment_item, dict) else None,
        })

        rerun_result = rerun_backtest_batch_sync(
            collections,
            current_user,
            batch_id,
            BacktestBatchRerunCreate(
                name=exp_name,
                strategy_params=after_params,
            ),
        )
        child_batch_id = rerun_result.get("batch_id")
        next_iteration = iteration + 1

        iterations = list(loop.get("iterations") or [])
        for e in iterations:
            if e.get("iteration") == iteration:
                e.update(
                    {
                        "review_id": review_id,
                        "decision": "continue",
                        "applied_change": {
                            "before": before_params,
                            "after": after_params,
                            "source": "next_experiments[0]",
                            "experiment": experiment_item,
                        },
                        "child_batch_id": child_batch_id,
                    }
                )
                break

        iterations.append(
            _iteration_record(next_iteration, child_batch_id, {"status": "pending", "total": 0})
        )
        loops_col.update_one(
            {"loop_id": loop_id},
            {
                "$set": {
                    "current_step": STEP_WAIT_BATCH,
                    "current_iteration": next_iteration,
                    "current_batch_id": child_batch_id,
                    "iterations": iterations,
                    "updated_at": datetime.now(),
                },
                "$push": {"batch_chain": child_batch_id},
            },
        )
        if child_batch_id:
            collections["backtest_batches"].update_one(
                {"batch_id": child_batch_id},
                {"$set": {"loop_id": loop_id}},
            )

        return {
            "loop_id": loop_id,
            "action": "rerun",
            "child_batch_id": child_batch_id,
            "iteration": next_iteration,
        }

    return {"loop_id": loop_id, "action": "noop", "step": step}


def advance_all_running_loops(limit: int = 20) -> int:
    collections = get_db_collections()
    loops = list(
        collections["strategy_lab_loops"]
        .find({"status": LOOP_STATUS_RUNNING})
        .sort("updated_at", 1)
        .limit(limit)
    )
    count = 0
    for loop in loops:
        try:
            advance_loop(loop["loop_id"])
            count += 1
        except Exception as exc:
            log.error("[StrategyLabLoop] advance failed loop_id=%s: %s", loop.get("loop_id"), exc, exc_info=True)
    return count


def get_loop_runtime_health(user_id: str) -> Dict[str, Any]:
    """Return scheduler/tick visibility for Strategy Lab UI."""
    collections = get_db_collections()
    status_doc = collections["scheduler_status"].find_one({"_id": "primary"}) or {}
    config_doc = collections["schedule_config"].find_one({"job_id": "strategy_lab_loop_tick"}) or {}
    default_config = {
        "name": "Strategy Lab 自动迭代推进",
        "type": "interval",
        "seconds": 45,
        "is_active": True,
    }
    job_config = {**default_config, **config_doc}
    job_config.pop("_id", None)

    heartbeat_ts = status_doc.get("heartbeat_ts")
    heartbeat_age_seconds = None
    scheduler_online = False
    try:
        heartbeat_age_seconds = max(0.0, time.time() - float(heartbeat_ts))
        scheduler_online = heartbeat_age_seconds <= 90
    except (TypeError, ValueError):
        pass

    jobs = status_doc.get("jobs") if isinstance(status_doc.get("jobs"), dict) else {}
    tick_job = jobs.get("strategy_lab_loop_tick") if scheduler_online else None
    tick_loaded = isinstance(tick_job, dict)
    tick_enabled = bool(job_config.get("is_active", True))

    active_count = collections["strategy_lab_loops"].count_documents(
        {"user_id": user_id, "status": LOOP_STATUS_RUNNING}
    )
    paused_count = collections["strategy_lab_loops"].count_documents(
        {"user_id": user_id, "status": LOOP_STATUS_PAUSED}
    )

    if not scheduler_online:
        message = "scheduler 未在线或心跳过期，Loop 不会自动推进"
    elif not tick_enabled:
        message = "strategy_lab_loop_tick 已禁用，Loop 不会自动推进"
    elif not tick_loaded:
        message = "scheduler 在线，但 strategy_lab_loop_tick 未加载"
    else:
        message = "自动推进服务正常"

    return {
        "scheduler_online": scheduler_online,
        "scheduler_state": status_doc.get("state"),
        "scheduler_instance": status_doc.get("instance_id"),
        "scheduler_heartbeat_at": status_doc.get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "tick_job_id": "strategy_lab_loop_tick",
        "tick_enabled": tick_enabled,
        "tick_loaded": tick_loaded,
        "tick_interval_seconds": job_config.get("seconds"),
        "tick_next_run_time": tick_job.get("next_run_time") if isinstance(tick_job, dict) else None,
        "active_loop_count": active_count,
        "paused_loop_count": paused_count,
        "auto_advance_available": bool(scheduler_online and tick_enabled and tick_loaded),
        "message": message,
    }


def create_loop(
    *,
    user_id: str,
    batch_config: Optional[Dict[str, Any]] = None,
    source_batch_id: Optional[str] = None,
    max_iterations: int = 3,
    objective: Optional[str] = None,
    stop_rules: Optional[Dict[str, Any]] = None,
    review_options: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    collections = get_db_collections()
    current_user = {"id": user_id}
    loops_col = collections["strategy_lab_loops"]

    if source_batch_id:
        batch = collections["backtest_batches"].find_one({"batch_id": source_batch_id, "user_id": user_id})
        if not batch:
            raise ValueError(f"Backtest batch not found: {source_batch_id}")
        if is_compare_batch(batch):
            raise ValueError("Compare batches cannot be used as Loop source batches")
        active_loop = _find_active_loop_for_batch(collections, user_id, source_batch_id)
        if active_loop:
            raise ValueError(
                f"Backtest batch is already used by active loop {active_loop.get('loop_id')}"
            )
        batch_id = source_batch_id
        frozen_config = {
            "strategy_key": batch.get("strategy_key"),
            "preset": batch.get("preset"),
            "asset_type": batch.get("asset_type"),
            "start_date": batch.get("start_date"),
            "end_date": batch.get("end_date"),
            "initial_cash": batch.get("initial_cash"),
            "universe_type": batch.get("universe_type"),
            "universe_value": batch.get("universe_value"),
            "regime_cash": bool(batch.get("regime_cash")),
            "regime_index": batch.get("regime_index"),
            "regime_rule": batch.get("regime_rule"),
        }
    elif batch_config:
        payload = BacktestBatchCreate(**batch_config)
        if not payload.name or str(payload.name).strip().startswith("Strategy Lab loop"):
            payload = payload.model_copy(update={"name": build_structured_batch_name(batch_config)})
        try:
            created = create_backtest_batch_sync(collections, current_user, payload)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            raise ValueError(detail) from exc
        batch_id = created["batch_id"]
        frozen_config = deepcopy(batch_config)
        collections["backtest_batches"].update_one({"batch_id": batch_id}, {"$set": {"loop_id": None}})
    else:
        raise ValueError("batch_config or source_batch_id is required")

    loop_id = _new_loop_id()
    merged_review = {**default_review_options(), **(review_options or {})}
    if objective:
        merged_review["objective"] = objective
    now = datetime.now()
    loop_doc = {
        "loop_id": loop_id,
        "user_id": user_id,
        "name": name or f"Strategy Lab loop {now.strftime('%Y-%m-%d %H:%M')}",
        "status": LOOP_STATUS_RUNNING,
        "current_step": STEP_WAIT_BATCH,
        "current_iteration": 1,
        "max_iterations": max(1, min(max_iterations, 20)),
        "objective": merged_review.get("objective"),
        "stop_rules": {**default_stop_rules(), **(stop_rules or {})},
        "review_options": merged_review,
        "root_batch_id": batch_id,
        "current_batch_id": batch_id,
        "batch_chain": [batch_id],
        "frozen_config": frozen_config,
        "iterations": [_iteration_record(1, batch_id, {"status": "pending"})],
        "stop_reason": None,
        "final_summary": None,
        "last_batch_summary": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    loops_col.insert_one(loop_doc)
    collections["backtest_batches"].update_one({"batch_id": batch_id}, {"$set": {"loop_id": loop_id}})
    return _serialize_loop(loop_doc)


def _serialize_loop(loop: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in loop.items() if k != "_id"}
    if loop.get("_id"):
        out["id"] = str(loop["_id"])
    return out


def get_loop(loop_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    collections = get_db_collections()
    doc = collections["strategy_lab_loops"].find_one({"loop_id": loop_id, "user_id": user_id})
    if not doc:
        return None
    doc = sync_loop_final_report_status(collections, doc)
    doc = resolve_loop_final_report(collections, doc, persist=True)
    return _serialize_loop(doc)


def _review_summary_text(review_doc: Optional[Dict[str, Any]]) -> Optional[str]:
    if not review_doc:
        return None
    body = review_doc.get("review") if isinstance(review_doc.get("review"), dict) else review_doc
    if not isinstance(body, dict):
        return None
    summary = body.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    findings = body.get("main_findings")
    if isinstance(findings, list) and findings:
        first = findings[0]
        return str(first).strip() if first else None
    return None


def _param_change_keys(applied_change: Optional[Dict[str, Any]]) -> List[str]:
    if not applied_change or not isinstance(applied_change, dict):
        return []
    before = applied_change.get("before") or {}
    after = applied_change.get("after") or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    keys = sorted(set(before.keys()) | set(after.keys()))
    changed = []
    for key in keys:
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def build_loop_summary_report(loop_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Aggregate per-iteration batch metrics and AI review snippets for the dashboard."""
    collections = get_db_collections()
    loop = collections["strategy_lab_loops"].find_one({"loop_id": loop_id, "user_id": user_id})
    if not loop:
        return None
    loop = sync_loop_final_report_status(collections, loop)
    loop = resolve_loop_final_report(collections, loop, persist=True)

    iteration_rows: List[Dict[str, Any]] = []
    seen_batches: set = set()
    entries_by_batch = {
        entry.get("batch_id"): entry
        for entry in (loop.get("iterations") or [])
        if entry.get("batch_id")
    }
    ordered_batch_ids = []
    for batch_id in loop.get("batch_chain") or []:
        if batch_id and batch_id not in ordered_batch_ids:
            ordered_batch_ids.append(batch_id)
    for entry in sorted(loop.get("iterations") or [], key=lambda e: int(e.get("iteration") or 0)):
        batch_id = entry.get("batch_id")
        if batch_id and batch_id not in ordered_batch_ids:
            ordered_batch_ids.append(batch_id)

    for idx, batch_id in enumerate(ordered_batch_ids, start=1):
        if not batch_id or batch_id in seen_batches:
            continue
        seen_batches.add(batch_id)
        entry = entries_by_batch.get(batch_id) or {"iteration": idx, "batch_id": batch_id}
        try:
            summary = refresh_batch_summary(collections, batch_id, user_id)
        except ValueError:
            summary = entry.get("batch_summary") if isinstance(entry.get("batch_summary"), dict) else {}

        batch_doc = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": user_id}) or {}
        review_doc = _latest_review_doc(batch_id)
        review_body = None
        if review_doc:
            review_body = review_doc.get("review") if isinstance(review_doc.get("review"), dict) else review_doc

        iteration_rows.append(
            {
                "iteration": entry.get("iteration") or idx,
                "batch_id": batch_id,
                "batch_name": batch_doc.get("name") or batch_id,
                "batch_status": summary.get("status"),
                "total": summary.get("total"),
                "completed": summary.get("completed"),
                "completion_rate": _completion_rate(summary),
                "median_total_return": summary.get("median_total_return"),
                "avg_max_drawdown": summary.get("avg_max_drawdown"),
                "median_win_rate": summary.get("median_win_rate"),
                "decision": entry.get("decision"),
                "decision_reason": entry.get("decision_reason"),
                "review_summary": _review_summary_text(review_doc),
                "param_changes": _param_change_keys(entry.get("applied_change")),
                "child_batch_id": entry.get("child_batch_id"),
                "child_batch_name": (
                    collections["backtest_batches"].find_one(
                        {"batch_id": entry.get("child_batch_id"), "user_id": user_id}
                    )
                    or {}
                ).get("name")
                if entry.get("child_batch_id")
                else None,
                "experiment_name": (entry.get("applied_change") or {}).get("experiment", {}).get("name")
                if isinstance(entry.get("applied_change"), dict)
                else None,
                "has_review": review_doc is not None,
            }
        )

    final_summary = loop.get("final_summary")
    if not final_summary and iteration_rows:
        final_summary = {
            "median_total_return": iteration_rows[-1].get("median_total_return"),
            "completion_rate": iteration_rows[-1].get("completion_rate"),
            "batch_status": iteration_rows[-1].get("batch_status"),
            "iteration": iteration_rows[-1].get("iteration"),
        }

    current_summary = loop.get("last_batch_summary") if isinstance(loop.get("last_batch_summary"), dict) else None
    if not current_summary and loop.get("current_batch_id"):
        current_summary = next(
            (
                row
                for row in iteration_rows
                if row.get("batch_id") == loop.get("current_batch_id")
            ),
            None,
        )

    waiting_status_counts = (current_summary or {}).get("status_counts") or {}
    waiting_pending = int(waiting_status_counts.get("pending") or 0)
    waiting_claimed = int(waiting_status_counts.get("claimed") or 0)
    waiting_running = int(waiting_status_counts.get("running") or 0)
    waiting_active = waiting_pending + waiting_claimed + waiting_running
    waiting_reason = None
    waiting_detail = None
    waiting_review_task = None
    waiting_review_doc_exists = None
    current_step = loop.get("current_step")
    if loop.get("status") == LOOP_STATUS_RUNNING and current_step == STEP_WAIT_BATCH:
        waiting_reason = "等待当前轮批量回测执行完成"
        waiting_detail = (
            f"还有 {waiting_active} 个子任务未结束"
            if waiting_active
            else "当前轮批量回测尚未被判定为终态"
        )
    elif loop.get("status") == LOOP_STATUS_RUNNING and current_step == STEP_WAIT_REVIEW:
        waiting_reason = "等待 AI 复盘完成并写入结果"
        current_entry = next(
            (
                entry
                for entry in (loop.get("iterations") or [])
                if entry.get("iteration") == loop.get("current_iteration")
            ),
            None,
        )
        review_task = _find_review_task(
            collections,
            loop.get("current_batch_id") or "",
            (current_entry or {}).get("review_task_id"),
        )
        waiting_review_task = _serialize_review_task(review_task)
        review_doc = _latest_review_doc(loop.get("current_batch_id") or "")
        waiting_review_doc_exists = review_doc is not None
        if not waiting_review_task:
            waiting_detail = "尚未找到 AI 复盘任务，下一次推进会尝试重新检查"
        elif waiting_review_task["status"] in RUNNING_STATUSES:
            waiting_detail = f"AI 复盘任务状态：{waiting_review_task['status']}，等待 quantAnalyzer 消费/完成"
        elif waiting_review_task["status"] == "completed" and not waiting_review_doc_exists:
            waiting_detail = "AI 复盘任务已完成，但复盘结果尚未写入 backtest_batch_reviews"
        elif waiting_review_task["status"] == "completed_with_parse_error":
            waiting_detail = f"AI 复盘 JSON 解析失败：{waiting_review_task.get('error') or 'LLM 输出不是合法 JSON'}"
        elif waiting_review_task["status"] in TERMINAL_STATUSES and waiting_review_task["status"] != "completed":
            waiting_detail = f"AI 复盘任务结束但未成功：{waiting_review_task['status']}"
        else:
            waiting_detail = "复盘任务完成后会读取参数建议并创建下一轮"

    return {
        "loop_id": loop_id,
        "name": loop.get("name"),
        "status": loop.get("status"),
        "current_iteration": loop.get("current_iteration"),
        "current_step": current_step,
        "current_batch_id": loop.get("current_batch_id"),
        "max_iterations": loop.get("max_iterations"),
        "stop_reason": loop.get("stop_reason"),
        "objective": (loop.get("review_options") or {}).get("objective"),
        "batch_chain": loop.get("batch_chain") or [],
        "waiting_reason": waiting_reason,
        "waiting_detail": waiting_detail,
        "waiting_status_counts": waiting_status_counts,
        "waiting_active_tasks": waiting_active,
        "waiting_review_task": waiting_review_task,
        "waiting_review_doc_exists": waiting_review_doc_exists,
        "final_summary": final_summary,
        "last_batch_summary": loop.get("last_batch_summary"),
        "iterations": iteration_rows,
        "is_terminal": loop.get("status") in {
            LOOP_STATUS_COMPLETED,
            LOOP_STATUS_FAILED,
            LOOP_STATUS_CANCELLED,
        },
        "final_report": loop.get("final_report"),
        "final_report_status": loop.get("final_report_status"),
        "final_report_task_id": loop.get("final_report_task_id"),
        "final_report_error": loop.get("final_report_error"),
        "final_report_at": loop.get("final_report_at"),
        "can_request_final_report": loop.get("status") == LOOP_STATUS_COMPLETED
        and not report_has_content(loop.get("final_report")),
        "has_final_report_content": report_has_content(loop.get("final_report")),
    }


def list_loops(user_id: str, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    docs = list(
        get_db_collections()["strategy_lab_loops"]
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_serialize_loop(d) for d in docs]


def set_loop_status(loop_id: str, user_id: str, status: str) -> bool:
    allowed = {LOOP_STATUS_RUNNING, LOOP_STATUS_PAUSED, LOOP_STATUS_CANCELLED}
    if status not in allowed:
        raise ValueError(f"Invalid status: {status}")
    collections = get_db_collections()
    loops_col = collections["strategy_lab_loops"]
    loop = loops_col.find_one({"loop_id": loop_id, "user_id": user_id})
    if not loop:
        return False
    current_status = loop.get("status")
    if current_status == status:
        return True
    allowed_transitions = {
        LOOP_STATUS_RUNNING: {LOOP_STATUS_PAUSED, LOOP_STATUS_CANCELLED},
        LOOP_STATUS_PAUSED: {LOOP_STATUS_RUNNING, LOOP_STATUS_CANCELLED},
    }
    if status not in allowed_transitions.get(current_status, set()):
        raise ValueError(f"Invalid loop status transition: {current_status} -> {status}")

    update = {"status": status, "updated_at": datetime.now()}
    if status == LOOP_STATUS_CANCELLED:
        update["stop_reason"] = loop.get("stop_reason") or "用户取消"
        update["completed_at"] = datetime.now()

    result = loops_col.update_one(
        {"loop_id": loop_id, "user_id": user_id},
        {"$set": update},
    )
    return bool(result.modified_count)


def delete_loop(loop_id: str, user_id: str) -> Dict[str, Any]:
    """Delete a terminal Strategy Lab loop record without deleting its batch experiments."""
    collections = get_db_collections()
    loops_col = collections["strategy_lab_loops"]
    loop = loops_col.find_one({"loop_id": loop_id, "user_id": user_id})
    if not loop:
        raise ValueError(f"Strategy lab loop not found: {loop_id}")

    status = loop.get("status")
    if status not in {LOOP_STATUS_COMPLETED, LOOP_STATUS_FAILED, LOOP_STATUS_CANCELLED}:
        raise ValueError("Only completed/failed/cancelled loops can be deleted")

    batch_ids = [bid for bid in (loop.get("batch_chain") or []) if bid]
    result = loops_col.delete_one({"loop_id": loop_id, "user_id": user_id})
    if batch_ids:
        collections["backtest_batches"].update_many(
            {"batch_id": {"$in": batch_ids}, "user_id": user_id, "loop_id": loop_id},
            {"$set": {"loop_id": None, "updated_at": datetime.now()}},
        )

    return {
        "message": "Strategy lab loop deleted",
        "loop_id": loop_id,
        "deleted_loops": int(result.deleted_count),
        "detached_batches": len(batch_ids),
    }
