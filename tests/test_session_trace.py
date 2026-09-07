from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.capture_config import matched_capture_root
from kb.session_trace import (
    SessionTraceStore,
    SharedTraceMetadata,
    TraceStoreCorruptionError,
    enrich_shared_trace,
    extract_trace_id,
    force_shared_trace_author_seat,
    force_shared_trace_id,
    mint_trace_id,
    prune_withdrawn_graph,
    result_trace_id,
)
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


def test_mint_trace_id_is_deterministic_and_opaque() -> None:
    data = "# Shared Session Trace\nAuthor-Seat: alice\n\nTask: x"
    first = mint_trace_id("alice", "sess-1", data)
    second = mint_trace_id("alice", "sess-1", data)
    assert first == second
    assert first.startswith("trace:")
    # Opaque: no source or query text leaks into the ID.
    assert "Task" not in first and "alice" not in first


def test_force_and_extract_trace_id_roundtrip() -> None:
    forced = force_shared_trace_id("# Shared Session Trace\n\nTask: x", "trace:abc123")
    assert forced.splitlines()[1] == "Trace-Id: trace:abc123"
    assert extract_trace_id(forced) == "trace:abc123"


def test_force_trace_id_replaces_every_occurrence() -> None:
    data = "# Shared Session Trace\nTrace-Id: trace:spoof\n\nTask\nTrace-Id: trace:spoof2\n"
    forced = force_shared_trace_id(data, "trace:real")
    assert "trace:spoof" not in forced
    assert forced.count("Trace-Id: trace:real") == 2


def test_result_trace_id_reads_dict_field_and_text() -> None:
    assert result_trace_id({"trace_id": "trace:x"}) == "trace:x"
    assert result_trace_id({"text": "# Shared Session Trace\nTrace-Id: trace:y\n"}) == "trace:y"
    assert result_trace_id({"metadata": {"content": "Trace-Id: trace:z"}}) == "trace:z"
    assert result_trace_id({"text": "no marker"}) is None


def test_empty_trace_marker_does_not_steal_next_line() -> None:
    # An empty ``Trace-Id:`` line must not borrow the following content line as
    # the ID: ``\s*`` used to let the value span the newline.
    text = "# Shared Session Trace\nTrace-Id:\nTask: private text\n"
    assert extract_trace_id(text) is None


def test_trace_marker_parses_normal_crlf_line() -> None:
    text = "# Shared Session Trace\r\nTrace-Id: trace:abc\r\nTask: x\r\n"
    assert extract_trace_id(text) == "trace:abc"


def test_direct_trace_id_field_rejects_embedded_newline() -> None:
    # A direct mapping value is line-oriented; a CR/LF would smuggle a second
    # spoofed line, so reject it rather than strip it.
    assert result_trace_id({"trace_id": "trace:x\nTrace-Id: trace:spoof"}) is None
    assert result_trace_id({"Trace-Id": "trace:y\r\nextra"}) is None
    assert result_trace_id({"trace_id": "trace:clean"}) == "trace:clean"


def test_mesh_trace_regex_matches_session_trace_parsing() -> None:
    from kb.knowledge_mesh import _node_trace_id

    # Graph node parity: the mesh copy must reject the empty marker the same way.
    assert _node_trace_id({"text": "Trace-Id: trace:node\nbody"}) == "trace:node"
    assert _node_trace_id({"text": "Trace-Id:\nTask: private text"}) is None


def test_session_trace_store_metadata_roundtrip(tmp_path: Path) -> None:
    store = SessionTraceStore(tmp_path / "traces.json")
    metadata = SharedTraceMetadata(
        trace_id="trace:1",
        author_principal="seat:alice",
        author_kind="human",
        seat_scope="alice",
        captured_at="2026-09-05T00:00:00+00:00",
        shared_at="2026-09-05T00:01:00+00:00",
        source_references=("repo", "main"),
        redaction_result="scanned",
        visibility="seat-shared",
        citations=("kb/server.py",),
        source_revision_ids=("source:abc",),
    )
    store.register(metadata)
    reopened = SessionTraceStore(tmp_path / "traces.json")
    loaded = reopened.get("trace:1")
    assert loaded is not None
    assert loaded.trace_id == "trace:1"
    assert loaded.author_principal == "seat:alice"
    assert loaded.trust == "reference-only"
    assert loaded.source_references == ("repo", "main")
    assert loaded.source_revision_ids == ("source:abc",)
    assert loaded.withdrawn is False

