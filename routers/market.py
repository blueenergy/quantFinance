# 新增：获取market spectrum的API接口
from collections import defaultdict
from typing import Optional, Dict, Any, List, Mapping
from fastapi import APIRouter, Query, Depends, HTTPException
from models.all_models import MarketAnalysisRequest
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db, get_data_db, realtime_db
from market_analysis_cache import market_analysis_cache
from datetime import date, datetime
import os
import time
from services.tushare_cached import get_latest_trade_date
from external_data import external_market_data
from utils.stock_utils import serialize_doc
from bson import ObjectId
from services.analysis_tasks import build_task_doc
import logging

router = APIRouter()
log = logging.getLogger(__name__)

_HOME_SUMMARIES_CACHE_TTL_SECONDS = 60
_MARKET_ANALYSIS_SUMMARY_CACHE_TTL_SECONDS = 60
_SECTOR_SPECTRUM_CACHE_TTL_SECONDS = 300
_home_summaries_cache: Dict[str, Any] = {"key": None, "expires_at": 0.0, "payload": None}
_market_analysis_summary_cache: Dict[str, Any] = {"key": None, "expires_at": 0.0, "payload": None}
_sector_spectrum_cache: Dict[str, Dict[str, Any]] = {}
_sector_members_cache: Dict[str, Dict[str, Any]] = {}


def _num_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_sector_spectrum_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    cached = _sector_spectrum_cache.get(cache_key) or {}
    if cached.get("payload") is not None and float(cached.get("expires_at") or 0) > now:
        return cached["payload"]
    return None


def _set_sector_spectrum_cache(cache_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _sector_spectrum_cache[cache_key] = {
        "payload": payload,
        "expires_at": time.time() + _SECTOR_SPECTRUM_CACHE_TTL_SECONDS,
    }
    return payload


def _sector_strength_distribution(doc: Dict[str, Any]) -> Dict[str, int]:
    return {
        "gt_7": 0,
        "p5_7": 0,
        "p2_5": 0,
        "p0_2": int(doc.get("positive_count") or 0),
        "flat": int(doc.get("flat_count") or 0),
        "m2_0": int(doc.get("negative_count") or 0),
        "m5_2": 0,
        "m7_5": 0,
        "lt_m7": 0,
    }


def _compact_summary_text(value: Any, fallback: str, max_len: int = 48) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return f"{text[:max_len].strip()}..." if len(text) > max_len else text


def _summary_signals(values: Any, fallback: List[str]) -> List[str]:
    out: List[str] = []
    seq = values if isinstance(values, list) else []
    for value in seq:
        if not value:
            continue
        if isinstance(value, str):
            item = value.strip()
        elif isinstance(value, Mapping):
            item = str(value.get("name") or value.get("label") or value.get("title") or value.get("sector") or "").strip()
        else:
            item = str(value).strip()
        if item:
            out.append(item)
        if len(out) >= 2:
            break
    return out or fallback


def _build_market_home_summary(result: Mapping[str, Any]) -> Dict[str, Any]:
    raw_existing = result.get("home_summary")
    existing: Mapping[str, Any] = raw_existing if isinstance(raw_existing, Mapping) else {}
    return {
        "summary_line": _compact_summary_text(
            existing.get("summary_line") or result.get("summary"),
            "今日暂无 AI 大盘分析摘要。",
        ),
        "signals": _summary_signals(
            existing.get("signals") or [
                result.get("riskLevel") and f"风险 {result.get('riskLevel')}",
                result.get("mainlineAnalysis") or result.get("macroDrivers") or result.get("mood"),
            ],
            ["风险等级", "主题主线"],
        ),
        "tone": str(existing.get("tone") or result.get("mood") or "中性").strip(),
        "risk_tag": str(existing.get("risk_tag") or result.get("riskLevel") or "medium").strip(),
    }


def _build_global_home_summary(doc: Mapping[str, Any]) -> Dict[str, Any]:
    raw_analysis = doc.get("analysis")
    analysis: Mapping[str, Any] = raw_analysis if isinstance(raw_analysis, Mapping) else {}
    raw_existing = analysis.get("home_summary")
    existing: Mapping[str, Any] = raw_existing if isinstance(raw_existing, Mapping) else {}
    mood = str(existing.get("tone") or analysis.get("sentiment_mood") or "中性").strip()
    net_impact = analysis.get("net_impact_score")
    if net_impact is not None and str(net_impact).strip() != "":
        try:
            summary_line = f"{mood} | 净影响分 {float(net_impact):+0.1f}"
        except Exception:
            summary_line = _compact_summary_text(
                existing.get("summary_line") or analysis.get("net_impact") or analysis.get("foreign_impact"),
                "今日暂无全球市场简报。",
            )
    else:
        summary_line = _compact_summary_text(
            existing.get("summary_line") or analysis.get("net_impact") or analysis.get("foreign_impact"),
            "今日暂无全球市场简报。",
        )
    return {
        "summary_line": summary_line,
        "signals": _summary_signals(
            existing.get("signals") or [*(analysis.get("risk_factors") or []), *(analysis.get("key_sectors") or [])],
            ["全球联动", "风险观察"],
        ),
        "tone": mood,
        "risk_tag": str(existing.get("risk_tag") or mood or "中性").strip(),
    }


def _build_data_pulse_home_summary(overview: Mapping[str, Any]) -> Dict[str, Any]:
    raw_sync_health = overview.get("sync_health")
    sync_health: Mapping[str, Any] = raw_sync_health if isinstance(raw_sync_health, Mapping) else {}
    freshness_detail = overview.get("freshness_detail") if isinstance(overview.get("freshness_detail"), list) else []
    raw_news = overview.get("news")
    news: Mapping[str, Any] = raw_news if isinstance(raw_news, Mapping) else {}

    health_pct = float(sync_health.get("health_pct") or 0)
    healthy = int(sync_health.get("healthy") or 0)
    total = int(sync_health.get("total_pipelines") or 0)
    latest_freshness = None
    if freshness_detail:
        latest_values = sorted(
            [str(row.get("latest_value") or "").strip() for row in freshness_detail if row.get("latest_value")],
        )
        latest_freshness = latest_values[-1] if latest_values else None

    if health_pct >= 80:
        status = "正常"
    elif health_pct >= 50:
        status = "部分延迟"
    else:
        status = "异常"

    headline_count = 0
    headlines = news.get("headlines")
    if isinstance(headlines, Mapping):
        headline_count = len([value for value in headlines.values() if value])

    return {
        "status": status if total > 0 else "暂无摘要",
        "summary_line": (
            f"{healthy}/{total} 条管线正常，当前健康度 {health_pct:.0f}%"
            if total > 0
            else "已获取数据脉搏概览，请进入模块查看细项。"
        ),
        "signals": _summary_signals(
            [
                headline_count > 0 and f"新闻 {headline_count} 条",
                "同步正常" if health_pct >= 80 else "存在延迟" if health_pct >= 50 else "需排障",
            ],
            ["同步状态", "新闻摘要"],
        ),
        "tone": status if total > 0 else "中性",
        "risk_tag": status if total > 0 else "暂无摘要",
        "updated_at": (
            f"最新数据 {str(latest_freshness)[4:6]}-{str(latest_freshness)[6:8]}"
            if latest_freshness and len(str(latest_freshness)) >= 8
            else "已更新概览"
        ),
    }


def _get_data_pulse_overview_for_home() -> Dict[str, Any]:
    from routers.data_pulse import _get_international_news_doc
    from services.data_monitoring import build_freshness_rows, build_sync_health_from_catalog

    from datetime import timezone

    sync_health = build_sync_health_from_catalog()
    freshness_rows = build_freshness_rows(include_coverage=False)
    overview: Dict[str, Any] = {
        "news": None,
        "sync_health": sync_health,
        "freshness_detail": freshness_rows,
        "pipelines_detail": [],
    }

    news_doc = _get_international_news_doc()
    if news_doc:
        fetched_at = news_doc.get("fetched_at")
        age_minutes = None
        if fetched_at:
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age_minutes = int((datetime.now(timezone.utc) - fetched_at).total_seconds() / 60)

        categories = news_doc.get("categories", {})
        headlines = {}
        for cat, items in categories.items():
            if items:
                headlines[cat] = items[0].get("title", "")

        overview["news"] = {
            "total": news_doc.get("total", 0),
            "age_minutes": age_minutes,
            "is_fresh": age_minutes is not None and age_minutes < 120,
            "headlines": headlines,
        }

    return overview


def _collect_home_summary_cards(analysis_date: Optional[str] = None) -> Dict[str, Any]:
    started_at = time.perf_counter()
    marks: List[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter() - started_at))

    market_result = fetch_market_analysis_result(analysis_date)
    mark("market_analysis")

    global_doc = get_analyzer_db()["global_analysis"].find_one(sort=[("_id", -1)])
    global_payload = serialize_doc(dict(global_doc)) if global_doc else {}
    global_summary = _build_global_home_summary(global_payload if isinstance(global_payload, dict) else {})
    mark("global_analysis")

    data_pulse_overview = _get_data_pulse_overview_for_home()
    data_pulse_summary = _build_data_pulse_home_summary(data_pulse_overview)
    mark("data_pulse")

    total_elapsed = time.perf_counter() - started_at
    threshold = float(os.getenv("HOME_SUMMARIES_TIMING_SECONDS", "1"))
    if total_elapsed >= threshold:
        previous = 0.0
        parts = []
        for label, elapsed in marks:
            parts.append(f"{label}={elapsed - previous:.3f}s")
            previous = elapsed
        log.warning(
            "Home summaries timing total=%.3fs date=%s %s",
            total_elapsed,
            analysis_date,
            " ".join(parts),
        )

    return {
        "global-market-brief": {
            "status": "已生成" if global_doc else "未生成",
            "summary_line": global_summary["summary_line"] if global_doc else "今日暂无全球市场简报，进入模块后可查看是否已生成新报告。",
            "signals": global_summary["signals"] if global_doc else ["等待生成", "无轮询"],
            "tone": global_summary["tone"] if global_doc else "中性",
            "risk_tag": global_summary["risk_tag"] if global_doc else "中性",
            "updated_at": global_payload.get("analyzed_at") if isinstance(global_payload, dict) else None,
            "success": bool(global_doc),
            "error": None if global_doc else "今日尚无全球市场分析数据",
        },
        "data-pulse": {
            **data_pulse_summary,
            "success": True,
            "error": None,
        },
        "market-analysis": {
            "status": market_result.get("analysisType") or ("已生成" if market_result.get("success") else "等待生成"),
            "summary_line": _build_market_home_summary(market_result)["summary_line"],
            "signals": _build_market_home_summary(market_result)["signals"],
            "tone": _build_market_home_summary(market_result)["tone"],
            "risk_tag": _build_market_home_summary(market_result)["risk_tag"],
            "updated_at": (market_result.get("cache_info") or {}).get("created_at") or market_result.get("analysis_date"),
            "analysis_date": market_result.get("analysis_date"),
            "success": bool(market_result.get("success")),
            "error": market_result.get("error"),
            "user_tip": market_result.get("user_tip"),
        },
    }


