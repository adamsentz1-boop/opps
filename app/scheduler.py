"""Background scheduler: periodic source scans + processing of NEW opportunities, and the Market Challenge scan.

The market scan only researches and proposes; it never executes anything."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db import session_scope

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_scan_lock = threading.Lock()
_market_lock = threading.Lock()


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


def scan_in_background() -> bool:
    """Kick off a scan on a worker thread. Returns False if one is already running."""
    if _scan_lock.locked():
        return False
    threading.Thread(target=scan_job, name="manual-scan", daemon=True).start()
    return True


# --------------------------------------------------------------------------- market challenge
def market_scan_job(trigger: str = "scheduler") -> dict | None:
    """One market scan (data -> research -> portfolio decision -> proposal). Never overlaps, never trades."""
    if not _market_lock.acquire(blocking=False):
        log.info("market scan already running; skipping")
        return None
    try:
        from app.market.scan import run_market_scan
        with session_scope() as db:
            return run_market_scan(db, trigger=trigger)
    except Exception:  # noqa: BLE001 - a failed scan must never take the scheduler down
        log.exception("market scan failed")
        return {"error": "market scan failed; see logs"}
    finally:
        _market_lock.release()


def market_scan_in_background() -> bool:
    if _market_lock.locked():
        return False
    threading.Thread(target=market_scan_job, kwargs={"trigger": "manual"}, name="market-scan", daemon=True).start()
    return True


def market_scan_interval_minutes() -> int:
    """Interval from the owner-editable setting, falling back to .env."""
    settings = get_settings()
    try:
        from app.settings_service import get_setting
        with session_scope() as db:
            return max(1, int(get_setting(db, "market_scan_interval_minutes")))
    except Exception:  # noqa: BLE001
        return max(1, settings.market_scan_interval_minutes)


def reschedule_market_scan(minutes: int | None = None) -> None:
    """Apply a new interval (called after /market/settings is saved)."""
    if _scheduler is None:
        return
    settings = get_settings()
    minutes = max(1, int(minutes or market_scan_interval_minutes()))
    job = _scheduler.get_job("market_scan")
    if settings.market_challenge_enabled and settings.market_scan_enabled:
        if job is None:
            _scheduler.add_job(market_scan_job, "interval", minutes=minutes, id="market_scan", max_instances=1,
                               coalesce=True)
        else:
            job.reschedule("interval", minutes=minutes)
    elif job is not None:
        job.remove()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(scan_job, "interval", minutes=max(1, settings.scan_interval_minutes), id="scan",
                       max_instances=1, coalesce=True)
    if settings.market_challenge_enabled and settings.market_scan_enabled:
        minutes = market_scan_interval_minutes()
        _scheduler.add_job(market_scan_job, "interval", minutes=minutes, id="market_scan", max_instances=1,
                           coalesce=True)
        log.info("market scan scheduled every %s min", minutes)
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


def market_scheduler_status() -> dict:
    settings = get_settings()
    job = _scheduler.get_job("market_scan") if _scheduler else None
    return {"enabled": job is not None, "configured": settings.market_scan_enabled and settings.market_challenge_enabled,
            "next_run": job.next_run_time if job else None,
            "interval_minutes": market_scan_interval_minutes() if job is not None else settings.market_scan_interval_minutes,
            "scanning": _market_lock.locked()}
