#!/usr/bin/env python3
"""
Phase A spike: Hermes gateway OpenAI-compatible API (/v1/models, /v1/chat/completions).

Hermes docker overview: https://hermes-agent.nousresearch.com/docs/user-guide/docker/
Use /v1/* with Authorization: Bearer <HERMES_API_SERVER_KEY> (see quantFinance docker-compose healthcheck).

Usage:
  cd quantFinance && python tools/spike_hermes_openai_gateway.py
  HERMES_API_SERVER_KEY=xxx python tools/spike_hermes_openai_gateway.py --base-url http://127.0.0.1:8642

Gaps vs quantFinance-dashboard ChatPanel (later phases):
  - ChatPanel uses quant-assistant REST + SSE with user JWT; Hermes uses shared Bearer API key and OpenAI payloads.
  - quant-assistant: Mongo assistant DB + quant-api tools; Hermes: /opt/data sessions/skills.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


def _json_req(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
) -> Tuple[int, str]:
    data = None
    h = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url=url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _pick_model_id(models_json: Dict[str, Any], override: Optional[str]) -> str:
    if override:
        return override
    data = models_json.get("data") or []
    if isinstance(data, list) and data:
        mid = data[0].get("id")
        if isinstance(mid, str) and mid:
            return mid
    raise SystemExit("No model id in /v1/models response; pass --model explicitly.")


def main() -> None:
    p = argparse.ArgumentParser(description="Spike Hermes OpenAI-compatible gateway (Phase A).")
    p.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_OPENAI_BASE_URL", "http://127.0.0.1:8642").rstrip("/"),
        help="Gateway base URL (default: env HERMES_OPENAI_BASE_URL or http://127.0.0.1:8642).",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("HERMES_API_SERVER_KEY", "dev-hermes-api-key-change-me"),
        help="Bearer token (default: env HERMES_API_SERVER_KEY or compose dev default).",
    )
    p.add_argument("--model", default="", help="Override model id (default: first from /v1/models).")
    p.add_argument("--no-stream", action="store_true", help="Skip streaming probe.")
    args = p.parse_args()

    base = args.base_url
    auth = {"Authorization": f"Bearer {args.api_key}"}

    print(f"==> GET {base}/v1/models")
    code, raw = _json_req("GET", f"{base}/v1/models", headers=auth, timeout=30.0)
    print(f"    status={code}")
    if code != 200:
        print(raw[:4000])
        sys.exit(1)
    try:
        models = json.loads(raw)
    except json.JSONDecodeError:
        print("Non-JSON body:", raw[:800])
        sys.exit(1)
    model_id = _pick_model_id(models, args.model or None)
    print(f"    using model: {model_id}")

    body: Dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You reply in one short English sentence."},
            {"role": "user", "content": "Say hello and the word pong."},
        ],
        "max_tokens": 80,
        "temperature": 0.2,
    }
    print(f"==> POST {base}/v1/chat/completions (non-stream)")
    code, raw = _json_req("POST", f"{base}/v1/chat/completions", headers=auth, body={**body, "stream": False})
    print(f"    status={code}")
    if code != 200:
        print(raw[:4000])
        sys.exit(1)
    try:
        chat = json.loads(raw)
    except json.JSONDecodeError:
        print("Non-JSON:", raw[:800])
        sys.exit(1)
    msg = (chat.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content", "")
    print("    assistant:", (content or str(msg))[:500])

    if args.no_stream:
        print("==> streaming skipped (--no-stream)")
        print("==> Phase A spike finished OK.")
        return

    print(f"==> POST {base}/v1/chat/completions (stream, first chunks)")
    stream_body = {**body, "stream": True, "max_tokens": 120}
    data = json.dumps(stream_body).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base}/v1/chat/completions",
        method="POST",
        data=data,
        headers={
            **auth,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            if resp.status != 200:
                print(resp.read().decode()[:2000])
                sys.exit(1)
            max_lines = 40
            n = 0
            while n < max_lines:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text or text.startswith(":"):
                    continue
                if text == "data: [DONE]":
                    print("    [DONE]")
                    break
                if text.startswith("data: "):
                    payload = text[6:]
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        print("    raw:", text[:200])
                        n += 1
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        print("    stream:", repr(piece))
                n += 1
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"stream HTTP {e.code}: {err[:4000]}")
        sys.exit(1)

    print("==> Phase A spike finished OK.")


if __name__ == "__main__":
    main()
