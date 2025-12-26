from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
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

@router.get("/securities_accounts", response_model=list[SecuritiesAccountResponse])
def list_accounts(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    docs = col.find({"user_id": str(current_user["id"])})
    out = []
    for d in docs:
        out.append(
            SecuritiesAccountResponse(
                id=str(d["_id"]),
                broker=d["broker"],
                account_id=d["account_id"],
                created_at=d.get("created_at", datetime.utcnow()),
                updated_at=d.get("updated_at"),
            )
        )
    return out

@router.post("/securities_accounts", response_model=SecuritiesAccountResponse)
def create_account(payload: SecuritiesAccountCreate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    doc = {
        "user_id": str(current_user["id"]),
        "broker": payload.broker,
        "account_id": payload.account_id,
        "password_enc": _encrypt_password(payload.password),
        "created_at": datetime.utcnow(),
        "updated_at": None,
    }
    # enforce uniqueness per user per broker+account_id
    existing = col.find_one({
        "user_id": doc["user_id"],
        "broker": doc["broker"],
        "account_id": doc["account_id"],
    })
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists for this broker")
    res = col.insert_one(doc)
    return SecuritiesAccountResponse(
        id=str(res.inserted_id),
        broker=doc["broker"],
        account_id=doc["account_id"],
        created_at=doc["created_at"],
        updated_at=None,
    )

@router.put("/securities_accounts/{account_id}")
def update_account(account_id: str, payload: SecuritiesAccountUpdate, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    update = {}
    if payload.broker is not None:
        update["broker"] = payload.broker
    if payload.account_id is not None:
        update["account_id"] = payload.account_id
    if payload.password is not None:
        update["password_enc"] = _encrypt_password(payload.password)
    if not update:
        return {"success": True}
    update["updated_at"] = datetime.utcnow()
    res = col.update_one({"user_id": str(current_user["id"]), "account_id": account_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True}

@router.delete("/securities_accounts/{account_id}")
def delete_account(account_id: str, current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    col: Collection = collections["securities_accounts"]
    res = col.delete_one({"user_id": str(current_user["id"]), "account_id": account_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True}
