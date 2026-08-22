"""Compatibility guard for terminal provider errors in Cognee retries."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Mapping


_TERMINAL_PROVIDER_STATUS = re.compile(
    r"(?:[\"']?(?:status_code|http_status|status|code)[\"']?)"
    r"\s*[:=]\s*[\"']?(401|403|404)\b",
    re.IGNORECASE,
)
_TERMINAL_PROVIDER_TEXT = re.compile(
    r"\b(?:HTTP(?:\s+status)?|status(?:_code)?|status\s+code|error\s+code|code)"
    r"\s*[:=]?\s*(401|403|404)\b"
    r"|\b(401|403|404)\s+(?:unauthorized|forbidden|not\s+found)\b"
    r"|\binvalid\s+url\b"
    r"|\bfree-models-per-day-high-balance\b"
    r"|\bopenrouter_free_tier_daily\b",
    re.IGNORECASE,
)
_FREE_QUOTA_MARKERS = (
    "free-model daily quota",
    "free model daily quota",
    "free-models-per-day-high-balance",
    "free-models-per-hour",
    "free-models-per-min",
    "openrouter_free_tier_daily",
)
_FREE_QUOTA_RESET = re.compile(
    r"x-ratelimit-reset[\"']?\s*[:=]\s*[\"']?(\d{10,13})",
    re.IGNORECASE,
)
_FREE_ROUTE_MARKERS = (":free", "openrouter/free")
_free_quota_circuit_open = False
_free_quota_circuit_reset_at: float | None = None
_free_route_call_lock = asyncio.Lock()
_free_route_circuit_installed = False


def _status_values(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(
            value.get(key) for key in ("status_code", "http_status", "status", "code")
        )
    return tuple(
        getattr(value, key, None) for key in ("status_code", "http_status", "status", "code")
    )


def _has_terminal_provider_status(error: BaseException) -> bool:
    """Return true for provider authentication, permission, or model errors."""
    seen: set[int] = set()
    pending: list[object] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for value in _status_values(current):
            if value is not None:
                try:
                    if int(value) in {401, 403, 404}:
                        return True
                except (TypeError, ValueError):
                    pass
        message = str(current)
        if _TERMINAL_PROVIDER_STATUS.search(message) or _TERMINAL_PROVIDER_TEXT.search(message):
            return True
        if isinstance(current, BaseException):
            pending.extend(
                (
                    current.__cause__,
                    current.__context__,
                    getattr(current, "response", None),
                )
            )
        else:
            pending.append(getattr(current, "response", None))
    return False


def _has_free_quota_error(error: BaseException) -> bool:
    """Return true when an error reports exhausted OpenRouter free quota."""
    seen: set[int] = set()
    pending: list[object] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current).lower()
        if any(marker in message for marker in _FREE_QUOTA_MARKERS):
            return True
        if isinstance(current, BaseException):
            pending.extend(
                (
                    current.__cause__,
                    current.__context__,
                    getattr(current, "response", None),
                )
            )
        else:
            pending.append(getattr(current, "response", None))
    return False


def _free_quota_reset_at(error: BaseException) -> float | None:
    """Extract the provider reset epoch from a quota error, when present."""
    seen: set[int] = set()
    pending: list[object] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        match = _FREE_QUOTA_RESET.search(str(current))
        if match:
            raw = int(match.group(1))
            return raw / (1000 if raw > 10_000_000_000 else 1)
        if isinstance(current, BaseException):
            pending.extend((current.__cause__, current.__context__))
    return None


def _free_quota_error_message() -> str:
    message = "OpenRouter free-model daily quota is exhausted"
    if _free_quota_circuit_reset_at is not None:
        return f"{message}; X-RateLimit-Reset={int(_free_quota_circuit_reset_at * 1000)}"
    return message


def _free_route_configured() -> bool:
    values = (
        value.lower()
        for key, value in os.environ.items()
        if "MODEL" in key and value
    )
    return any(marker in value for value in values for marker in _FREE_ROUTE_MARKERS)


def _install_free_route_circuit() -> None:
    """Stop a free-model quota outage from spawning one request per chunk."""
    global _free_route_circuit_installed
    if _free_route_circuit_installed or not _free_route_configured():
        return

    from cognee.infrastructure.llm.exceptions import LLMQuotaExceededError
    from cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm.generic_llm_api.adapter import (
        GenericAPIAdapter,
    )

    GenericAPIAdapter.MAX_RETRIES = 1
    original = GenericAPIAdapter.acreate_structured_output
    if getattr(original, "_citadel_free_route_circuit", False):
        _free_route_circuit_installed = True
        return

    async def guarded(self, *args, **kwargs):
        global _free_quota_circuit_open, _free_quota_circuit_reset_at
        async with _free_route_call_lock:
            if _free_quota_circuit_open:
                if (
                    _free_quota_circuit_reset_at is None
                    or time.time() < _free_quota_circuit_reset_at
                ):
                    raise LLMQuotaExceededError(_free_quota_error_message())
                _free_quota_circuit_open = False
                _free_quota_circuit_reset_at = None
            try:
                return await original(self, *args, **kwargs)
            except BaseException as error:
                if _has_free_quota_error(error):
                    _free_quota_circuit_open = True
                    _free_quota_circuit_reset_at = _free_quota_reset_at(error)
                    if _free_quota_circuit_reset_at is None:
                        _free_quota_circuit_reset_at = time.time() + 3600
                raise

    guarded._citadel_free_route_circuit = True
    GenericAPIAdapter.acreate_structured_output = guarded
    _free_route_circuit_installed = True


def install_cognee_retry_guard() -> None:
    """Make Cognee fail fast for terminal provider responses.

    Cognee owns the retry predicate. Mutating that shared predicate keeps all
    ingestion inside Cognee while preventing generic provider HTTP errors from
    entering Cognee's long transient-retry window.
    """
    from cognee.infrastructure.llm.retry_config import llm_retry_condition

    if getattr(llm_retry_condition, "_citadel_provider_guard", False):
        _install_free_route_circuit()
        return

    original_predicate = getattr(llm_retry_condition, "predicate", None)
    if original_predicate is None:
        return

    def predicate(error: BaseException) -> bool:
        if _has_terminal_provider_status(error) or _has_free_quota_error(error):
            return False
        return original_predicate(error)

    llm_retry_condition.predicate = predicate
    llm_retry_condition._citadel_provider_guard = True
    _install_free_route_circuit()
