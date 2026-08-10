from __future__ import annotations

import io

import pytest

import kb.promotion_client as pc
from kb.promotion_client import PromotionClientError, _request


class _Response:
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return io.BytesIO(b'{"ok": true}').read()


def test_request_allows_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    opened_urls: list[str] = []

    def open_request(req, timeout):  # noqa: ANN001, ARG001
        opened_urls.append(req.full_url)
        return _Response()

    monkeypatch.setattr(pc._OPENER, "open", open_request)

    result = _request(
        "GET",
        "/api/session",
        base_url="http://127.0.0.1:8000",
        token="ctdl_t",
    )

    assert result == {"ok": True}
    assert opened_urls == ["http://127.0.0.1:8000/api/session"]


def test_request_rejects_public_http() -> None:
    with pytest.raises(PromotionClientError, match="refusing non-HTTPS Node URL"):
        _request("GET", "/api/session", base_url="http://node.example", token="ctdl_t")


def test_request_converts_read_timeout_to_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # #39: a read-phase timeout is a bare TimeoutError (not a URLError) and must be
    # converted to PromotionClientError so the CLI prints a clean line, not a traceback.
    def boom(req, timeout):  # noqa: ANN001, ARG001
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(pc._OPENER, "open", boom)

    with pytest.raises(PromotionClientError) as exc_info:
        _request("GET", "/api/session", base_url="https://node.example", token="ctdl_t")

    assert not isinstance(exc_info.value, TimeoutError)
    assert "timed out" in str(exc_info.value).lower()


def test_request_converts_oserror_to_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout):  # noqa: ANN001, ARG001
        raise OSError("connection reset by peer")

    monkeypatch.setattr(pc._OPENER, "open", boom)

    with pytest.raises(PromotionClientError) as exc_info:
        _request("GET", "/api/session", base_url="https://node.example", token="ctdl_t")

    assert "connection reset by peer" in str(exc_info.value)
