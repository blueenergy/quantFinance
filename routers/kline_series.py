"""/api/kline-series — K线走势对比数据查询

优先查 index_prices（ts_code 字段），其次场内 ETF（etf_daily），
再次期货主力（fut_daily），无数据时回退查 volume_price（symbol 字段）。
个股 volume_price / ETF etf_daily 支持读取侧复权（默认前复权 qfq）。
期货 fut_daily 不复权。
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Query, Depends
from auth import get_current_active_user
from db import data_db
from services.etf_kline import load_etf_kline_rows
from services.futures_varieties import FUT_WHITELIST_CODES
from stock_data_access.prices import AdjustedPriceDataAccess, VALID_ADJUST

router = APIRouter()

_index_prices = data_db["index_prices"]
_volume_price = data_db["volume_price"]
_etf_basic = data_db["etf_basic"]
_etf_daily = data_db["etf_daily"]
_etf_adj_factor = data_db["etf_adj_factor"]
_fut_daily = data_db["fut_daily"]

_PROJ = {"_id": 0, "trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
_FUT_PROJ = {
    "_id": 0,
    "trade_date": 1,
    "open": 1,
    "high": 1,
    "low": 1,
    "close": 1,
    "vol": 1,
    "amount": 1,
    "oi": 1,
    "contract": 1,
}


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
    used_adjust = "none"

    # 2. 场内 ETF：etf_daily + etf_adj_factor（不要落到 volume_price 残留快照）
    if not docs:
        etf_rows = load_etf_kline_rows(
            _etf_basic,
            _etf_daily,
            symbol,
            start=sd or None,
            end=ed or None,
            limit=limit,
        )
        if etf_rows is not None:
            from routers.records import _apply_one_symbol

            ts_code = etf_rows[0]["symbol"] if etf_rows else symbol
            docs = _apply_one_symbol(etf_rows, ts_code, adjust, _etf_adj_factor)
            docs = list(reversed(docs))
            source = "etf_daily"
            used_adjust = adjust

    # 3. 期货主力日线（白名单 ts_code，禁止回落 volume_price）
    if not docs and source != "etf_daily" and symbol in FUT_WHITELIST_CODES:
        flt_fut: dict = {"ts_code": symbol}
        if date_flt:
            flt_fut["trade_date"] = date_flt
        docs = list(_fut_daily.find(flt_fut, _FUT_PROJ).sort("trade_date", 1).limit(limit))
        if docs:
            source = "fut_daily"
            used_adjust = "none"

    # 4. 回退查 volume_price（个股 + 部分指数如 000852.SZ）
    if not docs and source not in ("etf_daily", "fut_daily"):
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
        used_adjust = adjust

    result = [
        {
            "trade_date": d.get("trade_date"),
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "close": d.get("close"),
            "volume": d.get("volume") if d.get("volume") is not None else d.get("vol"),
            **(
                {
                    "amount": d.get("amount"),
                    "oi": d.get("oi"),
                    "contract": d.get("contract"),
                }
                if source == "fut_daily"
                else {}
            ),
        }
        for d in docs
        if d.get("close") is not None
    ]

    return {
        "success": True,
        "data": result,
        "source": source,
        "count": len(result),
        "adjust": used_adjust if source not in ("index_prices", "fut_daily") else "none",
    }
