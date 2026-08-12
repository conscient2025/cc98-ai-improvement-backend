from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def hash_secret(value: str) -> str:
    salt = os.getenv("AUTH_HASH_SALT", "cc98-ai-dev-salt")
    digest = hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def new_id(prefix: str = "") -> str:
    token = secrets.token_urlsafe(18)
    return f"{prefix}{token}" if prefix else token


def make_code(length: int = 6) -> str:
    upper = 10**length
    return f"{secrets.randbelow(upper):0{length}d}"


def expires_in_minutes(minutes: int) -> datetime:
    return utc_now() + timedelta(minutes=minutes)


def normalize_topic_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())

