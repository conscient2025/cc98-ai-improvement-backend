from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.matcher import rule_match
from app.notifiers import SendResult, build_batch_notification_text, send_email_notification


def _client(tmp_path: Path) -> TestClient:
    os.environ["APP_ENV"] = "development"
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path / 'test.db'}"
    os.environ["AUTH_DEV_PRINT_CODE"] = "true"
    os.environ["AUTH_EMAIL_DELIVERY"] = "false"
    os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
    os.environ["WATCH_FORCE_MOCK_TOPICS"] = "true"
    os.environ["MATCHER_FORCE_RULES"] = "true"
    os.environ["ENABLE_SCHEDULER"] = "false"
    os.environ["SUBSCRIPTION_LIMIT"] = "10"
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    os.environ["WATCH_INITIAL_CURSOR_MODE"] = "backfill"
    os.environ["NOTIFICATION_READ_RATE_LIMIT_SECONDS"] = "0"
    os.environ["MAX_NEW_POST_PAGES"] = "10"

    import app.database as database
    import app.main as main

    database.Base.metadata.drop_all(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    return TestClient(main.app)


def _login(client: TestClient, email: str = "student@zju.edu.cn") -> tuple[dict[str, str], str]:
    code_response = client.post("/api/v1/auth/request-code", json={"email": email})
    assert code_response.status_code == 200
    code = code_response.json()["dev_code"]
    assert code

    verify_response = client.post("/api/v1/auth/verify-code", json={"email": email, "code": code})
    assert verify_response.status_code == 200
    data = verify_response.json()
    token = data["access_token"]
    assert token
    return {"Authorization": f"Bearer {token}"}, data["user"]["id"]


def test_health(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["components"]["subscription_limit"] == 10
    assert response.json()["components"]["subscription_expression_max_length"] == 255


def test_auth_subscription_scan_notifications(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)

    sub_response = client.post(
        "/api/v1/subscriptions",
        headers=headers,
        json={"expression": "CC98 AI"},
    )
    assert sub_response.status_code == 200
    subscription = sub_response.json()
    assert subscription["status"] == "enabled"
    assert subscription["expression"] == "CC98 AI"
    assert "user_id" not in subscription

    scan_response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert scan["scanned_subscriptions"] == 1
    assert scan["created_notifications"] >= 1

    second_scan_response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert second_scan_response.status_code == 200
    assert second_scan_response.json()["created_notifications"] == 0

    notifications_response = client.get("/api/v1/notifications", headers=headers)
    assert notifications_response.status_code == 200
    notifications = notifications_response.json()
    assert notifications
    assert "dispatch_pending" not in notifications[0]
    assert "delivery_status" not in notifications[0]
    assert "is_read" not in notifications[0]


def test_subscription_limit(tmp_path: Path) -> None:
    client = _client(tmp_path)
    os.environ["SUBSCRIPTION_LIMIT"] = "1"
    headers, _user_id = _login(client)

    first = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "backend"})
    assert first.status_code == 200
    assert client.patch(f"/api/v1/subscriptions/{first.json()['id']}", headers=headers, json={"status": "paused"}).status_code == 200

    second = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "LLM"})
    assert second.status_code == 400
    assert "暂停的订阅也会计入" in second.json()["detail"]


def test_duplicate_subscription_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)
    payload = {"expression": "大一/求助/军训"}

    first = client.post("/api/v1/subscriptions", headers=headers, json=payload)
    second = client.post("/api/v1/subscriptions", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "相同的订阅" in second.json()["detail"]


def test_user_endpoints_require_bearer_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/subscriptions")

    assert response.status_code == 401


def test_subscription_rejects_too_short_keywords(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    response = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "了"})

    assert response.status_code == 400
    assert "至少需要 2 个字符" in response.json()["detail"]


def test_subscription_update_rejects_too_short_keywords(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    created = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "电脑"})
    assert created.status_code == 200

    response = client.patch(f"/api/v1/subscriptions/{created.json()['id']}", headers=headers, json={"expression": "的"})

    assert response.status_code == 400
    assert "至少需要 2 个字符" in response.json()["detail"]


