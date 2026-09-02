from __future__ import annotations

import logging
import os

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ModuleNotFoundError:
    BackgroundScheduler = None  # type: ignore[assignment]

from .database import SessionLocal
from .watch import run_watch_scan


logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="Asia/Shanghai") if BackgroundScheduler else None


def scan_job() -> None:
    db = SessionLocal()
    try:
        run_watch_scan(db)
    finally:
        db.close()


def start_scheduler() -> None:
    if os.getenv("ENABLE_SCHEDULER", "false").lower() not in {"1", "true", "yes", "on"}:
        logger.info("watch scheduler disabled")
        return
    if scheduler is None:
        logger.warning("watch scheduler unavailable: APScheduler is not installed")
        return
    if scheduler.running:
        return
    minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "10"))
    scheduler.add_job(scan_job, "interval", minutes=minutes, id="watch_scan", replace_existing=True)
    scheduler.start()
    logger.info("watch scheduler started interval_minutes=%d", minutes)


def stop_scheduler() -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("watch scheduler stopped")
