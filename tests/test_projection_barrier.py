from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kb.lifecycle import CaptureContext, LifecycleStore, ProjectionRequest
from kb.projection_barrier import ProjectionBarrierResult, wait_for_projection_barrier
from kb.service import Citadel

_REQUIRED_RECEIPTS = (
    {"backend": "relational", "state": "searchable"},
    {"backend": "vector", "state": "searchable"},
    {"backend": "graph", "state": "searchable"},
)


class FakeCitadel:
    def __init__(self, operations: dict[str, dict[str, Any]]) -> None:
        self.operations = operations
        self.resume_calls: list[bool] = []
        self.poll_calls: list[str] = []

    def resume_lifecycle_queue(self, *, include_deferred: bool = False) -> bool:
        self.resume_calls.append(include_deferred)
        return True

    def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
        self.poll_calls.append(projection_job_id)
        operation = self.operations[projection_job_id]
        if callable(operation):
            return operation()
        return operation


class ExactFilterCitadel:
    def __init__(self) -> None:
        self.block = False
        self.release = asyncio.Event()
        self.filter_calls: list[
            tuple[tuple[str, ...] | None, str | None]
        ] = []
        self.started = asyncio.Event()
        self.exact_job_id = "job:exact"

    def resume_lifecycle_queue(
        self,
        *,
        include_deferred: bool = False,
        job_ids: tuple[str, ...] | None = None,
        job_filter_owner: str | None = None,
    ) -> bool:
        assert include_deferred is True
        self.filter_calls.append((job_ids, job_filter_owner))
        return True
    async def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.filter_calls[-1][0] != (self.exact_job_id,):
            return {"state": "pending", "receipts": []}
        return {
            "state": "completed",
            "receipts": list(_REQUIRED_RECEIPTS),
        }




@pytest.mark.asyncio
async def test_barrier_reports_searchable_pending_and_failed_jobs() -> None:
    fake = FakeCitadel(
        {
            "job:searchable": {
                "state": "completed",
                "receipts": list(_REQUIRED_RECEIPTS),
            },
            "job:pending": {
                "state": "deferred",
                "receipts": [
                    {"backend": "relational", "state": "searchable"},
                    {"backend": "vector", "state": "searchable"},
                    {"backend": "graph", "state": "pending"},
                ],
            },
            "job:failed": {
                "state": "failed",
                "receipts": [
                    {"backend": "relational", "state": "failed"},
                    {"backend": "vector", "state": "searchable"},
                    {"backend": "graph", "state": "pending"},
                ],
            },
        }
    )

    result = await wait_for_projection_barrier(
        fake,
        ["job:searchable", "job:pending", "job:failed"],
        timeout_seconds=0.01,
    )

    assert isinstance(result, ProjectionBarrierResult)
    assert result.job_ids == ("job:searchable", "job:pending", "job:failed")
    assert result.searchable_job_ids == ("job:searchable",)
    assert result.pending_job_ids == ("job:pending",)
    assert result.failed_job_ids == ("job:failed",)
    assert result.complete is False
    assert fake.resume_calls == [True]


@pytest.mark.asyncio
async def test_empty_barrier_is_complete_without_polling() -> None:
    class NoPollingCitadel:
        def resume_lifecycle_queue(self, **kwargs: Any) -> None:
            raise AssertionError(f"empty barrier resumed lifecycle queue: {kwargs}")

        def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
            raise AssertionError(f"empty barrier polled {projection_job_id}")

    result = await wait_for_projection_barrier(
        NoPollingCitadel(), [], timeout_seconds=0.01
    )

    assert result == ProjectionBarrierResult((), (), (), (), True)


@pytest.mark.asyncio
async def test_barrier_timeout_leaves_uncompleted_ids_pending() -> None:
    fake = FakeCitadel(
        {
            "job:pending": {
                "state": "running",
                "receipts": [
                    {"backend": "relational", "state": "searchable"},
                    {"backend": "vector", "state": "running"},
                    {"backend": "graph", "state": "pending"},
                ],
            }
        }
    )

    result = await wait_for_projection_barrier(
        fake, ["job:pending"], timeout_seconds=0.01
    )

    assert result.complete is False
    assert result.pending_job_ids == ("job:pending",)
    assert result.failed_job_ids == ()
    assert len(fake.poll_calls) >= 1


@pytest.mark.asyncio
async def test_barrier_requires_all_relational_vector_and_graph_receipts() -> None:
    fake = FakeCitadel(
        {
            "job:missing-graph": {
                "state": "searchable",
                "receipts": [
                    {"backend": "relational", "state": "searchable"},
                    {"backend": "vector", "state": "searchable"},
                ],
            }
        }
    )

    result = await wait_for_projection_barrier(
        fake, ["job:missing-graph"], timeout_seconds=0.01
    )

    assert result.complete is False
    assert result.searchable_job_ids == ()
    assert result.pending_job_ids == ("job:missing-graph",)


