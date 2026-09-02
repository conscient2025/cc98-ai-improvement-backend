from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SubscriptionStatus = Literal["enabled", "paused"]
NotificationProvider = Literal["dingtalk", "feishu", "email"]


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
    user_id: str = "demo_user"
    name: str | None = None
    description: str | None = None
    topic: str | None = None
    board_id: str | None = None


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: SubscriptionStatus | None = None
    board_id: str | None = None


class SubscriptionOut(BaseModel):
    id: int
    user_id: str
    name: str
    description: str
    topic: str
    board_id: str | None = None
    status: str
    active: bool
    created_at: datetime
    updated_at: datetime


class NotificationChannelSave(BaseModel):
    user_id: str = "demo_user"
    provider: NotificationProvider = "dingtalk"
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    notify_interval_minutes: int | None = Field(default=None, ge=1)


class NotificationSettingSave(BaseModel):
    user_id: str = "demo_user"
    dingtalk_enabled: bool = False
    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None
    notify_interval_minutes: int | None = Field(default=None, ge=1)


class NotificationChannelOut(BaseModel):
    id: int | None = None
    user_id: str
    provider: str
    enabled: bool
    config: dict[str, Any]
    has_secret: bool = False
    notify_interval_minutes: int
    last_test_at: datetime | None = None
    last_attempted_at: datetime | None = None
    last_sent_at: datetime | None = None
    last_test_status: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NotificationSettingOut(BaseModel):
    user_id: str
    dingtalk_enabled: bool
    dingtalk_webhook: str | None = None
    has_dingtalk_secret: bool = False
    notify_interval_minutes: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NotificationSettingTest(BaseModel):
    user_id: str = "demo_user"
    message: str = "CC98 AI Watch notification test"


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
    user_id: str
    subscription_id: int | None = None
    topic_id: str
    topic_title: str
    topic_url: str
    topic: str
    matched_reason: str | None = None
    summary: str | None = None
    dispatch_pending: bool
    dispatch_processed_at: datetime | None = None
    # Deprecated compatibility fields; delivery is now tracked only as a
    # one-shot queue transition and channel-level health.
    delivery_channel: str | None = None
    delivery_status: str | None = None
    sent_at: datetime | None = None
    is_read: bool
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
