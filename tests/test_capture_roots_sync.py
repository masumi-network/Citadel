from __future__ import annotations

from typing import Any

import pytest

from kb.capture_config import CaptureConfig
from kb.capture_roots_sync import (
    CaptureRootsSyncResult,
    merge_capture_root_paths,
    sync_local_capture_roots_to_server,
    sync_warning_message,
)


def test_merge_capture_root_paths_unions_and_dedupes() -> None:
    merged = merge_capture_root_paths(
        ["/tmp/a", "/tmp/b"],
        ["/tmp/b", "/tmp/c"],
    )
    assert merged == ("/tmp/a", "/tmp/b", "/tmp/c")


def test_merge_capture_root_paths_normalizes() -> None:
    merged = merge_capture_root_paths(["~/work"], ["~/work"])
    assert len(merged) == 1


def test_sync_skips_when_no_local_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    result = sync_local_capture_roots_to_server(CaptureConfig())
    assert result.status == "skipped"
    assert "no local" in result.detail


def test_sync_skips_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CITADEL_WRITER_KEYS", raising=False)
    config = CaptureConfig().with_root("/tmp/repo", ["personal"])
    result = sync_local_capture_roots_to_server(config)
    assert result.status == "skipped"
    assert "CITADEL_MCP_ACCESS_TOKEN" in result.detail


