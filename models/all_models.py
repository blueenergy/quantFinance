from pydantic import BaseModel, EmailStr, model_validator
from typing import Optional, List, Any, Dict, Literal
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
    service_level: Optional[str] = "free"
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
    type: Optional[str] = None
    date: Optional[str] = None
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
    # None = 不修改已有 params（用于只切换 enabled 的场景）；dict = 合并到已有 params 上。
    params: Optional[Dict[str, Any]] = None
    # 显式要求删除的 params 键；合并语义下，未发送的键默认保留。
    deleted_keys: List[str] = []
    enabled: Optional[bool] = True
    # Bind the strategy to one securities account.  A user may own several
    # accounts (personal + strategy); without an explicit binding the
    # orchestrator only auto-binds when the user has exactly one.
    securities_account_id: Optional[str] = None
    # Disabling a strategy that still manages shares requires an explicit
    # decision: "clear" flattens the managed sleeve, "keep" leaves it alone.
    position_action: Optional[Literal["clear", "keep"]] = None
    # Only meaningful with position_action="clear": also cancel same-symbol
    # BUY signals that could fill after the strategy stops.
    cancel_pending_buys: bool = False

class WatchlistStrategyResponse(BaseModel):
    symbol: str
    strategy: str
    params: dict


class ThemeLagValidationSubscriptionUpsert(BaseModel):
    """订阅 quant_data 主题补涨验证结果更新（站内通知 + 可选邮件）。"""
    index_store_key: str
    enabled: bool = True

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
    live_trading_enabled: bool = False
    risk_limits: Optional[Dict[str, Any]] = None
    fee_model: Optional[Dict[str, Any]] = None

class BrokerOptionCreate(BaseModel):
    name: str
    enabled: bool = True
    qmt_supported: bool = False
    sort_order: Optional[int] = None


class BrokerOptionUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    qmt_supported: Optional[bool] = None
    sort_order: Optional[int] = None


class QmtClientCreate(BaseModel):
    name: str
    broker: str
    mailbox_root: str
    enabled: bool = True
    sort_order: Optional[int] = None


class QmtClientUpdate(BaseModel):
    name: Optional[str] = None
    broker: Optional[str] = None
    mailbox_root: Optional[str] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class TraderExecutorConfig(BaseModel):
    qmt_account_id: Optional[str] = None
    qmt_mailbox_root: Optional[str] = None
    trader_client_id: Optional[str] = None
    trader_container_name: Optional[str] = None
    auto_manage: Optional[bool] = None


class SecuritiesAccountUpdate(BaseModel):
    broker: Optional[str] = None
    account_id: Optional[str] = None
    password: Optional[str] = None
    account_type: Optional[str] = None
    live_trading_enabled: Optional[bool] = None
    risk_limits: Optional[Dict[str, Any]] = None
    fee_model: Optional[Dict[str, Any]] = None

class SecuritiesAccountResponse(SecuritiesAccountBase):
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    initial_cash: Optional[float] = None  # 模拟账户显示初始资金
    live_trading_enabled: bool = False
    risk_limits: Optional[Dict[str, Any]] = None
    fee_model: Optional[Dict[str, Any]] = None


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
