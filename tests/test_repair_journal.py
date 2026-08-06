from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from kb.config import CitadelConfig
from kb.repair_journal import RepairJournal, RepairJournalLeaseError
from kb.service import Citadel


def test_pending_operations_returns_latest_non_terminal_phase(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    journal = RepairJournal(path)
    journal.append(
        operation_id="finished",
        dataset="notes",
        phase="started",
        status="started",
        repair_document_ids=["doc-finished"],
        repair_datasets=["notes"],
    )
    journal.append(
        operation_id="finished",
        dataset="notes",
        phase="completed",
        status="completed",
        repair_document_ids=["doc-finished"],
        repair_datasets=["notes"],
    )
    journal.append(
        operation_id="failed",
        dataset="notes",
        phase="preflight",
        status="failed",
        repair_document_ids=["doc-failed"],
        repair_datasets=["notes"],
        reason="repair_delete_failed",
    )
    journal.append(
        operation_id="interrupted",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc-interrupted"],
        repair_datasets=["notes"],
    )

    pending = journal.pending_operations()

    assert [record["operation_id"] for record in pending] == ["interrupted"]
    assert pending[0]["phase"] == "delete"
    assert pending[0]["repair_document_ids"] == ["doc-interrupted"]


def test_pending_operations_keeps_failed_delete_without_rollback_proof(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    journal = RepairJournal(path)
    journal.append(
        operation_id="unsafe-failure",
        dataset="notes",
        phase="cognify",
        status="failed",
        repair_document_ids=["doc-unsafe"],
        repair_datasets=["notes"],
        deleted_document_ids=["doc-unsafe"],
        projections_preserved=False,
    )
    journal.append(
        operation_id="safe-failure",
        dataset="notes",
        phase="delete",
        status="failed",
        repair_document_ids=["doc-safe"],
        repair_datasets=["notes"],
        deleted_document_ids=["doc-safe"],
        projections_preserved=True,
    )

    pending = journal.pending_operations()

    assert [record["operation_id"] for record in pending] == ["unsafe-failure"]


def test_pending_operations_raises_on_malformed_record(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    path.write_text('{"operation_id":"op"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid JSON at line 2"):
        RepairJournal(path).pending_operations()


def test_pending_operations_does_not_return_missing_journal(tmp_path) -> None:
    assert RepairJournal(tmp_path / "missing.jsonl").pending_operations() == []


def test_repair_lease_rejects_concurrent_holder_and_releases(tmp_path) -> None:
    pytest.importorskip("fcntl")
    journal = RepairJournal(tmp_path / "repair.jsonl")

    with journal.lease():
        with pytest.raises(RepairJournalLeaseError, match="lease is busy"):
            with journal.lease():
                pass

    with journal.lease():
        pass


def test_pending_operations_keeps_journal_content_json_only(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    journal = RepairJournal(path)
    journal.append(
        operation_id="op",
        dataset="notes",
        phase="cognify",
        status="started",
        repair_document_ids=["doc"],
        repair_datasets=["notes"],
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert "text" not in record
    assert journal.pending_operations()[0]["operation_id"] == "op"


def test_pending_operations_carries_source_manifest_across_phases(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    journal = RepairJournal(path)
    manifest = {
        "doc": {
            "content_hash": "content-hash",
            "raw_content_hash": "raw-hash",
            "data_size": 42,
            "updated_at": "2026-08-06T20:00:00+00:00",
        }
    }
    journal.append(
        operation_id="op",
        dataset="notes",
        phase="started",
        status="started",
        repair_document_ids=["doc"],
        repair_datasets=["notes"],
        source_manifest=manifest,
    )
    journal.append(
        operation_id="op",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc"],
        repair_datasets=["notes"],
    )

    pending = journal.pending_operations()

    assert pending[0]["phase"] == "delete"
    assert pending[0]["source_manifest"] == manifest


def test_recovery_phase_is_terminal_only_after_postcheck(tmp_path) -> None:
    path = tmp_path / "repair.jsonl"
    journal = RepairJournal(path)
    fields = {
        "operation_id": "op",
        "dataset": "notes",
        "repair_document_ids": ["doc"],
        "repair_datasets": ["notes"],
    }
    journal.append(phase="recovery", status="started", **fields)
    assert journal.pending_operations()[0]["operation_id"] == "op"

    journal.append(phase="recovery", status="completed", **fields)
    assert journal.pending_operations()[0]["operation_id"] == "op"

    journal.append(
        phase="recovery",
        status="completed",
        post_repair_census_ok=True,
        post_repair_indexed=True,
        **fields,
    )
    assert journal.pending_operations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_method", ["combined", "oversized", "zero"])
async def test_repair_apply_fails_closed_on_interrupted_journal(
    tmp_path, repair_method: str
) -> None:
    journal_path = tmp_path / "repair.jsonl"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "crashed-operation",
                "dataset": "notes",
                "phase": "delete",
                "status": "started",
                "repair_document_ids": ["doc-a"],
                "repair_datasets": ["notes"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class NoopCognee:
        @asynccontextmanager
        async def maintenance(self):
            raise AssertionError("interrupted repair must fence before locking")
            yield

    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=NoopCognee(),
    )

    if repair_method == "combined":
        result = await kb.reconcile_corpus(apply=True, force=True)
    elif repair_method == "oversized":
        result = await kb.reconcile_oversized_chunks(apply=True, force=True)
    else:
        result = await kb.reconcile_zero_chunk_documents(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_interrupted"
    assert result["pending_operations"][0]["operation_id"] == "crashed-operation"
