"""宏观分析 API

两个独立分析页面：
  - 中国宏观  (scope = china_macro)
  - 美国利率  (scope = us_macro)

数据来源：
  - cn_macro_rates / cn_macro_monthly / cn_macro_quarterly  (quant_data DB)
  - us_rates_daily                                          (quant_data DB)
  - macro_analysis                                          (quant_analyzer DB)
"""

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db
import db as _app_db
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/macro", tags=["宏观分析"])

VALID_SCOPES = {"china_macro", "us_macro"}


def _serialize(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


# ---------------------------------------------------------------------------
# 1. 读取最新 LLM 分析结果
# ---------------------------------------------------------------------------

@router.get("/analysis/{scope}", summary="读取宏观 LLM 分析结果")
async def get_macro_analysis(
    scope: str,
    date: str = Query(None, description="分析日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回 quantAnalyzer 已写入的宏观分析结果。

    - scope = china_macro  → 中国宏观深度分析
    - scope = us_macro     → 美国利率市场深度分析
    """
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"未知 scope: {scope}，可选 china_macro / us_macro")

    coll = get_analyzer_db()["macro_analysis"]

    if date:
        doc = coll.find_one({"scope": scope, "analysis_date": date}, {"_id": 0})
    else:
        doc = coll.find_one({"scope": scope}, {"_id": 0}, sort=[("analysis_date", -1)])

    if not doc:
        raise HTTPException(404, f"暂无 {scope} 的分析数据，请先触发分析")

    return _serialize(doc)


# ---------------------------------------------------------------------------
# 2. 历史记录（最近 N 条）
# ---------------------------------------------------------------------------

@router.get("/analysis/{scope}/history", summary="读取宏观分析历史记录")
async def get_macro_analysis_history(
    scope: str,
    limit: int = Query(10, ge=1, le=60, description="返回条数上限"),
    current_user: dict = Depends(get_current_active_user),
):
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"未知 scope: {scope}")

    coll = get_analyzer_db()["macro_analysis"]
    docs = list(
        coll.find({"scope": scope}, {"_id": 0})
        .sort("analysis_date", -1)
        .limit(limit)
    )
    return {"scope": scope, "data": [_serialize(d) for d in docs]}


# ---------------------------------------------------------------------------
# 3. 触发 LLM 分析任务
# ---------------------------------------------------------------------------

@router.post("/analysis/{scope}/trigger", summary="触发宏观 LLM 深度分析")
async def trigger_macro_analysis(
    scope: str,
    date: str = Query(None, description="分析日期 YYYY-MM-DD，默认今天"),
    force: bool = Query(False, description="强制重新分析（即使当日已有结果）"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    向 analysis_tasks 队列投递一个 macro_analysis 任务。
    quantAnalyzer worker 消费后将结果写入 quant_analyzer.macro_analysis。

    默认幂等：当日已有 pending/processing 任务时返回已有 task_id；
    设 force=true 或已有结果且需刷新时可强制投递。
    """
    if scope not in VALID_SCOPES:
        raise HTTPException(400, f"未知 scope: {scope}，可选 china_macro / us_macro")

    analysis_date = date or datetime.today().strftime("%Y-%m-%d")

    # 幂等检查：今日是否已有待处理任务
    if not force:
        existing_task = _app_db.db["analysis_tasks"].find_one({
            "type": "macro_analysis",
            "params.scope": scope,
            "params.date": analysis_date,
            "status": {"$in": ["pending", "processing"]},
        })
        if existing_task:
            return {
                "task_id": str(existing_task["_id"]),
                "status": existing_task.get("status", "pending"),
                "analysis_date": analysis_date,
                "scope": scope,
                "message": "当日分析任务已在队列中",
            }

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="macro_analysis",
        priority=5,
        params={
            "scope": scope,
            "date": analysis_date,
        },
        source="manual_trigger",
    )
    result = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "analysis_date": analysis_date,
        "scope": scope,
    }


# ---------------------------------------------------------------------------
# 4. 读取原始数据（供前端图表）
# ---------------------------------------------------------------------------

@router.get("/raw-data/cn-rates", summary="中国利率原始数据")
async def get_cn_rates(
    data_type: str = Query(..., description="shibor / shibor_lpr"),
    days: int = Query(30, ge=1, le=3650),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["cn_macro_rates"]
    docs = list(
        coll.find({"data_type": data_type}, {"_id": 0})
        .sort("date", -1)
        .limit(days)
    )
    docs.reverse()
    return {"data_type": data_type, "data": docs}


@router.get("/raw-data/cn-monthly", summary="中国月度宏观数据")
async def get_cn_monthly(
    data_type: str = Query(..., description="pmi / cpi / ppi / m / sf"),
    months: int = Query(12, ge=1, le=60),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["cn_macro_monthly"]
    docs = list(
        coll.find({"data_type": data_type}, {"_id": 0})
        .sort("month", -1)
        .limit(months)
    )
    docs.reverse()
    return {"data_type": data_type, "data": docs}


@router.get("/raw-data/cn-quarterly", summary="中国季度GDP数据")
async def get_cn_quarterly(
    quarters: int = Query(8, ge=1, le=40),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["cn_macro_quarterly"]
    docs = list(
        coll.find({"data_type": "gdp"}, {"_id": 0})
        .sort("quarter", -1)
        .limit(quarters)
    )
    docs.reverse()
    return {"data": docs}


@router.get("/raw-data/us-rates", summary="美国国债收益率原始数据")
async def get_us_rates(
    data_type: str = Query(..., description="tycr / trycr / tbr / tltr / trltr"),
    days: int = Query(30, ge=1, le=3650),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["us_rates_daily"]
    docs = list(
        coll.find({"data_type": data_type}, {"_id": 0})
        .sort("date", -1)
        .limit(days)
    )
    docs.reverse()
    return {"data_type": data_type, "data": docs}
