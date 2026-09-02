from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .cc98_client import cc98_client
from .matcher import match_subscription_topic
from .models import (
    CC98Topic,
    Notification,
    NotificationChannel,
    Subscription,
    SystemCursor,
    WorkerStatus,
    utc_now,
)
from .notification_frequency import parse_datetime, user_notify_interval_minutes
from .notifiers import NotificationItem, SendResult, redact_delivery_error, send_batch_notification
from .schemas import ScanResponse
from .utils import json_dumps, json_loads


logger = logging.getLogger(__name__)
CURSOR_SOURCE = "cc98_watch:last_topic_id"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass
class TopicScan:
    source: str
    new_cursor: str | None
    topics: list[dict[str, Any]]
    topics_to_persist: list[dict[str, Any]]
    fetched_pages: int
    fetched_topic_items: int
    cursor_found: bool
    cursor_gap: bool
    baseline_created: bool = False


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


def _topic_id(topic: dict[str, Any]) -> str:
    value = topic.get("topic_id")
    return "" if value is None else str(value).strip()


def _topic_to_model(topic: dict[str, Any]) -> CC98Topic:
    created_at = topic.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = None
    if not isinstance(created_at, datetime):
        created_at = None
    topic_id = _topic_id(topic)
    return CC98Topic(
        topic_id=topic_id,
        title=str(topic.get("title") or ""),
        url=str(topic.get("url") or f"https://www.cc98.org/topic/{topic_id}"),
        board_id=str(topic.get("board_id")) if topic.get("board_id") is not None else None,
        author_id=str(topic.get("author_id")) if topic.get("author_id") is not None else None,
        author_name=str(topic.get("author_name")) if topic.get("author_name") is not None else None,
        created_at=created_at,
        fetched_at=utc_now(),
        raw_json=json_dumps(topic.get("raw") or topic),
    )


def _upsert_topics(db: Session, topics: Iterable[dict[str, Any]]) -> None:
    for topic in topics:
        topic_id = _topic_id(topic)
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


def fetch_new_topics(limit: int = 20) -> tuple[list[dict[str, Any]], str]:
    if os.getenv("WATCH_FORCE_MOCK_TOPICS", "").lower() in _TRUE_VALUES:
        if _is_production():
            raise RuntimeError("WATCH_FORCE_MOCK_TOPICS must be disabled in production")
        return _mock_topics()[:limit], "mock"
    if not os.getenv("CC98_SERVICE_USERNAME") and not os.getenv("CC98_SERVICE_REFRESH_TOKEN"):
        if _is_production():
            raise RuntimeError("CC98 service account is not configured")
        return _mock_topics()[:limit], "mock"

    try:
        return cc98_client.get_new_posts(limit=max(1, limit), offset=0), "cc98_new_posts"
    except Exception as exc:
        if _is_production():
            raise RuntimeError(f"CC98 new posts fetch failed: {exc}") from exc
        logger.warning("CC98 new-post fetch failed; using development mock topics: %s", exc)
        return _mock_topics()[:limit], "mock"


def _load_cursor(db: Session) -> str | None:
    cursor = db.get(SystemCursor, CURSOR_SOURCE)
    if cursor is not None and cursor.cursor_value.strip():
        return cursor.cursor_value.strip()
    configured = os.getenv("CC98_INITIAL_TOPIC_ID", "").strip()
    return configured or None


