from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

import pytest

import kb.generation_backup as generation_backup
from kb.cli import _backup_generation_create, _backup_generation_restore, build_parser
from kb.generation_backup import (
    GenerationBackupError,
    QdrantSnapshotStore,
    create_generation_backup,
    restore_generation_backup,
)
from kb.lite_runtime import acquire_single_instance_lock


class MemorySnapshotStore:
    def __init__(
        self,
        collections: dict[str, bytes],
        *,
        fail_collection: str | None = None,
    ) -> None:
        self.collections = dict(collections)
        self.restored: dict[str, bytes] = {}
        self.fail_collection = fail_collection

    def list_generation_collections(self, generation_id: str) -> list[str]:
        return sorted(self.collections)

    def download_collection_snapshot(
        self,
        collection: str,
        destination: Path,
    ) -> dict[str, Any]:
        payload = self.collections[collection]
        destination.write_bytes(payload)
        return {
            "snapshot_name": f"{collection}.snapshot",
            "point_count": len(payload),
        }

    def require_empty(self) -> None:
        if self.restored:
            raise GenerationBackupError("Qdrant restore target is not empty")

    def restore_collection(
        self,
        collection: str,
        snapshot_path: Path,
        *,
        expected_sha256: str,
    ) -> int:
        if collection == self.fail_collection:
            raise GenerationBackupError(f"forced restore failure for {collection}")
        payload = snapshot_path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        self.restored[collection] = payload
        return len(payload)

    def delete_collection(self, collection: str) -> None:
        self.restored.pop(collection, None)


def _sqlite(path: Path, table: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {table} (value TEXT PRIMARY KEY)")
        connection.execute(f"INSERT INTO {table} VALUES (?)", (value,))


def _lite_root(root: Path) -> None:
    _sqlite(root / "cognee-system/databases/cognee.db", "documents", "doc-1")
    _sqlite(root / "citadel-state/lifecycle.sqlite3", "revisions", "revision-1")
    (root / "cognee-system/databases/central_graph.lbug").write_bytes(b"ladybug")
    (root / "data-storage").mkdir(parents=True)
    (root / "data-storage/source.txt").write_text("retained source", encoding="utf-8")
    (root / "citadel-state/auth.json").write_text('{"key":"hash-only"}\n', encoding="utf-8")

@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout"),
    [(None, 900), ("1200", 1200)],
)
def test_qdrant_snapshot_store_passes_snapshot_timeout_to_client(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: str | None,
    expected_timeout: int,
) -> None:
    client_kwargs: dict[str, Any] = {}

    class FakeQdrantClient:
        def __init__(self, **kwargs: Any) -> None:
            client_kwargs.update(kwargs)

    monkeypatch.setattr(generation_backup, "QdrantClient", FakeQdrantClient)
    if configured_timeout is None:
        monkeypatch.delenv("CITADEL_QDRANT_SNAPSHOT_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CITADEL_QDRANT_SNAPSHOT_TIMEOUT_SECONDS", configured_timeout)

    QdrantSnapshotStore(url="http://qdrant:6333", api_key="secret")

    assert client_kwargs == {
        "url": "http://qdrant:6333",
        "api_key": "secret",
        "timeout": expected_timeout,
    }


@pytest.mark.parametrize(
    ("configured_timeout", "error"),
    [
        ("not-a-number", "must be a finite number"),
        ("0", "must be at least 1 second"),
    ],
)
def test_qdrant_snapshot_store_rejects_invalid_snapshot_timeout(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: str,
    error: str,
) -> None:
    class FakeQdrantClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(generation_backup, "QdrantClient", FakeQdrantClient)
    monkeypatch.setenv("CITADEL_QDRANT_SNAPSHOT_TIMEOUT_SECONDS", configured_timeout)

    with pytest.raises(GenerationBackupError, match=error):
        QdrantSnapshotStore(url="http://qdrant:6333", api_key="secret")



