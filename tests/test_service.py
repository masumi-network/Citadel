from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.lifecycle import (
    LifecycleConflictError,
    LifecycleRequeueIdentityMismatchError,
    lifecycle_chunk_source_key,
)
from kb.models import FeedbackRequest
from kb.repair_journal import RepairJournal
from kb.security_scan import SecretContentError
from kb.service import MAX_SEARCH_TOP_K, Citadel
import kb.service as service


class FakeCognee:
    def __init__(self) -> None:
        self.remember_calls: list[dict[str, Any]] = []
        self.feedback_calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []
        self.cognify_calls: list[dict[str, Any]] = []
        self.nodes: list[Any] = []
        self.edges: list[Any] = []
        self._pending: list[Any] = []

    @asynccontextmanager
    async def maintenance(self):
        yield

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_calls.append({"data": data, **kwargs})
        # Cognee.add stores data, but it only enters the graph once cognify
        # runs — the modern remember path does not cognify inline.
        self._pending.append(data)
        return {"ok": True}

    async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"query": query, **kwargs}]

    async def add_feedback(self, **kwargs: Any) -> bool:
        self.feedback_calls.append(kwargs)
        return True

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        return {"improved": True}

    async def get_document(self, document_id: str) -> dict[str, Any]:
        return {"id": document_id, "body": "A repairable document."}

    async def restore_document_chunks(self, deleted: dict[str, Any]) -> bool:
        return True

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        self.cognify_calls.append(kwargs)
        # Cognify turns added-but-uncognified data into graph nodes.
        self.nodes.extend(self._pending)
        self._pending.clear()
        return {"cognified": True}

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return list(self.nodes), list(self.edges)

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]:
        return [
            str(call["data_id"])
            for call in self.remember_calls
            if call.get("dataset_name") in datasets and call.get("data_id") is not None
        ]

    async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
        return {document_id: 1 for document_id in document_ids}

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str]:
        del datasets
        return set(document_ids)


class EmptyCognee(FakeCognee):
    async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def test_default_cognee_client_receives_configured_retry_queue_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    captured: dict[str, Any] = {}

    class ConstructedClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(service, "CogneePublicClient", ConstructedClient)
    path = tmp_path / "cognify-queue.json"
    Citadel(CitadelConfig(cognify_queue_path=str(path)))

    assert captured == {"queue_path": str(path)}


@pytest.mark.asyncio
async def test_ingest_applies_tags_and_dataset() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes", default_tags=("personal",)), cognee=fake)

    result = await kb.ingest("A useful note", tags=["AI"])

    assert result.accepted
    assert result.tags == ("personal", "ai")
    assert fake.remember_calls[0]["dataset_name"] == "notes"
    assert fake.remember_calls[0]["tags"] == ("personal", "ai")


@pytest.mark.asyncio
async def test_lifecycle_ingest_queues_durable_projection_and_returns_operation_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    monkeypatch.setenv("DB_PROVIDER", "sqlite")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "ladybug")
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(
            default_dataset="seat:alice",
            user_id="alice",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )

    result = await kb.ingest(
        "A retained lifecycle note",
        tags=["architecture"],
        session_id="session-1",
        source_key="manual:alice:note-1",
        source_locator="citadel://manual/note-1",
        capture_run_id="capture-1",
    )
    operation_payload = await kb.wait_for_lifecycle_operation(result.projection_job_id)

    assert result.accepted is True
    assert result.reason == "queued_not_confirmed"
    assert result.source_revision_id is not None
    assert result.projection_job_id is not None
    assert result.projection_state == "pending"
    assert fake.remember_calls == [
        {
            "data": "A retained lifecycle note",
            "dataset_name": "seat:alice",
            "data_id": result.source_revision_id,
            "defer_cognify": True,
            "tags": ("architecture",),
            "session_id": "session-1",
        }
    ]
    operation = kb.lifecycle_store.get_operation(result.projection_job_id)
    assert operation.state == "searchable"
    assert operation_payload["state"] == "searchable"
    assert operation.source_revision.source_key == "manual:alice:note-1"


