from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SubscriptionStatus = Literal["enabled", "paused"]
DeliveryStatus = Literal["pending", "sent", "failed", "skipped"]
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


class NotificationSettingSave(BaseModel):
    user_id: str = "demo_user"
    dingtalk_enabled: bool = False
    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None


class NotificationChannelOut(BaseModel):
    id: int | None = None
    user_id: str
    provider: str
    enabled: bool
    config: dict[str, Any]
    has_secret: bool = False
    last_test_at: datetime | None = None
    last_test_status: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NotificationSettingOut(BaseModel):
    user_id: str
    dingtalk_enabled: bool
    dingtalk_webhook: str | None = None
    has_dingtalk_secret: bool = False
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
    subscription_id: int
    topic_id: str
    topic_title: str
    topic_url: str
    topic: str
    matched_reason: str | None = None
    summary: str | None = None
    delivery_channel: str | None = None
    delivery_status: str
    sent_at: datetime | None = None
    is_read: bool
    created_at: datetime


class ScanResponse(BaseModel):
    scanned_subscriptions: int
    fetched_topics: int
    candidate_pairs: int
    matched_pairs: int
    created_notifications: int
    sent_notifications: int
    source: str = "watch"


class AdminHealthOut(BaseModel):
    zju_connect: dict[str, Any]
    cc98_service_account: dict[str, Any]
    workers: dict[str, Any]
    cursor: dict[str, Any] | None = None

