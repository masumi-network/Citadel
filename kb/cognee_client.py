from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
from collections.abc import Iterator
from time import monotonic, perf_counter
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

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

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
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

        # Add is a fast write to the relational + vector stores; it does NOT touch
        # the Kuzu graph (cognify is the graph write). Metadata rides in the
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
            return  # no running loop (sync caller); nothing to schedule

        async def _run() -> None:
            try:
                await self.cognify(datasets=wanted)
            except Exception:  # noqa: BLE001 - background task: log, never crash the loop
                logger.exception("background cognify for datasets %s failed", wanted)

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
        degrades to correct-but-slow, never a spurious 404. A genuinely missing
        node returns an empty graph (``get_node`` -> None, ``get_connections``
        -> []) so ``get_document`` still resolves it to None without a fallback.
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
        ``chunk_index``; ``None`` when the node is missing or no text is found
        either way (textless entities keep resolving to None/404).
        """
        try:
            nodes, edges = await self._document_graph(str(document_id))
        except Exception as exc:  # noqa: BLE001
            if self._is_no_data_error(exc):
                return None
            raise

        def _extract_text(props: dict[str, Any]) -> tuple[str | None, str | None]:
            for key in ("text", "chunk", "content", "raw_content"):
                value = props.get(key)
                if isinstance(value, str) and value.strip():
                    return value, key
            return None, None

        # One pass over nodes: props by str id for O(1) neighbor lookup
        # (setdefault keeps first-encounter semantics on duplicate ids).
        props_by_id: dict[str, dict[str, Any]] = {}
        for node_id, properties in nodes:
            props_by_id.setdefault(str(node_id), dict(properties or {}))
        doc_id = str(document_id)
        props = props_by_id.get(doc_id)
        if props is None:
            return None
        # ``is_part_of`` neighbors (either direction), collected once and used
        # twice: assembling a textless document's body from its chunks, and
        # dataset attribution for read-scope checks — a chunk id has no
        # relational Data row of its own, so its datasets live on the linked
        # document's node id. ``dataset_node_ids`` plumbs those candidate ids
        # to the caller (kb/server.py drill-down isolation, ADR-0009) so the
        # graph is never re-walked. Duplicate edges to one neighbor count once.
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
        text, text_key = _extract_text(props)
        if text is not None:
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
            neighbor_text, _ = _extract_text(neighbor_props)
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
        import cognee

        await self._ensure_cognee_ready(cognee)
        # Single Kuzu writer: serialize the graph write against any other in-process
        # cognify so they cannot collide on the lock (#47).
        async with self.writer_lock:
            return await cognee.cognify(datasets=datasets, incremental_loading=not force)

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
