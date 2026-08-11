from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import fcntl
import hashlib
import json
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

from kb.lite_runtime import LiteConfigurationError, acquire_single_instance_lock


_MANIFEST_SCHEMA_VERSION = 1
_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class GenerationBackupError(RuntimeError):
    pass


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


def _direct_destination(path: Path, *, label: str) -> Path:
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


def _reject_nested_backup_destination(source_root: Path, destination: Path) -> None:
    copied_roots = (
        source_root / "cognee-system",
        source_root / "data-storage",
        source_root / "citadel-state",
    )
    excluded_backup_root = source_root / "citadel-state/backups"
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
    system_source = source_root / "cognee-system"
    state_source = source_root / "citadel-state"
    _copy_tree(
        system_source,
        destination_root / "cognee-system",
        excluded=frozenset(
            {
                "databases/cognee.db",
                "databases/cognee.db-shm",
                "databases/cognee.db-wal",
            }
        ),
    )
    _copy_tree(source_root / "data-storage", destination_root / "data-storage")
    _copy_tree(
        state_source,
        destination_root / "citadel-state",
        excluded=frozenset(
            {
                "backups",
                "lite-runtime.lock",
                "lifecycle.sqlite3",
                "lifecycle.sqlite3-shm",
                "lifecycle.sqlite3-wal",
            }
        ),
    )
    _sqlite_backup(
        system_source / "databases/cognee.db",
        destination_root / "cognee-system/databases/cognee.db",
    )
    _sqlite_backup(
        state_source / "lifecycle.sqlite3",
        destination_root / "citadel-state/lifecycle.sqlite3",
    )


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _acquire_lite_lock(data_root: Path, *, operation: str) -> Any:
    try:
        return acquire_single_instance_lock(data_root)
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
    final_root = _direct_destination(destination, label="backup destination")
    _reject_nested_backup_destination(source_root, final_root)
    if final_root.exists():
        raise GenerationBackupError(f"backup destination already exists: {final_root}")
    lock = _acquire_lite_lock(source_root, operation="backup")
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
        _make_private_directory(target_root / "citadel-state")
        lock = _acquire_lite_lock(target_root, operation="restore")
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

        _copy_tree(source_root / "local", target_root)
        _verify_inventory(
            target_root,
            local_files,
            ignored_paths=frozenset({"citadel-state/lite-runtime.lock"}),
        )
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
                for name in ("cognee-system", "data-storage", "citadel-state"):
                    _remove_operation_tree(target_root / name)
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
        self._client = QdrantClient(url=self._url, api_key=api_key)

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
                with urlopen(request, timeout=300) as response:  # noqa: S310
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
