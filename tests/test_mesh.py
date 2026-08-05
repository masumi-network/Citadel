from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb.config import CitadelConfig
from kb.mesh import MeshState
from kb.models import FeedbackResult, IngestResult

CONFIG = CitadelConfig(tenant_id="test", default_dataset="notes")


async def test_record_ingest_adds_document_node_and_event() -> None:
    mesh = MeshState()
    result = IngestResult(True, "accepted", "notes", ("ops",))

    await mesh.record_ingest(CONFIG, result, data="Runbook: rotate keys", dataset="notes", tags=["ops"])
    snapshot = await mesh.snapshot(CONFIG)

    assert snapshot["stats"]["tracked_sources"] == 1
    document_nodes = [node for node in snapshot["nodes"] if node["type"] == "document"]
    assert document_nodes[0]["label"] == "Runbook: rotate keys"
    assert snapshot["events"][0]["type"] == "ingest"
    assert snapshot["events"][0]["details"]["dataset"] == "notes"
    assert snapshot["events"][0]["timeline"]["kind"] == "chunk_indexed"
    assert snapshot["stats"]["since_restart"]["indexed_chunks"] == 1


async def test_rejected_ingest_records_reject_event_without_document() -> None:
    mesh = MeshState()
    result = IngestResult(False, "too_short", "notes", ())

    await mesh.record_ingest(CONFIG, result, data="x", dataset="notes", tags=[])
    snapshot = await mesh.snapshot(CONFIG)

    assert snapshot["stats"]["tracked_sources"] == 0
    assert snapshot["events"][0]["type"] == "reject"
    assert snapshot["events"][0]["details"]["reason"] == "too_short"


async def test_record_repo_content_sync_adds_source_and_event() -> None:
    mesh = MeshState()
    result = {
        "org": "masumi-network",
        "checked_at": "2026-06-16T00:00:00Z",
        "repos_scanned": 2,
        "files_ingested": 5,
        "files_skipped": 1,
        "improved": True,
        "repositories": [
            {"repo": "masumi-network/sokosumi-cli", "ingested": 3, "skipped": 0},
        ],
    }

    await mesh.record_repo_content_sync(CONFIG, result)
    snapshot = await mesh.snapshot(CONFIG)

    repo_nodes = [node for node in snapshot["nodes"] if node["type"] == "repository"]
    assert any(node["label"] == "sokosumi-cli" for node in repo_nodes)
    assert snapshot["events"][-1]["type"] == "repo_content_sync"
    assert snapshot["events"][-1]["details"]["files_ingested"] == 5


async def test_revision_counter_increments_per_event() -> None:
    mesh = MeshState()

    await mesh.record_search(CONFIG, query="alpha", dataset="notes", result_count=1)
    await mesh.record_feedback(
        CONFIG,
        qa_id="qa-1",
        dataset="notes",
        result=FeedbackResult(recorded=True, improved=False),
    )
    await mesh.record_error(CONFIG, operation="search", error="boom")
    snapshot = await mesh.snapshot(CONFIG)

    assert snapshot["revision"] == 3
    assert snapshot["stats"]["since_restart"]["searches"] == 1
    assert snapshot["stats"]["since_restart"]["feedback"] == 1
    assert snapshot["stats"]["since_restart"]["errors"] == 1
    assert [event["id"] for event in snapshot["events"]] == [3, 2, 1]


async def test_events_deque_is_bounded_at_160() -> None:
    mesh = MeshState()

    for index in range(165):
        await mesh.record_error(CONFIG, operation="op", error=f"failure {index}")
    snapshot = await mesh.snapshot(CONFIG)

    assert len(snapshot["events"]) == 160
    assert snapshot["revision"] == 165
    assert snapshot["stats"]["since_restart"]["errors"] == 165
    # Newest first; the oldest five events fell off the bounded deque.
    assert snapshot["events"][0]["details"]["error"] == "failure 164"
    assert snapshot["events"][-1]["details"]["error"] == "failure 5"


