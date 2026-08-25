from __future__ import annotations

import os
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
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_subscription_scan_notifications(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, user_id = _login(client)

    sub_response = client.post(
        "/api/subscribe",
        headers=headers,
        json={"user_id": "demo_user", "topic": "CC98 AI", "description": "search and watch notification"},
    )
    assert sub_response.status_code == 200
    subscription = sub_response.json()
    assert subscription["active"] is True
    assert subscription["topic"] == "CC98 AI"
    assert subscription["user_id"] == user_id

    scan_response = client.post("/api/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert scan["scanned_subscriptions"] == 1
    assert scan["created_notifications"] >= 1

    second_scan_response = client.post("/api/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert second_scan_response.status_code == 200
    assert second_scan_response.json()["created_notifications"] == 0

    notifications_response = client.get("/api/notifications", headers=headers)
    assert notifications_response.status_code == 200
    notifications = notifications_response.json()
    assert notifications
    assert notifications[0]["delivery_status"] in {"pending", "sent", "failed"}


def test_subscription_limit(tmp_path: Path) -> None:
    client = _client(tmp_path)
    os.environ["SUBSCRIPTION_LIMIT"] = "1"
    headers, _user_id = _login(client)

    first = client.post("/api/v1/subscriptions", headers=headers, json={"user_id": "u1", "name": "backend", "description": "FastAPI"})
    assert first.status_code == 200

    second = client.post("/api/v1/subscriptions", headers=headers, json={"user_id": "u1", "name": "AI", "description": "LLM"})
    assert second.status_code == 400


def test_user_endpoints_require_bearer_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/subscriptions")

    assert response.status_code == 401


def test_subscription_rejects_too_short_keywords(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    response = client.post("/api/v1/subscriptions", headers=headers, json={"name": "了", "description": "的"})

    assert response.status_code == 400
    assert "2 个字以上" in response.json()["detail"]


def test_subscription_update_rejects_too_short_keywords(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    created = client.post("/api/v1/subscriptions", headers=headers, json={"name": "电脑", "description": ""})
    assert created.status_code == 200

    response = client.patch(f"/api/v1/subscriptions/{created.json()['id']}", headers=headers, json={"name": "的", "description": "了"})

    assert response.status_code == 400
    assert "2 个字以上" in response.json()["detail"]


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

    first = client.post("/api/v1/subscriptions", headers=headers, json={"name": "新生", "description": ""})
    second = client.post("/api/v1/subscriptions", headers=headers, json={"name": "校园", "description": ""})
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
    assert data["candidate_pairs"] == 4
    assert data["created_notifications"] >= 2


def test_pending_notifications_are_sent_after_channel_is_configured(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    created = client.post("/api/v1/subscriptions", headers=headers, json={"name": "CC98 AI", "description": ""})
    assert created.status_code == 200

    first_scan = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})
    assert first_scan.status_code == 200
    assert first_scan.json()["created_notifications"] >= 1

    notifications_response = client.get("/api/v1/notifications", headers=headers)
    assert notifications_response.status_code == 200
    assert notifications_response.json()[0]["delivery_status"] == "pending"

    channel_response = client.put(
        "/api/v1/notification-channels",
        headers=headers,
        json={
            "provider": "dingtalk",
            "enabled": True,
            "notify_interval_minutes": 10,
            "config": {"webhook": "https://example.com/webhook", "secret": ""},
        },
    )
    assert channel_response.status_code == 200

    from app import watch

    sent_batches: list[object] = []

    def fake_send_batch_notification(*args, **kwargs):
        sent_batches.append((args, kwargs))
        return SendResult(ok=True, status="sent")

    monkeypatch.setattr(watch, "send_batch_notification", fake_send_batch_notification)

    second_scan = client.post("/api/v1/tasks/scan", headers={"X-Admin-Token": "test-admin-token"})

    assert second_scan.status_code == 200
    assert second_scan.json()["created_notifications"] == 0
    assert second_scan.json()["sent_notifications"] >= 1
    assert len(sent_batches) == 1
    assert client.get("/api/v1/notifications", headers=headers).json()[0]["delivery_status"] == "sent"


def test_channel_interval_is_not_faster_than_scan(tmp_path: Path) -> None:
    os.environ["SCAN_INTERVAL_MINUTES"] = "10"
    client = _client(tmp_path)
    headers, _user_id = _login(client)

    response = client.put(
        "/api/v1/notification-channels",
        headers=headers,
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
    headers, user_id = _login(client)
    channel_response = client.put(
        "/api/v1/notification-channels",
        headers=headers,
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
        channel = db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).one()
        channel.last_sent_at = utc_now()
        notification = Notification(
            user_id=user_id,
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

        sent = watch._send_notification_batch(db, user_id, [notification])

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


def test_production_scan_does_not_fallback_to_mock(monkeypatch) -> None:
    from app import watch
    from app.models import Subscription

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WATCH_FORCE_MOCK_TOPICS", "false")
    monkeypatch.delenv("CC98_SERVICE_USERNAME", raising=False)
    monkeypatch.delenv("CC98_SERVICE_REFRESH_TOKEN", raising=False)

    subscription = Subscription(user_id="demo_user", name="latest", description="latest")

    try:
        watch.fetch_new_topics_for_subscription(subscription)
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
    subscription = {"name": "微积分/微甲/vjf 历年卷 资料", "description": ""}

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
    subscription = {"name": "计算机学院/计院 保研/推免", "description": ""}

    result = rule_match(subscription, {"title": "计院推免通知整理", "content": ""})

    assert result.matched is True
    assert "计院 + 推免" in result.reason


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
