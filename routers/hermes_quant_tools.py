"""
Hermes 容器内可调用的只读 quant-api 端点（共享密钥，无用户 JWT）。

搭配 docker-compose 中 QUANT_API_URL + HERMES_QUANT_INTERNAL_KEY。

端点一览：
  GET  /hermes-tools/ladder/daily          — 连板天梯
  GET  /hermes-tools/ladder/narrative      — 涨停分析（LLM 叙事，盘后可用）
  GET  /hermes-tools/market/summary        — AI 大盘分析（市场总结）
  GET  /hermes-tools/market/global         — 全球市场简报
  GET  /hermes-tools/market/hot-stock       — 热股分析（THS/DC 最新）
  GET  /hermes-tools/macro/analysis          — 宏观 AI 分析（中国/美国）
  GET  /hermes-tools/sector/latest           — 最新板块/概念 AI 分析
  GET  /hermes-tools/earnings/signals        — 财报猎手异动信号
  POST /hermes-tools/analysis/deep         — 提交深度分析任务
  POST /hermes-tools/analysis/deep-and-wait — 提交并等待完成（推荐）
  GET  /hermes-tools/analysis/task/{id}    — 轮询任务状态
  GET  /hermes-tools/analysis/history      — 查某股历史分析
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import require_hermes_quant_internal_key
from db import db, get_analyzer_db, get_db_collections
from models.analysis_task import AnalysisMode
from routers.market import fetch_market_analysis_result
from services.analysis_history import build_user_analysis_history_payload
from services.analysis_tasks import build_task_doc
from services.ladder_daily import build_daily_ladder_payload

router = APIRouter(prefix="/hermes-tools", tags=["Hermes quant tools"])

# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _resolve_user(username: Optional[str]) -> Dict[str, Any]:
    """根据 username 查库；未提供则返回 hermes-system 虚拟用户。"""
    if username:
        user = db["users"].find_one({"username": username})
        if user:
            return {"id": str(user["_id"]), "username": user["username"],
                    "is_admin": user.get("is_admin", False)}
    return {"id": "hermes-system", "username": "hermes-system", "is_admin": False}


def _norm_task_status(raw: Any) -> str:
    if raw is None:
        return "unknown"
    if hasattr(raw, "value"):
        return str(raw.value).lower()
    s = str(raw)
    if s.startswith("TaskStatus."):
        s = s.split(".", 1)[1]
    return s.lower()


# ---------------------------------------------------------------------------
# 连板天梯
# ---------------------------------------------------------------------------

@router.get("/ladder/daily", operation_id="hermes_ladder_daily")
async def hermes_ladder_daily(
    date: Optional[str] = Query(None, description="YYYYMMDD，默认今天"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """供 Hermes skill / curl 拉取连板天梯 JSON（需 X-Hermes-Quant-Key）。"""
    return build_daily_ladder_payload(date)


# ---------------------------------------------------------------------------
# 深度分析
# ---------------------------------------------------------------------------

class DeepAnalysisRequest(BaseModel):
    symbol: str
    analysis_mode: AnalysisMode = AnalysisMode.CLASSIC
    username: Optional[str] = None  # 归属到哪个用户；留空则用 hermes-system
    provider: Optional[str] = None
    model: Optional[str] = None


@router.post("/analysis/deep", operation_id="hermes_submit_deep_analysis")
async def hermes_submit_deep_analysis(
    body: DeepAnalysisRequest,
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    【高级用途】仅提交任务并立即返回 task_id，不等待结果。

    **绝大多数情况请改用 hermes_deep_analysis_wait**，它一次调用即可拿到完整分析结果。
    本接口仅供需要异步追踪多个任务的高级场景使用。
    """
    collections = get_db_collections()
    user = _resolve_user(body.username)

    task_doc = build_task_doc(
        current_user=user,
        task_type="deep_analysis",
        priority=30,
        symbol=body.symbol,
        params={"analysis_mode": body.analysis_mode.value},
        provider=body.provider,
        model=body.model,
        source="hermes_mcp",
    )
    result = collections["analysis_tasks"].insert_one(task_doc)
    task_id = str(result.inserted_id)

    queue_depth = collections["analysis_tasks"].count_documents({"status": "pending"})
    return {
        "success": True,
        "task_id": task_id,
        "status": "pending",
        "symbol": body.symbol,
        "analysis_mode": body.analysis_mode.value,
        "attributed_to": user["username"],
        "queue_depth": queue_depth,
        "message": f"深度分析任务已提交，队列中共 {queue_depth} 个待处理任务，通常 30-120 秒完成",
    }


