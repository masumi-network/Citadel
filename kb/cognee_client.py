from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from time import monotonic, perf_counter
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_OID, UUID, uuid5

from kb import chunk_window
from kb.logging_utils import configure_cognee_logging

logger = logging.getLogger(__name__)

# Strong refs to detached background cognify tasks so the loop does not GC them
# mid-flight (and so they can be awaited/observed in tests).
_BACKGROUND_COGNIFY_TASKS: set[Any] = set()

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
    # attribution does: specific Data/Dataset/DatasetData columns, the pgvector
    # adapter's table reflection + session, and the graph adapter's raw query
    # surface. Pin each one so a cognee bump that moves any of them fails loudly
    # at boot and in CI instead of quietly breaking the census.
    from cognee.infrastructure.databases.graph.ladybug.adapter import LadybugAdapter
    from cognee.infrastructure.databases.vector import get_vector_engine  # noqa: F401
    from cognee.infrastructure.databases.vector.pgvector.PGVectorAdapter import (
        PGVectorAdapter,
    )
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
    for adapter, method_name in (
        (PGVectorAdapter, "get_table"),
        (PGVectorAdapter, "get_async_session"),
        (LadybugAdapter, "query"),
    ):
        if not callable(getattr(adapter, method_name, None)):
            raise RuntimeError(
                f"cognee {adapter.__name__} no longer exposes {method_name}; "
                "the kb.cognee_client corpus census needs updating"
            )


