"""Healthcheck for the standalone scheduler runner container."""
from __future__ import annotations

import sys
import time

from db import get_db_collections


MAX_HEARTBEAT_AGE_SECONDS = 120


def main() -> int:
    status = get_db_collections()["scheduler_status"].find_one({"_id": "primary"}) or {}
    state = status.get("state")
    heartbeat_ts = status.get("heartbeat_ts")
    try:
        heartbeat_age = time.time() - float(heartbeat_ts)
    except (TypeError, ValueError):
        print("scheduler heartbeat missing")
        return 1

    if state != "running":
        print(f"scheduler state is {state!r}")
        return 1
    if heartbeat_age > MAX_HEARTBEAT_AGE_SECONDS:
        print(f"scheduler heartbeat stale: {heartbeat_age:.1f}s")
        return 1

    print(f"scheduler healthy: heartbeat_age={heartbeat_age:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
