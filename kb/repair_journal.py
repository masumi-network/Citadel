"""Durable, content-free audit records for corpus repair operations."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
import json
import os
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any


_REPAIR_PHASES = {
    "started",
    "preflight",
    "delete",
    "cognify",
    "post_census",
    "post_index_check",
    "recovery",
    "completed",
}
_REPAIR_STATUSES = {"started", "completed", "failed"}
_SOURCE_MANIFEST_FIELDS = {
    "content_hash",
    "raw_content_hash",
    "data_size",
    "raw_data_size",
    "updated_at",
    "source_readable",
}


class RepairJournalLeaseError(RuntimeError):
    """Another process currently owns the destructive repair lease."""


def _normalize_source_manifest(
    source_manifest: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Keep recovery evidence content-free and JSON-safe."""
    if source_manifest is None:
        return {}
    if not isinstance(source_manifest, Mapping):
        raise ValueError("source manifest must be a mapping")

    normalized: dict[str, dict[str, Any]] = {}
    for document_id, raw_entry in source_manifest.items():
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("source manifest contains an invalid document id")
        if not isinstance(raw_entry, Mapping):
            raise ValueError("source manifest entry must be a mapping")
        entry: dict[str, Any] = {}
        for key in _SOURCE_MANIFEST_FIELDS:
            value = raw_entry.get(key)
            if value is None:
                continue
            if key in {"data_size", "raw_data_size"}:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"source manifest {key} must be a non-negative integer")
            elif key == "source_readable":
                if value is not True:
                    raise ValueError("source manifest source_readable must be true")
            elif not isinstance(value, str) or not value:
                raise ValueError(f"source manifest {key} must be a non-empty string")
            entry[key] = value
        if not entry:
            raise ValueError("source manifest entry has no verifiable fields")
        normalized[document_id] = dict(sorted(entry.items()))
    return dict(sorted(normalized.items()))


def _normalize_repair_document_datasets(
    repair_document_datasets: Mapping[str, Iterable[str]] | None,
) -> dict[str, list[str]]:
    if repair_document_datasets is None:
        return {}
    if not isinstance(repair_document_datasets, Mapping):
        raise ValueError("repair document datasets must be a mapping")
    normalized: dict[str, list[str]] = {}
    for document_id, datasets in repair_document_datasets.items():
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("repair document datasets contains an invalid document id")
        if isinstance(datasets, (str, bytes)) or not isinstance(datasets, Iterable):
            raise ValueError("repair document datasets entry must be iterable")
        values = sorted({str(dataset) for dataset in datasets if str(dataset)})
        if not values:
            raise ValueError("repair document datasets entry cannot be empty")
        normalized[document_id] = values
    return dict(sorted(normalized.items()))


class RepairJournal:
    """Append repair lifecycle events without persisting document content."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def lease(self):
        """Serialize destructive repair across processes sharing the state volume."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows has no fcntl.
                yield
                return
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepairJournalLeaseError("repair journal lease is busy") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def append(
        self,
        *,
        operation_id: str,
        dataset: str | None,
        phase: str,
        status: str,
        repair_document_ids: Iterable[str],
        repair_datasets: Iterable[str],
        repair_document_datasets: Mapping[str, Iterable[str]] | None = None,
        deleted_document_ids: Iterable[str] = (),
        error_type: str | None = None,
        reason: str | None = None,
        post_repair_indexed: bool | None = None,
        post_repair_stored_budget_ok: bool | None = None,
        post_repair_census_ok: bool | None = None,
        projections_preserved: bool | None = None,
        source_manifest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if phase not in _REPAIR_PHASES:
            raise ValueError(f"invalid repair journal phase: {phase}")
        if status not in _REPAIR_STATUSES:
            raise ValueError(f"invalid repair journal status: {status}")
        record: dict[str, object] = {
            "schema_version": 2,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "operation_id": operation_id,
            "dataset": dataset,
            "phase": phase,
            "status": status,
            "repair_document_ids": sorted({str(item) for item in repair_document_ids}),
            "repair_datasets": sorted({str(item) for item in repair_datasets}),
        }
        normalized_repair_mapping = _normalize_repair_document_datasets(repair_document_datasets)
        if normalized_repair_mapping:
            record["repair_document_datasets"] = normalized_repair_mapping
        deleted = sorted({str(item) for item in deleted_document_ids})
        if deleted:
            record["deleted_document_ids"] = deleted
        if error_type:
            record["error_type"] = error_type
        if reason:
            record["reason"] = reason
        if post_repair_indexed is not None:
            record["post_repair_indexed"] = post_repair_indexed
        if post_repair_stored_budget_ok is not None:
            record["post_repair_stored_budget_ok"] = post_repair_stored_budget_ok
        if post_repair_census_ok is not None:
            record["post_repair_census_ok"] = post_repair_census_ok
        if projections_preserved is not None:
            record["projections_preserved"] = projections_preserved
        normalized_manifest = _normalize_source_manifest(source_manifest)
        if normalized_manifest:
            record["source_manifest"] = normalized_manifest

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def pending_operations(self) -> list[dict[str, Any]]:
        """Return operations whose latest record does not prove a terminal state.

        A process can die after a phase-start or phase-complete record and before
        the next phase is written. Treat every non-terminal latest phase as an
        interrupted operation so a new process cannot start another destructive
        repair on an unknown projection state.
        """
        if not self.path.exists():
            return []

        latest: dict[str, dict[str, Any]] = {}
        source_manifests: dict[str, dict[str, Any]] = {}
        repair_mappings: dict[str, dict[str, list[str]]] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError("repair journal cannot be read") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"repair journal contains invalid JSON at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise RuntimeError(f"repair journal record at line {line_number} is not an object")
            operation_id = record.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise RuntimeError(
                    f"repair journal record at line {line_number} has no operation id"
                )
            latest[operation_id] = record
            if isinstance(record.get("source_manifest"), Mapping):
                source_manifests[operation_id] = dict(record["source_manifest"])
            if isinstance(record.get("repair_document_datasets"), Mapping):
                repair_mappings[operation_id] = dict(record["repair_document_datasets"])

        def is_terminal(record: dict[str, Any]) -> bool:
            status = record.get("status")
            if status == "failed":
                # Preflight failures happen before destructive work. A later
                # failure is terminal only when rollback was explicitly proven.
                return record.get("phase") == "preflight" or (
                    record.get("projections_preserved") is True
                )
            if record.get("phase") == "recovery":
                return (
                    status == "completed"
                    and record.get("post_repair_census_ok") is True
                    and record.get("post_repair_indexed") is True
                )
            return record.get("phase") in {"completed", "post_index_check", "recovery"} and (
                status == "completed"
            )

        pending: list[dict[str, Any]] = []
        for operation_id, record in latest.items():
            if is_terminal(record):
                continue
            if "source_manifest" not in record and operation_id in source_manifests:
                record = {**record, "source_manifest": source_manifests[operation_id]}
            if "repair_document_datasets" not in record and operation_id in repair_mappings:
                record = {
                    **record,
                    "repair_document_datasets": repair_mappings[operation_id],
                }
            pending.append(record)
        return sorted(
            pending,
            key=lambda record: str(record.get("operation_id")),
        )