async def test_error_details_are_clipped_to_280_characters() -> None:
    mesh = MeshState()

    await mesh.record_error(CONFIG, operation="ingest", error="x" * 500, dataset="notes")
    snapshot = await mesh.snapshot(CONFIG)

    assert len(snapshot["events"][0]["details"]["error"]) == 280
    assert snapshot["events"][0]["details"]["dataset"] == "notes"


async def test_subscribers_receive_published_events() -> None:
    mesh = MeshState()
    queue = mesh.subscribe()

    await mesh.record_search(CONFIG, query="alpha", dataset="notes", result_count=0)
    event = queue.get_nowait()

    assert event["type"] == "search"
    mesh.unsubscribe(queue)
    assert queue not in mesh.subscribers


async def test_snapshot_contains_base_indexes() -> None:
    mesh = MeshState()

    snapshot = await mesh.snapshot(CONFIG)

    assert {index["id"] for index in snapshot["indexes"]} == {
        "graph",
        "vector",
        "feedback",
        "global",
    }


async def test_snapshot_always_includes_central_dataset_node() -> None:
    mesh = MeshState()
    config = CitadelConfig(
        tenant_id="test",
        default_dataset="seat:alice",
        github_sync_dataset="masumi-network",
    )

    snapshot = await mesh.snapshot(config)

    dataset_labels = {
        node["label"]
        for node in snapshot["nodes"]
        if node["type"] == "dataset"
    }
    assert dataset_labels == {"seat:alice", "masumi-network"}
    central_id = next(
        node["id"]
        for node in snapshot["nodes"]
        if node["type"] == "dataset" and node["label"] == "masumi-network"
    )
    assert any(
        edge["source"] == central_id and edge["target"] == "index:graph"
        for edge in snapshot["edges"]
    )


async def test_rehydrate_seeds_graph_and_timestamp_not_counters() -> None:
    mesh = MeshState()

    await mesh.rehydrate(
        CONFIG,
        sources=[
            {
                "type": "github",
                "label": "GitHub / acme",
                "dataset": "notes",
                "documents": 4,
                "last_indexed_at": "2026-06-20T00:00:00Z",
                "repos": ["acme/one", "acme/two"],
            },
            {
                "type": "linear",
                "label": "Linear",
                "dataset": "notes",
                "documents": 6,
                "last_indexed_at": "2026-06-25T00:00:00Z",
                "repos": [],
            },
        ],
    )
    snapshot = await mesh.snapshot(CONFIG)

    # Counters are NOT seeded (that would double-count the github/repo data the
    # next live sync re-ingests); the graph projection + last_indexed_at carry it.
    assert snapshot["stats"]["tracked_sources"] == 0
    assert snapshot["stats"]["since_restart"]["indexed_chunks"] == 0
    assert snapshot["stats"]["last_indexed_at"] == "2026-06-25T00:00:00Z"
    # Graph projection is non-empty: source + repository nodes survive the "restart".
    source_nodes = [node for node in snapshot["nodes"] if node["type"] == "source"]
    repo_nodes = [node for node in snapshot["nodes"] if node["type"] == "repository"]
    assert len(source_nodes) == 2
    assert {node["label"] for node in repo_nodes} == {"one", "two"}
    graph_index = next(index for index in snapshot["indexes"] if index["id"] == "graph")
    assert graph_index["records"] > 0


async def test_rehydrate_baseline_then_live_ingest_does_not_double_count() -> None:
    mesh = MeshState()
    baseline = [
        {
            "type": "github",
            "label": "GitHub",
            "dataset": "notes",
            "documents": 5,
            "last_indexed_at": "2026-06-20T00:00:00Z",
            "repos": [],
        }
    ]

    await mesh.rehydrate(CONFIG, sources=baseline)
    # Second call must be a no-op: the _rehydrated guard prevents baselines stacking.
    await mesh.rehydrate(CONFIG, sources=baseline)
    await mesh.record_ingest(
        CONFIG,
        IngestResult(True, "accepted", "notes", ("ops",)),
        data="Runbook: rotate keys",
        dataset="notes",
        tags=["ops"],
    )
    snapshot = await mesh.snapshot(CONFIG)

    # Counters are not seeded, so a live ingest just adds 1 — the baseline never
    # double-counts the github/repo data the next live sync will re-ingest.
    assert snapshot["stats"]["tracked_sources"] == 1
    assert snapshot["stats"]["since_restart"]["indexed_chunks"] == 1


