from pydantic import BaseModel, EmailStr, model_validator
from typing import Optional, List, Any, Dict
from datetime import datetime, timedelta

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class UserResponse(UserBase):
    id: str
    is_active: bool
    is_admin: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None  # This will not be returned to client for security
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    enable_analytics: Optional[bool] = True
    active_llm_config_id: Optional[str] = None

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

class WatchlistStrategySetRequest(BaseModel):
    symbol: str
    strategy: str
    params: dict
    enabled: Optional[bool] = True

class WatchlistStrategyResponse(BaseModel):
    symbol: str
    strategy: str
    params: dict

class AnalysisHistoryRequest(BaseModel):
    symbol: str
    prompt: Optional[str] = None
    # Legacy-compatible payload: analysis must live under analysis_result.analysis
    analysis_result: Any
    # Accept raw analysis for callers that haven't migrated; we will wrap it into analysis_result
    analysis: Optional[Any] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None
    stock_name: Optional[str] = None

    @model_validator(mode='before')
    def ensure_analysis_result(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # If caller only provided `analysis`, wrap it into the canonical structure
        if values.get("analysis_result") is None and values.get("analysis") is not None:
            values["analysis_result"] = {"analysis": values.get("analysis")}
        return values

# --- Securities accounts ---
class SecuritiesAccountBase(BaseModel):
    broker: str
    account_id: str
    account_type: str = "real"  # real(真实账户) / simulated(模拟账户)

class SecuritiesAccountCreate(BaseModel):
    # 模拟账户的 broker 和 account_id 在后端自动生成，真实账户必须提供
    broker: Optional[str] = None
    account_id: Optional[str] = None
    account_type: str = "real"
    password: Optional[str] = None  # 模拟账户不需要密码
    initial_cash: Optional[float] = None  # 模拟账户初始资金，默认100万

class SecuritiesAccountUpdate(BaseModel):
    broker: Optional[str] = None
    account_id: Optional[str] = None
    password: Optional[str] = None
    account_type: Optional[str] = None

class SecuritiesAccountResponse(SecuritiesAccountBase):
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    initial_cash: Optional[float] = None  # 模拟账户显示初始资金


class WorkerNodeRegisterRequest(BaseModel):
    node_id: str
    host: str
    node_type: str = "private"  # private(私有) / public(公有)
    labels: Optional[Dict[str, Any]] = None
    capacity: Optional[Dict[str, Any]] = None


class WorkerNodeHeartbeat(BaseModel):
    node_id: str
    timestamp: float
    processes: Optional[List[Dict[str, Any]]] = None


class WorkerGroupConfig(BaseModel):
    group_id: str
    node_id: str
    symbols: List[str]
    user_id: str
    mode: str = "live"  # live / backtest / replay
    task_type: str = "realtime"  # realtime(实时交易) / analysis(分析) / backtest(回测)
    desired_state: str = "running"  # running / stopped
    current_state: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class WorkerGroupsReport(BaseModel):
    node_id: str
    groups: List[WorkerGroupConfig]


class UpdateLLMConfigRequest(BaseModel):
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