def _get_cached_home_summary_cards(analysis_date: Optional[str] = None) -> Dict[str, Any]:
    cache_key = analysis_date or ""
    now = time.time()
    if (
        _home_summaries_cache.get("key") == cache_key
        and _home_summaries_cache.get("payload") is not None
        and float(_home_summaries_cache.get("expires_at") or 0) > now
    ):
        return _home_summaries_cache["payload"]
    payload = _collect_home_summary_cards(analysis_date)
    _home_summaries_cache.update({
        "key": cache_key,
        "payload": payload,
        "expires_at": now + _HOME_SUMMARIES_CACHE_TTL_SECONDS,
    })
    return payload


def _get_cached_market_analysis_summary(analysis_date: Optional[str] = None) -> Dict[str, Any]:
    cache_key = analysis_date or ""
    now = time.time()
    if (
        _market_analysis_summary_cache.get("key") == cache_key
        and _market_analysis_summary_cache.get("payload") is not None
        and float(_market_analysis_summary_cache.get("expires_at") or 0) > now
    ):
        return _market_analysis_summary_cache["payload"]

    result = fetch_market_analysis_result(analysis_date)
    summary = _build_market_home_summary(result)
    payload = {
        "success": bool(result.get("success")),
        "data": {
            "status": result.get("analysisType") or ("已生成" if result.get("success") else "等待生成"),
            "summary_line": summary["summary_line"],
            "signals": summary["signals"],
            "tone": summary["tone"],
            "risk_tag": summary["risk_tag"],
            "updated_at": (result.get("cache_info") or {}).get("created_at") or result.get("analysis_date"),
            "analysis_date": result.get("analysis_date"),
        },
        "error": result.get("error"),
        "user_tip": result.get("user_tip"),
    }
    _market_analysis_summary_cache.update({
        "key": cache_key,
        "payload": payload,
        "expires_at": now + _MARKET_ANALYSIS_SUMMARY_CACHE_TTL_SECONDS,
    })
    return payload


def _theme_lag_trade_date_compact(trade_date: str) -> str:
    """Normalize to YYYYMMDD."""
    raw = (trade_date or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="trade_date 不能为空")
    digits = "".join(c for c in raw if c.isdigit())[:8]
    if len(digits) != 8:
        raise HTTPException(status_code=400, detail="trade_date 须为 YYYYMMDD 或 YYYY-MM-DD")
    return digits


