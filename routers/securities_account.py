from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from pymongo.collection import Collection
from models.all_models import (
    SecuritiesAccountCreate,
    SecuritiesAccountUpdate,
    SecuritiesAccountResponse,
)
from auth import get_current_active_user
from db import get_db_collections

router = APIRouter()

def _encrypt_password(raw: str) -> str:
    # Placeholder: replace with KMS/Fernet integration later
    return raw


def _account_response(d: dict) -> SecuritiesAccountResponse:
    return SecuritiesAccountResponse(
        id=str(d["_id"]),
        broker=d["broker"],
        account_id=d["account_id"],
        account_type=d.get("account_type", "real"),
        created_at=d.get("created_at", datetime.utcnow()),
        updated_at=d.get("updated_at"),
        initial_cash=d.get("initial_cash"),
        live_trading_enabled=bool(d.get("live_trading_enabled", False)),
        risk_limits=d.get("risk_limits"),
        fee_model=d.get("fee_model"),
    )


def _normalize_risk_limits(raw: Optional[dict]) -> dict:
    if not raw:
        return {}
    allowed_fields = ("max_order_amount", "max_daily_amount", "max_position_pct")
    limits = {}
    for field in allowed_fields:
        value = raw.get(field)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{field} 必须是数字")
        if number < 0:
            raise HTTPException(status_code=400, detail=f"{field} 不能小于 0")
        limits[field] = number
    return limits


def _normalize_fee_model(raw: Optional[dict]) -> dict:
    if not raw:
        return {}
    allowed_fields = (
        "buy_commission_rate",
        "sell_commission_rate",
        "min_commission",
        "stamp_tax_rate",
        "transfer_fee_rate",
    )
    model = {}
    for field in allowed_fields:
        value = raw.get(field)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{field} 必须是数字")
        if number < 0:
            raise HTTPException(status_code=400, detail=f"{field} 不能小于 0")
        model[field] = number
    return model

