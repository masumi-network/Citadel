from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kb import status as status_mod
from kb.cli import _status
from kb.status import (
    Check,
    StatusReport,
    check_local_setup,
    gather_status,
    render_text,
)


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _route(responses: dict[str, dict[str, Any]]):
    """Return a fake opener.open that dispatches by URL substring."""

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        url = request.full_url
        for needle, payload in responses.items():
            if needle in url:
                return _FakeResp(payload)
        raise AssertionError(f"unexpected URL {url}")

    return fake_open


def test_request_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="non-HTTPS"):
        status_mod._request("GET", "http://node.example/healthz")


def test_check_local_setup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_local_token_123")
    monkeypatch.setenv("CITADEL_CURSOR_MCP_PATH", str(tmp_path / "no-cursor-mcp.json"))
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {
                        "type": "http",
                        "url": "https://citadel.example/mcp/",
                        "headers": {"Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}"},
                    }
                }
            }
        )
    )
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\n")
    # Session hook lives in user-scope ~/.claude/settings.json (#38), isolated via
    # the CITADEL_HOME autouse fixture.
    from kb.onboard import claude_user_settings_path

    user_settings = claude_user_settings_path()
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text(
        json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"command": status_mod.SESSION_HOOK_MARKER}]}]}})
    )
    cfg = tmp_path / "capture.json"
    cfg.write_text(json.dumps({"roots": [{"path": "/tmp/x", "tags": ["org-work"]}]}))

    checks = {c.name: c for c in check_local_setup(tmp_path, cfg)}
    assert checks["token"].ok
    assert checks["mcp"].ok
    assert checks["mcp"].data["state"] == status_mod.MCP_STATE_READY
    assert checks["pre_push_hook"].ok
    assert checks["session_hook"].ok
    assert checks["capture_roots"].data["count"] == 1


def test_assess_mcp_setup_states(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CITADEL_CURSOR_MCP_PATH", str(tmp_path / "no-cursor-mcp.json"))
    missing = status_mod.assess_mcp_setup(tmp_path)
    assert missing.data["state"] == status_mod.MCP_STATE_MISSING
    assert missing.data["next"] == status_mod.MCP_REMEDIATION
    assert missing.data["fallback"] == status_mod.MCP_AGENT_FALLBACK
    assert "vault-backed" in status_mod.MCP_AGENT_FALLBACK
    assert "citadel search" in status_mod.MCP_AGENT_FALLBACK

    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {
                        "type": "http",
                        "url": "https://citadel.example/mcp/",
                    }
                }
            }
        )
    )
    needs_auth = status_mod.assess_mcp_setup(tmp_path)
    assert needs_auth.data["state"] == status_mod.MCP_STATE_NEEDS_AUTH
    assert not needs_auth.ok
    assert "CITADEL_MCP_ACCESS_TOKEN" in (needs_auth.data.get("next") or "")
    assert needs_auth.data["next"] != status_mod.MCP_REMEDIATION
    assert needs_auth.data["fallback"] == status_mod.MCP_AGENT_FALLBACK
    assert "CLI search" in needs_auth.detail

    # Legacy stdio without a token must be readyButUnconfigured (not needsAuth) —
    # a token alone will not expose hosted tools.
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"citadel": {"command": "python", "args": ["-m", "kb.mcp_server"]}}})
    )
    unconfigured_no_token = status_mod.assess_mcp_setup(tmp_path)
    assert unconfigured_no_token.data["state"] == status_mod.MCP_STATE_READY_BUT_UNCONFIGURED
    assert "not hosted HTTP" in unconfigured_no_token.detail

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    unconfigured = status_mod.assess_mcp_setup(tmp_path)
    assert unconfigured.data["state"] == status_mod.MCP_STATE_READY_BUT_UNCONFIGURED
    assert not unconfigured.ok

    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {"type": "http", "url": "https://citadel.example/mcp/"}
                }
            }
        )
    )
    ready = status_mod.assess_mcp_setup(tmp_path)
    assert ready.ok
    assert ready.data["state"] == status_mod.MCP_STATE_READY
    assert ready.data["next"] is None
    assert "fallback" not in ready.data


