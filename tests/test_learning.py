from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.conflicts import KnowledgeConflictStore
from kb.learning import LearningProcess
from kb.mesh import MeshState
from kb.models import IngestResult


class FakeCitadel:
    def __init__(self, config: CitadelConfig) -> None:
        self.config = config
        self.ingest_calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []
        self.accept = True

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or self.config.default_dataset
        if not self.accept:
            return IngestResult(False, "too_short", dataset, ())
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        return {"ok": True}


class FailingIngestCitadel(FakeCitadel):
    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        raise RuntimeError("cognee offline")


class FailingImproveCitadel(FakeCitadel):
    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        raise RuntimeError("llm unavailable")


def config_for(tmp_path: Path) -> CitadelConfig:
    return CitadelConfig(
        default_dataset="notes",
        github_sync_state_path=str(tmp_path / "github_state.json"),
        obsidian_sync_state_path=str(tmp_path / "obsidian.json"),
        conflicts_store_path=str(tmp_path / "conflicts.json"),
    )


async def test_learn_ingests_and_records_mesh_activity(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    citadel = FakeCitadel(config)
    mesh = MeshState()
    learning = LearningProcess(citadel, mesh=mesh)

    outcome = await learning.learn("Runbook: rotate keys", tags=["ops"])
    snapshot = await mesh.snapshot(config)

    assert outcome.ingest.accepted is True
    assert outcome.dataset == "notes"
    assert outcome.conflict is None
    assert outcome.improve is None
    assert citadel.ingest_calls[0]["tags"] == ["ops"]
    assert snapshot["stats"]["tracked_sources"] == 1
    assert snapshot["events"][0]["type"] == "ingest"


async def test_learn_forwards_stable_source_identity_to_lifecycle_ingest(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)
    citadel = FakeCitadel(config)
    learning = LearningProcess(citadel)

    await learning.learn(
        "Repository document",
        tier="light",
        source_key="github:masumi-network/Citadel:path:README.md",
        source_locator="https://github.com/masumi-network/Citadel/blob/abc/README.md",
        media_type="text/markdown",
        capture_actor_id="github-repo-content-sync",
        capture_run_id="sync-1",
    )

    call = citadel.ingest_calls[0]
    assert call["source_key"] == "github:masumi-network/Citadel:path:README.md"
    assert call["source_locator"].endswith("/blob/abc/README.md")
    assert call["media_type"] == "text/markdown"
    assert call["capture_actor_id"] == "github-repo-content-sync"
    assert call["capture_run_id"] == "sync-1"


async def test_learn_runs_improve_only_for_accepted_material(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    citadel = FakeCitadel(config)
    learning = LearningProcess(citadel)

    accepted = await learning.learn(
        "GitHub digest body",
        dataset="masumi-network",
        session_id="masumi-github-daily",
        run_improve=True,
    )
    citadel.accept = False
    rejected = await learning.learn("x", run_improve=True)

    assert accepted.improved is True
    assert accepted.improve == {"ok": True}
    assert citadel.improve_calls == [
        {"dataset": "masumi-network", "session_ids": ["masumi-github-daily"]}
    ]
    assert rejected.improve is None
    assert rejected.improved is False


async def test_light_tier_skips_enrichment_and_improve(tmp_path: Path, monkeypatch: Any) -> None:
    config = config_for(tmp_path)
    citadel = FakeCitadel(config)
    learning = LearningProcess(citadel)
    called = {"enrich": 0}

    def fake_enrich(_data: str) -> None:
        called["enrich"] += 1
        return None

    monkeypatch.setattr(learning, "_enrich", fake_enrich)

    outcome = await learning.learn(
        "Seat working memory",
        dataset="seat:alice",
        run_improve=True,
        tier="light",
    )

    assert outcome.ingest.accepted is True
    assert outcome.improve is None
    assert called["enrich"] == 0
    assert citadel.improve_calls == []


async def test_learn_keeps_going_when_improve_fails(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    citadel = FailingImproveCitadel(config)
    learning = LearningProcess(citadel)

    outcome = await learning.learn("Digest", run_improve=True)

    assert outcome.ingest.accepted is True
    assert outcome.improved is False
    assert outcome.improve_error == "llm unavailable"


async def test_learn_records_mesh_error_and_reraises_on_ingest_failure(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    mesh = MeshState()
    learning = LearningProcess(FailingIngestCitadel(config), mesh=mesh)

    with pytest.raises(RuntimeError):
        await learning.learn("Anything", operation="obsidian_sync")
    snapshot = await mesh.snapshot(config)

    assert snapshot["stats"]["since_restart"]["errors"] == 1
    assert snapshot["events"][0]["details"]["operation"] == "obsidian_sync"


async def test_learn_detects_and_records_knowledge_conflicts(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    Path(config.github_sync_state_path).write_text(
        json.dumps(
            {
                "org": "masumi-network",
                "last_digest_at": "2026-06-09T00:00:00Z",
                "last_digest": "## Recent commits\n- abc: ship the digest composer.\n",
            }
        ),
        encoding="utf-8",
    )
    citadel = FakeCitadel(config)
    mesh = MeshState()
    conflicts = KnowledgeConflictStore(config.conflicts_store_path)
    learning = LearningProcess(citadel, mesh=mesh, conflicts=conflicts)

    outcome = await learning.learn("# Recent commits\n- the composer was reverted.")
    snapshot = await mesh.snapshot(config)

    assert outcome.conflict is not None
    assert outcome.conflict["kind"] == "contribution_vs_repository_update"
    assert conflicts.open_count() == 1
    conflict_events = [event for event in snapshot["events"] if event["type"] == "conflict"]
    assert conflict_events[0]["details"]["conflict_id"] == outcome.conflict["id"]


async def test_learn_can_skip_conflict_detection(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    Path(config.github_sync_state_path).write_text(
        json.dumps({"org": "x", "last_digest": "## Recent commits\n- abc.\n"}),
        encoding="utf-8",
    )
    conflicts = KnowledgeConflictStore(config.conflicts_store_path)
    learning = LearningProcess(FakeCitadel(config), conflicts=conflicts)

    outcome = await learning.learn(
        "# Recent commits\n- different body.",
        detect_conflicts=False,
    )

    assert outcome.conflict is None
    assert conflicts.list() == []
