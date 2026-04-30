"""
BFF: stream chat via Hermes OpenAI-compatible gateway, emit quant-assistant–style SSE.

MVP: single-user; no Mongo persistence; maps OpenAI stream deltas to {"type":"token","content":...}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import get_current_active_user
from services.ladder_daily import build_daily_ladder_payload

_log = logging.getLogger(__name__)

router = APIRouter()

# Container default; override for local dev without Docker.
_HERMES_BASE = os.getenv("HERMES_OPENAI_BASE_URL", "http://hermes:8642").rstrip("/")
_HERMES_KEY = os.getenv("HERMES_API_SERVER_KEY", "").strip()
_HERMES_MODEL = os.getenv("HERMES_OPENAI_MODEL", "hermes-agent").strip()

# 命中则注入与 /api/hermes-tools/ladder/daily 同源 JSON（见 build_daily_ladder_payload）
_LADDER_INJECT_ENABLED = os.getenv("HERMES_LADDER_INJECT_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
def _read_ladder_inject_max_chars() -> int:
    raw = os.getenv("HERMES_LADDER_INJECT_MAX_CHARS", "10000")
    try:
        return max(2000, int(raw))
    except ValueError:
        return 10000


_LADDER_INJECT_MAX_CHARS = _read_ladder_inject_max_chars()

_LADDER_TRIGGERS: tuple[str, ...] = (
    "连板天梯",
    "涨停梯队",
    "几连板",
    "今日连板",
    "连板股",
    "连板榜",
    "涨停天梯",
    "limit-up ladder",
    "ladder daily",
)


class HermesChatStreamRequest(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_id: Optional[str] = None


def _sse_data(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _user_wants_ladder_context(text: str) -> bool:
    if not _LADDER_INJECT_ENABLED:
        return False
    t = text.strip()
    if not t:
        return False
    tl = t.lower()
    for kw in _LADDER_TRIGGERS:
        if kw.isascii():
            if kw.lower() in tl:
                return True
        elif kw in t:
            return True
    return False


def _ladder_json_for_system_prompt(payload: dict, max_chars: int) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw) <= max_chars:
        body = raw
    else:
        body = raw[: max_chars - 40] + "\n...[truncated by HERMES_LADDER_INJECT_MAX_CHARS]"
    return (
        "【连板天梯数据】以下为 quant-api 与 /api/hermes-tools/ladder/daily 同源的 JSON。"
        "请仅依据其中事实回答；若 success 为 false，说明 error/suggestion，勿编造梯队或股票。"
        "\n\n"
        + body
    )


def _parse_openai_sse_line(line: str) -> Optional[str]:
    """Return assistant delta text chunk or None."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "__DONE__"
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    choices = data.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        return content if content else None
    return str(content)


async def _hermes_stream_events(
    body: Dict[str, Any],
) -> AsyncIterator[str]:
    if not _HERMES_KEY:
        yield _sse_data(
            {
                "type": "error",
                "message": "HERMES_API_SERVER_KEY is not set on quant-api",
            }
        )
        yield "data: [DONE]\n\n"
        return

    url = f"{_HERMES_BASE}/v1/chat/completions"
    timeout = httpx.Timeout(300.0, connect=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {_HERMES_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as response:
                if response.status_code != 200:
                    err = (await response.aread()).decode("utf-8", errors="replace")[:4000]
                    _log.warning("Hermes non-200: %s %s", response.status_code, err[:500])
                    yield _sse_data(
                        {
                            "type": "error",
                            "message": f"Hermes error {response.status_code}: {err[:500]}",
                        }
                    )
                    yield "data: [DONE]\n\n"
                    return

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    piece = _parse_openai_sse_line(line)
                    if piece is None:
                        continue
                    if piece == "__DONE__":
                        break
                    yield _sse_data({"type": "token", "content": piece})
    except httpx.RequestError as e:
        _log.exception("Hermes request failed: %s", e)
        yield _sse_data({"type": "error", "message": f"无法连接 Hermes: {e}"})
    except Exception as e:  # noqa: BLE001
        _log.exception("Hermes stream failed: %s", e)
        yield _sse_data({"type": "error", "message": str(e)})

    yield "data: [DONE]\n\n"


@router.post("/chat/stream")
async def chat_stream_hermes(
    req: HermesChatStreamRequest,
    _current_user: dict = Depends(get_current_active_user),
):
    """
    Hermes-backed streaming chat; SSE matches quant-assistant token/error/[DONE] shape.
    """
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is empty")

    cid = (req.conversation_id or "").strip()
    conv_id = cid if cid else str(uuid.uuid4())

    messages: List[Dict[str, str]] = []
    if _user_wants_ladder_context(text):
        try:
            ladder_payload = await asyncio.to_thread(build_daily_ladder_payload, None)
            sys_content = _ladder_json_for_system_prompt(ladder_payload, _LADDER_INJECT_MAX_CHARS)
            messages.append({"role": "system", "content": sys_content})
        except Exception as e:  # noqa: BLE001
            _log.exception("Hermes BFF ladder prefetch failed: %s", e)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "【连板天梯数据】服务端暂时无法加载数据。请勿编造涨停梯队或股票列表；可说明暂时不可用。"
                    ),
                }
            )
    messages.append({"role": "user", "content": text})

    payload = {
        "model": _HERMES_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": float(os.getenv("HERMES_CHAT_TEMPERATURE", "0.7")),
    }

    async def event_generator():
        yield _sse_data({"type": "conv_id", "conversation_id": conv_id})
        async for chunk in _hermes_stream_events(payload):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
