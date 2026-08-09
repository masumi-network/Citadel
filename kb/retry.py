from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import os
import random
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def default_max_attempts() -> int:
    return max(1, _int_env("CITADEL_RETRY_MAX_ATTEMPTS", 3))


def default_base_delay_seconds() -> float:
    return max(0.0, _float_env("CITADEL_RETRY_BASE_DELAY_SECONDS", 0.5))


def default_max_delay_seconds() -> float:
    return max(0.0, _float_env("CITADEL_RETRY_MAX_DELAY_SECONDS", 8.0))


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth retrying: connection/timeout, HTTP 429, and 5xx."""
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUSES
    return isinstance(exc, (URLError, TimeoutError, ConnectionError))


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a Retry-After header value (delta seconds or HTTP-date)."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def retry_after_from_error(exc: BaseException) -> float | None:
    if isinstance(exc, HTTPError) and exc.headers is not None:
        return retry_after_seconds(exc.headers.get("Retry-After"))
    return None


def run_with_retries(
    func: Callable[[], T],
    *,
    operation: str,
    max_attempts: int | None = None,
    base_delay_seconds: float | None = None,
    max_delay_seconds: float | None = None,
    is_retryable: Callable[[BaseException], bool] = is_transient_error,
    should_retry_result: Callable[[T], bool] | None = None,
    retry_after_from_result: Callable[[T], float | None] | None = None,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Run ``func`` with exponential backoff and full jitter.

    Retries only transient failures: exceptions accepted by ``is_retryable``
    (connection/timeout errors, HTTP 429, and 5xx by default), or results that
    ``should_retry_result`` marks retryable for callers that return error dicts
    instead of raising. Retry-After hints (from response headers or
    ``retry_after_from_result``) are honored, capped at ``max_delay_seconds``.
    On exhaustion the last exception is re-raised or the last result returned.
    """
    attempts = max(1, max_attempts if max_attempts is not None else default_max_attempts())
    base_delay = (
        base_delay_seconds if base_delay_seconds is not None else default_base_delay_seconds()
    )
    max_delay = max_delay_seconds if max_delay_seconds is not None else default_max_delay_seconds()
    chooser = rng or random
    sleeper = sleep if sleep is not None else time.sleep

    def delay_for(attempt: int, hint: float | None) -> float:
        if hint is not None:
            return min(max_delay, hint)
        return chooser.uniform(0.0, min(max_delay, base_delay * (2**attempt)))

    result: T
    for attempt in range(attempts):
        final_attempt = attempt >= attempts - 1
        try:
            result = func()
        except Exception as exc:
            if final_attempt or not is_retryable(exc):
                raise
            delay = delay_for(attempt, retry_after_from_error(exc))
            logger.warning(
                "%s failed with %s; retrying in %.2fs (attempt %d/%d)",
                operation,
                exc.__class__.__name__,
                delay,
                attempt + 1,
                attempts,
            )
            sleeper(delay)
            continue
        if final_attempt or should_retry_result is None or not should_retry_result(result):
            return result
        hint = retry_after_from_result(result) if retry_after_from_result else None
        delay = delay_for(attempt, hint)
        logger.warning(
            "%s returned a retryable result; retrying in %.2fs (attempt %d/%d)",
            operation,
            delay,
            attempt + 1,
            attempts,
        )
        sleeper(delay)
    return result