def test_subscription_rejects_invalid_slashes_and_long_expressions(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    for expression in ("/实习", "实习/", "实习//校招"):
        response = client.post("/api/v1/subscriptions", headers=headers, json={"expression": expression})
        assert response.status_code == 400
        assert "斜杠两侧" in response.json()["detail"]

    full_width = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "实习／校招"})
    assert full_width.status_code == 400
    assert "半角" in full_width.json()["detail"]

    too_long = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "后" * 256})
    assert too_long.status_code == 400
    assert "255" in too_long.json()["detail"]


def test_legacy_subscription_routes_are_removed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    assert client.post("/api/subscribe", headers=headers, json={"expression": "实习"}).status_code == 404
    assert client.get("/api/subscriptions", headers=headers).status_code == 404


def test_scan_requires_admin_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/v1/tasks/scan")

    assert response.status_code == 401


def test_scan_fetches_global_latest_posts_once_for_multiple_subscriptions(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")
    monkeypatch.delenv("CC98_SERVICE_REFRESH_TOKEN", raising=False)

    first = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "新生"})
    second = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "校园"})
    assert first.status_code == 200
    assert second.status_code == 200

    from app import watch

    calls = 0

    def fake_get_new_posts(*, limit: int = 20, offset: int = 0) -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        assert limit == 20
        assert offset == 0
        return [
            {"topic_id": "latest-1", "title": "新生校园通知", "url": "https://www.cc98.org/topic/latest-1"},
            {"topic_id": "latest-2", "title": "校园活动", "url": "https://www.cc98.org/topic/latest-2"},
        ]

    monkeypatch.setattr(watch.cc98_client, "get_new_posts", fake_get_new_posts)

    scan_response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})

    assert scan_response.status_code == 200
    data = scan_response.json()
    assert calls == 1
    assert data["scanned_subscriptions"] == 2
    assert data["fetched_topics"] == 2
    assert data["candidate_pairs"] == 3
    assert data["created_notifications"] >= 2


def test_notification_without_channel_is_history_only_and_not_backfilled(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)
    assert client.post("/api/v1/subscriptions", headers=headers, json={"expression": "CC98 AI"}).status_code == 200

    first_scan = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert first_scan.status_code == 200
    assert first_scan.json()["created_notifications"] >= 1
    assert first_scan.json()["queued_notifications"] == 0
    assert client.get("/api/v1/notifications", headers=headers).json()

    assert client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 10,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    ).status_code == 200

    from app import watch

    calls: list[object] = []
    monkeypatch.setattr(watch, "send_batch_notification", lambda *args, **kwargs: calls.append((args, kwargs)))
    second_scan = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})

    assert second_scan.status_code == 200
    assert second_scan.json()["created_notifications"] == 0
    assert calls == []


def test_channel_interval_is_not_faster_than_scan(tmp_path: Path) -> None:
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    response = client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 1,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["notify_interval_minutes"] == 10
    assert "notify_interval_minutes" not in data["config"]


