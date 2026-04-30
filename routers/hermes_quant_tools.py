"""
Hermes 容器内可调用的只读 quant-api 端点（共享密钥，无用户 JWT）。

搭配 docker-compose 中 QUANT_API_URL + HERMES_QUANT_INTERNAL_KEY。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import require_hermes_quant_internal_key
from services.ladder_daily import build_daily_ladder_payload

router = APIRouter(prefix="/hermes-tools", tags=["Hermes quant tools"])


@router.get("/ladder/daily")
async def hermes_ladder_daily(
    date: Optional[str] = Query(None, description="YYYYMMDD，默认今天"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """供 Hermes skill / curl 拉取连板天梯 JSON（需 X-Hermes-Quant-Key）。"""
    return build_daily_ladder_payload(date)