def test_lifecycle_config_digest_tracks_projection_affecting_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "256")
    kb = Citadel(
        CitadelConfig(
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    initial = kb._lifecycle_projection_request().config_digest

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    provider_changed = kb._lifecycle_projection_request().config_digest
    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "512")
    budget_changed = kb._lifecycle_projection_request().config_digest

    assert provider_changed != initial
    assert budget_changed != provider_changed


@pytest.mark.asyncio
async def test_lifecycle_restart_rejects_config_drift_until_generation_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    store_path = str(tmp_path / "lifecycle.sqlite3")
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    monkeypatch.setenv("EMBEDDING_MODEL", "model-1")
    original = Citadel(
        CitadelConfig(lifecycle_enabled=True, lifecycle_store_path=store_path),
        cognee=FakeCognee(),
    )
    accepted = await original.ingest("config-bound source", source_key="manual:config")
    await original.wait_for_lifecycle_operation(accepted.projection_job_id)

    monkeypatch.setenv("EMBEDDING_MODEL", "model-2")
    with pytest.raises(LifecycleConflictError, match="CITADEL_GENERATION_ID"):
        Citadel(
            CitadelConfig(lifecycle_enabled=True, lifecycle_store_path=store_path),
            cognee=FakeCognee(),
        )

    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-2")
    restarted = Citadel(
        CitadelConfig(lifecycle_enabled=True, lifecycle_store_path=store_path),
        cognee=FakeCognee(),
    )
    assert restarted.lifecycle_worker is not None


@pytest.mark.asyncio
async def test_lifecycle_search_returns_only_current_searchable_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    class LifecycleRecallCognee(FakeCognee):
        recall_ids: list[str] = []
        recall_top_k: int | None = None
        allowed_document_ids: list[str] | None = None

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_top_k = int(kwargs["top_k"])
            self.allowed_document_ids = list(kwargs["document_ids"])
            allowed = set(self.allowed_document_ids)
            return [
                {
                    "id": f"chunk-{index}",
                    "document_id": document_id,
                    "text": f"revision {index}",
                }
                for index, document_id in enumerate(
                    [item for item in self.recall_ids if item in allowed][
                        : self.recall_top_k
                    ]
                )
            ]

    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    fake = LifecycleRecallCognee()
    kb = Citadel(
        CitadelConfig(
            default_dataset="seat:alice",
            user_id="alice",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )
    first = await kb.ingest(
        "revision one",
        source_key="manual:alice:current-only",
    )
    await kb.wait_for_lifecycle_operation(first.projection_job_id)
    second = await kb.ingest(
        "revision two",
        source_key="manual:alice:current-only",
    )
    await kb.wait_for_lifecycle_operation(second.projection_job_id)
    fake.recall_ids = [first.source_revision_id, second.source_revision_id]

    results = await kb.search("revision", top_k=1)

    assert fake.recall_top_k == MAX_SEARCH_TOP_K
    assert fake.allowed_document_ids == [second.source_revision_id]
    assert [result["document_id"] for result in results] == [
        second.source_revision_id
    ]
    assert results[0]["_lifecycle"]["source_revision_id"] == second.source_revision_id
    assert results[0]["_lifecycle"]["backend"] == "vector"
    assert results[0]["_lifecycle"]["state"] == "searchable"


@pytest.mark.asyncio
async def test_lifecycle_duplicate_ingest_returns_same_operation(
    tmp_path: Any,
) -> None:
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )

    first = await kb.ingest("idempotent source", source_key="connector:stable")
    duplicate = await kb.ingest("idempotent source", source_key="connector:stable")

    assert duplicate.accepted is True
    assert duplicate.source_revision_id == first.source_revision_id
    assert duplicate.projection_job_id == first.projection_job_id
    assert kb.lifecycle_census()["source_revisions"] == 1
    assert kb.lifecycle_census()["projection_jobs"] == 1


def test_lifecycle_requeue_requires_active_worker_identity(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )

    preview = kb.lifecycle_requeue_failed_preview()
    assert preview["candidate_ids"] == []
    applied = kb.lifecycle_requeue_failed(
        generation_id=preview["generation_id"],
        projection_version=preview["projection_version"],
        config_digest=preview["config_digest"],
        expected_count=0,
        candidate_ids=(),
    )
    assert applied["applied"] is True
    assert applied["requeued"] == 0

    assert kb.lifecycle_worker is not None
    kb.lifecycle_worker.generation_id = "generation-stale"
    with pytest.raises(LifecycleRequeueIdentityMismatchError):
        kb.lifecycle_requeue_failed(
            generation_id=preview["generation_id"],
            projection_version=preview["projection_version"],
            config_digest=preview["config_digest"],
            expected_count=0,
            candidate_ids=(),
        )


@pytest.mark.asyncio
async def test_lifecycle_tombstone_failed_missing_path_jobs(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from kb.lifecycle import CaptureContext

    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    assert kb.lifecycle_store is not None
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    projection = kb._lifecycle_projection_request()
    accepted = kb.lifecycle_store.accept_source(
        b"/private/tmp/claude-501/marker3_pathfile.txt",
        capture=CaptureContext(
            dataset="seat:citadel-dev-team",
            source_key="manual:marker3-pathfile",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-pathfile",
            captured_at=now,
        ),
        projection=projection,
        now=now,
    )
    lease = kb.lifecycle_store.claim_next_job(
        worker_id="w1",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
        now=now,
        lease_seconds=30,
    )
    assert lease is not None
    kb.lifecycle_store.fail_job(
        lease,
        error_code="FileNotFoundError",
        error_message="Storage directory does not exist: '/private/tmp/claude-501/x'",
        now=now,
    )

    preview = kb.lifecycle_tombstone_failed_preview()
    assert preview["applied"] is False
    assert preview["candidate_ids"] == [accepted.projection_job_id]
    assert preview["candidates"][0]["source_key"] == "manual:marker3-pathfile"

    applied = await kb.lifecycle_tombstone_failed(
        generation_id=preview["generation_id"],
        projection_version=preview["projection_version"],
        config_digest=preview["config_digest"],
        expected_count=preview["candidate_count"],
        candidate_ids=tuple(preview["candidate_ids"]),
    )
    assert applied["applied"] is True
    assert applied["tombstoned"] == 1
    current = kb.lifecycle_store.current_revisions_for_source(
        "seat:citadel-dev-team",
        "manual:marker3-pathfile",
        include_chunks=False,
    )
    assert len(current) == 1
    assert current[0].tombstone is True
    assert kb.lifecycle_tombstone_failed_preview()["candidate_ids"] == []


def test_lifecycle_requeue_preview_does_not_mutate_failed_jobs(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3
    from datetime import UTC, datetime

    from kb.lifecycle import CaptureContext

    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    assert kb.lifecycle_store is not None
    projection = kb._lifecycle_projection_request()
    accepted = kb.lifecycle_store.accept_source(
        b"preview must not mutate",
        capture=CaptureContext(
            dataset="central",
            source_key="manual:preview-no-mutation",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="test",
            capture_run_id="preview-no-mutation",
            captured_at=datetime.now(UTC),
        ),
        projection=projection,
    )
    with sqlite3.connect(kb.lifecycle_store.path) as connection:
        connection.execute(
            "UPDATE projection_jobs SET state = 'failed' WHERE projection_job_id = ?",
            (accepted.projection_job_id,),
        )
        connection.execute(
            "UPDATE projection_receipts SET state = 'failed' WHERE projection_job_id = ?",
            (accepted.projection_job_id,),
        )

    before = kb.lifecycle_store.get_operation(accepted.projection_job_id)
    preview = kb.lifecycle_requeue_failed_preview()
    after = kb.lifecycle_store.get_operation(accepted.projection_job_id)

    assert preview["candidate_ids"] == [accepted.projection_job_id]
    assert before.job.state == after.job.state == "failed"
    assert {receipt.state for receipt in before.receipts} == {"failed"}
    assert {receipt.state for receipt in after.receipts} == {"failed"}


@pytest.mark.asyncio
async def test_lifecycle_ingest_does_not_retain_legacy_process_dedup_keys(
    tmp_path: Any,
) -> None:
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )

    await kb.ingest("first unique source", source_key="connector:first")
    await kb.ingest("second unique source", source_key="connector:second")

    assert kb._seen_ingest_keys == set()


@pytest.mark.asyncio
async def test_lifecycle_tombstone_covers_all_current_chunks(
    tmp_path: Any,
) -> None:
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    chunk_keys = [
        lifecycle_chunk_source_key("connector:item", index) for index in range(2)
    ]
    await kb.ingest(
        "chunk zero",
        source_key=chunk_keys[0],
        _lifecycle_parent_source_key="connector:item",
        _lifecycle_chunk_index=0,
    )
    await kb.ingest(
        "chunk one",
        source_key=chunk_keys[1],
        _lifecycle_parent_source_key="connector:item",
        _lifecycle_chunk_index=1,
    )
    await kb.wait_for_lifecycle_idle()

    tombstones = await kb.tombstone_source(
        dataset="central",
        source_key="connector:item",
        reason="upstream source deleted",
        capture_actor_id="connector-sync",
    )
    await kb.wait_for_lifecycle_idle()

    assert len(tombstones) == 2
    current = kb.lifecycle_store.current_revisions_for_source(
        "central",
        "connector:item",
    )
    assert {revision.source_key for revision in current} == set(chunk_keys)
    assert all(revision.tombstone for revision in current)


@pytest.mark.asyncio
async def test_lifecycle_chunk_namespace_does_not_capture_colon_suffixed_source(
    tmp_path: Any,
) -> None:
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    await kb.ingest("parent source", source_key="github:o/r:path:foo")
    await kb.ingest("unrelated source", source_key="github:o/r:path:foo:chunk:0")
    await kb.wait_for_lifecycle_idle()

    tombstones = await kb.tombstone_source(
        dataset="central",
        source_key="github:o/r:path:foo",
        reason="parent deleted",
    )

    assert len(tombstones) == 1
    unrelated = kb.lifecycle_store.get_current_revision(
        "central",
        "github:o/r:path:foo:chunk:0",
    )
    assert unrelated.tombstone is False


@pytest.mark.asyncio
async def test_lifecycle_rebuild_does_not_run_through_current_generation_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    kb = Citadel(
        CitadelConfig(
            default_dataset="central",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=FakeCognee(),
    )
    accepted = await kb.ingest("rebuild retained source", source_key="connector:stable")
    await kb.wait_for_lifecycle_operation(accepted.projection_job_id)

    rebuild_job_ids = kb.queue_lifecycle_rebuild(generation_id="generation-2")
    await kb.wait_for_lifecycle_idle()

    assert len(rebuild_job_ids) == 1
    rebuilt = kb.lifecycle_operation(rebuild_job_ids[0])
    assert rebuilt["job"]["generation_id"] == "generation-2"
    assert rebuilt["state"] == "pending"
    generation = kb.lifecycle_generation_census(
        generation_id="generation-2",
        projection_version=rebuilt["job"]["projection_version"],
    )
    assert generation["current_sources"] == 1
    assert generation["current_projection_jobs"] == 1
    assert generation["current_searchable_by_backend"] == {}


@pytest.mark.asyncio
async def test_ingest_blocks_high_severity_secret() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    with pytest.raises(SecretContentError) as exc_info:
        await kb.ingest("AWS key AKIAIOSFODNN7EXAMPLE leaked here")

    error = exc_info.value
    assert error.highest_severity in {"critical", "high"}
    assert error.findings  # carries redacted finding metadata
    # The raw secret must never appear in what we surface to callers.
    assert "AKIAIOSFODNN7EXAMPLE" not in error.public_message
    # Nothing reached the vault.
    assert fake.remember_calls == []


@pytest.mark.asyncio
async def test_ingest_allows_clean_content() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest("A perfectly ordinary engineering note about caching.")

    assert result.accepted
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_rejects_denied_env_path_on_source_locator() -> None:
    """Org deny globs must run at ingest, not only as JSON on the capture policy.

    `CITADEL_ADMIN_KEY=…` does not match `secret_assignment`. A `.env` file
    posted as ordinary text with a GitHub blob locator is the hole. Dropping
    the locator check (and leaving regex-only `_guard_content`) turns this red.
    """
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest(
        "NOTE=hello-from-an-env-file",
        source_locator="https://github.com/org/repo/blob/main/.env",
    )

    assert result.accepted is False
    assert result.reason == "excluded_path"
    assert fake.remember_calls == []


@pytest.mark.asyncio
async def test_ingest_rejects_absolute_env_path_as_data() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest("/Users/dev/project/.env")

    assert result.accepted is False
    assert result.reason == "excluded_path"
    assert fake.remember_calls == []


@pytest.mark.asyncio
async def test_ingest_scan_can_be_disabled() -> None:
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", content_scan_enabled=False),
        cognee=fake,
    )

    result = await kb.ingest("AWS key AKIAIOSFODNN7EXAMPLE leaked here")

    assert result.accepted
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_rejects_duplicate_in_process() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    first = await kb.ingest("same note")
    second = await kb.ingest("same note")

    assert first.accepted
    assert not second.accepted
    assert second.reason == "duplicate_in_process"
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_allows_same_content_to_different_datasets() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    node = await kb.ingest("shared trace body", dataset="seat:alice")
    shared = await kb.ingest("shared trace body", dataset="session-traces")

    assert node.accepted
    assert shared.accepted
    assert len(fake.remember_calls) == 2
    assert fake.remember_calls[0]["dataset_name"] == "seat:alice"
    assert fake.remember_calls[1]["dataset_name"] == "session-traces"


@pytest.mark.asyncio
async def test_search_uses_github_sync_session_for_github_dataset() -> None:
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(
            github_sync_dataset="masumi-network",
            github_sync_session="masumi-github-daily",
        ),
        cognee=fake,
    )

    result = await kb.search("weekly updates", dataset="masumi-network")

    assert result[0]["session_id"] == "masumi-github-daily"


@pytest.mark.asyncio
async def test_search_falls_back_to_persisted_github_digest(tmp_path: Any) -> None:
    state_path = tmp_path / "github_state.json"
    state_path.write_text(
        json.dumps(
            {
                "org": "masumi-network",
                "last_checked_at": "2026-06-01T14:27:10Z",
                "last_digest_at": "2026-06-01T14:27:10Z",
                "last_digest": (
                    "# masumi-network GitHub daily update\n\n"
                    "New commits observed: 1\n\n"
                    "## Recent commits\n"
                    "- 2026-06-01T13:15:28Z: mrgrauel committed 434cec44e6af "
                    "to masumi-network/sokosumi: organization seat assignment."
                ),
            }
        ),
        encoding="utf-8",
    )
    kb = Citadel(
        CitadelConfig(
            github_sync_dataset="masumi-network",
            github_sync_session="masumi-github-daily",
            github_sync_state_path=str(state_path),
        ),
        cognee=EmptyCognee(),
    )

    result = await kb.search("what were the new updates all week in the org", dataset="masumi-network")

    assert result[0]["source"] == "github_sync_state"
    assert result[0]["metadata"]["org"] == "masumi-network"
    assert any("organization seat assignment" in item["content"] for item in result)


@pytest.mark.asyncio
async def test_cognify_dataset_reports_graph_growth() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset()

    assert result["ok"]
    assert result["dataset"] == "masumi-network"
    assert result["verify"] is False
    assert fake.cognify_calls == [{"datasets": ["masumi-network"], "force": False}]
    assert result["graph_before"] == {"nodes": 0, "edges": 0}


@pytest.mark.asyncio
async def test_cognify_dataset_verify_ingests_marker_and_confirms_hit() -> None:
    class RecallingCognee(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"content": query}]

    fake = RecallingCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verify"] is True
    marker = fake.remember_calls[0]["data"]
    assert marker.startswith("COGNIFY_TEST_MARKER_")
    assert result["verification"]["search_hit"] is True
    assert result["verification"]["graph_grew"] is True
    assert result["verification"]["ok"] is True
    assert result["ok"] is True
    # verify is a superset: recovery cognify + an explicit cognify of the marker
    # (remember does not cognify inline on the modern Cognee path).
    assert fake.cognify_calls == [
        {"datasets": ["masumi-network"], "force": False},
        {"datasets": ["masumi-network"], "force": False},
    ]


