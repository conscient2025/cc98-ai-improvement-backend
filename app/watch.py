from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .cc98_client import cc98_client
from .matcher import match_subscription_topic
from .models import CC98Topic, Notification, NotificationChannel, Subscription, SystemCursor, WorkerStatus, utc_now
from .notification_frequency import effective_notify_interval_minutes, parse_datetime
from .notifiers import NotificationItem, send_batch_notification
from .schemas import ScanResponse
from .utils import json_dumps, json_loads


def _is_production() -> bool:
    return os.getenv("APP_ENV", "development").lower() in {"prod", "production"}


def _mock_topics() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "topic_id": "mock-cc98-ai-1",
            "title": "求一个 CC98 AI 搜索和订阅提醒工具",
            "url": "https://www.cc98.org/topic/mock-cc98-ai-1",
            "board_id": "AI",
            "author_name": "mock",
            "created_at": now,
            "content": "希望能按关键词订阅新帖，并在匹配时通知。",
            "raw": {"source": "mock"},
        },
        {
            "topic_id": "mock-hackathon-1",
            "title": "两天黑客松后端接口联调记录",
            "url": "https://www.cc98.org/topic/mock-hackathon-1",
            "board_id": "dev",
            "author_name": "mock",
            "created_at": now,
            "content": "FastAPI 订阅接口、通知接口、扫描 worker 已经可测试。",
            "raw": {"source": "mock"},
        },
    ]


def _topic_to_model(topic: dict[str, Any]) -> CC98Topic:
    created_at = topic.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = None
    if not isinstance(created_at, datetime):
        created_at = None
    return CC98Topic(
        topic_id=str(topic["topic_id"]),
        title=str(topic.get("title") or ""),
        url=str(topic.get("url") or f"https://www.cc98.org/topic/{topic['topic_id']}"),
        board_id=str(topic.get("board_id")) if topic.get("board_id") is not None else None,
        author_id=str(topic.get("author_id")) if topic.get("author_id") is not None else None,
        author_name=str(topic.get("author_name")) if topic.get("author_name") is not None else None,
        created_at=created_at,
        fetched_at=utc_now(),
        raw_json=json_dumps(topic.get("raw") or topic),
    )


def upsert_topics(db: Session, topics: Iterable[dict[str, Any]]) -> int:
    count = 0
    for topic in topics:
        topic_id = str(topic.get("topic_id") or "")
        if not topic_id:
            continue
        existing = db.get(CC98Topic, topic_id)
        model = _topic_to_model(topic)
        if existing is None:
            db.add(model)
        else:
            existing.title = model.title
            existing.url = model.url
            existing.board_id = model.board_id
            existing.author_id = model.author_id
            existing.author_name = model.author_name
            existing.created_at = model.created_at
            existing.fetched_at = model.fetched_at
            existing.raw_json = model.raw_json
        count += 1
    db.commit()
    return count


def fetch_new_topics(limit: int = 20) -> tuple[list[dict[str, Any]], str]:
    if os.getenv("WATCH_FORCE_MOCK_TOPICS", "").lower() in {"1", "true", "yes", "on"}:
        if _is_production():
            raise RuntimeError("WATCH_FORCE_MOCK_TOPICS must be disabled in production")
        return _mock_topics(), "mock"
    if not os.getenv("CC98_SERVICE_USERNAME") and not os.getenv("CC98_SERVICE_REFRESH_TOKEN"):
        if _is_production():
            raise RuntimeError("CC98 service account is not configured")
        return _mock_topics(), "mock"

    try:
        return cc98_client.get_new_posts(limit=max(1, limit)), "cc98_new_posts"
    except Exception as exc:
        if _is_production():
            raise RuntimeError(f"CC98 new posts fetch failed: {exc}") from exc
        return _mock_topics(), "mock"


def fetch_new_topics_for_subscription(subscription: Subscription, limit: int = 20) -> tuple[list[dict[str, Any]], str]:
    _ = subscription
    return fetch_new_topics(limit)


def _enabled_channels(db: Session, user_id: str) -> list[NotificationChannel]:
    return (
        db.query(NotificationChannel)
        .filter(NotificationChannel.user_id == user_id, NotificationChannel.enabled.is_(True))
        .all()
    )


def _pending_notifications(db: Session, user_id: str) -> list[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.delivery_status.in_(["pending", "failed"]))
        .order_by(Notification.id.asc())
        .all()
    )


