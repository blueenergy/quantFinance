"""Analysis Details API for detailed strategy analysis information."""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
from datetime import datetime, date
import json
from db import get_db_collections

router = APIRouter(prefix="/analysis", tags=["analysis"])

@router.get("/strategy/{strategy_name}/analysis-detail")
async def get_strategy_analysis_detail(
    strategy_name: str,
    limit: int = 50,
    start_date: str = None,
    end_date: str = None
):
    """
    Get detailed analysis information for a specific strategy.
    Returns detailed reasoning and decision process for deep analysis events.
    """
    try:
        collections = get_db_collections()
        col: Collection = collections["worker_activities"]
        # Build query
        query = {"strategy": strategy_name, "activity_type": "analysis"}
        
        if start_date or end_date:
            date_query = {}
            if start_date:
                start_dt = datetime.combine(date.fromisoformat(start_date), datetime.min.time())
                date_query["$gte"] = start_dt
            if end_date:
                end_dt = datetime.combine(date.fromisoformat(end_date), datetime.max.time())
                date_query["$lte"] = end_dt
            if date_query:
                query["timestamp"] = date_query
        
        # Query analysis records
        docs = col.find({"user_id": str(current_user["id"])})
        analysis_records = await docs.to_list(length=limit)
        
        # Filter records that have detailed reasoning
        detailed_records = []
        for record in analysis_records:
            if "result" in record and "detailed_reasoning" in record["result"]:
                detailed_records.append(record)
        
        return {
            "strategy": strategy_name,
            "total_records": len(detailed_records),
            "analysis_details": detailed_records
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching analysis details: {str(e)}")


@router.get("/symbol/{symbol}/analysis-detail")
async def get_symbol_analysis_detail(
    symbol: str,
    limit: int = 50,
    start_date: str = None,
    end_date: str = None
):
    """
    Get detailed analysis information for a specific symbol.
    Returns detailed reasoning and decision process for deep analysis events.
    """
    try:
        db = await get_mongo_db()
        
        # Build query
        query = {"symbol": symbol, "activity_type": "analysis"}
        
        if start_date or end_date:
            date_query = {}
            if start_date:
                start_dt = datetime.combine(date.fromisoformat(start_date), datetime.min.time())
                date_query["$gte"] = start_dt
            if end_date:
                end_dt = datetime.combine(date.fromisoformat(end_date), datetime.max.time())
                date_query["$lte"] = end_dt
            if date_query:
                query["timestamp"] = date_query
        
        # Query analysis records
        analysis_records = await db.worker_activities.find(
            query,
            {"_id": 0}  # Exclude _id field
        ).sort("timestamp", -1).limit(limit).to_list(length=limit)
        
        # Filter records that have detailed reasoning
        detailed_records = []
        for record in analysis_records:
            if "result" in record and "detailed_reasoning" in record["result"]:
                detailed_records.append(record)
        
        return {
            "symbol": symbol,
            "total_records": len(detailed_records),
            "analysis_details": detailed_records
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching analysis details: {str(e)}")


@router.get("/analysis-summary")
async def get_analysis_summary():
    """
    Get summary of all analysis activities with detailed reasoning.
    """
    try:
        db = await get_mongo_db()
        
        # Get all analysis records with detailed reasoning
        pipeline = [
            {
                "$match": {
                    "activity_type": "analysis",
                    "result.detailed_reasoning": {"$exists": True}
                }
            },
            {
                "$group": {
                    "_id": "$strategy",
                    "count": {"$sum": 1},
                    "symbols": {"$addToSet": "$symbol"},
                    "latest_analysis": {"$max": "$timestamp"}
                }
            },
            {
                "$project": {
                    "strategy": "$_id",
                    "analysis_count": "$count",
                    "symbols_count": {"$size": "$symbols"},
                    "symbols": 1,
                    "latest_analysis": 1,
                    "_id": 0
                }
            },
            {
                "$sort": {"analysis_count": -1}
            }
        ]
        
        summary = await db.worker_activities.aggregate(pipeline).to_list(length=None)
        
        return {
            "summary": summary,
            "total_strategies": len(summary)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching analysis summary: {str(e)}")