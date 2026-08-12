from __future__ import annotations

import os

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ModuleNotFoundError:
    BackgroundScheduler = None  # type: ignore[assignment]

from .database import SessionLocal
from .watch import run_watch_scan


scheduler = BackgroundScheduler(timezone="Asia/Shanghai") if BackgroundScheduler else None


def scan_job() -> None:
    db = SessionLocal()
    try:
        run_watch_scan(db)
    finally:
        db.close()


def start_scheduler() -> None:
    if os.getenv("ENABLE_SCHEDULER", "false").lower() not in {"1", "true", "yes", "on"}:
        return
    if scheduler is None:
        return
    if scheduler.running:
        return
    minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "10"))
    scheduler.add_job(scan_job, "interval", minutes=minutes, id="watch_scan", replace_existing=True)
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
