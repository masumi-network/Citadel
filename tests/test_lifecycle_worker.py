"""Projection worker contracts for lifecycle v1."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from kb.lifecycle import (
    CaptureContext,
    LifecycleStore,
    ProjectionLease,
    ProjectionRequest,
)
from kb.lifecycle_worker import (
    LifecycleProjectionWorker,
    ProjectionVerificationError,
    _LeaseHeartbeat,
)


T0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class FakeProjectionGateway:
    def __init__(self) -> None:
        self.remember_calls: list[dict[str, Any]] = []
        self.cognify_calls: list[dict[str, Any]] = []
        self.cognify_selected_data_calls: list[dict[str, Any]] = []
        self.document_id: str | None = None
        self.document_ids: list[str] = []
        self.chunk_count = 2
        self.graph_present = True
        self.projected = False

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_calls.append({"data": data, **kwargs})
        self.document_id = str(kwargs["data_id"])
        if self.document_id not in self.document_ids:
            self.document_ids.append(self.document_id)
        return {"added": [self.document_id], "cognify": "deferred"}

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        self.cognify_calls.append(kwargs)
        self.projected = True
        return {"processed": [self.document_id]}

    async def cognify_selected_data(self, **kwargs: Any) -> dict[str, Any]:
        self.cognify_selected_data_calls.append(kwargs)
        self.projected = True
        return {"processed": kwargs["data_ids"]}

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]:
        assert datasets == ["seat:alice"]
        known_ids = list(self.document_ids)
        if self.document_id is not None and self.document_id not in known_ids:
            known_ids.append(self.document_id)
        return known_ids

    async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
        return {
            str(document_id): self.chunk_count if self.projected else 0
            for document_id in document_ids
            if str(document_id) in await self.dataset_document_ids(["seat:alice"])
        }

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str]:
        assert datasets == ["seat:alice"]
        return (
            {
                str(document_id)
                for document_id in document_ids
                if str(document_id) in await self.dataset_document_ids(["seat:alice"])
            }
            if self.projected and self.graph_present
            else set()
        )


class VectorFirstProjectionGateway(FakeProjectionGateway):
    def __init__(self) -> None:
        super().__init__()
        self.vector_project_calls: list[dict[str, Any]] = []

    async def vector_project(self, **kwargs: Any) -> dict[str, Any]:
        self.vector_project_calls.append(kwargs)
        self.projected = True
        return {"operation_id": "vector-projection-1"}


class DatasetWideOnlyProjectionGateway(VectorFirstProjectionGateway):
    cognify_selected_data = None


class SlowBatchRememberGateway(VectorFirstProjectionGateway):
    def __init__(self) -> None:
        super().__init__()
        self.remember_started = asyncio.Event()
        self.remember_cancelled = asyncio.Event()
        self.release_remember = asyncio.Event()

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_started.set()
        try:
            await self.release_remember.wait()
        except asyncio.CancelledError:
            self.remember_cancelled.set()
            raise
        return await super().remember(data, **kwargs)


class SlowBatchVectorGateway(VectorFirstProjectionGateway):
    def __init__(self) -> None:
        super().__init__()
        self.vector_started = asyncio.Event()
        self.vector_cancelled = asyncio.Event()
        self.release_vector = asyncio.Event()

    async def vector_project(self, **kwargs: Any) -> dict[str, Any]:
        self.vector_started.set()
        try:
            await self.release_vector.wait()
        except asyncio.CancelledError:
            self.vector_cancelled.set()
            raise
        return await super().vector_project(**kwargs)


class GraphQuotaProjectionGateway(VectorFirstProjectionGateway):
    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("OpenRouter free-model daily quota is exhausted")

    async def cognify_selected_data(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("OpenRouter free-model daily quota is exhausted")


class MalformedGraphProjectionGateway(VectorFirstProjectionGateway):
    def __init__(self) -> None:
        super().__init__()
        self.graph_present = False

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError(
            "1 validation error for KnowledgeGraph: "
            "edges.1.target_node_id Input should be a valid string "
            "[type=string_type, input_value=None, input_type=None]"
        )

    async def cognify_selected_data(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError(
            "1 validation error for KnowledgeGraph: "
            "edges.1.target_node_id Input should be a valid string "
            "[type=string_type, input_value=None, input_type=None]"
        )


def _batch_projection() -> ProjectionRequest:
    return ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )


def _accept_batch_source(
    store: LifecycleStore,
    projection: ProjectionRequest,
    source_key: str,
) -> Any:
    return store.accept_source(
        f"content for {source_key}".encode(),
        capture=CaptureContext(
            dataset="seat:alice",
            source_key=source_key,
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id=source_key,
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )


class MissingLocalPathGateway(FakeProjectionGateway):
    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_calls.append({"data": data, **kwargs})
        raise FileNotFoundError(
            "Storage directory does not exist: "
            "'/private/tmp/claude-501/marker3_pathfile.txt'"
        )


class SlowRememberGateway(FakeProjectionGateway):
    def __init__(self) -> None:
        super().__init__()
        self.remember_started = asyncio.Event()
        self.remember_cancelled = asyncio.Event()
        self.release_remember = asyncio.Event()

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_started.set()
        try:
            await self.release_remember.wait()
        except asyncio.CancelledError:
            self.remember_cancelled.set()
            raise
        return await super().remember(data, **kwargs)


class PersistentProjectionGateway:
    """Process-safe-enough provider fixture for kill and resume tests."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "document_ids": [],
                "remember_calls": [],
                "cognify_calls": 0,
                "chunk_counts": {},
                "graph_ids": [],
            }
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        state = self._load()
        document_id = str(kwargs["data_id"])
        state["remember_calls"].append(document_id)
        if document_id not in state["document_ids"]:
            state["document_ids"].append(document_id)
        self._save(state)
        return {"id": document_id}

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        state = self._load()
        state["cognify_calls"] += 1
        state.setdefault("cognify_force", []).append(bool(kwargs.get("force")))
        for document_id in state["document_ids"]:
            state["chunk_counts"][document_id] = 1
            if document_id not in state["graph_ids"]:
                state["graph_ids"].append(document_id)
        self._save(state)
        return {"operation_id": f"cognify-{state['cognify_calls']}"}
    async def cognify_selected_data(self, **kwargs: Any) -> dict[str, Any]:
        state = self._load()
        state["cognify_calls"] += 1
        state.setdefault("cognify_force", []).append(bool(kwargs.get("force")))
        state.setdefault("cognify_selected_data_calls", []).append(
            {
                "dataset": kwargs["dataset"],
                "data_ids": list(kwargs["data_ids"]),
                "force": bool(kwargs.get("force")),
            }
        )
        selected_ids = {str(item) for item in kwargs["data_ids"]}
        for document_id in state["document_ids"]:
            if document_id not in selected_ids:
                continue
            state["chunk_counts"][document_id] = 1
            if document_id not in state["graph_ids"]:
                state["graph_ids"].append(document_id)
        self._save(state)
        return {"operation_id": f"cognify-{state['cognify_calls']}"}


    async def dataset_document_ids(self, datasets: list[str]) -> list[str]:
        return [str(item) for item in self._load()["document_ids"]]

    async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
        counts = self._load()["chunk_counts"]
        return {document_id: int(counts.get(document_id, 0)) for document_id in document_ids}

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str]:
        assert datasets == ["seat:alice"]
        present = set(self._load()["graph_ids"])
        return {document_id for document_id in document_ids if document_id in present}


