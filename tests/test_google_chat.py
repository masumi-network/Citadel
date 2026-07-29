from __future__ import annotations

from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from kb.google_chat import GoogleChatConfigError, GoogleChatDelivery


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"name":"spaces/AAA/messages/BBB","thread":{"name":"spaces/AAA/threads/T"}}'


def delivery(**overrides: Any) -> GoogleChatDelivery:
    options: dict[str, Any] = {
        "space_name": "spaces/AAA",
        "thread_key": "citadel-org-digest",
        "token_provider": lambda: "access-token",
        "retry_count": 2,
        **overrides,
    }
    return GoogleChatDelivery(**options)


def install_fake_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("CITADEL_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("CITADEL_RETRY_MAX_DELAY_SECONDS", "0")

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        calls.append({"url": request.full_url, "timeout": timeout})
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("kb.google_chat.open_secure", fake_urlopen)
    return calls


def http_error(code: int, headers: dict[str, str] | None = None) -> HTTPError:
    return HTTPError("https://chat.googleapis.com", code, "error", headers or {}, BytesIO(b""))


def test_post_digest_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_urlopen(
        monkeypatch,
        [http_error(429, {"Retry-After": "0"}), FakeResponse()],
    )

    result = delivery().post_digest("Digest body", message_id="2026-06-10")

    assert result["sent"] is True
    assert result["message_name"] == "spaces/AAA/messages/BBB"
    assert len(calls) == 2
    assert "retryable" not in result
    assert "retry_after" not in result


def test_post_digest_retries_server_errors_and_network_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(
        monkeypatch,
        [http_error(503), URLError("connection reset"), FakeResponse()],
    )

    result = delivery().post_digest("Digest body")

    assert result["sent"] is True
    assert len(calls) == 3


def test_post_digest_does_not_retry_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_urlopen(monkeypatch, [http_error(401)])

    result = delivery().post_digest("Digest body")

    assert result["sent"] is False
    assert result["status_category"] == "auth_error"
    assert result["status_code"] == 401
    assert len(calls) == 1


def test_post_digest_gives_up_after_configured_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_urlopen(monkeypatch, [http_error(503)])

    result = delivery(retry_count=1).post_digest("Digest body")

    assert result["sent"] is False
    assert result["status_category"] == "server_error"
    assert len(calls) == 2


def test_fit_message_truncates_to_byte_budget_with_valid_utf8() -> None:
    chat = delivery(max_message_bytes=1000)
    text = "é" * 2000  # 2 bytes per character; any naive byte cut would split a code point.

    fitted = chat._fit_message(text)
    encoded = fitted.encode("utf-8")

    assert len(encoded) <= 1000
    assert fitted.endswith("[truncated]")
    assert encoded.decode("utf-8") == fitted  # round-trips, so no broken code points


def test_fit_message_leaves_short_messages_untouched() -> None:
    chat = delivery(max_message_bytes=1000)

    assert chat._fit_message("short digest") == "short digest"


def test_space_name_must_look_like_spaces_prefix() -> None:
    with pytest.raises(GoogleChatConfigError):
        GoogleChatDelivery(space_name="rooms/AAA", token_provider=lambda: "token")
