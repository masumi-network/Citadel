"""Durable, content-free audit records for corpus repair operations."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Iterable


_REPAIR_PHASES = {
    "started",
    "preflight",
    "delete",
    "cognify",
    "post_census",
    "post_index_check",
    "completed",
}
_REPAIR_STATUSES = {"started", "completed", "failed"}


class RepairJournal:
    """Append repair lifecycle events without persisting document content."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        operation_id: str,
        dataset: str | None,
        phase: str,
        status: str,
        repair_document_ids: Iterable[str],
        repair_datasets: Iterable[str],
        deleted_document_ids: Iterable[str] = (),
        error_type: str | None = None,
        reason: str | None = None,
        post_repair_indexed: bool | None = None,
        post_repair_stored_budget_ok: bool | None = None,
        projections_preserved: bool | None = None,
    ) -> None:
        if phase not in _REPAIR_PHASES:
            raise ValueError(f"invalid repair journal phase: {phase}")
        if status not in _REPAIR_STATUSES:
            raise ValueError(f"invalid repair journal status: {status}")
        record: dict[str, object] = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "operation_id": operation_id,
            "dataset": dataset,
            "phase": phase,
            "status": status,
            "repair_document_ids": sorted({str(item) for item in repair_document_ids}),
            "repair_datasets": sorted({str(item) for item in repair_datasets}),
        }
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
        if projections_preserved is not None:
            record["projections_preserved"] = projections_preserved

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
                raise RuntimeError(
                    f"repair journal record at line {line_number} is not an object"
                )
            operation_id = record.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise RuntimeError(
                    f"repair journal record at line {line_number} has no operation id"
                )
            latest[operation_id] = record

        def is_terminal(record: dict[str, Any]) -> bool:
            status = record.get("status")
            if status == "failed":
                # Preflight failures happen before destructive work. A later
                # failure is terminal only when rollback was explicitly proven.
                return record.get("phase") == "preflight" or (
                    record.get("projections_preserved") is True
                )
            return record.get("phase") in {"completed", "post_index_check"} and (
                status == "completed"
            )

        pending = [record for record in latest.values() if not is_terminal(record)]
        return sorted(
            pending,
            key=lambda record: str(record.get("operation_id")),
        )