@router.get("/securities_accounts", response_model=list[SecuritiesAccountResponse])
def list_accounts(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    docs = col.find({"user_id": str(current_user["id"])})
    out = []
    for d in docs:
        out.append(_account_response(d))
    
    # 调试：打印返回的账户数据
    print(f"\n=== GET /securities_accounts ===")
    print(f"User ID: {current_user['id']}")
    print(f"Found {len(out)} accounts:")
    for acc in out:
        print(f"  - ID: {acc.id}, Broker: {acc.broker}, Account ID: {acc.account_id}, Type: {acc.account_type}")
    print("=" * 40 + "\n")
    
    return out

@router.post("/securities_accounts", response_model=SecuritiesAccountResponse)
def create_account(payload: SecuritiesAccountCreate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    
    # 验证账户类型
    account_type = payload.account_type or "real"
    if account_type not in ["real", "simulated"]:
        raise HTTPException(status_code=400, detail="账户类型必须是 'real' 或 'simulated'")
    
    # 真实账户必须提供 broker 和 account_id
    if account_type == "real":
        if not payload.broker:
            raise HTTPException(status_code=400, detail="真实账户必须提供券商")
        if not payload.account_id:
            raise HTTPException(status_code=400, detail="真实账户必须提供账户ID")
    
    # 模拟账户自动生成唯一的 account_id 和固定券商
    if account_type == "simulated":
        import time
        timestamp = int(time.time() * 1000)
        account_id = f"sim_{current_user['username']}_{timestamp}"
        broker = "模拟交易"  # 模拟账户固定券商
    else:
        account_id = payload.account_id
        broker = payload.broker
    
    # 准备文档
    doc = {
        "user_id": str(current_user["id"]),
        "broker": broker,
        "account_id": account_id,
        "account_type": account_type,
        "live_trading_enabled": bool(payload.live_trading_enabled) if account_type == "real" else False,
        "risk_limits": _normalize_risk_limits(payload.risk_limits),
        "fee_model": _normalize_fee_model(payload.fee_model),
        "created_at": datetime.utcnow(),
        "updated_at": None,
    }
    
    # 模拟账户初始化资金
    if account_type == "simulated":
        initial_cash = payload.initial_cash if payload.initial_cash is not None else 1000000.0
        doc["initial_cash"] = initial_cash
        
        # 同时在 trader_accounts 中初始化账户余额
        # 关键：使用 securities_account_id 而不是 account_id 来避免与真实账户冲突
        # 先插入 securities_accounts 获取 _id，然后再初始化 trader_accounts
        pass  # 稍后在插入后处理
    
    # 单账户限制：检查用户是否已有账户
    existing_count = col.count_documents({"user_id": str(current_user["id"])})
    if existing_count > 0:
        raise HTTPException(
            status_code=400, 
            detail="当前版本每个用户只支持一个账户。如需多账户，请注册多个用户。"
        )
    
    # enforce uniqueness per user per broker+account_id+account_type
    # 真实账户和模拟账户可以有相同的 broker+account_id
    existing = col.find_one({
        "user_id": doc["user_id"],
        "broker": doc["broker"],
        "account_id": doc["account_id"],
        "account_type": account_type,  # 添加账户类型到唯一性检查
    })
    if existing:
        raise HTTPException(status_code=409, detail=f"{account_type} 账户已存在：{payload.broker} - {payload.account_id}")
    
    res = col.insert_one(doc)
    securities_account_id = str(res.inserted_id)
    
    # 将 _id 保存到文档中用于后续查询
    col.update_one({"_id": res.inserted_id}, {"$set": {"securities_account_id": securities_account_id}})
    
    # 模拟账户：现在使用 securities_account_id 初始化 trader_accounts
    if account_type == "simulated":
        trader_accounts_col = collections["trader_accounts"]
        trader_accounts_col.insert_one({
            "user_id": str(current_user["id"]),
            "securities_account_id": securities_account_id,  # 使用唯一的 _id
            "broker": broker,  # 使用固定的券商
            "account_id": account_id,  # 使用生成的 account_id
            "account_type": "simulated",
            "total_asset": initial_cash,
            "cash": initial_cash,
            "available_cash": initial_cash,
            "market_value": 0.0,
            "synced_at": datetime.utcnow(),
        })
    
    return _account_response({**doc, "_id": securities_account_id})

@router.put("/securities_accounts/{id}")
def update_account(id: str, payload: SecuritiesAccountUpdate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    
    # 验证账户所有权
    try:
        account = col.find_one({"_id": ObjectId(id), "user_id": str(current_user["id"])})
    except:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    update = {}
    if payload.broker is not None:
        update["broker"] = payload.broker
    if payload.account_id is not None:
        update["account_id"] = payload.account_id
    if payload.account_type is not None:
        update["account_type"] = payload.account_type
    effective_account_type = payload.account_type or account.get("account_type", "real")
    if payload.live_trading_enabled is not None:
        update["live_trading_enabled"] = bool(payload.live_trading_enabled) if effective_account_type == "real" else False
    if payload.risk_limits is not None:
        update["risk_limits"] = _normalize_risk_limits(payload.risk_limits)
    if payload.fee_model is not None:
        update["fee_model"] = _normalize_fee_model(payload.fee_model)
    if effective_account_type != "real":
        update["live_trading_enabled"] = False
    
    if not update:
        return {"success": True}
    
    update["updated_at"] = datetime.utcnow()
    res = col.update_one({"_id": ObjectId(id)}, {"$set": update})
    
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return {"success": True}

@router.delete("/securities_accounts/{id}")
def delete_account(id: str, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    
    # 验证账户所有权并删除
    try:
        # 验证 ID 格式
        try:
            obj_id = ObjectId(id)
        except Exception as e:
            print(f"⚠️  Invalid ObjectId format: {id}, error: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid account ID format: {id}")
        
        # 先查询账户信息
        account = col.find_one({"_id": obj_id, "user_id": str(current_user["id"])})
        if not account:
            print(f"⚠️  Account not found: {id} for user {current_user['id']}")
            # 检查账户是否存在（但属于其他用户）
            other_account = col.find_one({"_id": obj_id})
            if other_account:
                print(f"⚠️  Account exists but belongs to user: {other_account.get('user_id')}")
                raise HTTPException(status_code=403, detail="Account belongs to another user")
            raise HTTPException(status_code=404, detail="Account not found")
        
        print(f"✅ Found account to delete: {id}, type={account.get('account_type')}, broker={account.get('broker')}")
        
        # 删除账户
        res = col.delete_one({"_id": obj_id})
        
        if res.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Account not found")
        
        # 如果是模拟账户，同时删除 trader_accounts 中的记录
        if account.get("account_type") == "simulated":
            trader_accounts_col = collections["trader_accounts"]
            deleted_trader = trader_accounts_col.delete_one({
                "user_id": str(current_user["id"]),
                "securities_account_id": id
            })
            print(f"✅ Deleted {deleted_trader.deleted_count} trader_accounts records")
            
            # 也删除模拟账户的持仓记录
            trader_positions_col = collections.get("trader_positions")
            if trader_positions_col:
                deleted_positions = trader_positions_col.delete_many({
                    "user_id": str(current_user["id"]),
                    "securities_account_id": id
                })
                print(f"✅ Deleted {deleted_positions.deleted_count} trader_positions records")
        
        print(f"✅ Successfully deleted account: {id}")
        return {"success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Delete account error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}")