def test_check_local_setup_all_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    cfg = tmp_path / "absent.json"
    checks = {c.name: c for c in check_local_setup(tmp_path, cfg)}
    assert not checks["token"].ok
    assert not checks["mcp"].ok
    assert not checks["pre_push_hook"].ok
    assert not checks["session_hook"].ok
    assert checks["capture_roots"].ok  # "none" is a valid state


def test_gather_status_skips_search_by_default(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        url = request.full_url
        seen.append(url)
        if "/healthz" in url:
            return _FakeResp({"ok": True})
        if "/api/session" in url:
            return _FakeResp({"ok": True, "role": "writer", "seat_slug": "s", "actor": {"name": "S"}})
        if "/readyz" in url:
            return _FakeResp({"ok": True, "corpus": {"ok": True, "tracked_sources": 1, "indexed_docs": 1}})
        if "/api/contributions/recent" in url:
            return _FakeResp({"contributions": []})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(status_mod._OPENER, "open", fake_open)
    report = gather_status(
        "https://node.example", "ctdl_tok", repo=tmp_path, config_path=tmp_path / "c.json"
    )
    assert report.healthy
    assert all("/search" not in u for u in seen)
    assert all(c.name != "search" for c in report.checks)


def test_check_search_uses_short_smoke_timeout(monkeypatch) -> None:
    captured: dict[str, float | None] = {"timeout": None}

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        captured["timeout"] = timeout
        return _FakeResp({"results": [{"id": 1}]})

    monkeypatch.setattr(status_mod._OPENER, "open", fake_open)
    check = status_mod.check_search("https://node.example", "ctdl_tok")
    assert check.ok
    assert captured["timeout"] == status_mod._SMOKE_SEARCH_TIMEOUT


def test_gather_status_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        status_mod._OPENER,
        "open",
        _route(
            {
                "/healthz": {"ok": True, "service": "citadel"},
                "/api/session": {
                    "ok": True,
                    "role": "writer",
                    "seat_slug": "sarthi",
                    "node_label": "Node · sarthi",
                    "capabilities": {"read": True, "write": True, "admin": False},
                    "actor": {"name": "Sarthi"},
                },
                "/search": {"results": [{"id": 1}]},
                "/api/contributions/recent": {"contributions": [{"title": "feat: x", "created_at": "2026-06-27T10:00:00"}]},
            }
        ),
    )
    report = gather_status(
        "https://node.example",
        "ctdl_tok",
        repo=tmp_path,
        config_path=tmp_path / "c.json",
        with_search=True,
    )
    assert report.healthy
    assert report.identity["seat_slug"] == "sarthi"
    names = {c.name: c.ok for c in report.checks}
    assert names["node"] and names["auth"] and names["search"]
    assert report.recent[0]["title"] == "feat: x"
    readiness = report.readiness()
    assert readiness["authenticated"] is True
    assert readiness["search"] is True
    assert readiness["search_probed"] is True
    assert readiness["reason"] == "ok"
    assert "code" not in readiness
    assert report.to_dict()["readiness"]["search"] is True


def test_readiness_auth_required_and_search_codes(tmp_path: Path) -> None:
    report = StatusReport(
        node_url="https://node.example",
        healthy=False,
        identity={},
        checks=[
            Check("auth", ok=False, detail="no token", data={"code": status_mod.CODE_AUTH_REQUIRED}),
            Check("search", ok=False, detail="skipped", data={"code": status_mod.CODE_AUTH_REQUIRED}),
        ],
        recent=[],
        repo=str(tmp_path),
    )
    r = report.readiness()
    assert r["authenticated"] is False
    assert r["search"] is False
    assert r["code"] == status_mod.CODE_AUTH_REQUIRED

    timeout_report = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={"seat_slug": "a"},
        checks=[
            Check("auth", ok=True, detail="valid"),
            Check(
                "search",
                ok=False,
                detail="timed out",
                data={"timed_out": True, "code": status_mod.CODE_TIMEOUT},
            ),
        ],
        recent=[],
    )
    tr = timeout_report.readiness()
    assert tr["authenticated"] is True
    assert tr["search"] is False
    assert tr["code"] == status_mod.CODE_TIMEOUT


def test_auth_no_token_sets_code() -> None:
    auth = status_mod.check_auth("https://node.example", None)
    assert auth.ok is False
    assert auth.data.get("code") == status_mod.CODE_AUTH_REQUIRED


