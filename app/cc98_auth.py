from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


@dataclass
class TokenState:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: datetime | None = None
    last_error: str | None = None
    last_login_at: datetime | None = None
    last_refresh_at: datetime | None = None


class CC98ServiceAuth:
    def __init__(self) -> None:
        self.openid_base = os.getenv("CC98_OPENID_BASE_URL", "https://openid.cc98.org").rstrip("/")
        self.client_id = os.getenv("CC98_CLIENT_ID", "9a1fd200-8687-44b1-4c20-08d50a96e5cd")
        self.client_secret = os.getenv("CC98_CLIENT_SECRET", "8b53f727-08e2-4509-8857-e34bf92b27f2")
        self.username = os.getenv("CC98_SERVICE_USERNAME", "")
        self.password = os.getenv("CC98_SERVICE_PASSWORD", "")
        self.timeout = float(os.getenv("CC98_TIMEOUT", "10"))
        self.state = TokenState(refresh_token=os.getenv("CC98_SERVICE_REFRESH_TOKEN", ""))
        self._lock = threading.Lock()

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._has_valid_access_token():
                return self.state.access_token
            if self.state.refresh_token:
                refreshed = self._refresh()
                if refreshed:
                    return self.state.access_token
            if self.username and self.password:
                logged_in = self._login_password()
                if logged_in:
                    return self.state.access_token
            raise RuntimeError(self.state.last_error or "CC98 service account is not configured")

    def auth_header(self) -> dict[str, str]:
        token = self.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def mark_auth_failed(self) -> None:
        self.state.expires_at = None

    def health(self) -> dict[str, Any]:
        return {
            "configured": bool(self.username and self.password) or bool(self.state.refresh_token),
            "has_access_token": bool(self.state.access_token),
            "has_refresh_token": bool(self.state.refresh_token),
            "expires_at": self.state.expires_at.isoformat() if self.state.expires_at else None,
            "last_error": self.state.last_error,
            "last_login_at": self.state.last_login_at.isoformat() if self.state.last_login_at else None,
            "last_refresh_at": self.state.last_refresh_at.isoformat() if self.state.last_refresh_at else None,
        }

    def _has_valid_access_token(self) -> bool:
        if not self.state.access_token or not self.state.expires_at:
            return False
        return self.state.expires_at > datetime.now(timezone.utc) + timedelta(seconds=60)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any] | None:
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            **data,
        }
        try:
            response = httpx.post(
                f"{self.openid_base}/connect/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            self.state.last_error = str(exc)
            return None
        if not body.get("access_token"):
            self.state.last_error = str(body.get("error_description") or body.get("error") or "token response missing access_token")
            return None
        return body

    def _apply_token(self, body: dict[str, Any], *, refreshed: bool) -> None:
        now = datetime.now(timezone.utc)
        self.state.access_token = str(body["access_token"])
        if body.get("refresh_token"):
            self.state.refresh_token = str(body["refresh_token"])
        expires_in = int(body.get("expires_in") or 3600)
        self.state.expires_at = now + timedelta(seconds=expires_in)
        self.state.last_error = None
        if refreshed:
            self.state.last_refresh_at = now
        else:
            self.state.last_login_at = now

    def _refresh(self) -> bool:
        body = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.state.refresh_token,
            }
        )
        if not body:
            return False
        self._apply_token(body, refreshed=True)
        return True

    def _login_password(self) -> bool:
        body = self._post_token(
            {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": "cc98-api openid offline_access",
            }
        )
        if not body:
            return False
        self._apply_token(body, refreshed=False)
        return True


service_auth = CC98ServiceAuth()

