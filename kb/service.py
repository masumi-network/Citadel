from __future__ import annotations

import asyncio
from collections.abc import Iterable
from hashlib import sha256
import logging
import re
from uuid import uuid4
from typing import Any

from kb import chunk_window
from kb.cognee_client import CogneeGateway, CogneePublicClient
from kb.config import CitadelConfig
from kb.filters import PreIngestFilter
from kb.logging_utils import safe_log_value
from kb.models import FeedbackRequest, FeedbackResult, IngestResult
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
# instead of sleeping through the real backoff.
CANARY_SEARCH_ATTEMPTS = 3
CANARY_SEARCH_BACKOFF_SECONDS = 2.0


class Citadel:
    def __init__(
        self,
        config: CitadelConfig | None = None,
        *,
        cognee: CogneeGateway | None = None,
    ) -> None:
        self.config = config or CitadelConfig.from_env()
        self.cognee = cognee or CogneePublicClient()
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
        defer_cognify: bool = False,
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
        if ingest_key in self._seen_ingest_keys:
            logger.info(
                "Ingest rejected for dataset %s: duplicate_in_process",
                safe_log_value(target_dataset),
            )
            return IngestResult(False, "duplicate_in_process", target_dataset, merged_tags)
        # Claimed BEFORE the await so two concurrent ingests of identical content
        # cannot both pass the check, and released again if the write fails. It
        # used to be added and never removed, so one failed remember() marked that
        # content "seen" for the life of the process: every retry then returned
        # duplicate_in_process, and a caller that only records state on success
        # (repo_content_sync) could never recover the file.
        self._seen_ingest_keys.add(ingest_key)
        try:
            result = await self.cognee.remember(
                data,
                dataset_name=target_dataset,
                session_id=session_id,
                tags=merged_tags,
                defer_cognify=defer_cognify,
            )
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
        return IngestResult(True, reason, target_dataset, merged_tags, result)

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
        results = await self.cognee.recall(
            query,
            dataset=target_dataset,
            session_id=session_id or self._default_session_for_dataset(target_dataset),
            top_k=top_k,
        )
        if results or target_dataset != self.config.github_sync_dataset:
            return results
        return search_github_sync_state(query, self.config, top_k=top_k)

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
            await self.ingest(marker, dataset=target_dataset)
            await self.cognee.cognify(datasets=[target_dataset], force=force)
            # Cognify can still be settling, so re-search a bounded number of
            # times before calling it a miss (#114). Without this, requiring a
            # real search hit would flag a slow-but-healthy node as broken.
            search_hit = False
            attempts = 0
            for attempt in range(CANARY_SEARCH_ATTEMPTS):
                attempts = attempt + 1
                matches = await self.search(marker, dataset=target_dataset, top_k=10)
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
            # Backprop (#15): the canary marker used to persist forever, surfacing in
            # search/linear_search results. Delete its node now so verify leaves no
            # trace. Best-effort — never fail the cognify on a cleanup hiccup.
            await self._delete_marker_node(marker)

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
    ) -> dict[str, Any]:
        """Audit and optionally repair zero and over-budget projections together.

        The combined path is the default because a document can satisfy both
        failure predicates. One census decides the union, one repair removes only
        stale oversized projections, one cognify rebuilds the union, and one
        post-census verifies the vector, graph, and chunk-budget invariants.
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
            async with maintenance():
                return await self._reconcile_corpus(
                    dataset=dataset,
                    apply=True,
                    force=force,
                )
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

        oversized_repair_ids_set = set(oversized_repair_ids)
        deleted: dict[str, Any] | None = None
        repair_phase = "delete"
        try:
            if oversized_repair_ids_set:
                deleted = await self.cognee.delete_document_chunks(
                    sorted(oversized_repair_ids_set)
                )
            repair_phase = "cognify"
            await self.cognee.cognify(
                datasets=repair_datasets,
                force=force or bool(oversized_repair_ids_set),
            )
            repair_phase = "post_census"
            after = await self.cognee.corpus_reconciliation_census(dataset=dataset)
            repair_phase = "post_index_check"
            post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
            post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
        except Exception as exc:  # noqa: BLE001 - return recoverable repair state
            logger.exception("corpus reconciliation failed during %s", repair_phase)
            return {
                "ok": False,
                "dataset": dataset,
                "apply": True,
                "force": force,
                "reason": "repair_failed",
                "repair_phase": repair_phase,
                "error_type": exc.__class__.__name__,
                "repair_required": True,
                "deleted": deleted,
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
        stored_budget_valid = (
            isinstance(stored_budget, dict)
            and stored_budget.get("ok") is True
            and stored_budget.get("violation_count") == 0
            and stored_budget.get("missing_document_id_violation_count", 0) == 0
        )
        repaired = (
            after_valid
            and after.get("ok") is True
            and after.get("census_complete") is True
            and after.get("cap_exceeded") is False
            and after.get("zero_chunk_count") == 0
            and after.get("oversized_document_count") == 0
            and after.get("oversized_chunk_count") == 0
            and after.get("missing_document_id_violation_count") == 0
            and post_counts_valid
            and post_graph_valid
            and stored_budget_valid
        )
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "repaired" if repaired else "reconciliation_invariants_remain",
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

        await self.cognee.cognify(datasets=repair_datasets, force=force)
        after = await self.cognee.corpus_zero_chunk_documents(dataset=dataset)
        after_zero_count = after.get("zero_chunk_count")
        repaired = (
            after.get("ok") is True
            and isinstance(after_zero_count, int)
            and not isinstance(after_zero_count, bool)
            and after_zero_count == 0
        )
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": force,
            "reason": "repaired" if repaired else "zero_chunk_documents_remain",
            "repair_required": not repaired,
            "repair_datasets": repair_datasets,
            "before": before,
            "after": after,
        }

    async def reconcile_oversized_chunks(
        self,
        *,
        dataset: str | None = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        maintenance = getattr(self.cognee, "maintenance", None)
        if apply and callable(maintenance):
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

        deleted = await self.cognee.delete_document_chunks(repair_ids)
        await self.cognee.cognify(datasets=repair_datasets, force=True)
        after = await self.cognee.corpus_oversized_chunk_documents(dataset=dataset)
        post_counts = await self.cognee.corpus_chunk_counts(repair_ids)
        post_graph_ids = await self.cognee.corpus_graph_presence(repair_ids)
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
            and post_indexed
        )
        return {
            "ok": repaired,
            "dataset": dataset,
            "apply": True,
            "force": True,
            "reason": "repaired" if repaired else "oversized_chunks_remain",
            "repair_required": not repaired,
            "repair_datasets": repair_datasets,
            "deleted": deleted,
            "before": before,
            "after": after,
            "post_repair_chunk_counts": post_counts,
            "post_repair_graph_documents": sorted(post_graph_ids or set()),
            "post_repair_indexed": post_indexed,
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
