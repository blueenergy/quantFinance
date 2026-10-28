"""
Hermes 容器内可调用的 quant-api 工具端点（共享密钥，无用户 JWT）。

搭配 docker-compose 中 QUANT_API_URL + HERMES_QUANT_INTERNAL_KEY。
MCP 暴露面按 route tag 拆分为 wukong-quant-read 与 wukong-quant-actions。

端点一览：
  GET  /hermes-tools/ladder/daily          — 连板天梯
  GET  /hermes-tools/ladder/narrative      — 涨停分析（LLM 叙事，盘后可用）
  GET  /hermes-tools/market/summary        — AI 大盘分析（市场总结）
  GET  /hermes-tools/market/events-weekly  — 市场事件日历（未来两周）
  GET  /hermes-tools/market/global         — 全球市场简报
  GET  /hermes-tools/market/hot-stock       — 热股分析（THS/DC 最新）
  GET  /hermes-tools/macro/analysis          — 宏观 AI 分析（中国/美国）
  GET  /hermes-tools/sector/latest           — 最新板块/概念 AI 分析
  GET  /hermes-tools/earnings/signals        — 财报猎手异动信号
  GET  /hermes-tools/stock/ranking            — 股票评分排行榜 Top N（按指数）
  GET  /hermes-tools/stock/score-detail       — 单股六维评分详情
  GET  /hermes-tools/stock/valuation-metrics  — 单股科技估值结构化指标
  GET  /hermes-tools/stock/earnings-consensus — 单股卖方一致预期
  GET  /hermes-tools/stock/industry-comparison — 单股行业横向估值/财务分位
  POST /hermes-tools/analysis/deep         — 提交深度分析任务
  POST /hermes-tools/analysis/deep-and-wait — 提交并等待完成（推荐）
  GET  /hermes-tools/analysis/task/{id}    — 轮询任务状态
  GET  /hermes-tools/analysis/history      — 查某股历史分析
  GET  /hermes-tools/shenwan/pe-pb           — 申万行业指数月度 PE/PB 历史分位
  GET  /hermes-tools/stock/screen            — 智能选股：多条件财务筛选 + 申万行业
  GET  /hermes-tools/stock/foreign-top10-floatholders — 外资投行进入前十大流通股东筛选

  — 组合研究（参数扫描）端点（需 X-User-Token）—
  GET  /hermes-tools/portfolio-research/jobs            — 列出组合研究任务，定位 job_id
  GET  /hermes-tools/portfolio-research/result/{job_id} — 结果 + 按轴归因（稳定性 + 配对比较）

  — 个人化端点（需先 user_login 获取 token 并设置 X-User-Token header）—
  POST /hermes-tools/user/login            — 用户登录，返回 JWT token
  GET  /hermes-tools/user/profile          — 当前用户账户信息
  GET  /hermes-tools/user/watchlist        — 自选股列表 + 最新量化评分排行
  POST /hermes-tools/user/watchlist/add    — 添加股票到自选股
  DEL  /hermes-tools/user/watchlist/remove/{symbol} — 从自选股删除
  GET  /hermes-tools/user/watchlist/history — 自选股历史深度分析记录
"""

import asyncio
import itertools
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_user_from_header_token, require_hermes_quant_internal_key
from db import data_db, db, get_analyzer_db, get_db_collections, get_trading_db_collections
from models.analysis_task import AnalysisMode
from routers.market import fetch_market_analysis_result
from routers.portfolio_research import _load_full_result_rows
from services.analysis_history import build_user_analysis_history_payload
from services.analysis_tasks import build_task_doc
from services.backtest_batch_schemas import BacktestBatchCreate, BacktestCombo
from services.backtest_batch_service import (
    create_batch,
    list_all_batch_result_rows,
    list_batch_result_rows,
    refresh_batch_summary,
)
from services.ladder_daily import build_daily_ladder_payload

MCP_READ_TAG = "Hermes quant read tools"
MCP_ACTION_TAG = "Hermes quant action tools"

