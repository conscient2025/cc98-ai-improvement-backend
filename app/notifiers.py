from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class SendResult:
    ok: bool
    status: str
    error: str | None = None


def redact_config(provider: str, config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    redacted = dict(config or {})
    has_secret = False
    for key in ("secret", "token", "password"):
        if redacted.get(key):
            has_secret = True
            redacted[key] = "***"
    if provider == "dingtalk" and redacted.get("webhook"):
        webhook = str(redacted["webhook"])
        redacted["webhook"] = webhook[:32] + "..." if len(webhook) > 35 else webhook
    return redacted, has_secret


def build_notification_text(title: str, url: str, reason: str | None = None) -> str:
    lines = [
        "CC98 订阅提醒",
        "",
        f"标题：{title}",
        f"链接：{url}",
    ]
    if reason:
        lines.extend(["", f"匹配原因：{reason}"])
    return "\n".join(lines)


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
        timeout = float(config.get("timeout") or 10)
        response = httpx.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return SendResult(ok=False, status="failed", error=str(exc))

    if data.get("errcode") in (0, None):
        return SendResult(ok=True, status="sent")
    return SendResult(ok=False, status="failed", error=str(data))


def send_notification(provider: str, config: dict[str, Any], title: str, url: str, reason: str | None = None) -> SendResult:
    text = build_notification_text(title=title, url=url, reason=reason)
    if provider == "dingtalk":
        return send_dingtalk(config, text)
    return SendResult(ok=False, status="failed", error=f"Unsupported notification provider: {provider}")
