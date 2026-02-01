"""Live strategy execution logs WebSocket endpoint.

Streams real-time strategy execution logs to the UI as strategies execute.
Logs are stored in MongoDB and streamed via WebSocket to connected clients.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from typing import Dict, List, Set
from datetime import datetime
import asyncio
import json
import os
from auth import get_current_active_user
from db import get_db_collections
from utils.stock_utils import serialize_doc
import logging

router = APIRouter()
logger = logging.getLogger("live_logs")

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        # Map: symbol -> set of connected WebSockets
        self.active_connections: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, symbol: str):
        await websocket.accept()
        if symbol not in self.active_connections:
            self.active_connections[symbol] = set()
        self.active_connections[symbol].add(websocket)
        logger.info(f"WebSocket connected for {symbol}. Active connections: {len(self.active_connections[symbol])}")
    
    def disconnect(self, websocket: WebSocket, symbol: str):
        if symbol in self.active_connections:
            self.active_connections[symbol].discard(websocket)
            logger.info(f"WebSocket disconnected for {symbol}. Remaining connections: {len(self.active_connections[symbol])}")
            if not self.active_connections[symbol]:
                del self.active_connections[symbol]
    
    async def broadcast(self, symbol: str, message: dict):
        """Broadcast message to all connected clients for a symbol"""
        if symbol in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[symbol]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.warning(f"Error sending message to connection: {e}")
                    disconnected.add(connection)
            
            # Remove disconnected connections
            for conn in disconnected:
                self.active_connections[symbol].discard(conn)

manager = ConnectionManager()


async def listen_for_logs(websocket: WebSocket, symbol: str):
    """
    Listens for new log entries for a specific symbol using a MongoDB change stream
    and sends them to the client via WebSocket.
    """
    collections = get_db_collections()
    logs_coll = collections.get("strategy_execution_logs")
    if not logs_coll:
        logger.warning(f"Collection 'strategy_execution_logs' not found for symbol {symbol}")
        return

    # Define the pipeline to filter for inserts for the specific symbol
    pipeline = [
        {
            '$match': {
                'operationType': 'insert',
                'fullDocument.symbol': symbol
            }
        }
    ]

    try:
        # Use 'with' to ensure the change stream is properly closed
        with logs_coll.watch(pipeline) as stream:
            logger.info(f"Change stream opened for symbol: {symbol}")
            for change in stream:
                new_log = change['fullDocument']
                # The client expects a specific format
                await websocket.send_json({
                    "type": "stream",
                    "data": serialize_doc(new_log)
                })
    except asyncio.CancelledError:
        logger.info(f"Change stream task cancelled for symbol: {symbol}")
    except Exception as e:
        logger.error(f"Error in change stream for {symbol}: {e}")
    finally:
        logger.info(f"Change stream closed for symbol: {symbol}")


@router.websocket("/ws/strategy-logs/{symbol}")
async def websocket_strategy_logs(websocket: WebSocket, symbol: str):
    """WebSocket endpoint for streaming live strategy execution logs.
    
    - Sends the last 50 logs as initial history.
    - Uses a MongoDB change stream to push new logs in real-time.
    """
    await manager.connect(websocket, symbol)
    listener_task = None
    try:
        # 1. Send initial history
        collections = get_db_collections()
        logs_coll = collections.get("strategy_execution_logs")
        if logs_coll:
            # Fetch the last 50 logs, sorted from oldest to newest for correct display order
            history_cursor = logs_coll.find(
                {'symbol': symbol},
                {'_id': 0}
            ).sort('timestamp', -1).limit(50)
            
            history = list(history_cursor)
            history.reverse() # Reverse to send oldest first
            
            await websocket.send_json({
                "type": "history",
                "data": [serialize_doc(log) for log in history]
            })
            logger.info(f"Sent {len(history)} historical logs to client for {symbol}")

        # 2. Start a background task to listen for new logs
        listener_task = asyncio.create_task(listen_for_logs(websocket, symbol))

        # 3. Keep the connection alive, the listener_task will handle sending data
        while True:
            # This loop can be used to handle incoming messages from the client if needed
            # Or just to keep the connection open.
            await asyncio.sleep(60) # Sleep to prevent a busy loop

    except WebSocketDisconnect:
        logger.info(f"Client for {symbol} disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error for {symbol}: {e}")
    finally:
        if listener_task:
            listener_task.cancel()
        manager.disconnect(websocket, symbol)


@router.get("/live-logs/history", summary="Get strategy execution log history")
async def get_strategy_logs_history(
    symbol: str = Query(..., description="Stock symbol"),
    limit: int = Query(100, description="Number of recent logs to return"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get historical strategy execution logs for a symbol.
    
    Returns the most recent execution logs with execution metrics.
    """
    try:
        collections = get_db_collections()
        logs_coll = collections.get("strategy_execution_logs")
        
        if not logs_coll:
            return {
                "success": True,
                "data": [],
                "count": 0
            }
        
        # Query recent logs for this symbol, sorted by timestamp descending
        logs = list(logs_coll.find(
            {"symbol": symbol},
            {"_id": 0}
        ).sort("timestamp", -1).limit(limit))
        
        # Serialize all logs
        logs = [serialize_doc(log) for log in logs]
        
        return {
            "success": True,
            "data": logs,
            "count": len(logs),
            "symbol": symbol
        }
    
    except Exception as e:
        logger.error(f"Failed to get logs for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": [],
            "count": 0
        }