def test_parser_exposes_generation_backup_and_restore() -> None:
    parser = build_parser()

    create = parser.parse_args(["backup", "create", "/tmp/citadel-backup"])
    restore = parser.parse_args(
        ["backup", "restore", "/tmp/citadel-backup", "/tmp/citadel-restore"]
    )

    assert create.handler is _backup_generation_create
    assert restore.handler is _backup_generation_restore


def test_generation_backup_restores_every_provider_from_downloaded_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (
        "CITADEL_LITE_DATA_ROOT",
        "SYSTEM_ROOT_DIRECTORY",
        "DATA_ROOT_DIRECTORY",
        "CITADEL_STATE_DIRECTORY",
        "DB_PATH",
        "DB_NAME",
        "CITADEL_LIFECYCLE_STORE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    source_root = tmp_path / "source"
    _lite_root(source_root)
    snapshots = {
        "citadel_g_abc_DocumentChunk_text_1": b"chunk-snapshot",
        "citadel_g_abc_Entity_name_2": b"entity-snapshot",
    }
    source_store = MemorySnapshotStore(snapshots)
    backup_root = tmp_path / "generation-backup"

    manifest = create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=source_store,
    )

    assert [item["collection"] for item in manifest["qdrant_collections"]] == sorted(
        snapshots
    )
    assert manifest["local_files"]
    assert (backup_root / "local/cognee-system/databases/central_graph.lbug").read_bytes() == b"ladybug"
    assert (backup_root / "local/data-storage/source.txt").read_text(encoding="utf-8") == "retained source"
    assert json.loads((backup_root / "manifest.json").read_text(encoding="utf-8")) == manifest

    lifecycle_artifact = backup_root / "local/citadel-state/lifecycle.sqlite3"
    lifecycle_bytes = bytearray(lifecycle_artifact.read_bytes())
    lifecycle_bytes[24:28] = b"\x00\x00\x00\x00"
    lifecycle_artifact.write_bytes(lifecycle_bytes)
    for item in manifest["local_files"]:
        if item["path"] == "citadel-state/lifecycle.sqlite3":
            item["size"] = len(lifecycle_bytes)
            item["sha256"] = hashlib.sha256(lifecycle_bytes).hexdigest()
            break
    manifest_path = backup_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (backup_root / "manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  manifest.json\n",
        encoding="ascii",
    )

    restore_root = tmp_path / "restore"
    restore_store = MemorySnapshotStore({})
    result = restore_generation_backup(
        generation_id="generation-1",
        backup_root=backup_root,
        target_data_root=restore_root,
        snapshot_store=restore_store,
    )

    assert result["generation_id"] == "generation-1"
    assert restore_store.restored == snapshots
    restored_database = restore_root / "cognee-system/databases/cognee.db"
    restored_lifecycle = restore_root / "citadel-state/lifecycle.sqlite3"
    assert restored_database.read_bytes() == (
        backup_root / "local/cognee-system/databases/cognee.db"
    ).read_bytes()
    assert restored_lifecycle.read_bytes() == lifecycle_artifact.read_bytes()
    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM documents").fetchall() == [("doc-1",)]
    with sqlite3.connect(restored_lifecycle) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM revisions").fetchall() == [
            ("revision-1",)
        ]


def test_generation_restore_rejects_mutated_target_sqlite_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )
    restore_store = MemorySnapshotStore({})
    original_copy_sqlite = generation_backup._copy_sqlite

    def copy_and_mutate_target(source_path: Path, destination_path: Path) -> None:
        original_copy_sqlite(source_path, destination_path)
        if destination_path.name == "lifecycle.sqlite3":
            mutated = bytearray(destination_path.read_bytes())
            mutated[-1] ^= 1
            destination_path.write_bytes(mutated)

    monkeypatch.setattr(generation_backup, "_copy_sqlite", copy_and_mutate_target)

    with pytest.raises(
        GenerationBackupError,
        match="restored artifact digest mismatch: citadel-state/lifecycle.sqlite3",
    ):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=tmp_path / "restore",
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert not (tmp_path / "restore").exists()