def test_check_local_setup_corrupt_config_does_not_raise(tmp_path: Path) -> None:
    cfg = tmp_path / "capture.json"
    cfg.write_text("{ broken json")
    checks = {c.name: c for c in check_local_setup(tmp_path, cfg)}
    assert checks["capture_roots"].ok is False
    assert "corrupt" in checks["capture_roots"].detail


def test_check_search_empty_list_is_zero(monkeypatch) -> None:
    monkeypatch.setattr(status_mod._OPENER, "open", _route({"/search": {"results": []}}))
    check = status_mod.check_search("https://node.example", "ctdl_tok")
    # #27: a zero-result smoke search is RED (read path up but data plane empty),
    # not always-green.
    assert check.ok is False and check.data["count"] == 0
    assert check.data.get("code") == status_mod.CODE_SEARCH_UNAVAILABLE
    assert "0 result(s)" in check.detail


def test_check_corpus_red_when_readyz_503(monkeypatch) -> None:
    # #27: /readyz answers 503 + body when the corpus gate trips; check_corpus
    # parses the body and reports RED.
    import io
    import urllib.error

    body = json.dumps(
        {"ok": False, "corpus": {"ok": False, "tracked_sources": 200, "indexed_docs": 0}, "canary": None}
    ).encode()

    def raise_503(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, io.BytesIO(body))

    monkeypatch.setattr(status_mod._OPENER, "open", raise_503)
    check = status_mod.check_corpus("https://node.example", "ctdl_tok")
    assert check is not None
    assert check.ok is False
    assert "0 indexed / 200 tracked" in check.detail


def test_check_corpus_ok_when_readyz_healthy(monkeypatch) -> None:
    monkeypatch.setattr(
        status_mod._OPENER,
        "open",
        _route({"/readyz": {"ok": True, "corpus": {"ok": True, "tracked_sources": 200, "indexed_docs": 280}, "canary": {"ok": True}}}),
    )
    check = status_mod.check_corpus("https://node.example", "ctdl_tok")
    assert check is not None and check.ok is True
    assert "280 indexed / 200 tracked" in check.detail


def test_gather_status_node_down(tmp_path: Path) -> None:
    import kb.status as s

    def boom(request: Any, timeout: float | None = None) -> None:
        raise OSError("connection refused")

    original = s._OPENER.open
    s._OPENER.open = boom  # type: ignore[assignment]
    try:
        report = gather_status("https://node.example", "ctdl_tok", repo=tmp_path, config_path=tmp_path / "c.json")
    finally:
        s._OPENER.open = original  # type: ignore[assignment]
    assert not report.healthy
    assert any(c.name == "node" and not c.ok for c in report.checks)


def test_auth_no_token() -> None:
    report = gather_status(
        "https://node.example", None, with_search=False, with_recent=False, repo=Path("/tmp")
    )
    auth = next(c for c in report.checks if c.name == "auth")
    assert not auth.ok
    assert "no token" in auth.detail


def test_render_text_smoke() -> None:
    report = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={"seat_slug": "sarthi", "role": "writer"},
        checks=[Check("node", ok=True, detail="healthy", latency_ms=38)],
        recent=[],
    )
    out = render_text(report)
    assert "seat: sarthi" in out
    assert "All systems go." in out
    assert "✓" in out


def test_render_text_names_the_checked_repo() -> None:
    # Local checks are repo-relative — a ✗ from the wrong directory must read
    # as "wrong directory", not "broken setup", so the repo is always named.
    report = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={},
        checks=[Check("mcp", ok=False, detail="not configured")],
        recent=[],
        repo="/some/checked/repo",
    )
    assert "/some/checked/repo" in render_text(report)


def test_render_text_search_failure_warns_not_fails() -> None:
    # Search never gates health — its failure renders as a yellow ! warning,
    # not the same red ✗ as a fatal check, so glyphs can't contradict the
    # green "Connected" verdict.
    report = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={},
        checks=[
            Check("node", ok=True, detail="healthy"),
            Check("search", ok=False, detail="timed out after 3s — node warming up", data={"timed_out": True}),
        ],
        recent=[],
    )
    out = render_text(report)
    search_row = next(line for line in out.splitlines() if "Search" in line)
    assert "!" in search_row
    assert "✗" not in search_row


