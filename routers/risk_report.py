"""市场风险预警 API

提供两类用途：
1. 前端直接读取结构化数据（风险得分、信号详情），用于图表和卡片展示
2. 触发 quantAnalyzer LLM 分析任务，结果写入 quant_analyzer.risk_report_analysis

数据来源：
  - risk_reports       (quant_data DB)        — quant-data-engine 每日写入
  - risk_report_analysis (quant_analyzer DB)   — quantAnalyzer 写入 LLM 解读
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db
import db as _app_db
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/risk-reports", tags=["市场风险预警"])

# 已知行业 key（与 quant-data-engine SECTOR_PROFILES 对应）
VALID_SECTORS = {
    "semicon", "semicon_eq", "finance", "consumer", "healthcare", "nev", "robot", "tourism",
    "aerospace",
    # 美股（阶段 3 后启用）
    "us_semicon", "us_tech", "us_macro",
}

A_SHARE_SECTORS = sorted(s for s in VALID_SECTORS if not s.startswith("us_"))


def _serialize(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _serialize_list(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_serialize(d) for d in docs if d]


# ---------------------------------------------------------------------------
# 1. 单日全行业概览 — 前端 dashboard 卡片
# ---------------------------------------------------------------------------

@router.get("/overview", summary="单日全行业风险概览")
async def get_overview(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回指定日期所有行业的风险得分和等级，用于 dashboard 卡片展示。

    若指定日期无数据，返回空列表（前端显示"暂无数据"）。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    coll = get_data_db()["risk_reports"]
    docs = list(coll.find({"scan_date": scan_date}, {"_id": 0}))

    # 只返回卡片所需字段，不包含 signals 详情
    result = [
        {
            "sector": d.get("sector"),
            "scan_date": d.get("scan_date"),
            "overall_risk_score": d.get("overall_risk_score"),
            "overall_risk_level": d.get("overall_risk_level"),
            "warnings": d.get("warnings", []),
            "generated_at": d.get("generated_at"),
        }
        for d in docs
    ]
    # 按得分降序，高风险在前
    result.sort(key=lambda x: x.get("overall_risk_score") or 0, reverse=True)

    # A 股扫描每日 19:30 才生成；盘前/盘中请求当天日期时，前端需要一个
    # 最近可看的 A 股日期来做“查看最近扫描”入口。
    latest_a = coll.find_one(
        {"sector": {"$in": A_SHARE_SECTORS}, "scan_date": {"$lte": scan_date}},
        {"scan_date": 1},
        sort=[("scan_date", -1)],
    )
    latest_a_share_date = latest_a.get("scan_date") if latest_a else None

    return {
        "scan_date": scan_date,
        "sectors": result,
        "latest_a_share_date": latest_a_share_date,
    }


# ---------------------------------------------------------------------------
# 2. 单行业单日详情 — 前端信号展开 / LLM 分析上下文
# ---------------------------------------------------------------------------

@router.get("/detail", summary="单行业单日信号详情")
async def get_detail(
    sector: str = Query(..., description="行业 key，如 semicon / nev"),
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    with_analysis: bool = Query(False, description="是否附带 LLM 解读（若已生成）"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回单个行业某天的完整信号数据，包含 5 个信号的原始值。

    设置 with_analysis=true 时，额外查询 risk_report_analysis 集合附加 LLM 解读。
    """
    if sector not in VALID_SECTORS:
        raise HTTPException(400, f"未知行业 key: {sector}")

    scan_date = date or datetime.today().strftime("%Y%m%d")
    coll = get_data_db()["risk_reports"]
    doc = coll.find_one({"scan_date": scan_date, "sector": sector}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"{scan_date} / {sector} 暂无数据")

    result = _serialize(doc)

    if with_analysis:
        analysis_coll = get_analyzer_db()["risk_report_analysis"]
        analysis = analysis_coll.find_one(
            {"scope": "sector_daily", "scan_date": scan_date, "sector": sector},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        result["llm_analysis"] = _serialize(analysis)

    return result


# ---------------------------------------------------------------------------
# 3. 单行业时序趋势 — 前端折线/点状图
# ---------------------------------------------------------------------------

@router.get("/trend", summary="单行业风险得分时序趋势")
async def get_trend(
    sector: str = Query(..., description="行业 key"),
    limit: int = Query(30, ge=1, le=90, description="返回最近 N 个交易日，最多 90"),
    start: Optional[str] = Query(None, description="起始日期 YYYYMMDD（与 limit 互斥，优先）"),
    end: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回单行业的历史风险得分序列，按日期升序排列，适合绘折线图或点图连线。

    响应格式::

        {
            "sector": "semicon",
            "data": [
                {"scan_date": "20260401", "overall_risk_score": 65.2, "overall_risk_level": "high"},
                ...
            ]
        }
    """
    if sector not in VALID_SECTORS:
        raise HTTPException(400, f"未知行业 key: {sector}")

    coll = get_data_db()["risk_reports"]
    projection = {"_id": 0, "scan_date": 1, "overall_risk_score": 1, "overall_risk_level": 1}

    if start:
        end_date = end or datetime.today().strftime("%Y%m%d")
        docs = list(
            coll.find(
                {"sector": sector, "scan_date": {"$gte": start, "$lte": end_date}},
                projection,
            ).sort("scan_date", 1)
        )
    else:
        docs = list(
            coll.find({"sector": sector}, projection)
            .sort("scan_date", -1)
            .limit(limit)
        )
        docs.reverse()  # 转为升序

    return {"sector": sector, "data": docs}


# ---------------------------------------------------------------------------
# 4. 触发 LLM 分析任务 — 单行业单日
# ---------------------------------------------------------------------------

@router.post("/analyze", summary="触发单行业 LLM 风险解读")
async def trigger_analysis(
    sector: str = Query(..., description="行业 key"),
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    向 analysis_tasks 队列投递一个 risk_report_analysis 任务，
    quantAnalyzer worker 消费后将解读结果写入 quant_analyzer.risk_report_analysis。

    任务参数 params 包含完整的结构化信号数据，quantAnalyzer 无需再查库。
    """
    if sector not in VALID_SECTORS:
        raise HTTPException(400, f"未知行业 key: {sector}")

    scan_date = date or datetime.today().strftime("%Y%m%d")

    # 读取结构化数据作为 LLM 上下文
    coll = get_data_db()["risk_reports"]
    report = coll.find_one({"scan_date": scan_date, "sector": sector}, {"_id": 0})
    if not report:
        raise HTTPException(404, f"{scan_date} / {sector} 暂无扫描数据，无法触发分析")

    # 序列化 datetime
    report = _serialize(report)

    collections = {"analysis_tasks": _app_db.db["analysis_tasks"]}
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="risk_report_analysis",
        priority=5,
        params={
            "scan_date": scan_date,
            "sector": sector,
            "scope": "sector_daily",
            "report": report,
        },
        source="manual_trigger",
    )
    result = collections["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "scan_date": scan_date,
        "sector": sector,
    }


# ---------------------------------------------------------------------------
# 5. 触发全市场日报 LLM 分析（A股）
# ---------------------------------------------------------------------------

@router.post("/analyze/market-daily", summary="触发A股市场日报 LLM 解读")
async def trigger_market_daily_analysis(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    将当日 A 股行业（非 us_ 前缀）扫描结果合并，投递 market_daily 任务。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    coll = get_data_db()["risk_reports"]
    docs = list(coll.find({"scan_date": scan_date}, {"_id": 0}))
    # 只保留 A 股板块
    docs = [d for d in docs if not (d.get("sector") or "").startswith("us_")]
    if not docs:
        raise HTTPException(404, f"{scan_date} 暂无 A 股行业扫描数据")

    docs = _serialize_list(docs)
    docs.sort(key=lambda x: x.get("overall_risk_score") or 0, reverse=True)

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="risk_report_analysis",
        priority=4,
        params={
            "scan_date": scan_date,
            "sector": None,
            "scope": "market_daily",
            "reports": docs,
        },
        source="manual_trigger",
    )
    result = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "scan_date": scan_date,
        "sector_count": len(docs),
    }


