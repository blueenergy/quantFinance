from fastapi import APIRouter
from db import get_db_collections
router = APIRouter()




@router.get("/info/{symbol}")
def get_stock_info(symbol: str):
    """
    获取指定股票的基本信息
    """
    collections = get_db_collections()
    stock_col = collections["stock_info"]
    stock_info = stock_col.find_one({"symbol": symbol})

    if stock_info:
        stock_info["_id"] = str(stock_info["_id"])
        return {"success": True, "data": stock_info}
    return {"success": False, "message": "未找到股票信息"}

