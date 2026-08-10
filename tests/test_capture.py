from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kb import capture as capture_mod
from kb.capture import (
    _redact_url_userinfo,
    _truncate_utf8,
    build_capture_payload,
    capture_token,
    post_capture,
    summarize_root,
)
from kb.capture_config import CaptureConfig, CaptureRoot, save_capture_config
from kb.cli import _capture


def test_summarize_missing_path() -> None:
    summary = summarize_root(CaptureRoot(path="/no/such/path", tags=("personal",)))
    assert "path not found" in summary
    assert "Capture Root Tags: personal" in summary


def test_summarize_non_git_folder_with_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Title\n\nA local notes folder.\n")
    summary = summarize_root(CaptureRoot(path=str(tmp_path), tags=("personal",)))
    assert "non-git folder" in summary
    assert "A local notes folder." in summary


def test_summarize_git_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "README.md").write_text("a repo summary line\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init commit"], check=True)

    summary = summarize_root(CaptureRoot(path=str(tmp_path), tags=("org-work",)))
    assert "Latest:" in summary
    assert "init commit" in summary
    assert "a repo summary line" in summary


def test_build_capture_payload_appends_capture_tag_and_dedupes() -> None:
    payload = build_capture_payload(CaptureRoot(path="/x", tags=("org-work", "capture")))
    assert payload["tags"] == ["org-work", "capture"]
    assert payload["data"].startswith("# Capture summary")


def test_capture_dry_run_prints_payloads_without_network(tmp_path: Path, capsys) -> None:
    (tmp_path / "README.md").write_text("dry run notes\n")
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig(node_url="https://node.example").with_root(str(tmp_path), ["personal"]),
        path=config_path,
    )
    args = argparse.Namespace(config=str(config_path), root=None, dry_run=True)
    asyncio.run(_capture(args))

    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["tags"] == ["personal", "capture"]
    assert "dry run notes" in out[0]["preview"]


def test_capture_no_roots_warns(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "empty.json"
    save_capture_config(CaptureConfig(), path=config_path)
    args = argparse.Namespace(config=str(config_path), root=None, dry_run=True)
    rc = asyncio.run(_capture(args))
    assert rc == 1
    assert "Run `citadel setup`" in capsys.readouterr().err


def test_capture_root_filter_no_match_message(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig().with_root("/tmp/approved", ["personal"]), path=config_path
    )
    args = argparse.Namespace(config=str(config_path), root=["/tmp/nope"], dry_run=True)
    rc = asyncio.run(_capture(args))
    assert rc == 1
    err = capsys.readouterr().err
    assert "No configured root matches" in err
    assert "Run `citadel setup`" not in err


def test_capture_corrupt_config_exits_clean(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "capture.json"
    config_path.write_text("{ broken json")
    args = argparse.Namespace(config=str(config_path), root=None, dry_run=True)
    rc = asyncio.run(_capture(args))
    assert rc == 1
    assert "corrupt capture config" in capsys.readouterr().err


def test_capture_node_down_is_handled_and_exits_nonzero(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig(node_url="https://node.example").with_root(str(tmp_path), ["personal"]),
        path=config_path,
    )
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")

    import urllib.error

    def boom(*a: Any, **k: Any) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("kb.cli.post_capture", boom)
    args = argparse.Namespace(config=str(config_path), root=None, dry_run=False)
    rc = asyncio.run(_capture(args))
    assert rc == 1
    assert "FAIL" in capsys.readouterr().err


def test_capture_dry_run_returns_zero(tmp_path: Path) -> None:
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig().with_root(str(tmp_path), ["personal"]), path=config_path
    )
    args = argparse.Namespace(config=str(config_path), root=None, dry_run=True)
    assert asyncio.run(_capture(args)) == 0


def test_post_capture_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="non-HTTPS"):
        post_capture("http://node.example", "ctdl_x", {"data": "x", "tags": []})


