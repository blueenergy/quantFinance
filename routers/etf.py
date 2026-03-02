from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from db import db
import pandas as pd

router = APIRouter(prefix="/etf", tags=["etf"])

@router.get("/list")
async def get_etf_list(
    q: Optional[str] = Query(None, description="Search by symbol or name"),
    limit: int = 50
):
    """
    Get a list of ETFs, optionally filtered by search query.
    """
    query = {}
    if q:
        import re
        # Case insensitive regex search on symbol or name
        pattern = re.compile(q, re.IGNORECASE)
        query = {"$or": [{"ts_code": pattern}, {"name": pattern}]}
        
    cursor = db["etf_basic"].find(query, {"_id": 0}).limit(limit)
    results = list(cursor)
    
    return {
        "success": True,
        "count": len(results),
        "data": results
    }

@router.get("/{symbol}/kline")
async def get_etf_kline(
    symbol: str, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    limit: int = 1000
):
    """
    Get ETF daily K-line data, merged with adjustment factors (for pre-adjusted prices) 
    and daily share scale.
    """
    # Base query for the specific ETF
    query = {"ts_code": symbol}
    
    # Add date filters if provided
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date.replace('-', '')
    if end_date:
        date_filter["$lte"] = end_date.replace('-', '')
        
    if date_filter:
        query["trade_date"] = date_filter
        
    # Execute queries synchronously (pymongo client is sync)
    daily_results = list(db["etf_daily"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    adj_results = list(db["etf_adj_factor"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    share_results = list(db["etf_shares"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    
    if not daily_results:
        return {"success": True, "symbol": symbol, "data": []}
        
    # Convert to pandas DataFrames for easy merging and calculation
    df_daily = pd.DataFrame(daily_results)
    
    # --- 1. Process Adjustment Factors (Pre-adjustment) ---
    if adj_results:
        df_adj = pd.DataFrame(adj_results)
        # Merge on trade_date
        df = pd.merge(df_daily, df_adj[['trade_date', 'adj_factor']], on='trade_date', how='left')
        
        # Forward fill adj_factors if some days are missing but earlier days exist
        df['adj_factor'] = df['adj_factor'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)
        
        # Calculate pre-adjusted prices
        # Pre-adjusted price = Current Price * (Current Adj Factor / Latest Adj Factor)
        # Since we sorted by trade_date descending, the latest available is at index 0
        latest_adj_factor = df['adj_factor'].iloc[0]
        
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col] * (df['adj_factor'] / latest_adj_factor)
            # Round to 3 decimal places for ETFs
            df[f'pre_adj_{col}'] = df[f'pre_adj_{col}'].round(3)
    else:
        df = df_daily
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col]

    # --- 2. Process Share Scale ---
    if share_results:
        df_share = pd.DataFrame(share_results)
        # Tushare fund_share might not be updated daily for all ETFs, typically week/month ends for some, daily for others.
        # We merge and forward fill the share scale so user always sees the 'latest known' scale.
        df = pd.merge(df, df_share[['trade_date', 'fd_share']], on='trade_date', how='left')
        
        # Need to sort by date ascending to safely forward fill shares into the future
        df = df.sort_values('trade_date', ascending=True)
        df['fd_share'] = df['fd_share'].fillna(method='ffill')
        
        # Sort back to descending for API output
        df = df.sort_values('trade_date', ascending=False)
    else:
        df['fd_share'] = None
        
    
    import math
    merged_data = []
    for row in df.to_dict('records'):
        clean_row = {}
        for k, v in row.items():
            if pd.isna(v) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                clean_row[k] = None
            else:
                # specifically convert NumPy floats to regular Python floats
                if hasattr(v, 'item'):
                    v = v.item()
                clean_row[k] = v
        merged_data.append(clean_row)
    
    return {
        "success": True,
        "symbol": symbol,
        "count": len(merged_data),
        "data": merged_data
    }
