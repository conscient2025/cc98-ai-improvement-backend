from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

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
