from __future__ import annotations

import asyncio
from collections.abc import Iterable
from hashlib import sha256
import logging
import re
from uuid import uuid4
from typing import Any

from kb.cognee_client import CogneeGateway, CogneePublicClient
from kb.config import CitadelConfig
from kb.filters import PreIngestFilter
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
            logger.info(
                "Ingest rejected for dataset %s: %s", target_dataset, decision.reason
            )
            return IngestResult(False, decision.reason, target_dataset, merged_tags)

        self._guard_content(data, target_dataset)

        content_hash = sha256(data.encode("utf-8")).hexdigest()
        ingest_key = (target_dataset, content_hash)
        if ingest_key in self._seen_ingest_keys:
            logger.info(
                "Ingest rejected for dataset %s: duplicate_in_process", target_dataset
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
        return IngestResult(True, "accepted", target_dataset, merged_tags, result)

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
        the literal ``[DataItem]`` / session-scaffold blobs, and explicit
        session-cache node types. The classifier is anchored so real content is
        never matched; the default dry run returns every candidate id + preview so a
        human verifies before any deletion.
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
        for probe in ("[DataItem]", "COGNIFY_TEST_MARKER", "Session ID Question Answer"):
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


def _legacy_garbage_kind(node_id: Any, properties: Any) -> str | None:
    """Classify a graph node as legacy garbage to purge, or None to keep (#15).

    Conservative + anchored: only an exact COGNIFY_TEST_MARKER id, the literal
    [DataItem]/session-scaffold blob, or an explicit session-cache node type. Real
    content is never classified — there is no substring-of-prose match.
    """
    props = properties if isinstance(properties, dict) else {}
    for value in (props.get("text"), props.get("name"), props.get("title"), props.get("id"), node_id):
        if isinstance(value, str) and _MARKER_RE.fullmatch(value.strip()):
            return "marker"
    text = props.get("text")
    if isinstance(text, str) and _is_dataitem_garbage(text):
        return "dataitem"
    for key in ("type", "node_type", "category", "source"):
        value = props.get(key)
        if isinstance(value, str) and value.strip().lower() in _SESSION_CACHE_TYPES:
            return "session_cache"
    return None
