"""Outbound ops notifications for quantFinance.

DingTalk signed-webhook sender, mirroring the HMAC-SHA256 scheme used by
quant-data-engine's ContractNotifier and bridge-heartbeat-monitor:
``sign = base64(hmac_sha256(secret, f"{ts}\\n{secret}"))``.

Configuration is optional: without ``OPS_DINGTALK_WEBHOOK_URL`` this is a
no-op, so the same code runs in every deployment.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def send_ops_alert(title: str, lines: list[str]) -> bool:
    url = os.getenv("OPS_DINGTALK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    secret = os.getenv("OPS_DINGTALK_WEBHOOK_SECRET", "").strip()
    if secret:
        ts = int(time.time() * 1000)
        sign = base64.b64encode(
            hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
        ).decode()
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote(sign)}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "\n".join([f"## {title}", ""] + lines)},
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001 - notification must never break the job
        log.warning("ops alert send failed: %s", exc)
        return False