def test_sync_skips_non_seat_token(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/repo", ["personal"])

    def fake_resolve(*args: Any, **kwargs: Any) -> str | None:
        return None

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", fake_resolve)
    result = sync_local_capture_roots_to_server(config)
    assert result.status == "skipped"
    assert "not seat-bound" in result.detail


def test_sync_puts_merged_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/new", ["org-work"])
    calls: dict[str, Any] = {}

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")

    def fake_resolve(base_url: str, token: str) -> str:
        calls["resolve"] = (base_url, token)
        return "alice"

    def fake_get(slug: str, *, base_url: str, token: str | None = None) -> dict[str, Any]:
        calls["get"] = (slug, base_url, token)
        return {"roots": ["/tmp/existing"]}

    def fake_put(
        slug: str,
        roots: list[str],
        *,
        base_url: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        calls["put"] = (slug, roots, base_url, token)
        return {"ok": True, "roots": roots}

    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", fake_resolve)
    monkeypatch.setattr("kb.capture_roots_sync.get_seat_capture_roots", fake_get)
    monkeypatch.setattr("kb.capture_roots_sync.update_seat_capture_roots", fake_put)

    result = sync_local_capture_roots_to_server(config)
    assert result.status == "synced"
    assert result.seat_slug == "alice"
    assert calls["put"][1] == ["/tmp/existing", "/tmp/new"]


def test_sync_unchanged_when_server_already_has_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/a", ["personal"])
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", lambda *a, **k: "alice")
    monkeypatch.setattr(
        "kb.capture_roots_sync.get_seat_capture_roots",
        lambda slug, **k: {"roots": ["/tmp/a", "/tmp/b"]},
    )

    put_called = False

    def fake_put(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal put_called
        put_called = True
        return {"ok": True}

    monkeypatch.setattr("kb.capture_roots_sync.update_seat_capture_roots", fake_put)
    result = sync_local_capture_roots_to_server(config)
    assert result.status == "unchanged"
    assert put_called is False


def test_sync_failed_on_node_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from kb.promotion_client import PromotionClientError

    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/a", ["personal"])
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", lambda *a, **k: "alice")
    attempts = 0

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise PromotionClientError("offline", status=None)

    monkeypatch.setattr("kb.capture_roots_sync.get_seat_capture_roots", boom)
    result = sync_local_capture_roots_to_server(config)
    assert result.status == "failed"
    assert "offline" in result.detail
    assert attempts == 2


def test_sync_does_not_retry_a_rejected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx means the Node understood and refused, so sending it again is noise.

    Production ran ten consecutive PUT .../capture-roots 422s, two per
    invocation, none of which could ever have succeeded.
    """
    from kb.promotion_client import PromotionClientError

    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/a", ["personal"])
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", lambda *a, **k: "alice")
    monkeypatch.setattr(
        "kb.capture_roots_sync.get_seat_capture_roots", lambda *a, **k: {"roots": []}
    )
    attempts = 0

    def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise PromotionClientError("unprocessable", status=422)

    monkeypatch.setattr("kb.capture_roots_sync.update_seat_capture_roots", rejected)

    result = sync_local_capture_roots_to_server(config)

    assert result.status == "failed"
    assert result.permanent is True
    assert attempts == 1, "a rejected payload must not be retried"


def test_sync_refuses_to_send_more_roots_than_the_node_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merge is a union, so it only grows, and an over-limit write never lands.

    Because it never lands, merged stays different from the server list, the
    "unchanged" short-circuit never fires, and every later sync re-sends the
    same rejected request. The loop cannot end on its own, so refuse locally
    and say what to do about it.
    """
    from kb.capture_config import MAX_APPROVED_CAPTURE_ROOTS

    config = CaptureConfig(node_url="https://node.example")
    for index in range(MAX_APPROVED_CAPTURE_ROOTS + 1):
        config = config.with_root(f"/tmp/root-{index}", ["personal"])
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", lambda *a, **k: "alice")
    monkeypatch.setattr(
        "kb.capture_roots_sync.get_seat_capture_roots", lambda *a, **k: {"roots": []}
    )
    put_called = False

    def put(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal put_called
        put_called = True
        return {}

    monkeypatch.setattr("kb.capture_roots_sync.update_seat_capture_roots", put)

    result = sync_local_capture_roots_to_server(config)

    assert put_called is False, "a request known to be invalid must not be sent"
    assert result.permanent is True
    assert str(MAX_APPROVED_CAPTURE_ROOTS) in result.detail
    assert "Remove some roots" in result.detail


def test_sync_retries_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from kb.promotion_client import PromotionClientError

    config = CaptureConfig(node_url="https://node.example").with_root("/tmp/new", ["org-work"])
    calls: dict[str, Any] = {}
    get_attempts = 0

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    monkeypatch.setattr("kb.capture_roots_sync.resolve_seat_slug", lambda *a, **k: "alice")

    def flaky_get(slug: str, *, base_url: str, token: str | None = None) -> dict[str, Any]:
        nonlocal get_attempts
        get_attempts += 1
        if get_attempts == 1:
            raise PromotionClientError("offline", status=None)
        calls["get"] = (slug, base_url, token)
        return {"roots": ["/tmp/existing"]}

    def fake_put(
        slug: str,
        roots: list[str],
        *,
        base_url: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        calls["put"] = (slug, roots, base_url, token)
        return {"ok": True, "roots": roots}

    monkeypatch.setattr("kb.capture_roots_sync.get_seat_capture_roots", flaky_get)
    monkeypatch.setattr("kb.capture_roots_sync.update_seat_capture_roots", fake_put)

    result = sync_local_capture_roots_to_server(config)
    assert result.status == "synced"
    assert result.seat_slug == "alice"
    assert get_attempts == 2
    assert calls["put"][1] == ["/tmp/existing", "/tmp/new"]


def test_sync_warning_message_for_failure() -> None:
    msg = sync_warning_message(
        CaptureRootsSyncResult(ok=False, status="failed", detail="timeout", seat_slug="alice")
    )
    assert msg is not None
    assert "alice" in msg
    assert "timeout" in msg


def test_sync_warning_message_for_missing_token() -> None:
    msg = sync_warning_message(
        CaptureRootsSyncResult(
            ok=True,
            status="skipped",
            detail="no seat token in environment — set CITADEL_MCP_ACCESS_TOKEN",
        )
    )
    assert msg is not None
    assert "locally only" in msg