@router.post("/analysis/deep-and-wait", operation_id="hermes_deep_analysis_wait")
async def hermes_deep_analysis_wait(
    body: DeepAnalysisRequest,
    timeout: int = Query(180, ge=30, le=300, description="最长等待秒数，默认 180"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    提交深度分析任务并等待完成后返回结果（单次调用，无需轮询）。

    服务端内部每 5 秒轮询一次，最长等待 timeout 秒（默认 180）。
    适合 Hermes 等不擅长多轮工具调用循环的场景。
    """
    collections = get_db_collections()
    user = _resolve_user(body.username)

    task_doc = build_task_doc(
        current_user=user,
        task_type="deep_analysis",
        priority=30,
        symbol=body.symbol,
        params={"analysis_mode": body.analysis_mode.value},
        provider=body.provider,
        model=body.model,
        source="hermes_mcp",
    )
    insert_result = collections["analysis_tasks"].insert_one(task_doc)
    task_id = str(insert_result.inserted_id)
    oid = insert_result.inserted_id

    # 服务端轮询直到完成
    deadline = timeout
    elapsed = 0
    poll_interval = 10
    while elapsed < deadline:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        task = collections["analysis_tasks"].find_one({"_id": oid})
        if not task:
            raise HTTPException(status_code=404, detail="任务丢失")
        status = _norm_task_status(task.get("status"))
        if status in ("completed", "failed", "completed_with_parse_error"):
            break

    task = collections["analysis_tasks"].find_one({"_id": oid})
    status = _norm_task_status(task.get("status"))

    if status == "completed":
        result = task.get("result", {}) or {}
        # worker 将分析字段直接存在 result 顶层，没有嵌套的 result.analysis
        analysis = result.get("analysis") or result or None
        return {
            "success": True,
            "task_id": task_id,
            "symbol": body.symbol,
            "status": "completed",
            "analysis": analysis,
            "model": result.get("model"),
            "elapsed_seconds": elapsed,
        }
    elif status == "failed":
        return {
            "success": False,
            "task_id": task_id,
            "symbol": body.symbol,
            "status": "failed",
            "error": task.get("error", "分析失败"),
        }
    else:
        return {
            "success": False,
            "task_id": task_id,
            "symbol": body.symbol,
            "status": status,
            "error": f"超时（{timeout}秒内未完成），可用 task_id 继续查询",
        }


@router.get("/analysis/task/{task_id}", operation_id="hermes_get_task_status")
async def hermes_get_task_status(
    task_id: str,
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    轮询深度分析任务状态。

    status 取值：pending / processing / completed / failed
    completed 时 analysis 字段含分析正文。
    """
    collections = get_db_collections()
    try:
        task = collections["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="task_id 格式无效")

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    status = _norm_task_status(task.get("status"))
    resp: Dict[str, Any] = {
        "task_id": task_id,
        "symbol": task.get("symbol"),
        "status": status,
        "analysis_mode": task.get("params", {}).get("analysis_mode", "classic"),
        "created_at": task["created_at"].isoformat() if task.get("created_at") else None,
    }

    if status == "completed":
        result = task.get("result", {}) or {}
        resp["success"] = result.get("success", True)
        # worker 将分析字段直接存在 result 顶层，没有嵌套的 result.analysis
        resp["analysis"] = result.get("analysis") or result or None
        resp["model"] = result.get("model")
    elif status == "failed":
        resp["error"] = task.get("error", "分析失败")

    return resp


@router.get("/analysis/history", operation_id="hermes_get_analysis_history")
async def hermes_get_analysis_history(
    symbol: str = Query(..., description="股票代码，例如 600519 或 600519.SH"),
    username: Optional[str] = Query(None, description="归属用户；留空则查 hermes-system"),
    limit: int = Query(5, ge=1, le=20, description="返回条数"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    查询某股的最新深度分析历史（已完成的任务写入 analysis_history 集合）。

    返回最近 limit 条，含分析正文摘要。
    """
    user = _resolve_user(username)
    payload = build_user_analysis_history_payload(
        user["id"],
        symbol=symbol,
        page=1,
        limit=limit,
    )
    return payload


# ---------------------------------------------------------------------------
# 涨停全景叙事
# ---------------------------------------------------------------------------

@router.get("/ladder/narrative", operation_id="hermes_ladder_narrative")
async def hermes_ladder_narrative(
    date: Optional[str] = Query(None, description="交易日 YYYYMMDD，默认最新有数据的交易日"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取 LLM 生成的涨停全景市场动向总结。

    包含：今日涨停全貌（含炸板）、主线格局、天梯健康度、与昨日关键变化、市场方向解读。
    数据已预先生成，通常每个交易日盘后自动更新，无需等待。
    """
    narrative_col = get_analyzer_db()["limit_up_daily_narrative"]

    if not date:
        latest = narrative_col.find_one(sort=[("trade_date", -1)])
        date = (latest or {}).get("trade_date", "")

    doc = narrative_col.find_one({"trade_date": date})
    if not doc:
        return {
            "success": False,
            "trade_date": date,
            "status": "not_found",
            "message": f"{date} 暂无涨停全景总结，通常盘后 19:00 后可用",
        }

    return {
        "success": True,
        "trade_date": doc["trade_date"],
        "headline": doc.get("headline", ""),
        "sentiment_signal": doc.get("sentiment_signal", ""),
        "narrative_markdown": doc.get("narrative_markdown", ""),
        "model": doc.get("model"),
    }


# ---------------------------------------------------------------------------
# AI 大盘分析（市场总结）
# ---------------------------------------------------------------------------

@router.get("/market/summary", operation_id="hermes_market_summary")
async def hermes_market_summary(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认最新交易日"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取 AI 大盘分析结果（市场总结）。

    包含：市场情绪、板块轮动、资金流向、风险提示、操作建议。
    数据由系统在交易日 10:00 / 11:30 / 15:30 自动生成，读取缓存不触发 LLM 调用。
    """
    result = fetch_market_analysis_result(date)
    # 精简返回，去掉 home_summary 等前端专用字段
    keys = [
        "success", "analysis_date", "notice", "user_tip", "error",
        "market_sentiment", "sector_rotation", "fund_flow",
        "risk_warning", "operation_suggestion", "summary",
        "overall_trend", "key_stocks", "market_overview",
    ]
    return {k: result[k] for k in keys if k in result}


# ---------------------------------------------------------------------------
# 热股分析
# ---------------------------------------------------------------------------

@router.get("/market/hot-stock", operation_id="hermes_hot_stock")
async def hermes_hot_stock(
    source: str = Query("ths", description="数据源: ths（同花顺）或 dc（东方财富）"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取最新热股分析结果（同花顺或东方财富热股榜 + AI 点评）。

    数据由系统盘中自动更新，读取缓存，不触发 LLM。
    参数 source: ths（默认）或 dc。
    """
    source = source.lower()
    if source not in ("ths", "dc"):
        return {"success": False, "error": "source 须为 ths 或 dc"}

    doc = get_analyzer_db()["hot_stock_analysis"].find_one(
        {"source": source}, sort=[("created_at", -1)]
    )
    if not doc:
        return {"success": False, "error": f"暂无 {source.upper()} 热股分析数据"}

    return {
        "success": True,
        "source": source,
        "source_name": doc.get("source_name"),
        "rank_time": doc.get("rank_time"),
        "trade_date": doc.get("trade_date"),
        "stock_count": doc.get("stock_count"),
        "analysis": doc.get("analysis"),
        "model": doc.get("model"),
        "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# 全球市场简报
# ---------------------------------------------------------------------------

@router.get("/market/global", operation_id="hermes_global_market")
async def hermes_global_market(
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取最新全球市场简报（美股、港股、外汇、大宗商品、宏观联动分析）。

    数据由系统自动生成，读取缓存，不触发 LLM 调用。
    """
    doc = get_analyzer_db()["global_analysis"].find_one(sort=[("_id", -1)])
    if not doc:
        return {"success": False, "error": "暂无全球市场分析数据"}

    # 只保留 AI 分析正文，去掉原始行情大数据（market_data / policy_data / raw_response 体积过大）
    result = {
        "success": True,
        "analysis": doc.get("analysis"),
        "analyzed_at": str(doc["analyzed_at"]) if hasattr(doc.get("analyzed_at"), "isoformat") else doc.get("analyzed_at"),
    }
    return result


# ---------------------------------------------------------------------------
# 宏观 AI 分析
# ---------------------------------------------------------------------------

@router.get("/macro/analysis", operation_id="hermes_macro_analysis")
async def hermes_macro_analysis(
    scope: str = Query("china_macro", description="china_macro（中国宏观）或 us_macro（美国利率）"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取最新宏观 AI 分析结果，读取缓存，不触发 LLM。

    - scope=china_macro — 中国宏观深度分析（PMI/CPI/PPI/M2/LPR/GDP等）
    - scope=us_macro    — 美国利率市场深度分析（联储政策/局形/收益率曲线）
    """
    if scope not in ("china_macro", "us_macro"):
        return {"success": False, "error": "scope 须为 china_macro 或 us_macro"}

    doc = get_analyzer_db()["macro_analysis"].find_one(
        {"scope": scope}, {"_id": 0}, sort=[("analysis_date", -1)]
    )
    if not doc:
        return {"success": False, "error": f"暂无 {scope} 分析数据"}

    result = dict(doc)
    for k, v in result.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
    result["success"] = True
    return result


# ---------------------------------------------------------------------------
# 板块/概念 AI 分析
# ---------------------------------------------------------------------------

@router.get("/sector/latest", operation_id="hermes_sector_latest")
async def hermes_sector_latest(
    source: str = Query("dc", description="数据源: dc（东方财富，默认）或 ths（同花顺）"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取最新板块/概念 AI 分析结果，读取缓存，不触发 LLM。

    返回当日热门板块/概念的消内容要和 AI 点评。
    """
    source = source.lower()
    if source not in ("dc", "ths"):
        return {"success": False, "error": "source 须为 dc 或 ths"}

    doc = get_analyzer_db()["sector_concept_analysis"].find_one(
        {"source": source}, sort=[("created_at", -1)]
    )
    if not doc:
        return {"success": False, "error": f"暂无 {source.upper()} 板块分析数据"}

    return {
        "success": True,
        "source": source,
        "trade_date": doc.get("trade_date"),
        "analysis": doc.get("analysis"),
        "data_summary": doc.get("data_summary"),
        "model": doc.get("model"),
        "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# 财报猎手异动信号
# ---------------------------------------------------------------------------

@router.get("/earnings/signals", operation_id="hermes_earnings_signals")
async def hermes_earnings_signals(
    days: int = Query(7, ge=1, le=30, description="扫描最近 N 天的异动信号，默认 7 天"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    财报猎手：扫描最近 N 天的业绩预告、快报和券商评级上调，
    寻找可能发生“戴维斯双击”（盈利上修 + 估值扩张）的拠点公司。

    返回信号列表，每条包括：股票代码、公司名、信号类型、核心指标、日期。
    """
    from datetime import timedelta
    from db import get_data_db

    data_db = get_data_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")
    signals = []

    # 1. 业绩预告：扫描扭亏/大幅预增
    for f in data_db["earnings_forecast"].find(
        {"ann_date": {"$gte": cutoff}, "type": {"$in": ["预增", "扭亏"]}},
        sort=[("ann_date", -1)],
    ):
        p_min = f.get("p_change_min")
        p_max = f.get("p_change_max")
        growth = ((p_min or 0) + (p_max or 0)) / 2 if p_min is not None else 0
        signals.append({
            "ts_code": f.get("ts_code"),
            "signal_type": "业绩预告",
            "subtype": f.get("type"),
            "date": f.get("ann_date"),
            "core_metric": f"净利润增速约 {growth:.1f}%" if growth else "增速未知",
            "reason": f.get("change_reason", ""),
        })

    # 2. 业绩快报：净利润同比 > 30%
    for e in data_db["earnings_express"].find(
        {"ann_date": {"$gte": cutoff}}, sort=[("ann_date", -1)]
    ):
        n = e.get("n_income")
        last = e.get("yoy_net_profit")
        if n is None or not last:
            continue
        yoy = (n - last) / abs(last) * 100
        if yoy < 30:
            continue
        revenue = e.get("revenue")
        signals.append({
            "ts_code": e.get("ts_code"),
            "signal_type": "业绩快报",
            "subtype": "实际高增",
            "date": e.get("ann_date"),
            "core_metric": f"净利润同比 {yoy:.1f}%" + (f"，营收 {revenue/1e8:.1f}亿" if revenue else ""),
            "reason": "",
        })

    # 3. 券商评级上调
    for r in data_db["earnings_report_rc"].find(
        {"report_date": {"$gte": cutoff}, "op_rt": {"$in": ["首次", "调高"]}},
        sort=[("report_date", -1)],
    ):
        signals.append({
            "ts_code": r.get("ts_code"),
            "signal_type": "卖方研报",
            "subtype": "评级" + r.get("op_rt", ""),
            "date": r.get("report_date"),
            "core_metric": f"[{r.get('org_name')}] {r.get('rating_name')}",
            "reason": r.get("title", ""),
        })

    signals.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"success": True, "days": days, "count": len(signals), "signals": signals}
