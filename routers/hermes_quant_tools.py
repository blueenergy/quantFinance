"""
Hermes 容器内可调用的 quant-api 工具端点（共享密钥，无用户 JWT）。

搭配 docker-compose 中 QUANT_API_URL + HERMES_QUANT_INTERNAL_KEY。
MCP 暴露面按 route tag 拆分为 wukong-quant-read 与 wukong-quant-actions。

端点一览：
  GET  /hermes-tools/ladder/daily          — 连板天梯
  GET  /hermes-tools/ladder/narrative      — 涨停分析（LLM 叙事，盘后可用）
  GET  /hermes-tools/market/summary        — AI 大盘分析（市场总结）
  GET  /hermes-tools/market/global         — 全球市场简报
  GET  /hermes-tools/market/hot-stock       — 热股分析（THS/DC 最新）
  GET  /hermes-tools/macro/analysis          — 宏观 AI 分析（中国/美国）
  GET  /hermes-tools/sector/latest           — 最新板块/概念 AI 分析
  GET  /hermes-tools/earnings/signals        — 财报猎手异动信号
  GET  /hermes-tools/stock/ranking            — 股票评分排行榜 Top N（按指数）
  GET  /hermes-tools/stock/score-detail       — 单股六维评分详情
  GET  /hermes-tools/stock/valuation-metrics  — 单股科技估值结构化指标
  GET  /hermes-tools/stock/industry-comparison — 单股行业横向估值/财务分位
  POST /hermes-tools/analysis/deep         — 提交深度分析任务
  POST /hermes-tools/analysis/deep-and-wait — 提交并等待完成（推荐）
  GET  /hermes-tools/analysis/task/{id}    — 轮询任务状态
  GET  /hermes-tools/analysis/history      — 查某股历史分析
  GET  /hermes-tools/shenwan/pe-pb           — 申万行业指数月度 PE/PB 历史分位
  GET  /hermes-tools/stock/screen            — 智能选股：多条件财务筛选 + 申万行业
  GET  /hermes-tools/stock/foreign-top10-floatholders — 外资投行进入前十大流通股东筛选

  — 个人化端点（需先 user_login 获取 token 并设置 X-User-Token header）—
  POST /hermes-tools/user/login            — 用户登录，返回 JWT token
  GET  /hermes-tools/user/profile          — 当前用户账户信息
  GET  /hermes-tools/user/watchlist        — 自选股列表 + 最新量化评分排行
  POST /hermes-tools/user/watchlist/add    — 添加股票到自选股
  DEL  /hermes-tools/user/watchlist/remove/{symbol} — 从自选股删除
  GET  /hermes-tools/user/watchlist/history — 自选股历史深度分析记录
"""

import asyncio
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from auth import get_user_from_header_token, require_hermes_quant_internal_key
from db import data_db, db, get_analyzer_db, get_db_collections
from models.analysis_task import AnalysisMode
from routers.market import fetch_market_analysis_result
from services.analysis_history import build_user_analysis_history_payload
from services.analysis_tasks import build_task_doc
from services.ladder_daily import build_daily_ladder_payload

MCP_READ_TAG = "Hermes quant read tools"
MCP_ACTION_TAG = "Hermes quant action tools"

router = APIRouter(prefix="/hermes-tools")
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


# ---------------------------------------------------------------------------
# 股票评分排行榜
# ---------------------------------------------------------------------------

_RANKING_INDEX_CODES = ("hs300", "a500", "csi500", "csi1000", "star50")


