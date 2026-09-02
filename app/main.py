from __future__ import annotations

import hmac
import logging
import math
import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from . import models
from .auth import request_email_code, token_payload, verify_email_code
from .cc98_client import cc98_client
from .database import get_db, init_db
from .env import load_dotenv
from .matcher import has_valid_search_expression
from .notification_frequency import effective_notify_interval_minutes, scan_interval_minutes, user_notify_interval_minutes
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
from .utils import as_utc, json_dumps, json_loads, normalize_topic_text, utc_now
from .watch import run_watch_scan


load_dotenv()
init_db()
logger = logging.getLogger(__name__)


def _public_docs_enabled() -> bool:
    if os.getenv("ENABLE_PUBLIC_DOCS", "").lower() in {"1", "true", "yes", "on"}:
        return True
    return os.getenv("APP_ENV", "development").lower() not in {"prod", "production"}


docs_enabled = _public_docs_enabled()
app = FastAPI(
    title="CC98 AI Improvement Backend",
    version="0.1.0",
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)
bearer_scheme = HTTPBearer(auto_error=False)

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


def _save_notify_interval(db: Session, user_id: str, requested: int | None) -> None:
    interval = effective_notify_interval_minutes({}, requested)
    preference = db.get(models.NotificationPreference, user_id)
    if preference is None:
        preference = models.NotificationPreference(user_id=user_id, notify_interval_minutes=interval)
        db.add(preference)
    else:
        preference.notify_interval_minutes = interval
        preference.updated_at = utc_now()


def _channel_out(db: Session, channel: models.NotificationChannel) -> NotificationChannelOut:
    raw_config = json_loads(channel.config_json, {})
    config, has_secret = redact_config(channel.provider, raw_config)
    interval = user_notify_interval_minutes(db, channel.user_id)
    # Keep the old nested value in responses while the frontend moves to the
    # top-level user preference. It is no longer stored per channel.
    config["notify_interval_minutes"] = interval
    return NotificationChannelOut(
        id=channel.id,
        user_id=channel.user_id,
        provider=channel.provider,
        enabled=channel.enabled,
        config=config,
        has_secret=has_secret,
        notify_interval_minutes=interval,
        last_test_at=channel.last_test_at,
        last_attempted_at=channel.last_attempted_at,
        last_sent_at=channel.last_sent_at,
        last_test_status=channel.last_test_status,
        last_error=channel.last_error,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _notification_out(notification: models.Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        user_id=notification.user_id,
        subscription_id=None,
        topic_id=notification.topic_id,
        topic_title=notification.topic_title,
        topic_url=notification.topic_url,
        topic=notification.topic_title,
        matched_reason=notification.matched_reason,
        summary=notification.matched_reason,
        dispatch_pending=notification.dispatch_pending,
        dispatch_processed_at=notification.dispatch_processed_at,
        delivery_channel=None,
        delivery_status="pending" if notification.dispatch_pending else "processed",
        sent_at=notification.dispatch_processed_at,
        is_read=notification.is_read,
        created_at=notification.created_at,
    )


def _require_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    payload = token_payload(credentials.credentials)
    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.get(models.User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=401, detail="User is not active")
    return user


def _require_admin_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    supplied = (x_admin_token or "").strip()
    if not supplied and credentials is not None:
        supplied = credentials.credentials
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _ensure_subscription_owner(subscription: models.Subscription, current_user: models.User) -> None:
    if subscription.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="不能操作其他用户的订阅")


def _ensure_valid_subscription_expression(name: str, description: str) -> None:
    if not has_valid_search_expression({"name": name, "description": description}):
        raise HTTPException(status_code=400, detail="订阅关键词至少需要包含一个 2 个字以上的有效词，例如：电脑、电影、微积分/微甲/vjf")


