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
from datetime import timedelta
from dotenv import load_dotenv
from llm_service import llm_manager
from auth import (
    UserCreate, UserLogin, Token, UserResponse,
    authenticate_user, create_access_token, create_user,
    get_current_active_user, ACCESS_TOKEN_EXPIRE_MINUTES
)

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
            provider=request.provider or "openai",
            model=request.model or "gpt-3.5-turbo"
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