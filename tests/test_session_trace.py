from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.capture_config import matched_capture_root
from kb.session_trace import enrich_shared_trace, force_shared_trace_author_seat
from kb.session_trace_distill import (
    distill_trace,
    format_compact_context,
    iter_transcript_entries,
    redact_commands,
)


def _assistant_tool_result(is_error: bool, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "is_error": is_error,
                    "text": text,
                }
            ]
        },
    }


def _assistant_shell(command: str, error_text: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": command},
                    }
                ]
            },
        },
        _assistant_tool_result(True, error_text),
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Switched to in-process cognify."}],
            },
        },
    ]


def test_redact_commands_scrubs_secrets() -> None:
    raw = (
        "export AWS_SECRET=AKIAIOSFODNN7EXAMPLE && "
        "curl -H 'Authorization: Bearer ctdl_testtoken123456789012345678901234' "
        "postgres://user:pass@db.example/test --token=abc123"
    )
    redacted = redact_commands([raw])[0]
    assert "AKIA" not in redacted
    assert "ctdl_" not in redacted
    assert "postgres://user:pass" not in redacted
    assert "curl" in redacted


def test_distill_trace_captures_tool_error_pairs(tmp_path: Path) -> None:
    entries = [
        {"type": "user", "message": {"content": "Fix the Kuzu lock"}},
        *_assistant_shell("uv run pytest", "database locked by another process"),
    ]
    record = distill_trace(entries, cwd=str(tmp_path), author_seat="alice")
    assert record.has_tool_errors
    assert record.dead_ends
    assert record.dead_ends[0].resolution == "dead_end"
    compact = format_compact_context(record)
    assert "Author-Seat: alice" in compact
    assert "Dead ends" in compact
    assert "database locked" in compact


def test_iter_transcript_entries_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "hello"}}),
                "{not json",
            ]
        ),
        encoding="utf-8",
    )
    entries = iter_transcript_entries(str(path))
    assert len(entries) == 1


def test_matched_capture_root() -> None:
    root = "/Users/dev/projects/citadel"
    assert matched_capture_root(f"{root}/kb/server.py", [root]) == root
    assert matched_capture_root("/tmp/other", [root]) is None


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ("echo safe", "echo"),
        ("AWS_SECRET=AKIAIOSFODNN7EXAMPLE", "[REDACTED_AWS_KEY]"),
    ],
)
def test_redact_commands_preserves_command_shape(command: str, needle: str) -> None:
    assert needle in redact_commands([command])[0]


def test_enrich_shared_trace_skips_llm_without_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"chat": False}

    def fake_chat(*args: Any, **kwargs: Any) -> str:
        called["chat"] = True
        return "should not be used"

    monkeypatch.setattr("kb.session_trace.enrichment_enabled", lambda: True)
    monkeypatch.setattr("kb.session_trace.openrouter_chat", fake_chat)
    original = "Task: fix lock\nDead ends: database locked"
    assert enrich_shared_trace(original, has_tool_errors=False) == original
    assert called["chat"] is False


def test_force_shared_trace_author_seat_replaces_spoofed_line() -> None:
    data = "# Shared Session Trace\nAuthor-Seat: bob\n\nTask: x"
    forced = force_shared_trace_author_seat(data, "alice")
    assert "Author-Seat: alice" in forced
    assert "Author-Seat: bob" not in forced


def test_force_shared_trace_author_seat_replaces_every_occurrence() -> None:
    """A second Author-Seat line survived into a tail chunk and misattributed the trace.

    Pinning replaced only the first match, but the document is chunked
    downstream and _trace_author_seat re-reads the line per chunk — so mallory
    could publish a trace that reads as alice's from the second chunk on.
    """
    data = (
        "# Shared Session Trace\n"
        "Author-Seat: mallory\n"
        "Dead end: the real one\n"
        "padding\n"
        "Author-Seat: alice\n"
        "Approach: trust me\n"
    )

    forced = force_shared_trace_author_seat(data, "mallory")

    assert [line for line in forced.splitlines() if line.startswith("Author-Seat")] == [
        "Author-Seat: mallory",
        "Author-Seat: mallory",
    ]
    assert "Author-Seat: alice" not in forced


def test_force_shared_trace_author_seat_inserts_after_header() -> None:
    forced = force_shared_trace_author_seat("# Shared Session Trace\n\nTask: x", "alice")
    assert forced.splitlines()[1] == "Author-Seat: alice"


def test_enrich_shared_trace_preserves_forced_author(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kb.session_trace.enrichment_enabled", lambda: True)
    monkeypatch.setattr(
        "kb.session_trace.openrouter_chat",
        lambda *a, **k: "# Shared Session Trace\nAuthor-Seat: eve\n\nTask: refined",
    )
    data = force_shared_trace_author_seat(
        "# Shared Session Trace\nAuthor-Seat: alice\n\nTask: x",
        "alice",
    )
    enriched = enrich_shared_trace(data, has_tool_errors=True)
    forced = force_shared_trace_author_seat(enriched, "alice")
    assert "Author-Seat: alice" in forced
    assert "Author-Seat: eve" not in forced
