from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
import json
import logging
from pathlib import Path
from typing import Any

from kb.config import CitadelConfig
from kb.models import FeedbackResult, IngestResult
from kb.security_scan import redact_secrets

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, value: str) -> str:
    digest = sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


@dataclass
class MeshState:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=160))
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    revision: int = 0
    documents: int = 0
    searches: int = 0
    feedback_items: int = 0
    upgrades: int = 0
    errors: int = 0
    indexed_chunks: int = 0
    pending_chunks: int = 0
    failed_chunks: int = 0
    last_indexed_at: str | None = None
    _rehydrated: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=20)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    async def rehydrate(
        self,
        config: CitadelConfig,
        *,
        sources: list[dict[str, Any]] | None = None,
    ) -> None:
        """Seed the snapshot's source graph projection + ``last_indexed_at`` from
        persistent source state, once at startup.

        A web-service restart drops the in-memory mesh, so the dashboard graph looks
        wiped even though the persistent knowledge graph and source state survive.
        Rehydrate re-projects lightweight source/repository nodes (keyed by id, so a
        later live sync overwrites rather than duplicates) and seeds
        ``last_indexed_at`` from the cheap source-state JSON files.

        It deliberately does NOT seed the accumulating ``documents`` /
        ``indexed_chunks`` counters: github and repo-content syncs already flow
        through ``record_ingest`` (``documents += 1``), so seeding them would
        double-count the same source data after the next sync.

        Once-only (``_rehydrated`` guard) and best-effort: missing or unreadable
        state files never raise, so a fresh deploy with no state seeds nothing and
        startup proceeds.
        """
        async with self._lock:
            if self._rehydrated:
                return
            self._rehydrated = True
            try:
                self._ensure_base_graph(config)
                baselines = (
                    sources if sources is not None else self._read_source_baselines(config)
                )
                latest_ts: str | None = None
                for source in baselines:
                    ts = source.get("last_indexed_at")
                    if isinstance(ts, str) and ts and (latest_ts is None or ts > latest_ts):
                        latest_ts = ts
                    self._seed_source_nodes(config, source)
                if self.last_indexed_at is None and latest_ts is not None:
                    self.last_indexed_at = latest_ts
            except Exception as exc:  # noqa: BLE001 - never block startup on rehydrate
                logger.warning("Mesh rehydrate skipped: %s", exc)

    def _read_state_json(self, path: str | None) -> dict[str, Any]:
        if not path:
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_source_baselines(self, config: CitadelConfig) -> list[dict[str, Any]]:
        """Cheap one-time reads of the persistent source-state JSON files.

        Each baseline is ``{type, label, dataset, documents, last_indexed_at, repos}``.
        Only sources with data are returned; all reads are best-effort.
        """
        baselines: list[dict[str, Any]] = []

        github = self._read_state_json(config.github_sync_state_path)
        repos = github.get("repos") if isinstance(github.get("repos"), dict) else {}
        digest = github.get("last_digest")
        if isinstance(digest, str) and digest.strip():
            from kb.source_search import _digest_sections

            github_docs = len(_digest_sections(digest))
        else:
            github_docs = len(repos)
        if github_docs:
            baselines.append(
                {
                    "type": "github",
                    "label": f"GitHub / {github.get('org') or config.github_org}",
                    "dataset": config.github_sync_dataset,
                    "documents": github_docs,
                    "last_indexed_at": github.get("last_checked_at"),
                    "repos": list(repos.keys()),
                }
            )

        repo = self._read_state_json(config.repo_content_sync_state_path)
        files = repo.get("files") if isinstance(repo.get("files"), dict) else {}
        if files:
            baselines.append(
                {
                    "type": "repo_content",
                    "label": f"Repo content / {config.github_org}",
                    "dataset": config.repo_content_sync_dataset,
                    "documents": len(files),
                    "last_indexed_at": repo.get("last_checked_at"),
                    "repos": [],
                }
            )

        linear = self._read_state_json(config.linear_sync_state_path)
        issues = linear.get("issues") if isinstance(linear.get("issues"), list) else []
        if issues:
            baselines.append(
                {
                    "type": "linear",
                    "label": "Linear",
                    "dataset": config.linear_sync_dataset,
                    "documents": len(issues),
                    "last_indexed_at": linear.get("last_synced_at"),
                    "repos": [],
                }
            )

        return baselines

    def _seed_source_nodes(self, config: CitadelConfig, source: dict[str, Any]) -> None:
        """Re-project lightweight source/dataset/repository nodes from persisted state.

        Keeps the snapshot's graph 'records' projection non-empty after a restart. The
        authoritative knowledge graph survives separately via /api/mesh/graph (Kuzu),
        so this projection is intentionally decoupled from any cognee read.
        """
        dataset_id = self._dataset_node(source.get("dataset") or config.default_dataset)
        source_id = stable_id(
            "source", f"rehydrate:{source.get('type')}:{source.get('label')}"
        )
        self.nodes[source_id] = {
            "id": source_id,
            "label": source.get("label") or "Source",
            "type": "source",
            "status": "synced",
            "size": 46,
            "metadata": {
                "source_type": source.get("type"),
                "documents": source.get("documents"),
                "last_indexed_at": source.get("last_indexed_at"),
                "rehydrated": True,
            },
        }
        self._edge(source_id, dataset_id, "updates")
        for repo_name in (source.get("repos") or [])[:12]:
            if not isinstance(repo_name, str) or not repo_name:
                continue
            repo_id = stable_id("repository", repo_name)
            self.nodes[repo_id] = {
                "id": repo_id,
                "label": repo_name.split("/")[-1],
                "type": "repository",
                "status": "synced",
                "size": 26,
                "metadata": {"full_name": repo_name, "rehydrated": True},
            }
            self._edge(source_id, repo_id, "observed")
            self._edge(repo_id, dataset_id, "summarized")

    async def snapshot(self, config: CitadelConfig) -> dict[str, Any]:
        async with self._lock:
            self._ensure_base_graph(config)
            indexes = self._indexes(config)
            return {
                "revision": self.revision,
                "generated_at": utc_now(),
                "tenant_id": config.tenant_id,
                "default_dataset": config.default_dataset,
                "stats": {
                    "nodes": len(self.nodes),
                    "edges": len(self.edges),
                    "documents": self.documents,
                    "searches": self.searches,
                    "feedback": self.feedback_items,
                    "upgrades": self.upgrades,
                    "errors": self.errors,
                    "indexed_chunks": self.indexed_chunks,
                    "pending_chunks": self.pending_chunks,
                    "failed_chunks": self.failed_chunks,
                    "last_indexed_at": self.last_indexed_at,
                    "latest_event_id": self.revision,
                },
                "indexes": indexes,
                "nodes": list(self.nodes.values()),
                "edges": list(self.edges.values()),
                "events": list(self.events),
            }

    async def timeline(
        self,
        *,
        after_id: int | None = None,
        limit: int = 50,
        event_type: str | None = None,
        kind: str | None = None,
        visible: Callable[[Any], bool] | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, newest-first event timeline for resume/backfill reads.

        ``visible`` scopes events to the caller by ``details.dataset`` (ADR-0009).
        It runs BEFORE the limit slice so a caller still receives a full page of
        their own events rather than whatever survives filtering one page of
        everyone's. ``latest_event_id`` stays global on purpose: --watch resumes
        from it, and rewinding it to the last visible id would re-poll forever.
        """
        async with self._lock:
            events = list(self.events)
            if after_id is not None:
                events = [event for event in events if int(event["id"]) > after_id]
            if event_type:
                events = [event for event in events if event.get("type") == event_type]
            if kind:
                events = [
                    event
                    for event in events
                    if event.get("timeline", {}).get("kind") == kind
                ]
            if visible is not None:
                events = [
                    event
                    for event in events
                    if visible((event.get("details") or {}).get("dataset"))
                ]
            return {
                "generated_at": utc_now(),
                "latest_event_id": self.revision,
                "oldest_available_id": self.events[-1]["id"] if self.events else None,
                "limit": limit,
                "truncated": len(events) > limit,
                "stats": self._timeline_stats(),
                "events": events[:limit],
            }

    async def record_ingest(
        self,
        config: CitadelConfig,
        result: IngestResult,
        *,
        data: str,
        dataset: str,
        tags: list[str],
    ) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            dataset_id = self._dataset_node(dataset)
            if result.accepted:
                document_id = stable_id("document", f"{dataset}:{data}")
                label = data.strip().splitlines()[0][:80] or "Untitled memory"
                self.nodes[document_id] = {
                    "id": document_id,
                    "label": label,
                    "type": "document",
                    "status": "indexed",
                    "size": min(20 + len(data) // 40, 64),
                    "metadata": {
                        "dataset": dataset,
                        "tags": list(result.tags),
                        "characters": len(data),
                    },
                }
                self._edge(dataset_id, document_id, "contains")
                for tag in result.tags or tuple(tags):
                    tag_id = self._tag_node(tag)
                    self._edge(document_id, tag_id, "tagged")
                self._edge(document_id, "index:vector", "embedded")
                self._edge(document_id, "index:graph", "linked")
                self.documents += 1
                await self._record_event(
                    "ingest",
                    "Memory indexed",
                    {
                        "dataset": dataset,
                        "reason": result.reason,
                        "tags": list(result.tags),
                        "chunks": 1,
                    },
                )
            else:
                await self._record_event(
                    "reject",
                    "Memory rejected",
                    {"dataset": dataset, "reason": result.reason},
                )

    async def record_search(
        self,
        config: CitadelConfig,
        *,
        query: str,
        dataset: str,
        result_count: int,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            query_id = stable_id("query", f"{dataset}:{query}:{self.searches}")
            metadata: dict[str, Any] = {"dataset": dataset, "results": result_count}
            if telemetry:
                # Keep node metadata compact — full payload lives on the event.
                for key in ("search_id", "empty", "low_score", "timed_out", "tool_name"):
                    if key in telemetry:
                        metadata[key] = telemetry[key]
            self.nodes[query_id] = {
                "id": query_id,
                "label": query[:80],
                "type": "query",
                "status": "complete",
                "size": 24,
                "metadata": metadata,
            }
            self._edge(query_id, self._dataset_node(dataset), "searched")
            self._edge(query_id, "index:vector", "retrieved")
            self._edge(query_id, "index:graph", "traversed")
            self.searches += 1
            details: dict[str, Any] = {"dataset": dataset, "results": result_count}
            if telemetry:
                details["telemetry"] = telemetry
                if isinstance(telemetry.get("latency_ms"), (int, float)):
                    details["latency_ms"] = telemetry["latency_ms"]
            await self._record_event(
                "search",
                "Search completed",
                details,
            )

    async def record_search_telemetry(
        self,
        config: CitadelConfig,
        *,
        telemetry: dict[str, Any],
        dataset: str | None = None,
    ) -> str:
        """Write implicit search telemetry into the feedback index (non-Cognee).

        Returns the feedback node id. Callers must treat failures as non-fatal.
        """
        async with self._lock:
            self._ensure_base_graph(config)
            target_dataset = (
                dataset
                or telemetry.get("primary_dataset")
                or config.default_dataset
            )
            search_id = str(telemetry.get("search_id") or f"search:{self.feedback_items}")
            feedback_id = stable_id(
                "feedback", f"search_telemetry:{target_dataset}:{search_id}:{self.feedback_items}"
            )
            self.nodes[feedback_id] = {
                "id": feedback_id,
                "label": f"Search {search_id[-8:]}",
                "type": "feedback",
                "status": "telemetry",
                "size": 18,
                "metadata": {
                    "dataset": target_dataset,
                    "kind": "search_telemetry",
                    "search_id": search_id,
                    "result_count": telemetry.get("result_count"),
                    "empty": telemetry.get("empty"),
                    "low_score": telemetry.get("low_score"),
                    "tool_name": telemetry.get("tool_name"),
                    "latency_ms": telemetry.get("latency_ms"),
                },
            }
            self._edge(feedback_id, "index:feedback", "updates")
            self._edge(feedback_id, self._dataset_node(str(target_dataset)), "teaches")
            self.feedback_items += 1
            await self._record_event(
                "feedback",
                "Search telemetry recorded",
                {
                    "dataset": target_dataset,
                    "qa_id": search_id,
                    "kind": "search_telemetry",
                    "improved": False,
                    "telemetry": telemetry,
                    "results": telemetry.get("result_count"),
                },
            )
            return feedback_id

    async def record_feedback(
        self,
        config: CitadelConfig,
        *,
        qa_id: str,
        dataset: str,
        result: FeedbackResult,
    ) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            feedback_id = stable_id("feedback", f"{dataset}:{qa_id}:{self.feedback_items}")
            self.nodes[feedback_id] = {
                "id": feedback_id,
                "label": f"Feedback {qa_id[:8]}",
                "type": "feedback",
                "status": "improved" if result.improved else "recorded",
                "size": 22,
                "metadata": {
                    "dataset": dataset,
                    "qa_id": qa_id,
                    "kind": "explicit",
                    "improved": result.improved,
                },
            }
            self._edge(feedback_id, "index:feedback", "updates")
            self._edge(feedback_id, self._dataset_node(dataset), "teaches")
            self.feedback_items += 1
            await self._record_event(
                "feedback",
                "Feedback recorded",
                {
                    "dataset": dataset,
                    "qa_id": qa_id,
                    "kind": "explicit",
                    "improved": result.improved,
                },
            )

    async def record_upgrade(
        self,
        config: CitadelConfig,
        *,
        dataset: str,
        session_ids: list[str] | None,
    ) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            upgrade_id = stable_id("upgrade", f"{dataset}:{self.upgrades}:{utc_now()}")
            self.nodes[upgrade_id] = {
                "id": upgrade_id,
                "label": "Self upgrade",
                "type": "upgrade",
                "status": "complete",
                "size": 30,
                "metadata": {"dataset": dataset, "sessions": session_ids or []},
            }
            self._edge(upgrade_id, self._dataset_node(dataset), "improves")
            self._edge(upgrade_id, "index:feedback", "reads")
            self._edge(upgrade_id, "index:global", "refreshes")
            self.upgrades += 1
            await self._record_event(
                "upgrade",
                "Self upgrade completed",
                {"dataset": dataset, "sessions": session_ids or []},
            )

    async def record_github_sync(self, config: CitadelConfig, result: dict[str, Any]) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            dataset_id = self._dataset_node(result.get("org") or config.github_sync_dataset)
            source_id = stable_id("source", result.get("source_url") or config.github_org)
            self.nodes[source_id] = {
                "id": source_id,
                "label": f"GitHub / {result.get('org') or config.github_org}",
                "type": "source",
                "status": "synced",
                "size": 46,
                "metadata": {
                    "url": result.get("source_url"),
                    "checked_at": result.get("checked_at"),
                    "repos_scanned": result.get("repos_scanned"),
                },
            }
            self._edge(source_id, dataset_id, "updates")

            for repo in result.get("changed_repositories", [])[:12]:
                repo_id = stable_id("repository", repo.get("full_name") or repo.get("name") or "")
                self.nodes[repo_id] = {
                    "id": repo_id,
                    "label": repo.get("name") or repo.get("full_name") or "Repository",
                    "type": "repository",
                    "status": "changed",
                    "size": 26,
                    "metadata": repo,
                }
                self._edge(source_id, repo_id, "observed")
                self._edge(repo_id, dataset_id, "summarized")

            await self._record_event(
                "github_sync",
                "GitHub sync completed",
                {
                    "org": result.get("org"),
                    "dataset": result.get("dataset") or result.get("org"),
                    "repos": result.get("repos_scanned"),
                    "changed": result.get("changed_count"),
                    "events": result.get("event_count"),
                    "ingested": result.get("ingested"),
                    "improved": result.get("improved"),
                },
            )

    async def record_repo_content_sync(self, config: CitadelConfig, result: dict[str, Any]) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            dataset_id = self._dataset_node(
                result.get("dataset") or config.repo_content_sync_dataset
            )
            source_id = stable_id("source", f"github-repo-content:{result.get('org') or config.github_org}")
            self.nodes[source_id] = {
                "id": source_id,
                "label": f"Repo content / {result.get('org') or config.github_org}",
                "type": "source",
                "status": "synced",
                "size": 48,
                "metadata": {
                    "source_type": "github_repo_content",
                    "checked_at": result.get("checked_at"),
                    "repos_scanned": result.get("repos_scanned"),
                    "files_ingested": result.get("files_ingested"),
                },
            }
            self._edge(source_id, dataset_id, "cognifies")

            for repo_result in result.get("repositories", [])[:12]:
                if not isinstance(repo_result, dict):
                    continue
                repo_name = repo_result.get("repo")
                if not repo_name:
                    continue
                repo_id = stable_id("repository", repo_name)
                self.nodes[repo_id] = {
                    "id": repo_id,
                    "label": repo_name.split("/")[-1],
                    "type": "repository",
                    "status": "indexed" if repo_result.get("ingested") else "ready",
                    "size": 28,
                    "metadata": repo_result,
                }
                self._edge(source_id, repo_id, "ingested")
                self._edge(repo_id, dataset_id, "documents")

            await self._record_event(
                "repo_content_sync",
                "Repository content sync completed",
                {
                    "org": result.get("org"),
                    "dataset": config.repo_content_sync_dataset,
                    "repos": result.get("repos_scanned"),
                    "files_ingested": result.get("files_ingested"),
                    "files_skipped": result.get("files_skipped"),
                    "improved": result.get("improved"),
                },
            )

    async def record_obsidian_sync(
        self,
        config: CitadelConfig,
        *,
        vault: dict[str, Any],
        result: dict[str, Any],
        dataset: str | None = None,
    ) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            dataset_id = self._dataset_node(dataset or config.default_dataset)
            source_id = stable_id("source", f"obsidian:{vault.get('id')}")
            self.nodes[source_id] = {
                "id": source_id,
                "label": f"Obsidian / {vault.get('name') or 'Vault'}",
                "type": "source",
                "status": "conflict" if result.get("conflicts") else "synced",
                "size": 44,
                "metadata": {
                    "source_type": "obsidian_vault",
                    "vault_id": vault.get("id"),
                    "team_id": vault.get("team_id"),
                    "last_push_at": vault.get("last_push_at"),
                    "accepted": len(result.get("accepted", [])),
                    "skipped": len(result.get("skipped", [])),
                    "conflicts": len(result.get("conflicts", [])),
                },
            }
            self._edge(source_id, dataset_id, "updates")

            for document in result.get("accepted", [])[:12]:
                document_id = stable_id(
                    "document",
                    f"obsidian:{vault.get('id')}:{document.get('path')}",
                )
                self.nodes[document_id] = {
                    "id": document_id,
                    "label": document.get("path") or "Obsidian note",
                    "type": "document",
                    "status": "deleted" if document.get("deleted") else "indexed",
                    "size": 24,
                    "metadata": {
                        "source_type": "obsidian_vault",
                        "vault_id": vault.get("id"),
                        "rev": document.get("rev"),
                        "content_hash": document.get("content_hash"),
                    },
                }
                self._edge(source_id, document_id, "synced")
                self._edge(document_id, dataset_id, "indexed")

            await self._record_event(
                "obsidian_sync",
                "Obsidian vault sync received",
                {
                    "vault_id": vault.get("id"),
                    "vault_name": vault.get("name"),
                    "dataset": dataset or config.default_dataset,
                    "accepted": len(result.get("accepted", [])),
                    "skipped": len(result.get("skipped", [])),
                    "conflicts": len(result.get("conflicts", [])),
                },
            )

    async def record_enrichment(
        self,
        config: CitadelConfig,
        *,
        dataset: str,
        chunks: int,
        used_llm: bool,
        reason: str,
        model: str | None = None,
    ) -> None:
        """Surface an LLM enrichment pass (or its fallback) in the activity stream."""
        async with self._lock:
            self._ensure_base_graph(config)
            await self._record_event(
                "enrichment",
                "Source material enriched",
                {
                    "dataset": dataset,
                    "chunks": chunks,
                    "used_llm": used_llm,
                    "reason": reason,
                    "model": model,
                },
            )

    async def record_optimization(
        self,
        config: CitadelConfig,
        *,
        dataset: str,
        reviewed: int,
        optimized: int,
        used_llm: bool,
        dry_run: bool,
    ) -> None:
        """Surface a self-improvement pass in the activity stream."""
        async with self._lock:
            self._ensure_base_graph(config)
            await self._record_event(
                "optimization",
                "Self-improvement pass completed",
                {
                    "dataset": dataset,
                    "reviewed": reviewed,
                    "optimized": optimized,
                    "used_llm": used_llm,
                    "dry_run": dry_run,
                },
            )

    async def record_conflict(self, config: CitadelConfig, *, conflict: dict[str, Any]) -> None:
        """Surface a detected Knowledge Conflict in the activity stream."""
        async with self._lock:
            self._ensure_base_graph(config)
            await self._record_event(
                "conflict",
                "Knowledge conflict detected",
                {
                    "conflict_id": conflict.get("id"),
                    "kind": conflict.get("kind"),
                    "status": conflict.get("status"),
                    "summary": str(conflict.get("summary") or "")[:280],
                },
            )

    async def record_error(self, config: CitadelConfig, *, operation: str, error: str) -> None:
        async with self._lock:
            self._ensure_base_graph(config)
            self.errors += 1
            logger.error("Mesh recorded %s failure: %s", operation, redact_secrets(error[:280]))
            await self._record_event(
                "error",
                "Operation failed",
                {"operation": operation, "error": redact_secrets(error[:280])},
            )

    def _ensure_base_graph(self, config: CitadelConfig) -> None:
        central = config.github_sync_dataset
        if config.default_dataset != central:
            self._dataset_node(config.default_dataset)
        dataset_id = self._dataset_node(central)
        for index_id, label, description in [
            ("index:graph", "Graph mesh", "Entity and relationship store"),
            ("index:vector", "Vector index", "Embedding retrieval store"),
            ("index:feedback", "Feedback memory", "Session feedback and memify input"),
            ("index:global", "Global context", "Cross-session improvement layer"),
        ]:
            self.nodes[index_id] = {
                "id": index_id,
                "label": label,
                "type": "index",
                "status": "active",
                "size": 40,
                "metadata": {"description": description},
            }
            self._edge(dataset_id, index_id, "uses")

    def _dataset_node(self, dataset: str) -> str:
        node_id = stable_id("dataset", dataset)
        self.nodes[node_id] = {
            "id": node_id,
            "label": dataset,
            "type": "dataset",
            "status": "active",
            "size": 52,
            "metadata": {"dataset": dataset},
        }
        return node_id

    def _tag_node(self, tag: str) -> str:
        node_id = stable_id("tag", tag)
        self.nodes[node_id] = {
            "id": node_id,
            "label": tag,
            "type": "tag",
            "status": "active",
            "size": 20,
            "metadata": {"tag": tag},
        }
        return node_id

    def _edge(self, source: str, target: str, label: str) -> None:
        edge_id = f"{source}->{target}:{label}"
        self.edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "label": label,
        }

    def _indexes(self, config: CitadelConfig) -> list[dict[str, Any]]:
        return [
            {
                "id": "graph",
                "name": "Graph mesh",
                "status": "active",
                "records": len([node for node in self.nodes.values() if node["type"] != "index"]),
                "updated_at": self.events[0]["created_at"] if self.events else None,
            },
            {
                "id": "vector",
                "name": "Vector index",
                "status": "active",
                "records": self.documents,
                "updated_at": self.events[0]["created_at"] if self.events else None,
            },
            {
                "id": "feedback",
                "name": "Feedback memory",
                "status": "active",
                "records": self.feedback_items,
                "updated_at": self.events[0]["created_at"] if self.events else None,
            },
            {
                "id": "global",
                "name": "Global context",
                "status": "enabled" if config.build_global_context_index else "standby",
                "records": self.upgrades,
                "updated_at": self.events[0]["created_at"] if self.events else None,
            },
        ]

    async def _record_event(self, event_type: str, message: str, details: dict[str, Any]) -> None:
        self.revision += 1
        created_at = utc_now()
        safe_details = self._redact_details(details)
        timeline = self._timeline_envelope(event_type, safe_details)
        self._apply_timeline_stats(timeline, created_at)
        event = {
            "id": self.revision,
            "type": event_type,
            "message": message,
            "details": safe_details,
            "timeline": timeline,
            "created_at": created_at,
        }
        self.events.appendleft(event)
        self._publish(event)

    def _timeline_stats(self) -> dict[str, Any]:
        return {
            "indexed_chunks": self.indexed_chunks,
            "pending_chunks": self.pending_chunks,
            "failed_chunks": self.failed_chunks,
            "last_indexed_at": self.last_indexed_at,
            "latest_event_id": self.revision,
        }

    def _apply_timeline_stats(self, timeline: dict[str, Any], created_at: str) -> None:
        if timeline["kind"] == "chunk_indexed":
            chunks = timeline["metrics"].get("chunks")
            self.indexed_chunks += max(int(chunks or 1), 0)
            self.last_indexed_at = created_at
        if timeline["status"] == "failed":
            self.failed_chunks += 1

    def _timeline_envelope(self, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        profiles = {
            "ingest": ("chunk_indexed", "indexed", "manual_ingest"),
            "reject": ("chunk_rejected", "rejected", "manual_ingest"),
            "search": ("retrieval_served", "searched", "search"),
            "feedback": ("feedback_recorded", "recorded", "feedback"),
            "upgrade": ("agent_action", "completed", "self_upgrade"),
            "github_sync": ("source_synced", "synced", "github"),
            "obsidian_sync": ("source_synced", "synced", "obsidian"),
            "enrichment": ("chunk_indexed", "indexed", "enrichment"),
            "optimization": ("agent_action", "completed", "self_improvement"),
            "conflict": ("conflict_detected", "detected", "conflict_detector"),
            "error": ("pipeline_error", "failed", "runtime"),
        }
        kind, status, source = profiles.get(event_type, ("agent_action", "recorded", "runtime"))
        return {
            "kind": kind,
            "status": status,
            "dataset": details.get("dataset") or details.get("org") or details.get("vault_id"),
            "source": details.get("source") or details.get("operation") or source,
            "metrics": self._timeline_metrics(details),
        }

    def _timeline_metrics(self, details: dict[str, Any]) -> dict[str, int | float]:
        metric_keys = {
            "chunks",
            "results",
            "repos",
            "changed",
            "events",
            "accepted",
            "skipped",
            "conflicts",
            "reviewed",
            "optimized",
            "latency_ms",
        }
        metrics: dict[str, int | float] = {}
        for key in metric_keys:
            value = details.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key] = value
        return metrics

    def _redact_details(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_secrets(value)
        if isinstance(value, dict):
            return {key: self._redact_details(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_details(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact_details(item) for item in value]
        return value

    def _publish(self, event: dict[str, Any]) -> None:
        dead_queues: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self.subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead_queues.append(queue)
        for queue in dead_queues:
            self.unsubscribe(queue)
