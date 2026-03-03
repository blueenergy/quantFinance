# 新增：获取market spectrum的API接口
from typing import Optional, Dict, Any
from fastapi import APIRouter,Query, Depends, HTTPException
from models.all_models import MarketAnalysisRequest
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db
from market_analysis_cache import market_analysis_cache
from datetime import date, datetime
from external_data import external_market_data
from utils.stock_utils import serialize_doc
from bson import ObjectId
from models.analysis_task import TaskStatus

router = APIRouter()


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
            
        task_doc = {
            "type": "global_analysis",
            "status": "pending",
            "priority": 20, # High priority for manual trigger
            "user_id": user_id,
            "stock_name": "全球市场联动",
            "symbol": "GLOBAL",
            "params": {
                "date": today_str
            },
            "created_at": datetime.now(),
            "source": "manual_trigger"
        }
        
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
    if analysis_date:
        today_str = analysis_date
        print(f"[Market Analysis] Explicit date requested: {today_str}")
    else:
        today = date.today()
        today_str = today.strftime('%Y-%m-%d')
        print(f"[Market Analysis] No date provided, using default: {today_str}")

    # Try to get the analysis for the target date
    cached = market_analysis_cache.get_cached_analysis(today_str)
    
    if cached:
        result = cached['analysis_result']
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
    else:
        # ONLY fallback if NO date was explicitly requested
        if not analysis_date:
            print(f"[Market Analysis] {today_str} not found, falling back to latest trading day")
            latest_analysis = market_analysis_cache.get_latest_trading_day_analysis()
            if latest_analysis:
                result = latest_analysis['analysis_result']
                result['analysis_date'] = latest_analysis['analysis_date']
                result['cache_info'] = {
                    'from_cache': True,
                    'cache_date': latest_analysis['analysis_date'],
                    'created_at': latest_analysis.get('created_at'),
                    'reason': f"当日无分析，返回最近交易日({latest_analysis['analysis_date']})的结果"
                }
                result['success'] = True
                return result
        
        print(f"[Market Analysis] {today_str} not found and no fallback allowed")
        return {
            "success": False,
            "error": f"暂无 {today_str} 的历史大盘分析数据",
            "is_trading_day": market_analysis_cache.is_trading_day(date.fromisoformat(today_str)) if not analysis_date else True
        }

@router.post("/analyze-market")
async def analyze_market(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """Frontend endpoint: only fetch cached market analysis result.

    实际分析工作由后台 Celery 定时任务完成，这里不再触发 LLM 调用。
    支持可选的 date 参数。
    """
    return fetch_market_analysis_result(request.date)

@router.get("/global-analysis")
async def get_global_analysis(current_user: dict = Depends(get_current_active_user)):
    """获取最新的全球市场联动分析结果"""
    try:
        collections = get_db_collections()
        # Fetch the latest document from global_analysis collection
        # Sort by _id (desc) to get the most recently inserted document regardless of timestamp field
        doc = get_analyzer_db()["global_analysis"].find_one(
            sort=[("_id", -1)]
        )
        
        if not doc:
            return {
                "success": False,
                "error": "今日尚无全球市场分析数据"
            }
            
        # Serialize MongoDB document
        result = serialize_doc(doc)
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
        collections = get_db_collections()
        collection = collections["volume_price"] # type: ignore
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
    collections = get_db_collections()
    collection = collections['market_spectrum']
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
    collections = get_db_collections()
    collection = collections.get('market_spectrum_minute')  # type: ignore

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
    数据由 quant-data-engine 侧独立脚本 (run_global_market_sync) 写入 global_market_quotes 集合。
    返回: { symbol: { price: float, change_pct: float, timestamp: str } }
    """
    from db import get_collection
    
    symbol_list = [s.strip() for s in symbols.split(',') if s.strip()]
    if not symbol_list:
        return {"success": True, "data": {}}
        
    try:
        col = get_collection("global_market_quotes")
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


# -----------------------------
# Northbound Capital & Margin Trading (Direct Tushare API)
# -----------------------------

@router.get("/northbound-capital", summary="获取北向资金数据", tags=["Market"])
def get_northbound_capital(
    days: int = Query(30, description="获取最近N天的数据", ge=1, le=365)
):
    """获取沪深港通资金流向数据 (北向资金).
    
    数据来源: Tushare moneyflow_hsgt API (实时调用，非数据库存储)
    
    返回字段:
    - trade_date: 交易日期
    - ggt_ss: 港股通(上海)
    - ggt_sz: 港股通(深圳)
    - hgt: 沪股通(百万元)
    - sgt: 深股通(百万元)
    - north_money: 北向资金(百万元)
    - south_money: 南向资金(百万元)
    """
    from services.tushare_cached import get_northbound_capital as fetch_northbound
    return fetch_northbound(days)


@router.get("/margin-trading", summary="获取融资融券数据", tags=["Market"])
def get_margin_trading(
    days: int = Query(30, description="获取最近N天的数据", ge=1, le=365)
):
    """获取融资融券汇总数据.
    
    数据来源: Tushare margin API (实时调用，非数据库存储)
    
    返回字段:
    - trade_date: 交易日期
    - rzye: 融资余额(亿元)
    - rzmre: 融资买入额(亿元)
    - rzche: 融资偿还额(亿元)
    - rqye: 融券余额(亿元)
    - rqmcl: 融券卖出量(万股)
    - rzrqye: 融资融券余额(亿元)
    """
    from services.tushare_cached import get_margin_trading as fetch_margin
    return fetch_margin(days)