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

@router.post("/analyze-market")
async def analyze_market(request: MarketAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    today = date.today()
    analysis_date = today.strftime('%Y-%m-%d')

    # 强制刷新直接分析
    if getattr(request, 'force_refresh', False):
        pass  # 跳过缓存逻辑
    else:
        # 1. 检查是否需要进行市场分析
        should_analyze, reason = market_analysis_cache.should_analyze_market(today)
        if not should_analyze:
            # 非交易日，尝试返回最近交易日的分析结果
            if "非交易日" in reason:
                latest_analysis = market_analysis_cache.get_latest_trading_day_analysis()
                if latest_analysis:
                    result = latest_analysis['analysis_result']
                    result['cache_info'] = {
                        'from_cache': True,
                        'cache_date': latest_analysis['analysis_date'],
                        'reason': f"{reason}，返回最近交易日({latest_analysis['analysis_date']})的分析结果"
                    }
                    return result
                else:
                    return {
                        "success": False,
                        "error": f"{reason}，且未找到最近的交易日分析结果",
                        "is_trading_day": False
                    }
            # 交易日但有缓存，直接返回缓存结果
            elif "缓存" in reason:
                cached_analysis = market_analysis_cache.get_cached_analysis(analysis_date)
                if cached_analysis:
                    result = cached_analysis['analysis_result']
                    result['cache_info'] = {
                        'from_cache': True,
                        'cache_date': analysis_date,
                        'reason': reason
                    }
                    return result

    # 获取市场数据
    market_data = fetch_market_data()  # 或 get_market_data_for_analysis()
    if not market_data:
        raise HTTPException(status_code=404, detail="未找到市场数据")

    # 获取用户激活的LLM配置
    collections = get_db_collections()
    users_col = collections["users"]
    
    user_doc = users_col.find_one({"_id": ObjectId(current_user["id"])})
    if not user_doc:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    active_config_id = user_doc.get("active_llm_config_id")
    llm_configs = user_doc.get("llm_configs", [])
    
    # 如果没有激活配置但有可用配置，自动激活第一个
    if not active_config_id and llm_configs:
        active_config_id = str(llm_configs[0].get("_id"))
        # 更新用户文档以激活该配置
        users_col.update_one(
            {"_id": ObjectId(current_user["id"])},
            {"$set": {"active_llm_config_id": active_config_id}}
        )
    
    if not active_config_id:
        raise HTTPException(status_code=400, detail="请先在用户配置中配置一个LLM配置")
    
    # 查找激活的配置
    active_config = None
    for config in llm_configs:
        if str(config.get("_id")) == active_config_id:
            active_config = config
            break
    
    if not active_config:
        raise HTTPException(status_code=404, detail="激活的LLM配置不存在")
    
    # 获取配置参数
    user_llm_provider = active_config.get("provider")
    user_llm_api_key = active_config.get("api_key")
    user_llm_base_url = active_config.get("base_url")
    user_llm_model = active_config.get("model")
    
    # 获取LLM服务，使用用户特定的配置
    llm_service = llm_manager.get_service(
        provider=user_llm_provider,
        model=user_llm_model,
        api_key=user_llm_api_key,
        base_url=user_llm_base_url
    )
    analysis_result = await llm_service.analyze_market(market_data)

    if analysis_result.get("success"):
        # 提取分析内容
        analysis = analysis_result.get("analysis", {})
        # 构建结果
        result = {
            "success": True,
            "analysis_date": market_data.get("analysis_date", analysis_date),
            "mood": analysis.get("mood", "市场情绪复杂"),
            "summary": analysis.get("summary", "市场分析结果"),
            "keyPoints": analysis.get("keyPoints", []),
            "outlook": analysis.get("outlook", "请关注市场动态"),
            "riskLevel": analysis.get("riskLevel", "medium"),
            "riskNote": analysis.get("riskNote", "注意投资风险"),
            "analysis_type": "market_overview",
            "provider": analysis_result.get("provider"),
            "model": analysis_result.get("model"),
            "cache_info": {
                'from_cache': False,
                'cache_date': analysis_date,
                'reason': "新生成的分析结果"
            }
        }
        # 保存分析结果到缓存
        cache_success = market_analysis_cache.save_analysis(result, analysis_date)
        if cache_success:
            print(f"[市场分析] 分析结果已缓存到数据库")
        else:
            print(f"[市场分析] 缓存保存失败，但分析正常返回")
        return result
    else:
        return {
            "success": False,
            "error": analysis_result.get("error", "分析失败"),
            "error_type": analysis_result.get("error_type", "Unknown")
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