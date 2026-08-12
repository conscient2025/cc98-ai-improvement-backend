from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from .cc98_auth import service_auth


class CC98APIError(RuntimeError):
    def __init__(self, method: str, url: str, status_code: int, body: str):
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {body[:200]}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body


class CC98ServiceClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("CC98_API_BASE_URL", "https://api.cc98.org").rstrip("/") + "/"
        self.timeout = float(os.getenv("CC98_TIMEOUT", "10"))
        self.search_min_interval = float(os.getenv("CC98_SEARCH_MIN_INTERVAL_SECONDS", "1.2"))
        self.search_retry_attempts = int(os.getenv("CC98_SEARCH_RETRY_ATTEMPTS", "2"))
        self._search_lock = threading.Lock()
        self._last_search_at = 0.0
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None

    def probe(self) -> dict[str, Any]:
        try:
            response = httpx.get(self._url("/me"), timeout=self.timeout)
            reachable = response.status_code in {200, 401, 403}
            if reachable:
                self.last_success_at = datetime.utcnow()
                self.last_error = None
            return {
                "reachable": reachable,
                "status_code": response.status_code,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "last_error": self.last_error,
            }
        except Exception as exc:
            self.last_error = str(exc)
            return {
                "reachable": False,
                "status_code": None,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "last_error": self.last_error,
            }

    def search_posts(self, query: str, *, board_id: str | None = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        endpoint = "/topic/search"
        if board_id:
            endpoint = f"/topic/search/board/{int(board_id)}"
        topics: list[dict[str, Any]] = []
        current_offset = offset
        while len(topics) < limit:
            size = min(20, limit - len(topics))
            payload = self._get_search_json(
                endpoint,
                params={"keyword": query.strip(), "from": current_offset, "size": size},
            )
            if not isinstance(payload, list) or not payload:
                break
            topics.extend(normalize_topic(item) for item in payload if isinstance(item, dict))
            if len(payload) < size:
                break
            current_offset += len(payload)
        return topics[:limit]

    def get_board_posts(self, board_id: str, *, page: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        offset = (page - 1) * limit
        payload = self._get_json(f"/board/{int(board_id)}/topic", params={"from": offset, "size": limit})
        if not isinstance(payload, list):
            return []
        return [normalize_topic(item) for item in payload if isinstance(item, dict)]

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None, retry_auth: bool = True) -> Any:
        headers = service_auth.auth_header()
        response = httpx.get(self._url(path), params=params, headers=headers, timeout=self.timeout)
        if response.status_code in {401, 403} and retry_auth:
            service_auth.mark_auth_failed()
            headers = service_auth.auth_header()
            response = httpx.get(self._url(path), params=params, headers=headers, timeout=self.timeout)
        if response.status_code < 200 or response.status_code >= 300:
            self.last_error = response.text
            raise CC98APIError("GET", str(response.url), response.status_code, response.text)
        self.last_success_at = datetime.utcnow()
        self.last_error = None
        if not response.content:
            return None
        return response.json()

    def _get_search_json(self, path: str, *, params: dict[str, Any]) -> Any:
        for attempt in range(self.search_retry_attempts + 1):
            self._wait_search_slot()
            try:
                return self._get_json(path, params=params)
            except CC98APIError as exc:
                rate_limited = exc.status_code == 403 and "last_search_in_1_seconds" in exc.body
                if not rate_limited or attempt >= self.search_retry_attempts:
                    raise
                time.sleep(max(self.search_min_interval, 1.0))
        return []

    def _wait_search_slot(self) -> None:
        if self.search_min_interval <= 0:
            return
        with self._search_lock:
            elapsed = time.monotonic() - self._last_search_at
            delay = self.search_min_interval - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last_search_at = time.monotonic()

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))


def normalize_topic(item: dict[str, Any]) -> dict[str, Any]:
    topic_id = item.get("id") or item.get("topicId")
    board_id = item.get("boardId") or item.get("board_id")
    return {
        "topic_id": str(topic_id),
        "title": str(item.get("title") or f"CC98 topic {topic_id}"),
        "url": item.get("url") or f"https://www.cc98.org/topic/{topic_id}",
        "board_id": str(board_id) if board_id is not None else None,
        "author_id": str(item.get("userId")) if item.get("userId") is not None else None,
        "author_name": item.get("userName") or item.get("authorName") or item.get("lastPostUser"),
        "created_at": item.get("time") or item.get("createTime"),
        "raw": item,
    }


cc98_client = CC98ServiceClient()

