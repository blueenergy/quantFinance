from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any, List
from db import get_db_collections
from datetime import datetime
from bson import ObjectId

router = APIRouter(prefix="/permissions", tags=["permissions"])

@router.get("/user/{user_identifier}")
def get_user_permissions(user_identifier: str) -> Dict[str, Any]:
    """Get user's service level and permissions by username or user_id."""
    collections = get_db_collections()
    user_col = collections.get("users")
    
    if user_col is None:
        raise HTTPException(status_code=500, detail="Users collection not available")
    
    # Try to find user by username first, then by user_id
    user = user_col.find_one({"username": user_identifier}, {"_id": 0})
    if not user:
        # If not found by username, try to find by user_id
        user = user_col.find_one({"_id": user_identifier}, {"_id": 0})
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Return user permissions
    permissions = {
        "user_id": user.get("id", user_identifier),
        "username": user.get("username", "unknown"),
        "service_level": user.get("service_level", "free"),
        "features": user.get("features", {}),
        "permissions": user.get("permissions", {}),
        "updated_at": user.get("updated_at")
    }
    
    return {"ok": True, "permissions": permissions}


@router.put("/user/{user_identifier}")
def update_user_permissions(user_identifier: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update user's service level and permissions by username or user_id."""
    collections = get_db_collections()
    user_col = collections.get("users")
    
    if user_col is None:
        raise HTTPException(status_code=500, detail="Users collection not available")
    
    # Try to find user by username first, then by user_id
    user = user_col.find_one({"username": user_identifier})
    if not user:
        # If not found by username, try to find by user_id
        user = user_col.find_one({"_id": user_identifier})
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prepare update data
    update_data = {
        "service_level": payload.get("service_level", "free"),
        "features": payload.get("features", {}),
        "permissions": payload.get("permissions", {}),
        "updated_at": datetime.utcnow()
    }
    
    # Update user by username or _id
    if user.get("username") == user_identifier:
        result = user_col.update_one(
            {"username": user_identifier},
            {"$set": update_data}
        )
    else:
        result = user_col.update_one(
            {"_id": user_identifier},
            {"$set": update_data}
        )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update user permissions")
    
    return {"ok": True, "message": f"User {user_identifier} permissions updated successfully"}


@router.get("/strategies")
def get_strategy_permissions() -> Dict[str, Any]:
    """Get all strategies and their service level requirements."""
    collections = get_db_collections()
    strategy_col = collections.get("strategy_metadata")
    
    if strategy_col is None:
        raise HTTPException(status_code=500, detail="Strategy metadata collection not available")
    
    strategies = list(strategy_col.find({}, {"_id": 0, "key": 1, "name": 1, "min_service_level": 1}))
    
    return {"ok": True, "strategies": strategies}


@router.put("/strategy/{strategy_key}")
def update_strategy_permissions(strategy_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update a strategy's service level requirement."""
    collections = get_db_collections()
    strategy_col = collections.get("strategy_metadata")
    
    if strategy_col is None:
        raise HTTPException(status_code=500, detail="Strategy metadata collection not available")
    
    # Check if strategy exists
    strategy = strategy_col.find_one({"key": strategy_key})
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    # Prepare update data
    min_service_level = payload.get("min_service_level", "free")
    
    # Validate service level
    valid_levels = ["free", "basic", "vip", "premium"]
    if min_service_level not in valid_levels:
        raise HTTPException(status_code=400, detail=f"Invalid service level. Valid values: {valid_levels}")
    
    # Update strategy
    result = strategy_col.update_one(
        {"key": strategy_key},
        {"$set": {"min_service_level": min_service_level, "updated_at": datetime.utcnow()}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update strategy permissions")
    
    return {"ok": True, "message": f"Strategy {strategy_key} permissions updated to {min_service_level}"}


@router.get("/service-levels")
def get_service_levels() -> Dict[str, Any]:
    """Get all available service levels and their features."""
    # Define service levels with their features
    service_levels = {
        "free": {
            "name": "免费版",
            "description": "基础功能",
            "features": {
                "max_strategies": 5,
                "max_symbols": 10,
                "backtest_access": True,
                "live_trading": False,
                "vip_strategies": False
            }
        },
        "basic": {
            "name": "基础版", 
            "description": "增强功能",
            "features": {
                "max_strategies": 10,
                "max_symbols": 20,
                "backtest_access": True,
                "live_trading": True,
                "vip_strategies": False
            }
        },
        "vip": {
            "name": "VIP版",
            "description": "高级功能",
            "features": {
                "max_strategies": 20,
                "max_symbols": 50,
                "backtest_access": True,
                "live_trading": True,
                "vip_strategies": True
            }
        },
        "premium": {
            "name": "专业版",
            "description": "专业功能",
            "features": {
                "max_strategies": 50,
                "max_symbols": 100,
                "backtest_access": True,
                "live_trading": True,
                "vip_strategies": True,
                "priority_support": True,
                "custom_indicators": True
            }
        }
    }
    
    return {"ok": True, "service_levels": service_levels}


@router.post("/upgrade-requests")
def submit_upgrade_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Submit a service level upgrade request for a user."""
    collections = get_db_collections()
    upgrade_requests_col = collections.get("upgrade_requests")
    users_col = collections.get("users")
    
    if upgrade_requests_col is None or users_col is None:
        raise HTTPException(status_code=500, detail="Collections not available")
    
    # Verify user exists
    user = users_col.find_one({"username": payload["username"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Validate target service level
    valid_levels = ["free", "basic", "vip", "premium"]
    target_level = payload.get("target_service_level", "basic")
    if target_level not in valid_levels:
        raise HTTPException(status_code=400, detail=f"Invalid service level. Valid values: {valid_levels}")
    
    # Check if user already has this level or higher
    current_level = user.get("service_level", "free")
    level_priority = {"free": 0, "basic": 1, "vip": 2, "premium": 3}
    if level_priority.get(current_level, 0) >= level_priority.get(target_level, 0):
        raise HTTPException(status_code=400, detail="User already has this service level or higher")
    
    # Check if there's already a pending request
    existing_request = upgrade_requests_col.find_one({
        "username": payload["username"],
        "status": "pending"
    })
    if existing_request:
        raise HTTPException(status_code=400, detail="User already has a pending upgrade request")
    
    # Create the upgrade request
    request_data = {
        "username": payload["username"],
        "current_service_level": current_level,
        "target_service_level": target_level,
        "reason": payload.get("reason", ""),
        "status": "pending",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    result = upgrade_requests_col.insert_one(request_data)
    
    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to create upgrade request")
    
    return {
        "ok": True, 
        "message": f"Upgrade request submitted for user {payload['username']} to {target_level}",
        "request_id": str(result.inserted_id)
    }


@router.get("/upgrade-requests")
def get_upgrade_requests() -> Dict[str, Any]:
    """Get all upgrade requests (for admin use)."""
    try:
        collections = get_db_collections()
        upgrade_requests_col = collections.get("upgrade_requests")
        
        if upgrade_requests_col is None:
            raise HTTPException(status_code=500, detail="Upgrade requests collection not available")
        
        requests = list(upgrade_requests_col.find({}, {"_id": 1, "username": 1, 
                                               "current_service_level": 1, 
                                               "target_service_level": 1, 
                                               "reason": 1, 
                                               "status": 1, 
                                               "created_at": 1, 
                                               "updated_at": 1}))
        
        # Convert ObjectId to string
        for req in requests:
            req["id"] = str(req["_id"])
            del req["_id"]
        
        return {"ok": True, "requests": requests}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in get_upgrade_requests: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.put("/upgrade-requests/{request_id}")
def update_upgrade_request(request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Approve or reject an upgrade request (for admin use)."""
    collections = get_db_collections()
    upgrade_requests_col = collections.get("upgrade_requests")
    users_col = collections.get("users")
    
    if upgrade_requests_col is None or users_col is None:
        raise HTTPException(status_code=500, detail="Collections not available")
    
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")
    
    # Get the request
    request = upgrade_requests_col.find_one({"_id": obj_id})
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    
    # Check if request is already processed
    if request["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")
    
    # Update request status
    new_status = payload.get("status", "pending")
    if new_status not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="Status must be 'approved' or 'rejected'")
    
    update_data = {
        "status": new_status,
        "updated_at": datetime.utcnow(),
        "processed_by": payload.get("processed_by", "admin")
    }
    
    result = upgrade_requests_col.update_one(
        {"_id": obj_id},
        {"$set": update_data}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update request status")
    
    # If approved, update user's service level
    if new_status == "approved":
        print(f"[审批通过] 准备更新用户 {request['username']} 的服务等级到 {request['target_service_level']}")
        
        # 先查找用户
        user = users_col.find_one({"username": request["username"]})
        if not user:
            print(f"[错误] 找不到用户: {request['username']}")
            raise HTTPException(status_code=404, detail=f"User {request['username']} not found")
        
        print(f"[找到用户] username={user.get('username')}, 当前service_level={user.get('service_level', 'free')}")
        
        user_result = users_col.update_one(
            {"username": request["username"]},
            {"$set": {"service_level": request["target_service_level"], "updated_at": datetime.utcnow()}}
        )
        
        print(f"[更新结果] matched_count={user_result.matched_count}, modified_count={user_result.modified_count}")
        
        if user_result.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"User {request['username']} not found in database")
        
        # 验证更新
        updated_user = users_col.find_one({"username": request["username"]})
        print(f"[验证更新] 更新后的service_level={updated_user.get('service_level')}")
    
    return {
        "ok": True, 
        "message": f"Upgrade request {new_status} for user {request['username']}",
        "status": new_status
    }