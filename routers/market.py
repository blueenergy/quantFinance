from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from models.all_models import MarketAnalysisRequest
from auth import get_current_active_user
from db import get_db_collections
from market_analysis_cache import market_analysis_cache
from llm_service import llm_manager
from datetime import date,datetime
from external_data import external_market_data
from utils.stock_utils import serialize_doc
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

    # 获取LLM服务
    llm_service = llm_manager.get_service()
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
    
