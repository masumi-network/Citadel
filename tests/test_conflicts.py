from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kb.config import CitadelConfig
from kb.conflicts import (
    ConflictCandidate,
    ConflictSide,
    KnowledgeConflictStore,
    clip_excerpt,
    detect_contribution_conflict,
    obsidian_push_conflict_candidate,
)


def candidate(suffix: str = "1") -> ConflictCandidate:
    return ConflictCandidate(
        kind="test_kind",
        summary=f"Disagreement {suffix}",
        side_a=ConflictSide(source="vault_contribution", excerpt=f"new {suffix}"),
        side_b=ConflictSide(source="existing_note", excerpt=f"old {suffix}"),
        dedupe_key=f"key-{suffix}",
    )


def test_record_list_and_resolve_round_trip(tmp_path: Path) -> None:
    store = KnowledgeConflictStore(tmp_path / "conflicts.json")

    recorded = store.record(candidate())
    listed = store.list(status="open")
    resolved = store.resolve(
        recorded["id"],
        resolution_note="Kept the repository truth.",
        resolved_by="principal_test",
    )

    assert recorded["status"] == "open"
    assert recorded["kind"] == "test_kind"
    assert recorded["side_a"]["source"] == "vault_contribution"
    assert listed[0]["id"] == recorded["id"]
    assert resolved["status"] == "resolved"
    assert resolved["resolution_note"] == "Kept the repository truth."
    assert resolved["resolved_by"] == "principal_test"
    assert store.list(status="open") == []
    assert store.open_count() == 0
    assert store.list(status="resolved")[0]["id"] == recorded["id"]


def test_recording_same_open_conflict_twice_is_deduplicated(tmp_path: Path) -> None:
    store = KnowledgeConflictStore(tmp_path / "conflicts.json")

    first = store.record(candidate())
    second = store.record(candidate())

    assert first["id"] == second["id"]
    assert len(store.list()) == 1


def test_scoped_conflicts_with_same_key_do_not_collide(tmp_path: Path) -> None:
    store = KnowledgeConflictStore(tmp_path / "conflicts.json")

    alice = store.record(
        replace(candidate("same"), dataset="seat:alice")
    )
    bob = store.record(
        replace(candidate("same"), dataset="seat:bob")
    )

    assert alice["id"] != bob["id"]
    assert {item["dataset"] for item in store.list()} == {"seat:alice", "seat:bob"}


def test_store_is_bounded_and_drops_resolved_records_first(tmp_path: Path) -> None:
    store = KnowledgeConflictStore(tmp_path / "conflicts.json", max_records=3)
    resolved = store.record(candidate("resolved"))
    store.resolve(resolved["id"], resolution_note="done", resolved_by="tester")
    for index in range(3):
        store.record(candidate(str(index)))

    remaining = store.list()

    assert len(remaining) == 3
    assert all(item["status"] == "open" for item in remaining)


def test_resolve_unknown_conflict_raises_key_error(tmp_path: Path) -> None:
    store = KnowledgeConflictStore(tmp_path / "conflicts.json")

    with pytest.raises(KeyError):
        store.resolve("kconflict_missing", resolution_note="x", resolved_by="tester")


def test_corrupt_state_file_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "conflicts.json"
    path.write_text("{not json", encoding="utf-8")
    store = KnowledgeConflictStore(path)

    assert store.list() == []
    assert store.record(candidate())["status"] == "open"


def test_excerpts_are_clipped_and_redacted() -> None:
    clipped = clip_excerpt("x" * 600)
    redacted = clip_excerpt("token=ghp_abcdef1234567890abcdef1234567890abcd")

    assert len(clipped) == 240
    assert "ghp_abcdef1234567890abcdef1234567890abcd" not in redacted


def test_obsidian_push_conflict_candidate_maps_sides() -> None:
    sync_conflict = {
        "id": "conflict_abc",
        "path": "Team/Roadmap.md",
        "local_body": "Local stale edit",
        "remote_body": "Server newer copy",
        "remote_rev": 2,
        "created_at": "2026-06-09T00:00:00Z",
        "updated_at": "2026-06-10T00:00:00Z",
    }

    result = obsidian_push_conflict_candidate(sync_conflict, vault_name="Team Vault")

    assert result.kind == "obsidian_push"
    assert "Team/Roadmap.md" in result.summary
    assert result.side_a.excerpt == "Local stale edit"
    assert result.side_b.excerpt == "Server newer copy"
    assert "server revision 2" in result.side_b.source
    assert result.dedupe_key == "conflict_abc"


def github_state_config(tmp_path: Path) -> CitadelConfig:
    state_path = tmp_path / "github_state.json"
    state_path.write_text(
        json.dumps(
            {
                "org": "masumi-network",
                "last_digest_at": "2026-06-09T00:00:00Z",
                "last_digest": (
                    "# masumi-network GitHub daily update\n\n"
                    "## Recent commits\n- abc: ship the digest composer.\n"
                ),
            }
        ),
        encoding="utf-8",
    )
    return CitadelConfig(
        github_sync_state_path=str(state_path),
        obsidian_sync_state_path=str(tmp_path / "missing_obsidian.json"),
    )


def test_detects_contribution_disagreeing_with_repository_update(tmp_path: Path) -> None:
    config = github_state_config(tmp_path)

    detected = detect_contribution_conflict(
        "# Recent commits\n- the digest composer was reverted yesterday.",
        config=config,
    )

    assert detected is not None
    assert detected.kind == "contribution_vs_repository_update"
    assert "repository truth" in detected.summary
    assert detected.side_b.timestamp == "2026-06-09T00:00:00Z"
    assert "Recent commits" in detected.side_b.source


def test_identical_contribution_is_not_a_conflict(tmp_path: Path) -> None:
    config = github_state_config(tmp_path)
    section_body = "## Recent commits\n- abc: ship the digest composer."

    assert detect_contribution_conflict(section_body, config=config) is None


def test_unrelated_contribution_is_not_a_conflict(tmp_path: Path) -> None:
    config = github_state_config(tmp_path)

    detected = detect_contribution_conflict(
        "# Quarterly planning\nNothing about repositories here.",
        config=config,
    )

    assert detected is None


def test_detects_contribution_matching_obsidian_note_with_other_hash(tmp_path: Path) -> None:
    obsidian_path = tmp_path / "obsidian.json"
    obsidian_path.write_text(
        json.dumps(
            {
                "documents": {
                    "doc_1": {
                        "id": "doc_1",
                        "source_id": "vault_1",
                        "normalized_path": "Team/Architecture.md",
                        "content_hash": "0" * 64,
                        "current_rev": 3,
                        "updated_at": "2026-06-08T00:00:00Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = CitadelConfig(
        github_sync_state_path=str(tmp_path / "missing_github.json"),
        obsidian_sync_state_path=str(obsidian_path),
    )

    detected = detect_contribution_conflict(
        "# Architecture\nA different architecture decision entirely.",
        config=config,
    )

    assert detected is not None
    assert detected.kind == "contribution_vs_obsidian_note"
    assert "Team/Architecture.md" in detected.summary
    assert detected.side_b.timestamp == "2026-06-08T00:00:00Z"


def test_detection_without_state_files_returns_none(tmp_path: Path) -> None:
    config = CitadelConfig(
        github_sync_state_path=str(tmp_path / "missing_github.json"),
        obsidian_sync_state_path=str(tmp_path / "missing_obsidian.json"),
    )

    assert detect_contribution_conflict("# Anything\nbody", config=config) is None
