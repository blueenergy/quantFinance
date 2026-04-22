from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


TERMINAL_STATUSES = {"completed", "failed", "completed_with_parse_error"}
RUNNING_STATUSES = {"pending", "processing"}


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

    if ttype in {"limit_up_feedback", "broken_limit_up_feedback", "limit_up", "broken_limit_up"}:
        return _norm_iso_date(params.get("date"))

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
    if task_type in {"global_analysis", "market_analysis"}:
        return str(result.get("summary") or result.get("conclusion") or "市场分析完成")[:120]

    analysis = result.get("analysis")
    if isinstance(analysis, dict):
        return str(analysis.get("technical_analysis") or analysis.get("reason") or "分析完成")[:120]
    if isinstance(analysis, str):
        return analysis[:120]
    return "分析完成"

