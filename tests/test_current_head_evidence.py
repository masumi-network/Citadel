"""Exact current-head lifecycle evidence used by release acceptance."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any

from kb.config import CitadelConfig
from kb.lifecycle import CaptureContext, LifecycleStore, ProjectionRequest
from kb.service import Citadel


T0 = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
PROJECTION = ProjectionRequest(
    generation_id="generation-release",
    projection_version="lifecycle-v1:cognee-1.4.1",
    config_digest="sha256:release-config",
    providers={
        "relational": "sqlite",
        "vector": "qdrant",
        "graph": "ladybug",
    },
)


def _accept(
    store: LifecycleStore,
    source_key: str,
    content: bytes,
    *,
    projection: ProjectionRequest = PROJECTION,
) -> Any:
    return store.accept_source(
        content,
        capture=CaptureContext(
            dataset="masumi-network",
            source_key=source_key,
            source_locator=f"https://github.com/{source_key}",
            media_type="text/markdown",
            capture_actor_id="github-sync",
            capture_run_id="frozen-ring-3",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )


def _make_searchable(store: LifecycleStore, projection_job_id: str) -> None:
    lease = store.claim_next_job(
        worker_id="release-fixture",
        generation_id=PROJECTION.generation_id,
        projection_version=PROJECTION.projection_version,
        config_digest=PROJECTION.config_digest,
        now=T0,
    )
    assert lease is not None
    assert lease.projection_job_id == projection_job_id
    for backend in ("relational", "vector", "graph"):
        store.begin_backend(lease, backend, now=T0)
        store.complete_backend(lease, backend, affected_count=1, now=T0)
        store.mark_backend_searchable(lease, backend, now=T0)


def _evidence(store: LifecycleStore, *source_keys: str) -> Any:
    return store.current_head_evidence(
        "masumi-network",
        list(source_keys),
        generation_id=PROJECTION.generation_id,
        projection_version=PROJECTION.projection_version,
        config_digest=PROJECTION.config_digest,
    )


def test_current_head_evidence_selects_only_active_github_revisions_in_order(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    digest_key = "github:masumi-network:daily-digest"
    readme_key = "github:masumi-network/Citadel:path:README.md"
    historical = _accept(store, digest_key, b"historical digest")
    _make_searchable(store, historical.projection_job_id)
    current_digest = _accept(store, digest_key, b"current digest")
    _make_searchable(store, current_digest.projection_job_id)
    current_readme = _accept(store, readme_key, b"current README")
    _make_searchable(store, current_readme.projection_job_id)

    result = _evidence(store, readme_key, digest_key)

    assert result.ok is True
    assert result.errors == ()
    assert [row.source_key for row in result.evidence] == [readme_key, digest_key]
    assert [row.source_revision_id for row in result.evidence] == [
        current_readme.source_revision_id,
        current_digest.source_revision_id,
    ]
    assert historical.source_revision_id not in {
        row.source_revision_id for row in result.evidence
    }
    for row in result.evidence:
        assert row.state == "searchable"
        assert row.generation_id == PROJECTION.generation_id
        assert row.projection_version == PROJECTION.projection_version
        assert row.config_digest == PROJECTION.config_digest
        assert [receipt.backend for receipt in row.receipts] == [
            "relational",
            "vector",
            "graph",
        ]
        assert [receipt.state for receipt in row.receipts] == ["searchable"] * 3

    stale = store.get_operation(historical.projection_job_id)
    assert stale.state == "stale"
    assert {receipt.state for receipt in stale.receipts} == {"stale"}


def test_current_head_evidence_duplicate_fails_without_database_evidence(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    source_key = "github:masumi-network:daily-digest"
    accepted = _accept(store, source_key, b"digest")
    _make_searchable(store, accepted.projection_job_id)

    result = _evidence(store, source_key, source_key)

    assert result.ok is False
    assert result.evidence == ()
    assert [(error.code, error.source_key) for error in result.errors] == [
        ("SOURCE_KEY_DUPLICATE", source_key)
    ]


def test_current_head_evidence_reports_missing_and_tombstoned_heads_in_order(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    tombstoned_key = "github:masumi-network/Citadel:path:deleted.md"
    _accept(store, tombstoned_key, b"before deletion")
    store.accept_tombstone(
        reason="upstream file deleted",
        capture=CaptureContext(
            dataset="masumi-network",
            source_key=tombstoned_key,
            source_locator=None,
            media_type="text/markdown",
            capture_actor_id="github-sync",
            capture_run_id="frozen-ring-3",
            captured_at=T0,
        ),
        projection=PROJECTION,
        now=T0,
    )

    result = _evidence(store, "github:missing:path:none.md", tombstoned_key)

    assert result.ok is False
    assert result.evidence == ()
    assert [error.code for error in result.errors] == [
        "CURRENT_HEAD_MISSING",
        "CURRENT_HEAD_TOMBSTONED",
    ]


def test_current_head_evidence_rejects_cross_wired_source_head(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    requested_key = "github:masumi-network/Citadel:path:requested.md"
    foreign_key = "github:masumi-network/Citadel:path:foreign.md"
    _accept(store, requested_key, b"requested source")
    foreign = _accept(store, foreign_key, b"foreign source")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE source_heads
            SET source_revision_id = ?
            WHERE dataset = 'masumi-network' AND source_key = ?
            """,
            (foreign.source_revision_id, requested_key),
        )

    result = _evidence(store, requested_key)

    assert result.ok is False
    assert result.evidence == ()
    assert [(error.code, error.source_key) for error in result.errors] == [
        ("CURRENT_HEAD_MISSING", requested_key)
    ]


