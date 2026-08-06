"""Durable, content-free audit records for corpus repair operations."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Iterable


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
    ) -> None:
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

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
