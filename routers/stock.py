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


@router.get("/search")
def search_stocks(q: str):
    """
    搜索股票，支持模糊匹配股票代码或名称
    """
    collections = get_db_collections()
    stock_col = collections["stock_info"]
    
    # 搜索匹配的股票，支持股票代码和名称的模糊匹配
    # 如果查询是数字开头，优先匹配股票代码
    if q.isdigit():
        # 对于数字查询，查找以该数字开头的股票代码
        query = {"symbol": {"$regex": f"^{q}", "$options": "i"}}
    else:
        # 对于非数字查询，匹配股票代码或名称
        query = {"$or": [
            {"symbol": {"$regex": q, "$options": "i"}},
            {"name": {"$regex": q, "$options": "i"}}
        ]}
    
    stocks = list(stock_col.find(query).limit(20))  # 限制返回数量
    
    # 处理返回数据
    for stock in stocks:
        stock["_id"] = str(stock["_id"])
    
    return {"success": True, "data": stocks}