_SUPPRESS_INLINE_COGNIFY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "citadel_suppress_inline_cognify", default=False
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
        defer_cognify: bool = False,
    ) -> Any:
        ...

    def schedule_cognify(self, datasets: list[str]) -> None:
        ...

    async def recall(
        self,
        query: str,
        *,
        dataset: str,
        session_id: str | None = None,
        top_k: int = 10,
    ) -> list[Any]:
        ...

    async def add_feedback(
        self,
        *,
        session_id: str,
        qa_id: str,
        score: int | None,
        text: str | None,
    ) -> bool:
        ...

    async def improve(
        self,
        *,
        dataset: str,
        session_ids: list[str] | None = None,
        build_global_context_index: bool = False,
    ) -> Any:
        ...

    async def cognify(self, *, datasets: list[str], force: bool = False) -> Any:
        ...

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        ...

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        ...

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        ...

    async def corpus_health(self, *, limit: int = 64) -> dict[str, Any]:
        ...

    async def corpus_zero_chunk_documents(
        self,
        *,
        dataset: str | None = None,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        ...

    async def delete_graph_nodes(self, node_ids: list[str]) -> int:
        ...


class CogneePublicClient:
    def __init__(self) -> None:
        self._startup_migrations_done = False
        # Serializes graph writes within this process — Kuzu is a single-writer
        # embedded DB, so two overlapping cognify calls (an inline ingest cognify,
        # the evolve scheduler, /api/cognify/run) must not collide (#47). One client
        # per Citadel; the app uses a single Citadel singleton, so this is the
        # process-wide writer gate. The evolve scheduler also holds it across its
        # Phase-1 subprocess so the web never cognifies while the subprocess owns
        # the on-disk Kuzu lock.
        self.writer_lock = asyncio.Lock()
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
        self._ensure_auto_feedback_default()
        self._ensure_chunk_budget()

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
        return exc.__class__.__name__ == "NoDataError" or "No data found in the system" in str(exc)

    async def _create_cognee_database(self) -> None:
        from cognee.infrastructure.databases.relational import get_relational_engine

        db_engine = get_relational_engine()
        await db_engine.create_database()

    def _data_with_metadata(self, data: Any, metadata: dict[str, Any] | None) -> Any:
        if not metadata:
            return data
        try:
            from cognee.tasks.ingestion.data_item import DataItem
        except Exception:
            return data

        def attach(item: Any) -> Any:
            if isinstance(item, DataItem):
                merged = {**(item.external_metadata or {}), **metadata}
                return DataItem(
                    data=item.data,
                    label=item.label,
                    external_metadata=merged,
                    data_id=item.data_id,
                )
            return DataItem(data=item, external_metadata=metadata)

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
        run_startup_migrations = getattr(cognee, "run_startup_migrations", None)
        if run_startup_migrations is not None:
            try:
                await run_startup_migrations()
            except Exception as exc:
                logger.warning(
                    "Cognee startup migrations failed with %s; creating database and retrying",
                    exc.__class__.__name__,
                )
                await self._create_cognee_database()
                await run_startup_migrations()
        self._startup_migrations_done = True
        logger.info("Cognee startup migrations completed")

    async def remember(
        self,
        data: Any,
        *,
        dataset_name: str,
        session_id: str | None = None,
        tags: tuple[str, ...] = (),
        defer_cognify: bool = False,
    ) -> Any:
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
        metadata = {"citadel_tags": list(tags)} if tags else None
        # Durable knowledge writes always go to cognee's permanent graph
        # (add+cognify), never its per-session cache. When a session_id was
        # supplied, cognee routed the write into the session cache, which (a)
        # stored an unserializable payload as the literal "[DataItem]"
        # placeholder instead of the real text, (b) never cognified it inline so
        # ingest reported items_processed:0, and (c) re-embedded a growing
        # scaffolded "Session ID:/Question:/Answer:" blob every sync cycle.
        # session_id is still accepted (callers pass it as provenance) but no
        # longer diverts the write away from the durable path.
        data = self._data_with_metadata(data, metadata)

        # Add is a fast write to Cognee's relational/source stores; it does NOT
        # create chunks, embeddings, or touch the Kuzu graph. Cognify is the
        # chunk, vector, and graph write. Metadata rides in the
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
        self._schedule_background_cognify(dataset_name)
        return {"added": added, "background_cognify": True}

    def _schedule_background_cognify(self, dataset_name: str) -> None:
        """Schedule a tracked, writer-lock-guarded cognify so ingest stays fast.

        Replaces cognee's fire-and-forget run_in_background cognify with one that
        acquires our writer lock (via cognify()) — serializing the Kuzu write and
        surfacing failures instead of swallowing them (#47/#56).
        """
        self.schedule_cognify([dataset_name])

    def schedule_cognify(self, datasets: list[str]) -> None:
        """Schedule ONE tracked, writer-lock-guarded background cognify.

        Lets a bulk writer (the Linear resync) coalesce a single cognify over every
        dataset it touched instead of one-per-write, so the request is not starved by
        a storm of per-issue cognifies (#46/#52). The single cognify still serializes
        on the writer lock (single Kuzu writer, #47) and logs — never crashes — on
        failure. No-op with no running loop (sync caller) or no datasets.
        """
        wanted = list(dict.fromkeys(datasets))  # de-dup, preserve order
        if not wanted:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Silent here meant content was recorded as ingested and never
            # reached the graph, with nothing in the logs either way. cognee.add
            # writes the relational/source stores; ONLY cognify writes chunks,
            # vectors, and Kuzu graph state. Skipping it makes a document that
            # exists as a row and cannot be retrieved. Say so.
            logger.error(
                "cognify NOT scheduled for %s: no running event loop. The data "
                "is stored but will not be searchable until a cognify runs.",
                wanted,
            )
            return

        async def _run() -> None:
            try:
                await self.cognify(datasets=wanted)
            except Exception:  # noqa: BLE001 - background task: log, never crash the loop
                logger.exception("background cognify for datasets %s failed", wanted)
            else:
                # Log SUCCESS too. Previously only failures were logged, so a
                # cognify that never ran — because the process exited before this
                # detached task was scheduled — was indistinguishable in the logs
                # from one that completed. That is how a whole repository stayed
                # missing from the index while every surface reported it synced.
                logger.info("background cognify finished for datasets %s", wanted)

        task = loop.create_task(_run())
        _BACKGROUND_COGNIFY_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_COGNIFY_TASKS.discard)

    async def recall(
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

        The read scans the ENTIRE graph (~0.3s+ on the prod node) and is now
        front-loaded on every dashboard open, so the result is cached for
        ``GRAPH_DATA_CACHE_TTL_SECONDS`` behind a single-flight lock: a burst of
        concurrent /api/mesh/graph opens collapses to one Kuzu read instead of
        one per caller (#28/#50). Per-caller shaping still runs per request.
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
        self._prepare_cognee_environment()
        import cognee

        await self._ensure_cognee_ready(cognee)
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
        chunked_ids: set[str] = set()
        graph_ids_seen: set[str] = set()
        fully_indexed_ids: set[str] = set()
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

            page_ids: list[str] = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise RuntimeError("corpus page returned a row without an id")
                document_id = str(row["id"])
                if document_id in document_ids_seen:
                    raise RuntimeError(
                        f"corpus page returned duplicate document {document_id}"
                    )
                document_ids_seen.add(document_id)
                page_ids.append(document_id)
            if len(document_ids_seen) > total_documents:
                raise RuntimeError("corpus pages exceeded the reported document total")

            chunk_counts = await self.corpus_chunk_counts(page_ids)
            if chunk_counts is None:
                raise RuntimeError("vector chunk measurement is unavailable")
            graph_ids = await self.corpus_graph_presence(page_ids)
            if graph_ids is None:
                raise RuntimeError("graph presence measurement is unavailable")

            graph_id_set = {str(document_id) for document_id in graph_ids}
            page_chunked_ids = {
                document_id
                for document_id in page_ids
                if int(chunk_counts.get(document_id, 0)) > 0
            }
            page_graph_ids = set(page_ids) & graph_id_set
            chunked_ids.update(page_chunked_ids)
            graph_ids_seen.update(page_graph_ids)
            fully_indexed_ids.update(page_chunked_ids & page_graph_ids)
            pages += 1

            if len(document_ids_seen) >= total_documents:
                break
            last_row = rows[-1]
            if not last_row.get("created_at") or not last_row.get("id"):
                raise RuntimeError("corpus page cannot form a keyset cursor")
            after_created_at = str(last_row["created_at"])
            after_id = str(last_row["id"])

        probe_complete = len(document_ids_seen) == total_documents
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
        if os.getenv("VECTOR_DB_PROVIDER", "").lower() != "pgvector":
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
                if datasets:
                    repair_datasets.update([dataset] if dataset else datasets)
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
            "zero_chunk_documents": zero_documents,
            "zero_chunk_documents_truncated": zero_count > len(zero_documents),
            "repair_datasets": sorted(repair_datasets),
            "unassigned_zero_chunk_count": unassigned,
            "census_complete": complete,
        }

    async def stored_chunk_budget_check(
        self,
        document_ids: list[str] | None = None,
        *,
        budget: int | None = None,
    ) -> dict[str, Any] | None:
        """Measure exact persisted ``DocumentChunk_text`` payloads.

        Cognee's public vector interface cannot enumerate a collection. Citadel's
        production provider is pgvector, whose reflected table can be scanned
        directly. Other providers return ``None`` because a similarity query is
        not evidence of corpus-wide compliance.
        """
        self._prepare_cognee_environment()
        if os.getenv("VECTOR_DB_PROVIDER", "").lower() != "pgvector":
            return None
        limit = budget if budget is not None else chunk_window.resolve_chunk_budget()
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
            }
        if document_ids is not None:
            payload_document_id = table.c.payload["document_id"].as_string()
            statement = statement.where(payload_document_id.in_(wanted))

        violations: list[dict[str, Any]] = []
        violation_count = 0
        chunks_scanned = 0
        async with engine.get_async_session() as session:
            rows = await session.execute(statement)
            for row in rows.all():
                chunks_scanned += 1
                violation = chunk_window.check_stored_chunk_payload(
                    row[1], chunk_id=str(row[0]), budget=limit
                )
                if violation is not None:
                    violation_count += 1
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
        }

    async def corpus_graph_presence(self, document_ids: list[str]) -> set[str] | None:
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
        engine = await self._graph_engine()
        query = getattr(engine, "query", None)
        if not callable(query):
            return None

        requested = {str(document_id) for document_id in document_ids}
        query_text = """
        MATCH (n:Node)
        WITH CAST(json_extract(n.properties, '$.document_id') AS STRING) AS document_id_json
        WHERE document_id_json IN $document_ids_json
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
        unchanged. Falls back to the full ``graph_data()`` read when the engine
        lacks these primitives or the targeted read raises — a shape surprise
        degrades to correct-but-slow, never a spurious 404. A node missing from
        the graph returns an empty targeted result (``get_node`` -> None,
        ``get_connections`` -> []) WITHOUT triggering that fallback: a full
        graph read cannot contain a node the graph lacks. Ids the graph cannot
        resolve are instead retried against the durable chunk store by
        ``get_document`` (``_document_from_chunk_store``).
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

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
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
        """
        doc_id = str(document_id)
        document = await self._document_from_graph(doc_id, follow_parent=True)
        if document is not None:
            return document
        return await self._document_from_chunk_store(doc_id)

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
            engine = await self._vector_engine()
            retrieve = getattr(engine, "retrieve", None)
            if not callable(retrieve):
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

    async def _document_from_graph(
        self, doc_id: str, *, follow_parent: bool
    ) -> dict[str, Any] | None:
        """Resolve ``doc_id`` against the graph store (targeted read).

        ``follow_parent=True`` (the drill-down entrypoint) additionally chases a
        text-bearing chunk's ``is_part_of`` parent document and returns the
        parent's FULL assembled body; the recursive parent call passes
        ``follow_parent=False`` so resolution is bounded at one hop.
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
            if follow_parent:
                # A text-bearing node with a TEXTLESS ``is_part_of`` neighbor is
                # a DocumentChunk next to its parent document (chunks carry the
                # text; TextDocument nodes carry none). Search hands out CHUNK
                # ids, so resolving the id to just this chunk's text re-serves
                # the same fragment the search snippet already showed. Resolve
                # the PARENT instead so drill-down returns the whole document;
                # the chunk's own text stays the fallback when the parent
                # cannot be assembled (never worse than before).
                parent_id = self._textless_neighbor_id(props_by_id, part_neighbor_ids)
                if parent_id is not None:
                    parent = await self._document_from_graph(
                        parent_id, follow_parent=False
                    )
                    if parent is not None and parent.get("body"):
                        owner_ids = list(parent.get("dataset_node_ids") or [parent_id])
                        if doc_id not in owner_ids:
                            owner_ids.append(doc_id)
                        parent["dataset_node_ids"] = owner_ids
                        return parent
            return {
                "id": doc_id,
                "source_type": "cognee",
                "title": props.get("title") or None,
                "body": text,
                "metadata": {k: v for k, v in props.items() if k != text_key},
                "dataset_node_ids": [doc_id, *part_neighbor_ids],
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

    async def _document_from_chunk_store(self, doc_id: str) -> dict[str, Any] | None:
        """Assemble a document from the durable chunk store when the graph can't.

        Accepts either a chunk id (resolved to its parent via the payload's
        ``document_id``) or a document id (siblings probed directly). Best
        effort by design: any failure degrades to ``None`` — exactly the 404
        the caller would have served anyway — never an error. Returns the same
        shape as the graph assembly; ``dataset_node_ids`` carries the parent
        document id, which is the relational ``Data.id`` the read-scope map
        keys on (ADR-0009), so the drill-down isolation gate is unchanged.
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
            engine = await self._vector_engine()
            retrieve = getattr(engine, "retrieve", None)
            if not callable(retrieve):
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

    async def cognify(self, *, datasets: list[str], force: bool = False) -> Any:
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
        # Single Kuzu writer: serialize the graph write against any other in-process
        # cognify so they cannot collide on the lock (#47).
        async with self.writer_lock:
            result = await cognee.cognify(
                datasets=datasets, incremental_loading=not force
            )
            self._invalidate_graph_data_cache()
        # The graph writer lock protects Kuzu only. Run the vector payload census
        # after releasing it so a slow SQL scan cannot block another cognify.
        processed_ids = _cognify_data_ids(result)
        if processed_ids:
            stored_check = await self.stored_chunk_budget_check(
                document_ids=processed_ids
            )
            if stored_check is None:
                logger.warning(
                    "stored chunk budget was not measured after cognify: "
                    "VECTOR_DB_PROVIDER is not pgvector"
                )
            elif not stored_check["ok"]:
                logger.error(
                    "stored chunk budget check failed after cognify: "
                    "%d violation(s) across %d chunk(s)",
                    stored_check["violation_count"],
                    stored_check["chunks_scanned"],
                )
                raise RuntimeError(
                    "stored chunk budget check failed: "
                    f"{stored_check['violation_count']} violation(s) across "
                    f"{stored_check['chunks_scanned']} persisted chunk(s)"
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