router = APIRouter(prefix="/hermes-tools")
logger = logging.getLogger(__name__)
MCP_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("MCP_ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))

_FOREIGN_HOLDER_PATTERNS = (
    ("GoldmanSachs", "高盛", re.compile(r"GOLDMAN|高盛", re.I)),
    ("MorganStanley", "摩根士丹利", re.compile(r"MORGAN\s*STANLEY|摩根士丹利|摩根斯坦利", re.I)),
    ("UBS", "瑞银/UBS", re.compile(r"\bUBS\s*AG\b|\bUBS\b|瑞银", re.I)),
    ("Barclays", "巴克莱/Barclays", re.compile(r"BARCLAYS?|BARCARLY|巴克莱", re.I)),
)


def _format_expiry(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"

# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _resolve_user(username: Optional[str], x_user_token: Optional[str] = None) -> Dict[str, Any]:
    """优先使用 MCP 登录 token；未提供时按 username 归属，最后回退到 hermes-system。"""
    if x_user_token:
        return get_user_from_header_token(x_user_token=x_user_token)
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

@router.get("/ladder/daily", operation_id="ladder_daily", tags=[MCP_READ_TAG])
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


@router.post("/analysis/deep", operation_id="submit_deep", tags=[MCP_ACTION_TAG])
async def hermes_submit_deep_analysis(
    body: DeepAnalysisRequest,
    x_user_token: Optional[str] = Header(None, alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    【高级用途】仅提交任务并立即返回 task_id，不等待结果。

    **绝大多数情况请改用 hermes_deep_analysis_wait**，它一次调用即可拿到完整分析结果。
    本接口仅供需要异步追踪多个任务的高级场景使用。
    """
    collections = get_db_collections()
    user = _resolve_user(body.username, x_user_token)

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


@router.post("/analysis/deep-and-wait", operation_id="deep_analysis", tags=[MCP_ACTION_TAG])
async def hermes_deep_analysis_wait(
    body: DeepAnalysisRequest,
    timeout: int = Query(180, ge=30, le=300, description="最长等待秒数，默认 180"),
    x_user_token: Optional[str] = Header(None, alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    提交深度分析任务并等待完成后返回结果（单次调用，无需轮询）。

    服务端内部每 5 秒轮询一次，最长等待 timeout 秒（默认 180）。
    适合 Hermes 等不擅长多轮工具调用循环的场景。
    """
    collections = get_db_collections()
    user = _resolve_user(body.username, x_user_token)

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
            "attributed_to": user["username"],
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


@router.get("/analysis/task/{task_id}", operation_id="task_status", tags=[MCP_ACTION_TAG])
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


@router.get("/analysis/history", operation_id="analysis_history", tags=[MCP_READ_TAG])
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
        include_full=True,
    )
    return payload


# ---------------------------------------------------------------------------
# 涨停全景叙事
# ---------------------------------------------------------------------------

@router.get("/ladder/narrative", operation_id="ladder_narrative", tags=[MCP_READ_TAG])
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

@router.get("/market/summary", operation_id="market_summary", tags=[MCP_READ_TAG])
async def hermes_market_summary(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认最新交易日"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取 AI 大盘分析结果（市场总结）。

    包含：市场情绪、市场概览、宏观驱动、量能/板块、资金面（北向/两融）、关键要点、
    市场展望、风险等级与风险提示、仓位档位与操作倾向、事件观察点。
    数据由系统在交易日 11:30 / 16:30 自动生成，读取缓存不触发 LLM 调用。
    """
    result = fetch_market_analysis_result(date)
    # 精简返回，去掉 home_summary / iv_snapshot 等前端与快照专用字段。
    # 注意：这里必须是 prompts.yaml 的 market_analysis_prompt schema 里真实存在的 key
    # （2026-09-23 之前列了 8 个早已不存在的字段，且漏掉了 riskNote/outlook/keyPoints，
    #  导致钉钉推送只拿得到 summary 一个正文字段）。
    keys = [
        "success", "analysis_date", "notice", "user_tip", "error",
        "mood", "summary", "keyPoints",
        "macroDrivers", "volumeAnalysis", "sectorPerformance",
        "northboundFlow", "marginFlow", "capitalFlowInsight", "sectorFundFlow",
        "outlook", "riskLevel", "riskNote",
        "positionLevel", "operationBias",
        "ladderInsight", "mainlineAnalysis", "abnormalStockInsight",
        "next_trading_days", "industry_signals", "event_watch",
    ]
    return {k: result[k] for k in keys if k in result}


@router.get("/market/events-weekly", operation_id="market_events_weekly", tags=[MCP_READ_TAG])
async def hermes_market_events_weekly(
    min_importance: int = Query(3, ge=1, le=4),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """本周/未来两周市场事件日历（宏观 + A股公司事件），供悟空「本周事件」简报。"""
    from routers.market_events import query_market_events

    payload = query_market_events(min_importance=min_importance, limit=200)
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for ev in payload.get("events") or []:
        day = str(ev.get("event_time") or "")[:10]
        by_day.setdefault(day, []).append(
            {
                "time": ev.get("event_time"),
                "title": ev.get("title"),
                "region": ev.get("region"),
                "category": ev.get("category"),
                "importance": ev.get("importance"),
                "brief": (ev.get("llm_brief") or {}).get("summary"),
            }
        )
    return {
        "success": True,
        "from": payload.get("from"),
        "to": payload.get("to"),
        "count": payload.get("count"),
        "by_day": by_day,
    }


# ---------------------------------------------------------------------------
# 热股分析
# ---------------------------------------------------------------------------

@router.get("/market/hot-stock", operation_id="hot_stock", tags=[MCP_READ_TAG])
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

@router.get("/market/global", operation_id="global_market", tags=[MCP_READ_TAG])
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

@router.get("/macro/analysis", operation_id="macro_analysis", tags=[MCP_READ_TAG])
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

@router.get("/sector/latest", operation_id="sector_latest", tags=[MCP_READ_TAG])
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

@router.get("/earnings/signals", operation_id="earnings_signals", tags=[MCP_READ_TAG])
async def hermes_earnings_signals(
    days: int = Query(7, ge=1, le=30, description="扫描最近 N 天的异动信号，默认 7 天"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    财报猎手：扫描最近 N 天的业绩预告和快报，
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

    signals.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"success": True, "days": days, "count": len(signals), "signals": signals}


# ---------------------------------------------------------------------------
# 股票评分排行榜
# ---------------------------------------------------------------------------

_RANKING_INDEX_CODES = ("hs300", "a500", "csi500", "csi1000", "csi2000", "star50")


@router.get("/stock/ranking", operation_id="stock_ranking", tags=[MCP_READ_TAG])
async def stock_ranking(
    index_code: str = Query("hs300", description="指数: hs300/a500/csi500/csi1000/csi2000/star50"),
    top: int = Query(20, ge=1, le=100, description="返回前 N 名，默认 20"),
    strategy: str = Query("balanced", description="策略: balanced/aggressive/conservative"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取 A 股量化评分排行榜 Top N（按指数成分筛选）。

    - index_code: hs300（沪深300）、a500、csi500（中证500）、csi1000、csi2000（中证2000）、star50（科创50）
    - strategy: balanced（均衡，默认）、aggressive（进取）、conservative（保守）
    - 返回每只股票的综合评分及六维子评分（周期/价值/基本面/成长/技术/资金流）
    """
    ic = index_code.strip().lower()
    if ic not in _RANKING_INDEX_CODES:
        return {"success": False, "error": f"不支持的 index_code: {ic}，可选: {', '.join(_RANKING_INDEX_CODES)}"}

    from stock_data_access.score_utils import extract_strategy_score, safe_round
    db = get_db_collections()
    stock_scores = db["stock_scores"]
    stock_info = db["stock_info"]

    # Step 1: get index member symbols from historical index_codes records
    hs_symbols = list(stock_scores.distinct("symbol", {"index_codes": ic}))
    if not hs_symbols:
        return {"success": False, "error": f"未找到 {ic} 成分股数据，index_codes 可能尚未回填"}

    # Step 2: get latest score for each member symbol
    pipeline = [
        {"$match": {"symbol": {"$in": hs_symbols}}},
        {"$sort": {"score_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {
            "_id": 0, "symbol": 1, "score_date": 1,
            "composite_score": 1, "cycle_score": 1, "value_score": 1,
            "fundamental_score": 1, "growth_score": 1, "technical_score": 1,
            "money_flow_score": 1,
        }},
    ]
    raw = list(stock_scores.aggregate(pipeline))
    raw.sort(key=lambda d: extract_strategy_score(d.get("composite_score", 0), strategy), reverse=True)
    raw = raw[:top]

    symbols = [d["symbol"] for d in raw if d.get("symbol")]
    info_map = {
        info["symbol"]: info
        for info in stock_info.find({"symbol": {"$in": symbols}}, {"symbol": 1, "name": 1, "industry": 1})
        if info.get("symbol")
    }

    result = []
    for i, d in enumerate(raw, 1):
        sym = d.get("symbol", "")
        inf = info_map.get(sym, {})
        cs = d.get("composite_score", 0)
        score_val = extract_strategy_score(cs, strategy)
        result.append({
            "rank": i,
            "symbol": sym,
            "name": inf.get("name", ""),
            "industry": inf.get("industry", ""),
            "score": safe_round(score_val, 2),
            "score_date": d.get("score_date", ""),
            "cycle_score": safe_round(d.get("cycle_score", 0), 2),
            "value_score": safe_round(d.get("value_score", 0), 2),
            "fundamental_score": safe_round(d.get("fundamental_score", 0), 2),
            "growth_score": safe_round(d.get("growth_score", 0), 2),
            "technical_score": safe_round(d.get("technical_score", 0), 2),
            "money_flow_score": safe_round(d.get("money_flow_score", 0), 2),
        })

    return {
        "success": True,
        "index_code": ic,
        "strategy": strategy,
        "top": top,
        "count": len(result),
        "ranking": result,
    }


# ---------------------------------------------------------------------------
# 单股科技估值结构化指标
# ---------------------------------------------------------------------------

def _collection_from(container: Any, name: str) -> Any:
    if isinstance(container, dict):
        return container.get(name)
    try:
        return container[name]
    except Exception:
        return None


def _to_float_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _round_or_none(value: Any, digits: int = 4) -> Optional[float]:
    number = _to_float_or_none(value)
    if number is None:
        return None
    return round(number, digits)


def _recent_docs_for_valuation(
    collection: Any,
    candidates: List[str],
    *,
    date_field: str,
    limit: int,
    projection: Dict[str, int],
    fields: tuple[str, ...] = ("ts_code", "symbol"),
) -> List[Dict[str, Any]]:
    if collection is None or not candidates:
        return []
    for field in fields:
        try:
            rows = list(
                collection.find(
                    {field: {"$in": candidates}},
                    projection,
                    sort=[(date_field, -1), ("_id", -1)],
                    limit=limit,
                )
            )
        except Exception:
            continue
        if rows:
            return rows
    return []


def _latest_stock_info_for_valuation(collection: Any, candidates: List[str]) -> Dict[str, Any]:
    rows = _recent_docs_for_valuation(
        collection,
        candidates,
        date_field="valuation_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "name": 1,
            "industry": 1,
            "sw_l1": 1,
            "sw_l2": 1,
            "sw_l3": 1,
        },
    )
    if rows:
        return rows[0]
    # Some stock_info rows do not carry valuation_date; fall back to a plain find.
    if collection is None:
        return {}
    for field in ("symbol", "ts_code"):
        try:
            rows = list(
                collection.find(
                    {field: {"$in": candidates}},
                    {
                        "_id": 0,
                        "symbol": 1,
                        "ts_code": 1,
                        "name": 1,
                        "industry": 1,
                        "sw_l1": 1,
                        "sw_l2": 1,
                        "sw_l3": 1,
                    },
                    limit=1,
                )
            )
        except Exception:
            continue
        if rows:
            return rows[0]
    return {}


def _first_positive(*values: Any) -> Optional[float]:
    for value in values:
        number = _to_float_or_none(value)
        if number is not None and number > 0:
            return number
    return None


def _valuation_symbol_candidates(symbol: str) -> List[str]:
    variants = list(_symbol_variants(symbol))
    if "." not in symbol and len(symbol) == 6 and symbol.isdigit():
        variants.extend([f"{symbol}.SH", f"{symbol}.SZ", f"{symbol}.BJ"])
    return list(dict.fromkeys(variants))


_VALUATION_MISSING_NOTES = {
    "financial_daily_basic": "缺少日线基本面快照，PE/PB/PS 与市值均无法取得",
    "financial_daily_basic.ps_ttm": "缺少 PS(TTM)，无法做 PS/EV-Sales 估值",
    "financial_daily_basic.pe_ttm": "缺少 PE(TTM)，无法做 PEG/PE 交叉验证",
    "financial_daily_basic.pb": "缺少 PB，无法做 PB-ROE 参考",
    "financial_indicator": "缺少财务指标，研发强度/利润率/现金流均不可得",
    "financial_indicator.rd_exp": "缺少研发支出，无法计算研发强度",
    "financial_indicator.fcff": "缺少 FCFF，无法判断现金流质量与 DCF 适用性",
    "financial_indicator.grossprofit_margin": "缺少毛利率，盈利质量画像不完整",
    "financial_indicator.netprofit_margin": "缺少净利率，无法算 Rule of 40(净利口径)",
    "financial_indicator.tr_yoy/or_yoy": "缺少收入同比，无法判断成长性",
    "financial_indicator.q_netprofit_margin": "缺少单季度净利率，无法交叉验证异常净利率",
    "financial_indicator.dtprofit_to_profit": "缺少扣非净利润占比，无法判断利润质量",
    "financial_indicator.opincome_of_ebt": "缺少营业利润/利润总额比，无法判断主业利润占比",
    "financial_indicator.investincome_of_ebt": "缺少投资收益/利润总额比，无法识别非主业利润贡献",
    "financial_indicator.salescash_to_or": "缺少销售收现比，无法判断收入变现质量",
    "financial_indicator.ocf_to_or": "缺少经营现金流/营收比，无法判断现金流质量",
    "financial_indicator.ocf_yoy": "缺少经营现金流同比，无法判断现金流趋势",
    "financial_indicator.fixed_assets": "缺少固定资产，无法做资产效率 proxy 分析",
    "financial_indicator.total_fa_trun": "缺少固定资产周转率，无法做资产效率分析",
    "financial_indicator.capitalized_to_da": "缺少资本化/折旧摊销比，无法判断研发资本化程度",
    "financial_indicator.roic_yearly": "缺少年化 ROIC，无法做投入资本回报分析",
    "financial_income": "缺少利润表，无法取营收用于研发强度/FCFF margin",
    "financial_income.revenue/total_revenue": "缺少营收，研发强度与 FCFF margin 无法计算",
    "financial_mainbz": "缺少主营构成(fina_mainbz)，无法做分部收入/SOTP 分析",
}

_QDE_COMMAND_PREFIX = "cd quant-data-engine && PYTHONPATH=src python -m "

_DATA_QUALITY_STATUS_MUST_FIX = {
    "missing_collection",
    "missing_field",
    "insufficient_history",
    "insufficient_consensus",
}

_VALUATION_REPAIR_COMMANDS = {
    "financial_daily_basic": (
        _QDE_COMMAND_PREFIX
        + "quant_data_engine.tasks.run_stock_daily --mode by-stock --codes {symbol} "
        "--start {start} --end {end} --basic-only"
    ),
    "financial_indicator": (
        _QDE_COMMAND_PREFIX
        + "quant_data_engine.tasks.run_financial_sync --codes {symbol} "
        "--start {start} --data-types financial_indicator"
    ),
    "financial_income": (
        _QDE_COMMAND_PREFIX
        + "quant_data_engine.tasks.run_financial_sync --codes {symbol} "
        "--start {start} --data-types income"
    ),
    "financial_mainbz": (
        _QDE_COMMAND_PREFIX
        + "quant_data_engine.tasks.run_financial_sync --codes {symbol} "
        "--start {start} --data-types mainbz"
    ),
    "earnings_report_rc": _QDE_COMMAND_PREFIX + "quant_data_engine.tasks.run_earnings_sync --days 365",
}

_MAINBZ_TYPE_LABELS = {"P": "按产品", "D": "按地区", "I": "按行业"}

_QUALITY_PROFIT_FIELDS = (
    "q_netprofit_margin",
    "dtprofit_to_profit",
    "opincome_of_ebt",
    "investincome_of_ebt",
)
_QUALITY_CASH_FLOW_FIELDS = ("salescash_to_or", "ocf_to_or", "ocf_yoy")
_QUALITY_ASSET_EFFICIENCY_FIELDS = (
    "fixed_assets",
    "total_fa_trun",
    "capitalized_to_da",
    "roic_yearly",
)
_QUALITY_NOTES = (
    "profit_quality: 单季净利率/扣非比/利润结构用于交叉验证异常净利率与非经常性损益；"
    "cash_flow_quality: 销售收现比与经营现金流/营收比偏低时应降权利润表口径；"
    "asset_efficiency: fixed_assets/total_fa_trun 仅为资产效率 proxy，不得直接断言为产能利用率"
)


def _quality_metric_block(
    indicator: Dict[str, Any], fields: tuple[str, ...], *, digits: int = 4
) -> Dict[str, Any]:
    return {field: _round_or_none(indicator.get(field), digits) for field in fields}


def _append_quality_missing(missing: List[str], indicator: Dict[str, Any], fields: tuple[str, ...]) -> None:
    if not indicator:
        return
    for field in fields:
        if _to_float_or_none(indicator.get(field)) is None:
            missing.append(f"financial_indicator.{field}")


def _valuation_repair_start(*periods: Any, years: int = 2) -> str:
    latest_year: Optional[int] = None
    for period in periods:
        year = _period_year_valuation(period)
        if year is not None and (latest_year is None or year > latest_year):
            latest_year = year
    if latest_year is None:
        latest_year = datetime.utcnow().year
    return f"{max(latest_year - years, 2000)}0101"


def _valuation_repair_command(
    collection: str, symbol: str, *, start: str, end: Optional[str] = None
) -> Optional[str]:
    template = _VALUATION_REPAIR_COMMANDS.get(collection)
    if not template:
        return None
    return template.format(symbol=symbol, start=start, end=end or datetime.utcnow().strftime("%Y%m%d"))


def _data_quality_item(
    *,
    metric: str,
    status: str,
    problem: str,
    source_collection: str,
    required_fields: List[str],
    affected_output: List[str],
    symbol: str,
    start: str,
    repair_collection: Optional[str] = None,
    repair_command: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    must_fix = status in _DATA_QUALITY_STATUS_MUST_FIX
    command = repair_command
    if command is None and must_fix:
        command = _valuation_repair_command(
            repair_collection or source_collection, symbol, start=start, end=end
        )
    item: Dict[str, Any] = {
        "metric": metric,
        "status": status,
        "must_fix": must_fix,
        "problem": problem,
        "source_collection": source_collection,
        "required_fields": required_fields,
        "affected_output": affected_output,
    }
    if command:
        item["repair_command"] = command
    return item


def _data_quality_overall(items: List[Dict[str, Any]]) -> str:
    must_fix_count = sum(1 for item in items if item.get("must_fix"))
    if must_fix_count == 0:
        return "ok"
    severe = {"missing_collection", "missing_field"}
    if any(item.get("status") in severe for item in items):
        return "degraded"
    return "partial"


def _has_at_least_two_forward_q4(consensus_payload: Dict[str, Any], latest_period: Any) -> bool:
    forecasts = (consensus_payload or {}).get("forecasts_by_quarter") or {}
    min_year = _latest_completed_fiscal_year(latest_period)
    years = []
    for label, row in forecasts.items():
        if len(str(label)) != 6 or not str(label)[:4].isdigit() or str(label)[4:] != "Q4":
            continue
        year = int(str(label)[:4])
        if min_year is not None and year < min_year:
            continue
        if _to_float_or_none((row or {}).get("np_median_wan")) is not None:
            years.append(year)
    return len(set(years)) >= 2


def _period_year_valuation(period: Any) -> Optional[int]:
    text = str(period or "")
    if len(text) < 8:
        return None
    try:
        return int(text[:4])
    except ValueError:
        return None


def _period_suffix_valuation(period: Any) -> str:
    return str(period or "")[4:8]


def _ttm_from_cumulative_income(
    rows: List[Dict[str, Any]], field: str = "n_income_attr_p"
) -> Optional[Dict[str, Any]]:
    """Build TTM net income from cumulative income statement rows."""
    by_period = {str(row.get("end_date") or ""): row for row in rows if row.get("end_date")}
    for row in rows:
        period = str(row.get("end_date") or "")
        value = _to_float_or_none(row.get(field))
        if value is None or value <= 0:
            continue
        suffix = _period_suffix_valuation(period)
        year = _period_year_valuation(period)
        if suffix == "1231":
            return {
                "value": value,
                "field": f"financial_income.{field}",
                "method": "latest_full_year",
                "period": period,
            }
        if year is None or suffix not in {"0331", "0630", "0930"}:
            continue
        prev_year_row = by_period.get(f"{year - 1}1231")
        prev_same_row = by_period.get(f"{year - 1}{suffix}")
        prev_year_value = _to_float_or_none((prev_year_row or {}).get(field))
        prev_same_value = _to_float_or_none((prev_same_row or {}).get(field))
        if prev_year_value is None or prev_same_value is None:
            continue
        ttm = value + prev_year_value - prev_same_value
        if ttm > 0:
            return {
                "value": ttm,
                "field": f"financial_income.{field}",
                "method": "ttm_from_cumulative_income",
                "period": period,
                "formula": f"{period} + {year - 1}1231 - {year - 1}{suffix}",
            }
    return None


def _normalized_pe_from_ttm(
    total_mv_wan: Optional[float], ttm_info: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """PE using total market cap (万元) and TTM net income (元)."""
    if ttm_info is None:
        return {
            "normalized_pe_ttm": None,
            "normalized_pe_method": None,
            "normalized_pe_period": None,
            "ttm_net_income_yuan": None,
        }
    ttm_net = _to_float_or_none(ttm_info.get("value"))
    mv = _to_float_or_none(total_mv_wan)
    pe = round(mv * 10000 / ttm_net, 4) if ttm_net and mv and ttm_net > 0 else None
    return {
        "normalized_pe_ttm": pe,
        "normalized_pe_method": ttm_info.get("method"),
        "normalized_pe_period": ttm_info.get("period"),
        "ttm_net_income_yuan": _round_or_none(ttm_net, 2),
        "normalized_pe_formula": ttm_info.get("formula"),
    }


def _peg_or_none(
    pe: Optional[Any], growth_pct: Optional[Any], *, digits: int = 4
) -> Optional[float]:
    """PEG = PE / 增速(%)。PE<=0 或 增速<=0 时返回 None（PEG 无意义）。"""
    pe_v = _to_float_or_none(pe)
    growth = _to_float_or_none(growth_pct)
    if pe_v is None or pe_v <= 0 or growth is None or growth <= 0:
        return None
    return round(pe_v / growth, digits)


def _latest_completed_fiscal_year(period: Optional[Any]) -> Optional[int]:
    """Return latest completed fiscal year implied by a financial report period."""
    text = str(period or "").strip()
    if len(text) < 8 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    return year if text[4:8] == "1231" else year - 1


def _forward_growth_from_consensus(
    consensus: Dict[str, Any], *, latest_period: Optional[Any] = None
) -> Optional[Dict[str, Any]]:
    """从一致预期取最近两个预测财年的净利增速。

    forecasts_by_quarter 形如 {"2025Q4": {"np_median_wan": ...}, ...}，
    只取 Q4 全年预测，并从最新已完成财年开始取最近两个预测财年。
    """
    forecasts = (consensus or {}).get("forecasts_by_quarter") or {}
    min_year = _latest_completed_fiscal_year(latest_period)
    np_by_year: List[tuple[int, str, float]] = []
    for label in sorted(forecasts.keys()):
        if len(label) != 6 or not label[:4].isdigit() or label[4:] != "Q4":
            continue
        year = int(label[:4])
        if min_year is not None and year < min_year:
            continue
        np_med = _to_float_or_none((forecasts.get(label) or {}).get("np_median_wan"))
        if np_med is not None:
            np_by_year.append((year, label, np_med))
    if len(np_by_year) < 2:
        return None
    (_, cur_label, cur_np), (_, next_label, next_np) = sorted(np_by_year)[:2]
    if cur_np <= 0:
        return None
    growth_pct = round((next_np - cur_np) / abs(cur_np) * 100, 2)
    return {
        "cur_year": cur_label,
        "next_year": next_label,
        "cur_np_wan": round(cur_np, 2),
        "next_np_wan": round(next_np, 2),
        "growth_pct": growth_pct,
    }


def _profit_quality_alert(indicator: Dict[str, Any]) -> Optional[str]:
    if not indicator:
        return None
    alerts: List[str] = []
    invest_pct = _to_float_or_none(indicator.get("investincome_of_ebt"))
    dt_pct = _to_float_or_none(indicator.get("dtprofit_to_profit"))
    q_margin = _to_float_or_none(indicator.get("q_netprofit_margin"))
    net_margin = _to_float_or_none(indicator.get("netprofit_margin"))
    # indicator 字段为累计口径；非年报期(1231)即为年内累计，提示用 TTM/归一化 PE。
    is_annual = _period_suffix_valuation(indicator.get("end_date")) == "1231"
    period_caliber = "全年" if is_annual else "年内累计"
    pe_hint = "PE" if is_annual else "当期 PE"
    if invest_pct is not None and abs(invest_pct) > 50:
        direction = "贡献" if invest_pct > 0 else "拖累"
        alerts.append(
            f"投资收益占利润总额 {invest_pct:.1f}%（{direction}主导），"
            f"{period_caliber}利润表口径失真，{pe_hint}不可作主估值依据，建议用 derived.normalized_pe_ttm"
        )
    if dt_pct is not None and dt_pct < 60:
        alerts.append(f"扣非净利润占比仅 {dt_pct:.1f}%，非经常性损益影响较大")
    if (
        q_margin is not None
        and net_margin is not None
        and net_margin > 0
        and q_margin > net_margin * 2
        and q_margin > 30
    ):
        alerts.append(
            f"单季净利率({q_margin:.1f}%)显著高于累计净利率({net_margin:.1f}%)，"
            "建议用 derived.normalized_pe_ttm 交叉验证"
        )
    return "；".join(alerts) if alerts else None


def _fcff_seasonality_note(series: List[Dict[str, Any]]) -> Optional[str]:
    vals = [
        (str(row.get("period") or ""), _to_float_or_none(row.get("fcff_yuan")))
        for row in series
        if row.get("fcff_yuan") is not None
    ]
    if not vals:
        return None
    latest_period, latest_fcff = vals[0]
    if latest_fcff is None:
        return None
    positive = sum(1 for _, value in vals if value is not None and value > 0)
    negative = sum(1 for _, value in vals if value is not None and value < 0)
    if latest_fcff >= 0:
        if negative > 0 and positive > 0:
            return f"最新期 FCFF 为正，但样本中有 {negative} 期为负，存在季节性波动"
        return None
    suffix = _period_suffix_valuation(latest_period)
    if positive > 0 and suffix == "0331":
        return (
            f"Q1 FCFF 为负（{latest_fcff:.2f} 元）但样本中另有 {positive} 期为正，"
            "偏向季节性；不宜单季否定全年现金流"
        )
    if positive == 0:
        return (
            f"样本期内 Tushare 口径 FCFF(≈OCF-资本开支) 均为负（{len(vals)} 期）；"
            "需结合 cash_flow 桥接区分营运资本占用/扩张资本开支与真实盈利下滑，"
            "勿仅凭此判定现金流恶化"
        )
    return f"最新期 FCFF 为负但历史有 {positive} 期为正，需核实是否一次性因素"


_YI = 1e8

_CASHFLOW_VALUATION_FIELDS = {
    "_id": 0,
    "symbol": 1,
    "ts_code": 1,
    "ann_date": 1,
    "end_date": 1,
    "report_type": 1,
    "n_cashflow_act": 1,
    "c_pay_acq_const_fiolta": 1,
    "free_cashflow": 1,
    "net_profit": 1,
    "finan_exp": 1,
    "depr_fa_coga_dpba": 1,
    "amort_intang_assets": 1,
    "lt_amort_deferred_exp": 1,
    "decr_inventories": 1,
    "decr_oper_payable": 1,
    "incr_oper_payable": 1,
    "c_fr_oth_operate_a": 1,
    "oth_cash_pay_oper_act": 1,
    "oth_pay_ral_inv_act": 1,
    "oth_recp_ral_inv_act": 1,
}


def _yi(value: Any) -> Optional[float]:
    """Convert a yuan amount to 亿元 (rounded), preserving sign; None-safe."""
    number = _to_float_or_none(value)
    return round(number / _YI, 4) if number is not None else None


def _cashflow_rows_for_valuation(
    collection: Any, candidates: List[str], *, limit: int = 8
) -> List[Dict[str, Any]]:
    """Latest cumulative (report_type=1) cash-flow rows for the bridge block."""
    if collection is None or not candidates:
        return []
    for field in ("ts_code", "symbol"):
        try:
            rows = list(
                collection.find(
                    {field: {"$in": candidates}, "report_type": 1},
                    _CASHFLOW_VALUATION_FIELDS,
                    sort=[("end_date", -1), ("_id", -1)],
                    limit=limit,
                )
            )
        except Exception:
            continue
        if rows:
            return rows
    # Fallback: report_type may be absent in older docs.
    return _recent_docs_for_valuation(
        collection,
        candidates,
        date_field="end_date",
        limit=limit,
        projection=_CASHFLOW_VALUATION_FIELDS,
    )


def _cash_flow_bridge_steps(
    ebitda: Optional[float],
    wc_change: Optional[float],
    residual: Optional[float],
    ocf: Optional[float],
    capex: Optional[float],
    fcf_strict: Optional[float],
) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    if ebitda is not None:
        steps.append({"step": "EBITDA(经营造血潜力)", "value_yi": _yi(ebitda)})
    if wc_change is not None:
        steps.append({"step": "+ 营运资本变化(负=占用现金)", "value_yi": _yi(wc_change)})
    if residual is not None:
        steps.append({"step": "+ 税费/利息/其他经营调整", "value_yi": _yi(residual)})
    if ocf is not None:
        steps.append({"step": "= 经营现金流(OCF)", "value_yi": _yi(ocf)})
    if capex is not None:
        steps.append({"step": "- 资本开支(capex)", "value_yi": _yi(capex)})
    if fcf_strict is not None:
        steps.append({"step": "= 自由现金流(扣全额capex)", "value_yi": _yi(fcf_strict)})
    return steps


def _cash_flow_attribution_note(
    *,
    ebitda: Optional[float],
    ocf: Optional[float],
    capex: Optional[float],
    fcf_strict: Optional[float],
    wc_change: Optional[float],
) -> Optional[str]:
    """Non-misleading attribution: never call growth/working-capital-driven
    negative FCF a profitability deterioration."""
    if fcf_strict is None:
        return None
    if fcf_strict >= 0:
        return f"自由现金流(扣全额capex)为正({_yi(fcf_strict)}亿)，现金可自我覆盖资本开支"
    drivers: List[str] = []
    if wc_change is not None and wc_change < 0:
        drivers.append(f"营运资本占用{abs(_yi(wc_change))}亿")
    if capex is not None and capex > 0:
        drivers.append(f"资本开支{_yi(capex)}亿")
    driver_text = " + ".join(drivers) if drivers else "经营性现金流不足"
    if ebitda is None:
        return (
            f"自由现金流(扣全额capex)为负({_yi(fcf_strict)}亿)，主因{driver_text}；"
            "当期缺 EBITDA(季度口径常缺)，无法判断造血强弱，"
            "勿仅凭负 FCF 判定盈利恶化，请用年报口径或结合 OCF 交叉验证"
        )
    if ebitda > 0:
        return (
            f"自由现金流(扣全额capex)为负({_yi(fcf_strict)}亿)，主因{driver_text}；"
            f"但 EBITDA 造血仍为正({_yi(ebitda)}亿)，属投入/营运资本占用型负FCF，"
            "不得据此判定盈利恶化"
        )
    return (
        f"自由现金流(扣全额capex)为负({_yi(fcf_strict)}亿)，且 EBITDA 非正({_yi(ebitda)}亿)，"
        "存在真实盈利与现金流压力，DCF 应降权"
    )


def _cash_flow_caliber_block(
    cashflow_row: Optional[Dict[str, Any]], latest_indicator: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Multi-caliber cash flow: EBITDA -> OCF -> FCF bridge with attribution.

    口径声明：
    - per_share 取自 Tushare fina_indicator(ocfps/fcff_ps/fcfe_ps/cfps)，其
      fcff_ps 为现金流口径(≈OCF-资本开支)，与同花顺/学术 FCFF(加回折旧摊销、
      仅扣维持性capex)不同，数值可能大幅不同，不可混用。
    - absolute_yi 绝对额(亿元)来自 financial_cashflow，桥接满足恒等式
      EBITDA + ΔWC + (税/利息/其他) = OCF；OCF - capex = FCF。
    """
    cf = cashflow_row or {}
    ind = latest_indicator or {}

    ocf = _to_float_or_none(cf.get("n_cashflow_act"))
    capex = _to_float_or_none(cf.get("c_pay_acq_const_fiolta"))
    ebitda = _to_float_or_none(ind.get("ebitda"))
    net_profit = _to_float_or_none(cf.get("net_profit"))
    daa = _to_float_or_none(ind.get("daa"))

    wc_items = [
        _to_float_or_none(cf.get("decr_inventories")),
        _to_float_or_none(cf.get("decr_oper_payable")),
        _to_float_or_none(cf.get("incr_oper_payable")),
    ]
    wc_present = [v for v in wc_items if v is not None]
    wc_change = sum(wc_present) if wc_present else None

    fcf_strict = ocf - capex if ocf is not None and capex is not None else None

    recv = _to_float_or_none(cf.get("c_fr_oth_operate_a"))
    pay = _to_float_or_none(cf.get("oth_cash_pay_oper_act"))
    other_oper_net = recv - pay if recv is not None and pay is not None else None

    invest_wm_pay = _to_float_or_none(cf.get("oth_pay_ral_inv_act"))
    invest_wm_recv = _to_float_or_none(cf.get("oth_recp_ral_inv_act"))

    bridge_residual = None
    if ocf is not None and ebitda is not None and wc_change is not None:
        bridge_residual = ocf - ebitda - wc_change

    return {
        "period": cf.get("end_date") or ind.get("end_date"),
        "report_type": cf.get("report_type"),
        "available": bool(cf),
        "per_share": {
            "ocfps": _round_or_none(ind.get("ocfps"), 4),
            "fcff_ps": _round_or_none(ind.get("fcff_ps"), 4),
            "fcfe_ps": _round_or_none(ind.get("fcfe_ps"), 4),
            "cfps": _round_or_none(ind.get("cfps"), 4),
        },
        "per_share_caliber_note": (
            "ocfps=每股经营现金流；fcff_ps/fcfe_ps 为 Tushare 现金流口径每股自由现金流"
            "(≈OCF-资本开支)，与同花顺/学术 FCFF(加回折旧摊销、仅扣维持性capex)不同口径，"
            "不可混用；不得用 ocfps 代替自由现金流"
        ),
        "absolute_yi": {
            "ebitda": _yi(ebitda),
            "net_profit": _yi(net_profit),
            "depr_amort": _yi(daa),
            "ocf": _yi(ocf),
            "working_capital_change": _yi(wc_change),
            "capex": _yi(capex),
            "fcf_strict": _yi(fcf_strict),
            "free_cashflow_tushare": _yi(cf.get("free_cashflow")),
            "other_operating_net": _yi(other_oper_net),
            "invest_wealth_mgmt_pay": _yi(invest_wm_pay),
            "invest_wealth_mgmt_recv": _yi(invest_wm_recv),
        },
        "absolute_caliber_note": (
            "单位亿元。fcf_strict=OCF-全额capex(≈Tushare口径，对扩张期重资产偏保守)；"
            "EBITDA=经营造血潜力(同花顺式自由现金流口径，对应不扣capex/营运资本，偏激进)；"
            "理财/定存进出多在投资活动(invest_wealth_mgmt_*)，不影响 OCF，归因勿张冠李戴"
        ),
        "bridge": _cash_flow_bridge_steps(
            ebitda, wc_change, bridge_residual, ocf, capex, fcf_strict
        ),
        "note": _cash_flow_attribution_note(
            ebitda=ebitda, ocf=ocf, capex=capex, fcf_strict=fcf_strict, wc_change=wc_change
        ),
    }


def _mainbz_segments_for_valuation(
    collection: Any, candidates: List[str], *, limit: int = 300
) -> Dict[str, Any]:
    """读取最新一期主营构成，按 P/D/I 维度归一化为分部收入占比。

    占比以该维度内 bz_sales 净额(含抵消负项)为分母，使各项占比之和约等于 100；
    分母 <= 0 时不输出占比。无数据返回空结构。
    """
    rows = _recent_docs_for_valuation(
        collection,
        candidates,
        date_field="end_date",
        limit=limit,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "end_date": 1,
            "bz_type": 1,
            "bz_item": 1,
            "bz_sales": 1,
            "bz_profit": 1,
        },
    )
    if not rows:
        return {"period": None, "by_type": {}}

    latest_period = str(rows[0].get("end_date") or "")
    latest_rows = [r for r in rows if str(r.get("end_date") or "") == latest_period]

    by_type: Dict[str, Any] = {}
    for bz_type in ("P", "D", "I"):
        type_rows = [r for r in latest_rows if str(r.get("bz_type") or "") == bz_type]
        if not type_rows:
            continue
        total_sales = sum(
            v for v in (_to_float_or_none(r.get("bz_sales")) for r in type_rows) if v is not None
        )
        items: List[Dict[str, Any]] = []
        for r in type_rows:
            sales = _to_float_or_none(r.get("bz_sales"))
            items.append(
                {
                    "bz_item": r.get("bz_item"),
                    "bz_sales_yuan": _round_or_none(sales, 2),
                    "bz_profit_yuan": _round_or_none(r.get("bz_profit"), 2),
                    "share_pct": round(sales / total_sales * 100, 2)
                    if sales is not None and total_sales and total_sales > 0
                    else None,
                }
            )
        items.sort(
            key=lambda it: it["bz_sales_yuan"] if it["bz_sales_yuan"] is not None else float("-inf"),
            reverse=True,
        )
        by_type[bz_type] = {
            "label": _MAINBZ_TYPE_LABELS[bz_type],
            "total_sales_yuan": _round_or_none(total_sales, 2),
            "item_count": len(items),
            "items": items,
        }

    return {"period": latest_period or None, "by_type": by_type}


def _mainbz_segments_series(
    collection: Any, candidates: List[str], *, limit: int = 600
) -> Dict[str, Any]:
    """Multi-period mainbz with YoY sales growth per bz_item."""
    rows = _recent_docs_for_valuation(
        collection,
        candidates,
        date_field="end_date",
        limit=limit,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "end_date": 1,
            "bz_type": 1,
            "bz_item": 1,
            "bz_sales": 1,
            "bz_profit": 1,
        },
    )
    if not rows:
        return {"latest_period": None, "prior_period": None, "by_type": {}}

    latest_period = str(rows[0].get("end_date") or "")
    year = _period_year_valuation(latest_period)
    suffix = _period_suffix_valuation(latest_period)
    prior_period = f"{year - 1}{suffix}" if year else ""
    latest_rows = [r for r in rows if str(r.get("end_date") or "") == latest_period]
    prior_rows = [r for r in rows if prior_period and str(r.get("end_date") or "") == prior_period]

    prior_sales: Dict[tuple[str, str], float] = {}
    for row in prior_rows:
        key = (str(row.get("bz_type") or ""), str(row.get("bz_item") or ""))
        sales = _to_float_or_none(row.get("bz_sales"))
        if sales is not None:
            prior_sales[key] = sales

    by_type: Dict[str, Any] = {}
    for bz_type in ("P", "D", "I"):
        type_rows = [r for r in latest_rows if str(r.get("bz_type") or "") == bz_type]
        if not type_rows:
            continue
        items: List[Dict[str, Any]] = []
        for row in type_rows:
            bz_item = str(row.get("bz_item") or "")
            sales = _to_float_or_none(row.get("bz_sales"))
            prior = prior_sales.get((bz_type, bz_item))
            yoy_pct = (
                round((sales - prior) / abs(prior) * 100, 2)
                if sales is not None and prior is not None and prior != 0
                else None
            )
            items.append(
                {
                    "bz_item": bz_item,
                    "bz_sales_yuan": _round_or_none(sales, 2),
                    "bz_profit_yuan": _round_or_none(row.get("bz_profit"), 2),
                    "yoy_pct": yoy_pct,
                    "prior_period": prior_period or None if prior is not None else None,
                }
            )
        items.sort(
            key=lambda it: it["bz_sales_yuan"] if it["bz_sales_yuan"] is not None else float("-inf"),
            reverse=True,
        )
        by_type[bz_type] = {
            "label": _MAINBZ_TYPE_LABELS[bz_type],
            "item_count": len(items),
            "items": items,
        }

    return {
        "latest_period": latest_period or None,
        "prior_period": prior_period or None,
        "by_type": by_type,
        "note": (
            "yoy_pct 为最新期 bz_sales 相对去年同期同季度的同比增速；"
            "无对应产品线或去年同期无数据时为 null"
        ),
    }


_CONSENSUS_MISSING_NOTES = {
    "earnings_report_rc": "缺少卖方研报一致预期，无法给出机构预测中位数",
}

_CONSENSUS_RATING_BUCKETS = ("买入", "增持", "中性", "减持", "卖出")


def _median_or_none_values(values: List[Any], *, digits: int = 4) -> Optional[float]:
    nums = sorted(v for v in (_to_float_or_none(x) for x in values) if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return round(nums[mid], digits)
    return round((nums[mid - 1] + nums[mid]) / 2, digits)


def _stock_earnings_consensus_payload(
    symbol: str,
    *,
    data_collections: Any = None,
    lookback_days: int = 180,
) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol 不能为空"}

    data = data_collections if data_collections is not None else data_db
    candidates = _valuation_symbol_candidates(normalized_symbol)
    collection = _collection_from(data, "earnings_report_rc")
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y%m%d")

    rows: List[Dict[str, Any]] = []
    if collection is not None:
        for field in ("ts_code", "symbol"):
            try:
                rows = list(
                    collection.find(
                        {field: {"$in": candidates}, "report_date": {"$gte": cutoff}},
                        {
                            "_id": 0,
                            "ts_code": 1,
                            "symbol": 1,
                            "name": 1,
                            "report_date": 1,
                            "org_name": 1,
                            "quarter": 1,
                            "np": 1,
                            "op_rt": 1,
                            "eps": 1,
                            "pe": 1,
                            "roe": 1,
                            "rating": 1,
                            "report_title": 1,
                        },
                        sort=[("report_date", -1)],
                    )
                )
            except Exception:
                continue
            if rows:
                break

    missing: List[str] = []
    if not rows:
        missing.append("earnings_report_rc")
        return {
            "success": True,
            "symbol": normalized_symbol,
            "resolved_symbol": normalized_symbol,
            "lookback_days": lookback_days,
            "report_count": 0,
            "deduped_report_count": 0,
            "forecasts_by_quarter": {},
            "rating_distribution": {},
            "org_names": [],
            "org_count": 0,
            "latest_report_date": None,
            "missing": missing,
            "missing_notes": {
                key: _CONSENSUS_MISSING_NOTES[key] for key in missing if key in _CONSENSUS_MISSING_NOTES
            },
            "note": (
                "np/revenue 单位为万元(与 Tushare report_rc 一致)；"
                "quarter 如 2025Q4 表示预测财年；按 org+quarter 去重取最新研报"
            ),
            "source": "quant_data.earnings_report_rc; Hermes MCP read-only earnings consensus",
        }

    resolved_symbol = str(rows[0].get("ts_code") or rows[0].get("symbol") or normalized_symbol)
    latest_report_date = max(str(row.get("report_date") or "") for row in rows)

    # Latest report per (org, quarter) to avoid duplicate stale entries.
    latest_by_org_quarter: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        org = str(row.get("org_name") or "").strip()
        quarter = str(row.get("quarter") or "").strip()
        if not org or not quarter:
            continue
        key = (org, quarter)
        existing = latest_by_org_quarter.get(key)
        if existing is None or str(row.get("report_date") or "") >= str(existing.get("report_date") or ""):
            latest_by_org_quarter[key] = row

    deduped = list(latest_by_org_quarter.values())
    org_names = sorted({str(row.get("org_name") or "") for row in deduped if row.get("org_name")})

    rating_distribution: Dict[str, int] = {bucket: 0 for bucket in _CONSENSUS_RATING_BUCKETS}
    other_ratings = 0
    for row in deduped:
        rating = str(row.get("rating") or "").strip()
        if rating in rating_distribution:
            rating_distribution[rating] += 1
        elif rating:
            other_ratings += 1
    if other_ratings:
        rating_distribution["其他"] = other_ratings

    by_quarter: Dict[str, List[Dict[str, Any]]] = {}
    for row in deduped:
        quarter = str(row.get("quarter") or "").strip()
        if quarter:
            by_quarter.setdefault(quarter, []).append(row)

    def _valid_count(rows_: List[Dict[str, Any]], field: str) -> int:
        return sum(1 for r in rows_ if _to_float_or_none(r.get(field)) is not None)

    forecasts_by_quarter: Dict[str, Any] = {}
    for quarter in sorted(by_quarter.keys()):
        q_rows = by_quarter[quarter]
        forecasts_by_quarter[quarter] = {
            "fiscal_label": quarter,
            "sample_count": len(q_rows),
            "np_median_wan": _median_or_none_values([r.get("np") for r in q_rows], digits=2),
            "np_sample_count": _valid_count(q_rows, "np"),
            "revenue_median_wan": _median_or_none_values([r.get("op_rt") for r in q_rows], digits=2),
            "revenue_sample_count": _valid_count(q_rows, "op_rt"),
            "eps_median": _median_or_none_values([r.get("eps") for r in q_rows], digits=4),
            "eps_sample_count": _valid_count(q_rows, "eps"),
            "pe_median": _median_or_none_values([r.get("pe") for r in q_rows], digits=4),
            "roe_median_pct": _median_or_none_values([r.get("roe") for r in q_rows], digits=2),
            "orgs": sorted({str(r.get("org_name") or "") for r in q_rows if r.get("org_name")}),
        }

    return {
        "success": True,
        "symbol": normalized_symbol,
        "resolved_symbol": resolved_symbol,
        "lookback_days": lookback_days,
        "report_count": len(rows),
        "deduped_report_count": len(deduped),
        "forecasts_by_quarter": forecasts_by_quarter,
        "rating_distribution": rating_distribution,
        "org_names": org_names,
        "org_count": len(org_names),
        "latest_report_date": latest_report_date or None,
        "missing": missing,
        "missing_notes": {key: _CONSENSUS_MISSING_NOTES[key] for key in missing if key in _CONSENSUS_MISSING_NOTES},
        "note": (
            "np/revenue 单位为万元(与 Tushare report_rc 一致)；"
            "quarter 如 2025Q4 表示预测财年；按 org+quarter 去重取最新研报；"
            "sample_count 为该财年研报条数，各指标实际有效值数见对应 *_sample_count"
        ),
        "source": "quant_data.earnings_report_rc; Hermes MCP read-only earnings consensus",
    }


def _clean_numeric(values: List[Any], *, positive_only: bool = True) -> List[float]:
    """转 float 并去掉 None/NaN；positive_only 时仅保留正值。

    PE/PB/PS、市值这类只有正值有意义的指标用 positive_only=True；
    ROE、净利率、收入增速等可正可负的财务指标须传 positive_only=False，
    否则负值会被静默丢弃，使同业中位数/分位口径失真。
    """
    nums: List[float] = []
    for raw in values:
        number = _to_float_or_none(raw)
        if number is None:
            continue
        if positive_only and number <= 0:
            continue
        nums.append(number)
    return nums


def _percentile_rank_or_none(
    values: List[Any],
    current: Optional[float],
    *,
    min_samples: int = 30,
    positive_only: bool = True,
) -> Optional[float]:
    """当前值在样本中的分位（0-100，含自身），样本不足返回 None。"""
    if current is None:
        return None
    nums = _clean_numeric(values, positive_only=positive_only)
    if len(nums) < min_samples:
        return None
    below = sum(1 for v in nums if v <= current)
    return round(below / len(nums) * 100, 2)


_PEER_LEVEL_FIELDS = ("sw_l2", "sw_l1", "industry")


def _stock_identifier(row: Dict[str, Any]) -> Optional[str]:
    value = row.get("ts_code") or row.get("symbol")
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _latest_peer_rows_by_symbol(
    collection: Any,
    peer_symbols: List[str],
    *,
    date_field: str,
    as_of: Optional[str],
    projection: Dict[str, int],
    fields: tuple[str, ...] = ("ts_code", "symbol"),
) -> Dict[str, Dict[str, Any]]:
    if collection is None or not peer_symbols:
        return {}

    for field in fields:
        query: Dict[str, Any] = {field: {"$in": peer_symbols}}
        if as_of:
            query[date_field] = as_of
        try:
            rows = list(
                collection.find(
                    query,
                    projection,
                    sort=[(date_field, -1), ("_id", -1)],
                )
            )
        except Exception:
            continue
        if not rows:
            continue

        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = _stock_identifier(row)
            if symbol and symbol not in out:
                out[symbol] = row
        if out:
            return out
    return {}


def _median_or_none(
    values: List[Any], *, min_samples: int = 1, positive_only: bool = True
) -> Optional[float]:
    nums = sorted(_clean_numeric(values, positive_only=positive_only))
    if len(nums) < min_samples:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return round(nums[mid], 4)
    return round((nums[mid - 1] + nums[mid]) / 2, 4)


def _peer_metric_summary(
    rows_by_symbol: Dict[str, Dict[str, Any]],
    target_symbol: str,
    field: str,
    *,
    min_peer_count: int,
    digits: int = 4,
    positive_only: bool = True,
) -> Dict[str, Any]:
    values = [row.get(field) for row in rows_by_symbol.values()]
    target_value = _to_float_or_none((rows_by_symbol.get(target_symbol) or {}).get(field))
    valid_values = _clean_numeric(values, positive_only=positive_only)
    return {
        "value": _round_or_none(target_value, digits),
        "peer_median": _median_or_none(
            valid_values, min_samples=min_peer_count, positive_only=positive_only
        ),
        "peer_percentile": _percentile_rank_or_none(
            valid_values, target_value, min_samples=min_peer_count, positive_only=positive_only
        ),
        "sample_count": len(valid_values),
    }


def _stock_industry_comparison_payload(
    symbol: str,
    *,
    level: str = "sw_l2",
    data_collections: Any = None,
    min_peer_count: int = 5,
    peer_symbols: Optional[List[str]] = None,
    named_peer_limit: int = 5,
) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol 不能为空"}

    data = data_collections if data_collections is not None else data_db
    stock_info = _collection_from(data, "stock_info")
    candidates = _valuation_symbol_candidates(normalized_symbol)
    info = _latest_stock_info_for_valuation(stock_info, candidates)
    if not info:
        return {
            "success": False,
            "symbol": normalized_symbol,
            "candidates": candidates,
            "error": "未找到 stock_info，无法确定同业集合",
        }

    requested_level = level if level in _PEER_LEVEL_FIELDS else "sw_l2"
    fallback_order = [requested_level] + [
        field for field in _PEER_LEVEL_FIELDS if field != requested_level
    ]
    peer_field = next((field for field in fallback_order if info.get(field)), None)
    if not peer_field:
        return {
            "success": False,
            "symbol": normalized_symbol,
            "resolved_symbol": _stock_identifier(info) or normalized_symbol,
            "name": info.get("name", ""),
            "error": "stock_info 缺少 sw_l2/sw_l1/industry，无法确定同业集合",
        }

    peer_value = info.get(peer_field)
    try:
        peer_info_rows = list(
            stock_info.find(
                {peer_field: peer_value},
                {
                    "_id": 0,
                    "symbol": 1,
                    "ts_code": 1,
                    "name": 1,
                    "industry": 1,
                    "sw_l1": 1,
                    "sw_l2": 1,
                    "sw_l3": 1,
                },
            )
        )
    except Exception:
        peer_info_rows = []

    peer_stock_symbols = [
        symbol_id
        for symbol_id in (_stock_identifier(row) for row in peer_info_rows)
        if symbol_id
    ]
    peer_stock_symbols = list(dict.fromkeys(peer_stock_symbols))
    stock_info_target_symbol = _stock_identifier(info) or normalized_symbol
    if stock_info_target_symbol not in peer_stock_symbols:
        peer_stock_symbols.append(stock_info_target_symbol)

    peer_query_symbols: List[str] = []
    for peer_symbol in peer_stock_symbols:
        peer_query_symbols.extend(_valuation_symbol_candidates(peer_symbol))
    peer_query_symbols = list(dict.fromkeys(peer_query_symbols))

    daily_target_rows = _recent_docs_for_valuation(
        _collection_from(data, "financial_daily_basic"),
        [stock_info_target_symbol] + candidates,
        date_field="trade_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "pe_ttm": 1,
            "pb": 1,
            "ps_ttm": 1,
            "total_mv": 1,
        },
    )
    indicator_target_rows = _recent_docs_for_valuation(
        _collection_from(data, "financial_indicator"),
        [stock_info_target_symbol] + candidates,
        date_field="end_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "end_date": 1,
            "roe": 1,
            "netprofit_margin": 1,
            "grossprofit_margin": 1,
            "tr_yoy": 1,
            "or_yoy": 1,
            "netprofit_yoy": 1,
        },
    )
    daily_as_of = str(daily_target_rows[0].get("trade_date")) if daily_target_rows else None
    indicator_as_of = (
        str(indicator_target_rows[0].get("end_date")) if indicator_target_rows else None
    )
    target_symbol = (
        (_stock_identifier(daily_target_rows[0]) if daily_target_rows else None)
        or (_stock_identifier(indicator_target_rows[0]) if indicator_target_rows else None)
        or stock_info_target_symbol
    )

    # as_of 为空意味着目标股没有最新行情/财报，此时同业横截面注定为空；
    # 直接跳过查询，避免对大行业做一次不带日期的全历史扫描。
    daily_rows_by_symbol = (
        _latest_peer_rows_by_symbol(
            _collection_from(data, "financial_daily_basic"),
            peer_query_symbols,
            date_field="trade_date",
            as_of=daily_as_of,
            projection={
                "_id": 0,
                "symbol": 1,
                "ts_code": 1,
                "trade_date": 1,
                "pe_ttm": 1,
                "pb": 1,
                "ps_ttm": 1,
                "total_mv": 1,
            },
        )
        if daily_as_of
        else {}
    )
    indicator_rows_by_symbol = (
        _latest_peer_rows_by_symbol(
            _collection_from(data, "financial_indicator"),
            peer_query_symbols,
            date_field="end_date",
            as_of=indicator_as_of,
            projection={
                "_id": 0,
                "symbol": 1,
                "ts_code": 1,
                "end_date": 1,
                "roe": 1,
                "netprofit_margin": 1,
                "grossprofit_margin": 1,
                "tr_yoy": 1,
                "or_yoy": 1,
                "netprofit_yoy": 1,
            },
        )
        if indicator_as_of
        else {}
    )

    if target_symbol not in daily_rows_by_symbol and daily_target_rows:
        daily_rows_by_symbol[target_symbol] = daily_target_rows[0]
    if target_symbol not in indicator_rows_by_symbol and indicator_target_rows:
        indicator_rows_by_symbol[target_symbol] = indicator_target_rows[0]

    revenue_growth_rows: Dict[str, Dict[str, Any]] = {}
    for sym, row in indicator_rows_by_symbol.items():
        revenue_growth_rows[sym] = {
            **row,
            "revenue_growth": row.get("tr_yoy")
            if row.get("tr_yoy") is not None
            else row.get("or_yoy"),
        }

    # PEG(历史) = pe_ttm / netprofit_yoy，逐个体计算后再统计中位数/分位。
    peg_rows: Dict[str, Dict[str, Any]] = {}
    for sym in set(daily_rows_by_symbol) | set(indicator_rows_by_symbol):
        pe = (daily_rows_by_symbol.get(sym) or {}).get("pe_ttm")
        growth = (indicator_rows_by_symbol.get(sym) or {}).get("netprofit_yoy")
        peg_rows[sym] = {"peg": _peg_or_none(pe, growth)}

    metrics = {
        "market": {
            "pe_ttm": _peer_metric_summary(
                daily_rows_by_symbol, target_symbol, "pe_ttm", min_peer_count=min_peer_count
            ),
            "pb": _peer_metric_summary(
                daily_rows_by_symbol, target_symbol, "pb", min_peer_count=min_peer_count
            ),
            "ps_ttm": _peer_metric_summary(
                daily_rows_by_symbol, target_symbol, "ps_ttm", min_peer_count=min_peer_count
            ),
            "peg": _peer_metric_summary(
                peg_rows, target_symbol, "peg", min_peer_count=min_peer_count
            ),
            "total_mv_wan": _peer_metric_summary(
                daily_rows_by_symbol, target_symbol, "total_mv", min_peer_count=min_peer_count, digits=2
            ),
        },
        "financial": {
            "roe_pct": _peer_metric_summary(
                indicator_rows_by_symbol,
                target_symbol,
                "roe",
                min_peer_count=min_peer_count,
                digits=2,
                positive_only=False,
            ),
            "net_margin_pct": _peer_metric_summary(
                indicator_rows_by_symbol,
                target_symbol,
                "netprofit_margin",
                min_peer_count=min_peer_count,
                digits=2,
                positive_only=False,
            ),
            "gross_margin_pct": _peer_metric_summary(
                indicator_rows_by_symbol,
                target_symbol,
                "grossprofit_margin",
                min_peer_count=min_peer_count,
                digits=2,
                positive_only=False,
            ),
            "revenue_growth_pct": _peer_metric_summary(
                revenue_growth_rows,
                target_symbol,
                "revenue_growth",
                min_peer_count=min_peer_count,
                digits=2,
                positive_only=False,
            ),
        },
    }

    # 点名同业并排对比：默认取同行业市值最大的 Top N，传 peer_symbols 则用指定名单。
    name_by_symbol: Dict[str, str] = {}
    for row in peer_info_rows:
        sid = _stock_identifier(row)
        if sid:
            name_by_symbol[sid] = str(row.get("name") or "")

    def _named_peer_entry(sym: str) -> Dict[str, Any]:
        daily = daily_rows_by_symbol.get(sym) or {}
        ind = indicator_rows_by_symbol.get(sym) or {}
        pe = daily.get("pe_ttm")
        growth = ind.get("netprofit_yoy")
        rev_growth = (
            ind.get("tr_yoy") if ind.get("tr_yoy") is not None else ind.get("or_yoy")
        )
        return {
            "symbol": sym,
            "name": name_by_symbol.get(sym, ""),
            "trade_date": daily.get("trade_date"),
            "end_date": ind.get("end_date"),
            "pe_ttm": _round_or_none(pe, 4),
            "ps_ttm": _round_or_none(daily.get("ps_ttm"), 4),
            "pb": _round_or_none(daily.get("pb"), 4),
            "peg": _peg_or_none(pe, growth),
            "net_margin_pct": _round_or_none(ind.get("netprofit_margin"), 2),
            "netprofit_yoy_pct": _round_or_none(growth, 2),
            "revenue_growth_pct": _round_or_none(rev_growth, 2),
            "total_mv_wan": _round_or_none(daily.get("total_mv"), 2),
        }

    def _resolve_named_peer(raw_symbol: str) -> Optional[str]:
        cands = _valuation_symbol_candidates(raw_symbol)
        if not cands:
            return None
        info_row = _latest_stock_info_for_valuation(stock_info, cands)
        sid = _stock_identifier(info_row) if info_row else None
        lookup = ([sid] if sid else []) + cands
        d_rows = _recent_docs_for_valuation(
            _collection_from(data, "financial_daily_basic"),
            lookup,
            date_field="trade_date",
            limit=1,
            projection={
                "_id": 0,
                "symbol": 1,
                "ts_code": 1,
                "trade_date": 1,
                "pe_ttm": 1,
                "pb": 1,
                "ps_ttm": 1,
                "total_mv": 1,
            },
        )
        i_rows = _recent_docs_for_valuation(
            _collection_from(data, "financial_indicator"),
            lookup,
            date_field="end_date",
            limit=1,
            projection={
                "_id": 0,
                "symbol": 1,
                "ts_code": 1,
                "end_date": 1,
                "netprofit_margin": 1,
                "tr_yoy": 1,
                "or_yoy": 1,
                "netprofit_yoy": 1,
            },
        )
        resolved = (
            sid
            or (_stock_identifier(d_rows[0]) if d_rows else None)
            or (_stock_identifier(i_rows[0]) if i_rows else None)
            or str(raw_symbol).strip().upper()
        )
        if d_rows:
            daily_rows_by_symbol.setdefault(resolved, d_rows[0])
        if i_rows:
            indicator_rows_by_symbol.setdefault(resolved, i_rows[0])
        if info_row and resolved not in name_by_symbol:
            name_by_symbol[resolved] = str(info_row.get("name") or "")
        return resolved

    named_peers_source = "top_market_cap"
    named_symbols: List[str] = []
    if peer_symbols:
        named_peers_source = "peer_symbols"
        for raw in peer_symbols:
            resolved = _resolve_named_peer(raw)
            if resolved and resolved != target_symbol and resolved not in named_symbols:
                named_symbols.append(resolved)
    else:
        ranked = sorted(
            (
                s
                for s in peer_stock_symbols
                if s != target_symbol
                and _to_float_or_none((daily_rows_by_symbol.get(s) or {}).get("total_mv"))
                is not None
            ),
            key=lambda s: _to_float_or_none(
                (daily_rows_by_symbol.get(s) or {}).get("total_mv")
            )
            or 0.0,
            reverse=True,
        )
        named_symbols = ranked[:named_peer_limit]

    named_peers = [_named_peer_entry(sym) for sym in named_symbols]
    target_snapshot = _named_peer_entry(target_symbol)

    notes: List[str] = []
    if len(peer_stock_symbols) < min_peer_count:
        notes.append(f"同业股票数少于 {min_peer_count}，分位与中位数返回 null")
    if daily_as_of is None:
        notes.append("缺少目标股最新 financial_daily_basic，市场估值横截面不可得")
    if indicator_as_of is None:
        notes.append("缺少目标股最新 financial_indicator，财务指标横截面不可得")

    return {
        "success": True,
        "symbol": normalized_symbol,
        "resolved_symbol": target_symbol,
        "name": info.get("name", ""),
        "requested_level": requested_level,
        "peer_level": peer_field,
        "peer_value": peer_value,
        "peer_count": len(peer_stock_symbols),
        "as_of": {
            "market_trade_date": daily_as_of,
            "financial_period": indicator_as_of,
        },
        "metrics": metrics,
        "target_snapshot": target_snapshot,
        "named_peers": named_peers,
        "named_peers_source": named_peers_source,
        "named_peers_note": (
            "named_peers 为点名同业并排：默认取同行业市值最大的 Top N，"
            "传 peer_symbols 则用指定名单(可跨行业)；peg 为历史 PE_ttm/净利同比，"
            "前瞻 PEG 仅在 stock_valuation_metrics(目标股) 提供；"
            "每行 trade_date/end_date 为该同业自身最新快照口径"
        ),
        "notes": notes,
        "percentile_note": (
            "peer_percentile 为目标值在同业横截面中的分位(0-100，含自身)；"
            "估值倍数/市值/PEG 仅统计正值，ROE/利润率/收入增速含负值；"
            "PEG=PE_ttm/净利同比，净利增速<=0 的个体不计入"
        ),
        "source": (
            "quant_data.stock_info + financial_daily_basic + financial_indicator; "
            "same-industry cross-section by selected industry level"
        ),
    }


def _stock_valuation_metrics_payload(
    symbol: str,
    *,
    data_collections: Any = None,
    indicator_limit: int = 8,
    income_limit: int = 8,
    daily_history_limit: int = 1250,
) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol 不能为空"}

    data = data_collections if data_collections is not None else data_db
    candidates = _valuation_symbol_candidates(normalized_symbol)
    daily_rows = _recent_docs_for_valuation(
        _collection_from(data, "financial_daily_basic"),
        candidates,
        date_field="trade_date",
        limit=daily_history_limit,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "close": 1,
            "pe": 1,
            "pe_ttm": 1,
            "pb": 1,
            "ps": 1,
            "ps_ttm": 1,
            "total_mv": 1,
            "circ_mv": 1,
            "dv_ttm": 1,
        },
    )
    indicator_rows = _recent_docs_for_valuation(
        _collection_from(data, "financial_indicator"),
        candidates,
        date_field="end_date",
        limit=indicator_limit,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "ann_date": 1,
            "end_date": 1,
            "rd_exp": 1,
            "grossprofit_margin": 1,
            "netprofit_margin": 1,
            "fcff": 1,
            "fcfe": 1,
            "ocfps": 1,
            "fcff_ps": 1,
            "fcfe_ps": 1,
            "cfps": 1,
            "ebitda": 1,
            "daa": 1,
            "roe": 1,
            "roic": 1,
            "netprofit_yoy": 1,
            "tr_yoy": 1,
            "or_yoy": 1,
            "debt_to_assets": 1,
            "q_netprofit_margin": 1,
            "dtprofit_to_profit": 1,
            "opincome_of_ebt": 1,
            "investincome_of_ebt": 1,
            "salescash_to_or": 1,
            "ocf_to_or": 1,
            "ocf_yoy": 1,
            "fixed_assets": 1,
            "total_fa_trun": 1,
            "capitalized_to_da": 1,
            "roic_yearly": 1,
        },
    )
    income_rows = _recent_docs_for_valuation(
        _collection_from(data, "financial_income"),
        candidates,
        date_field="end_date",
        limit=income_limit,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "ann_date": 1,
            "end_date": 1,
            "revenue": 1,
            "total_revenue": 1,
            "n_income_attr_p": 1,
            "invest_income": 1,
            "report_type": 1,
        },
    )
    info = _latest_stock_info_for_valuation(_collection_from(data, "stock_info"), candidates)

    latest_daily = daily_rows[0] if daily_rows else {}
    latest_indicator = indicator_rows[0] if indicator_rows else {}
    latest_income = income_rows[0] if income_rows else {}
    if not (latest_daily or latest_indicator or latest_income or info):
        return {
            "success": False,
            "symbol": normalized_symbol,
            "candidates": candidates,
            "error": "未找到单股估值指标所需的 financial_daily_basic/financial_indicator/financial_income/stock_info 数据",
        }

    revenue = _first_positive(latest_income.get("revenue"), latest_income.get("total_revenue"))
    rd_exp = _to_float_or_none(latest_indicator.get("rd_exp"))
    fcff = _to_float_or_none(latest_indicator.get("fcff"))
    rd_intensity = rd_exp / revenue if rd_exp is not None and revenue else None
    fcff_margin = fcff / revenue if fcff is not None and revenue else None
    revenue_growth_pct = _round_or_none(
        latest_indicator.get("tr_yoy")
        if latest_indicator.get("tr_yoy") is not None
        else latest_indicator.get("or_yoy"),
        2,
    )
    net_margin_pct = _round_or_none(latest_indicator.get("netprofit_margin"), 2)
    fcff_margin_pct = round(fcff_margin * 100, 2) if fcff_margin is not None else None
    rule_of_40_net = (
        round(revenue_growth_pct + net_margin_pct, 2)
        if revenue_growth_pct is not None and net_margin_pct is not None
        else None
    )
    rule_of_40_fcff = (
        round(revenue_growth_pct + fcff_margin_pct, 2)
        if revenue_growth_pct is not None and fcff_margin_pct is not None
        else None
    )

    missing: List[str] = []
    if not latest_daily:
        missing.append("financial_daily_basic")
    for field in ("ps_ttm", "pe_ttm", "pb"):
        if latest_daily and _to_float_or_none(latest_daily.get(field)) is None:
            missing.append(f"financial_daily_basic.{field}")
    if not latest_indicator:
        missing.append("financial_indicator")
    for field in ("rd_exp", "fcff", "grossprofit_margin", "netprofit_margin", "tr_yoy/or_yoy"):
        if latest_indicator:
            if field == "tr_yoy/or_yoy":
                if latest_indicator.get("tr_yoy") is None and latest_indicator.get("or_yoy") is None:
                    missing.append("financial_indicator.tr_yoy/or_yoy")
            elif _to_float_or_none(latest_indicator.get(field)) is None:
                missing.append(f"financial_indicator.{field}")
    if not latest_income:
        missing.append("financial_income")
    elif revenue is None:
        missing.append("financial_income.revenue/total_revenue")

    _append_quality_missing(missing, latest_indicator, _QUALITY_PROFIT_FIELDS)
    _append_quality_missing(missing, latest_indicator, _QUALITY_CASH_FLOW_FIELDS)
    _append_quality_missing(missing, latest_indicator, _QUALITY_ASSET_EFFICIENCY_FIELDS)

    segments = _mainbz_segments_for_valuation(_collection_from(data, "financial_mainbz"), candidates)
    segments_series = _mainbz_segments_series(_collection_from(data, "financial_mainbz"), candidates)
    if not segments.get("by_type"):
        missing.append("financial_mainbz")

    invest_income = _to_float_or_none(latest_income.get("invest_income"))
    invest_income_to_revenue_pct = (
        round(invest_income / revenue * 100, 2) if invest_income is not None and revenue else None
    )

    missing_notes = {field: _VALUATION_MISSING_NOTES[field] for field in missing if field in _VALUATION_MISSING_NOTES}

    income_by_period: Dict[str, Dict[str, Any]] = {}
    for row in income_rows:
        period = str(row.get("end_date") or "")
        if period and period not in income_by_period:
            income_by_period[period] = row
    series: List[Dict[str, Any]] = []
    for row in indicator_rows:
        period = str(row.get("end_date") or "")
        income_row = income_by_period.get(period, {})
        period_revenue = _first_positive(income_row.get("revenue"), income_row.get("total_revenue"))
        period_rd = _to_float_or_none(row.get("rd_exp"))
        period_fcff = _to_float_or_none(row.get("fcff"))
        series.append(
            {
                "period": period or None,
                "revenue_growth_pct": _round_or_none(
                    row.get("tr_yoy") if row.get("tr_yoy") is not None else row.get("or_yoy"), 2
                ),
                "net_margin_pct": _round_or_none(row.get("netprofit_margin"), 2),
                "gross_margin_pct": _round_or_none(row.get("grossprofit_margin"), 2),
                "fcff_yuan": _round_or_none(period_fcff, 2),
                "fcff_margin_pct": round(period_fcff / period_revenue * 100, 2)
                if period_fcff is not None and period_revenue
                else None,
                "rd_intensity_pct": round(period_rd / period_revenue * 100, 2)
                if period_rd is not None and period_revenue
                else None,
                "roe_pct": _round_or_none(row.get("roe"), 2),
            }
        )
    fcff_series_vals = [r["fcff_yuan"] for r in series if r["fcff_yuan"] is not None]
    fcff_positive_count = sum(1 for v in fcff_series_vals if v > 0)

    ttm_income_info = _ttm_from_cumulative_income(income_rows, "n_income_attr_p")
    normalized_pe_block = _normalized_pe_from_ttm(
        _to_float_or_none(latest_daily.get("total_mv")), ttm_income_info
    )
    profit_quality_alert = _profit_quality_alert(latest_indicator)
    fcff_seasonality_note = _fcff_seasonality_note(series)

    cashflow_rows = _cashflow_rows_for_valuation(
        _collection_from(data, "financial_cashflow"), candidates
    )
    latest_cashflow = cashflow_rows[0] if cashflow_rows else {}
    cash_flow_block = _cash_flow_caliber_block(latest_cashflow, latest_indicator)

    pe_ttm_val = _to_float_or_none(latest_daily.get("pe_ttm"))
    netprofit_yoy_val = _to_float_or_none(latest_indicator.get("netprofit_yoy"))
    peg_hist = _peg_or_none(pe_ttm_val, netprofit_yoy_val)

    consensus_payload = _stock_earnings_consensus_payload(normalized_symbol, data_collections=data)
    forward_growth = _forward_growth_from_consensus(
        consensus_payload, latest_period=latest_indicator.get("end_date")
    )
    total_mv_wan_val = _to_float_or_none(latest_daily.get("total_mv"))
    forward_pe = None
    forward_peg = None
    if forward_growth is not None and total_mv_wan_val and total_mv_wan_val > 0:
        cur_np_wan = forward_growth.get("cur_np_wan")
        if cur_np_wan and cur_np_wan > 0:
            forward_pe = round(total_mv_wan_val / cur_np_wan, 4)
            forward_peg = _peg_or_none(forward_pe, forward_growth.get("growth_pct"))

    valuation_percentiles = {
        "pe_ttm": _percentile_rank_or_none(
            [r.get("pe_ttm") for r in daily_rows], _to_float_or_none(latest_daily.get("pe_ttm"))
        ),
        "pb": _percentile_rank_or_none(
            [r.get("pb") for r in daily_rows], _to_float_or_none(latest_daily.get("pb"))
        ),
        "ps_ttm": _percentile_rank_or_none(
            [r.get("ps_ttm") for r in daily_rows], _to_float_or_none(latest_daily.get("ps_ttm"))
        ),
    }

    resolved_symbol = (
        latest_daily.get("ts_code")
        or latest_daily.get("symbol")
        or latest_indicator.get("ts_code")
        or latest_indicator.get("symbol")
        or latest_income.get("ts_code")
        or latest_income.get("symbol")
        or info.get("ts_code")
        or info.get("symbol")
        or normalized_symbol
    )
    repair_start = _valuation_repair_start(
        latest_indicator.get("end_date"), latest_income.get("end_date")
    )
    today = datetime.utcnow().strftime("%Y%m%d")
    data_quality_items: List[Dict[str, Any]] = []

    def add_quality_item(**kwargs: Any) -> None:
        data_quality_items.append(
            _data_quality_item(
                symbol=str(resolved_symbol),
                start=repair_start,
                end=today,
                **kwargs,
            )
        )

    if not latest_daily:
        add_quality_item(
            metric="market",
            status="missing_collection",
            problem="缺少 financial_daily_basic 最新快照，PE/PB/PS/市值与历史分位不可得",
            source_collection="financial_daily_basic",
            required_fields=["pe_ttm", "pb", "ps_ttm", "total_mv", "trade_date"],
            affected_output=["market", "valuation_percentiles", "PEG/PE 交叉验证"],
        )
    else:
        for field, output in (
            ("ps_ttm", "PS TTM"),
            ("pe_ttm", "PE TTM / PEG"),
            ("pb", "PB-ROE 参考"),
        ):
            if _to_float_or_none(latest_daily.get(field)) is None:
                add_quality_item(
                    metric=f"market.{field}",
                    status="missing_field",
                    problem=f"financial_daily_basic 最新快照缺少 {field}",
                    source_collection="financial_daily_basic",
                    required_fields=[field],
                    affected_output=[output],
                )
        for field, label in (("ps_ttm", "PS TTM"), ("pe_ttm", "PE TTM"), ("pb", "PB")):
            if (
                valuation_percentiles.get(field) is None
                and _to_float_or_none(latest_daily.get(field)) is not None
                and len(_clean_numeric([r.get(field) for r in daily_rows])) < 30
            ):
                add_quality_item(
                    metric=f"valuation_percentiles.{field}",
                    status="insufficient_history",
                    problem=f"{label} 历史分位需要至少 30 个正值交易日样本，当前样本不足",
                    source_collection="financial_daily_basic",
                    required_fields=[field, "trade_date"],
                    affected_output=[f"{label} 自身历史分位"],
                )

    indicator_field_outputs = {
        "rd_exp": ["研发强度", "Rule of 40"],
        "fcff": ["FCFF margin", "DCF 权重", "FCFF 季节性"],
        "grossprofit_margin": ["毛利率", "PS 合理性判断"],
        "netprofit_margin": ["净利率", "Rule of 40"],
        "q_netprofit_margin": ["利润质量: 单季净利率"],
        "dtprofit_to_profit": ["利润质量: 扣非净利润占比"],
        "opincome_of_ebt": ["利润质量: 主业利润占比"],
        "investincome_of_ebt": ["利润质量: 投资收益占利润总额比"],
        "salescash_to_or": ["现金流质量: 销售收现比"],
        "ocf_to_or": ["现金流质量: 经营现金流/营收比"],
        "ocf_yoy": ["现金流质量: 经营现金流同比"],
        "fixed_assets": ["资产效率 proxy: 固定资产"],
        "total_fa_trun": ["资产效率 proxy: 固定资产周转率"],
        "capitalized_to_da": ["资产效率 proxy: 研发资本化程度"],
        "roic_yearly": ["资产效率 proxy: 年化 ROIC"],
    }
    if not latest_indicator:
        add_quality_item(
            metric="financial_indicator",
            status="missing_collection",
            problem="缺少 financial_indicator 最新财务指标，研发/利润率/现金流/质量指标不可得",
            source_collection="financial_indicator",
            required_fields=list(indicator_field_outputs.keys()),
            affected_output=["财务质量", "现金流质量", "资产效率", "Rule of 40"],
        )
    else:
        for field, outputs in indicator_field_outputs.items():
            if _to_float_or_none(latest_indicator.get(field)) is None:
                add_quality_item(
                    metric=f"financial_indicator.{field}",
                    status="missing_field",
                    problem=f"financial_indicator 最新期缺少 {field}，可能需要回填新增字段",
                    source_collection="financial_indicator",
                    required_fields=[field],
                    affected_output=outputs,
                )
        if latest_indicator.get("tr_yoy") is None and latest_indicator.get("or_yoy") is None:
            add_quality_item(
                metric="financial.revenue_growth_pct",
                status="missing_field",
                problem="financial_indicator 最新期缺少 tr_yoy/or_yoy，收入增速不可得",
                source_collection="financial_indicator",
                required_fields=["tr_yoy", "or_yoy"],
                affected_output=["收入增速", "Rule of 40", "PEG/PS 增速匹配"],
            )

    if not latest_income:
        add_quality_item(
            metric="financial_income",
            status="missing_collection",
            problem="缺少 financial_income 最新利润表，营收/净利/投资收益不可得",
            source_collection="financial_income",
            required_fields=["revenue", "total_revenue", "n_income_attr_p", "invest_income"],
            affected_output=["研发强度", "FCFF margin", "归一化 PE TTM", "投资收益科目"],
        )
    else:
        if revenue is None:
            add_quality_item(
                metric="financial.revenue_yuan",
                status="missing_field",
                problem="financial_income 最新期缺少 revenue/total_revenue",
                source_collection="financial_income",
                required_fields=["revenue", "total_revenue"],
                affected_output=["研发强度", "FCFF margin", "Rule of 40"],
            )
        if _to_float_or_none(latest_income.get("invest_income")) is None:
            add_quality_item(
                metric="financial.invest_income_yuan",
                status="missing_field",
                problem="financial_income 最新期缺少 invest_income，无法量化投资收益科目",
                source_collection="financial_income",
                required_fields=["invest_income"],
                affected_output=["投资收益科目", "利润质量交叉验证"],
            )

    if normalized_pe_block.get("normalized_pe_ttm") is None:
        if ttm_income_info is None:
            add_quality_item(
                metric="derived.normalized_pe_ttm",
                status="insufficient_history",
                problem="归一化 PE TTM 需要最近季度、去年年报、去年同期季度的 n_income_attr_p，当前利润表期数不足或净利为非正",
                source_collection="financial_income",
                required_fields=["n_income_attr_p", "end_date"],
                affected_output=["归一化 PE TTM", "失真当期 PE 的交叉验证"],
            )
        elif _to_float_or_none(latest_daily.get("total_mv")) is None:
            add_quality_item(
                metric="derived.normalized_pe_ttm",
                status="missing_field",
                problem="已有 TTM 净利但缺少 total_mv，无法计算归一化 PE",
                source_collection="financial_daily_basic",
                required_fields=["total_mv"],
                affected_output=["归一化 PE TTM"],
            )

    if forward_peg is None:
        if not _has_at_least_two_forward_q4(consensus_payload, latest_indicator.get("end_date")):
            add_quality_item(
                metric="derived.forward_peg",
                status="insufficient_consensus",
                problem="前瞻 PEG 需要至少两个 Q4 全年一致预期净利中位数，当前 consensus 样本不足",
                source_collection="earnings_report_rc",
                required_fields=["quarter", "np", "report_date", "org_name"],
                affected_output=["前瞻 PE", "前瞻 PEG"],
                repair_collection="earnings_report_rc",
            )
        else:
            add_quality_item(
                metric="derived.forward_peg",
                status="not_applicable",
                problem="一致预期增速或前瞻 PE 为非正，PEG 口径当前不适用",
                source_collection="earnings_report_rc",
                required_fields=["quarter", "np"],
                affected_output=["前瞻 PEG"],
                repair_command=None,
            )

    if not segments.get("by_type"):
        add_quality_item(
            metric="segments",
            status="missing_collection",
            problem="缺少 financial_mainbz 主营构成，无法输出分部收入占比与分部增速",
            source_collection="financial_mainbz",
            required_fields=["bz_type", "bz_item", "bz_sales", "end_date"],
            affected_output=["分部收入占比", "segments_series", "SOTP 输入"],
        )

    # 仅当利润质量判断所需字段齐全且确实未触发时才标 not_triggered；
    # 字段缺失的情况已由对应 missing_field 项覆盖，避免“缺数据”与“已评估未触发”口径冲突。
    profit_fields_complete = bool(latest_indicator) and all(
        _to_float_or_none(latest_indicator.get(field)) is not None
        for field in _QUALITY_PROFIT_FIELDS
    )
    if profit_quality_alert is None and profit_fields_complete:
        add_quality_item(
            metric="derived.profit_quality_alert",
            status="not_triggered",
            problem="利润质量字段齐全，未触发投资收益占比过高、扣非占比过低或单季净利率异常等告警",
            source_collection="financial_indicator",
            required_fields=list(_QUALITY_PROFIT_FIELDS),
            affected_output=["利润质量告警"],
            repair_command=None,
        )
    if fcff_seasonality_note is None and fcff_series_vals:
        add_quality_item(
            metric="derived.fcff_seasonality_note",
            status="not_triggered",
            problem="未触发 FCFF 季节性或结构性现金流压力提示",
            source_collection="financial_indicator",
            required_fields=["fcff"],
            affected_output=["FCFF 季节性说明"],
            repair_command=None,
        )
    if not cash_flow_block.get("available"):
        add_quality_item(
            metric="cash_flow",
            status="missing_collection",
            problem="缺少 financial_cashflow 数据，EBITDA→OCF→FCF 桥接与现金流归因不可得",
            source_collection="financial_cashflow",
            required_fields=[
                "n_cashflow_act",
                "c_pay_acq_const_fiolta",
                "decr_oper_payable",
                "oth_pay_ral_inv_act",
            ],
            affected_output=["cash_flow 桥接", "自由现金流口径归因"],
        )
    elif cash_flow_block.get("note") is None:
        add_quality_item(
            metric="cash_flow.note",
            status="missing_field",
            problem="financial_cashflow 缺少经营现金流/资本开支关键科目，无法给出自由现金流口径结论",
            source_collection="financial_cashflow",
            required_fields=["n_cashflow_act", "c_pay_acq_const_fiolta"],
            affected_output=["自由现金流口径归因"],
        )

    data_quality = {
        "overall": _data_quality_overall(data_quality_items),
        "items": data_quality_items,
        "summary": {
            "must_fix_count": sum(1 for item in data_quality_items if item.get("must_fix")),
            "info_count": sum(1 for item in data_quality_items if not item.get("must_fix")),
        },
        "status_note": (
            "missing_collection/missing_field/insufficient_history/insufficient_consensus 需要补数；"
            "not_triggered/not_applicable 不应进入缺失清单"
        ),
    }

    return {
        "success": True,
        "symbol": normalized_symbol,
        "resolved_symbol": resolved_symbol,
        "name": info.get("name", ""),
        "industry": info.get("industry", ""),
        "sw_industry": {
            "l1": info.get("sw_l1"),
            "l2": info.get("sw_l2"),
            "l3": info.get("sw_l3"),
        },
        "market": {
            "trade_date": latest_daily.get("trade_date"),
            "close": _round_or_none(latest_daily.get("close"), 4),
            "pe": _round_or_none(latest_daily.get("pe"), 4),
            "pe_ttm": _round_or_none(latest_daily.get("pe_ttm"), 4),
            "pb": _round_or_none(latest_daily.get("pb"), 4),
            "ps": _round_or_none(latest_daily.get("ps"), 4),
            "ps_ttm": _round_or_none(latest_daily.get("ps_ttm"), 4),
            "total_mv_wan": _round_or_none(latest_daily.get("total_mv"), 2),
            "circ_mv_wan": _round_or_none(latest_daily.get("circ_mv"), 2),
            "dv_ttm": _round_or_none(latest_daily.get("dv_ttm"), 4),
        },
        "financial": {
            "indicator_period": latest_indicator.get("end_date"),
            "income_period": latest_income.get("end_date"),
            "revenue_yuan": _round_or_none(revenue, 2),
            "revenue_source": (
                "financial_income.revenue"
                if _first_positive(latest_income.get("revenue")) is not None
                else ("financial_income.total_revenue" if revenue is not None else None)
            ),
            "rd_exp_yuan": _round_or_none(rd_exp, 2),
            "gross_margin_pct": _round_or_none(latest_indicator.get("grossprofit_margin"), 2),
            "net_margin_pct": net_margin_pct,
            "fcff_yuan": _round_or_none(fcff, 2),
            "fcfe_yuan": _round_or_none(latest_indicator.get("fcfe"), 2),
            "ocfps": _round_or_none(latest_indicator.get("ocfps"), 4),
            "roe_pct": _round_or_none(latest_indicator.get("roe"), 2),
            "roic_pct": _round_or_none(latest_indicator.get("roic"), 2),
            "revenue_growth_pct": revenue_growth_pct,
            "netprofit_yoy_pct": _round_or_none(latest_indicator.get("netprofit_yoy"), 2),
            "debt_to_assets_pct": _round_or_none(latest_indicator.get("debt_to_assets"), 2),
            "n_income_attr_p_yuan": _round_or_none(latest_income.get("n_income_attr_p"), 2),
            "invest_income_yuan": _round_or_none(invest_income, 2),
            "invest_income_to_revenue_pct": invest_income_to_revenue_pct,
        },
        "derived": {
            "rd_intensity": _round_or_none(rd_intensity, 6),
            "rd_intensity_pct": round(rd_intensity * 100, 2) if rd_intensity is not None else None,
            "fcff_margin": _round_or_none(fcff_margin, 6),
            "fcff_margin_pct": fcff_margin_pct,
            "rule_of_40_net_margin": rule_of_40_net,
            "rule_of_40_fcff_margin": rule_of_40_fcff,
            "rule_of_40_note": "收入增速(%) + 净利率(%) / FCFF margin(%)，仅作科技企业质量粗筛",
            "fcff_positive_periods": fcff_positive_count,
            "fcff_sample_periods": len(fcff_series_vals),
            "fcff_caliber_note": (
                "financial.fcff_yuan / derived.fcff_margin* 基于 financial_indicator.fcff"
                "(Tushare 现金流口径自由现金流≈OCF-资本开支)，非同花顺/学术 FCFF；"
                "现金流口径分析与每股值请以 cash_flow 块为准"
            ),
            "profit_quality_alert": profit_quality_alert,
            "fcff_seasonality_note": fcff_seasonality_note,
            "peg_hist": peg_hist,
            "peg_hist_note": (
                "PEG(历史) = market.pe_ttm / financial.netprofit_yoy_pct；"
                "PE<=0 或净利增速<=0 时为 null"
            ),
            "forward_pe": forward_pe,
            "forward_peg": forward_peg,
            "forward_growth": forward_growth,
            "forward_peg_note": (
                "forward_pe = 总市值 / 最近预测财年一致净利(中位数)；"
                "forward_peg = forward_pe / 一致净利增速(%)；"
                "增速取最新已完成财年起最近两个 Q4 全年一致净利中位数；"
                "无一致预期时为 null"
            ),
            **normalized_pe_block,
        },
        "cash_flow": cash_flow_block,
        "series": series,
        "segments": segments,
        "segments_series": segments_series,
        "segments_note": (
            "最新一期主营构成(fina_mainbz)按产品(P)/地区(D)/行业(I)拆分；"
            "share_pct 为该维度内 bz_sales 净额占比，利润/估值倍数需研究员另行确认"
        ),
        "valuation_percentiles": valuation_percentiles,
        "valuation_percentile_note": (
            "当前 PE/PB/PS(TTM) 在自身近 "
            f"{len(daily_rows)} 个交易日历史中的分位(0-100)，样本不足 30 返回 null"
        ),
        "quality": {
            "profit_quality": _quality_metric_block(latest_indicator, _QUALITY_PROFIT_FIELDS),
            "cash_flow_quality": _quality_metric_block(latest_indicator, _QUALITY_CASH_FLOW_FIELDS),
            "asset_efficiency": _quality_metric_block(latest_indicator, _QUALITY_ASSET_EFFICIENCY_FIELDS, digits=2),
        },
        "quality_notes": _QUALITY_NOTES,
        "samples": {
            "indicator_count": len(indicator_rows),
            "income_count": len(income_rows),
            "daily_count": len(daily_rows),
        },
        "missing": missing,
        "missing_notes": missing_notes,
        "data_quality": data_quality,
        "source": (
            "quant_data.financial_daily_basic + financial_indicator + financial_income + "
            "financial_mainbz + stock_info; Hermes MCP read-only valuation metrics"
        ),
    }


@router.get("/stock/valuation-metrics", operation_id="stock_valuation_metrics", tags=[MCP_READ_TAG])
async def stock_valuation_metrics(
    symbol: str = Query(..., description="股票代码，如 688111.SH 或 688111"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单股科技估值资料包需要的结构化指标。

    直接读取 quant_data 的 financial_daily_basic、financial_indicator、
    financial_income 和 stock_info，返回 PS、研发强度、FCFF margin、收入增速、
    毛利率/净利率及 Rule of 40 粗值。缺字段进入 missing，不让 LLM 猜。
    """
    try:
        return _stock_valuation_metrics_payload(symbol)
    except Exception:
        # 临时诊断日志：保留原 500 行为，但把真实 traceback 打到 quant-api 日志。
        logger.exception("stock_valuation_metrics failed symbol=%s", symbol)
        raise


@router.get("/stock/earnings-consensus", operation_id="stock_earnings_consensus", tags=[MCP_READ_TAG])
async def stock_earnings_consensus(
    symbol: str = Query(..., description="股票代码，如 688111.SH 或 688111"),
    lookback_days: int = Query(180, ge=30, le=730, description="回溯研报天数，默认 180"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单股卖方研报一致预期（按预测财年聚合）。

    读取 quant_data.earnings_report_rc，按 quarter（如 2025Q4）聚合净利/营收/EPS
    中位数、评级分布与覆盖机构列表。缺数据进入 missing。
    """
    return _stock_earnings_consensus_payload(symbol, lookback_days=lookback_days)


@router.get("/stock/industry-comparison", operation_id="stock_industry_comparison", tags=[MCP_READ_TAG])
async def stock_industry_comparison(
    symbol: str = Query(..., description="股票代码，如 688111.SH 或 688111"),
    level: str = Query("sw_l2", description="同业层级：sw_l2、sw_l1 或 industry；缺失时自动降级"),
    min_peer_count: int = Query(
        5, ge=2, le=100, description="计算中位数/分位所需的最少同业样本数，低于该值返回 null"
    ),
    peer_symbols: Optional[str] = Query(
        None,
        description="点名同业并排对比的代码，逗号分隔，如 603039.SH,600588.SH；留空则取同行业市值 Top N",
    ),
    named_peer_limit: int = Query(
        5, ge=1, le=20, description="未指定 peer_symbols 时，按市值返回的点名同业数量"
    ),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单股相对同业的估值与财务指标分位。

    使用 stock_info 的申万/行业字段确定同业集合，并在目标股最新交易日/财报期
    上计算 PE/PB/PS/PEG、市值、ROE、利润率、收入增速的行业横截面分位；
    named_peers 额外给出点名同业(默认市值 Top N 或 peer_symbols 指定)的并排数值。
    """
    parsed_peer_symbols = (
        [s.strip() for s in peer_symbols.split(",") if s.strip()] if peer_symbols else None
    )
    return _stock_industry_comparison_payload(
        symbol,
        level=level,
        min_peer_count=min_peer_count,
        peer_symbols=parsed_peer_symbols,
        named_peer_limit=named_peer_limit,
    )


# ---------------------------------------------------------------------------
# 单股量化评分详情
# ---------------------------------------------------------------------------

@router.get("/stock/score-detail", operation_id="stock_score_detail", tags=[MCP_READ_TAG])
async def stock_score_detail(
    symbol: str = Query(..., description="股票代码，如 600519"),
    index_code: Optional[str] = Query(None, description="宽基 scope；不传则显式取该票全局最新"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单只股票最新量化评分的六维详细拆解。

    返回字段：综合评分（balanced/aggressive/conservative 三策略）及
    cycle/value/fundamental/growth/technical/money_flow 六维子评分。
    """
    from stock_data_access.score_query import merge_score_scope
    from stock_data_access.score_utils import safe_round
    db = get_db_collections()
    doc = db["stock_scores"].find_one(
        merge_score_scope({"symbol": symbol}, index_code),
        sort=[("score_date", -1)],
    )
    if not doc:
        return {"success": False, "error": f"未找到 {symbol} 的评分数据"}

    info = db["stock_info"].find_one({"symbol": symbol}, {"name": 1, "industry": 1}) or {}
    cs = doc.get("composite_score", 0)
    if not isinstance(cs, dict):
        cs = {"balanced": cs}

    return {
        "success": True,
        "symbol": symbol,
        "name": info.get("name", ""),
        "industry": info.get("industry", ""),
        "score_date": doc.get("score_date", ""),
        "composite_score": {k: safe_round(v, 2) for k, v in cs.items() if isinstance(v, (int, float))},
        "cycle_score": safe_round(doc.get("cycle_score", 0), 2),
        "value_score": safe_round(doc.get("value_score", 0), 2),
        "fundamental_score": safe_round(doc.get("fundamental_score", 0), 2),
        "growth_score": safe_round(doc.get("growth_score", 0), 2),
        "technical_score": safe_round(doc.get("technical_score", 0), 2),
        "money_flow_score": safe_round(doc.get("money_flow_score", 0), 2),
    }


# ---------------------------------------------------------------------------
# 外资投行前十大流通股东筛选
# ---------------------------------------------------------------------------

def _normalize_foreign_holder_name(name: Any) -> Optional[Dict[str, str]]:
    text = str(name or "").strip()
    if not text:
        return None
    for key, label, pattern in _FOREIGN_HOLDER_PATTERNS:
        if pattern.search(text):
            return {"key": key, "label": label, "holder_name": text}
    return None


def _selected_foreign_holder_keys(raw: Optional[str]) -> Optional[set[str]]:
    if not raw:
        return None
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens or any(token.lower() in {"all", "*"} for token in tokens):
        return None

    keys: set[str] = set()
    for token in tokens:
        matched = _normalize_foreign_holder_name(token)
        if matched:
            keys.add(matched["key"])
            continue
        tl = token.lower()
        for key, label, _pattern in _FOREIGN_HOLDER_PATTERNS:
            if tl == key.lower() or token in label:
                keys.add(key)
                break
    return keys


def _get_number(row: Dict[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    if value is None and isinstance(row.get("raw"), dict):
        value = row["raw"].get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_percent_points(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:.2f}%"


def _symbol_variants(ts_code: str) -> List[str]:
    symbol = str(ts_code or "").strip()
    if not symbol:
        return []
    variants = {symbol}
    if "." in symbol:
        variants.add(symbol.split(".", 1)[0])
    return sorted(variants)


def _date_query(start: Optional[str], end: Optional[str]) -> Dict[str, str]:
    query: Dict[str, str] = {}
    if start:
        query["$gte"] = start
    if end and end.lower() != "latest":
        query["$lte"] = end
    return query


def _range_return_for_symbol(
    price_collection: Any,
    ts_code: str,
    *,
    start: Optional[str],
    end: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not start:
        return None

    date_filter = _date_query(start, end)
    variants = _symbol_variants(ts_code)
    if not variants:
        return None

    query: Dict[str, Any] = {
        "$or": [
            {"symbol": {"$in": variants}},
            {"ts_code": {"$in": variants}},
            {"con_code": {"$in": variants}},
        ],
        "trade_date": date_filter,
    }
    projection = {"_id": 0, "symbol": 1, "ts_code": 1, "trade_date": 1, "close": 1}

    rows = list(price_collection.find(query, projection, sort=[("trade_date", 1)]))
    valid_rows = [row for row in rows if _get_number(row, "close") not in (None, 0)]
    if len(valid_rows) < 2:
        return None

    start_row = valid_rows[0]
    end_row = valid_rows[-1]
    start_close = _get_number(start_row, "close")
    end_close = _get_number(end_row, "close")
    if start_close in (None, 0) or end_close is None:
        return None

    return_pct = (end_close - start_close) / start_close * 100
    return {
        "return_start": start,
        "return_end": end or "latest",
        "return_start_trade_date": start_row.get("trade_date"),
        "return_end_trade_date": end_row.get("trade_date"),
        "return_start_close": start_close,
        "return_end_close": end_close,
        "return_pct": round(return_pct, 4),
    }


def _range_returns_for_symbols(
    price_collection: Any,
    symbols: List[str],
    *,
    start: Optional[str],
    end: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    if not start:
        return {}

    canonical_symbols = sorted({str(symbol or "").strip() for symbol in symbols if symbol})
    if not canonical_symbols:
        return {}

    variant_to_symbol: Dict[str, str] = {}
    for symbol in canonical_symbols:
        for variant in _symbol_variants(symbol):
            variant_to_symbol[variant] = symbol

    date_filter = _date_query(start, end)
    query: Dict[str, Any] = {
        "$or": [
            {"symbol": {"$in": sorted(variant_to_symbol)}},
            {"ts_code": {"$in": sorted(variant_to_symbol)}},
            {"con_code": {"$in": sorted(variant_to_symbol)}},
        ],
        "trade_date": date_filter,
    }
    projection = {"_id": 0, "symbol": 1, "ts_code": 1, "con_code": 1, "trade_date": 1, "close": 1}

    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = {symbol: [] for symbol in canonical_symbols}
    for row in price_collection.find(query, projection, sort=[("trade_date", 1)]):
        matched_symbol = None
        for field in ("ts_code", "symbol", "con_code"):
            value = str(row.get(field) or "").strip()
            if value in variant_to_symbol:
                matched_symbol = variant_to_symbol[value]
                break
        if not matched_symbol:
            continue
        if _get_number(row, "close") in (None, 0):
            continue
        rows_by_symbol.setdefault(matched_symbol, []).append(row)

    returns: Dict[str, Dict[str, Any]] = {}
    for symbol, rows in rows_by_symbol.items():
        if len(rows) < 2:
            continue
        start_row = rows[0]
        end_row = rows[-1]
        start_close = _get_number(start_row, "close")
        end_close = _get_number(end_row, "close")
        if start_close in (None, 0) or end_close is None:
            continue
        return_pct = (end_close - start_close) / start_close * 100
        returns[symbol] = {
            "return_start": start,
            "return_end": end or "latest",
            "return_start_trade_date": start_row.get("trade_date"),
            "return_end_trade_date": end_row.get("trade_date"),
            "return_start_close": start_close,
            "return_end_close": end_close,
            "return_pct": round(return_pct, 4),
        }

    return returns


def _latest_previous_period(collection: Any, period: str) -> Optional[str]:
    try:
        periods = collection.distinct("end_date", {"end_date": {"$lt": period}})
    except Exception:
        periods = []
    valid = sorted(str(p) for p in periods if p and str(p) < period)
    return valid[-1] if valid else None


def _foreign_holder_row(row: Dict[str, Any], norm: Dict[str, str]) -> Dict[str, Any]:
    hold_ratio = _get_number(row, "hold_ratio")
    return {
        "ts_code": str(row.get("ts_code") or ""),
        "ann_date": row.get("ann_date"),
        "end_date": row.get("end_date"),
        "holder_name": row.get("holder_name"),
        "norm_key": norm["key"],
        "norm_label": norm["label"],
        "holder_rank": row.get("holder_rank"),
        "hold_amount": _get_number(row, "hold_amount"),
        # Tushare/top10_floatholders stores hold_ratio as percent points
        # (e.g. 0.199 means 0.199%), not as a 0-1 fraction.
        "hold_ratio": hold_ratio,
        "hold_ratio_pct": hold_ratio,
        "hold_ratio_display": _format_percent_points(hold_ratio),
    }


def _foreign_holder_matches(collection: Any, period: str, selected_keys: Optional[set[str]]) -> List[Dict[str, Any]]:
    rows = list(collection.find(
        {"end_date": period},
        {
            "_id": 0,
            "ts_code": 1,
            "ann_date": 1,
            "end_date": 1,
            "holder_name": 1,
            "holder_rank": 1,
            "hold_amount": 1,
            "hold_ratio": 1,
            "raw": 1,
        },
    ))
    matches: List[Dict[str, Any]] = []
    for row in rows:
        norm = _normalize_foreign_holder_name(row.get("holder_name"))
        if not norm:
            continue
        if selected_keys and norm["key"] not in selected_keys:
            continue
        matches.append(_foreign_holder_row(row, norm))
    return matches


def _holder_pair(row: Dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("ts_code") or ""), str(row.get("norm_key") or ""))


def _change_status(current: Optional[Dict[str, Any]], previous: Optional[Dict[str, Any]]) -> str:
    if current and not previous:
        return "new"
    if previous and not current:
        return "exited"
    if not current or not previous:
        return "unknown"

    cur_ratio = current.get("hold_ratio_pct")
    prev_ratio = previous.get("hold_ratio_pct")
    if cur_ratio is not None and prev_ratio is not None:
        delta = cur_ratio - prev_ratio
    else:
        cur_amount = current.get("hold_amount")
        prev_amount = previous.get("hold_amount")
        if cur_amount is None or prev_amount is None:
            return "unchanged"
        delta = cur_amount - prev_amount

    if delta > 0:
        return "increased"
    if delta < 0:
        return "decreased"
    return "unchanged"


def _foreign_holder_changes(
    current_rows: List[Dict[str, Any]],
    previous_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    current_map = {_holder_pair(row): row for row in current_rows}
    previous_map = {_holder_pair(row): row for row in previous_rows}
    changes: List[Dict[str, Any]] = []

    for pair in sorted(set(current_map) | set(previous_map)):
        current = current_map.get(pair)
        previous = previous_map.get(pair)
        base = current or previous or {}

        amount_delta = None
        ratio_delta = None
        if current and previous:
            cur_amount = current.get("hold_amount")
            prev_amount = previous.get("hold_amount")
            if cur_amount is not None and prev_amount is not None:
                amount_delta = cur_amount - prev_amount
            cur_ratio = current.get("hold_ratio_pct")
            prev_ratio = previous.get("hold_ratio_pct")
            if cur_ratio is not None and prev_ratio is not None:
                ratio_delta = cur_ratio - prev_ratio

        changes.append({
            "ts_code": base.get("ts_code"),
            "norm_key": base.get("norm_key"),
            "norm_label": base.get("norm_label"),
            "change_type": _change_status(current, previous),
            "current": current,
            "previous": previous,
            "hold_amount_chg": amount_delta,
            "hold_ratio_chg": ratio_delta,
            "hold_ratio_chg_display": _format_percent_points(ratio_delta),
        })

    return changes


def _stock_info_map(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    try:
        collections = get_db_collections()
        stock_info = collections.get("stock_info") if isinstance(collections, dict) else None
        if stock_info is None:
            return {}
        rows = stock_info.find(
            {"symbol": {"$in": symbols}},
            {"_id": 0, "symbol": 1, "name": 1, "industry": 1},
        )
        return {row.get("symbol"): row for row in rows if row.get("symbol")}
    except Exception:
        return {}


def _foreign_top10_floatholders_payload(
    *,
    period: str,
    holders: Optional[str] = None,
    new_only: bool = False,
    previous_period: Optional[str] = None,
    return_start: Optional[str] = None,
    return_end: Optional[str] = None,
    max_return_pct: Optional[float] = None,
    limit: int = 200,
    collection: Any = None,
    price_collection: Any = None,
) -> Dict[str, Any]:
    normalized_period = period.strip()
    if not normalized_period:
        return {"success": False, "error": "period 不能为空，格式为 YYYYMMDD，例如 20260331"}

    coll = collection or data_db["top10_floatholders"]
    prices = price_collection or data_db["volume_price"]
    selected_keys = _selected_foreign_holder_keys(holders)
    current_matches = _foreign_holder_matches(coll, normalized_period, selected_keys)

    previous_pairs: set[tuple[str, str]] = set()
    resolved_previous_period = previous_period.strip() if previous_period else None
    if not resolved_previous_period:
        resolved_previous_period = _latest_previous_period(coll, normalized_period)
    previous_matches = (
        _foreign_holder_matches(coll, resolved_previous_period, selected_keys)
        if resolved_previous_period
        else []
    )
    changes = _foreign_holder_changes(current_matches, previous_matches) if previous_matches else []
    warning = None
    if new_only:
        if previous_matches:
            previous_pairs = {_holder_pair(row) for row in previous_matches}
        else:
            warning = "未找到上一报告期数据，new_only=true 时无法判断是否新进；返回当期命中结果。"

    matches: List[Dict[str, Any]] = []
    for row in current_matches:
        if new_only and previous_pairs and _holder_pair(row) in previous_pairs:
            continue
        matches.append(dict(row))

    missing_return_count = 0
    if return_start:
        filtered_matches: List[Dict[str, Any]] = []
        return_map = _range_returns_for_symbols(
            prices,
            [row.get("ts_code") or "" for row in matches],
            start=return_start.strip(),
            end=return_end.strip() if return_end else None,
        )
        for row in matches:
            ret = return_map.get(row.get("ts_code") or "")
            if ret is None:
                missing_return_count += 1
                if max_return_pct is None:
                    row["range_return"] = None
                    row["max_return_pct"] = max_return_pct
                    filtered_matches.append(row)
                continue
            row["range_return"] = ret
            row["return_pct"] = ret["return_pct"]
            row["return_pct_display"] = _format_percent_points(ret["return_pct"])
            row["return_start_trade_date"] = ret["return_start_trade_date"]
            row["return_end_trade_date"] = ret["return_end_trade_date"]
            row["max_return_pct"] = max_return_pct
            if max_return_pct is None or ret["return_pct"] <= max_return_pct:
                filtered_matches.append(row)
        matches = filtered_matches

    symbols = sorted({
        str(row.get("ts_code") or "")
        for row in matches + changes
        if row.get("ts_code")
    })
    info_map = _stock_info_map(symbols)
    for row in matches:
        info = info_map.get(row["ts_code"], {})
        row["name"] = info.get("name", "")
        row["industry"] = info.get("industry", "")
    for row in changes:
        info = info_map.get(row.get("ts_code"), {})
        row["name"] = info.get("name", "")
        row["industry"] = info.get("industry", "")

    matches.sort(key=lambda row: (
        row.get("ts_code") or "",
        row.get("holder_rank") if row.get("holder_rank") is not None else 999,
        row.get("norm_key") or "",
    ))
    limited = matches[:limit]

    by_holder: Dict[str, int] = {}
    for row in matches:
        by_holder[row["norm_label"]] = by_holder.get(row["norm_label"], 0) + 1
    change_summary: Dict[str, int] = {}
    for row in changes:
        key = row.get("change_type") or "unknown"
        change_summary[key] = change_summary.get(key, 0) + 1

    return {
        "success": True,
        "period": normalized_period,
        "previous_period": resolved_previous_period,
        "new_only": new_only,
        "holders": holders or "all",
        "return_filter": {
            "return_start": return_start,
            "return_end": return_end or "latest",
            "max_return_pct": max_return_pct,
            "missing_return_count": missing_return_count,
        } if return_start else None,
        "matched_count": len(matches),
        "returned_count": len(limited),
        "by_holder": by_holder,
        "change_summary": change_summary,
        "changes": changes,
        "results": limited,
        **({"warning": warning} if warning else {}),
    }


@router.get("/stock/foreign-top10-floatholders", operation_id="foreign_top10_floatholders", tags=[MCP_READ_TAG])
def foreign_top10_floatholders(
    period: str = Query(..., description="报告期 YYYYMMDD，例如 20260331"),
    holders: Optional[str] = Query(
        None,
        description="逗号分隔机构关键词，默认全部：高盛,摩根士丹利,瑞银,Barclays",
    ),
    new_only: bool = Query(False, description="是否仅返回较上一报告期新进前十大流通股东的机构"),
    previous_period: Optional[str] = Query(None, description="上一报告期 YYYYMMDD；不填则自动取 period 前最近一期"),
    return_start: Optional[str] = Query(None, description="区间涨幅起始交易日 YYYYMMDD，例如 20260101"),
    return_end: Optional[str] = Query(None, description="区间涨幅结束交易日 YYYYMMDD；不填或 latest 表示最新可用交易日"),
    max_return_pct: Optional[float] = Query(None, description="最大区间涨幅百分比，例如 30 表示涨幅不超过 30%"),
    limit: int = Query(200, ge=1, le=1000, description="最多返回条数"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    按报告期筛选高盛、摩根士丹利、瑞银、Barclays/巴克莱等外资投行进入
    前十大流通股东的股票。数据来自 quant_data.top10_floatholders。
    """
    return _foreign_top10_floatholders_payload(
        period=period,
        holders=holders,
        new_only=new_only,
        previous_period=previous_period,
        return_start=return_start,
        return_end=return_end,
        max_return_pct=max_return_pct,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# 用户登录
# ---------------------------------------------------------------------------

class _UserLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/user/login", operation_id="user_login", tags=[MCP_ACTION_TAG])
async def user_login(
    req: _UserLoginRequest,
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    用户登录，返回 JWT token（默认有效期 30 天，可用 MCP_ACCESS_TOKEN_EXPIRE_MINUTES 调整）。

    登录成功后，将返回的 token 配置到 MCP headers 的 X-User-Token 字段，
    之后即可使用 user_profile / user_watchlist_stocks 等个人化工具。

    配置步骤：
    1. 打开 .vscode/mcp.json（或 ~/.hermes/config.yaml）
    2. 在 wukong-quant 的 headers 下添加 "X-User-Token": "<返回的 token>"
    3. 重新加载 VS Code 窗口（Ctrl+Shift+P → Developer: Reload Window）
    """
    from auth import authenticate_user, create_access_token
    user = authenticate_user(req.username, req.password)
    if not user:
        return {"success": False, "error": "用户名或密码错误"}
    token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=timedelta(minutes=MCP_ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "success": True,
        "username": user["username"],
        "token": token,
        "expires_in": _format_expiry(MCP_ACCESS_TOKEN_EXPIRE_MINUTES),
        "next_step": (
            "请将上面的 token 添加到 .vscode/mcp.json 的 wukong-quant.headers 中："
            ' "X-User-Token": "<token>"，然后重载 VS Code 窗口。'
            "之后即可调用 user_profile、user_watchlist_stocks、user_watchlist_add 等个人化工具。"
        ),
    }


# ---------------------------------------------------------------------------
# 用户账户信息
# ---------------------------------------------------------------------------

@router.get("/user/profile", operation_id="user_profile", tags=[MCP_READ_TAG])
async def user_profile(
    user: dict = Depends(get_user_from_header_token),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取当前登录用户的账户基本信息（用户名、邮箱、服务等级、注册时间等）。

    需要在 MCP headers 中设置 X-User-Token（通过 user_login 工具获取）。
    """
    return {
        "success": True,
        "username": user.get("username"),
        "email": user.get("email"),
        "full_name": user.get("full_name"),
        "is_active": user.get("is_active", True),
        "is_admin": user.get("is_admin", False),
        "service_level": user.get("service_level", "free"),
        "created_at": str(user.get("created_at", "")),
        "last_login": str(user.get("last_login", "")),
    }


# ---------------------------------------------------------------------------
# 用户自选股评分列表
# ---------------------------------------------------------------------------

@router.get("/user/watchlist", operation_id="user_watchlist_stocks", tags=[MCP_READ_TAG])
async def user_watchlist_stocks(
    strategy: str = Query("balanced", description="评分策略：balanced / aggressive / conservative"),
    user: dict = Depends(get_user_from_header_token),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取当前用户自选股列表，并附带每只股票最新量化评分（六维子分），按综合分降序排列。

    需要在 MCP headers 中设置 X-User-Token（通过 user_login 工具获取）。
    """
    from stock_data_access.score_utils import safe_round
    user_id = user["id"]
    colls = get_db_collections()

    doc = colls["user_watchlists"].find_one({"user_id": user_id})
    symbols = doc.get("symbols", []) if doc else []
    if not symbols:
        return {
            "success": True,
            "username": user.get("username"),
            "count": 0,
            "watchlist": [],
            "note": "自选股为空，可使用 user_watchlist_add 工具添加股票",
        }

    strat_map = {"aggressive": "aggressive", "conservative": "conservative"}
    strat = strat_map.get(strategy, "balanced")

    pipeline = [
        # Watchlists span indexes; this is an explicit unscoped latest-per-symbol read.
        {"$match": {"symbol": {"$in": symbols}}},
        {"$sort": {"score_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
    ]
    score_docs = list(colls["stock_scores"].aggregate(pipeline))
    scores_by_symbol = {d["symbol"]: d for d in score_docs}

    info_docs = list(colls["stock_info"].find(
        {"symbol": {"$in": symbols}}, {"symbol": 1, "name": 1, "industry": 1}
    ))
    info_by_symbol = {d["symbol"]: d for d in info_docs}

    result = []
    for sym in symbols:
        d = scores_by_symbol.get(sym, {})
        info = info_by_symbol.get(sym, {})
        cs = d.get("composite_score", 0)
        score_val = cs.get(strat, cs.get("balanced", 0)) if isinstance(cs, dict) else cs
        result.append({
            "symbol": sym,
            "name": info.get("name", ""),
            "industry": info.get("industry", ""),
            "score": safe_round(score_val, 2),
            "score_date": d.get("score_date", ""),
            "cycle_score": safe_round(d.get("cycle_score", 0), 2),
            "value_score": safe_round(d.get("value_score", 0), 2),
            "fundamental_score": safe_round(d.get("fundamental_score", 0), 2),
            "growth_score": safe_round(d.get("growth_score", 0), 2),
            "technical_score": safe_round(d.get("technical_score", 0), 2),
            "money_flow_score": safe_round(d.get("money_flow_score", 0), 2),
        })
    result.sort(key=lambda x: x["score"] or 0, reverse=True)

    return {
        "success": True,
        "username": user.get("username"),
        "strategy": strat,
        "count": len(result),
        "watchlist": result,
    }


# ---------------------------------------------------------------------------
# 自选股添加 / 删除
# ---------------------------------------------------------------------------

class _WatchlistSymbolRequest(BaseModel):
    symbol: str


@router.post("/user/watchlist/add", operation_id="user_watchlist_add", tags=[MCP_ACTION_TAG])
async def user_watchlist_add(
    req: _WatchlistSymbolRequest,
    user: dict = Depends(get_user_from_header_token),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    将股票代码添加到当前用户自选股列表（幂等，重复添加不报错）。

    需要在 MCP headers 中设置 X-User-Token（通过 user_login 工具获取）。
    """
    colls = get_db_collections()
    colls["user_watchlists"].update_one(
        {"user_id": user["id"]},
        {"$addToSet": {"symbols": req.symbol}},
        upsert=True,
    )
    return {"success": True, "symbol": req.symbol, "action": "added"}


@router.delete("/user/watchlist/remove/{symbol}", operation_id="user_watchlist_remove", tags=[MCP_ACTION_TAG])
async def user_watchlist_remove(
    symbol: str,
    user: dict = Depends(get_user_from_header_token),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    从当前用户自选股列表中删除指定股票代码。

    需要在 MCP headers 中设置 X-User-Token（通过 user_login 工具获取）。
    """
    colls = get_db_collections()
    colls["user_watchlists"].update_one(
        {"user_id": user["id"]},
        {"$pull": {"symbols": symbol}},
    )
    return {"success": True, "symbol": symbol, "action": "removed"}


# ---------------------------------------------------------------------------
# 自选股历史分析记录
# ---------------------------------------------------------------------------

@router.get("/user/watchlist/history", operation_id="user_watchlist_history", tags=[MCP_READ_TAG])
async def user_watchlist_history(
    symbol: Optional[str] = Query(None, description="可选：筛选指定股票的历史分析，不填则返回所有自选股历史"),
    limit: int = Query(10, description="最多返回条数，默认 10，最大 50"),
    user: dict = Depends(get_user_from_header_token),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    查询当前用户的历史深度分析记录（自选股分析历史）。

    可通过 symbol 参数筛选单只股票，不指定则按时间倒序返回最近的分析记录。
    需要在 MCP headers 中设置 X-User-Token（通过 user_login 工具获取）。
    """
    payload = build_user_analysis_history_payload(
        user_id=user["id"],
        symbol=symbol,
        limit=min(limit, 50),
        include_full=True,
    )
    payload["username"] = user.get("username")
    return payload


# ---------------------------------------------------------------------------
# 申万行业指数 PE/PB 历史分位
# ---------------------------------------------------------------------------

@router.get("/shenwan/pe-pb", operation_id="shenwan_index", tags=[MCP_READ_TAG])
async def shenwan_pe_pb(
    ts_code: str = Query(..., description="申万行业指数 ts_code，如 801012.SI（采掘）、801010.SI（农林牧渔）"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    查询申万行业指数月度 PE/PB 历史分位数，用于行业估值定价与安全边际判断。

    - 返回最新月末 PE、PB 及其在 5 年 / 10 年 / 全历史区间内的百分位
    - 百分位越低代表估值处于历史底部（越便宜），越高代表估值偏贵
    - 数据来自 sw_monthly 预计算月线集合（需先回补申万行业月线数据）
    - ts_code 示例：801012.SI（采掘）、801010.SI（农林牧渔）、801050.SI（有色金属）等
    """
    monthly_col = data_db["sw_monthly"]

    rows = list(
        monthly_col.find(
            {"ts_code": ts_code.strip()},
            {"_id": 0, "trade_date": 1, "pe": 1, "pb": 1},
        ).sort("trade_date", 1)
    )

    if not rows:
        return {
            "success": False,
            "ts_code": ts_code,
            "error": (
                f"未找到 {ts_code} 的月线数据，请检查 ts_code 是否正确（格式如 801012.SI），"
                "或先执行申万行业月线回补任务"
            ),
        }

    def _pct_rank(series: list, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        valid = [v for v in series if isinstance(v, (int, float)) and v == v]
        if not valid:
            return None
        below = sum(1 for v in valid if v < value)
        return round(below / len(valid) * 100, 1)

    def _stats(series: list) -> dict:
        valid = [v for v in series if isinstance(v, (int, float)) and v == v]
        if not valid:
            return {"min": None, "max": None, "mean": None, "count": 0}
        mean_val = sum(valid) / len(valid)
        return {
            "min": round(min(valid), 2),
            "max": round(max(valid), 2),
            "mean": round(mean_val, 2),
            "count": len(valid),
        }

    latest = rows[-1]
    cur_pe = latest.get("pe")
    cur_pb = latest.get("pb")
    cur_date = str(latest.get("trade_date", ""))[:8]

    all_pe = [r.get("pe") for r in rows]
    all_pb = [r.get("pb") for r in rows]

    # 5-year window: last 60 months
    win5 = rows[-60:] if len(rows) >= 60 else rows
    pe_5y = [r.get("pe") for r in win5]
    pb_5y = [r.get("pb") for r in win5]

    # 10-year window: last 120 months
    win10 = rows[-120:] if len(rows) >= 120 else rows
    pe_10y = [r.get("pe") for r in win10]
    pb_10y = [r.get("pb") for r in win10]

    return {
        "success": True,
        "ts_code": ts_code,
        "latest_month": cur_date,
        "current": {
            "pe": round(cur_pe, 2) if isinstance(cur_pe, (int, float)) else None,
            "pb": round(cur_pb, 2) if isinstance(cur_pb, (int, float)) else None,
        },
        "pe_percentile": {
            "5y": _pct_rank(pe_5y, cur_pe),
            "10y": _pct_rank(pe_10y, cur_pe),
            "all": _pct_rank(all_pe, cur_pe),
        },
        "pb_percentile": {
            "5y": _pct_rank(pb_5y, cur_pb),
            "10y": _pct_rank(pb_10y, cur_pb),
            "all": _pct_rank(all_pb, cur_pb),
        },
        "pe_stats": {
            "5y": _stats(pe_5y),
            "10y": _stats(pe_10y),
        },
        "pb_stats": {
            "5y": _stats(pb_5y),
            "10y": _stats(pb_10y),
        },
        "sample_months": len(rows),
    }


# ---------------------------------------------------------------------------
# 智能选股（多条件财务筛选）
# ---------------------------------------------------------------------------

# 字段/算子白名单与筛选逻辑统一收敛到 services.screening（见 registry.py）。


@router.get("/stock/screen", operation_id="stock_screen", tags=[MCP_READ_TAG])
async def hermes_stock_screen(
    period: str = Query(..., description="目标报告期 YYYYMMDD，如 20260331；或 'latest' 自动取最近期"),
    filters: str = Query(..., description="过滤条件列表的 JSON 字符串，元素形如 {field, metric_type, op, value, base_period?}"),
    report_type: int = Query(1, description="1=合并(累计，行业默认)，2=单季合并"),
    include_sw_industry: bool = Query(True, description="是否附申万 L1/L2/L3 行业"),
    exclude_st: bool = Query(True, description="排除股票名含 ST/*ST 的标的"),
    exclude_negative_base: bool = Query(False, description="True 则剔除去年同期<=0的股；默认保留并用 yoy_quality 标注扭亏/减亏"),
    sort_by: str = Query("auto", description="排序字段；auto 取第一个 yoy filter；可显式指定 operate_profit_yoy 等"),
    top_n: int = Query(100, ge=1, le=500, description="最多返回条数"),
    include_express: bool = Query(True, description="该股当期无正式财报时，回退用业绩快报"),
    include_forecast: bool = Query(True, description="无正式财报且无快报时，回退用业绩预告（仅归母净利润）"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    多条件财务筛选：按营业利润/营收等字段的同比增速或绝对值过滤 A 股，附申万行业。

    示例：找 2026Q1 营业利润 YoY > 35% 且 营收 YoY > 30% 且 2024Q1 营业利润 > 1 亿
        period=20260331
        filters=[
          {"field":"operate_profit","metric_type":"yoy","op":"gt","value":0.35},
          {"field":"revenue","metric_type":"yoy","op":"gt","value":0.30},
          {"field":"operate_profit","metric_type":"abs","op":"gt","value":100000000,
           "base_period":"20240331"}
        ]

    LLM 翻译要点：
    - 百分数→小数：35% → 0.35
    - 季报到期次：2026Q1 → 20260331；H1 → 0630；Q3 → 0930；年报 → 1231
    - 默认 report_type=1（累计同比，行业惯例），用户说"单季"才用 2
    - 营收默认 revenue（主营业务收入），"营业总收入"才用 total_revenue
    - "超过/大于" → gt；"不低于/至少" → gte

    数据来源：财报季早期正式财报尚少，缺正式财报的个股会回退到业绩快报、再回退到
    业绩预告。每行带 data_source（formal/express/forecast），summary.sources_distribution
    给出条数分布；引用 express/forecast 的数字时须注明是预披露值。

    同比可信度：扭亏、两期均亏、低基数的个股会保留，但每个同比字段附带
    {field}_yoy_quality（normal/turnaround/loss/low_base）与 {field}_yoy_base（去年同期金额）。
    播报 turnaround/loss/low_base 的个股时，说「扭亏为盈」「亏损收窄」「低基数」并给出
    两期金额，不要把百分比当成真实增速。
    """
    from services.screening.adapters.hermes_financial import legacy_financial_screen_payload

    return legacy_financial_screen_payload(
        period=period,
        filters=filters,
        report_type=report_type,
        include_sw_industry=include_sw_industry,
        exclude_st=exclude_st,
        exclude_negative_base=exclude_negative_base,
        sort_by=sort_by,
        top_n=top_n,
        include_express=include_express,
        include_forecast=include_forecast,
    )


# ---------------------------------------------------------------------------
# Backtest sweep (strategy compare grid)
# ---------------------------------------------------------------------------


def _require_mcp_user_token(x_user_token: Optional[str]) -> Dict[str, Any]:
    if not x_user_token:
        raise HTTPException(status_code=401, detail="X-User-Token is required for backtest sweep tools")
    user = get_user_from_header_token(x_user_token=x_user_token)
    if user.get("id") == "hermes-system":
        raise HTTPException(
            status_code=401,
            detail="Valid user token required; hermes-system fallback is not allowed for backtest tools",
        )
    return user


def _build_backtest_strategies_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    collections = get_db_collections()
    meta_col = collections.get("strategy_metadata")
    template_col = collections.get("strategy_template")
    strategies_out: List[Dict[str, Any]] = []
    user_service_level = user.get("service_level", "free")

    if meta_col is not None:
        for s in meta_col.find({}, {"_id": 0}):
            key = s.get("key")
            if not key:
                continue
            item = {
                "key": key,
                "name": s.get("name", key),
                "min_service_level": s.get("min_service_level", "free"),
            }
            user_level_index = ["free", "basic", "vip", "premium"].index(user_service_level)
            required_level_index = ["free", "basic", "vip", "premium"].index(item["min_service_level"])
            item["can_use"] = user_level_index >= required_level_index
            presets: List[Dict[str, Any]] = []
            if template_col is not None:
                for tpl in template_col.find({"strategy": key}, {"_id": 0}):
                    presets.append(
                        {
                            "preset": tpl.get("preset"),
                            "name": tpl.get("name") or tpl.get("preset"),
                            "description": tpl.get("description"),
                            "is_default": tpl.get("is_default", False),
                            "params": tpl.get("params") or {},
                            "params_with_desc": tpl.get("params_with_desc") or {},
                        }
                    )
            item["presets"] = presets
            strategies_out.append(item)

    return {"success": True, "strategies": strategies_out}


class BacktestSweepRequest(BaseModel):
    name: Optional[str] = None
    symbols: List[str] = Field(default_factory=list)
    asset_type: str = "stock"
    universe_type: str = "manual"
    universe_value: Optional[str] = None
    strategy_key: str
    preset: Optional[str] = None
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    start_date: str
    end_date: str
    initial_cash: float = 1000000.0
    limit_symbols: int = 0
    experiment_type: str = "compare"
    combos: List[BacktestCombo] = Field(default_factory=list)
    regime_cash: bool = False
    regime_index: Optional[str] = None
    regime_rule: Optional[str] = None


@router.get("/backtest/strategies", operation_id="backtest_strategies", tags=[MCP_READ_TAG])
async def hermes_backtest_strategies(
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """List strategies with presets and parameter metadata for sweep planning."""
    user = _require_mcp_user_token(x_user_token)
    return _build_backtest_strategies_payload(user)


@router.post("/backtest/sweep", operation_id="submit_backtest_sweep", tags=[MCP_ACTION_TAG])
async def hermes_submit_backtest_sweep(
    body: BacktestSweepRequest,
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Submit a compare-grid backtest batch and return batch_id immediately."""
    collections = get_db_collections()
    user = _require_mcp_user_token(x_user_token)
    payload = BacktestBatchCreate(**body.model_dump())
    result = create_batch(collections, user, payload)
    return {
        "success": True,
        "batch_id": result.get("batch_id"),
        "created_tasks": result.get("created_tasks"),
        "task_ids": result.get("task_ids"),
        "attributed_to": user.get("username"),
    }


@router.get("/backtest/sweep/{batch_id}", operation_id="backtest_sweep_status", tags=[MCP_ACTION_TAG])
async def hermes_backtest_sweep_status(
    batch_id: str,
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Poll compare batch progress and return all completed metric rows."""
    collections = get_db_collections()
    user = _require_mcp_user_token(x_user_token)
    summary = refresh_batch_summary(collections, batch_id, user["id"])
    rows = list_all_batch_result_rows(collections, batch_id, user["id"])
    return {
        "success": True,
        "batch_id": batch_id,
        "summary": summary,
        "total": len(rows),
        "rows": rows,
        "attributed_to": user.get("username"),
    }


@router.post("/backtest/sweep-and-wait", operation_id="backtest_sweep_wait", tags=[MCP_ACTION_TAG])
async def hermes_backtest_sweep_wait(
    body: BacktestSweepRequest,
    timeout: int = Query(180, ge=30, le=300, description="Max wait seconds; default 180"),
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    Submit a compare-grid batch and poll until completion or timeout.
    Large grids should use submit_backtest_sweep + backtest_sweep_status instead.
    """
    collections = get_db_collections()
    user = _require_mcp_user_token(x_user_token)
    payload = BacktestBatchCreate(**body.model_dump())
    result = create_batch(collections, user, payload)
    batch_id = result.get("batch_id")

    elapsed = 0
    poll_interval = 10
    summary: Dict[str, Any] = result.get("summary") or {}
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        summary = refresh_batch_summary(collections, batch_id, user["id"])
        if int(summary.get("running") or 0) == 0:
            break

    rows = list_all_batch_result_rows(collections, batch_id, user["id"])
    summary = refresh_batch_summary(collections, batch_id, user["id"])
    timed_out = int(summary.get("running") or 0) > 0
    return {
        "success": not timed_out,
        "timed_out": timed_out,
        "batch_id": batch_id,
        "summary": summary,
        "total": len(rows),
        "rows": rows,
        "attributed_to": user.get("username"),
        "message": (
            "Batch still running; poll backtest_sweep_status with batch_id"
            if timed_out
            else "Batch completed"
        ),
    }


# ---------------------------------------------------------------------------
# 组合研究（portfolio research）结果读取与按轴归因
#
# 数据在 quant_trading 库（get_trading_db_collections），不是主 finance 库。
# 结果 doc 的 rows 只存前 200 行、且已按 risk_adjusted_score 降序 —— 直接用会
# 把按轴均值系统性高估。一律走 _load_full_result_rows 从 artifact 回填全量。
# ---------------------------------------------------------------------------

# 归因指标：指数超额（不受平均仓位影响）+ 最大回撤（风控轴要看它）。
# 不用 cumulative_return：它与 index_excess_cumulative_return 只差整个 job 的基准
# 收益常数（实测 delta 逐对完全相同），一起返回是纯冗余。
_AXIS_EFFECT_METRICS: Sequence[str] = (
    "index_excess_cumulative_return",
    "max_drawdown",
)

# 参与"控制维度"推导的候选列。sweep_view.sweep_axes 未必列全真正被扫描的维度
# （实测 variant 有 6 档在变却没进 sweep_axes），只按 sweep_axes 控制会把
# variant 的差异错误归因到别的轴上。所以控制维度从真实数据推导。
_CONTROL_CANDIDATE_KEYS: Sequence[str] = (
    "score_variant",
    "variant",
    "construction_mode",
    "selection_mode",
    "top_n",
    "rebalance_interval_days",
    "max_industry_weight",
    "trailing_stop_pct",
    "regime_mode",
)

# 这些列不参与控制：combo_key 逐行唯一，控制它会让每组只剩一行、配对数恒为 0。
_CONTROL_EXCLUDED_KEYS = frozenset({"combo_key"})

# 精简 row 时保留的字段（指标与 sweep_view.METRIC_KEYS 一致）。
_ROW_METRIC_KEYS: Sequence[str] = (
    "cumulative_return",
    "annualized_return",
    "index_excess_cumulative_return",
    "sharpe",
    "max_drawdown",
    "average_turnover",
    "average_exposure",
    "avg_active_share",
    "risk_adjusted_score",
)

_ROW_IDENTITY_KEYS: Sequence[str] = (
    "score_variant",
    "variant",
    "top_n",
    "rebalance_interval_days",
    "max_industry_weight",
    "combo_key",
)


def _axis_token(value: Any) -> str:
    """分组用的稳定取值标识。

    trailing stop 的 off 在参数侧写作 0.0、个别行里可能是 None，两者必须同组；
    数值轴同时要避免 10 与 10.0 被分到两组。
    """
    if value is None:
        return "0"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if number == 0:
            return "0"
        return str(int(number)) if number.is_integer() else str(number)
    return str(value)


def _as_metric(row: Dict[str, Any], key: str) -> Optional[float]:
    try:
        value = row.get(key)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _varying_dimensions(
    rows: Sequence[Dict[str, Any]],
    sweep_axes: Sequence[Dict[str, Any]],
) -> List[str]:
    """真实数据里确实在变的维度，用作配对的控制键。

    ``sweep_view.sweep_axes`` 未必列全真正被扫描的维度：实测某次 324 组网格里
    ``variant`` 有 6 档在变却没进 sweep_axes。只按 sweep_axes 控制，variant 的差异
    会被错误归因到别的轴上（实测把 max_industry_weight 报出 31 个"有效配对"，
    实际它与 variant 共线、残差里根本没有配对数）。所以控制维度从数据推导。
    """
    ordered: List[str] = []
    for axis in sweep_axes:
        key = axis.get("key")
        if key and key not in ordered:
            ordered.append(key)
    for key in _CONTROL_CANDIDATE_KEYS:
        if key not in ordered:
            ordered.append(key)

    varying: List[str] = []
    for key in ordered:
        if key in _CONTROL_EXCLUDED_KEYS:
            continue
        if len({_axis_token(row.get(key)) for row in rows if key in row}) > 1:
            varying.append(key)
    return varying


def _effective_axes(
    rows: Sequence[Dict[str, Any]],
    sweep_axes: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """给 agent 看的轴清单：sweep_view 声明的轴 + 数据里额外变化的维度。

    补漏很重要：漏掉的维度会被 agent 误当成固定参数。标 ``source: derived``
    的即 sweep_view 未声明、由数据发现的轴。
    """
    declared = {axis.get("key") for axis in sweep_axes if axis.get("key")}
    axes: List[Dict[str, Any]] = [dict(axis) for axis in sweep_axes if axis.get("key")]
    for key in _varying_dimensions(rows, sweep_axes):
        if key in declared:
            continue
        seen: Dict[str, Any] = {}
        for row in rows:
            if key in row:
                seen.setdefault(_axis_token(row.get(key)), row.get(key))
        axes.append(
            {
                "key": key,
                "label": key,
                "values": [seen[token] for token in sorted(seen)],
                "source": "derived",
            }
        )
    return axes


def _pairwise_axis_effects(
    rows: Sequence[Dict[str, Any]],
    sweep_axes: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """固定其余**全部变化维度**，比较同一组合内单个轴两个取值的指标差。

    配对比边际均值可信：边际均值会把混杂效应误读成轴效应（某轴的一档恰好都落在
    更有利的其他轴配置上）。控制了其余维度，才敢下「没有一对反过来」这类强结论。

    delta 一律为 a − b。``a_worse_count`` 是 a 更差的配对数，对两个指标同义：
    收益上 delta<0 表示 a 收益更低；回撤上 delta<0 表示 a 的回撤更负。为 0 表示
    a 在所有配对中都不劣于 b，等于 n_pairs 则表示 b 完胜 a。

    ``n_pairs == 0`` 的轴会连 ``unpaired_reason`` 一起返回 —— 常见于该轴与别的
    维度共线（控制之后没有残差变异），此时该轴不可单独归因，而不是"无影响"。
    """
    control_keys = _varying_dimensions(rows, sweep_axes)
    effects: List[Dict[str, Any]] = []
    for axis in _effective_axes(rows, sweep_axes):
        axis_key = axis.get("key")
        axis_values = list(axis.get("values") or [])
        if not axis_key or len(axis_values) < 2:
            continue
        other_keys = [key for key in control_keys if key != axis_key]

        grouped: Dict[tuple, List[Dict[str, Any]]] = {}
        for row in rows:
            if axis_key not in row:
                continue
            grouped.setdefault(tuple(_axis_token(row.get(key)) for key in other_keys), []).append(row)

        # 组内同一轴取值出现多行 => 还有未受控维度在变，该组不可比，整组丢弃。
        resolved: List[Dict[str, Dict[str, Any]]] = []
        ambiguous_groups = 0
        for group_rows in grouped.values():
            by_token: Dict[str, Dict[str, Any]] = {}
            duplicated = False
            for row in group_rows:
                token = _axis_token(row.get(axis_key))
                if token in by_token:
                    duplicated = True
                by_token[token] = row
            if duplicated:
                ambiguous_groups += 1
                continue
            resolved.append(by_token)

        pairs: List[Dict[str, Any]] = []
        for value_a, value_b in itertools.combinations(axis_values, 2):
            token_a, token_b = _axis_token(value_a), _axis_token(value_b)
            deltas: Dict[str, List[float]] = {metric: [] for metric in _AXIS_EFFECT_METRICS}
            for by_token in resolved:
                row_a, row_b = by_token.get(token_a), by_token.get(token_b)
                if row_a is None or row_b is None:
                    continue
                for metric in _AXIS_EFFECT_METRICS:
                    metric_a, metric_b = _as_metric(row_a, metric), _as_metric(row_b, metric)
                    if metric_a is not None and metric_b is not None:
                        deltas[metric].append(metric_a - metric_b)
            entry: Dict[str, Any] = {
                "a": value_a,
                "b": value_b,
                "n_pairs": len(deltas[_AXIS_EFFECT_METRICS[0]]),
            }
            for metric in _AXIS_EFFECT_METRICS:
                values = deltas[metric]
                if not values:
                    entry[metric] = None
                    continue
                worse = sum(1 for value in values if value < 0)
                if all(value == 0 for value in values):
                    # 逐对完全相同：没有差异，不能说成 a 完胜（a_worse_count 也是 0）。
                    verdict = "no_difference"
                elif worse == 0:
                    verdict = "a_dominates"
                elif worse == len(values):
                    verdict = "b_dominates"
                else:
                    verdict = "mixed"
                entry[metric] = {
                    "median_delta": round(_median(values), 4),
                    "min_delta": round(min(values), 4),
                    "max_delta": round(max(values), 4),
                    "a_worse_count": worse,
                    "verdict": verdict,
                }
            if entry["n_pairs"]:
                pairs.append(entry)

        entry_axis: Dict[str, Any] = {
            "axis": axis_key,
            "label": axis.get("label") or axis_key,
            "pairs": pairs,
        }
        if not pairs:
            entry_axis["unpaired_reason"] = (
                "ambiguous_groups" if ambiguous_groups else "collinear_with_other_axes"
            )
        effects.append(entry_axis)
    return effects


def _axis_marginals(
    rows: Sequence[Dict[str, Any]],
    sweep_axes: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """每个轴取值的组内聚合并附该档行数 —— 注意这是**有偏**的边际视角。

    为什么行数必须一起返回：结果 doc 只存按 risk_adjusted_score 降序的前 200 行，
    真网格可能大得多（实测 324 组只留 200）。各档被保留的比例极不均（实测
    regime off 67 / bull 133，真值各半），边际均值因此系统性偏向分数高的配置。
    看均值之前先看 observations 均衡不均衡；结论强度一律以 axis_effects 的配对为准。

    另注：``stability`` 字段不是边际聚合。它按**全部轴** groupby，每组只剩一个
    combo（observations 恒为 1），只是一张换排序方式的明细表。
    """
    marginals: List[Dict[str, Any]] = []
    for axis in _effective_axes(rows, sweep_axes):
        axis_key = axis.get("key")
        if not axis_key:
            continue
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            if axis_key in row:
                buckets.setdefault(_axis_token(row.get(axis_key)), []).append(row)
        if len(buckets) < 2:
            continue
        entries: List[Dict[str, Any]] = []
        for token in sorted(buckets):
            group = buckets[token]
            excess = [v for v in (_as_metric(r, "index_excess_cumulative_return") for r in group) if v is not None]
            returns = [v for v in (_as_metric(r, "cumulative_return") for r in group) if v is not None]
            drawdown = [v for v in (_as_metric(r, "max_drawdown") for r in group) if v is not None]
            exposure = [v for v in (_as_metric(r, "average_exposure") for r in group) if v is not None]
            entries.append(
                {
                    "value": group[0].get(axis_key),
                    "observations": len(group),
                    "avg_index_excess_return": round(sum(excess) / len(excess), 4) if excess else None,
                    "min_index_excess_return": round(min(excess), 4) if excess else None,
                    "avg_cumulative_return": round(sum(returns) / len(returns), 4) if returns else None,
                    "worst_max_drawdown": round(min(drawdown), 4) if drawdown else None,
                    "avg_average_exposure": round(sum(exposure) / len(exposure), 4) if exposure else None,
                }
            )
        entries.sort(
            key=lambda entry: (
                entry["avg_index_excess_return"] is None,
                -(entry["avg_index_excess_return"] or 0.0),
            )
        )
        marginals.append(
            {"axis": axis_key, "label": axis.get("label") or axis_key, "values": entries}
        )
    return marginals


def _compact_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """只保留指标与身份字段，避免把整行 30+ 字段灌进 LLM 上下文。"""
    if not row:
        return None
    keys = (*_ROW_IDENTITY_KEYS, *_ROW_METRIC_KEYS)
    return {key: row.get(key) for key in keys if row.get(key) is not None}


_PORTFOLIO_RESEARCH_JOB_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "name": 1,
    "status": 1,
    "universe_index": 1,
    "start_date": 1,
    "end_date": 1,
    "result_id": 1,
    "created_at": 1,
    "updated_at": 1,
    "error_message": 1,
}


@router.get("/portfolio-research/jobs", operation_id="portfolio_research_jobs", tags=[MCP_READ_TAG])
async def hermes_portfolio_research_jobs(
    limit: int = Query(10, ge=1, le=50, description="返回条数"),
    status: Optional[str] = Query(None, description="按状态过滤：pending / running / completed / failed"),
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """列出组合研究（参数扫描）任务，用于定位要复盘的 job_id。

    组合研究在 stock_scores 之上回答：因子有没有预测力、Top-N/权重/成本是否合适、
    风控叠加能否改善回撤。返回按创建时间倒序。
    """
    collections = get_trading_db_collections()
    user = _require_mcp_user_token(x_user_token)
    query: Dict[str, Any] = {"user_id": user["id"]}
    if status:
        query["status"] = status
    jobs = list(
        collections["portfolio_research_jobs"]
        .find(query, _PORTFOLIO_RESEARCH_JOB_PROJECTION)
        .sort("created_at", -1)
        .limit(limit)
    )
    return {
        "success": True,
        "total": len(jobs),
        "jobs": jobs,
        "attributed_to": user.get("username"),
    }


@router.get("/portfolio-research/result/{job_id}", operation_id="portfolio_research_result", tags=[MCP_READ_TAG])
async def hermes_portfolio_research_result(
    job_id: str,
    include_rows: bool = Query(False, description="是否附带全量 combo 行（大网格很占上下文，默认关）"),
    x_user_token: str = Header(..., alias="X-User-Token"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """读取组合研究结果，并附带按轴归因，用于分析多组参数组合的收益差异。

    归因证据（可信度依次递减）：
      - axis_effects：固定其余**全部变化维度**后的配对比较，结论强度最高
      - axes / axis_marginals：轴清单与边际聚合。axes 除 sweep_view 声明的轴外，
        还会补上数据里额外变化的维度（source=derived，例如 variant）；marginals
        附每档 observations —— 行数不均是截断偏差的直接体现，看均值前先看它
      - global_best_row：全局最优行，**带选择偏差**

    注意 ``stability`` 不是边际聚合：它按全部轴 groupby，observations 恒为 1，
    只是一张按超额排序的明细表。真正的边际聚合看 axis_marginals。

    解读纪律：网格越大 max 越乐观，不要把 global_best_row 直接当推荐；累计收益
    不可跨平均仓位直接比较（regime 空仓组约 49% 暴露 vs 满仓 100%），跨组比较
    请用 index_excess_cumulative_return。coverage.truncated 为真时说明只拿到
    前 200 行、边际均值有偏，结论以配对为准；n_pairs 为 0 的轴是共线不可归因，
    不等于「无影响」。
    """
    collections = get_trading_db_collections()
    user = _require_mcp_user_token(x_user_token)
    job = collections["portfolio_research_jobs"].find_one(
        {"job_id": job_id, "user_id": user["id"]},
        _PORTFOLIO_RESEARCH_JOB_PROJECTION,
    )
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = collections["portfolio_research_results"].find_one(
        {"job_id": job_id, "user_id": user["id"]},
        {"_id": 0},
    )
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")

    rows = _load_full_result_rows(result)
    preview_count = len(result.get("rows") or [])
    sweep_view = result.get("sweep_view") or {}
    sweep_axes = list(sweep_view.get("sweep_axes") or [])

    row_count_total = result.get("row_count_total") or sweep_view.get("row_count") or len(rows)
    payload: Dict[str, Any] = {
        "success": True,
        "job": job,
        "result": {
            "params": result.get("params"),
            "sweep_axes": sweep_axes,
            "axes": _effective_axes(rows, sweep_axes),
            "axis_effects": _pairwise_axis_effects(rows, sweep_axes),
            "axis_marginals": _axis_marginals(rows, sweep_axes),
            "stability": result.get("stability") or [],
            "global_best_row": _compact_row(result.get("best_row") or sweep_view.get("global_best_row")),
            "metric_keys": sweep_view.get("metric_keys"),
            "coverage": {
                "row_count": len(rows),
                "row_count_total": row_count_total,
                "truncated": len(rows) < row_count_total,
                "rows_source": "artifact_full" if len(rows) > preview_count else "mongo_preview",
            },
            "row_count": len(rows),
            "row_count_total": row_count_total,
            "rows_source": "artifact_full" if len(rows) > preview_count else "mongo_preview",
            "data_watermark": result.get("data_watermark"),
        },
        "attributed_to": user.get("username"),
    }
    if include_rows:
        payload["rows"] = [_compact_row(row) for row in rows]
    return payload
