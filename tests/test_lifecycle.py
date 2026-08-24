"""Public contract tests for Citadel's durable source and projection lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from kb.lifecycle import (
    CaptureContext,
    LifecycleConflictError,
    LifecycleNotFoundError,
    LifecycleRequeueDriftError,
    LifecycleSchemaError,
    LifecycleStore,
    ProjectionLeaseError,
    ProjectionRequest,
)


T0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _crash_accept_process(path: str, fault_stage: str) -> None:
    def crash(stage: str) -> None:
        if stage == fault_stage:
            os._exit(77)

    store = LifecycleStore(path, fault_injector=crash)
    store.accept_source(
        b"process crash acceptance",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:process-crash",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="crash-test",
            capture_run_id="run-crash",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )


@pytest.mark.parametrize(
    "table_name",
    ["source_revisions", "projection_jobs", "projection_receipts"],
)
def test_operation_rejects_unknown_record_schema_versions(
    tmp_path: Path,
    table_name: str,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    accepted = store.accept_source(
        b"schema version fixture",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:schema-version",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="run-schema",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE {table_name} SET schema_version = 2")

    with pytest.raises(LifecycleSchemaError, match="record schema 2"):
        store.get_operation(accepted.projection_job_id)


def test_accept_source_persists_revision_job_and_receipts_in_one_operation(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="seat:alice",
        source_key="manual:alice:note-1",
        source_locator="citadel://manual/note-1",
        media_type="text/plain",
        capture_actor_id="alice",
        capture_run_id="run-1",
        captured_at=T0,
        metadata={"tags": ["manual", "architecture"], "session_id": "session-1"},
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    accepted = store.accept_source(
        b"retained lifecycle evidence",
        capture=capture,
        projection=projection,
        now=T0,
    )

    assert accepted.accepted is True
    assert accepted.operation.state == "pending"
    assert accepted.operation.source_revision.source_revision_id == accepted.source_revision_id
    assert accepted.operation.job.projection_job_id == accepted.projection_job_id
    assert accepted.operation.source_revision.previous_revision_id is None
    assert accepted.operation.source_revision.capture_metadata == {
        "tags": ["manual", "architecture"],
        "session_id": "session-1",
    }
    assert accepted.operation.source_revision.retained_content_ref.startswith("citadel-sqlite:")
    assert store.read_retained_content(accepted.source_revision_id) == b"retained lifecycle evidence"
    assert LifecycleStore(store.path).get_operation(
        accepted.projection_job_id
    ).source_revision.capture_metadata == accepted.operation.source_revision.capture_metadata
    assert (
        store.get_current_revision("seat:alice", "manual:alice:note-1").source_revision_id
        == accepted.source_revision_id
    )
    assert [receipt.backend for receipt in accepted.operation.receipts] == [
        "relational",
        "vector",
        "graph",
    ]
    assert [receipt.provider for receipt in accepted.operation.receipts] == [
        "sqlite",
        "qdrant",
        "ladybug",
    ]
    assert {receipt.state for receipt in accepted.operation.receipts} == {"pending"}


def test_lexical_search_reads_current_retained_heads_before_projection(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    capture = CaptureContext(
        dataset="central",
        source_key="manual:current-head",
        source_locator="citadel://manual/current-head",
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="run-1",
        captured_at=T0,
        metadata={"title": "Current head"},
    )

    store.accept_source(
        b"old content without the target phrase",
        capture=capture,
        projection=projection,
        now=T0,
    )
    current = store.accept_source(
        b"current content contains the target phrase",
        capture=capture,
        projection=projection,
        now=T0,
    )

    results = store.lexical_search(
        "target phrase",
        dataset="central",
        projection=projection,
    )

    assert len(results) == 1
    assert results[0]["document_id"] == current.source_revision_id
    assert results[0]["_lifecycle"]["backend"] == "lexical"
    assert results[0]["_lifecycle"]["vector_state"] == "pending"
    assert results[0]["metadata"]["source_locator"] == "citadel://manual/current-head"
    assert results[0]["references"][0]["document_id"] == current.source_revision_id

    store.accept_tombstone(
        reason="removed for test",
        capture=capture,
        projection=projection,
        now=T0,
    )
    assert store.lexical_search(
        "target phrase",
        dataset="central",
        projection=projection,
    ) == []


def test_duplicate_submission_reuses_revision_job_and_receipts(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="github:masumi-network/Citadel:issue:256",
        source_locator="https://github.com/masumi-network/Citadel/pull/256",
        media_type="text/markdown",
        capture_actor_id="github-sync",
        capture_run_id="sync-1",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    first = store.accept_source(
        b"same retained source",
        capture=capture,
        projection=projection,
        now=T0,
    )
    duplicate = store.accept_source(
        b"same retained source",
        capture=capture,
        projection=projection,
        now=T0,
    )

    assert duplicate.source_revision_id == first.source_revision_id
    assert duplicate.projection_job_id == first.projection_job_id
    assert [item.projection_receipt_id for item in duplicate.operation.receipts] == [
        item.projection_receipt_id for item in first.operation.receipts
    ]
    assert store.census().source_revisions == 1
    assert store.census().current_sources == 1
    assert store.census().projection_jobs == 1
    assert store.census().projection_receipts == 3


def test_deterministic_ids_preserve_dataset_and_source_key_boundaries(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    first = store.accept_source(
        b"same retained source",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:x",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id=None,
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    second = store.accept_source(
        b"same retained source",
        capture=CaptureContext(
            dataset="seat",
            source_key="alice:manual:x",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id=None,
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )

    assert second.source_revision_id != first.source_revision_id
    assert second.operation.source_revision.dataset == "seat"
    assert second.operation.source_revision.source_key == "alice:manual:x"
    assert store.census().source_revisions == 2


def test_new_revision_moves_head_and_stales_predecessor_receipts(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="github:masumi-network/Citadel:issue:128",
        source_locator="https://github.com/masumi-network/Citadel/issues/128",
        media_type="text/markdown",
        capture_actor_id="github-sync",
        capture_run_id="sync-2",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    first = store.accept_source(
        b"issue body revision one",
        capture=capture,
        projection=projection,
        now=T0,
    )
    second = store.accept_source(
        b"issue body revision two",
        capture=capture,
        projection=projection,
        now=T0,
    )

    assert second.source_revision_id != first.source_revision_id
    assert second.operation.source_revision.previous_revision_id == first.source_revision_id
    assert (
        store.get_current_revision(capture.dataset, capture.source_key).source_revision_id
        == second.source_revision_id
    )
    predecessor = store.get_operation(first.projection_job_id)
    assert predecessor.state == "stale"
    assert {receipt.state for receipt in predecessor.receipts} == {"stale"}
    assert store.census().source_revisions == 2
    assert store.census().current_sources == 1
    assert store.census().projection_jobs == 2
    assert store.census().projection_receipts == 6


def test_reverting_to_prior_content_reactivates_prior_revision(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="manual:revertible",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="run-revert",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    first = store.accept_source(b"revision A", capture=capture, projection=projection, now=T0)
    second = store.accept_source(b"revision B", capture=capture, projection=projection, now=T0)
    reverted = store.accept_source(b"revision A", capture=capture, projection=projection, now=T0)

    assert reverted.source_revision_id == first.source_revision_id
    assert (
        store.get_current_revision(capture.dataset, capture.source_key).source_revision_id
        == first.source_revision_id
    )
    assert reverted.operation.state == "pending"
    assert {receipt.state for receipt in reverted.operation.receipts} == {"pending"}
    assert store.get_operation(second.projection_job_id).state == "stale"
    assert store.census().source_revisions == 2
    assert store.census().current_sources == 1


def test_tombstone_is_a_new_retained_revision_with_projection_work(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="github:masumi-network/Citadel:issue:128",
        source_locator="https://github.com/masumi-network/Citadel/issues/128",
        media_type="text/markdown",
        capture_actor_id="github-sync",
        capture_run_id="sync-3",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    original = store.accept_source(
        b"issue body before erasure",
        capture=capture,
        projection=projection,
        now=T0,
    )

    tombstone = store.accept_tombstone(
        reason="upstream source removed",
        capture=capture,
        projection=projection,
        now=T0,
    )

    assert tombstone.source_revision_id != original.source_revision_id
    assert tombstone.operation.source_revision.tombstone is True
    assert tombstone.operation.source_revision.previous_revision_id == original.source_revision_id
    assert (
        store.get_current_revision(capture.dataset, capture.source_key).source_revision_id
        == tombstone.source_revision_id
    )
    assert b"upstream source removed" in store.read_retained_content(
        tombstone.source_revision_id
    )
    assert store.get_operation(original.projection_job_id).state == "stale"
    assert tombstone.operation.state == "pending"
    assert len(tombstone.operation.receipts) == 3


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_source_revision",
        "after_source_head",
        "after_projection_job",
        "after_projection_receipt:relational",
        "after_projection_receipt:vector",
        "after_projection_receipt:graph",
        "before_accept_commit",
    ],
)
def test_accept_source_rolls_back_every_precommit_fault(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    class InjectedFault(RuntimeError):
        pass

    def inject(stage: str) -> None:
        if stage == fault_stage:
            raise InjectedFault(stage)

    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path, fault_injector=inject)
    capture = CaptureContext(
        dataset="seat:alice",
        source_key="manual:atomic-fault",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="alice",
        capture_run_id="run-fault",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    with pytest.raises(InjectedFault, match=fault_stage):
        store.accept_source(
            b"must commit with its job or not at all",
            capture=capture,
            projection=projection,
            now=T0,
        )

    recovered = LifecycleStore(path)
    census = recovered.census()
    assert census.source_revisions == 0
    assert census.current_sources == 0
    assert census.projection_jobs == 0
    assert census.projection_receipts == 0


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_source_revision",
        "after_source_head",
        "after_projection_job",
        "after_projection_receipt:relational",
        "after_projection_receipt:vector",
        "after_projection_receipt:graph",
        "before_accept_commit",
    ],
)
def test_process_death_rolls_back_each_acceptance_stage(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_accept_process,
        args=(str(path), fault_stage),
    )

    process.start()
    process.join(timeout=10)

    assert process.exitcode == 77
    census = LifecycleStore(path).census()
    assert census.source_revisions == 0
    assert census.current_sources == 0
    assert census.projection_jobs == 0
    assert census.projection_receipts == 0


def test_projection_lease_tracks_each_backend_before_whole_job_success(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"projection state machine",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:projection-state",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-projection",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="cognee-1.4.1-qdrant-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )

    lease = store.claim_next_job(worker_id="worker-1", now=T0, lease_seconds=30)

    assert lease is not None
    assert lease.projection_job_id == accepted.projection_job_id
    assert lease.attempt == 1
    assert store.claim_next_job(worker_id="worker-2", now=T0, lease_seconds=30) is None

    for backend in ("relational", "vector", "graph"):
        running = store.begin_backend(lease, backend, now=T0)
        assert running.state == "running"
        assert running.attempt == 1

        completed = store.complete_backend(
            lease,
            backend,
            provider_operation_id=f"provider-{backend}-1",
            affected_ids=(f"{backend}-id-1",),
            affected_count=1,
            metadata={"verified_by": "contract-test"},
            now=T0,
        )
        assert completed.state == "completed"
        assert store.get_operation(accepted.projection_job_id).state != "searchable"

        searchable = store.mark_backend_searchable(lease, backend, now=T0)
        assert searchable.state == "searchable"

    operation = store.get_operation(accepted.projection_job_id)
    assert operation.state == "searchable"
    assert operation.job.state == "completed"
    assert operation.job.lease_id is None
    assert {receipt.state for receipt in operation.receipts} == {"searchable"}
    assert store.census().receipt_states == {"searchable": 3}


def test_expired_projection_lease_recovers_running_receipt_in_second_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    first_store = LifecycleStore(path)
    accepted = first_store.accept_source(
        b"recover after worker loss",
        capture=CaptureContext(
            dataset="central",
            source_key="github:masumi-network/Citadel:issue:247",
            source_locator="https://github.com/masumi-network/Citadel/issues/247",
            media_type="text/markdown",
            capture_actor_id="github-sync",
            capture_run_id="sync-recovery",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="cognee-1.4.1-qdrant-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    old_lease = first_store.claim_next_job(
        worker_id="worker-before-crash",
        now=T0,
        lease_seconds=30,
    )
    assert old_lease is not None
    first_store.begin_backend(old_lease, "vector", now=T0)

    recovered_store = LifecycleStore(path)
    recovered_lease = recovered_store.claim_next_job(
        worker_id="worker-after-crash",
        now=T0.replace(second=31),
        lease_seconds=30,
    )

    assert recovered_lease is not None
    assert recovered_lease.projection_job_id == accepted.projection_job_id
    assert recovered_lease.lease_id != old_lease.lease_id
    assert recovered_lease.attempt == 2
    recovered_vector = next(
        receipt
        for receipt in recovered_store.get_operation(accepted.projection_job_id).receipts
        if receipt.backend == "vector"
    )
    assert recovered_vector.state == "pending"
    assert recovered_vector.attempt == 1
    with pytest.raises(ProjectionLeaseError, match="no longer active"):
        first_store.begin_backend(old_lease, "graph", now=T0.replace(second=31))


def test_projection_failure_reschedules_with_bounded_error_and_backoff(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"retry provider operation",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:retry-provider",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="release-check",
            capture_run_id="run-retry",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="cognee-1.4.1-qdrant-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    lease = store.claim_next_job(worker_id="worker-1", now=T0, lease_seconds=30)
    assert lease is not None
    store.begin_backend(lease, "relational", now=T0)

    rescheduled = store.reschedule_job(
        lease,
        error_code="provider_timeout",
        error_message="x" * 2_000,
        now=T0,
        backoff_seconds=5,
    )

    assert rescheduled.state == "pending"
    assert rescheduled.lease_id is None
    assert rescheduled.last_error_code == "provider_timeout"
    assert rescheduled.last_error_message == "x" * 1_000
    assert store.next_wakeup_delay(now=T0) == pytest.approx(5)
    assert store.get_operation(accepted.projection_job_id).receipts[0].state == "pending"
    assert store.claim_next_job(
        worker_id="worker-2",
        now=T0.replace(second=4),
        lease_seconds=30,
    ) is None
    retry = store.claim_next_job(
        worker_id="worker-2",
        now=T0.replace(second=5),
        lease_seconds=30,
    )
    assert retry is not None
    assert retry.attempt == 2


def test_duplicate_job_rejects_changed_provider_identity(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="manual:provider-conflict",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="release-check",
        capture_run_id="run-provider-conflict",
        captured_at=T0,
    )
    original = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    store.accept_source(b"same source", capture=capture, projection=original, now=T0)

    changed_provider = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "pgvector",
            "graph": "ladybug",
        },
    )
    with pytest.raises(LifecycleConflictError, match="CITADEL_GENERATION_ID"):
        store.accept_source(
            b"same source",
            capture=capture,
            projection=changed_provider,
            now=T0,
        )


def test_generation_rejects_config_drift_before_accepting_a_new_source(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    providers = {
        "relational": "sqlite",
        "vector": "qdrant",
        "graph": "ladybug",
    }
    original = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers=providers,
    )
    store.accept_source(
        b"first source",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:first",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="run-config-1",
            captured_at=T0,
        ),
        projection=original,
        now=T0,
    )

    with pytest.raises(LifecycleConflictError, match="CITADEL_GENERATION_ID"):
        store.accept_source(
            b"second source",
            capture=CaptureContext(
                dataset="central",
                source_key="manual:second",
                source_locator=None,
                media_type="text/plain",
                capture_actor_id="test",
                capture_run_id="run-config-2",
                captured_at=T0,
            ),
            projection=ProjectionRequest(
                generation_id="generation-1",
                projection_version="projection-v1",
                config_digest="sha256:config-2",
                providers=providers,
            ),
            now=T0,
        )

    census = store.census()
    assert (census.source_revisions, census.projection_jobs, census.projection_receipts) == (
        1,
        1,
        3,
    )
    with pytest.raises(LifecycleNotFoundError):
        store.get_current_revision("central", "manual:second")


def test_generation_rejects_provider_drift_with_reused_digest(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="central",
        source_key="manual:first-provider",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="run-provider-1",
        captured_at=T0,
    )
    store.accept_source(
        b"first provider source",
        capture=capture,
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )

    with pytest.raises(LifecycleConflictError, match="CITADEL_GENERATION_ID"):
        store.accept_source(
            b"second provider source",
            capture=CaptureContext(
                **{
                    **capture.__dict__,
                    "source_key": "manual:second-provider",
                }
            ),
            projection=ProjectionRequest(
                generation_id="generation-1",
                projection_version="projection-v1",
                config_digest="sha256:config-1",
                providers={
                    "relational": "sqlite",
                    "vector": "pgvector",
                    "graph": "ladybug",
                },
            ),
            now=T0,
        )


def test_retrieval_binding_requires_matching_projection_config(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"retrieval config source",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:retrieval-config",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="run-retrieval-config",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    lease = store.claim_next_job(worker_id="worker-config", now=T0)
    assert lease is not None
    for backend in ("relational", "vector", "graph"):
        store.begin_backend(lease, backend, now=T0)
        store.complete_backend(lease, backend, affected_count=1, now=T0)
        store.mark_backend_searchable(lease, backend, now=T0)

    matching = store.retrieval_binding(
        accepted.source_revision_id,
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
    )
    drifted = store.retrieval_binding(
        accepted.source_revision_id,
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
    )

    assert matching is not None and matching.receipt is not None
    assert drifted is not None and drifted.current is True and drifted.receipt is None


def test_generation_rebuild_queues_only_current_heads_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    original_projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    first_capture = CaptureContext(
        dataset="central",
        source_key="github:repo:path:README.md",
        source_locator="https://github.com/example/repo/blob/main/README.md",
        media_type="text/markdown",
        capture_actor_id="github-sync",
        capture_run_id="sync-1",
        captured_at=T0,
    )
    stale = store.accept_source(
        b"old README",
        capture=first_capture,
        projection=original_projection,
        now=T0,
    )
    current = store.accept_source(
        b"current README",
        capture=first_capture,
        projection=original_projection,
        now=T0,
    )
    second = store.accept_source(
        b"current architecture",
        capture=CaptureContext(
            dataset="central",
            source_key="github:repo:path:docs/architecture.md",
            source_locator="https://github.com/example/repo/blob/main/docs/architecture.md",
            media_type="text/markdown",
            capture_actor_id="github-sync",
            capture_run_id="sync-1",
            captured_at=T0,
        ),
        projection=original_projection,
        now=T0,
    )
    target = ProjectionRequest(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )

    queued = store.queue_generation_rebuild(target, now=T0)
    replayed = LifecycleStore(path).queue_generation_rebuild(target, now=T0)

    assert {item.source_revision.source_revision_id for item in queued} == {
        current.source_revision_id,
        second.source_revision_id,
    }
    assert stale.source_revision_id not in {
        item.source_revision.source_revision_id for item in queued
    }
    assert [item.job.projection_job_id for item in replayed] == [
        item.job.projection_job_id for item in queued
    ]
    census = store.census()
    assert (census.source_revisions, census.current_sources) == (3, 2)
    assert (census.projection_jobs, census.projection_receipts) == (5, 15)
    generation = store.generation_census(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
    )
    assert generation.current_sources == 2
    assert generation.current_projection_jobs == 2
    assert generation.current_projection_receipts == 6
    assert generation.current_receipts_by_backend == {
        "graph": 2,
        "relational": 2,
        "vector": 2,
    }
    assert generation.current_searchable_by_backend == {}


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_rebuild_projection_job",
        "after_rebuild_projection_receipt:relational",
        "after_rebuild_projection_receipt:vector",
        "after_rebuild_projection_receipt:graph",
        "before_rebuild_commit",
    ],
)
def test_generation_rebuild_rolls_back_every_precommit_fault(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    class InjectedFault(RuntimeError):
        pass

    path = tmp_path / "lifecycle.sqlite3"
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    LifecycleStore(path).accept_source(
        b"retained rebuild source",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:rebuild-fault",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="rebuild-fault",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )

    def inject(stage: str) -> None:
        if stage == fault_stage:
            raise InjectedFault(stage)

    target = ProjectionRequest(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
        providers=projection.providers,
    )
    with pytest.raises(InjectedFault, match=fault_stage):
        LifecycleStore(path, fault_injector=inject).queue_generation_rebuild(
            target,
            now=T0,
        )

    recovered = LifecycleStore(path)
    census = recovered.census()
    assert (census.projection_jobs, census.projection_receipts) == (1, 3)
    target_census = recovered.generation_census(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
    )
    assert target_census.current_projection_jobs == 0
    assert target_census.current_projection_receipts == 0


def test_requeue_failed_projections_resets_only_active_current_heads(tmp_path: Path) -> None:
    # 2026-08-12: 235 jobs failed while the embedding engine was down. The
    # heal-on-recapture path never fires for unchanged content, so this manual
    # requeue is the only way back for stable sources.
    store = LifecycleStore(str(tmp_path / "lifecycle.db"))
    capture = CaptureContext(
        dataset="central",
        source_key="manual:requeue-case",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="run-1",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    accepted = store.accept_source(
        b"requeue me", capture=capture, projection=projection, now=T0
    )
    active = ProjectionRequest(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
        providers=projection.providers,
    )
    rebuilt = store.queue_generation_rebuild(active, now=T0)
    historical_active_job_id = rebuilt[0].job.projection_job_id
    current = store.accept_source(
        b"current revision", capture=capture, projection=active, now=T0
    )
    current_active_job_id = current.projection_job_id
    foreign = ProjectionRequest(
        generation_id="generation-foreign",
        projection_version=active.projection_version,
        config_digest=active.config_digest,
        providers=active.providers,
    )
    foreign_job_id = store.queue_generation_rebuild(foreign, now=T0)[
        0
    ].job.projection_job_id
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE projection_jobs SET state = 'failed'")
        connection.execute("UPDATE projection_receipts SET state = 'failed'")

    preview = store.failed_projection_candidates(active)
    assert preview == (current_active_job_id,)
    assert store.census().job_states["failed"] == 4
    active_census = store.generation_census(
        generation_id=active.generation_id,
        projection_version=active.projection_version,
        config_digest=active.config_digest,
    )
    assert active_census.current_job_states == {"failed": 1}
    assert active_census.current_receipt_states == {"failed": 3}

    assert store.requeue_failed_projections(
        active,
        expected_count=1,
        candidate_ids=preview,
        now=T0,
    ) == preview

    assert store.get_operation(accepted.projection_job_id).job.state == "failed"
    assert store.get_operation(historical_active_job_id).job.state == "failed"
    assert store.get_operation(foreign_job_id).job.state == "failed"
    operation = store.get_operation(current_active_job_id)
    assert operation.job.state == "pending"
    assert operation.job.attempt == 0
    assert {receipt.state for receipt in operation.receipts} == {"pending"}
    release = store.claim_next_job(
        worker_id="worker-2",
        generation_id=active.generation_id,
        projection_version=active.projection_version,
        config_digest=active.config_digest,
        now=T0,
        lease_seconds=30,
    )
    assert release is not None
    assert release.projection_job_id == current_active_job_id


def test_requeue_failed_projections_ignores_healthy_jobs(tmp_path: Path) -> None:
    store = LifecycleStore(str(tmp_path / "lifecycle.db"))
    capture = CaptureContext(
        dataset="central",
        source_key="manual:healthy-case",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="run-1",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    store.accept_source(b"leave me queued", capture=capture, projection=projection, now=T0)
    assert store.failed_projection_candidates(projection) == ()
    assert store.requeue_failed_projections(
        projection,
        expected_count=0,
        candidate_ids=(),
        now=T0,
    ) == ()


def test_requeue_failed_projections_rejects_preview_drift(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )

    def accept_failed(source_key: str) -> str:
        accepted = store.accept_source(
            source_key.encode(),
            capture=CaptureContext(
                dataset="central",
                source_key=source_key,
                source_locator=None,
                media_type="text/plain",
                capture_actor_id="test",
                capture_run_id=source_key,
                captured_at=T0,
            ),
            projection=projection,
            now=T0,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE projection_jobs SET state = 'failed' WHERE projection_job_id = ?",
                (accepted.projection_job_id,),
            )
            connection.execute(
                "UPDATE projection_receipts SET state = 'failed' WHERE projection_job_id = ?",
                (accepted.projection_job_id,),
            )
        return accepted.projection_job_id

    first_job_id = accept_failed("manual:first")
    preview = store.failed_projection_candidates(projection)
    assert preview == (first_job_id,)
    second_job_id = accept_failed("manual:second")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE projection_jobs SET state = 'pending' WHERE projection_job_id = ?",
            (first_job_id,),
        )
        connection.execute(
            "UPDATE projection_receipts SET state = 'pending' WHERE projection_job_id = ?",
            (first_job_id,),
        )
    assert store.failed_projection_candidates(projection) == (second_job_id,)

    with pytest.raises(LifecycleRequeueDriftError):
        store.requeue_failed_projections(
            projection,
            expected_count=1,
            candidate_ids=preview,
            now=T0,
        )
    assert store.get_operation(first_job_id).job.state == "pending"
    assert store.get_operation(second_job_id).job.state == "failed"


def test_requeue_failed_projections_compares_inside_immediate_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    original_candidates = store._failed_projection_candidates
    transaction_states: list[bool] = []

    def observed_candidates(
        connection: sqlite3.Connection,
        requested: ProjectionRequest,
    ) -> tuple[str, ...]:
        transaction_states.append(connection.in_transaction)
        return original_candidates(connection, requested)

    monkeypatch.setattr(store, "_failed_projection_candidates", observed_candidates)

    assert store.requeue_failed_projections(
        projection,
        expected_count=0,
        candidate_ids=(),
        now=T0,
    ) == ()
    assert transaction_states == [True]


def test_requeue_failed_projections_rolls_back_precommit_fault(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    store = LifecycleStore(path)
    accepted = store.accept_source(
        b"rollback recovery",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:rollback",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="rollback",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE projection_jobs SET state = 'failed'")
        connection.execute("UPDATE projection_receipts SET state = 'failed'")

    def fail_before_commit(stage: str) -> None:
        if stage == "before_requeue_commit":
            raise RuntimeError(stage)

    recovery = LifecycleStore(path, fault_injector=fail_before_commit)
    preview = recovery.failed_projection_candidates(projection)
    with pytest.raises(RuntimeError, match="before_requeue_commit"):
        recovery.requeue_failed_projections(
            projection,
            expected_count=1,
            candidate_ids=preview,
            now=T0,
        )
    operation = LifecycleStore(path).get_operation(accepted.projection_job_id)
    assert operation.job.state == "failed"
    assert {receipt.state for receipt in operation.receipts} == {"failed"}


def test_requeue_failed_projections_explicitly_rolls_back_on_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    store = LifecycleStore(path)
    accepted = store.accept_source(
        b"explicit rollback",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:explicit-rollback",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="explicit-rollback",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE projection_jobs SET state = 'failed'")
        connection.execute("UPDATE projection_receipts SET state = 'failed'")

    def fail_before_commit(stage: str) -> None:
        if stage == "before_requeue_commit":
            raise RuntimeError(stage)

    recovery = LifecycleStore(path, fault_injector=fail_before_commit)
    preview = recovery.failed_projection_candidates(projection)
    raw_connection = sqlite3.connect(path, isolation_level=None)
    raw_connection.row_factory = sqlite3.Row

    class NoImplicitRollbackConnection:
        @property
        def in_transaction(self) -> bool:
            return raw_connection.in_transaction

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> bool:
            return False

        def execute(self, *args, **kwargs):
            return raw_connection.execute(*args, **kwargs)

    monkeypatch.setattr(
        recovery,
        "_connect",
        lambda: NoImplicitRollbackConnection(),
    )
    try:
        with pytest.raises(RuntimeError, match="before_requeue_commit"):
            recovery.requeue_failed_projections(
                projection,
                expected_count=1,
                candidate_ids=preview,
                now=T0,
            )
        assert raw_connection.in_transaction is False
    finally:
        if raw_connection.in_transaction:
            raw_connection.execute("ROLLBACK")
        raw_connection.close()

    operation = LifecycleStore(path).get_operation(accepted.projection_job_id)
    assert operation.job.state == "failed"
    assert {receipt.state for receipt in operation.receipts} == {"failed"}


def test_projection_source_revision_states_tracks_active_and_completed_jobs(
    tmp_path: Path,
) -> None:
    # The stored-chunk census resolves missing cognee document ids against
    # this read (#286): a pending/running projection means "not written YET";
    # a completed-searchable one gets one exact census recheck before failure.
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="cognee-1.4.1-qdrant-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = store.accept_source(
        b"in-flight lookup",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:in-flight",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-in-flight",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    revision_id = accepted.source_revision_id

    # Pending job: in flight. Unknown ids and empty input never match.
    lookup_kwargs = {
        "generation_id": projection.generation_id,
        "projection_version": projection.projection_version,
        "config_digest": projection.config_digest,
    }
    assert store.projection_source_revision_states(
        [revision_id], **lookup_kwargs
    ) == ({revision_id}, set())
    assert store.projection_source_revision_states(
        ["no-such-id"], **lookup_kwargs
    ) == (set(), set())
    assert store.projection_source_revision_states([], **lookup_kwargs) == (
        set(),
        set(),
    )
    assert store.projection_source_revision_states(
        [revision_id],
        generation_id="other-generation",
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    ) == (set(), set())

    # Drive the job to completed-searchable through the real choreography.
    # The caller may recheck a stale census once; a still-missing id is fatal.
    lease = store.claim_next_job(worker_id="worker-1", now=T0, lease_seconds=30)
    assert lease is not None
    for backend in ("relational", "vector", "graph"):
        store.begin_backend(lease, backend, now=T0)
        store.complete_backend(lease, backend, affected_count=1, now=T0)
        store.mark_backend_searchable(lease, backend, now=T0)
    assert store.projection_source_revision_states(
        [revision_id], **lookup_kwargs
    ) == (set(), {revision_id})


def test_failed_missing_path_candidates_select_file_not_found_jobs(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    pathfile = store.accept_source(
        b"/private/tmp/claude-501/marker3_pathfile.txt",
        capture=CaptureContext(
            dataset="seat:citadel-dev-team",
            source_key="manual:marker3-pathfile",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-pathfile",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    other = store.accept_source(
        b"transient provider failure",
        capture=CaptureContext(
            dataset="seat:citadel-dev-team",
            source_key="manual:other-failure",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-other",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    first = store.claim_next_job(worker_id="w1", now=T0, lease_seconds=30)
    assert first is not None
    assert first.projection_job_id == pathfile.projection_job_id
    store.fail_job(
        first,
        error_code="FileNotFoundError",
        error_message="Storage directory does not exist: '/private/tmp/claude-501/x'",
        now=T0,
    )
    second = store.claim_next_job(worker_id="w1", now=T0, lease_seconds=30)
    assert second is not None
    assert second.projection_job_id == other.projection_job_id
    store.fail_job(
        second,
        error_code="ProjectionVerificationError",
        error_message="no chunks",
        now=T0,
    )
    candidates = store.failed_projection_records(
        projection, error_code="FileNotFoundError"
    )
    assert [item["projection_job_id"] for item in candidates] == [
        pathfile.projection_job_id
    ]
    assert candidates[0]["source_key"] == "manual:marker3-pathfile"
    assert candidates[0]["dataset"] == "seat:citadel-dev-team"
    assert candidates[0]["error_code"] == "FileNotFoundError"
