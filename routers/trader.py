"""Trader API endpoints for quantTrader client.

Provides endpoints for:
- Fetching trading signals
- Reporting execution results
- Syncing positions and account data
- Trade history queries

This is the bridge between quantTrader (client) and quantFinance (backend).
"""

from fastapi import APIRouter, Depends, Query, HTTPException, Body
from typing import List, Optional, Dict, Any
from datetime import datetime
import time
from auth import get_current_active_user
from db import get_db_collections, get_data_db
from utils.stock_utils import serialize_doc
from bson import ObjectId

router = APIRouter()


@router.get("/trader/signals", summary="Get pending trading signals")
def get_pending_signals(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of signals"),
    include_submitted: bool = Query(False, description="Include submitted signals"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get pending trading signals for quantTrader execution.
    
    Returns signals that are:
    - status='pending' (not yet submitted to broker)
    - status='retry_pending' (failed, needs retry)
    - Optionally: status='submitted' (if include_submitted=True)
    """
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # Build query
        query = {
            "user_id": user_id,
            "is_executable": True,
            "mode": "live"
        }
        
        # Status filter
        if include_submitted:
            query["status"] = {"$in": ["pending", "retry_pending", "submitted"]}
        else:
            query["status"] = {"$in": ["pending", "retry_pending"]}
        
        # Fetch signals sorted by timestamp
        cursor = signals_coll.find(query).sort("timestamp", 1).limit(limit)
        signals = [serialize_doc(doc) for doc in cursor]
        
        return {
            "success": True,
            "data": signals,
            "count": len(signals)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch signals: {str(e)}")


@router.post("/trader/signals/{order_id}/status", summary="Update signal status")
def update_signal_status(
    order_id: str,
    status_update: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_active_user)
):
    """Update trading signal status.
    
    Used by quantTrader to report:
    - Order submission (status='submitted')
    - Order failure (status='retry_pending')
    - Broker order ID (qmt_order_id)
    """
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # Validate order_id exists and belongs to user
        signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
        if not signal:
            raise HTTPException(status_code=404, detail=f"Signal not found: {order_id}")
        
        # Update fields
        update_doc = {"$set": status_update}
        
        result = signals_coll.update_one(
            {"order_id": order_id, "user_id": user_id},
            update_doc
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Signal not found")
        
        return {"success": True, "order_id": order_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update signal: {str(e)}")


@router.post("/trader/executions", summary="Report trade execution")
def create_execution(
    execution: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_active_user)
):
    """Record trade execution result.
    
    Called by quantTrader after order fills to record:
    - Filled price and quantity
    - Commission fees
    - Broker order ID
    - Execution timestamp
    """
    try:
        collections = get_db_collections()
        executions_coll = collections["trade_executions"]
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # Add user_id and timestamp
        execution["user_id"] = user_id
        if "timestamp" not in execution:
            execution["timestamp"] = time.time()
        
        # Insert execution record
        result = executions_coll.insert_one(execution)
        
        # Update signal status to 'executed'
        order_id = execution.get("order_id")
        if order_id:
            signals_coll.update_one(
                {"order_id": order_id, "user_id": user_id},
                {"$set": {"status": "executed", "executed_at": execution["timestamp"]}}
            )
        
        return {
            "success": True,
            "message": "Execution recorded",
            "execution_id": str(result.inserted_id)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create execution: {str(e)}")


@router.post("/trader/positions/sync", summary="Sync positions from broker")
def sync_positions(
    positions: List[Dict[str, Any]] = Body(...),
    securities_account_id: Optional[str] = Body(None),
    current_user: dict = Depends(get_current_active_user)
):
    """Sync real-time positions from quantTrader.
    
    Receives position snapshots from broker (via quantTrader) and stores them.
    This enables:
    - Cross-device position viewing
    - Historical position tracking
    - Strategy integration with current positions
    
    Args:
        positions: List of position dicts from broker
        securities_account_id: Optional link to securities_accounts._id
        current_user: Authenticated user
    """
    try:
        collections = get_db_collections()
        positions_coll = collections["trader_positions"]
        securities_coll = collections["securities_accounts"]
        
        user_id = current_user["id"]
        current_time = time.time()
        
        # Validate securities_account_id if provided
        account_doc = None
        if securities_account_id:
            try:
                account_doc = securities_coll.find_one({
                    "_id": ObjectId(securities_account_id),
                    "user_id": user_id
                })
                if not account_doc:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Securities account not found: {securities_account_id}"
                    )
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid securities_account_id: {str(e)}"
                )
        
        # Clear existing positions for this user (and account if specified)
        delete_query = {"user_id": user_id}
        if securities_account_id:
            delete_query["securities_account_id"] = securities_account_id
        positions_coll.delete_many(delete_query)
        
        # Insert new positions
        if positions:
            for pos in positions:
                pos["user_id"] = user_id
                pos["synced_at"] = current_time
                # Link to securities_account if provided
                if securities_account_id:
                    pos["securities_account_id"] = securities_account_id
                    # Fill broker and account_id from securities_accounts if not present
                    if not pos.get("broker") or not pos.get("account_id"):
                        pos["broker"] = account_doc.get("broker")
                        pos["account_id"] = account_doc.get("account_id")
            
            positions_coll.insert_many(positions)
        
        return {
            "success": True,
            "synced_count": len(positions),
            "timestamp": current_time
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync positions: {str(e)}")


@router.post("/trader/account/sync", summary="Sync account information")
def sync_account(
    account_data: Dict[str, Any] = Body(...),
    securities_account_id: Optional[str] = Body(None),
    current_user: dict = Depends(get_current_active_user)
):
    """Sync account balance and cash information.
    
    Receives account data from broker (via quantTrader) including:
    - Total assets
    - Available cash
    - Frozen cash
    - Market value
    - P&L
    
    Args:
        account_data: Account balance data from broker
        securities_account_id: Optional link to securities_accounts._id
        current_user: Authenticated user
    """
    try:
        collections = get_db_collections()
        accounts_coll = collections["trader_accounts"]
        securities_coll = collections["securities_accounts"]
        
        user_id = current_user["id"]
        current_time = time.time()
        
        # Add metadata
        account_data["user_id"] = user_id
        account_data["synced_at"] = current_time
        
        # Validate and link to securities_account if provided
        if securities_account_id:
            try:
                account_doc = securities_coll.find_one({
                    "_id": ObjectId(securities_account_id),
                    "user_id": user_id
                })
                if not account_doc:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Securities account not found: {securities_account_id}"
                    )
                # Add link and metadata from securities_account
                account_data["securities_account_id"] = securities_account_id
                # Fill broker and account_id from securities_accounts if not already present
                if not account_data.get("broker"):
                    account_data["broker"] = account_doc.get("broker")
                if not account_data.get("account_id"):
                    account_data["account_id"] = account_doc.get("account_id")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid securities_account_id: {str(e)}"
                )
        
        # Build upsert query
        # 关键：必须使用 securities_account_id 作为唯一标识
        # 避免使用 account_id 匹配，因为真实账户和模拟账户可能有相同ID
        if not securities_account_id:
            raise HTTPException(
                status_code=400,
                detail="securities_account_id is required to prevent account conflicts"
            )
        
        query = {
            "user_id": user_id,
            "securities_account_id": securities_account_id
        }
        
        # Upsert (update or insert)
        accounts_coll.update_one(
            query,
            {"$set": account_data},
            upsert=True
        )
        
        return {
            "success": True,
            "timestamp": current_time
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync account: {str(e)}")


@router.post("/trader/positions/snapshot", summary="Store position snapshot")
def store_position_snapshot(
    snapshot: Dict[str, Any] = Body(...),
    securities_account_id: Optional[str] = Body(None),
    current_user: dict = Depends(get_current_active_user)
):
    """Store daily position snapshot for historical tracking.
    
    Creates point-in-time snapshots for:
    - Portfolio performance analysis
    - P&L tracking over time
    - AI training data
    
    Args:
        snapshot: Position snapshot data
        securities_account_id: Optional link to securities_accounts._id
        current_user: Authenticated user
    """
    try:
        collections = get_db_collections()
        snapshots_coll = collections["position_snapshots"]
        securities_coll = collections["securities_accounts"]
        
        user_id = current_user["id"]
        
        # Add user_id
        snapshot["user_id"] = user_id
        
        # Validate and link to securities_account if provided
        if securities_account_id:
            try:
                account_doc = securities_coll.find_one({
                    "_id": ObjectId(securities_account_id),
                    "user_id": user_id
                })
                if account_doc:
                    snapshot["securities_account_id"] = securities_account_id
                    # Fill broker and account_id from securities_accounts if not present
                    if not snapshot.get("broker"):
                        snapshot["broker"] = account_doc.get("broker")
                    if not snapshot.get("account_id"):
                        snapshot["account_id"] = account_doc.get("account_id")
            except Exception:
                pass  # Optional field, don't fail if invalid
        
        # Check if snapshot for this date already exists
        date_str = snapshot.get("date")
        if date_str:
            query = {"user_id": user_id, "date": date_str}
            if securities_account_id:
                query["securities_account_id"] = securities_account_id
            
            existing = snapshots_coll.find_one(query)
            
            if existing:
                # Update existing snapshot
                snapshots_coll.update_one(
                    {"_id": existing["_id"]},
                    {"$set": snapshot}
                )
                return {
                    "success": True,
                    "message": "Snapshot updated",
                    "date": date_str
                }
        
        # Insert new snapshot
        result = snapshots_coll.insert_one(snapshot)
        
        return {
            "success": True,
            "message": "Snapshot created",
            "snapshot_id": str(result.inserted_id)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store snapshot: {str(e)}")


@router.get("/trader/positions", summary="Get current positions")
def get_positions(
    securities_account_id: Optional[str] = Query(None, description="Filter by securities account ID"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get current positions synced from quantTrader."""
    try:
        collections = get_db_collections()
        data_collections = get_data_db()
        positions_coll = collections["trader_positions"]
        stock_info_coll = data_collections["stock_info"]
        
        user_id = current_user["id"]
        
        # Build query
        query = {"user_id": user_id}
        if securities_account_id:
            query["securities_account_id"] = securities_account_id
        
        # Fetch positions
        cursor = positions_coll.find(query)
        positions = [serialize_doc(doc) for doc in cursor]
        
        # Transform field names to match frontend expectations and enrich with stock names
        if positions:
            # Extract unique symbols from positions
            symbols = list(set(pos.get("symbol") for pos in positions if pos.get("symbol")))
            
            # Query stock info for these symbols (symbols now include exchange suffixes)
            stock_info_docs = list(stock_info_coll.find(
                {"symbol": {"$in": symbols}},
                {"symbol": 1, "name": 1}
            ))
            
            # Create symbol to name mapping
            symbol_to_name = {doc["symbol"]: doc.get("name", "") for doc in stock_info_docs}
            
            # Transform field names to match frontend expectations and add stock names
            transformed_positions = []
            for pos in positions:
                transformed_pos = {
                    "symbol": pos.get("symbol", ""),
                    "stock_name": symbol_to_name.get(pos.get("symbol"), ""),
                    "quantity": pos.get("qty", 0),  # Map 'qty' to 'quantity'
                    "available_qty": pos.get("can_use_volume", 0),  # Map 'can_use_volume' to 'available_qty'
                    "frozen_qty": pos.get("frozen_volume", 0),  # Map 'frozen_volume' to 'frozen_qty'
                    "cost_price": pos.get("avg_price", 0.0),  # Map 'avg_price' to 'cost_price'
                    "current_price": pos.get("last_price", 0.0),  # Map 'last_price' to 'current_price'
                    "market_value": pos.get("market_value", 0.0),
                    "on_road_volume": pos.get("on_road_volume", 0),
                    "timestamp": pos.get("timestamp"),
                    "updated_at": pos.get("updated_at"),
                    "account_id": pos.get("account_id"),
                    "broker": pos.get("broker"),
                    "securities_account_id": pos.get("securities_account_id"),
                    "user_id": pos.get("user_id"),
                    "synced_at": pos.get("synced_at"),
                    # Calculate profit/loss fields
                    "unrealized_pnl": pos.get("unrealized_pnl") if pos.get("unrealized_pnl") is not None else (pos.get("market_value", 0) - (pos.get("avg_price", 0) * pos.get("qty", 0))),
                    "unrealized_pnl_pct": pos.get("unrealized_pnl_pct") if pos.get("unrealized_pnl_pct") is not None else ((pos.get("market_value", 0) - (pos.get("avg_price", 0) * pos.get("qty", 0))) / (pos.get("avg_price", 0) * pos.get("qty", 0)) * 100 if (pos.get("avg_price", 0) * pos.get("qty", 0)) != 0 else 0)
                }
                transformed_positions.append(transformed_pos)
            
            positions = transformed_positions
        
        return {
            "success": True,
            "data": positions,
            "count": len(positions)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch positions: {str(e)}")


@router.delete("/trader/positions/cleanup", summary="Cleanup stale positions")
def cleanup_positions(
    current_symbols: List[str] = Body(..., description="List of symbols currently held"),
    securities_account_id: Optional[str] = Body(None),
    current_user: dict = Depends(get_current_active_user)
):
    """Clean up stale positions not currently held by broker.
    
    Removes positions for symbols that are NOT in the current_symbols list.
    This is called after position sync to ensure the database only contains
    positions that are actually held in the broker account.
    
    Args:
        current_symbols: List of symbols currently held in the account
        securities_account_id: Optional filter by securities account
        current_user: Authenticated user
    """
    try:
        collections = get_db_collections()
        positions_coll = collections["trader_positions"]
        
        user_id = current_user["id"]
        
        # Build cleanup query - remove positions NOT in current_symbols
        cleanup_query = {
            "user_id": user_id,
            "symbol": {"$nin": current_symbols}
        }
        
        # Filter by securities_account_id if provided
        if securities_account_id:
            cleanup_query["securities_account_id"] = securities_account_id
        
        # Delete stale positions
        result = positions_coll.delete_many(cleanup_query)
        
        return {
            "success": True,
            "deleted_count": result.deleted_count,
            "timestamp": time.time()
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cleanup positions: {str(e)}")


@router.get("/trader/account", summary="Get account information")
def get_account(
    securities_account_id: Optional[str] = Query(None, description="Filter by securities account ID"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get account balance and cash information."""
    try:
        collections = get_db_collections()
        accounts_coll = collections["trader_accounts"]
        
        user_id = current_user["id"]
        
        # Build query
        query = {"user_id": user_id}
        if securities_account_id:
            query["securities_account_id"] = securities_account_id
        
        # Fetch latest account info
        account = accounts_coll.find_one(
            query,
            sort=[("synced_at", -1)]
        )
        
        if not account:
            return {
                "success": True,
                "data": None
            }
        
        # Transform field names to match frontend expectations
        transformed_account = {
            "total_assets": account.get("total_asset", 0),  # Map 'total_asset' to 'total_assets'
            "available_cash": account.get("available_cash", 0),
            "market_value": account.get("market_value", 0),
            "daily_pnl": account.get("pnl", 0),  # Map 'pnl' to 'daily_pnl'
            "daily_pnl_ratio": account.get("pnl_ratio", 0),  # Map 'pnl_ratio' to 'daily_pnl_ratio'
            "cash": account.get("cash", 0),
            "frozen_cash": account.get("frozen_cash", 0),
            "buying_power": account.get("buying_power", 0),
            "account_type": account.get("account_type"),
            "account_id": account.get("account_id"),
            "broker": account.get("broker"),
            "user_id": account.get("user_id"),
            "securities_account_id": account.get("securities_account_id"),
            "synced_at": account.get("synced_at")
        }
        
        return {
            "success": True,
            "data": transformed_account
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch account: {str(e)}")
