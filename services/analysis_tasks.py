from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


TERMINAL_STATUSES = {"completed", "failed", "completed_with_parse_error"}
RUNNING_STATUSES = {"pending", "processing"}
DEFAULT_TASK_DURATION_SECONDS = {
    "deep_analysis": 120,
    "portfolio_analysis": 120,
    "market_analysis": 180,
    "global_analysis": 180,
    "sector_concept_analysis": 90,
    "hot_stock_analysis": 90,
    "etf_analysis": 90,
    "backtest_batch_review": 180,
    "strategy_lab_loop_report": 240,
}


def normalize_task_status(raw_status: Any) -> str:
    """Normalize status into lowercase canonical values."""
    if raw_status is None:
        return "unknown"
    if hasattr(raw_status, "value"):
        try:
            return str(raw_status.value).lower()
        except Exception:
            pass
    s = str(raw_status)
    if s.startswith("TaskStatus."):
        s = s.split(".", 1)[1]
    return s.lower()


def infer_task_type(task: Dict[str, Any]) -> str:
    return str(task.get("task_type") or task.get("type") or "deep_analysis")


def _norm_iso_date(value: Any) -> Optional[str]:
    """Normalize to YYYY-MM-DD when possible."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            pass
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) >= 10 and s[4] == "/" and s[7] == "/":
        return f"{s[:4]}-{s[5:7]}-{s[8:10]}"
    head = s.split()[0].split("T")[0]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    return None


def infer_task_data_date(task: Dict[str, Any]) -> Optional[str]:
    """
    任务所依据的「数据日期」（交易日/榜单日/天梯日等），YYYY-MM-DD。
    用于与业务集合对齐、避免按 created_at 取到错误的一条（尤其反馈重跑）。
    """
    params = task.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    result = task.get("result")
    if not isinstance(result, dict):
        result = {}

    ttype = infer_task_type(task)

    if ttype == "market_analysis":
        d = params.get("effective_analysis_date") or params.get("date")
        nd = _norm_iso_date(d)
        if nd:
            return nd
        return _norm_iso_date(result.get("analysis_date"))

    if ttype == "global_analysis":
        d = params.get("date")
        nd = _norm_iso_date(d)
        if nd:
            return nd
        return _norm_iso_date(result.get("date") or result.get("analysis_date"))

    if ttype == "sector_concept_analysis":
        d = params.get("date") or result.get("trade_date")
        if isinstance(d, str) and len(d) == 8 and d.isdigit():
            return _norm_iso_date(d)
        return _norm_iso_date(d)

    if ttype == "hot_stock_analysis":
        rt = result.get("rank_time") or params.get("rank_time")
        if rt:
            nd = _norm_iso_date(rt)
            if nd:
                return nd
        td = result.get("trade_date") or params.get("date")
        return _norm_iso_date(td)

    if ttype == "portfolio_analysis":
        ladder = params.get("ladder_data") or {}
        if isinstance(ladder, dict):
            ld = ladder.get("date")
            nd = _norm_iso_date(ld)
            if nd:
                return nd
        return _norm_iso_date(result.get("ladder_date"))

    if ttype == "etf_analysis":
        return _norm_iso_date(result.get("trade_date") or params.get("trade_date"))

    if ttype == "backtest_batch_review":
        return _norm_iso_date(result.get("created_at") or params.get("end_date"))

    if ttype == "strategy_lab_loop_report":
        return _norm_iso_date(result.get("created_at"))

    if ttype in {"limit_up_feedback", "broken_limit_up_feedback", "limit_up", "broken_limit_up"}:
        return _norm_iso_date(params.get("date"))

    if ttype == "x_influencer_post_analysis":
        post = params.get("post") or {}
        ca = post.get("created_at")
        if isinstance(ca, str) and len(ca) >= 10:
            head = ca[:10].replace("/", "-")
            return _norm_iso_date(head)
        return _norm_iso_date(ca)

    return None


def build_task_doc(
    *,
    current_user: Optional[Dict[str, Any]],
    task_type: str,
    priority: int,
    symbol: Optional[str] = None,
    stock_name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    source: str = "manual_trigger",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    revision_of: Optional[str] = None,
    feedback: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a normalized analysis task document."""
    now = datetime.now()
    user_id = str((current_user or {}).get("id") or (current_user or {}).get("username") or "system")
    username = (current_user or {}).get("username")
    if username is not None:
        username = str(username)

    task_doc: Dict[str, Any] = {
        # Backward compatibility for existing worker logic.
        "type": task_type,
        "status": "pending",
        "priority": int(priority),
        "user_id": user_id,
        # New normalized metadata.
        "task_type": task_type,
        "created_by_user_id": user_id,
        "created_by_username": username,
        "source": source,
        "revision_of": revision_of,
        "feedback": feedback,
        "params": params or {},
        "created_at": now,
        "updated_at": now,
    }
    if symbol:
        task_doc["symbol"] = symbol
    if stock_name:
        task_doc["stock_name"] = stock_name
    if provider:
        task_doc["provider"] = provider
    if model:
        task_doc["model"] = model
    if extra_fields:
        _PROTECTED = {
            "type", "task_type", "status", "priority",
            "user_id", "created_by_user_id", "created_by_username",
            "source", "created_at", "updated_at", "params",
        }
        for k, v in extra_fields.items():
            if k not in _PROTECTED:
                task_doc[k] = v
    return task_doc


