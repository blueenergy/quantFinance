"""Trader API endpoints for quantTrader client.

Provides endpoints for:
- Fetching trading signals
- Reporting execution results
- Syncing positions and account data
- Trade history queries

This is the bridge between quantTrader (client) and quantFinance (backend).
"""

from fastapi import APIRouter, Depends, Query, HTTPException, Body
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging
import threading
import time
from auth import get_current_active_user
from db import get_db_collections, get_data_db, get_trading_db_collections
from utils.stock_utils import serialize_doc
from utils.time_display import add_display_time_fields
from bson import ObjectId
from services.live_equity import list_account_equity
from services.trader_execution_upload import upsert_trader_execution
from services.portfolio_live_snapshot import rebuild_live_snapshot_for_plan_guarded
from services import trader_signal_reprice as signal_reprice

router = APIRouter()
logger = logging.getLogger(__name__)

_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        "filled",
        "executed",
        "rejected",
        "cancelled",
        "canceled",
        "partial_cancelled",
        "failed",
    }
)


def _schedule_live_snapshot_rebuild_for_execution(
    *,
    user_id: str,
    order_id: str,
    execution_status: str,
    signals_coll: Any,
) -> None:
    """Rebuild portfolio live snapshot after a terminal execution upsert."""
    if execution_status not in _TERMINAL_EXECUTION_STATUSES:
        return

    signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
    if not signal:
        return
    plan_id = signal.get("plan_id")
    if not plan_id:
        return

    def _run() -> None:
        try:
            trading_collections = get_trading_db_collections()
            finance_collections = get_db_collections()
            plan = trading_collections["portfolio_trade_plans"].find_one(
                {"plan_id": plan_id, "user_id": user_id}
            )
            if not plan:
                return
            rebuild_live_snapshot_for_plan_guarded(
                trading_collections,
                finance_collections,
                plan,
                user_id,
            )
        except Exception:
            logger.exception(
                "Failed to rebuild live snapshot after execution upsert: "
                "order_id=%s plan_id=%s",
                order_id,
                plan_id,
            )

    threading.Thread(target=_run, daemon=True).start()


def _require_securities_account_doc(
    securities_coll,
    *,
    securities_account_id: Optional[str],
    user_id: str,
) -> Dict[str, Any]:
    if not securities_account_id:
        raise HTTPException(
            status_code=400,
            detail="securities_account_id is required to prevent account conflicts",
        )

    query_ids: List[Any] = [securities_account_id]
    try:
        query_ids.insert(0, ObjectId(securities_account_id))
    except Exception:
        pass

    for query_id in query_ids:
        account_doc = securities_coll.find_one({"_id": query_id, "user_id": user_id})
        if account_doc:
            return account_doc

    raise HTTPException(
        status_code=404,
        detail=f"Securities account not found: {securities_account_id}",
    )

# Fields quantTrader may set via POST /trader/signals/{order_id}/status (allowlist).
_TRADER_SIGNAL_STATUS_ALLOWED_FIELDS = frozenset(
    {
        "status",
        "qmt_order_id",
        "broker_order_id",
        "retry_count",
        "last_error",
        "error",
        "submitted_at",
        "executed_at",
        "filled_qty",
        "avg_price",
        "effective_limit_price",
        "execution_phase",
        "execution_priority",
        "plan_item_rank",
        "remaining_size",
        "chase_suggestion",
        "cancel_requested_at",
        # submitted / poll diagnostics
        "submitted_age_seconds",
        "broker_status",
        "broker_status_msg",
        "last_status_checked_at",
        "last_market_price",
    }
)


