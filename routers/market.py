# 新增：获取market spectrum的API接口
from typing import Optional, Dict, Any
from fastapi import APIRouter,Query, Depends, HTTPException
from models.all_models import MarketAnalysisRequest
from auth import get_current_active_user
from db import get_db_collections
from market_analysis_cache import market_analysis_cache
from llm_service import llm_manager
from datetime import date,datetime
from external_data import external_market_data
from utils.stock_utils import serialize_doc
from bson import ObjectId
router = APIRouter()


def fetch_market_analysis_result() -> Dict[str, Any]:
    """Fetch cached market analysis for frontend.

    - On trading days: return only today's cached result (or a clear message if none).
    - On non-trading days: fall back to the latest trading day's cached result.
    """
    today = date.today()
    today_str = today.strftime('%Y-%m-%d')

    # 只从缓存读取结果：交易日只看当日缓存，非交易日才回退到最近交易日
    if market_analysis_cache.is_trading_day(today):
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
            return result
        else:
            return {
                "success": False,
                "error": "今日尚无大盘分析，请等待系统定时任务生成",
                "is_trading_day": True
            }
    else:
        latest_analysis = market_analysis_cache.get_latest_trading_day_analysis()
        if latest_analysis:
            result = latest_analysis['analysis_result']
            result['analysis_date'] = latest_analysis['analysis_date']
            result['cache_info'] = {
                'from_cache': True,
                'cache_date': latest_analysis['analysis_date'],
                'created_at': latest_analysis.get('created_at'),
                'reason': f"非交易日，返回最近交易日({latest_analysis['analysis_date']})的分析结果"
            }
            return result
        else:
            return {
                "success": False,
                "error": "非交易日，且最近交易日尚无大盘分析结果",
                "is_trading_day": False
            }

@router.post("/analyze-market")
async def analyze_market(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """Frontend endpoint: only fetch cached market analysis result.

    实际分析工作由后台 Celery 定时任务完成，这里不再触发 LLM 调用。
    """
    return fetch_market_analysis_result()

@router.get("/global-analysis")
async def get_global_analysis(current_user: dict = Depends(get_current_active_user)):
    """获取最新的全球市场联动分析结果"""
    try:
        collections = get_db_collections()
        # Fetch the latest document from global_analysis collection
        doc = collections["global_analysis"].find_one(
            sort=[("timestamp", -1)]
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
        'above_ma5_count': 1,
        'below_ma5_count': 1
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
        'ma_period': 1,
        'granularity': 1,
    }
    cursor = collection.find(query, projection).sort('trade_date', 1)
    data = list(cursor)
    return {"data": data}