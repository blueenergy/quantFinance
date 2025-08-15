from fastapi import FastAPI, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from typing import List, Optional, Dict, Any
from bson import ObjectId
import os
import httpx
import json
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from datetime import timedelta, date
from dotenv import load_dotenv
from llm_service import llm_manager
from auth import (
    UserCreate, UserLogin, Token, UserResponse,
    authenticate_user, create_access_token, create_user,
    get_current_active_user, ACCESS_TOKEN_EXPIRE_MINUTES
)
from external_data import external_market_data
from market_analysis_cache import market_analysis_cache

# 加载环境变量
load_dotenv()

app = FastAPI()

# CORS middleware for Vue.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request models
class StockAnalysisRequest(BaseModel):
    symbol: str
    provider: Optional[str] = "openai"  # 默认使用OpenAI
    model: Optional[str] = None

@app.get("/")
def read_root():
    return {"message": "Welcome to the FastAPI MongoDB Data Service!"}

# 用户认证路由
@app.post("/api/register", response_model=UserResponse)
def register(user: UserCreate):
    """用户注册"""
    return create_user(user)

@app.post("/api/login", response_model=Token)
def login(user_data: UserLogin):
    """用户登录"""
    user = authenticate_user(user_data.username, user_data.password)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    
    # 准备用户响应数据
    user_response = UserResponse(
        id=user["id"],
        username=user["username"],
        email=user["email"],
        full_name=user.get("full_name"),
        is_active=user.get("is_active", True),
        created_at=user["created_at"]
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=user_response
    )