def _ensure_no_duplicate_subscription(
    db: Session,
    current_user: models.User,
    name: str,
    description: str,
    board_id: str | None,
    *,
    exclude_id: int | None = None,
) -> None:
    query = db.query(models.Subscription).filter(
        models.Subscription.user_id == current_user.id,
        models.Subscription.name == name,
        models.Subscription.description == description,
        models.Subscription.board_id == board_id,
    )
    if exclude_id is not None:
        query = query.filter(models.Subscription.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(status_code=400, detail="已经存在相同的订阅，请不要重复创建")


def _legacy_notification_settings_out(db: Session, user_id: str) -> NotificationSettingOut:
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == user_id, models.NotificationChannel.provider == "dingtalk")
        .first()
    )
    if channel is None:
        return NotificationSettingOut(user_id=user_id, dingtalk_enabled=False, notify_interval_minutes=user_notify_interval_minutes(db, user_id))
    config = json_loads(channel.config_json, {})
    return NotificationSettingOut(
        user_id=user_id,
        dingtalk_enabled=channel.enabled,
        dingtalk_webhook=config.get("webhook"),
        has_dingtalk_secret=bool(config.get("secret")),
        notify_interval_minutes=user_notify_interval_minutes(db, user_id),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _get_or_create_channel(db: Session, payload: NotificationChannelSave, user_id: str | None = None) -> models.NotificationChannel:
    target_user_id = user_id or payload.user_id
    requested_interval = payload.notify_interval_minutes
    if requested_interval is None and payload.config.get("notify_interval_minutes") is not None:
        requested_interval = int(payload.config["notify_interval_minutes"])
    preference = db.get(models.NotificationPreference, target_user_id)
    if requested_interval is not None or preference is None:
        _save_notify_interval(db, target_user_id, requested_interval)
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == target_user_id, models.NotificationChannel.provider == payload.provider)
        .first()
    )
    if channel is None:
        config = dict(payload.config)
        config.pop("notify_interval_minutes", None)
        channel = models.NotificationChannel(
            user_id=target_user_id,
            provider=payload.provider,
            config_json=json_dumps(config),
            enabled=payload.enabled,
        )
        db.add(channel)
    else:
        old_config = json_loads(channel.config_json, {})
        new_config = dict(payload.config)
        for secret_key in ("secret", "token", "password", "smtp_password"):
            if new_config.get(secret_key) == "***" and old_config.get(secret_key):
                new_config[secret_key] = old_config[secret_key]
        new_config.pop("notify_interval_minutes", None)
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
            "scan_interval_minutes": scan_interval_minutes(),
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
def create_subscription(
    payload: SubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> SubscriptionOut:
    name = normalize_topic_text(payload.name or payload.topic or "")
    description = normalize_topic_text(payload.description or payload.topic or payload.name or "")
    if not name:
        raise HTTPException(status_code=400, detail="订阅名称不能为空")
    _ensure_valid_subscription_expression(name, description)
    _ensure_no_duplicate_subscription(db, current_user, name, description, payload.board_id)
    active_count = (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == current_user.id, models.Subscription.status == "enabled")
        .count()
    )
    limit = int(os.getenv("SUBSCRIPTION_LIMIT", "10"))
    if active_count >= limit:
        raise HTTPException(status_code=400, detail=f"最多只能启用 {limit} 个订阅")
    subscription = models.Subscription(
        user_id=current_user.id,
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
def list_subscriptions(
    user_id: str = Query("demo_user"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[SubscriptionOut]:
    _ = user_id
    rows = db.query(models.Subscription).filter(models.Subscription.user_id == current_user.id).order_by(models.Subscription.id.desc()).all()
    return [_subscription_out(row) for row in rows]


@app.patch("/api/v1/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> SubscriptionOut:
    subscription = db.get(models.Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    _ensure_subscription_owner(subscription, current_user)
    next_name = subscription.name
    next_description = subscription.description
    if payload.name is not None:
        next_name = normalize_topic_text(payload.name)
    if payload.description is not None:
        next_description = normalize_topic_text(payload.description)
    _ensure_valid_subscription_expression(next_name, next_description)
    subscription.name = next_name
    subscription.description = next_description
    if payload.board_id is not None:
        subscription.board_id = payload.board_id
    if payload.status is not None:
        subscription.status = payload.status
    _ensure_no_duplicate_subscription(db, current_user, subscription.name, subscription.description, subscription.board_id, exclude_id=subscription.id)
    subscription.updated_at = utc_now()
    db.commit()
    db.refresh(subscription)
    return _subscription_out(subscription)


@app.delete("/api/v1/subscriptions/{subscription_id}")
@app.delete("/api/subscribe/{subscription_id}")
def delete_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> dict[str, Any]:
    subscription = db.get(models.Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    _ensure_subscription_owner(subscription, current_user)
    db.delete(subscription)
    db.commit()
    return {"status": "ok", "deleted": subscription_id}


@app.get("/api/v1/notification-channels", response_model=list[NotificationChannelOut])
def list_channels(
    user_id: str = Query("demo_user"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[NotificationChannelOut]:
    _ = user_id
    rows = db.query(models.NotificationChannel).filter(models.NotificationChannel.user_id == current_user.id).all()
    return [_channel_out(db, row) for row in rows]


@app.put("/api/v1/notification-channels", response_model=NotificationChannelOut)
def save_channel(
    payload: NotificationChannelSave,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> NotificationChannelOut:
    return _channel_out(db, _get_or_create_channel(db, payload, current_user.id))


@app.post("/api/v1/notification-channels/test")
def test_channel(
    payload: NotificationChannelSave,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> dict[str, Any]:
    result = send_notification(payload.provider, payload.config, "CC98 AI Watch 测试通知", "https://www.cc98.org", "如果你收到这条消息，说明通知通道可用")
    channel = _get_or_create_channel(db, payload, current_user.id)
    channel.last_test_at = utc_now()
    channel.last_test_status = result.status
    channel.last_error = result.error
    db.commit()
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error or "通知发送失败")
    return {"status": "ok"}


@app.get("/api/notification-settings", response_model=NotificationSettingOut)
def get_legacy_notification_settings(
    user_id: str = Query("demo_user"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> NotificationSettingOut:
    _ = user_id
    return _legacy_notification_settings_out(db, current_user.id)


@app.put("/api/notification-settings", response_model=NotificationSettingOut)
def save_legacy_notification_settings(
    payload: NotificationSettingSave,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> NotificationSettingOut:
    channel_payload = NotificationChannelSave(
        user_id=current_user.id,
        provider="dingtalk",
        enabled=payload.dingtalk_enabled,
        config={"webhook": payload.dingtalk_webhook or "", "secret": payload.dingtalk_secret or ""},
        notify_interval_minutes=payload.notify_interval_minutes,
    )
    _get_or_create_channel(db, channel_payload, current_user.id)
    return _legacy_notification_settings_out(db, current_user.id)


@app.post("/api/notification-settings/test")
def test_legacy_notification_settings(
    payload: NotificationSettingTest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> dict[str, Any]:
    _ = payload.user_id
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == current_user.id, models.NotificationChannel.provider == "dingtalk")
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


def _record_notification_list_success(db: Session, user_id: str) -> None:
    limit_seconds = max(0, int(os.getenv("NOTIFICATION_READ_RATE_LIMIT_SECONDS", "60")))
    if limit_seconds == 0:
        return
    now = utc_now()
    state = db.get(models.NotificationReadState, user_id)
    if state is None:
        try:
            with db.begin_nested():
                state = models.NotificationReadState(user_id=user_id, last_success_at=now)
                db.add(state)
                db.flush()
            db.commit()
            return
        except IntegrityError:
            state = db.get(models.NotificationReadState, user_id)

    last_success_at = as_utc(state.last_success_at) if state is not None else None
    if last_success_at is not None:
        elapsed = max(0.0, (now - last_success_at).total_seconds())
        if elapsed < limit_seconds:
            retry_after = max(1, math.ceil(limit_seconds - elapsed))
            logger.info("notification list rate limited user_id=%s retry_after=%d", user_id, retry_after)
            raise HTTPException(
                status_code=429,
                detail=f"通知列表每 {limit_seconds} 秒最多刷新一次",
                headers={"Retry-After": str(retry_after)},
            )

    if state is None:
        raise RuntimeError("notification read rate-limit state could not be created")
    state.last_success_at = now
    db.commit()


@app.get("/api/v1/notifications", response_model=list[NotificationOut])
@app.get("/api/notifications", response_model=list[NotificationOut])
def list_notifications(
    user_id: str = Query("demo_user"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[NotificationOut]:
    _ = user_id
    _record_notification_list_success(db, current_user.id)
    rows = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == current_user.id)
        .order_by(models.Notification.id.desc())
        .limit(100)
        .all()
    )
    logger.info("notification list read user_id=%s count=%d", current_user.id, len(rows))
    return [_notification_out(row) for row in rows]


@app.post("/api/v1/tasks/scan", response_model=ScanResponse)
@app.post("/api/tasks/scan", response_model=ScanResponse)
def run_scan(_admin: None = Depends(_require_admin_token), db: Session = Depends(get_db)) -> ScanResponse:
    return run_watch_scan(db)


@app.get("/api/v1/admin/health", response_model=AdminHealthOut)
def admin_health(_admin: None = Depends(_require_admin_token), db: Session = Depends(get_db)) -> AdminHealthOut:
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
