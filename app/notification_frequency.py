from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .models import NotificationPreference


def scan_interval_minutes() -> int:
    try:
        return max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", "10")))
    except ValueError:
        return 10


def effective_notify_interval_minutes(config: dict[str, Any] | None, requested: int | None = None) -> int:
    config = config or {}
    raw = requested if requested is not None else config.get("notify_interval_minutes")
    if raw is None:
        raw = config.get("interval_minutes")
    try:
        minutes = int(raw) if raw is not None else scan_interval_minutes()
    except (TypeError, ValueError):
        minutes = scan_interval_minutes()
    return max(scan_interval_minutes(), minutes)


def user_notify_interval_minutes(db: Session, user_id: str) -> int:
    preference = db.get(NotificationPreference, user_id)
    requested = preference.notify_interval_minutes if preference is not None else None
    return effective_notify_interval_minutes({}, requested)


def parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
