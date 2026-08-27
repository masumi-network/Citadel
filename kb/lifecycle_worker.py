"""Durable lifecycle projection worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
import inspect
import logging
import re
from typing import Any, Protocol

from kb.lifecycle import (
    CaptureContext,
    LifecycleStore,
    ProjectionLease,
    ProjectionLeaseError,
    ProjectionOperation,
    ProjectionRequest,
)


logger = logging.getLogger(__name__)


_PROVIDER_ERROR_MARKERS = (
    "openrouter",
    "litellm",
    "free-models-per-day-high-balance",
    "openrouter_free_tier_daily",
    "authenticationerror",
    "permissionerror",
    "model unavailable",
    "model_not_found",
)
_PROVIDER_TERMINAL_STATUS = re.compile(
    r"(?:[\"']?status(?:_code)?[\"']?|[\"']?http(?:_status)?[\"']?|[\"']?code[\"']?)"
    r"\s*[:=]\s*[\"']?(401|403|404)\b"
)
_PROVIDER_QUOTA_MARKERS = (
    "free-models-per-day-high-balance",
    "free-models-per-hour",
    "free-models-per-min",
    "openrouter_free_tier_daily",
    "openrouter free-model daily quota",
)
_PROVIDER_RESET_MARKER = re.compile(
    r"x-ratelimit-reset[\"']?\s*[:=]\s*[\"']?(\d{10,13})",
    re.IGNORECASE,
)
_GRAPH_OUTPUT_RETRY_BACKOFF_SECONDS = 60 * 60


def _is_provider_error(exc: BaseException) -> bool:
    """Return true when an exception came from the configured model provider."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in _PROVIDER_ERROR_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_non_retryable_provider_error(exc: BaseException) -> bool:
    """Return true for provider authentication, permission, or model failures."""
    if not _is_provider_error(exc):
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if _PROVIDER_TERMINAL_STATUS.search(text):
            return True
        if "model unavailable" in text or "model_not_found" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_provider_quota_exhausted(exc: BaseException) -> bool:
    """Return true for a provider quota that should retry after its reset."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in _PROVIDER_QUOTA_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _provider_quota_backoff_seconds(
    exc: BaseException,
    *,
    now: datetime,
) -> float:
    """Wait until the provider reset, with bounded fallback when absent."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        match = _PROVIDER_RESET_MARKER.search(str(current))
        if match:
            raw_reset = int(match.group(1))
            reset_at = datetime.fromtimestamp(
                raw_reset / (1000 if raw_reset > 10_000_000_000 else 1),
                tz=UTC,
            )
            return max(60.0, min(24 * 60 * 60, (reset_at - now).total_seconds()))
        current = current.__cause__ or current.__context__
    return 60 * 60


def _safe_error_message(exc: BaseException) -> str:
    """Keep provider response bodies, keys, and URLs out of lifecycle records."""
    if _is_provider_error(exc):
        return f"{exc.__class__.__name__}: model provider request failed"
    return str(exc)


def _is_missing_local_path_error(exc: BaseException) -> bool:
    """True when Cognee failed because a path-string note is not on disk."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FileNotFoundError):
            return True
        if "Storage directory does not exist" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_malformed_graph_output_error(exc: BaseException) -> bool:
    """Return true for invalid structured output from the graph LLM pass."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if (
            "knowledgegraph" in text
            and ("target_node_id" in text or "source_node_id" in text)
            and ("validation error" in text or "input should be" in text)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class _LeaseHeartbeat:
    def __init__(
        self,
        store: LifecycleStore,
        lease: ProjectionLease,
        *,
        lease_seconds: float,
        started_at: datetime,
    ) -> None:
        self.store = store
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.started_at = started_at
        self.loop = asyncio.get_running_loop()
        self.started_monotonic = self.loop.time()
        self.interval = max(0.01, min(30.0, lease_seconds / 3))

    def _now(self) -> datetime:
        return self.started_at + timedelta(
            seconds=self.loop.time() - self.started_monotonic
        )

    async def wait(self, awaitable: Awaitable[Any]) -> Any:
        try:
            self.store.renew_lease(
                self.lease,
                now=self._now(),
                lease_seconds=self.lease_seconds,
            )
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            else:
                task = asyncio.ensure_future(awaitable)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=self.interval,
                    )
                except TimeoutError:
                    if task.done():
                        return task.result()
                    self.store.renew_lease(
                        self.lease,
                        now=self._now(),
                        lease_seconds=self.lease_seconds,
                    )
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise


class ProjectionGateway(Protocol):
    async def remember(
        self,
        data: Any,
        *,
        dataset_name: str,
        data_id: str,
        defer_cognify: bool,
        tags: tuple[str, ...] = (),
        session_id: str | None = None,
        attestation: Mapping[str, str] | None = None,
    ) -> Any: ...

    async def cognify(self, *, datasets: list[str], force: bool = False) -> Any: ...

    async def vector_project(
        self,
        *,
        datasets: list[str],
        force: bool = False,
        document_ids: list[str] | None = None,
    ) -> Any: ...

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]: ...

    async def corpus_chunk_counts(
        self,
        document_ids: list[str],
    ) -> dict[str, int] | None: ...

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str] | None: ...


class _BatchVectorGateway:
    """Reuse one vector projection result for a bounded source batch."""

    _UNSET = object()

    def __init__(self, gateway: ProjectionGateway, document_ids: list[str]) -> None:
        self._gateway = gateway
        self._document_ids = document_ids
        self._result: Any = self._UNSET
        self.error: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gateway, name)

    async def vector_project(
        self,
        *,
        datasets: list[str],
        force: bool = False,
        document_ids: list[str] | None = None,
    ) -> Any:
        del document_ids
        if self._result is not self._UNSET:
            return self._result
        if self.error is not None:
            raise self.error
        try:
            self._result = await self._gateway.vector_project(
                datasets=datasets,
                force=force,
                document_ids=self._document_ids,
            )
        except BaseException as exc:
            self.error = exc
            raise
        return self._result


class ProjectionVerificationError(RuntimeError):
    """Raised when a provider write lacks its bounded read receipt."""


