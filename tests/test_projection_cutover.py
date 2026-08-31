from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

import pytest

from kb import projection_cutover
from kb.generation_backup import GenerationBackupError
from kb.projection_cutover import ProjectionCutoverError, prepare_projection_cutover


GENERATION = "generation-059"
OWNER = "2a868377-2ed0-4882-b536-7f43c099af3d"
ACTIVE_ID = "0d8aa28c-b6bd-5abc-b500-95085c38830b"
STALE_ID = "1e9f89ff-cc36-50bc-bbf3-c915b8c8fdf2"
DATA_ONLY_ID = "e70a9a75-c785-5d7f-bfd2-c2a6650e7079"
DATASET = "e75f1811-0b4d-5e8b-8cdc-13fcfb7d0d32"


class _SnapshotStore:
    def require_empty(self) -> None:
        return None

    def restore_collection(
        self,
        collection: str,
        snapshot_path: Path,
        *,
        expected_sha256: str,
    ) -> int:
        assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == expected_sha256
        return 0

    def delete_collection(self, collection: str) -> None:
        return None


def _sqlite_schema(
    root: Path,
    *,
    active_job_states: tuple[str, ...] = ("pending",),
    active_graph_receipt_states: tuple[str, ...] = ("pending",),
) -> None:
    database = root / "cognee-system/databases/cognee.db"
    lifecycle = root / "citadel-state/lifecycle.sqlite3"
    database.parent.mkdir(parents=True)
    lifecycle.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE data (
                id TEXT PRIMARY KEY,
                pipeline_status TEXT
            );
            CREATE TABLE pipeline_runs (
                id TEXT PRIMARY KEY,
                created_at TEXT,
                status TEXT,
                pipeline_run_id TEXT,
                pipeline_name TEXT,
                pipeline_id TEXT,
                dataset_id TEXT,
                run_info TEXT
            );
            CREATE TABLE dataset_database (
                owner_id TEXT,
                dataset_id TEXT PRIMARY KEY,
                vector_database_name TEXT NOT NULL,
                graph_database_name TEXT NOT NULL,
                vector_database_provider TEXT NOT NULL,
                graph_database_provider TEXT NOT NULL,
                graph_dataset_database_handler TEXT NOT NULL,
                vector_dataset_database_handler TEXT NOT NULL
            );
            INSERT INTO data VALUES
                ('0d8aa28cb6bd5abcb50095085c38830b', '{"add_pipeline":"done","cognify_pipeline":"old"}'),
                ('1e9f89ffcc3650bcbbf3c915b8c8fdf2', '{"cognify_pipeline":"old","other":{"keep":true}}'),
                ('e70a9a75c7855d7fbfd2c2a6650e7079', '{"other":"stale"}');
            INSERT INTO pipeline_runs VALUES
                ('run-cognify-1', '2026-08-17', 'STARTED', 'pipeline-run-1', 'cognify_pipeline', 'pipeline-1', 'dataset-1', '{"payload":"archive me"}'),
                ('run-cognify-2', '2026-08-18', 'COMPLETED', 'pipeline-run-2', 'cognify_pipeline', 'pipeline-1', 'dataset-1', '{"payload":"archive me too"}'),
                ('run-add', '2026-08-18', 'COMPLETED', 'pipeline-run-3', 'add_pipeline', 'pipeline-2', 'dataset-1', '{"payload":"keep active"}');
            INSERT INTO dataset_database VALUES
                ('2a868377-2ed0-4882-b536-7f43c099af3d', 'e75f1811-0b4d-5e8b-8cdc-13fcfb7d0d32', 'vector', 'e75f1811-0b4d-5e8b-8cdc-13fcfb7d0d32.lbug', 'qdrant', 'ladybug', 'ladybug', 'qdrant');
            """
        )
    with sqlite3.connect(lifecycle) as connection:
        connection.executescript(
            """
            CREATE TABLE source_revisions (
                source_revision_id TEXT PRIMARY KEY,
                tombstone INTEGER NOT NULL
            );
            CREATE TABLE source_heads (
                dataset TEXT NOT NULL,
                source_key TEXT NOT NULL,
                source_revision_id TEXT NOT NULL
            );
            CREATE TABLE projection_jobs (
                projection_job_id TEXT PRIMARY KEY,
                source_revision_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE projection_receipts (
                projection_receipt_id TEXT PRIMARY KEY,
                projection_job_id TEXT NOT NULL,
                source_revision_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                backend TEXT NOT NULL,
                state TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO source_revisions VALUES (?, ?)",
            [(ACTIVE_ID, 0), (STALE_ID, 1), (DATA_ONLY_ID, 0)],
        )
        connection.executemany(
            "INSERT INTO source_heads VALUES (?, ?, ?)",
            [("dataset", "active", ACTIVE_ID), ("dataset", "tombstone", STALE_ID)],
        )
        active_jobs = [
            (f"job-active-{index}", ACTIVE_ID, GENERATION, state)
            for index, state in enumerate(active_job_states, start=1)
        ]
        connection.executemany(
            "INSERT INTO projection_jobs VALUES (?, ?, ?, ?)",
            [*active_jobs, ("job-tombstone", STALE_ID, GENERATION, "completed")],
        )
        active_receipts = [
            (
                f"receipt-active-{index}",
                "job-active-1",
                ACTIVE_ID,
                GENERATION,
                "graph",
                state,
            )
            for index, state in enumerate(active_graph_receipt_states, start=1)
        ]
        connection.executemany(
            "INSERT INTO projection_receipts VALUES (?, ?, ?, ?, ?, ?)",
            [
                *active_receipts,
                (
                    "receipt-tombstone",
                    "job-tombstone",
                    STALE_ID,
                    GENERATION,
                    "graph",
                    "searchable",
                ),
            ],
        )


def _make_backup(
    tmp_path: Path,
    *,
    active_job_states: tuple[str, ...] = ("pending",),
    active_graph_receipt_states: tuple[str, ...] = ("pending",),
) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    _sqlite_schema(
        source,
        active_job_states=active_job_states,
        active_graph_receipt_states=active_graph_receipt_states,
    )
    database_dir = source / "cognee-system/databases" / OWNER
    database_dir.mkdir(parents=True)
    (database_dir / f"{DATASET}.lbug").write_bytes(b"old graph")
    (database_dir / f"{DATASET}.lbug.wal").write_bytes(b"old wal")
    (database_dir / "unmapped.lbug").write_bytes(b"unmapped graph")
    (source / "data-storage/source.txt").parent.mkdir(parents=True)
    (source / "data-storage/source.txt").write_text("retain", encoding="utf-8")
    (source / "citadel-state/auth.json").write_text('{"keep":true}\n', encoding="utf-8")

    backup = tmp_path / "backup"
    local = backup / "local"
    shutil.copytree(source, local)
    files = []
    for path in sorted(local.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(local).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    snapshot = backup / "qdrant/chunks.snapshot"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"snapshot")
    snapshot_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generation_id": GENERATION,
        "local_files": files,
        "qdrant_collections": [
            {
                "collection": "chunks",
                "artifact": "qdrant/chunks.snapshot",
                "point_count": 0,
                "size": snapshot.stat().st_size,
                "sha256": snapshot_hash,
            }
        ],
    }
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (backup / "manifest.sha256").write_text(f"{manifest_hash}  manifest.json\n", encoding="ascii")
    return backup, source, manifest_hash