def test_channel_test_uses_draft_config_without_saving(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    from app import main

    monkeypatch.setattr(main, "send_notification", lambda *args, **kwargs: SendResult(ok=True, status="sent"))
    response = client.post(
        "/api/v1/notification-channels/test",
        headers=headers,
        json={"provider": "dingtalk", "config": {"webhook": "https://example.com/draft"}},
    )

    assert response.status_code == 200
    assert client.get("/api/v1/notification-channels", headers=headers).json() == []


def test_notification_interval_defers_pending_batch(tmp_path: Path, monkeypatch) -> None:
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    client = _client(tmp_path)
    headers, user_id = _login(client)
    channel_response = client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 60,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    )
    assert channel_response.status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification, NotificationPreference, utc_now

    calls: list[object] = []

    def fake_send_batch_notification(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("notification should be deferred until interval is due")

    monkeypatch.setattr(watch, "send_batch_notification", fake_send_batch_notification)

    db = SessionLocal()
    try:
        preference = db.get(NotificationPreference, user_id)
        assert preference is not None
        preference.last_dispatch_started_at = utc_now()
        notification = Notification(
            user_id=user_id,
            topic_id="interval-topic-1",
            topic_title="interval topic",
            topic_url="https://www.cc98.org/topic/interval-topic-1",
            matched_reason="hit keyword",
            dispatch_pending=True,
        )
        db.add(notification)
        db.commit()
        db.refresh(notification)

        result = watch._send_notification_batch(db, user_id, [notification])

        db.refresh(notification)
        assert result["processed_notifications"] == 0
        assert calls == []
        assert notification.dispatch_pending is True
    finally:
        db.close()


def test_user_dispatch_cycle_ignores_channel_delay_and_allows_scheduler_grace(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)
    assert client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 10,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    ).status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification, NotificationChannel, NotificationPreference

    now = datetime(2026, 9, 3, 3, 43, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(watch, "utc_now", lambda: now)
    monkeypatch.setenv("NOTIFICATION_DUE_GRACE_SECONDS", "5")
    calls: list[object] = []
    monkeypatch.setattr(
        watch,
        "send_batch_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SendResult(ok=True, status="sent"),
    )

    db = SessionLocal()
    try:
        preference = db.get(NotificationPreference, user_id)
        assert preference is not None
        preference.last_dispatch_started_at = now - timedelta(minutes=10) + timedelta(seconds=2)
        channel = db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).one()
        channel.last_attempted_at = now - timedelta(minutes=9, seconds=50)
        notification = Notification(
            user_id=user_id,
            topic_id="scheduler-boundary",
            topic_title="scheduler boundary",
            topic_url="https://www.cc98.org/topic/scheduler-boundary",
            dispatch_pending=True,
        )
        db.add(notification)
        db.commit()

        result = watch._send_notification_batch(db, user_id, [notification])

        db.refresh(preference)
        db.refresh(notification)
        assert result["dispatch_successes"] == 1
        assert len(calls) == 1
        assert preference.last_dispatch_started_at == now.replace(tzinfo=None)
        assert notification.dispatch_pending is False
    finally:
        db.close()


def test_first_matching_subscription_wins_for_same_user_topic(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)
    first = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "新生"})
    second = client.post("/api/v1/subscriptions", headers=headers, json={"expression": "校园"})
    assert first.status_code == 200
    assert second.status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification

    monkeypatch.setattr(
        watch,
        "fetch_new_topics",
        lambda limit=20: ([{"topic_id": "same-topic", "title": "新生校园通知", "url": "https://www.cc98.org/topic/same-topic"}], "mock"),
    )
    response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert response.json()["matched_user_topics"] == 1
    assert response.json()["created_notifications"] == 1

    db = SessionLocal()
    try:
        rows = db.query(Notification).filter(Notification.user_id == user_id, Notification.topic_id == "same-topic").all()
        assert len(rows) == 1
        assert "新生" in (rows[0].matched_reason or "")
    finally:
        db.close()


