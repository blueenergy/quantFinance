# app/routers/index_data.py
from fastapi import APIRouter, HTTPException
#from services.index_services import index_service
from models.index_models import HS300ConstituentsResponse
from services.index_services import IndexService
router = APIRouter()

@router.get("/hs300/constituents", response_model=HS300ConstituentsResponse)
async def get_hs300_constituents(force_refresh: bool = False):
    """HTTP接口 - 处理API请求"""
    try:
        # 1. 参数验证
        # 2. 调用服务层
        service = IndexService()
        constituents = await service.get_hs300_constituents()      
        # 3. 构造响应
        return HS300ConstituentsResponse(
            success=True,
            data=constituents,
            total_count=len(constituents)
        )
    except Exception as e:
        # 4. 错误处理
        raise HTTPException(status_code=500, detail=str(e))