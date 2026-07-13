#!/usr/bin/env python3
"""Tab 性能验收：对关键列表接口跑 explain，输出 docsExamined / nReturned 诊断。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Allow running from repo root: python tools/tab_performance_diagnostics.py
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_analyzer_db, get_data_db, get_db_collections  # noqa: E402


def _extract_execution_stats(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize find/aggregate explain payloads across MongoDB versions."""
    if not isinstance(plan, dict):
        return {}
    stats = plan.get("executionStats")
    if isinstance(stats, dict):
        return stats
    for stage in plan.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        cursor = stage.get("$cursor") or stage.get("$cursorWithPrefix") or {}
        stats = cursor.get("executionStats") if isinstance(cursor, dict) else None
        if isinstance(stats, dict):
            return stats
    return {}


def _extract_stage(plan: Dict[str, Any]) -> Optional[str]:
    if not isinstance(plan, dict):
        return None
    winning = (plan.get("queryPlanner") or {}).get("winningPlan")
    if isinstance(winning, dict) and winning.get("stage"):
        return str(winning.get("stage"))
    for stage in plan.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        cursor = stage.get("$cursor") or stage.get("$cursorWithPrefix") or {}
        if isinstance(cursor, dict):
            winning = (cursor.get("queryPlanner") or {}).get("winningPlan")
            if isinstance(winning, dict) and winning.get("stage"):
                return str(winning.get("stage"))
    return None


def _execution_stats(coll, operation: str, **kwargs) -> Dict[str, Any]:
    plan = coll.find(**kwargs).explain()
    stats = _extract_execution_stats(plan)
    return {
        "operation": operation,
        "nReturned": stats.get("nReturned"),
        "totalDocsExamined": stats.get("totalDocsExamined"),
        "totalKeysExamined": stats.get("totalKeysExamined"),
        "executionTimeMillis": stats.get("executionTimeMillis"),
        "stage": _extract_stage(plan),
    }


def _match_query_from_pipeline(pipeline: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for stage in pipeline:
        if isinstance(stage, dict) and "$match" in stage:
            match = stage.get("$match")
            if isinstance(match, dict):
                return match
    return None


def _limit_from_pipeline(pipeline: List[Dict[str, Any]]) -> Optional[int]:
    for stage in pipeline:
        if isinstance(stage, dict) and "$limit" in stage:
            try:
                return int(stage["$limit"])
            except (TypeError, ValueError):
                return None
    return None


def _explain_aggregate(coll, pipeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run aggregation explain in a PyMongo-version-safe way."""
    try:
        return coll.aggregate(pipeline, explain=True)
    except Exception:
        pass
    return coll.database.command(
        {
            "aggregate": coll.name,
            "pipeline": pipeline,
            "cursor": {},
            "explain": True,
        }
    )


def _aggregate_stats(coll, pipeline: List[Dict[str, Any]], operation: str) -> Dict[str, Any]:
    plan = _explain_aggregate(coll, pipeline)
    stats = _extract_execution_stats(plan)
    stage = _extract_stage(plan)

    # MongoDB 7 aggregate explain is often planner-only; supplement with $match find explain.
    if stats.get("totalDocsExamined") is None:
        match_query = _match_query_from_pipeline(pipeline)
        if match_query is not None:
            find_kwargs: Dict[str, Any] = {"filter": match_query}
            limit = _limit_from_pipeline(pipeline)
            if limit is not None:
                find_kwargs["limit"] = limit
            match_plan = coll.find(**find_kwargs).explain()
            match_stats = _extract_execution_stats(match_plan)
            if match_stats:
                stats = {**stats, **match_stats}
                if stage is None:
                    stage = _extract_stage(match_plan)

    return {
        "operation": operation,
        "nReturned": stats.get("nReturned"),
        "totalDocsExamined": stats.get("totalDocsExamined"),
        "totalKeysExamined": stats.get("totalKeysExamined"),
        "executionTimeMillis": stats.get("executionTimeMillis"),
        "stage": stage,
    }


def _format_ratio(examined: Any, returned: Any) -> str:
    if examined is None or returned is None:
        return "n/a"
    if not returned:
        return str(examined)
    return f"{examined / returned:.1f}"


def run_diagnostics(user_id: Optional[str] = None) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    analyzer = get_analyzer_db()["analysis_history"]
    entropy = get_db_collections()["entropy_scores"]
    etf_share = get_data_db()["etf_share_size"]

    hist_query: Dict[str, Any] = {}
    if user_id:
        hist_query["user_id"] = user_id
    results.append(
        _execution_stats(
            analyzer,
            "analysis_history_list",
            filter=hist_query or {},
            projection={
                "symbol": 1,
                "timestamp": 1,
                "analysis.investment_advice": 1,
                "analysis.risk_level": 1,
            },
            sort=[("timestamp", -1)],
            limit=50,
        )
    )

    latest_entropy = entropy.find_one({}, sort=[("score_date", -1)], projection={"score_date": 1})
    score_date = latest_entropy.get("score_date") if latest_entropy else None
    if score_date:
        pipeline = [
            {"$match": {"score_date": score_date, "index_codes": "csi1000"}},
            {
                "$addFields": {
                    "_rank_entropy": {
                        "$ifNull": ["$final_entropy_score", {"$ifNull": ["$entropy_score", 1e18]}]
                    }
                }
            },
            {"$sort": {"_rank_entropy": 1, "symbol": 1}},
            {"$limit": 100},
        ]
        results.append(_aggregate_stats(entropy, pipeline, "entropy_rankings"))

    latest_etf = etf_share.find_one({}, sort=[("trade_date", -1)], projection={"trade_date": 1})
    trade_date = latest_etf.get("trade_date") if latest_etf else None
    if trade_date:
        results.append(
            _execution_stats(
                etf_share,
                "etf_share_size_page",
                filter={"trade_date": trade_date},
                sort=[("total_share", -1)],
                limit=50,
            )
        )

    return {"success": True, "checks": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Tab list API MongoDB scan diagnostics")
    parser.add_argument("--user-id", default=os.getenv("TAB_PERF_USER_ID"), help="Optional user_id for history query")
    parser.add_argument("--json", action="store_true", help="Print raw JSON")
    args = parser.parse_args()

    payload = run_diagnostics(user_id=args.user_id)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Tab performance diagnostics")
    for row in payload.get("checks") or []:
        examined = row.get("totalDocsExamined")
        returned = row.get("nReturned")
        ratio = _format_ratio(examined, returned)
        ms = row.get("executionTimeMillis")
        ms_text = ms if ms is not None else "n/a"
        print(
            f"- {row.get('operation')}: examined={examined} returned={returned} "
            f"ratio={ratio} stage={row.get('stage')} ms={ms_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