@router.get("/live-logs/current", summary="Get current strategy execution state")
async def get_current_strategy_log(
    symbol: str = Query(..., description="Stock symbol"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get the most recent execution log for a symbol (current state).
    
    Returns latest call count, data bars, price, etc.
    """
    try:
        collections = get_db_collections()
        logs_coll = collections.get("strategy_execution_logs")
        
        if not logs_coll:
            return {
                "success": True,
                "data": None,
                "symbol": symbol
            }
        
        # Get latest log entry
        latest_log = logs_coll.find_one(
            {"symbol": symbol},
            sort=[("timestamp", -1)]
        )
        
        if latest_log:
            latest_log = serialize_doc(latest_log)
        
        return {
            "success": True,
            "data": latest_log,
            "symbol": symbol,
            "timestamp": datetime.now().timestamp()
        }
    
    except Exception as e:
        logger.error(f"Failed to get current log for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": None,
            "symbol": symbol
        }


@router.get("/live-logs/symbols", summary="Get active symbols with live logs")
async def get_active_symbols(
    current_user: dict = Depends(get_current_active_user)
):
    """Get list of symbols currently being logged (active strategies).
    
    Returns all symbols that have been logging in the last 5 minutes.
    """
    try:
        collections = get_db_collections()
        logs_coll = collections.get("strategy_execution_logs")
        
        if not logs_coll:
            return {
                "success": True,
                "symbols": [],
                "count": 0
            }
        
        # Get all distinct symbols from recent logs (last 5 minutes)
        import time
        cutoff_time = time.time() - 300  # 5 minutes ago
        
        symbols = logs_coll.distinct("symbol", {"timestamp": {"$gte": cutoff_time}})
        
        return {
            "success": True,
            "symbols": sorted(symbols),
            "count": len(symbols),
            "timestamp": datetime.now().timestamp()
        }
    
    except Exception as e:
        logger.error(f"Failed to get active symbols: {e}")
        return {
            "success": False,
            "error": str(e),
            "symbols": [],
            "count": 0
        }


@router.get("/live-logs/stats", summary="Get strategy execution statistics")
async def get_strategy_logs_stats(
    symbol: str = Query(..., description="Stock symbol"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get statistics about strategy execution logs for a symbol.
    
    Returns average call frequency, data bars processed, price range, etc.
    """
    try:
        collections = get_db_collections()
        logs_coll = collections.get("strategy_execution_logs")
        
        if not logs_coll:
            return {
                "success": True,
                "data": {
                    "total_logs": 0,
                    "avg_call_count": 0,
                    "avg_data_bars": 0,
                    "latest_price": 0,
                    "symbol": symbol
                }
            }
        
        # Aggregate statistics
        pipeline = [
            {"$match": {"symbol": symbol}},
            {"$group": {
                "_id": "$symbol",
                "total_logs": {"$sum": 1},
                "avg_call_count": {"$avg": "$call_count"},
                "avg_data_bars": {"$avg": "$data_bars"},
                "max_call_count": {"$max": "$call_count"},
                "max_data_bars": {"$max": "$data_bars"},
                "latest_price": {"$last": "$price"},
                "latest_timestamp": {"$max": "$timestamp"}
            }}
        ]
        
        stats = list(logs_coll.aggregate(pipeline))
        
        if stats:
            data = stats[0]
            data.pop("_id", None)
            data["symbol"] = symbol
        else:
            data = {
                "total_logs": 0,
                "avg_call_count": 0,
                "avg_data_bars": 0,
                "latest_price": 0,
                "symbol": symbol
            }
        
        return {
            "success": True,
            "data": data
        }
    
    except Exception as e:
        logger.error(f"Failed to get stats for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": {}
        }


@router.get("/live-logs/messages", summary="Get strategy messages/alerts history")
async def get_strategy_messages(
    symbol: str = Query(..., description="Stock symbol"),
    message_type: str = Query(None, description="Filter by message type: analysis, signal, buy, sell, alert, etc."),
    limit: int = Query(50, description="Number of recent messages to return"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get historical strategy messages (analysis, signals, trades, alerts, etc.).
    
    Returns the most recent messages with full context and metadata.
    """
    try:
        collections = get_db_collections()
        messages_coll = collections.get("strategy_messages")
        
        if not messages_coll:
            return {
                "success": True,
                "data": [],
                "count": 0,
                "symbol": symbol
            }
        
        # Build query
        query = {"symbol": symbol}
        if message_type:
            query["message_type"] = message_type
        
        # Query recent messages, sorted by timestamp descending
        messages = list(messages_coll.find(
            query,
            {"_id": 0}
        ).sort("timestamp", -1).limit(limit))
        
        # Serialize all messages
        messages = [serialize_doc(msg) for msg in messages]
        
        return {
            "success": True,
            "data": messages,
            "count": len(messages),
            "symbol": symbol
        }
    
    except Exception as e:
        logger.error(f"Failed to get messages for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": [],
            "count": 0
        }


@router.get("/live-logs/latest-message", summary="Get latest strategy message")
async def get_latest_message(
    symbol: str = Query(..., description="Stock symbol"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get the most recent message from a strategy.
    
    Useful for showing current status/alert in a compact format.
    """
    try:
        collections = get_db_collections()
        messages_coll = collections.get("strategy_messages_latest")
        
        if not messages_coll:
            return {
                "success": True,
                "data": None,
                "symbol": symbol
            }
        
        latest_msg = messages_coll.find_one(
            {"symbol": symbol},
            {"_id": 0}
        )
        
        if latest_msg:
            latest_msg = serialize_doc(latest_msg)
        
        return {
            "success": True,
            "data": latest_msg,
            "symbol": symbol,
            "timestamp": datetime.now().timestamp()
        }
    
    except Exception as e:
        logger.error(f"Failed to get latest message for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": None,
            "symbol": symbol
        }