# ---------------------------------------------------------------------------
# 5b. 触发美股市场日报 LLM 分析
# ---------------------------------------------------------------------------

@router.post("/analyze/us-market-daily", summary="触发美股市场日报 LLM 解读")
async def trigger_us_market_daily_analysis(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    将当日美股板块（us_ 前缀）扫描结果合并，投递 us_market_daily 任务。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    coll = get_data_db()["risk_reports"]
    docs = list(coll.find({"scan_date": scan_date}, {"_id": 0}))
    # 只保留美股板块
    docs = [d for d in docs if (d.get("sector") or "").startswith("us_")]
    if not docs:
        raise HTTPException(404, f"{scan_date} 暂无美股板块扫描数据")

    docs = _serialize_list(docs)
    docs.sort(key=lambda x: x.get("overall_risk_score") or 0, reverse=True)

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="risk_report_analysis",
        priority=4,
        params={
            "scan_date": scan_date,
            "sector": None,
            "scope": "us_market_daily",
            "reports": docs,
        },
        source="manual_trigger",
    )
    result = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "scan_date": scan_date,
        "sector_count": len(docs),
    }


# ---------------------------------------------------------------------------
# 6. 获取全市场日报 LLM 解读结果（A股）
# ---------------------------------------------------------------------------

@router.get("/market-daily-analysis", summary="获取A股市场日报 LLM 解读结果")
async def get_market_daily_analysis(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回指定日期的 A 股市场风险日报 LLM 解读结果（scope=market_daily）。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    analysis_coll = get_analyzer_db()["risk_report_analysis"]
    doc = analysis_coll.find_one(
        {"scope": "market_daily", "scan_date": scan_date},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    return {"scan_date": scan_date, "analysis": _serialize(doc)}


# ---------------------------------------------------------------------------
# 6b. 获取美股市场日报 LLM 解读结果
# ---------------------------------------------------------------------------

@router.get("/us-market-daily-analysis", summary="获取美股市场日报 LLM 解读结果")
async def get_us_market_daily_analysis(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    返回指定日期的美股市场风险日报 LLM 解读结果（scope=us_market_daily）。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    analysis_coll = get_analyzer_db()["risk_report_analysis"]
    doc = analysis_coll.find_one(
        {"scope": "us_market_daily", "scan_date": scan_date},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    return {"scan_date": scan_date, "analysis": _serialize(doc)}


# ---------------------------------------------------------------------------
# 7. A股波动率快照 — 独立卡片数据 + LLM 解读
# ---------------------------------------------------------------------------

@router.get("/iv-snapshot", summary="获取A股最新四指数波动率快照")
async def get_iv_snapshot(
    current_user: dict = Depends(get_current_active_user),
):
    """
    从 MongoDB quant_analyzer.iv_analysis 读取最新波动率快照与LLM解读。
    数据由 quantAnalyzer iv_analysis 任务异步生产（Tushare + LLM），
    本接口只做纯读操作，无跨包依赖。

    返回格式：{iv_snapshot, snapshot_date, snapshot_source, interpretation, interpreted_at}
    snapshot_source: "db_cache"（iv_analysis集合）| "market_daily_fallback"（旧 risk_report_analysis 兜底）
    """
    iv_coll = get_analyzer_db()["iv_analysis"]
    iv_snap: Optional[Dict[str, Any]] = None
    scan_date: Optional[str] = None
    interpretation: Optional[str] = None
    interpreted_at: Optional[str] = None
    alert: Optional[Dict[str, Any]] = None
    snapshot_source = "db_cache"

    # ── 1. quant_analyzer.iv_analysis（主数据源）─────────────────────────
    doc = iv_coll.find_one(
        {"iv_snapshot.available": True},
        {"_id": 0},
        sort=[("generated_at", -1)],
    )
    if doc:
        iv_snap = doc.get("iv_snapshot")
        scan_date = doc.get("analysis_date")
        interpretation = doc.get("interpretation")
        interpreted_at = doc.get("interpreted_at")
        alert = doc.get("alert")

    # ── 2. 旧 risk_report_analysis.market_daily 兜底（向后兼容）──────────
    if not iv_snap:
        analysis_coll = get_analyzer_db()["risk_report_analysis"]
        fb_doc = analysis_coll.find_one(
            {"scope": "market_daily", "analysis.iv_snapshot.available": True},
            {"_id": 0, "analysis.iv_snapshot": 1, "scan_date": 1},
            sort=[("created_at", -1)],
        )
        iv_snap = (fb_doc or {}).get("analysis", {}).get("iv_snapshot") if fb_doc else None
        scan_date = (fb_doc or {}).get("scan_date") if fb_doc else None
        snapshot_source = "market_daily_fallback"

    return {
        "iv_snapshot":     iv_snap,
        "snapshot_date":   scan_date,
        "snapshot_source": snapshot_source,
        "interpretation":  interpretation,
        "interpreted_at":  interpreted_at,
        "alert":           alert,
    }


@router.post("/iv-snapshot/interpret", summary="触发A股波动率分析任务（Tushare + LLM解读）")
async def trigger_iv_analysis(
    date: str = Query(None, description="分析日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    向 analysis_tasks 队列投递 iv_analysis 任务。
    quantAnalyzer worker 消费后将：
      1. 从 Tushare 拉取四指数期权结算价，计算IV
      2. 调用 LLM 生成专业解读
      3. 结果写入 quant_analyzer.iv_analysis

    本接口立即返回 task_id，客户端轮询 GET /iv-snapshot 获取最终结果。
    """
    scan_date = date or datetime.today().strftime("%Y%m%d")
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="iv_analysis",
        priority=3,
        params={"date": scan_date, "with_llm": True},
        source="manual_trigger",
    )
    result = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "analysis_date": scan_date,
        "message": "IV分析任务已提交，quantAnalyzer 将从 Tushare 拉取数据并完成LLM解读，稍后刷新查看结果",
    }
