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
    return {"scan_date": scan_date, "sectors": result}


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
    从 MongoDB 读取最近一条 market_daily 解读中内嵌的 iv_snapshot，
    同时携带缓存的 LLM 解读文本（若已生成）。
    返回格式：{iv_snapshot: {...}, interpretation: str|null, interpreted_at: str|null}
    """
    analysis_coll = get_analyzer_db()["risk_report_analysis"]
    # 从最新 market_daily 中取 iv_snapshot（不限日期，取最新有效）
    doc = analysis_coll.find_one(
        {"scope": "market_daily", "analysis.iv_snapshot.available": True},
        {"_id": 0, "analysis.iv_snapshot": 1, "scan_date": 1},
        sort=[("created_at", -1)],
    )
    iv_snap = (doc or {}).get("analysis", {}).get("iv_snapshot") if doc else None
    scan_date = (doc or {}).get("scan_date") if doc else None

    # 读取最近的 LLM 解读缓存
    interp_doc = analysis_coll.find_one(
        {"scope": "iv_interpretation"},
        {"_id": 0, "interpretation": 1, "created_at": 1, "scan_date": 1},
        sort=[("created_at", -1)],
    )
    return {
        "iv_snapshot": iv_snap,
        "snapshot_date": scan_date,
        "interpretation": (interp_doc or {}).get("interpretation"),
        "interpreted_at": (
            interp_doc["created_at"].isoformat()
            if interp_doc and isinstance(interp_doc.get("created_at"), datetime)
            else (interp_doc or {}).get("created_at")
        ),
    }


@router.post("/iv-snapshot/interpret", summary="触发A股波动率LLM专业解读（同步）")
async def interpret_iv_snapshot(
    current_user: dict = Depends(get_current_active_user),
):
    """
    读取当前 iv_snapshot，构造专业提示词，调用 Hermes LLM 同步返回解读文本。
    结果同时写入 MongoDB（scope=iv_interpretation）供下次快速读取。
    """
    import os
    import httpx

    # 1. 获取 iv_snapshot
    analysis_coll = get_analyzer_db()["risk_report_analysis"]
    doc = analysis_coll.find_one(
        {"scope": "market_daily", "analysis.iv_snapshot.available": True},
        {"_id": 0, "analysis.iv_snapshot": 1, "scan_date": 1},
        sort=[("created_at", -1)],
    )
    iv_snap = (doc or {}).get("analysis", {}).get("iv_snapshot") if doc else None
    if not iv_snap or not iv_snap.get("available"):
        raise HTTPException(503, "暂无可用的 iv_snapshot，请先触发 A股日报解读")

    indices = iv_snap.get("indices") or {}
    idx_lines = "\n".join(
        f"  {v['name']}（{v.get('label','')}）: IV={v['iv_30']}%  {v['level_zh']}"
        f"{'  认沽偏度+'+str(v['skew_pp'])+'pp' if v.get('skew_pp') else ''}"
        for v in indices.values()
    ) if indices else f"  {iv_snap.get('underlying','')}: IV={iv_snap['iv_30']}%  {iv_snap['level_zh']}"

    prompt = f"""你是专业的A股量化波动率分析师。请根据以下当日隐含波动率（IV）数据，给出一段专业、精炼的市场解读（300字以内，中文）。

【当日A股指数期权IV快照】
{idx_lines}

主要参考指数：{iv_snap.get('underlying','沪深300')}  IV={iv_snap['iv_30']}%（{iv_snap['level_zh']}）

解读要求：
1. 说明当前整体波动率水位处于什么区间（历史分位参考：<15% 极低，15-20% 正常，20-25% 偏高，>25% 高位警示）
2. 对比各指数IV差异，解读风格轮动或资金偏好信号（大盘/小盘/成长）
3. 认沽偏度（skew）含义：>3pp 表示市场对下行对冲需求旺盛，投资者情绪偏谨慎
4. 给出对短期仓位管理的具体参考建议（如仓位比例、止损策略、是否适合追涨等）

请直接输出解读正文，无需重复列出原始数据。"""

    # 2. 调用 Hermes LLM（非流式）
    hermes_base = os.getenv("HERMES_OPENAI_BASE_URL", "http://hermes:8642").rstrip("/")
    hermes_key  = os.getenv("HERMES_API_SERVER_KEY", "")
    hermes_model = os.getenv("HERMES_OPENAI_MODEL", "hermes-agent").strip()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{hermes_base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {hermes_key}", "Content-Type": "application/json"},
                json={
                    "model": hermes_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            interpretation = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise HTTPException(502, f"LLM 调用失败: {e}")

    # 3. 写入 MongoDB 缓存
    now = datetime.utcnow()
    analysis_coll.update_one(
        {"scope": "iv_interpretation"},
        {"$set": {
            "scope": "iv_interpretation",
            "interpretation": interpretation,
            "iv_snapshot": iv_snap,
            "scan_date": (doc or {}).get("scan_date"),
            "created_at": now,
            "generated_by": hermes_model,
        }},
        upsert=True,
    )

    return {
        "interpretation": interpretation,
        "interpreted_at": now.isoformat(),
        "iv_snapshot": iv_snap,
    }
