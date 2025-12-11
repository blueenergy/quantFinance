from typing import List
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from models.all_models import UserCreate, UserLogin, UserResponse, Token, WatchlistRequest, WatchlistUpdateRequest, WatchlistStrategySetRequest, WatchlistStrategyResponse, SecuritiesAccountCreate, SecuritiesAccountUpdate, SecuritiesAccountResponse
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

# --- Watchlist strategies per symbol ---
@router.get("/watchlist/strategies")
def get_watchlist_strategies(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    strat_col = collections["watchlist_strategies"]
    user_id = get_user_id(current_user)
    docs = strat_col.find({"user_id": user_id})
    data = []
    for d in docs:
        data.append({
            "symbol": d["symbol"],
            "strategy_key": d.get("strategy_key"),
            "params": d.get("params", {}),
            "updated_at": d.get("updated_at"),
            "enabled": d.get("enabled", True),
        })
    return {"success": True, "data": data}

@router.post("/watchlist/strategy")
def set_watchlist_strategy(req: WatchlistStrategySetRequest, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    watchlist_col = collections["user_watchlists"]
    strat_col = collections["watchlist_strategies"]
    user_id = get_user_id(current_user)
    # Ensure symbol is in user watchlist
    doc = watchlist_col.find_one({"user_id": user_id})
    symbols = doc["symbols"] if doc and "symbols" in doc else []
    if req.symbol not in symbols:
        raise HTTPException(status_code=400, detail="Symbol not in user watchlist")
    import time
    enabled = getattr(req, "enabled", True)
    strat_col.update_one(
        {"user_id": user_id, "symbol": req.symbol},
        {"$set": {"strategy_key": req.strategy, "params": req.params, "updated_at": time.time(), "enabled": enabled}},
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

        volume_price_col = collections["volume_price"]
        for symbol in symbols:
            stock_info = collections["stock_info"].find_one({"symbol": symbol})
            name = stock_info["name"] if stock_info and "name" in stock_info else symbol

            # Find latest record for symbol in volume_price
            latest_record = volume_price_col.find_one(
                {"symbol": symbol},
                sort=[("trade_date", -1)]
            )

            if latest_record:
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
                    "turnover_rate": latest_record.get("turnover_rate"),
                    "pe": latest_record.get("pe"),
                    "market_cap": latest_record.get("market_cap"),
                    "circ_market_cap": latest_record.get("circ_market_cap")
                }
                result.append(stock_data)
            else:
                result.append({
                    "symbol": symbol,
                    "name": name,
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

# 证券账户管理
from models.all_models import SecuritiesAccountCreate, SecuritiesAccountUpdate, SecuritiesAccountResponse

@router.post("/securities_account", response_model=SecuritiesAccountResponse)
def create_securities_account(request: SecuritiesAccountCreate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col = collections["securities_accounts"]
    user_id = get_user_id(current_user)
    import datetime
    doc = {
        "user_id": user_id,
        "broker": request.broker,
        "account_id": request.account_id,
        "password": request.password,
        "created_at": datetime.datetime.utcnow(),
        "updated_at": datetime.datetime.utcnow(),
    }
    result = col.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    return SecuritiesAccountResponse(**doc)

@router.get("/securities_account", response_model=List[SecuritiesAccountResponse])
def list_securities_accounts(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col = collections["securities_accounts"]
    user_id = get_user_id(current_user)
    docs = list(col.find({"user_id": user_id}))
    for d in docs:
        d["id"] = str(d["_id"])
    return [SecuritiesAccountResponse(**d) for d in docs]

@router.put("/securities_account/{account_id}", response_model=SecuritiesAccountResponse)
def update_securities_account(account_id: str, request: SecuritiesAccountUpdate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col = collections["securities_accounts"]
    user_id = get_user_id(current_user)
    update_doc = {k: v for k, v in request.dict().items() if v is not None}
    update_doc["updated_at"] = datetime.datetime.utcnow()
    col.update_one({"user_id": user_id, "account_id": account_id}, {"$set": update_doc})
    doc = col.find_one({"user_id": user_id, "account_id": account_id})
    doc["id"] = str(doc["_id"])
    return SecuritiesAccountResponse(**doc)

@router.delete("/securities_account/{account_id}")
def delete_securities_account(account_id: str, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col = collections["securities_accounts"]
    user_id = get_user_id(current_user)
    col.delete_one({"user_id": user_id, "account_id": account_id})
    return {"success": True}