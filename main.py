from fastapi import FastAPI, Query, HTTPException, Depends, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from typing import List, Optional, Dict, Any
from bson import ObjectId
import os
import httpx
import json
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from datetime import timedelta, date, datetime
from dotenv import load_dotenv
import efinance as ef
from llm_service import llm_manager
from auth import (
    UserCreate, UserLogin, Token, UserResponse,
    authenticate_user, create_access_token, create_user,
    get_current_active_user, get_current_admin_user, 
    log_login_attempt, log_admin_action, create_admin_user,
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from external_data import external_market_data
from market_analysis_cache import market_analysis_cache

# 加载环境变量
load_dotenv()

app = FastAPI()
router = APIRouter()

# CORS middleware for Vue.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", 
        "http://localhost:5173",
        "http://10.181.200.185:5173",
        "http://10.181.200.185:3000"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 添加OPTIONS预检请求处理器
@app.options("/api/user/watchlist")
async def watchlist_options():
    """处理自选股相关的OPTIONS预检请求"""
    return {"message": "OK"}

@app.options("/api/user/watchlist/add")
async def add_watchlist_options():
    """处理添加自选股的OPTIONS预检请求"""
    return {"message": "OK"}

@app.options("/api/user/watchlist/remove/{symbol}")
async def remove_watchlist_options(symbol: str):
    """处理移除自选股的OPTIONS预检请求"""
    return {"message": "OK"}

@app.options("/api/user/watchlist-stocks")
async def watchlist_stocks_options():
    """处理自选股详情的OPTIONS预检请求"""
    return {"message": "OK"}

# Request models
class StockAnalysisRequest(BaseModel):
    symbol: str
    provider: Optional[str] = "openai"  # 默认使用OpenAI
    model: Optional[str] = None

class WatchlistRequest(BaseModel):
    symbol: str

class WatchlistUpdateRequest(BaseModel):
    symbols: List[str]

class MarketAnalysisRequest(BaseModel):
    type: str
    force_refresh: bool = False

@app.get("/")
def read_root():
    return {"message": "Welcome to the FastAPI MongoDB Data Service!"}

# 用户认证路由
@app.post("/api/register", response_model=UserResponse)
def register(user: UserCreate):
    """用户注册"""
    return create_user(user)

@app.post("/api/login", response_model=Token)
def login(user_data: UserLogin, request: Request):
    """用户登录"""
    # 获取客户端信息
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    
    user = authenticate_user(user_data.username, user_data.password)
    if not user:
        # 记录失败的登录尝试
        log_login_attempt(
            username=user_data.username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="Incorrect username or password"
        )
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 记录成功的登录
    log_login_attempt(
        username=user_data.username,
        ip_address=ip_address,
        user_agent=user_agent,
        success=True
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
        is_admin=user.get("is_admin", False),
        created_at=user["created_at"],
        last_login=user.get("last_login")
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
        is_admin=current_user.get("is_admin", False),
        created_at=current_user["created_at"],
        last_login=current_user.get("last_login")
    )

# 用户自选股管理API
@app.get("/api/user/watchlist")
def get_user_watchlist(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户的自选股列表"""
    try:
        user_id = current_user["id"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        
        if watchlist_doc:
            symbols = watchlist_doc.get("symbols", [])
        else:
            symbols = []
            
        return {
            "success": True,
            "data": {
                "symbols": symbols,
                "count": len(symbols)
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取自选股列表失败: {str(e)}"
        }

@app.post("/api/user/watchlist/add")
def add_to_watchlist(
    request: WatchlistRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """添加股票到自选股"""
    try:
        user_id = current_user["id"]
        symbol = request.symbol.strip().upper()
        
        # 检查股票代码是否有效（优先检查stock_info，如果没有则检查volume_price）
        stock_exists = stock_info_collection.find_one({"symbol": symbol}) is not None
        if not stock_exists:
            # 如果股票信息表中没有，再检查价格数据表
            stock_exists = collection.find_one({"symbol": symbol}) is not None
            if not stock_exists:
                return {
                    "success": False,
                    "message": f"股票代码 {symbol} 不存在或暂无数据"
                }
        
        # 更新用户自选股，使用upsert和addToSet避免重复
        result = user_watchlists_collection.update_one(
            {"user_id": user_id},
            {
                "$addToSet": {"symbols": symbol},
                "$set": {"updated_at": datetime.now()}
            },
            upsert=True
        )
        
        return {
            "success": True,
            "message": f"股票 {symbol} 已添加到自选股",
            "data": {"symbol": symbol}
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"添加自选股失败: {str(e)}"
        }

@app.delete("/api/user/watchlist/remove/{symbol}")
def remove_from_watchlist(
    symbol: str,
    current_user: dict = Depends(get_current_active_user)
):
    """从自选股中移除股票"""
    try:
        user_id = current_user["id"]
        symbol = symbol.strip().upper()
        
        result = user_watchlists_collection.update_one(
            {"user_id": user_id},
            {
                "$pull": {"symbols": symbol},
                "$set": {"updated_at": datetime.now()}
            }
        )
        
        return {
            "success": True,
            "message": f"股票 {symbol} 已从自选股中移除",
            "data": {"symbol": symbol}
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"移除自选股失败: {str(e)}"
        }

@app.put("/api/user/watchlist")
def update_watchlist(
    request: WatchlistUpdateRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """批量更新用户自选股"""
    try:
        user_id = current_user["id"]
        symbols = [s.strip().upper() for s in request.symbols if s.strip()]
        
        # 验证所有股票代码
        invalid_symbols = []
        for symbol in symbols:
            if not collection.find_one({"symbol": symbol}):
                invalid_symbols.append(symbol)
        
        if invalid_symbols:
            return {
                "success": False,
                "message": f"以下股票代码无效: {', '.join(invalid_symbols)}"
            }
        
        # 更新自选股列表
        user_watchlists_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "symbols": symbols,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )
        
        return {
            "success": True,
            "message": f"自选股列表已更新，共 {len(symbols)} 只股票",
            "data": {"symbols": symbols, "count": len(symbols)}
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"更新自选股失败: {str(e)}"
        }

@app.get("/api/user/watchlist-stocks")
def get_user_watchlist_stocks(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的详细信息"""
    try:
        user_id = current_user["id"]
        
        # 获取用户自选股列表
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]
        result = []
        quotes = ef.stock.get_realtime_quotes()
        filtered = quotes[quotes['股票代码'].isin(symbols)]
        for symbol in symbols:
            # 获取最新价格数据
            # latest_record = collection.find_one(
            #     {"symbol": symbol},
            #     sort=[("trade_date", -1)]
            # )
            latest_record = filtered[filtered['股票代码'] == symbol].iloc[0]

            if latest_record.all():
                stock_data = {
                    "symbol": symbol,
                    "name": latest_record.get("股票名称", symbol),
                    "close": latest_record.get("最新价"),
                    "trade_date": latest_record.get("最新交易日"),
                    "volume": latest_record.get("成交量"),
                    "turnover": latest_record.get("成交额"),
                    "high": latest_record.get("最高"),
                    "low": latest_record.get("最低"),
                    "open": latest_record.get("今开"),
                    "change": latest_record.get("涨跌额"),
                    "change_percent": latest_record.get("涨跌幅"),
                    "turnover_rate": latest_record.get("换手率"),
                    "pe": latest_record.get("动态市盈率"),
                    #"pb": latest_record.get("市净率"),
                    "market_cap": latest_record.get("总市值"),
                    "circ_market_cap": latest_record.get("流通市值")
                }

                result.append(stock_data)
        
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取用户自选股信息失败: {str(e)}"
        }

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
db = client[os.getenv("MONGO_DB", "finance")]  # 使用与认证系统相同的数据库
collection = db["volume_price"]
stock_info_collection = db["stock_info"]  # 新增：股票信息集合
user_watchlists_collection = db["user_watchlists"]  # 新增：用户自选股集合
analysis_history_collection = db["analysis_history"]

