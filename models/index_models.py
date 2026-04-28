# app/models/index_models.py
from pydantic import BaseModel
from typing import List, Optional

class IndexConstituent(BaseModel):
    """通用指数成分股模型；symbol 为 Tushare 风格 ts_code（含 .SH/.SZ/.BJ）。"""
    symbol: str
    name: str
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    weight: Optional[float] = None
    update_time: Optional[str] = None

# 向后兼容旧名称（HS300Constituent，用于已存在的导入）
HS300Constituent = IndexConstituent

class IndexConstituentsResponse(BaseModel):
    success: bool
    data: List[IndexConstituent]
    total_count: int
    message: Optional[str] = None

# 旧响应名称保持以防已被引用
HS300ConstituentsResponse = IndexConstituentsResponse