from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from . import models
from .auth import request_email_code, verify_email_code
from .cc98_client import cc98_client
from .database import get_db, init_db
from .env import load_dotenv
from .notifiers import redact_config, send_notification
from .schemas import (
    AdminHealthOut,
    AuthRequestCodeIn,
    AuthRequestCodeOut,
    AuthTokenOut,
    AuthVerifyCodeIn,
    HealthResponse,
    NotificationChannelOut,
    NotificationChannelSave,
    NotificationOut,
    NotificationSettingOut,
    NotificationSettingSave,
    NotificationSettingTest,
    ScanResponse,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
    UserOut,
)
from .tasks import start_scheduler, stop_scheduler
from .utils import json_dumps, json_loads, normalize_topic_text, utc_now
from .watch import run_watch_scan


load_dotenv()
init_db()

app = FastAPI(title="CC98 AI Improvement Backend", version="0.1.0")

origins = os.getenv("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if origins == "*" else [item.strip() for item in origins.split(",") if item.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    start_scheduler()


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_scheduler()


def _subscription_out(subscription: models.Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=subscription.id,
        user_id=subscription.user_id,
        name=subscription.name,
        description=subscription.description,
        topic=subscription.name,
        board_id=subscription.board_id,
        status=subscription.status,
        active=subscription.status == "enabled",
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


def _channel_out(channel: models.NotificationChannel) -> NotificationChannelOut:
    config, has_secret = redact_config(channel.provider, json_loads(channel.config_json, {}))
    return NotificationChannelOut(
        id=channel.id,
        user_id=channel.user_id,
        provider=channel.provider,
        enabled=channel.enabled,
        config=config,
        has_secret=has_secret,
        last_test_at=channel.last_test_at,
        last_test_status=channel.last_test_status,
        last_error=channel.last_error,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _notification_out(notification: models.Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        user_id=notification.user_id,
        subscription_id=notification.subscription_id,
        topic_id=notification.topic_id,
        topic_title=notification.topic_title,
        topic_url=notification.topic_url,
        topic=notification.topic_title,
        matched_reason=notification.matched_reason,
        summary=notification.matched_reason,
        delivery_channel=notification.delivery_channel,
        delivery_status=notification.delivery_status,
        sent_at=notification.sent_at,
        is_read=notification.is_read,
        created_at=notification.created_at,
    )


def _get_or_create_channel(db: Session, payload: NotificationChannelSave) -> models.NotificationChannel:
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == payload.user_id, models.NotificationChannel.provider == payload.provider)
        .first()
    )
    if channel is None:
        channel = models.NotificationChannel(
            user_id=payload.user_id,
            provider=payload.provider,
            config_json=json_dumps(payload.config),
            enabled=payload.enabled,
        )
        db.add(channel)
    else:
        old_config = json_loads(channel.config_json, {})
        new_config = dict(payload.config)
        for secret_key in ("secret", "token", "password"):
            if new_config.get(secret_key) == "***" and old_config.get(secret_key):
                new_config[secret_key] = old_config[secret_key]
        channel.config_json = json_dumps(new_config)
        channel.enabled = payload.enabled
        channel.updated_at = utc_now()
    db.commit()
    db.refresh(channel)
    return channel


@app.get("/api/health", response_model=HealthResponse)
@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        components={
            "database": "ok",
            "scheduler_enabled": os.getenv("ENABLE_SCHEDULER", "false").lower() in {"1", "true", "yes", "on"},
            "cc98_mode": "service_account" if os.getenv("CC98_SERVICE_USERNAME") else "mock_fallback",
        },
    )


@app.post("/api/v1/auth/request-code", response_model=AuthRequestCodeOut)
def auth_request_code(payload: AuthRequestCodeIn, db: Session = Depends(get_db)) -> AuthRequestCodeOut:
    email, dev_code = request_email_code(db, payload.email)
    return AuthRequestCodeOut(status="ok", email=email, dev_code=dev_code)


@app.post("/api/v1/auth/verify-code", response_model=AuthTokenOut)
def auth_verify_code(payload: AuthVerifyCodeIn, db: Session = Depends(get_db)) -> AuthTokenOut:
    user, token = verify_email_code(db, payload.email, payload.code)
    return AuthTokenOut(
        access_token=token,
        user=UserOut(
            id=user.id,
            email=user.email,
            email_verified_at=user.email_verified_at,
            status=user.status,
            created_at=user.created_at,
        ),
    )