def _theme_lag_json_float(value: Any) -> Optional[float]:
    """Mongo / BSON 数值转为 JSON 安全 float；无效或 NaN 为 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


# GlobalMarketBrief 仪表盘实际用到的标的（与 Vue 模板一致）；默认裁剪以减小序列化与传输耗时。
_DASHBOARD_MARKET_SYMBOLS: Dict[str, List[str]] = {
    "indices": ["^IXIC", "^GSPC", "^DJI", "^RUT", "^VIX"],
    "us_index_futures": ["NQ=F", "ES=F", "YM=F", "RTY=F"],
    "china_concepts": ["FXI", "2823.HK", "PGJ", "BABA", "PDD", "JD", "NIO"],
    "tech_giants": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "AMD", "INTC"],
    "commodities": ["GC=F", "SI=F", "CL=F"],
    "forex": ["USDCNH=X", "DX-Y.NYB"],
    "bonds": ["^TNX"],
    "crypto": ["BTC-USD", "ETH-USD"],
}

# 与 quant_analyzer GlobalMarketService.TICKERS 中仪表盘用到的名称对齐（供前端展示）
_DASHBOARD_SYMBOL_NAMES: Dict[str, Dict[str, str]] = {
    "indices": {
        "^IXIC": "Nasdaq",
        "^GSPC": "S&P 500",
        "^DJI": "Dow Jones",
        "^RUT": "Russell 2000",
        "^VIX": "VIX Volatility",
    },
    "us_index_futures": {
        "ES=F": "S&P 500 Futures",
        "YM=F": "Dow Futures",
        "NQ=F": "Nasdaq Futures",
        "RTY=F": "Russell 2000 Futures",
    },
    "china_concepts": {
        "FXI": "iShares China Large-Cap ETF",
        "2823.HK": "iShares A50 China (HK)",
        "PGJ": "Golden Dragon ETF",
        "BABA": "Alibaba",
        "PDD": "Pinduoduo",
        "JD": "JD.com",
        "NIO": "NIO",
    },
    "tech_giants": {
        "NVDA": "NVIDIA",
        "TSLA": "Tesla",
        "AAPL": "Apple",
        "MSFT": "Microsoft",
        "AMD": "AMD",
        "INTC": "Intel",
        "GOOG": "Google",
        "META": "Meta",
        "AMZN": "Amazon",
    },
    "commodities": {"GC=F": "Gold", "SI=F": "Silver", "CL=F": "Crude Oil"},
    "forex": {"DX-Y.NYB": "USD Index", "USDCNH=X": "USD/CNH (Offshore)"},
    "bonds": {"^TNX": "US 10Y Treasury Yield"},
    "crypto": {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"},
}


def _parse_quote_ts(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def read_global_market_dashboard_from_db() -> Dict[str, Any]:
    """
    从 quant_data.global_market_quotes 读取 GlobalMarketBrief 仪表盘所需的最新同步行情
   （与 quant-data-engine run_global_market_sync 写入结构一致）。
    """
    categories: Dict[str, Dict[str, Any]] = {cat: {} for cat in _DASHBOARD_MARKET_SYMBOLS}
    max_dt: Optional[datetime] = None
    symbol_flat: List[str] = []
    for cat, syms in _DASHBOARD_MARKET_SYMBOLS.items():
        symbol_flat.extend(syms)
    symbol_flat = list(dict.fromkeys(symbol_flat))

    try:
        col = get_data_db()["global_market_quotes"]
        docs = list(col.find({"symbol": {"$in": symbol_flat}}))
    except Exception as e:
        return {"success": False, "error": str(e), "categories": categories}

    by_sym = {d.get("symbol"): d for d in docs if d.get("symbol")}

    for category, symbols in _DASHBOARD_MARKET_SYMBOLS.items():
        names = _DASHBOARD_SYMBOL_NAMES.get(category, {})
        for symbol in symbols:
            doc = by_sym.get(symbol)
            if not doc:
                continue
            price = doc.get("price", 0.0)
            change_pct = doc.get("change_pct", 0.0)
            timestamp_str = doc.get("timestamp", "") or ""
            dt = _parse_quote_ts(timestamp_str)
            if dt is not None and (max_dt is None or dt > max_dt):
                max_dt = dt

            date_val = ""
            if timestamp_str:
                try:
                    date_val = str(timestamp_str).split("T")[0] if "T" in str(timestamp_str) else str(timestamp_str)[:10]
                except Exception:
                    date_val = ""

            try:
                price_f = round(float(price), 2) if price is not None else 0.0
            except (TypeError, ValueError):
                price_f = 0.0
            try:
                chg_f = round(float(change_pct), 2) if change_pct is not None else 0.0
            except (TypeError, ValueError):
                chg_f = 0.0

            item: Dict[str, Any] = {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "price": price_f,
                "change_pct": chg_f,
                "volume": 0,
                "date": date_val,
            }
            if doc.get("prev_close") is not None:
                try:
                    item["prev_close"] = round(float(doc.get("prev_close")), 2)
                except (TypeError, ValueError):
                    pass
            if doc.get("change_pct_source"):
                item["change_pct_source"] = str(doc.get("change_pct_source"))
            if doc.get("prev_close_source"):
                item["prev_close_source"] = str(doc.get("prev_close_source"))
            if timestamp_str:
                item["quote_timestamp"] = str(timestamp_str)

            categories[category][symbol] = item

    synced_max = max_dt.isoformat() if max_dt else None
    return {
        "success": True,
        "categories": categories,
        "synced_at_max": synced_max,
        "data_source": "global_market_quotes",
        "source_note": "Yahoo Finance（经 quant-data-engine 同步至本地库）",
    }


def _prune_market_data_for_dashboard(market_data: Mapping[str, Any]) -> Dict[str, Any]:
    """仅保留前端卡片用到的行情键，避免整包同步数据拖慢 JSON 与浏览器。"""
    out: Dict[str, Any] = dict(market_data)
    cats = out.get("categories")
    if isinstance(cats, dict):
        new_cats: Dict[str, Any] = {}
        for cat, allow in _DASHBOARD_MARKET_SYMBOLS.items():
            src = cats.get(cat)
            if isinstance(src, dict):
                allow_set = set(allow)
                new_cats[cat] = {k: v for k, v in src.items() if k in allow_set}
            else:
                new_cats[cat] = {}
        out["categories"] = new_cats
        return out
    for cat, allow in _DASHBOARD_MARKET_SYMBOLS.items():
        src = out.get(cat)
        if isinstance(src, dict):
            allow_set = set(allow)
            out[cat] = {k: v for k, v in src.items() if k in allow_set}
    return out


def _format_trade_date(raw_date: str) -> Optional[str]:
    """Normalize date string to YYYY-MM-DD."""
    if not raw_date:
        return None
    date_str = str(raw_date).strip()
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    if len(date_str) == 10 and "-" in date_str:
        return date_str
    return None


def _resolve_effective_market_analysis_date(requested_date: str) -> Dict[str, Any]:
    """Resolve effective trading date for market analysis tasks."""
    requested_day = date.fromisoformat(requested_date)
    if market_analysis_cache.is_trading_day(requested_day):
        return {
            "requested_date": requested_date,
            "effective_analysis_date": requested_date,
            "is_non_trading_trigger": False
        }

    latest_trade_date_raw = get_latest_trade_date()
    effective_date = _format_trade_date(latest_trade_date_raw or "") or requested_date
    return {
        "requested_date": requested_date,
        "effective_analysis_date": effective_date,
        "is_non_trading_trigger": True
    }


@router.post("/analyze-global")
async def trigger_global_analysis(current_user: dict = Depends(get_current_active_user)):
    """手动触发全球市场分析任务
    
    插入任务到 analysis_tasks 集合，由 quantAnalyzer 异步处理。
    """
    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        user_id = str(current_user["id"])
            
        # 避免当天重复提交 (pending or processing)
        existing = tasks_col.find_one({
            "type": "global_analysis",
            "params.date": today_str,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            return {
                "success": True, 
                "message": "任务已在队列中，请稍候", 
                "task_id": str(existing["_id"])
            }
            
        task_doc = build_task_doc(
            current_user=current_user,
            task_type="global_analysis",
            priority=20,
            symbol="GLOBAL",
            stock_name="全球市场联动",
            params={"date": today_str},
            source="manual_trigger",
        )
        
        result = tasks_col.insert_one(task_doc)
        print(f"[Global Analysis] Manual trigger task {result.inserted_id}")
        
        return {
            "success": True, 
            "task_id": str(result.inserted_id),
            "message": "分析任务已提交，请稍后刷新查看结果"
        }
        
    except Exception as e:
        print(f"[Global Analysis] Trigger failed: {e}")
        return {"success": False, "error": str(e)}


def fetch_market_analysis_result(
    analysis_date: Optional[str] = None,
    time_slot: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch cached market analysis for frontend.

    - If analysis_date is provided: return the cached result for that specific date.
    - If not provided: on trading days, return today's cached result; 
      on non-trading days, fall back to the latest available trading day.
    - If time_slot is provided: return that specific HHMM slot; otherwise the latest slot
      for the chosen date.
    """
    target_day = date.today()
    is_explicit_date = bool(analysis_date)

    if analysis_date:
        target_day = date.fromisoformat(analysis_date)
        today_str = analysis_date
        print(f"[Market Analysis] Explicit date requested: {today_str} slot={time_slot or 'latest'}")
    else:
        today_str = target_day.strftime('%Y-%m-%d')
        print(f"[Market Analysis] No date provided, using default: {today_str} slot={time_slot or 'latest'}")

    is_trading_day = market_analysis_cache.is_trading_day(target_day)

    # Try to get the analysis for the target date / slot
    cached = market_analysis_cache.get_cached_analysis(today_str, time_slot=time_slot)

    def _attach_comparison(result: Dict[str, Any], cached_doc: Dict[str, Any]) -> None:
        """Append previous_snapshot diff context to a result dict (mutates in place)."""
        cur_slot = cached_doc.get("time_slot") or time_slot
        cur_date = cached_doc.get("analysis_date", today_str)
        snapshot = cached_doc.get("snapshot")
        if snapshot:
            result["snapshot"] = snapshot
        if cur_slot:
            result["time_slot"] = cur_slot
        # Same-day other slots (for tab UI)
        try:
            result["available_slots"] = market_analysis_cache.list_slots_for_date(cur_date)
        except Exception as exc:
            print(f"[Market Analysis] list_slots_for_date failed: {exc}")
            result["available_slots"] = []
        # Nearest previous snapshot (cross-day, same-time-window)
        if cur_slot:
            try:
                prev = market_analysis_cache.get_nearest_previous_snapshot(cur_date, cur_slot)
            except Exception as exc:
                print(f"[Market Analysis] get_nearest_previous_snapshot failed: {exc}")
                prev = None
            if prev:
                result["previous_snapshot"] = prev["snapshot"]
                result["previous_snapshot_meta"] = {
                    "analysis_date": prev["analysis_date"],
                    "time_slot": prev["time_slot"],
                    "lag_minutes": prev["lag_minutes"],
                }

    if cached:
        result = dict(cached.get('analysis_result', {}))
        result['home_summary'] = _build_market_home_summary(result)
        result['analysis_date'] = cached.get('analysis_date', today_str)
        result['cache_info'] = {
            'from_cache': True,
            'cache_date': cached.get('analysis_date', today_str),
            'created_at': cached.get('created_at'),
            'reason': "返回当日缓存的大盘分析结果"
        }
        # Add success flag for frontend
        result['success'] = True
        _attach_comparison(result, cached)
        return result

    # 非交易日：优先回退到最近交易日结果，避免用户误判系统异常
    if not is_trading_day:
        print(f"[Market Analysis] {today_str} is non-trading day, falling back to latest trading day")
        latest_analysis = market_analysis_cache.get_latest_trading_day_analysis()
        if latest_analysis:
            result = dict(latest_analysis.get('analysis_result', {}))
            result['home_summary'] = _build_market_home_summary(result)
            result['analysis_date'] = latest_analysis['analysis_date']
            result['cache_info'] = {
                'from_cache': True,
                'cache_date': latest_analysis['analysis_date'],
                'created_at': latest_analysis.get('created_at'),
                'reason': f"今日非交易日，展示最近交易日({latest_analysis['analysis_date']})的分析结果"
            }
            result['success'] = True
            result['notice'] = f"{today_str} 为非交易日，已展示最近交易日数据。"
            _attach_comparison(result, latest_analysis)
            return result

        return {
            "success": False,
            "error": f"{today_str} 为非交易日，且暂无可展示的历史大盘分析数据",
            "is_trading_day": False,
            "is_expected": True,
            "user_tip": "今日不是交易日，系统不会生成新大盘分析。"
        }

    # 交易日：若分析未产出，明确提示预计时间，不做回退以免误导用户
    schedule_text = "10:00 / 11:30 / 15:30"
    print(f"[Market Analysis] {today_str} trading-day analysis not ready")
    message_date = "今日" if target_day == date.today() and not is_explicit_date else today_str
    # Surface available slots even when the requested slot is missing, so the UI can
    # offer a fallback instead of a hard "not ready" state.
    try:
        slots_today = market_analysis_cache.list_slots_for_date(today_str)
    except Exception:
        slots_today = []
    return {
        "success": False,
        "error": f"{message_date} 的大盘分析尚未生成",
        "is_trading_day": True,
        "is_expected": True,
        "expected_schedule": ["10:00", "11:30", "15:30"],
        "available_slots": slots_today,
        "user_tip": f"系统会在交易日 {schedule_text} 自动更新，请稍后刷新查看。"
    }

@router.get("/market-analysis")
async def get_market_analysis(
    date: Optional[str] = Query(default=None, description="分析日期，格式 YYYY-MM-DD"),
    time_slot: Optional[str] = Query(default=None, description="HHMM 时间槽，缺省返回当日最新槽"),
    current_user: dict = Depends(get_current_active_user)
):
    """Canonical read endpoint for cached market analysis results."""
    return fetch_market_analysis_result(date, time_slot=time_slot)


@router.get("/market-analysis/slots")
async def list_market_analysis_slots(
    date: Optional[str] = Query(default=None, description="日期 YYYY-MM-DD，默认今日"),
    current_user: dict = Depends(get_current_active_user),
):
    """Return the list of HHMM slots available for a given analysis date."""
    _ = current_user
    target = date or datetime.now().strftime('%Y-%m-%d')
    try:
        slots = market_analysis_cache.list_slots_for_date(target)
    except Exception as exc:
        return {"success": False, "error": str(exc), "analysis_date": target, "slots": []}
    return {"success": True, "analysis_date": target, "slots": slots}