@router.get("/stock/ranking", operation_id="stock_ranking", tags=[MCP_READ_TAG])
async def stock_ranking(
    index_code: str = Query("hs300", description="指数: hs300/a500/csi500/csi1000/star50"),
    top: int = Query(20, ge=1, le=100, description="返回前 N 名，默认 20"),
    strategy: str = Query("balanced", description="策略: balanced/aggressive/conservative"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取 A 股量化评分排行榜 Top N（按指数成分筛选）。

    - index_code: hs300（沪深300）、a500、csi500（中证500）、csi1000、star50（科创50）
    - strategy: balanced（均衡，默认）、aggressive（进取）、conservative（保守）
    - 返回每只股票的综合评分及六维子评分（周期/价值/基本面/成长/技术/资金流）
    """
    ic = index_code.strip().lower()
    if ic not in _RANKING_INDEX_CODES:
        return {"success": False, "error": f"不支持的 index_code: {ic}，可选: {', '.join(_RANKING_INDEX_CODES)}"}

    from utils.score_utils import extract_strategy_score, safe_round
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
    fields: tuple[str, ...] = ("symbol", "ts_code"),
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
) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol 不能为空"}

    data = data_collections or data_db
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
        "notes": notes,
        "percentile_note": (
            "peer_percentile 为目标值在同业横截面中的分位(0-100，含自身)；"
            "估值倍数/市值仅统计正值，ROE/利润率/收入增速含负值"
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

    data = data_collections or data_db
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
    if not segments.get("by_type"):
        missing.append("financial_mainbz")

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

    return {
        "success": True,
        "symbol": normalized_symbol,
        "resolved_symbol": (
            latest_daily.get("ts_code")
            or latest_daily.get("symbol")
            or latest_indicator.get("ts_code")
            or latest_indicator.get("symbol")
            or latest_income.get("ts_code")
            or latest_income.get("symbol")
            or info.get("ts_code")
            or info.get("symbol")
            or normalized_symbol
        ),
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
        },
        "series": series,
        "segments": segments,
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
    return _stock_valuation_metrics_payload(symbol)


@router.get("/stock/industry-comparison", operation_id="stock_industry_comparison", tags=[MCP_READ_TAG])
async def stock_industry_comparison(
    symbol: str = Query(..., description="股票代码，如 688111.SH 或 688111"),
    level: str = Query("sw_l2", description="同业层级：sw_l2、sw_l1 或 industry；缺失时自动降级"),
    min_peer_count: int = Query(
        5, ge=2, le=100, description="计算中位数/分位所需的最少同业样本数，低于该值返回 null"
    ),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单股相对同业的估值与财务指标分位。

    使用 stock_info 的申万/行业字段确定同业集合，并在目标股最新交易日/财报期
    上计算 PE/PB/PS、市值、ROE、利润率、收入增速的行业横截面分位。
    """
    return _stock_industry_comparison_payload(symbol, level=level, min_peer_count=min_peer_count)


# ---------------------------------------------------------------------------
# 单股量化评分详情
# ---------------------------------------------------------------------------

@router.get("/stock/score-detail", operation_id="stock_score_detail", tags=[MCP_READ_TAG])
async def stock_score_detail(
    symbol: str = Query(..., description="股票代码，如 600519"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """
    获取单只股票最新量化评分的六维详细拆解。

    返回字段：综合评分（balanced/aggressive/conservative 三策略）及
    cycle/value/fundamental/growth/technical/money_flow 六维子评分。
    """
    from utils.score_utils import safe_round
    db = get_db_collections()
    doc = db["stock_scores"].find_one({"symbol": symbol}, sort=[("score_date", -1)])
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
    from utils.score_utils import safe_round
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

# 允许过滤的 financial_income 字段白名单（防止 LLM 幻觉字段名）
_SCREEN_ALLOWED_FIELDS = {
    "operate_profit",     # 营业利润
    "revenue",            # 主营业务收入（默认营收）
    "total_revenue",      # 营业总收入
    "n_income",           # 净利润
    "n_income_attr_p",    # 归母净利润
    "total_profit",       # 利润总额
    "oper_cost",          # 营业成本
}

_SCREEN_ALLOWED_OPS = {"gt", "gte", "lt", "lte"}
_SCREEN_ALLOWED_METRICS = {"yoy", "abs"}


class ScreenFilter(BaseModel):
    field: str
    metric_type: str  # "yoy" | "abs"
    op: str           # "gt" | "gte" | "lt" | "lte"
    value: float
    base_period: Optional[str] = None  # abs 用：缺省取目标 period


def _screen_prev_year_period(period: str) -> str:
    """20260331 -> 20250331"""
    return str(int(period[:4]) - 1) + period[4:]


def _screen_compare(left: float, op: str, right: float) -> bool:
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    return False


@router.get("/stock/screen", operation_id="stock_screen", tags=[MCP_READ_TAG])
async def hermes_stock_screen(
    period: str = Query(..., description="目标报告期 YYYYMMDD，如 20260331；或 'latest' 自动取最近期"),
    filters: str = Query(..., description="ScreenFilter 列表的 JSON 字符串"),
    report_type: int = Query(1, description="1=合并(累计，行业默认)，2=单季合并"),
    include_sw_industry: bool = Query(True, description="是否附申万 L1/L2/L3 行业"),
    exclude_st: bool = Query(True, description="排除股票名含 ST/*ST 的标的"),
    exclude_negative_base: bool = Query(True, description="YoY 计算时若同期值<=0则跳过该股，避免扭亏假高增"),
    sort_by: str = Query("auto", description="排序字段；auto 取第一个 yoy filter；可显式指定 operate_profit_yoy 等"),
    top_n: int = Query(100, ge=1, le=500, description="最多返回条数"),
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
    """
    import json as _json

    # --- Step 1: 解析 & 校验 filters ---
    try:
        raw_filters = _json.loads(filters)
        if not isinstance(raw_filters, list) or not raw_filters:
            return {"success": False, "error": "filters 必须是非空 JSON 数组"}
        parsed: list[ScreenFilter] = [ScreenFilter(**f) for f in raw_filters]
    except Exception as e:
        return {"success": False, "error": f"filters 解析失败: {e}"}

    for f in parsed:
        if f.field not in _SCREEN_ALLOWED_FIELDS:
            return {"success": False,
                    "error": f"不支持的 field: {f.field}",
                    "allowed_fields": sorted(_SCREEN_ALLOWED_FIELDS)}
        if f.metric_type not in _SCREEN_ALLOWED_METRICS:
            return {"success": False, "error": f"不支持的 metric_type: {f.metric_type}"}
        if f.op not in _SCREEN_ALLOWED_OPS:
            return {"success": False, "error": f"不支持的 op: {f.op}"}

    fin = data_db["financial_income"]

    # --- Step 2: 处理 period=latest ---
    target_period = period.strip()
    if target_period.lower() == "latest":
        latest = fin.find_one({"report_type": report_type}, sort=[("end_date", -1)])
        if not latest:
            return {"success": False, "error": "financial_income 无数据"}
        target_period = latest["end_date"]

    # --- Step 3: 推算所需 end_date 集合 ---
    needed_periods = {target_period}
    has_yoy = any(f.metric_type == "yoy" for f in parsed)
    if has_yoy:
        needed_periods.add(_screen_prev_year_period(target_period))
    for f in parsed:
        if f.metric_type == "abs" and f.base_period:
            needed_periods.add(f.base_period)

    needed_fields = {f.field for f in parsed}
    if has_yoy:
        # YoY 也需要这些字段
        pass  # already in needed_fields

    proj = {"_id": 0, "ts_code": 1, "end_date": 1}
    for fld in needed_fields:
        proj[fld] = 1

    # --- Step 4: 一次批量查 financial_income，组织成 (ts_code, end_date) -> doc ---
    cursor = fin.find(
        {"end_date": {"$in": list(needed_periods)}, "report_type": report_type},
        proj,
    )
    by_code: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for d in cursor:
        ts = d.get("ts_code")
        ed = d.get("end_date")
        if not ts or not ed:
            continue
        by_code.setdefault(ts, {})[ed] = d

    # 仅保留在目标期有数据的股票
    candidates_in_target = [ts for ts, m in by_code.items() if target_period in m]
    total_screened = len(candidates_in_target)

    prev_period = _screen_prev_year_period(target_period) if has_yoy else None

    matched: list[Dict[str, Any]] = []
    for ts in candidates_in_target:
        m = by_code[ts]
        cur = m[target_period]
        prev = m.get(prev_period) if prev_period else None

        # 计算/取值并比较每个 filter
        per_stock_metrics: Dict[str, Any] = {}
        ok = True
        for f in parsed:
            cur_v = cur.get(f.field)
            if cur_v is None:
                ok = False
                break
            per_stock_metrics[f.field] = cur_v

            if f.metric_type == "yoy":
                if not prev:
                    ok = False
                    break
                prev_v = prev.get(f.field)
                if prev_v is None:
                    ok = False
                    break
                if exclude_negative_base and prev_v <= 0:
                    ok = False
                    break
                if prev_v == 0:
                    ok = False
                    break
                yoy = (cur_v - prev_v) / abs(prev_v)
                per_stock_metrics[f"{f.field}_yoy"] = yoy
                if not _screen_compare(yoy, f.op, f.value):
                    ok = False
                    break
            else:  # abs
                base_p = f.base_period or target_period
                if base_p == target_period:
                    base_v = cur_v
                else:
                    base_doc = m.get(base_p)
                    if not base_doc:
                        ok = False
                        break
                    base_v = base_doc.get(f.field)
                    if base_v is None:
                        ok = False
                        break
                    per_stock_metrics[f"{f.field}@{base_p}"] = base_v
                if not _screen_compare(base_v, f.op, f.value):
                    ok = False
                    break

        if ok:
            matched.append({"ts_code": ts, **per_stock_metrics})

    matched_codes = [r["ts_code"] for r in matched]

    # --- Step 5: 附 stock name + ST 过滤 ---
    stock_info = data_db["stock_info"]
    info_map = {
        d["ts_code"]: d
        for d in stock_info.find(
            {"ts_code": {"$in": matched_codes}},
            {"_id": 0, "ts_code": 1, "name": 1},
        )
    }
    enriched: list[Dict[str, Any]] = []
    for r in matched:
        name = (info_map.get(r["ts_code"], {}) or {}).get("name", "")
        if exclude_st and name and ("ST" in name.upper()):
            continue
        r["name"] = name
        enriched.append(r)

    # --- Step 6: 附申万行业 ---
    if include_sw_industry and enriched:
        codes = [r["ts_code"] for r in enriched]
        sw_map = {
            d["con_code"]: d
            for d in data_db["sw_index_member"].find(
                {"con_code": {"$in": codes}, "is_new": "Y", "src": "SW2021"},
                {"_id": 0, "con_code": 1, "l1_name": 1, "l2_name": 1, "l3_name": 1,
                 "l1_code": 1, "l2_code": 1, "l3_code": 1},
            )
        }
        for r in enriched:
            sw = sw_map.get(r["ts_code"], {}) or {}
            r["sw_l1"] = sw.get("l1_name", "")
            r["sw_l2"] = sw.get("l2_name", "")
            r["sw_l3"] = sw.get("l3_name", "")

    # --- Step 7: 排序 + 截断 ---
    if sort_by == "auto":
        first_yoy = next((f for f in parsed if f.metric_type == "yoy"), None)
        sort_field = f"{first_yoy.field}_yoy" if first_yoy else parsed[0].field
    else:
        sort_field = sort_by
    enriched.sort(key=lambda d: (d.get(sort_field) is None, -(d.get(sort_field) or 0)))
    results = enriched[:top_n]

    # --- Step 8: industries_distribution summary ---
    industries_dist: list[Dict[str, Any]] = []
    if include_sw_industry:
        from collections import Counter
        cnt = Counter(r.get("sw_l1", "") or "未知" for r in results)
        industries_dist = [{"sw_l1": k, "count": v} for k, v in cnt.most_common()]

    return {
        "success": True,
        "period": target_period,
        "report_type": report_type,
        "total_screened": total_screened,
        "matched": len(enriched),
        "returned": len(results),
        "filters_applied": [f.model_dump() for f in parsed],
        "summary": {"industries_distribution": industries_dist},
        "results": results,
    }