def test_post_capture_allows_loopback_http(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = request.full_url
        return _FakeResp()

    monkeypatch.setattr(capture_mod._OPENER, "open", fake_open)

    assert post_capture(
        "http://127.0.0.1:8000",
        "ctdl_tok",
        {"data": "d", "tags": ["capture"]},
    ) == {"status": "ok"}
    assert captured["url"] == "http://127.0.0.1:8000/ingest"


def test_post_capture_posts_payload(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"status": "ok"}).encode()

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode())
        return _FakeResp()

    monkeypatch.setattr(capture_mod._OPENER, "open", fake_open)
    result = post_capture("https://node.example/", "ctdl_tok", {"data": "d", "tags": ["personal"]})

    assert result == {"status": "ok"}
    assert captured["url"] == "https://node.example/ingest"
    assert captured["headers"]["Authorization"] == "Bearer ctdl_tok"
    assert captured["body"] == {"data": "d", "tags": ["personal"]}


def test_capture_token_drops_admin_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CITADEL_WRITER_KEYS", raising=False)
    monkeypatch.setenv("CITADEL_ADMIN_KEY", "admin-should-not-be-used")
    assert capture_token() == ""

    monkeypatch.setenv("CITADEL_WRITER_KEYS", "writer-key, second")
    assert capture_token() == "writer-key"

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "seat-token")
    assert capture_token() == "seat-token"


def test_build_capture_payload_truncates_oversized_summary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "200")
    (tmp_path / "README.md").write_text("x " * 5000 + "\n")
    payload = build_capture_payload(CaptureRoot(path=str(tmp_path), tags=("personal",)))
    assert len(payload["data"].encode("utf-8")) <= 200


def test_truncate_utf8_does_not_split_codepoints() -> None:
    text = "é" * 100  # 2 bytes each
    out = _truncate_utf8(text, 5)
    assert out == "éé"  # 4 bytes, never a half codepoint


def test_redact_url_userinfo() -> None:
    assert _redact_url_userinfo("https://user:tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert _redact_url_userinfo("git@github.com:o/r.git") == "git@github.com:o/r.git"


def test_git_helper_survives_missing_git(monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(capture_mod.subprocess, "run", boom)
    assert capture_mod._git("/tmp", "status") is None


# --- server-decision truth: accepted / rejected / timeout --------------------


def _configured_args(tmp_path: Path, *, as_json: bool) -> argparse.Namespace:
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig(node_url="https://node.example").with_root(str(tmp_path), ["personal"]),
        path=config_path,
    )
    return argparse.Namespace(config=str(config_path), root=None, dry_run=False, json=as_json)


def _rejection(reason: str) -> dict[str, Any]:
    # Shape of a rejected /ingest write: cognee_result is null, NOT absent.
    return {
        "accepted": False,
        "reason": reason,
        "dataset": "seat:test",
        "tags": ["personal", "capture"],
        "cognee_result": None,
    }


def test_capture_duplicate_rejection_reports_ok_false_with_reason(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    monkeypatch.setattr(
        "kb.cli.post_capture", lambda *a, **k: _rejection("duplicate_in_process")
    )
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=True)))
    out = json.loads(capsys.readouterr().out)
    entry = out["results"][0]
    assert entry["ok"] is False
    assert entry["reason"] == "duplicate_in_process"
    # The desired end state (content on the server) already holds, so an
    # unchanged root is a visible no-op, not a failure.
    assert rc == 0
    assert out["ok"] is True


def test_capture_duplicate_human_output_says_nothing_stored(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    monkeypatch.setattr(
        "kb.cli.post_capture", lambda *a, **k: _rejection("duplicate_in_process")
    )
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=False)))
    captured = capsys.readouterr()
    assert rc == 0
    assert "nothing stored" in captured.out
    assert "duplicate_in_process" in captured.out


def test_capture_non_duplicate_rejection_is_a_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    monkeypatch.setattr("kb.cli.post_capture", lambda *a, **k: _rejection("content_too_short"))
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=False)))
    captured = capsys.readouterr()
    assert rc == 1
    assert "nothing stored" in captured.err
    assert "content_too_short" in captured.err


