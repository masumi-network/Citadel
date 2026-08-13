from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from kb.hooks import sync_session


def _user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _assistant_text(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _assistant_edit(file_path: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": file_path}}
            ]
        },
    }


def _sample_entries() -> list[dict[str, Any]]:
    return [
        _user("Build the autonomous personal-KB sync hook for org devs."),
        _assistant_text("I chose urllib so the script stays stdlib-only."),
        _assistant_edit("skills/citadel-proactive-ingest/scripts/sync_session.py"),
        _assistant_edit("tests/test_sync_session.py"),
        _assistant_text("Done. The hook is non-blocking and personal-by-default."),
    ]


def _write_transcript(tmp_path: Path, entries: list[Any], monkeypatch: Any | None = None) -> Path:
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps(e) if not isinstance(e, str) else e for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("CITADEL_TRANSCRIPT_ALLOW_ROOT", str(tmp_path))
    return path


class _RecordingPost:
    """Capture post_ingest calls instead of hitting the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, base_url: str, token: str, data: str, tags: list[str]) -> None:
        self.calls.append(
            {"base_url": base_url, "token": token, "data": data, "tags": tags}
        )


# --- distillation -----------------------------------------------------------


def test_distill_produces_nonempty_short_note() -> None:
    note = sync_session.distill_transcript(_sample_entries())
    assert note.strip()
    assert "Dev session note" in note
    assert "Task:" in note
    # Decision marker ("chose") was captured.
    assert "urllib" in note
    # Files changed section present.
    assert "sync_session.py" in note


def test_distill_empty_transcript_returns_empty() -> None:
    # An empty session has nothing worth storing. Returning "" lets the hook
    # skip the send instead of shipping a constant placeholder note.
    assert sync_session.distill_transcript([]) == ""


def test_distill_files_only_session_still_produces_note() -> None:
    # A session with edits but no prompt/outcome text is NOT empty.
    note = sync_session.distill_transcript([_assistant_edit("a.py")])
    assert "a.py" in note


# --- size cap ---------------------------------------------------------------


def test_size_cap_truncates(monkeypatch: Any) -> None:
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "50")
    assert sync_session._max_ingest_bytes() == 50
    big = "x" * 1000
    out = sync_session._truncate_utf8(big, 50)
    assert len(out.encode("utf-8")) <= 50


def test_truncate_never_splits_multibyte() -> None:
    # 10 emoji = 40 UTF-8 bytes; cap at 7 bytes must not yield a partial char.
    text = "😀" * 10
    out = sync_session._truncate_utf8(text, 7)
    assert len(out.encode("utf-8")) <= 7
    # Decodes cleanly (no replacement char from a split).
    assert out == "😀"


# --- defensive transcript parsing -------------------------------------------


def test_malformed_lines_skipped_without_crash(tmp_path: Path, monkeypatch: Any) -> None:
    entries: list[Any] = [
        _user("Real prompt here."),
        "{ this is not valid json",
        "",
        _assistant_edit("a.py"),
        "still : not json :::",
        _assistant_text("We decided to ship it."),
    ]
    path = _write_transcript(tmp_path, entries, monkeypatch)
    parsed = sync_session._iter_transcript(str(path))
    # 3 valid dict entries; 2 malformed + 1 blank skipped.
    assert len(parsed) == 3
    note = sync_session.distill_transcript(parsed)
    assert "Real prompt here." in note
    assert "a.py" in note


def test_iter_transcript_missing_file_returns_empty() -> None:
    assert sync_session._iter_transcript("/nonexistent/path/to/transcript.jsonl") == []


def test_iter_transcript_refuses_paths_outside_allowlist(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("CITADEL_TRANSCRIPT_ALLOW_ROOT", raising=False)
    path = _write_transcript(tmp_path, _sample_entries())
    assert sync_session._iter_transcript(str(path)) == []


# --- run(): missing token -> no POST + clean exit ---------------------------


def test_missing_token_no_post_clean_exit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    recorder = _RecordingPost()
    monkeypatch.setattr(sync_session, "post_ingest", recorder)

    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    payload = json.dumps(
        {
            "transcript_path": str(path),
            "cwd": str(tmp_path),
            "session_id": "s1",
            "hook_event_name": "SessionEnd",
        }
    )
    code = sync_session.run(io.StringIO(payload))
    assert code == 0
    assert recorder.calls == []  # no POST without a token


# --- run(): personal-by-default (no dataset field on POST) -------------------


def test_post_omits_dataset_field(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setenv("CITADEL_BASE_URL", "https://example.invalid")
    # Avoid invoking real git in build_tags.
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])

    captured: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: Any, timeout: int | None = None) -> _FakeResp:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(sync_session.urllib.request, "urlopen", fake_urlopen)

    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    payload = json.dumps(
        {
            "transcript_path": str(path),
            "cwd": str(tmp_path),
            "session_id": "s1",
            "hook_event_name": "SessionEnd",
        }
    )
    code = sync_session.run(io.StringIO(payload))
    assert code == 0

    # POST happened, over HTTPS, with the token in the Authorization header.
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.invalid/ingest"
    auth = captured["headers"].get("Authorization")
    assert auth == "Bearer ctdl_test_token"

    # Personal-by-default: NO dataset field on the body.
    body = captured["body"]
    assert "dataset" not in body
    assert set(body.keys()) == {"data", "tags"}
    assert body["data"].strip()
    assert body["tags"] == ["dev-session"]


# --- HTTPS-only invariant ----------------------------------------------------


def test_post_refuses_non_https() -> None:
    with pytest.raises(ValueError):
        sync_session.post_ingest(
            "http://example.invalid", "ctdl_x", "note", ["dev-session"]
        )


def test_redirects_are_not_followed() -> None:
    # A 3xx (esp. an https->http downgrade) must not re-send the Authorization
    # header. The handler refuses to follow any redirect.
    handler = sync_session._NoRedirectHandler()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.invalid")
        is None
    )


def test_run_swallows_post_errors(monkeypatch: Any, tmp_path: Path) -> None:
    """A failing POST must never raise out of the hook; run() returns 0."""
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(sync_session, "post_ingest", boom)

    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    payload = json.dumps({"transcript_path": str(path), "cwd": str(tmp_path)})
    assert sync_session.run(io.StringIO(payload)) == 0


def test_run_handles_garbage_stdin() -> None:
    # Non-JSON STDIN must not crash; clean exit.
    assert sync_session.run(io.StringIO("not json at all {{{")) == 0


# --- receipts tell the truth about what the server decided -------------------


def _hook_payload(path: Path, tmp_path: Path) -> str:
    return json.dumps(
        {
            "transcript_path": str(path),
            "cwd": str(tmp_path),
            "session_id": "s1",
            "hook_event_name": "SessionEnd",
        }
    )


def _fake_urlopen_with_body(monkeypatch: Any, body: bytes) -> None:
    class _Resp:
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return body

    monkeypatch.setattr(
        sync_session.urllib.request, "urlopen", lambda request, timeout=None: _Resp()
    )


def _receipt_text() -> str:
    from kb.hooks.receipt import activity_log_path

    path = activity_log_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_receipt_on_accepted_says_captured(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])
    _fake_urlopen_with_body(
        monkeypatch,
        json.dumps({"accepted": True, "reason": "accepted", "cognee_result": {}}).encode(),
    )
    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    receipt = _receipt_text()
    assert "session captured" in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_records_rejection_not_capture(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])
    _fake_urlopen_with_body(
        monkeypatch,
        json.dumps(
            {
                "accepted": False,
                "reason": "duplicate_in_process",
                "dataset": "seat:test",
                "tags": ["dev-session"],
                "cognee_result": None,
            }
        ).encode(),
    )
    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    receipt = _receipt_text()
    assert "not stored" in receipt
    assert "duplicate_in_process" in receipt
    assert "session captured" not in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_on_unreadable_body_does_not_claim_capture(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])
    _fake_urlopen_with_body(monkeypatch, b"not json")
    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    receipt = _receipt_text()
    assert "unreadable" in receipt
    assert "session captured" not in receipt


def test_receipt_on_timeout_says_write_may_have_completed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(sync_session, "post_ingest", boom)
    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    receipt = _receipt_text()
    assert "may still have completed" in receipt
    assert "session captured" not in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_on_send_failure_names_class_only(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_session, "build_tags", lambda cwd: ["dev-session"])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom with sensitive detail")

    monkeypatch.setattr(sync_session, "post_ingest", boom)
    path = _write_transcript(tmp_path, _sample_entries(), monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    receipt = _receipt_text()
    assert "not captured" in receipt
    assert "RuntimeError" in receipt
    assert "sensitive detail" not in receipt
    assert "session captured" not in receipt


def test_empty_session_is_not_sent(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    recorder = _RecordingPost()
    monkeypatch.setattr(sync_session, "post_ingest", recorder)
    path = _write_transcript(tmp_path, [], monkeypatch)
    assert sync_session.run(io.StringIO(_hook_payload(path, tmp_path))) == 0
    assert recorder.calls == []  # nothing extractable -> nothing sent
    receipt = _receipt_text()
    assert "skipped" in receipt
    assert "captured" not in receipt


def test_receipt_summary_missing_accepted_is_unconfirmed_not_captured() -> None:
    # A 2xx body without accepted: true must not read as "captured".
    summary = sync_session.receipt_summary({"reason": "queued"})
    assert summary == "session unconfirmed: server did not state accepted: true"
