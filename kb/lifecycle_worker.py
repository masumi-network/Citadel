"""Durable lifecycle projection worker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import logging
from typing import Any, Protocol

from kb.lifecycle import (
    LifecycleStore,
    ProjectionLease,
    ProjectionLeaseError,
    ProjectionOperation,
)


logger = logging.getLogger(__name__)


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

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]: ...

    async def corpus_chunk_counts(
        self,
        document_ids: list[str],
    ) -> dict[str, int] | None: ...

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
    ) -> set[str] | None: ...


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
        lease_seconds: float = 120,
        retry_backoff_seconds: float = 5,
        max_attempts: int = 5,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.gateway = gateway
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_attempts = max_attempts
        self._fault_injector = fault_injector

    def _inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    async def run_once(self, *, now: datetime | None = None) -> bool:
        """Process one due job. Return false when no work is due."""
        operation_time = now or datetime.now(UTC)
        lease = self.store.claim_next_job(
            worker_id=self.worker_id,
            now=operation_time,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return False
        try:
            await self._project(lease, now=operation_time)
        except BaseException as exc:
            try:
                if isinstance(exc, Exception) and lease.attempt >= self.max_attempts:
                    self.store.fail_job(
                        lease,
                        error_code=exc.__class__.__name__,
                        error_message=str(exc),
                        now=operation_time,
                    )
                else:
                    self.store.reschedule_job(
                        lease,
                        error_code=exc.__class__.__name__,
                        error_message=str(exc),
                        now=operation_time,
                        backoff_seconds=self.retry_backoff_seconds,
                    )
            except ProjectionLeaseError:
                logger.warning(
                    "projection job lost its lease before retry could be recorded: %s",
                    lease.projection_job_id,
                )
            raise
        return True

    async def _project(self, lease: ProjectionLease, *, now: datetime) -> None:
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

        await self._project_relational(lease, operation, text, now=now)
        operation = self.store.get_operation(lease.projection_job_id)
        await self._project_vector_and_graph(lease, operation, now=now)

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
        now: datetime,
    ) -> None:
        receipt = self._receipt(operation, "relational")
        source = operation.source_revision
        if receipt.state not in {"completed", "searchable"}:
            existing_ids = await self.gateway.dataset_document_ids([source.dataset])
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
            added = await self.gateway.remember(text, **remember_kwargs)
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
            document_ids = await self.gateway.dataset_document_ids([source.dataset])
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
        now: datetime,
    ) -> None:
        source = operation.source_revision
        receipts = {
            backend: self._receipt(operation, backend)
            for backend in ("vector", "graph")
        }
        if any(
            receipt.state not in {"completed", "searchable"}
            for receipt in receipts.values()
        ):
            await self._reconcile_existing_vector_and_graph(
                lease,
                operation,
                now=now,
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
            cognify_result = await self.gateway.cognify(
                datasets=[source.dataset],
                force=False,
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
            chunk_counts = await self.gateway.corpus_chunk_counts(
                [source.source_revision_id]
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
            graph_ids = await self.gateway.corpus_graph_presence(
                [source.source_revision_id]
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
        now: datetime,
    ) -> None:
        source = operation.source_revision
        chunk_counts = await self.gateway.corpus_chunk_counts(
            [source.source_revision_id]
        )
        graph_ids = await self.gateway.corpus_graph_presence(
            [source.source_revision_id]
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
            "graph": 1 if graph_present else None,
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