def _is_channel_due(channel: NotificationChannel, config: dict[str, Any], now: datetime) -> bool:
    last_sent_at = parse_datetime(channel.last_sent_at)
    if last_sent_at is None:
        return True
    interval = timedelta(minutes=effective_notify_interval_minutes(config))
    return now - last_sent_at >= interval


def _send_notification_batch(db: Session, user_id: str, notifications: list[Notification]) -> int:
    if not notifications:
        return 0

    channels = _enabled_channels(db, user_id)
    if not channels:
        for notification in notifications:
            notification.delivery_status = "skipped"
        db.commit()
        return 0

    now = utc_now()
    items: list[NotificationItem] = [
        {
            "title": notification.topic_title,
            "url": notification.topic_url,
            "reason": notification.matched_reason,
        }
        for notification in notifications
    ]

    for channel in channels:
        config = json_loads(channel.config_json, {})
        if not _is_channel_due(channel, config, now):
            continue
        result = send_batch_notification(channel.provider, config, items)
        channel.last_test_status = result.status
        channel.last_error = result.error
        if result.ok:
            channel.last_sent_at = now
            for notification in notifications:
                notification.delivery_channel = channel.provider
                notification.delivery_status = "sent"
                notification.sent_at = now
            db.commit()
            return len(notifications)

        for notification in notifications:
            notification.delivery_channel = channel.provider
            notification.delivery_status = "failed"

    db.commit()
    return 0


def _set_worker_status(db: Session, name: str, status: str, error: str | None = None, metrics: dict[str, Any] | None = None) -> None:
    worker = db.get(WorkerStatus, name)
    now = utc_now()
    if worker is None:
        worker = WorkerStatus(name=name)
        db.add(worker)
    worker.status = status
    worker.updated_at = now
    if status == "running":
        worker.last_started_at = now
    elif status == "ok":
        worker.last_success_at = now
        worker.last_error = None
        worker.consecutive_failures = 0
    elif status == "failed":
        worker.last_failure_at = now
        worker.last_error = error
        worker.consecutive_failures += 1
    if metrics is not None:
        worker.metrics_json = json_dumps(metrics)
    db.commit()


def _update_cursor(db: Session, source: str, value: str) -> None:
    cursor = db.get(SystemCursor, source)
    if cursor is None:
        cursor = SystemCursor(source=source, cursor_value=value)
        db.add(cursor)
    else:
        cursor.cursor_value = value
        cursor.updated_at = utc_now()
    db.commit()


def run_watch_scan(db: Session) -> ScanResponse:
    _set_worker_status(db, "watch_scan", "running")
    metrics = {
        "scanned_subscriptions": 0,
        "fetched_topics": 0,
        "candidate_pairs": 0,
        "matched_pairs": 0,
        "created_notifications": 0,
        "sent_notifications": 0,
    }
    source = "watch"
    try:
        subscriptions = db.query(Subscription).filter(Subscription.status == "enabled").all()
        metrics["scanned_subscriptions"] = len(subscriptions)
        latest_topic_id: str | None = None
        users_to_notify = {subscription.user_id for subscription in subscriptions}
        topics: list[dict[str, Any]] = []
        if subscriptions:
            topics, source = fetch_new_topics()
            metrics["fetched_topics"] = upsert_topics(db, topics)
            for topic in topics:
                latest_topic_id = str(topic.get("topic_id") or latest_topic_id or "")

        for subscription in subscriptions:
            subscription_data = {
                "name": subscription.name,
                "description": subscription.description,
                "board_id": subscription.board_id,
            }
            for topic in topics:
                metrics["candidate_pairs"] += 1
                result = match_subscription_topic(subscription_data, topic)
                if not result.matched:
                    continue
                metrics["matched_pairs"] += 1
                notification = Notification(
                    user_id=subscription.user_id,
                    subscription_id=subscription.id,
                    topic_id=str(topic["topic_id"]),
                    topic_title=str(topic.get("title") or ""),
                    topic_url=str(topic.get("url") or f"https://www.cc98.org/topic/{topic['topic_id']}"),
                    matched_reason=result.reason,
                    delivery_status="pending",
                )
                db.add(notification)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    continue
                metrics["created_notifications"] += 1

        for user_id in users_to_notify:
            metrics["sent_notifications"] += _send_notification_batch(db, user_id, _pending_notifications(db, user_id))

        if latest_topic_id:
            _update_cursor(db, "cc98_watch:last_topic_id", latest_topic_id)
        _set_worker_status(db, "watch_scan", "ok", metrics=metrics)
        return ScanResponse(**metrics, source=source)
    except Exception as exc:
        _set_worker_status(db, "watch_scan", "failed", error=str(exc), metrics=metrics)
        raise