def test_notification_batch_dedupes_same_destination_across_users(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    first_headers, first_user_id = _login(client, "first@zju.edu.cn")
    second_headers, second_user_id = _login(client, "second@zju.edu.cn")
    shared_channel = {
        "provider": "dingtalk",
        "enabled": True,
        "notify_interval_minutes": 10,
        "config": {"webhook": "https://example.com/shared-webhook", "secret": ""},
    }
    assert client.put("/api/v1/notification-channels", headers=first_headers, json=shared_channel).status_code == 200
    assert client.put("/api/v1/notification-channels", headers=second_headers, json=shared_channel).status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification

    captured_items: list[list[dict[str, str | None]]] = []

    def fake_send_batch_notification(provider, config, items):
        captured_items.append(items)
        return SendResult(ok=True, status="sent")

    monkeypatch.setattr(watch, "send_batch_notification", fake_send_batch_notification)

    db = SessionLocal()
    try:
        first = Notification(
            user_id=first_user_id,
            topic_id="shared-topic",
            topic_title="同一个帖子",
            topic_url="https://www.cc98.org/topic/shared-topic",
            matched_reason="命中搜索表达式：求助",
            dispatch_pending=True,
        )
        second = Notification(
            user_id=second_user_id,
            topic_id="shared-topic",
            topic_title="同一个帖子",
            topic_url="https://www.cc98.org/topic/shared-topic",
            matched_reason="命中搜索表达式：求助",
            dispatch_pending=True,
        )
        db.add_all([first, second])
        db.commit()

        sent_destinations: dict[tuple[str, str], set[str]] = {}
        first_result = watch._send_notification_batch(db, first_user_id, [first], sent_destinations)
        second_result = watch._send_notification_batch(db, second_user_id, [second], sent_destinations)

        assert first_result["dispatch_successes"] == 1
        assert second_result["deduplicated_destination_topics"] == 1
        assert len(captured_items) == 1
        assert len(captured_items[0]) == 1
        db.refresh(first)
        db.refresh(second)
        assert first.dispatch_pending is False
        assert second.dispatch_pending is False
    finally:
        db.close()


def test_all_enabled_channels_are_attempted_and_failure_is_not_requeued(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)
    for provider, config in (
        ("email", {"to_email": "student@example.com"}),
        ("dingtalk", {"webhook": "https://example.com/hook"}),
    ):
        assert client.put(
            "/api/v1/notification-channels",
            headers=headers,
            json={"provider": provider, "enabled": True, "notify_interval_minutes": 10, "config": config},
        ).status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification, NotificationChannel

    providers: list[str] = []

    def fake_send(provider, config, items):
        providers.append(provider)
        return SendResult(ok=provider == "email", status="sent" if provider == "email" else "failed", error=None if provider == "email" else "bad webhook")

    monkeypatch.setattr(watch, "send_batch_notification", fake_send)
    db = SessionLocal()
    try:
        notification = Notification(
            user_id=user_id,
            topic_id="all-channels",
            topic_title="all channels",
            topic_url="https://www.cc98.org/topic/all-channels",
            dispatch_pending=True,
        )
        db.add(notification)
        db.commit()
        result = watch._send_notification_batch(db, user_id, [notification])
        db.refresh(notification)

        assert set(providers) == {"email", "dingtalk"}
        assert result["dispatch_successes"] == 1
        assert result["dispatch_failures"] == 1
        assert notification.dispatch_pending is False
        failed = db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id, NotificationChannel.provider == "dingtalk").one()
        assert failed.last_dispatch_status == "failed"
        assert failed.last_dispatch_error == "bad webhook"
    finally:
        db.close()


def test_scan_paginates_to_old_cursor_and_saves_first_topic_as_new_cursor(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)
    assert client.post("/api/v1/subscriptions", headers=headers, json={"expression": "目标"}).status_code == 200
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")

    from app import watch
    from app.database import SessionLocal
    from app.models import SystemCursor

    first_page = [
        {"topic_id": f"new-{index}", "title": f"目标帖子 {index}", "url": f"https://www.cc98.org/topic/new-{index}"}
        for index in range(40, 20, -1)
    ]
    second_page = [first_page[-1]] + [
        {"topic_id": f"new-{index}", "title": f"目标帖子 {index}", "url": f"https://www.cc98.org/topic/new-{index}"}
        for index in range(20, 16, -1)
    ] + [{"topic_id": "old-cursor", "title": "old", "url": "https://www.cc98.org/topic/old-cursor"}]
    calls: list[int] = []

    def fake_get_new_posts(*, limit: int = 20, offset: int = 0):
        calls.append(offset)
        return first_page if offset == 0 else second_page

    monkeypatch.setattr(watch.cc98_client, "get_new_posts", fake_get_new_posts)
    db = SessionLocal()
    try:
        db.add(SystemCursor(source=watch.CURSOR_SOURCE, cursor_value="old-cursor"))
        db.commit()
    finally:
        db.close()

    response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    data = response.json()
    assert response.status_code == 200
    assert calls == [0, 20]
    assert data["fetched_pages"] == 2
    assert data["fetched_topic_items"] == 26
    assert data["unique_topics_before_cursor"] == 24
    assert data["cursor_found"] is True

    db = SessionLocal()
    try:
        assert db.get(SystemCursor, watch.CURSOR_SOURCE).cursor_value == "new-40"
    finally:
        db.close()


