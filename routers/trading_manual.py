"""
Manual trading operations API
- Retry failed trade signals
- Manually create trade signals
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
from db import get_db_collections
from auth import get_current_active_user
from services.live_trade_signals import default_valid_until
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol
import time
import uuid

router = APIRouter()

_TERMINAL_RETRY_STATUSES = frozenset({"cancelled", "canceled", "partial_cancelled"})
_TERMINAL_RETRY_UNSET_FIELDS = {
    "qmt_order_id": "",
    "broker_order_id": "",
    "cancel_requested_at": "",
    "submitted_at": "",
    "filled_qty": "",
    "avg_price": "",
    "chase_suggestion": "",
    "broker_status": "",
    "broker_status_msg": "",
    "remaining_size": "",
}


def _parse_optional_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _retry_valid_until(signal: Dict[str, Any]) -> datetime:
    """Fresh expiry window for a manual retry, anchored to now (or future activate_after)."""
    now = datetime.now(timezone.utc)
    activate_after = _parse_optional_datetime(signal.get("activate_after"))
    if activate_after is not None and activate_after > now:
        return default_valid_until(now, activate_after)
    return default_valid_until(now, now)


def _load_user_securities_account(
    securities_accounts_coll,
    *,
    user_id: str,
    securities_account_id: str,
) -> Dict[str, Any]:
    if not securities_account_id:
        raise HTTPException(status_code=400, detail="请选择证券账户")

    query_ids = [securities_account_id]
    try:
        query_ids.insert(0, ObjectId(securities_account_id))
    except (InvalidId, TypeError):
        pass

    for query_id in query_ids:
        account = securities_accounts_coll.find_one({"_id": query_id, "user_id": user_id})
        if account:
            return account

    raise HTTPException(status_code=404, detail="证券账户不存在或无权访问")


@router.post("/trade-signals/manual-retry/{order_id}", summary="手动重试失败的交易信号")
def manual_retry_failed_signal(
    order_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """手动重试一个失败的交易信号，将其状态重置为pending以便重新执行"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 查找指定的信号
        signal = signals_coll.find_one({
            "order_id": order_id,
            "user_id": user_id
        })
        
        if not signal:
            raise HTTPException(status_code=404, detail=f"交易信号不存在: {order_id}")
        
        current_status = signal.get("status", "unknown")

        # 检查是否为可以重试的状态
        retryable_statuses = ["failed", "rejected", "retry_pending", "cancelled", "canceled", "partial_cancelled"]
        if current_status not in retryable_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"信号状态为 {current_status}，无法重试。仅支持 {', '.join(retryable_statuses)} 状态的信号。"
            )

        is_terminal_retry = current_status in _TERMINAL_RETRY_STATUSES

        # 重置状态为 pending，重置重试次数；重算 valid_until 避免沿用已过期窗口
        update_doc = {
            "$set": {
                "status": "pending",
                "retry_count": 0,
                "last_error": None,
                "updated_at": time.time(),
                "retried_at": time.time(),  # 记录重试时间
                "retried_by": user_id,  # 记录重试用户
                "valid_until": _retry_valid_until(signal),
            }
        }
        # For terminal states, clear old QMT tracking fields so the trader
        # doesn't re-claim the cancelled QMT order by order_remark.
        if is_terminal_retry:
            update_doc["$unset"] = dict(_TERMINAL_RETRY_UNSET_FIELDS)

        update_result = signals_coll.update_one(
            {"order_id": order_id, "user_id": user_id},
            update_doc
        )
        
        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="更新信号状态失败")
        
        return {
            "success": True,
            "message": f"信号 {order_id} 已重置为待执行状态",
            "order_id": order_id,
            "previous_status": current_status,
            "new_status": "pending"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重试信号失败: {str(e)}")


@router.post("/trade-signals/manual-create", summary="手动创建交易信号")
def manual_create_signal(
    request: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user)
):
    """手动创建一个交易信号，模拟手动下单"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 从请求体获取参数
        symbol = request.get("symbol")
        action = request.get("action")
        size = request.get("size")
        price = request.get("price")
        strategy = request.get("strategy", "manual")
        securities_account_id = str(request.get("securities_account_id") or "").strip()
        
        if not symbol:
            raise HTTPException(status_code=400, detail="股票代码不能为空")
        
        if not action:
            raise HTTPException(status_code=400, detail="操作类型不能为空")
        
        if not size:
            raise HTTPException(status_code=400, detail="交易数量不能为空")
        
        # 规范化股票代码
        normalized_symbol = normalize_symbol(symbol)
        
        # 验证参数
        if action.lower() not in ["buy", "sell"]:
            raise HTTPException(status_code=400, detail="操作类型必须是 'buy' 或 'sell'")
        
        if size <= 0:
            raise HTTPException(status_code=400, detail="交易数量必须大于0")
        
        # 手动交易必须显式选择证券账户，避免多账户场景下误下到第一个账户。
        securities_accounts_coll = collections["securities_accounts"]
        user_account = _load_user_securities_account(
            securities_accounts_coll,
            user_id=user_id,
            securities_account_id=securities_account_id,
        )
        securities_account_id = str(user_account["_id"])
        
        # 生成订单ID
        order_id = f"manual_{int(time.time())}_{str(uuid.uuid4())[:8]}"
        
        # 创建信号文档
        signal_doc = {
            "order_id": order_id,
            "symbol": normalized_symbol,
            "action": action.lower(),
            "size": size,
            "price": price,
            "strategy": strategy,
            "user_id": user_id,
            "securities_account_id": securities_account_id,  # 关联证券账户
            "broker": user_account.get("broker", ""),
            "account_id": user_account.get("account_id", ""),
            "status": "pending",  # 新建信号为待执行状态
            "retry_count": 0,
            "timestamp": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_executable": True,  # 标记为可执行
            "mode": "live",  # 实盘模式
            "source": "manual",  # 标记为手动创建
            "created_by": user_id,
            "manual_entry": True  # 标记为手动输入
        }
        
        # 插入信号
        result = signals_coll.insert_one(signal_doc)
        
        return {
            "success": True,
            "message": "手动交易信号创建成功",
            "order_id": order_id,
            "signal": serialize_doc(signal_doc)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建信号失败: {str(e)}")


@router.get("/trade-signals/failed", summary="获取失败的交易信号")
def get_failed_signals(
    limit: int = Query(100, description="返回记录数量限制"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取当前用户失败的交易信号列表"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 查询失败的信号
        failed_signals = list(signals_coll.find({
            "user_id": user_id,
            "status": {"$in": ["failed", "rejected"]}
        }).sort("timestamp", -1).limit(limit))
        
        signals = [serialize_doc(doc) for doc in failed_signals]
        
        return {
            "success": True,
            "data": signals,
            "count": len(signals)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败信号失败: {str(e)}")


@router.get("/trade-signals/manual", summary="获取手动创建的交易信号")
def get_manual_signals(
    limit: int = Query(100, description="返回记录数量限制"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取当前用户手动创建的交易信号列表"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 查询手动创建的信号
        manual_signals = list(signals_coll.find({
            "user_id": user_id,
            "$or": [
                {"source": "manual"},
                {"manual_entry": True}
            ]
        }).sort("timestamp", -1).limit(limit))
        
        signals = [serialize_doc(doc) for doc in manual_signals]
        
        return {
            "success": True,
            "data": signals,
            "count": len(signals)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询手动信号失败: {str(e)}")


@router.post("/trade-signals/batch-retry", summary="批量重试失败的交易信号")
def batch_retry_failed_signals(
    status_filter: str = Query("failed", description="状态筛选: failed, rejected, retry_pending, cancelled, all_failed"),
    limit: int = Query(10, description="最大重试数量"),
    current_user: dict = Depends(get_current_active_user)
):
    """批量重试失败的交易信号"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]

        user_id = current_user["id"]

        # 确定要重试的状态
        valid_statuses = {
            "failed": ["failed"],
            "rejected": ["rejected"],
            "retry_pending": ["retry_pending"],
            "cancelled": ["cancelled", "canceled", "partial_cancelled"],
            "all_failed": ["failed", "rejected", "retry_pending", "cancelled", "canceled", "partial_cancelled"],
        }

        status_list = valid_statuses.get(status_filter, ["failed"])

        # 查询符合条件的信号
        signals_to_retry = list(signals_coll.find({
            "user_id": user_id,
            "status": {"$in": status_list}
        }).sort("timestamp", -1).limit(limit))

        if not signals_to_retry:
            return {
                "success": True,
                "message": f"没有找到状态为 {status_filter} 的信号",
                "retry_count": 0
            }

        # 批量更新：每个信号单独 update_one（支持 $unset）
        retried_count = 0
        retried_order_ids = []
        for signal_doc in signals_to_retry:
            order_id = signal_doc["order_id"]
            status = signal_doc.get("status", "")
            update_doc: Dict[str, Any] = {
                "$set": {
                    "status": "pending",
                    "retry_count": 0,
                    "last_error": None,
                    "updated_at": time.time(),
                    "retried_at": time.time(),
                    "retried_by": user_id,
                    "valid_until": _retry_valid_until(signal_doc),
                }
            }
            if status in _TERMINAL_RETRY_STATUSES:
                update_doc["$unset"] = dict(_TERMINAL_RETRY_UNSET_FIELDS)
            result = signals_coll.update_one(
                {"order_id": order_id, "user_id": user_id},
                update_doc,
            )
            if result.modified_count:
                retried_count += 1
                retried_order_ids.append(order_id)

        return {
            "success": True,
            "message": f"成功重试 {retried_count} 个信号",
            "retry_count": retried_count,
            "order_ids": retried_order_ids,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量重试失败: {str(e)}")


@router.get("/trade-signals/retry-pending", summary="获取待重试的交易信号")
def get_retry_pending_signals(
    limit: int = Query(100, description="返回记录数量限制"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取当前用户待重试的交易信号列表"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 查询待重试的信号
        retry_pending_signals = list(signals_coll.find({
            "user_id": user_id,
            "status": "retry_pending"
        }).sort("timestamp", -1).limit(limit))
        
        signals = [serialize_doc(doc) for doc in retry_pending_signals]
        
        return {
            "success": True,
            "data": signals,
            "count": len(signals)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询待重试信号失败: {str(e)}")


@router.get("/trade-signals/status-summary", summary="获取交易信号状态摘要")
def get_trade_signals_status_summary(
    current_user: dict = Depends(get_current_active_user)
):
    """获取交易信号状态摘要统计"""
    try:
        collections = get_db_collections()
        signals_coll = collections["trade_signals"]
        
        user_id = current_user["id"]
        
        # 统计各种状态的信号数量
        status_counts = {}
        for status in ["pending", "retry_pending", "submitted", "filled", "partial_filled", "rejected", "failed"]:
            count = signals_coll.count_documents({
                "user_id": user_id,
                "status": status
            })
            status_counts[status] = count
        
        # 总数
        total = sum(status_counts.values())
        
        return {
            "success": True,
            "summary": status_counts,
            "total": total
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询信号状态摘要失败: {str(e)}")