def test_generation_backup_maps_configured_lite_roots_and_database_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured_root = tmp_path / "configured-volume"
    source_root = tmp_path / "source"
    system_root = configured_root / ".cognee_system"
    data_root = configured_root / ".data_storage"
    state_root = configured_root / "citadel-state"
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(configured_root))
    monkeypatch.setenv("SYSTEM_ROOT_DIRECTORY", str(system_root))
    monkeypatch.setenv("DATA_ROOT_DIRECTORY", str(data_root))
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(state_root))
    monkeypatch.setenv("DB_PATH", str(system_root / "databases"))
    monkeypatch.setenv("DB_NAME", "runtime.sqlite3")
    monkeypatch.setenv(
        "CITADEL_LIFECYCLE_STORE_PATH",
        str(state_root / "runtime-lifecycle.sqlite3"),
    )
    _sqlite(
        source_root / ".cognee_system/databases/runtime.sqlite3",
        "documents",
        "dot-root-doc",
    )
    _sqlite(
        source_root / "citadel-state/runtime-lifecycle.sqlite3",
        "revisions",
        "dot-root-revision",
    )
    (source_root / ".cognee_system/databases/central_graph.lbug").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (source_root / ".cognee_system/databases/central_graph.lbug").write_bytes(
        b"ladybug"
    )
    (source_root / ".data_storage/source.txt").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (source_root / ".data_storage/source.txt").write_text(
        "configured source",
        encoding="utf-8",
    )
    (source_root / "citadel-state/auth.json").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (source_root / "citadel-state/auth.json").write_text(
        '{"key":"hash-only"}\n',
        encoding="utf-8",
    )

    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )

    assert (backup_root / "local/cognee-system/databases/cognee.db").is_file()
    assert (backup_root / "local/citadel-state/lifecycle.sqlite3").is_file()
    assert not (backup_root / "local/.cognee_system").exists()
    assert not (backup_root / "local/.data_storage").exists()

    restore_root = tmp_path / "restore"
    restore_generation_backup(
        generation_id="generation-1",
        backup_root=backup_root,
        target_data_root=restore_root,
        snapshot_store=MemorySnapshotStore({}),
    )

    restored_database = restore_root / ".cognee_system/databases/runtime.sqlite3"
    restored_lifecycle = restore_root / "citadel-state/runtime-lifecycle.sqlite3"
    assert restored_database.is_file()
    assert restored_lifecycle.is_file()
    assert (restore_root / "citadel-state/lite-runtime.lock").is_file()
    assert not (restore_root / "cognee-system").exists()
    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("SELECT value FROM documents").fetchall() == [
            ("dot-root-doc",)
        ]
    with sqlite3.connect(restored_lifecycle) as connection:
        assert connection.execute("SELECT value FROM revisions").fetchall() == [
            ("dot-root-revision",)
        ]


def test_generation_restore_rejects_tampered_artifact_before_qdrant_write(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )
    (backup_root / "qdrant/collection-1.snapshot").write_bytes(b"tampered")
    restore_store = MemorySnapshotStore({})

    with pytest.raises(GenerationBackupError, match="digest mismatch"):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=tmp_path / "restore",
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert not (tmp_path / "restore").exists()


