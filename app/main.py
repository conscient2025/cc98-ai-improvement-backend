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
from .matcher import ExpressionSyntaxError, normalize_search_expression
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
    NotificationChannelStatusUpdate,
    NotificationChannelTest,
    NotificationOut,
    NotificationProvider,
    ScanResponse,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
    UserOut,
)
from .tasks import start_scheduler, stop_scheduler
from .utils import as_utc, json_dumps, json_loads, utc_now
from .watch import run_watch_scan


load_dotenv()


def _configure_app_logging() -> None:
    level = getattr(logging, os.getenv("APP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)
    app_logger.propagate = False


_configure_app_logging()
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
        expression=subscription.expression,
        status=subscription.status,
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
    return NotificationChannelOut(
        id=channel.id,
        provider=channel.provider,
        enabled=channel.enabled,
        config=config,
        has_secret=has_secret,
        notify_interval_minutes=interval,
        last_attempted_at=channel.last_attempted_at,
        last_sent_at=channel.last_sent_at,
        last_dispatch_status=channel.last_dispatch_status,
        last_dispatch_error=channel.last_dispatch_error,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _notification_out(notification: models.Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        topic_id=notification.topic_id,
        topic_title=notification.topic_title,
        topic_url=notification.topic_url,
        matched_reason=notification.matched_reason,
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


def _validated_subscription_expression(value: str) -> str:
    try:
        return normalize_search_expression(value)
    except ExpressionSyntaxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ensure_no_duplicate_subscription(
    db: Session,
    current_user: models.User,
    expression: str,
    *,
    exclude_id: int | None = None,
) -> None:
    query = db.query(models.Subscription).filter(
        models.Subscription.user_id == current_user.id,
        models.Subscription.expression == expression,
    )
    if exclude_id is not None:
        query = query.filter(models.Subscription.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(status_code=400, detail="已经存在相同的订阅，请不要重复创建")


def _get_or_create_channel(db: Session, payload: NotificationChannelSave, user_id: str) -> models.NotificationChannel:
    target_user_id = user_id
    requested_interval = payload.notify_interval_minutes
    preference = db.get(models.NotificationPreference, target_user_id)
    if requested_interval is not None or preference is None:
        _save_notify_interval(db, target_user_id, requested_interval)
    channel = (
        db.query(models.NotificationChannel)
        .filter(models.NotificationChannel.user_id == target_user_id, models.NotificationChannel.provider == payload.provider)
        .first()
    )
    if channel is None:
        if payload.config is None:
            raise HTTPException(status_code=400, detail="首次保存通知渠道时必须提供配置")
        config = dict(payload.config)
        channel = models.NotificationChannel(
            user_id=target_user_id,
            provider=payload.provider,
            config_json=json_dumps(config),
            enabled=payload.enabled,
        )
        db.add(channel)
    else:
        old_config = json_loads(channel.config_json, {})
        if payload.config is not None:
            new_config = dict(old_config)
            for key, value in payload.config.items():
                if key in {"webhook", "secret", "token", "password", "smtp_password"} and value == "***":
                    continue
                new_config[key] = value
            channel.config_json = json_dumps(new_config)
        channel.enabled = payload.enabled
        channel.updated_at = utc_now()
    db.commit()
    db.refresh(channel)
    return channel


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        components={
            "database": "ok",
            "scheduler_enabled": os.getenv("ENABLE_SCHEDULER", "false").lower() in {"1", "true", "yes", "on"},
            "scan_interval_minutes": scan_interval_minutes(),
            "subscription_limit": int(os.getenv("SUBSCRIPTION_LIMIT", "10")),
            "subscription_expression_max_length": 255,
            "notification_read_rate_limit_seconds": max(
                0, int(os.getenv("NOTIFICATION_READ_RATE_LIMIT_SECONDS", "60"))
            ),
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
def create_subscription(
    payload: SubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> SubscriptionOut:
    expression = _validated_subscription_expression(payload.expression)
    _ensure_no_duplicate_subscription(db, current_user, expression)
    subscription_count = db.query(models.Subscription).filter(models.Subscription.user_id == current_user.id).count()
    limit = int(os.getenv("SUBSCRIPTION_LIMIT", "10"))
    if subscription_count >= limit:
        raise HTTPException(status_code=400, detail=f"最多只能创建 {limit} 个订阅，暂停的订阅也会计入数量")
    subscription = models.Subscription(
        user_id=current_user.id,
        expression=expression,
        status="enabled",
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return _subscription_out(subscription)


@app.get("/api/v1/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[SubscriptionOut]:
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
    if payload.expression is not None:
        subscription.expression = _validated_subscription_expression(payload.expression)
    if payload.status is not None:
        subscription.status = payload.status
    _ensure_no_duplicate_subscription(db, current_user, subscription.expression, exclude_id=subscription.id)
    subscription.updated_at = utc_now()
    db.commit()
    db.refresh(subscription)
    return _subscription_out(subscription)


@app.delete("/api/v1/subscriptions/{subscription_id}")
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
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[NotificationChannelOut]:
    rows = db.query(models.NotificationChannel).filter(models.NotificationChannel.user_id == current_user.id).all()
    return [_channel_out(db, row) for row in rows]


@app.put("/api/v1/notification-channels", response_model=NotificationChannelOut)
def save_channel(
    payload: NotificationChannelSave,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> NotificationChannelOut:
    return _channel_out(db, _get_or_create_channel(db, payload, current_user.id))


@app.patch("/api/v1/notification-channels/{provider}", response_model=NotificationChannelOut)
def update_channel_status(
    provider: NotificationProvider,
    payload: NotificationChannelStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> NotificationChannelOut:
    channel = (
        db.query(models.NotificationChannel)
        .filter(
            models.NotificationChannel.user_id == current_user.id,
            models.NotificationChannel.provider == provider,
        )
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="通知渠道不存在，请先保存配置")
    channel.enabled = payload.enabled
    channel.updated_at = utc_now()
    db.commit()
    db.refresh(channel)
    return _channel_out(db, channel)


@app.post("/api/v1/notification-channels/test")
def test_channel(
    payload: NotificationChannelTest,
    current_user: models.User = Depends(_require_current_user),
) -> dict[str, Any]:
    _ = current_user
    result = send_notification(payload.provider, payload.config, "CC98 AI Watch 测试通知", "https://www.cc98.org", "如果你收到这条消息，说明通知通道可用")
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error or "通知发送失败")
    return {"status": "ok"}


def _record_notification_list_success(db: Session, user_id: str) -> None:
    limit_seconds = max(0, int(os.getenv("NOTIFICATION_READ_RATE_LIMIT_SECONDS", "60")))
    if limit_seconds == 0:
        return
    now = utc_now()
    state = db.get(models.NotificationListRateLimitState, user_id)
    if state is None:
        try:
            with db.begin_nested():
                state = models.NotificationListRateLimitState(user_id=user_id, last_success_at=now)
                db.add(state)
                db.flush()
            db.commit()
            return
        except IntegrityError:
            state = db.get(models.NotificationListRateLimitState, user_id)

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
def list_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_current_user),
) -> list[NotificationOut]:
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
