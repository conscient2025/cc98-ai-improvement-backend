from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.matcher import rule_match
from app.notifiers import build_batch_notification_text


def _client(tmp_path: Path) -> TestClient:
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    os.environ["AUTH_DEV_PRINT_CODE"] = "true"
    os.environ["WATCH_FORCE_MOCK_TOPICS"] = "true"
    os.environ["MATCHER_FORCE_RULES"] = "true"
    os.environ["ENABLE_SCHEDULER"] = "false"

    import app.database as database
    import app.main as main

    database.Base.metadata.drop_all(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    return TestClient(main.app)


def test_health(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_subscription_scan_notifications(tmp_path: Path) -> None:
    client = _client(tmp_path)

    code_response = client.post("/api/v1/auth/request-code", json={"email": "student@zju.edu.cn"})
    assert code_response.status_code == 200
    code = code_response.json()["dev_code"]
    assert code

    verify_response = client.post("/api/v1/auth/verify-code", json={"email": "student@zju.edu.cn", "code": code})
    assert verify_response.status_code == 200
    assert verify_response.json()["access_token"]

    sub_response = client.post(
        "/api/subscribe",
        json={"user_id": "demo_user", "topic": "CC98 AI", "description": "search and watch notification"},
    )
    assert sub_response.status_code == 200
    subscription = sub_response.json()
    assert subscription["active"] is True
    assert subscription["topic"] == "CC98 AI"

    scan_response = client.post("/api/tasks/scan")
    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert scan["scanned_subscriptions"] == 1
    assert scan["created_notifications"] >= 1

    second_scan_response = client.post("/api/tasks/scan")
    assert second_scan_response.status_code == 200
    assert second_scan_response.json()["created_notifications"] == 0

    notifications_response = client.get("/api/notifications")
    assert notifications_response.status_code == 200
    notifications = notifications_response.json()
    assert notifications
    assert notifications[0]["delivery_status"] in {"skipped", "sent", "failed"}


def test_subscription_limit(tmp_path: Path) -> None:
    os.environ["SUBSCRIPTION_LIMIT"] = "1"
    client = _client(tmp_path)

    first = client.post("/api/v1/subscriptions", json={"user_id": "u1", "name": "backend", "description": "FastAPI"})
    assert first.status_code == 200

    second = client.post("/api/v1/subscriptions", json={"user_id": "u1", "name": "AI", "description": "LLM"})
    assert second.status_code == 400


def test_channel_interval_is_not_faster_than_scan(tmp_path: Path) -> None:
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    client = _client(tmp_path)

    response = client.put(
        "/api/v1/notification-channels",
        json={
            "user_id": "demo_user",
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 1,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["notify_interval_minutes"] == 10
    assert data["config"]["notify_interval_minutes"] == 10


def test_notification_interval_defers_pending_batch(tmp_path: Path, monkeypatch) -> None:
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    client = _client(tmp_path)
    channel_response = client.put(
        "/api/v1/notification-channels",
        json={
            "user_id": "demo_user",
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 60,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    )
    assert channel_response.status_code == 200

    from app import watch
    from app.database import SessionLocal
    from app.models import Notification, NotificationChannel, utc_now

    calls: list[object] = []

    def fake_send_batch_notification(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("notification should be deferred until interval is due")

    monkeypatch.setattr(watch, "send_batch_notification", fake_send_batch_notification)

    db = SessionLocal()
    try:
        channel = db.query(NotificationChannel).filter(NotificationChannel.user_id == "demo_user").one()
        channel.last_sent_at = utc_now()
        notification = Notification(
            user_id="demo_user",
            subscription_id=1,
            topic_id="interval-topic-1",
            topic_title="interval topic",
            topic_url="https://www.cc98.org/topic/interval-topic-1",
            matched_reason="hit keyword",
            delivery_status="pending",
        )
        db.add(notification)
        db.commit()
        db.refresh(notification)

        sent = watch._send_notification_batch(db, "demo_user", [notification])

        db.refresh(notification)
        assert sent == 0
        assert calls == []
        assert notification.delivery_status == "pending"
    finally:
        db.close()


def test_real_scan_without_board_uses_global_latest(monkeypatch) -> None:
    from app import watch
    from app.models import Subscription

    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.setenv("CC98_SERVICE_USERNAME", "demo")
    monkeypatch.delenv("CC98_SERVICE_REFRESH_TOKEN", raising=False)

    def fake_get_new_posts(*, limit: int = 20, offset: int = 0) -> list[dict[str, str]]:
        assert limit == 20
        assert offset == 0
        return [{"topic_id": "latest-1", "title": "latest", "url": "https://www.cc98.org/topic/latest-1"}]

    monkeypatch.setattr(watch.cc98_client, "get_new_posts", fake_get_new_posts)

    subscription = Subscription(user_id="demo_user", name="latest", description="latest")
    topics, source = watch.fetch_new_topics_for_subscription(subscription)

    assert source == "cc98_new_posts"
    assert topics[0]["topic_id"] == "latest-1"


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


def test_keyword_groups_use_and_inside_or_between_groups() -> None:
    subscription = {"name": "计算机学院 保研，软院 推免", "description": ""}

    first_group = rule_match(
        subscription,
        {"title": "计算机学院保研经验分享", "content": ""},
    )
    assert first_group.matched is True
    assert "计算机学院 + 保研" in first_group.reason

    second_group = rule_match(
        subscription,
        {"title": "软院推免通知整理", "content": ""},
    )
    assert second_group.matched is True
    assert "软院 + 推免" in second_group.reason

    partial = rule_match(
        subscription,
        {"title": "计算机学院活动通知", "content": ""},
    )
    assert partial.matched is False

    unrelated_mix = rule_match(
        subscription,
        {"title": "软院保研交流帖", "content": ""},
    )
    assert unrelated_mix.matched is False
