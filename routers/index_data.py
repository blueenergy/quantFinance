# app/routers/index_data.py
from fastapi import APIRouter, HTTPException
#from services.index_services import index_service
from models.index_models import HS300ConstituentsResponse, IndexConstituentsResponse
from services.index_services import IndexService
router = APIRouter()

# 沪深300
@router.get("/hs300/constituents", response_model=HS300ConstituentsResponse)
async def get_hs300_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("hs300")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 科创50
@router.get("/star50/constituents", response_model=IndexConstituentsResponse)
async def get_star50_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("star50")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 中证A500
@router.get("/a500/constituents", response_model=IndexConstituentsResponse)
async def get_a500_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("a500")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# 中证500
@router.get("/csi500/constituents", response_model=IndexConstituentsResponse)
async def get_csi500_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("csi500")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 中证1000
@router.get("/csi1000/constituents", response_model=IndexConstituentsResponse)
async def get_csi1000_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("csi1000")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 中证2000
@router.get("/csi2000/constituents", response_model=IndexConstituentsResponse)
async def get_csi2000_constituents():
    try:
        service = IndexService()
        data = await service.get_index_constituents("csi2000")
        return IndexConstituentsResponse(success=True, data=data, total_count=len(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))