def test_generation_restore_rejects_wrong_generation_before_any_write(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )
    restore_store = MemorySnapshotStore({})

    with pytest.raises(GenerationBackupError, match="generation ID mismatch"):
        restore_generation_backup(
            generation_id="generation-2",
            backup_root=backup_root,
            target_data_root=tmp_path / "restore",
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert not (tmp_path / "restore").exists()


def test_generation_backup_refuses_an_active_lite_writer(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    active_lock = acquire_single_instance_lock(source_root)
    try:
        with pytest.raises(GenerationBackupError, match="writer is active"):
            create_generation_backup(
                generation_id="generation-1",
                data_root=source_root,
                destination=tmp_path / "generation-backup",
                snapshot_store=MemorySnapshotStore({}),
            )
    finally:
        active_lock.close()

    assert not (tmp_path / "generation-backup").exists()


def test_generation_backup_refuses_collection_inventory_drift(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)

    class DriftingSnapshotStore(MemorySnapshotStore):
        calls = 0

        def list_generation_collections(self, generation_id: str) -> list[str]:
            self.calls += 1
            if self.calls == 1:
                return ["collection-1"]
            return ["collection-1", "collection-2"]

    with pytest.raises(GenerationBackupError, match="collection inventory changed"):
        create_generation_backup(
            generation_id="generation-1",
            data_root=source_root,
            destination=tmp_path / "generation-backup",
            snapshot_store=DriftingSnapshotStore({"collection-1": b"snapshot"}),
        )

    assert not (tmp_path / "generation-backup").exists()


@pytest.mark.parametrize(
    "copied_subtree",
    [
        "cognee-system/operator-backups",
        "data-storage/operator-backups",
        "citadel-state/operator-backups",
    ],
)
def test_generation_backup_rejects_destination_nested_under_copied_source_before_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    copied_subtree: str,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    destination = source_root / copied_subtree / "generation-backup"
    traversed = False

    def fail_if_traversed(_source_root: Path, _destination_root: Path) -> None:
        nonlocal traversed
        traversed = True
        raise AssertionError("source traversal started")

    monkeypatch.setattr(generation_backup, "_copy_lite_state", fail_if_traversed)

    with pytest.raises(GenerationBackupError, match="nested under copied source"):
        create_generation_backup(
            generation_id="generation-1",
            data_root=source_root,
            destination=destination,
            snapshot_store=MemorySnapshotStore({}),
        )

    assert not traversed
    assert not destination.exists()
    assert not (source_root / "citadel-state/lite-runtime.lock").exists()


def test_generation_backup_creates_private_directories_and_files(
    tmp_path: Path,
) -> None:
    previous_umask = os.umask(0)
    try:
        source_root = tmp_path / "source"
        _lite_root(source_root)
        backup_root = tmp_path / "generation-backup"
        create_generation_backup(
            generation_id="generation-1",
            data_root=source_root,
            destination=backup_root,
            snapshot_store=MemorySnapshotStore(
                {"collection-1": b"downloaded-snapshot"}
            ),
        )
    finally:
        os.umask(previous_umask)

    directory_modes = {
        path.relative_to(backup_root).as_posix() or ".": stat.S_IMODE(
            path.stat().st_mode
        )
        for path in [backup_root, *sorted(backup_root.rglob("*"))]
        if path.is_dir()
    }
    file_modes = {
        path.relative_to(backup_root).as_posix(): stat.S_IMODE(path.stat().st_mode)
        for path in sorted(backup_root.rglob("*"))
        if path.is_file()
    }

    assert directory_modes
    assert file_modes
    assert set(directory_modes.values()) == {0o700}
    assert set(file_modes.values()) == {0o600}


def test_generation_restore_rejects_symlink_target_without_touching_referent(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )
    referent = tmp_path / "operator-state"
    referent.mkdir()
    restore_target = tmp_path / "restore"
    restore_target.symlink_to(referent, target_is_directory=True)
    restore_store = MemorySnapshotStore({})

    with pytest.raises(GenerationBackupError, match="symbolic link"):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=restore_target,
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert restore_target.is_symlink()
    assert restore_target.resolve() == referent.resolve()
    assert list(referent.iterdir()) == []


def test_generation_restore_rejects_nested_target_before_provider_write(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )

    class ProviderWriteTrackingStore(MemorySnapshotStore):
        restore_calls = 0

        def restore_collection(
            self,
            collection: str,
            snapshot_path: Path,
            *,
            expected_sha256: str,
        ) -> int:
            self.restore_calls += 1
            return super().restore_collection(
                collection,
                snapshot_path,
                expected_sha256=expected_sha256,
            )

    restore_store = ProviderWriteTrackingStore({})
    restore_target = backup_root / "nested-restore"

    with pytest.raises(GenerationBackupError, match="nested under backup root"):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=restore_target,
            snapshot_store=restore_store,
        )

    assert restore_store.restore_calls == 0
    assert restore_store.restored == {}
    assert not restore_target.exists()


def test_generation_restore_losing_lite_lock_preserves_winning_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )
    restore_target = tmp_path / "restore"
    restore_target.mkdir()
    restore_store = MemorySnapshotStore({})
    original_acquire = generation_backup._acquire_lite_lock
    winning_lock = None

    def lose_lock_after_runtime_wins(
        data_root: Path,
        *,
        operation: str,
        state_root: Path | None = None,
    ) -> Any:
        nonlocal winning_lock
        winning_lock = acquire_single_instance_lock(data_root, state_root=state_root)
        winning_state_root = state_root or data_root / "citadel-state"
        (winning_state_root / "winner-owned.txt").write_text(
            "active runtime state\n",
            encoding="utf-8",
        )
        return original_acquire(
            data_root,
            operation=operation,
            state_root=state_root,
        )

    monkeypatch.setattr(
        generation_backup,
        "_acquire_lite_lock",
        lose_lock_after_runtime_wins,
    )
    try:
        with pytest.raises(GenerationBackupError, match="writer is active"):
            restore_generation_backup(
                generation_id="generation-1",
                backup_root=backup_root,
                target_data_root=restore_target,
                snapshot_store=restore_store,
            )

        assert (restore_target / "citadel-state/winner-owned.txt").read_text(
            encoding="utf-8"
        ) == "active runtime state\n"
        assert (restore_target / "citadel-state/lite-runtime.lock").is_file()
        assert restore_store.restored == {}
    finally:
        if winning_lock is not None:
            winning_lock.close()