@pytest.mark.asyncio
async def test_barrier_polls_until_operation_becomes_searchable() -> None:
    calls = 0

    def operation() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "state": "searchable" if calls > 1 else "pending",
            "receipts": list(_REQUIRED_RECEIPTS)
            if calls > 1
            else [
                {"backend": "relational", "state": "pending"},
                {"backend": "vector", "state": "pending"},
                {"backend": "graph", "state": "pending"},
            ],
        }

    fake = FakeCitadel({"job:later": operation})
    result = await wait_for_projection_barrier(
        fake, ["job:later"], timeout_seconds=1.1
    )

    assert result.complete is True
    assert result.searchable_job_ids == ("job:later",)
    assert result.pending_job_ids == ()
    assert calls == 2
    assert fake.resume_calls == [True]



@pytest.mark.asyncio
async def test_large_pending_watermark_uses_one_batch_read_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kb import projection_barrier as barrier_module

    job_ids = tuple(f"job:{index}" for index in range(1000))

    class BatchCitadel:
        def __init__(self) -> None:
            self.batch_calls: list[tuple[str, ...]] = []
            self.operation_calls: list[str] = []
            self.resume_calls = 0

        def resume_lifecycle_queue(self, **kwargs: Any) -> bool:
            self.resume_calls += 1
            return True

        def projection_states_for_job_ids(
            self, requested_job_ids: tuple[str, ...]
        ) -> dict[str, dict[str, Any]]:
            self.batch_calls.append(requested_job_ids)
            return {
                job_id: {
                    "state": "running",
                    "receipts": {
                        "relational": "searchable",
                        "vector": "running",
                        "graph": "pending",
                    },
                }
                for job_id in requested_job_ids
            }

        def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
            self.operation_calls.append(projection_job_id)
            raise AssertionError(
                f"large watermark used per-ID read: {projection_job_id}"
            )

    fake = BatchCitadel()
    sleep_calls: list[float] = []
    original_sleep = barrier_module.asyncio.sleep

    async def record_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await original_sleep(delay)
    monkeypatch.setattr(barrier_module.asyncio, "sleep", record_sleep)
    result = await wait_for_projection_barrier(
        fake,
        job_ids,
        timeout_seconds=0.52,
    )


    assert result.job_ids == job_ids
    assert result.searchable_job_ids == ()
    assert result.pending_job_ids == job_ids
    assert result.failed_job_ids == ()
    assert result.complete is False
    assert fake.operation_calls == []
    assert fake.batch_calls
    assert all(call == job_ids for call in fake.batch_calls)
    assert len(fake.batch_calls) == len(sleep_calls) + 1
    assert sleep_calls[0] >= 0.5