def test_cursor_gap_preserves_old_cursor(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")
    monkeypatch.setenv("MAX_NEW_POST_PAGES", "2")

    from app import watch
    from app.database import SessionLocal
    from app.models import SystemCursor, WorkerStatus

    def fake_get_new_posts(*, limit: int = 20, offset: int = 0):
        return [
            {"topic_id": f"page-{offset}-{index}", "title": "topic", "url": f"https://www.cc98.org/topic/{offset}-{index}"}
            for index in range(20)
        ]

    monkeypatch.setattr(watch.cc98_client, "get_new_posts", fake_get_new_posts)
    db = SessionLocal()
    try:
        db.add(SystemCursor(source=watch.CURSOR_SOURCE, cursor_value="missing-cursor"))
        db.commit()
    finally:
        db.close()

    response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert response.json()["status"] == "cursor_gap"
    assert response.json()["cursor_gap"] is True

    db = SessionLocal()
    try:
        assert db.get(SystemCursor, watch.CURSOR_SOURCE).cursor_value == "missing-cursor"
        assert db.get(WorkerStatus, "watch_scan").status == "cursor_gap"
    finally:
        db.close()


def test_first_scan_establishes_server_baseline_without_historical_notifications(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)
    assert client.post("/api/v1/subscriptions", headers=headers, json={"expression": "最新"}).status_code == 200
    monkeypatch.setenv("WATCH_INITIAL_CURSOR_MODE", "baseline")
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")

    from app import watch
    from app.database import SessionLocal
    from app.models import SystemCursor

    monkeypatch.setattr(
        watch.cc98_client,
        "get_new_posts",
        lambda *, limit=20, offset=0: [{"topic_id": "server-head", "title": "最新帖子", "url": "https://www.cc98.org/topic/server-head"}],
    )
    response = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert response.json()["baseline_created"] is True
    assert response.json()["created_notifications"] == 0

    db = SessionLocal()
    try:
        assert db.get(SystemCursor, watch.CURSOR_SOURCE).cursor_value == "server-head"
    finally:
        db.close()


def test_notification_batches_are_split_at_configured_size(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)
    assert client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={"provider": "email", "enabled": True, "config": {"to_email": "student@example.com"}},
    ).status_code == 200
    monkeypatch.setenv("NOTIFICATION_BATCH_SIZE", "20")

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification

    sizes: list[int] = []
    monkeypatch.setattr(
        watch,
        "send_batch_notification",
        lambda provider, config, items: (sizes.append(len(items)) or SendResult(ok=True, status="sent")),
    )
    db = SessionLocal()
    try:
        notifications = [
            Notification(
                user_id=user_id,
                topic_id=f"batch-{index}",
                topic_title=f"topic {index}",
                topic_url=f"https://www.cc98.org/topic/batch-{index}",
                dispatch_pending=True,
            )
            for index in range(21)
        ]
        db.add_all(notifications)
        db.commit()
        result = watch._send_notification_batch(db, user_id, notifications)

        assert sizes == [20, 1]
        assert result["dispatch_attempts"] == 2
        assert all(not item.dispatch_pending for item in notifications)
    finally:
        db.close()


