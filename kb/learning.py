"""The Learning Process: governed transformation of Source Material into
Structured Knowledge.

Per CONTEXT.md and docs/architecture-deepening-opportunities.md (item 1), this
module owns the workflow that callers previously wired by hand: pre-ingest
filtering and structured ingestion (delegated to :class:`kb.service.Citadel`),
Knowledge Mesh projection recording, ingest-time Knowledge Conflict detection,
and the optional Cognee improvement step. Server routes, GitHub sync, and
Obsidian sync feed it source material instead of repeating the orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime
import logging
from typing import Any, Literal

from kb.conflicts import KnowledgeConflictStore, detect_contribution_conflict
from kb.llm_enrichment import EnrichedChunk, EnrichmentOutcome, enrich_source_material
from kb.lifecycle import lifecycle_chunk_source_key
from kb.mesh import MeshState
from kb.security_scan import SecretContentError, SecurityScanEntry, scan_text_entries
from kb.models import IngestResult
from kb.improvement_policy import (
    require_automation_evaluation,
    validate_central_improvement_scope,
)
from kb.service import Citadel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LearningOutcome:
    """What one Learning Process pass produced."""

    ingest: IngestResult
    dataset: str
    improve: Any | None = None
    conflict: dict[str, Any] | None = None
    chunk_ingests: tuple[IngestResult, ...] = ()
    enrichment: dict[str, Any] | None = None

    @property
    def all_ingests(self) -> tuple[IngestResult, ...]:
        return self.chunk_ingests or (self.ingest,)

    @property
    def accepted_chunks(self) -> int:
        return sum(1 for result in self.all_ingests if result.accepted)

    @property
    def improved(self) -> bool:
        if not self.improve:
            return False
        return not (isinstance(self.improve, dict) and self.improve.get("ok") is False)

    @property
    def improve_error(self) -> str | None:
        if isinstance(self.improve, dict) and self.improve.get("ok") is False:
            return self.improve.get("error")
        return None


class LearningProcess:
    """One entry point from accepted Source Material to vault memory."""

    def __init__(
        self,
        citadel: Citadel,
        *,
        mesh: MeshState | None = None,
        conflicts: KnowledgeConflictStore | None = None,
    ) -> None:
        self.citadel = citadel
        self.config = citadel.config
        self.mesh = mesh
        self.conflicts = conflicts

    async def learn(
        self,
        data: str,
        *,
        dataset: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        attestation: Mapping[str, str] | None = None,
        operation: str = "ingest",
        run_improve: bool = False,
        detect_conflicts: bool = True,
        tier: Literal["full", "light", "shared"] = "full",
        allow_llm: bool = True,
        defer_cognify: bool = False,
        source_key: str | None = None,
        source_locator: str | None = None,
        media_type: str = "text/plain",
        capture_actor_id: str | None = None,
        capture_run_id: str | None = None,
        captured_at: datetime | None = None,
    ) -> LearningOutcome:
        """Filter, optionally enrich/chunk, ingest, record mesh activity,
        detect conflicts, and optionally run improvement for one piece of
        Source Material.

        LLM enrichment (``CITADEL_LLM_ENRICHMENT_ENABLED``) is a best-effort
        pre-ingest step: any failure falls back to deterministic chunking and
        ingestion proceeds. On ingest failure the error is recorded to the
        mesh projection (when attached) and re-raised for the caller.

        ``tier="light"`` skips enrichment and improvement for seat-node memory.
        ``tier="shared"`` skips full-document enrichment and improvement for
        volunteered Shared Session Traces (dead-end refinement happens upstream).
        ``allow_llm=False`` skips every LLM-backed step for user-level writes.
        """
        target_dataset = dataset or self.config.default_dataset
        # ADR-0005 step 1: scan the WHOLE document up front — before enrichment
        # (so a secret never leaves to the external LLM) and before chunking (so a
        # secret spanning a chunk boundary can't slip past the per-chunk gate in
        # citadel.ingest, which stays as the universal backstop).
        if self.config.content_scan_enabled:
            scan = scan_text_entries(
                [SecurityScanEntry(source="ingest", location=target_dataset, text=data)],
                block_severity=self.config.content_scan_block_severity,
            )
            if scan.get("blocked"):
                raise SecretContentError(
                    dataset=target_dataset,
                    highest_severity=scan.get("highest_severity"),
                    block_severity=self.config.content_scan_block_severity,
                    findings=scan.get("findings", []),
                )
        if not allow_llm or tier in {"light", "shared"}:
            enrichment = None
            effective_run_improve = False
        else:
            enrichment = self._enrich(data)
            effective_run_improve = run_improve
        if effective_run_improve:
            validate_central_improvement_scope(
                self.config,
                dataset=target_dataset,
                session_ids=None,
            )
            require_automation_evaluation(self.config)
        chunk_inputs = self._chunk_inputs(data, list(tags or []), enrichment)
        active_source_keys = {
            source_key
            if len(chunk_inputs) == 1
            else lifecycle_chunk_source_key(source_key, index)
            for index in range(len(chunk_inputs))
        } if source_key is not None else set()
        previous_source_keys: set[str] = set()
        lifecycle_source_keys = getattr(self.citadel, "lifecycle_source_keys", None)
        if source_key is not None and callable(lifecycle_source_keys):
            previous_source_keys = set(
                lifecycle_source_keys(
                    dataset=target_dataset,
                    source_key=source_key,
                    include_chunks=True,
                )
            )

        results: list[IngestResult] = []
        for chunk_index, (chunk_data, chunk_tags) in enumerate(chunk_inputs):
            chunk_source_key = None
            try:
                ingest_kwargs: dict[str, Any] = {
                    "dataset": dataset,
                    "tags": chunk_tags,
                    "session_id": session_id,
                    "defer_cognify": defer_cognify,
                }
                if attestation is not None:
                    ingest_kwargs["attestation"] = attestation
                if source_key is not None:
                    if len(chunk_inputs) == 1:
                        chunk_source_key = source_key
                    else:
                        chunk_source_key = lifecycle_chunk_source_key(
                            source_key,
                            chunk_index,
                        )
                    ingest_kwargs["source_key"] = chunk_source_key
                    if len(chunk_inputs) > 1:
                        ingest_kwargs["_lifecycle_parent_source_key"] = source_key
                        ingest_kwargs["_lifecycle_chunk_index"] = chunk_index
                if source_locator is not None:
                    ingest_kwargs["source_locator"] = source_locator
                if media_type != "text/plain":
                    ingest_kwargs["media_type"] = media_type
                if capture_actor_id is not None:
                    ingest_kwargs["capture_actor_id"] = capture_actor_id
                if capture_run_id is not None:
                    ingest_kwargs["capture_run_id"] = capture_run_id
                if captured_at is not None:
                    ingest_kwargs["captured_at"] = captured_at
                result = await self.citadel.ingest(chunk_data, **ingest_kwargs)
            except Exception as exc:
                if self.mesh:
                    await self.mesh.record_error(
                        self.config, operation=operation, error=str(exc)
                    )
                raise
            results.append(result)
            if self.mesh:
                await self.mesh.record_ingest(
                    self.config,
                    result,
                    data=chunk_data,
                    dataset=target_dataset,
                    tags=list(chunk_tags),
                    source_key=chunk_source_key,
                    source_locator=source_locator,
                    capture_actor_id=capture_actor_id,
                    capture_run_id=capture_run_id,
                    captured_at=captured_at,
                )

        if enrichment is not None and self.mesh:
            await self.mesh.record_enrichment(
                self.config,
                dataset=target_dataset,
                chunks=len(results),
                used_llm=enrichment.used_llm,
                reason=enrichment.reason,
                model=enrichment.model,
            )

        primary = next((result for result in results if result.accepted), results[0])
        accepted_any = any(result.accepted for result in results)
        if source_key is not None and results and all(result.accepted for result in results):
            tombstone_source = getattr(self.citadel, "tombstone_source", None)
            if callable(tombstone_source):
                for obsolete_key in sorted(previous_source_keys - active_source_keys):
                    await tombstone_source(
                        dataset=target_dataset,
                        source_key=obsolete_key,
                        reason="source chunk set replaced",
                        source_locator=source_locator,
                        capture_actor_id=capture_actor_id,
                        capture_run_id=capture_run_id,
                        captured_at=captured_at,
                        include_chunks=False,
                    )

        conflict = None
        if detect_conflicts and accepted_any:
            conflict = self._detect_conflict(data)
            if conflict and self.mesh:
                await self.mesh.record_conflict(self.config, conflict=conflict)

        improve_result = None
        if effective_run_improve and accepted_any:
            improve_result = await self._improve(
                dataset=target_dataset,
                session_ids=None,
            )

        return LearningOutcome(
            ingest=primary,
            dataset=target_dataset,
            improve=improve_result,
            conflict=conflict,
            chunk_ingests=tuple(results) if len(results) > 1 else (),
            enrichment=None
            if enrichment is None
            else {
                "used_llm": enrichment.used_llm,
                "reason": enrichment.reason,
                "chunks": len(results),
                "model": enrichment.model,
            },
        )

    def _enrich(self, data: str) -> EnrichmentOutcome | None:
        """Best-effort enrichment; ``None`` means plain single-piece ingestion."""
        try:
            outcome = enrich_source_material(data)
        except Exception as exc:  # pragma: no cover - enrichment must never break ingestion.
            logger.warning(
                "LLM enrichment failed with %s; ingesting without enrichment",
                exc.__class__.__name__,
            )
            return None
        if outcome.reason in {"disabled", "below_threshold"} and not outcome.chunked:
            return None
        return outcome

    @staticmethod
    def _chunk_inputs(
        data: str,
        tags: list[str],
        enrichment: EnrichmentOutcome | None,
    ) -> list[tuple[str, list[str]]]:
        if enrichment is None or not enrichment.chunks:
            return [(data, tags)]
        inputs: list[tuple[str, list[str]]] = []
        for chunk in enrichment.chunks:
            inputs.append((_chunk_text(chunk), [*tags, *chunk.tags]))
        return inputs

    async def record_failure(self, *, operation: str, error: str) -> None:
        """Record a learning-related failure on the mesh projection."""
        if self.mesh:
            await self.mesh.record_error(self.config, operation=operation, error=error)

    def _detect_conflict(self, data: str) -> dict[str, Any] | None:
        if not self.conflicts:
            return None
        try:
            candidate = detect_contribution_conflict(data, config=self.config)
        except Exception as exc:  # pragma: no cover - defensive; detection is best-effort.
            logger.warning(
                "Knowledge conflict detection failed with %s; continuing",
                exc.__class__.__name__,
            )
            return None
        if not candidate:
            return None
        return self.conflicts.record(candidate)

    async def improve_once(
        self,
        *,
        dataset: str | None = None,
        session_ids: list[str] | None = None,
    ) -> Any:
        """Run a single improve pass, decoupled from any one document.

        `learn(run_improve=True)` improves after every document, which is right
        for a one-off capture and ruinous for a batch importer: a full cognee
        improve pass costs roughly two minutes, so a 60-file sync spent about
        two hours improving and was killed by the platform's 300 s request
        ceiling before it could record any of it. Batch callers pass
        `run_improve=False` per document and call this once at the end.
        """
        return await self._improve(
            dataset=dataset or self.config.default_dataset,
            session_ids=session_ids,
        )

    async def _improve(
        self,
        *,
        dataset: str,
        session_ids: list[str] | None,
    ) -> Any:
        try:
            return await self.citadel.improve(
                dataset=dataset,
                session_ids=session_ids,
                automation=True,
            )
        except Exception as exc:
            logger.warning(
                "Learning process improve step failed with %s; continuing without improve",
                exc.__class__.__name__,
            )
            return {"ok": False, "error": str(exc)}


def _chunk_text(chunk: EnrichedChunk) -> str:
    """Merge the one-line summary into the chunk body so it is searchable."""
    if chunk.summary:
        return f"Summary: {chunk.summary}\n\n{chunk.text}"
    return chunk.text
