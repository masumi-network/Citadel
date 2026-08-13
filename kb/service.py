from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
import json
import logging
import os
import re
from uuid import uuid4
from typing import Any

from kb import chunk_window
from kb.cognee_client import (
    CogneeGateway,
    CogneePublicClient,
    _suppress_inline_cognify,
)
from kb.config import CitadelConfig
from kb.filters import PreIngestFilter
from kb.logging_utils import safe_log_value
from kb.lifecycle import (
    CaptureContext,
    LIFECYCLE_CHUNK_SOURCE_PREFIX,
    LifecycleNotFoundError,
    LifecycleStore,
    ProjectionRequest,
    lifecycle_chunk_source_key,
)
from kb.lifecycle_worker import LifecycleProjectionWorker
from kb.models import FeedbackRequest, FeedbackResult, IngestResult
from kb.repair_journal import RepairJournal, RepairJournalLeaseError
from kb.security_scan import (
    SecretContentError,
    SecurityScanEntry,
    scan_text_entries,
)
from kb.source_search import search_github_sync_state
from kb.tags import merge_tags

logger = logging.getLogger(__name__)

# Upper bound for search breadth. The HTTP /search route (SearchBody) already rejects
# top_k outside [1, 100] and the MCP layer clamps to 25, but this is the single
# chokepoint every read path funnels through (search, the /api/knowledge alias,
# promotion/learning-agent lookups, the cognify marker probe). Clamping here floors
# negatives/zero to 1 and caps absurd values so no caller — present, future, or one that
# bypasses pydantic — can drive an unbounded recall into the search-backend timeout.
MAX_SEARCH_TOP_K = 100

# The cognify verify canary re-searches for its marker, because cognify can be
# settling when the first search runs. Module-level so a test can shrink them
# instead of sleeping through the real backoff. Non-lifecycle deployments only:
# under lifecycle v1 the canary waits on the marker's projection operation
# instead (see ``_canary_timeout_seconds``).
CANARY_SEARCH_ATTEMPTS = 3
CANARY_SEARCH_BACKOFF_SECONDS = 2.0

# Ceiling for the lifecycle canary's wait on its marker's projection operation.
# The drain runs minutes behind the accept under normal load, and a held writer
# lock can park a job far longer, so the default is generous. Env-tunable for
# operators; tests park the drain instead of shrinking this.
DEFAULT_CANARY_TIMEOUT_SECONDS = 600.0