# 导入管理员功能需要的集合
from auth import users_collection, login_logs_collection, admin_logs_collection

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
    request: MarketAnalysisRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """AI市场分析 (需要登录) - 带缓存和交易日检查"""
    try:
        today = date.today()
        analysis_date = today.strftime('%Y-%m-%d')

        # 如果force_refresh为True，强制跳过缓存，直接分析
        if getattr(request, 'force_refresh', False):
            print("[市场分析] 用户强制刷新，跳过缓存，直接分析...")
        else:
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
            print(f"[市场分析] {reason}，开始新的分析...")
        
        # 获取市场数据
        market_data = get_market_data_for_analysis()
        if not market_data:
            raise HTTPException(status_code=404, detail="未找到市场数据")
        
        # 获取LLM服务
        llm_service = llm_manager.get_service(
            provider=os.getenv('DEFAULT_LLM_PROVIDER', 'custom'),
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

# ===========================================
# 管理员API端点
# ===========================================

@app.get("/api/admin/users")
def get_all_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取所有用户列表"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_USERS",
            details=f"Page: {page}, Search: {search}"
        )
        
        # 构建查询条件
        query = {}
        if search:
            query = {
                "$or": [
                    {"username": {"$regex": search, "$options": "i"}},
                    {"email": {"$regex": search, "$options": "i"}},
                    {"full_name": {"$regex": search, "$options": "i"}}
                ]
            }
        
        # 计算总数
        total = users_collection.count_documents(query)
        
        # 分页查询
        skip = (page - 1) * page_size
        users = list(users_collection.find(
            query,
            {"hashed_password": 0}  # 不返回密码哈希
        ).sort("created_at", -1).skip(skip).limit(page_size))
        
        # 处理用户数据
        for user in users:
            user["id"] = str(user["_id"])
            del user["_id"]
        
        return {
            "success": True,
            "data": {
                "users": users,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户列表失败: {str(e)}")

@app.get("/api/admin/users/{user_id}")
def get_user_detail(
    user_id: str,
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取用户详细信息"""
    try:
        from bson import ObjectId
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_USER_DETAIL",
            target_user_id=user_id,
            details=f"View user {user_id} details"
        )
        
        # 查找用户
        user = users_collection.find_one(
            {"_id": ObjectId(user_id)},
            {"hashed_password": 0}
        )
        
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        user["id"] = str(user["_id"])
        del user["_id"]
        
        # 获取用户的自选股信息
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user["id"]})
        watchlist_count = len(watchlist_doc.get("symbols", [])) if watchlist_doc else 0
        
        # 获取用户的最近登录记录
        recent_logins = list(login_logs_collection.find(
            {"user_id": user["id"]},
            sort=[("login_time", -1)]
        ).limit(5))
        
        for login in recent_logins:
            login["_id"] = str(login["_id"])
        
        return {
            "success": True,
            "data": {
                "user": user,
                "statistics": {
                    "watchlist_count": watchlist_count,
                    "recent_logins": recent_logins
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户详情失败: {str(e)}")

@app.get("/api/admin/login-logs")
def get_login_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: str = Query(""),
    success_only: bool = Query(False),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取登录日志"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_LOGIN_LOGS",
            details=f"Page: {page}, UserID: {user_id}, SuccessOnly: {success_only}"
        )
        
        # 构建查询条件
        query = {}
        if user_id:
            query["user_id"] = user_id
        if success_only:
            query["success"] = True
        
        # 计算总数
        total = login_logs_collection.count_documents(query)
        
        # 分页查询
        skip = (page - 1) * page_size
        logs = list(login_logs_collection.find(query).sort("login_time", -1).skip(skip).limit(page_size))
        
        # 处理日志数据
        for log in logs:
            log["_id"] = str(log["_id"])
        
        return {
            "success": True,
            "data": {
                "logs": logs,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取登录日志失败: {str(e)}")

@app.get("/api/admin/statistics")
def get_admin_statistics(admin_user: dict = Depends(get_current_admin_user)):
    """管理员获取系统统计信息"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_STATISTICS",
            details="View system statistics"
        )
        
        # 用户统计
        total_users = users_collection.count_documents({})
        active_users = users_collection.count_documents({"is_active": True})
        admin_users = users_collection.count_documents({"is_admin": True})
        
        # 今日新注册用户
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_new_users = users_collection.count_documents({
            "created_at": {"$gte": today}
        })
        
        # 登录统计
        total_logins = login_logs_collection.count_documents({})
        successful_logins = login_logs_collection.count_documents({"success": True})
        failed_logins = login_logs_collection.count_documents({"success": False})
        
        # 今日登录统计
        today_logins = login_logs_collection.count_documents({
            "login_time": {"$gte": today}
        })
        today_successful_logins = login_logs_collection.count_documents({
            "login_time": {"$gte": today},
            "success": True
        })
        
        # 自选股统计
        total_watchlists = user_watchlists_collection.count_documents({})
        
        # 最近7天的登录趋势
        seven_days_ago = datetime.now() - timedelta(days=7)
        daily_logins = []
        for i in range(7):
            day_start = seven_days_ago + timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            day_count = login_logs_collection.count_documents({
                "login_time": {"$gte": day_start, "$lt": day_end},
                "success": True
            })
            daily_logins.append({
                "date": day_start.strftime("%Y-%m-%d"),
                "count": day_count
            })
        
        return {
            "success": True,
            "data": {
                "users": {
                    "total": total_users,
                    "active": active_users,
                    "admin": admin_users,
                    "today_new": today_new_users
                },
                "logins": {
                    "total": total_logins,
                    "successful": successful_logins,
                    "failed": failed_logins,
                    "today_total": today_logins,
                    "today_successful": today_successful_logins
                },
                "watchlists": {
                    "total": total_watchlists
                },
                "trends": {
                    "daily_logins": daily_logins
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")

@app.put("/api/admin/users/{user_id}/status")
def update_user_status(
    user_id: str,
    is_active: bool,
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员更新用户状态（激活/禁用）"""
    try:
        from bson import ObjectId
        
        # 不能禁用自己
        if user_id == admin_user["id"]:
            raise HTTPException(status_code=400, detail="不能禁用自己的账户")
        
        # 查找目标用户
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        # 更新用户状态
        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"is_active": is_active}}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=400, detail="更新失败")
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="UPDATE_USER_STATUS",
            target_user_id=user_id,
            target_username=target_user.get("username"),
            details=f"Set user active status to {is_active}"
        )
        
        return {
            "success": True,
            "message": f"用户状态已更新为 {'激活' if is_active else '禁用'}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新用户状态失败: {str(e)}")

@app.get("/api/admin/actions")
def get_admin_actions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取管理员操作日志"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_ADMIN_LOGS",
            details=f"Page: {page}"
        )
        
        # 计算总数
        total = admin_logs_collection.count_documents({})
        
        # 分页查询
        skip = (page - 1) * page_size
        actions = list(admin_logs_collection.find({}).sort("timestamp", -1).skip(skip).limit(page_size))
        
        # 处理日志数据
        for action in actions:
            action["_id"] = str(action["_id"])
        
        return {
            "success": True,
            "data": {
                "actions": actions,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取管理员日志失败: {str(e)}")

# 创建初始管理员用户的端点（仅用于初始化）
@app.post("/api/admin/init")
def initialize_admin():
    """初始化管理员账户（仅限初次使用）"""
    try:
        # 检查是否已有管理员
        existing_admin = users_collection.find_one({"is_admin": True})
        if existing_admin:
            raise HTTPException(status_code=400, detail="管理员账户已存在")
        
        # 创建默认管理员
        result = create_admin_user(
            username="admin",
            password="admin123",  # 默认密码，需要立即修改
            email="admin@quantfinance.com",
            full_name="系统管理员"
        )
        
        return {
            "success": True,
            "message": "管理员账户创建成功",
            "warning": "请立即修改默认密码！",
            "credentials": {
                "username": "admin",
                "password": "admin123"
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"初始化管理员失败: {str(e)}")

# 在 main.py 适当位置添加
analysis_history_collection = db["analysis_history"]

from pydantic import BaseModel

class AnalysisHistoryRequest(BaseModel):
    symbol: str
    analysis_result: dict
    provider: str
    model: str
    timestamp: str  # ISO 格式

@app.post("/api/analysis-history")
def save_analysis_history(
    req: AnalysisHistoryRequest,
    current_user: dict = Depends(get_current_active_user)
):
    doc = {
        "user_id": current_user["id"],
        "symbol": req.symbol,
        "analysis_result": req.analysis_result,
        "provider": req.provider,
        "model": req.model,
        "timestamp": req.timestamp
    }
    analysis_history_collection.insert_one(doc)
    return {"success": True}

@app.get("/api/analysis-history")
def get_analysis_history(
    symbol: str,
    current_user: dict = Depends(get_current_active_user)
):
    docs = list(analysis_history_collection.find(
        {"user_id": current_user["id"], "symbol": symbol}
    ).sort("timestamp", -1))
    for doc in docs:
        doc["_id"] = str(doc["_id"])
    return {"success": True, "data": docs}

# Run with: uvicorn main:app --reload