def can_access_task(task: Dict[str, Any], current_user: Dict[str, Any]) -> bool:
    if current_user.get("is_admin"):
        return True
    user_id = str(current_user.get("id") or "")
    username = str(current_user.get("username") or "")
    owner_candidates = {
        str(task.get("created_by_user_id") or ""),
        str(task.get("user_id") or ""),
    }
    owner_name = str(task.get("created_by_username") or "")
    return (user_id and user_id in owner_candidates) or (username and (username == owner_name or username in owner_candidates))


def format_dt(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def summarize_task_result(task: Dict[str, Any]) -> str:
    status = normalize_task_status(task.get("status"))
    if status == "failed":
        return str(task.get("error") or "任务失败")
    if status == "completed_with_parse_error":
        return str(task.get("error") or "结果解析失败")
    result = task.get("result")
    if not isinstance(result, dict):
        return "任务执行中" if status in RUNNING_STATUSES else "无结果"

    task_type = infer_task_type(task)
    if task_type in {"hot_stock_analysis", "sector_concept_analysis"}:
        return str(result.get("analysis", ""))[:120] or "分析完成"
    if task_type == "portfolio_analysis":
        return str(result.get("analysis", ""))[:120] or "组合分析完成"
    if task_type == "etf_analysis":
        analysis = result.get("analysis")
        if isinstance(analysis, dict):
            return str(analysis.get("summary") or analysis.get("investment_advice") or "ETF分析完成")[:120]
        return "ETF分析完成"
    if task_type == "backtest_batch_review":
        review = result.get("review")
        if isinstance(review, dict):
            return str(review.get("summary") or "批量回测复盘完成")[:120]
        return "批量回测复盘完成"
    if task_type == "strategy_lab_loop_report":
        report = result.get("report")
        if isinstance(report, dict):
            return str(report.get("summary") or "Loop 综合报告完成")[:120]
        return "Loop 综合报告完成"
    if task_type in {"global_analysis", "market_analysis"}:
        return str(result.get("summary") or result.get("conclusion") or "市场分析完成")[:120]
    if task_type == "x_influencer_post_analysis":
        analysis = result.get("analysis")
        if isinstance(analysis, dict):
            return str(analysis.get("summary_zh") or result.get("summary") or "X推文解读完成")[:120]
        return str(result.get("summary") or "X推文解读完成")[:120]

    analysis = result.get("analysis")
    if isinstance(analysis, dict):
        return str(analysis.get("technical_analysis") or analysis.get("reason") or "分析完成")[:120]
    if isinstance(analysis, str):
        return analysis[:120]
    return "分析完成"


def queue_wait_seconds_to_text(seconds: Optional[int]) -> str:
    if seconds is None:
        return "预计等待时间暂不可估算"
    if seconds <= 0:
        return "即将开始"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"预计等待约 {minutes} 分钟"
    hours = minutes // 60
    remain = minutes % 60
    if remain:
        return f"预计等待约 {hours} 小时 {remain} 分钟"
    return f"预计等待约 {hours} 小时"


def _duration_seconds(task: Dict[str, Any]) -> Optional[float]:
    start = task.get("started_at") or task.get("created_at")
    end = task.get("completed_at") or task.get("failed_at") or task.get("updated_at")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    return seconds


def estimate_task_duration_seconds(tasks_col, task_type: str, sample_size: int = 30) -> int:
    query = {
        "$or": [{"task_type": task_type}, {"type": task_type}],
        "status": {"$in": list(TERMINAL_STATUSES)},
    }
    docs = tasks_col.find(
        query,
        {
            "_id": 0,
            "created_at": 1,
            "started_at": 1,
            "completed_at": 1,
            "failed_at": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1).limit(sample_size)
    durations: List[float] = []
    for doc in docs:
        seconds = _duration_seconds(doc)
        if seconds is not None:
            durations.append(seconds)
    if durations:
        durations.sort()
        # Use median to avoid a single slow LLM call making every ETA look scary.
        return max(30, int(durations[len(durations) // 2]))
    return DEFAULT_TASK_DURATION_SECONDS.get(task_type, 120)


def compute_queue_ahead(tasks_col, task: Dict[str, Any]) -> int:
    if normalize_task_status(task.get("status")) != "pending":
        return 0
    priority = int(task.get("priority") or 0)
    created_at = task.get("created_at")
    if not isinstance(created_at, datetime):
        return tasks_col.count_documents({"status": "pending"})

    same_priority_before: Dict[str, Any] = {
        "status": "pending",
        "priority": priority,
        "created_at": {"$lt": created_at},
    }
    # Worker sort is priority desc + created_at asc. _id keeps same-timestamp
    # estimates stable without changing worker claim semantics.
    if task.get("_id") is not None:
        same_time_before = {
            "status": "pending",
            "priority": priority,
            "created_at": created_at,
            "_id": {"$lt": task["_id"]},
        }
        same_priority_query = {"$or": [same_priority_before, same_time_before]}
    else:
        same_priority_query = same_priority_before

    return int(tasks_col.count_documents({"status": "pending", "priority": {"$gt": priority}})) + int(
        tasks_col.count_documents(same_priority_query)
    )


def estimate_wait_seconds(
    *,
    queue_ahead: int,
    processing_count: int,
    average_duration_seconds: int,
    estimated_parallelism: int,
) -> int:
    parallelism = max(1, int(estimated_parallelism or 1))
    units_before_start = max(0, int(queue_ahead))
    if processing_count > 0:
        units_before_start += min(int(processing_count), parallelism)
    return int((units_before_start * max(1, average_duration_seconds)) / parallelism)