def test_render_text_warns_seatless_token() -> None:
    report = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={"seat_slug": None, "role": "writer", "capabilities": {"write": True}},
        checks=[Check("node", ok=True, detail="healthy")],
        recent=[],
    )
    out = render_text(report, verdict=False)
    assert "no seat" in out
    assert "citadel_share_session" in out


def test_seatless_token_hint() -> None:
    assert status_mod.seatless_token_hint({"seat_slug": "alice"}) is None
    hint = status_mod.seatless_token_hint({"role": "writer"})
    assert hint and "403" in hint and "seat-bound" in hint


def test_search_node_returns_full_payload(monkeypatch) -> None:
    payload = {
        "results": [{"id": "1"}],
        "sections": {"central": [{"id": "1"}], "session_traces": [], "node": []},
    }
    monkeypatch.setattr(status_mod._OPENER, "open", _route({"/search": payload}))
    out = status_mod.search_node("https://node.example", "ctdl_tok", "q")
    assert out["sections"]["central"][0]["id"] == "1"
    assert out["results"][0]["id"] == "1"


def test_search_node_forwards_filter_fields(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _FakeResp({"results": [], "sections": {}})

    monkeypatch.setattr(status_mod._OPENER, "open", fake_open)
    status_mod.search_node(
        "https://node.example",
        "ctdl_tok",
        "schema",
        8,
        types=["spec", "skill"],
        repo="masumi-network/agent",
        path="docs/MIP",
        canonical_only=True,
    )
    assert captured["body"] == {
        "query": "schema",
        "top_k": 8,
        "types": ["spec", "skill"],
        "repo": "masumi-network/agent",
        "path": "docs/MIP",
        "canonical_only": True,
    }


def test_humanize_net_error_translates_dns_noise() -> None:
    # Raw urllib reasons read like C errno dumps — humans get words.
    exc = OSError("<urlopen error [Errno 8] nodename nor servname provided, or not known>")
    assert status_mod._humanize_net_error(exc) == "cannot resolve host"
    assert status_mod._humanize_net_error(OSError("[Errno 61] Connection refused")) == "connection refused"


def test_status_command_json_and_exit(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        status_mod._OPENER,
        "open",
        _route({"/healthz": {"ok": True}, "/api/session": {"ok": True, "role": "writer", "seat_slug": "s"}, "/search": {"results": []}, "/api/contributions/recent": {"contributions": []}}),
    )
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_tok")
    args = argparse.Namespace(
        json=True,
        node_url="https://node.example",
        repo=str(tmp_path),
        config=str(tmp_path / "c.json"),
        check_search=False,
        no_search=False,
        no_recent=False,
    )
    rc = asyncio.run(_status(args))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["identity"]["seat_slug"] == "s"


def test_status_command_unhealthy_exits_one(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)

    def boom(request: Any, timeout: float | None = None) -> None:
        raise OSError("down")

    monkeypatch.setattr(status_mod._OPENER, "open", boom)
    args = argparse.Namespace(
        json=False,
        node_url="https://node.example",
        repo=str(tmp_path),
        config=str(tmp_path / "c.json"),
        check_search=False,
        no_search=True,
        no_recent=True,
    )
    rc = asyncio.run(_status(args))
    assert rc == 1
    assert "Not connected" in capsys.readouterr().out


# --- Vault Activity feed (citadel activity / DX-6) ---------------------------


def test_fetch_events_empty_without_token() -> None:
    assert status_mod.fetch_events("https://node.example", None) == {}


def test_fetch_events_builds_scoped_url_and_parses(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = request.full_url
        return _FakeResp(
            {"events": [{"id": 7, "type": "ingest", "message": "Memory indexed"}], "latest_event_id": 7}
        )

    monkeypatch.setattr(status_mod._OPENER, "open", fake_open)
    data = status_mod.fetch_events(
        "https://node.example", "ctdl_tok", after_id=3, limit=5, event_type="ingest"
    )
    assert "/api/knowledge/events?" in captured["url"]
    assert "limit=5" in captured["url"]
    assert "after_id=3" in captured["url"]
    assert "type=ingest" in captured["url"]
    assert data["latest_event_id"] == 7


def test_fetch_events_marks_network_failure(monkeypatch) -> None:
    # A network failure must be distinguishable from an empty feed (#101), so the
    # renderer can say "couldn't reach the Node" instead of "No recent activity".
    def boom(request: Any, timeout: float | None = None) -> _FakeResp:
        raise RuntimeError("network down")

    monkeypatch.setattr(status_mod._OPENER, "open", boom)
    result = status_mod.fetch_events("https://node.example", "ctdl_tok")
    assert result.get("error")
    assert "events" not in result
    # A missing token is a genuinely-empty state, NOT a failure.
    assert status_mod.fetch_events("https://node.example", None) == {}


def test_render_event_line() -> None:
    from kb.cli import _render_event

    event = {
        "id": 5,
        "type": "ingest",
        "message": "Memory indexed",
        "details": {"dataset": "seat:sarthi"},
        "created_at": "2026-07-16T12:13:16Z",
    }
    line = _render_event(event, color=False)
    assert "12:13" in line
    assert "ingest" in line
    assert "Memory indexed" in line
    assert "seat:sarthi" in line


# --- Seat Presence broadcast (citadel activity --global / DX-7) --------------


def test_fetch_presence_extracts_only_seat_hubs_no_content(monkeypatch) -> None:
    # A content node MUST be ignored — fetch_presence may only surface Seat
    # Presence (slug + count), never another seat's Node content (ADR-0009).
    graph = {
        "nodes": [
            {"id": "doc-1", "label": "Alice private doc", "type": "TextDocument"},
            {"id": "dataset:seat:alice", "label": "seat:alice", "type": "dataset",
             "presence": {"documents": 5}},
            {"id": "dataset:masumi-network", "label": "masumi-network", "type": "dataset",
             "presence": {"documents": 100}},
        ]
    }
    monkeypatch.setattr(status_mod._OPENER, "open", _route({"/api/mesh/graph": graph}))
    board = status_mod.fetch_presence("https://node.example", "ctdl_tok")

    labels = {s["seat"] for s in board["seats"]}
    assert labels == {"seat:alice", "masumi-network"}
    assert not any("private doc" in str(s).lower() for s in board["seats"])
    alice = next(s for s in board["seats"] if s["seat"] == "seat:alice")
    assert alice["documents"] == 5


def test_fetch_presence_empty_without_token() -> None:
    assert status_mod.fetch_presence("https://node.example", None) == {}


def test_fetch_presence_marks_network_failure(monkeypatch) -> None:
    # "No seats visible." must not be printed for a timed-out node (#101).
    def boom(request: Any, timeout: float | None = None) -> _FakeResp:
        raise RuntimeError("timed out")

    monkeypatch.setattr(status_mod._OPENER, "open", boom)
    board = status_mod.fetch_presence("https://node.example", "ctdl_tok")
    assert board.get("error")
    assert "seats" not in board


def test_seat_hint_suppressed_when_auth_failed() -> None:
    # A timed-out /api/session leaves no seat_slug; the seatless hint used to fire
    # and tell a working token to go ask an admin for a seat (#101).
    from kb.status import Check, StatusReport, render_text

    failed = StatusReport(
        node_url="https://node.example",
        healthy=False,
        identity={"code": "AUTH_REQUIRED"},
        checks=[
            Check("node", ok=False, detail="unreachable (timed out)"),
            Check("auth", ok=False, detail="unreachable (timed out)", data={"code": "AUTH_REQUIRED"}),
        ],
        recent=[],
    )
    assert "has no seat" not in render_text(failed, color=False)

    # A genuinely seatless but VALID token still gets the warning.
    seatless = StatusReport(
        node_url="https://node.example",
        healthy=True,
        identity={"seat_slug": None, "role": "writer"},
        checks=[
            Check("node", ok=True, detail="healthy"),
            Check("auth", ok=True, detail="valid", data={"role": "writer"}),
        ],
        recent=[],
    )
    assert "has no seat" in render_text(seatless, color=False)


def test_render_presence_board_sorts_by_count() -> None:
    from kb.cli import _render_presence

    board = {"seats": [
        {"seat": "seat:sarthi", "documents": 23},
        {"seat": "masumi-network", "documents": 1022},
    ]}
    out = _render_presence(board, color=False)
    assert "Team presence" in out
    assert "seat:sarthi" in out
    assert "23 docs" in out
    assert out.index("masumi-network") < out.index("seat:sarthi")
