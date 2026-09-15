"""Background scheduler: periodic source scans + processing of NEW opportunities."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db import session_scope

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_scan_lock = threading.Lock()


def scan_job() -> dict | None:
    """Run one scan+process cycle. Safe to call from the scheduler or the UI; never overlaps."""
    if not _scan_lock.acquire(blocking=False):
        log.info("scan already running; skipping")
        return None
    try:
        from app.pipeline import run_scan
        with session_scope() as db:
            return run_scan(db)
    finally:
        _scan_lock.release()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(scan_job, "interval", minutes=max(1, settings.scan_interval_minutes), id="scan",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("scheduler started (every %s min)", settings.scan_interval_minutes)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_status() -> dict:
    job = _scheduler.get_job("scan") if _scheduler else None
    return {"enabled": _scheduler is not None, "next_run": job.next_run_time if job else None,
            "interval_minutes": get_settings().scan_interval_minutes, "scanning": _scan_lock.locked()}
