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


def add_exchange_suffix(symbol: str) -> str:
    """Add exchange suffix for stock symbol.
    
    Rules:
    - 6xxxxx (Shanghai) -> .SH
    - 0xxxxx, 3xxxxx (Shenzhen) -> .SZ
    - Already has suffix -> return as-is
    
    Args:
        symbol: Stock symbol (e.g., "600990" or "600990.SH")
        
    Returns:
        str: Symbol with exchange suffix (e.g., "600990.SH")
    """
    if '.' in symbol:
        return symbol  # Already has suffix
    
    if symbol.startswith('6'):
        return f"{symbol}.SH"
    elif symbol.startswith(('0', '3')):
        return f"{symbol}.SZ"
    else:
        return f"{symbol}.SZ"  # Default to Shenzhen


def remove_exchange_suffix(symbol: str) -> str:
    """Remove exchange suffix from stock symbol.
    
    Args:
        symbol: Symbol with suffix (e.g., "600990.SH")
        
    Returns:
        str: Symbol without suffix (e.g., "600990")
    """
    return symbol.replace(".SH", "").replace(".SZ", "")


@router.get("/watchlist-stocks/realtime")
def get_user_watchlist_realtime(current_user: dict = Depends(get_current_active_user)):
    """获取当前用户自选股的实时数据基于分钟K线）
    
    Returns:
        {
            "success": True,
            "data": [
                {
                    "symbol": "600990",
                    "name": "四创电子",
                    "price": 29.61,        # 最新价格（最新分钟K线的收盘价）
                    "open": 27.61,         # 今日开盘价
                    "high": 31.0,          # 今日最高价
                    "low": 27.45,          # 今日最低价
                    "volume": 278479.0,    # 今日累计成交量
                    "change": 0.50,        # 涨跌额
                    "change_pct": 1.72,    # 涨跌幅%
                    "update_time": "2025-12-16 14:30:00"  # 最后更新时间
                },
                ...
            ]
        }
    """
    try:
        user_id = current_user["id"]
        
        # 1. 获取用户自选股列表
        collections = get_db_collections()
        user_watchlists_collection = collections["user_watchlists"]
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_id})
        
        if not watchlist_doc or not watchlist_doc.get("symbols"):
            return {
                "success": True,
                "data": []
            }
        
        symbols = watchlist_doc["symbols"]  # e.g., ["600990", "300722"]
        
        # 2. 获取昨日收盘价（用于计算涨跌幅）
        yesterday_close_map = {}  # symbol -> close_price
        volume_price_col = collections["volume_price"]
        
        for symbol in symbols:
            latest_record = volume_price_col.find_one(
                {"symbol": symbol},
                sort=[("trade_date", -1)]
            )
            if latest_record:
                yesterday_close_map[symbol] = latest_record.get("close")
        
        # 3. 准备查询条件（minute_bars 中 symbol 没有交易所后缀）
        # symbols 已经是不带后缀的格式，如 ["600990", "300722"]
        
        # 4. 聚合今日分钟K线数据
        minute_bars_col = collections["minute_bars"]
        today = datetime.now().strftime("%Y%m%d")  # e.g., "20251216"
        
        # minute_bars 的 trade_date 格式是 YYYYMMDDHHmm，需要按日期前缀匹配
        pipeline = [
            {
                "$match": {
                    "symbol": {"$in": symbols},  # 不需要后缀
                    "trade_date": {"$regex": f"^{today}"}  # 匹配今天的所有分钟数据
                }
            },
            {"$sort": {"trade_date": 1}},  # 按时间排序
            {
                "$group": {
                    "_id": "$symbol",
                    "open": {"$first": "$open"},      # 第一根K线的开盘价
                    "close": {"$last": "$close"},     # 最新K线的收盘价
                    "high": {"$max": "$high"},        # 最高价
                    "low": {"$min": "$low"},          # 最低价
                    "volume": {"$sum": "$volume"},    # 累计成交量
                    "latest_time": {"$last": "$trade_date"}  # 最后更新时间
                }
            }
        ]
        
        realtime_data = list(minute_bars_col.aggregate(pipeline))
        
        # 5. 构建结果并计算涨跌幅
        result = []
        realtime_map = {item["_id"]: item for item in realtime_data}  # symbol -> data
        
        stock_info_col = collections["stock_info"]
        
        for symbol in symbols:
            # 获取股票名称
            stock_info = stock_info_col.find_one({"symbol": symbol})
            name = stock_info["name"] if stock_info and "name" in stock_info else symbol
            
            if symbol in realtime_map:
                # 有今日实时数据
                item = realtime_map[symbol]
                current_price = item["close"]
                yesterday_close = yesterday_close_map.get(symbol, item["open"])  # 如果没有昨日数据，用今日开盘价
                
                # 计算涨跌额和涨跌幅
                change = current_price - yesterday_close if yesterday_close else 0
                change_pct = (change / yesterday_close * 100) if yesterday_close else 0
                
                # 格式化更新时间（YYYYMMDDHHmm -> YYYY-MM-DD HH:MM:SS）
                latest_time_str = item["latest_time"]
                if latest_time_str and len(latest_time_str) >= 12:
                    formatted_time = f"{latest_time_str[0:4]}-{latest_time_str[4:6]}-{latest_time_str[6:8]} {latest_time_str[8:10]}:{latest_time_str[10:12]}:00"
                else:
                    formatted_time = str(latest_time_str)
                
                result.append({
                    "symbol": symbol,
                    "name": name,
                    "price": round(current_price, 2),
                    "open": round(item["open"], 2),
                    "high": round(item["high"], 2),
                    "low": round(item["low"], 2),
                    "volume": round(item["volume"], 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "update_time": formatted_time
                })
            else:
                # 无今日实时数据，返回昨日数据或空值
                yesterday_close = yesterday_close_map.get(symbol)
                
                if yesterday_close:
                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": round(yesterday_close, 2),
                        "open": None,
                        "high": None,
                        "low": None,
                        "volume": None,
                        "change": 0.0,
                        "change_pct": 0.0,
                        "update_time": None
                    })
                else:
                    result.append({
                        "symbol": symbol,
                        "name": name,
                        "price": None,
                        "open": None,
                        "high": None,
                        "low": None,
                        "volume": None,
                        "change": None,
                        "change_pct": None,
                        "update_time": None
                    })
        
        return {
            "success": True,
            "data": result
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"获取实时自选股数据失败: {str(e)}"
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