def test_notification_list_rate_limit_is_per_user(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    first_headers, _first_user_id = _login(client, "first@zju.edu.cn")
    second_headers, _second_user_id = _login(client, "second@zju.edu.cn")
    monkeypatch.setenv("NOTIFICATION_READ_RATE_LIMIT_SECONDS", "60")

    assert client.get("/api/v1/notifications", headers=first_headers).status_code == 200
    limited = client.get("/api/v1/notifications", headers=first_headers)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert client.get("/api/v1/notifications", headers=second_headers).status_code == 200


def test_real_scan_without_board_uses_global_latest(monkeypatch) -> None:
    from app import watch

    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")
    monkeypatch.delenv("CC98_SERVICE_REFRESH_TOKEN", raising=False)

    def fake_get_new_posts(*, limit: int = 20, offset: int = 0) -> list[dict[str, str]]:
        assert limit == 20
        assert offset == 0
        return [{"topic_id": "latest-1", "title": "latest", "url": "https://www.cc98.org/topic/latest-1"}]

    monkeypatch.setattr(watch.cc98_client, "get_new_posts", fake_get_new_posts)

    topics, source = watch.fetch_new_topics()

    assert source == "cc98_new_posts"
    assert topics[0]["topic_id"] == "latest-1"


def test_production_scan_does_not_fallback_to_mock(monkeypatch) -> None:
    from app import watch

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.delenv("CC98_SERVICE_USERNAME", raising=False)
    monkeypatch.delenv("CC98_SERVICE_REFRESH_TOKEN", raising=False)

    try:
        watch.fetch_new_topics()
    except RuntimeError as exc:
        assert "CC98 service account is not configured" in str(exc)
    else:
        raise AssertionError("production scan should fail instead of falling back to mock topics")


def test_batch_notification_text() -> None:
    text = build_batch_notification_text(
        [
            {"title": "topic 1", "url": "https://www.cc98.org/topic/1", "reason": "hit keyword"},
            {"title": "topic 2", "url": "https://www.cc98.org/topic/2", "reason": "semantic match"},
        ]
    )
    assert "2 个匹配帖子" in text
    assert "topic 1" in text
    assert "topic 2" in text


def test_email_notification_accepts_to_email(monkeypatch) -> None:
    from app import notifiers

    captured: dict[str, object] = {}

    def fake_send_email(to_addr: str, subject: str, body: str, config: dict[str, object] | None = None) -> SendResult:
        captured["to_addr"] = to_addr
        captured["subject"] = subject
        captured["body"] = body
        captured["config"] = config
        return SendResult(ok=True, status="sent")

    monkeypatch.setattr(notifiers, "_send_email", fake_send_email)

    result = send_email_notification(
        {"to_email": "student@qq.com", "subject_prefix": "CC98 订阅提醒"},
        "测试内容",
        count=2,
    )

    assert result.ok is True
    assert captured["to_addr"] == "student@qq.com"
    assert captured["subject"] == "CC98 订阅提醒：2 个新匹配帖子"


def test_keyword_expression_uses_space_and_slash_synonym_or() -> None:
    subscription = {"expression": "微积分/微甲/vjf 历年卷 资料"}

    synonym_hit = rule_match(
        subscription,
        {"title": "求微甲历年卷资料整理", "content": ""},
    )
    assert synonym_hit.matched is True
    assert "微甲 + 历年卷 + 资料" in synonym_hit.reason

    latin_synonym_hit = rule_match(
        subscription,
        {"title": "vjf 历年卷资料分享", "content": ""},
    )
    assert latin_synonym_hit.matched is True
    assert "vjf + 历年卷 + 资料" in latin_synonym_hit.reason

    missing_required_group = rule_match(
        subscription,
        {"title": "微积分资料汇总", "content": ""},
    )
    assert missing_required_group.matched is False

    missing_synonym_group = rule_match(
        subscription,
        {"title": "高数历年卷资料分享", "content": ""},
    )
    assert missing_synonym_group.matched is False


def test_keyword_expression_can_model_a_and_b_or_c() -> None:
    subscription = {"expression": "计算机学院/计院 保研/推免"}

    result = rule_match(subscription, {"title": "计院推免通知整理", "content": ""})

    assert result.matched is True
    assert "计院 + 推免" in result.reason


def test_keyword_expression_keeps_non_operator_punctuation_literal() -> None:
    subscription = {"expression": "C++ 后端/服务端"}

    result = rule_match(subscription, {"title": "C++ 服务端开发交流", "content": ""})
    assert result.matched is True
    assert "C++ + 服务端" in result.reason

    comma_is_literal = rule_match(
        {"expression": "实习，校招"},
        {"title": "实习和校招信息", "content": ""},
    )
    assert comma_is_literal.matched is False


def test_cc98_requests_ignore_system_proxy_by_default(monkeypatch) -> None:
    from app import cc98_auth, cc98_client

    monkeypatch.delenv("CC98_TRUST_ENV", raising=False)
    captured: list[tuple[str, bool]] = []

    class TokenResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"access_token": "token", "expires_in": 3600}

    class ProbeResponse:
        status_code = 401

    def fake_post(*args, **kwargs):
        captured.append(("post", kwargs["trust_env"]))
        return TokenResponse()

    def fake_get(*args, **kwargs):
        captured.append(("get", kwargs["trust_env"]))
        return ProbeResponse()

    monkeypatch.setattr(cc98_auth.httpx, "post", fake_post)
    monkeypatch.setattr(cc98_client.httpx, "get", fake_get)

    assert cc98_auth.CC98ServiceAuth()._post_token({"grant_type": "password"})
    assert cc98_client.CC98ServiceClient().probe()["reachable"] is True
    assert captured == [("post", False), ("get", False)]


