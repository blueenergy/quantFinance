from pydantic import BaseModel, EmailStr
from typing import Optional, List,Any
from datetime import datetime, timedelta

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(UserBase):
    id: str
    is_active: bool
    is_admin: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

class TokenData(BaseModel):
    username: Optional[str] = None

class LoginLog(BaseModel):
    user_id: str
    username: str
    ip_address: str
    user_agent: str
    login_time: datetime
    success: bool
    failure_reason: Optional[str] = None

class AdminAction(BaseModel):
    admin_id: str
    admin_username: str
    action: str
    target_user_id: Optional[str] = None
    target_username: Optional[str] = None
    details: str
    timestamp: datetime



class StockAnalysisRequest(BaseModel):
    symbol: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    prompt: Optional[str] = None  # <--- 补充这一行
    provider: Optional[str] = None   # <--- 补充
    model: Optional[str] = None      # <--- 补充
class MarketAnalysisRequest(BaseModel):
    type: str
    force_refresh: Optional[bool] = False

# Add more models as needed for your API
class StockInfo(BaseModel):
    symbol: str
    name: str
    current: float
    change: float
    change_percent: float
    volume: int
    amount: float
    trade_date: str
    source: str

class WatchlistRequest(BaseModel):
    symbol: str

class WatchlistUpdateRequest(BaseModel):
    symbols: List[str]

class AnalysisHistoryRequest(BaseModel):
    symbol: str
    prompt: Optional[str] = None
    analysis_result: Any
    provider: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None