async def test_rehydrate_reads_state_files_and_tolerates_missing(tmp_path: Path) -> None:
    github = tmp_path / "github_sync_state.json"
    github.write_text(
        json.dumps(
            {
                "org": "acme",
                "last_checked_at": "2026-06-21T00:00:00Z",
                "repos": {"acme/one": {}, "acme/two": {}},
            }
        ),
        encoding="utf-8",
    )
    linear = tmp_path / "linear_sync_state.json"
    linear.write_text(
        json.dumps(
            {
                "issues": [{"id": 1}, {"id": 2}, {"id": 3}],
                "last_synced_at": "2026-06-24T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    config = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        github_sync_state_path=str(github),
        # Repo-content state file is intentionally absent — must be tolerated.
        repo_content_sync_state_path=str(tmp_path / "missing_repo_state.json"),
        linear_sync_state_path=str(linear),
    )
    mesh = MeshState()

    await mesh.rehydrate(config)
    snapshot = await mesh.snapshot(config)

    # Counters are not seeded; last_indexed_at + the graph projection carry the
    # persistent state. Missing repo-content file is tolerated (contributes nothing).
    assert snapshot["stats"]["tracked_sources"] == 0
    assert snapshot["stats"]["since_restart"]["indexed_chunks"] == 0
    assert snapshot["stats"]["last_indexed_at"] == "2026-06-24T00:00:00Z"
    source_nodes = [node for node in snapshot["nodes"] if node["type"] == "source"]
    assert source_nodes  # github + linear sources projected from persistent state


async def test_timeline_tracks_chunks_and_resume_filters() -> None:
    mesh = MeshState()

    await mesh.record_ingest(
        CONFIG,
        IngestResult(True, "accepted", "notes", ("ops",)),
        data="Runbook: rotate keys",
        dataset="notes",
        tags=["ops"],
    )
    await mesh.record_enrichment(
        CONFIG,
        dataset="notes",
        chunks=3,
        used_llm=False,
        reason="fallback",
    )
    await mesh.record_search(CONFIG, query="rotate", dataset="notes", result_count=2)

    resumed = await mesh.timeline(after_id=1, limit=1)
    chunk_events = await mesh.timeline(kind="chunk_indexed", limit=5)

    assert resumed["latest_event_id"] == 3
    assert resumed["truncated"] is True
    assert [event["id"] for event in resumed["events"]] == [3]
    assert [event["id"] for event in chunk_events["events"]] == [2, 1]
    assert chunk_events["stats"]["indexed_chunks"] == 4
    assert chunk_events["stats"]["last_indexed_at"] == chunk_events["events"][0]["created_at"]


# --- the dashboard reported the vault as empty -----------------------------
#
# 2026-07-31: /api/indexes reported documents=1 and indexed_chunks=1 against a
# real 15641 indexed / 308 tracked. Every MeshState counter is in-memory and
# rehydrate deliberately does not reseed them, so on a service that redeploys
# on every merge to main they measured uptime, not corpus.


@pytest.mark.asyncio
async def test_snapshot_reports_authoritative_corpus_totals() -> None:
    config = CitadelConfig()
    mesh = MeshState()
    mesh.documents = 1
    mesh.indexed_chunks = 1
    mesh.searches = 386

    snapshot = await mesh.snapshot(
        config, corpus={"ok": True, "tracked_sources": 308, "indexed_docs": 15641}
    )
    stats = snapshot["stats"]

    assert stats["tracked_sources"] == 308
    assert stats["nodes"] == 15641
    # No indexed_chunks at the top level: it only ever duplicated `nodes` (both
    # were corpus["indexed_docs"]), so it is removed rather than published wrong.
    assert "indexed_chunks" not in stats
    # The in-memory values are still available, correctly labelled.
    assert stats["since_restart"]["documents"] == 1
    assert stats["since_restart"]["indexed_chunks"] == 1
    # Activity counters are uptime-scoped, so they live under since_restart
    # too — a top-level "searches" reads as a lifetime total (#196).
    assert stats["since_restart"]["searches"] == 386


@pytest.mark.asyncio
async def test_snapshot_falls_back_when_corpus_is_unavailable() -> None:
    """_corpus_health fails soft and returns None counts; do not report None."""
    config = CitadelConfig()
    mesh = MeshState()
    mesh.documents = 4

    snapshot = await mesh.snapshot(
        config, corpus={"ok": True, "tracked_sources": None, "indexed_docs": None}
    )

    assert snapshot["stats"]["tracked_sources"] == 4


@pytest.mark.asyncio
async def test_snapshot_without_corpus_keeps_the_old_shape() -> None:
    config = CitadelConfig()
    mesh = MeshState()
    mesh.documents = 7

    snapshot = await mesh.snapshot(config)

    assert snapshot["stats"]["tracked_sources"] == 7


# --- /api/mesh published three counters that did not mean their names -------
#
# Verified live 2026-08-03. stats.edges and stats.since_restart.projection_edges
# were the SAME expression (len(self.edges)): the in-memory projection sat at
# the top level next to authoritative corpus figures, where it reads as a
# corpus total. It was 5511 against a real 131843 graph edges (24x off).
# Separately, stats.nodes and stats.indexed_chunks were BOTH assigned
# corpus["indexed_docs"] — equal by construction, and nothing on this surface
# ever counted a chunk. And stats.documents was corpus["tracked_sources"] (318:
# github + repo-content + linear tracked counts) while the durable corpus store
# held ~2876 rows — a name that promises a document count and delivers a
# source count.


@pytest.mark.asyncio
async def test_snapshot_edges_reports_the_real_graph_total_not_the_projection() -> None:
    config = CitadelConfig()
    mesh = MeshState()
    # A handful of in-memory projection edges, distinct from the real total below.
    for i in range(3):
        mesh.edges[f"e{i}"] = {"id": f"e{i}", "source": "a", "target": "b"}

    snapshot = await mesh.snapshot(
        config,
        corpus={
            "ok": True,
            "tracked_sources": 308,
            "indexed_docs": 15641,
            "indexed_edges": 131843,
        },
    )
    stats = snapshot["stats"]

    assert stats["edges"] == 131843
    # The in-memory projection is still available, correctly scoped, and
    # distinct from the real total above (the base graph seeds a few edges of
    # its own, so this asserts against the live in-memory count, not a magic
    # number).
    assert stats["since_restart"]["projection_edges"] == len(mesh.edges)
    assert stats["since_restart"]["projection_edges"] < stats["edges"]


@pytest.mark.asyncio
async def test_snapshot_edges_falls_back_to_the_projection_without_a_real_total() -> None:
    """No indexed_edges on the corpus payload (degraded read, or no corpus at
    all) — the projection is the best available number, same as before.

    Both branches are asserted against the SAME mesh, because the fallback
    assertion alone does not discriminate: before this change `edges` was
    unconditionally `len(self.edges)`, so `edges == len(mesh.edges)` held for
    every payload. Pinning that the real total wins when it is present is what
    makes the fallback a fallback rather than the only behaviour.
    """
    config = CitadelConfig()
    mesh = MeshState()
    for i in range(3):
        mesh.edges[f"e{i}"] = {"id": f"e{i}", "source": "a", "target": "b"}

    with_total = await mesh.snapshot(
        config,
        corpus={"ok": True, "tracked_sources": 308, "indexed_docs": 15641, "indexed_edges": 131843},
    )
    degraded = await mesh.snapshot(
        config, corpus={"ok": True, "tracked_sources": None, "indexed_docs": None}
    )

    assert with_total["stats"]["edges"] == 131843
    assert degraded["stats"]["edges"] == len(mesh.edges)
    assert degraded["stats"]["edges"] == degraded["stats"]["since_restart"]["projection_edges"]
    # The two disagree, so the second number is demonstrably the fallback and
    # not the same expression under a different corpus payload.
    assert degraded["stats"]["edges"] != with_total["stats"]["edges"]


@pytest.mark.asyncio
async def test_snapshot_drops_indexed_chunks_when_it_only_duplicates_nodes() -> None:
    """indexed_chunks was assigned the same corpus['indexed_docs'] as nodes,
    equal by construction. Removed, not renamed — the same call #206 made for
    failed_chunks: nothing on this surface counts chunks, so nothing may claim
    to. The honest restart-scoped accumulator stays under since_restart."""
    config = CitadelConfig()
    mesh = MeshState()
    mesh.indexed_chunks = 9

    snapshot = await mesh.snapshot(
        config, corpus={"ok": True, "tracked_sources": 308, "indexed_docs": 15641}
    )
    stats = snapshot["stats"]

    assert "indexed_chunks" not in stats
    assert stats["nodes"] == 15641
    assert stats["since_restart"]["indexed_chunks"] == 9


@pytest.mark.asyncio
async def test_snapshot_documents_field_is_named_for_what_it_measures() -> None:
    """The old `documents` field held corpus['tracked_sources'] — github repos
    + repo-content files + linear issues, a source count, not a document count
    (live: 318 tracked sources vs ~2876 durable corpus rows). Renamed to match
    what it actually reports."""
    config = CitadelConfig()
    mesh = MeshState()

    snapshot = await mesh.snapshot(
        config, corpus={"ok": True, "tracked_sources": 308, "indexed_docs": 15641}
    )
    stats = snapshot["stats"]

    assert stats["tracked_sources"] == 308
    assert "documents" not in stats


# --- activity counters are restart-scoped, and must say so (#196, #197) -----
#
# Measured live on 2026-08-02, a day with several redeploys: stats.searches
# and stats.since_restart.searches were both 294 — the top-level number was
# the since-restart number under a totals-shaped name. Separately, a
# "12 failed chunks" reading decomposed to 11 search timeouts + 1
# DatasetNotFoundError with zero ingestion failures: failed_chunks was the
# errors counter surfaced a second time, one increment per failed operation,
# none per chunk.


@pytest.mark.asyncio
async def test_activity_counters_publish_only_under_since_restart() -> None:
    """#196: a counter that resets on deploy must not sit at the top level of
    the stats payload, where every consumer reads it as a lifetime total."""
    mesh = MeshState()
    await mesh.record_search(CONFIG, query="rotate", dataset="notes", result_count=2)
    await mesh.record_feedback(
        CONFIG, qa_id="qa-1", dataset="notes", result=FeedbackResult(True, True)
    )
    await mesh.record_upgrade(CONFIG, dataset="notes", session_ids=None)
    await mesh.record_error(CONFIG, operation="search", error="boom")

    stats = (await mesh.snapshot(CONFIG))["stats"]

    for counter in ("searches", "feedback", "upgrades", "errors", "pending_chunks"):
        assert counter not in stats, f"{counter} is restart-scoped but top-level"
    since = stats["since_restart"]
    assert since["searches"] == 1
    assert since["feedback"] == 1
    assert since["upgrades"] == 1
    assert since["errors"] == 1
    # The window the counters cover is part of the payload, so a consumer can
    # tell a quiet vault from a recent deploy.
    assert since["started_at"]


@pytest.mark.asyncio
async def test_errors_is_the_single_failure_counter() -> None:
    """#197: failed_chunks was the errors counter incremented a second time in
    the same code path — record_error's event is the only one whose timeline
    status is "failed", so the two fields could never diverge and failed_chunks
    never counted a chunk. One counter, one name."""
    mesh = MeshState()
    await mesh.record_error(CONFIG, operation="search", error="timeout")
    await mesh.record_error(CONFIG, operation="evolve", error="DatasetNotFoundError")

    snapshot = await mesh.snapshot(CONFIG)
    timeline = await mesh.timeline()

    assert "failed_chunks" not in snapshot["stats"]
    assert "failed_chunks" not in timeline["stats"]
    assert snapshot["stats"]["since_restart"]["errors"] == 2
    assert timeline["stats"]["errors"] == 2
