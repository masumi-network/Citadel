from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from kb import access_client as ac


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_admin_key_missing_raises(monkeypatch) -> None:
    monkeypatch.delenv(ac.ADMIN_KEY_ENV, raising=False)
    with pytest.raises(ac.AccessClientError, match="missing admin key"):
        ac.admin_key()


def test_admin_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv(ac.ADMIN_KEY_ENV, "owner-admin")
    assert ac.admin_key() == "owner-admin"


def test_request_refuses_non_https() -> None:
    with pytest.raises(ac.AccessClientError, match="non-HTTPS"):
        ac.list_seats(base_url="http://node.example", key="owner-admin")


def test_create_seat_sends_payload_and_auth(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(req: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp({"ok": True, "token": "ctdl_new", "principal": {"seat_slug": "alice"}})

    monkeypatch.setattr(ac._OPENER, "open", fake_open)
    out = ac.create_seat(base_url="https://node.example", name="Alice", slug="alice", key="owner-admin")
    assert out["token"] == "ctdl_new"
    assert captured["url"] == "https://node.example/api/access/seats"
    assert captured["method"] == "POST"
    assert captured["body"] == {"name": "Alice", "slug": "alice", "role": "writer", "issue_token": True}
    assert captured["auth"] == "Bearer owner-admin"


def test_issue_seat_token_posts_to_seat_endpoint(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(req: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp({"ok": True, "token": "ctdl_seat", "principal": {"seat_slug": "sarthi"}})

    monkeypatch.setattr(ac._OPENER, "open", fake_open)
    out = ac.issue_seat_token("sarthi", base_url="https://node.example", key="owner-admin")
    assert out["token"] == "ctdl_seat"
    assert captured["url"] == "https://node.example/api/access/seats/sarthi/tokens"
    assert captured["method"] == "POST"


def test_http_error_maps_detail_and_status(monkeypatch) -> None:
    def fake_open(req: Any, timeout: float | None = None) -> _FakeResp:
        raise urllib.error.HTTPError(
            req.full_url,
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(json.dumps({"detail": "Seat already exists"}).encode()),
        )

    monkeypatch.setattr(ac._OPENER, "open", fake_open)
    with pytest.raises(ac.AccessClientError) as excinfo:
        ac.create_seat(base_url="https://node.example", name="A", slug="a", key="owner-admin")
    assert excinfo.value.status == 422
    assert "already exists" in str(excinfo.value)


def test_request_converts_read_timeout_to_client_error(monkeypatch) -> None:
    # #116: a read-phase timeout is a bare TimeoutError (not a URLError) and
    # escapes _request's HTTPError/URLError-only handling as a raw traceback
    # instead of a clean AccessClientError — the same #39 bug, unbackported
    # to this copy of the helper.
    def boom(req: Any, timeout: float | None = None) -> None:
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(ac._OPENER, "open", boom)
    with pytest.raises(ac.AccessClientError) as excinfo:
        ac.list_seats(base_url="https://node.example", key="owner-admin")
    assert not isinstance(excinfo.value, TimeoutError)
    assert "timed out" in str(excinfo.value).lower()


def test_request_converts_oserror_to_client_error(monkeypatch) -> None:
    def boom(req: Any, timeout: float | None = None) -> None:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(ac._OPENER, "open", boom)
    with pytest.raises(ac.AccessClientError) as excinfo:
        ac.list_seats(base_url="https://node.example", key="owner-admin")
    assert "connection reset by peer" in str(excinfo.value)