def _run_projection_process(
    lifecycle_path: str,
    provider_path: str,
    now_text: str,
    fault_stage: str | None,
) -> None:
    def crash(stage: str) -> None:
        if stage == fault_stage:
            os._exit(78)

    worker = LifecycleProjectionWorker(
        LifecycleStore(lifecycle_path),
        PersistentProjectionGateway(provider_path),
        worker_id=f"worker-{os.getpid()}",
        lease_seconds=1,
        fault_injector=crash if fault_stage is not None else None,
    )
    asyncio.run(
        worker.run_once(
            now=datetime.fromisoformat(now_text.replace("Z", "+00:00")),
        )
    )


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_backend_write:relational",
        "after_receipt_write:relational",
        "after_searchability_check:relational",
        "after_backend_write:vector",
        "after_backend_write:graph",
        "after_receipt_write:vector",
        "after_receipt_write:graph",
        "after_searchability_check:vector",
        "after_searchability_check:graph",
    ],
)
def test_process_death_during_projection_converges_on_restart(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    provider_path = tmp_path / "provider.json"
    store = LifecycleStore(lifecycle_path)
    accepted = store.accept_source(
        b"process restart projection",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:worker-process-restart",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-process-restart",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    process_context = multiprocessing.get_context("spawn")
    crashed = process_context.Process(
        target=_run_projection_process,
        args=(str(lifecycle_path), str(provider_path), T0.isoformat(), fault_stage),
    )
    crashed.start()
    crashed.join(timeout=10)
    assert crashed.exitcode == 78

    recovered = process_context.Process(
        target=_run_projection_process,
        args=(
            str(lifecycle_path),
            str(provider_path),
            (T0 + timedelta(seconds=2)).isoformat(),
            None,
        ),
    )
    recovered.start()
    recovered.join(timeout=10)
    assert recovered.exitcode == 0

    operation = LifecycleStore(lifecycle_path).get_operation(accepted.projection_job_id)
    assert operation.state == "searchable"
    assert {receipt.state for receipt in operation.receipts} == {"searchable"}
    census = LifecycleStore(lifecycle_path).census()
    assert (census.source_revisions, census.projection_jobs, census.projection_receipts) == (
        1,
        1,
        3,
    )
    provider_state = json.loads(provider_path.read_text(encoding="utf-8"))
    assert set(provider_state["document_ids"]) == {accepted.source_revision_id}


async def test_worker_projects_retained_source_and_attests_all_backends(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"worker retained source",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:worker-source",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-worker",
            captured_at=T0,
            metadata={
                "tags": ["manual", "architecture"],
                "session_id": "session-1",
                "attestation": {
                    "promoted_by": "admin-1",
                    "promoted_at": "2026-08-09T12:00:00Z",
                },
            },
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    gateway = FakeProjectionGateway()
    worker = LifecycleProjectionWorker(store, gateway, worker_id="worker-1")

    processed = await worker.run_once(now=T0)

    assert processed is True
    assert gateway.remember_calls == [
        {
            "data": "worker retained source",
            "dataset_name": "seat:alice",
            "data_id": accepted.source_revision_id,
            "defer_cognify": True,
            "tags": ("manual", "architecture"),
            "session_id": "session-1",
            "attestation": {
                "promoted_by": "admin-1",
                "promoted_at": "2026-08-09T12:00:00Z",
            },
        }
    ]
    assert gateway.cognify_selected_data_calls == [
        {
            "dataset": "seat:alice",
            "data_ids": [accepted.source_revision_id],
            "force": False,
        }
    ]
    assert gateway.cognify_calls == []
    operation = store.get_operation(accepted.projection_job_id)
    assert operation.state == "searchable"
    assert {receipt.state for receipt in operation.receipts} == {"searchable"}
    assert next(
        receipt.affected_count
        for receipt in operation.receipts
        if receipt.backend == "vector"
    ) == 2


@pytest.mark.asyncio
async def test_worker_runs_vector_projection_before_llm_graph_enrichment(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"vector first source",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:vector-first",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-vector-first",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    gateway = VectorFirstProjectionGateway()
    worker = LifecycleProjectionWorker(store, gateway, worker_id="worker-vector-first")

    assert await worker.run_once(now=T0) is True

    operation = store.get_operation(accepted.projection_job_id)
    assert gateway.vector_project_calls == [
        {
            "datasets": ["seat:alice"],
            "force": False,
            "document_ids": [accepted.source_revision_id],
        }
    ]
    assert gateway.cognify_selected_data_calls == [
        {
            "dataset": "seat:alice",
            "data_ids": [accepted.source_revision_id],
            "force": False,
        }
    ]
    assert gateway.cognify_calls == []
    assert [receipt.state for receipt in operation.receipts] == [
        "searchable",
        "searchable",
        "searchable",
    ]


@pytest.mark.asyncio
async def test_graph_projection_requires_selected_data_method(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = _accept_batch_source(store, _batch_projection(), "manual:missing-seam")
    gateway = DatasetWideOnlyProjectionGateway()
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-missing-selected-data",
    )

    with pytest.raises(ProjectionVerificationError, match="cognify_selected_data"):
        await worker.run_once(now=T0)

    assert gateway.cognify_calls == []
    operation = store.get_operation(accepted.projection_job_id)
    assert operation.job.state == "pending"


@pytest.mark.asyncio
async def test_vector_lane_releases_graph_work_without_blocking_search(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = store.accept_source(
        b"vector lane does not wait for graph enrichment",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:vector-lane",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-vector-lane",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    gateway = VectorFirstProjectionGateway()
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-lane",
        include_graph=False,
    )

    assert await worker.run_once(now=T0) is True

    operation = store.get_operation(accepted.projection_job_id)
    assert operation.job.state == "deferred"
    assert operation.state == "pending"
    assert gateway.vector_project_calls == [
        {
            "datasets": ["seat:alice"],
            "force": False,
            "document_ids": [accepted.source_revision_id],
        }
    ]
    assert gateway.cognify_calls == []
    assert store.searchable_source_revision_ids(
        dataset="seat:alice",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    ) == (accepted.source_revision_id,)
    binding = store.retrieval_binding(
        accepted.source_revision_id,
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    )
    assert binding is not None
    assert binding.receipt is not None
    assert binding.receipt.backend == "vector"


@pytest.mark.asyncio
async def test_vector_lane_batches_same_dataset_sources_with_separate_receipts(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted_ids = []
    accepted_jobs = []
    for source_key in ("manual:batch:one", "manual:batch:two"):
        accepted = store.accept_source(
            source_key.encode(),
            capture=CaptureContext(
                dataset="seat:alice",
                source_key=source_key,
                source_locator=None,
                media_type="text/plain",
                capture_actor_id="alice",
                capture_run_id=source_key,
                captured_at=T0,
            ),
            projection=projection,
            now=T0,
        )
        accepted_ids.append(accepted.source_revision_id)
        accepted_jobs.append(accepted.projection_job_id)

    gateway = VectorFirstProjectionGateway()
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-batch",
        include_graph=False,
    )

    assert await worker.run_batch(max_jobs=2, now=T0) is True

    assert len(gateway.vector_project_calls) == 1
    assert set(gateway.vector_project_calls[0]["document_ids"]) == set(accepted_ids)
    for source_id, projection_job_id in zip(accepted_ids, accepted_jobs):
        operation = store.get_operation(projection_job_id)
        assert operation.job.state == "deferred"
        assert next(
            receipt.state for receipt in operation.receipts if receipt.backend == "vector"
        ) == "searchable"


@pytest.mark.asyncio
async def test_vector_batch_cancellation_releases_relational_leases(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = _batch_projection()
    accepted = [
        _accept_batch_source(store, projection, source_key)
        for source_key in ("manual:cancel:one", "manual:cancel:two")
    ]
    gateway = SlowBatchRememberGateway()
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-batch-cancel-relational",
        include_graph=False,
    )

    task = asyncio.create_task(worker.run_batch(max_jobs=2, now=T0))
    await asyncio.wait_for(gateway.remember_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert gateway.remember_cancelled.is_set()
    for source in accepted:
        operation = store.get_operation(source.projection_job_id)
        assert operation.job.state == "pending"
        assert operation.job.lease_id is None
        assert {receipt.state for receipt in operation.receipts} == {"pending"}


@pytest.mark.asyncio
async def test_vector_batch_cancellation_releases_vector_leases(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = _batch_projection()
    accepted = [
        _accept_batch_source(store, projection, source_key)
        for source_key in ("manual:cancel:vector:one", "manual:cancel:vector:two")
    ]
    gateway = SlowBatchVectorGateway()
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-batch-cancel-vector",
        include_graph=False,
    )

    task = asyncio.create_task(worker.run_batch(max_jobs=2, now=T0))
    await asyncio.wait_for(gateway.vector_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert gateway.vector_cancelled.is_set()
    for source in accepted:
        operation = store.get_operation(source.projection_job_id)
        assert operation.job.state == "pending"
        assert operation.job.lease_id is None
        receipt_states = {receipt.backend: receipt.state for receipt in operation.receipts}
        assert receipt_states == {
            "relational": "searchable",
            "vector": "pending",
            "graph": "pending",
        }


@pytest.mark.asyncio
async def test_graph_lane_claims_deferred_work_after_vector_lane(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = store.accept_source(
        b"graph lane resumes deferred source",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:graph-lane",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-graph-lane",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    gateway = VectorFirstProjectionGateway()
    vector_worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-lane",
        include_graph=False,
    )
    graph_worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-graph-lane",
        include_graph=True,
        include_deferred=True,
    )

    assert await vector_worker.run_once(now=T0) is True
    assert await graph_worker.run_once(now=T0 + timedelta(seconds=3601)) is True

    operation = store.get_operation(accepted.projection_job_id)
    assert operation.state == "searchable"
    # The provider already exposed the graph source after vector projection, so
    # the graph lane closes its receipt through reconciliation without another
    # LLM call.
    assert gateway.cognify_calls == []


@pytest.mark.asyncio
async def test_vector_search_remains_available_while_graph_quota_retries(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = store.accept_source(
        b"vector remains usable during graph retry",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:graph-quota",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-graph-quota",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    gateway = GraphQuotaProjectionGateway()
    worker = LifecycleProjectionWorker(store, gateway, worker_id="worker-graph-quota")

    assert await worker.run_once(now=T0) is True

    operation = store.get_operation(accepted.projection_job_id)
    vector = next(receipt for receipt in operation.receipts if receipt.backend == "vector")
    graph = next(receipt for receipt in operation.receipts if receipt.backend == "graph")
    assert operation.job.state == "pending"
    assert vector.state == "searchable"
    assert graph.state == "pending"
    assert store.searchable_source_revision_ids(
        dataset="seat:alice",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    ) == (accepted.source_revision_id,)


@pytest.mark.asyncio
async def test_malformed_graph_output_defers_without_failing_vector_search(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = store.accept_source(
        b"vector remains searchable after malformed graph output",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:graph-structured-output",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-graph-structured-output",
            captured_at=T0,
        ),
        projection=projection,
        now=T0,
    )
    gateway = MalformedGraphProjectionGateway()
    vector_worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-vector-lane",
        include_graph=False,
    )
    graph_worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-graph-lane",
        include_graph=True,
        include_deferred=True,
        deferred_only=True,
    )

    assert await vector_worker.run_once(now=T0) is True
    assert await graph_worker.run_once(now=T0 + timedelta(seconds=3601)) is True

    operation = store.get_operation(accepted.projection_job_id)
    vector = next(receipt for receipt in operation.receipts if receipt.backend == "vector")
    graph = next(receipt for receipt in operation.receipts if receipt.backend == "graph")
    assert operation.state == "pending"
    assert operation.job.state == "deferred"
    assert operation.job.last_error_code == "graph_output_invalid"
    assert vector.state == "searchable"
    assert graph.state == "pending"
    assert store.searchable_source_revision_ids(
        dataset="seat:alice",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    ) == (accepted.source_revision_id,)


async def test_worker_reconciles_existing_provider_projection_without_rewriting(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"already projected by a coalesced connector run",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:coalesced-provider",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-coalesced",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    gateway = FakeProjectionGateway()
    gateway.document_id = accepted.source_revision_id
    gateway.projected = True
    worker = LifecycleProjectionWorker(store, gateway, worker_id="worker-1")

    assert await worker.run_once(now=T0) is True

    assert gateway.remember_calls == []
    assert gateway.cognify_calls == []
    operation = store.get_operation(accepted.projection_job_id)
    assert operation.state == "searchable"
    assert all(receipt.metadata["reconciled"] is True for receipt in operation.receipts)


async def test_worker_reschedules_completed_provider_call_until_read_check_passes(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"provider call needs a read receipt",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:read-receipt",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-read-receipt",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    gateway = FakeProjectionGateway()
    gateway.chunk_count = 0
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-1",
        retry_backoff_seconds=5,
    )

    with pytest.raises(ProjectionVerificationError, match="no chunks"):
        await worker.run_once(now=T0)

    failed_check = store.get_operation(accepted.projection_job_id)
    assert failed_check.state == "completed"
    assert failed_check.job.state == "pending"
    assert failed_check.job.last_error_code == "ProjectionVerificationError"
    assert next(
        receipt.state
        for receipt in failed_check.receipts
        if receipt.backend == "vector"
    ) == "completed"

    gateway.chunk_count = 2
    assert await worker.run_once(now=T0.replace(second=5)) is True
    assert store.get_operation(accepted.projection_job_id).state == "searchable"
    assert len(gateway.cognify_selected_data_calls) == 1


async def test_worker_marks_job_failed_after_bounded_attempts(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"permanent provider read failure",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:terminal-provider-failure",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-terminal-failure",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )
    gateway = FakeProjectionGateway()
    gateway.chunk_count = 0
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-terminal",
        max_attempts=1,
    )

    with pytest.raises(ProjectionVerificationError, match="no chunks"):
        await worker.run_once(now=T0)

    operation = store.get_operation(accepted.projection_job_id)
    assert operation.state == "failed"
    assert operation.job.state == "failed"
    assert operation.job.last_error_code == "ProjectionVerificationError"
    assert operation.job.lease_id is None
    assert {receipt.state for receipt in operation.receipts} == {
        "failed",
        "searchable",
    }
    assert store.next_wakeup_delay(now=T0) is None

    retried = store.accept_source(
        b"permanent provider read failure",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:terminal-provider-failure",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="run-terminal-failure",
            captured_at=T0,
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
        now=T0,
    )

    assert retried.projection_job_id == accepted.projection_job_id
    assert retried.operation.state == "pending"
    assert {receipt.state for receipt in retried.operation.receipts} == {"pending"}


async def test_worker_does_not_tombstone_missing_path_when_content_retained(
    tmp_path: Path,
) -> None:
    """A missing local file must not tombstone a source whose durable
    retained_content is still stored. accept_tombstone replaces the head
    globally and rollback cannot restore it, so a systemic FileNotFoundError
    during a bulk rebuild (or a path-string note) bounded-fails instead.
    """
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    capture = CaptureContext(
        dataset="seat:alice",
        source_key="manual:marker3-pathfile",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="alice",
        capture_run_id="run-pathfile",
        captured_at=T0,
    )
    accepted = store.accept_source(
        b"/private/tmp/claude-501/marker3_pathfile.txt",
        capture=capture,
        projection=projection,
        now=T0,
    )
    worker = LifecycleProjectionWorker(
        store,
        MissingLocalPathGateway(),
        worker_id="worker-pathfile",
        max_attempts=1,
    )

    with pytest.raises(FileNotFoundError):
        await worker.run_once(now=T0)

    operation = store.get_operation(accepted.projection_job_id)
    assert operation.job.state == "failed"
    assert operation.job.last_error_code == "FileNotFoundError"
    current = store.current_revisions_for_source(
        capture.dataset,
        capture.source_key,
        include_chunks=False,
    )
    assert len(current) == 1
    assert current[0].tombstone is False
    assert store.read_retained_content(current[0].source_revision_id) == (
        b"/private/tmp/claude-501/marker3_pathfile.txt"
    )


async def test_worker_does_not_tombstone_on_reconcile_graph_filenotfound(
    tmp_path: Path,
) -> None:
    """The real bulk-rebuild shape: an existing Cognee Data row reconciles at
    the relational stage, then the graph-presence read raises a nested
    FileNotFoundError (e.g. an uncreated Ladybug storage dir). The current head
    must be preserved, not tombstoned, because rollback cannot restore it.
    """

    class ReconcileGraphMissingGateway(FakeProjectionGateway):
        async def corpus_graph_presence(
            self, document_ids: list[str], *, datasets: list[str] | None = None
        ) -> set[str]:
            raise FileNotFoundError(
                "Storage directory does not exist: '/data/ladybug-home/graph_db'"
            )

    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    capture = CaptureContext(
        dataset="seat:alice",
        source_key="manual:existing-row",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="alice",
        capture_run_id="run-existing-row",
        captured_at=T0,
    )
    accepted = store.accept_source(
        b"the actual note body",
        capture=capture,
        projection=projection,
        now=T0,
    )
    gateway = ReconcileGraphMissingGateway()
    # Present the source as an existing Data row so the relational stage
    # reconciles without an add; the failure then arises in the graph-presence
    # read, exactly as a bulk rebuild would hit it.
    gateway.document_ids = [accepted.source_revision_id]
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-existing-row",
        max_attempts=1,
    )

    with pytest.raises(FileNotFoundError):
        await worker.run_once(now=T0)

    assert gateway.remember_calls == []
    current = store.current_revisions_for_source(
        capture.dataset,
        capture.source_key,
        include_chunks=False,
    )
    assert len(current) == 1
    assert current[0].tombstone is False
    operation = store.get_operation(accepted.projection_job_id)
    assert operation.job.state == "failed"
    assert operation.job.last_error_code == "FileNotFoundError"


async def test_worker_projects_tombstone_as_provider_neutral_exclusion(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    capture = CaptureContext(
        dataset="seat:alice",
        source_key="manual:tombstone-worker",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="alice",
        capture_run_id="run-tombstone",
        captured_at=T0,
    )
    projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    store.accept_source(
        b"content before tombstone",
        capture=capture,
        projection=projection,
        now=T0,
    )
    tombstone = store.accept_tombstone(
        reason="manual note removed",
        capture=capture,
        projection=projection,
        now=T0,
    )
    gateway = FakeProjectionGateway()
    worker = LifecycleProjectionWorker(store, gateway, worker_id="worker-1")

    assert await worker.run_once(now=T0) is True

    operation = store.get_operation(tombstone.projection_job_id)
    assert operation.state == "searchable"
    assert gateway.remember_calls == []
    assert gateway.cognify_calls == []
    assert {receipt.affected_count for receipt in operation.receipts} == {0}
    assert all(receipt.metadata["tombstone"] is True for receipt in operation.receipts)


async def test_empty_generation_rebuild_converges_against_fresh_provider_state(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    original_projection = ProjectionRequest(
        generation_id="generation-1",
        projection_version="projection-v1",
        config_digest="sha256:config-1",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    accepted = [
        store.accept_source(
            content,
            capture=CaptureContext(
                dataset="seat:alice",
                source_key=f"manual:rebuild:{index}",
                source_locator=None,
                media_type="text/plain",
                capture_actor_id="alice",
                capture_run_id="rebuild-fixture",
                captured_at=T0,
            ),
            projection=original_projection,
            now=T0,
        )
        for index, content in enumerate((b"source one", b"source two"), start=1)
    ]
    old_worker = LifecycleProjectionWorker(
        store,
        PersistentProjectionGateway(str(tmp_path / "provider-old.json")),
        worker_id="worker-old",
        generation_id="generation-1",
    )
    assert await old_worker.run_once(now=T0) is True
    assert await old_worker.run_once(now=T0) is True
    assert await old_worker.run_once(now=T0) is False

    target = ProjectionRequest(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
        providers={
            "relational": "sqlite",
            "vector": "qdrant",
            "graph": "ladybug",
        },
    )
    rebuilt = store.queue_generation_rebuild(target, now=T0)
    fresh_provider_path = tmp_path / "provider-new.json"
    new_worker = LifecycleProjectionWorker(
        store,
        PersistentProjectionGateway(str(fresh_provider_path)),
        worker_id="worker-new",
        generation_id="generation-2",
    )

    assert await new_worker.run_once(now=T0) is True
    assert await new_worker.run_once(now=T0) is True
    assert await new_worker.run_once(now=T0) is False

    assert {item.source_revision.source_revision_id for item in rebuilt} == {
        item.source_revision_id for item in accepted
    }
    assert all(
        store.get_operation(item.job.projection_job_id).state == "searchable"
        for item in rebuilt
    )
    provider_state = json.loads(fresh_provider_path.read_text(encoding="utf-8"))
    assert provider_state["cognify_force"] == [True, True]
    selected_calls = provider_state["cognify_selected_data_calls"]
    assert len(selected_calls) == len(accepted)
    assert {
        (
            call["dataset"],
            tuple(call["data_ids"]),
            call["force"],
        )
        for call in selected_calls
    } == {
        ("seat:alice", (item.source_revision_id,), True) for item in accepted
    }
    assert set(provider_state["document_ids"]) == {
        item.source_revision_id for item in accepted
    }
    generation = store.generation_census(
        generation_id="generation-2",
        projection_version="projection-v1",
        config_digest="sha256:config-2",
    )
    assert generation.current_sources == 2
    assert generation.current_projection_jobs == 2
    assert generation.current_projection_receipts == 6
    assert generation.current_searchable_by_backend == {
        "graph": 2,
        "relational": 2,
        "vector": 2,
    }


@pytest.mark.parametrize("eager_task_factory", [False, True], ids=["default", "eager"])
async def test_heartbeat_reaps_provider_when_initial_renewal_fails(
    eager_task_factory: bool,
) -> None:
    renewal_error = RuntimeError("lost lease")

    class FailingRenewalStore:
        def renew_lease(self, *_args: Any, **_kwargs: Any) -> None:
            raise renewal_error

    provider_started = False
    running_provider_started = asyncio.Event()
    running_provider_cancelled = asyncio.Event()

    async def provider() -> str:
        nonlocal provider_started
        provider_started = True
        return "provider result"

    async def running_provider() -> None:
        running_provider_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            running_provider_cancelled.set()
            raise

    loop = asyncio.get_running_loop()
    previous_task_factory = loop.get_task_factory()
    if eager_task_factory:
        factory = getattr(asyncio, "eager_task_factory", None)
        if factory is None:
            pytest.skip("asyncio.eager_task_factory requires Python 3.12+")
        loop.set_task_factory(factory)

    provider_coroutine = provider()
    running_task: asyncio.Task[None] | None = None
    heartbeat = _LeaseHeartbeat(
        FailingRenewalStore(),  # type: ignore[arg-type]
        ProjectionLease(
            projection_job_id="job-1",
            lease_id="lease-1",
            worker_id="worker-1",
            leased_until="2026-08-09T12:00:30Z",
            attempt=1,
        ),
        lease_seconds=30,
        started_at=T0,
    )

    try:
        with pytest.raises(RuntimeError) as raised:
            await heartbeat.wait(provider_coroutine)

        assert raised.value is renewal_error
        assert provider_started is False
        assert inspect.getcoroutinestate(provider_coroutine) == inspect.CORO_CLOSED

        running_task = asyncio.create_task(running_provider())
        await running_provider_started.wait()

        with pytest.raises(RuntimeError) as raised:
            await heartbeat.wait(running_task)

        assert raised.value is renewal_error
        assert running_provider_cancelled.is_set()
        assert running_task.cancelled()
    finally:
        provider_coroutine.close()
        if running_task is not None and not running_task.done():
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)
        loop.set_task_factory(previous_task_factory)


@pytest.mark.parametrize(
    "complete_projection",
    [False, True],
    ids=["cancelled", "completed"],
)
async def test_slow_provider_write_renews_lease_before_second_worker_can_claim(
    monkeypatch: Any,
    tmp_path: Path,
    complete_projection: bool,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    accepted = store.accept_source(
        b"slow projection",
        capture=CaptureContext(
            dataset="seat:alice",
            source_key="manual:slow-provider",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="alice",
            capture_run_id="slow-run",
            captured_at=datetime.now(UTC),
        ),
        projection=ProjectionRequest(
            generation_id="generation-1",
            projection_version="projection-v1",
            config_digest="sha256:config-1",
            providers={
                "relational": "sqlite",
                "vector": "qdrant",
                "graph": "ladybug",
            },
        ),
    )
    gateway = SlowRememberGateway()
    periodic_renewal = asyncio.Event()
    renew_calls = 0
    periodic_renewed_at: datetime | None = None
    renew_lease = store.renew_lease
    lease_seconds = 1.5
    started_at = datetime.now(UTC)
    cleanup_renewed_at: datetime | None = None

    def track_renewal(*args: Any, **kwargs: Any) -> None:
        nonlocal periodic_renewed_at, renew_calls
        renewal_kwargs = dict(kwargs)
        if cleanup_renewed_at is not None:
            renewal_kwargs["now"] = cleanup_renewed_at
        renew_lease(*args, **renewal_kwargs)
        renew_calls += 1
        if gateway.remember_started.is_set() and not gateway.release_remember.is_set():
            periodic_renewed_at = kwargs["now"]
            periodic_renewal.set()

    monkeypatch.setattr(store, "renew_lease", track_renewal)
    worker = LifecycleProjectionWorker(
        store,
        gateway,
        worker_id="worker-slow",
        lease_seconds=lease_seconds,
    )

    projection = asyncio.create_task(worker.run_once(now=started_at))
    try:
        await asyncio.wait_for(gateway.remember_started.wait(), timeout=5)
        await asyncio.wait_for(periodic_renewal.wait(), timeout=5)

        assert renew_calls >= 3
        assert periodic_renewed_at is not None
        original_expiry = started_at + timedelta(seconds=lease_seconds)
        renewed_expiry = periodic_renewed_at + timedelta(seconds=lease_seconds)
        assert renewed_expiry > original_expiry
        probe_at = original_expiry + (renewed_expiry - original_expiry) / 2
        assert original_expiry < probe_at < renewed_expiry
        assert (
            store.claim_next_job(
                worker_id="worker-second",
                lease_seconds=lease_seconds,
                now=probe_at,
            )
            is None
        )
        if complete_projection:
            cleanup_renewed_at = periodic_renewed_at
            gateway.release_remember.set()
            assert await asyncio.wait_for(projection, timeout=5) is True
        else:
            projection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(projection, timeout=5)
    finally:
        if not projection.done():
            projection.cancel()
            await asyncio.wait_for(
                asyncio.gather(projection, return_exceptions=True),
                timeout=5,
            )

    operation = store.get_operation(accepted.projection_job_id)
    if complete_projection:
        assert not gateway.remember_cancelled.is_set()
        assert len(gateway.remember_calls) == 1
        assert operation.state == "searchable"
        assert operation.job.state == "completed"
    else:
        assert gateway.remember_cancelled.is_set()
        assert projection.cancelled()
        assert not gateway.release_remember.is_set()
        assert gateway.remember_calls == []
        assert operation.state == "pending"
        assert operation.job.lease_id is None
        assert operation.job.lease_owner is None
        assert operation.job.leased_until is None
        assert {receipt.state for receipt in operation.receipts} == {"pending"}