def _attach_stock_names(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    missing = [row for row in rows if row.get("symbol") and not (row.get("name") or row.get("stock_name"))]
    if not missing:
        return rows
    symbols = {str(row.get("symbol")) for row in missing}
    base_symbols = {symbol.split(".", 1)[0] for symbol in symbols}
    try:
        stock_info = get_data_db()["stock_info"]
        stock_rows = list(stock_info.find({"symbol": {"$in": list(symbols | base_symbols)}}))
    except Exception:
        return rows
    names = {}
    for stock in stock_rows:
        symbol = str(stock.get("symbol") or "")
        name = stock.get("name") or stock.get("stock_name")
        if symbol and name:
            names[symbol] = name
    for row in missing:
        symbol = str(row.get("symbol") or "")
        name = names.get(symbol) or names.get(symbol.split(".", 1)[0])
        if name:
            row["name"] = name
    return rows


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
            query["status"] = {"$in": ["pending", "retry_pending", "submitted", "partial_filled", "cancel_requested"]}
        else:
            query["status"] = {"$in": ["pending", "retry_pending"]}
        
        # Fetch signals sorted by timestamp, then apply execution intent ordering
        # locally for backwards-compatible documents that may not have phase fields.
        cursor = signals_coll.find(query).sort("timestamp", 1).limit(limit)
        signals = [serialize_doc(doc) for doc in cursor]
        signals.sort(
            key=lambda row: (
                0 if str(row.get("execution_phase") or row.get("action") or "").lower() == "sell" else 1,
                int(row.get("execution_priority") or 1000),
                float(row.get("timestamp") or 0),
                str(row.get("order_id") or ""),
            )
        )
        
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
        
        if "status" not in status_update:
            raise HTTPException(status_code=400, detail="status is required")

        filtered = {
            k: v for k, v in status_update.items() if k in _TRADER_SIGNAL_STATUS_ALLOWED_FIELDS
        }
        filtered["updated_at"] = time.time()

        result = signals_coll.update_one(
            {"order_id": order_id, "user_id": user_id},
            {"$set": filtered},
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
        execution_id, inserted = upsert_trader_execution(
            dict(execution),
            user_id,
            executions_coll=executions_coll,
            signals_coll=signals_coll,
        )

        _schedule_live_snapshot_rebuild_for_execution(
            user_id=user_id,
            order_id=str(execution.get("order_id") or ""),
            execution_status=str(execution.get("status") or "filled"),
            signals_coll=signals_coll,
        )

        return {
            "success": True,
            "message": "Execution recorded",
            "execution_id": execution_id,
            "inserted": inserted,
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create execution: {str(e)}")


class RepriceConfirmBody(BaseModel):
    """Confirm replacement buy signal after preview (manual pricing)."""

    effective_limit_price: float = Field(..., gt=0, description="Limit price for the new buy order")
    size: Optional[int] = Field(None, gt=0, description="Override size; default from cancelled signal")
    reason: str = Field("", max_length=2000)


@router.post("/trader/signals/{order_id}/reprice", summary="Preview repricing for a cancelled buy signal")
def preview_signal_reprice(
    order_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        collections = get_db_collections()
        return signal_reprice.build_reprice_preview(
            collections,
            user_id=str(current_user["id"]),
            order_id=order_id,
        )
    except signal_reprice.RepriceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/trader/signals/{order_id}/reprice/confirm",
    summary="Create replacement buy signal (manual confirm)",
)
def confirm_signal_reprice(
    order_id: str,
    body: RepriceConfirmBody,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        collections = get_db_collections()
        return signal_reprice.confirm_reprice_and_insert_signal(
            collections,
            user_id=str(current_user["id"]),
            order_id=order_id,
            effective_limit_price=float(body.effective_limit_price),
            size_override=body.size,
            reason=body.reason or "",
            reviewer=str(
                current_user.get("username")
                or current_user.get("email")
                or current_user.get("id")
                or "unknown"
            ),
        )
    except signal_reprice.RepriceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/trader/live-signals", summary="List live trading signals")
def list_live_signals(
    securities_account_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_active_user)
):
    try:
        collections = get_db_collections()
        query: Dict[str, Any] = {"user_id": current_user["id"], "mode": "live"}
        if securities_account_id:
            query["securities_account_id"] = securities_account_id
        if status:
            query["status"] = status
        rows = _attach_stock_names(list(collections["trade_signals"].find(query).sort("created_at", -1).limit(limit)))
        return {"success": True, "data": [serialize_doc(doc) for doc in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list live signals: {str(e)}")


@router.get("/trader/executions", summary="List trade executions")
def list_executions(
    securities_account_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    plan_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_active_user)
):
    try:
        collections = get_db_collections()
        user_id = current_user["id"]
        query: Dict[str, Any] = {"user_id": user_id}
        if securities_account_id:
            query["securities_account_id"] = securities_account_id
        if status:
            query["status"] = status
        if plan_id:
            signal_query: Dict[str, Any] = {"user_id": user_id, "plan_id": plan_id, "mode": "live"}
            if securities_account_id:
                signal_query["securities_account_id"] = securities_account_id
            signals = list(collections["trade_signals"].find(signal_query, {"_id": 0, "order_id": 1}))
            order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
            if not order_ids:
                return {"success": True, "data": [], "count": 0}
            query["order_id"] = {"$in": order_ids}
        rows = list(collections["trade_executions"].find(query).sort("timestamp", -1).limit(limit))
        return {"success": True, "data": [add_display_time_fields(serialize_doc(doc)) for doc in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list executions: {str(e)}")


@router.post("/trader/heartbeat", summary="Record quantTrader heartbeat")
def record_trader_heartbeat(
    heartbeat: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_active_user)
):
    try:
        collections = get_db_collections()
        user_id = current_user["id"]
        now = time.time()
        worker_id = heartbeat.get("worker_id") or f"quantTrader:{heartbeat.get('securities_account_id') or heartbeat.get('account_id') or user_id}"
        doc = {
            **heartbeat,
            "worker_id": worker_id,
            "worker_type": "quantTrader",
            "user_id": user_id,
            "last_seen_at": now,
            "status": heartbeat.get("status") or "running",
        }
        collections["worker_status"].update_one(
            {"worker_id": worker_id, "user_id": user_id},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return {"success": True, "data": serialize_doc(doc)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record heartbeat: {str(e)}")


@router.get("/trader/heartbeat", summary="List quantTrader heartbeat status")
def list_trader_heartbeats(
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_active_user)
):
    try:
        collections = get_db_collections()
        query = {"user_id": current_user["id"], "worker_type": "quantTrader"}
        rows = list(collections["worker_status"].find(query).sort("last_seen_at", -1).limit(limit))
        return {"success": True, "data": [serialize_doc(doc) for doc in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list heartbeats: {str(e)}")


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
        
        account_doc = _require_securities_account_doc(
            securities_coll,
            securities_account_id=securities_account_id,
            user_id=user_id,
        )
        
        # Clear existing positions for this user (and account if specified)
        delete_query = {"user_id": user_id, "securities_account_id": securities_account_id}
        positions_coll.delete_many(delete_query)
        
        # Insert new positions
        if positions:
            for pos in positions:
                pos["user_id"] = user_id
                pos["synced_at"] = current_time
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
        
    except HTTPException:
        raise
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
        
        account_doc = _require_securities_account_doc(
            securities_coll,
            securities_account_id=securities_account_id,
            user_id=user_id,
        )
        # Add link and metadata from securities_account
        account_data["securities_account_id"] = securities_account_id
        # Fill broker and account_id from securities_accounts if not already present
        if not account_data.get("broker"):
            account_data["broker"] = account_doc.get("broker")
        if not account_data.get("account_id"):
            account_data["account_id"] = account_doc.get("account_id")
        
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
        
    except HTTPException:
        raise
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
        
        account_doc = _require_securities_account_doc(
            securities_coll,
            securities_account_id=securities_account_id,
            user_id=user_id,
        )
        snapshot["securities_account_id"] = securities_account_id
        # Fill broker and account_id from securities_accounts if not present
        if not snapshot.get("broker"):
            snapshot["broker"] = account_doc.get("broker")
        if not snapshot.get("account_id"):
            snapshot["account_id"] = account_doc.get("account_id")
        
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
        
    except HTTPException:
        raise
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
        securities_coll = collections["securities_accounts"]
        
        user_id = current_user["id"]
        _require_securities_account_doc(
            securities_coll,
            securities_account_id=securities_account_id,
            user_id=user_id,
        )
        
        # Build cleanup query - remove positions NOT in current_symbols
        cleanup_query = {
            "user_id": user_id,
            "symbol": {"$nin": current_symbols}
        }
        
        cleanup_query["securities_account_id"] = securities_account_id
        
        # Delete stale positions
        result = positions_coll.delete_many(cleanup_query)
        
        return {
            "success": True,
            "deleted_count": result.deleted_count,
            "timestamp": time.time()
        }
        
    except HTTPException:
        raise
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


@router.get("/trader/account/equity", summary="Get account equity curve")
def get_account_equity(
    securities_account_id: Optional[str] = Query(None, description="Filter by securities account ID"),
    start_date: Optional[str] = Query(None, description="Start date YYYYMMDD or YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    """Get persisted account-level daily equity snapshots."""
    try:
        collections = get_db_collections()
        rows = list_account_equity(
            collections,
            user_id=current_user["id"],
            securities_account_id=securities_account_id,
            start_date=start_date,
            end_date=end_date,
        )
        return {"success": True, "data": [serialize_doc(row) for row in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch account equity: {str(e)}")