def test_capture_accepted_reports_ok_true(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    monkeypatch.setattr(
        "kb.cli.post_capture",
        lambda *a, **k: {
            "accepted": True,
            "reason": "accepted",
            "dataset": "seat:test",
            "tags": ["personal", "capture"],
            "cognee_result": {"status": "ok"},
        },
    )
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=True)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["results"][0]["ok"] is True
    assert out["results"][0]["status"] == "ok"


def test_capture_json_preserves_returned_lifecycle_fields_per_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_path = tmp_path / "capture.json"
    save_capture_config(
        CaptureConfig(node_url="https://node.example")
        .with_root(str(first), ["personal"])
        .with_root(str(second), ["personal"]),
        path=config_path,
    )
    responses = iter(
        [
            {
                "accepted": True,
                "status": "queued",
                "source_revision_id": "source-first",
                "projection_job_id": "job-first",
                "projection_state": "pending",
            },
            {
                "accepted": True,
                "status": "queued",
                "source_revision_id": "source-second",
            },
        ]
    )
    monkeypatch.setattr("kb.cli.post_capture", lambda *a, **k: next(responses))

    rc = asyncio.run(
        _capture(argparse.Namespace(config=str(config_path), root=None, dry_run=False, json=True))
    )

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["results"][0]["source_revision_id"] == "source-first"
    assert out["results"][0]["projection_job_id"] == "job-first"
    assert out["results"][0]["projection_state"] == "pending"
    assert out["results"][1]["source_revision_id"] == "source-second"
    assert "projection_job_id" not in out["results"][1]
    assert "projection_state" not in out["results"][1]


def test_capture_response_without_accepted_field_still_ok(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Legacy shape: a 200 body with no explicit rejection reads as accepted.
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")
    monkeypatch.setattr("kb.cli.post_capture", lambda *a, **k: {"status": "ok"})
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=True)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["results"][0]["ok"] is True


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("The read operation timed out"),
        urllib.error.URLError(TimeoutError("timed out")),
    ],
    ids=["read-timeout", "urlerror-wrapped-timeout"],
)
def test_capture_timeout_is_reported_unknown_not_failed(
    tmp_path: Path, monkeypatch, capsys, exc: Exception
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")

    def boom(*a: Any, **k: Any) -> None:
        raise exc

    monkeypatch.setattr("kb.cli.post_capture", boom)
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=True)))
    out = json.loads(capsys.readouterr().out)
    entry = out["results"][0]
    # A timeout proves only that the client stopped waiting, so the outcome is
    # unknown (null), never a claimed failure.
    assert entry["ok"] is None
    assert "may still have completed" in entry["error"]
    assert out["ok"] is False
    assert rc == 1


def test_capture_timeout_human_output_is_unknown_not_fail(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test")

    def boom(*a: Any, **k: Any) -> None:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("kb.cli.post_capture", boom)
    rc = asyncio.run(_capture(_configured_args(tmp_path, as_json=False)))
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNKNOWN" in captured.err
    assert "may still have completed" in captured.err
    assert "FAIL" not in captured.err


def test_http_timeout_default_raised_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS", raising=False)
    assert capture_mod.http_timeout_seconds() == 300.0
    monkeypatch.setenv("CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS", "45")
    assert capture_mod.http_timeout_seconds() == 45.0
    monkeypatch.setenv("CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS", "not-a-number")
    assert capture_mod.http_timeout_seconds() == 300.0


def test_post_capture_uses_configured_timeout(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_open(request: Any, timeout: float | None = None) -> _FakeResp:
        seen["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(capture_mod._OPENER, "open", fake_open)
    monkeypatch.setenv("CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS", "77")
    post_capture("https://node.example", "ctdl_t", {"data": "d", "tags": []})
    assert seen["timeout"] == 77.0