def test_session_trace_store_fails_closed_on_corrupt_withdrawal_state(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    path.write_text("{", encoding="utf-8")
    store = SessionTraceStore(path)

    with pytest.raises(TraceStoreCorruptionError):
        store.withdrawn_trace_ids()



def test_session_trace_withdrawal_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    store = SessionTraceStore(path)
    store.register(
        SharedTraceMetadata(trace_id="trace:1", author_principal="seat:alice")
    )
    first = store.withdraw("trace:1", actor_principal="seat:bob", reason_code="privacy")
    assert first.trace_id == "trace:1"
    assert first.actor_principal == "seat:bob"
    assert first.reason_code == "privacy"
    # Idempotent: repeat withdrawal keeps the original tombstone timestamp/actor.
    second = store.withdraw("trace:1", actor_principal="seat:carol", reason_code="other")
    assert second == first
    # Durable across reopen.
    reopened = SessionTraceStore(path)
    assert reopened.is_withdrawn("trace:1") is True
    assert reopened.withdrawn_trace_ids() == frozenset({"trace:1"})
    meta = reopened.get("trace:1")
    assert meta is not None and meta.withdrawn is True
    assert meta.withdrawn_by == "seat:bob"


def test_withdraw_unregistered_trace_still_tombstones(tmp_path: Path) -> None:
    store = SessionTraceStore(tmp_path / "traces.json")
    tombstone = store.withdraw("trace:ghost", actor_principal="seat:a", reason_code="x")
    assert tombstone.trace_id == "trace:ghost"
    assert store.is_withdrawn("trace:ghost") is True
    assert store.get("trace:ghost") is None


def test_withdraw_rejects_empty_inputs(tmp_path: Path) -> None:
    store = SessionTraceStore(tmp_path / "traces.json")
    with pytest.raises(ValueError):
        store.withdraw("  ", actor_principal="seat:a", reason_code="x")
    with pytest.raises(ValueError):
        store.withdraw("trace:1", actor_principal="seat:a", reason_code="  ")


def test_register_preserves_existing_withdrawal(tmp_path: Path) -> None:
    store = SessionTraceStore(tmp_path / "traces.json")
    store.register(SharedTraceMetadata(trace_id="trace:1", author_principal="seat:a"))
    store.withdraw("trace:1", actor_principal="seat:a", reason_code="privacy")
    # A later re-share must not silently un-withdraw the trace.
    refreshed = store.register(
        SharedTraceMetadata(trace_id="trace:1", author_principal="seat:a")
    )
    assert refreshed.withdrawn is True
    assert store.is_withdrawn("trace:1") is True


def test_prune_withdrawn_graph_drops_nodes_and_dangling_edges() -> None:
    payload = {
        "nodes": [
            {"id": "n1", "label": "keep", "properties": {"text": "ordinary"}},
            {"id": "n2", "text": "# Shared Session Trace\nTrace-Id: trace:gone\n"},
            {"id": "n3", "label": "also keep"},
        ],
        "edges": [
            {"source": "n1", "target": "n3"},
            {"source": "n1", "target": "n2"},
            {"source": "n2", "target": "n3"},
        ],
    }
    pruned = prune_withdrawn_graph(payload, {"trace:gone"})
    node_ids = {node["id"] for node in pruned["nodes"]}
    assert node_ids == {"n1", "n3"}
    assert pruned["edges"] == [{"source": "n1", "target": "n3"}]


def test_prune_withdrawn_graph_noop_without_withdrawn() -> None:
    payload = {"nodes": [{"id": "n1"}], "edges": []}
    assert prune_withdrawn_graph(payload, None)["nodes"] == [{"id": "n1"}]
    assert prune_withdrawn_graph(payload, set())["nodes"] == [{"id": "n1"}]