def _canary_timeout_seconds() -> float:
    raw = os.getenv("CITADEL_CANARY_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CANARY_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CANARY_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_CANARY_TIMEOUT_SECONDS


class Citadel:
    def __init__(
        self,
        config: CitadelConfig | None = None,
        *,
        cognee: CogneeGateway | None = None,
    ) -> None:
        self.config = config or CitadelConfig.from_env()
        self.cognee = cognee or CogneePublicClient(queue_path=self.config.cognify_queue_path)
        self.lifecycle_store: LifecycleStore | None = None
        self.lifecycle_worker: LifecycleProjectionWorker | None = None
        self._lifecycle_projection_task: asyncio.Task[Any] | None = None
        if self.config.lifecycle_enabled:
            self.lifecycle_store = LifecycleStore(self.config.lifecycle_store_path)
            lifecycle_projection = self._lifecycle_projection_request()
            self.lifecycle_store.assert_generation_binding(lifecycle_projection)
            self.lifecycle_worker = LifecycleProjectionWorker(
                self.lifecycle_store,
                self.cognee,
                worker_id=f"citadel-{uuid4().hex}",
                generation_id=lifecycle_projection.generation_id,
                projection_version=lifecycle_projection.projection_version,
                config_digest=lifecycle_projection.config_digest,
            )
        self.repair_journal = RepairJournal(self.config.repair_journal_path)
        self.filter = PreIngestFilter(
            min_chars=self.config.min_chars,
            exclude_patterns=self.config.exclude_patterns,
        )
        # Keyed by (dataset, content_hash) so dual-writes (e.g. share_session to
        # seat Node + session-traces) are not rejected as duplicate_in_process.
        self._seen_ingest_keys: set[tuple[str, str]] = set()

    def _default_session_for_dataset(self, dataset: str) -> str:
        if dataset == self.config.github_sync_dataset:
            return self.config.github_sync_session
        return self.config.default_session

    @classmethod
    def from_env(cls) -> "Citadel":
        return cls(CitadelConfig.from_env())

    async def ingest(
        self,
        data: str,
        *,
        dataset: str | None = None,
        tags: Iterable[str] | None = None,
        session_id: str | None = None,
        attestation: Mapping[str, str] | None = None,
        defer_cognify: bool = False,
        source_key: str | None = None,
        source_locator: str | None = None,
        media_type: str = "text/plain",
        capture_actor_id: str | None = None,
        capture_run_id: str | None = None,
        captured_at: datetime | None = None,
        _lifecycle_parent_source_key: str | None = None,
        _lifecycle_chunk_index: int | None = None,
    ) -> IngestResult:
        target_dataset = dataset or self.config.default_dataset
        merged_tags = merge_tags(self.config.default_tags, tags)
        decision = self.filter.check(data)
        if not decision.accepted:
            # Escaped for the same reason as the un-chunkable branch below: the
            # dataset name is whatever the caller sent, and an unescaped newline
            # in it writes a second log line indistinguishable from a real one.
            logger.info(
                "Ingest rejected for dataset %s: %s",
                safe_log_value(target_dataset),
                decision.reason,
            )
            return IngestResult(False, decision.reason, target_dataset, merged_tags)

        self._guard_content(data, target_dataset)

        unchunkable = self._guard_chunkable(data, target_dataset)
        if unchunkable is not None:
            return IngestResult(False, unchunkable, target_dataset, merged_tags)

        content_hash = sha256(data.encode("utf-8")).hexdigest()
        ingest_key = (target_dataset, content_hash)
        if self.lifecycle_store is not None:
            resolved_source_key = (source_key or "").strip() or (
                f"manual:{target_dataset}:{content_hash}"
            )
            capture_metadata: dict[str, Any] = {"tags": list(merged_tags)}
            if resolved_source_key.startswith(LIFECYCLE_CHUNK_SOURCE_PREFIX):
                if (
                    _lifecycle_parent_source_key is None
                    or _lifecycle_chunk_index is None
                    or resolved_source_key
                    != lifecycle_chunk_source_key(
                        _lifecycle_parent_source_key,
                        _lifecycle_chunk_index,
                    )
                ):
                    raise ValueError(
                        "reserved lifecycle chunk source key requires its exact parent binding"
                    )
                capture_metadata["lifecycle_parent_source_key"] = (
                    _lifecycle_parent_source_key
                )
                capture_metadata["lifecycle_chunk_index"] = _lifecycle_chunk_index
            elif (
                _lifecycle_parent_source_key is not None
                or _lifecycle_chunk_index is not None
            ):
                raise ValueError(
                    "lifecycle chunk parent binding requires a reserved chunk source key"
                )
            if session_id:
                capture_metadata["session_id"] = session_id
            if attestation:
                capture_metadata["attestation"] = dict(attestation)
            acceptance = self.lifecycle_store.accept_source(
                data.encode("utf-8"),
                capture=CaptureContext(
                    dataset=target_dataset,
                    source_key=resolved_source_key,
                    source_locator=source_locator,
                    media_type=media_type,
                    capture_actor_id=capture_actor_id or self.config.user_id,
                    capture_run_id=capture_run_id or session_id,
                    captured_at=captured_at or datetime.now(UTC),
                    metadata=capture_metadata,
                ),
                projection=self._lifecycle_projection_request(),
            )
            if not self._inline_projection_suppressed():
                self._start_lifecycle_projection()
            return IngestResult(
                True,
                "queued_not_confirmed",
                target_dataset,
                merged_tags,
                {
                    "source_revision_id": acceptance.source_revision_id,
                    "projection_job_id": acceptance.projection_job_id,
                    "state": acceptance.operation.state,
                },
                source_revision_id=acceptance.source_revision_id,
                projection_job_id=acceptance.projection_job_id,
                projection_state=acceptance.operation.state,
            )
        if ingest_key in self._seen_ingest_keys:
            logger.info(
                "Ingest rejected for dataset %s: duplicate_in_process",
                safe_log_value(target_dataset),
            )
            return IngestResult(False, "duplicate_in_process", target_dataset, merged_tags)
        # Claimed BEFORE the await so two concurrent legacy ingests of identical
        # content cannot both pass the check, and released again if the write
        # fails. Lifecycle mode uses SQLite uniqueness for this boundary.
        self._seen_ingest_keys.add(ingest_key)
        try:
            remember_kwargs: dict[str, Any] = {
                "dataset_name": target_dataset,
                "session_id": session_id,
                "tags": merged_tags,
                "defer_cognify": defer_cognify,
            }
            if attestation is not None:
                remember_kwargs["attestation"] = attestation
            result = await self.cognee.remember(data, **remember_kwargs)
        except BaseException:
            self._seen_ingest_keys.discard(ingest_key)
            raise
        # ``cognee.add`` confirms durable source storage, not chunks, vectors, or
        # graph presence. Keep accepted=True for the write receipt, but make the
        # indexing state explicit until a blocking cognify result exists.
        reason = "accepted"
        if isinstance(result, dict):
            cognify_state = result.get("cognify")
            if result.get("background_cognify") is True or cognify_state in {
                "deferred",
                "suppressed",
                "queued_not_confirmed",
            }:
                reason = "queued_not_confirmed"
            elif cognify_state == "not_scheduled":
                reason = "not_scheduled"
            elif result.get("background_cognify") is False:
                reason = "not_scheduled"
        return IngestResult(True, reason, target_dataset, merged_tags, result)

    def lifecycle_source_keys(
        self,
        *,
        dataset: str,
        source_key: str,
        include_chunks: bool = True,
    ) -> tuple[str, ...]:
        """Return current lifecycle keys for one logical source."""
        if self.lifecycle_store is None:
            return ()
        return tuple(
            revision.source_key
            for revision in self.lifecycle_store.current_revisions_for_source(
                dataset,
                source_key,
                include_chunks=include_chunks,
            )
            if not revision.tombstone
        )

    async def tombstone_source(
        self,
        *,
        dataset: str,
        source_key: str,
        reason: str,
        source_locator: str | None = None,
        capture_actor_id: str | None = None,
        capture_run_id: str | None = None,
        captured_at: datetime | None = None,
        include_chunks: bool = True,
    ) -> tuple[IngestResult, ...]:
        """Tombstone current exact and optional chunk revisions for one source."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        projection = self._lifecycle_projection_request()
        current = self.lifecycle_store.current_revisions_for_source(
            dataset,
            source_key,
            include_chunks=include_chunks,
        )
        results: list[IngestResult] = []
        for revision in current:
            if revision.tombstone:
                continue
            acceptance = self.lifecycle_store.accept_tombstone(
                reason=reason,
                capture=CaptureContext(
                    dataset=dataset,
                    source_key=revision.source_key,
                    source_locator=source_locator or revision.source_locator,
                    media_type=revision.media_type,
                    capture_actor_id=capture_actor_id or self.config.user_id,
                    capture_run_id=capture_run_id,
                    captured_at=captured_at or datetime.now(UTC),
                    metadata={
                        "replaces_source_revision_id": revision.source_revision_id,
                        **{
                            key: revision.capture_metadata[key]
                            for key in (
                                "lifecycle_parent_source_key",
                                "lifecycle_chunk_index",
                            )
                            if key in revision.capture_metadata
                        },
                    },
                ),
                projection=projection,
            )
            results.append(
                IngestResult(
                    True,
                    "queued_not_confirmed",
                    dataset,
                    (),
                    {
                        "source_revision_id": acceptance.source_revision_id,
                        "projection_job_id": acceptance.projection_job_id,
                        "state": acceptance.operation.state,
                    },
                    source_revision_id=acceptance.source_revision_id,
                    projection_job_id=acceptance.projection_job_id,
                    projection_state=acceptance.operation.state,
                )
            )
        if results and not self._inline_projection_suppressed():
            self._start_lifecycle_projection()
        return tuple(results)

    def _lifecycle_projection_request(
        self,
        *,
        generation_id: str | None = None,
        projection_version: str | None = None,
    ) -> ProjectionRequest:
        generation_id = (
            generation_id
            if generation_id is not None
            else os.getenv("CITADEL_GENERATION_ID", "citadel-default")
        ).strip()
        projection_version = (
            projection_version
            if projection_version is not None
            else os.getenv(
                "CITADEL_PROJECTION_VERSION",
                "lifecycle-v1:cognee-1.4.1",
            )
        ).strip()
        providers = {
            "relational": os.getenv("DB_PROVIDER", "sqlite").strip().lower(),
            "vector": os.getenv("VECTOR_DB_PROVIDER", "qdrant").strip().lower(),
            "graph": os.getenv("GRAPH_DATABASE_PROVIDER", "ladybug").strip().lower(),
        }
        digest_fields = {
            "generation_id": generation_id,
            "projection_version": projection_version,
            "providers": providers,
            "llm_provider": os.getenv("LLM_PROVIDER", ""),
            "llm_model": os.getenv("LLM_MODEL", ""),
            "embedding_provider": os.getenv("EMBEDDING_PROVIDER", ""),
            "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
            "embedding_dimensions": os.getenv("EMBEDDING_DIMENSIONS", ""),
            "chunk_budget_tokens": chunk_window.resolve_chunk_budget(),
        }
        config_digest = sha256(
            json.dumps(digest_fields, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return ProjectionRequest(
            generation_id=generation_id or "citadel-default",
            projection_version=projection_version or "lifecycle-v1:cognee-1.4.1",
            config_digest=f"sha256:{config_digest}",
            providers=providers,
        )

    @staticmethod
    def _inline_projection_suppressed() -> bool:
        # Delegate rather than re-read the environment. Evolve Phase 1 moved
        # into the web process (#88) and now marks itself with the
        # _SUPPRESS_INLINE_COGNIFY context variable, not the environment
        # variable, precisely because the env var is process-wide and would
        # silence a teammate's ingest too. This checker only read os.getenv, so
        # it returned False for the whole of Phase 1 and started the drain while
        # Phase 1 held the graph writer lock. On 2026-08-13 that parked a
        # projection job on writer_lock.acquire() for 66 minutes, and the lease
        # heartbeat renewed it throughout, so nothing reclaimed it.
        return _suppress_inline_cognify()

    def _start_lifecycle_projection(self) -> bool:
        if self.lifecycle_worker is None:
            return False
        task = self._lifecycle_projection_task
        if task is not None and not task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error(
                "lifecycle projection not started: no running event loop; "
                "durable work will resume on server startup"
            )
            return False
        self._lifecycle_projection_task = loop.create_task(self._drain_lifecycle())
        return True

    async def _drain_lifecycle(self) -> int:
        if self.lifecycle_worker is None or self.lifecycle_store is None:
            return 0
        processed_count = 0
        while True:
            try:
                processed = await self.lifecycle_worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lifecycle projection failed and was rescheduled")
                processed = False
            if processed:
                processed_count += 1
                continue
            delay = self.lifecycle_store.next_wakeup_delay(
                generation_id=self.lifecycle_worker.generation_id,
                projection_version=self.lifecycle_worker.projection_version,
                config_digest=self.lifecycle_worker.config_digest,
            )
            if delay is None:
                return processed_count
            if delay > 0:
                await asyncio.sleep(delay)

    def resume_lifecycle_queue(self) -> bool:
        """Resume durable projection jobs after process startup."""
        return self._start_lifecycle_projection()

    async def wait_for_lifecycle_idle(self) -> int:
        """Wait until every due lifecycle job finishes or reaches a retry delay."""
        if self.lifecycle_worker is None:
            return 0
        self._start_lifecycle_projection()
        task = self._lifecycle_projection_task
        if task is None:
            return 0
        return await task

    async def wait_for_lifecycle_operation(
        self,
        projection_job_id: str,
        *,
        timeout_seconds: float = 120,
        poll_seconds: float = 0.05,
    ) -> dict[str, Any]:
        """Wait for one accepted source to pass every backend read check."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        self._start_lifecycle_projection()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            operation = self.lifecycle_operation(projection_job_id)
            state = str(operation["state"])
            if state == "searchable":
                return operation
            if state in {"failed", "stale"}:
                raise RuntimeError(
                    f"projection operation {projection_job_id} reached {state}"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"projection operation {projection_job_id} did not become searchable"
                )
            await asyncio.sleep(min(poll_seconds, remaining))

    async def stop_lifecycle_queue(self) -> None:
        task = self._lifecycle_projection_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
        """Return one bounded source-to-provider operation record."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        operation = self.lifecycle_store.get_operation(projection_job_id)
        source = operation.source_revision
        job = operation.job
        return {
            "schema_version": job.schema_version,
            "projection_job_id": job.projection_job_id,
            "source_revision_id": source.source_revision_id,
            "dataset": job.dataset,
            "state": operation.state,
            "source_revision": {
                "schema_version": source.schema_version,
                "source_revision_id": source.source_revision_id,
                "source_key": source.source_key,
                "dataset": source.dataset,
                "byte_length": source.byte_length,
                "media_type": source.media_type,
                "previous_revision_id": source.previous_revision_id,
                "captured_at": source.captured_at,
                "accepted_at": source.accepted_at,
                "tombstone": source.tombstone,
            },
            "job": {
                "schema_version": job.schema_version,
                "projection_job_id": job.projection_job_id,
                "source_revision_id": job.source_revision_id,
                "generation_id": job.generation_id,
                "dataset": job.dataset,
                "projection_version": job.projection_version,
                "state": job.state,
                "attempt": job.attempt,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "last_error_code": job.last_error_code,
            },
            "receipts": [
                {
                    "schema_version": receipt.schema_version,
                    "projection_receipt_id": receipt.projection_receipt_id,
                    "projection_job_id": receipt.projection_job_id,
                    "source_revision_id": receipt.source_revision_id,
                    "generation_id": receipt.generation_id,
                    "dataset": receipt.dataset,
                    "backend": receipt.backend,
                    "provider": receipt.provider,
                    "projection_version": receipt.projection_version,
                    "state": receipt.state,
                    "attempt": receipt.attempt,
                    "affected_count": receipt.affected_count,
                    "model": receipt.model,
                    "dimensions": receipt.dimensions,
                    "created_at": receipt.created_at,
                    "updated_at": receipt.updated_at,
                    "completed_at": receipt.completed_at,
                    "searchable_at": receipt.searchable_at,
                    "error_code": receipt.error_code,
                }
                for receipt in operation.receipts
            ],
        }

    def lifecycle_requeue_failed(self) -> int:
        """Reset all failed projection jobs to pending; returns the count."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        return self.lifecycle_store.requeue_failed_projections()

    def lifecycle_census(self) -> dict[str, Any]:
        """Return exact SQLite lifecycle counts and state buckets."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        projection = self._lifecycle_projection_request()
        payload = asdict(self.lifecycle_store.census())
        payload["current_generation"] = asdict(
            self.lifecycle_store.generation_census(
                generation_id=projection.generation_id,
                projection_version=projection.projection_version,
                config_digest=projection.config_digest,
            )
        )
        return payload

    def lifecycle_current_head_evidence(
        self,
        *,
        dataset: str,
        source_keys: tuple[str, ...] | list[str],
    ) -> dict[str, Any]:
        """Return exact current-head evidence for the active projection identity."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        projection = self._lifecycle_projection_request()
        return asdict(
            self.lifecycle_store.current_head_evidence(
                dataset,
                source_keys,
                generation_id=projection.generation_id,
                projection_version=projection.projection_version,
                config_digest=projection.config_digest,
            )
        )

    def lifecycle_generation_census(
        self,
        *,
        generation_id: str,
        projection_version: str,
    ) -> dict[str, Any]:
        """Return current-head projection counts for one target generation."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        projection = self._lifecycle_projection_request(
            generation_id=generation_id,
            projection_version=projection_version,
        )
        return asdict(
            self.lifecycle_store.generation_census(
                generation_id=generation_id,
                projection_version=projection_version,
                config_digest=projection.config_digest,
            )
        )

    def queue_lifecycle_rebuild(
        self,
        *,
        generation_id: str,
        projection_version: str | None = None,
    ) -> tuple[str, ...]:
        """Queue current source heads into an empty target generation."""
        if self.lifecycle_store is None:
            raise LifecycleNotFoundError("lifecycle v1 is disabled")
        if not generation_id.strip():
            raise ValueError("generation_id must be a non-empty string")
        projection = self._lifecycle_projection_request(
            generation_id=generation_id,
            projection_version=projection_version,
        )
        operations = self.lifecycle_store.queue_generation_rebuild(projection)
        if self.lifecycle_worker is not None and self.lifecycle_worker.matches_projection(
            generation_id=projection.generation_id,
            projection_version=projection.projection_version,
            config_digest=projection.config_digest,
        ):
            self._start_lifecycle_projection()
        return tuple(operation.job.projection_job_id for operation in operations)

    def _guard_content(self, data: str, dataset: str) -> None:
        """Block storing content that carries a blocking-severity secret.

        Single content-policy chokepoint for every write path: ``/ingest``,
        ``/api/contribute``, the Obsidian sync, autosync (which POSTs ``/ingest``),
        and the MCP writer tools (which call the same HTTP API) all funnel through
        ``ingest``. This scans the exact text about to be stored and raises
        :class:`SecretContentError` before it can reach the vault (ADR-0005 step 1).
        Reuses the existing GitHub-sync scanner so detection is not reinvented.
        """
        if not self.config.content_scan_enabled:
            return
        scan = scan_text_entries(
            [SecurityScanEntry(source="ingest", location=dataset, text=data)],
            block_severity=self.config.content_scan_block_severity,
        )
        if scan.get("blocked"):
            raise SecretContentError(
                dataset=dataset,
                highest_severity=scan.get("highest_severity"),  # type: ignore[arg-type]
                block_severity=self.config.content_scan_block_severity,
                findings=scan.get("findings", []),  # type: ignore[arg-type]
            )

    def _guard_chunkable(self, data: str, dataset: str) -> str | None:
        """Refuse content the chunker cannot fit in the budget, and say why (#227).

        cognee breaks words on a single space and on sentence endings only, so one
        line of minified output is one word to it. A word over the budget either
        raises ``ValueError`` out of ``chunk_by_sentence`` or is emitted as an
        over-budget chunk. The pre-storage validator below catches the latter
        before ``cognee.add`` can create durable state.

        The choice here is refuse and record, not split. A splitter that edits
        content to make it fit has already corrupted two of this project's own
        documents inside fenced config blocks, turning ``SEVERITY=high`` into
        ``SEVERITY=hig h`` and removing ``CITADEL_GOOGLE_CHAT_SPACE_NAME`` from
        the index entirely. Content that is stored wrong is worse than content
        that is visibly refused, because only one of the two tells anyone.

        Returns a rejection reason, or None to let the write proceed.
        """
        if not chunk_window.guard_enabled():
            return None
        span = chunk_window.check_chunkable(data)
        if span is None:
            try:
                violation = chunk_window.validate_cognee_chunk_budget(data)
            except chunk_window.ChunkBudgetValidationError as exc:
                logger.error(
                    "Ingest refused for dataset %s: final chunk budget could not "
                    "be verified (%s)",
                    safe_log_value(dataset),
                    safe_log_value(str(exc)),
                )
                return "chunk_budget_unmeasured"
            if violation is None:
                return None
            logger.warning(
                "Ingest refused for dataset %s: final Cognee chunk violates its "
                "budget (%s)",
                safe_log_value(dataset),
                safe_log_value(violation.describe()),
            )
            return "chunk_budget_violation"
        # The dataset name arrives from the caller and is not constrained to a
        # charset anywhere on the way here, so it is escaped before it goes into a
        # line this project later reads back as evidence (CodeQL py/log-injection).
        # The document itself never reaches the log: only its digest and lengths.
        logger.warning(
            "Ingest refused for dataset %s: unchunkable_content. One unbroken word is "
            "%s against a budget of %d (%d characters, %s). cognee cannot "
            "split it, so accepting it would either fail this dataset's next cognify "
            "pass outright or store text the embedder silently truncates.",
            safe_log_value(dataset),
            span.describe_tokens(),
            span.budget,
            span.char_length,
            span.fingerprint,
        )
        return "unchunkable_content"

    async def search(
        self,
        query: str,
        *,
        dataset: str | None = None,
        session_id: str | None = None,
        top_k: int = 10,
    ) -> list[Any]:
        top_k = min(max(int(top_k), 1), MAX_SEARCH_TOP_K)
        target_dataset = dataset or self.config.default_dataset
        provider_top_k = MAX_SEARCH_TOP_K if self.lifecycle_store is not None else top_k
        recall_kwargs: dict[str, Any] = {}
        if self.lifecycle_store is not None:
            projection = self._lifecycle_projection_request()
            recall_kwargs["document_ids"] = list(
                self.lifecycle_store.searchable_source_revision_ids(
                    dataset=target_dataset,
                    generation_id=projection.generation_id,
                    projection_version=projection.projection_version,
                    config_digest=projection.config_digest,
                )
            )
        results = await self.cognee.recall(
            query,
            dataset=target_dataset,
            session_id=session_id or self._default_session_for_dataset(target_dataset),
            top_k=provider_top_k,
            **recall_kwargs,
        )
        results = [
            {key: value for key, value in result.items() if key != "_lifecycle"}
            if isinstance(result, dict)
            else result
            for result in results
        ]
        if self.lifecycle_store is not None:
            results = self._filter_lifecycle_search_results(results)[:top_k]
        if results or target_dataset != self.config.github_sync_dataset:
            return results
        return search_github_sync_state(query, self.config, top_k=top_k)

    def _filter_lifecycle_search_results(self, results: list[Any]) -> list[Any]:
        """Require every lifecycle-mode hit to bind to a current receipt."""
        if self.lifecycle_store is None:
            return results
        projection = self._lifecycle_projection_request()
        filtered: list[Any] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            document_id = result.get("document_id")
            if not isinstance(document_id, str) or not document_id:
                continue
            binding = self.lifecycle_store.retrieval_binding(
                document_id,
                generation_id=projection.generation_id,
                projection_version=projection.projection_version,
                config_digest=projection.config_digest,
            )
            if binding is None:
                continue
            receipt = binding.receipt
            if not binding.current or binding.source_revision.tombstone or receipt is None:
                continue
            filtered.append(
                {
                    **result,
                    "_lifecycle": {
                        "schema_version": receipt.schema_version,
                        "source_revision_id": binding.source_revision.source_revision_id,
                        "projection_receipt_id": receipt.projection_receipt_id,
                        "generation_id": receipt.generation_id,
                        "backend": receipt.backend,
                        "provider": receipt.provider,
                        "projection_version": receipt.projection_version,
                        "state": receipt.state,
                    },
                }
            )
        return filtered

    async def feedback(self, request: FeedbackRequest) -> FeedbackResult:
        session_id = request.session_id or self.config.default_session
        dataset = request.dataset or self.config.default_dataset
        # Try cognee's per-session QA cache first (preserves the QA linkage when a
        # live session match exists). Since #54 durable recall bypasses that cache,
        # add_feedback usually finds no matching qa_id and returns False — which
        # used to surface as a silent recorded:false, exit 0 (#40).
        try:
            session_recorded = await self.cognee.add_feedback(
                session_id=session_id,
                qa_id=request.qa_id,
                score=request.score,
                text=request.text,
            )
        except Exception as exc:  # noqa: BLE001 - cognee session cache is best-effort
            logger.warning("session feedback cache rejected qa_id=%s: %s", request.qa_id, exc)
            session_recorded = False

        recorded = session_recorded
        reason: str | None = None
        if not session_recorded:
            # Fall back to a durable, searchable feedback note so the signal is
            # never silently dropped.
            note = (
                f"Feedback for QA {request.qa_id}: score={request.score} | "
                f"{request.text or ''}"
            )
            durable = await self.ingest(
                note,
                dataset=dataset,
                tags=("feedback", f"qa:{request.qa_id}", f"score:{request.score}"),
            )
            recorded = durable.accepted
            if not recorded:
                reason = (
                    f"feedback not recorded: no matching QA in the session cache and the "
                    f"durable write was rejected ({durable.reason})"
                )

        improved = False
        if recorded and self.config.auto_improve:
            await self.cognee.improve(
                dataset=dataset,
                session_ids=[session_id],
                build_global_context_index=self.config.build_global_context_index,
            )
            improved = True
        return FeedbackResult(recorded=recorded, improved=improved, ok=recorded, reason=reason)

    async def improve(
        self,
        *,
        dataset: str | None = None,
        session_ids: list[str] | None = None,
    ) -> Any:
        target_dataset = dataset or self.config.default_dataset
        # Short-circuit an empty graph: cognee.improve raises a raw
        # EntityNotFoundError ("Empty graph projected") with nothing to improve, so
        # return a clean no-op instead of a traceback (#41).
        counts = await self._graph_counts()
        if counts["nodes"] == 0 and counts["edges"] == 0:
            return {
                "ok": True,
                "skipped": "empty_graph",
                "dataset": target_dataset,
                "reason": "graph is empty; nothing to improve",
            }
        return await self.cognee.improve(
            dataset=target_dataset,
            session_ids=session_ids,
            build_global_context_index=self.config.build_global_context_index,
        )

    def _repair_chunkability_reason(self, data: Any, dataset: str) -> str | None:
        """Return a repair refusal before a projection can be deleted."""
        if not isinstance(data, str) or not data.strip():
            return "invalid_document_content"
        try:
            span = chunk_window.check_chunkable(data)
            if span is not None:
                logger.warning(
                    "Repair refused for dataset %s: unchunkable_content (%s)",
                    safe_log_value(dataset),
                    span.describe_tokens(),
                )
                return "unchunkable_content"
            violation = chunk_window.validate_cognee_chunk_budget(data)
        except Exception:  # noqa: BLE001 - fail closed before deletion
            logger.exception(
                "Repair refused for dataset %s: chunk budget could not be measured",
                safe_log_value(dataset),
            )
            return "chunk_budget_unmeasured"
        if violation is not None:
            logger.warning(
                "Repair refused for dataset %s: chunk_budget_violation (%s)",
                safe_log_value(dataset),
                violation.describe(),
            )
            return "chunk_budget_violation"
        return None

    async def _preflight_repair_candidates(
        self,
        repair_ids: list[str],
        repair_document_datasets: Mapping[str, list[str]],
        *,
        source_required_ids: set[str] | None = None,
    ) -> dict[str, str] | None:
        """Validate source text and required safety capabilities before deletion."""
        required_ids = source_required_ids or set()
        get_document = getattr(self.cognee, "get_document", None)
        if not callable(get_document):
            return (
                {"reason": "repair_candidate_lookup_unavailable"}
                if required_ids
                else None
            )
        for document_id in repair_ids:
            try:
                document = await get_document(document_id)
            except Exception as exc:  # noqa: BLE001 - fail closed
                if document_id not in required_ids:
                    continue
                return {
                    "document_id": document_id,
                    "reason": "repair_candidate_lookup_failed",
                    "error_type": exc.__class__.__name__,
                }
            if not isinstance(document, dict):
                if document_id not in required_ids:
                    continue
                return {
                    "document_id": document_id,
                    "reason": "repair_candidate_unavailable",
                }
            body = document.get("body")
            if (
                document_id not in required_ids
                and (not isinstance(body, str) or not body.strip())
            ):
                continue
            datasets = repair_document_datasets.get(document_id) or []
            for target_dataset in datasets:
                reason = self._repair_chunkability_reason(body, target_dataset)
                if reason is not None:
                    return {
                        "document_id": document_id,
                        "reason": reason,
                    }
        return None

    def _repair_rollback_available(self, requires_rollback: bool) -> bool:
        """Do not delete projections unless the gateway can restore them."""
        return not requires_rollback or callable(
            getattr(self.cognee, "restore_document_chunks", None)
        )

    def _repair_journal_gate(
        self,
        *,
        dataset: str | None,
        force: bool,
    ) -> dict[str, Any] | None:
        """Fence destructive repair after a process dies between journal phases."""
        try:
            pending = self.repair_journal.pending_operations()
        except Exception as exc:  # noqa: BLE001 - fail closed before mutation
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_required": True,
                "before": None,
                "after": None,
            }
        if not pending:
            return None
        return {
            "ok": False,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "repair_interrupted",
            "repair_required": True,
            "pending_operations": pending,
            "before": None,
            "after": None,
        }

    async def _restore_repair_projections(
        self, deleted: dict[str, Any] | None
    ) -> bool:
        restore = getattr(self.cognee, "restore_document_chunks", None)
        if not callable(restore) or deleted is None:
            return False
        try:
            result = await restore(deleted)
        except Exception:  # noqa: BLE001 - preserve failure evidence
            logger.exception("repair projection restore failed")
            return False
        return result is not False

    async def _discard_repair_snapshot(self, deleted: dict[str, Any] | None) -> None:
        """Release an adapter-owned snapshot after a successful repair."""
        discard = getattr(self.cognee, "discard_document_chunk_snapshot", None)
        if not callable(discard) or deleted is None:
            return
        try:
            await discard(deleted)
        except Exception:  # noqa: BLE001 - cleanup must not change repair result
            logger.warning("repair snapshot cleanup failed", exc_info=True)

    async def _capture_repair_source_manifest(
        self, document_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
        """Capture content-free source fingerprints before a destructive repair.

        Older test gateways and non-Cognee adapters may not expose this optional
        capability. The normal repair remains compatible with those adapters, but
        interrupted recovery will refuse to proceed without the manifest.
        """
        capture = getattr(self.cognee, "source_manifest_for_documents", None)
        if not callable(capture):
            return None, None
        try:
            manifest = await capture(document_ids)
        except Exception as exc:  # noqa: BLE001 - fail closed before mutation
            return None, {
                "reason": "repair_source_manifest_unavailable",
                "error_type": exc.__class__.__name__,
            }
        if not isinstance(manifest, Mapping):
            return None, {"reason": "repair_source_manifest_unavailable"}
        normalized: dict[str, dict[str, Any]] = {}
        for document_id, entry in manifest.items():
            if not isinstance(document_id, str) or not document_id:
                return None, {"reason": "repair_source_manifest_invalid"}
            if not isinstance(entry, Mapping) or not entry:
                return None, {"reason": "repair_source_manifest_invalid"}
            normalized[document_id] = dict(entry)
        expected_ids = set(document_ids)
        if set(normalized) != expected_ids:
            return None, {
                "reason": "repair_source_manifest_incomplete",
                "missing_document_ids": sorted(expected_ids - set(normalized)),
            }
        return normalized, None

    async def _verify_repair_source_manifest(
        self,
        document_ids: list[str],
        expected: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Refuse recovery when persisted source fingerprints no longer match."""
        current, failure = await self._capture_repair_source_manifest(document_ids)
        if failure is not None:
            return {"ok": False, **failure}
        if current is None:
            return {"ok": False, "reason": "repair_recovery_manifest_unavailable"}
        for document_id in document_ids:
            expected_entry = expected.get(document_id)
            current_entry = current.get(document_id)
            if not isinstance(expected_entry, Mapping) or not isinstance(
                current_entry, Mapping
            ):
                return {
                    "ok": False,
                    "reason": "repair_source_manifest_incomplete",
                    "document_id": document_id,
                }
            for key, expected_value in expected_entry.items():
                if current_entry.get(key) != expected_value:
                    return {
                        "ok": False,
                        "reason": "repair_source_changed",
                        "document_id": document_id,
                        "field": key,
                    }
        return {"ok": True, "source_manifest": current}

    async def _verify_repair_dataset_membership(
        self,
        document_ids: list[str],
        expected: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        """Refuse recovery when live dataset membership differs from the journal."""
        lookup = getattr(self.cognee, "dataset_membership_for_documents", None)
        if not callable(lookup):
            return {"ok": False, "reason": "repair_dataset_membership_unavailable"}
        try:
            current = await lookup(document_ids)
        except Exception as exc:  # noqa: BLE001 - fail closed before cognify
            return {
                "ok": False,
                "reason": "repair_dataset_membership_unavailable",
                "error_type": exc.__class__.__name__,
            }
        if not isinstance(current, Mapping):
            return {"ok": False, "reason": "repair_dataset_membership_unavailable"}

        def normalize(mapping: Mapping[str, Any]) -> dict[str, list[str]] | None:
            if set(mapping) != set(document_ids):
                return None
            normalized: dict[str, list[str]] = {}
            for document_id in document_ids:
                values = mapping.get(document_id)
                if not isinstance(values, (list, tuple, set)):
                    return None
                if any(not isinstance(value, str) or not value for value in values):
                    return None
                normalized[document_id] = sorted(set(values))
            return normalized

        expected_normalized = normalize(expected)
        current_normalized = normalize(current)
        if expected_normalized is None or current_normalized is None:
            return {"ok": False, "reason": "repair_dataset_membership_unavailable"}
        for document_id in document_ids:
            expected_datasets = set(expected_normalized[document_id])
            current_datasets = set(current_normalized[document_id])
            # A scoped repair journals only its target datasets. Other live
            # memberships must not turn an unchanged target into a false drift.
            if not expected_datasets.issubset(current_datasets):
                return {
                    "ok": False,
                    "reason": "repair_dataset_membership_changed",
                    "document_id": document_id,
                }
        return {"ok": True}

    async def _recover_interrupted_repairs(
        self,
        *,
        dataset: str | None,
        force: bool,
    ) -> dict[str, Any]:
        """Rebuild interrupted operations from unchanged durable source rows.

        This is a source-backed rebuild, not an exact rollback. A journal record
        becomes terminal only after the source fingerprint check, force cognify,
        complete census, and targeted vector/graph checks all pass.
        """
        try:
            pending = self.repair_journal.pending_operations()
        except Exception as exc:  # noqa: BLE001 - fail closed before mutation
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_required": True,
                "before": None,
                "after": None,
            }
        if not pending:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "no_interrupted_repairs",
                "recovered_operations": [],
                "before": None,
                "after": None,
            }

        scoped = [
            record
            for record in pending
            if dataset is None or record.get("dataset") == dataset
        ]
        if len(scoped) != len(pending):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_interrupted",
                "repair_required": True,
                "pending_operations": pending,
                "before": None,
                "after": None,
            }

        recovered: list[str] = []
        for record in scoped:
            operation_id = record.get("operation_id")
            repair_ids = record.get("repair_document_ids")
            repair_datasets = record.get("repair_datasets")
            repair_mapping = record.get("repair_document_datasets")
            source_manifest = record.get("source_manifest")
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or not isinstance(repair_ids, list)
                or not repair_ids
                or any(not isinstance(item, str) or not item for item in repair_ids)
                or not isinstance(repair_datasets, list)
                or not repair_datasets
                or any(not isinstance(item, str) or not item for item in repair_datasets)
                or not isinstance(repair_mapping, Mapping)
                or set(repair_mapping) != set(repair_ids)
                or any(
                    not isinstance(datasets, list)
                    or not datasets
                    or any(not isinstance(item, str) or not item for item in datasets)
                    or any(item not in repair_datasets for item in datasets)
                    for datasets in repair_mapping.values()
                )
                or not isinstance(source_manifest, Mapping)
                or not source_manifest
            ):
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "repair_recovery_context_unavailable",
                    "repair_required": True,
                    "pending_operations": pending,
                    "before": None,
                    "after": None,
                }

            verification = await self._verify_repair_source_manifest(
                repair_ids, source_manifest
            )
            if verification.get("ok") is not True:
                reason = str(
                    verification.get("reason") or "repair_recovery_manifest_unavailable"
                )
                try:
                    self.repair_journal.append(
                        operation_id=operation_id,
                        dataset=record.get("dataset"),
                        phase="recovery",
                        status="failed",
                        repair_document_ids=repair_ids,
                        repair_datasets=repair_datasets,
                        repair_document_datasets=repair_mapping,
                        reason=reason,
                        error_type=verification.get("error_type"),
                        source_manifest=source_manifest,
                        projections_preserved=False,
                    )
                except Exception:  # noqa: BLE001 - preserve the recovery refusal
                    logger.exception("repair journal failed during recovery refusal")
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": reason,
                    "repair_required": True,
                    "pending_operations": self.repair_journal.pending_operations(),
                    "before": None,
                    "after": None,
                }

            membership_verification = await self._verify_repair_dataset_membership(
                repair_ids, repair_mapping
            )
            if membership_verification.get("ok") is not True:
                reason = str(
                    membership_verification.get("reason")
                    or "repair_dataset_membership_unavailable"
                )
                try:
                    self.repair_journal.append(
                        operation_id=operation_id,
                        dataset=record.get("dataset"),
                        phase="recovery",
                        status="failed",
                        repair_document_ids=repair_ids,
                        repair_datasets=repair_datasets,
                        repair_document_datasets=repair_mapping,
                        reason=reason,
                        error_type=membership_verification.get("error_type"),
                        source_manifest=source_manifest,
                        projections_preserved=False,
                    )
                except Exception:  # noqa: BLE001 - preserve the recovery refusal
                    logger.exception("repair journal failed during membership refusal")
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": reason,
                    "error_type": membership_verification.get("error_type"),
                    "document_id": membership_verification.get("document_id"),
                    "repair_required": True,
                    "pending_operations": self.repair_journal.pending_operations(),
                    "before": None,
                    "after": None,
                }

            try:
                self.repair_journal.append(
                    operation_id=operation_id,
                    dataset=record.get("dataset"),
                    phase="recovery",
                    status="started",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    repair_document_datasets=repair_mapping,
                    source_manifest=source_manifest,
                )
                await self.cognee.cognify(datasets=repair_datasets, force=True)
                after = await self.cognee.corpus_reconciliation_census(
                    dataset=record.get("dataset")
                )
                post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
                post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
            except Exception as exc:  # noqa: BLE001 - keep operation pending
                logger.exception("interrupted repair recovery failed")
                try:
                    self.repair_journal.append(
                        operation_id=operation_id,
                        dataset=record.get("dataset"),
                        phase="recovery",
                        status="failed",
                        repair_document_ids=repair_ids,
                        repair_datasets=repair_datasets,
                        repair_document_datasets=repair_mapping,
                        reason="repair_recovery_failed",
                        error_type=exc.__class__.__name__,
                        source_manifest=source_manifest,
                        projections_preserved=False,
                    )
                except Exception:  # noqa: BLE001 - preserve original failure
                    logger.exception("repair journal failed during recovery failure")
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "repair_recovery_failed",
                    "error_type": exc.__class__.__name__,
                    "repair_required": True,
                    "pending_operations": self.repair_journal.pending_operations(),
                    "before": None,
                    "after": None,
                }

            post_counts_valid = isinstance(post_counts, dict) and all(
                isinstance(post_counts.get(document_id), int)
                and not isinstance(post_counts.get(document_id), bool)
                and post_counts.get(document_id, 0) > 0
                for document_id in repair_ids
            )
            post_graph_valid = post_graph_ids is not None and set(repair_ids).issubset(
                {str(document_id) for document_id in post_graph_ids}
            )
            post_census_clean = (
                isinstance(after, dict)
                and after.get("ok") is True
                and after.get("census_complete") is True
                and after.get("cap_exceeded") is False
                and after.get("zero_chunk_count") == 0
                and after.get("oversized_document_count") == 0
                and after.get("oversized_chunk_count") == 0
                and after.get("unassigned_zero_chunk_document_count") == 0
                and after.get("unassigned_oversized_document_count") == 0
                and after.get("orphan_oversized_document_count") == 0
                and after.get("missing_document_id_violation_count") == 0
            )
            recovered_ok = post_census_clean and post_counts_valid and post_graph_valid
            if not recovered_ok:
                try:
                    repair_result = await self._reconcile_corpus(
                        dataset=record.get("dataset"),
                        apply=True,
                        force=True,
                    )
                except Exception as exc:  # noqa: BLE001 - retain the pending fence
                    logger.exception("interrupted repair continuation failed")
                    repair_result = {
                        "ok": False,
                        "reason": "repair_recovery_continuation_failed",
                        "error_type": exc.__class__.__name__,
                    }
                repair_result_ok = repair_result.get("ok") is True
                repair_result_indexed = repair_result.get("post_repair_indexed") is True
                if repair_result_ok and (
                    repair_result_indexed or (post_counts_valid and post_graph_valid)
                ):
                    self.repair_journal.append(
                        operation_id=operation_id,
                        dataset=record.get("dataset"),
                        phase="recovery",
                        status="completed",
                        repair_document_ids=repair_ids,
                        repair_datasets=repair_datasets,
                        repair_document_datasets=repair_mapping,
                        reason="source_backed_rebuild_then_repair",
                        source_manifest=source_manifest,
                        projections_preserved=False,
                        post_repair_census_ok=True,
                        post_repair_indexed=True,
                    )
                    recovered.append(operation_id)
                    continue

                reason = "repair_recovery_postcheck_failed"
                try:
                    self.repair_journal.append(
                        operation_id=operation_id,
                        dataset=record.get("dataset"),
                        phase="recovery",
                        status="failed",
                        repair_document_ids=repair_ids,
                        repair_datasets=repair_datasets,
                        repair_document_datasets=repair_mapping,
                        reason=reason,
                        source_manifest=source_manifest,
                        projections_preserved=False,
                        post_repair_indexed=post_counts_valid and post_graph_valid,
                    )
                except Exception:  # noqa: BLE001 - preserve the recovery refusal
                    logger.exception("repair journal failed during recovery postcheck")
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": reason,
                    "repair_required": True,
                    "repair_operation_id": operation_id,
                    "pending_operations": self.repair_journal.pending_operations(),
                    "before": None,
                    "after": after,
                    "post_repair_chunk_counts": post_counts,
                    "post_repair_graph_documents": sorted(post_graph_ids or set()),
                }

            self.repair_journal.append(
                operation_id=operation_id,
                dataset=record.get("dataset"),
                phase="recovery",
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
                reason="source_backed_rebuild",
                source_manifest=source_manifest,
                projections_preserved=False,
                post_repair_census_ok=True,
                post_repair_indexed=True,
            )
            recovered.append(operation_id)

        return {
            "ok": True,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "recovered_interrupted_repairs",
            "recovered_operations": recovered,
            "before": None,
            "after": None,
        }

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        """Resolve a cognee search-hit id to its document/chunk (#28)."""
        return await self.cognee.get_document(document_id)

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        """Owner node ids for the drill-down visibility rule, body not assembled.

        The cheap read /search's per-hit hint uses; /api/documents keeps the
        full ``get_document`` assembly. ``None`` means the id would not resolve.
        """
        return await self.cognee.resolve_document_owner_ids(document_id)

    async def _graph_counts(self) -> dict[str, int]:
        nodes, edges = await self.cognee.graph_data()
        return {"nodes": len(nodes), "edges": len(edges)}

    async def cognify_dataset(
        self,
        *,
        dataset: str | None = None,
        verify: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Cognify already-added data in ``dataset`` and report graph growth.

        This recovers data that was added but never cognified. ``cognee.cognify``
        only processes uncognified data (incremental by default), so re-running is
        safe and idempotent. ``force=True`` overrides the incremental guard by
        passing ``incremental_loading=False`` — use it when Cognee marks a
        dataset "already processed" but the graph store is empty (e.g. the graph
        DB was reset while Cognee's processed-flag persisted). ``verify=True`` is
        a superset: it runs the same recovery cognify and *also* ingests a unique
        marker, cognifies it, and searches for it — an end-to-end health check
        that ingest + cognify fills the graph. The marker is cognified explicitly
        because the modern Cognee ``remember`` path does not cognify inline.
        """
        target_dataset = dataset or self.config.default_dataset
        before = await self._graph_counts()

        # Recovery: cognify already-added-but-uncognified data for the dataset.
        await self.cognee.cognify(datasets=[target_dataset], force=force)

        verification: dict[str, Any] | None = None
        if verify:
            marker = f"COGNIFY_TEST_MARKER_{uuid4().hex}"
            marker_ingest = await self.ingest(marker, dataset=target_dataset)
            await self.cognee.cognify(datasets=[target_dataset], force=force)
            search_hit = False
            attempts = 0
            failure_reason: str | None = None
            if (
                self.lifecycle_store is not None
                and marker_ingest.projection_job_id is not None
            ):
                # Lifecycle v1: the marker ingest queues an async projection
                # JOB and returns before cognee sees the content, and search
                # excludes revisions without a searchable receipt before it
                # consults any backend — so a seconds-scale re-search loop can
                # never bridge a minutes-scale drain and the canary was a
                # deterministic miss (#114's loop predates lifecycle v1). Wait
                # on the operation record instead (the wait kicks the drain
                # itself), then run ONE confirming search so a green canary
                # still proves end-to-end recall, not receipt bookkeeping.
                try:
                    await self.wait_for_lifecycle_operation(
                        marker_ingest.projection_job_id,
                        timeout_seconds=_canary_timeout_seconds(),
                    )
                except TimeoutError:
                    failure_reason = "projection_timeout"
                except RuntimeError:
                    failure_reason = "projection_failed"
                else:
                    attempts = 1
                    matches = await self.search(
                        marker, dataset=target_dataset, top_k=10
                    )
                    search_hit = _marker_in_results(marker, matches)
                    if not search_hit:
                        failure_reason = "search_miss_after_searchable"
            else:
                # Cognify can still be settling, so re-search a bounded number
                # of times before calling it a miss (#114). Without this,
                # requiring a real search hit would flag a slow-but-healthy
                # node as broken.
                for attempt in range(CANARY_SEARCH_ATTEMPTS):
                    attempts = attempt + 1
                    matches = await self.search(
                        marker, dataset=target_dataset, top_k=10
                    )
                    if _marker_in_results(marker, matches):
                        search_hit = True
                        break
                    if attempts < CANARY_SEARCH_ATTEMPTS:
                        await asyncio.sleep(CANARY_SEARCH_BACKOFF_SECONDS)
            verification = {
                "marker": marker,
                "search_hit": search_hit,
                "search_attempts": attempts,
            }
            if marker_ingest.projection_job_id is not None:
                verification["projection_job_id"] = marker_ingest.projection_job_id
            if failure_reason is not None:
                verification["reason"] = failure_reason
            # Backprop (#15): the canary marker used to persist forever, surfacing in
            # search/linear_search results. Delete its node now so verify leaves no
            # trace. Best-effort — never fail the cognify on a cleanup hiccup.
            await self._delete_marker_node(marker)
            if (
                self.lifecycle_store is not None
                and marker_ingest.projection_job_id is not None
            ):
                await self._tombstone_marker_source(
                    marker_ingest.projection_job_id, marker=marker
                )

        after = await self._graph_counts()
        graph_grew = (
            after["nodes"] > before["nodes"] or after["edges"] > before["edges"]
        )
        if verification is not None:
            verification["graph_grew"] = graph_grew
            # graph_grew is diagnostic detail, NOT a pass condition (#114). Any
            # concurrent ingest grows the graph, so growth is no evidence that
            # THIS marker became retrievable. Production showed the cost:
            # "grew=True canary_ok=True" every hour while two of five ingest
            # stages were dead on the Kuzu lock and contributing nothing.
            verification["ok"] = bool(verification["search_hit"])

        return {
            # Surface the verify canary verdict at the top level so the CLI exit
            # code (and API callers) go red when an end-to-end check fails,
            # instead of always reporting ok=True (false-green).
            "ok": True if verification is None else bool(verification["ok"]),
            "dataset": target_dataset,
            "graph_before": before,
            "graph_after": after,
            "graph_grew": graph_grew,
            "verify": verify,
            "verification": verification,
        }

    async def reconcile_corpus(
        self,
        *,
        dataset: str | None = None,
        apply: bool = False,
        force: bool = False,
        recover: bool = False,
    ) -> dict[str, Any]:
        """Audit and optionally repair zero and over-budget projections together.

        The combined path is the default because a document can satisfy both
        failure predicates. One census decides the union, one repair removes only
        stale oversized projections, one cognify rebuilds the union, and one
        post-census verifies the vector, graph, and chunk-budget invariants.
        ``recover=True`` explicitly permits source-backed recovery of an
        interrupted journal operation before the normal census runs.
        """
        if recover and not apply:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": False,
                "force": force,
                "recover": True,
                "reason": "repair_recovery_requires_apply",
                "repair_required": True,
                "before": None,
                "after": None,
            }
        if recover and not force:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": False,
                "recover": True,
                "reason": "repair_recovery_requires_force",
                "repair_required": True,
                "before": None,
                "after": None,
            }
        if apply:
            maintenance = getattr(self.cognee, "maintenance", None)
            if not callable(maintenance):
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "recover": recover,
                    "reason": "maintenance_unavailable",
                    "before": None,
                    "after": None,
                }
            try:
                with self.repair_journal.lease():
                    if not recover:
                        journal_gate = self._repair_journal_gate(
                            dataset=dataset, force=force
                        )
                        if journal_gate is not None:
                            return journal_gate
                    async with maintenance():
                        recovery: dict[str, Any] | None = None
                        if recover:
                            recovery = await self._recover_interrupted_repairs(
                                dataset=dataset,
                                force=force,
                            )
                            if recovery.get("ok") is not True:
                                return recovery
                        result = await self._reconcile_corpus(
                            dataset=dataset,
                            apply=True,
                            force=force,
                        )
                        if recovery is not None:
                            result["recovery"] = recovery
                        return result
            except RepairJournalLeaseError:
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "recover": recover,
                    "reason": "repair_journal_busy",
                    "repair_required": True,
                    "before": None,
                    "after": None,
                }
        return await self._reconcile_corpus(
            dataset=dataset,
            apply=False,
            force=force,
        )

    async def _reconcile_corpus(
        self,
        *,
        dataset: str | None,
        apply: bool,
        force: bool,
    ) -> dict[str, Any]:
        before = await self.cognee.corpus_reconciliation_census(dataset=dataset)
        if (
            before.get("ok") is not True
            or before.get("census_complete") is not True
            or before.get("cap_exceeded") is not False
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": before.get("reason") or "census_failed",
                "before": before,
                "after": None,
            }

        zero_count = before.get("zero_chunk_count")
        oversized_count = before.get("oversized_document_count")
        oversized_chunk_count = before.get("oversized_chunk_count")
        missing_count = before.get("missing_document_id_violation_count")
        zero_unassigned = before.get("unassigned_zero_chunk_document_count")
        oversized_unassigned = before.get("unassigned_oversized_document_count")
        orphan_count = before.get("orphan_oversized_document_count")
        zero_ids = before.get("zero_chunk_document_ids")
        oversized_ids = before.get("oversized_document_ids")
        zero_repair_ids = before.get("zero_repair_document_ids")
        oversized_repair_ids = before.get("oversized_repair_document_ids")
        repair_ids = before.get("repair_document_ids")
        repair_datasets = before.get("repair_datasets")
        repair_mapping = before.get("repair_document_datasets")

        def valid_count(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        def valid_ids(value: Any) -> bool:
            return (
                isinstance(value, list)
                and all(isinstance(item, str) and item for item in value)
                and len(set(value)) == len(value)
            )

        if (
            not valid_count(zero_count)
            or not valid_count(oversized_count)
            or not valid_count(oversized_chunk_count)
            or not valid_count(missing_count)
            or not valid_count(zero_unassigned)
            or not valid_count(oversized_unassigned)
            or not valid_count(orphan_count)
            or not valid_ids(zero_ids)
            or not valid_ids(oversized_ids)
            or not valid_ids(zero_repair_ids)
            or not valid_ids(oversized_repair_ids)
            or not valid_ids(repair_ids)
            or not isinstance(repair_datasets, list)
            or any(not isinstance(item, str) or not item for item in repair_datasets)
            or len(set(repair_datasets)) != len(repair_datasets)
            or not isinstance(repair_mapping, dict)
            or set(repair_mapping) != set(repair_ids)
            or set(repair_ids) != set(zero_repair_ids) | set(oversized_repair_ids)
            or set(zero_repair_ids) - set(zero_ids)
            or set(oversized_repair_ids) - set(oversized_ids)
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "census_returned_invalid_repair_metadata",
                "before": before,
                "after": None,
            }

        for document_id, datasets in repair_mapping.items():
            if (
                not isinstance(datasets, list)
                or not datasets
                or any(not isinstance(item, str) or not item for item in datasets)
                or any(item not in repair_datasets for item in datasets)
            ):
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": apply,
                    "force": force,
                    "reason": "census_returned_invalid_repair_metadata",
                    "before": before,
                    "after": None,
                }

        has_candidates = bool(
            zero_count
            or oversized_count
            or oversized_chunk_count
            or missing_count
            or zero_unassigned
            or oversized_unassigned
            or orphan_count
        )
        if not has_candidates:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "no_repair_required",
                "before": before,
                "after": before if apply else None,
            }

        if not apply:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": False,
                "force": force,
                "reason": "repair_required",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        if oversized_count or oversized_chunk_count or missing_count:
            if not force:
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": False,
                    "reason": "combined_repair_requires_force",
                    "repair_required": True,
                    "before": before,
                    "after": None,
                }

        if (
            zero_unassigned
            or oversized_unassigned
            or orphan_count
            or missing_count
            or not repair_ids
            or not repair_datasets
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "reconciliation_candidates_not_fully_assigned",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        source_manifest, source_manifest_failure = (
            await self._capture_repair_source_manifest(repair_ids)
        )
        if source_manifest_failure is not None:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": source_manifest_failure["reason"],
                "error_type": source_manifest_failure.get("error_type"),
                "missing_document_ids": source_manifest_failure.get(
                    "missing_document_ids", []
                ),
                "repair_required": True,
                "before": before,
                "after": None,
            }

        repair_operation_id = uuid4().hex
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase="started",
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
                source_manifest=source_manifest,
            )
        except Exception as exc:  # noqa: BLE001 - refuse unjournaled mutation
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "before": before,
                "after": None,
            }

        oversized_repair_ids_set = set(oversized_repair_ids)
        deleted: dict[str, Any] | None = None
        repair_phase = "delete"
        try:
            repair_phase = "preflight"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            preflight_failure = await self._preflight_repair_candidates(
                repair_ids,
                repair_mapping,
                source_required_ids=oversized_repair_ids_set,
            )
            if preflight_failure is not None:
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    reason=preflight_failure["reason"],
                    error_type=preflight_failure.get("error_type"),
                )
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "repair_preflight_failed",
                    "repair_phase": repair_phase,
                    "repair_operation_id": repair_operation_id,
                    "repair_required": True,
                    "preflight": preflight_failure,
                    "before": before,
                    "after": None,
                }
            if not self._repair_rollback_available(bool(oversized_repair_ids_set)):
                reason = "repair_rollback_unavailable"
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    reason=reason,
                )
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": reason,
                    "repair_phase": repair_phase,
                    "repair_operation_id": repair_operation_id,
                    "repair_required": True,
                    "before": before,
                    "after": None,
                }
            if dataset is not None and not callable(
                getattr(self.cognee, "stored_chunk_budget_check", None)
            ):
                reason = "scoped_postcheck_unavailable"
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    reason=reason,
                )
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": reason,
                    "repair_phase": repair_phase,
                    "repair_operation_id": repair_operation_id,
                    "repair_required": True,
                    "before": before,
                    "after": None,
                }
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "delete"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            if oversized_repair_ids_set:
                deleted = await self.cognee.delete_document_chunks(
                    sorted(oversized_repair_ids_set)
                )
            if isinstance(deleted, dict) and deleted.get("ok") is False:
                raise RuntimeError(
                    str(deleted.get("reason") or "repair_delete_failed")
                )
            deleted_ids = (
                deleted.get("document_ids", [])
                if isinstance(deleted, dict)
                else []
            )
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                deleted_document_ids=deleted_ids,
            )
            repair_phase = "cognify"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            await self.cognee.cognify(
                datasets=repair_datasets,
                force=force or bool(oversized_repair_ids_set),
            )
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "post_census"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            after = await self.cognee.corpus_reconciliation_census(dataset=dataset)
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "post_index_check"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
            post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
            post_budget = after.get("stored_chunk_budget")
            if dataset is not None:
                post_budget = await self.cognee.stored_chunk_budget_check(
                    repair_ids,
                    budget=after.get("budget"),
                )
        except Exception as exc:  # noqa: BLE001 - return recoverable repair state
            logger.exception("corpus reconciliation failed during %s", repair_phase)
            deleted_ids = (
                deleted.get("document_ids", [])
                if isinstance(deleted, dict)
                else []
            )
            projections_preserved = (
                isinstance(deleted, dict)
                and deleted.get("projections_preserved") is True
            )
            if deleted is not None and not projections_preserved:
                projections_preserved = await self._restore_repair_projections(deleted)
            failure_reason = (
                deleted.get("reason")
                if isinstance(deleted, dict) and isinstance(deleted.get("reason"), str)
                else "repair_failed"
            )
            try:
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    deleted_document_ids=deleted_ids,
                    error_type=exc.__class__.__name__,
                    reason=failure_reason,
                    projections_preserved=projections_preserved,
                )
            except Exception:  # noqa: BLE001 - preserve the original failure
                logger.exception("repair journal failed during repair failure handling")
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": failure_reason,
                "repair_phase": repair_phase,
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "deleted": deleted,
                "projections_preserved": projections_preserved,
                "before": before,
                "after": None,
            }

        after_valid = isinstance(after, dict)
        post_counts_valid = (
            isinstance(post_counts, dict)
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                for value in (post_counts.get(document_id) for document_id in repair_ids)
            )
        )
        post_graph_valid = (
            post_graph_ids is not None
            and set(repair_ids).issubset({str(document_id) for document_id in post_graph_ids})
        )
        stored_budget = after.get("stored_chunk_budget") if isinstance(after, dict) else None
        if dataset is not None:
            stored_budget = post_budget
        stored_budget_valid = (
            isinstance(stored_budget, dict)
            and stored_budget.get("ok") is True
            and stored_budget.get("violation_count") == 0
            and stored_budget.get("missing_document_id_violation_count", 0) == 0
        )
        relevant_census_valid = (
            after_valid
            and after.get("unassigned_zero_chunk_document_count") == 0
            and after.get("unassigned_oversized_document_count") == 0
            and after.get("orphan_oversized_document_count") == 0
            and after.get("missing_document_id_violation_count") == 0
        )
        repaired = (
            after_valid
            and after.get("ok") is True
            and after.get("census_complete") is True
            and after.get("cap_exceeded") is False
            and after.get("zero_chunk_count") == 0
            and after.get("oversized_document_count") == 0
            and after.get("oversized_chunk_count") == 0
            and relevant_census_valid
            and post_counts_valid
            and post_graph_valid
            and stored_budget_valid
        )
        projections_preserved: bool | None = None
        if repaired:
            await self._discard_repair_snapshot(deleted)
        elif deleted is not None:
            projections_preserved = await self._restore_repair_projections(deleted)
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase="completed" if repaired else repair_phase,
                status="completed" if repaired else "failed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                reason="repaired" if repaired else "reconciliation_invariants_remain",
                post_repair_indexed=post_counts_valid and post_graph_valid,
                post_repair_stored_budget_ok=stored_budget_valid,
                projections_preserved=projections_preserved,
            )
        except Exception as exc:  # noqa: BLE001 - keep failed result auditable
            logger.exception("repair journal failed during post-repair audit")
            repaired = False
            journal_error = exc.__class__.__name__
        else:
            journal_error = None
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "repaired" if repaired else "reconciliation_invariants_remain",
            "repair_operation_id": repair_operation_id,
            "repair_journal_error": journal_error,
            "repair_required": not repaired,
            "repair_datasets": repair_datasets,
            "repair_document_ids": repair_ids,
            "deleted": deleted,
            "before": before,
            "after": after,
            "post_repair_chunk_counts": post_counts,
            "post_repair_graph_documents": sorted(post_graph_ids or set()),
            "post_repair_indexed": post_counts_valid and post_graph_valid,
            "post_repair_stored_budget_ok": stored_budget_valid,
            "projections_preserved": projections_preserved,
        }

    async def reconcile_zero_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Audit and optionally repair accepted documents with no vector chunks.

        The default is a read-only census. Applying a repair only cognifies the
        datasets attached to the affected rows, then performs the same census
        again. Rows without dataset membership are reported as unrepairable
        instead of being guessed into the default dataset.
        """
        if apply:
            maintenance = getattr(self.cognee, "maintenance", None)
            if not callable(maintenance):
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "maintenance_unavailable",
                    "before": None,
                    "after": None,
                }
            try:
                with self.repair_journal.lease():
                    journal_gate = self._repair_journal_gate(dataset=dataset, force=force)
                    if journal_gate is not None:
                        return journal_gate
                    async with maintenance():
                        return await self._reconcile_zero_chunk_documents(
                            dataset=dataset,
                            apply=True,
                            force=force,
                        )
            except RepairJournalLeaseError:
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "repair_journal_busy",
                    "repair_required": True,
                    "before": None,
                    "after": None,
                }
        return await self._reconcile_zero_chunk_documents(
            dataset=dataset,
            apply=False,
            force=force,
        )

    async def _reconcile_zero_chunk_documents(
        self,
        *,
        dataset: str | None,
        apply: bool,
        force: bool,
    ) -> dict[str, Any]:
        """Run the zero-chunk census inside the caller's maintenance boundary."""
        before = await self.cognee.corpus_zero_chunk_documents(dataset=dataset)
        if before.get("ok") is not True:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": before.get("reason") or "census_failed",
                "before": before,
                "after": None,
            }

        zero_count = before.get("zero_chunk_count")
        unassigned_count = before.get("unassigned_zero_chunk_count")
        repair_datasets = before.get("repair_datasets")
        zero_ids = before.get("zero_chunk_document_ids")
        repair_ids = before.get("repair_document_ids")
        repair_mapping = before.get("repair_document_datasets")
        if (
            isinstance(zero_count, bool)
            or not isinstance(zero_count, int)
            or zero_count < 0
            or isinstance(unassigned_count, bool)
            or not isinstance(unassigned_count, int)
            or unassigned_count < 0
            or not isinstance(repair_datasets, list)
            or any(not isinstance(item, str) or not item for item in repair_datasets)
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "census_returned_invalid_repair_metadata",
                "before": before,
                "after": None,
            }

        if zero_count == 0:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "no_zero_chunk_documents",
                "before": before,
                "after": before if apply else None,
            }

        if not apply:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": False,
                "force": force,
                "reason": "repair_required",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        if unassigned_count or not repair_datasets:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "zero_chunk_documents_without_dataset",
                "repair_required": True,
                "before": before,
                "after": None,
            }
        if (
            not isinstance(zero_ids, list)
            or any(not isinstance(item, str) or not item for item in zero_ids)
            or len(set(zero_ids)) != len(zero_ids)
            or not isinstance(repair_ids, list)
            or any(not isinstance(item, str) or not item for item in repair_ids)
            or len(set(repair_ids)) != len(repair_ids)
            or set(repair_ids) != set(zero_ids)
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "census_returned_invalid_repair_metadata",
                "repair_required": True,
                "before": before,
                "after": None,
            }
        if repair_mapping is None:
            repair_mapping = {document_id: list(repair_datasets) for document_id in repair_ids}
        if (
            not isinstance(repair_mapping, Mapping)
            or set(repair_mapping) != set(repair_ids)
            or any(
                not isinstance(datasets, list)
                or not datasets
                or any(not isinstance(item, str) or not item for item in datasets)
                for datasets in repair_mapping.values()
            )
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "census_returned_invalid_repair_metadata",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        source_manifest, source_manifest_failure = (
            await self._capture_repair_source_manifest(repair_ids)
        )
        if source_manifest_failure is not None:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": source_manifest_failure["reason"],
                "error_type": source_manifest_failure.get("error_type"),
                "missing_document_ids": source_manifest_failure.get(
                    "missing_document_ids", []
                ),
                "repair_required": True,
                "before": before,
                "after": None,
            }

        repair_operation_id = uuid4().hex
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase="started",
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
                source_manifest=source_manifest,
            )
        except Exception as exc:  # noqa: BLE001 - refuse unjournaled mutation
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "before": before,
                "after": None,
            }

        repair_phase = "cognify"
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
            )
            await self.cognee.cognify(datasets=repair_datasets, force=force)
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
            )
            repair_phase = "post_census"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
            )
            after = await self.cognee.corpus_zero_chunk_documents(dataset=dataset)
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
            )
            repair_phase = "post_index_check"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
            )
            post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
            post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
        except Exception as exc:  # noqa: BLE001 - retain the restart fence
            logger.exception("zero-chunk reconciliation failed during %s", repair_phase)
            try:
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    repair_document_datasets=repair_mapping,
                    error_type=exc.__class__.__name__,
                    reason="repair_failed",
                    source_manifest=source_manifest,
                    projections_preserved=False,
                )
            except Exception:  # noqa: BLE001 - preserve original failure
                logger.exception("repair journal failed during zero-chunk failure")
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_failed",
                "repair_phase": repair_phase,
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "before": before,
                "after": None,
            }

        after_zero_count = after.get("zero_chunk_count")
        post_counts_valid = isinstance(post_counts, dict) and all(
            isinstance(post_counts.get(document_id), int)
            and not isinstance(post_counts.get(document_id), bool)
            and post_counts.get(document_id, 0) > 0
            for document_id in repair_ids
        )
        post_graph_valid = post_graph_ids is not None and set(repair_ids).issubset(
            {str(document_id) for document_id in post_graph_ids}
        )
        repaired = (
            after.get("ok") is True
            and isinstance(after_zero_count, int)
            and not isinstance(after_zero_count, bool)
            and after_zero_count == 0
            and post_counts_valid
            and post_graph_valid
        )
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase="post_index_check",
                status="completed" if repaired else "failed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_mapping,
                reason="repaired" if repaired else "zero_chunk_documents_remain",
                source_manifest=source_manifest,
                post_repair_census_ok=after.get("ok") is True and after_zero_count == 0,
                post_repair_indexed=post_counts_valid and post_graph_valid,
                projections_preserved=False if repaired else None,
            )
        except Exception as exc:  # noqa: BLE001 - keep failed result auditable
            logger.exception("repair journal failed during zero-chunk postcheck")
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "before": before,
                "after": after,
            }
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "repaired" if repaired else "zero_chunk_documents_remain",
            "repair_required": not repaired,
            "repair_operation_id": repair_operation_id,
            "repair_datasets": repair_datasets,
            "repair_document_ids": repair_ids,
            "before": before,
            "after": after,
            "post_repair_chunk_counts": post_counts,
            "post_repair_graph_documents": sorted(post_graph_ids or set()),
            "post_repair_indexed": post_counts_valid and post_graph_valid,
            "projections_preserved": False if repaired else None,
        }

    async def reconcile_oversized_chunks(
        self,
        *,
        dataset: str | None = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        maintenance = getattr(self.cognee, "maintenance", None)
        if apply:
            try:
                with self.repair_journal.lease():
                    journal_gate = self._repair_journal_gate(dataset=dataset, force=force)
                    if journal_gate is not None:
                        return journal_gate
                    if callable(maintenance):
                        async with maintenance():
                            return await self._reconcile_oversized_chunks(
                                dataset=dataset,
                                apply=apply,
                                force=force,
                            )
                    return await self._reconcile_oversized_chunks(
                        dataset=dataset,
                        apply=apply,
                        force=force,
                    )
            except RepairJournalLeaseError:
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": force,
                    "reason": "repair_journal_busy",
                    "repair_required": True,
                    "before": None,
                    "after": None,
                }
        return await self._reconcile_oversized_chunks(
            dataset=dataset,
            apply=apply,
            force=force,
        )

    async def _reconcile_oversized_chunks(
        self,
        *,
        dataset: str | None = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Audit and optionally rebuild persisted chunks over the embed budget.

        This path is separate from zero-chunk repair because old chunk rows must
        be removed before Cognee re-cognifies. It is dry-run by default and
        requires ``force`` on apply so a caller cannot accidentally trigger a
        full dataset rebuild from a routine zero-chunk command.
        """
        before = await self.cognee.corpus_oversized_chunk_documents(dataset=dataset)
        if (
            before.get("ok") is not True
            or before.get("census_complete") is not True
            or before.get("cap_exceeded") is not False
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": before.get("reason") or "census_failed",
                "before": before,
                "after": None,
            }

        oversized_count = before.get("oversized_document_count")
        unassigned_count = before.get("unassigned_oversized_document_count")
        missing_id_count = before.get("missing_document_id_violation_count")
        orphan_count = before.get("orphan_oversized_document_count")
        report_truncated = before.get("oversized_documents_truncated")
        repair_ids = before.get("repair_document_ids")
        repair_datasets = before.get("repair_datasets")
        if (
            isinstance(oversized_count, bool)
            or not isinstance(oversized_count, int)
            or oversized_count < 0
            or isinstance(unassigned_count, bool)
            or not isinstance(unassigned_count, int)
            or unassigned_count < 0
            or isinstance(missing_id_count, bool)
            or not isinstance(missing_id_count, int)
            or missing_id_count < 0
            or isinstance(orphan_count, bool)
            or not isinstance(orphan_count, int)
            or orphan_count < 0
            or not isinstance(report_truncated, bool)
            or not isinstance(repair_ids, list)
            or any(not isinstance(item, str) or not item for item in repair_ids)
            or len(set(repair_ids)) != len(repair_ids)
            or not isinstance(repair_datasets, list)
            or any(not isinstance(item, str) or not item for item in repair_datasets)
            or (not unassigned_count and oversized_count != len(repair_ids))
        ):
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "census_returned_invalid_repair_metadata",
                "before": before,
                "after": None,
            }

        if oversized_count == 0 and not unassigned_count and not missing_id_count:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "no_oversized_chunks",
                "before": before,
                "after": before if apply else None,
            }

        if not apply:
            return {
                "ok": True,
                "dataset": dataset,
                "apply": False,
                "force": force,
                "reason": "oversized_chunks_repair_required",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        if not force:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": False,
                "reason": "oversized_chunks_repair_requires_force",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        if unassigned_count or missing_id_count or not repair_ids or not repair_datasets:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": True,
                "reason": "oversized_chunks_not_fully_assigned",
                "repair_required": True,
                "before": before,
                "after": None,
            }

        repair_operation_id = uuid4().hex
        repair_document_datasets = {
            document_id: ([dataset] if dataset else list(repair_datasets))
            for document_id in repair_ids
        }
        source_manifest, source_manifest_failure = (
            await self._capture_repair_source_manifest(repair_ids)
        )
        if source_manifest_failure is not None:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": True,
                "reason": source_manifest_failure["reason"],
                "error_type": source_manifest_failure.get("error_type"),
                "missing_document_ids": source_manifest_failure.get(
                    "missing_document_ids", []
                ),
                "repair_required": True,
                "before": before,
                "after": None,
            }
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase="started",
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                repair_document_datasets=repair_document_datasets,
                source_manifest=source_manifest,
            )
        except Exception as exc:  # noqa: BLE001 - refuse unjournaled mutation
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": True,
                "reason": "repair_journal_unavailable",
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "before": before,
                "after": None,
            }

        deleted: dict[str, Any] | None = None
        repair_phase = "preflight"
        try:
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            preflight_failure = await self._preflight_repair_candidates(
                repair_ids, repair_document_datasets
            )
            if preflight_failure is not None:
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    reason=preflight_failure["reason"],
                    error_type=preflight_failure.get("error_type"),
                )
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": True,
                    "reason": "repair_preflight_failed",
                    "repair_phase": repair_phase,
                    "repair_operation_id": repair_operation_id,
                    "repair_required": True,
                    "preflight": preflight_failure,
                    "before": before,
                    "after": None,
                }
            if not self._repair_rollback_available(True):
                reason = "repair_rollback_unavailable"
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    reason=reason,
                )
                return {
                    "ok": False,
                    "dataset": dataset,
                    "apply": True,
                    "force": True,
                    "reason": reason,
                    "repair_phase": repair_phase,
                    "repair_operation_id": repair_operation_id,
                    "repair_required": True,
                    "before": before,
                    "after": None,
                }
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "delete"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            deleted = await self.cognee.delete_document_chunks(repair_ids)
            if isinstance(deleted, dict) and deleted.get("ok") is False:
                raise RuntimeError(
                    str(deleted.get("reason") or "repair_delete_failed")
                )
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
                deleted_document_ids=deleted.get("document_ids", [])
                if isinstance(deleted, dict)
                else (),
            )
            repair_phase = "cognify"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            await self.cognee.cognify(datasets=repair_datasets, force=True)
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "post_census"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            after = await self.cognee.corpus_oversized_chunk_documents(dataset=dataset)
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="completed",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            repair_phase = "post_index_check"
            self.repair_journal.append(
                operation_id=repair_operation_id,
                dataset=dataset,
                phase=repair_phase,
                status="started",
                repair_document_ids=repair_ids,
                repair_datasets=repair_datasets,
            )
            post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
            post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
        except Exception as exc:  # noqa: BLE001 - return recoverable repair state
            logger.exception("oversized reconciliation failed during %s", repair_phase)
            deleted_ids = (
                deleted.get("document_ids", [])
                if isinstance(deleted, dict)
                else []
            )
            projections_preserved = (
                isinstance(deleted, dict)
                and deleted.get("projections_preserved") is True
            )
            if deleted is not None and not projections_preserved:
                projections_preserved = await self._restore_repair_projections(deleted)
            failure_reason = (
                deleted.get("reason")
                if isinstance(deleted, dict) and isinstance(deleted.get("reason"), str)
                else "repair_failed"
            )
            try:
                self.repair_journal.append(
                    operation_id=repair_operation_id,
                    dataset=dataset,
                    phase=repair_phase,
                    status="failed",
                    repair_document_ids=repair_ids,
                    repair_datasets=repair_datasets,
                    deleted_document_ids=deleted_ids,
                    error_type=exc.__class__.__name__,
                    reason=failure_reason,
                    projections_preserved=projections_preserved,
                )
            except Exception:  # noqa: BLE001 - preserve original failure
                logger.exception("repair journal failed during repair failure handling")
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": True,
                "reason": failure_reason,
                "repair_phase": repair_phase,
                "error_type": exc.__class__.__name__,
                "repair_operation_id": repair_operation_id,
                "repair_required": True,
                "deleted": deleted,
                "projections_preserved": projections_preserved,
                "before": before,
                "after": None,
            }
        post_indexed = (
            isinstance(post_counts, dict)
            and post_graph_ids is not None
            and all(int(post_counts.get(document_id, 0)) > 0 for document_id in repair_ids)
            and set(repair_ids).issubset({str(document_id) for document_id in post_graph_ids})
        )
        after_oversized_count = after.get("oversized_document_count")
        after_oversized_chunk_count = after.get("oversized_chunk_count")
        repaired = (
            after.get("ok") is True
            and after.get("census_complete") is True
            and after.get("cap_exceeded") is False
            and isinstance(after_oversized_count, int)
            and not isinstance(after_oversized_count, bool)
            and after_oversized_count == 0
            and isinstance(after_oversized_chunk_count, int)
            and not isinstance(after_oversized_chunk_count, bool)
            and after_oversized_chunk_count == 0
            and after.get("missing_document_id_violation_count") == 0
            and after.get("unassigned_oversized_document_count") == 0
            and after.get("orphan_oversized_document_count") == 0
            and post_indexed
        )
        projections_preserved: bool | None = None
        if repaired:
            await self._discard_repair_snapshot(deleted)
        elif deleted is not None:
            projections_preserved = await self._restore_repair_projections(deleted)
        self.repair_journal.append(
            operation_id=repair_operation_id,
            dataset=dataset,
            phase="post_index_check",
            status="completed" if repaired else "failed",
            repair_document_ids=repair_ids,
            repair_datasets=repair_datasets,
            reason="repaired" if repaired else "reconciliation_invariants_remain",
            post_repair_indexed=post_indexed,
            projections_preserved=projections_preserved,
        )
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": True,
            "reason": "repaired" if repaired else "oversized_chunks_remain",
            "repair_required": not repaired,
            "repair_operation_id": repair_operation_id,
            "repair_datasets": repair_datasets,
            "deleted": deleted,
            "before": before,
            "after": after,
            "post_repair_chunk_counts": post_counts,
            "post_repair_graph_documents": sorted(post_graph_ids or set()),
            "post_repair_indexed": post_indexed,
            "projections_preserved": projections_preserved,
        }

    async def _delete_marker_node(self, marker: str) -> None:
        """Best-effort delete of a cognify verify-marker node (backprop, #15)."""
        try:
            nodes, _ = await self.cognee.graph_data()
            ids = [
                str(node_id)
                for node_id, properties in nodes
                if marker
                in str((properties or {}).get("text") or (properties or {}).get("name") or "")
            ]
            if ids:
                await self.cognee.delete_graph_nodes(ids)
        except Exception:  # noqa: BLE001 - cleanup must never fail the cognify
            logger.warning("could not delete cognify verify marker %s", marker, exc_info=True)

    async def _tombstone_marker_source(
        self, projection_job_id: str, *, marker: str
    ) -> None:
        """Best-effort durable cleanup of a verify marker's source revision.

        ``_delete_marker_node`` removes the already-projected graph node, but
        under lifecycle v1 the marker is also a durable source revision whose
        current head would otherwise stay searchable forever — one accreted
        marker document per verify pass. Tombstoning supersedes the head, so
        the search filters exclude the marker durably whether or not its
        projection ever drained. The tombstone projection writes current-head
        exclusion receipts and deletes no provider content
        (``kb/lifecycle_worker.py`` ``_project_tombstone``), so the vector
        chunk becomes an excluded orphan rather than disappearing; the graph
        node is the ``_delete_marker_node`` call above. Never fails the
        cognify on a cleanup hiccup.
        """
        try:
            operation = self.lifecycle_operation(projection_job_id)
            await self.tombstone_source(
                dataset=str(operation["dataset"]),
                source_key=str(operation["source_revision"]["source_key"]),
                reason="cognify_verify_marker_cleanup",
            )
        except Exception:  # noqa: BLE001 - cleanup must never fail the cognify
            logger.warning(
                "could not tombstone cognify verify marker %s", marker, exc_info=True
            )

    async def cleanup_legacy_nodes(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Find (and, when dry_run is False, delete) legacy garbage nodes (#15).

        Targets only the well-identified leak classes — COGNIFY_TEST_MARKER canaries,
        the literal ``[DataItem]`` / session-scaffold blobs, explicit session-cache
        node types, pre-ADR-0016 repo-content fossils (machine-rendered headers
        still carrying a ``Retrieved:`` timestamp line), and pre-fix GitHub digest
        fossils (machine-rendered digest headers still carrying a ``Checked at:``
        timestamp line). Each class is counted under its own ``counts_by_kind``
        key so a human can approve one class independently of the others. The
        classifier is anchored so real content is never matched; the default dry
        run returns every candidate id + preview so a human verifies before any
        deletion.
        """
        nodes, _ = await self.cognee.graph_data()
        candidates: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        seen: set[str] = set()

        def _add(node_id: Any, kind: str, text: Any) -> None:
            cid = str(node_id)
            if cid in seen:
                return
            seen.add(cid)
            candidates.append({"id": cid, "kind": kind, "preview": _normalize_text(text)[:120]})
            counts[kind] = counts.get(kind, 0) + 1

        for node_id, properties in nodes:
            kind = _legacy_garbage_kind(node_id, properties)
            if kind is not None:
                props = properties if isinstance(properties, dict) else {}
                _add(node_id, kind, props.get("text") or props.get("name") or node_id)

        # The same garbage was also cognified into the chunk vector store, which the
        # graph scan can't see once the graph node is gone. Sweep it via search so
        # orphaned [DataItem]/marker chunks are caught and purged too (#15).
        for probe in (
            "[DataItem]",
            "COGNIFY_TEST_MARKER",
            "Session ID Question Answer",
            "Repository: Source: Commit: Blob: Retrieved:",
            "GitHub daily update Checked at: Repositories scanned:",
        ):
            try:
                hits = await self.search(probe, dataset=self.config.default_dataset, top_k=100)
            except Exception:  # noqa: BLE001 - sweep is best-effort
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                hit_id = hit.get("id")
                text = hit.get("text") or hit.get("answer") or ""
                if hit_id:
                    kind = _legacy_garbage_kind(hit_id, {"text": text})
                    if kind is not None:
                        _add(hit_id, kind, text)

        deleted = 0
        if not dry_run and candidates:
            deleted = await self.cognee.delete_graph_nodes([c["id"] for c in candidates])
        return {
            "dry_run": dry_run,
            "counts_by_kind": counts,
            "candidates": candidates,
            "deleted": deleted,
        }


def _marker_in_results(marker: str, results: list[Any]) -> bool:
    for item in results:
        if marker in str(item):
            return True
    return False


_MARKER_RE = re.compile(r"^COGNIFY_TEST_MARKER_[0-9a-f]{32}$")
_SESSION_CACHE_TYPES = {"user_sessions_from_cache", "session_cache"}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _is_dataitem_garbage(text: str) -> bool:
    """True for the #26/#52 ``[DataItem]`` leak only.

    Matches a bare ``[DataItem]`` placeholder, or a session-scaffold blob whose
    every ``Answer:`` line is exactly ``[DataItem]`` and every ``Question:`` line
    is empty. Never matches real prose that merely contains the substring (a real
    answer or a non-empty question keeps the node).
    """
    if _normalize_text(text) == "[DataItem]":
        return True
    has_answer = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Answer:"):
            has_answer = True
            if line[len("Answer:"):].strip() != "[DataItem]":
                return False
        elif line.startswith("Question:"):
            if line[len("Question:"):].strip():
                return False
    return has_answer


_REPO_CONTENT_TITLE_RE = re.compile(r"^#\s+[^\s/]+/[^\s/]+/\S")
_REPO_CONTENT_HEADER_FIELDS = ("Repository:", "Source:", "Commit:", "Blob:")


def _is_repo_content_fossil(text: str) -> bool:
    """True for a pre-ADR-0016 repo-content document only (d5d0fe3).

    Until d5d0fe3 (ADR-0016) every repo-content sync stamped
    ``Retrieved: {checked_at}`` into the rendered header, so an unchanged file
    minted a NEW document each pass. Those pre-fix copies are never superseded
    and keep occupying result slots.

    A fossil is identified only by the FULL machine-rendered header: the text
    must start with the ``# org/repo/path`` title line, the header block up to
    the ``---`` separator may contain only blank lines, the ``Repository:`` /
    ``Source:`` / ``Commit:`` / ``Blob:`` field lines in renderer order, and a
    ``Retrieved:`` line — and that ``Retrieved:`` line must be present INSIDE
    the header, before the separator. A bare "Retrieved:" in a body (after the
    ``---``), a post-fix header without the line, or any line the renderer
    never produced means the node is kept. No separator seen (e.g. a chunk cut
    mid-header) also keeps the node — fail closed on deletion.
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or not _REPO_CONTENT_TITLE_RE.match(lines[0]):
        return False
    fields = iter(_REPO_CONTENT_HEADER_FIELDS)
    pending: str | None = next(fields)
    retrieved_in_header = False
    for line in lines[1:]:
        if line == "---":
            # End of header: qualify only with all four fields AND Retrieved.
            return pending is None and retrieved_in_header
        if not line:
            continue
        if pending is not None and line.startswith(pending):
            pending = next(fields, None)
        elif line.startswith("Retrieved:"):
            retrieved_in_header = True
        else:
            # A line the renderer never emitted: this is not a rendered
            # repo-content header, so never a fossil.
            return False
    return False


_DIGEST_TITLE_SUFFIX = " GitHub daily update"
# Every header field the digest renderer has ever emitted between the title
# line and the first section heading, in renderer order. Optional entries cover
# older render vintages: the earliest digests had neither ``Window started
# at:`` nor the last three counter lines.
_GITHUB_DIGEST_HEADER_FIELDS: tuple[tuple[str, bool], ...] = (
    ("Checked at:", True),
    ("Window started at:", False),
    ("Source:", True),
    ("Repositories scanned:", True),
    ("Changed repositories since last check:", True),
    ("New public organization events:", True),
    ("New commits observed:", False),
    ("Open pull requests active in window:", False),
    ("Merged pull requests in window:", False),
)
_DIGEST_HEADER_TERMINATOR = "## Changed repositories"


def _is_github_digest_fossil(text: str) -> bool:
    """True for a pre-fix GitHub org digest only.

    Until the digest renderer was brought under ADR-0016, every GitHub sync
    stamped ``Checked at: {utc_now}`` (and later a derived ``Window started
    at:`` line) into the digest body, so a digest whose reported activity had
    not changed still minted a NEW document each pass — the same defect class
    d5d0fe3 fixed for repo content via its ``Retrieved:`` line. Those pre-fix
    copies are never superseded and keep occupying result slots.

    A fossil is identified only by the FULL machine-rendered header: the text
    must start with the ``# {org} GitHub daily update`` title line, every
    non-blank line before the ``## Changed repositories`` section heading must
    match the renderer's header fields in renderer order, every required field
    (``Checked at:`` above all) must be present, and the section heading itself
    must be reached. A ``Checked at:`` in prose, a post-fix header (which has
    no ``Checked at:``), an unknown or out-of-order line, or a chunk cut before
    the section heading all keep the node — fail closed on deletion.
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines:
        return False
    title = lines[0]
    if (
        not title.startswith("# ")
        or not title.endswith(_DIGEST_TITLE_SUFFIX)
        or not title[2 : -len(_DIGEST_TITLE_SUFFIX)].strip()
    ):
        return False
    index = 0
    for line in lines[1:]:
        if line == _DIGEST_HEADER_TERMINATOR:
            # End of header: qualify only when every required field was seen.
            return all(not required for _, required in _GITHUB_DIGEST_HEADER_FIELDS[index:])
        if not line:
            continue
        while index < len(_GITHUB_DIGEST_HEADER_FIELDS) and not line.startswith(
            _GITHUB_DIGEST_HEADER_FIELDS[index][0]
        ):
            if _GITHUB_DIGEST_HEADER_FIELDS[index][1]:
                # A required renderer field is missing or out of order: this is
                # not a pre-fix rendered digest header, so never a fossil.
                return False
            index += 1
        if index >= len(_GITHUB_DIGEST_HEADER_FIELDS):
            # A line the renderer never emitted in this position: keep the node.
            return False
        index += 1
    return False


def _legacy_garbage_kind(node_id: Any, properties: Any) -> str | None:
    """Classify a graph node as legacy garbage to purge, or None to keep (#15).

    Conservative + anchored: only an exact COGNIFY_TEST_MARKER id, the literal
    [DataItem]/session-scaffold blob, an explicit session-cache node type, a
    pre-ADR-0016 repo-content fossil (full rendered header with a Retrieved:
    line inside it), or a pre-fix GitHub digest fossil (full rendered digest
    header with a Checked at: line inside it). Real content is never
    classified — there is no substring-of-prose match.
    """
    props = properties if isinstance(properties, dict) else {}
    for value in (props.get("text"), props.get("name"), props.get("title"), props.get("id"), node_id):
        if isinstance(value, str) and _MARKER_RE.fullmatch(value.strip()):
            return "marker"
    text = props.get("text")
    if isinstance(text, str) and _is_dataitem_garbage(text):
        return "dataitem"
    if isinstance(text, str) and _is_repo_content_fossil(text):
        return "repo_content_fossil"
    if isinstance(text, str) and _is_github_digest_fossil(text):
        return "github_digest_fossil"
    for key in ("type", "node_type", "category", "source"):
        value = props.get(key)
        if isinstance(value, str) and value.strip().lower() in _SESSION_CACHE_TYPES:
            return "session_cache"
    return None
