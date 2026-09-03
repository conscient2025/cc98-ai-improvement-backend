from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SubscriptionStatus = Literal["enabled", "paused"]
NotificationProvider = Literal["dingtalk", "email"]


class HealthResponse(BaseModel):
    status: str
    components: dict[str, Any] = Field(default_factory=dict)


class AuthRequestCodeIn(BaseModel):
    email: str


class AuthRequestCodeOut(BaseModel):
    status: str
    email: str
    dev_code: str | None = None


class AuthVerifyCodeIn(BaseModel):
    email: str
    code: str


class UserOut(BaseModel):
    id: str
    email: str
    email_verified_at: datetime | None = None
    status: str
    created_at: datetime


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class SubscriptionCreate(BaseModel):
    expression: str


class SubscriptionUpdate(BaseModel):
    expression: str | None = None
    status: SubscriptionStatus | None = None


class SubscriptionOut(BaseModel):
    id: int
    expression: str
    status: str
    created_at: datetime
    updated_at: datetime


class NotificationChannelSave(BaseModel):
    provider: NotificationProvider
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    notify_interval_minutes: int | None = Field(default=None, ge=1)


class NotificationChannelTest(BaseModel):
    provider: NotificationProvider
    config: dict[str, Any] = Field(default_factory=dict)


class NotificationChannelOut(BaseModel):
    id: int
    provider: str
    enabled: bool
    config: dict[str, Any]
    has_secret: bool = False
    notify_interval_minutes: int
    last_attempted_at: datetime | None = None
    last_sent_at: datetime | None = None
    last_dispatch_status: str | None = None
    last_dispatch_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CC98TopicOut(BaseModel):
    topic_id: str
    title: str
    url: str
    board_id: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    created_at: datetime | None = None
    fetched_at: datetime


class NotificationOut(BaseModel):
    id: int
    topic_id: str
    topic_title: str
    topic_url: str
    matched_reason: str | None = None
    created_at: datetime


class ScanResponse(BaseModel):
    scanned_subscriptions: int
    fetched_pages: int = 0
    fetched_topic_items: int = 0
    unique_topics_before_cursor: int = 0
    fetched_topics: int
    candidate_pairs: int
    matched_user_topics: int = 0
    matched_pairs: int
    created_notifications: int
    queued_notifications: int = 0
    processed_notifications: int = 0
    sent_notifications: int
    dispatch_batches: int = 0
    dispatch_attempts: int = 0
    dispatch_successes: int = 0
    dispatch_failures: int = 0
    deduplicated_destination_topics: int = 0
    cursor_found: bool = False
    cursor_gap: bool = False
    baseline_created: bool = False
    source: str = "watch"
    status: str = "ok"


class AdminHealthOut(BaseModel):
    zju_connect: dict[str, Any]
    cc98_service_account: dict[str, Any]
    workers: dict[str, Any]
    cursor: dict[str, Any] | None = None