def test_current_head_evidence_is_diagnostic_complete_but_fails_partial_result(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    searchable_key = "github:masumi-network/Citadel:path:searchable.md"
    pending_key = "github:masumi-network/Citadel:path:pending.md"
    searchable = _accept(store, searchable_key, b"searchable")
    _make_searchable(store, searchable.projection_job_id)
    _accept(store, pending_key, b"pending")

    result = _evidence(store, searchable_key, "github:missing:path:none.md", pending_key)

    assert result.ok is False
    assert [row.source_key for row in result.evidence] == [searchable_key]
    assert [(error.source_key, error.code) for error in result.errors] == [
        ("github:missing:path:none.md", "CURRENT_HEAD_MISSING"),
        (pending_key, "RECEIPT_NOT_SEARCHABLE"),
    ]


def test_current_head_evidence_reports_missing_current_job(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    source_key = "github:masumi-network/Citadel:path:jobless.md"
    accepted = _accept(store, source_key, b"jobless")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE projection_job_id = ?",
            (accepted.projection_job_id,),
        )
        connection.execute(
            "DELETE FROM projection_jobs WHERE projection_job_id = ?",
            (accepted.projection_job_id,),
        )

    result = _evidence(store, source_key)

    assert result.ok is False
    assert result.errors[0].code == "CURRENT_JOB_MISSING"


def test_current_head_evidence_reports_projection_identity_mismatch(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    source_key = "github:masumi-network/Citadel:path:mismatch.md"
    accepted = _accept(store, source_key, b"mismatch")

    result = store.current_head_evidence(
        "masumi-network",
        [source_key],
        generation_id="another-generation",
        projection_version=PROJECTION.projection_version,
        config_digest=PROJECTION.config_digest,
    )

    assert result.ok is False
    assert result.errors[0].code == "CURRENT_JOB_MISMATCH"
    assert result.errors[0].projection_job_ids == (accepted.projection_job_id,)


def test_current_head_evidence_reports_ambiguous_current_job(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    source_key = "github:masumi-network/Citadel:path:ambiguous.md"
    accepted = _accept(store, source_key, b"ambiguous")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("ALTER TABLE projection_jobs RENAME TO projection_jobs_unique")
        connection.execute(
            "CREATE TABLE projection_jobs AS SELECT * FROM projection_jobs_unique"
        )
        row = connection.execute(
            "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
            (accepted.projection_job_id,),
        ).fetchone()
        assert row is not None
        columns = list(row.keys())
        values = [row[column] for column in columns]
        values[columns.index("projection_job_id")] = "ambiguous-second-job"
        values[columns.index("idempotency_key")] = "ambiguous-second-idempotency-key"
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO projection_jobs VALUES ({placeholders})",
            values,
        )

    result = _evidence(store, source_key)

    assert result.ok is False
    assert result.errors[0].code == "CURRENT_JOB_AMBIGUOUS"
    assert set(result.errors[0].projection_job_ids) == {
        accepted.projection_job_id,
        "ambiguous-second-job",
    }


def test_current_head_evidence_reports_receipt_set_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    source_key = "github:masumi-network/Citadel:path:missing-graph.md"
    accepted = _accept(store, source_key, b"missing graph receipt")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE projection_job_id = ? AND backend = 'graph'",
            (accepted.projection_job_id,),
        )

    result = _evidence(store, source_key)

    assert result.ok is False
    assert result.errors[0].code == "RECEIPT_SET_MISMATCH"
    assert result.errors[0].backend_states == {
        "relational": "pending",
        "vector": "pending",
    }


def test_current_head_evidence_bounds_corrupted_extra_receipts(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = LifecycleStore(path)
    source_key = "github:masumi-network/Citadel:path:extra-receipts.md"
    accepted = _accept(store, source_key, b"extra receipts")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM projection_receipts
            WHERE projection_job_id = ? AND backend = 'vector'
            """,
            (accepted.projection_job_id,),
        ).fetchone()
        assert row is not None
        columns = list(row.keys())
        for index in range(20):
            values = [row[column] for column in columns]
            values[columns.index("projection_receipt_id")] = f"extra-receipt-{index:02d}"
            values[columns.index("backend")] = f"extra-{index:02d}"
            values[columns.index("provider")] = "corrupt-provider"
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO projection_receipts VALUES ({placeholders})",
                values,
            )

    result = _evidence(store, source_key)

    assert result.ok is False
    assert result.errors[0].code == "RECEIPT_SET_MISMATCH"
    assert result.errors[0].backend_states == {
        "relational": "pending",
        "vector": "pending",
        "graph": "pending",
        "extra-00": "pending",
    }


def test_current_head_evidence_does_not_use_historical_searchable_projection(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    source_key = "github:masumi-network/Citadel:path:current-pending.md"
    historical = _accept(store, source_key, b"historical searchable")
    _make_searchable(store, historical.projection_job_id)
    current = _accept(store, source_key, b"current pending")

    result = _evidence(store, source_key)

    assert result.ok is False
    assert result.evidence == ()
    assert result.errors[0].code == "RECEIPT_NOT_SEARCHABLE"
    assert result.errors[0].projection_job_ids == (current.projection_job_id,)
    assert result.errors[0].backend_states == {
        "relational": "pending",
        "vector": "pending",
        "graph": "pending",
    }


def test_service_current_head_evidence_uses_active_projection_identity(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-release")
    monkeypatch.setenv("CITADEL_PROJECTION_VERSION", "lifecycle-v1:cognee-1.4.1")
    citadel = Citadel(
        CitadelConfig(
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=object(),  # type: ignore[arg-type]
    )
    assert citadel.lifecycle_store is not None
    projection = citadel._lifecycle_projection_request()
    source_key = "github:masumi-network:daily-digest"
    accepted = _accept(
        citadel.lifecycle_store,
        source_key,
        b"active configuration digest",
        projection=projection,
    )
    lease = citadel.lifecycle_store.claim_next_job(
        worker_id="release-fixture",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
        now=T0,
    )
    assert lease is not None
    assert lease.projection_job_id == accepted.projection_job_id
    for backend in ("relational", "vector", "graph"):
        citadel.lifecycle_store.begin_backend(lease, backend, now=T0)
        citadel.lifecycle_store.complete_backend(
            lease,
            backend,
            affected_count=1,
            now=T0,
        )
        citadel.lifecycle_store.mark_backend_searchable(lease, backend, now=T0)

    result = citadel.lifecycle_current_head_evidence(
        dataset="masumi-network",
        source_keys=[source_key],
    )

    assert result["ok"] is True
    assert result["evidence"][0]["config_digest"] == projection.config_digest
    assert [row["backend"] for row in result["evidence"][0]["receipts"]] == [
        "relational",
        "vector",
        "graph",
    ]
