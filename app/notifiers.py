from __future__ import annotations

import base64
import hashlib
import hmac
import os
import smtplib
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx


@dataclass
class SendResult:
    ok: bool
    status: str
    error: str | None = None


NotificationItem = dict[str, str | None]


def redact_config(provider: str, config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    redacted = dict(config or {})
    has_secret = False
    for key in ("secret", "token", "password", "smtp_password"):
        if redacted.get(key):
            has_secret = True
            redacted[key] = "***"
    if provider == "dingtalk" and redacted.get("webhook"):
        webhook = str(redacted["webhook"])
        redacted["webhook"] = webhook[:32] + "..." if len(webhook) > 35 else webhook
    return redacted, has_secret


def build_notification_text(title: str, url: str, reason: str | None = None) -> str:
    return build_batch_notification_text([{"title": title, "url": url, "reason": reason}])


def build_batch_notification_text(items: list[NotificationItem]) -> str:
    if not items:
        return "CC98 订阅提醒\n\n本次没有新的匹配帖子。"
    lines = [f"CC98 订阅提醒：本次找到 {len(items)} 个匹配帖子", ""]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.get('title') or '未命名帖子'}")
        lines.append(f"链接：{item.get('url') or ''}")
        if item.get("reason"):
            lines.append(f"匹配原因：{item.get('reason')}")
        lines.append("")
    return "\n".join(lines).strip()


def _signed_dingtalk_url(webhook: str, secret: str | None) -> str:
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    encoded = urllib.parse.quote_plus(base64.b64encode(sign))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={timestamp}&sign={encoded}"


def send_dingtalk(config: dict[str, Any], text: str) -> SendResult:
    webhook = str(config.get("webhook") or "").strip()
    if not webhook:
        return SendResult(ok=False, status="failed", error="DingTalk webhook is empty")
    url = _signed_dingtalk_url(webhook, config.get("secret"))
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        timeout = float(config.get("timeout") or os.getenv("DINGTALK_TIMEOUT", "10"))
        response = httpx.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return SendResult(ok=False, status="failed", error=str(exc))

    if data.get("errcode") in (0, None):
        return SendResult(ok=True, status="sent")
    return SendResult(ok=False, status="failed", error=str(data))


def _smtp_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    return {
        "host": config.get("smtp_host") or os.getenv("SMTP_HOST", ""),
        "port": int(config.get("smtp_port") or os.getenv("SMTP_PORT", "465")),
        "username": config.get("smtp_username") or os.getenv("SMTP_USERNAME", ""),
        "password": config.get("smtp_password") or os.getenv("SMTP_PASSWORD", ""),
        "from_addr": config.get("from") or os.getenv("SMTP_FROM") or config.get("smtp_username") or os.getenv("SMTP_USERNAME", ""),
        "use_ssl": str(config.get("smtp_use_ssl") or os.getenv("SMTP_USE_SSL", "true")).lower() in {"1", "true", "yes", "on"},
        "timeout": float(config.get("smtp_timeout") or os.getenv("SMTP_TIMEOUT", "10")),
    }


def _send_email(to_addr: str, subject: str, body: str, config: dict[str, Any] | None = None) -> SendResult:
    smtp = _smtp_config(config)
    if not to_addr:
        return SendResult(ok=False, status="failed", error="Email recipient is empty")
    if not smtp["host"] or not smtp["username"] or not smtp["password"] or not smtp["from_addr"]:
        return SendResult(ok=False, status="failed", error="SMTP config is incomplete")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp["from_addr"]
    message["To"] = to_addr
    message.set_content(body)

    try:
        if smtp["use_ssl"]:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=smtp["timeout"], context=context) as server:
                server.login(smtp["username"], smtp["password"])
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=smtp["timeout"]) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(smtp["username"], smtp["password"])
                server.send_message(message)
    except Exception as exc:  # noqa: BLE001
        return SendResult(ok=False, status="failed", error=str(exc))
    return SendResult(ok=True, status="sent")


def send_email(to_addr: str, subject: str, body: str, config: dict[str, Any] | None = None) -> SendResult:
    return _send_email(to_addr=to_addr, subject=subject, body=body, config=config)


def send_email_notification(config: dict[str, Any], text: str, count: int = 1) -> SendResult:
    to_addr = str(config.get("to") or config.get("email") or config.get("recipient") or "").strip()
    subject_prefix = str(config.get("subject_prefix") or "CC98 订阅提醒")
    subject = f"{subject_prefix}：{count} 个新匹配帖子"
    return _send_email(to_addr=to_addr, subject=subject, body=text, config=config)


def send_notification(provider: str, config: dict[str, Any], title: str, url: str, reason: str | None = None) -> SendResult:
    return send_batch_notification(provider, config, [{"title": title, "url": url, "reason": reason}])


def send_batch_notification(provider: str, config: dict[str, Any], items: list[NotificationItem]) -> SendResult:
    text = build_batch_notification_text(items)
    if provider == "dingtalk":
        return send_dingtalk(config, text)
    if provider == "email":
        return send_email_notification(config, text, count=len(items))
    return SendResult(ok=False, status="failed", error=f"Unsupported notification provider: {provider}")
