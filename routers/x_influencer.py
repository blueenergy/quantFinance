"""X 大 V 推文 API：读 quant_data.x_influencer_posts，解读读 quant_analyzer.x_influencer_post_analysis。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db
import db as _app_db
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/x-influencer", tags=["X大V情报"])


def _serialize(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


@router.get("/posts", summary="推文列表（含可选 LLM 解读）")
async def list_posts(
    limit: int = Query(30, ge=1, le=100),
    username: Optional[str] = Query(None, description="筛选作者，不含 @"),
    with_analysis: bool = Query(True, description="附加 quant_analyzer 解读"),
    current_user: dict = Depends(get_current_active_user),
):
    posts_col = get_data_db()["x_influencer_posts"]
    q: Dict[str, Any] = {}
    if username:
        q["author_username"] = username.strip().lstrip("@").lower()

    cursor = posts_col.find(q).sort("ingested_at", -1).limit(limit)
    rows = list(cursor)
    analyses: Dict[str, Dict[str, Any]] = {}
    if with_analysis and rows:
        tids = [r.get("tweet_id") for r in rows if r.get("tweet_id")]
        acol = get_analyzer_db()["x_influencer_post_analysis"]
        for ad in acol.find({"tweet_id": {"$in": tids}}, {"_id": 0}):
            tid = ad.get("tweet_id")
            if tid:
                analyses[str(tid)] = _serialize(ad) or {}

    out: List[Dict[str, Any]] = []
    for r in rows:
        ser = _serialize(r) or {}
        tid = str(ser.get("tweet_id") or "")
        if with_analysis and tid:
            ser["analysis"] = analyses.get(tid)
        out.append(ser)

    return {"items": out, "count": len(out)}


@router.post("/posts/{tweet_id}/analyze", summary="手动触发单条推文 LLM 解读")
async def trigger_analyze(
    tweet_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    posts_col = get_data_db()["x_influencer_posts"]
    doc = posts_col.find_one({"tweet_id": tweet_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"未找到 tweet_id={tweet_id}")

    post_snapshot = {
        "tweet_id": doc.get("tweet_id"),
        "text": doc.get("text"),
        "author_username": doc.get("author_username"),
        "author_id": doc.get("author_id"),
        "created_at": doc.get("created_at"),
        "permalink": doc.get("permalink"),
        "lang": doc.get("lang"),
    }
    if isinstance(post_snapshot.get("created_at"), datetime):
        post_snapshot["created_at"] = post_snapshot["created_at"].isoformat()

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="x_influencer_post_analysis",
        priority=6,
        params={"tweet_id": tweet_id, "post": post_snapshot},
        source="manual_trigger",
    )
    ins = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {"task_id": str(ins.inserted_id), "status": "pending", "tweet_id": tweet_id}


@router.get("/posts/{tweet_id}/analysis", summary="单条解读结果")
async def get_analysis(
    tweet_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    acol = get_analyzer_db()["x_influencer_post_analysis"]
    doc = acol.find_one({"tweet_id": tweet_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "暂无解读结果")
    return {"tweet_id": tweet_id, "analysis": _serialize(doc)}
