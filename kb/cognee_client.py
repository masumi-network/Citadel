from __future__ import annotations

import asyncio
import copy
import contextlib
import contextvars
from hashlib import md5
import json
import logging
import os
import secrets
from collections.abc import Awaitable, AsyncIterator, Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_OID, UUID, uuid5

from kb import chunk_window
from kb.cognify_queue import CognifyLease, CognifyRetryQueue
from kb.logging_utils import configure_cognee_logging

logger = logging.getLogger(__name__)

# Strong refs to detached background cognify tasks so the loop does not GC them
# mid-flight (and so they can be awaited/observed in tests).
_BACKGROUND_COGNIFY_TASKS: set[Any] = set()
DEFAULT_COGNIFY_QUEUE_PATH = Path(".citadel/cognify_queue.json")
COGNIFY_EXECUTION_LOCK_POLL_SECONDS = 1.0

def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


DEFAULT_CORPUS_HEALTH_MAX_DOCUMENTS = 10_000
MAX_ZERO_CHUNK_REPORT_DOCUMENTS = 1_000
MAX_OVERSIZED_CHUNK_REPORT_DOCUMENTS = 1_000
MAX_REPAIR_SNAPSHOTS = 32


def _corpus_health_max_documents() -> int:
    raw = os.getenv(
        "CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS",
        str(DEFAULT_CORPUS_HEALTH_MAX_DOCUMENTS),
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS must be a positive integer"
        ) from exc
    if value < 1:
        raise RuntimeError(
            "CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS must be a positive integer"
        )
    return value


def _cognify_data_ids(result: Any) -> list[str]:
    """Extract source ``Data.id`` values from a blocking cognify result.

    Cognee returns one ``PipelineRunInfo`` per dataset. Its
    ``data_ingestion_info`` entries are mappings containing ``data_id``. Keep the
    parser tolerant of the single-result and list-shaped variants so the stored
    chunk guard remains scoped to the pass that just ran.
    """
    if isinstance(result, Mapping):
        runs = list(result.values())
    elif isinstance(result, (list, tuple)):
        runs = list(result)
    else:
        runs = [result]

    data_ids: list[str] = []
    for run in runs:
        info = getattr(run, "data_ingestion_info", None)
        if info is None and isinstance(run, Mapping):
            info = run.get("data_ingestion_info")
        if not isinstance(info, (list, tuple)):
            continue
        for item in info:
            data_id = item.get("data_id") if isinstance(item, Mapping) else getattr(item, "data_id", None)
            if data_id is not None:
                data_ids.append(str(data_id))
    return list(dict.fromkeys(data_ids))


def _cognify_data_ids_by_dataset(
    result: Any,
    datasets: list[str],
) -> dict[str, list[str]]:
    """Bind every processed source ID to the dataset attested by Cognee."""
    if isinstance(result, Mapping):
        return {
            str(dataset_id): data_ids
            for dataset_id, run in result.items()
            if (data_ids := _cognify_data_ids([run]))
        }
    data_ids = _cognify_data_ids(result)
    if not data_ids:
        return {}
    if len(datasets) != 1:
        raise RuntimeError(
            "multi-dataset cognify receipt does not bind processed source IDs to datasets"
        )
    return {str(datasets[0]): data_ids}


def _utc_datetime(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Postgres returns ``data.created_at`` timezone-aware; sqlite (the test and
    default local store) drops the offset and returns it naive. Every write
    path stamps UTC, so pinning naive values to UTC keeps cursor comparisons
    from mixing naive and aware datetimes (which raises) or shifting rows by a
    local offset.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _isoformat_utc(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    return _utc_datetime(value).isoformat()


def _parse_citadel_tags(external_metadata: Any) -> list[str]:
    """Our tags out of cognee's external_metadata, [] when absent or malformed.

    cognee itself stores this column as a dict but tolerates JSON strings on
    read (``parse_external_metadata``), so accept both. Display-only: a row
    whose metadata does not parse still enumerates, just without tags.
    """
    metadata = external_metadata
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return []
    if not isinstance(metadata, dict):
        return []
    tags = metadata.get("citadel_tags")
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags]


# Dataset-attribution tuning (#50): /api/mesh/graph calls node_dataset_map on
# every poll, so successful results are cached for the TTL, and the relational
# read is time-bounded so a non-erroring outage (TCP blackhole, saturated pool)
# degrades to "no attribution" instead of stalling the endpoint. All three are
# env-overridable so a live node can be tuned without a redeploy.
NODE_DATASET_MAP_TTL_SECONDS = _float_env("CITADEL_NODE_DATASET_MAP_TTL_SECONDS", 60.0)
NODE_DATASET_MAP_TIMEOUT_SECONDS = _float_env(
    "CITADEL_NODE_DATASET_MAP_TIMEOUT_SECONDS", 5.0
)
# A failed/timed-out read is cached only briefly (NOT the full content TTL): a
# transient stall must re-read quickly instead of latching a 60s content
# blackout for every scoped caller (#50). On failure we also prefer the last
# known-good mapping over {} (stale-while-error) so a stall degrades to stale
# attribution, never to an empty vault.
NODE_DATASET_MAP_FAILURE_TTL_SECONDS = _float_env(
    "CITADEL_NODE_DATASET_MAP_FAILURE_TTL_SECONDS", 5.0
)
# The whole-graph Kuzu read is expensive (5,382 nodes / 33,859 edges, ~0.3s+)
# and is now front-loaded on every dashboard open. TTL-cache the RAW read so N
# concurrent /api/mesh/graph opens collapse to one Kuzu read + one format pass
# (#28/#50). Shaping still runs per caller (off-loop). Env-overridable.
GRAPH_DATA_CACHE_TTL_SECONDS = _float_env("CITADEL_GRAPH_DATA_CACHE_TTL_SECONDS", 30.0)


def assert_cognee_dataset_api() -> None:
    """Import the cognee private symbols dataset attribution depends on.

    ``_read_node_dataset_map`` reads cognee internals (``cognee.modules.data``
    models + ``get_default_user``) that are NOT part of cognee's public API and
    could move in any 1.x release. An ImportError there is swallowed by the
    best-effort ``node_dataset_map`` guard and silently blanks every scoped
    caller's vault (fail-closed). Call this at boot so a cognee version bump
    surfaces as a loud error instead of a silent content blackout; a test also
    calls it so the bump fails CI, not prod. Raises on any missing symbol.
    """
    from cognee.infrastructure.databases.relational import (  # noqa: F401
        get_relational_engine,
    )
    from cognee.modules.data.models import Dataset, DatasetData  # noqa: F401
    from cognee.modules.users.methods import get_default_user  # noqa: F401

    # Symbol imports alone were not enough. `cognee.add()` returns a
    # PipelineRunInfo model whose `data_ingestion_info` is an ATTRIBUTE;
    # `_cognee_data_ids` read it as a dict key, always got nothing, and left the
    # repo-content sync unable to converge. Nothing failed loudly because the
    # shape is only ever read best-effort. Pin the field's EXISTENCE here so a
    # cognee bump that renames or moves it fails at boot and in CI rather than
    # degrading into a silent livelock.
    from cognee.modules.pipelines.models.PipelineRunInfo import (
        PipelineRunCompleted,
        PipelineRunInfo,
    )

    for model in (PipelineRunInfo, PipelineRunCompleted):
        if "data_ingestion_info" not in getattr(model, "model_fields", {}):
            raise RuntimeError(
                f"cognee {model.__name__} no longer declares data_ingestion_info; "
                "kb.repo_content_sync._cognee_data_ids needs updating"
            )

    # The corpus census (corpus_page / corpus_totals / corpus_chunk_counts /
    # corpus_graph_presence) leans on more private surface than dataset
    # attribution does: specific Data/Dataset/DatasetData columns, the active
    # vector adapter's administrative methods, and the graph adapter's raw query
    # surface. Pin each one so a cognee bump that moves any of them fails loudly
    # at boot and in CI instead of quietly breaking the census.
    from cognee.infrastructure.databases.graph.ladybug.adapter import LadybugAdapter
    from cognee.infrastructure.databases.vector import get_vector_engine  # noqa: F401
    from cognee.modules.data.models import Data

    for model, required in (
        (
            Data,
            {
                "id",
                "name",
                "content_hash",
                "raw_content_hash",
                "mime_type",
                "external_metadata",
                "token_count",
                "data_size",
                "created_at",
                "updated_at",
                "owner_id",
            },
        ),
        (Dataset, {"id", "name", "owner_id"}),
        (DatasetData, {"dataset_id", "data_id"}),
    ):
        missing = required - set(model.__table__.c.keys())
        if missing:
            raise RuntimeError(
                f"cognee {model.__name__} table no longer carries {sorted(missing)}; "
                "the kb.cognee_client corpus census needs updating"
            )
    adapter_methods: list[tuple[type[Any], str]] = [(LadybugAdapter, "query")]
    vector_provider = os.getenv("VECTOR_DB_PROVIDER", "").strip().lower()
    if vector_provider == "pgvector":
        from cognee.infrastructure.databases.vector.pgvector.PGVectorAdapter import (
            PGVectorAdapter,
        )

        adapter_methods.extend(
            (
                (PGVectorAdapter, "get_table"),
                (PGVectorAdapter, "get_async_session"),
                (PGVectorAdapter, "delete_data_points"),
            )
        )
    elif vector_provider == "qdrant":
        from .qdrant_adapter import CitadelQdrantAdapter

        adapter_methods.extend(
            (CitadelQdrantAdapter, method_name)
            for method_name in (
                "retrieve",
                "delete_data_points",
                "count_data_points",
                "scroll_data_points",
                "prune",
            )
        )

    for adapter, method_name in adapter_methods:
        if not callable(getattr(adapter, method_name, None)):
            raise RuntimeError(
                f"cognee {adapter.__name__} no longer exposes {method_name}; "
                "the kb.cognee_client corpus census needs updating"
            )


_SUPPRESS_INLINE_COGNIFY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "citadel_suppress_inline_cognify", default=False
)
_COGNEE_MAINTENANCE_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "citadel_cognee_maintenance_held", default=False
)


@contextlib.contextmanager
def suppress_inline_cognify() -> Iterator[None]:
    """Make this task tree ADD only, without touching the rest of the process.

    The evolve Phase 1 used to be a subprocess carrying
    CITADEL_SUPPRESS_INLINE_COGNIFY=true in its env. Now that it runs inside the
    web process (#88), an env var would be the wrong tool: it is process-wide, so
    a teammate's ingest arriving during the ~30s pass would silently go add-only
    too. A context variable is scoped to this task tree, and asyncio propagates
    it into child tasks and ``asyncio.to_thread`` calls, which is exactly the
    reach the stages need and no further.
    """
    token = _SUPPRESS_INLINE_COGNIFY.set(True)
    try:
        yield
    finally:
        _SUPPRESS_INLINE_COGNIFY.reset(token)