@router.get("/market-analysis/summary")
async def get_market_analysis_summary(
    date: Optional[str] = Query(default=None, description="分析日期，格式 YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user)
):
    _ = current_user
    return _get_cached_market_analysis_summary(date)


@router.get("/home-summaries")
async def get_home_summaries(
    date: Optional[str] = Query(default=None, description="大盘分析日期，格式 YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user)
):
    _ = current_user
    try:
        return {
            "success": True,
            "data": _get_cached_home_summary_cards(date),
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"系统错误: {str(e)}",
        }


@router.post("/analyze-market")
async def analyze_market(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """Legacy compatibility endpoint for reading cached market analysis.

    实际分析工作由后台 Celery 定时任务完成，这里不再触发 LLM 调用。
    新调用方应改用 GET /market-analysis；此路由仅为兼容旧前端保留。
    """
    return fetch_market_analysis_result(request.date)


@router.post("/trigger-market-analysis")
async def trigger_market_analysis(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """手动触发大盘分析任务（强制生成新分析）

    插入任务到 analysis_tasks 集合，由 quantAnalyzer 异步处理。
    """
    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]

        requested_date = request.date or datetime.now().strftime('%Y-%m-%d')
        user_id = str(current_user["id"])
        trigger_context = _resolve_effective_market_analysis_date(requested_date)
        effective_date = trigger_context["effective_analysis_date"]
        is_non_trading_trigger = trigger_context["is_non_trading_trigger"]

        if is_non_trading_trigger and not request.force_refresh:
            existing_analysis = market_analysis_cache.get_cached_analysis(effective_date)
            if existing_analysis:
                return {
                    "success": True,
                    "skipped_existing": True,
                    "message": f"所选日期为非交易日，最近交易日({effective_date})已有分析结果，无需重复触发",
                    "requested_date": requested_date,
                    "effective_analysis_date": effective_date,
                    "is_non_trading_trigger": True
                }

        # 避免重复提交 (pending or processing)
        existing = tasks_col.find_one({
            "type": "market_analysis",
            "status": {"$in": ["pending", "processing"]},
            "$or": [
                {"params.effective_analysis_date": effective_date},
                {"params.date": effective_date},
            ]
        })

        if existing:
            return {
                "success": True,
                "message": "任务已在队列中，请稍候",
                "task_id": str(existing["_id"]),
                "requested_date": requested_date,
                "effective_analysis_date": effective_date,
                "is_non_trading_trigger": is_non_trading_trigger
            }

        task_doc = build_task_doc(
            current_user=current_user,
            task_type="market_analysis",
            priority=20,
            symbol="MARKET",
            stock_name="大盘分析",
            params={
                "date": effective_date,
                "requested_date": requested_date,
                "effective_analysis_date": effective_date,
                "is_non_trading_trigger": is_non_trading_trigger,
                "force_refresh": bool(request.force_refresh),
            },
            source="manual_trigger",
        )

        result = tasks_col.insert_one(task_doc)
        print(f"[Market Analysis] Manual trigger task {result.inserted_id}")

        return {
            "success": True,
            "task_id": str(result.inserted_id),
            "message": (
                f"所选日期为非交易日，将按最近交易日({effective_date})口径生成分析"
                if is_non_trading_trigger
                else "分析任务已提交，请稍后刷新查看结果"
            ),
            "requested_date": requested_date,
            "effective_analysis_date": effective_date,
            "is_non_trading_trigger": is_non_trading_trigger
        }

    except Exception as e:
        print(f"[Market Analysis] Trigger failed: {e}")
        return {"success": False, "error": str(e)}

@router.get("/global-analysis")
async def get_global_analysis(
    current_user: dict = Depends(get_current_active_user),
    full: bool = Query(
        False,
        description="为 true 时返回完整 market_data（体积大）；默认 false 仅返回仪表盘用到的标的",
    ),
):
    """获取最新的全球市场联动分析结果。

    默认可显著减小响应体：``market_data`` 中仅保留 GlobalMarketBrief 模板用到的代码，
    以降低 ``serialize_doc`` / JSON 与网络耗时（Mongo 仍读取整文档；若需全量请传 ``full=true``）。
    """
    try:
        # Primary-only source: quant_analyzer.global_analysis.
        # Intentionally do NOT fallback to finance.analysis_tasks so data issues surface early.
        doc = get_analyzer_db()["global_analysis"].find_one(sort=[("_id", -1)])

        if not doc:
            return {"success": False, "error": "今日尚无全球市场分析数据"}

        payload = dict(doc)
        if not full:
            md = payload.get("market_data")
            if isinstance(md, dict):
                payload["market_data"] = _prune_market_data_for_dashboard(md)

        result = serialize_doc(payload)
        if isinstance(result, dict):
            result["home_summary"] = _build_global_home_summary(result)
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        print(f"[全球分析] 获取失败: {e}")
        return {
            "success": False,
            "error": f"系统错误: {str(e)}"
        }


@router.get("/global-analysis/summary")
async def get_global_analysis_summary(current_user: dict = Depends(get_current_active_user)):
    _ = current_user
    try:
        doc = get_analyzer_db()["global_analysis"].find_one(sort=[("_id", -1)])
        if not doc:
            return {
                "success": False,
                "data": {
                    "status": "未生成",
                    "summary_line": "今日暂无全球市场简报，进入模块后可查看是否已生成新报告。",
                    "signals": ["等待生成", "无轮询"],
                    "tone": "中性",
                    "risk_tag": "中性",
                    "updated_at": None,
                },
                "error": "今日尚无全球市场分析数据",
            }
        payload = serialize_doc(dict(doc))
        payload_dict: Dict[str, Any] = payload if isinstance(payload, dict) else {}
        summary = _build_global_home_summary(payload_dict)
        return {
            "success": True,
            "data": {
                "status": "已生成",
                "summary_line": summary["summary_line"],
                "signals": summary["signals"],
                "tone": summary["tone"],
                "risk_tag": summary["risk_tag"],
                "updated_at": payload_dict.get("analyzed_at") or payload_dict.get("created_at") or payload_dict.get("timestamp"),
            },
        }
    except Exception as e:
        return {
            "success": False,
            "data": {
                "status": "读取失败",
                "summary_line": "全球市场简报摘要暂时不可用，可直接进入模块查看详情。",
                "signals": ["手动进入模块", "稍后重试"],
                "tone": "中性",
                "risk_tag": "中性",
                "updated_at": None,
            },
            "error": f"系统错误: {str(e)}",
        }


@router.get("/global-market-dashboard")
async def get_global_market_dashboard(current_user: dict = Depends(get_current_active_user)):
    """GlobalMarketBrief 仪表盘：返回 MongoDB 中最新同步的全球行情（与 AI 分析报告解耦）。"""
    _ = current_user
    payload = read_global_market_dashboard_from_db()
    if payload.get("success"):
        return {"success": True, "data": payload}
    return {"success": False, "error": payload.get("error", "unknown")}


# ---------------------------------------------------------------------------
# 全球市场日线 K 线接口（global_market_daily 集合）
# ---------------------------------------------------------------------------

# 已在 global_market_daily 中同步历史日线的品种白名单
_KLINE_AVAILABLE_SYMBOLS = {
    # US broad market
    "SPY", "^GSPC", "^DJI", "^RUT",
    # Nasdaq / tech
    "QQQ", "XLK", "^IXIC",
    # Semiconductor
    "SOXX", "SMH", "^SOX",
    # Sector ETFs
    "XLE", "XLF", "XLV", "XLI", "XLU", "XLRE",
    # Innovation
    "ARKK",
    # Volatility + rates
    "^VIX", "^TNX", "^TYX",
    # USD index
    "DX-Y.NYB",
    # Global indices
    "^N225", "^HSI", "000001.SS", "FXI", "EWJ", "EWG", "EWY",
    # Commodities
    "GC=F", "CL=F", "SI=F",
    # Crypto
    "BTC-USD", "ETH-USD",
    # US index futures
    "ES=F", "NQ=F",
    # Tech giants
    "NVDA", "AMD", "AAPL", "MSFT", "TSLA", "GOOG", "META", "AMZN", "INTC",
    # China concepts
    "BABA", "PDD", "JD", "NIO", "2823.HK", "PGJ",
    # US index futures (additional)
    "YM=F", "RTY=F",
}


def _resample_global_ohlcv(rows: List[Dict[str, Any]], rule: str) -> List[Dict[str, Any]]:
    """将日线 rows（升序）聚合为周线(1w)或月线(1m)，返回降序结果。"""
    import pandas as pd
    import math as _math
    if not rows:
        return []
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
    df["_td"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["_td"]).set_index("_td").sort_index()
    rule_s = "W-FRI" if rule == "1w" else "ME"
    rs_kw = {"label": "right", "closed": "right"}
    agg = df.resample(rule_s, **rs_kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    out = []
    for dt_idx, row in agg.iterrows():
        def _f(v):
            return None if v is None or (isinstance(v, float) and _math.isnan(v)) else round(float(v), 4)
        out.append({
            "trade_date": dt_idx.strftime("%Y%m%d"),
            "open": _f(row.get("open")),
            "high": _f(row.get("high")),
            "low":  _f(row.get("low")),
            "close": _f(row.get("close")),
            "volume": int(row.get("volume", 0)),
        })
    return list(reversed(out))


# NOTE: this route must be declared BEFORE the parameterised /{symbol}/kline
# route so FastAPI does not match "kline-symbols" as a symbol value.
@router.get("/global-market/kline-symbols")
async def get_kline_available_symbols(current_user: dict = Depends(get_current_active_user)):
    """返回 global_market_daily 中有历史 K 线数据的品种列表。"""
    _ = current_user
    return {"success": True, "symbols": sorted(_KLINE_AVAILABLE_SYMBOLS)}


@router.get("/global-market/{symbol}/kline")
async def get_global_market_kline(
    symbol: str,
    start_date: Optional[str] = Query(None, description="起始日期 YYYYMMDD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    limit: int = Query(500, ge=1, le=2000, description="返回最大根数"),
    tf: str = Query("1d", description="时间周期：1d 日线 | 1w 周线 | 1m 月线"),
    current_user: dict = Depends(get_current_active_user),
):
    """从 quant_data.global_market_daily 返回指定品种 OHLCV K 线。

    支持日/周/月三个周期；周月线由日线聚合得到。
    数据字段与 ETF kline 接口保持一致：trade_date / open / high / low / close / volume。
    """
    _ = current_user
    import math as _math

    tfc = tf.strip().lower()
    # normalise shorthand
    tfc = {"d": "1d", "w": "1w", "m": "1m"}.get(tfc.lstrip("1"), tfc)
    if tfc not in ("1d", "1w", "1m"):
        raise HTTPException(400, "tf must be 1d, 1w, or 1m")

    query: Dict[str, Any] = {"symbol": symbol}
    date_f: Dict[str, str] = {}
    if start_date:
        date_f["$gte"] = start_date.replace("-", "")
    if end_date:
        date_f["$lte"] = end_date.replace("-", "")
    if date_f:
        query["date"] = date_f

    # For weekly/monthly resampling we need more raw daily points
    fetch_limit = limit if tfc == "1d" else min(limit * 7, 2000)

    raw = list(
        get_data_db()["global_market_daily"]
        .find(query, {"_id": 0, "symbol": 0, "updated_at": 0})
        .sort("date", -1)
        .limit(fetch_limit)
    )

    if not raw:
        return {"success": True, "symbol": symbol, "tf": tfc, "count": 0, "data": []}

    # Normalise: rename date → trade_date, cast numerics
    rows = []
    for doc in raw:
        def _safe(v):
            if v is None:
                return None
            try:
                f = float(v)
                return None if _math.isnan(f) else round(f, 4)
            except (TypeError, ValueError):
                return None

        rows.append({
            "trade_date": doc.get("date", ""),
            "open":   _safe(doc.get("open")),
            "high":   _safe(doc.get("high")),
            "low":    _safe(doc.get("low")),
            "close":  _safe(doc.get("close")),
            "volume": int(doc["volume"]) if doc.get("volume") is not None else 0,
        })

    if tfc in ("1w", "1m"):
        rows = _resample_global_ohlcv(list(reversed(rows)), tfc)
        rows = rows[:limit]

    return {
        "success": True,
        "symbol": symbol,
        "tf": tfc,
        "count": len(rows),
        "data": rows,
    }


def fetch_market_data() -> Optional[Dict[str, Any]]:
    """获取用于市场分析的数据 - 优先使用外部数据源"""
    try:
        print("[市场数据] 开始获取分析数据...")
        
        # 首先尝试获取外部实时数据
        try:
            external_data = external_market_data.get_real_market_data()
            if external_data and external_data.get('market_data'):
                print(f"[市场数据] 外部数据获取成功，包含 {len(external_data['market_data'])} 个指数")
                return external_data
        except Exception as e:
            print(f"[市场数据] 外部数据获取失败，尝试本地数据: {e}")
        
        # 备用方案：从本地数据库获取
        print("[市场数据] 使用本地数据库数据...")
        data_collections = get_data_db()
        collection = data_collections["volume_price"] # type: ignore
        index_symbols = ['1A0001', '399001', '399006', '899050']  # 上证指数、深证成指、创业板指、北证50
        market_data = {}
        analysis_date = None
        
        for symbol in index_symbols:
            # 获取最新记录
            latest_record = collection.find_one(
                {"symbol": symbol},
                sort=[("trade_date", -1)]
            )
            
            if latest_record:
                # 记录最新的交易日期作为分析基准日期
                if analysis_date is None or latest_record["trade_date"] > analysis_date:
                    analysis_date = latest_record["trade_date"]
                
                # 获取最近10天的历史记录
                historical_records = list(collection.find(
                    {"symbol": symbol},
                    sort=[("trade_date", -1)]
                ).limit(10))
                
                market_data[symbol] = {
                    "symbol": symbol,
                    "latest_record": serialize_doc(latest_record),
                    "historical_records": [serialize_doc(doc) for doc in historical_records]
                }
        
        # 如果本地数据库也没有指数数据，使用有代表性的个股
        if not market_data:
            print("[市场数据] 无指数数据，使用代表性个股...")
            representative_stocks = ['000001', '000002', '300347']  # 平安、万科、代表性创业板股票
            
            for symbol in representative_stocks:
                latest_record = collection.find_one(
                    {"symbol": symbol},
                    sort=[("trade_date", -1)]
                )
                
                if latest_record:
                    if analysis_date is None or latest_record["trade_date"] > analysis_date:
                        analysis_date = latest_record["trade_date"]
                    
                    historical_records = list(collection.find(
                        {"symbol": symbol},
                        sort=[("trade_date", -1)]
                    ).limit(10))
                    
                    market_data[symbol] = {
                        "symbol": symbol,
                        "latest_record": serialize_doc(latest_record),
                        "historical_records": [serialize_doc(doc) for doc in historical_records]
                    }
        
        # 添加分析基准日期信息
        if market_data and analysis_date:
            result = {
                "analysis_date": analysis_date.strftime("%Y-%m-%d") if hasattr(analysis_date, 'strftime') else str(analysis_date),
                "market_data": market_data,
                "data_source": "local_database"
            }
            print(f"[市场数据] 本地数据获取成功，包含 {len(market_data)} 个品种")
            return result
        
        print("[市场数据] 无法获取到有效的市场数据")
        return None
        
    except Exception as e:
        print(f"[市场数据] 获取失败: {e}")
        return None
    
@router.get('/market-spectrum', summary="获取市场阴阳谱数据", tags=["Market"])
def get_market_spectrum(
    start_date: str = Query(..., description="开始日期,格式YYYYMMDD"),
    end_date: str = Query(..., description="结束日期,格式YYYYMMDD")
):  
    data_collections = get_data_db()
    collection = data_collections['market_spectrum']
    query = {
        'trade_date': {'$gte': start_date, '$lte': end_date}
    }
    projection = {
        '_id': 0,
        'trade_date': 1,
        'yang_spectrum': 1,
        'yin_spectrum': 1,
        'total_stocks': 1,
        'above_ma_count': 1,
        'below_ma_count': 1,
        'distribution': 1
    }
    cursor = collection.find(query, projection).sort('trade_date', 1)
    data = list(cursor)
    return {"data": data}


def _resolve_sector_members(level: str) -> Dict[str, Dict[str, str]]:
    now = time.time()
    cached = _sector_members_cache.get(level) or {}
    if cached.get("payload") is not None and float(cached.get("expires_at") or 0) > now:
        return cached["payload"]

    prefix = level.lower()
    cursor = get_data_db()["sw_index_member"].find(
        {"src": "SW2021", "is_new": "Y"},
        {
            "_id": 0,
            "con_code": 1,
            f"{prefix}_code": 1,
            f"{prefix}_name": 1,
        },
    )
    members: Dict[str, Dict[str, str]] = {}
    for doc in cursor:
        symbol = str(doc.get("con_code") or "").strip()
        sector_code = str(doc.get(f"{prefix}_code") or "").strip()
        sector_name = str(doc.get(f"{prefix}_name") or "").strip()
        if symbol and sector_code and sector_name:
            members[symbol] = {"sector_code": sector_code, "sector_name": sector_name}
    _sector_members_cache[level] = {
        "payload": members,
        "expires_at": now + _SECTOR_SPECTRUM_CACHE_TTL_SECONDS,
    }
    return members


def _sector_distribution(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "gt_7": sum(1 for row in rows if (_num_or_none(row.get("pct_chg")) or 0) > 7),
        "p5_7": sum(1 for row in rows if 5 < (_num_or_none(row.get("pct_chg")) or 0) <= 7),
        "p2_5": sum(1 for row in rows if 2 < (_num_or_none(row.get("pct_chg")) or 0) <= 5),
        "p0_2": sum(1 for row in rows if 0 < (_num_or_none(row.get("pct_chg")) or 0) <= 2),
        "flat": sum(1 for row in rows if (_num_or_none(row.get("pct_chg")) or 0) == 0),
        "m2_0": sum(1 for row in rows if -2 <= (_num_or_none(row.get("pct_chg")) or 0) < 0),
        "m5_2": sum(1 for row in rows if -5 <= (_num_or_none(row.get("pct_chg")) or 0) < -2),
        "m7_5": sum(1 for row in rows if -7 <= (_num_or_none(row.get("pct_chg")) or 0) < -5),
        "lt_m7": sum(1 for row in rows if (_num_or_none(row.get("pct_chg")) or 0) < -7),
    }


def _previous_trade_dates(volume_price, start_date: str, needed: int) -> List[str]:
    """Return latest trading dates before start_date (distinct on trade_date)."""
    if needed <= 0:
        return []
    dates = volume_price.distinct(
        "trade_date",
        {"trade_date": {"$lt": start_date}, "close": {"$exists": True, "$ne": None}},
    )
    valid = sorted({str(d) for d in dates if d}, reverse=True)
    return sorted(valid[:needed])


@router.get('/market-spectrum/sectors', summary="获取板块日线阴阳谱数据", tags=["Market"])
def get_market_spectrum_sectors(
    start_date: str = Query(..., description="开始日期,格式YYYYMMDD"),
    end_date: str = Query(..., description="结束日期,格式YYYYMMDD"),
    level: str = Query("L1", pattern="^L[12]$", description="申万层级: L1 或 L2"),
    ma_period: int = Query(5, ge=2, le=30, description="均线周期"),
    limit: int = Query(80, ge=1, le=300, description="返回板块数量上限"),
    sector_code: Optional[str] = Query(None, description="可选：只返回指定板块的区间曲线"),
):
    """Dynamically aggregate daily sector breadth from volume_price.

    The first version intentionally supports L1/L2 only. L3 is better served as
    an anomaly drill-down because the full list is noisy and expensive.
    """
    cache_key = "|".join([start_date, end_date, level, str(ma_period), str(limit), sector_code or ""])
    cached = _get_sector_spectrum_cache(cache_key)
    if cached is not None:
        return cached

    data_db = get_data_db()
    members = _resolve_sector_members(level)
    if sector_code:
        members = {
            symbol: sector
            for symbol, sector in members.items()
            if sector["sector_code"] == sector_code
        }
    if not members:
        return {"data": [], "level": level, "error": "No sector members"}

    volume_price = data_db["volume_price"]
    lookback_start = start_date
    prev_days = _previous_trade_dates(volume_price, start_date, ma_period - 1)
    if prev_days:
        lookback_start = prev_days[0]

    member_symbols = list(members.keys())
    cursor = volume_price.find(
        {
            "symbol": {"$in": member_symbols},
            "trade_date": {"$gte": lookback_start, "$lte": end_date},
            "close": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1, "pct_chg": 1},
    ).sort([("symbol", 1), ("trade_date", 1)])

    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in cursor:
        symbol = str(doc.get("symbol") or "")
        rows_by_symbol[symbol].append(doc)

    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for symbol, rows in rows_by_symbol.items():
        sector = members[symbol]
        for idx, row in enumerate(rows):
            trade_date = str(row.get("trade_date") or "")
            if trade_date < start_date or trade_date > end_date:
                continue
            window = rows[max(0, idx - ma_period + 1): idx + 1]
            closes = [_num_or_none(item.get("close")) for item in window]
            closes = [value for value in closes if value is not None]
            if len(closes) < ma_period:
                continue
            close = _num_or_none(row.get("close"))
            if close is None:
                continue
            enriched = {
                **row,
                "sector_code": sector["sector_code"],
                "sector_name": sector["sector_name"],
                "above_ma": close > (sum(closes) / len(closes)),
            }
            groups[(trade_date, sector["sector_code"], sector["sector_name"])].append(enriched)

    docs: List[Dict[str, Any]] = []
    for (trade_date, sector_code, sector_name), rows in groups.items():
        total = len(rows)
        if total <= 0:
            continue
        above = sum(1 for row in rows if row.get("above_ma"))
        docs.append({
            "trade_date": trade_date,
            "level": level,
            "sector_code": sector_code,
            "sector_name": sector_name,
            "yang_spectrum": above / total,
            "yin_spectrum": 1 - (above / total),
            "total_stocks": total,
            "above_ma_count": above,
            "below_ma_count": total - above,
            "distribution": _sector_distribution(rows),
            "ma_period": ma_period,
            "granularity": "daily_sector",
        })

    docs.sort(key=lambda row: (row["trade_date"], -row["yang_spectrum"], row["sector_code"]))
    if limit and not sector_code:
        per_date: Dict[str, int] = defaultdict(int)
        trimmed: List[Dict[str, Any]] = []
        for row in docs:
            if per_date[row["trade_date"]] >= limit:
                continue
            trimmed.append(row)
            per_date[row["trade_date"]] += 1
        docs = trimmed

    return _set_sector_spectrum_cache(cache_key, {"data": docs, "level": level, "ma_period": ma_period})


@router.get('/market-spectrum/realtime-sectors', summary="获取实时板块阴阳谱数据", tags=["Market"])
def get_market_spectrum_realtime_sectors(
    start_time: str = Query(..., description="开始时间,格式YYYYMMDDHHMM"),
    end_time: str = Query(..., description="结束时间,格式YYYYMMDDHHMM"),
    level: str = Query("L1", pattern="^L[12]$", description="申万层级: L1 或 L2"),
    ma_period: int = Query(5, ge=2, le=30, description="均线周期"),
    limit: int = Query(80, ge=1, le=300, description="返回板块数量上限"),
    sector_code: Optional[str] = Query(None, description="可选：只返回指定板块的区间曲线"),
):
    """Return materialized realtime sector breadth snapshots.

    Data is produced by realtime_daily_k sync into realtime_sector_strength,
    so the query path stays light enough for the realtime spectrum tab.
    """
    cache_key = "|".join([
        "realtime",
        start_time,
        end_time,
        level,
        str(ma_period),
        str(limit),
        sector_code or "",
    ])
    cached = _get_sector_spectrum_cache(cache_key)
    if cached is not None:
        return cached

    collection = get_data_db()["realtime_sector_strength"]
    query: Dict[str, Any] = {
        "snapshot_time": {"$gte": start_time, "$lte": end_time},
        "level": level,
    }
    if sector_code:
        query["sector_code"] = sector_code

    projection = {
        "_id": 0,
        "snapshot_time": 1,
        "trade_date": 1,
        "level": 1,
        "sector_code": 1,
        "sector_name": 1,
        "stock_count": 1,
        "above_ma5_count": 1,
        "above_ma5_ratio": 1,
        "positive_count": 1,
        "negative_count": 1,
        "flat_count": 1,
        "avg_pct_chg": 1,
        "median_pct_chg": 1,
        "total_amount_yuan": 1,
        "relative_positive_ratio": 1,
        "data_source": 1,
        "updated_at": 1,
    }
    docs: List[Dict[str, Any]] = []
    for row in collection.find(query, projection).sort([("snapshot_time", 1), ("above_ma5_ratio", -1)]):
        total = int(row.get("stock_count") or 0)
        if total <= 0:
            continue
        above = int(row.get("above_ma5_count") or 0)
        yang = _num_or_none(row.get("above_ma5_ratio"))
        if yang is None:
            yang = above / total
        docs.append({
            "trade_date": str(row.get("snapshot_time") or ""),
            "snapshot_time": str(row.get("snapshot_time") or ""),
            "trade_day": str(row.get("trade_date") or "")[:8],
            "level": level,
            "sector_code": str(row.get("sector_code") or ""),
            "sector_name": str(row.get("sector_name") or ""),
            "yang_spectrum": yang,
            "yin_spectrum": 1 - yang,
            "total_stocks": total,
            "above_ma_count": above,
            "below_ma_count": total - above,
            "positive_count": int(row.get("positive_count") or 0),
            "negative_count": int(row.get("negative_count") or 0),
            "flat_count": int(row.get("flat_count") or 0),
            "avg_pct_chg": _num_or_none(row.get("avg_pct_chg")),
            "median_pct_chg": _num_or_none(row.get("median_pct_chg")),
            "total_amount_yuan": _num_or_none(row.get("total_amount_yuan")),
            "relative_positive_ratio": _num_or_none(row.get("relative_positive_ratio")),
            "distribution": _sector_strength_distribution(row),
            "ma_period": ma_period,
            "granularity": "daily_realtime_sector",
            "data_source": row.get("data_source"),
            "updated_at": row.get("updated_at"),
        })

    docs.sort(key=lambda row: (row["trade_date"], -row["yang_spectrum"], row["sector_code"]))
    if limit and not sector_code:
        per_snapshot: Dict[str, int] = defaultdict(int)
        trimmed: List[Dict[str, Any]] = []
        for row in docs:
            if per_snapshot[row["trade_date"]] >= limit:
                continue
            trimmed.append(row)
            per_snapshot[row["trade_date"]] += 1
        docs = trimmed

    return _set_sector_spectrum_cache(cache_key, {"data": docs, "level": level, "ma_period": ma_period})


def _normalize_compact_date(value: str) -> str:
    return str(value or "").replace("-", "")[:8]


def _realtime_amount_yuan_from_doc(doc: Mapping[str, Any]) -> Optional[float]:
    raw = _num_or_none(doc.get("realtime_amount_yuan"))
    if raw is not None:
        return raw
    amt_k = _num_or_none(doc.get("amount"))
    if amt_k is None:
        return None
    return amt_k * 1000.0


def _load_stock_names_map(data_db, symbols: List[str]) -> Dict[str, str]:
    """Resolve display names for ts_code-style symbols (e.g. 000001.SZ).

    Primary source is ``stock_info`` (``symbol`` + ``name``), which matches how
    the rest of quantFinance resolves names. ``stock_basic`` is used as a
    fallback when names are still missing.
    """
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    out: Dict[str, str] = {s: s for s in uniq}

    stock_info = data_db["stock_info"]
    for row in stock_info.find({"symbol": {"$in": uniq}}, {"_id": 0, "symbol": 1, "name": 1, "stock_name": 1}):
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        raw = row.get("name") or row.get("stock_name") or ""
        name = str(raw).strip()
        if name:
            out[sym] = name

    missing = [s for s in uniq if out.get(s) == s]
    if not missing:
        return out

    basic = data_db["stock_basic"]
    for row in basic.find(
        {"$or": [{"ts_code": {"$in": missing}}, {"symbol": {"$in": missing}}]},
        {"_id": 0, "ts_code": 1, "symbol": 1, "name": 1},
    ):
        code = str(row.get("ts_code") or row.get("symbol") or "").strip()
        name = str(row.get("name") or "").strip()
        if code and name and out.get(code) == code:
            out[code] = name

    return out


def _build_sector_leader_previews(rows_out: List[Dict[str, Any]], k: int = 5) -> Dict[str, Any]:
    """Top-k representatives by amount, pct_chg, and |pct_chg| (volatility / attention proxy).

    Same snapshot as MA contributor tables; multiple lenses for user judgment, not a single '龙头' label.
    """
    if not rows_out or k <= 0:
        return {
            "leader_preview_k": k,
            "leader_amount_top": [],
            "leader_pct_chg_top": [],
            "leader_volatility_top": [],
        }

    def slim(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": r.get("close"),
            "pct_chg": r.get("pct_chg"),
            "amount_yuan": r.get("amount_yuan"),
            "margin_to_ma": r.get("margin_to_ma"),
            "limit_up_seen": r.get("limit_up_seen", False),
            "limit_up_status": r.get("limit_up_status"),
            "limit_up_reasoning_available": r.get("limit_up_reasoning_available", False),
            "limit_up_reasoning_status": r.get("limit_up_reasoning_status"),
            "limit_up_reasoning_symbol": r.get("limit_up_reasoning_symbol"),
            "limit_up_reasoning_date": r.get("limit_up_reasoning_date"),
        }

    by_amt = sorted(rows_out, key=lambda r: -(r.get("amount_yuan") or 0.0))[:k]
    by_pct = sorted(
        rows_out,
        key=lambda r: r.get("pct_chg") if r.get("pct_chg") is not None else float("-inf"),
        reverse=True,
    )[:k]
    with_pct = [r for r in rows_out if r.get("pct_chg") is not None]
    by_abs = sorted(with_pct, key=lambda r: abs(float(r.get("pct_chg") or 0.0)), reverse=True)[:k]
    return {
        "leader_preview_k": k,
        "leader_amount_top": [slim(r) for r in by_amt],
        "leader_pct_chg_top": [slim(r) for r in by_pct],
        "leader_volatility_top": [slim(r) for r in by_abs],
    }


def _normalize_ts_code(symbol: str) -> Optional[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return None
    if "." in raw:
        return raw
    bare = raw.split(".")[0]
    if len(bare) != 6:
        return raw
    if bare.startswith(("30", "00", "12")):
        return f"{bare}.SZ"
    if bare.startswith(("60", "68", "11")):
        return f"{bare}.SH"
    if bare.startswith(("8", "4", "92")):
        return f"{bare}.BJ"
    return raw


def _attach_limit_up_reasoning_meta(data_db, rows: List[Dict[str, Any]], trade_day: str) -> None:
    """Annotate contributor rows with limit-up calendar and available LLM reasoning status.

    ``quant_analyzer.limit_up_analysis`` is the single source for LLM
    reasoning.  ``daily_limit_up`` may contain structural facts or older
    fallback text, but contributor deep-dive links should not depend on it.
    """
    if not rows or not trade_day:
        return

    symbols = [str(r.get("symbol") or "").strip() for r in rows if r.get("symbol")]
    ts_code_to_symbol: Dict[str, str] = {}
    for sym in symbols:
        ts_code = _normalize_ts_code(sym)
        if ts_code:
            ts_code_to_symbol.setdefault(ts_code, sym)

    if not ts_code_to_symbol:
        return

    for row in rows:
        row["limit_up_seen"] = False
        row["limit_up_status"] = None
        row["limit_up_reasoning_available"] = False
        row["limit_up_reasoning_status"] = None
        row["limit_up_reasoning_symbol"] = None
        row["limit_up_reasoning_date"] = None

    reasoning_by_symbol: Dict[str, Dict[str, Any]] = {}
    try:
        analysis_col = get_analyzer_db()["limit_up_analysis"]
        for doc in analysis_col.find(
            {"date": trade_day, "symbol": {"$in": list(ts_code_to_symbol.keys())}},
            {"_id": 0, "symbol": 1, "date": 1},
        ):
            doc_symbol = str(doc.get("symbol") or "")
            orig = ts_code_to_symbol.get(doc_symbol)
            if not orig:
                continue
            reasoning_by_symbol[orig] = {
                "limit_up_seen": True,
                "limit_up_status": "reasoned",
                "limit_up_reasoning_available": True,
                "limit_up_reasoning_status": "available",
                "limit_up_reasoning_symbol": doc_symbol,
                "limit_up_reasoning_date": str(doc.get("date") or trade_day),
            }
    except Exception:
        reasoning_by_symbol = {}

    for row in rows:
        sym = str(row.get("symbol") or "")
        if sym in reasoning_by_symbol:
            row.update(reasoning_by_symbol[sym])


def _sector_contributors_payload_daily(
    data_db,
    *,
    level: str,
    sector_code: str,
    trade_date: str,
    ma_period: int,
    top_n: int,
) -> Dict[str, Any]:
    members = _resolve_sector_members(level)
    members = {s: m for s, m in members.items() if m.get("sector_code") == sector_code}
    if not members:
        return {"success": False, "error": "No members for sector", "rows": []}

    sector_name = next((m.get("sector_name", "") for m in members.values()), "")
    symbols = sorted(members.keys())
    volume_price = data_db["volume_price"]
    lookback_start = trade_date
    prev_days = _previous_trade_dates(volume_price, trade_date, ma_period - 1)
    if prev_days:
        lookback_start = prev_days[0]

    cursor = volume_price.find(
        {
            "symbol": {"$in": symbols},
            "trade_date": {"$gte": lookback_start, "$lte": trade_date},
            "close": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1, "pct_chg": 1, "amount": 1},
    ).sort([("symbol", 1), ("trade_date", 1)])

    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in cursor:
        sym = str(doc.get("symbol") or "")
        if sym in members:
            rows_by_symbol[sym].append(doc)

    names = _load_stock_names_map(data_db, symbols)
    rows_out: List[Dict[str, Any]] = []
    for sym in symbols:
        seq = rows_by_symbol.get(sym, [])
        if not seq:
            continue
        idx = None
        for i, row in enumerate(seq):
            if str(row.get("trade_date") or "") == trade_date:
                idx = i
                break
        if idx is None:
            continue
        window = seq[max(0, idx - ma_period + 1): idx + 1]
        closes = [_num_or_none(r.get("close")) for r in window]
        closes = [c for c in closes if c is not None]
        if len(closes) < ma_period:
            continue
        close_t = _num_or_none(seq[idx].get("close"))
        if close_t is None:
            continue
        ma_line = sum(closes) / len(closes)
        above_ma = close_t > ma_line
        pct = _num_or_none(seq[idx].get("pct_chg"))
        amt_k = _num_or_none(seq[idx].get("amount"))
        amount_yuan = amt_k * 1000.0 if amt_k is not None else None
        rows_out.append(
            {
                "symbol": sym,
                "name": names.get(sym, sym),
                "close": close_t,
                "pct_chg": pct,
                "amount_yuan": amount_yuan,
                "ma": ma_line,
                "above_ma": above_ma,
                "margin_to_ma": (close_t - ma_line) if ma_line is not None else None,
            }
        )

    _attach_limit_up_reasoning_meta(data_db, rows_out, trade_date)

    above_rows = [r for r in rows_out if r.get("above_ma")]
    below_rows = [r for r in rows_out if not r.get("above_ma")]
    above_rows.sort(
        key=lambda r: (
            -(r.get("amount_yuan") or 0.0),
            -(r.get("pct_chg") or 0.0),
            -(r.get("margin_to_ma") or 0.0),
        )
    )
    below_rows.sort(
        key=lambda r: (
            (r.get("pct_chg") if r.get("pct_chg") is not None else 0.0),
            -(r.get("amount_yuan") or 0.0),
        )
    )

    total = len(rows_out)
    above_ct = len(above_rows)
    previews = _build_sector_leader_previews(rows_out, k=5)
    return {
        "success": True,
        "mode": "daily",
        "level": level,
        "sector_code": sector_code,
        "sector_name": sector_name,
        "trade_date": trade_date,
        "ma_period": ma_period,
        "total_members": len(symbols),
        "computed": total,
        "above_ma_count": above_ct,
        "yang_spectrum": (above_ct / total) if total else None,
        "top_above_ma": above_rows[:top_n],
        "top_below_ma": below_rows[:top_n],
        **previews,
    }


def _sector_contributors_payload_realtime(
    data_db,
    *,
    level: str,
    sector_code: str,
    snapshot_time: str,
    ma_period: int,
    top_n: int,
) -> Dict[str, Any]:
    trade_day = str(snapshot_time)[:8]
    members = _resolve_sector_members(level)
    members = {s: m for s, m in members.items() if m.get("sector_code") == sector_code}
    if not members:
        return {"success": False, "error": "No members for sector", "rows": []}

    sector_name = next((m.get("sector_name", "") for m in members.values()), "")
    symbols = sorted(members.keys())
    volume_price = data_db["volume_price"]
    lookback_start = trade_day
    prev_days = _previous_trade_dates(volume_price, trade_day, ma_period - 1)
    if prev_days:
        lookback_start = prev_days[0]
    need_prev = ma_period - 1

    cursor_vp = volume_price.find(
        {
            "symbol": {"$in": symbols},
            "trade_date": {"$gte": lookback_start, "$lt": trade_day},
            "close": {"$exists": True, "$ne": None},
        },
        {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1},
    ).sort([("symbol", 1), ("trade_date", 1)])

    closes_before: Dict[str, List[float]] = defaultdict(list)
    for doc in cursor_vp:
        sym = str(doc.get("symbol") or "")
        c = _num_or_none(doc.get("close"))
        if sym in members and c is not None:
            closes_before[sym].append(c)

    sum_prev_by_symbol: Dict[str, Optional[float]] = {}
    for sym in symbols:
        seq = closes_before.get(sym, [])
        if len(seq) < need_prev:
            sum_prev_by_symbol[sym] = None
        else:
            sum_prev_by_symbol[sym] = float(sum(seq[-need_prev:]))

    rt_col = realtime_db["volume_price_realtime_latest"]
    rt_docs = list(
        rt_col.find(
            {"trade_date": trade_day, "symbol": {"$in": symbols}},
            {
                "_id": 0,
                "symbol": 1,
                "close": 1,
                "pct_chg": 1,
                "volume": 1,
                "amount": 1,
                "realtime_amount_yuan": 1,
            },
        )
    )
    rt_by_symbol = {str(d.get("symbol")): d for d in rt_docs if d.get("symbol")}

    names = _load_stock_names_map(data_db, symbols)
    rows_out: List[Dict[str, Any]] = []
    for sym in symbols:
        doc = rt_by_symbol.get(sym)
        if not doc:
            continue
        p = _num_or_none(doc.get("close"))
        sum_prev = sum_prev_by_symbol.get(sym)
        if p is None or sum_prev is None:
            continue
        ma_line = (sum_prev + p) / ma_period
        above_ma = p > ma_line
        rows_out.append(
            {
                "symbol": sym,
                "name": names.get(sym, sym),
                "close": p,
                "pct_chg": _num_or_none(doc.get("pct_chg")),
                "amount_yuan": _realtime_amount_yuan_from_doc(doc),
                "volume": doc.get("volume"),
                "ma": ma_line,
                "above_ma": above_ma,
                "margin_to_ma": (p - ma_line),
            }
        )

    _attach_limit_up_reasoning_meta(data_db, rows_out, trade_day)

    above_rows = [r for r in rows_out if r.get("above_ma")]
    below_rows = [r for r in rows_out if not r.get("above_ma")]
    above_rows.sort(
        key=lambda r: (
            -(r.get("amount_yuan") or 0.0),
            -(r.get("pct_chg") or 0.0),
            -(r.get("margin_to_ma") or 0.0),
        )
    )
    below_rows.sort(
        key=lambda r: (
            (r.get("pct_chg") if r.get("pct_chg") is not None else 0.0),
            -(r.get("amount_yuan") or 0.0),
        )
    )

    total = len(rows_out)
    above_ct = len(above_rows)
    previews = _build_sector_leader_previews(rows_out, k=5)
    return {
        "success": True,
        "mode": "realtime",
        "level": level,
        "sector_code": sector_code,
        "sector_name": sector_name,
        "snapshot_time": snapshot_time,
        "trade_day": trade_day,
        "ma_period": ma_period,
        "total_members": len(symbols),
        "computed": total,
        "above_ma_count": above_ct,
        "yang_spectrum": (above_ct / total) if total else None,
        "top_above_ma": above_rows[:top_n],
        "top_below_ma": below_rows[:top_n],
        **previews,
    }


@router.get(
    "/market-spectrum/sector-contributors",
    summary="板块贡献股拆解（按站上MA与成交排序）",
    tags=["Market"],
)
def get_market_spectrum_sector_contributors(
    mode: str = Query(..., pattern="^(daily|realtime)$", description="daily=日线收盘; realtime=实时日K快照"),
    level: str = Query("L1", pattern="^L[123]$", description="申万 L1、L2 或 L3"),
    sector_code: str = Query(..., min_length=1, description="板块指数代码，如 801080.SI"),
    trade_date: Optional[str] = Query(None, description="YYYYMMDD，mode=daily 时必填"),
    snapshot_time: Optional[str] = Query(None, description="YYYYMMDDHHMM，mode=realtime 时必填"),
    ma_period: int = Query(5, ge=2, le=30),
    top_n: int = Query(30, ge=5, le=150),
):
    """List sector member stocks with MA alignment consistent with sector spectrum tabs.

    * **daily**: same rolling window as ``/market-spectrum/sectors`` (``volume_price`` closes).
    * **realtime**: last ``ma_period-1`` ``volume_price`` closes **before** trade day plus
      today's snapshot close from ``volume_price_realtime_latest`` (aligned with data-engine
      realtime sector strength).
    """
    cache_key = "|".join(
        ["contrib", mode, level, sector_code, str(ma_period), str(top_n), trade_date or "", snapshot_time or ""]
    )
    cached = _get_sector_spectrum_cache(cache_key)
    if cached is not None:
        return cached

    data_db = get_data_db()
    if mode == "daily":
        td = _normalize_compact_date(trade_date or "")
        if len(td) != 8:
            raise HTTPException(status_code=400, detail="trade_date is required (YYYYMMDD) for mode=daily")
        payload = _sector_contributors_payload_daily(
            data_db,
            level=level,
            sector_code=sector_code,
            trade_date=td,
            ma_period=ma_period,
            top_n=top_n,
        )
    else:
        st = str(snapshot_time or "").strip()
        if len(st) < 12:
            raise HTTPException(
                status_code=400,
                detail="snapshot_time is required (YYYYMMDDHHMM) for mode=realtime",
            )
        payload = _sector_contributors_payload_realtime(
            data_db,
            level=level,
            sector_code=sector_code,
            snapshot_time=st,
            ma_period=ma_period,
            top_n=top_n,
        )

    if payload.get("success"):
        return _set_sector_spectrum_cache(cache_key, payload)
    return payload


@router.get('/market-spectrum-minute', summary="获取实时日K阴阳谱数据", tags=["Market"])
def get_market_spectrum_minute(
    start_time: str = Query(..., description="开始时间,格式YYYYMMDDHHMM"),
    end_time: str = Query(..., description="结束时间,格式YYYYMMDDHHMM"),
    ma_period: int = Query(5, description="均线周期,默认5"),
):
    """获取实时日K阴阳谱数据.

    数据来源:
      - market_spectrum_realtime_daily (实时日K快照阴阳谱)
    字段包括:
      - trade_date: YYYYMMDDHHMM
      - yang_spectrum / yin_spectrum
      - total_stocks
      - above_ma_count / below_ma_count
      - ma_period, granularity
    """
    data_collections = get_data_db()
    collection = data_collections.get('market_spectrum_realtime_daily')  # type: ignore

    if collection is None:
        raise HTTPException(status_code=500, detail="market spectrum collection not configured")

    query = {
        'trade_date': {'$gte': start_time, '$lte': end_time},
        'ma_period': ma_period,
        'granularity': 'daily_realtime'
    }
    projection = {
        '_id': 0,
        'trade_date': 1,
        'yang_spectrum': 1,
        'yin_spectrum': 1,
        'total_stocks': 1,
        'above_ma_count': 1,
        'below_ma_count': 1,
        'distribution': 1,
        'ma_period': 1,
        'granularity': 1,
        'snapshot_time': 1,
        'source_mode': 1,
        'coverage_ratio': 1,
        'quality_passed': 1,
    }
    cursor = collection.find(query, projection).sort('trade_date', 1)
    data = list(cursor)
    return {"data": data}


@router.get('/market-spectrum-realtime-daily/status', summary="获取实时日K阴阳谱同步状态", tags=["Market"])
def get_market_spectrum_realtime_daily_status(
    trade_date: str = Query(..., description="交易日期,格式YYYYMMDD"),
):
    """Return realtime daily K sync health and fallback decision."""
    td = trade_date.replace("-", "")[:8]
    if len(td) != 8:
        raise HTTPException(status_code=400, detail="trade_date 须为 YYYYMMDD 或 YYYY-MM-DD")

    collections = get_db_collections()
    pipeline_tasks = collections.get("pipeline_tasks")
    if pipeline_tasks is None:
        raise HTTPException(status_code=500, detail="pipeline_tasks collection not configured")

    sync_doc = pipeline_tasks.find_one(
        {"domain": "data_collection", "task_type": "a_daily_realtime_sync", "date": td},
        {"_id": 0},
    )
    compare_doc = pipeline_tasks.find_one(
        {"domain": "data_collection", "task_type": "a_daily_realtime_eod_compare", "date": td},
        {"_id": 0},
    )
    latest_spectrum = (
        get_data_db()["market_spectrum_realtime_daily"]
        .find_one({"snapshot_trade_date": td}, {"_id": 0}, sort=[("snapshot_time", -1)])
    )
    return {
        "success": True,
        "trade_date": td,
        "sync_status": sync_doc,
        "compare_status": compare_doc,
        "latest_spectrum": latest_spectrum,
    }

@router.get(
    "/theme-lag-panel",
    summary="主题补涨候选 + 叙事（只读）",
    tags=["Market"],
)
def get_theme_lag_panel(
    trade_date: str = Query(..., description="交易日期 YYYYMMDD 或 YYYY-MM-DD"),
    index_store_key: str = Query("csi1000", description="index_constituents.index_code，如 csi1000 或 csi2000"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    登录可读：``quant_data.theme_lag_candidates``、同锚定日下的
    ``quant_data.theme_lag_candidate_validation``（按 symbol 合并 P₀、退出日/价、已实现收益率），以及
    ``finance.analysis_tasks`` 中 ``theme_lag_narrative`` 最新一条（按更新时间），不校验任务属主。
    """
    _ = current_user
    td = _theme_lag_trade_date_compact(trade_date)
    sk = (index_store_key or "csi1000").strip() or "csi1000"

    data_db = get_data_db()
    cand_col = data_db.get("theme_lag_candidates")
    if cand_col is None:
        raise HTTPException(status_code=500, detail="theme_lag_candidates 集合不可用")

    cand_cursor = cand_col.find({"trade_date": td, "index_store_key": sk}).sort(
        [("bucket_rank", 1), ("symbol", 1)]
    )
    candidates: List[Dict[str, Any]] = []
    for doc in cand_cursor:
        row = dict(doc)
        if "_id" in row:
            row["_id"] = str(row["_id"])
        for k, v in list(row.items()):
            if isinstance(v, datetime):
                row[k] = v.isoformat()
        candidates.append(row)

    exit_by_symbol: Dict[str, Dict[str, Any]] = {}
    val_col = data_db.get("theme_lag_candidate_validation")
    if val_col is not None:
        vdoc = val_col.find_one({"anchor_trade_date": td, "index_store_key": sk})
        if vdoc and isinstance(vdoc.get("per_symbol"), list):
            for p in vdoc["per_symbol"]:
                if not isinstance(p, dict):
                    continue
                sym = str(p.get("symbol") or "").strip()
                if not sym:
                    continue
                exit_by_symbol[sym] = {
                    "p0": _theme_lag_json_float(p.get("p0")),
                    "return_pct": _theme_lag_json_float(p.get("return_pct")),
                    "exit_trade_date": p.get("exit_trade_date"),
                    "exit_price": _theme_lag_json_float(p.get("exit_price")),
                    "outcome": p.get("outcome"),
                }

    for row in candidates:
        sym = str(row.get("symbol") or "").strip()
        v = exit_by_symbol.get(sym)
        if v:
            row["validation_p0"] = v.get("p0")
            row["validation_return_pct"] = v.get("return_pct")
            row["validation_exit_trade_date"] = v.get("exit_trade_date")
            row["validation_exit_price"] = v.get("exit_price")
            row["validation_outcome"] = v.get("outcome")
        else:
            row["validation_p0"] = None
            row["validation_return_pct"] = None
            row["validation_exit_trade_date"] = None
            row["validation_exit_price"] = None
            row["validation_outcome"] = None

    tasks_col = get_db_collections()["analysis_tasks"]
    cur = tasks_col.find(
        {
            "$or": [{"type": "theme_lag_narrative"}, {"task_type": "theme_lag_narrative"}],
            "params.trade_date": td,
            "params.index_store_key": sk,
        }
    ).sort([("updated_at", -1), ("_id", -1)]).limit(1)
    task = next(cur, None)
    # 防御：历史或异常文档若 params.index_store_key 与查询不一致，不当作本池叙事
    if task:
        p = task.get("params") if isinstance(task.get("params"), dict) else {}
        if str(p.get("index_store_key") or "").strip() != sk:
            task = None

    narrative: Optional[Dict[str, Any]] = None
    if task:
        tid = str(task["_id"])
        st = str(task.get("status") or "").lower()
        ca = task.get("completed_at")
        ca_out = ca.isoformat() if isinstance(ca, datetime) else (str(ca) if ca else None)
        ua = task.get("updated_at")
        ua_out = ua.isoformat() if isinstance(ua, datetime) else (str(ua) if ua else None)
        res = task.get("result") if isinstance(task.get("result"), dict) else {}
        if st == "completed":
            narrative = {
                "status": "completed",
                "task_id": tid,
                "narrative_markdown": res.get("narrative_markdown"),
                "model": res.get("model"),
                "completed_at": ca_out,
                "updated_at": ua_out,
            }
        else:
            narrative = {
                "status": st,
                "task_id": tid,
                "error": task.get("error"),
                "updated_at": ua_out,
            }

    return {
        "success": True,
        "trade_date": td,
        "index_store_key": sk,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "narrative": narrative,
    }


@router.get("/market-quotes", summary="获取实时市场报价(Lightweight)", tags=["Market"])
def get_realtime_quotes(symbols: str = Query(..., description="逗号分隔的股票代码，如 ^IXIC,FXI")):
    """
    获取指定代码的最新实时报价，用于前端 Watchdog 监控。
    数据由 quant-data-engine 侧独立脚本 (run_global_market_sync) 写入 DATA_DB_NAME（默认 quant_data）下的 global_market_quotes 集合。
    返回: { symbol: { price: float, change_pct: float, timestamp: str } }
    """
    symbol_list = [s.strip() for s in symbols.split(',') if s.strip()]
    if not symbol_list:
        return {"success": True, "data": {}}
        
    try:
        col = get_data_db()["global_market_quotes"]
        docs = list(col.find({"symbol": {"$in": symbol_list}}))
        
        result = {}
        for doc in docs:
            sym = doc.get("symbol")
            if sym:
                result[sym] = {
                    "price": doc.get("price", 0.0),
                    "change_pct": doc.get("change_pct", 0.0),
                    "timestamp": doc.get("timestamp", "")
                }
                
        return {"success": True, "data": result}
        
    except Exception as e:
        print(f"[MarketWatchdog] API Error: {e}")
        return {"success": False, "error": str(e)}