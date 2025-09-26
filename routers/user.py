import os
import tushare as ts
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from models.all_models import UserCreate, UserLogin, UserResponse, Token, WatchlistRequest, WatchlistUpdateRequest
from auth import create_user, authenticate_user, create_access_token, get_current_active_user, log_login_attempt,ACCESS_TOKEN_EXPIRE_MINUTES
from db import get_db_collections
from datetime import timedelta, datetime
from utils.stock_utils import get_ts_code

router = APIRouter()

# 注册、登录、个人信息
@router.post("/register", response_model=UserResponse)
def register(user: UserCreate):
    return create_user(user)

@router.post("/login", response_model=Token)
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
    print(user_response)
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=user_response
    )


@router.get("/profile", response_model=UserResponse)
def get_profile(current_user: dict = Depends(get_current_active_user)):
    # 补全所有字段，避免缺失
    return UserResponse(
        id=current_user.get("id"),
        username=current_user.get("username"),
        email=current_user.get("email"),
        full_name=current_user.get("full_name"),
        is_active=current_user.get("is_active", True),
        is_admin=current_user.get("is_admin", False),
        created_at=current_user.get("created_at"),
        last_login=current_user.get("last_login")
    )

# 自选股相关
def get_user_id(current_user: dict):
    # 统一获取用户唯一标识（主键 _id），并转换为字符串
    return str(current_user["id"])

@router.get("/watchlist")
def get_user_watchlist(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    #print("current_user:", current_user)
    user_id = get_user_id(current_user)
    doc = watchlist_col.find_one({"user_id": user_id})
    symbols = doc["symbols"] if doc and "symbols" in doc else []
    return {"success": True, "data": {"symbols": symbols}}

@router.post("/watchlist/add")
def add_to_watchlist(request: WatchlistRequest, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    symbol = request.symbol
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$addToSet": {"symbols": symbol}},
        upsert=True
    )
    return {"success": True}

@router.delete("/watchlist/remove/{symbol}")
def remove_from_watchlist(symbol: str, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$pull": {"symbols": symbol}}
    )
    return {"success": True}

@router.put("/watchlist")
def update_watchlist(request: WatchlistUpdateRequest, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    user_id = get_user_id(current_user)
    watchlist_col.update_one(
        {"user_id": user_id},
        {"$set": {"symbols": request.symbols}},
        upsert=True
    )
    return {"success": True}

@router.get("/watchlist-stocks")
def get_user_watchlist_stocks(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的详细信息"""
    try:
        user_id = current_user["id"]
        
        # 获取用户自选股列表
        collections = get_db_collections()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]
        result = []

        ts.set_token(os.getenv("TUSHARE_TOKEN"))
        pro = ts.pro_api()



        # quotes = ef.stock.get_realtime_quotes()
        # filtered = quotes[quotes['股票代码'].isin(symbols)]
        for symbol in symbols:
            ts_code = get_ts_code(symbol)
            df = pro.daily(ts_code=ts_code, limit=1)
            name = collections["stock_info"].find_one({"symbol": symbol})['name']

            if not df.empty:
                latest_record = df.iloc[0]
                stock_data = {
                    "symbol": symbol,
                    "name": name,
                    "close": latest_record.get("close"),
                    "trade_date": latest_record.get("trade_date"),
                    "volume": latest_record.get("vol"),
                    "turnover": latest_record.get("amount"),
                    "high": latest_record.get("high"),
                    "low": latest_record.get("low"),
                    "open": latest_record.get("open"),
                    "change": latest_record.get("change"),
                    "change_percent": latest_record.get("pct_chg"),
                    "turnover_rate": None,
                    "pe": None,
                    "market_cap": None,
                    "circ_market_cap": None
                }

                result.append(stock_data)
            else:
                 result.append({
                "symbol": symbol,
                "name": symbol,
                "close": None,
                "trade_date": None,
                "volume": None,
                "turnover": None,
                "high": None,
                "low": None,
                "open": None,
                "change": None,
                "change_percent": None,
                "turnover_rate": None,
                "pe": None,
                "market_cap": None,
                "circ_market_cap": None
                 })
        
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取用户自选股信息失败: {str(e)}"
        }