def _suppress_inline_cognify() -> bool:
    """True when this caller must ADD only and never cognify (Kuzu write).

    Set by :func:`suppress_inline_cognify` around the in-loop evolve Phase 1, or
    process-wide by CITADEL_SUPPRESS_INLINE_COGNIFY for the standalone evolve
    entrypoint. Either way the web cognifies in Phase 2 as the sole writer (#47).
    """
    if _SUPPRESS_INLINE_COGNIFY.get():
        return True
    return os.getenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _session_recall_enabled() -> bool:
    """Whether to read cognee's per-session QA cache before the durable store.

    The session cache is the deprecated, pre-#54 corrupt path: it stored writes as
    the literal "[DataItem]" placeholder and is no longer written to (durable
    writes go to the chunk/vector store). Reading it first only resurfaces that
    stale garbage in search/linear_search (#15/#52/#26), so it is OFF by default.
    """
    return os.getenv("CITADEL_COGNEE_SESSION_RECALL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _search_timing_enabled() -> bool:
    """Whether to log a per-search wall-time breakdown (#50, node profiling).

    Off by default; set ``CITADEL_SEARCH_TIMING=true`` to emit setup vs recall vs
    total elapsed ms per search at INFO so the ~6-9s node latency can be attributed
    on the live node later. Embedding + vector recall + cognee's per-read history
    writes all happen INSIDE the single ``cognee.search`` call, so they are lumped
    into the ``recall`` bucket — splitting them further needs cognee-internal
    instrumentation, not something the client boundary can see.
    """
    return os.getenv("CITADEL_SEARCH_TIMING", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class CogneeGateway(Protocol):
    async def remember(
        self,
        data: Any,
        *,
        dataset_name: str,
        session_id: str | None = None,
        tags: tuple[str, ...] = (),
        attestation: Mapping[str, str] | None = None,
        defer_cognify: bool = False,
        data_id: str | None = None,
    ) -> Any:
        raise NotImplementedError

    def schedule_cognify(self, datasets: list[str]) -> bool:
        raise NotImplementedError

    async def recall(
        self,
        query: str,
        *,
        dataset: str,
        session_id: str | None = None,
        top_k: int = 10,
        document_ids: list[str] | None = None,
    ) -> list[Any]:
        raise NotImplementedError

    async def add_feedback(
        self,
        *,
        session_id: str,
        qa_id: str,
        score: int | None,
        text: str | None,
    ) -> bool:
        raise NotImplementedError

    async def improve(
        self,
        *,
        dataset: str,
        session_ids: list[str] | None = None,
        build_global_context_index: bool = False,
    ) -> Any:
        raise NotImplementedError

    async def cognify(self, *, datasets: list[str], force: bool = False) -> Any:
        raise NotImplementedError

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]:
        raise NotImplementedError

    def maintenance(self) -> AsyncIterator[None]:
        raise NotImplementedError

    async def get_document(
        self, document_id: str, *, chunk_scope: bool = False
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        raise NotImplementedError

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        raise NotImplementedError

    async def corpus_health(self, *, limit: int = 64) -> dict[str, Any]:
        raise NotImplementedError

    async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int] | None:
        raise NotImplementedError

    async def source_manifest_for_documents(
        self, document_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        raise NotImplementedError

    async def dataset_membership_for_documents(
        self, document_ids: list[str]
    ) -> dict[str, list[str]]:
        raise NotImplementedError

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str] | None:
        raise NotImplementedError

    async def corpus_zero_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def corpus_oversized_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def corpus_reconciliation_census(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def delete_graph_nodes(self, node_ids: list[str]) -> int:
        raise NotImplementedError

    async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    async def restore_document_chunks(self, deleted: Mapping[str, Any]) -> bool:
        raise NotImplementedError

    async def discard_document_chunk_snapshot(
        self, deleted: Mapping[str, Any]
    ) -> bool:
        raise NotImplementedError


class CogneePublicClient:
    def __init__(
        self,
        *,
        queue_path: str | Path | None = None,
        retry_queue: CognifyRetryQueue | None = None,
    ) -> None:
        self._startup_migrations_done = False
        # Wired by the service layer when a lifecycle store exists: maps a list
        # of cognee document ids (== lifecycle source_revision_ids for drain-
        # projected docs) to the subset whose projection is still in flight, so
        # the post-cognify stored check does not fail on chunks the drain has
        # not written yet (#286). None (no lifecycle) keeps the check fully
        # fail-closed.
        self.lifecycle_active_projection_lookup: (
            Callable[[list[str]], set[str]] | None
        ) = None
        configured_queue_path = queue_path or os.getenv("CITADEL_COGNIFY_QUEUE_PATH")
        self.cognify_queue = retry_queue or CognifyRetryQueue(
            configured_queue_path or DEFAULT_COGNIFY_QUEUE_PATH
        )
        self._cognify_queue_task: asyncio.Task[Any] | None = None
        self._cognify_queue_retry_handle: asyncio.TimerHandle | None = None
        # Serializes graph writes within this process — Kuzu is a single-writer
        # embedded DB, so two overlapping cognify calls (an inline ingest cognify,
        # the evolve scheduler, /api/cognify/run) must not collide (#47). One client
        # per Citadel; the app uses a single Citadel singleton, so this is the
        # process-wide writer gate. The evolve scheduler also holds it across its
        # Phase-1 subprocess so the web never cognifies while the subprocess owns
        # the on-disk Kuzu lock.
        self.writer_lock = asyncio.Lock()
        # Serializes corpus repair with every regular cognify call in this
        # process. The repair context keeps this lock across census, deletion,
        # rebuild, and post-repair verification.
        self.maintenance_lock = asyncio.Lock()
        # node→dataset attribution cache: (monotonic_ts, mapping, ok). ``ok``
        # distinguishes a fresh successful read (full TTL) from a cached
        # failure (short failure TTL), and the last successful mapping is kept
        # separately so a stall serves stale attribution instead of blanking
        # the vault (#50). Single-flight lock collapses a cold-cache burst to
        # one relational read. One client per Citadel singleton, so all of this
        # is effectively process-wide.
        self._node_dataset_cache: tuple[float, dict[str, list[str]], bool] | None = None
        self._node_dataset_last_good: dict[str, list[str]] | None = None
        self._node_dataset_lock = asyncio.Lock()
        # Raw whole-graph read cache: (monotonic_ts, (nodes, edges)) with a
        # single-flight lock so a burst of dashboard opens shares one Kuzu read.
        self._graph_data_cache: tuple[float, tuple[list[Any], list[Any]]] | None = None
        self._graph_data_lock = asyncio.Lock()
        # Repair snapshots stay process-local and are addressed by an opaque,
        # single-use token returned from delete_document_chunks. Keeping the
        # snapshot out of the result prevents payload/text from crossing the
        # service boundary while preserving exact rollback data for the caller.
        self._repair_snapshots: dict[str, dict[str, Any]] = {}

    def _copy_env_if_missing(self, target: str, *sources: str) -> None:
        if os.getenv(target):
            return
        for source in sources:
            value = os.getenv(source)
            if value:
                os.environ[target] = value
                return

    def _derive_db_env_from_database_url(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            return
        parsed = urlparse(database_url)
        if parsed.scheme not in {"postgres", "postgresql"}:
            return

        os.environ.setdefault("DB_PROVIDER", "postgres")
        if parsed.hostname:
            os.environ.setdefault("DB_HOST", parsed.hostname)
        if parsed.port:
            os.environ.setdefault("DB_PORT", str(parsed.port))
        if parsed.path and parsed.path != "/":
            os.environ.setdefault("DB_NAME", unquote(parsed.path.lstrip("/")))
        if parsed.username:
            os.environ.setdefault("DB_USERNAME", unquote(parsed.username))
        if parsed.password:
            os.environ.setdefault("DB_PASSWORD", unquote(parsed.password))

    def _ensure_cognee_database_env(self) -> None:
        self._derive_db_env_from_database_url()
        if os.getenv("VECTOR_DB_PROVIDER", "").lower() == "pgvector":
            self._copy_env_if_missing("VECTOR_DB_HOST", "DB_HOST")
            self._copy_env_if_missing("VECTOR_DB_PORT", "DB_PORT")
            self._copy_env_if_missing("VECTOR_DB_NAME", "DB_NAME")
            self._copy_env_if_missing("VECTOR_DB_USERNAME", "DB_USERNAME")
            self._copy_env_if_missing("VECTOR_DB_PASSWORD", "DB_PASSWORD")

        if os.getenv("GRAPH_DATABASE_PROVIDER", "").lower() == "postgres":
            self._copy_env_if_missing("GRAPH_DATABASE_HOST", "DB_HOST")
            self._copy_env_if_missing("GRAPH_DATABASE_PORT", "DB_PORT")
            self._copy_env_if_missing("GRAPH_DATABASE_NAME", "DB_NAME")
            self._copy_env_if_missing("GRAPH_DATABASE_USERNAME", "DB_USERNAME")
            self._copy_env_if_missing("GRAPH_DATABASE_PASSWORD", "DB_PASSWORD")

    def _ensure_llm_api_key(self) -> None:
        if not os.getenv("LLM_API_KEY") and os.getenv("OPENROUTER_API_KEY"):
            os.environ["LLM_API_KEY"] = os.environ["OPENROUTER_API_KEY"]

    def _prepare_cognee_environment(self) -> None:
        self._ensure_llm_api_key()
        self._ensure_cognee_database_env()
        self._ensure_qdrant_adapter_registered()
        self._ensure_auto_feedback_default()
        self._ensure_chunk_budget()

    def _ensure_qdrant_adapter_registered(self) -> None:
        """Register Citadel's Qdrant adapter before Cognee creates an engine."""
        if os.getenv("VECTOR_DB_PROVIDER", "").strip().lower() != "qdrant":
            return
        from .qdrant_adapter import register_qdrant_adapter

        register_qdrant_adapter()

    def _ensure_chunk_budget(self) -> None:
        """Size chunks for the embedder, and watch the embed boundary (#227).

        cognee counts the chunk budget in GPT-4o BPE tokens and defaults it to
        8191; the deployed embedder truncates at 512 wordpieces and raises
        nothing. See kb/chunk_window.py for the measurement and for why the
        budget it ships is an observation rather than a bound.

        Here rather than in the deploy environment for the same reason
        AUTO_FEEDBACK is: a variable nobody sets is not a fix. An explicit
        CITADEL_CHUNK_BUDGET_TOKENS or EMBEDDING_MAX_COMPLETION_TOKENS still
        wins. Both calls are idempotent, so paying for them on every cognee
        operation costs a dictionary lookup after the first.
        """
        chunk_window.apply_chunk_budget()
        chunk_window.install_embed_window_detector()

    def _ensure_auto_feedback_default(self) -> None:
        """Default cognee's AUTO_FEEDBACK off, because it costs an LLM call per search.

        cognee's CacheConfig defaults ``caching`` and ``auto_feedback`` to True,
        and every retriever inherits ``prepare_session_turn_for_retrieval`` from
        BaseRetriever, so the CHUNKS path Citadel runs is gated the same as any
        other. With it on, ``session_turn.prepare_session_turn`` runs a
        structured-output LLM call before retrieval; with it off it returns at
        session_turn.py:379 having done nothing. Measured on this node at 6 to 9
        seconds per search (#50).

        That call is awaited directly on the FastAPI event loop, because nothing
        in the search path uses run_in_executor or to_thread. So it does not
        merely make one search slow, it blocks every other request for its
        duration, which is what makes a trivial /healthz or a 401 take 30
        seconds under ordinary search traffic (#105).

        Set here rather than in Railway's environment on purpose. This fix has
        been written down in docs/progress.md, docs/uat-2026-07-23-findings.md
        and tasks.md since 2026-06-30 and was never applied to a running node,
        because a variable nobody sets is not a fix. An explicit AUTO_FEEDBACK
        in the environment still wins, so it can be turned back on without a
        deploy.
        """
        os.environ.setdefault("AUTO_FEEDBACK", "false")

    def _configured_search_type(self, cognee: Any) -> Any | None:
        raw_value = os.getenv("CITADEL_COGNEE_SEARCH_TYPE", "CHUNKS").strip().upper()
        if raw_value in {"", "AUTO", "RECALL"}:
            return None
        search_type = getattr(cognee, "SearchType", None)
        if search_type is None:
            return None
        return getattr(search_type, raw_value, getattr(search_type, "CHUNKS", None))

    def _is_no_data_error(self, exc: Exception) -> bool:
        return exc.__class__.__name__ in {
            "DatasetNotFoundError",
            "NoDataError",
        } or "No data found in the system" in str(exc)

    async def _create_cognee_database(self) -> None:
        from cognee.infrastructure.databases.relational import get_relational_engine

        db_engine = get_relational_engine()
        await db_engine.create_database()

    def _data_with_metadata(
        self,
        data: Any,
        metadata: dict[str, Any] | None,
        data_id: str | None = None,
    ) -> Any:
        if not metadata and data_id is None:
            return data
        try:
            from cognee.tasks.ingestion.data_item import DataItem
        except Exception:
            return data

        explicit_data_id = UUID(data_id) if data_id is not None else None
        if explicit_data_id is not None and isinstance(data, list):
            raise ValueError("one explicit lifecycle data_id cannot identify a list payload")

        def attach(item: Any) -> Any:
            if isinstance(item, DataItem):
                merged = {**(item.external_metadata or {}), **(metadata or {})}
                return DataItem(
                    data=item.data,
                    label=item.label,
                    external_metadata=merged,
                    data_id=explicit_data_id or item.data_id,
                )
            kwargs: dict[str, Any] = {
                "data": item,
                "external_metadata": metadata,
            }
            if explicit_data_id is not None:
                kwargs["data_id"] = explicit_data_id
            return DataItem(**kwargs)

        if isinstance(data, list):
            return [attach(item) for item in data]
        return attach(data)

    async def _ensure_cognee_ready(self, cognee: Any) -> None:
        # Cognee configures structlog on import and emits high-volume INFO
        # records during cognify/search. Apply Citadel's scoped threshold after
        # that setup, without hiding Citadel's own INFO records.
        configure_cognee_logging()
        if self._startup_migrations_done:
            return
        run_migrations = getattr(cognee, "run_migrations", None)
        if not callable(run_migrations):
            raise RuntimeError("Cognee 1.4.1 run_migrations API is unavailable")
        failed_database_ids = await run_migrations()
        if failed_database_ids:
            raise RuntimeError(
                "Cognee migrations failed for "
                f"{len(failed_database_ids)} database(s)"
            )
        self._startup_migrations_done = True
        logger.info("Cognee startup migrations completed")

    async def remember(
        self,
        data: Any,
        *,
        dataset_name: str,
        session_id: str | None = None,
        tags: tuple[str, ...] = (),
        attestation: Mapping[str, str] | None = None,
        defer_cognify: bool = False,
        data_id: str | None = None,
    ) -> Any:
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        metadata: dict[str, Any] = {}
        if tags:
            metadata["citadel_tags"] = list(tags)
        if attestation:
            promoted_by = attestation.get("promoted_by")
            promoted_at = attestation.get("promoted_at")
            if (
                isinstance(promoted_by, str)
                and promoted_by.strip()
                and isinstance(promoted_at, str)
                and promoted_at.strip()
            ):
                metadata["citadel_attestation"] = {
                    "promoted_by": promoted_by.strip(),
                    "promoted_at": promoted_at.strip(),
                }
        # Durable knowledge writes always go to cognee's permanent graph
        # (add+cognify), never its per-session cache. When a session_id was
        # supplied, cognee routed the write into the session cache, which (a)
        # stored an unserializable payload as the literal "[DataItem]"
        # placeholder instead of the real text, (b) never cognified it inline so
        # ingest reported items_processed:0, and (c) re-embedded a growing
        # scaffolded "Session ID:/Question:/Answer:" blob every sync cycle.
        # session_id is still accepted (callers pass it as provenance) but no
        # longer diverts the write away from the durable path.
        data = self._data_with_metadata(data, metadata or None, data_id)

        # Add is a fast write to Cognee's relational/source stores; it does NOT
        # create chunks, embeddings, or a graph projection. It still opens the
        # graph engine during generic pipeline completion, so ADR-0015's
        # single-process rule applies. Cognify is the chunk, vector, and graph
        # write. Metadata rides in the
        # DataItem (external_metadata) via _data_with_metadata, never as an add()
        # keyword — cognee rejects external_metadata as a kwarg.
        added = await cognee.add(data, dataset_name=dataset_name)

        # The cognify is a single-writer Kuzu write, so it must be coordinated (#47).
        # We previously used cognee.remember(run_in_background=True), but that
        # fire-and-forget cognify is NOT behind our writer lock and fires in EVERY
        # process — so the evolve Phase-1 subprocess and the web cognified Kuzu at
        # the same time, the hourly "Lock is held by PID N" crash.
        #
        # 1) In the Phase-1 evolve subprocess (CITADEL_SUPPRESS_INLINE_COGNIFY=true)
        #    we ADD ONLY and never write Kuzu — the web cognifies everything in
        #    Phase 2 as the sole writer.
        # 2) Otherwise we schedule our OWN background cognify that serializes on the
        #    writer lock (kept non-blocking for the caller, #56), so concurrent
        #    in-process ingests and the evolve scheduler never collide.
        if _suppress_inline_cognify():
            return {"added": added, "cognify": "suppressed"}
        if defer_cognify:
            # The caller (e.g. a bulk Linear resync) coalesces ONE cognify over every
            # dataset it touched at the end, instead of scheduling one-per-write — a
            # 200-issue resync otherwise fires 200 background cognifies that storm the
            # writer lock and starve the request (#46/#52). Add-only here; the caller
            # calls schedule_cognify() once when the batch is done.
            return {"added": added, "cognify": "deferred"}
        queued = self._schedule_background_cognify(dataset_name)
        return {"added": added, "background_cognify": queued}

    def _schedule_background_cognify(self, dataset_name: str) -> bool:
        """Schedule a tracked, writer-lock-guarded cognify so ingest stays fast.

        Replaces cognee's fire-and-forget run_in_background cognify with one that
        acquires our writer lock (via cognify()) — serializing the Kuzu write and
        surfacing failures instead of swallowing them (#47/#56).
        """
        return self.schedule_cognify([dataset_name])

    def schedule_cognify(self, datasets: list[str]) -> bool:
        """Schedule ONE tracked, writer-lock-guarded background cognify.

        Lets a bulk writer (the Linear resync) coalesce a single cognify over every
        dataset it touched instead of one-per-write, so the request is not starved by
        a storm of per-issue cognifies (#46/#52). The single cognify still serializes
        on the writer lock (single Kuzu writer, #47) and logs — never crashes — on
        failure. Returns whether the durable queue accepted the work.
        """
        wanted = list(dict.fromkeys(datasets))  # de-dup, preserve order
        if not wanted:
            return False
        try:
            self.cognify_queue.enqueue(wanted)
        except Exception:  # noqa: BLE001 - a corrupt queue must be visible and fail closed
            logger.exception(
                "cognify NOT queued for datasets %s: durable retry state is unavailable",
                wanted,
            )
            return False
        self._cancel_cognify_retry()
        self._start_cognify_queue_drain()
        return True

    def resume_cognify_queue(self) -> None:
        """Resume due cognify work after a process restart."""
        self._start_cognify_queue_drain()

    def _start_cognify_queue_drain(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Silent here meant content was recorded as ingested and never
            # reached the graph, with nothing in the logs either way. cognee.add
            # writes the relational/source stores; ONLY cognify writes chunks,
            # vectors, and Kuzu graph state. Skipping it makes a document that
            # exists as a row and cannot be retrieved. Say so.
            logger.error(
                "cognify retry drain not started: no running event loop; "
                "durable work will resume on the next server startup"
            )
            return

        existing = self._cognify_queue_task
        if existing is not None and not existing.done():
            return

        task = loop.create_task(self._drain_cognify_queue())
        self._cognify_queue_task = task
        _BACKGROUND_COGNIFY_TASKS.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            _BACKGROUND_COGNIFY_TASKS.discard(done)
            if self._cognify_queue_task is done:
                self._cognify_queue_task = None

        task.add_done_callback(_finished)

    def _cancel_cognify_retry(self) -> None:
        handle = self._cognify_queue_retry_handle
        if handle is not None:
            handle.cancel()
            self._cognify_queue_retry_handle = None

    def _schedule_cognify_retry(
        self,
        *,
        minimum_delay: float = 0.0,
        maximum_delay: float | None = None,
    ) -> None:
        try:
            delay = self.cognify_queue.next_wakeup_delay()
        except Exception:  # noqa: BLE001 - queue corruption must be visible
            logger.exception("cognify retry wakeup scheduling failed")
            delay = min(self.cognify_queue.backoff_seconds, 30.0)
        if delay is None:
            if minimum_delay <= 0:
                return
            delay = minimum_delay
        else:
            delay = max(delay, minimum_delay)
        if maximum_delay is not None:
            delay = min(delay, maximum_delay)
        self._cancel_cognify_retry()
        loop = asyncio.get_running_loop()

        def _wake() -> None:
            self._cognify_queue_retry_handle = None
            self._start_cognify_queue_drain()

        self._cognify_queue_retry_handle = loop.call_later(delay, _wake)

    async def _cognify_with_lease(self, lease: CognifyLease) -> Any:
        cognify = asyncio.create_task(self.cognify(datasets=list(lease.datasets)))
        heartbeat = asyncio.create_task(self._renew_cognify_lease(lease))
        try:
            done, _ = await asyncio.wait(
                {cognify, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                heartbeat.result()
                raise RuntimeError(
                    f"cognify retry lease heartbeat stopped for {lease.job_id}"
                )
            return await cognify
        finally:
            for task in (cognify, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(cognify, heartbeat, return_exceptions=True)

    async def _renew_cognify_lease(self, lease: CognifyLease) -> None:
        interval = min(max(self.cognify_queue.lease_seconds / 3, 0.1), 30.0)
        while True:
            await asyncio.sleep(interval)
            try:
                self.cognify_queue.renew(lease)
            except Exception:  # noqa: BLE001 - lease loss must stop active graph work
                logger.exception("cognify retry lease renewal failed for %s", lease.job_id)
                raise

    async def _drain_cognify_queue(self) -> None:
        try:
            execution_guard = self.cognify_queue.try_acquire_execution()
        except Exception:  # noqa: BLE001 - execution ownership must fail closed
            logger.exception("cognify retry execution lock unavailable")
            self._schedule_cognify_retry(
                minimum_delay=COGNIFY_EXECUTION_LOCK_POLL_SECONDS,
                maximum_delay=30.0,
            )
            return
        if execution_guard is None:
            logger.info("cognify retry execution lock busy; work remains unclaimed")
            self._schedule_cognify_retry(
                minimum_delay=COGNIFY_EXECUTION_LOCK_POLL_SECONDS,
                maximum_delay=COGNIFY_EXECUTION_LOCK_POLL_SECONDS,
            )
            return
        try:
            await self._drain_cognify_queue_locked()
        finally:
            try:
                execution_guard.release()
            except Exception:  # noqa: BLE001 - descriptor close still releases on exit
                logger.exception("cognify retry execution lock release failed")

    async def _drain_cognify_queue_locked(self) -> None:
        while True:
            try:
                lease = self.cognify_queue.claim()
            except Exception:  # noqa: BLE001 - queue corruption must be visible
                logger.exception("cognify retry queue claim failed")
                return
            if lease is None:
                self._schedule_cognify_retry()
                return
            try:
                await self._cognify_with_lease(lease)
            except asyncio.CancelledError:
                try:
                    self.cognify_queue.reschedule(lease, error="cognify worker cancelled")
                except Exception:  # noqa: BLE001 - preserve cancellation semantics
                    logger.exception("cognify retry cancellation reschedule failed")
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                logger.error(
                    "background cognify cancelled for datasets %s; retry persisted",
                    lease.datasets,
                )
                self._schedule_cognify_retry()
                return
            except Exception as exc:  # noqa: BLE001 - retry after the lease expires
                try:
                    self.cognify_queue.reschedule(
                        lease,
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001 - preserve the original failure
                    logger.exception("cognify retry reschedule failed")
                logger.exception(
                    "background cognify failed for datasets %s; retry persisted",
                    lease.datasets,
                )
                self._schedule_cognify_retry()
                return
            try:
                self.cognify_queue.acknowledge(lease)
            except Exception:  # noqa: BLE001 - expired lease will be recovered
                logger.exception(
                    "cognify retry acknowledgement failed for datasets %s",
                    lease.datasets,
                )
                self._schedule_cognify_retry()
                return
            logger.info("background cognify finished for datasets %s", lease.datasets)

    async def stop_cognify_queue(self) -> None:
        """Cancel the local drain and return active work to the retry queue."""
        self._cancel_cognify_retry()
        task = self._cognify_queue_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await task
        if self._cognify_queue_task is task:
            self._cognify_queue_task = None

    async def recall(
        self,
        query: str,
        *,
        dataset: str,
        session_id: str | None = None,
        top_k: int = 10,
        document_ids: list[str] | None = None,
    ) -> list[Any]:
        if (
            document_ids is None
            or os.getenv("VECTOR_DB_PROVIDER", "").strip().lower() != "qdrant"
        ):
            return await self._recall_unscoped(
                query,
                dataset=dataset,
                session_id=session_id,
                top_k=top_k,
            )
        from kb.qdrant_adapter import qdrant_document_scope

        with qdrant_document_scope(document_ids):
            return await self._recall_unscoped(
                query,
                dataset=dataset,
                session_id=session_id,
                top_k=top_k,
            )

    async def _recall_unscoped(
        self,
        query: str,
        *,
        dataset: str,
        session_id: str | None = None,
        top_k: int = 10,
    ) -> list[Any]:
        timing = _search_timing_enabled()
        t_start = perf_counter() if timing else 0.0
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        t_ready = perf_counter() if timing else 0.0
        # The per-session QA cache is the deprecated pre-#54 path and now serves only
        # stale "[DataItem]" scaffolds, so it is OFF by default — durable recall goes
        # straight to the chunk/vector store (#15/#52). Re-enable per-session reads
        # with CITADEL_COGNEE_SESSION_RECALL=true only if the cache is ever repaired.
        if session_id and hasattr(cognee, "recall") and _session_recall_enabled():
            try:
                session_results = await cognee.recall(
                    query,
                    session_id=session_id,
                    top_k=top_k,
                    scope="session",
                )
            except Exception as exc:
                if not self._is_no_data_error(exc):
                    raise
                session_results = []
            if session_results:
                return session_results

        query_type = self._configured_search_type(cognee)
        if query_type is None and hasattr(cognee, "recall"):
            try:
                results = await cognee.recall(
                    query,
                    datasets=[dataset],
                    session_id=session_id,
                    top_k=top_k,
                )
            except Exception as exc:
                if self._is_no_data_error(exc):
                    results = []
                else:
                    raise
            if timing:
                self._log_search_timing(
                    t_start, t_ready, dataset=dataset, top_k=top_k, query_type=None
                )
            return results

        # NOTE (#50): we deliberately do NOT pass cognee's only_context=True here.
        # For the CHUNKS query_type this node uses, only_context flips the return
        # value from the list-of-chunk-payload dicts the callers rely on
        # (result_provenance/_citadel envelope, dedup, drill-down) to a single
        # newline-joined string — a real shape change (verified against cognee 1.2.2
        # source). It also would NOT remove the write-per-read: for CHUNKS cognee
        # persists no session QA at all, and the per-search writes that remain
        # (log_query/log_result history) are unconditional and not gated by
        # only_context. See the timing instrument to attribute the residual latency.
        search_kwargs = {
            "query_text": query,
            "datasets": [dataset],
            "session_id": session_id,
            "top_k": top_k,
        }
        if query_type is not None:
            search_kwargs["query_type"] = query_type
        try:
            results = await cognee.search(**search_kwargs)
        except Exception as exc:
            if self._is_no_data_error(exc):
                results = []
            else:
                raise
        if timing:
            self._log_search_timing(
                t_start, t_ready, dataset=dataset, top_k=top_k, query_type=query_type
            )
        if results and all(
            isinstance(result, dict) and "search_result" in result for result in results
        ):
            flattened: list[Any] = []
            for result in results:
                search_result = result["search_result"]
                if isinstance(search_result, list):
                    flattened.extend(search_result)
                elif search_result is not None:
                    flattened.append(search_result)
            return flattened
        return results

    def _log_search_timing(
        self,
        t_start: float,
        t_ready: float,
        *,
        dataset: str,
        top_k: int,
        query_type: Any,
    ) -> None:
        """Emit one setup/recall/total wall-time line for a search (#50).

        ``setup`` = env prep + cognee import + startup migrations; ``recall`` = the
        single cognee search/recall call (embedding + vector recall + cognee's
        per-read history writes, not separable from here); ``total`` = both.
        """
        now = perf_counter()
        logger.info(
            "search timing: setup=%.1fms recall=%.1fms total=%.1fms dataset=%s top_k=%s query_type=%s",
            (t_ready - t_start) * 1000.0,
            (now - t_ready) * 1000.0,
            (now - t_start) * 1000.0,
            dataset,
            top_k,
            getattr(query_type, "name", query_type),
        )

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        """Return raw nodes and edges from Cognee's graph engine (TTL-cached).

        Nodes arrive as ``(node_id, properties)`` tuples and edges as
        ``(source_id, target_id, relationship_name, properties)`` tuples, per
        ``cognee.infrastructure.databases.graph.graph_db_interface``.

        The refill is one full ``get_graph_data()`` per provisioned dataset
        store (see ``_read_graph_data``), sequential, each store entered
        through cognee's semaphore-backed DatasetQueue
        (``DATASET_QUEUE_MAX_CONCURRENT``, defaulting to
        ``DATABASE_MAX_LRU_CACHE_SIZE`` = 6), the whole sweep under
        ``_graph_data_lock``. Cost scales with the store count, and
        provisioned datasets beyond the engine LRU cap churn the cached
        engines — raise ``DATABASE_MAX_LRU_CACHE_SIZE`` in the deploy
        environment when stores exceed 6 (ADR-0020). The sweep is
        front-loaded on every dashboard open, so the result is cached for
        ``GRAPH_DATA_CACHE_TTL_SECONDS`` behind that single-flight lock: a
        burst of concurrent /api/mesh/graph opens collapses to one store
        sweep instead of one per caller (#28/#50). Per-caller shaping still
        runs per request.
        """
        cached = self._graph_data_cache
        if cached is not None and monotonic() - cached[0] < GRAPH_DATA_CACHE_TTL_SECONDS:
            return cached[1]
        async with self._graph_data_lock:
            cached = self._graph_data_cache
            if (
                cached is not None
                and monotonic() - cached[0] < GRAPH_DATA_CACHE_TTL_SECONDS
            ):
                return cached[1]
            result = await self._read_graph_data()
            self._graph_data_cache = (monotonic(), result)
            return result

    def _invalidate_graph_data_cache(self) -> None:
        """Drop the raw graph snapshot after a successful graph mutation."""
        self._graph_data_cache = None

    async def _read_graph_data(self) -> tuple[list[Any], list[Any]]:
        """Merge every provisioned dataset store into one org-wide graph read.

        Under ENABLE_BACKEND_ACCESS_CONTROL each dataset lives in its own graph
        database, and ``get_graph_engine()`` resolves whichever per-dataset
        config the cognee ``graph_db_config`` ContextVar carries — cognee
        deliberately leaves the LAST entered dataset context in place after an
        ``async with set_database_global_context_variables(...)`` block exits.
        Trusting that ambient context made this read return whichever single
        store the current task last touched: refilled after a cognify it was
        the content dataset's store (43,732 total nodes live on 2026-08-13),
        refilled right after the corpus-health census — whose per-dataset loop
        ends on the alphabetically last probed dataset — it was that store (21
        total nodes, same day), and /api/mesh/graph plus the corpus-health
        ``indexed_docs``/``indexed_edges`` flapped between the two. ADR-0020
        names the org-wide read: one ``get_graph_data()`` per dataset, merged
        with node-id deduplication. Edges dedupe on (source, target,
        relationship), the triple cognee's adapters MERGE on within a store,
        so a document mirrored into two datasets returns once. Datasets come
        from the relational ``dataset_database`` rows — already-provisioned
        stores only, so a read never creates databases — and a node with no
        provisioned store keeps the single ambient-context read (access
        control off, single-store deployments, local fakes).

        A store that fails to open or read is skipped — logged by dataset id
        and exception class only, never row contents — and the healthy stores
        still merge, so one Kuzu lock timeout does not blank the mesh for
        every caller. When EVERY provisioned store fails the read raises:
        /api/mesh/graph keeps its honest fallback and corpus health degrades,
        instead of a dead graph layer presenting as an empty vault (ADR-0020).
        """
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        provisioned = await self._provisioned_dataset_databases()
        if not provisioned:
            return await self._read_graph_data_current_context()
        nodes: list[Any] = []
        edges: list[Any] = []
        seen_nodes: set[str] = set()
        seen_edges: set[tuple[str, str, str]] = set()
        failures = 0
        last_failure: Exception | None = None
        for dataset_id, owner_id in provisioned:
            try:
                scoped_nodes, scoped_edges = await self._read_graph_data_for_dataset(
                    dataset_id, owner_id
                )
            except Exception as exc:  # noqa: BLE001 - one bad store must not blank the mesh
                # Dataset id + exception class only: dataset_database rows
                # carry store credentials in other columns, and exception
                # text can quote connection strings.
                failures += 1
                last_failure = exc
                logger.warning(
                    "org-wide graph read skipped dataset %s: %s",
                    dataset_id,
                    exc.__class__.__name__,
                )
                continue
            for raw in scoped_nodes:
                try:
                    node_id = str(raw[0])
                except (TypeError, IndexError, KeyError):
                    nodes.append(raw)
                    continue
                if node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)
                nodes.append(raw)
            for raw in scoped_edges:
                try:
                    edge_key = (str(raw[0]), str(raw[1]), str(raw[2]))
                except (TypeError, IndexError, KeyError):
                    edges.append(raw)
                    continue
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edges.append(raw)
        if last_failure is not None and failures == len(provisioned):
            raise last_failure
        return nodes, edges

    async def _provisioned_dataset_databases(self) -> list[tuple[UUID, UUID]]:
        """(dataset_id, owner_id) for every dataset with a provisioned database.

        Read straight from the relational ``dataset_database`` rows so the
        org-wide loop only ever opens stores that already exist:
        ``set_database_global_context_variables`` provisions a database for a
        dataset that lacks one, and a read path must not create databases.
        Sorted by dataset id for a deterministic merge order.
        """
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.users.models import DatasetDatabase
        from sqlalchemy import select

        engine = get_relational_engine()
        async with engine.get_async_session() as session:
            rows = await session.execute(
                select(DatasetDatabase.dataset_id, DatasetDatabase.owner_id)
            )
            pairs = rows.all()
        return sorted(
            ((dataset_id, owner_id) for dataset_id, owner_id in pairs),
            key=lambda pair: str(pair[0]),
        )

    async def _read_graph_data_for_dataset(
        self, dataset_id: UUID, owner_id: UUID
    ) -> tuple[list[Any], list[Any]]:
        """One dataset store's raw graph, with the ambient context restored.

        cognee keeps the per-dataset graph/vector/file-storage configs set
        after the ``async with`` block exits, on purpose, so callers can keep
        reading the dataset databases they just wrote. This read must not
        repoint the surrounding task at whichever dataset merged last:
        ``cognify_dataset`` deletes its verify marker through the engine the
        ambient context resolves, and that marker lives in the just-cognified
        dataset's store. The prior values go back once the store is read.
        """
        from cognee.context_global_variables import (
            graph_db_config,
            set_database_global_context_variables,
            vector_db_config,
        )
        from cognee.infrastructure.files.storage.config import file_storage_config

        prior = [
            (variable, variable.get(None))
            for variable in (graph_db_config, vector_db_config, file_storage_config)
        ]
        try:
            async with set_database_global_context_variables(dataset_id, owner_id):
                return await self._read_graph_data_current_context()
        finally:
            for variable, value in prior:
                variable.set(value)

    async def _read_graph_data_current_context(self) -> tuple[list[Any], list[Any]]:
        """Raw nodes and edges from the engine the active context resolves."""
        from cognee.infrastructure.databases.graph import get_graph_engine

        engine = await get_graph_engine()
        nodes, edges = await engine.get_graph_data()
        return list(nodes), list(edges)

    async def node_dataset_map(self) -> dict[str, list[str]]:
        """Map document graph-node ids to the cognee dataset names they belong to.

        A TextDocument graph node's id IS the relational ``Data.id`` (same UUID),
        and dataset membership lives only in the relational store (datasets ↔
        dataset_data ↔ data) — node properties do not reliably carry the dataset
        name. Read-only relational query, so no ``writer_lock``. A Data item can
        belong to multiple datasets (mirrors), hence list values. Attribution is
        best-effort: any failure (timeout included) degrades to ``{}`` and never
        breaks callers.

        Called on every /api/mesh/graph poll, so a SUCCESSFUL result is cached
        for ``NODE_DATASET_MAP_TTL_SECONDS`` and the relational read is bounded
        by ``NODE_DATASET_MAP_TIMEOUT_SECONDS``. A single-flight lock collapses a
        cold-cache burst (15 seats opening the dashboard) to one relational read
        instead of a thundering herd. A FAILED read is cached only for
        ``NODE_DATASET_MAP_FAILURE_TTL_SECONDS`` and prefers the last known-good
        mapping over ``{}`` (stale-while-error), so a transient stall degrades to
        stale attribution — never a fail-closed content blackout (#50).
        """
        fresh = self._fresh_cached_map()
        if fresh is not None:
            return fresh
        async with self._node_dataset_lock:
            # Re-check under the lock: a race loser serves the just-populated
            # cache instead of issuing its own read.
            fresh = self._fresh_cached_map()
            if fresh is not None:
                return fresh
            try:
                mapping = await asyncio.wait_for(
                    self._read_node_dataset_map(),
                    timeout=NODE_DATASET_MAP_TIMEOUT_SECONDS,
                )
                self._node_dataset_last_good = mapping
                self._node_dataset_cache = (monotonic(), mapping, True)
                return mapping
            except Exception:  # noqa: BLE001 - dataset attribution is best-effort
                if self._node_dataset_last_good is not None:
                    logger.warning(
                        "node dataset map read failed; isolation degraded, "
                        "serving last known-good attribution (stale-while-error)",
                        exc_info=True,
                    )
                    mapping = self._node_dataset_last_good
                else:
                    logger.warning(
                        "node dataset map read failed with no prior good read; "
                        "isolation degraded, content hidden for scoped callers",
                        exc_info=True,
                    )
                    mapping = {}
                self._node_dataset_cache = (monotonic(), mapping, False)
                return mapping

    def _fresh_cached_map(self) -> dict[str, list[str]] | None:
        """Return the cached mapping if still within its (success/failure) TTL."""
        cached = self._node_dataset_cache
        if cached is None:
            return None
        ts, mapping, ok = cached
        ttl = (
            NODE_DATASET_MAP_TTL_SECONDS if ok else NODE_DATASET_MAP_FAILURE_TTL_SECONDS
        )
        if monotonic() - ts < ttl:
            return mapping
        return None

    async def _read_node_dataset_map(self) -> dict[str, list[str]]:
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Dataset, DatasetData
        from cognee.modules.users.methods import get_default_user

        # One joined query returns exactly (data_id, dataset_name) pairs — no
        # per-dataset round-trips, no ORM hydration of full Data rows (JSON
        # pipeline_status/external_metadata), no lazy selectin re-loads. 19x
        # faster than the prior get_datasets + get_dataset_data-per-dataset loop
        # and mostly off the event loop (#50).
        from sqlalchemy import select

        user = await get_default_user()
        engine = get_relational_engine()
        mapping: dict[str, list[str]] = {}
        async with engine.get_async_session() as session:
            query = (
                select(DatasetData.data_id, Dataset.name)
                .join(Dataset, Dataset.id == DatasetData.dataset_id)
                .filter(Dataset.owner_id == user.id)
            )
            rows = await session.execute(query)
            for data_id, dataset_name in rows.all():
                mapping.setdefault(str(data_id), []).append(dataset_name)
        return mapping

    async def dataset_document_ids(self, datasets: list[str]) -> list[str]:
        """Return source ids in the authorized datasets, without graph access.

        Forced cognify must verify the receipt against the same dataset scope
        Cognee resolves for the current user. Querying DatasetData through
        Cognee's authorized dataset ids avoids treating another owner's
        same-named dataset as part of the rebuild.
        """
        wanted = list(dict.fromkeys(str(dataset) for dataset in datasets if str(dataset)))
        if not wanted:
            return []
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Dataset, DatasetData
        from cognee.modules.users.methods import get_default_user

        from sqlalchemy import select

        user = await get_default_user()
        engine = get_relational_engine()
        async with engine.get_async_session() as session:
            dataset_rows = await session.execute(
                select(Dataset.id).where(
                    Dataset.owner_id == user.id,
                    Dataset.tenant_id == user.tenant_id,
                    Dataset.name.in_(wanted),
                )
            )
            dataset_ids = [dataset_id for (dataset_id,) in dataset_rows.all()]
            if not dataset_ids:
                return []
            rows = await session.execute(
                select(DatasetData.data_id).where(
                    DatasetData.dataset_id.in_(dataset_ids)
                )
            )
        return sorted({str(data_id) for (data_id,) in rows.all() if data_id is not None})

    async def document_counts_by_dataset(self) -> dict[str, int]:
        """Durable per-dataset document counts, straight from the relational store.

        The mesh snapshot also knows document counts, but it is rebuilt in
        memory and empties on every process restart, so a count taken from it
        drops after each redeploy. This reads the tables that own the answer, so
        the number survives a restart.

        Counted in the database with a GROUP BY rather than by materialising
        ``_read_node_dataset_map`` and summing in Python: the caller is a page
        load, and that map holds one entry per document in the whole vault.
        """
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Dataset, DatasetData
        from cognee.modules.users.methods import get_default_user

        from sqlalchemy import func, select

        user = await get_default_user()
        engine = get_relational_engine()
        counts: dict[str, int] = {}
        async with engine.get_async_session() as session:
            query = (
                select(Dataset.name, func.count(DatasetData.data_id))
                .join(Dataset, Dataset.id == DatasetData.dataset_id)
                .filter(Dataset.owner_id == user.id)
                .group_by(Dataset.name)
            )
            rows = await session.execute(query)
            for dataset_name, total in rows.all():
                counts[str(dataset_name)] = int(total or 0)
        return counts

    async def corpus_page(
        self,
        *,
        after_created_at: str | None = None,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """One keyset page of the relational corpus, ordered by (created_at, id).

        The ``data`` table is the durable record of everything cognee accepted,
        so it is the only store a row-level census can enumerate: the graph
        endpoint caps at 1000 nodes and per-source "documents" counters are
        sync bookkeeping, not corpus rows. Deliberately NOT filtered by
        ``get_default_user()`` like the attribution reads above — an
        owner-scoped census would silently drop rows held by another owner_id;
        ``corpus_totals`` reports that split instead.

        Keyset over (created_at, id) rather than OFFSET so a deep page does not
        rescan everything before it. Both cursor parts cross this boundary as
        strings (ISO timestamp, str UUID) like every other id in this module;
        rows come back with ISO UTC timestamps, dataset names batched in via
        one join query per page, and ``citadel_tags`` parsed out of
        ``external_metadata`` (empty list when absent or unparseable).
        """
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Data, Dataset, DatasetData

        from sqlalchemy import and_, or_, select

        query = select(
            Data.id,
            Data.name,
            Data.content_hash,
            Data.raw_content_hash,
            Data.mime_type,
            Data.token_count,
            Data.data_size,
            Data.created_at,
            Data.updated_at,
            Data.owner_id,
            Data.external_metadata,
        ).order_by(Data.created_at.asc(), Data.id.asc())
        if after_created_at and after_id:
            after_dt = _utc_datetime(datetime.fromisoformat(after_created_at))
            after_uuid = UUID(after_id)
            query = query.where(
                or_(
                    Data.created_at > after_dt,
                    and_(Data.created_at == after_dt, Data.id > after_uuid),
                )
            )
        query = query.limit(max(1, int(limit)))

        engine = get_relational_engine()
        rows: list[dict[str, Any]] = []
        async with engine.get_async_session() as session:
            for row in (await session.execute(query)).all():
                rows.append(
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "content_hash": row.content_hash,
                        "raw_content_hash": row.raw_content_hash,
                        "mime_type": row.mime_type,
                        "token_count": row.token_count,
                        "data_size": row.data_size,
                        "created_at": _isoformat_utc(row.created_at),
                        "updated_at": _isoformat_utc(row.updated_at),
                        "owner_id": str(row.owner_id) if row.owner_id else None,
                        "datasets": [],
                        "citadel_tags": _parse_citadel_tags(row.external_metadata),
                    }
                )
            if rows:
                # Dataset names for the whole page in one join query, unscoped
                # for the same reason as the page itself.
                membership = await session.execute(
                    select(DatasetData.data_id, Dataset.name)
                    .join(Dataset, Dataset.id == DatasetData.dataset_id)
                    .where(
                        DatasetData.data_id.in_([UUID(row["id"]) for row in rows])
                    )
                )
                names_by_id: dict[str, list[str]] = {}
                for data_id, dataset_name in membership.all():
                    names_by_id.setdefault(str(data_id), []).append(str(dataset_name))
                for row in rows:
                    row["datasets"] = sorted(names_by_id.get(row["id"], []))
        return rows

    async def corpus_totals(self) -> dict[str, Any]:
        """Corpus row counts, both unscoped and scoped to the default owner.

        The attribution reads above filter by ``get_default_user().id``. The
        census must not inherit that silently: rows under another owner_id
        would vanish from a scoped count with nothing saying so. Return both
        and let the endpoint surface the difference.
        """
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Data, Dataset, DatasetData
        from cognee.modules.users.methods import get_default_user

        from sqlalchemy import func, select

        user = await get_default_user()
        engine = get_relational_engine()
        async with engine.get_async_session() as session:
            documents = int(
                (await session.execute(select(func.count()).select_from(Data))).scalar()
                or 0
            )
            documents_default_owner = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Data)
                        .where(Data.owner_id == user.id)
                    )
                ).scalar()
                or 0
            )
            by_dataset_query = select(
                Dataset.name, func.count(DatasetData.data_id)
            ).join(Dataset, Dataset.id == DatasetData.dataset_id)
            by_dataset = {
                str(name): int(total or 0)
                for name, total in (
                    await session.execute(by_dataset_query.group_by(Dataset.name))
                ).all()
            }
            by_dataset_default_owner = {
                str(name): int(total or 0)
                for name, total in (
                    await session.execute(
                        by_dataset_query.where(Dataset.owner_id == user.id).group_by(
                            Dataset.name
                        )
                    )
                ).all()
            }
        return {
            "documents": documents,
            "documents_default_owner": documents_default_owner,
            "documents_other_owners": documents - documents_default_owner,
            "by_dataset": by_dataset,
            "by_dataset_default_owner": by_dataset_default_owner,
        }

    async def corpus_health(self, *, limit: int = 64) -> dict[str, Any]:
        """Walk relational rows and verify their vector and graph projections.

        The graph can contain stale or unrelated nodes, so graph volume cannot
        certify accepted relational documents. Walk the relational corpus with
        keyset pagination and inspect each page's exact ids in both projections.
        The hard document cap prevents readiness from doing unbounded work.
        """
        probe_limit = max(1, min(int(limit), 256))
        max_documents = _corpus_health_max_documents()
        totals = await self.corpus_totals()
        total_documents = totals.get("documents")
        if (
            isinstance(total_documents, bool)
            or not isinstance(total_documents, int)
            or total_documents < 0
        ):
            raise RuntimeError("corpus totals returned an invalid document count")

        empty_result = {
            "relational_documents": total_documents,
            "probe_limit": probe_limit,
            "probe_max_documents": max_documents,
            "probe_documents": 0,
            "probe_pages": 0,
            "probe_complete": False,
            "probe_cap_exceeded": False,
            "probe_chunked_documents": 0,
            "probe_graph_documents": 0,
            "probe_fully_indexed_documents": 0,
            "probe_ok": False,
        }
        if total_documents > max_documents:
            return {**empty_result, "probe_cap_exceeded": True}

        after_created_at: str | None = None
        after_id: str | None = None
        document_ids_seen: set[str] = set()
        document_ids: list[str] = []
        pages = 0

        while len(document_ids_seen) < total_documents:
            page_limit = min(
                probe_limit,
                max_documents - len(document_ids_seen),
                total_documents - len(document_ids_seen),
            )
            rows = list(
                await self.corpus_page(
                    after_created_at=after_created_at,
                    after_id=after_id,
                    limit=page_limit,
                )
                or []
            )
            if not rows:
                break
            if len(rows) > page_limit:
                raise RuntimeError("corpus page exceeded its requested limit")

            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise RuntimeError("corpus page returned a row without an id")
                document_id = str(row["id"])
                if document_id in document_ids_seen:
                    raise RuntimeError(
                        f"corpus page returned duplicate document {document_id}"
                    )
                document_ids_seen.add(document_id)
                document_ids.append(document_id)
            if len(document_ids_seen) > total_documents:
                raise RuntimeError("corpus pages exceeded the reported document total")
            pages += 1

            if len(document_ids_seen) >= total_documents:
                break
            last_row = rows[-1]
            if not last_row.get("created_at") or not last_row.get("id"):
                raise RuntimeError("corpus page cannot form a keyset cursor")
            after_created_at = str(last_row["created_at"])
            after_id = str(last_row["id"])

        probe_complete = len(document_ids_seen) == total_documents
        chunked_ids: set[str] = set()
        graph_ids_seen: set[str] = set()
        fully_indexed_ids: set[str] = set()
        if document_ids:
            # Projection methods scan their backing stores only after the full
            # relational walk. Qdrant counts resolve dataset membership
            # internally. Ladybug uses one graph per dataset, so scan each
            # dataset's exact document ids and union the results.
            chunk_counts = await self.corpus_chunk_counts(document_ids)
            if chunk_counts is None:
                raise RuntimeError("vector chunk measurement is unavailable")
            memberships = await self.dataset_membership_for_documents(document_ids)
            graph_ids: set[str] = set()
            by_dataset: dict[str, list[str]] = {}
            for document_id in document_ids:
                for dataset in memberships.get(document_id, []):
                    by_dataset.setdefault(str(dataset), []).append(document_id)
            for dataset in sorted(by_dataset):
                scoped_graph_ids = await self.corpus_graph_presence(
                    by_dataset[dataset],
                    datasets=[dataset],
                )
                if scoped_graph_ids is None:
                    raise RuntimeError("graph presence measurement is unavailable")
                graph_ids.update(str(document_id) for document_id in scoped_graph_ids)
            chunked_ids = {
                document_id
                for document_id in document_ids
                if int(chunk_counts.get(document_id, 0)) > 0
            }
            graph_ids_seen = set(document_ids) & {
                str(document_id) for document_id in graph_ids
            }
            fully_indexed_ids = chunked_ids & graph_ids_seen

        return {
            "relational_documents": total_documents,
            "probe_limit": probe_limit,
            "probe_max_documents": max_documents,
            "probe_documents": len(document_ids_seen),
            "probe_pages": pages,
            "probe_complete": probe_complete,
            "probe_cap_exceeded": False,
            "probe_chunked_documents": len(chunked_ids),
            "probe_graph_documents": len(graph_ids_seen),
            "probe_fully_indexed_documents": len(fully_indexed_ids),
            "probe_ok": probe_complete
            and (
                total_documents == 0
                or len(fully_indexed_ids) == len(document_ids_seen)
            ),
        }

    async def corpus_chunk_counts(
        self, document_ids: list[str]
    ) -> dict[str, int] | None:
        """Chunk rows per document from the vector store; None when not measured.

        None and 0 are different answers: 0 claims cognee accepted a document
        and indexed no chunk for it, None says this node could not look (the
        provider is not pgvector, so there is no chunk table to count). A
        MISSING chunk collection, by contrast, is a real measurement — nothing
        was ever indexed — and returns an empty mapping (absent id = 0).

        One grouped query per page over the ``DocumentChunk_text`` collection,
        keyed on the ``document_id`` each chunk row carries in its payload; the
        ids are compared as strings because that is how the payload stores them.
        """
        if not document_ids:
            return {}
        self._prepare_cognee_environment()
        provider = os.getenv("VECTOR_DB_PROVIDER", "").strip().lower()
        if provider == "qdrant":
            wanted = list(dict.fromkeys(str(document_id) for document_id in document_ids))
            memberships = await self.dataset_membership_for_documents(wanted)
            by_dataset: dict[str, list[str]] = {}
            for document_id in wanted:
                for dataset in memberships.get(document_id, []):
                    by_dataset.setdefault(str(dataset), []).append(document_id)
            counts = {document_id: 0 for document_id in wanted}
            for dataset in sorted(by_dataset):
                scoped_ids = by_dataset[dataset]
                report = await self._stored_qdrant_chunk_budget_check(
                    document_ids=scoped_ids,
                    budget=chunk_window.resolve_chunk_budget(),
                    datasets=[dataset],
                    document_ids_by_dataset=None,
                )
                measured = report.get("document_chunk_counts")
                if not isinstance(measured, dict):
                    raise RuntimeError("Qdrant chunk census omitted document counts")
                for document_id in scoped_ids:
                    value = measured.get(document_id, 0)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise RuntimeError("Qdrant chunk census returned an invalid count")
                    counts[document_id] = max(counts[document_id], value)
            return counts
        if provider != "pgvector":
            return None
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.vector import get_vector_engine
        from cognee.infrastructure.databases.vector.exceptions import (
            CollectionNotFoundError,
        )

        from sqlalchemy import func, select

        engine = get_vector_engine()
        try:
            table = await engine.get_table("DocumentChunk_text")
        except CollectionNotFoundError:
            return {}

        payload_document_id = table.c.payload["document_id"].as_string()
        wanted = [str(document_id) for document_id in document_ids]
        counts: dict[str, int] = {}
        async with engine.get_async_session() as session:
            grouped = await session.execute(
                select(payload_document_id, func.count())
                .where(payload_document_id.in_(wanted))
                .group_by(payload_document_id)
            )
            for document_id, total in grouped.all():
                if document_id:
                    counts[str(document_id)] = int(total or 0)
        return counts

    async def source_manifest_for_documents(
        self, document_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        """Return content-free source fingerprints for recovery fencing.

        The manifest deliberately excludes source text and storage locations. It
        proves that the relational source row still describes the same bytes and
        lets recovery refuse a changed source before a dataset-wide force
        cognify. A missing row is absent from the result so callers can fail
        closed instead of treating a deleted source as recoverable.
        """
        wanted = list(dict.fromkeys(str(document_id) for document_id in document_ids))
        if not wanted:
            return {}
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.infrastructure.files.utils.open_data_file import open_data_file
        from cognee.modules.data.models import Data

        from sqlalchemy import select

        try:
            wanted_ids = [UUID(document_id) for document_id in wanted]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("source manifest requested a non-UUID document id") from exc

        engine = get_relational_engine()
        manifest: dict[str, dict[str, Any]] = {}
        async with engine.get_async_session() as session:
            rows = await session.execute(
                select(
                    Data.id,
                    Data.content_hash,
                    Data.raw_content_hash,
                    Data.data_size,
                    Data.raw_data_location,
                    Data.updated_at,
                ).where(Data.id.in_(wanted_ids))
            )
            for row in rows.all():
                raw_location = getattr(row, "raw_data_location", None)
                stored_raw_hash = getattr(row, "raw_content_hash", None)
                if not isinstance(raw_location, str) or not raw_location:
                    raise RuntimeError("repair source location is unavailable")
                if not isinstance(stored_raw_hash, str) or not stored_raw_hash:
                    raise RuntimeError("repair source hash is unavailable")
                digest = md5()
                raw_size = 0
                try:
                    async with open_data_file(raw_location) as source_file:
                        while True:
                            chunk = source_file.read(digest.block_size)
                            if not chunk:
                                break
                            digest.update(chunk)
                            raw_size += len(chunk)
                except Exception as exc:  # noqa: BLE001 - source must be readable
                    raise RuntimeError("repair source is unreadable") from exc
                if digest.hexdigest() != stored_raw_hash:
                    raise RuntimeError("repair source fingerprint mismatch")
                entry: dict[str, Any] = {}
                for key in ("content_hash", "raw_content_hash"):
                    value = getattr(row, key, None)
                    if value is not None:
                        entry[key] = str(value)
                if row.data_size is not None:
                    entry["data_size"] = int(row.data_size)
                entry["raw_data_size"] = raw_size
                entry["source_readable"] = True
                updated_at = _isoformat_utc(row.updated_at)
                if updated_at is not None:
                    entry["updated_at"] = updated_at
                if entry:
                    manifest[str(row.id)] = entry
        return manifest

    async def dataset_membership_for_documents(
        self, document_ids: list[str]
    ) -> dict[str, list[str]]:
        """Return the current relational dataset membership for each source id.

        Recovery uses this as a strict fence, so unlike ``node_dataset_map`` this
        read is unscoped, uncached, and does not degrade to stale or empty data.
        The census records the same unscoped DatasetData membership.
        """
        wanted = list(dict.fromkeys(str(document_id) for document_id in document_ids))
        if not wanted:
            return {}
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Dataset, DatasetData

        from sqlalchemy import select

        try:
            wanted_ids = [UUID(document_id) for document_id in wanted]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "dataset membership requested a non-UUID document id"
            ) from exc

        engine = get_relational_engine()
        names_by_id: dict[str, set[str]] = {document_id: set() for document_id in wanted}
        async with engine.get_async_session() as session:
            rows = await session.execute(
                select(DatasetData.data_id, Dataset.name)
                .join(Dataset, Dataset.id == DatasetData.dataset_id)
                .where(DatasetData.data_id.in_(wanted_ids))
            )
            for data_id, dataset_name in rows.all():
                if data_id is None or not isinstance(dataset_name, str) or not dataset_name:
                    raise RuntimeError("dataset membership returned an invalid row")
                document_id = str(data_id)
                if document_id not in names_by_id:
                    raise RuntimeError("dataset membership returned an unexpected document")
                names_by_id[document_id].add(dataset_name)
        return {
            document_id: sorted(names_by_id[document_id]) for document_id in wanted
        }

    async def corpus_zero_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        """Enumerate accepted documents with no measured vector chunks.

        ``corpus_chunk_counts`` returns an empty mapping for a real missing row
        and ``None`` when the provider cannot measure chunks. The distinction is
        preserved here because a repair must never treat an unavailable vector
        store as evidence that documents need reindexing.
        """
        probe_limit = max(1, min(int(page_limit), 256))
        max_documents = _corpus_health_max_documents()
        totals = await self.corpus_totals()
        total_documents = totals.get("documents")
        if (
            isinstance(total_documents, bool)
            or not isinstance(total_documents, int)
            or total_documents < 0
        ):
            raise RuntimeError("corpus totals returned an invalid document count")

        base: dict[str, Any] = {
            "dataset": dataset,
            "relational_documents": total_documents,
            "probe_limit": probe_limit,
            "probe_max_documents": max_documents,
            "documents_scanned": 0,
            "pages": 0,
            "zero_chunk_count": 0,
            "zero_chunk_documents": [],
            "zero_chunk_documents_truncated": False,
            "zero_chunk_document_ids": [],
            "zero_repair_document_ids": [],
            "repair_document_ids": [],
            "repair_document_datasets": {},
            "repair_datasets": [],
            "unassigned_zero_chunk_count": 0,
            "census_complete": False,
        }
        if total_documents > max_documents:
            return {
                **base,
                "ok": False,
                "reason": "document_cap_exceeded",
                "cap_exceeded": True,
            }

        after_created_at: str | None = None
        after_id: str | None = None
        seen_ids: set[str] = set()
        zero_documents: list[dict[str, Any]] = []
        zero_ids: set[str] = set()
        zero_repair_ids: set[str] = set()
        repair_document_datasets: dict[str, list[str]] = {}
        repair_datasets: set[str] = set()
        unassigned = 0
        zero_count = 0
        pages = 0

        while len(seen_ids) < total_documents:
            limit = min(
                probe_limit,
                max_documents - len(seen_ids),
                total_documents - len(seen_ids),
            )
            rows = list(
                await self.corpus_page(
                    after_created_at=after_created_at,
                    after_id=after_id,
                    limit=limit,
                )
                or []
            )
            if not rows:
                break
            if len(rows) > limit:
                raise RuntimeError("corpus page exceeded its requested limit")

            page_ids: list[str] = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise RuntimeError("corpus page returned a row without an id")
                document_id = str(row["id"])
                if document_id in seen_ids:
                    raise RuntimeError(
                        f"corpus page returned duplicate document {document_id}"
                    )
                seen_ids.add(document_id)
                page_ids.append(document_id)

            chunk_counts = await self.corpus_chunk_counts(page_ids)
            if chunk_counts is None:
                raise RuntimeError("vector chunk measurement is unavailable")
            for row in rows:
                document_id = str(row["id"])
                if int(chunk_counts.get(document_id, 0)) != 0:
                    continue
                row_datasets = row.get("datasets")
                datasets = (
                    sorted({str(value) for value in row_datasets if value})
                    if isinstance(row_datasets, list)
                    else []
                )
                if dataset and dataset not in datasets:
                    continue
                zero_count += 1
                zero_ids.add(document_id)
                if datasets:
                    assigned_datasets = [dataset] if dataset else datasets
                    zero_repair_ids.add(document_id)
                    repair_datasets.update(assigned_datasets)
                    repair_document_datasets[document_id] = assigned_datasets
                else:
                    unassigned += 1
                if len(zero_documents) < MAX_ZERO_CHUNK_REPORT_DOCUMENTS:
                    zero_documents.append(
                        {
                            "id": document_id,
                            "name": row.get("name"),
                            "datasets": datasets,
                            "created_at": row.get("created_at"),
                        }
                    )
            pages += 1

            if len(seen_ids) >= total_documents:
                break
            last_row = rows[-1]
            if not last_row.get("created_at") or not last_row.get("id"):
                raise RuntimeError("corpus page cannot form a keyset cursor")
            after_created_at = str(last_row["created_at"])
            after_id = str(last_row["id"])

        complete = len(seen_ids) == total_documents
        return {
            **base,
            "ok": complete,
            "reason": None if complete else "incomplete_corpus_walk",
            "cap_exceeded": False,
            "documents_scanned": len(seen_ids),
            "pages": pages,
            "zero_chunk_count": zero_count,
            "zero_chunk_document_ids": sorted(zero_ids),
            "zero_repair_document_ids": sorted(zero_repair_ids),
            "zero_chunk_documents": zero_documents,
            "zero_chunk_documents_truncated": zero_count > len(zero_documents),
            "repair_document_ids": sorted(zero_repair_ids),
            "repair_document_datasets": dict(sorted(repair_document_datasets.items())),
            "repair_datasets": sorted(repair_datasets),
            "unassigned_zero_chunk_count": unassigned,
            "census_complete": complete,
        }

    async def stored_chunk_budget_check(
        self,
        document_ids: list[str] | None = None,
        *,
        budget: int | None = None,
        datasets: list[str] | None = None,
        document_ids_by_dataset: Mapping[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Measure exact persisted ``DocumentChunk_text`` payloads.

        Pgvector is scanned through its reflected table. Qdrant is scanned one
        authorized dataset context at a time through Citadel's tenant-filtered
        scroll path. Other providers return ``None`` because a similarity query
        is not evidence of corpus-wide compliance.
        """
        self._prepare_cognee_environment()
        provider = os.getenv("VECTOR_DB_PROVIDER", "").strip().lower()
        limit = budget if budget is not None else chunk_window.resolve_chunk_budget()
        if provider == "qdrant":
            return await self._stored_qdrant_chunk_budget_check(
                document_ids=document_ids,
                budget=limit,
                datasets=datasets,
                document_ids_by_dataset=document_ids_by_dataset,
            )
        if provider != "pgvector":
            return None
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.vector import get_vector_engine
        from cognee.infrastructure.databases.vector.exceptions import (
            CollectionNotFoundError,
        )
        from sqlalchemy import select

        engine = get_vector_engine()
        try:
            table = await engine.get_table("DocumentChunk_text")
        except CollectionNotFoundError:
            return {
                "ok": True,
                "provider": "pgvector",
                "collection": "DocumentChunk_text",
                "collection_present": False,
                "budget": limit,
                "scope": "document_ids" if document_ids is not None else "full",
                "chunks_scanned": 0,
                "violation_count": 0,
                "violations": [],
                "violation_document_counts": {},
                "violation_document_ids": [],
                "missing_document_id_violation_count": 0,
                "violations_truncated": False,
            }

        statement = select(table.c.id, table.c.payload)
        wanted = [str(document_id) for document_id in document_ids or []]
        if document_ids is not None and not wanted:
            return {
                "ok": True,
                "provider": "pgvector",
                "collection": "DocumentChunk_text",
                "collection_present": True,
                "budget": limit,
                "scope": "document_ids",
                "scope_document_count": 0,
                "chunks_scanned": 0,
                "violation_count": 0,
                "violations": [],
                "violation_document_counts": {},
                "violation_document_ids": [],
                "missing_document_id_violation_count": 0,
                "violations_truncated": False,
            }
        if document_ids is not None:
            payload_document_id = table.c.payload["document_id"].as_string()
            statement = statement.where(payload_document_id.in_(wanted))

        violations: list[dict[str, Any]] = []
        violation_count = 0
        chunks_scanned = 0
        violation_document_counts: dict[str, int] = {}
        missing_document_id_violation_count = 0
        async with engine.get_async_session() as session:
            rows = await session.execute(statement)
            for row in rows.all():
                chunks_scanned += 1
                violation = chunk_window.check_stored_chunk_payload(
                    row[1], chunk_id=str(row[0]), budget=limit
                )
                if violation is not None:
                    violation_count += 1
                    if violation.document_id is None:
                        missing_document_id_violation_count += 1
                    else:
                        violation_document_counts[violation.document_id] = (
                            violation_document_counts.get(violation.document_id, 0) + 1
                        )
                    if len(violations) < 50:
                        violations.append(violation.as_dict())

        return {
            "ok": violation_count == 0,
            "provider": "pgvector",
            "collection": "DocumentChunk_text",
            "collection_present": True,
            "budget": limit,
            "scope": "document_ids" if document_ids is not None else "full",
            "scope_document_count": len(wanted) if document_ids is not None else None,
            "chunks_scanned": chunks_scanned,
            "violation_count": violation_count,
            # Keep the failure response bounded. The count remains exact and
            # each item contains an id/digest, never the stored text.
            "violations": violations[:50],
            "violation_document_counts": dict(sorted(violation_document_counts.items())),
            "violation_document_ids": sorted(violation_document_counts),
            "missing_document_id_violation_count": missing_document_id_violation_count,
            "violations_truncated": violation_count > len(violations),
        }

    async def _stored_qdrant_chunk_budget_check(
        self,
        *,
        document_ids: list[str] | None,
        budget: int,
        datasets: list[str] | None,
        document_ids_by_dataset: Mapping[str, list[str]] | None,
    ) -> dict[str, Any]:
        """Enumerate exact Qdrant chunk payloads inside authorized dataset scopes."""
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.context_global_variables import (
            set_database_global_context_variables,
        )
        from cognee.infrastructure.databases.vector import get_vector_engine_async
        from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
            resolve_authorized_user_datasets,
        )

        requested_datasets = list(
            dict.fromkeys(str(dataset).strip() for dataset in datasets or [] if str(dataset).strip())
        )
        if not requested_datasets:
            raise RuntimeError("Qdrant chunk census requires explicit dataset scope")
        expected_input = {
            str(dataset_id).strip(): {
                str(document_id)
                for document_id in scoped_document_ids
                if str(document_id)
            }
            for dataset_id, scoped_document_ids in (document_ids_by_dataset or {}).items()
            if str(dataset_id).strip()
        }
        wanted = {
            str(document_id) for document_id in document_ids or [] if str(document_id)
        }
        document_scope = document_ids is not None or document_ids_by_dataset is not None
        if document_ids_by_dataset is not None:
            wanted = set().union(*expected_input.values()) if expected_input else set()
        elif document_ids is not None and len(requested_datasets) != 1:
            raise RuntimeError(
                "multi-dataset Qdrant census requires document_ids_by_dataset"
            )
        if document_scope and not wanted:
            return {
                "ok": True,
                "provider": "qdrant",
                "collection": "DocumentChunk_text",
                "collection_present": False,
                "budget": budget,
                "scope": "document_ids",
                "scope_document_count": 0,
                "scope_dataset_count": 0,
                "chunks_scanned": 0,
                "document_chunk_counts": {},
                "violation_count": 0,
                "violations": [],
                "violation_document_counts": {},
                "violation_document_ids": [],
                "missing_document_id_violation_count": 0,
                "missing_document_ids": [],
                "violations_truncated": False,
            }

        _, authorized_datasets = await resolve_authorized_user_datasets(
            requested_datasets,
            None,
        )
        if len(authorized_datasets) != len(requested_datasets):
            raise RuntimeError("Qdrant chunk census did not resolve every requested dataset")
        authorized_by_id = {str(dataset.id): dataset for dataset in authorized_datasets}
        if len(authorized_by_id) != len(authorized_datasets):
            raise RuntimeError("Qdrant chunk census resolved duplicate dataset identities")
        if expected_input:
            unknown_dataset_ids = sorted(set(expected_input) - set(authorized_by_id))
            if unknown_dataset_ids:
                raise RuntimeError(
                    "Qdrant chunk census receipt contains unauthorized dataset identities"
                )
            expected_by_dataset = {
                dataset_id: expected_input.get(dataset_id, set())
                for dataset_id in authorized_by_id
            }
        elif document_ids is not None:
            only_dataset_id = next(iter(authorized_by_id))
            expected_by_dataset = {only_dataset_id: wanted}
        else:
            expected_by_dataset = {
                dataset_id: set() for dataset_id in authorized_by_id
            }
        collection_present = False
        chunks_scanned = 0
        violations: list[dict[str, Any]] = []
        violation_count = 0
        violation_document_counts: dict[str, int] = {}
        missing_document_id_violation_count = 0
        document_chunk_counts: dict[str, int] = {}
        missing_dataset_document_ids: list[dict[str, str]] = []
        dataset_reports: dict[str, dict[str, Any]] = {}

        for dataset in authorized_datasets:
            dataset_id = str(dataset.id)
            dataset_wanted = expected_by_dataset[dataset_id]
            dataset_seen_document_ids: set[str] = set()
            dataset_chunks_scanned = 0
            dataset_violation_count = 0
            dataset_collection_present = False
            async with set_database_global_context_variables(
                dataset.id,
                dataset.owner_id,
            ):
                engine = await get_vector_engine_async()
                if await engine.has_collection("DocumentChunk_text"):
                    collection_present = True
                    dataset_collection_present = True
                    offset: str | int | UUID | None = None
                    seen_offsets: set[str] = set()
                    while True:
                        rows, next_offset = await engine.scroll_data_points(
                            "DocumentChunk_text",
                            offset=offset,
                            limit=256,
                            with_vectors=False,
                            document_ids=(
                                sorted(dataset_wanted)
                                if document_scope
                                else None
                            ),
                        )
                        for row in rows:
                            payload = getattr(row, "payload", None)
                            document_id = (
                                str(payload.get("document_id"))
                                if isinstance(payload, dict)
                                and payload.get("document_id") is not None
                                else None
                            )
                            if document_scope and document_id not in dataset_wanted:
                                if document_id is not None:
                                    continue
                            chunks_scanned += 1
                            dataset_chunks_scanned += 1
                            if document_id is not None:
                                dataset_seen_document_ids.add(document_id)
                                document_chunk_counts[document_id] = (
                                    document_chunk_counts.get(document_id, 0) + 1
                                )
                            violation = chunk_window.check_stored_chunk_payload(
                                payload,
                                chunk_id=str(getattr(row, "id", "unavailable")),
                                budget=budget,
                            )
                            if violation is None:
                                continue
                            violation_count += 1
                            dataset_violation_count += 1
                            if violation.document_id is None:
                                missing_document_id_violation_count += 1
                            else:
                                violation_document_counts[violation.document_id] = (
                                    violation_document_counts.get(violation.document_id, 0)
                                    + 1
                                )
                            if len(violations) < 50:
                                violations.append(
                                    {"dataset": dataset_id, **violation.as_dict()}
                                )
                        if next_offset is None:
                            break
                        offset_key = str(next_offset)
                        if offset_key in seen_offsets:
                            raise RuntimeError("Qdrant chunk scroll repeated an offset")
                        seen_offsets.add(offset_key)
                        offset = next_offset

            dataset_missing = (
                sorted(dataset_wanted - dataset_seen_document_ids)
                if document_scope
                else []
            )
            missing_dataset_document_ids.extend(
                {"dataset": dataset_id, "document_id": document_id}
                for document_id in dataset_missing
            )
            dataset_reports[dataset_id] = {
                "collection_present": dataset_collection_present,
                "scope_document_count": len(dataset_wanted) if document_scope else None,
                "chunks_scanned": dataset_chunks_scanned,
                "violation_count": dataset_violation_count,
                "missing_document_ids": dataset_missing,
            }

        missing_document_ids = sorted(
            {item["document_id"] for item in missing_dataset_document_ids}
        )
        return {
            "ok": violation_count == 0 and not missing_dataset_document_ids,
            "provider": "qdrant",
            "collection": "DocumentChunk_text",
            "collection_present": collection_present,
            "budget": budget,
            "scope": "document_ids" if document_scope else "full",
            "scope_document_count": (
                sum(len(ids) for ids in expected_by_dataset.values())
                if document_scope
                else None
            ),
            "scope_dataset_count": len(authorized_datasets),
            "chunks_scanned": chunks_scanned,
            "document_chunk_counts": dict(sorted(document_chunk_counts.items())),
            "violation_count": violation_count,
            "violations": violations,
            "violation_document_counts": dict(sorted(violation_document_counts.items())),
            "violation_document_ids": sorted(violation_document_counts),
            "missing_document_id_violation_count": missing_document_id_violation_count,
            "missing_document_ids": missing_document_ids,
            "missing_dataset_document_ids": missing_dataset_document_ids,
            "dataset_reports": dataset_reports,
            "violations_truncated": violation_count > len(violations),
        }

    async def stored_chunk_ids_for_documents(
        self, document_ids: list[str]
    ) -> list[str] | None:
        """Return every persisted chunk id for ``document_ids``.

        A repair must delete the complete old projection before Cognee rebuilds
        it. Similarity search cannot enumerate that projection, so non-pgvector
        providers are explicitly unmeasured and refuse repair.
        """
        if not document_ids:
            return []
        self._prepare_cognee_environment()
        if os.getenv("VECTOR_DB_PROVIDER", "").lower() != "pgvector":
            return None
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.vector import get_vector_engine
        from cognee.infrastructure.databases.vector.exceptions import (
            CollectionNotFoundError,
        )
        from sqlalchemy import select

        engine = get_vector_engine()
        try:
            table = await engine.get_table("DocumentChunk_text")
        except CollectionNotFoundError:
            return []

        wanted = list(dict.fromkeys(str(document_id) for document_id in document_ids))
        payload_document_id = table.c.payload["document_id"].as_string()
        statement = select(table.c.id).where(payload_document_id.in_(wanted))
        async with engine.get_async_session() as session:
            rows = await session.execute(statement)
            return [str(row[0]) for row in rows.all() if row[0] is not None]

    async def corpus_oversized_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        """Enumerate accepted documents with persisted over-budget chunks.

        The relational corpus is walked to map accepted source ids. A complete
        unfiltered vector scan then checks exact stored payloads, so malformed
        and orphan rows cannot disappear behind a page filter.
        """
        probe_limit = max(1, min(int(page_limit), 256))
        max_documents = _corpus_health_max_documents()
        totals = await self.corpus_totals()
        total_documents = totals.get("documents")
        if (
            isinstance(total_documents, bool)
            or not isinstance(total_documents, int)
            or total_documents < 0
        ):
            raise RuntimeError("corpus totals returned an invalid document count")

        base: dict[str, Any] = {
            "dataset": dataset,
            "budget": chunk_window.resolve_chunk_budget(),
            "relational_documents": total_documents,
            "probe_limit": probe_limit,
            "probe_max_documents": max_documents,
            "documents_scanned": 0,
            "pages": 0,
            "oversized_document_count": 0,
            "oversized_chunk_count": 0,
            "oversized_documents": [],
            "oversized_documents_truncated": False,
            "repair_document_ids": [],
            "repair_datasets": [],
            "unassigned_oversized_document_count": 0,
            "orphan_oversized_document_count": 0,
            "missing_document_id_violation_count": 0,
            "census_complete": False,
        }
        if total_documents > max_documents:
            return {
                **base,
                "ok": False,
                "reason": "document_cap_exceeded",
                "cap_exceeded": True,
            }

        after_created_at: str | None = None
        after_id: str | None = None
        seen_ids: set[str] = set()
        corpus_rows_by_id: dict[str, dict[str, Any]] = {}
        pages = 0

        while len(seen_ids) < total_documents:
            limit = min(
                probe_limit,
                max_documents - len(seen_ids),
                total_documents - len(seen_ids),
            )
            rows = list(
                await self.corpus_page(
                    after_created_at=after_created_at,
                    after_id=after_id,
                    limit=limit,
                )
                or []
            )
            if not rows:
                break
            if len(rows) > limit:
                raise RuntimeError("corpus page exceeded its requested limit")

            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise RuntimeError("corpus page returned a row without an id")
                document_id = str(row["id"])
                if document_id in seen_ids:
                    raise RuntimeError(
                        f"corpus page returned duplicate document {document_id}"
                    )
                seen_ids.add(document_id)
                corpus_rows_by_id[document_id] = row

            pages += 1
            if len(seen_ids) >= total_documents:
                break
            last_row = rows[-1]
            if not last_row.get("created_at") or not last_row.get("id"):
                raise RuntimeError("corpus page cannot form a keyset cursor")
            after_created_at = str(last_row["created_at"])
            after_id = str(last_row["id"])

        complete = len(seen_ids) == total_documents
        if not complete:
            return {
                **base,
                "ok": False,
                "reason": "incomplete_corpus_walk",
                "cap_exceeded": False,
                "documents_scanned": len(seen_ids),
                "pages": pages,
                "census_complete": False,
            }

        # Run the persisted scan without a document-id filter. A scoped query
        # cannot see malformed/orphan payloads, which must block an apply rather
        # than disappear behind an apparently clean accepted-document census.
        check = await self.stored_chunk_budget_check(None, budget=base["budget"])
        if check is None:
            raise RuntimeError("stored chunk budget measurement is unavailable")
        violation_counts = check.get("violation_document_counts")
        if not isinstance(violation_counts, dict):
            raise RuntimeError("stored chunk budget returned invalid document counts")
        violation_count = check.get("violation_count")
        missing_document_id_violation_count = check.get(
            "missing_document_id_violation_count", 0
        )
        if (
            isinstance(violation_count, bool)
            or not isinstance(violation_count, int)
            or violation_count < 0
            or isinstance(missing_document_id_violation_count, bool)
            or not isinstance(missing_document_id_violation_count, int)
            or missing_document_id_violation_count < 0
        ):
            raise RuntimeError("stored chunk budget returned invalid violation totals")
        grouped_violation_count = sum(
            int(value) for value in violation_counts.values()
        )
        if grouped_violation_count + missing_document_id_violation_count != violation_count:
            raise RuntimeError("stored chunk budget violation totals do not reconcile")

        oversized_ids: set[str] = set()
        repair_ids: set[str] = set()
        oversized_chunk_count = 0
        oversized_documents: list[dict[str, Any]] = []
        repair_datasets: set[str] = set()
        unassigned = 0
        orphan_count = 0
        for raw_document_id, raw_chunk_count in violation_counts.items():
            document_id = str(raw_document_id)
            if (
                isinstance(raw_chunk_count, bool)
                or not isinstance(raw_chunk_count, int)
                or raw_chunk_count < 1
            ):
                raise RuntimeError("stored chunk budget returned invalid violation count")
            row = corpus_rows_by_id.get(document_id)
            if row is None:
                # This is an oversized persisted projection with no accepted
                # relational source. Keep it visible and refuse repair.
                orphan_count += 1
                oversized_ids.add(document_id)
                oversized_chunk_count += raw_chunk_count
                if len(oversized_documents) < MAX_OVERSIZED_CHUNK_REPORT_DOCUMENTS:
                    oversized_documents.append(
                        {
                            "id": document_id,
                            "name": None,
                            "datasets": [],
                            "created_at": None,
                            "violation_count": raw_chunk_count,
                            "unrepairable": True,
                        }
                    )
                continue

            row_datasets = row.get("datasets")
            datasets = (
                sorted({str(value) for value in row_datasets if value})
                if isinstance(row_datasets, list)
                else []
            )
            if dataset and dataset not in datasets:
                continue
            oversized_ids.add(document_id)
            oversized_chunk_count += raw_chunk_count
            if datasets:
                repair_ids.add(document_id)
                repair_datasets.update([dataset] if dataset else datasets)
            else:
                unassigned += 1
            if len(oversized_documents) < MAX_OVERSIZED_CHUNK_REPORT_DOCUMENTS:
                oversized_documents.append(
                    {
                        "id": document_id,
                        "name": row.get("name"),
                        "datasets": datasets,
                        "created_at": row.get("created_at"),
                        "violation_count": raw_chunk_count,
                    }
                )

        return {
            **base,
            "ok": complete,
            "reason": None if complete else "incomplete_corpus_walk",
            "cap_exceeded": False,
            "documents_scanned": len(seen_ids),
            "pages": pages,
            "oversized_document_count": len(oversized_ids),
            "oversized_chunk_count": oversized_chunk_count,
            "oversized_documents": oversized_documents,
            "oversized_documents_truncated": len(oversized_ids)
            > len(oversized_documents),
            "repair_document_ids": sorted(repair_ids),
            "repair_datasets": sorted(repair_datasets),
            "unassigned_oversized_document_count": unassigned + orphan_count,
            "orphan_oversized_document_count": orphan_count,
            "missing_document_id_violation_count": missing_document_id_violation_count,
            "census_complete": complete,
        }

    async def corpus_reconciliation_census(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        """Run one complete census for zero and over-budget projections.

        The walk is intentionally separate from the legacy single-population
        helpers. A combined repair must make its decision from one snapshot:
        accepted source rows are enumerated once, vector chunk counts identify
        zero-chunk rows, and one unfiltered pgvector payload scan identifies
        over-budget rows and malformed/orphan payloads.
        """
        probe_limit = max(1, min(int(page_limit), 256))
        max_documents = _corpus_health_max_documents()
        totals = await self.corpus_totals()
        total_documents = totals.get("documents")
        if (
            isinstance(total_documents, bool)
            or not isinstance(total_documents, int)
            or total_documents < 0
        ):
            raise RuntimeError("corpus totals returned an invalid document count")

        base: dict[str, Any] = {
            "dataset": dataset,
            "budget": chunk_window.resolve_chunk_budget(),
            "relational_documents": total_documents,
            "probe_limit": probe_limit,
            "probe_max_documents": max_documents,
            "documents_scanned": 0,
            "pages": 0,
            "zero_chunk_count": 0,
            "zero_chunk_document_ids": [],
            "zero_chunk_documents": [],
            "zero_chunk_documents_truncated": False,
            "oversized_document_count": 0,
            "oversized_chunk_count": 0,
            "oversized_document_ids": [],
            "oversized_documents": [],
            "oversized_documents_truncated": False,
            "zero_repair_document_ids": [],
            "oversized_repair_document_ids": [],
            "repair_document_ids": [],
            "repair_document_datasets": {},
            "repair_datasets": [],
            "unassigned_zero_chunk_document_count": 0,
            "unassigned_oversized_document_count": 0,
            "orphan_oversized_document_count": 0,
            "missing_document_id_violation_count": 0,
            "stored_chunk_budget": None,
            "census_complete": False,
        }
        if total_documents > max_documents:
            return {
                **base,
                "ok": False,
                "reason": "document_cap_exceeded",
                "cap_exceeded": True,
            }

        after_created_at: str | None = None
        after_id: str | None = None
        seen_ids: set[str] = set()
        corpus_rows_by_id: dict[str, dict[str, Any]] = {}
        zero_ids: set[str] = set()
        zero_repair_ids: set[str] = set()
        zero_documents: list[dict[str, Any]] = []
        zero_unassigned = 0
        pages = 0

        while len(seen_ids) < total_documents:
            limit = min(
                probe_limit,
                max_documents - len(seen_ids),
                total_documents - len(seen_ids),
            )
            rows = list(
                await self.corpus_page(
                    after_created_at=after_created_at,
                    after_id=after_id,
                    limit=limit,
                )
                or []
            )
            if not rows:
                break
            if len(rows) > limit:
                raise RuntimeError("corpus page exceeded its requested limit")

            page_ids: list[str] = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise RuntimeError("corpus page returned a row without an id")
                document_id = str(row["id"])
                if document_id in seen_ids:
                    raise RuntimeError(
                        f"corpus page returned duplicate document {document_id}"
                    )
                seen_ids.add(document_id)
                page_ids.append(document_id)
                corpus_rows_by_id[document_id] = row

            chunk_counts = await self.corpus_chunk_counts(page_ids)
            if chunk_counts is None:
                return {
                    **base,
                    "ok": False,
                    "reason": "vector_measurement_unavailable",
                    "cap_exceeded": False,
                    "documents_scanned": len(seen_ids),
                    "pages": pages,
                    "census_complete": False,
                }
            for document_id, count in chunk_counts.items():
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or document_id not in page_ids
                ):
                    raise RuntimeError("vector chunk measurement returned invalid counts")
            for row in rows:
                document_id = str(row["id"])
                if int(chunk_counts.get(document_id, 0)) != 0:
                    continue
                row_datasets = row.get("datasets")
                datasets = (
                    sorted({str(value) for value in row_datasets if value})
                    if isinstance(row_datasets, list)
                    else []
                )
                if dataset and dataset not in datasets:
                    continue
                zero_ids.add(document_id)
                if datasets:
                    zero_repair_ids.add(document_id)
                else:
                    zero_unassigned += 1
                if len(zero_documents) < MAX_ZERO_CHUNK_REPORT_DOCUMENTS:
                    zero_documents.append(
                        {
                            "id": document_id,
                            "name": row.get("name"),
                            "datasets": datasets,
                            "created_at": row.get("created_at"),
                        }
                    )
            pages += 1

            if len(seen_ids) >= total_documents:
                break
            last_row = rows[-1]
            if not last_row.get("created_at") or not last_row.get("id"):
                raise RuntimeError("corpus page cannot form a keyset cursor")
            after_created_at = str(last_row["created_at"])
            after_id = str(last_row["id"])

        complete = len(seen_ids) == total_documents
        if not complete:
            return {
                **base,
                "ok": False,
                "reason": "incomplete_corpus_walk",
                "cap_exceeded": False,
                "documents_scanned": len(seen_ids),
                "pages": pages,
                "census_complete": False,
            }

        check = await self.stored_chunk_budget_check(None, budget=base["budget"])
        if check is None:
            return {
                **base,
                "ok": False,
                "reason": "vector_measurement_unavailable",
                "cap_exceeded": False,
                "documents_scanned": len(seen_ids),
                "pages": pages,
                "zero_chunk_count": len(zero_ids),
                "zero_chunk_document_ids": sorted(zero_ids),
                "zero_chunk_documents": zero_documents,
                "zero_chunk_documents_truncated": len(zero_ids) > len(zero_documents),
                "census_complete": False,
            }
        violation_counts = check.get("violation_document_counts")
        violation_count = check.get("violation_count")
        missing_count = check.get("missing_document_id_violation_count", 0)
        if (
            check.get("scope") not in {None, "full"}
            or not isinstance(violation_counts, dict)
            or isinstance(violation_count, bool)
            or not isinstance(violation_count, int)
            or violation_count < 0
            or isinstance(missing_count, bool)
            or not isinstance(missing_count, int)
            or missing_count < 0
        ):
            raise RuntimeError("stored chunk budget returned invalid census metadata")
        grouped_violation_count = 0
        for raw_document_id, raw_count in violation_counts.items():
            if (
                not raw_document_id
                or isinstance(raw_count, bool)
                or not isinstance(raw_count, int)
                or raw_count < 1
            ):
                raise RuntimeError("stored chunk budget returned invalid violation count")
            grouped_violation_count += raw_count
        if grouped_violation_count + missing_count != violation_count:
            raise RuntimeError("stored chunk budget violation totals do not reconcile")

        oversized_ids: set[str] = set()
        oversized_repair_ids: set[str] = set()
        oversized_documents: list[dict[str, Any]] = []
        # ``stored_chunk_budget_check`` is intentionally a full-corpus scan.
        # Keep the public census totals scoped to ``dataset`` so a violation in
        # another dataset cannot turn a clean repair request into a false
        # candidate.
        oversized_chunk_count = 0
        oversized_unassigned = 0
        orphan_count = 0
        for raw_document_id, raw_count in violation_counts.items():
            document_id = str(raw_document_id)
            row = corpus_rows_by_id.get(document_id)
            if row is None:
                orphan_count += 1
                oversized_unassigned += 1
                oversized_ids.add(document_id)
                oversized_chunk_count += raw_count
                if len(oversized_documents) < MAX_OVERSIZED_CHUNK_REPORT_DOCUMENTS:
                    oversized_documents.append(
                        {
                            "id": document_id,
                            "name": None,
                            "datasets": [],
                            "created_at": None,
                            "violation_count": raw_count,
                            "unrepairable": True,
                        }
                    )
                continue

            row_datasets = row.get("datasets")
            datasets = (
                sorted({str(value) for value in row_datasets if value})
                if isinstance(row_datasets, list)
                else []
            )
            if dataset and dataset not in datasets:
                continue
            oversized_ids.add(document_id)
            oversized_chunk_count += raw_count
            if datasets:
                oversized_repair_ids.add(document_id)
            else:
                oversized_unassigned += 1
            if len(oversized_documents) < MAX_OVERSIZED_CHUNK_REPORT_DOCUMENTS:
                oversized_documents.append(
                    {
                        "id": document_id,
                        "name": row.get("name"),
                        "datasets": datasets,
                        "created_at": row.get("created_at"),
                        "violation_count": raw_count,
                    }
                )

        repair_ids = zero_repair_ids | oversized_repair_ids
        repair_document_datasets: dict[str, list[str]] = {}
        for document_id in sorted(repair_ids):
            row = corpus_rows_by_id[document_id]
            row_datasets = row.get("datasets")
            datasets = (
                sorted({str(value) for value in row_datasets if value})
                if isinstance(row_datasets, list)
                else []
            )
            repair_document_datasets[document_id] = [dataset] if dataset else datasets

        return {
            **base,
            "ok": True,
            "reason": None,
            "cap_exceeded": False,
            "documents_scanned": len(seen_ids),
            "pages": pages,
            "zero_chunk_count": len(zero_ids),
            "zero_chunk_document_ids": sorted(zero_ids),
            "zero_chunk_documents": zero_documents,
            "zero_chunk_documents_truncated": len(zero_ids) > len(zero_documents),
            "oversized_document_count": len(oversized_ids),
            "oversized_chunk_count": oversized_chunk_count,
            "oversized_document_ids": sorted(oversized_ids),
            "oversized_documents": oversized_documents,
            "oversized_documents_truncated": len(oversized_ids) > len(oversized_documents),
            "zero_repair_document_ids": sorted(zero_repair_ids),
            "oversized_repair_document_ids": sorted(oversized_repair_ids),
            "repair_document_ids": sorted(repair_ids),
            "repair_document_datasets": repair_document_datasets,
            "repair_datasets": sorted(
                {
                    name
                    for names in repair_document_datasets.values()
                    for name in names
                }
            ),
            "unassigned_zero_chunk_document_count": zero_unassigned,
            "unassigned_oversized_document_count": oversized_unassigned,
            "orphan_oversized_document_count": orphan_count,
            "missing_document_id_violation_count": missing_count,
            "stored_chunk_budget": check,
            "census_complete": True,
        }

    async def graph_chunk_ids_for_documents(
        self, document_ids: list[str]
    ) -> set[str] | None:
        """Return graph node ids for ``DocumentChunk`` properties by source id."""
        if not document_ids:
            return set()
        engine = await self._graph_engine()
        query = getattr(engine, "query", None)
        if not callable(query):
            return None

        requested = {str(document_id) for document_id in document_ids}
        query_text = """
        MATCH (n:Node)
        WITH n.id AS node_id,
             n.type AS node_type,
             CAST(json_extract(n.properties, '$.document_id') AS STRING) AS document_id_json
        WHERE node_type = 'DocumentChunk'
          AND document_id_json IN $document_ids_json
        RETURN node_id, node_type, document_id_json
        """
        rows = await query(
            query_text,
            {"document_ids_json": [json.dumps(document_id) for document_id in requested]},
        )
        node_ids: set[str] = set()
        for row in rows or []:
            if not isinstance(row, (tuple, list)) or len(row) != 3:
                return None
            node_id, node_type, value = row
            if isinstance(node_id, bytes):
                node_id = node_id.decode("utf-8")
            if node_id is None:
                return None
            if isinstance(node_type, bytes):
                node_type = node_type.decode("utf-8")
            if node_type != "DocumentChunk":
                return None
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            if isinstance(value, str):
                raw_value = value
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    # Ladybug may return a plain string instead of JSON.
                    value = raw_value
            if not isinstance(value, str) or value not in requested:
                return None
            node_ids.add(str(node_id))
        return node_ids

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str] | None:
        """Which source documents have a graph projection; None when unmeasured.

        Cognee's TextChunker gives DocumentChunk nodes their own ids. The
        relational ``Data.id`` is copied into each chunk's ``document_id``
        property, so probing graph node ids with Data ids is invalid. Query the
        graph property instead, returning one distinct source id per page. An
        engine without raw query support returns None rather than pretending it
        measured presence.
        """
        if not document_ids:
            return set()
        requested_datasets = list(
            dict.fromkeys(
                str(dataset).strip()
                for dataset in datasets or []
                if str(dataset).strip()
            )
        )
        if requested_datasets:
            from cognee.context_global_variables import (
                set_database_global_context_variables,
            )
            from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
                resolve_authorized_user_datasets,
            )

            _, authorized_datasets = await resolve_authorized_user_datasets(
                requested_datasets,
                None,
            )
            if len(authorized_datasets) != len(requested_datasets):
                raise RuntimeError(
                    "graph presence did not resolve every requested dataset"
                )
            present: set[str] = set()
            for dataset in authorized_datasets:
                async with set_database_global_context_variables(
                    dataset.id,
                    dataset.owner_id,
                ):
                    scoped = await self._corpus_graph_presence_current_context(
                        document_ids
                    )
                if scoped is None:
                    return None
                present.update(scoped)
            return present
        return await self._corpus_graph_presence_current_context(document_ids)

    async def _corpus_graph_presence_current_context(
        self,
        document_ids: list[str],
    ) -> set[str] | None:
        """Read graph presence inside the caller's active Cognee dataset context."""
        engine = await self._graph_engine()
        query = getattr(engine, "query", None)
        if not callable(query):
            return None

        requested = {str(document_id) for document_id in document_ids}
        query_text = """
        MATCH (n:Node)
        WITH n.type AS node_type,
             CAST(json_extract(n.properties, '$.document_id') AS STRING) AS document_id_json
        WHERE node_type = 'DocumentChunk'
          AND document_id_json IN $document_ids_json
        RETURN DISTINCT document_id_json
        """
        rows = await query(
            query_text,
            {"document_ids_json": [json.dumps(document_id) for document_id in requested]},
        )
        present: set[str] = set()
        for row in rows or []:
            if not isinstance(row, (tuple, list)) or len(row) != 1:
                return None
            value = row[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    # Ladybug may return a plain string instead of a JSON string;
                    # keep it unchanged and let the membership check below validate it.
                    pass
            if not isinstance(value, str) or value not in requested:
                return None
            present.add(value)
        return present

    async def ensure_dataset(self, name: str) -> bool:
        """Provision the cognee Dataset row for ``name``, returning whether it was new.

        cognee only creates a Dataset row on the WRITE path: ``cognee.add``
        resolves the name and calls ``create_authorized_dataset``. The read path
        does not, so it raises ``DatasetNotFoundError`` instead. A seat whose
        holder has never successfully ingested therefore has no row, and every
        search against their node fails (#147). Provision at seat creation
        rather than waiting for a write that may never come.

        The permission rows are not optional. The read path resolves a name in
        two steps, and only the first one is about the row existing:
        ``get_dataset_ids`` matches on name, owner and tenant, then
        ``get_specific_user_permission_datasets`` filters by ACL. With the row
        but no ACL a search fails just as hard, only with ``PermissionDeniedError``
        instead. ``create_authorized_dataset`` writes both.

        Idempotent on both halves, so backfilling an already-healthy seat is a
        no-op: ``create_dataset`` selects on (name, owner, tenant) before it
        inserts, and ``give_permission_on_dataset`` looks for the ACL row before
        adding it.

        Relational store only. This never opens the graph, so it cannot collide
        with the single Kuzu writer or with an in-flight cognify.
        """
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.modules.data.methods import create_authorized_dataset, get_datasets
        from cognee.modules.users.methods import get_default_user
        from sqlalchemy.exc import IntegrityError

        user = await get_default_user()
        # Read FIRST, purely to decide what to report. Mirror create_dataset's
        # own uniqueness filter: get_datasets narrows by owner only, so the
        # tenant check has to happen here or a seat in another tenant reads as
        # already provisioned.
        existing = await get_datasets(user.id)
        was_missing = not any(
            dataset.name == name and dataset.tenant_id == user.tenant_id for dataset in existing
        )
        # Then provision UNCONDITIONALLY. Returning early on a present row would
        # leave a half-provisioned seat broken forever: the row and its four ACL
        # rows are written by different statements, and give_permission_on_dataset
        # gives up after three attempts, so a partial failure is reachable. That
        # seat has a row, fails the existence check, and never gets repaired,
        # while its searches keep failing on PermissionDeniedError rather than
        # DatasetNotFoundError. Calling through costs four guarded no-op queries
        # on a healthy seat and repairs the broken one.
        try:
            await create_authorized_dataset(name, user)
        except IntegrityError:
            # Lost a creation race. create_dataset is SELECT-then-INSERT on a
            # deterministic uuid5 id (get_unique_dataset_id) and handles no
            # IntegrityError, so two writers for the same name collide on the
            # primary key. That is reachable here rather than theoretical:
            # Railway runs evolve and linear-sync as separate OS processes
            # against the same Postgres, and linear_sync writes into
            # seat:<slug>, so a boot backfill can meet an in-flight cognee.add.
            #
            # The row exists either way, which is the whole point of the call,
            # and the loser reports False because it did not create it.
            logger.info("ensure_dataset lost a creation race for %s", name)
            return False
        return was_missing

    async def delete_graph_nodes(self, node_ids: list[str]) -> int:
        """Delete nodes by id from BOTH the graph and the chunk vector store (#15).

        The same UUID identifies a DocumentChunk in the Kuzu graph AND in the
        DocumentChunk_text vector collection that CHUNKS search reads — so deleting
        only the graph node leaves the chunk searchable. Remove both. Serializes on
        the writer lock like cognify (single Kuzu writer, #47).
        """
        if not node_ids:
            return 0
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.graph import get_graph_engine

        engine = await get_graph_engine()
        async with self.writer_lock:
            await engine.delete_nodes(node_ids)
            await self._delete_vector_points(node_ids)
            self._invalidate_graph_data_cache()
        return len(node_ids)

    async def delete_document_chunks(self, document_ids: list[str]) -> dict[str, Any]:
        """Delete every old vector and graph chunk for source documents.

        Vector and graph ids are collected independently. Cognee 1.2.2 uses
        content-derived ids for some first chunks, so assuming both stores share
        the same id can leave a searchable stale row behind.
        """
        requested = list(dict.fromkeys(str(document_id) for document_id in document_ids))
        if not requested:
            return {
                "document_ids": [],
                "vector_chunk_count": 0,
                "graph_node_count": 0,
            }

        vector_ids = await self.stored_chunk_ids_for_documents(requested)
        if vector_ids is None:
            raise RuntimeError("stored chunk deletion is unavailable outside pgvector")
        graph_ids = await self.graph_chunk_ids_for_documents(requested)
        if graph_ids is None:
            raise RuntimeError("graph chunk deletion is unavailable")

        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.graph import get_graph_engine
        from cognee.infrastructure.databases.vector import get_vector_engine

        vector_uuids: list[UUID] = []
        for vector_id in vector_ids:
            try:
                vector_uuids.append(UUID(str(vector_id)))
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    "stored chunk collection returned a non-UUID row id"
                ) from exc

        graph_engine = await get_graph_engine()
        vector_engine = get_vector_engine()
        async with self.writer_lock:
            snapshot = await self._snapshot_document_chunks(
                document_ids=requested,
                vector_engine=vector_engine,
                graph_engine=graph_engine,
                vector_ids=vector_uuids,
                graph_ids=sorted(graph_ids),
            )
            snapshot_token = self._store_repair_snapshot(snapshot)
            try:
                if vector_uuids:
                    await vector_engine.delete_data_points(
                        "DocumentChunk_text", vector_uuids
                    )
                if graph_ids:
                    await graph_engine.delete_nodes(sorted(graph_ids))
                self._invalidate_graph_data_cache()
            except Exception as exc:
                restored = False
                try:
                    restored = await self._restore_document_chunk_snapshot_locked(
                        snapshot,
                        document_ids=requested,
                        vector_engine=vector_engine,
                        graph_engine=graph_engine,
                    )
                except Exception:  # noqa: BLE001 - preserve original delete failure
                    logger.exception("repair snapshot restore failed after delete error")
                if restored:
                    self._repair_snapshots.pop(snapshot_token, None)
                return {
                    "ok": False,
                    "document_ids": requested,
                    "vector_chunk_count": len(vector_uuids),
                    "graph_node_count": len(graph_ids),
                    "snapshot_token": snapshot_token if not restored else None,
                    "reason": "repair_delete_failed",
                    "error_type": exc.__class__.__name__,
                    "projections_preserved": restored,
                }
        return {
            "document_ids": requested,
            "vector_chunk_count": len(vector_uuids),
            "graph_node_count": len(graph_ids),
            "snapshot_token": snapshot_token,
        }

    def _store_repair_snapshot(self, snapshot: dict[str, Any]) -> str:
        """Keep rollback data private and return only an opaque handle."""
        if len(self._repair_snapshots) >= MAX_REPAIR_SNAPSHOTS:
            raise RuntimeError("repair snapshot capacity exhausted")
        token = secrets.token_urlsafe(24)
        self._repair_snapshots[token] = snapshot
        return token

    @staticmethod
    def _graph_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    @classmethod
    def _graph_properties(cls, value: Any) -> str:
        value = value.decode("utf-8") if isinstance(value, bytes) else value
        if isinstance(value, str):
            return value
        if value is None:
            return "{}"
        try:
            return json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("graph properties are not serializable") from exc

    async def _snapshot_vector_rows(
        self, vector_engine: Any, vector_ids: list[UUID]
    ) -> list[dict[str, Any]]:
        if not vector_ids:
            return []
        from sqlalchemy import select

        try:
            table = await vector_engine.get_table("DocumentChunk_text")
            statement = select(table.c.id, table.c.payload, table.c.vector).where(
                table.c.id.in_(vector_ids)
            )
        except Exception as exc:  # noqa: BLE001 - snapshot must be complete
            raise RuntimeError("vector projection snapshot is unavailable") from exc

        async with vector_engine.get_async_session() as session:
            rows = (await session.execute(statement)).all()
        expected = {str(vector_id) for vector_id in vector_ids}
        actual = {str(row[0]) for row in rows if row[0] is not None}
        if actual != expected:
            raise RuntimeError(
                "vector projection changed while preparing repair snapshot"
            )
        return [
            {
                "id": row[0],
                "payload": copy.deepcopy(row[1]),
                "vector": copy.deepcopy(row[2]),
            }
            for row in rows
        ]

    async def _snapshot_graph_projection(
        self, graph_engine: Any, graph_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not graph_ids:
            return {"nodes": [], "edges": []}
        query = getattr(graph_engine, "query", None)
        if not callable(query):
            raise RuntimeError("graph projection snapshot is unavailable")

        node_rows = await query(
            """
            MATCH (n:Node)
            WHERE n.id IN $node_ids
            RETURN n.id, n.name, n.type, n.properties
            """,
            {"node_ids": graph_ids},
        )
        nodes: list[dict[str, Any]] = []
        for row in node_rows or []:
            if not isinstance(row, (tuple, list)) or len(row) != 4 or row[0] is None:
                raise RuntimeError("graph node snapshot returned an invalid row")
            nodes.append(
                {
                    "id": self._graph_text(row[0]),
                    "name": self._graph_text(row[1]),
                    "type": self._graph_text(row[2]),
                    "properties": self._graph_properties(row[3]),
                }
            )
        if {node["id"] for node in nodes} != set(graph_ids):
            raise RuntimeError(
                "graph projection changed while preparing repair snapshot"
            )

        edge_rows = await query(
            """
            MATCH (source:Node)-[r:EDGE]->(target:Node)
            WHERE source.id IN $node_ids OR target.id IN $node_ids
            RETURN source.id, target.id, r.relationship_name, r.properties
            """,
            {"node_ids": graph_ids},
        )
        edges: list[dict[str, Any]] = []
        for row in edge_rows or []:
            if not isinstance(row, (tuple, list)) or len(row) != 4:
                raise RuntimeError("graph edge snapshot returned an invalid row")
            if row[0] is None or row[1] is None or row[2] is None:
                raise RuntimeError("graph edge snapshot returned an incomplete row")
            edges.append(
                {
                    "source": self._graph_text(row[0]),
                    "target": self._graph_text(row[1]),
                    "relationship": self._graph_text(row[2]),
                    "properties": self._graph_properties(row[3]),
                }
            )
        return {"nodes": nodes, "edges": edges}

    async def _snapshot_document_chunks(
        self,
        *,
        document_ids: list[str],
        vector_engine: Any,
        graph_engine: Any,
        vector_ids: list[UUID],
        graph_ids: list[str],
    ) -> dict[str, Any]:
        """Capture both projections before a destructive repair delete."""
        return {
            "document_ids": list(document_ids),
            "vector_rows": await self._snapshot_vector_rows(vector_engine, vector_ids),
            "graph": await self._snapshot_graph_projection(graph_engine, graph_ids),
        }

    async def _restore_vector_rows(
        self, vector_engine: Any, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        from sqlalchemy.dialects.postgresql import insert

        table = await vector_engine.get_table("DocumentChunk_text")
        values = [
            {
                "id": row["id"],
                "payload": copy.deepcopy(row["payload"]),
                "vector": copy.deepcopy(row["vector"]),
            }
            for row in rows
        ]
        statement = insert(table).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.id],
            set_={
                "payload": statement.excluded.payload,
                "vector": statement.excluded.vector,
            },
        )
        async with vector_engine.get_async_session() as session:
            await session.execute(statement)
            await session.commit()

    async def _restore_graph_projection(
        self, graph_engine: Any, graph: Mapping[str, Any]
    ) -> None:
        query = getattr(graph_engine, "query", None)
        if not callable(query):
            raise RuntimeError("graph projection restore is unavailable")
        nodes = list(graph.get("nodes") or [])
        edges = list(graph.get("edges") or [])
        if nodes:
            await query(
                """
                UNWIND $nodes AS node
                MERGE (n:Node {id: node.id})
                ON CREATE SET
                    n.name = node.name,
                    n.type = node.type,
                    n.properties = node.properties
                ON MATCH SET
                    n.name = node.name,
                    n.type = node.type,
                    n.properties = node.properties
                """,
                {"nodes": nodes},
            )
        if edges:
            await query(
                """
                UNWIND $edges AS edge
                MATCH (source:Node), (target:Node)
                WHERE source.id = edge.source AND target.id = edge.target
                MERGE (source)-[r:EDGE {
                    relationship_name: edge.relationship
                }]->(target)
                ON CREATE SET r.properties = edge.properties
                ON MATCH SET r.properties = edge.properties
                """,
                {"edges": edges},
            )
        checkpoint = getattr(graph_engine, "checkpoint", None)
        if callable(checkpoint):
            await checkpoint()

    async def _restore_document_chunk_snapshot_locked(
        self,
        snapshot: Mapping[str, Any],
        *,
        document_ids: list[str],
        vector_engine: Any,
        graph_engine: Any,
    ) -> bool:
        vector_ids = await self.stored_chunk_ids_for_documents(document_ids)
        graph_ids = await self.graph_chunk_ids_for_documents(document_ids)
        if vector_ids is None or graph_ids is None:
            return False
        current_vector_uuids: list[UUID] = []
        for vector_id in vector_ids:
            try:
                current_vector_uuids.append(UUID(str(vector_id)))
            except (TypeError, ValueError, AttributeError) as exc:
                raise RuntimeError("current vector projection has a non-UUID id") from exc
        if current_vector_uuids:
            await vector_engine.delete_data_points(
                "DocumentChunk_text", current_vector_uuids
            )
        if graph_ids:
            await graph_engine.delete_nodes(sorted(graph_ids))
        await self._restore_vector_rows(vector_engine, list(snapshot.get("vector_rows") or []))
        await self._restore_graph_projection(graph_engine, snapshot.get("graph") or {})
        self._invalidate_graph_data_cache()
        return True

    async def restore_document_chunks(self, deleted: Mapping[str, Any]) -> bool:
        """Restore a private exact snapshot after a failed repair operation."""
        token = deleted.get("snapshot_token")
        document_ids = deleted.get("document_ids")
        if not isinstance(token, str) or not isinstance(document_ids, list):
            return False
        snapshot = self._repair_snapshots.get(token)
        if snapshot is None:
            return False
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.graph import get_graph_engine
        from cognee.infrastructure.databases.vector import get_vector_engine

        async with self.writer_lock:
            restored = await self._restore_document_chunk_snapshot_locked(
                snapshot,
                document_ids=[str(document_id) for document_id in document_ids],
                vector_engine=get_vector_engine(),
                graph_engine=await get_graph_engine(),
            )
        if restored:
            self._repair_snapshots.pop(token, None)
        return restored

    async def discard_document_chunk_snapshot(self, deleted: Mapping[str, Any]) -> bool:
        """Release a successful repair's private rollback snapshot."""
        token = deleted.get("snapshot_token")
        if not isinstance(token, str):
            return False
        return self._repair_snapshots.pop(token, None) is not None

    async def _delete_vector_points(self, node_ids: list[str]) -> None:
        """Drop the same ids from the chunk vector collection (best-effort, #15)."""
        try:
            from uuid import UUID

            from cognee.infrastructure.databases.vector import get_vector_engine

            ids: list[Any] = []
            for node_id in node_ids:
                try:
                    ids.append(UUID(str(node_id)))
                except (ValueError, TypeError, AttributeError):
                    continue
            if ids:
                await get_vector_engine().delete_data_points("DocumentChunk_text", ids)
        except Exception:  # noqa: BLE001 - vector cleanup is best-effort
            logger.warning("vector-store cleanup delete skipped/failed", exc_info=True)

    async def _graph_engine(self) -> Any:
        """Return cognee's graph engine (seam for targeted reads and tests)."""
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.graph import get_graph_engine

        return await get_graph_engine()

    async def _document_graph(self, document_id: str) -> tuple[list[Any], list[Any]]:
        """Targeted read of one document node + its immediate connections (#28).

        Resolving a single id previously loaded the ENTIRE graph via
        ``graph_data()`` (5,382 nodes / 33,859 edges) on every drill-down click
        and every ``citadel_get_document`` call. Instead read only the document
        node (``get_node``) and its incident edges (``get_connections``) through
        the graph engine, returning the same ``(nodes, edges)`` tuple shape
        ``graph_data`` yields so the assembly logic in ``get_document`` is
        unchanged.

        Under ENABLE_BACKEND_ACCESS_CONTROL each dataset lives in its own graph
        database (ADR-0020) and ``get_graph_engine()`` resolves whichever
        per-dataset context the task last touched. ``get_node`` against that
        ambient store returned None for any id living in another store — no
        error, no fallback — which 404'd drill-down for ids the mesh itself was
        displaying (the measured DocumentChunk/TextSummary 404s). Mirror
        ``_read_graph_data``: probe each provisioned dataset store in the same
        deterministic order, first store that knows the id wins, ambient
        context restored afterwards. No provisioned stores (access control
        off, single-store deployments, local fakes) keeps the single
        ambient-context read unchanged.
        """
        try:
            self._prepare_cognee_environment()
            import cognee

            await self._ensure_cognee_ready(cognee)
            provisioned = await self._provisioned_dataset_databases()
        except Exception:  # noqa: BLE001 - relational read down -> ambient read
            provisioned = []
        if not provisioned:
            return await self._document_graph_current_context(document_id)
        for dataset_id, owner_id in provisioned:
            try:
                nodes, edges = await self._targeted_read_for_dataset(
                    dataset_id, owner_id, document_id
                )
            except Exception as exc:  # noqa: BLE001 - one bad store must not 404 the rest
                logger.warning(
                    "targeted document read skipped dataset %s: %s",
                    dataset_id,
                    exc.__class__.__name__,
                )
                continue
            if nodes:
                return nodes, edges
        return [], []

    async def _targeted_read_for_dataset(
        self, dataset_id: UUID, owner_id: UUID, document_id: str
    ) -> tuple[list[Any], list[Any]]:
        """One store's targeted read, ambient context restored afterwards
        (same context contract as ``_read_graph_data_for_dataset``)."""
        from cognee.context_global_variables import (
            graph_db_config,
            set_database_global_context_variables,
            vector_db_config,
        )
        from cognee.infrastructure.files.storage.config import file_storage_config

        prior = [
            (variable, variable.get(None))
            for variable in (graph_db_config, vector_db_config, file_storage_config)
        ]
        try:
            async with set_database_global_context_variables(dataset_id, owner_id):
                return await self._document_graph_current_context(document_id)
        finally:
            for variable, value in prior:
                variable.set(value)

    async def _document_graph_current_context(
        self, document_id: str
    ) -> tuple[list[Any], list[Any]]:
        """One store's targeted read via the engine the active context resolves.

        Falls back to the full ``graph_data()`` read when the engine lacks the
        targeted primitives or the read raises — a shape surprise degrades to
        correct-but-slow, never a spurious 404. A node missing from this store
        returns an empty result (``get_node`` -> None, ``get_connections`` ->
        []) WITHOUT triggering that fallback: a full graph read cannot contain
        a node the graph lacks. Ids no store resolves are instead retried
        against the durable chunk store by ``get_document``
        (``_document_from_chunk_store``).
        """
        try:
            engine = await self._graph_engine()
        except Exception:  # noqa: BLE001 - engine unavailable -> full-read fallback
            engine = None
        get_node = getattr(engine, "get_node", None)
        get_connections = getattr(engine, "get_connections", None)
        if engine is None or not callable(get_node) or not callable(get_connections):
            return await self.graph_data()
        try:
            nodes: list[Any] = []
            edges: list[Any] = []
            seen: set[str] = set()
            doc_props = await get_node(document_id)
            if isinstance(doc_props, dict):
                nodes.append((document_id, doc_props))
                seen.add(str(document_id))
            for source, relationship, target in await get_connections(document_id):
                if not isinstance(source, dict) or not isinstance(target, dict):
                    continue
                source_id = str(source.get("id"))
                target_id = str(target.get("id"))
                rel_name = (
                    relationship.get("relationship_name")
                    if isinstance(relationship, dict)
                    else str(relationship)
                )
                for node_id, node_props in ((source_id, source), (target_id, target)):
                    if node_id not in seen:
                        seen.add(node_id)
                        nodes.append((node_id, node_props))
                edges.append((source_id, target_id, rel_name or "related", {}))
            return nodes, edges
        except Exception:  # noqa: BLE001 - targeted read surprise -> full-read fallback
            logger.warning(
                "targeted document graph read failed; falling back to full graph read",
                exc_info=True,
            )
            return await self.graph_data()

    async def get_document(
        self, document_id: str, *, chunk_scope: bool = False
    ) -> dict[str, Any] | None:
        """Resolve a search-hit node id back to its stored text (#28).

        cognee search hits carry a graph node/chunk id with no backing document
        store, so ``/api/documents`` previously 404'd on every cognee hit. Look
        the node up in the graph and return its text plus the remaining
        properties. Document nodes (e.g. TextDocument) carry no text themselves
        — it lives on linked DocumentChunk nodes — so a textless match is
        assembled from its ``is_part_of`` chunk neighbors, ordered by
        ``chunk_index``. A CHUNK id (what every CHUNKS search hit carries) is
        resolved through its ``is_part_of`` parent document so drill-down
        returns the WHOLE document, not just the ~2K fragment search already
        showed. When the graph cannot resolve the id at all, fall back to the
        durable chunk store search reads from (``_document_from_chunk_store``)
        — the graph and chunk stores can diverge, and a hit that search can
        retrieve must stay reachable through drill-down. ``None`` only when no
        text exists in either store (textless entities keep resolving to
        None/404).

        ``chunk_scope=True`` (the /api/documents ``?scope=chunk`` contract)
        keeps a chunk id's OWN text instead of assembling the parent: the
        graph inspector wants the clicked passage, not the whole document.
        Ownership is unchanged — the ``is_part_of`` parent still rides in
        ``dataset_node_ids``, so the ADR-0009 gate decides exactly as it does
        for the default read. Non-chunk ids (documents, summaries, textless
        entities) behave as without the flag.
        """
        doc_id = str(document_id)
        document = await self._document_from_graph(
            doc_id, follow_parent=True, chunk_scope=chunk_scope
        )
        if document is not None:
            return document
        return await self._document_from_chunk_store(doc_id, chunk_scope=chunk_scope)

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        """Owner node ids for the ADR-0009 drill-down visibility rule — WITHOUT
        assembling the document body.

        /search resolves its drill-down hint once per unique hit id per page,
        and the hint needs only ``dataset_node_ids``. Answering that through
        ``get_document`` paid for the full body assembly per id — for a
        graph-missing id that is the ``_CHUNK_SIBLING_PROBE_LIMIT``-id vector
        retrieve returning up to ~1 MB of chunk payloads, all discarded. This
        read is bounded instead: ONE targeted graph read, and (only when the
        graph lacks the id) at most two single-id vector retrieves. The full
        assembly stays on /api/documents, one document per explicit click.

        Returns the same owner ids ``get_document`` carries in
        ``dataset_node_ids`` (a chunk id resolves through its ``is_part_of``
        parent document, whose node id is the relational Data.id the read-scope
        map keys on), or ``None`` when the id would not resolve. Corner cases
        this cheap read cannot see (a transient second graph read failing, a
        chunk row whose text vanished while siblings survived) return ``None``
        — the hint then under-promises (a 200 it did not advertise), never an
        ADR-0009 404 it promised was a document.
        """
        doc_id = str(document_id)
        try:
            nodes, edges = await self._document_graph(doc_id)
        except Exception as exc:  # noqa: BLE001
            if not self._is_no_data_error(exc):
                raise
            nodes, edges = [], []
        props, props_by_id, part_neighbor_ids = self._graph_projection(
            doc_id, nodes, edges
        )
        if props is not None:
            text, _ = self._extract_text(props)
            if text is not None:
                # Chunk id: its datasets live on the textless parent document
                # (the id get_document's parent-follow resolves through).
                parent_id = self._textless_neighbor_id(props_by_id, part_neighbor_ids)
                if parent_id is not None:
                    return [parent_id, doc_id]
                return [doc_id, *part_neighbor_ids]
            # Textless document node: drillable only when a text-bearing chunk
            # neighbor exists (textless entities must stay a 404).
            for neighbor_id in part_neighbor_ids:
                neighbor = props_by_id.get(neighbor_id)
                if neighbor is not None and self._extract_text(neighbor)[0] is not None:
                    return [doc_id]
            # No text anywhere in the graph: get_document falls through to the
            # chunk store, and so does this.
        return await self._owner_ids_from_chunk_store(doc_id)

    async def _owner_ids_from_chunk_store(self, doc_id: str) -> list[str] | None:
        """Owner ids from the durable chunk store via SEED lookups only.

        At most two single-id retrieves (string ids — see
        ``_document_from_chunk_store`` for why UUID objects break LanceDB):
        the requested id itself, then — when that misses — the deterministic
        chunk-0 probe ``uuid5(NAMESPACE_OID, f"{doc_id}-0")``, since
        TextChunker numbers chunks sequentially from zero. Never the sibling
        probe. Best effort like the full fallback: any failure returns ``None``
        (the hint denies, fail-closed).
        """
        try:
            requested = str(UUID(doc_id))
        except (TypeError, ValueError):
            # Synthetic ids (chunk:<sha256>, ghsync:*) have no chunk-store row.
            return None
        try:
            retrieve = await self._chunk_store_reader(requested)
            if retrieve is None:
                return None
            seed_rows = await retrieve(self._CHUNK_VECTOR_COLLECTION, [requested])
            seed_payload = (
                self._retrieved_payload(seed_rows[0]) if seed_rows else None
            )
            if seed_payload is not None:
                # Chunk hit: the parent document id rides in the payload —
                # exactly the owner attribution the full assembly reports.
                if self._extract_text(seed_payload)[0] is None:
                    return None
                parent_raw = seed_payload.get("document_id")
                if not parent_raw and isinstance(
                    seed_payload.get("is_part_of"), dict
                ):
                    parent_raw = seed_payload["is_part_of"].get("id")
                try:
                    parent_id = str(UUID(str(parent_raw))) if parent_raw else None
                except (TypeError, ValueError):
                    parent_id = None
                owner_ids = [parent_id] if parent_id is not None else []
                if doc_id not in owner_ids:
                    owner_ids.append(doc_id)
                return owner_ids
            # Not a chunk id — probe it as a document id through its chunk 0.
            probe_rows = await retrieve(
                self._CHUNK_VECTOR_COLLECTION,
                [str(uuid5(NAMESPACE_OID, f"{requested}-0"))],
            )
            probe_payload = (
                self._retrieved_payload(probe_rows[0]) if probe_rows else None
            )
            if probe_payload is None or self._extract_text(probe_payload)[0] is None:
                return None
            owner_ids = [requested]
            if doc_id not in owner_ids:
                owner_ids.append(doc_id)
            return owner_ids
        except Exception:  # noqa: BLE001 - best-effort; None == deny, fail-closed
            logger.warning(
                "chunk-store owner-id lookup failed; drill-down hint denies",
                exc_info=True,
            )
            return None

    @staticmethod
    def _extract_text(props: dict[str, Any]) -> tuple[str | None, str | None]:
        """First non-empty text field under the keys cognee nodes/chunks use."""
        for key in ("text", "chunk", "content", "raw_content"):
            value = props.get(key)
            if isinstance(value, str) and value.strip():
                return value, key
        return None, None

    @staticmethod
    def _graph_projection(
        doc_id: str, nodes: list[Any], edges: list[Any]
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], list[str]]:
        """(requested props, props by id, ``is_part_of`` neighbor ids) of one read.

        One pass over nodes: props by str id for O(1) neighbor lookup
        (setdefault keeps first-encounter semantics on duplicate ids).
        ``is_part_of`` neighbors (either direction) are collected once and used
        twice: assembling a textless document's body from its chunks, and
        dataset attribution for read-scope checks — a chunk id has no
        relational Data row of its own, so its datasets live on the linked
        document's node id. ``dataset_node_ids`` plumbs those candidate ids
        to the caller (kb/server.py drill-down isolation, ADR-0009) so the
        graph is never re-walked. Duplicate edges to one neighbor count once.
        Shared by ``_document_from_graph`` and ``resolve_document_owner_ids``
        so the hint's owner attribution can never drift from the endpoint's.
        """
        props_by_id: dict[str, dict[str, Any]] = {}
        for node_id, properties in nodes:
            props_by_id.setdefault(str(node_id), dict(properties or {}))
        part_neighbor_ids: list[str] = []
        seen: set[str] = set()
        for source_id, target_id, relationship, _edge_props in edges:
            if str(relationship) != "is_part_of":
                continue
            if str(source_id) == doc_id:
                neighbor_id = str(target_id)
            elif str(target_id) == doc_id:
                neighbor_id = str(source_id)
            else:
                continue
            if neighbor_id in seen:
                continue
            seen.add(neighbor_id)
            part_neighbor_ids.append(neighbor_id)
        return props_by_id.get(doc_id), props_by_id, part_neighbor_ids

    @classmethod
    def _textless_neighbor_id(
        cls, props_by_id: dict[str, dict[str, Any]], part_neighbor_ids: list[str]
    ) -> str | None:
        """First textless ``is_part_of`` neighbor: a chunk's parent document."""
        return next(
            (
                neighbor_id
                for neighbor_id in part_neighbor_ids
                if (neighbor := props_by_id.get(neighbor_id)) is not None
                and cls._extract_text(neighbor)[0] is None
            ),
            None,
        )

    async def _summary_owner_ids(
        self,
        doc_id: str,
        edges: list[Any],
        props_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Mappable owner ids for a text-bearing node with no ``is_part_of`` parent.

        TextSummary -[made_from]-> DocumentChunk -[is_part_of]-> TextDocument:
        only the last id is a relational ``Data.id`` the read-scope map keys
        on. One extra targeted read (the chunk's connections) recovers it.
        Best-effort: a node with no text-bearing ``made_from`` neighbor
        (ordinary documents) returns [] and changes nothing; a failed chase
        still returns the chunk id it found.
        """
        chunk_id: str | None = None
        for raw in edges:
            try:
                source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
            except (TypeError, IndexError, KeyError):
                continue
            if relationship != "made_from":
                continue
            if source == doc_id:
                other = target
            elif target == doc_id:
                other = source
            else:
                continue
            neighbor = props_by_id.get(other)
            if neighbor is not None and self._extract_text(neighbor)[0] is not None:
                chunk_id = other
                break
        if chunk_id is None:
            return []
        try:
            chunk_nodes, chunk_edges = await self._document_graph(chunk_id)
        except Exception:  # noqa: BLE001 - owner chase is best-effort
            return [chunk_id]
        _, chunk_props_by_id, chunk_part_ids = self._graph_projection(
            chunk_id, chunk_nodes, chunk_edges
        )
        parent_id = self._textless_neighbor_id(chunk_props_by_id, chunk_part_ids)
        if parent_id is None:
            return [chunk_id]
        return [chunk_id, parent_id]

    async def _document_from_graph(
        self, doc_id: str, *, follow_parent: bool, chunk_scope: bool = False
    ) -> dict[str, Any] | None:
        """Resolve ``doc_id`` against the graph store (targeted read).

        ``follow_parent=True`` (the drill-down entrypoint) additionally chases a
        text-bearing chunk's ``is_part_of`` parent document and returns the
        parent's FULL assembled body; the recursive parent call passes
        ``follow_parent=False`` so resolution is bounded at one hop.
        ``chunk_scope=True`` keeps the chunk's own text instead: no parent
        assembly read, the parent id recorded in ``metadata`` and (via
        ``part_neighbor_ids``) in ``dataset_node_ids`` as before.
        """
        try:
            nodes, edges = await self._document_graph(doc_id)
        except Exception as exc:  # noqa: BLE001
            if self._is_no_data_error(exc):
                return None
            raise

        props, props_by_id, part_neighbor_ids = self._graph_projection(
            doc_id, nodes, edges
        )
        if props is None:
            return None
        text, text_key = self._extract_text(props)
        if text is not None:
            summary_owner_ids: list[str] = []
            chunk_metadata: dict[str, Any] = {}
            if follow_parent:
                parent_id = self._textless_neighbor_id(props_by_id, part_neighbor_ids)
                if parent_id is not None and chunk_scope:
                    # ?scope=chunk: the caller wants the clicked passage, not
                    # the assembled parent — keep the chunk's own text and skip
                    # the parent assembly read. The parent still rides in
                    # part_neighbor_ids, so the ADR-0009 gate is unchanged.
                    chunk_metadata = {"chunk_id": doc_id, "document_id": parent_id}
                else:
                    # A text-bearing node with a TEXTLESS ``is_part_of``
                    # neighbor is a DocumentChunk next to its parent document
                    # (chunks carry the text; TextDocument nodes carry none).
                    # Search hands out CHUNK ids, so resolving the id to just
                    # this chunk's text re-serves the same fragment the search
                    # snippet already showed. Resolve the PARENT instead so
                    # drill-down returns the whole document; the chunk's own
                    # text stays the fallback when the parent cannot be
                    # assembled (never worse than before).
                    if parent_id is not None:
                        parent = await self._document_from_graph(
                            parent_id, follow_parent=False
                        )
                        if parent is not None and parent.get("body"):
                            owner_ids = list(
                                parent.get("dataset_node_ids") or [parent_id]
                            )
                            if doc_id not in owner_ids:
                                owner_ids.append(doc_id)
                            parent["dataset_node_ids"] = owner_ids
                            return parent
                    # No is_part_of parent: a TextSummary carries its own text
                    # but hangs off the content it summarizes via ``made_from``.
                    # Its id has no relational Data row, so owner ids of just
                    # [doc_id] fail-close the ADR-0009 drill-down gate on
                    # content the caller may read — chase the summarized
                    # chunk's parent document id.
                    summary_owner_ids = await self._summary_owner_ids(
                        doc_id, edges, props_by_id
                    )
            return {
                "id": doc_id,
                "source_type": "cognee",
                "title": props.get("title") or None,
                "body": text,
                "metadata": {
                    **{k: v for k, v in props.items() if k != text_key},
                    **chunk_metadata,
                },
                "dataset_node_ids": [doc_id, *part_neighbor_ids, *summary_owner_ids],
            }
        # Textless document node: its text lives on DocumentChunk neighbors
        # linked via ``is_part_of`` (chunk --is_part_of--> doc; stay
        # direction-agnostic on endpoints but ONLY follow is_part_of edges —
        # entities are also graph-adjacent to text-bearing chunks via
        # ``contains``/``mentions``, and assembling those would fabricate a
        # document for a textless entity, which must stay None (404).
        # Textless neighbors are skipped; chunks sort by numeric chunk_index,
        # unindexed ones trail in encounter order.
        chunks: list[tuple[tuple[int, float], str]] = []
        for neighbor_id in part_neighbor_ids:
            neighbor_props = props_by_id.get(neighbor_id)
            if neighbor_props is None:
                continue
            neighbor_text, _ = self._extract_text(neighbor_props)
            if neighbor_text is None:
                continue
            index = neighbor_props.get("chunk_index")
            if isinstance(index, (int, float)):
                sort_key = (0, float(index))
            else:
                sort_key = (1, float(len(chunks)))
            chunks.append((sort_key, neighbor_text))
        if not chunks:
            return None
        chunks.sort(key=lambda item: item[0])
        return {
            "id": doc_id,
            "source_type": "cognee",
            "title": props.get("title") or props.get("name") or None,
            "body": "\n\n".join(chunk_text for _, chunk_text in chunks),
            "chunk_count": len(chunks),
            "metadata": dict(props),
            "dataset_node_ids": [doc_id],
        }

    # Search hits come from the durable chunk vector collection (cognee's
    # ChunksRetriever reads ONLY "DocumentChunk_text"), while drill-down
    # resolves through the graph store. The two stores can diverge — an
    # interrupted cognify, or a graph emptied/rebuilt by a past incident while
    # the chunk store survived — leaving chunks that search retrieves but the
    # graph cannot resolve (the measured drill-down 404s). The chunk store is
    # therefore the fallback source of truth for document text.
    _CHUNK_VECTOR_COLLECTION = "DocumentChunk_text"
    # cognee's TextChunker assigns chunk ids deterministically:
    # uuid5(NAMESPACE_OID, f"{document_id}-{chunk_index}") — so a document's
    # chunks are recoverable with ONE batched retrieve over probe indexes
    # 0..N-1. 512 covers ~1 MB of text at the node's observed ~2K chunk size.
    # Known gap: a single paragraph larger than the chunk size gets a
    # content-derived id (TextChunker's oversized-paragraph branch), which an
    # index probe cannot enumerate; such a chunk is included only when it is
    # the requested id itself.
    _CHUNK_SIBLING_PROBE_LIMIT = 512

    async def _vector_engine(self) -> Any:
        """Return cognee's vector engine (seam for chunk-store reads and tests)."""
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.vector import get_vector_engine

        return get_vector_engine()

    @staticmethod
    def _retrieved_payload(row: Any) -> dict[str, Any] | None:
        """Payload dict of one vector-store ``retrieve`` row (ScoredResult)."""
        payload = getattr(row, "payload", None)
        return payload if isinstance(payload, dict) else None

    async def _owning_datasets(self, doc_id: str) -> list[tuple[Any, Any]]:
        """``(dataset_id, owner_id)`` of every dataset holding ``doc_id``. READ ONLY.

        Deliberately NOT cognee's ``resolve_authorized_user_datasets``: that
        helper calls ``load_or_create_datasets``, so a name it cannot authorize
        for the caller is CREATED and granted. Pointing a drill-down at it gave
        this read path a relational write (measured: datasets 1 -> 2, acls 0 -> 4
        against a foreign-owned name) and, under the mandatory
        ENABLE_BACKEND_ACCESS_CONTROL=true, a cross-tenant dataset-creation path.
        A read must not be able to insert a Dataset row, a permission grant, or a
        default user, so this is the same unscoped DatasetData join
        ``dataset_membership_for_documents`` uses, returning the identity columns
        the dataset database context needs. Resolving by id rather than by name
        also means a name collision across owners cannot redirect the read.
        """
        try:
            document_uuid = UUID(str(doc_id))
        except (TypeError, ValueError):
            return []
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        from cognee.infrastructure.databases.relational import get_relational_engine
        from cognee.modules.data.models import Dataset, DatasetData

        from sqlalchemy import select

        engine = get_relational_engine()
        async with engine.get_async_session() as session:
            rows = await session.execute(
                select(Dataset.id, Dataset.owner_id)
                .join(DatasetData, DatasetData.dataset_id == Dataset.id)
                .where(DatasetData.data_id == document_uuid)
                .distinct()
            )
            return [(dataset_id, owner_id) for dataset_id, owner_id in rows.all()]

    async def _chunk_store_reader(
        self, doc_id: str
    ) -> Callable[[str, list[str]], Awaitable[list[Any]]] | None:
        """A chunk-store ``retrieve`` bound to the datasets that own ``doc_id``.

        ``get_vector_engine()`` outside a dataset context builds the Qdrant
        adapter with an EMPTY database name (cognee's ``vector_db_name`` default
        is ""), and that adapter refuses every operation carrying no Citadel
        scope. The unbound engine could therefore never read one chunk: the
        drill-down fallback failed closed on every id under this provider, no
        matter what the store held. Read inside the database context of each
        dataset that owns the document, the same way the chunk census does, over
        the read-only membership ``_owning_datasets`` resolves. Providers that
        enforce no scope keep the unbound engine, and so does an id with no
        membership row (a chunk id, a synthetic id) — the fallback stays
        best-effort and still degrades to 404. Reading the owning dataset is not
        a new privilege: the body still passes the ADR-0009 gate the caller
        applies to ``dataset_node_ids`` afterwards.
        """
        if os.getenv("VECTOR_DB_PROVIDER", "").strip().lower() != "qdrant":
            return await self._unbound_chunk_store_reader()
        datasets = await self._owning_datasets(doc_id)
        if not datasets:
            # A CHUNK id has no DatasetData row (membership keys on the parent
            # Data.id), so the unbound engine was the only reader left — and
            # under this provider it refuses every unscoped operation
            # (QdrantScopeError, the measured drill-down 404). Probe every
            # provisioned dataset store instead, the same rows the org-wide
            # graph read sweeps. Not a new privilege: the assembled body still
            # passes the caller's ADR-0009 gate over ``dataset_node_ids``.
            try:
                datasets = await self._provisioned_dataset_databases()
            except Exception:  # noqa: BLE001 - relational read down -> unbound reader
                datasets = []
        if not datasets:
            return await self._unbound_chunk_store_reader()
        from cognee.context_global_variables import (
            set_database_global_context_variables,
        )
        from cognee.infrastructure.databases.vector import get_vector_engine_async

        async def scoped_retrieve(
            collection_name: str, data_point_ids: list[str]
        ) -> list[Any]:
            rows: list[Any] = []
            for dataset_id, owner_id in datasets:
                async with set_database_global_context_variables(
                    dataset_id,
                    owner_id,
                ):
                    engine = await get_vector_engine_async()
                    retrieve = getattr(engine, "retrieve", None)
                    if not callable(retrieve):
                        continue
                    found = await retrieve(collection_name, data_point_ids)
                rows.extend(found or [])
            return rows

        return scoped_retrieve

    async def _unbound_chunk_store_reader(
        self,
    ) -> Callable[[str, list[str]], Awaitable[list[Any]]] | None:
        engine = await self._vector_engine()
        retrieve = getattr(engine, "retrieve", None)
        return retrieve if callable(retrieve) else None

    async def _document_from_chunk_store(
        self, doc_id: str, *, chunk_scope: bool = False
    ) -> dict[str, Any] | None:
        """Assemble a document from the durable chunk store when the graph can't.

        Accepts either a chunk id (resolved to its parent via the payload's
        ``document_id``) or a document id (siblings probed directly). Best
        effort by design: any failure degrades to ``None`` — exactly the 404
        the caller would have served anyway — never an error. Returns the same
        shape as the graph assembly; ``dataset_node_ids`` carries the parent
        document id, which is the relational ``Data.id`` the read-scope map
        keys on (ADR-0009), so the drill-down isolation gate is unchanged.
        ``chunk_scope=True`` with a chunk id serves the seed row's own text
        (no sibling probe); a non-chunk id ignores the flag.
        """
        try:
            # STRING ids, never uuid.UUID objects — cognee's own retrieve()
            # callers pass strings (hybrid/chunks.py), and LanceDBAdapter
            # renders the filter with an f-string, so a UUID object becomes
            # `id IN (UUID('…'),)` and the whole query errors out ("Error
            # optimizing sql filter"). pgvector tolerated the objects, which is
            # how the difference stayed invisible on the production node.
            requested = str(UUID(doc_id))
        except (TypeError, ValueError):
            # Synthetic ids (chunk:<sha256>, ghsync:*) have no chunk-store row.
            return None
        try:
            retrieve = await self._chunk_store_reader(requested)
            if retrieve is None:
                return None
            seed_rows = await retrieve(self._CHUNK_VECTOR_COLLECTION, [requested])
            seed_payload = (
                self._retrieved_payload(seed_rows[0]) if seed_rows else None
            )
            document_id: str | None
            if seed_payload is not None:
                # Chunk hit: the parent document id rides in the payload.
                parent_raw = seed_payload.get("document_id")
                if not parent_raw and isinstance(
                    seed_payload.get("is_part_of"), dict
                ):
                    parent_raw = seed_payload["is_part_of"].get("id")
                document_id = str(parent_raw) if parent_raw else None
            else:
                # Not a chunk id — probe it as a document id.
                document_id = doc_id
            payloads: dict[str, dict[str, Any]] = {}
            if seed_payload is not None:
                payloads[requested] = seed_payload
            if document_id is not None:
                try:
                    document_id = str(UUID(document_id))
                except (TypeError, ValueError):
                    document_id = None
            if chunk_scope and seed_payload is not None:
                seed_text, _ = self._extract_text(seed_payload)
                if seed_text is not None:
                    # ?scope=chunk on a graph-missing chunk: the seed row IS
                    # the clicked chunk — serve its own text, skip the sibling
                    # probe. Owner ids keep the parent Data.id first, exactly
                    # like the assembled shape, so the gate is unchanged.
                    owner_ids = [document_id] if document_id is not None else []
                    if requested not in owner_ids:
                        owner_ids.append(requested)
                    seed_metadata: dict[str, Any] = {
                        "assembled_from": "chunk_store",
                        "chunk_id": requested,
                    }
                    if document_id is not None:
                        seed_metadata["document_id"] = document_id
                    if seed_payload.get("document_name"):
                        seed_metadata["document_name"] = seed_payload["document_name"]
                    if isinstance(seed_payload.get("chunk_index"), (int, float)):
                        seed_metadata["chunk_index"] = seed_payload["chunk_index"]
                    return {
                        "id": requested,
                        "source_type": "cognee",
                        "title": seed_payload.get("document_name") or None,
                        "body": seed_text,
                        "metadata": seed_metadata,
                        "dataset_node_ids": owner_ids,
                    }
            if document_id is not None:
                sibling_ids = [
                    str(uuid5(NAMESPACE_OID, f"{document_id}-{index}"))
                    for index in range(self._CHUNK_SIBLING_PROBE_LIMIT)
                ]
                for row in await retrieve(
                    self._CHUNK_VECTOR_COLLECTION, sibling_ids
                ):
                    payload = self._retrieved_payload(row)
                    if payload is None:
                        continue
                    row_id = str(getattr(row, "id", "") or payload.get("id") or "")
                    if row_id:
                        payloads.setdefault(row_id, payload)
            chunks: list[tuple[tuple[int, float], dict[str, Any]]] = []
            for payload in payloads.values():
                text, _ = self._extract_text(payload)
                if text is None:
                    continue
                index = payload.get("chunk_index")
                if isinstance(index, (int, float)):
                    sort_key = (0, float(index))
                else:
                    sort_key = (1, float(len(chunks)))
                chunks.append((sort_key, payload))
            if not chunks:
                return None
            chunks.sort(key=lambda item: item[0])
            title = next(
                (
                    payload.get("document_name")
                    for _, payload in chunks
                    if payload.get("document_name")
                ),
                None,
            )
            metadata: dict[str, Any] = {"assembled_from": "chunk_store"}
            if document_id is not None:
                metadata["document_id"] = document_id
            if title is not None:
                metadata["document_name"] = title
            owner_ids = [document_id] if document_id is not None else []
            if doc_id not in owner_ids:
                owner_ids.append(doc_id)
            return {
                "id": document_id or doc_id,
                "source_type": "cognee",
                "title": title,
                "body": "\n\n".join(
                    self._extract_text(payload)[0] or "" for _, payload in chunks
                ),
                "chunk_count": len(chunks),
                "metadata": metadata,
                "dataset_node_ids": owner_ids,
            }
        except Exception:  # noqa: BLE001 - fallback is best-effort; None == 404
            logger.warning(
                "chunk-store drill-down fallback failed; document stays unresolved",
                exc_info=True,
            )
            return None

    @contextlib.asynccontextmanager
    async def maintenance(self) -> AsyncIterator[None]:
        """Hold the process-wide maintenance lock across a repair operation."""
        async with self.maintenance_lock:
            token = _COGNEE_MAINTENANCE_HELD.set(True)
            try:
                yield
            finally:
                _COGNEE_MAINTENANCE_HELD.reset(token)

    async def cognify(self, *, datasets: list[str], force: bool = False) -> Any:
        """Cognify under the maintenance lock unless a repair already holds it."""
        if _COGNEE_MAINTENANCE_HELD.get():
            return await self._cognify_unlocked(datasets=datasets, force=force)
        async with self.maintenance():
            return await self._cognify_unlocked(datasets=datasets, force=force)

    async def _cognify_unlocked(
        self, *, datasets: list[str], force: bool = False
    ) -> Any:
        """Cognify already-added data in ``datasets``.

        ``cognee.cognify`` defaults to ``incremental_loading=True``, so this only
        processes uncognified data and is idempotent over a dataset. It exists to
        recover data that was added but never cognified (e.g. a prior cognify
        failed with a bad LLM config). Pass ``force=True`` to set
        ``incremental_loading=False`` and reprocess data Cognee has marked
        "already processed" (use when the graph store is empty but the dataset
        reports as processed).
        """
        self._prepare_cognee_environment()
        # Fail loud on a missing LLM key. cognee swallows LLMAPIKeyNotSetError
        # inside its pipeline and returns normally, so a keyless cognify would
        # otherwise report success while indexing nothing (false-green exit 0).
        if not os.getenv("LLM_API_KEY"):
            raise RuntimeError(
                "LLM_API_KEY (or OPENROUTER_API_KEY) is not set; cognify requires an "
                "LLM key to extract the knowledge graph."
            )
        # Load the exact GPT-4o vocabulary before Cognee starts a write. If the
        # asset is missing, fail before a partial cognify can leave rows that the
        # persisted-chunk audit cannot measure.
        chunk_window.require_bpe_encoding()
        import cognee

        await self._ensure_cognee_ready(cognee)
        expected_ids = await self.dataset_document_ids(datasets) if force else []
        # Single Kuzu writer: serialize the graph write against any other in-process
        # cognify so they cannot collide on the lock (#47).
        async with self.writer_lock:
            result = await cognee.cognify(
                datasets=datasets,
                incremental_loading=not force,
                data_cache=not force,
            )
            self._invalidate_graph_data_cache()
        # The graph writer lock protects Kuzu only. Run the vector payload census
        # after releasing it so a slow SQL scan cannot block another cognify.
        processed_ids = _cognify_data_ids(result)
        processed_ids_by_dataset = _cognify_data_ids_by_dataset(result, datasets)
        if force:
            missing_ids = sorted(set(expected_ids) - set(processed_ids))
            if missing_ids:
                raise RuntimeError(
                    "forced cognify receipt omitted "
                    f"{len(missing_ids)} expected source id(s)"
                )
        if processed_ids:
            stored_check = await self.stored_chunk_budget_check(
                document_ids=processed_ids,
                datasets=datasets,
                document_ids_by_dataset=processed_ids_by_dataset,
            )
            if stored_check is None:
                logger.warning(
                    "stored chunk budget was not measured after cognify: "
                    "VECTOR_DB_PROVIDER does not support exact enumeration"
                )
            elif not stored_check["ok"]:
                missing = list(stored_check.get("missing_document_ids") or [])
                # Phase-2 incremental cognify sweeps docs the lifecycle drain
                # is still projecting into its receipt, so the census counts
                # their not-yet-written chunks as missing (#286, the 18:02Z
                # canary; same design family as #273, one layer deeper — the
                # check raced the drain). Partition the missing ids by job
                # state: a missing id WITH a pending/running projection is
                # in-flight, not a gap — the drain's own post-projection check
                # gates those chunks when they land. A missing id with NO
                # active job stays fatal (fail-closed), and violations are
                # fatal regardless.
                in_flight: set[str] = set()
                lookup = self.lifecycle_active_projection_lookup
                if missing and lookup is not None:
                    try:
                        in_flight = await asyncio.to_thread(lookup, list(missing))
                    except Exception:  # noqa: BLE001 - advisory read; absence fails closed
                        logger.warning(
                            "lifecycle in-flight lookup failed; treating all "
                            "missing document ids as fatal",
                            exc_info=True,
                        )
                        in_flight = set()
                fatal_missing = [
                    document_id
                    for document_id in missing
                    if document_id not in in_flight
                ]
                if (
                    stored_check["violation_count"] == 0
                    and missing
                    and not fatal_missing
                ):
                    logger.warning(
                        "stored chunk budget check: %d document id(s) missing "
                        "only because their lifecycle projection is still in "
                        "flight, not failing: %s",
                        len(missing),
                        missing[:10],
                    )
                else:
                    # Name the violators. The check already collects chunk_id
                    # and document_id per violation, but this site used to drop
                    # them and raise a bare count, so the 2026-08-13 canary
                    # failed for hours on one unidentifiable chunk out of ~1951
                    # with no surface anywhere naming the offending document.
                    named = "; ".join(
                        f"document {violation.get('document_id') or 'unknown'} "
                        f"chunk {violation.get('chunk_id') or 'unknown'} "
                        f"({violation.get('measured_tokens') or violation.get('configured_size')}"
                        f" tokens > budget {stored_check.get('budget')})"
                        for violation in (stored_check.get("violations") or [])[:3]
                    ) or "no violation rows"
                    if fatal_missing:
                        named += f"; missing document ids {fatal_missing[:3]}"
                    if in_flight:
                        named += (
                            f"; {len(in_flight)} more still in lifecycle projection"
                        )
                    logger.error(
                        "stored chunk budget check failed after cognify: "
                        "%d violation(s) across %d chunk(s): %s",
                        stored_check["violation_count"],
                        stored_check["chunks_scanned"],
                        named,
                    )
                    raise RuntimeError(
                        "stored chunk budget check failed: "
                        f"{stored_check['violation_count']} violation(s) across "
                        f"{stored_check['chunks_scanned']} persisted chunk(s): {named}"
                    )
        return result

    async def add_feedback(
        self,
        *,
        session_id: str,
        qa_id: str,
        score: int | None,
        text: str | None,
    ) -> bool:
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        return await cognee.session.add_feedback(
            session_id=session_id,
            qa_id=qa_id,
            feedback_score=score,
            feedback_text=text,
        )

    async def improve(
        self,
        *,
        dataset: str,
        session_ids: list[str] | None = None,
        build_global_context_index: bool = False,
    ) -> Any:
        self._prepare_cognee_environment()
        # Mirror cognify's fail-loud guard: cognee swallows LLMAPIKeyNotSetError
        # internally and returns normally, so a keyless improve would report
        # success while doing nothing (false-green exit 0, #41).
        if not os.getenv("LLM_API_KEY"):
            raise RuntimeError(
                "LLM_API_KEY (or OPENROUTER_API_KEY) is not set; improve requires an "
                "LLM key."
            )
        import cognee

        await self._ensure_cognee_ready(cognee)
        return await cognee.improve(
            dataset=dataset,
            session_ids=session_ids,
            build_global_context_index=build_global_context_index,
        )
