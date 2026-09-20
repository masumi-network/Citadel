from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from qdrant_client import QdrantClient, models

from kb.lite_lock import LiteConfigurationError, acquire_single_instance_lock


_MANIFEST_SCHEMA_VERSION = 1
_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class GenerationBackupError(RuntimeError):
    pass
_QDRANT_SNAPSHOT_TIMEOUT_ENV = "CITADEL_QDRANT_SNAPSHOT_TIMEOUT_SECONDS"
_DEFAULT_QDRANT_SNAPSHOT_TIMEOUT_SECONDS = 900
_MIN_QDRANT_SNAPSHOT_TIMEOUT_SECONDS = 1


def _qdrant_snapshot_timeout_seconds() -> int:
    raw = os.getenv(_QDRANT_SNAPSHOT_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_QDRANT_SNAPSHOT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise GenerationBackupError(
            f"{_QDRANT_SNAPSHOT_TIMEOUT_ENV} must be a finite number"
        ) from error
    if not math.isfinite(value):
        raise GenerationBackupError(
            f"{_QDRANT_SNAPSHOT_TIMEOUT_ENV} must be a finite number"
        )
    if value < _MIN_QDRANT_SNAPSHOT_TIMEOUT_SECONDS:
        raise GenerationBackupError(
            f"{_QDRANT_SNAPSHOT_TIMEOUT_ENV} must be at least "
            f"{_MIN_QDRANT_SNAPSHOT_TIMEOUT_SECONDS} second"
        )
    return math.ceil(value)



_CANONICAL_SYSTEM_ROOT = Path("cognee-system")
_CANONICAL_DATA_ROOT = Path("data-storage")
_CANONICAL_STATE_ROOT = Path("citadel-state")
_CANONICAL_DATABASE = _CANONICAL_SYSTEM_ROOT / "databases/cognee.db"
_CANONICAL_LIFECYCLE = _CANONICAL_STATE_ROOT / "lifecycle.sqlite3"


@dataclass(frozen=True)
class _LiteStorageLayout:
    system_root: Path
    data_storage_root: Path
    state_root: Path
    database_path: Path
    database_file: Path
    lifecycle_file: Path


class SnapshotStore(Protocol):
    def list_generation_collections(self, generation_id: str) -> list[str]: ...

    def download_collection_snapshot(
        self,
        collection: str,
        destination: Path,
    ) -> Mapping[str, Any]: ...

    def require_empty(self) -> None: ...

    def restore_collection(
        self,
        collection: str,
        snapshot_path: Path,
        *,
        expected_sha256: str,
    ) -> int: ...

    def delete_collection(self, collection: str) -> None: ...


def _direct_path(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise GenerationBackupError(f"{label} is not a direct path: {absolute}") from error
    if resolved != absolute:
        raise GenerationBackupError(
            f"{label} must not resolve through a symbolic link: {absolute}"
        )
    return absolute


def _direct_destination(path: Path, *, label: str) -> Path:
    return _direct_path(path, label=label)


def _configured_volume_root(root: Path) -> Path:
    configured = Path(os.getenv("CITADEL_LITE_DATA_ROOT", str(root)))
    return _direct_path(configured, label="CITADEL_LITE_DATA_ROOT")


def _map_configured_path(
    *,
    name: str,
    configured_root: Path,
    actual_root: Path,
    fallback: Path,
) -> Path:
    raw = os.getenv(name)
    configured_path = Path(raw) if raw else configured_root / fallback
    if not configured_path.is_absolute():
        configured_path = configured_root / configured_path
    configured_path = _direct_path(configured_path, label=name)
    if not configured_path.is_relative_to(configured_root):
        raise GenerationBackupError(
            f"{name} must resolve inside CITADEL_LITE_DATA_ROOT: {configured_path}"
        )
    return actual_root / configured_path.relative_to(configured_root)


def _validate_layout_roots(layout: _LiteStorageLayout) -> None:
    roots = {
        "SYSTEM_ROOT_DIRECTORY": layout.system_root,
        "DATA_ROOT_DIRECTORY": layout.data_storage_root,
        "CITADEL_STATE_DIRECTORY": layout.state_root,
    }
    items = list(roots.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise GenerationBackupError(
                    f"{left_name} and {right_name} must be separate directories"
                )
    if not layout.database_path.is_relative_to(layout.system_root):
        raise GenerationBackupError(
            "DB_PATH must resolve inside SYSTEM_ROOT_DIRECTORY: "
            f"{layout.database_path}"
        )
    if not layout.lifecycle_file.is_relative_to(layout.state_root):
        raise GenerationBackupError(
            "CITADEL_LIFECYCLE_STORE_PATH must resolve inside "
            f"CITADEL_STATE_DIRECTORY: {layout.lifecycle_file}"
        )
    if layout.lifecycle_file == layout.state_root / "lite-runtime.lock":
        raise GenerationBackupError(
            "CITADEL_LIFECYCLE_STORE_PATH must not use the Lite runtime lock path"
        )


def _lite_storage_layout(root: Path) -> _LiteStorageLayout:
    actual_root = Path(root)
    configured_root = _configured_volume_root(actual_root)
    system_root = _map_configured_path(
        name="SYSTEM_ROOT_DIRECTORY",
        configured_root=configured_root,
        actual_root=actual_root,
        fallback=_CANONICAL_SYSTEM_ROOT,
    )
    data_storage_root = _map_configured_path(
        name="DATA_ROOT_DIRECTORY",
        configured_root=configured_root,
        actual_root=actual_root,
        fallback=_CANONICAL_DATA_ROOT,
    )
    state_root = _map_configured_path(
        name="CITADEL_STATE_DIRECTORY",
        configured_root=configured_root,
        actual_root=actual_root,
        fallback=_CANONICAL_STATE_ROOT,
    )
    database_path = _map_configured_path(
        name="DB_PATH",
        configured_root=configured_root,
        actual_root=actual_root,
        fallback=_CANONICAL_SYSTEM_ROOT / "databases",
    )
    database_name = os.getenv("DB_NAME", "cognee.db")
    if (
        not database_name
        or database_name in {".", ".."}
        or Path(database_name).name != database_name
    ):
        raise GenerationBackupError(f"DB_NAME must be a single file name: {database_name!r}")
    database_file = database_path / database_name
    if os.getenv("CITADEL_LIFECYCLE_STORE_PATH"):
        lifecycle_file = _map_configured_path(
            name="CITADEL_LIFECYCLE_STORE_PATH",
            configured_root=configured_root,
            actual_root=actual_root,
            fallback=_CANONICAL_LIFECYCLE,
        )
    else:
        lifecycle_file = state_root / "lifecycle.sqlite3"
    layout = _LiteStorageLayout(
        system_root=system_root,
        data_storage_root=data_storage_root,
        state_root=state_root,
        database_path=database_path,
        database_file=database_file,
        lifecycle_file=lifecycle_file,
    )
    _validate_layout_roots(layout)
    return layout


def _sqlite_exclusions(database_file: Path, root: Path) -> frozenset[str]:
    relative = database_file.relative_to(root).as_posix()
    return frozenset({relative, f"{relative}-shm", f"{relative}-wal"})


def _reject_nested_backup_destination(
    source_root: Path,
    destination: Path,
    layout: _LiteStorageLayout,
) -> None:
    copied_roots = (
        layout.system_root,
        layout.data_storage_root,
        layout.state_root,
    )
    excluded_backup_root = layout.state_root / "backups"
    if destination.is_relative_to(excluded_backup_root):
        return
    for copied_root in copied_roots:
        if destination.is_relative_to(copied_root):
            raise GenerationBackupError(
                "backup destination is nested under copied source: "
                f"{destination}"
            )


def _reject_nested_restore_target(backup_root: Path, target_root: Path) -> None:
    if target_root == backup_root or target_root.is_relative_to(backup_root):
        raise GenerationBackupError(
            f"restore target is nested under backup root: {target_root}"
        )


def _create_private_directory_chain(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise GenerationBackupError(
                f"backup destination must not resolve through a symbolic link: {current}"
            )
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise GenerationBackupError(
                f"backup destination parent is unavailable: {path}"
            )
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise GenerationBackupError(
            f"backup destination parent is not a direct directory: {current}"
        )
    for directory in reversed(missing):
        directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        directory.chmod(_PRIVATE_DIRECTORY_MODE)
    return missing


def _make_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise GenerationBackupError(f"private backup path is a symbolic link: {path}")
    path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
    if not path.is_dir():
        raise GenerationBackupError(f"private backup path is not a directory: {path}")
    path.chmod(_PRIVATE_DIRECTORY_MODE)


def _prepare_private_file(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise GenerationBackupError(f"private backup file already exists: {path}")
    path.touch(mode=_PRIVATE_FILE_MODE, exist_ok=False)
    path.chmod(_PRIVATE_FILE_MODE)


def _write_private_bytes(path: Path, content: bytes) -> None:
    _prepare_private_file(path)
    path.write_bytes(content)
    path.chmod(_PRIVATE_FILE_MODE)


def _remove_operation_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_collection_name(collection: str) -> str:
    if (
        not collection
        or collection in {".", ".."}
        or _COLLECTION_NAME.fullmatch(collection) is None
    ):
        raise GenerationBackupError(f"unsafe Qdrant collection name: {collection!r}")
    return collection


def _sqlite_backup(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise GenerationBackupError(f"required SQLite database is missing: {source_path}")
    _make_private_directory(destination_path.parent)
    _prepare_private_file(destination_path)
    try:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        with sqlite3.connect(f"file:{destination_path}?mode=ro", uri=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as error:
        raise GenerationBackupError(f"SQLite backup failed for {source_path}") from error
    if integrity != "ok":
        raise GenerationBackupError(
            f"SQLite backup integrity check failed for {source_path}: {integrity}"
        )


def _copy_sqlite(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise GenerationBackupError(f"required SQLite database is missing: {source_path}")
    _make_private_directory(destination_path.parent)
    _prepare_private_file(destination_path)
    try:
        shutil.copyfile(source_path, destination_path)
        destination_path.chmod(_PRIVATE_FILE_MODE)
        with sqlite3.connect(f"file:{destination_path}?mode=ro", uri=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except (OSError, sqlite3.Error) as error:
        raise GenerationBackupError(f"SQLite restore failed for {source_path}") from error
    if integrity != "ok":
        raise GenerationBackupError(
            f"SQLite restore integrity check failed for {source_path}: {integrity}"
        )


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    if not source.exists():
        return
    if source.is_symlink():
        raise GenerationBackupError(f"backup source must not be a symlink: {source}")
    _make_private_directory(destination)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        relative_text = relative.as_posix()
        if any(
            relative_text == name or relative_text.startswith(f"{name}/")
            for name in excluded
        ):
            continue
        if item.is_symlink():
            raise GenerationBackupError(f"backup source must not contain symlinks: {item}")
        target = destination / relative
        if item.is_dir():
            _make_private_directory(target)
        elif item.is_file():
            _make_private_directory(target.parent)
            _prepare_private_file(target)
            shutil.copyfile(item, target)
            target.chmod(_PRIVATE_FILE_MODE)


def _file_inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _verify_inventory(
    root: Path,
    inventory: list[dict[str, Any]],
    *,
    ignored_paths: frozenset[str] = frozenset(),
) -> None:
    expected_paths: set[str] = set()
    for item in inventory:
        relative = str(item.get("path", ""))
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise GenerationBackupError(f"unsafe manifest path: {relative!r}")
        path = root / relative
        expected_paths.add(relative)
        if not path.is_file():
            raise GenerationBackupError(f"backup artifact is missing: {relative}")
        actual_size = path.stat().st_size
        actual_digest = _sha256(path)
        if actual_size != int(item["size"]) or actual_digest != str(item["sha256"]):
            raise GenerationBackupError(f"backup artifact digest mismatch: {relative}")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() not in ignored_paths
    }
    if actual_paths != expected_paths:
        raise GenerationBackupError("backup local-file inventory is incomplete")


def _copy_lite_state(source_root: Path, destination_root: Path) -> None:
    layout = _lite_storage_layout(source_root)
    _copy_tree(
        layout.system_root,
        destination_root / _CANONICAL_SYSTEM_ROOT,
        excluded=_sqlite_exclusions(layout.database_file, layout.system_root),
    )
    _copy_tree(
        layout.data_storage_root,
        destination_root / _CANONICAL_DATA_ROOT,
    )
    state_excluded = set(_sqlite_exclusions(layout.lifecycle_file, layout.state_root))
    state_excluded.update({"backups", "lite-runtime.lock"})
    _copy_tree(
        layout.state_root,
        destination_root / _CANONICAL_STATE_ROOT,
        excluded=frozenset(state_excluded),
    )
    _sqlite_backup(
        layout.database_file,
        destination_root / _CANONICAL_DATABASE,
    )
    _sqlite_backup(
        layout.lifecycle_file,
        destination_root / _CANONICAL_LIFECYCLE,
    )


def _archive_path_to_target(relative: str, layout: _LiteStorageLayout) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise GenerationBackupError(f"unsafe manifest path: {relative!r}")
    if path == _CANONICAL_DATABASE:
        return layout.database_file
    if path == _CANONICAL_LIFECYCLE:
        return layout.lifecycle_file
    for prefix, target_root in (
        (_CANONICAL_SYSTEM_ROOT, layout.system_root),
        (_CANONICAL_DATA_ROOT, layout.data_storage_root),
        (_CANONICAL_STATE_ROOT, layout.state_root),
    ):
        if path == prefix or path.is_relative_to(prefix):
            return target_root / path.relative_to(prefix)
    raise GenerationBackupError(f"unsupported local backup path: {relative}")


def _target_path_to_archive(path: Path, layout: _LiteStorageLayout) -> str:
    if path == layout.database_file:
        return _CANONICAL_DATABASE.as_posix()
    if path == layout.lifecycle_file:
        return _CANONICAL_LIFECYCLE.as_posix()
    for prefix, source_root in (
        (_CANONICAL_SYSTEM_ROOT, layout.system_root),
        (_CANONICAL_DATA_ROOT, layout.data_storage_root),
        (_CANONICAL_STATE_ROOT, layout.state_root),
    ):
        if path == source_root or path.is_relative_to(source_root):
            return (prefix / path.relative_to(source_root)).as_posix()
    raise GenerationBackupError(f"unsupported restore path: {path}")


def _restore_lite_state(source_root: Path, target_layout: _LiteStorageLayout) -> None:
    canonical_system = source_root / _CANONICAL_SYSTEM_ROOT
    canonical_data = source_root / _CANONICAL_DATA_ROOT
    canonical_state = source_root / _CANONICAL_STATE_ROOT
    _copy_tree(
        canonical_system,
        target_layout.system_root,
        excluded=_sqlite_exclusions(
            _CANONICAL_DATABASE,
            _CANONICAL_SYSTEM_ROOT,
        ),
    )
    _copy_tree(canonical_data, target_layout.data_storage_root)
    state_excluded = set(
        _sqlite_exclusions(_CANONICAL_LIFECYCLE, _CANONICAL_STATE_ROOT)
    )
    state_excluded.update({"backups", "lite-runtime.lock"})
    _copy_tree(
        canonical_state,
        target_layout.state_root,
        excluded=frozenset(state_excluded),
    )
    _copy_sqlite(
        source_root / _CANONICAL_DATABASE,
        target_layout.database_file,
    )
    _copy_sqlite(
        source_root / _CANONICAL_LIFECYCLE,
        target_layout.lifecycle_file,
    )


def _verify_restored_inventory(
    target_layout: _LiteStorageLayout,
    inventory: list[dict[str, Any]],
) -> None:
    expected: dict[Path, tuple[str, int, str]] = {}
    for item in inventory:
        relative = str(item.get("path", ""))
        target = _archive_path_to_target(relative, target_layout)
        if target in expected:
            raise GenerationBackupError(
                f"duplicate restore target for local backup path: {relative}"
            )
        expected[target] = (relative, int(item["size"]), str(item["sha256"]))

    actual_paths: set[Path] = set()
    for root in (
        target_layout.system_root,
        target_layout.data_storage_root,
        target_layout.state_root,
    ):
        if not root.exists():
            continue
        if root.is_symlink():
            raise GenerationBackupError(f"restore path must not be a symlink: {root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise GenerationBackupError(f"restore path must not be a symlink: {path}")
            if path.is_file() and path != target_layout.state_root / "lite-runtime.lock":
                _target_path_to_archive(path, target_layout)
                actual_paths.add(path)
    if actual_paths != set(expected):
        raise GenerationBackupError("restored local-file inventory is incomplete")
    for path, (relative, size, digest) in expected.items():
        if not path.is_file():
            raise GenerationBackupError(f"restored backup artifact is missing: {relative}")
        if path.stat().st_size != size or _sha256(path) != digest:
            raise GenerationBackupError(f"restored artifact digest mismatch: {relative}")


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _acquire_lite_lock(
    data_root: Path,
    *,
    operation: str,
    state_root: Path | None = None,
) -> Any:
    try:
        return acquire_single_instance_lock(data_root, state_root=state_root)
    except (LiteConfigurationError, OSError) as error:
        raise GenerationBackupError(
            f"Citadel writer is active or the Lite {operation} lock is unavailable"
        ) from error


def create_generation_backup(
    *,
    generation_id: str,
    data_root: Path,
    destination: Path,
    snapshot_store: SnapshotStore,
) -> dict[str, Any]:
    generation = generation_id.strip()
    if not generation:
        raise GenerationBackupError("generation ID must not be empty")
    source_root = data_root.resolve()
    source_layout = _lite_storage_layout(source_root)
    final_root = _direct_destination(destination, label="backup destination")
    _reject_nested_backup_destination(source_root, final_root, source_layout)
    if final_root.exists():
        raise GenerationBackupError(f"backup destination already exists: {final_root}")
    lock = _acquire_lite_lock(
        source_root,
        operation="backup",
        state_root=source_layout.state_root,
    )
    temporary_root = final_root.with_name(f".{final_root.name}.tmp-{uuid4().hex}")
    created_parent_directories: list[Path] = []
    try:
        created_parent_directories = _create_private_directory_chain(final_root.parent)
        temporary_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        temporary_root.chmod(_PRIVATE_DIRECTORY_MODE)
        local_root = temporary_root / "local"
        qdrant_root = temporary_root / "qdrant"
        _make_private_directory(local_root)
        _make_private_directory(qdrant_root)
        _copy_lite_state(source_root, local_root)

        generation_collections = sorted(
            _safe_collection_name(collection)
            for collection in snapshot_store.list_generation_collections(generation)
        )
        if len(generation_collections) != len(set(generation_collections)):
            raise GenerationBackupError("Qdrant generation collection inventory has duplicates")
        qdrant_collections: list[dict[str, Any]] = []
        for collection in generation_collections:
            artifact = qdrant_root / f"{collection}.snapshot"
            _prepare_private_file(artifact)
            receipt = dict(
                snapshot_store.download_collection_snapshot(collection, artifact)
            )
            if not artifact.is_file():
                raise GenerationBackupError(
                    f"Qdrant snapshot download is missing for {collection}"
                )
            artifact.chmod(_PRIVATE_FILE_MODE)
            qdrant_collections.append(
                {
                    "collection": collection,
                    "artifact": artifact.relative_to(temporary_root).as_posix(),
                    "snapshot_name": str(receipt.get("snapshot_name", "")),
                    "point_count": int(receipt.get("point_count", 0)),
                    "size": artifact.stat().st_size,
                    "sha256": _sha256(artifact),
                }
            )
        final_collections = sorted(
            _safe_collection_name(collection)
            for collection in snapshot_store.list_generation_collections(generation)
        )
        if final_collections != generation_collections:
            raise GenerationBackupError(
                "Qdrant generation collection inventory changed during backup"
            )

        manifest: dict[str, Any] = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "generation_id": generation,
            "created_at": datetime.now(UTC).isoformat(),
            "backup_mode": "offline-single-writer",
            "local_files": _file_inventory(local_root),
            "qdrant_collections": qdrant_collections,
        }
        manifest_path = temporary_root / "manifest.json"
        _write_private_bytes(manifest_path, _manifest_bytes(manifest))
        _write_private_bytes(
            temporary_root / "manifest.sha256",
            f"{_sha256(manifest_path)}  manifest.json\n".encode("ascii"),
        )
        if final_root.exists() or final_root.is_symlink():
            raise GenerationBackupError(
                f"backup destination already exists: {final_root}"
            )
        os.replace(temporary_root, final_root)
        return manifest
    except Exception:
        _remove_operation_tree(temporary_root)
        for directory in created_parent_directories:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _load_manifest(backup_root: Path) -> dict[str, Any]:
    manifest_path = backup_root / "manifest.json"
    seal_path = backup_root / "manifest.sha256"
    if not manifest_path.is_file() or not seal_path.is_file():
        raise GenerationBackupError("backup manifest or seal is missing")
    expected_seal = seal_path.read_text(encoding="ascii").split(maxsplit=1)[0]
    if _sha256(manifest_path) != expected_seal:
        raise GenerationBackupError("backup manifest digest mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise GenerationBackupError("backup manifest is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise GenerationBackupError("unsupported backup manifest schema")
    return manifest


def _verify_qdrant_artifacts(
    backup_root: Path,
    collections: list[dict[str, Any]],
) -> None:
    names: set[str] = set()
    for item in collections:
        collection = _safe_collection_name(str(item.get("collection", "")))
        if collection in names:
            raise GenerationBackupError(f"duplicate Qdrant collection: {collection}")
        names.add(collection)
        expected_artifact = f"qdrant/{collection}.snapshot"
        if item.get("artifact") != expected_artifact:
            raise GenerationBackupError(
                f"unexpected Qdrant snapshot path for {collection}"
            )
        artifact = backup_root / expected_artifact
        if not artifact.is_file():
            raise GenerationBackupError(f"Qdrant snapshot is missing: {collection}")
        if (
            artifact.stat().st_size != int(item["size"])
            or _sha256(artifact) != str(item["sha256"])
        ):
            raise GenerationBackupError(
                f"backup artifact digest mismatch: {expected_artifact}"
            )


def restore_generation_backup(
    *,
    generation_id: str,
    backup_root: Path,
    target_data_root: Path,
    snapshot_store: SnapshotStore,
) -> dict[str, Any]:
    generation = generation_id.strip()
    if not generation:
        raise GenerationBackupError("generation ID must not be empty")
    source_root = backup_root.resolve()
    target_root = _direct_destination(target_data_root, label="restore target")
    _reject_nested_restore_target(source_root, target_root)
    target_layout = _lite_storage_layout(target_root)
    manifest = _load_manifest(source_root)
    artifact_generation = manifest.get("generation_id")
    if artifact_generation != generation:
        raise GenerationBackupError(
            "generation ID mismatch: "
            f"restore target is {generation!r}, backup is {artifact_generation!r}"
        )
    local_files = manifest.get("local_files")
    collections = manifest.get("qdrant_collections")
    if not isinstance(local_files, list) or not isinstance(collections, list):
        raise GenerationBackupError("backup manifest inventory is malformed")
    _verify_inventory(source_root / "local", local_files)
    for item in local_files:
        _archive_path_to_target(str(item.get("path", "")), target_layout)
    _verify_qdrant_artifacts(source_root, collections)
    if target_root.exists():
        if not target_root.is_dir() or any(target_root.iterdir()):
            raise GenerationBackupError(f"restore target is not empty: {target_root}")

    snapshot_store.require_empty()
    target_existed = target_root.exists()
    created_parent_directories: list[Path] = []
    restored_collections: list[dict[str, Any]] = []
    rollback_collections: list[str] = []
    lock: Any | None = None
    owns_target_cleanup = False
    try:
        if not target_existed:
            created_parent_directories = _create_private_directory_chain(
                target_root.parent
            )
            target_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            target_root.chmod(_PRIVATE_DIRECTORY_MODE)
        _make_private_directory(target_layout.state_root)
        lock = _acquire_lite_lock(
            target_root,
            operation="restore",
            state_root=target_layout.state_root,
        )
        owns_target_cleanup = True

        for item in collections:
            collection = str(item["collection"])
            rollback_collections.append(collection)
            point_count = snapshot_store.restore_collection(
                collection,
                source_root / str(item["artifact"]),
                expected_sha256=str(item["sha256"]),
            )
            expected_count = int(item["point_count"])
            if point_count != expected_count:
                raise GenerationBackupError(
                    f"restored Qdrant count mismatch for {collection}: "
                    f"expected {expected_count}, got {point_count}"
                )
            restored_collections.append(
                {"collection": collection, "point_count": point_count}
            )

        _restore_lite_state(source_root / "local", target_layout)
        _verify_restored_inventory(target_layout, local_files)
        return {
            "ok": True,
            "generation_id": str(manifest["generation_id"]),
            "local_files": len(local_files),
            "qdrant_collections": restored_collections,
        }
    except Exception as error:
        rollback_failures: list[str] = []
        for collection in reversed(rollback_collections):
            try:
                snapshot_store.delete_collection(collection)
            except Exception:
                rollback_failures.append(collection)
        if owns_target_cleanup:
            if target_existed:
                for path in (
                    target_layout.system_root,
                    target_layout.data_storage_root,
                    target_layout.state_root,
                ):
                    _remove_operation_tree(path)
            else:
                _remove_operation_tree(target_root)
                for directory in created_parent_directories:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
        if rollback_failures:
            raise GenerationBackupError(
                "restore failed and Qdrant rollback was incomplete: "
                + ", ".join(rollback_failures)
            ) from error
        raise
    finally:
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


class QdrantSnapshotStore:
    def __init__(self, *, url: str, api_key: str | None) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._snapshot_timeout_seconds = _qdrant_snapshot_timeout_seconds()
        self._client = QdrantClient(
            url=self._url,
            api_key=api_key,
            timeout=self._snapshot_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> QdrantSnapshotStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_generation_collections(self, generation_id: str) -> list[str]:
        generation_hash = hashlib.sha256(generation_id.encode("utf-8")).hexdigest()[:12]
        prefix = f"citadel_g_{generation_hash}_"
        return sorted(
            item.name
            for item in self._client.get_collections().collections
            if item.name.startswith(prefix)
        )

    def download_collection_snapshot(
        self,
        collection: str,
        destination: Path,
    ) -> Mapping[str, Any]:
        snapshot = self._client.create_snapshot(collection, wait=True)
        if snapshot is None:
            raise GenerationBackupError(
                f"Qdrant did not create a snapshot for {collection}"
            )
        headers = {"Accept": "application/octet-stream"}
        if self._api_key:
            headers["api-key"] = self._api_key
        request = Request(
            f"{self._url}/collections/{quote(collection, safe='')}"
            f"/snapshots/{quote(snapshot.name, safe='')}",
            headers=headers,
        )
        try:
            try:
                with urlopen(
                    request, timeout=self._snapshot_timeout_seconds
                ) as response:  # noqa: S310
                    with destination.open("wb") as handle:
                        shutil.copyfileobj(response, handle)
            except OSError as error:
                raise GenerationBackupError(
                    f"Qdrant snapshot download failed for {collection}"
                ) from error
        finally:
            deleted = self._client.delete_snapshot(
                collection,
                snapshot.name,
                wait=True,
            )
            if not deleted:
                raise GenerationBackupError(
                    f"Qdrant temporary snapshot cleanup failed for {collection}"
                )
        point_count = int(self._client.count(collection, exact=True).count)
        return {
            "snapshot_name": snapshot.name,
            "point_count": point_count,
        }

    def require_empty(self) -> None:
        collections = sorted(
            item.name for item in self._client.get_collections().collections
        )
        if collections:
            raise GenerationBackupError(
                "Qdrant restore target is not empty: " + ", ".join(collections)
            )

    def restore_collection(
        self,
        collection: str,
        snapshot_path: Path,
        *,
        expected_sha256: str,
    ) -> int:
        if _sha256(snapshot_path) != expected_sha256:
            raise GenerationBackupError(
                f"backup artifact digest mismatch: {snapshot_path.name}"
            )
        try:
            with snapshot_path.open("rb") as snapshot:
                response = self._client.http.snapshots_api.recover_from_uploaded_snapshot(
                    collection_name=collection,
                    wait=True,
                    priority=models.SnapshotPriority.SNAPSHOT,
                    snapshot=snapshot,
                )
        except Exception as error:
            raise GenerationBackupError(
                f"Qdrant uploaded snapshot restore failed for {collection}"
            ) from error
        if not bool(getattr(response, "result", False)):
            raise GenerationBackupError(
                f"Qdrant uploaded snapshot restore returned false for {collection}"
            )
        return int(self._client.count(collection, exact=True).count)

    def delete_collection(self, collection: str) -> None:
        if not self._client.collection_exists(collection_name=collection):
            return
        deleted = self._client.delete_collection(collection_name=collection)
        if not deleted:
            raise GenerationBackupError(
                f"Qdrant rollback failed for collection {collection}"
            )
