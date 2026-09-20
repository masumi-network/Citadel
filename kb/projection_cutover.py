from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any
from uuid import UUID

from kb.generation_backup import GenerationBackupError


PROJECTION_CUTOVER_SCHEMA_VERSION = 1
PROJECTION_CUTOVER_SCHEMA = "citadel.projection-cutover"
PROJECTION_CUTOVER_RECEIPT = Path("citadel-state/projection-cutover.json")
_ARCHIVE_TABLE = "pipeline_runs_cognify_archive"
_ARCHIVE_METADATA_TABLE = "projection_cutover_archive_metadata"
_GENERATION_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_GRAPH_SUFFIXES = (".lbug", ".lbug.wal")


class ProjectionCutoverError(GenerationBackupError):
    """Raised when the clean projection cutover cannot be applied safely."""


def _direct_path(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ProjectionCutoverError(f"{label} is not a direct path: {absolute}") from error
    if resolved != absolute:
        raise ProjectionCutoverError(
            f"{label} must not resolve through a symbolic link: {absolute}"
        )
    return absolute


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_generation(generation_id: str) -> str:
    generation = generation_id.strip()
    if not generation:
        raise ProjectionCutoverError("generation ID must not be empty")
    return generation


def _require_manifest_hash(manifest_sha256: str) -> str:
    digest = manifest_sha256.strip().lower()
    if _HEX_SHA256.fullmatch(digest) is None:
        raise ProjectionCutoverError("manifest SHA256 must be 64 hexadecimal characters")
    return digest


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _receipt_digest(receipt: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _read_existing_receipt(
    target_root: Path,
    *,
    generation_id: str,
    manifest_sha256: str,
) -> dict[str, Any] | None:
    receipt_path = target_root / PROJECTION_CUTOVER_RECEIPT
    if not receipt_path.exists():
        return None
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ProjectionCutoverError("projection cutover receipt must be a regular file")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectionCutoverError("projection cutover receipt is unreadable") from error
    if not isinstance(receipt, dict):
        raise ProjectionCutoverError("projection cutover receipt is malformed")
    if receipt.get("schema") != PROJECTION_CUTOVER_SCHEMA:
        raise ProjectionCutoverError("projection cutover receipt schema is unsupported")
    if receipt.get("schema_version") != PROJECTION_CUTOVER_SCHEMA_VERSION:
        raise ProjectionCutoverError("projection cutover receipt schema is unsupported")
    if receipt.get("generation_id") != generation_id or receipt.get("manifest_sha256") != manifest_sha256:
        raise ProjectionCutoverError("projection cutover receipt does not match requested cutover")
    if receipt.get("receipt_sha256") != _receipt_digest(receipt):
        raise ProjectionCutoverError("projection cutover receipt hash mismatch")
    return receipt


def _layout(target_root: Path, backup_module: Any) -> Any:
    return backup_module._LiteStorageLayout(
        system_root=target_root / "cognee-system",
        data_storage_root=target_root / "data-storage",
        state_root=target_root / "citadel-state",
        database_path=target_root / "cognee-system/databases",
        database_file=target_root / "cognee-system/databases/cognee.db",
        lifecycle_file=target_root / "citadel-state/lifecycle.sqlite3",
    )


def _reject_symlinks(root: Path, *, label: str) -> None:
    if not root.exists():
        raise ProjectionCutoverError(f"{label} is missing: {root}")
    if root.is_symlink():
        raise ProjectionCutoverError(f"{label} must not contain symbolic links: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ProjectionCutoverError(f"{label} must not contain symbolic links: {path}")


def _validate_manifest(
    backup_root: Path,
    *,
    target_layout: Any,
    generation_id: str,
    manifest_sha256: str,
    backup_module: Any,
) -> dict[str, Any]:
    if not backup_root.is_dir() or backup_root.is_symlink():
        raise ProjectionCutoverError(f"backup root is not a direct directory: {backup_root}")
    manifest_path = backup_root / "manifest.json"
    actual_hash = _sha256(manifest_path) if manifest_path.is_file() else ""
    if actual_hash != manifest_sha256:
        raise ProjectionCutoverError(
            f"backup manifest hash mismatch: expected {manifest_sha256}, got {actual_hash}"
        )
    manifest = backup_module._load_manifest(backup_root)
    if manifest.get("generation_id") != generation_id:
        raise ProjectionCutoverError(
            "generation ID mismatch: "
            f"cutover is {generation_id!r}, backup is {manifest.get('generation_id')!r}"
        )
    local_files = manifest.get("local_files")
    collections = manifest.get("qdrant_collections")
    if not isinstance(local_files, list) or not isinstance(collections, list):
        raise ProjectionCutoverError("backup manifest inventory is malformed")
    local_root = backup_root / "local"
    _reject_symlinks(local_root, label="backup local tree")
    backup_module._verify_inventory(local_root, local_files)
    for item in local_files:
        relative = str(item.get("path", ""))
        try:
            backup_module._archive_path_to_target(relative, target_layout)
        except GenerationBackupError as error:
            raise ProjectionCutoverError(str(error)) from error
    backup_module._verify_qdrant_artifacts(backup_root, collections)
    return manifest


def _normalize_id(value: Any) -> str:
    text = str(value).strip()
    try:
        return UUID(text).hex
    except (ValueError, AttributeError):
        return text.casefold()


def _safe_error_ids(values: set[str]) -> str:
    safe = []
    for value in sorted(values)[:20]:
        safe.append(value if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else "<unsafe>")
    return ",".join(safe) or "-"


def _validate_lifecycle_projection(
    lifecycle_path: Path,
    *,
    generation_id: str,
) -> set[str]:
    if not lifecycle_path.is_file() or lifecycle_path.is_symlink():
        raise ProjectionCutoverError(f"lifecycle SQLite is missing: {lifecycle_path}")
    allowed_job_states = {"pending", "running", "deferred"}
    allowed_receipt_states = {"pending", "running"}
    try:
        with sqlite3.connect(f"{lifecycle_path.as_uri()}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {
                "source_heads",
                "source_revisions",
                "projection_jobs",
                "projection_receipts",
            }
            if not required_tables.issubset(tables):
                raise ProjectionCutoverError(
                    "lifecycle projection precondition failed: "
                    "missing_projection_tables_count=1"
                )
            rows = connection.execute(
                """
                SELECT revisions.source_revision_id,
                       jobs.projection_job_id,
                       jobs.state,
                       receipts.projection_receipt_id,
                       receipts.generation_id,
                       receipts.state,
                       receipts.source_revision_id
                FROM source_heads AS heads
                LEFT JOIN source_revisions AS revisions
                  ON revisions.source_revision_id = heads.source_revision_id
                LEFT JOIN projection_jobs AS jobs
                  ON jobs.source_revision_id = revisions.source_revision_id
                 AND jobs.generation_id = ?
                LEFT JOIN projection_receipts AS receipts
                  ON receipts.projection_job_id = jobs.projection_job_id
                 AND receipts.backend = 'graph'
                WHERE revisions.tombstone = 0
                ORDER BY revisions.source_revision_id,
                         jobs.projection_job_id,
                         receipts.projection_receipt_id
                """,
                (generation_id,),
            ).fetchall()
    except ProjectionCutoverError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise ProjectionCutoverError("lifecycle SQLite cannot be read") from error

    rows_by_source: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        source_id = _normalize_id(row[0])
        rows_by_source.setdefault(source_id, []).append(tuple(row))

    missing_sources: set[str] = set()
    duplicate_sources: set[str] = set()
    invalid_job_ids: set[str] = set()
    invalid_receipt_ids: set[str] = set()
    mismatched_receipt_ids: set[str] = set()
    active_ids = set(rows_by_source)
    for source_id, source_rows in rows_by_source.items():
        job_ids = {
            _normalize_id(row[1])
            for row in source_rows
            if row[1] is not None
        }
        receipt_ids = {
            _normalize_id(row[3])
            for row in source_rows
            if row[3] is not None
        }
        if not job_ids or not receipt_ids:
            missing_sources.add(source_id)
        if len(job_ids) > 1 or len(receipt_ids) > 1:
            duplicate_sources.add(source_id)

        job_states: dict[str, set[str]] = {}
        receipt_states: dict[str, set[str]] = {}
        for row in source_rows:
            if row[1] is not None:
                job_id = _normalize_id(row[1])
                job_states.setdefault(job_id, set()).add(str(row[2]))
            if row[3] is not None:
                receipt_id = _normalize_id(row[3])
                receipt_states.setdefault(receipt_id, set()).add(str(row[5]))
                if (
                    str(row[4]) != generation_id
                    or _normalize_id(row[6]) != source_id
                ):
                    mismatched_receipt_ids.add(receipt_id)
        for job_id, states in job_states.items():
            if not states.issubset(allowed_job_states):
                invalid_job_ids.add(job_id)
        for receipt_id, states in receipt_states.items():
            if not states.issubset(allowed_receipt_states):
                invalid_receipt_ids.add(receipt_id)

    if (
        missing_sources
        or duplicate_sources
        or invalid_job_ids
        or invalid_receipt_ids
        or mismatched_receipt_ids
    ):
        raise ProjectionCutoverError(
            "lifecycle projection precondition failed: "
            f"active_source_count={len(active_ids)} "
            f"missing_job_or_receipt_count={len(missing_sources)} "
            f"missing_source_revision_ids={_safe_error_ids(missing_sources)} "
            f"duplicate_job_or_receipt_count={len(duplicate_sources)} "
            f"duplicate_source_revision_ids={_safe_error_ids(duplicate_sources)} "
            f"invalid_job_state_count={len(invalid_job_ids)} "
            f"invalid_job_ids={_safe_error_ids(invalid_job_ids)} "
            f"invalid_graph_receipt_state_count={len(invalid_receipt_ids)} "
            f"invalid_graph_receipt_ids={_safe_error_ids(invalid_receipt_ids)} "
            f"mismatched_graph_receipt_count={len(mismatched_receipt_ids)} "
            f"mismatched_graph_receipt_ids={_safe_error_ids(mismatched_receipt_ids)}"
        )
    return active_ids


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
    except sqlite3.Error as error:
        raise ProjectionCutoverError(f"cannot inspect SQLite table: {table}") from error
    columns = [str(row[1]) for row in rows]
    if not columns:
        raise ProjectionCutoverError(f"required SQLite table is missing: {table}")
    return columns


def _safe_graph_name(value: Any, *, dataset_id: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or Path(value).name != value
        or value.endswith(".lbug.wal")
        or not value.endswith(".lbug")
    ):
        raise ProjectionCutoverError(f"malformed graph mapping for dataset {dataset_id}")
    return value


def _safe_component(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or value in {".", ".."}
    ):
        raise ProjectionCutoverError(f"malformed graph mapping {label}")
    return value


def _uuid_path_component(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectionCutoverError(f"malformed graph mapping {label}")
    try:
        return str(UUID(value))
    except ValueError:
        return _safe_component(value, label=label)


def _generation_slug(generation_id: str) -> str:
    slug = _GENERATION_SAFE.sub("-", generation_id).strip(".-")
    return slug[:48] or "generation"


def _graph_mapping_plan(
    connection: sqlite3.Connection,
    target_root: Path,
    generation_id: str,
) -> list[dict[str, Any]]:
    columns = set(_table_columns(connection, "dataset_database"))
    required = {"owner_id", "dataset_id", "graph_database_name"}
    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns))
        raise ProjectionCutoverError(f"dataset_database schema is missing: {missing}")
    rows = connection.execute(
        "SELECT owner_id, dataset_id, graph_database_name FROM dataset_database ORDER BY dataset_id"
    ).fetchall()
    seen_dataset_ids: set[str] = set()
    seen_old_paths: set[Path] = set()
    seen_new_paths: set[Path] = set()
    digest = hashlib.sha256(generation_id.encode("utf-8")).hexdigest()[:12]
    slug = _generation_slug(generation_id)
    plan: list[dict[str, Any]] = []
    for row in rows:
        owner_value = row[0]
        owner = _uuid_path_component(owner_value, label="owner ID")
        dataset = _safe_component(row[1], label="dataset ID")
        normalized_dataset = _normalize_id(dataset)
        if normalized_dataset in seen_dataset_ids:
            raise ProjectionCutoverError(f"duplicate dataset mapping: {dataset}")
        seen_dataset_ids.add(normalized_dataset)
        old_name = _safe_graph_name(row[2], dataset_id=dataset)
        old_path = target_root / "cognee-system/databases" / owner / old_name
        if (
            not old_path.exists()
            and isinstance(owner_value, str)
            and owner_value != owner
        ):
            raw_owner_path = target_root / "cognee-system/databases" / owner_value / old_name
            if raw_owner_path.exists():
                old_path = raw_owner_path
        new_name = f"{dataset}.projection-{slug}-{digest}.lbug"
        new_path = target_root / "cognee-system/databases" / owner / new_name
        if old_path in seen_old_paths or new_path in seen_new_paths:
            raise ProjectionCutoverError(f"duplicate graph mapping for dataset {dataset}")
        if new_path.exists():
            raise ProjectionCutoverError(f"new graph path already exists: {new_path}")
        seen_old_paths.add(old_path)
        seen_new_paths.add(new_path)
        plan.append(
            {
                "dataset_id": row[1],
                "old": old_path.relative_to(target_root).as_posix(),
                "new": new_path.relative_to(target_root).as_posix(),
                "archive": None,
            }
        )
    return plan


def _graph_files(target_root: Path) -> list[Path]:
    database_root = target_root / "cognee-system/databases"
    if not database_root.is_dir() or database_root.is_symlink():
        raise ProjectionCutoverError(f"Cognee database tree is missing: {database_root}")
    paths: list[Path] = []
    for path in sorted(database_root.rglob("*")):
        if path.is_symlink():
            raise ProjectionCutoverError(
                f"Cognee database tree must not contain symbolic links: {path}"
            )
        if path.is_file() and path.name.endswith(_GRAPH_SUFFIXES):
            paths.append(path)
    return paths


def _move_graph_files(target_root: Path, paths: list[Path]) -> dict[str, str]:
    archive_root = target_root / "graph-archive"
    if archive_root.exists() and archive_root.is_symlink():
        raise ProjectionCutoverError("graph archive must not be a symbolic link")
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    moved: dict[str, str] = {}
    destinations: list[tuple[Path, Path]] = []
    for source in paths:
        relative = source.relative_to(target_root).as_posix()
        destination = archive_root / source.relative_to(target_root / "cognee-system/databases")
        if destination.exists() or destination.is_symlink():
            raise ProjectionCutoverError(f"graph archive path already exists: {destination}")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destinations.append((source, destination))
        moved[relative] = destination.relative_to(target_root).as_posix()
    for source, destination in destinations:
        shutil.move(os.fspath(source), os.fspath(destination))
    return moved


def _archive_pipeline_runs(
    connection: sqlite3.Connection,
    *,
    generation_id: str,
) -> int:
    columns = _table_columns(connection, "pipeline_runs")
    if "pipeline_name" not in columns:
        raise ProjectionCutoverError("pipeline_runs schema is missing pipeline_name")
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_ARCHIVE_TABLE,),
    ).fetchone()
    if existing is not None:
        raise ProjectionCutoverError(f"archive table already exists: {_ARCHIVE_TABLE}")
    source_schema_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_runs'"
    ).fetchone()
    source_schema = source_schema_row[0] if source_schema_row else None
    if not isinstance(source_schema, str) or not source_schema:
        raise ProjectionCutoverError("pipeline_runs schema is unavailable")
    archive_name = _identifier(_ARCHIVE_TABLE)
    source_name = _identifier("pipeline_runs")
    connection.execute(f"CREATE TABLE {archive_name} AS SELECT * FROM {source_name} WHERE 0")
    column_list = ", ".join(_identifier(column) for column in columns)
    cursor = connection.execute(
        f"INSERT INTO {archive_name} ({column_list}) "
        f"SELECT {column_list} FROM {source_name} WHERE pipeline_name = ?",
        ("cognify_pipeline",),
    )
    archived_count = cursor.rowcount
    connection.execute(
        f"CREATE TABLE {_identifier(_ARCHIVE_METADATA_TABLE)} ("
        "archive_table TEXT PRIMARY KEY, generation_id TEXT NOT NULL, "
        "source_table TEXT NOT NULL, source_schema TEXT NOT NULL, "
        "archived_row_count INTEGER NOT NULL, archived_at TEXT NOT NULL)"
    )
    connection.execute(
        f"INSERT INTO {_identifier(_ARCHIVE_METADATA_TABLE)} VALUES (?, ?, ?, ?, ?, ?)",
        (
            _ARCHIVE_TABLE,
            generation_id,
            "pipeline_runs",
            source_schema,
            archived_count,
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.execute(
        "DELETE FROM pipeline_runs WHERE pipeline_name = ?", ("cognify_pipeline",)
    )
    return archived_count


def _clear_active_data_status(
    connection: sqlite3.Connection,
    active_ids: set[str],
) -> int:
    columns = _table_columns(connection, "data")
    if not {"id", "pipeline_status"}.issubset(columns):
        raise ProjectionCutoverError("data schema is missing pipeline status")
    cleared = 0
    rows = connection.execute("SELECT id, pipeline_status FROM data").fetchall()
    for row in rows:
        if _normalize_id(row[0]) not in active_ids:
            continue
        raw_status = row[1]
        if raw_status is None:
            continue
        try:
            status = json.loads(raw_status) if isinstance(raw_status, (str, bytes)) else raw_status
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectionCutoverError(f"data pipeline_status is invalid for {row[0]}") from error
        if not isinstance(status, dict):
            raise ProjectionCutoverError(f"data pipeline_status is not an object for {row[0]}")
        if "cognify_pipeline" not in status:
            continue
        del status["cognify_pipeline"]
        connection.execute(
            "UPDATE data SET pipeline_status = ? WHERE id = ?",
            (_canonical_json(status), row[0]),
        )
        cleared += 1
    return cleared


def _write_receipt(target_root: Path, receipt: dict[str, Any]) -> Path:
    receipt_path = target_root / PROJECTION_CUTOVER_RECEIPT
    if receipt_path.is_symlink():
        raise ProjectionCutoverError("projection cutover receipt must not be a symbolic link")
    receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = dict(receipt)
    payload["receipt_sha256"] = _receipt_digest(payload)
    temporary = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, receipt_path)
    except OSError as error:
        # Best-effort cleanup: the temporary file may already be gone, and the
        # original OSError below is the failure that matters.
        with suppress(OSError):
            temporary.unlink()
        raise ProjectionCutoverError("projection cutover receipt could not be written") from error
    return receipt_path


def prepare_projection_cutover(
    *,
    backup_root: Path,
    target_root: Path,
    generation_id: str,
    manifest_sha256: str,
    snapshot_store: Any | None = None,
) -> dict[str, Any]:
    """Restore a sealed Lite backup and move its graph projection to new paths."""
    del snapshot_store
    generation = _require_generation(generation_id)
    manifest_hash = _require_manifest_hash(manifest_sha256)
    target = _direct_path(Path(target_root), label="projection cutover target")
    backup = _direct_path(Path(backup_root), label="projection cutover backup")
    if target == backup or backup in target.parents:
        raise ProjectionCutoverError(
            f"projection cutover target must not be inside the backup: {target}"
        )
    if target.exists() and target.is_symlink():
        raise ProjectionCutoverError(
            f"projection cutover target must not be a symbolic link: {target}"
        )
    if target.exists() and not target.is_dir():
        raise ProjectionCutoverError(f"projection cutover target is not a directory: {target}")
    if target.exists():
        receipt = _read_existing_receipt(
            target,
            generation_id=generation,
            manifest_sha256=manifest_hash,
        )
        if receipt is not None:
            return {**receipt, "ok": True, "idempotent": True}
        if any(target.iterdir()):
            raise ProjectionCutoverError(
                "projection cutover target must be empty or contain a matching receipt"
            )
    else:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = target.with_name(f".{target.name}.projection-cutover-{os.getpid()}.tmp")
    if stage.exists() or stage.is_symlink():
        raise ProjectionCutoverError(f"projection cutover staging path already exists: {stage}")
    stage.mkdir(mode=0o700)
    target_removed = False
    try:
        from kb import generation_backup as backup_module

        target_layout = _layout(stage, backup_module)
        manifest = _validate_manifest(
            backup,
            target_layout=target_layout,
            generation_id=generation,
            manifest_sha256=manifest_hash,
            backup_module=backup_module,
        )
        backup_module._restore_lite_state(backup / "local", target_layout)
        backup_module._verify_restored_inventory(target_layout, manifest["local_files"])
        active_ids = _validate_lifecycle_projection(
            target_layout.lifecycle_file,
            generation_id=generation,
        )
        graph_paths = _graph_files(stage)
        with sqlite3.connect(target_layout.database_file) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                graph_plan = _graph_mapping_plan(connection, stage, generation)
                archived_count = _archive_pipeline_runs(connection, generation_id=generation)
                cleared_count = _clear_active_data_status(connection, active_ids)
                for entry in graph_plan:
                    connection.execute(
                        "UPDATE dataset_database SET graph_database_name = ? WHERE dataset_id = ?",
                        (Path(entry["new"]).name, entry["dataset_id"]),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        archived_graphs = _move_graph_files(stage, graph_paths)
        receipt_graph_plan = [
            {
                "old": entry["old"],
                "new": entry["new"],
                "archive": archived_graphs.get(entry["old"]),
            }
            for entry in graph_plan
        ]
        receipt = {
            "schema": PROJECTION_CUTOVER_SCHEMA,
            "schema_version": PROJECTION_CUTOVER_SCHEMA_VERSION,
            "generation_id": generation,
            "manifest_sha256": manifest_hash,
            "archived_pipeline_run_count": archived_count,
            "cleared_data_count": cleared_count,
            "remapped_dataset_count": len(graph_plan),
            "old_new_graph_paths": receipt_graph_plan,
            "archived_graph_paths": [
                archived_graphs[key] for key in sorted(archived_graphs)
            ],
        }
        _write_receipt(stage, receipt)
        if target.exists():
            if any(target.iterdir()):
                raise ProjectionCutoverError(
                    "projection cutover target changed while preparing cutover"
                )
            target.rmdir()
            target_removed = True
        os.replace(stage, target)
    except Exception:
        if target_removed and not target.exists():
            target.mkdir(mode=0o700)
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise
    return {**receipt, "ok": True, "idempotent": False}
