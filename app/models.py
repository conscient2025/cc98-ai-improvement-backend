from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), index=True, nullable=False)
    code_hash = Column(String(128), nullable=False)
    purpose = Column(String(32), default="login", nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "expression", name="uq_subscription_user_expression"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    expression = Column(String(255), nullable=False)
    status = Column(String(32), default="enabled", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_notification_channel_user_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    provider = Column(String(32), nullable=False)
    config_json = Column(Text, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_dispatch_status = Column(String(64), nullable=True)
    last_dispatch_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CC98Topic(Base):
    __tablename__ = "cc98_topics"

    topic_id = Column(String(128), primary_key=True)
    title = Column(String(500), nullable=False)
    url = Column(Text, nullable=False)
    board_id = Column(String(128), nullable=True)
    author_id = Column(String(128), nullable=True)
    author_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    fetched_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    raw_json = Column(Text, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "topic_id", name="uq_notification_user_topic"),
        Index("ix_notifications_user_id_id", "user_id", "id"),
        Index("ix_notifications_user_dispatch_pending_id", "user_id", "dispatch_pending", "id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    topic_id = Column(String(128), index=True, nullable=False)
    topic_title = Column(String(500), nullable=False)
    topic_url = Column(Text, nullable=False)
    matched_reason = Column(Text, nullable=True)
    dispatch_pending = Column(Boolean, default=False, nullable=False)
    dispatch_processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    user_id = Column(String(64), primary_key=True)
    notify_interval_minutes = Column(Integer, nullable=False)
    last_dispatch_started_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class NotificationListRateLimitState(Base):
    __tablename__ = "notification_list_rate_limit_states"

    user_id = Column(String(64), primary_key=True)
    last_success_at = Column(DateTime(timezone=True), nullable=False)


class SystemCursor(Base):
    __tablename__ = "system_cursors"

    source = Column(String(128), primary_key=True)
    cursor_value = Column(String(255), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class WorkerStatus(Base):
    __tablename__ = "worker_statuses"

    name = Column(String(128), primary_key=True)
    status = Column(String(32), default="unknown", nullable=False)
    last_started_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=False)
    metrics_json = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