def test_cc98_new_posts_retries_403_with_bounded_backoff(monkeypatch) -> None:
    from app import cc98_client

    client = cc98_client.CC98ServiceClient()
    client.new_posts_min_interval = 0
    client.new_posts_retry_attempts = 2
    attempts = 0

    def fake_get_json(path, *, params=None, retry_auth=True):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise cc98_client.CC98APIError("GET", "https://api.cc98.org/topic/new", 403, "")
        return [{"id": 123, "title": "latest"}]

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    monkeypatch.setattr(cc98_client.time, "sleep", lambda seconds: None)

    topics = client.get_new_posts(limit=20, offset=40)
    assert attempts == 3
    assert topics[0]["topic_id"] == "123"


def test_legacy_sqlite_notifications_are_deduplicated_and_retired(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect, text

    from app import database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE notifications (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    subscription_id INTEGER NOT NULL,
                    topic_id VARCHAR(128) NOT NULL,
                    topic_title VARCHAR(500) NOT NULL,
                    topic_url TEXT NOT NULL,
                    matched_reason TEXT,
                    delivery_channel VARCHAR(32),
                    delivery_status VARCHAR(32) NOT NULL,
                    sent_at DATETIME,
                    is_read BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_notification_user_subscription_topic
                        UNIQUE (user_id, subscription_id, topic_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notifications (
                    id, user_id, subscription_id, topic_id, topic_title, topic_url,
                    matched_reason, delivery_status, is_read, created_at
                ) VALUES
                    (1, 'user-1', 10, 'topic-1', 'first', 'https://example.com/1', 'first reason', 'sent', 0, CURRENT_TIMESTAMP),
                    (2, 'user-1', 11, 'topic-1', 'second', 'https://example.com/1', 'second reason', 'failed', 1, CURRENT_TIMESTAMP)
                """
            )
        )

    monkeypatch.setattr(database, "engine", migration_engine)
    database.init_db()

    inspector = inspect(migration_engine)
    columns = {column["name"] for column in inspector.get_columns("notifications")}
    assert "dispatch_pending" in columns
    assert "subscription_id" not in columns
    assert "is_read" not in columns
    assert any(set(item["column_names"]) == {"user_id", "topic_id"} for item in inspector.get_unique_constraints("notifications"))
    with migration_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, matched_reason, dispatch_pending, dispatch_processed_at FROM notifications")
        ).all()
    assert len(rows) == 1
    assert rows[0].id == 1
    assert rows[0].matched_reason == "first reason"
    assert rows[0].dispatch_pending == 0
    assert rows[0].dispatch_processed_at is not None


def test_removing_is_read_preserves_current_pending_queue(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect, text

    from app import database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'current-notifications.db'}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notifications ("
                "id INTEGER NOT NULL PRIMARY KEY, user_id VARCHAR(64) NOT NULL, topic_id VARCHAR(128) NOT NULL, "
                "topic_title VARCHAR(500) NOT NULL, topic_url TEXT NOT NULL, matched_reason TEXT, "
                "dispatch_pending BOOLEAN NOT NULL DEFAULT 0, dispatch_processed_at DATETIME, "
                "is_read BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME NOT NULL, "
                "UNIQUE (user_id, topic_id))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notifications "
                "(id, user_id, topic_id, topic_title, topic_url, dispatch_pending, is_read, created_at) "
                "VALUES (1, 'user-1', 'pending-topic', 'pending', 'https://example.com/pending', 1, 0, CURRENT_TIMESTAMP)"
            )
        )

    monkeypatch.setattr(database, "engine", migration_engine)
    database.init_db()

    assert "is_read" not in {column["name"] for column in inspect(migration_engine).get_columns("notifications")}
    with migration_engine.connect() as connection:
        row = connection.execute(
            text("SELECT dispatch_pending, dispatch_processed_at FROM notifications WHERE id = 1")
        ).one()
    assert row.dispatch_pending == 1
    assert row.dispatch_processed_at is None


def test_legacy_channel_and_rate_limit_status_are_migrated(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect, text

    from app import database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-channel-status.db'}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notification_channels ("
                "id INTEGER NOT NULL PRIMARY KEY, user_id VARCHAR(64) NOT NULL, provider VARCHAR(32) NOT NULL, "
                "config_json TEXT NOT NULL, enabled BOOLEAN NOT NULL, last_test_at DATETIME, last_sent_at DATETIME, "
                "last_attempted_at DATETIME, last_test_status VARCHAR(64), last_error TEXT, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notification_channels "
                "(id, user_id, provider, config_json, enabled, last_test_status, last_error, created_at, updated_at) "
                "VALUES (1, 'user-1', 'dingtalk', '{}', 1, 'failed', 'bad webhook', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE notification_read_states ("
                "user_id VARCHAR(64) NOT NULL PRIMARY KEY, last_success_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO notification_read_states VALUES ('user-1', CURRENT_TIMESTAMP)")
        )

    monkeypatch.setattr(database, "engine", migration_engine)
    database.init_db()

    with migration_engine.connect() as connection:
        channel = connection.execute(
            text("SELECT last_dispatch_status, last_dispatch_error FROM notification_channels WHERE id = 1")
        ).one()
        rate_count = connection.execute(text("SELECT COUNT(*) FROM notification_list_rate_limit_states")).scalar_one()
    assert channel.last_dispatch_status == "failed"
    assert channel.last_dispatch_error == "bad webhook"
    assert rate_count == 1
    assert "notification_read_states" not in inspect(migration_engine).get_table_names()


def test_legacy_sqlite_subscriptions_are_migrated_to_expressions(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect, text

    from app import database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-subscriptions.db'}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE subscriptions ("
                "id INTEGER NOT NULL PRIMARY KEY, user_id VARCHAR(64) NOT NULL, "
                "name VARCHAR(255) NOT NULL, description TEXT NOT NULL, board_id VARCHAR(128), "
                "status VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO subscriptions "
                "(id, user_id, name, description, board_id, status, created_at, updated_at) VALUES "
                "(1, 'user-1', 'display only', 'C++，后端/服务端', NULL, 'paused', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "(2, 'user-1', 'AI 校招', '', NULL, 'enabled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    monkeypatch.setattr(database, "engine", migration_engine)
    database.init_db()

    inspector = inspect(migration_engine)
    columns = {column["name"] for column in inspector.get_columns("subscriptions")}
    assert "expression" in columns
    assert "name" not in columns
    assert "description" not in columns
    assert "board_id" not in columns
    with migration_engine.connect() as connection:
        rows = connection.execute(text("SELECT id, expression, status FROM subscriptions ORDER BY id")).all()
    assert [(row.id, row.expression, row.status) for row in rows] == [
        (1, "C++ 后端/服务端", "paused"),
        (2, "AI 校招", "enabled"),
    ]


def test_legacy_notification_preferences_gain_user_dispatch_timestamp(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import create_engine, inspect, text

    from app import database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-preferences.db'}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notification_preferences ("
                "user_id VARCHAR(64) NOT NULL PRIMARY KEY, "
                "notify_interval_minutes INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notification_preferences "
                "(user_id, notify_interval_minutes, created_at, updated_at) "
                "VALUES ('user-1', 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    monkeypatch.setattr(database, "engine", migration_engine)
    database.init_db()

    columns = {column["name"] for column in inspect(migration_engine).get_columns("notification_preferences")}
    assert "last_dispatch_started_at" in columns
