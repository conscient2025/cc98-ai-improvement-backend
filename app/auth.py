from __future__ import annotations

import base64
import json
import os
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import EmailVerificationCode, User
from .notifiers import send_email
from .utils import as_utc, expires_in_minutes, hash_secret, make_code, new_id, utc_now


def _dev_print_code_enabled() -> bool:
    return os.getenv("AUTH_DEV_PRINT_CODE", "true").lower() in {"1", "true", "yes", "on"}


def _allowed_domains() -> set[str]:
    raw = os.getenv("ZJU_EMAIL_DOMAINS", "zju.edu.cn,intl.zju.edu.cn")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _email_delivery_enabled() -> bool:
    return os.getenv("AUTH_EMAIL_DELIVERY", "true").lower() in {"1", "true", "yes", "on"}


def _verification_email_body(code: str, expire_minutes: int) -> str:
    return (
        "你好，\n\n"
        f"你的 CC98 AI 订阅提醒登录验证码是：{code}\n"
        f"验证码 {expire_minutes} 分钟内有效，请不要转发给其他人。\n\n"
        "如果不是你本人操作，可以忽略这封邮件。"
    )


def validate_zju_email(email: str) -> str:
    normalized = email.strip().lower()
    if "@" not in normalized:
        raise HTTPException(status_code=400, detail="Invalid email")
    domain = normalized.rsplit("@", 1)[1]
    if domain not in _allowed_domains():
        raise HTTPException(status_code=400, detail="Only ZJU email domains are allowed")
    return normalized


def request_email_code(db: Session, email: str) -> tuple[str, str | None]:
    email = validate_zju_email(email)
    code = make_code()
    expire_minutes = int(os.getenv("EMAIL_CODE_EXPIRE_MINUTES", "10"))
    dev_code_enabled = _dev_print_code_enabled()
    email_delivery_enabled = _email_delivery_enabled()
    if not dev_code_enabled and not email_delivery_enabled:
        raise HTTPException(status_code=503, detail="验证码邮件发送未启用")
    record = EmailVerificationCode(
        email=email,
        code_hash=hash_secret(code),
        expires_at=expires_in_minutes(expire_minutes),
    )
    db.add(record)
    db.commit()

    if email_delivery_enabled:
        result = send_email(
            to_addr=email,
            subject="CC98 AI 订阅提醒登录验证码",
            body=_verification_email_body(code, expire_minutes),
        )
        if not result.ok:
            if not dev_code_enabled:
                raise HTTPException(status_code=503, detail=f"验证码邮件发送失败：{result.error or 'SMTP unavailable'}")
            print(f"[CC98 AI] verification email failed for {email}: {result.error}")

    if dev_code_enabled:
        print(f"[CC98 AI] verification code for {email}: {code}")
    dev_code = code if dev_code_enabled else None
    return email, dev_code


def verify_email_code(db: Session, email: str, code: str) -> tuple[User, str]:
    email = validate_zju_email(email)
    record = (
        db.query(EmailVerificationCode)
        .filter(
            EmailVerificationCode.email == email,
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .first()
    )
    now = utc_now()
    if record is None or as_utc(record.expires_at) < now:
        raise HTTPException(status_code=400, detail="Verification code expired or missing")

    record.attempts += 1
    if record.attempts > 5:
        db.commit()
        raise HTTPException(status_code=429, detail="Too many verification attempts")
    if record.code_hash != hash_secret(code.strip()):
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid verification code")

    record.consumed_at = now
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(id=new_id("usr_"), email=email, email_verified_at=now)
        db.add(user)
    else:
        user.email_verified_at = user.email_verified_at or now
        user.status = "active"
    db.commit()
    db.refresh(user)
    return user, issue_token(user)


def issue_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "email": user.email,
        "exp": (utc_now() + timedelta(hours=int(os.getenv("SESSION_EXPIRE_HOURS", "168")))).timestamp(),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hash_secret(body)[:32]
    return f"{body}.{sig}"


def token_payload(token: str) -> dict[str, Any]:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if hash_secret(body)[:32] != sig:
        raise HTTPException(status_code=401, detail="Invalid token")
    padded = body + "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    if float(payload.get("exp", 0)) < utc_now().timestamp():
        raise HTTPException(status_code=401, detail="Token expired")
    return payload