@pytest.mark.asyncio
async def test_cognify_dataset_verify_failure_propagates_top_level_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed verify canary must set top-level ok=False (CLI exit code)."""
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class StuckCognee(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return []  # the marker is never retrievable

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"cognified": True}  # ...and the graph never grows

    fake = StuckCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["ok"] is False
    assert result["verification"]["search_attempts"] == service.CANARY_SEARCH_ATTEMPTS
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cognify_canary_is_not_ok_on_graph_growth_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#114: a growing graph is not evidence the marker became retrievable.

    Production ran "grew=True canary_ok=True" every hour while github_sync and
    linear_sync were dead on the Kuzu lock. Any concurrent ingest grows the
    graph, so growth must stay diagnostic detail and never a pass condition.
    """
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class GrowsButUnsearchable(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return []  # never retrievable...

    fake = GrowsButUnsearchable()  # ...but FakeCognee's graph still grows
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["graph_grew"] is True
    assert result["verification"]["search_hit"] is False
    assert result["verification"]["ok"] is False
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cognify_canary_accepts_a_late_search_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cognify settles in the background, so a bounded retry must be allowed."""
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class SlowCognee(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.recall_calls = 0

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_calls += 1
            # Miss once, then settle — a healthy node that is merely slow.
            return [] if self.recall_calls < 2 else [{"content": query}]

    fake = SlowCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["search_hit"] is True
    assert result["verification"]["search_attempts"] == 2
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_lifecycle_cognify_canary_waits_for_projection_then_confirms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Lifecycle v1: the canary waits on the marker's projection operation.

    The marker ingest queues an async projection job and returns before cognee
    sees the content, and search excludes revisions without a searchable
    receipt before consulting any backend, so the pre-fix 3x2s re-search loop
    was a deterministic miss against a minutes-scale drain (live 2026-08-13: a
    marker ingested 13:38Z became searchable at 13:43:17Z while its canary had
    long reported red). The canary now waits on the operation record, runs ONE
    confirming search, and tombstones the marker's source revision so markers
    stop accreting as searchable documents, one per pass.
    """

    class LifecycleCanaryCognee(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.recall_calls = 0

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_calls += 1
            return [
                {"content": query, "document_id": document_id}
                for document_id in kwargs.get("document_ids") or []
            ]

    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    monkeypatch.setenv("DB_PROVIDER", "sqlite")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "ladybug")
    fake = LifecycleCanaryCognee()
    kb = Citadel(
        CitadelConfig(
            default_dataset="masumi-network",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )

    result = await kb.cognify_dataset(verify=True)
    await kb.wait_for_lifecycle_idle()

    verification = result["verification"]
    assert verification["search_hit"] is True
    assert verification["ok"] is True
    assert result["ok"] is True
    # One confirming search after the operation went searchable — not a blind
    # retry loop racing the drain.
    assert verification["search_attempts"] == 1
    assert fake.recall_calls == 1
    assert "reason" not in verification
    operation = kb.lifecycle_operation(verification["projection_job_id"])
    source_key = operation["source_revision"]["source_key"]
    # Durable cleanup: the marker's head is tombstoned, so the search filters
    # exclude it instead of accreting one searchable marker per pass.
    assert (
        kb.lifecycle_source_keys(dataset="masumi-network", source_key=source_key)
        == ()
    )


@pytest.mark.asyncio
async def test_lifecycle_cognify_canary_times_out_red_and_tombstones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """An undrained projection turns the canary red with a machine-readable
    reason — and the marker is tombstoned anyway, so a stuck drain cannot
    accrete markers either."""

    class CountingRecallCognee(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.recall_calls = 0

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_calls += 1
            return []

    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    monkeypatch.setenv("DB_PROVIDER", "sqlite")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "ladybug")
    monkeypatch.setenv("CITADEL_CANARY_TIMEOUT_SECONDS", "0.2")
    fake = CountingRecallCognee()
    kb = Citadel(
        CitadelConfig(
            default_dataset="masumi-network",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )
    # Park the drain: jobs stay pending, exactly like a held writer lock.
    monkeypatch.setattr(kb, "_start_lifecycle_projection", lambda: False)

    result = await kb.cognify_dataset(verify=True)

    verification = result["verification"]
    assert verification["search_hit"] is False
    assert verification["ok"] is False
    assert result["ok"] is False
    assert verification["reason"] == "projection_timeout"
    # No blind re-search loop ran against the excluded revision.
    assert verification["search_attempts"] == 0
    assert fake.recall_calls == 0
    operation = kb.lifecycle_operation(verification["projection_job_id"])
    source_key = operation["source_revision"]["source_key"]
    assert (
        kb.lifecycle_source_keys(dataset="masumi-network", source_key=source_key)
        == ()
    )


@pytest.mark.asyncio
async def test_lifecycle_cognify_canary_flags_miss_after_searchable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A searchable operation whose confirming search still misses is red.

    This is the outcome the confirming search exists to catch: every receipt
    says searchable, yet retrieval does not return the marker — a real
    end-to-end recall failure that receipt bookkeeping alone would report
    green. Its distinct reason string separates it from a drain that never
    finished."""

    class SearchableButUnfindableCognee(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.recall_calls = 0

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_calls += 1
            return []

    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-1")
    monkeypatch.setenv("DB_PROVIDER", "sqlite")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "ladybug")
    fake = SearchableButUnfindableCognee()
    kb = Citadel(
        CitadelConfig(
            default_dataset="masumi-network",
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )

    result = await kb.cognify_dataset(verify=True)
    await kb.wait_for_lifecycle_idle()

    verification = result["verification"]
    assert verification["search_hit"] is False
    assert verification["ok"] is False
    assert result["ok"] is False
    assert verification["reason"] == "search_miss_after_searchable"
    # The wait succeeded, so exactly one confirming search ran and missed.
    assert verification["search_attempts"] == 1
    assert fake.recall_calls == 1
    operation = kb.lifecycle_operation(verification["projection_job_id"])
    source_key = operation["source_revision"]["source_key"]
    assert (
        kb.lifecycle_source_keys(dataset="masumi-network", source_key=source_key)
        == ()
    )


def test_canary_timeout_rejects_non_finite_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """float() parses "inf", which passes a > 0 check — and an infinite
    timeout makes the canary wait poll forever, a silent permanent scheduler
    hang. Non-finite and non-positive values fall back to the default."""
    for raw in ("inf", "+inf", "-inf", "nan", "0", "-5", "bogus", ""):
        monkeypatch.setenv("CITADEL_CANARY_TIMEOUT_SECONDS", raw)
        assert (
            service._canary_timeout_seconds()
            == service.DEFAULT_CANARY_TIMEOUT_SECONDS
        ), raw
    monkeypatch.setenv("CITADEL_CANARY_TIMEOUT_SECONDS", "45.5")
    assert service._canary_timeout_seconds() == 45.5


@pytest.mark.asyncio
async def test_search_clamps_top_k_to_safe_bounds() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    huge = await kb.search("anything", top_k=100_000)
    negative = await kb.search("anything", top_k=-1)

    assert huge[0]["top_k"] == MAX_SEARCH_TOP_K
    assert negative[0]["top_k"] == 1


@pytest.mark.asyncio
async def test_cognify_dataset_force_passes_incremental_loading_false() -> None:
    """force=True must propagate as incremental_loading=False so Cognee reprocesses
    a dataset it has marked "already processed" (the empty-graph recovery case)."""
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    await kb.cognify_dataset(force=True)

    assert fake.cognify_calls == [{"datasets": ["masumi-network"], "force": True}]


@pytest.mark.asyncio
async def test_reconcile_zero_chunks_is_dry_run_by_default() -> None:
    class RepairGateway(FakeCognee):
        async def corpus_zero_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "zero_chunk_count": 2,
                "unassigned_zero_chunk_count": 0,
                "repair_datasets": ["notes"],
            }

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_zero_chunk_documents()

    assert result["ok"] is True
    assert result["reason"] == "repair_required"
    assert result["repair_required"] is True
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_zero_chunks_applies_and_rechecks() -> None:
    class RepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []
            self.reports = [
                {
                    "ok": True,
                    "zero_chunk_count": 1,
                    "zero_chunk_document_ids": ["doc-zero"],
                    "repair_document_ids": ["doc-zero"],
                    "repair_document_datasets": {"doc-zero": ["notes"]},
                    "unassigned_zero_chunk_count": 0,
                    "repair_datasets": ["notes"],
                },
                {
                    "ok": True,
                    "zero_chunk_count": 0,
                    "unassigned_zero_chunk_count": 0,
                    "repair_datasets": [],
                },
            ]

        @asynccontextmanager
        async def maintenance(self):
            self.events.append("lock_enter")
            try:
                yield
            finally:
                self.events.append("lock_exit")

        async def corpus_zero_chunk_documents(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            return self.reports.pop(0)

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.events.append("cognify")
            return await super().cognify(**kwargs)

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_zero_chunk_documents(apply=True, force=True)

    assert result["ok"] is True
    assert result["reason"] == "repaired"
    assert result["after"]["zero_chunk_count"] == 0
    assert fake.cognify_calls == [{"datasets": ["notes"], "force": True}]
    assert fake.events == ["lock_enter", "census", "cognify", "census", "lock_exit"]


@pytest.mark.asyncio
async def test_reconcile_zero_chunks_refuses_unassigned_apply() -> None:
    class RepairGateway(FakeCognee):
        async def corpus_zero_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "zero_chunk_count": 1,
                "unassigned_zero_chunk_count": 1,
                "repair_datasets": [],
            }

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_zero_chunk_documents(apply=True)

    assert result["ok"] is False
    assert result["reason"] == "zero_chunk_documents_without_dataset"
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_oversized_chunks_is_dry_run_by_default() -> None:
    class RepairGateway(FakeCognee):
        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "missing_document_id_violation_count": 0,
                "orphan_oversized_document_count": 0,
                "oversized_document_count": 1,
                "oversized_documents_truncated": False,
                "unassigned_oversized_document_count": 0,
                "repair_document_ids": ["doc-a"],
                "repair_datasets": ["notes"],
            }

        async def delete_document_chunks(self, _: list[str]) -> dict[str, Any]:
            raise AssertionError("dry run must not delete old chunks")

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_oversized_chunks()

    assert result["ok"] is True
    assert result["reason"] == "oversized_chunks_repair_required"
    assert result["repair_required"] is True
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_oversized_chunks_requires_force_on_apply() -> None:
    class RepairGateway(FakeCognee):
        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "missing_document_id_violation_count": 0,
                "orphan_oversized_document_count": 0,
                "oversized_document_count": 1,
                "oversized_documents_truncated": False,
                "unassigned_oversized_document_count": 0,
                "repair_document_ids": ["doc-a"],
                "repair_datasets": ["notes"],
            }

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_oversized_chunks(apply=True)

    assert result["ok"] is False
    assert result["reason"] == "oversized_chunks_repair_requires_force"
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_oversized_chunks_deletes_rebuilds_and_rechecks(tmp_path) -> None:
    class RepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "missing_document_id_violation_count": 0,
                    "orphan_oversized_document_count": 0,
                    "oversized_document_count": 1,
                    "oversized_chunk_count": 2,
                    "oversized_documents_truncated": True,
                    "unassigned_oversized_document_count": 0,
                    "repair_document_ids": ["doc-a"],
                    "repair_datasets": ["notes"],
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "missing_document_id_violation_count": 0,
                    "orphan_oversized_document_count": 0,
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_documents_truncated": False,
                    "unassigned_oversized_document_count": 0,
                    "repair_document_ids": [],
                    "repair_datasets": [],
                },
            ]
            self.deleted: list[str] = []

        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.deleted.extend(document_ids)
            return {"document_ids": document_ids, "vector_chunk_count": 2, "graph_node_count": 2}

        async def corpus_chunk_counts(self, _: list[str]) -> dict[str, int]:
            return {"doc-a": 4}

        async def corpus_graph_presence(self, _: list[str]) -> set[str]:
            return {"doc-a"}

    fake = RepairGateway()
    journal_path = tmp_path / "repair.jsonl"
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_oversized_chunks(apply=True, force=True)

    assert result["ok"] is True
    assert result["reason"] == "repaired"
    assert fake.deleted == ["doc-a"]
    assert fake.cognify_calls == [{"datasets": ["notes"], "force": True}]
    assert result["post_repair_indexed"] is True
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert [event["phase"] for event in journal] == [
        "started",
        "preflight",
        "preflight",
        "delete",
        "delete",
        "cognify",
        "cognify",
        "post_census",
        "post_census",
        "post_index_check",
        "post_index_check",
    ]
    assert journal[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_reconcile_oversized_chunks_preflights_before_delete() -> None:
    class RepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.deleted = False

        async def get_document(self, document_id: str) -> dict[str, Any]:
            return {"id": document_id, "body": "x" * 100_000}

        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "missing_document_id_violation_count": 0,
                "orphan_oversized_document_count": 0,
                "oversized_document_count": 1,
                "oversized_chunk_count": 1,
                "oversized_documents_truncated": False,
                "unassigned_oversized_document_count": 0,
                "repair_document_ids": ["doc-a"],
                "repair_datasets": ["notes"],
            }

        async def delete_document_chunks(self, _: list[str]) -> dict[str, Any]:
            self.deleted = True
            raise AssertionError("invalid source must be rejected before delete")

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_oversized_chunks(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_preflight_failed"
    assert result["preflight"]["reason"] == "unchunkable_content"
    assert fake.deleted is False
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_oversized_chunks_fails_closed_without_restore_capability() -> None:
    class NoRollbackGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.restore_document_chunks = None

        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "missing_document_id_violation_count": 0,
                "orphan_oversized_document_count": 0,
                "oversized_document_count": 1,
                "oversized_chunk_count": 1,
                "oversized_documents_truncated": False,
                "unassigned_oversized_document_count": 0,
                "repair_document_ids": ["doc-a"],
                "repair_datasets": ["notes"],
            }

        async def delete_document_chunks(self, _: list[str]) -> dict[str, Any]:
            raise AssertionError("restore capability must be checked before delete")

    fake = NoRollbackGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_oversized_chunks(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_rollback_unavailable"
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_corpus_repairs_mixed_candidates_once_and_holds_lock(tmp_path) -> None:
    class RepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 1,
                    "zero_chunk_document_ids": ["doc-zero"],
                    "oversized_document_count": 1,
                    "oversized_chunk_count": 2,
                    "oversized_document_ids": ["doc-over"],
                    "zero_repair_document_ids": ["doc-zero"],
                    "oversized_repair_document_ids": ["doc-over"],
                    "repair_document_ids": ["doc-over", "doc-zero"],
                    "repair_document_datasets": {
                        "doc-over": ["notes"],
                        "doc-zero": ["notes"],
                    },
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 2,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": [],
                    "repair_document_datasets": {},
                    "repair_datasets": [],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]
            self.deleted: list[str] = []

        @asynccontextmanager
        async def maintenance(self):
            self.events.append("lock_enter")
            try:
                yield
            finally:
                self.events.append("lock_exit")

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            return self.reports.pop(0)

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.events.append("delete")
            self.deleted.extend(document_ids)
            return {"document_ids": document_ids}

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.events.append("cognify")
            self.cognify_calls.append(kwargs)
            return {"ok": True}

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            self.events.append("counts")
            return {document_id: 3 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            self.events.append("graph")
            return set(document_ids)

        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {
                document_id: {"content_hash": f"hash-{document_id}"}
                for document_id in document_ids
            }

    fake = RepairGateway()
    journal_path = tmp_path / "repair.jsonl"
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is True
    assert result["reason"] == "repaired"
    assert fake.deleted == ["doc-over"]
    assert fake.cognify_calls == [{"datasets": ["notes"], "force": True}]
    assert result["post_repair_indexed"] is True
    assert result["post_repair_stored_budget_ok"] is True
    assert fake.events == [
        "lock_enter",
        "census",
        "delete",
        "cognify",
        "census",
        "counts",
        "graph",
        "lock_exit",
    ]
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert journal[0]["phase"] == "started"
    assert journal[0]["repair_document_ids"] == ["doc-over", "doc-zero"]
    assert journal[0]["source_manifest"] == {
        "doc-over": {"content_hash": "hash-doc-over"},
        "doc-zero": {"content_hash": "hash-doc-zero"},
    }
    assert journal[-1]["status"] == "completed"
    assert journal[-1]["post_repair_indexed"] is True
    assert journal[-1]["post_repair_stored_budget_ok"] is True
    assert all("text" not in event for event in journal)


@pytest.mark.asyncio
async def test_reconcile_corpus_refuses_apply_when_source_manifest_fails(tmp_path) -> None:
    class UnreadableSourceGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "zero_chunk_count": 1,
                "zero_chunk_document_ids": ["doc-zero"],
                "oversized_document_count": 1,
                "oversized_chunk_count": 1,
                "oversized_document_ids": ["doc-over"],
                "zero_repair_document_ids": ["doc-zero"],
                "oversized_repair_document_ids": ["doc-over"],
                "repair_document_ids": ["doc-over", "doc-zero"],
                "repair_document_datasets": {
                    "doc-over": ["notes"],
                    "doc-zero": ["notes"],
                },
                "repair_datasets": ["notes"],
                "unassigned_zero_chunk_document_count": 0,
                "unassigned_oversized_document_count": 0,
                "orphan_oversized_document_count": 0,
                "missing_document_id_violation_count": 0,
            }

        async def source_manifest_for_documents(
            self, _: list[str]
        ) -> dict[str, dict[str, Any]]:
            raise RuntimeError("source unavailable")

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.deleted.extend(document_ids)
            return {"document_ids": document_ids}

    fake = UnreadableSourceGateway()
    journal_path = tmp_path / "repair.jsonl"
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_source_manifest_unavailable"
    assert fake.deleted == []
    assert fake.cognify_calls == []
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_reconcile_corpus_keeps_repair_datasets_and_ids_separate(tmp_path) -> None:
    class MultiDatasetGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []
            self.census_calls: list[dict[str, Any]] = []
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 2,
                    "zero_chunk_document_ids": ["doc-notes-zero", "doc-other-zero"],
                    "oversized_document_count": 2,
                    "oversized_chunk_count": 2,
                    "oversized_document_ids": ["doc-notes-over", "doc-other-over"],
                    "zero_repair_document_ids": ["doc-notes-zero", "doc-other-zero"],
                    "oversized_repair_document_ids": ["doc-notes-over", "doc-other-over"],
                    "repair_document_ids": [
                        "doc-notes-over",
                        "doc-notes-zero",
                        "doc-other-over",
                        "doc-other-zero",
                    ],
                    "repair_document_datasets": {
                        "doc-notes-over": ["notes"],
                        "doc-notes-zero": ["notes"],
                        "doc-other-over": ["other"],
                        "doc-other-zero": ["other"],
                    },
                    "repair_datasets": ["notes", "other"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 2,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": [],
                    "repair_document_datasets": {},
                    "repair_datasets": [],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def corpus_reconciliation_census(self, **kwargs: Any) -> dict[str, Any]:
            self.census_calls.append(kwargs)
            return self.reports.pop(0)

        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {document_id: {"content_hash": document_id} for document_id in document_ids}

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.deleted.extend(document_ids)
            return {"document_ids": document_ids}

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"ok": True}

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 2 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

    fake = MultiDatasetGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(tmp_path / "repair.jsonl")),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is True
    assert fake.deleted == ["doc-notes-over", "doc-other-over"]
    assert fake.cognify_calls == [{"datasets": ["notes", "other"], "force": True}]
    assert fake.census_calls == [{"dataset": None}, {"dataset": None}]
    assert result["repair_document_ids"] == [
        "doc-notes-over",
        "doc-notes-zero",
        "doc-other-over",
        "doc-other-zero",
    ]


@pytest.mark.asyncio
async def test_reconcile_corpus_recovers_interrupted_source_backed_repair(tmp_path) -> None:
    manifest = {"doc-a": {"content_hash": "hash-a", "data_size": 12}}
    clean = {
        "ok": True,
        "census_complete": True,
        "cap_exceeded": False,
        "zero_chunk_count": 0,
        "oversized_document_count": 0,
        "oversized_chunk_count": 0,
        "unassigned_zero_chunk_document_count": 0,
        "unassigned_oversized_document_count": 0,
        "orphan_oversized_document_count": 0,
        "missing_document_id_violation_count": 0,
        "zero_chunk_document_ids": [],
        "oversized_document_ids": [],
        "zero_repair_document_ids": [],
        "oversized_repair_document_ids": [],
        "repair_document_ids": [],
        "repair_document_datasets": {},
        "repair_datasets": [],
    }

    class RecoveryGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [dict(clean), dict(clean)]

        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {document_id: manifest[document_id] for document_id in document_ids}

        async def dataset_membership_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, list[str]]:
            # Scoped repair journals its target dataset; a shared document may
            # also remain attached to another dataset without being drift.
            return {document_id: ["notes", "other"] for document_id in document_ids}

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 2 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

    journal_path = tmp_path / "repair.jsonl"
    journal = RepairJournal(journal_path)
    journal.append(
        operation_id="interrupted-op",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc-a"],
        repair_datasets=["notes"],
        repair_document_datasets={"doc-a": ["notes"]},
        source_manifest=manifest,
    )
    fake = RecoveryGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True, recover=True)

    assert result["ok"] is True
    assert result["reason"] == "no_repair_required"
    assert result["recovery"]["reason"] == "recovered_interrupted_repairs"
    assert result["recovery"]["recovered_operations"] == ["interrupted-op"]
    assert fake.cognify_calls == [{"datasets": ["notes"], "force": True}]
    assert RepairJournal(journal_path).pending_operations() == []
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert [record["phase"] for record in records[-2:]] == ["recovery", "recovery"]
    assert records[-1]["status"] == "completed"
    assert records[-1]["reason"] == "source_backed_rebuild"


@pytest.mark.asyncio
async def test_reconcile_corpus_continues_normal_repair_after_recovery_postcheck(tmp_path) -> None:
    manifest = {"doc-a": {"content_hash": "hash-a", "data_size": 12}}

    def census(*, oversized: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "census_complete": True,
            "cap_exceeded": False,
            "zero_chunk_count": 0,
            "oversized_document_count": 1 if oversized else 0,
            "oversized_chunk_count": 1 if oversized else 0,
            "unassigned_zero_chunk_document_count": 0,
            "unassigned_oversized_document_count": 0,
            "orphan_oversized_document_count": 0,
            "missing_document_id_violation_count": 0,
            "zero_chunk_document_ids": [],
            "oversized_document_ids": ["doc-a"] if oversized else [],
            "zero_repair_document_ids": [],
            "oversized_repair_document_ids": ["doc-a"] if oversized else [],
            "repair_document_ids": ["doc-a"] if oversized else [],
            "repair_document_datasets": {"doc-a": ["notes"]} if oversized else {},
            "repair_datasets": ["notes"] if oversized else [],
            "stored_chunk_budget": {
                "ok": True,
                "violation_count": 0,
                "missing_document_id_violation_count": 0,
            },
            "budget": 1000,
        }

    class RecoveryContinuationGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                census(oversized=True),
                census(oversized=True),
                census(oversized=False),
                census(oversized=False),
            ]

        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {document_id: manifest[document_id] for document_id in document_ids}

        async def dataset_membership_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, list[str]]:
            return {document_id: ["notes"] for document_id in document_ids}

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            return {"document_ids": document_ids}

        async def stored_chunk_budget_check(
            self, document_ids: list[str], *, budget: int | None = None
        ) -> dict[str, Any]:
            return {
                "ok": True,
                "document_ids": document_ids,
                "budget": budget,
                "violation_count": 0,
                "missing_document_id_violation_count": 0,
            }

    journal_path = tmp_path / "repair.jsonl"
    RepairJournal(journal_path).append(
        operation_id="interrupted-op",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc-a"],
        repair_datasets=["notes"],
        repair_document_datasets={"doc-a": ["notes"]},
        source_manifest=manifest,
    )
    fake = RecoveryContinuationGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True, recover=True)

    assert result["ok"] is True
    assert result["reason"] == "no_repair_required"
    assert result["recovery"]["recovered_operations"] == ["interrupted-op"]
    assert fake.cognify_calls == [
        {"datasets": ["notes"], "force": True},
        {"datasets": ["notes"], "force": True},
    ]
    assert RepairJournal(journal_path).pending_operations() == []
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    recovery_terminal = [
        record
        for record in records
        if record["operation_id"] == "interrupted-op" and record["status"] == "completed"
    ]
    assert recovery_terminal[-1]["reason"] == "source_backed_rebuild_then_repair"
    assert recovery_terminal[-1]["post_repair_indexed"] is True


@pytest.mark.asyncio
async def test_reconcile_corpus_refuses_recovery_when_source_changed(tmp_path) -> None:
    journal_path = tmp_path / "repair.jsonl"
    expected = {"doc-a": {"content_hash": "before"}}
    RepairJournal(journal_path).append(
        operation_id="changed-op",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc-a"],
        repair_datasets=["notes"],
        repair_document_datasets={"doc-a": ["notes"]},
        source_manifest=expected,
    )

    class ChangedSourceGateway(FakeCognee):
        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {document_id: {"content_hash": "after"} for document_id in document_ids}

        async def dataset_membership_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, list[str]]:
            return {document_id: ["notes"] for document_id in document_ids}

    fake = ChangedSourceGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True, recover=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_source_changed"
    assert fake.cognify_calls == []
    assert result["pending_operations"][0]["operation_id"] == "changed-op"


@pytest.mark.asyncio
async def test_reconcile_corpus_refuses_recovery_when_dataset_membership_changed(
    tmp_path,
) -> None:
    manifest = {"doc-a": {"content_hash": "same"}}
    journal_path = tmp_path / "repair.jsonl"
    RepairJournal(journal_path).append(
        operation_id="membership-drift-op",
        dataset="notes",
        phase="delete",
        status="started",
        repair_document_ids=["doc-a"],
        repair_datasets=["notes"],
        repair_document_datasets={"doc-a": ["notes"]},
        source_manifest=manifest,
    )

    class ChangedMembershipGateway(FakeCognee):
        async def source_manifest_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {document_id: manifest[document_id] for document_id in document_ids}

        async def dataset_membership_for_documents(
            self, document_ids: list[str]
        ) -> dict[str, list[str]]:
            return {document_id: ["other"] for document_id in document_ids}

    fake = ChangedMembershipGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True, recover=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_dataset_membership_changed"
    assert fake.cognify_calls == []
    assert result["pending_operations"][0]["operation_id"] == "membership-drift-op"
    assert result["pending_operations"][0]["reason"] == (
        "repair_dataset_membership_changed"
    )


@pytest.mark.asyncio
async def test_reconcile_corpus_refuses_recovery_without_source_manifest(tmp_path) -> None:
    journal_path = tmp_path / "repair.jsonl"
    RepairJournal(journal_path).append(
        operation_id="legacy-op",
        dataset="notes",
        phase="cognify",
        status="started",
        repair_document_ids=["doc-a"],
        repair_datasets=["notes"],
    )
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True, recover=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_recovery_context_unavailable"
    assert fake.cognify_calls == []


@pytest.mark.asyncio
async def test_reconcile_corpus_recovery_requires_apply() -> None:
    result = await Citadel(CitadelConfig(default_dataset="notes"), cognee=FakeCognee()).reconcile_corpus(
        recover=True
    )

    assert result["ok"] is False
    assert result["reason"] == "repair_recovery_requires_apply"


@pytest.mark.asyncio
async def test_reconcile_corpus_recovery_requires_force() -> None:
    result = await Citadel(CitadelConfig(default_dataset="notes"), cognee=FakeCognee()).reconcile_corpus(
        apply=True, recover=True
    )

    assert result["ok"] is False
    assert result["reason"] == "repair_recovery_requires_force"


@pytest.mark.asyncio
async def test_reconcile_corpus_allows_zero_chunk_repair_without_projection_body() -> None:
    class ZeroChunkGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 1,
                    "zero_chunk_document_ids": ["doc-zero"],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": ["doc-zero"],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": ["doc-zero"],
                    "repair_document_datasets": {"doc-zero": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": [],
                    "repair_document_datasets": {},
                    "repair_datasets": [],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]
            self.restore_document_chunks = None

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def get_document(self, _: str) -> None:
            return None

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"ok": True}

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 1 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

        async def delete_document_chunks(self, _: list[str]) -> dict[str, Any]:
            raise AssertionError("zero-chunk repair must not delete projections")

    fake = ZeroChunkGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_corpus(apply=True)

    assert result["ok"] is True
    assert result["reason"] == "repaired"
    assert fake.cognify_calls == [{"datasets": ["notes"], "force": False}]


@pytest.mark.asyncio
async def test_reconcile_corpus_refuses_incomplete_census_before_mutation() -> None:
    class RepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        @asynccontextmanager
        async def maintenance(self):
            self.events.append("lock_enter")
            try:
                yield
            finally:
                self.events.append("lock_exit")

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            return {
                "ok": False,
                "census_complete": False,
                "cap_exceeded": False,
                "reason": "incomplete_corpus_walk",
            }

    fake = RepairGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "incomplete_corpus_walk"
    assert fake.cognify_calls == []
    assert fake.events == ["lock_enter", "census", "lock_exit"]


@pytest.mark.asyncio
async def test_reconcile_corpus_reports_failure_phase_after_deletion(tmp_path) -> None:
    class FailingGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []
            self.projections = {"doc-over": "old projection"}

        @asynccontextmanager
        async def maintenance(self):
            self.events.append("lock_enter")
            try:
                yield
            finally:
                self.events.append("lock_exit")

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            if self.events.count("census") == 1:
                return {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 1,
                    "oversized_chunk_count": 1,
                    "oversized_document_ids": ["doc-over"],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": ["doc-over"],
                    "repair_document_ids": ["doc-over"],
                    "repair_document_datasets": {"doc-over": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 1,
                        "missing_document_id_violation_count": 0,
                    },
                }
            raise AssertionError("post-census must not run after cognify failure")

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.events.append("delete")
            self.projections.clear()
            return {"document_ids": document_ids}

        async def restore_document_chunks(self, deleted: dict[str, Any]) -> bool:
            self.events.append("restore")
            self.projections["doc-over"] = "old projection"
            return True

        async def cognify(self, **_: Any) -> dict[str, Any]:
            self.events.append("cognify")
            raise RuntimeError("cognify failed")

    fake = FailingGateway()
    journal_path = tmp_path / "repair.jsonl"
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_failed"
    assert result["repair_phase"] == "cognify"
    assert result["error_type"] == "RuntimeError"
    assert result["deleted"] == {"document_ids": ["doc-over"]}
    assert result["repair_required"] is True
    assert fake.events == [
        "lock_enter",
        "census",
        "delete",
        "cognify",
        "restore",
        "lock_exit",
    ]
    assert fake.projections == {"doc-over": "old projection"}
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert journal[-1]["status"] == "failed"
    assert journal[-1]["phase"] == "cognify"
    assert journal[-1]["error_type"] == "RuntimeError"
    assert journal[-1]["deleted_document_ids"] == ["doc-over"]
    assert journal[-1]["projections_preserved"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_method", ["combined", "oversized"])
async def test_reconcile_stops_after_partial_delete_failure(
    tmp_path, repair_method: str
) -> None:
    class PartialDeleteGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        @asynccontextmanager
        async def maintenance(self):
            self.events.append("lock_enter")
            try:
                yield
            finally:
                self.events.append("lock_exit")

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "zero_chunk_count": 0,
                "zero_chunk_document_ids": [],
                "oversized_document_count": 1,
                "oversized_chunk_count": 1,
                "oversized_document_ids": ["doc-over"],
                "zero_repair_document_ids": [],
                "oversized_repair_document_ids": ["doc-over"],
                "repair_document_ids": ["doc-over"],
                "repair_document_datasets": {"doc-over": ["notes"]},
                "repair_datasets": ["notes"],
                "unassigned_zero_chunk_document_count": 0,
                "unassigned_oversized_document_count": 0,
                "orphan_oversized_document_count": 0,
                "missing_document_id_violation_count": 0,
            }

        async def corpus_oversized_chunk_documents(self, **_: Any) -> dict[str, Any]:
            self.events.append("census")
            return {
                "ok": True,
                "census_complete": True,
                "cap_exceeded": False,
                "oversized_document_count": 1,
                "oversized_chunk_count": 1,
                "oversized_documents_truncated": False,
                "repair_document_ids": ["doc-over"],
                "repair_datasets": ["notes"],
                "unassigned_oversized_document_count": 0,
                "orphan_oversized_document_count": 0,
                "missing_document_id_violation_count": 0,
            }

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            self.events.append("delete")
            return {
                "ok": False,
                "document_ids": document_ids,
                "snapshot_token": "snap-1",
                "reason": "repair_delete_failed",
                "error_type": "RuntimeError",
                "projections_preserved": True,
            }

        async def restore_document_chunks(self, _: dict[str, Any]) -> bool:
            self.events.append("restore")
            return True

        async def cognify(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("cognify must not run after a failed delete")

    fake = PartialDeleteGateway()
    journal_path = tmp_path / "repair.jsonl"
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(journal_path)),
        cognee=fake,
    )

    if repair_method == "combined":
        result = await kb.reconcile_corpus(apply=True, force=True)
    else:
        result = await kb.reconcile_oversized_chunks(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "repair_delete_failed"
    assert result["repair_phase"] == "delete"
    assert result["deleted"]["snapshot_token"] == "snap-1"
    assert result["projections_preserved"] is True
    assert fake.events == ["lock_enter", "census", "delete", "lock_exit"]
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert journal[-1]["status"] == "failed"
    assert journal[-1]["phase"] == "delete"
    assert journal[-1]["reason"] == "repair_delete_failed"
    assert journal[-1]["projections_preserved"] is True


@pytest.mark.asyncio
async def test_reconcile_corpus_restores_when_post_check_invariants_remain(tmp_path) -> None:
    class IncompleteRepairGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 1,
                    "oversized_chunk_count": 1,
                    "oversized_document_ids": ["doc-over"],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": ["doc-over"],
                    "repair_document_ids": ["doc-over"],
                    "repair_document_datasets": {"doc-over": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 1,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 1,
                    "oversized_chunk_count": 1,
                    "oversized_document_ids": ["doc-over"],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": ["doc-over"],
                    "repair_document_ids": ["doc-over"],
                    "repair_document_datasets": {"doc-over": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 1,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]
            self.restored: list[dict[str, Any]] = []

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
            return {"document_ids": document_ids, "snapshot_token": "snap-1"}

        async def restore_document_chunks(self, deleted: dict[str, Any]) -> bool:
            self.restored.append(deleted)
            return True

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"ok": True}

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 1 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

    fake = IncompleteRepairGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(tmp_path / "repair.jsonl")),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "reconciliation_invariants_remain"
    assert result["projections_preserved"] is True
    assert fake.restored == [{"document_ids": ["doc-over"], "snapshot_token": "snap-1"}]


@pytest.mark.asyncio
async def test_reconcile_corpus_rejects_remaining_global_invariants(tmp_path) -> None:
    class IncompleteGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 1,
                    "zero_chunk_document_ids": ["doc-zero"],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": ["doc-zero"],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": ["doc-zero"],
                    "repair_document_datasets": {"doc-zero": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": [],
                    "repair_document_datasets": {},
                    "repair_datasets": [],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 1,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": True,
                        "violation_count": 0,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 1 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

    fake = IncompleteGateway()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", repair_journal_path=str(tmp_path / "repair.jsonl")),
        cognee=fake,
    )

    result = await kb.reconcile_corpus(apply=True, force=True)

    assert result["ok"] is False
    assert result["reason"] == "reconciliation_invariants_remain"
    assert result["post_repair_indexed"] is True


@pytest.mark.asyncio
async def test_reconcile_corpus_scopes_post_budget_to_requested_dataset() -> None:
    class ScopedGateway(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.reports = [
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 1,
                    "zero_chunk_document_ids": ["doc-notes"],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": ["doc-notes"],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": ["doc-notes"],
                    "repair_document_datasets": {"doc-notes": ["notes"]},
                    "repair_datasets": ["notes"],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 1,
                        "missing_document_id_violation_count": 0,
                    },
                },
                {
                    "ok": True,
                    "census_complete": True,
                    "cap_exceeded": False,
                    "zero_chunk_count": 0,
                    "zero_chunk_document_ids": [],
                    "oversized_document_count": 0,
                    "oversized_chunk_count": 0,
                    "oversized_document_ids": [],
                    "zero_repair_document_ids": [],
                    "oversized_repair_document_ids": [],
                    "repair_document_ids": [],
                    "repair_document_datasets": {},
                    "repair_datasets": [],
                    "unassigned_zero_chunk_document_count": 0,
                    "unassigned_oversized_document_count": 0,
                    "orphan_oversized_document_count": 0,
                    "missing_document_id_violation_count": 0,
                    "stored_chunk_budget": {
                        "ok": False,
                        "violation_count": 1,
                        "missing_document_id_violation_count": 0,
                    },
                },
            ]

        @asynccontextmanager
        async def maintenance(self):
            yield

        async def corpus_reconciliation_census(self, **_: Any) -> dict[str, Any]:
            return self.reports.pop(0)

        async def stored_chunk_budget_check(
            self, document_ids: list[str], **_: Any
        ) -> dict[str, Any]:
            assert document_ids == ["doc-notes"]
            return {
                "ok": True,
                "scope": "document_ids",
                "violation_count": 0,
                "missing_document_id_violation_count": 0,
            }

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"ok": True}

        async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
            return {document_id: 1 for document_id in document_ids}

        async def corpus_graph_presence(self, document_ids: list[str]) -> set[str]:
            return set(document_ids)

    fake = ScopedGateway()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.reconcile_corpus(dataset="notes", apply=True, force=True)

    assert result["ok"] is True
    assert result["reason"] == "repaired"
    assert result["post_repair_stored_budget_ok"] is True


@pytest.mark.asyncio
async def test_ingest_reports_queued_not_confirmed_until_cognify_finishes() -> None:
    class QueuedCognee(FakeCognee):
        async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
            self.remember_calls.append({"data": data, **kwargs})
            return {"added": {"ok": True}, "background_cognify": True}

    fake = QueuedCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest("a queued note")

    assert result.accepted is True
    assert result.reason == "queued_not_confirmed"


@pytest.mark.asyncio
async def test_ingest_reports_not_scheduled_when_retry_queue_rejects() -> None:
    class RejectedQueueCognee(FakeCognee):
        async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
            self.remember_calls.append({"data": data, **kwargs})
            return {"added": {"ok": True}, "background_cognify": False}

    fake = RejectedQueueCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest("a note whose retry was rejected")

    assert result.accepted is True
    assert result.reason == "not_scheduled"


@pytest.mark.asyncio
async def test_feedback_can_auto_improve() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(auto_improve=True), cognee=fake)

    result = await kb.feedback(FeedbackRequest(qa_id="qa-1", score=1, text="useful"))

    assert result.recorded
    assert not result.improved
    assert result.reason is not None
    assert "automated Cognee improvement is disabled" in result.reason
    assert fake.feedback_calls[0]["qa_id"] == "qa-1"
    assert fake.improve_calls == []


class _SessionMissCognee(FakeCognee):
    """add_feedback finds no matching qa_id in the session cache (post-#54 norm)."""

    async def add_feedback(self, **kwargs: Any) -> bool:
        self.feedback_calls.append(kwargs)
        return False


@pytest.mark.asyncio
async def test_feedback_falls_back_to_durable_write_when_session_cache_misses() -> None:
    # #40: a session-cache miss must not be a silent no-op — persist durably.
    fake = _SessionMissCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    result = await kb.feedback(FeedbackRequest(qa_id="qa-9", score=-1, text="wrong answer"))

    assert result.recorded is True
    assert result.ok is True
    assert result.reason is None
    note = fake.remember_calls[-1]
    assert "qa-9" in note["data"]
    assert "wrong answer" in note["data"]
    assert "feedback" in note["tags"]
    assert "qa:qa-9" in note["tags"]


@pytest.mark.asyncio
async def test_feedback_reports_reason_when_not_recorded() -> None:
    # #40: when even the durable write is rejected, report ok:False + a reason
    # (so the CLI exits nonzero) instead of recorded:false, exit 0.
    fake = _SessionMissCognee()
    kb = Citadel(CitadelConfig(min_chars=100_000), cognee=fake)  # forces filter rejection

    result = await kb.feedback(FeedbackRequest(qa_id="qa-9", score=0))

    assert result.recorded is False
    assert result.ok is False
    assert result.reason is not None and "not recorded" in result.reason


def test_legacy_garbage_kind_classifies_safely() -> None:
    # #15: the classifier must purge only well-identified garbage and NEVER real
    # content — this is the safety gate for a destructive admin operation.
    from kb.service import _legacy_garbage_kind

    hex32 = "a" * 32
    assert _legacy_garbage_kind("n1", {"text": f"COGNIFY_TEST_MARKER_{hex32}"}) == "marker"
    assert _legacy_garbage_kind("n2", {"text": "[DataItem]"}) == "dataitem"
    assert (
        _legacy_garbage_kind("n3", {"text": "Session ID: x\n\nQuestion: \n\nAnswer: [DataItem]"})
        == "dataitem"
    )
    assert _legacy_garbage_kind("n4", {"type": "user_sessions_from_cache"}) == "session_cache"

    # SAFETY — real content is never classified:
    assert _legacy_garbage_kind("r1", {"text": "We fixed the [DataItem] bug in #26."}) is None
    assert _legacy_garbage_kind("r2", {"text": "COGNIFY_TEST_MARKER is a concept."}) is None
    assert _legacy_garbage_kind("r3", {"text": "Question: how?\n\nAnswer: hold a lock"}) is None
    assert _legacy_garbage_kind("r4", {"text": "A genuine project decision."}) is None
    assert _legacy_garbage_kind("r5", {"type": "TextSummary"}) is None


_FOSSIL_DOC = "\n".join(
    [
        "# masumi-network/sokosumi/README.md",
        "",
        "Repository: masumi-network/sokosumi",
        "Source: https://github.com/masumi-network/sokosumi/blob/main/README.md",
        "Commit: 4f2a9c1db8e77a01c3d5f6a2b9e01234567890ab",
        "Blob: 8c31b0e4f2a9c1db8e77a01c3d5f6a2b9e012345",
        "Retrieved: 2026-07-28T14:03:11Z",
        "",
        "---",
        "",
        "# Sokosumi",
        "",
        "Sokosumi is a marketplace for AI agents on Masumi Network.",
        "",
        "---",
        "",
        "## Getting started",
        "",
        "Run `npm install` and configure your payment node.",
    ]
)

_POST_FIX_DOC = "\n".join(
    [
        "# masumi-network/sokosumi/README.md",
        "",
        "Repository: masumi-network/sokosumi",
        "Source: https://github.com/masumi-network/sokosumi/blob/main/README.md",
        "Commit: 4f2a9c1db8e77a01c3d5f6a2b9e01234567890ab",
        "Blob: 8c31b0e4f2a9c1db8e77a01c3d5f6a2b9e012345",
        "",
        "---",
        "",
        "# Sokosumi",
        "",
        "Sokosumi is a marketplace for AI agents on Masumi Network.",
    ]
)

# Post-fix document whose BODY (after the --- separator) legitimately contains
# the literal text "Retrieved: ..." — real documentation about retrieval.
_BODY_RETRIEVED_DOC = "\n".join(
    [
        "# masumi-network/citadel/docs/adr/0016-drop-retrieval-timestamp.md",
        "",
        "Repository: masumi-network/citadel",
        "Source: https://github.com/masumi-network/citadel/blob/main/docs/adr/0016.md",
        "Commit: d5d0fe3b1656807547e77b5ee82eaf4c5a337c1f",
        "Blob: c03a30b4f2a9c1db8e77a01c3d5f6a2b9e012345",
        "",
        "---",
        "",
        "Documents used to carry a header line of the form:",
        "",
        "Retrieved: 2026-07-28T14:03:11Z",
        "",
        "which minted a new copy on every sync pass.",
    ]
)


def test_repo_content_fossil_is_matched() -> None:
    # ADR-0016 / d5d0fe3: a pre-fix repo-content document — full rendered header
    # with Retrieved: inside it — is classified under its OWN kind so a human
    # can approve the class independently of marker/dataitem/session_cache.
    from kb.service import _legacy_garbage_kind

    assert _legacy_garbage_kind("f1", {"text": _FOSSIL_DOC}) == "repo_content_fossil"


def test_repo_content_fossil_never_matches_real_content() -> None:
    # SAFETY — this is deletion tooling; each of these is real knowledge that
    # must survive the classifier.
    from kb.service import _legacy_garbage_kind

    # (a) Post-fix repo-content document: same header, no Retrieved: line.
    assert _legacy_garbage_kind("k1", {"text": _POST_FIX_DOC}) is None
    # (b) "Retrieved: ..." appearing in the BODY, after the --- separator.
    assert _legacy_garbage_kind("k2", {"text": _BODY_RETRIEVED_DOC}) is None
    # (c) A Retrieved: line with no repo-content header around it.
    assert (
        _legacy_garbage_kind(
            "k3",
            {"text": "Meeting notes\n\nRetrieved: 2026-07-28 from the archive\n\n---\n\nBody."},
        )
        is None
    )
    # (d) An ordinary Linear issue document.
    assert (
        _legacy_garbage_kind(
            "k4",
            {
                "text": (
                    "# MAS-142: Payment webhook retries\n\n"
                    "Status: In Progress\nAssignee: sarthi\n\n---\n\n"
                    "Webhook delivery fails on cold starts; add retry with backoff."
                )
            },
        )
        is None
    )
    # (e) A personal seat note.
    assert (
        _legacy_garbage_kind(
            "k5",
            {"text": "Note to self: the Kuzu writer lock serializes cognify; never fork it."},
        )
        is None
    )
    # A chunk cut mid-header (no --- separator reached) is kept — fail closed.
    truncated = _FOSSIL_DOC.split("---")[0]
    assert _legacy_garbage_kind("k6", {"text": truncated}) is None


# Body lines the renderer builds by joining several f-string fragments
# (kb/repository_update.py:415-416 does the same). Assembled with explicit
# concatenation here rather than adjacent string literals inside the list
# below, so neither a reader nor a scanner has to decide whether a comma was
# forgotten. These lines sit AFTER the section heading, so they are realism
# only — the classifier never reads past the heading.
_DIGEST_REPO_LINE = (
    "- masumi-network/sokosumi (TypeScript): pushed 2026-07-19T04:12:00Z, "
    + "updated 2026-07-19T04:12:00Z, open issues 4, stars 12, forks 3. "
    + "Marketplace for AI agents. https://github.com/masumi-network/sokosumi"
)
_DIGEST_PR_LINE = (
    "- masumi-network/sokosumi#88 by sarthib7: Ship the composer. "
    + "Updated 2026-07-19T03:00:00Z. https://github.com/masumi-network/sokosumi/pull/88"
)

# Pre-fix GitHub org digest, current render vintage: full machine-rendered
# header with Checked at: and Window started at: before the first section.
_DIGEST_FOSSIL_DOC = "\n".join(
    [
        "# masumi-network GitHub daily update",
        "",
        "Checked at: 2026-07-19T05:44:02Z",
        "Window started at: 2026-07-18T05:44:02Z",
        "Source: https://github.com/orgs/masumi-network/repositories",
        "Repositories scanned: 42",
        "Changed repositories since last check: 3",
        "New public organization events: 7",
        "New commits observed: 12",
        "Open pull requests active in window: 2",
        "Merged pull requests in window: 1",
        "",
        "## Changed repositories",
        _DIGEST_REPO_LINE,
        "",
        "## Open pull requests worth attention",
        _DIGEST_PR_LINE,
    ]
)

# Oldest render vintage: no Window started at:, only the first three counter
# lines existed. These are the earliest copies in the vault.
_DIGEST_FOSSIL_OLDEST_DOC = "\n".join(
    [
        "# masumi-network GitHub daily update",
        "",
        "Checked at: 2026-06-12T05:00:00Z",
        "Source: https://github.com/orgs/masumi-network/repositories",
        "Repositories scanned: 30",
        "Changed repositories since last check: 1",
        "New public organization events: 2",
        "",
        "## Changed repositories",
        "- masumi-network/agent (Python): pushed 2026-06-12T04:00:00Z.",
    ]
)

# Post-fix digest: same renderer, no wall-clock lines. Cleanup must keep it.
_DIGEST_POST_FIX_DOC = "\n".join(
    [
        "# masumi-network GitHub daily update",
        "",
        "Source: https://github.com/orgs/masumi-network/repositories",
        "Repositories scanned: 42",
        "Changed repositories since last check: 3",
        "New public organization events: 7",
        "New commits observed: 12",
        "Open pull requests active in window: 2",
        "Merged pull requests in window: 1",
        "",
        "## Changed repositories",
        "- masumi-network/sokosumi (TypeScript): pushed 2026-08-02T04:12:00Z.",
    ]
)


def test_github_digest_fossil_never_matches_real_content() -> None:
    # SAFETY FIRST — this is deletion tooling; each of these is real knowledge
    # that must survive the classifier. Written before the classifier existed.
    from kb.service import _legacy_garbage_kind

    # (a) An ordinary Linear issue document.
    assert (
        _legacy_garbage_kind(
            "d1",
            {
                "text": (
                    "# MAS-201: Digest cron misses the window\n\n"
                    "Status: Todo\nAssignee: sarthi\n\n---\n\n"
                    "The daily update sometimes runs twice; investigate the scheduler."
                )
            },
        )
        is None
    )
    # (b) A personal seat note.
    assert (
        _legacy_garbage_kind(
            "d2",
            {"text": "Note to self: the GitHub daily update lands around 05:44 UTC."},
        )
        is None
    )
    # (c) Repo-content documents: the pre-ADR-0016 fossil keeps its OWN kind
    # (never the digest kind), and the post-fix repo document is kept.
    assert _legacy_garbage_kind("d3", {"text": _FOSSIL_DOC}) == "repo_content_fossil"
    assert _legacy_garbage_kind("d4", {"text": _POST_FIX_DOC}) is None
    # (d) A document whose BODY merely mentions "Checked at:" — prose about the
    # digest, not a rendered digest header.
    assert (
        _legacy_garbage_kind(
            "d5",
            {
                "text": (
                    "# Ops runbook: reading the sync dashboard\n\n"
                    "Every sync stamps a Checked at: timestamp into its state file.\n"
                    "Compare it against last_digest_at before blaming the cron.\n\n"
                    "## Changed repositories\n\n"
                    "This section of the dashboard lists fingerprint changes."
                )
            },
        )
        is None
    )
    # (e) A post-fix digest: rendered header WITHOUT the wall-clock lines.
    assert _legacy_garbage_kind("d6", {"text": _DIGEST_POST_FIX_DOC}) is None
    # (f) A chunk cut mid-header (## Changed repositories never reached).
    truncated_digest = _DIGEST_FOSSIL_DOC.split("## Changed repositories")[0]
    assert _legacy_garbage_kind("d7", {"text": truncated_digest}) is None
    # (g) A line the renderer never emitted inside the header block.
    tampered = _DIGEST_FOSSIL_DOC.replace(
        "Source: https://github.com/orgs/masumi-network/repositories",
        "Source: https://github.com/orgs/masumi-network/repositories\n"
        "Reviewed by: sarthi",
    )
    assert _legacy_garbage_kind("d8", {"text": tampered}) is None
    # (h) Header fields out of renderer order.
    reordered = _DIGEST_FOSSIL_DOC.replace(
        "Checked at: 2026-07-19T05:44:02Z\nWindow started at: 2026-07-18T05:44:02Z\n"
        "Source: https://github.com/orgs/masumi-network/repositories",
        "Source: https://github.com/orgs/masumi-network/repositories\n"
        "Checked at: 2026-07-19T05:44:02Z\nWindow started at: 2026-07-18T05:44:02Z",
    )
    assert _legacy_garbage_kind("d9", {"text": reordered}) is None
    # (i) A title that is not the digest title.
    retitled = _DIGEST_FOSSIL_DOC.replace(
        "# masumi-network GitHub daily update", "# masumi-network GitHub weekly report"
    )
    assert _legacy_garbage_kind("d10", {"text": retitled}) is None


def test_github_digest_fossil_is_matched() -> None:
    # A pre-fix GitHub org digest — full rendered header carrying the moving
    # Checked at: line — is classified under its OWN kind so a human can
    # approve this class independently of repo_content_fossil and the rest.
    from kb.service import _legacy_garbage_kind

    assert _legacy_garbage_kind("g1", {"text": _DIGEST_FOSSIL_DOC}) == "github_digest_fossil"
    assert (
        _legacy_garbage_kind("g2", {"text": _DIGEST_FOSSIL_OLDEST_DOC})
        == "github_digest_fossil"
    )
    # The middle vintage rendered Checked at: without a Window started at: line.
    no_window = _DIGEST_FOSSIL_DOC.replace("Window started at: 2026-07-18T05:44:02Z\n", "")
    assert _legacy_garbage_kind("g3", {"text": no_window}) == "github_digest_fossil"


class _GraphGateway(FakeCognee):
    def __init__(self, graph_nodes: list[Any]) -> None:
        super().__init__()
        self._graph_nodes = graph_nodes
        self.deleted: list[str] = []

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return self._graph_nodes, []

    async def delete_graph_nodes(self, node_ids: list[str]) -> int:
        self.deleted.extend(node_ids)
        return len(node_ids)


@pytest.mark.asyncio
async def test_cleanup_legacy_nodes_dry_run_then_delete() -> None:
    nodes = [
        ("g1", {"text": "COGNIFY_TEST_MARKER_" + "b" * 32}),
        ("g2", {"text": "[DataItem]"}),
        ("real1", {"text": "A genuine project decision."}),
    ]
    gw = _GraphGateway(nodes)
    kb = Citadel(CitadelConfig(), cognee=gw)

    dry = await kb.cleanup_legacy_nodes(dry_run=True)
    assert dry["dry_run"] is True
    assert dry["deleted"] == 0
    assert gw.deleted == []  # dry run deletes nothing
    assert {c["id"] for c in dry["candidates"]} == {"g1", "g2"}
    assert dry["counts_by_kind"] == {"marker": 1, "dataitem": 1}

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert res["deleted"] == 2
    assert set(gw.deleted) == {"g1", "g2"}  # real1 is never deleted


@pytest.mark.asyncio
async def test_cleanup_reports_fossils_under_their_own_kind() -> None:
    # A repo-content fossil must surface under its OWN counts_by_kind key so a
    # human reviewing the dry run can approve the class separately from the
    # legacy marker/dataitem/session_cache classes.
    nodes = [
        ("g1", {"text": "COGNIFY_TEST_MARKER_" + "c" * 32}),
        ("fossil1", {"text": _FOSSIL_DOC}),
        ("keep1", {"text": _POST_FIX_DOC}),
    ]
    gw = _GraphGateway(nodes)
    kb = Citadel(CitadelConfig(), cognee=gw)

    dry = await kb.cleanup_legacy_nodes(dry_run=True)
    assert dry["counts_by_kind"] == {"marker": 1, "repo_content_fossil": 1}
    assert {c["id"] for c in dry["candidates"]} == {"g1", "fossil1"}
    kinds = {c["id"]: c["kind"] for c in dry["candidates"]}
    assert kinds["fossil1"] == "repo_content_fossil"

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert res["deleted"] == 2
    assert "keep1" not in gw.deleted  # the post-fix document survives


@pytest.mark.asyncio
async def test_cleanup_counts_digest_fossils_under_their_own_key() -> None:
    # Pre-fix GitHub digests are a separate accumulation from repo-content
    # fossils (different render shape, different sync). They must surface under
    # their OWN counts_by_kind key so a human can approve the digest class
    # separately from repo_content_fossil.
    nodes = [
        ("digest1", {"text": _DIGEST_FOSSIL_DOC}),
        ("digest2", {"text": _DIGEST_FOSSIL_OLDEST_DOC}),
        ("repofossil1", {"text": _FOSSIL_DOC}),
        ("keepdigest", {"text": _DIGEST_POST_FIX_DOC}),
        ("keepnote", {"text": "A genuine project decision."}),
    ]
    gw = _GraphGateway(nodes)
    kb = Citadel(CitadelConfig(), cognee=gw)

    dry = await kb.cleanup_legacy_nodes(dry_run=True)
    assert dry["counts_by_kind"] == {"github_digest_fossil": 2, "repo_content_fossil": 1}
    kinds = {c["id"]: c["kind"] for c in dry["candidates"]}
    assert kinds == {
        "digest1": "github_digest_fossil",
        "digest2": "github_digest_fossil",
        "repofossil1": "repo_content_fossil",
    }

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert res["deleted"] == 3
    assert "keepdigest" not in gw.deleted  # the post-fix digest survives
    assert "keepnote" not in gw.deleted


@pytest.mark.asyncio
async def test_cleanup_sweeps_orphaned_vector_chunks() -> None:
    # #15: garbage cognified into the chunk vector store is found via the search
    # sweep even when its graph node is already gone (graph is empty here).
    class SweepGateway(_GraphGateway):
        def __init__(self) -> None:
            super().__init__([])  # empty graph

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {"id": "chunk-1", "text": "Session ID: x\n\nQuestion: \n\nAnswer: [DataItem]"},
                {"id": "real-1", "text": "A genuine note about payments."},
            ]

    gw = SweepGateway()
    kb = Citadel(CitadelConfig(), cognee=gw)

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert "chunk-1" in gw.deleted  # garbage chunk purged via the sweep
    assert "real-1" not in gw.deleted  # real content untouched
    assert res["counts_by_kind"].get("dataitem", 0) >= 1


@pytest.mark.asyncio
async def test_cognify_verify_deletes_its_marker_node() -> None:
    # #15 backprop: the verify canary must not leave a marker node behind.
    class MarkerGateway(_GraphGateway):
        def __init__(self) -> None:
            super().__init__([])

        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return [(f"node-{i}", {"text": text}) for i, text in enumerate(self.nodes)], []

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"text": query}]

    gw = MarkerGateway()
    kb = Citadel(CitadelConfig(), cognee=gw)

    await kb.cognify_dataset(verify=True)
    assert gw.deleted, "the cognify verify marker node should be deleted"


@pytest.mark.asyncio
async def test_improve_short_circuits_on_empty_graph() -> None:
    # #41: an empty graph yields a clean no-op, not a raw EntityNotFoundError.
    fake = FakeCognee()  # nodes/edges empty
    kb = Citadel(CitadelConfig(), cognee=fake)

    result = await kb.improve()

    assert result["ok"] is True
    assert result["skipped"] == "empty_graph"
    assert fake.improve_calls == []


@pytest.mark.asyncio
async def test_improve_runs_when_graph_has_data() -> None:
    fake = FakeCognee()
    fake.nodes = ["n1"]
    kb = Citadel(CitadelConfig(), cognee=fake)

    await kb.improve()

    assert fake.improve_calls, "cognee.improve should run on a non-empty graph"


@pytest.mark.asyncio
async def test_every_rejection_line_in_ingest_escapes_the_dataset_name(
    caplog: Any,
) -> None:
    """``Citadel.ingest`` logs the caller's dataset name on three refusal paths.

    CodeQL raised ``py/log-injection`` on one of them (the un-chunkable branch);
    the other two are the same variable, in the same function, reported on main as
    open alerts of the same rule. Escaping one and not the others closes an alert
    without closing the hole: a dataset name carrying a newline still writes a
    second line that looks exactly like a genuine record, in a project that reads
    its own logs back as evidence.
    """
    import logging

    forged = "notes\n2026-08-04 INFO kb.service Ingest accepted for dataset central"
    kb = Citadel(CitadelConfig(), cognee=FakeCognee())

    with caplog.at_level(logging.INFO, logger="kb.service"):
        # 1. the pre-ingest filter refuses it
        empty = await kb.ingest("   ", dataset=forged)
        # 2. the same bytes arrive twice in one process
        await kb.ingest("a real note", dataset=forged)
        duplicate = await kb.ingest("a real note", dataset=forged)

    assert empty.reason == "empty"
    assert duplicate.reason == "duplicate_in_process"

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2, messages
    for message in messages:
        assert "\n" not in message, "a caller-supplied value opened a new log line"
        assert "\\n" in message, "the value must survive the escaping, not be deleted"


def test_lifecycle_wires_projection_state_lookup_into_cognee(
    tmp_path, monkeypatch
) -> None:
    # The post-cognify stored check partitions missing document ids by
    # projection state through this hook (#286). The closure must preserve the
    # configured generation and projection contract. Unwired (lifecycle off)
    # it stays None and the check remains fully fail-closed.
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(
            lifecycle_enabled=True,
            lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
        ),
        cognee=fake,
    )
    assert kb.lifecycle_store is not None
    captured: dict[str, Any] = {}

    def state_lookup(
        source_revision_ids: list[str], **scope: str
    ) -> tuple[set[str], set[str]]:
        captured.update(scope)
        return set(source_revision_ids), set()

    monkeypatch.setattr(
        kb.lifecycle_store,
        "projection_source_revision_states",
        state_lookup,
    )
    projection = kb._lifecycle_projection_request()
    assert fake.lifecycle_projection_state_lookup(["revision-1"]) == (
        {"revision-1"},
        set(),
    )
    assert captured == {
        "generation_id": projection.generation_id,
        "projection_version": projection.projection_version,
        "config_digest": projection.config_digest,
    }

    unwired = Citadel(CitadelConfig(lifecycle_enabled=False), cognee=FakeCognee())
    assert getattr(unwired.cognee, "lifecycle_projection_state_lookup", None) is None
