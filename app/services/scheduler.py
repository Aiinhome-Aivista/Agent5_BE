"""
Auto-scan scheduler.

Runs a full Orchestrator scan every SCAN_INTERVAL_MINUTES minutes
(set in .env, 0 = disabled). Uses APScheduler's AsyncIOScheduler which
shares the FastAPI event loop. The job creates its own DB session
to stay isolated from any web-request session.
"""
from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.executors.pool import ThreadPoolExecutor

from app.config import settings

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _run_scheduled_scan():
    """Job body — runs in a worker thread (not blocking the event loop)."""
    from app.database import SessionLocal
    from app.agents.orchestrator import Orchestrator

    logger.info("Auto-scan triggered (every %d min)", settings.SCAN_INTERVAL_MINUTES)
    db = SessionLocal()
    try:
        result = Orchestrator(db).run_scan(
            provider=settings.SCAN_PROVIDER,
            dry_run=not settings.AUTO_EXECUTE_LOW_RISK,
            trigger="scheduled",
        )
        logger.info(
            "Auto-scan complete: scanned=%d anomalies=%d recs=%d",
            sum((p or {}).get("scanned_resources", 0) for p in result.get("telemetry", {}).values()),
            result.get("anomalies", 0),
            result.get("recommendations", 0),
        )
    except Exception:
        logger.exception("Auto-scan failed")
    finally:
        db.close()


def start_scheduler() -> None:
    """Called from FastAPI lifespan startup."""
    global _scheduler
    if settings.SCAN_INTERVAL_MINUTES <= 0:
        logger.info("Auto-scan disabled (SCAN_INTERVAL_MINUTES=0)")
        return

    _scheduler = AsyncIOScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={
            "coalesce": True,        # drop missed runs
            "max_instances": 1,      # never overlap scans
            "misfire_grace_time": 60,
        },
    )
    _scheduler.add_job(
        _run_scheduled_scan,
        trigger=IntervalTrigger(minutes=settings.SCAN_INTERVAL_MINUTES),
        id="auto_scan",
        name=f"Auto-scan every {settings.SCAN_INTERVAL_MINUTES}min",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Auto-scan scheduler started: every %d min, provider=%s",
        settings.SCAN_INTERVAL_MINUTES, settings.SCAN_PROVIDER,
    )

    if settings.SCAN_ON_STARTUP:
        logger.info("Running initial scan-on-startup")
        try:
            _run_scheduled_scan()
        except Exception:
            logger.exception("Startup scan failed")


def stop_scheduler() -> None:
    """Called from FastAPI lifespan shutdown."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Auto-scan scheduler stopped")


def scheduler_status() -> dict:
    """Expose current scheduler state for the UI."""
    if not _scheduler:
        return {
            "enabled": False,
            "interval_minutes": settings.SCAN_INTERVAL_MINUTES,
            "reason": "disabled" if settings.SCAN_INTERVAL_MINUTES <= 0 else "not_started",
        }
    job = _scheduler.get_job("auto_scan")
    return {
        "enabled": True,
        "interval_minutes": settings.SCAN_INTERVAL_MINUTES,
        "provider": settings.SCAN_PROVIDER,
        "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }
