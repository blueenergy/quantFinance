#!/usr/bin/env python3
"""
验证 Hermes POST /v1/chat/completions 是否会把请求体里的 OpenAI 风格 `tools`
传入 Agent（否则「带 / 不带 tools」的返回应几乎一致）。

用法（宿主机，Hermes 映射 8642 时）:
  cd quantFinance
  export HERMES_API_SERVER_KEY=你的密钥
  ./tools/verify_hermes_tools_body.py
  # 或
  python3 tools/verify_hermes_tools_body.py --save-dir /tmp/hermes-tools-verify

或显式指定:
  python3 tools/verify_hermes_tools_body.py \\
    --base-url http://127.0.0.1:8642 \\
    --api-key "$HERMES_API_SERVER_KEY"

参考: gateway/platforms/api_server.py 中 chat completions 未将 body.tools 交给 AIAgent。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
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
    if not data:
        return "hermes-agent"
    mid = data[0].get("id")
    return str(mid) if mid else "hermes-agent"


def _strip_volatile(obj: Any) -> Any:
    """去掉 id/created/object 等仅用于对比「语义是否相同」."""
    if not isinstance(obj, dict):
        return obj
    out = deepcopy(obj)
    for k in ("id", "created", "object"):
        out.pop(k, None)
    ch = out.get("choices")
    if isinstance(ch, list) and ch:
        c0 = ch[0]
        if isinstance(c0, dict):
            c0.pop("index", None)
            msg = c0.get("message")
            if isinstance(msg, dict):
                # 保留 content / tool_calls；去掉可能变化不大的噪声字段
                pass
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="对比 Hermes chat/completions 带与不带 body.tools 的响应。"
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_OPENAI_BASE_URL", "http://127.0.0.1:8642").rstrip("/"),
        help="Hermes 网关根 URL（默认 env HERMES_OPENAI_BASE_URL 或 http://127.0.0.1:8642）",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("HERMES_API_SERVER_KEY", "").strip(),
        help="Bearer 密钥（默认 env HERMES_API_SERVER_KEY）",
    )
    p.add_argument("--model", default="", help="固定 model id；默认从 GET /v1/models 取第一个")
    p.add_argument(
        "--user-message",
        default="用一句话回答：只输出单词 pong。",
        help="测试用的 user 内容（固定便于对比）",
    )
    p.add_argument(
        "--save-dir",
        default="",
        help="若设置，将完整 JSON 写入该目录: no_tools.json, with_tools.json",
    )
    args = p.parse_args()

    if not args.api_key:
        print(
            "错误: 未设置 HERMES_API_SERVER_KEY，请 export 或使用 --api-key。",
            file=sys.stderr,
        )
        sys.exit(2)

    base = args.base_url
    auth = {"Authorization": f"Bearer {args.api_key}"}

    print(f"==> GET {base}/v1/models")
    code, raw = _json_req("GET", f"{base}/v1/models", headers=auth, timeout=30.0)
    if code != 200:
        print(f"    status={code}\n{raw[:2000]}", file=sys.stderr)
        sys.exit(1)
    try:
        models = json.loads(raw)
    except json.JSONDecodeError:
        print("Invalid JSON from /v1/models", file=sys.stderr)
        sys.exit(1)
    model_id = _pick_model_id(models, args.model or None)
    print(f"    using model: {model_id}")

    common: Dict[str, Any] = {
        "model": model_id,
        "stream": False,
        "max_tokens": 128,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": args.user_message}],
    }

    fake_tools = [
        {
            "type": "function",
            "function": {
                "name": "verify_fake_tool_never_exists",
                "description": "若 Hermes 把 body.tools 交给上游，模型可能会尝试调用；用于对照实验",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": [],
                },
            },
        }
    ]

    print(f"==> POST {base}/v1/chat/completions (without tools)")
    code_a, raw_a = _json_req(
        "POST",
        f"{base}/v1/chat/completions",
        headers=auth,
        body=common,
        timeout=180.0,
    )
    print(f"    status={code_a}")

    print(f"==> POST {base}/v1/chat/completions (with tools + tool_choice=auto)")
    code_b, raw_b = _json_req(
        "POST",
        f"{base}/v1/chat/completions",
        headers=auth,
        body={**common, "tools": fake_tools, "tool_choice": "auto"},
        timeout=180.0,
    )
    print(f"    status={code_b}")

    if code_a != 200 or code_b != 200:
        print("--- response A (trim) ---")
        print(raw_a[:3000])
        print("--- response B (trim) ---")
        print(raw_b[:3000])
        sys.exit(1)

    try:
        ja = json.loads(raw_a)
        jb = json.loads(raw_b)
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}", file=sys.stderr)
        sys.exit(1)

    def _msg_summary(j: Dict[str, Any]) -> Dict[str, Any]:
        choices = j.get("choices") or []
        if not choices:
            return {}
        m = choices[0].get("message") or {}
        return {
            "content": m.get("content"),
            "tool_calls": m.get("tool_calls"),
            "refusal": m.get("refusal"),
        }

    sa = _msg_summary(ja)
    sb = _msg_summary(jb)

    print()
    print("--- summary: choices[0].message (要点) ---")
    print("without tools:", json.dumps(sa, ensure_ascii=False, indent=2))
    print("with tools:   ", json.dumps(sb, ensure_ascii=False, indent=2))

    na = _strip_volatile(ja)
    nb = _strip_volatile(jb)

    same_normalized = json.dumps(na, sort_keys=True) == json.dumps(nb, sort_keys=True)
    same_message = sa == sb

    print()
    print("--- 结论 ---")
    if same_message:
        print(
            "choices[0].message 完全一致：当前网关很可能未根据 body.tools 改变 completion "
            "（与 api_server 仅使用 platform_toolsets.api_server 的行为一致）。"
        )
    else:
        print("choices[0].message 不一致：带 tools 时行为有变化，请人工查看差异是否来自 tool_calls。")

    if same_normalized:
        print("去掉 id/created 后整份 JSON 仍一致。")
    else:
        print("去掉 id/created 后整份 JSON 仍有差异（可能仅 usage/tokens 不同）。")
        print(
            "如需保存完整响应用于 diff:\n"
            f"  echo '{base}' # 将 raw 写入文件自行 diff"
        )

    if args.save_dir:
        d = Path(args.save_dir)
        d.mkdir(parents=True, exist_ok=True)
        pa = d / "no_tools.json"
        pb = d / "with_tools.json"
        pa.write_text(raw_a, encoding="utf-8")
        pb.write_text(raw_b, encoding="utf-8")
        print(f"已写入 {pa} 与 {pb}（可 diff -u 比较）")


if __name__ == "__main__":
    main()
