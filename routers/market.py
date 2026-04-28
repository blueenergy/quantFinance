# 新增：获取market spectrum的API接口
from typing import Optional, Dict, Any, List, Mapping
from fastapi import APIRouter, Query, Depends, HTTPException
from models.all_models import MarketAnalysisRequest
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db, get_data_db
from market_analysis_cache import market_analysis_cache
from datetime import date, datetime
from services.tushare_cached import get_latest_trade_date
from external_data import external_market_data
from utils.stock_utils import serialize_doc
from bson import ObjectId
from services.analysis_tasks import build_task_doc

router = APIRouter()

# GlobalMarketBrief 仪表盘实际用到的标的（与 Vue 模板一致）；默认裁剪以减小序列化与传输耗时。
_DASHBOARD_MARKET_SYMBOLS: Dict[str, List[str]] = {
    "indices": ["^IXIC", "^GSPC", "^DJI", "^RUT", "^VIX"],
    "us_index_futures": ["NQ=F", "ES=F", "YM=F", "RTY=F"],
    "china_concepts": ["FXI", "2823.HK", "PGJ", "BABA", "PDD", "JD", "NIO"],
    "tech_giants": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "AMD", "INTC"],
    "commodities": ["GC=F", "SI=F", "CL=F"],
    "forex": ["CNH=X", "DX-Y.NYB"],
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
    "forex": {"DX-Y.NYB": "USD Index", "CNH=X": "USD/CNH (Offshore)"},
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


def fetch_market_analysis_result(analysis_date: Optional[str] = None) -> Dict[str, Any]:
    """Fetch cached market analysis for frontend.

    - If analysis_date is provided: return the cached result for that specific date.
    - If not provided: on trading days, return today's cached result; 
      on non-trading days, fall back to the latest available trading day.
    """
    target_day = date.today()
    is_explicit_date = bool(analysis_date)

    if analysis_date:
        target_day = date.fromisoformat(analysis_date)
        today_str = analysis_date
        print(f"[Market Analysis] Explicit date requested: {today_str}")
    else:
        today_str = target_day.strftime('%Y-%m-%d')
        print(f"[Market Analysis] No date provided, using default: {today_str}")

    is_trading_day = market_analysis_cache.is_trading_day(target_day)

    # Try to get the analysis for the target date
    cached = market_analysis_cache.get_cached_analysis(today_str)
    
    if cached:
        result = dict(cached.get('analysis_result', {}))
        result['analysis_date'] = cached.get('analysis_date', today_str)
        result['cache_info'] = {
            'from_cache': True,
            'cache_date': cached.get('analysis_date', today_str),
            'created_at': cached.get('created_at'),
            'reason': "返回当日缓存的大盘分析结果"
        }
        # Add success flag for frontend
        result['success'] = True
        return result

    # 非交易日：优先回退到最近交易日结果，避免用户误判系统异常
    if not is_trading_day:
        print(f"[Market Analysis] {today_str} is non-trading day, falling back to latest trading day")
        latest_analysis = market_analysis_cache.get_latest_trading_day_analysis()
        if latest_analysis:
            result = dict(latest_analysis.get('analysis_result', {}))
            result['analysis_date'] = latest_analysis['analysis_date']
            result['cache_info'] = {
                'from_cache': True,
                'cache_date': latest_analysis['analysis_date'],
                'created_at': latest_analysis.get('created_at'),
                'reason': f"今日非交易日，展示最近交易日({latest_analysis['analysis_date']})的分析结果"
            }
            result['success'] = True
            result['notice'] = f"{today_str} 为非交易日，已展示最近交易日数据。"
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
    return {
        "success": False,
        "error": f"{message_date} 的大盘分析尚未生成",
        "is_trading_day": True,
        "is_expected": True,
        "expected_schedule": ["10:00", "11:30", "15:30"],
        "user_tip": f"系统会在交易日 {schedule_text} 自动更新，请稍后刷新查看。"
    }

@router.post("/analyze-market")
async def analyze_market(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """Frontend endpoint: only fetch cached market analysis result.

    实际分析工作由后台 Celery 定时任务完成，这里不再触发 LLM 调用。
    支持可选的 date 参数。
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


@router.get("/global-market-dashboard")
async def get_global_market_dashboard(current_user: dict = Depends(get_current_active_user)):
    """GlobalMarketBrief 仪表盘：返回 MongoDB 中最新同步的全球行情（与 AI 分析报告解耦）。"""
    _ = current_user
    payload = read_global_market_dashboard_from_db()
    if payload.get("success"):
        return {"success": True, "data": payload}
    return {"success": False, "error": payload.get("error", "unknown")}


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


@router.get('/market-spectrum-minute', summary="获取分钟级市场阴阳谱数据", tags=["Market"])
def get_market_spectrum_minute(
    start_time: str = Query(..., description="开始时间,格式YYYYMMDDHHMM"),
    end_time: str = Query(..., description="结束时间,格式YYYYMMDDHHMM"),
    ma_period: int = Query(5, description="均线周期,默认5"),
    spectrum_type: str = Query("daily_realtime", alias="type", description="曲线类型: minute 或 daily_realtime")
):
    """获取分钟级市场阴阳谱数据.

    数据来源: market_spectrum_minute 集合, 字段包括:
      - trade_date: YYYYMMDDHHMM
      - yang_spectrum / yin_spectrum
      - total_stocks
      - above_ma_count / below_ma_count
      - ma_period, granularity 字段区分来源
    """
    data_collections = get_data_db()
    collection = data_collections.get('market_spectrum_minute')  # type: ignore

    if collection is None:
        raise HTTPException(status_code=500, detail="market_spectrum_minute collection not configured")

    # Map type -> granularity field stored in DB
    granularity = 'daily_realtime' if spectrum_type == 'daily_realtime' else 'minute'

    query = {
        'trade_date': {'$gte': start_time, '$lte': end_time},
        'ma_period': ma_period,
        'granularity': granularity
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
    }
    cursor = collection.find(query, projection).sort('trade_date', 1)
    data = list(cursor)
    return {"data": data}

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