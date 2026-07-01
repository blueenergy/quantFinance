"""/api/kline-series — K线走势对比数据查询

优先查 index_prices（ts_code 字段），无数据时回退查 volume_price（symbol 字段）。
个股 volume_price 支持读取侧复权（默认前复权 qfq）。
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Query, Depends
from auth import get_current_active_user
from db import data_db
from stock_data_access.prices import AdjustedPriceDataAccess, VALID_ADJUST

router = APIRouter()

_index_prices = data_db["index_prices"]
_volume_price = data_db["volume_price"]

_PROJ = {"_id": 0, "trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}


@router.get("/kline-series")
def get_kline_series(
    symbol: str = Query(..., description="指数或股票代码，如 000300.SH / 600519.SH"),
    start_date: Optional[str] = Query(None, description="起始日期 YYYYMMDD 或 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYYMMDD 或 YYYY-MM-DD"),
    limit: int = Query(2000, le=5000),
    adjust: str = Query("qfq", description="个股复权口径: none/hfq/qfq（指数忽略）"),
    current_user: dict = Depends(get_current_active_user),
):
    if adjust not in VALID_ADJUST:
        adjust = "qfq"

    sd = (start_date or "").replace("-", "")
    ed = (end_date or "").replace("-", "")

    date_flt: dict = {}
    if sd:
        date_flt["$gte"] = sd
    if ed:
        date_flt["$lte"] = ed

    # 1. 先查 index_prices（常见指数：000300.SH / 000905.SH 等）
    flt: dict = {"ts_code": symbol}
    if date_flt:
        flt["trade_date"] = date_flt
    docs = list(_index_prices.find(flt, _PROJ).sort("trade_date", 1).limit(limit))
    source = "index_prices"

    # 2. 回退查 volume_price（个股 + 部分指数如 000852.SZ）
    if not docs:
        if sd and ed:
            adj_dao = AdjustedPriceDataAccess(db=data_db)
            df = adj_dao.load_adjusted_ohlc(symbol, sd, ed, adjust=adjust)
            if not df.empty:
                df = df.reset_index(drop=True)
                docs = df.to_dict("records")[:limit]
            else:
                docs = []
        else:
            flt2: dict = {"symbol": symbol}
            if date_flt:
                flt2["trade_date"] = date_flt
            docs = list(_volume_price.find(flt2, _PROJ).sort("trade_date", 1).limit(limit))
        source = "volume_price"

    result = [
        {
            "trade_date": d.get("trade_date"),
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "close": d.get("close"),
            "volume": d.get("volume"),
        }
        for d in docs
        if d.get("close") is not None
    ]

    return {"success": True, "data": result, "source": source, "count": len(result), "adjust": adjust if source == "volume_price" else "none"}