def test_backup_cli_reports_missing_server_dependency_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    args = build_parser().parse_args(
        [
            "backup",
            "create",
            str(tmp_path / "generation-backup"),
            "--data-root",
            str(tmp_path / "source"),
            "--generation-id",
            "generation-1",
            "--qdrant-url",
            "http://127.0.0.1:6333",
        ]
    )
    monkeypatch.setenv("VECTOR_DB_KEY", "test-key")
    original_import = builtins.__import__

    def import_without_backup_dependency(
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if name == "kb.generation_backup":
            raise ModuleNotFoundError(
                "No module named 'qdrant_client'",
                name="qdrant_client",
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_backup_dependency)

    exit_code = asyncio.run(args.handler(args))
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "`citadel backup` needs the server extra" in captured.err
    assert "missing dependency: qdrant_client" in captured.err


def test_generation_restore_rolls_back_qdrant_after_partial_failure(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore(
            {
                "collection-1": b"first-snapshot",
                "collection-2": b"second-snapshot",
            }
        ),
    )
    restore_store = MemorySnapshotStore({}, fail_collection="collection-2")

    with pytest.raises(GenerationBackupError, match="forced restore failure"):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=tmp_path / "restore",
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert not (tmp_path / "restore").exists()


def test_generation_restore_rolls_back_qdrant_after_count_mismatch(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _lite_root(source_root)
    backup_root = tmp_path / "generation-backup"
    create_generation_backup(
        generation_id="generation-1",
        data_root=source_root,
        destination=backup_root,
        snapshot_store=MemorySnapshotStore({"collection-1": b"snapshot"}),
    )

    class MismatchedCountSnapshotStore(MemorySnapshotStore):
        def restore_collection(
            self,
            collection: str,
            snapshot_path: Path,
            *,
            expected_sha256: str,
        ) -> int:
            actual = super().restore_collection(
                collection,
                snapshot_path,
                expected_sha256=expected_sha256,
            )
            return actual + 1

    restore_store = MismatchedCountSnapshotStore({})

    with pytest.raises(GenerationBackupError, match="count mismatch"):
        restore_generation_backup(
            generation_id="generation-1",
            backup_root=backup_root,
            target_data_root=tmp_path / "restore",
            snapshot_store=restore_store,
        )

    assert restore_store.restored == {}
    assert not (tmp_path / "restore").exists()