def _collect_new_topics(old_cursor: str | None) -> TopicScan:
    page_size = max(1, min(20, int(os.getenv("NEW_POST_PAGE_SIZE", "20"))))
    max_pages = max(1, int(os.getenv("MAX_NEW_POST_PAGES", "10")))
    first_page, source = fetch_new_topics(page_size)
    fetched_pages = 1
    fetched_items = len(first_page)
    new_cursor = _topic_id(first_page[0]) if first_page else None
    logger.info(
        "new-post page fetched source=%s page=1 offset=0 items=%d old_cursor=%s",
        source,
        len(first_page),
        old_cursor or "none",
    )

    if not first_page:
        return TopicScan(source, None, [], [], fetched_pages, fetched_items, False, False)

    # With no persisted high-water mark, use the server's current list head as a
    # baseline by default. A bounded backfill remains available for local demos.
    if old_cursor is None and os.getenv("WATCH_INITIAL_CURSOR_MODE", "baseline").lower() != "backfill":
        logger.info("scan cursor baseline created topic_id=%s; historical notifications skipped", new_cursor)
        return TopicScan(
            source,
            new_cursor,
            [],
            _deduplicate_topics(first_page),
            fetched_pages,
            fetched_items,
            False,
            False,
            baseline_created=True,
        )

    seen_topic_ids: set[str] = set()
    topics: list[dict[str, Any]] = []
    cursor_found = False
    page = first_page
    offset = 0

    while True:
        for topic in page:
            topic_id = _topic_id(topic)
            if not topic_id:
                continue
            if old_cursor is not None and topic_id == old_cursor:
                cursor_found = True
                break
            if topic_id not in seen_topic_ids:
                seen_topic_ids.add(topic_id)
                topics.append(topic)

        if cursor_found or len(page) < page_size or source == "mock":
            break
        if fetched_pages >= max_pages:
            break

        offset += page_size
        page = cc98_client.get_new_posts(limit=page_size, offset=offset)
        fetched_pages += 1
        fetched_items += len(page)
        logger.info(
            "new-post page fetched source=%s page=%d offset=%d items=%d old_cursor_found=%s",
            source,
            fetched_pages,
            offset,
            len(page),
            cursor_found,
        )

    cursor_gap = old_cursor is not None and not cursor_found and fetched_pages >= max_pages and len(page) >= page_size
    if cursor_gap:
        logger.error(
            "scan cursor gap detected old_cursor=%s pages=%d unique_topics=%d; cursor will not advance",
            old_cursor,
            fetched_pages,
            len(topics),
        )
    return TopicScan(
        source,
        new_cursor,
        topics,
        topics,
        fetched_pages,
        fetched_items,
        cursor_found,
        cursor_gap,
        baseline_created=False,
    )


def _deduplicate_topics(topics: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for topic in topics:
        topic_id = _topic_id(topic)
        if topic_id and topic_id not in seen:
            seen.add(topic_id)
            result.append(topic)
    return result


def _enabled_channels(db: Session, user_id: str) -> list[NotificationChannel]:
    return (
        db.query(NotificationChannel)
        .filter(NotificationChannel.user_id == user_id, NotificationChannel.enabled.is_(True))
        .order_by(NotificationChannel.id.asc())
        .all()
    )


def _pending_notifications(db: Session, user_id: str) -> list[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.dispatch_pending.is_(True))
        .order_by(Notification.id.asc())
        .all()
    )


def _is_user_due(db: Session, user_id: str, channels: list[NotificationChannel], now: datetime) -> bool:
    attempted = [parse_datetime(channel.last_attempted_at) for channel in channels]
    last_attempted_at = max((value for value in attempted if value is not None), default=None)
    if last_attempted_at is None:
        return True
    interval = timedelta(minutes=user_notify_interval_minutes(db, user_id))
    return now - last_attempted_at >= interval


def _channel_destination_key(channel: NotificationChannel, config: dict[str, Any]) -> tuple[str, str]:
    if channel.provider == "dingtalk":
        destination = str(config.get("webhook") or "").strip()
        normalized = hashlib.sha256(destination.encode("utf-8")).hexdigest()
    elif channel.provider == "email":
        normalized = str(
            config.get("to") or config.get("to_email") or config.get("email") or config.get("recipient") or ""
        ).strip().lower()
    else:
        normalized = hashlib.sha256(json_dumps(config).encode("utf-8")).hexdigest()
    return channel.provider, normalized