def test_prepare_rejects_target_inside_backup(tmp_path: Path) -> None:
    backup, _source, manifest_hash = _make_backup(tmp_path)
    before = sorted(path.relative_to(backup) for path in backup.rglob("*"))
    with pytest.raises(ProjectionCutoverError, match="inside the backup"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=backup / "local" / "new-root",
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
        )
    with pytest.raises(ProjectionCutoverError, match="inside the backup"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=backup,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
        )
    after = sorted(path.relative_to(backup) for path in backup.rglob("*"))
    assert after == before


def test_prepare_rejects_manifest_hash_and_generation_mismatch(tmp_path: Path) -> None:
    backup, _source, manifest_hash = _make_backup(tmp_path)
    with pytest.raises(GenerationBackupError, match="manifest hash mismatch"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=tmp_path / "target-hash",
            generation_id=GENERATION,
            manifest_sha256="0" * 64,
            snapshot_store=_SnapshotStore(),
        )
    with pytest.raises(GenerationBackupError, match="generation ID mismatch"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=tmp_path / "target-generation",
            generation_id="generation-other",
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )


def test_prepare_cuts_over_projection_and_is_idempotent(tmp_path: Path) -> None:
    backup, source, manifest_hash = _make_backup(tmp_path)
    lifecycle_path = backup / "local/citadel-state/lifecycle.sqlite3"
    lifecycle_before = lifecycle_path.read_bytes()
    source_lifecycle_path = source / "citadel-state/lifecycle.sqlite3"
    source_lifecycle_before = source_lifecycle_path.read_bytes()
    target = tmp_path / "target"
    result = prepare_projection_cutover(
        backup_root=backup,
        target_root=target,
        generation_id=GENERATION,
        manifest_sha256=manifest_hash,
        snapshot_store=_SnapshotStore(),
    )

    assert result["archived_pipeline_run_count"] == 2
    assert result["cleared_data_count"] == 1
    assert result["remapped_dataset_count"] == 1
    assert (target / "data-storage/source.txt").read_text(encoding="utf-8") == "retain"
    assert not (target / "cognee-system/databases" / OWNER / f"{DATASET}.lbug").exists()
    assert (target / "graph-archive" / OWNER / f"{DATASET}.lbug").read_bytes() == b"old graph"
    assert (target / "graph-archive" / OWNER / f"{DATASET}.lbug.wal").read_bytes() == b"old wal"
    assert (target / "graph-archive" / OWNER / "unmapped.lbug").read_bytes() == b"unmapped graph"

    with sqlite3.connect(target / "cognee-system/databases/cognee.db") as connection:
        active_status = json.loads(
            connection.execute("SELECT pipeline_status FROM data WHERE id LIKE '0d8a%'").fetchone()[0]
        )
        stale_status = json.loads(
            connection.execute("SELECT pipeline_status FROM data WHERE id LIKE '1e9f%'").fetchone()[0]
        )
        archive_rows = connection.execute(
            "SELECT id, pipeline_name FROM pipeline_runs_cognify_archive ORDER BY id"
        ).fetchall()
        active_runs = connection.execute(
            "SELECT id, pipeline_name FROM pipeline_runs ORDER BY id"
        ).fetchall()
        graph_name = connection.execute(
            "SELECT graph_database_name FROM dataset_database"
        ).fetchone()[0]
    assert active_status == {"add_pipeline": "done"}
    assert stale_status == {"cognify_pipeline": "old", "other": {"keep": True}}
    assert archive_rows == [("run-cognify-1", "cognify_pipeline"), ("run-cognify-2", "cognify_pipeline")]
    assert active_runs == [("run-add", "add_pipeline")]
    assert GENERATION in graph_name
    assert graph_name.endswith(".lbug")
    assert lifecycle_path.read_bytes() == lifecycle_before
    assert source_lifecycle_path.read_bytes() == source_lifecycle_before
    assert (target / "citadel-state/lifecycle.sqlite3").read_bytes() == lifecycle_before
    with sqlite3.connect(target / "citadel-state/lifecycle.sqlite3") as connection:
        lifecycle_states = connection.execute(
            """
            SELECT revisions.source_revision_id, jobs.state, receipts.state
            FROM source_heads AS heads
            JOIN source_revisions AS revisions
              ON revisions.source_revision_id = heads.source_revision_id
            JOIN projection_jobs AS jobs
              ON jobs.source_revision_id = revisions.source_revision_id
             AND jobs.generation_id = ?
            JOIN projection_receipts AS receipts
              ON receipts.projection_job_id = jobs.projection_job_id
             AND receipts.backend = 'graph'
            ORDER BY revisions.source_revision_id
            """,
            (GENERATION,),
        ).fetchall()
    assert lifecycle_states == [
        (ACTIVE_ID, "pending", "pending"),
        (STALE_ID, "completed", "searchable"),
    ]
    receipt_path = target / "citadel-state/projection-cutover.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["manifest_sha256"] == manifest_hash
    assert receipt["old_new_graph_paths"]
    rerun = prepare_projection_cutover(
        backup_root=backup,
        target_root=target,
        generation_id=GENERATION,
        manifest_sha256=manifest_hash,
        snapshot_store=_SnapshotStore(),
    )
    assert rerun["idempotent"] is True
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt

def test_prepare_rejects_completed_active_projection_before_graph_archive(
    tmp_path: Path,
) -> None:
    backup, source, manifest_hash = _make_backup(
        tmp_path,
        active_job_states=("completed",),
        active_graph_receipt_states=("searchable",),
    )
    target = tmp_path / "target"
    target.mkdir()
    graph_path = source / "cognee-system/databases" / OWNER / f"{DATASET}.lbug"
    graph_before = graph_path.read_bytes()
    with pytest.raises(ProjectionCutoverError, match="lifecycle projection precondition"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=target,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )
    assert list(target.iterdir()) == []
    assert graph_path.read_bytes() == graph_before
    assert (backup / "local/citadel-state/lifecycle.sqlite3").read_bytes() == (
        source / "citadel-state/lifecycle.sqlite3"
    ).read_bytes()


@pytest.mark.parametrize(
    "active_graph_receipt_states",
    [(), ("pending", "pending")],
    ids=["missing", "duplicate"],
)
def test_prepare_rejects_missing_or_duplicate_active_graph_receipt(
    tmp_path: Path,
    active_graph_receipt_states: tuple[str, ...],
) -> None:
    backup, source, manifest_hash = _make_backup(
        tmp_path,
        active_graph_receipt_states=active_graph_receipt_states,
    )
    target = tmp_path / "target"
    target.mkdir()
    graph_path = source / "cognee-system/databases" / OWNER / f"{DATASET}.lbug"
    graph_before = graph_path.read_bytes()
    with pytest.raises(ProjectionCutoverError, match="lifecycle projection precondition"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=target,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )
    assert list(target.iterdir()) == []
    assert graph_path.read_bytes() == graph_before