@app.post("/api/v1/subscriptions", response_model=SubscriptionOut)
@app.post("/api/subscribe", response_model=SubscriptionOut)
def create_subscription(payload: SubscriptionCreate, db: Session = Depends(get_db)) -> SubscriptionOut:
    name = normalize_topic_text(payload.name or payload.topic or "")
    description = normalize_topic_text(payload.description or payload.topic or payload.name or "")
    if not name:
        raise HTTPException(status_code=400, detail="订阅名称不能为空")
    active_count = (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == payload.user_id, models.Subscription.status == "enabled")
        .count()
    )
    limit = int(os.getenv("SUBSCRIPTION_LIMIT", "10"))
    if active_count >= limit:
        raise HTTPException(status_code=400, detail=f"最多只能启用 {limit} 个订阅")
    subscription = models.Subscription(
        user_id=payload.user_id,
        name=name,
        description=description,
        board_id=payload.board_id,
        status="enabled",
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return _subscription_out(subscription)


@app.get("/api/v1/subscriptions", response_model=list[SubscriptionOut])
@app.get("/api/subscriptions", response_model=list[SubscriptionOut])
@app.get("/api/admin/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(user_id: str = Query("demo_user"), db: Session = Depends(get_db)) -> list[SubscriptionOut]:
    rows = db.query(models.Subscription).filter(models.Subscription.user_id == user_id).order_by(models.Subscription.id.desc()).all()
    return [_subscription_out(row) for row in rows]


@app.patch("/api/v1/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(subscription_id: int, payload: SubscriptionUpdate, db: Session = Depends(get_db)) -> SubscriptionOut:
    subscription = db.get(models.Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    if payload.name is not None:
        subscription.name = normalize_topic_text(payload.name)
    if payload.description is not None:
        subscription.description = normalize_topic_text(payload.description)
    if payload.board_id is not None:
        subscription.board_id = payload.board_id
    if payload.status is not None:
        subscription.status = payload.status
    subscription.updated_at = utc_now()
    db.commit()
    db.refresh(subscription)
    return _subscription_out(subscription)


@app.delete("/api/v1/subscriptions/{subscription_id}")
@app.delete("/api/subscribe/{subscription_id}")
def delete_subscription(subscription_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    subscription = db.get(models.Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    db.delete(subscription)
    db.commit()
    return {"status": "ok", "deleted": subscription_id}


@app.get("/api/v1/notification-channels", response_model=list[NotificationChannelOut])
def list_channels(user_id: str = Query("demo_user"), db: Session = Depends(get_db)) -> list[NotificationChannelOut]:
    rows = db.query(models.NotificationChannel).filter(models.NotificationChannel.user_id == user_id).all()
    return [_channel_out(row) for row in rows]


@app.put("/api/v1/notification-channels", response_model=NotificationChannelOut)
def save_channel(payload: NotificationChannelSave, db: Session = Depends(get_db)) -> NotificationChannelOut:
    return _channel_out(_get_or_create_channel(db, payload))


@app.post("/api/v1/notification-channels/test")
def test_channel(payload: NotificationChannelSave, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = send_notification(payload.provider, payload.config, "CC98 AI Watch 测试通知", "https://www.cc98.org", "如果你收到这条消息，说明通知通道可用")
    channel = _get_or_create_channel(db, payload)
    channel.last_test_at = utc_now()
    channel.last_test_status = result.status
    channel.last_error = result.error
    db.commit()
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error or "通知发送失败")
    return {"status": "ok"}


@app.get("/api/notification-settings", response_model=NotificationSettingOut)
def get_legacy_notification_settings(user_id: str = Query("demo_user"), db: Session = Depends(get_db)) -> NotificationSettingOut:
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == user_id, models.NotificationChannel.provider == "dingtalk")
        .first()
    )
    if channel is None:
        return NotificationSettingOut(user_id=user_id, dingtalk_enabled=False)
    config = json_loads(channel.config_json, {})
    return NotificationSettingOut(
        user_id=user_id,
        dingtalk_enabled=channel.enabled,
        dingtalk_webhook=config.get("webhook"),
        has_dingtalk_secret=bool(config.get("secret")),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@app.put("/api/notification-settings", response_model=NotificationSettingOut)
def save_legacy_notification_settings(payload: NotificationSettingSave, db: Session = Depends(get_db)) -> NotificationSettingOut:
    channel_payload = NotificationChannelSave(
        user_id=payload.user_id,
        provider="dingtalk",
        enabled=payload.dingtalk_enabled,
        config={"webhook": payload.dingtalk_webhook or "", "secret": payload.dingtalk_secret or ""},
    )
    _get_or_create_channel(db, channel_payload)
    return get_legacy_notification_settings(payload.user_id, db)


@app.post("/api/notification-settings/test")
def test_legacy_notification_settings(payload: NotificationSettingTest, db: Session = Depends(get_db)) -> dict[str, Any]:
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == payload.user_id, models.NotificationChannel.provider == "dingtalk")
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=400, detail="请先配置 DingTalk 通知渠道")
    config = json_loads(channel.config_json, {})
    result = send_notification("dingtalk", config, payload.message, "https://www.cc98.org", "通知通道测试")
    channel.last_test_at = utc_now()
    channel.last_test_status = result.status
    channel.last_error = result.error
    db.commit()
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error or "通知发送失败")
    return {"status": "ok"}


@app.get("/api/v1/notifications", response_model=list[NotificationOut])
@app.get("/api/notifications", response_model=list[NotificationOut])
def list_notifications(user_id: str = Query("demo_user"), db: Session = Depends(get_db)) -> list[NotificationOut]:
    rows = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == user_id)
        .order_by(models.Notification.id.desc())
        .limit(100)
        .all()
    )
    return [_notification_out(row) for row in rows]


@app.post("/api/v1/tasks/scan", response_model=ScanResponse)
@app.post("/api/tasks/scan", response_model=ScanResponse)
def run_scan(db: Session = Depends(get_db)) -> ScanResponse:
    return run_watch_scan(db)


@app.get("/api/v1/admin/health", response_model=AdminHealthOut)
def admin_health(db: Session = Depends(get_db)) -> AdminHealthOut:
    cc98_status = cc98_client.probe()
    workers = {
        row.name: {
            "status": row.status,
            "last_success_at": row.last_success_at,
            "last_failure_at": row.last_failure_at,
            "last_error": row.last_error,
            "metrics": json_loads(row.metrics_json, {}),
        }
        for row in db.query(models.WorkerStatus).all()
    }
    cursor = db.get(models.SystemCursor, "cc98_watch:last_topic_id")
    return AdminHealthOut(
        zju_connect={"status": "unknown", "note": "ZJU-Connect probing is deployment-specific"},
        cc98_service_account=cc98_status,
        workers=workers,
        cursor={"source": cursor.source, "value": cursor.cursor_value, "updated_at": cursor.updated_at} if cursor else None,
    )


@app.get("/api/research")
def legacy_research(query: str = Query(...)) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="历史搜索功能已砍掉；后端现在只保留订阅新帖提醒。")
