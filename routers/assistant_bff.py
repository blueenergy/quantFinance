"""
BFF: stream chat via Hermes OpenAI-compatible gateway, emit quant-assistant–style SSE.

MVP: single-user; no Mongo persistence; maps OpenAI stream deltas to {"type":"token","content":...}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import date, datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import get_current_active_user
from services.analysis_history import build_user_analysis_history_payload
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

# 与 GET /api/analysis-history（build_user_analysis_history_payload）同源
_ANALYSIS_HISTORY_INJECT_ENABLED = os.getenv("HERMES_ANALYSIS_HISTORY_INJECT_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _read_analysis_history_inject_max_chars() -> int:
    raw = os.getenv("HERMES_ANALYSIS_HISTORY_INJECT_MAX_CHARS", "12000")
    try:
        return max(2000, int(raw))
    except ValueError:
        return 12000


_ANALYSIS_HISTORY_INJECT_MAX_CHARS = _read_analysis_history_inject_max_chars()


def _read_analysis_history_limit() -> int:
    raw = os.getenv("HERMES_ANALYSIS_HISTORY_LIMIT", "15")
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return 15


_ANALYSIS_HISTORY_LIMIT = _read_analysis_history_limit()


def _read_analysis_history_item_max_chars() -> int:
    raw = os.getenv("HERMES_ANALYSIS_HISTORY_ITEM_MAX_CHARS", "6000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 6000


_ANALYSIS_HISTORY_ITEM_MAX_CHARS = _read_analysis_history_item_max_chars()

_ANALYSIS_HISTORY_TRIGGERS: tuple[str, ...] = (
    "自选股深度分析",
    "深度分析历史",
    "分析历史",
    "我的深度分析",
    "深度分析记录",
    "股票分析历史",
    "之前的深度分析",
    "watchlist deep analysis",
    "analysis history",
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


def _user_wants_analysis_history_context(text: str) -> bool:
    if not _ANALYSIS_HISTORY_INJECT_ENABLED:
        return False
    t = text.strip()
    if not t:
        return False
    tl = t.lower()
    for kw in _ANALYSIS_HISTORY_TRIGGERS:
        if kw.isascii():
            if kw.lower() in tl:
                return True
        elif kw in t:
            return True
    return False


def _guess_symbol_for_history(text: str) -> Optional[str]:
    """识别消息中的 A 股代码（可选），裸 6 位数字可与 .SH/.SZ 多种形式匹配。"""
    m = re.search(r"\b(\d{6})(\.(?:SZ|SH|BJ))?\b", text, re.I)
    if not m:
        return None
    digits = m.group(1)
    suf = m.group(2)
    return digits.upper() + (suf.upper() if suf else "")


def _json_dt_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(repr(o))


def _compact_analysis_history_row(rec: dict, max_analysis_chars: int) -> dict:
    out: Dict[str, Any] = {
        "id": rec.get("id"),
        "symbol": rec.get("symbol"),
        "stock_name": rec.get("stock_name"),
        "provider": rec.get("provider"),
        "model": rec.get("model"),
        "created_at": rec.get("created_at"),
    }
    created = out["created_at"]
    if hasattr(created, "isoformat"):
        try:
            out["created_at"] = created.isoformat()
        except Exception:
            out["created_at"] = str(created)

    a = rec.get("analysis")
    if isinstance(a, dict):
        dumped = json.dumps(a, ensure_ascii=False, default=_json_dt_default)
        if len(dumped) > max_analysis_chars:
            out["analysis_excerpt"] = dumped[:max_analysis_chars] + "..."
        else:
            out["analysis"] = a
    elif a is not None:
        out["analysis"] = str(a)[:max_analysis_chars]

    return out


def _analysis_history_json_for_system_prompt(payload: dict, max_chars: int, item_max: int) -> str:
    meta = {
        "page": payload.get("page"),
        "limit": payload.get("limit"),
        "total": payload.get("total"),
        "record_count": len(payload.get("data") or []),
    }
    rows = [_compact_analysis_history_row(r, item_max) for r in (payload.get("data") or [])]
    body_obj = {"meta": meta, "records": rows}
    raw = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"), default=_json_dt_default)
    if len(raw) <= max_chars:
        body = raw
    else:
        body = raw[: max_chars - 40] + "\n...[truncated by HERMES_ANALYSIS_HISTORY_INJECT_MAX_CHARS]"
    return (
        "【自选股深度分析历史】以下为当前登录用户在 GET /api/analysis-history 同源数据中的近期记录（按时间倒序）。"
        "请仅依据其中内容归纳、对比或答疑；若无记录则说明暂无历史，勿编造报告。"
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
    current_user: dict = Depends(get_current_active_user),
):
    """
    Hermes-backed streaming chat; SSE matches quant-assistant token/error/[DONE] shape.
    """
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is empty")

    cid = (req.conversation_id or "").strip()
    conv_id = cid if cid else str(uuid.uuid4())

    system_fragments: List[str] = []

    if _user_wants_ladder_context(text):
        try:
            ladder_payload = await asyncio.to_thread(build_daily_ladder_payload, None)
            system_fragments.append(
                _ladder_json_for_system_prompt(ladder_payload, _LADDER_INJECT_MAX_CHARS)
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("Hermes BFF ladder prefetch failed: %s", e)
            system_fragments.append(
                "【连板天梯数据】服务端暂时无法加载数据。请勿编造涨停梯队或股票列表；可说明暂时不可用。"
            )

    if _user_wants_analysis_history_context(text):
        uid = current_user.get("id")
        sym_guess = _guess_symbol_for_history(text)
        try:
            hist_payload = await asyncio.to_thread(
                build_user_analysis_history_payload,
                uid,
                symbol=sym_guess if sym_guess else None,
                page=1,
                limit=_ANALYSIS_HISTORY_LIMIT,
            )
            system_fragments.append(
                _analysis_history_json_for_system_prompt(
                    hist_payload,
                    _ANALYSIS_HISTORY_INJECT_MAX_CHARS,
                    _ANALYSIS_HISTORY_ITEM_MAX_CHARS,
                )
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("Hermes BFF analysis history prefetch failed: %s", e)
            system_fragments.append(
                "【自选股深度分析历史】暂时无法从服务端读取历史记录。请勿编造分析内容；可说明暂时不可用。"
            )

    messages: List[Dict[str, str]] = []
    if system_fragments:
        messages.append({"role": "system", "content": "\n\n---\n\n".join(system_fragments)})
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