class LifecycleProjectionWorker:
    """Projects one retained source at a time from the SQLite lifecycle queue."""

    def __init__(
        self,
        store: LifecycleStore,
        gateway: ProjectionGateway,
        *,
        worker_id: str,
        generation_id: str | None = None,
        projection_version: str | None = None,
        config_digest: str | None = None,
        lease_seconds: float = 120,
        retry_backoff_seconds: float = 5,
        max_attempts: int = 5,
        fault_injector: Callable[[str], None] | None = None,
        include_graph: bool = True,
        include_deferred: bool = False,
        deferred_only: bool = False,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.gateway = gateway
        self.worker_id = worker_id
        self.generation_id = generation_id
        self.projection_version = projection_version
        self.config_digest = config_digest
        self.lease_seconds = lease_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_attempts = max_attempts
        self._fault_injector = fault_injector
        self.include_graph = include_graph
        self.include_deferred = include_deferred
        self.deferred_only = deferred_only

    def _inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _record_projection_failure(
        self,
        lease: ProjectionLease,
        exc: BaseException,
        *,
        now: datetime,
    ) -> bool:
        """Record a failure and return whether the caller should re-raise it."""
        if _is_missing_local_path_error(exc):
            # Path-string notes (citadel ingest used to store the path, not
            # the file). Cognee then tries to open that local path on the Node
            # and retries forever. Missing paths are not retryable; tombstone
            # the current head so requeue cannot resurrect them.
            self._tombstone_missing_path(lease, exc, now=now)
            return False
        try:
            if self.deferred_only and _is_malformed_graph_output_error(exc):
                self.store.reschedule_job(
                    lease,
                    error_code="graph_output_invalid",
                    error_message=_safe_error_message(exc),
                    now=now,
                    backoff_seconds=_GRAPH_OUTPUT_RETRY_BACKOFF_SECONDS,
                    deferred=True,
                )
                logger.warning(
                    "graph enrichment deferred after invalid structured output: "
                    "retry_seconds=%d",
                    _GRAPH_OUTPUT_RETRY_BACKOFF_SECONDS,
                )
                return False
            if _is_provider_quota_exhausted(exc):
                backoff_seconds = _provider_quota_backoff_seconds(
                    exc,
                    now=now,
                )
                self.store.reschedule_job(
                    lease,
                    error_code="provider_quota_exhausted",
                    error_message=_safe_error_message(exc),
                    now=now,
                    backoff_seconds=backoff_seconds,
                    deferred=self.deferred_only,
                )
                logger.warning(
                    "projection job delayed for provider quota reset: "
                    "backoff_seconds=%.0f",
                    backoff_seconds,
                )
                return False
            if _is_non_retryable_provider_error(exc):
                self.store.fail_job(
                    lease,
                    error_code="provider_non_retryable",
                    error_message=_safe_error_message(exc),
                    now=now,
                )
                logger.error(
                    "projection job failed permanently: provider error type=%s",
                    exc.__class__.__name__,
                )
                return False
            if isinstance(exc, Exception) and lease.attempt >= self.max_attempts:
                self.store.fail_job(
                    lease,
                    error_code=exc.__class__.__name__,
                    error_message=_safe_error_message(exc),
                    now=now,
                )
            else:
                self.store.reschedule_job(
                    lease,
                    error_code=exc.__class__.__name__,
                    error_message=_safe_error_message(exc),
                    now=now,
                    backoff_seconds=self.retry_backoff_seconds,
                    deferred=self.deferred_only,
                )
        except ProjectionLeaseError:
            logger.warning(
                "projection job lost its lease before retry could be recorded: %s",
                lease.projection_job_id,
            )
        return True

    def _release_cancelled_batch_leases(
        self,
        leases: list[ProjectionLease],
        *,
        now: datetime,
    ) -> None:
        for lease in leases:
            try:
                self.store.reschedule_job(
                    lease,
                    error_code="batch_cancelled",
                    error_message="projection batch cancelled",
                    now=now,
                    backoff_seconds=self.retry_backoff_seconds,
                    deferred=self.deferred_only,
                )
            except ProjectionLeaseError:
                logger.warning(
                    "cancelled batch lease was already settled: job=%s",
                    lease.projection_job_id,
                )

    async def run_once(self, *, now: datetime | None = None) -> bool:
        """Process one due job. Return false when no work is due."""
        operation_time = now or datetime.now(UTC)
        lease = self.store.claim_next_job(
            worker_id=self.worker_id,
            generation_id=self.generation_id,
            projection_version=self.projection_version,
            config_digest=self.config_digest,
            now=operation_time,
            lease_seconds=self.lease_seconds,
            include_deferred=self.include_deferred,
            deferred_only=self.deferred_only,
        )
        if lease is None:
            return False
        heartbeat = _LeaseHeartbeat(
            self.store,
            lease,
            lease_seconds=self.lease_seconds,
            started_at=operation_time,
        )
        try:
            await self._project(lease, heartbeat=heartbeat, now=operation_time)
        except BaseException as exc:
            if self._record_projection_failure(lease, exc, now=operation_time):
                raise
        return True

    async def run_batch(
        self,
        *,
        max_jobs: int = 20,
        now: datetime | None = None,
    ) -> bool:
        """Process a bounded same-dataset vector batch with per-source receipts."""
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        if max_jobs == 1 or self.include_graph:
            return await self.run_once(now=now)

        operation_time = now or datetime.now(UTC)
        first_lease = self.store.claim_next_job(
            worker_id=self.worker_id,
            generation_id=self.generation_id,
            projection_version=self.projection_version,
            config_digest=self.config_digest,
            now=operation_time,
            lease_seconds=self.lease_seconds,
            include_deferred=self.include_deferred,
            deferred_only=self.deferred_only,
        )
        if first_lease is None:
            return False

        first_source = self.store.get_operation(
            first_lease.projection_job_id
        ).source_revision
        leases = [first_lease]
        for _ in range(max_jobs - 1):
            lease = self.store.claim_next_job(
                worker_id=self.worker_id,
                generation_id=self.generation_id,
                projection_version=self.projection_version,
                config_digest=self.config_digest,
                dataset=first_source.dataset,
                now=operation_time,
                lease_seconds=self.lease_seconds,
                include_deferred=self.include_deferred,
                deferred_only=self.deferred_only,
            )
            if lease is None:
                break
            leases.append(lease)

        active: list[ProjectionLease] = []
        settled: set[str] = set()
        for lease in leases:
            heartbeat = _LeaseHeartbeat(
                self.store,
                lease,
                lease_seconds=self.lease_seconds,
                started_at=operation_time,
            )
            try:
                operation = self.store.get_operation(lease.projection_job_id)
                source = operation.source_revision
                if source.tombstone:
                    self._project_tombstone(lease, operation, now=operation_time)
                    settled.add(lease.projection_job_id)
                    continue
                content = self.store.read_retained_content(source.source_revision_id)
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProjectionVerificationError(
                        "lifecycle v1 cannot project non-UTF-8 media type "
                        f"{source.media_type!r}"
                    ) from exc
                await self._project_relational(
                    lease,
                    operation,
                    text,
                    heartbeat=heartbeat,
                    now=operation_time,
                )
                active.append(lease)
            except asyncio.CancelledError:
                self._release_cancelled_batch_leases(
                    [
                        candidate
                        for candidate in leases
                        if candidate.projection_job_id not in settled
                    ],
                    now=operation_time,
                )
                raise
            except BaseException as exc:
                self._record_projection_failure(lease, exc, now=operation_time)
                settled.add(lease.projection_job_id)
                logger.warning(
                    "batched lifecycle source was rescheduled: job=%s error=%s detail=%s",
                    lease.projection_job_id,
                    exc.__class__.__name__,
                    _safe_error_message(exc),
                )

        if not active:
            return True

        document_ids = [
            self.store.get_operation(lease.projection_job_id)
            .source_revision.source_revision_id
            for lease in active
        ]
        batch_gateway = _BatchVectorGateway(self.gateway, document_ids)
        batch_worker = LifecycleProjectionWorker(
            self.store,
            batch_gateway,
            worker_id=self.worker_id,
            generation_id=self.generation_id,
            projection_version=self.projection_version,
            config_digest=self.config_digest,
            lease_seconds=self.lease_seconds,
            retry_backoff_seconds=self.retry_backoff_seconds,
            max_attempts=self.max_attempts,
            fault_injector=self._fault_injector,
            include_graph=False,
            include_deferred=self.include_deferred,
            deferred_only=self.deferred_only,
        )

        async def renew_batch_leases() -> None:
            while True:
                await asyncio.sleep(max(0.5, self.lease_seconds / 3))
                for lease in active:
                    try:
                        self.store.renew_lease(
                            lease,
                            now=datetime.now(UTC),
                            lease_seconds=self.lease_seconds,
                        )
                    except ProjectionLeaseError:
                        logger.warning(
                            "batched lifecycle source lost its lease: job=%s",
                            lease.projection_job_id,
                        )

        renewal_task = asyncio.create_task(renew_batch_leases())
        try:
            for index, lease in enumerate(active):
                heartbeat = _LeaseHeartbeat(
                    self.store,
                    lease,
                    lease_seconds=self.lease_seconds,
                    started_at=operation_time,
                )
                try:
                    await batch_worker._project(
                        lease,
                        heartbeat=heartbeat,
                        now=operation_time,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        self._release_cancelled_batch_leases(
                            active[index:],
                            now=operation_time,
                        )
                        raise
                    provider_error = batch_gateway.error
                    if provider_error is not None:
                        for remaining in active[index:]:
                            self._record_projection_failure(
                                remaining,
                                provider_error,
                                now=operation_time,
                            )
                        break
                    self._record_projection_failure(lease, exc, now=operation_time)
                    logger.warning(
                        "batched lifecycle source was rescheduled: job=%s error=%s detail=%s",
                        lease.projection_job_id,
                        exc.__class__.__name__,
                        _safe_error_message(exc),
                    )
        finally:
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)
        return True

    def matches_projection(
        self,
        *,
        generation_id: str,
        projection_version: str,
        config_digest: str,
    ) -> bool:
        return (
            self.generation_id == generation_id
            and self.projection_version == projection_version
            and self.config_digest == config_digest
        )

    def _tombstone_missing_path(
        self,
        lease: ProjectionLease,
        exc: BaseException,
        *,
        now: datetime,
    ) -> None:
        """Fail the poison job, then replace the current head with a tombstone."""
        self.store.fail_job(
            lease,
            error_code="FileNotFoundError",
            error_message=str(exc),
            now=now,
        )
        operation = self.store.get_operation(lease.projection_job_id)
        source = operation.source_revision
        if source.tombstone:
            return
        self.store.accept_tombstone(
            reason=f"FileNotFoundError: {str(exc)[:200]}",
            capture=CaptureContext(
                dataset=source.dataset,
                source_key=source.source_key,
                source_locator=source.source_locator,
                media_type=source.media_type,
                capture_actor_id=source.capture_actor_id,
                capture_run_id=source.capture_run_id,
                captured_at=now,
                metadata=dict(source.capture_metadata),
            ),
            projection=self._projection_request(operation),
            now=now,
        )
        logger.warning(
            "tombstoned source %s after non-retryable FileNotFoundError: %s",
            source.source_key,
            exc,
        )

    @staticmethod
    def _projection_request(operation: ProjectionOperation) -> ProjectionRequest:
        job = operation.job
        return ProjectionRequest(
            generation_id=job.generation_id,
            projection_version=job.projection_version,
            config_digest=job.config_digest,
            providers={
                receipt.backend: receipt.provider
                for receipt in operation.receipts
                if receipt.backend and receipt.provider
            },
        )

    async def _project(
        self,
        lease: ProjectionLease,
        *,
        heartbeat: _LeaseHeartbeat,
        now: datetime,
    ) -> None:
        operation = self.store.get_operation(lease.projection_job_id)
        source = operation.source_revision
        if source.tombstone:
            self._project_tombstone(lease, operation, now=now)
            return
        content = self.store.read_retained_content(source.source_revision_id)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectionVerificationError(
                f"lifecycle v1 cannot project non-UTF-8 media type {source.media_type!r}"
            ) from exc

        await self._project_relational(
            lease,
            operation,
            text,
            heartbeat=heartbeat,
            now=now,
        )
        operation = self.store.get_operation(lease.projection_job_id)
        await self._project_vector_and_graph(
            lease,
            operation,
            heartbeat=heartbeat,
            now=now,
        )

    def _project_tombstone(
        self,
        lease: ProjectionLease,
        operation: ProjectionOperation,
        *,
        now: datetime,
    ) -> None:
        """Activate current-head exclusion receipts without provider content writes."""
        source = operation.source_revision
        for backend in ("relational", "vector", "graph"):
            receipt = self._receipt(
                self.store.get_operation(lease.projection_job_id),
                backend,
            )
            if receipt.state not in {"completed", "searchable"}:
                self.store.begin_backend(lease, backend, now=now)
                self.store.complete_backend(
                    lease,
                    backend,
                    affected_count=0,
                    metadata={
                        "tombstone": True,
                        "exclusion": "current_source_head",
                        "source_revision_id": source.source_revision_id,
                    },
                    now=now,
                )
            if receipt.state != "searchable":
                self.store.mark_backend_searchable(lease, backend, now=now)

    async def _project_relational(
        self,
        lease: ProjectionLease,
        operation: ProjectionOperation,
        text: str,
        *,
        heartbeat: _LeaseHeartbeat,
        now: datetime,
    ) -> None:
        receipt = self._receipt(operation, "relational")
        source = operation.source_revision
        if receipt.state not in {"completed", "searchable"}:
            existing_ids = await heartbeat.wait(
                self.gateway.dataset_document_ids([source.dataset])
            )
            if source.source_revision_id in {str(item) for item in existing_ids}:
                self.store.begin_backend(lease, "relational", now=now)
                self.store.complete_backend(
                    lease,
                    "relational",
                    affected_ids=(source.source_revision_id,),
                    affected_count=1,
                    metadata={
                        "data_id": source.source_revision_id,
                        "reconciled": True,
                    },
                    now=now,
                )
                self.store.mark_backend_searchable(lease, "relational", now=now)
                return
            self.store.begin_backend(lease, "relational", now=now)
            remember_kwargs: dict[str, Any] = {
                "dataset_name": source.dataset,
                "data_id": source.source_revision_id,
                "defer_cognify": True,
            }
            tags = source.capture_metadata.get("tags")
            if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
                remember_kwargs["tags"] = tuple(tags)
            session_id = source.capture_metadata.get("session_id")
            if isinstance(session_id, str) and session_id:
                remember_kwargs["session_id"] = session_id
            attestation = source.capture_metadata.get("attestation")
            if isinstance(attestation, Mapping) and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in attestation.items()
            ):
                remember_kwargs["attestation"] = dict(attestation)
            added = await heartbeat.wait(self.gateway.remember(text, **remember_kwargs))
            self._inject_fault("after_backend_write:relational")
            self.store.complete_backend(
                lease,
                "relational",
                provider_operation_id=self._provider_operation_id(added),
                affected_ids=(source.source_revision_id,),
                affected_count=1,
                metadata={"data_id": source.source_revision_id},
                now=now,
            )
            self._inject_fault("after_receipt_write:relational")
        if receipt.state != "searchable":
            document_ids = await heartbeat.wait(
                self.gateway.dataset_document_ids([source.dataset])
            )
            if source.source_revision_id not in {str(item) for item in document_ids}:
                raise ProjectionVerificationError(
                    "relational source read did not return the accepted source revision"
                )
            self._inject_fault("after_searchability_check:relational")
            self.store.mark_backend_searchable(lease, "relational", now=now)

    async def _project_vector_and_graph(
        self,
        lease: ProjectionLease,
        operation: ProjectionOperation,
        *,
        heartbeat: _LeaseHeartbeat,
        now: datetime,
    ) -> None:
        if not callable(getattr(self.gateway, "vector_project", None)):
            if not self.include_graph:
                raise ProjectionVerificationError(
                    "vector-only projection requires the gateway vector_project method"
                )
            await self._project_vector_and_graph_legacy(
                lease,
                operation,
                heartbeat=heartbeat,
                now=now,
            )
            return

        source = operation.source_revision
        requires_reprojection = self.store.has_completed_projection_other_than(
            source_revision_id=source.source_revision_id,
            projection_job_id=operation.job.projection_job_id,
        )
        await self._reconcile_existing_vector_and_graph(
            lease,
            operation,
            heartbeat=heartbeat,
            now=now,
            reconcile_graph=self.include_graph and not requires_reprojection,
        )
        operation = self.store.get_operation(lease.projection_job_id)
        vector = self._receipt(operation, "vector")
        if vector.state not in {"completed", "searchable"}:
            self.store.begin_backend(lease, "vector", now=now)
            vector_result = await heartbeat.wait(
                self.gateway.vector_project(
                    datasets=[source.dataset],
                    force=requires_reprojection,
                    document_ids=[source.source_revision_id],
                )
            )
            self._inject_fault("after_backend_write:vector")
            self.store.complete_backend(
                lease,
                "vector",
                provider_operation_id=self._provider_operation_id(vector_result),
                affected_ids=(source.source_revision_id,),
                affected_count=1,
                metadata={"data_id": source.source_revision_id},
                now=now,
            )
            self._inject_fault("after_receipt_write:vector")

        operation = self.store.get_operation(lease.projection_job_id)
        vector = self._receipt(operation, "vector")
        if vector.state != "searchable":
            chunk_counts = await heartbeat.wait(
                self.gateway.corpus_chunk_counts([source.source_revision_id])
            )
            chunk_count = None if chunk_counts is None else chunk_counts.get(
                source.source_revision_id
            )
            if chunk_count is None or chunk_count < 1:
                raise ProjectionVerificationError(
                    "vector read check returned no chunks for the accepted source revision"
                )
            self._inject_fault("after_searchability_check:vector")
            self.store.complete_backend(
                lease,
                "vector",
                provider_operation_id=vector.provider_operation_id,
                affected_ids=vector.affected_ids or (source.source_revision_id,),
                affected_count=chunk_count,
                model=vector.model,
                dimensions=vector.dimensions,
                metadata=vector.metadata,
                now=now,
            )
            self.store.mark_backend_searchable(lease, "vector", now=now)

        if not self.include_graph:
            self.store.defer_graph_enrichment(lease, now=now)
            return

        operation = self.store.get_operation(lease.projection_job_id)
        graph = self._receipt(operation, "graph")
        if graph.state not in {"completed", "searchable"}:
            self.store.begin_backend(lease, "graph", now=now)
            graph_result = await heartbeat.wait(
                self.gateway.cognify(
                    datasets=[source.dataset],
                    force=requires_reprojection,
                )
            )
            self._inject_fault("after_backend_write:graph")
            self.store.complete_backend(
                lease,
                "graph",
                provider_operation_id=self._provider_operation_id(graph_result),
                affected_ids=(source.source_revision_id,),
                affected_count=1,
                metadata={"data_id": source.source_revision_id},
                now=now,
            )
            self._inject_fault("after_receipt_write:graph")

        operation = self.store.get_operation(lease.projection_job_id)
        graph = self._receipt(operation, "graph")
        if graph.state != "searchable":
            graph_ids = await heartbeat.wait(
                self.gateway.corpus_graph_presence(
                    [source.source_revision_id],
                    datasets=[source.dataset],
                )
            )
            if graph_ids is None or source.source_revision_id not in {
                str(item) for item in graph_ids
            }:
                raise ProjectionVerificationError(
                    "graph read check did not return the accepted source revision"
                )
            self._inject_fault("after_searchability_check:graph")
            self.store.mark_backend_searchable(lease, "graph", now=now)

    async def _project_vector_and_graph_legacy(
        self,
        lease: ProjectionLease,
        operation: ProjectionOperation,
        *,
        heartbeat: _LeaseHeartbeat,
        now: datetime,
    ) -> None:
        source = operation.source_revision
        receipts = {
            backend: self._receipt(operation, backend)
            for backend in ("vector", "graph")
        }
        requires_reprojection = self.store.has_completed_projection_other_than(
            source_revision_id=source.source_revision_id,
            projection_job_id=operation.job.projection_job_id,
        )
        if any(
            receipt.state not in {"completed", "searchable"}
            for receipt in receipts.values()
        ):
            await self._reconcile_existing_vector_and_graph(
                lease,
                operation,
                heartbeat=heartbeat,
                now=now,
                reconcile_graph=self.include_graph and not requires_reprojection,
            )
            operation = self.store.get_operation(lease.projection_job_id)
            receipts = {
                backend: self._receipt(operation, backend)
                for backend in ("vector", "graph")
            }
        pending = [
            backend
            for backend, receipt in receipts.items()
            if receipt.state not in {"completed", "searchable"}
        ]
        if pending:
            for backend in pending:
                self.store.begin_backend(lease, backend, now=now)
            cognify_result = await heartbeat.wait(
                self.gateway.cognify(
                    datasets=[source.dataset],
                    force=requires_reprojection,
                )
            )
            provider_operation_id = self._provider_operation_id(cognify_result)
            for backend in pending:
                self._inject_fault(f"after_backend_write:{backend}")
            for backend in pending:
                self.store.complete_backend(
                    lease,
                    backend,
                    provider_operation_id=provider_operation_id,
                    affected_ids=(source.source_revision_id,),
                    affected_count=1,
                    metadata={"data_id": source.source_revision_id},
                    now=now,
                )
                self._inject_fault(f"after_receipt_write:{backend}")

        operation = self.store.get_operation(lease.projection_job_id)
        vector = self._receipt(operation, "vector")
        if vector.state != "searchable":
            chunk_counts = await heartbeat.wait(
                self.gateway.corpus_chunk_counts([source.source_revision_id])
            )
            chunk_count = None if chunk_counts is None else chunk_counts.get(
                source.source_revision_id
            )
            if chunk_count is None or chunk_count < 1:
                raise ProjectionVerificationError(
                    "vector read check returned no chunks for the accepted source revision"
                )
            self._inject_fault("after_searchability_check:vector")
            self.store.complete_backend(
                lease,
                "vector",
                provider_operation_id=vector.provider_operation_id,
                affected_ids=vector.affected_ids or (source.source_revision_id,),
                affected_count=chunk_count,
                model=vector.model,
                dimensions=vector.dimensions,
                metadata=vector.metadata,
                now=now,
            )
            self.store.mark_backend_searchable(lease, "vector", now=now)

        operation = self.store.get_operation(lease.projection_job_id)
        graph = self._receipt(operation, "graph")
        if graph.state != "searchable":
            graph_ids = await heartbeat.wait(
                self.gateway.corpus_graph_presence(
                    [source.source_revision_id],
                    datasets=[source.dataset],
                )
            )
            if graph_ids is None or source.source_revision_id not in {
                str(item) for item in graph_ids
            }:
                raise ProjectionVerificationError(
                    "graph read check did not return the accepted source revision"
                )
            self._inject_fault("after_searchability_check:graph")
            self.store.mark_backend_searchable(lease, "graph", now=now)

    async def _reconcile_existing_vector_and_graph(
        self,
        lease: ProjectionLease,
        operation: ProjectionOperation,
        *,
        heartbeat: _LeaseHeartbeat,
        now: datetime,
        reconcile_graph: bool,
    ) -> None:
        source = operation.source_revision
        chunk_counts = await heartbeat.wait(
            self.gateway.corpus_chunk_counts([source.source_revision_id])
        )
        graph_ids = await heartbeat.wait(
            self.gateway.corpus_graph_presence(
                [source.source_revision_id],
                datasets=[source.dataset],
            )
        )
        vector_count = (
            None
            if chunk_counts is None
            else chunk_counts.get(source.source_revision_id)
        )
        graph_present = graph_ids is not None and source.source_revision_id in {
            str(item) for item in graph_ids
        }
        existing = {
            "vector": vector_count if vector_count is not None and vector_count > 0 else None,
            "graph": 1 if graph_present and reconcile_graph else None,
        }
        for backend, affected_count in existing.items():
            receipt = self._receipt(
                self.store.get_operation(lease.projection_job_id),
                backend,
            )
            if affected_count is None or receipt.state in {"completed", "searchable"}:
                continue
            self.store.begin_backend(lease, backend, now=now)
            self.store.complete_backend(
                lease,
                backend,
                affected_ids=(source.source_revision_id,),
                affected_count=affected_count,
                metadata={
                    "data_id": source.source_revision_id,
                    "reconciled": True,
                },
                now=now,
            )
            self.store.mark_backend_searchable(lease, backend, now=now)

    @staticmethod
    def _receipt(operation: ProjectionOperation, backend: str) -> Any:
        for receipt in operation.receipts:
            if receipt.backend == backend:
                return receipt
        raise ProjectionVerificationError(
            f"projection job is missing required backend receipt {backend!r}"
        )

    @staticmethod
    def _provider_operation_id(result: Any) -> str | None:
        if isinstance(result, dict):
            for field in ("operation_id", "pipeline_run_id", "id"):
                value = result.get(field)
                if value is not None:
                    return str(value)
        for field in ("operation_id", "pipeline_run_id", "id"):
            value = getattr(result, field, None)
            if value is not None:
                return str(value)
        return None
