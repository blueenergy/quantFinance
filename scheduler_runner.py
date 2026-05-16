"""Standalone APScheduler runner for quantFinance.

This process owns the in-memory APScheduler instance. FastAPI workers only read
and update MongoDB schedule_config/scheduler_status so API scaling cannot create
duplicate scheduler instances.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from scheduler import (
    acquire_primary_scheduler_lock,
    apply_schedule_config,
    release_primary_scheduler_lock,
    renew_primary_scheduler_lock,
    schedule_config_signature,
    start_scheduler,
    stop_scheduler,
    write_scheduler_status,
)


logging.basicConfig(
    level=os.getenv("SCHEDULER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scheduler_runner")

LOCK_TTL_SECONDS = int(os.getenv("SCHEDULER_LOCK_TTL_SECONDS", "90"))
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", "15"))
RELOAD_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_RELOAD_INTERVAL_SECONDS", "15"))

_stopping = False


def _handle_signal(signum, _frame):
    global _stopping
    log.info("Received signal %s, shutting down scheduler runner.", signum)
    _stopping = True


def _sleep(seconds: int) -> None:
    deadline = time.time() + seconds
    while not _stopping and time.time() < deadline:
        time.sleep(min(1, max(0, deadline - time.time())))


def _run_owned_scheduler() -> None:
    """Run APScheduler while this process owns the primary Mongo lease."""
    last_reload = 0.0
    last_signature = ""
    start_scheduler()
    try:
        last_signature = schedule_config_signature()
        write_scheduler_status("running")
        while not _stopping:
            if not renew_primary_scheduler_lock(LOCK_TTL_SECONDS):
                log.warning("Lost scheduler primary lock; stopping in-memory scheduler.")
                write_scheduler_status("lock_lost", error="primary lock lost")
                raise RuntimeError("primary scheduler lock lost")

            now = time.time()
            if now - last_reload >= RELOAD_INTERVAL_SECONDS:
                signature = schedule_config_signature()
                if signature != last_signature:
                    log.info("Detected schedule_config change; reloading scheduler jobs.")
                    apply_schedule_config()
                    last_signature = signature
                last_reload = now

            write_scheduler_status("running")
            _sleep(HEARTBEAT_INTERVAL_SECONDS)
    except Exception as exc:
        log.exception("Scheduler runner loop failed: %s", exc)
        write_scheduler_status("error", error=str(exc))
        raise
    finally:
        stop_scheduler()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Starting standalone scheduler runner.")
    owned_primary_lock = False
    while not _stopping:
        if not acquire_primary_scheduler_lock(LOCK_TTL_SECONDS):
            log.warning("Another scheduler runner owns the primary lock; waiting.")
            _sleep(HEARTBEAT_INTERVAL_SECONDS)
            continue

        owned_primary_lock = True
        log.info("Acquired scheduler primary lock.")
        try:
            _run_owned_scheduler()
        finally:
            release_primary_scheduler_lock()

        if not _stopping:
            _sleep(HEARTBEAT_INTERVAL_SECONDS)

    if owned_primary_lock:
        write_scheduler_status("stopped")
    log.info("Scheduler runner stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