@app.get("/api/profile", response_model=UserResponse)
def get_profile(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户信息"""
    return UserResponse(
        id=current_user["id"],
        username=current_user["username"],
        email=current_user["email"],
        full_name=current_user.get("full_name"),
        is_active=current_user.get("is_active", True),
        created_at=current_user["created_at"]
    )

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
db = client["quant"]
collection = db["volume_price"]
stock_info_collection = db["stock_info"]  # 新增：股票信息集合

def serialize_doc(doc):
    doc["_id"] = str(doc["_id"])
    return doc

@app.get("/records/", response_model=List[dict])
def get_records(symbol: Optional[str] = Query(None), limit: int = 10, sort: Optional[str] = None):
    query = {}
    if symbol:
        query["symbol"] = symbol
    cursor = collection.find(query)
    if sort:
        # 支持 '-trade_date' 降序
        field = sort.lstrip('-')
        direction = -1 if sort.startswith('-') else 1
        cursor = cursor.sort(field, direction)
    docs = cursor.limit(limit)
    return [serialize_doc(doc) for doc in docs]

@app.get("/record/{record_id}")
def get_record(record_id: str):
    doc = collection.find_one({"_id": ObjectId(record_id)})
    if doc:
        return serialize_doc(doc)
    return {"error": "Not found"}

@app.get("/api/stock-info/{symbol}")
def get_stock_info(symbol: str):
    """获取股票基本信息（包括名称）"""
    try:
        # 先从股票信息集合获取
        stock_info = stock_info_collection.find_one({"symbol": symbol})
        if stock_info:
            return {
                "success": True,
                "data": {
                    "symbol": stock_info["symbol"],
                    "name": stock_info["name"],
                    "update_time": stock_info.get("update_time")
                }
            }
        
        # 如果没有找到，从价格数据中获取
        price_record = collection.find_one(
            {"symbol": symbol}, 
            sort=[("trade_date", -1)]
        )
        if price_record and "stock_name" in price_record:
            return {
                "success": True,
                "data": {
                    "symbol": symbol,
                    "name": price_record["stock_name"],
                    "update_time": price_record.get("update_time")
                }
            }
        
        return {
            "success": False,
            "message": f"未找到股票 {symbol} 的信息"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取股票信息失败: {str(e)}"
        }

@app.get("/api/watchlist-stocks")
def get_watchlist_stocks(symbols: str = Query(..., description="逗号分隔的股票代码列表")):
    """批量获取自选股的基本信息和最新价格"""
    try:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
        result = []
        
        for symbol in symbol_list:
            # 获取最新价格数据
            latest_record = collection.find_one(
                {"symbol": symbol},
                sort=[("trade_date", -1)]
            )
            
            if latest_record:
                stock_data = {
                    "symbol": symbol,
                    "name": latest_record.get("stock_name", symbol),
                    "close": latest_record.get("close"),
                    "trade_date": latest_record.get("trade_date"),
                    "volume": latest_record.get("volume"),
                    "high": latest_record.get("high"),
                    "low": latest_record.get("low"),
                    "open": latest_record.get("open")
                }
                
                # 计算涨跌幅（需要前一日数据）
                prev_record = collection.find_one(
                    {"symbol": symbol, "trade_date": {"$lt": latest_record["trade_date"]}},
                    sort=[("trade_date", -1)]
                )
                
                if prev_record:
                    change = latest_record["close"] - prev_record["close"]
                    change_percent = (change / prev_record["close"]) * 100
                    stock_data["change"] = round(change, 2)
                    stock_data["change_percent"] = round(change_percent, 2)
                else:
                    stock_data["change"] = 0
                    stock_data["change_percent"] = 0
                
                result.append(stock_data)
        
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取自选股信息失败: {str(e)}"
        }

@app.post("/api/analyze-market")
async def analyze_market(
    request: dict,
    current_user: dict = Depends(get_current_active_user)
):
    """AI市场分析 (需要登录) - 带缓存和交易日检查"""
    try:
        today = date.today()
        analysis_date = today.strftime('%Y-%m-%d')
        
        # 1. 检查是否需要进行市场分析
        should_analyze, reason = market_analysis_cache.should_analyze_market(today)
        
        if not should_analyze:
            print(f"[市场分析] {reason}")
            
            # 如果是非交易日，尝试返回最近交易日的分析结果
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
            
            # 如果是交易日但有缓存，直接返回缓存结果
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
        
        # 2. 需要进行新的市场分析
        print(f"[市场分析] {reason}，开始新的分析...")
        
        # 获取市场数据
        market_data = get_market_data_for_analysis()
        if not market_data:
            raise HTTPException(status_code=404, detail="未找到市场数据")
        
        # 获取LLM服务
        llm_service = llm_manager.get_service(
            provider=os.getenv('DEFAULT_LLM_PROVIDER', 'openai'),
            model=os.getenv('DEFAULT_MODEL', 'gpt-3.5-turbo')
        )
        
        # 调用LLM分析市场
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
            
            # 3. 保存分析结果到缓存
            cache_success = market_analysis_cache.save_analysis(result, analysis_date)
            if cache_success:
                print(f"[市场分析] 分析结果已缓存到数据库")
            else:
                print(f"[市场分析] 缓存保存失败，但分析正常返回")
            
            return result
        else:
            # 分析失败时返回错误
            result = {
                "success": False,
                "error": analysis_result.get("error", "分析失败"),
                "error_type": analysis_result.get("error_type", "Unknown")
            }
            return result
        
    except Exception as e:
        print(f"[市场分析] 异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"市场分析失败: {str(e)}")

def get_market_data_for_analysis() -> Optional[Dict[str, Any]]:
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

@app.post("/api/analyze-stock")
async def analyze_stock(
    request: StockAnalysisRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """使用外部LLM API分析股票数据 (需要登录)"""
    try:
        # 获取股票数据
        stock_data = get_stock_data_for_analysis(request.symbol)
        
        if not stock_data:
            raise HTTPException(status_code=404, detail=f"未找到股票 {request.symbol} 的数据")
        
        # 获取LLM服务
        llm_service = llm_manager.get_service(
            provider=request.provider or os.getenv('DEFAULT_LLM_PROVIDER', 'openai'),
            model=request.model or os.getenv('DEFAULT_MODEL', 'gpt-3.5-turbo')
        )
        
        # 调用LLM分析
        analysis_result = await llm_service.analyze_stock(request.symbol, stock_data)
        
        # 记录分析历史
        analysis_result["user_id"] = current_user["id"]
        analysis_result["username"] = current_user["username"]
        
        return analysis_result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")

def get_stock_data_for_analysis(symbol: str) -> Optional[Dict[str, Any]]:
    """获取用于分析的股票数据"""
    try:
        # 获取最新记录
        latest_record = collection.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        )
        
        if not latest_record:
            return None
        
        # 获取最近30天的历史记录
        historical_records = list(collection.find(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        ).limit(30))
        
        return {
            "symbol": symbol,
            "latest_record": serialize_doc(latest_record),
            "historical_records": [serialize_doc(doc) for doc in historical_records]
        }
    except Exception as e:
        print(f"获取股票数据失败: {e}")
        return None

@app.get("/api/llm-providers")
def get_available_llm_providers():
    """获取可用的LLM服务提供商"""
    try:
        providers = llm_manager.list_available_providers()
        return {
            "providers": providers,
            "default": os.getenv('DEFAULT_LLM_PROVIDER', 'openai')
        }
    except Exception as e:
        return {"providers": [], "error": str(e)}

# Run with: uvicorn main:app --reload