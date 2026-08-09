from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from kb.secure_http import open_secure

from kb.config import CitadelConfig
from kb.retry import retry_after_seconds, run_with_retries

logger = logging.getLogger(__name__)

CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
CHAT_API_ROOT = "https://chat.googleapis.com/v1"


class GoogleChatConfigError(RuntimeError):
    pass


class GoogleChatDelivery:
    def __init__(
        self,
        *,
        space_name: str,
        service_account_json: str | None = None,
        service_account_file: str | None = None,
        thread_key: str = "citadel-org-digest",
        message_prefix: str = "citadel-org-digest",
        max_message_bytes: int = 30000,
        timeout_seconds: int = 20,
        retry_count: int = 2,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.space_name = space_name.strip().removeprefix("/")
        self.service_account_json = service_account_json
        self.service_account_file = service_account_file
        self.thread_key = thread_key
        self.message_prefix = message_prefix
        self.max_message_bytes = max(1000, max_message_bytes)
        self.timeout_seconds = max(1, timeout_seconds)
        self.retry_count = max(0, retry_count)
        self._token_provider = token_provider
        if not self.space_name.startswith("spaces/"):
            raise GoogleChatConfigError("CITADEL_GOOGLE_CHAT_SPACE_NAME must look like spaces/...")

    @classmethod
    def from_config(cls, config: CitadelConfig) -> "GoogleChatDelivery | None":
        if not config.google_chat_enabled:
            return None
        if not config.google_chat_space_name:
            return None
        return cls(
            space_name=config.google_chat_space_name,
            service_account_json=config.google_chat_service_account_json,
            service_account_file=config.google_chat_service_account_file,
            thread_key=config.google_chat_thread_key,
            message_prefix=config.google_chat_message_prefix,
            max_message_bytes=config.google_chat_max_message_bytes,
            timeout_seconds=config.google_chat_timeout_seconds,
            retry_count=config.google_chat_retry_count,
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "space_name": self.space_name,
            "thread_key": self.thread_key,
            "retry_count": self.retry_count,
        }

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"text": self._fit_message(text)}
        if self.thread_key:
            body["thread"] = {"threadKey": self.thread_key}
        query: dict[str, str] = {
            "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
        }
        if message_id:
            query["messageId"] = self._message_id(message_id)
        url = f"{CHAT_API_ROOT}/{self.space_name}/messages?{urlencode(query)}"

        result = run_with_retries(
            lambda: self._post_once(url, body),
            operation="google_chat.post_digest",
            max_attempts=self.retry_count + 1,
            should_retry_result=lambda outcome: (
                not outcome["ok"] and bool(outcome.get("retryable"))
            ),
            retry_after_from_result=lambda outcome: outcome.get("retry_after"),
        )
        if result["ok"]:
            logger.info(
                "Google Chat digest posted to %s (status %s)",
                self.space_name,
                result.get("status_code"),
            )
        else:
            logger.error(
                "Google Chat digest post failed: status_category=%s status_code=%s",
                result.get("status_category"),
                result.get("status_code"),
            )
        return {
            key: value for key, value in result.items() if key not in {"retryable", "retry_after"}
        }

    def _post_once(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        token = self._access_token()
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json",
                "User-Agent": "citadel-google-chat-digest",
            },
            method="POST",
        )
        try:
            with open_secure(request, timeout=self.timeout_seconds) as response:
                response_body = json.loads(response.read().decode("utf-8") or "{}")
                return {
                    "ok": 200 <= response.status < 300,
                    "sent": 200 <= response.status < 300,
                    "status_code": response.status,
                    "status_category": "success",
                    "message_name": response_body.get("name"),
                    "thread_name": (response_body.get("thread") or {}).get("name"),
                    "retryable": False,
                }
        except HTTPError as exc:
            if exc.code in {401, 403}:
                logger.warning(
                    "Google Chat rejected the request with HTTP %s (auth_error)", exc.code
                )
            return {
                "ok": False,
                "sent": False,
                "status_code": exc.code,
                "status_category": _status_category(exc.code),
                "retryable": exc.code in {429, 500, 502, 503, 504},
                "retry_after": retry_after_seconds(
                    exc.headers.get("Retry-After") if exc.headers is not None else None
                ),
            }
        except (URLError, TimeoutError):
            return {
                "ok": False,
                "sent": False,
                "status_category": "network_error",
                "retryable": True,
            }

    def _access_token(self) -> str:
        if self._token_provider:
            return self._token_provider()
        info = self._service_account_info()
        try:
            from google.auth.transport.requests import Request as AuthRequest
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - depends on runtime deps.
            raise GoogleChatConfigError(
                "google-auth and requests are required for Google Chat app auth."
            ) from exc
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=[CHAT_SCOPE],
        )
        credentials.refresh(AuthRequest())
        if not credentials.token:
            raise GoogleChatConfigError("Google service account did not return an access token.")
        return str(credentials.token)

    def _service_account_info(self) -> dict[str, Any]:
        if self.service_account_json:
            try:
                data = json.loads(self.service_account_json)
            except json.JSONDecodeError as exc:
                raise GoogleChatConfigError(
                    "CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not valid JSON."
                ) from exc
            if not isinstance(data, dict):
                raise GoogleChatConfigError("Google service account JSON must be an object.")
            return data
        if self.service_account_file:
            try:
                data = json.loads(Path(self.service_account_file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise GoogleChatConfigError(
                    "Could not read CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_FILE."
                ) from exc
            if not isinstance(data, dict):
                raise GoogleChatConfigError("Google service account file must contain an object.")
            return data
        raise GoogleChatConfigError(
            "Set CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON or "
            "CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_FILE."
        )

    def _fit_message(self, text: str) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= self.max_message_bytes:
            return text
        suffix = "\n\n[truncated]"
        budget = self.max_message_bytes - len(suffix.encode("utf-8"))
        clipped = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
        return f"{clipped}{suffix}"

    def _message_id(self, value: str) -> str:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
        safe = safe.strip("-_").lower()[:56] or "digest"
        if safe.startswith("client-"):
            return safe
        return f"client-{self.message_prefix}-{safe}"[:63]


def _status_category(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 404:
        return "not_found"
    return "client_error"