def _chunks(items: list[Notification], size: int) -> Iterable[list[Notification]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _empty_dispatch_metrics() -> dict[str, int]:
    return {
        "processed_notifications": 0,
        "sent_notifications": 0,
        "dispatch_batches": 0,
        "dispatch_attempts": 0,
        "dispatch_successes": 0,
        "dispatch_failures": 0,
        "deduplicated_destination_topics": 0,
    }


def _send_notification_batch(
    db: Session,
    user_id: str,
    notifications: list[Notification],
    sent_topics_by_destination: dict[tuple[str, str], set[str]] | None = None,
) -> dict[str, int]:
    metrics = _empty_dispatch_metrics()
    if not notifications:
        return metrics

    channels = _enabled_channels(db, user_id)
    now = utc_now()
    if channels and not _is_user_due(db, user_id, channels, now):
        logger.info(
            "notification dispatch deferred user_id=%s pending=%d interval_minutes=%d",
            user_id,
            len(notifications),
            user_notify_interval_minutes(db, user_id),
        )
        return metrics

    # At-most-once: the queue transition is committed before any external call.
    for notification in notifications:
        notification.dispatch_pending = False
        notification.dispatch_processed_at = now
        logger.info(
            "notification dequeued notification_id=%s user_id=%s topic_id=%s",
            notification.id,
            user_id,
            notification.topic_id,
        )
    db.commit()
    metrics["processed_notifications"] = len(notifications)

    if not channels:
        logger.info("notification batch processed without enabled channels user_id=%s count=%d", user_id, len(notifications))
        return metrics

    sent_topics_by_destination = sent_topics_by_destination if sent_topics_by_destination is not None else {}
    batch_size = max(1, min(20, int(os.getenv("NOTIFICATION_BATCH_SIZE", "20"))))
    any_channel_success = False

    for channel in channels:
        config = json_loads(channel.config_json, {})
        destination_key = _channel_destination_key(channel, config)
        sent_topic_ids = sent_topics_by_destination.setdefault(destination_key, set())
        channel_attempted = False
        channel_succeeded = False
        channel_errors: list[str] = []

        for batch_number, batch in enumerate(_chunks(notifications, batch_size), start=1):
            filtered = [item for item in batch if str(item.topic_id) not in sent_topic_ids]
            deduplicated = len(batch) - len(filtered)
            metrics["deduplicated_destination_topics"] += deduplicated
            if not filtered:
                logger.info(
                    "notification batch deduplicated user_id=%s provider=%s batch=%d topics=%d",
                    user_id,
                    channel.provider,
                    batch_number,
                    len(batch),
                )
                continue

            items: list[NotificationItem] = [
                {"title": item.topic_title, "url": item.topic_url, "reason": item.matched_reason}
                for item in filtered
            ]
            metrics["dispatch_batches"] += 1
            metrics["dispatch_attempts"] += 1
            channel_attempted = True
            attempted_at = utc_now()
            channel.last_attempted_at = attempted_at
            logger.info(
                "notification send attempt user_id=%s provider=%s batch=%d topics=%d",
                user_id,
                channel.provider,
                batch_number,
                len(filtered),
            )
            try:
                result = send_batch_notification(channel.provider, config, items)
            except Exception as exc:  # noqa: BLE001
                result = SendResult(ok=False, status="failed", error=str(exc))

            if result.ok:
                metrics["dispatch_successes"] += 1
                channel_succeeded = True
                any_channel_success = True
                channel.last_sent_at = attempted_at
                sent_topic_ids.update(str(item.topic_id) for item in filtered)
                logger.info(
                    "notification send succeeded user_id=%s provider=%s batch=%d topics=%d",
                    user_id,
                    channel.provider,
                    batch_number,
                    len(filtered),
                )
            else:
                metrics["dispatch_failures"] += 1
                error = redact_delivery_error(result.error or "notification provider returned failure", config)[:500]
                channel_errors.append(error)
                logger.warning(
                    "notification send failed user_id=%s provider=%s batch=%d topics=%d error=%s",
                    user_id,
                    channel.provider,
                    batch_number,
                    len(filtered),
                    error,
                )
            db.commit()

        if channel_errors:
            channel.last_test_status = "failed"
            channel.last_error = "; ".join(channel_errors)[:2000]
        elif channel_attempted and channel_succeeded:
            channel.last_test_status = "sent"
            channel.last_error = None
        else:
            channel.last_test_status = "deduplicated"
            channel.last_error = None
        db.commit()

    if any_channel_success:
        metrics["sent_notifications"] = len(notifications)
    return metrics


def _merge_metrics(target: dict[str, Any], additions: dict[str, int]) -> None:
    for key, value in additions.items():
        target[key] = int(target.get(key, 0)) + value


def _dispatch_pending_notifications(db: Session, metrics: dict[str, Any]) -> None:
    user_rows = (
        db.query(Notification.user_id)
        .filter(Notification.dispatch_pending.is_(True))
        .distinct()
        .order_by(Notification.user_id.asc())
        .all()
    )
    sent_topics_by_destination: dict[tuple[str, str], set[str]] = {}
    for (user_id,) in user_rows:
        additions = _send_notification_batch(
            db,
            str(user_id),
            _pending_notifications(db, str(user_id)),
            sent_topics_by_destination,
        )
        _merge_metrics(metrics, additions)


def _set_worker_status(
    db: Session,
    name: str,
    status: str,
    error: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
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
    elif status in {"failed", "cursor_gap"}:
        worker.last_failure_at = now
        worker.last_error = error
        worker.consecutive_failures += 1
    if metrics is not None:
        worker.metrics_json = json_dumps(metrics)
    db.commit()


def _stage_cursor(db: Session, value: str) -> None:
    cursor = db.get(SystemCursor, CURSOR_SOURCE)
    if cursor is None:
        db.add(SystemCursor(source=CURSOR_SOURCE, cursor_value=value))
    else:
        cursor.cursor_value = value
        cursor.updated_at = utc_now()


def _existing_notification_keys(db: Session, topics: list[dict[str, Any]]) -> set[tuple[str, str]]:
    topic_ids = [_topic_id(topic) for topic in topics if _topic_id(topic)]
    if not topic_ids:
        return set()
    return {
        (str(user_id), str(topic_id))
        for user_id, topic_id in db.query(Notification.user_id, Notification.topic_id)
        .filter(Notification.topic_id.in_(topic_ids))
        .all()
    }


def _create_matching_notifications(
    db: Session,
    subscriptions: list[Subscription],
    topics: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> None:
    enabled_user_ids = {
        str(user_id)
        for (user_id,) in db.query(NotificationChannel.user_id)
        .filter(NotificationChannel.enabled.is_(True))
        .distinct()
        .all()
    }
    existing_keys = _existing_notification_keys(db, topics)
    matched_user_topics: set[tuple[str, str]] = set()

    for topic in topics:
        topic_id = _topic_id(topic)
        for subscription in subscriptions:
            key = (str(subscription.user_id), topic_id)
            if key in matched_user_topics:
                continue
            metrics["candidate_pairs"] += 1
            result = match_subscription_topic(
                {
                    "name": subscription.name,
                    "description": subscription.description,
                    "board_id": subscription.board_id,
                },
                topic,
            )
            if not result.matched:
                continue

            matched_user_topics.add(key)
            metrics["matched_user_topics"] += 1
            metrics["matched_pairs"] += 1
            if key in existing_keys:
                logger.info("notification deduplicated user_id=%s topic_id=%s", subscription.user_id, topic_id)
                continue

            notification = Notification(
                user_id=subscription.user_id,
                topic_id=topic_id,
                topic_title=str(topic.get("title") or ""),
                topic_url=str(topic.get("url") or f"https://www.cc98.org/topic/{topic_id}"),
                matched_reason=result.reason,
                dispatch_pending=str(subscription.user_id) in enabled_user_ids,
            )
            try:
                with db.begin_nested():
                    db.add(notification)
                    db.flush()
            except IntegrityError:
                logger.info("notification insert deduplicated by database user_id=%s topic_id=%s", subscription.user_id, topic_id)
                existing_keys.add(key)
                continue

            existing_keys.add(key)
            metrics["created_notifications"] += 1
            if notification.dispatch_pending:
                metrics["queued_notifications"] += 1
            logger.info(
                "notification created notification_id=%s user_id=%s topic_id=%s dispatch_pending=%s",
                notification.id,
                subscription.user_id,
                topic_id,
                notification.dispatch_pending,
            )


def _initial_metrics() -> dict[str, Any]:
    return {
        "scanned_subscriptions": 0,
        "fetched_pages": 0,
        "fetched_topic_items": 0,
        "unique_topics_before_cursor": 0,
        "fetched_topics": 0,
        "candidate_pairs": 0,
        "matched_user_topics": 0,
        "matched_pairs": 0,
        "created_notifications": 0,
        "queued_notifications": 0,
        **_empty_dispatch_metrics(),
    }


def run_watch_scan(db: Session) -> ScanResponse:
    _set_worker_status(db, "watch_scan", "running")
    metrics = _initial_metrics()
    source = "watch"
    old_cursor = _load_cursor(db)
    logger.info("watch scan started old_cursor=%s", old_cursor or "none")

    try:
        subscriptions = (
            db.query(Subscription)
            .filter(Subscription.status == "enabled")
            .order_by(Subscription.id.asc())
            .all()
        )
        metrics["scanned_subscriptions"] = len(subscriptions)
        topic_scan = _collect_new_topics(old_cursor)
        source = topic_scan.source
        metrics["fetched_pages"] = topic_scan.fetched_pages
        metrics["fetched_topic_items"] = topic_scan.fetched_topic_items
        metrics["unique_topics_before_cursor"] = len(topic_scan.topics)
        metrics["fetched_topics"] = len(topic_scan.topics)
        metrics["cursor_found"] = topic_scan.cursor_found
        metrics["cursor_gap"] = topic_scan.cursor_gap
        metrics["baseline_created"] = topic_scan.baseline_created

        _upsert_topics(db, topic_scan.topics_to_persist)
        _create_matching_notifications(db, subscriptions, topic_scan.topics, metrics)

        if topic_scan.new_cursor and not topic_scan.cursor_gap:
            _stage_cursor(db, topic_scan.new_cursor)
        db.commit()

        _dispatch_pending_notifications(db, metrics)
        status = "cursor_gap" if topic_scan.cursor_gap else "ok"
        error = f"old cursor {old_cursor} was not found within {topic_scan.fetched_pages} pages" if topic_scan.cursor_gap else None
        _set_worker_status(db, "watch_scan", status, error=error, metrics=metrics)
        logger.info(
            "watch scan completed status=%s source=%s pages=%d items=%d unique_topics=%d created_notifications=%d "
            "processed_notifications=%d dispatch_attempts=%d successes=%d failures=%d new_cursor=%s",
            status,
            source,
            metrics["fetched_pages"],
            metrics["fetched_topic_items"],
            metrics["unique_topics_before_cursor"],
            metrics["created_notifications"],
            metrics["processed_notifications"],
            metrics["dispatch_attempts"],
            metrics["dispatch_successes"],
            metrics["dispatch_failures"],
            topic_scan.new_cursor if not topic_scan.cursor_gap else "unchanged",
        )
        return ScanResponse(**metrics, source=source, status=status)
    except Exception as exc:
        db.rollback()
        _set_worker_status(db, "watch_scan", "failed", error=str(exc), metrics=metrics)
        logger.exception("watch scan failed old_cursor=%s source=%s", old_cursor or "none", source)
        raise