def test_prepare_rejects_symlink_target(tmp_path: Path) -> None:
    backup, _source, manifest_hash = _make_backup(tmp_path)
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    symlink_target = tmp_path / "target"
    symlink_target.symlink_to(real_target, target_is_directory=True)
    with pytest.raises(GenerationBackupError, match="symbolic link"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=symlink_target,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )


def test_prepare_rejects_mismatched_existing_receipt(tmp_path: Path) -> None:
    backup, _source, manifest_hash = _make_backup(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    state = target / "citadel-state"
    state.mkdir()
    (state / "projection-cutover.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": "other",
                "manifest_sha256": manifest_hash,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProjectionCutoverError, match="receipt"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=target,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )


def test_prepare_failure_keeps_target_unmodified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup, _source, manifest_hash = _make_backup(tmp_path)
    target = tmp_path / "target"
    target.mkdir()

    def fail_receipt(*_args: object, **_kwargs: object) -> Path:
        raise ProjectionCutoverError("receipt write failed")

    monkeypatch.setattr(projection_cutover, "_write_receipt", fail_receipt)
    with pytest.raises(ProjectionCutoverError, match="receipt write failed"):
        prepare_projection_cutover(
            backup_root=backup,
            target_root=target,
            generation_id=GENERATION,
            manifest_sha256=manifest_hash,
            snapshot_store=_SnapshotStore(),
        )
    assert list(target.iterdir()) == []
