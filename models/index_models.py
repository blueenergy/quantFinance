# app/models/index_models.py
from pydantic import BaseModel
from typing import List, Optional

class HS300Constituent(BaseModel):
    """沪深300成分股模型 - 定义数据结构"""
    symbol: str
    name: str
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    weight: Optional[float] = None
    update_time: Optional[str] = None

class HS300ConstituentsResponse(BaseModel):
    """API响应模型 - 定义返回格式"""
    success: bool
    data: List[HS300Constituent]
    total_count: int
    message: Optional[str] = None