@pytest.mark.asyncio
async def test_capture_barrier_ignores_superseded_stale_job(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation-exact",
        projection_version="projection-exact",
        config_digest="sha256:exact",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    capture = CaptureContext(
        dataset="central",
        source_key="source:revised",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="test",
        capture_run_id="capture:watermark",
        captured_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    )

    stale = store.accept_source(
        b"stale revision",
        capture=capture,
        projection=projection,
        now=capture.captured_at,
    )
    current = store.accept_source(
        b"current revision",
        capture=capture,
        projection=projection,
        now=capture.captured_at,
    )
    lease = store.claim_next_job(
        worker_id="barrier-test",
        now=capture.captured_at,
        lease_seconds=30,
    )
    assert lease is not None
    assert lease.projection_job_id == current.projection_job_id
    for backend in ("relational", "vector", "graph"):
        store.begin_backend(lease, backend, now=capture.captured_at)
        store.complete_backend(
            lease,
            backend,
            affected_count=1,
            now=capture.captured_at,
        )
        store.mark_backend_searchable(lease, backend, now=capture.captured_at)

    citadel = object.__new__(Citadel)
    citadel.lifecycle_store = store
    resume_calls: list[tuple[tuple[str, ...] | None, str | None]] = []

    def resume_lifecycle_queue(
        *,
        include_deferred: bool = False,
        job_ids: tuple[str, ...] | None = None,
        job_filter_owner: str | None = None,
    ) -> bool:
        assert include_deferred is True
        resume_calls.append((job_ids, job_filter_owner))
        return True

    citadel.resume_lifecycle_queue = resume_lifecycle_queue
    job_ids = store.projection_job_ids_for_capture_run(
        "capture:watermark",
        generation_id=projection.generation_id,
        projection_version=projection.projection_version,
        config_digest=projection.config_digest,
    )

    result = await wait_for_projection_barrier(
        citadel,
        job_ids,
        timeout_seconds=0.1,
    )

    assert store.get_operation(stale.projection_job_id).state == "stale"
    assert job_ids == (current.projection_job_id,)
    assert result == ProjectionBarrierResult(
        job_ids=(current.projection_job_id,),
        searchable_job_ids=(current.projection_job_id,),
        pending_job_ids=(),
        failed_job_ids=(),
        complete=True,
    )
    assert len(resume_calls) == 2
    assert resume_calls[0][0] == job_ids
    assert resume_calls[0][1]
    assert resume_calls[1] == (None, resume_calls[0][1])


@pytest.mark.asyncio
async def test_lifecycle_operation_wait_resumes_deferred_queue() -> None:
    citadel = object.__new__(Citadel)
    citadel.lifecycle_store = object()
    resume_calls: list[bool] = []
    operation_calls: list[str] = []

    def resume_lifecycle_queue(*, include_deferred: bool = False) -> bool:
        resume_calls.append(include_deferred)
        return True

    def lifecycle_operation(projection_job_id: str) -> dict[str, Any]:
        operation_calls.append(projection_job_id)
        return {
            "projection_job_id": projection_job_id,
            "state": "searchable",
            "receipts": list(_REQUIRED_RECEIPTS),
        }

    citadel.resume_lifecycle_queue = resume_lifecycle_queue
    citadel.lifecycle_operation = lifecycle_operation

    result = await citadel.wait_for_lifecycle_operation(
        "job:deferred", timeout_seconds=0.01
    )


    assert result["state"] == "searchable"
@pytest.mark.asyncio
async def test_resume_lifecycle_queue_preserves_owned_graph_filter() -> None:
    from kb.lifecycle_worker import LifecycleProjectionWorker

    citadel = object.__new__(Citadel)
    citadel._lifecycle_projection_gate = asyncio.Event()
    citadel._lifecycle_vector_only = True
    citadel.lifecycle_store = object()
    citadel.lifecycle_worker = type(
        "Worker",
        (),
        {
            "generation_id": "generation-1",
            "projection_version": "projection-v1",
            "config_digest": "sha256:config-1",
        },
    )()
    citadel._lifecycle_projection_task = None
    citadel._start_lifecycle_projection = lambda: False
    graph_worker = LifecycleProjectionWorker(
        object(),
        object(),
        worker_id="graph-worker",
        include_graph=True,
        include_deferred=True,
        deferred_only=True,
    )
    citadel.lifecycle_graph_worker = graph_worker
    citadel._lifecycle_graph_projection_task = asyncio.create_task(
        asyncio.sleep(10)
    )

    try:
        owner = "barrier-a"
        assert citadel.resume_lifecycle_queue(
            include_deferred=True,
            job_ids=("job:exact",),
            job_filter_owner=owner,
        )
        assert citadel.lifecycle_graph_worker is graph_worker
        assert graph_worker.job_ids == ("job:exact",)
        assert graph_worker.job_filter_owner == owner
        citadel.resume_lifecycle_queue(include_deferred=True)
        assert graph_worker.job_ids == ("job:exact",)
        citadel.resume_lifecycle_queue(include_deferred=True, job_ids=None)
        assert graph_worker.job_ids == ("job:exact",)
        citadel.resume_lifecycle_queue(
            include_deferred=True,
            job_ids=None,
            job_filter_owner=owner,
        )
        assert graph_worker.job_ids is None
        assert graph_worker.job_filter_owner is None
    finally:
        citadel._lifecycle_graph_projection_task.cancel()
        with suppress(asyncio.CancelledError):
            await citadel._lifecycle_graph_projection_task


@pytest.mark.asyncio
async def test_in_loop_stage_runner_binds_capture_run_id(monkeypatch: Any) -> None:
    import scripts.run_railway as run_railway
    from kb.service import current_capture_run_id

    seen: list[str | None] = []

    async def source_stage() -> int:
        seen.append(current_capture_run_id())
        return 0

    monkeypatch.setattr(
        run_railway,
        "evolve_stages_async",
        lambda: [("source", True, source_stage)],
    )

    assert (
        await run_railway.run_evolve_in_loop(
            capture_run_id="capture:scheduled", stages=("source",)
        )
        == 0
    )
    assert seen == ["capture:scheduled"]


@pytest.mark.asyncio
async def test_barrier_clears_exact_filter_after_completion() -> None:
    fake = ExactFilterCitadel()

    result = await wait_for_projection_barrier(
        fake, [fake.exact_job_id], timeout_seconds=0.01
    )

    assert result.complete is True
    assert fake.filter_calls[0][0] == (fake.exact_job_id,)
    owner = fake.filter_calls[0][1]
    assert owner
    assert fake.filter_calls[1] == (None, owner)


@pytest.mark.asyncio
async def test_barrier_clears_exact_filter_after_cancellation() -> None:
    fake = ExactFilterCitadel()
    fake.block = True
    barrier_task = asyncio.create_task(
        wait_for_projection_barrier(
            fake, [fake.exact_job_id], timeout_seconds=10.0
        )
    )
    await asyncio.wait_for(fake.started.wait(), timeout=1)
    barrier_task.cancel()

    with suppress(asyncio.CancelledError):
        await barrier_task

    owner = fake.filter_calls[0][1]
    assert owner
    assert fake.filter_calls[1] == (None, owner)
