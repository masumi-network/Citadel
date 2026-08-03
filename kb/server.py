from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kb.access import (
    CENTRAL_DATASET,
    SESSION_TRACES_DATASET,
    SEAT_DATASET_PREFIX,
    AccessIdentity,
    AccessStore,
    ROLE_ORDER,
    default_scopes,
    hash_api_token,
    is_seat_dataset,
    seat_dataset,
    validate_seat_slug,
)
from kb.capture_policy import SeatCapturePolicy, capture_policy_payload
from kb.capture_config import MAX_APPROVED_CAPTURE_ROOTS, matched_capture_root
from kb.backup_mirror import BackupMirror, BackupMirrorDisabled, BackupMirrorPublishError
from kb.conflicts import KnowledgeConflictStore, obsidian_push_conflict_candidate
from kb.tags import normalize_tags
from kb.cognee_client import assert_cognee_dataset_api
from kb.config import CitadelConfig
from kb.contact_store import ContactStore
from kb.github_sync import GitHubOrgSyncer
from kb.google_chat import GoogleChatConfigError, GoogleChatDelivery
from kb.linear_sync import LinearSyncer
from kb.knowledge_mesh import KnowledgeMesh
from kb.learning import LearningOutcome, LearningProcess
from kb.session_trace import enrich_shared_trace, force_shared_trace_author_seat
from kb.learning_agent import LearningAgent
from kb.logging_utils import configure_logging
from kb.mcp_server import (
    TOOL_POLICIES,
    _max_ingest_bytes,
    create_mcp_server,
    set_tools_list_session_resolver,
)
from kb.mesh import MeshState
from kb.models import FeedbackRequest
from kb.obsidian_sync import ObsidianSyncStore, SyncPushDocument, normalize_path
from kb.promotion import PromotionEngine
from kb.promotion_queue import (
    APPROVED_STATUS,
    PENDING_STATUS,
    REJECTED_STATUS,
    scan_candidate,
)
from kb.repo_content_sync import RepoContentSyncer
from kb.search_feedback import build_search_telemetry, presence_only_telemetry
from kb.search_format import (
    CODE_TIMEOUT,
    DOC_TYPE_CANONICAL,
    DOC_TYPE_TRACE,
    NO_LEXICAL_MATCH_WARNING,
    apply_query_ranking,
    best_match_window,
    compact_search_filters,
    filter_hits,
    hit_term_coverage,
    infer_content_hint,
    infer_doc_type,
    infer_trust_tier,
    is_docs_mode_query,
    is_spec_mode_query,
    lexical_relevance_summary,
    parse_content_header,
    query_terms,
    token_asset_authority_warning,
)
from kb.security_scan import (
    SecretContentError,
    SecurityScanEntry,
    redact_secrets,
    scan_text_entries,
)
from kb.self_improve import SelfImprovement
from kb.service import Citadel
from kb.skills import skill_catalog, skill_integrity, skill_path
from kb.source_search import GITHUB_DOC_ID_PREFIX, github_section_document

configure_logging()
logger = logging.getLogger(__name__)

# Hosted MCP: one streamable-HTTP endpoint at /mcp/, authenticated per request by
# the caller's ctdl_ bearer token. No clone, no local Python — agents point their
# MCP client at https://<host>/mcp/ with Authorization: Bearer <token>.
MCP_ENDPOINT_PATH = "/mcp/"

# Strong refs to detached fire-and-forget tasks (e.g. the webhook re-ingest) so
# the event loop does not garbage-collect them mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _forget_background_task(task: "asyncio.Task[Any]") -> None:
    """Drop a finished task, and say so out loud if it died.

    A bare ``set.discard`` done-callback loses the exception: a scheduler that
    raises on its first line just stops existing, and the only trace is
    asyncio's "Task exception was never retrieved" whenever the object happens
    to be collected. That is the same silent-failure shape as #89, and it got
    more likely once the evolve scheduler started importing and running the
    stage code itself rather than shelling out (#88).
    """
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task %s died: %s", task.get_name(), exc, exc_info=exc
        )

# Most recent evolve-scheduler cognify canary verdict (verify=True), surfaced via
# /readyz so an always-on health probe goes RED when end-to-end ingest+cognify+
# search stops working — not only when node/auth are down (#27). None until the
# first scheduled pass runs.
_LAST_CANARY: dict[str, Any] | None = None
# Corpus-volume gate: if at least this many sources are tracked but the graph holds
# fewer than the floor of indexed nodes, the data plane is broken (green dashboards
# over an empty graph were the #27 failure mode).
_MIN_TRACKED_FOR_CORPUS = 10
_INDEXED_FLOOR = 1

# In-flight counts for the soft concurrency cap / 429 backpressure contract
# (#50). Single-loop server → increment/decrement need no lock. Search and the
# mesh graph read hold SEPARATE budgets: the Knowledge Mesh is the default
# dashboard view, so a ~15-seat login burst on it must not consume /search's
# budget (both would 429). Same limiter shape, independent counters.
_search_inflight = 0
_mesh_graph_inflight = 0
# The corpus census budget is separate for the same reason mesh's is: an admin
# paging the corpus during an evolve pass must not eat /search's slots.
_corpus_inflight = 0


class _SearchSlot:
    """Soft concurrency cap for a read path (#50).

    At capacity, returns a 429 + Retry-After + X-RateLimit-* contract instead of
    failing silently under load. Sync context manager so the slot is always
    released, even when the wrapped read raises. ``counter`` names the
    module-level in-flight global to bound, so distinct read paths (search vs the
    mesh graph) keep independent budgets while sharing one limiter shape.
    """

    def __init__(self, limit: int, counter: str = "_search_inflight") -> None:
        self.limit = limit
        self.remaining = limit
        self._counter = counter

    def __enter__(self) -> "_SearchSlot":
        inflight = globals()[self._counter]
        if inflight >= self.limit:
            raise HTTPException(
                status_code=429,
                detail="Read path is at capacity; retry after a moment.",
                headers={
                    "Retry-After": "1",
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        globals()[self._counter] = inflight + 1
        self.remaining = max(0, self.limit - globals()[self._counter])
        return self

    def __exit__(self, *exc: Any) -> None:
        globals()[self._counter] -= 1


async def _search_within_budget(
    citadel: Citadel, **kwargs: Any
) -> tuple[list[tuple[str, Any]], bool]:
    """Run search_across_datasets under the per-request time budget (#44).

    Returns (merged, timed_out). On timeout, degrade to empty-fast rather than
    hanging for 100s+ on a slow cognee recall.
    """
    try:
        merged = await asyncio.wait_for(
            search_across_datasets(citadel, **kwargs),
            timeout=citadel.config.search_timeout_seconds,
        )
        return merged, False
    except asyncio.TimeoutError:
        return [], True

mcp_server = create_mcp_server()
mcp_app = mcp_server.streamable_http_app()


class _McpAcceptShim:
    """Augment the Accept header so minimal MCP clients reach the transport.

    The mcp>=1.23 StreamableHTTP transport answers a POST with HTTP 406 unless
    ``Accept`` lists BOTH ``application/json`` and ``text/event-stream`` (``*/*``
    is deliberately not honored). Minimal clients (e.g. the Raspberry Pi MCP
    bridge) send ``application/json`` only, or omit Accept, and cannot connect.
    This shim rewrites the header in-flight to advertise both content types
    before delegating to the mounted streamable-HTTP app, leaving callers that
    already send both untouched.
    """

    _BOTH = "application/json, text/event-stream"
    _REQUIRED = ("application/json", "text/event-stream")

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers") or []
        kept = [(name, value) for name, value in raw_headers if name.lower() != b"accept"]
        existing = (
            b", ".join(value for name, value in raw_headers if name.lower() == b"accept")
            .decode("latin-1")
            .strip()
        )
        if not existing or existing == "*/*":
            merged = self._BOTH
        else:
            merged = existing
            lowered = existing.lower()
            for needed in self._REQUIRED:
                if needed not in lowered:
                    merged = f"{merged}, {needed}"
        kept.append((b"accept", merged.encode("latin-1")))
        await self.app({**scope, "headers": kept}, receive, send)


async def _evolve_scheduler_loop(interval_seconds: int) -> None:
    """Run the evolve cycle every ``interval_seconds``: heavy stages in a
    subprocess, then cognify in-loop.

    Two cognee/Kuzu constraints force this split:
    - cognee binds its async resources to the loop that created them, so cognify
      must run in the server's long-lived loop — a fresh ``asyncio.run()`` raises
      "got Future attached to a different loop".
    - Kuzu (the graph store) is a single-writer embedded DB, so only one process
      may hold it. The subprocess opens Kuzu during its add stages and releases it
      when it EXITS; only then can the web process cognify as the sole writer.

    Phase 1 runs github_sync → repo_content_sync → self_improve → promotion as a
    subprocess (``CITADEL_EVOLVE_COGNIFY_ENABLED=false``) that exits. Phase 2
    awaits cognify in-loop on the web's own Citadel. Serial and fail-soft; the
    first pass waits one full interval so a redeploy never triggers a heavy cycle
    on boot.
    """
    from kb.cognee_client import suppress_inline_cognify
    from scripts.run_railway import run_evolve_in_loop

    while True:
        await asyncio.sleep(interval_seconds)
        logger.info("Evolve scheduler: starting scheduled pass")
        # Phase 1 — heavy stages, in this loop. Hold the in-process writer lock
        # across them so no interactive cognify (an ingest's, /api/cognify/run)
        # writes Kuzu underneath the pass. Phase 2 re-acquires it itself.
        writer_lock = getattr(getattr(get_citadel(), "cognee", None), "writer_lock", None)
        acquired = False
        try:
            if writer_lock is not None:
                await writer_lock.acquire()
                acquired = True
            # Phase 1 runs HERE, in the web's own loop, not in a subprocess (#88).
            # A second process can never open the graph: cognee holds an exclusive
            # OS file lock on cognee_graph_kuzu for the lifetime of whichever
            # process opens it, and that is always this one. github_sync and
            # linear_sync died on "Could not set lock on file" every hour because
            # of it. Add-only for the duration so the per-ingest background
            # cognify does not storm the writer lock; Phase 2 below cognifies once
            # as the sole writer (#47).
            with suppress_inline_cognify():
                code = await run_evolve_in_loop()
            if code == 0:
                logger.info("Evolve scheduler: stages finished (exit=0)")
            else:
                # A partial failure is the normal broken case, not an edge one:
                # the stage names are already on the "Evolve finished: ...
                # failed=..." line, so log at ERROR here to make the cycle
                # visibly bad rather than an INFO nobody reads (#89).
                logger.error(
                    "Evolve scheduler: stages finished with failures (exit=%s) — "
                    "see the 'Evolve finished' line above for which stages failed",
                    code,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Evolve scheduler: stages failed")
        finally:
            if acquired:
                writer_lock.release()
        # Phase 2 — cognify in-loop; the web process is the sole Kuzu writer now.
        force = os.getenv("CITADEL_EVOLVE_COGNIFY_FORCE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            # verify=True runs the end-to-end ingest+cognify+search canary and
            # records its verdict for /readyz (#27).
            result = await get_citadel().cognify_dataset(force=force, verify=True)
            global _LAST_CANARY
            verification = result.get("verification") or {}
            _LAST_CANARY = {
                "ok": bool(result.get("ok")),
                "search_hit": verification.get("search_hit"),
                "graph_grew": result.get("graph_grew"),
                "marker": verification.get("marker"),
            }
            logger.info(
                "Evolve scheduler: cognify finished (graph_after=%s grew=%s canary_ok=%s)",
                result.get("graph_after"),
                result.get("graph_grew"),
                _LAST_CANARY["ok"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Evolve scheduler: cognify failed")


def _start_evolve_scheduler() -> "asyncio.Task[Any] | None":
    """Launch the 6h evolve cron when enabled.

    A separate Railway service cannot host this: the stages work against the Kuzu
    graph + JSON access store on the local ``/data`` volume, Railway volumes
    attach to a single service, and a second process cannot open the graph at all
    while this one holds cognee's exclusive Kuzu lock (#88). The scheduler runs
    the stages in this loop and then cognifies, both here. Off by default
    (``CITADEL_EVOLVE_SCHEDULER_ENABLED``).
    """
    config = get_citadel().config
    if not config.evolve_scheduler_enabled:
        return None
    interval = max(60, config.evolve_interval_seconds)
    logger.info("Evolve scheduler enabled: interval=%ss", interval)
    task = asyncio.create_task(_evolve_scheduler_loop(interval), name="evolve-scheduler")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_forget_background_task)
    return task


async def _repo_stats_loop(interval_seconds: int) -> None:
    """Keep the public pages' repo figures current, once a day.

    Deliberately its own task rather than a stage inside
    :func:`_evolve_scheduler_loop`, for two reasons. The evolve scheduler is off
    by default and, on the deployed node, its cadence is an operator Railway
    cron step rather than this loop, so a stage in there would leave the public
    numbers frozen on exactly the deployment that shows them. And it shares
    none of the evolve constraints: this is one outbound GET, no Kuzu writer
    lock, no subprocess, so serialising it behind a heavy cycle buys nothing.

    The first pass runs shortly after boot rather than a day later. The cache
    lives on the mounted volume, so a redeploy already has yesterday's answer
    to serve while this runs; the short delay only keeps startup clear.
    """
    from kb import repo_stats

    await asyncio.sleep(30)
    while True:
        try:
            # refresh() enforces the once-a-day floor itself, so an early wake
            # or a restart loop cannot turn into repeated GitHub requests.
            await asyncio.to_thread(repo_stats.refresh, get_citadel().config)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a stats problem end the loop or surface to a request.
            logger.exception("Repo stats refresh raised")
        await asyncio.sleep(interval_seconds)


def _start_repo_stats_scheduler() -> "asyncio.Task[Any] | None":
    """Launch the daily repo-stats refresh when enabled."""
    config = get_citadel().config
    if not getattr(config, "repo_stats_enabled", False):
        return None
    # Wake more often than the refresh floor so a missed window is picked up
    # within the hour instead of a day later. refresh() decides what actually
    # goes out to GitHub.
    interval = max(600, min(3600, int(config.repo_stats_interval_seconds)))
    logger.info(
        "Repo stats scheduler enabled: refresh floor=%ss",
        config.repo_stats_interval_seconds,
    )
    task = asyncio.create_task(_repo_stats_loop(interval))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _stop_evolve_scheduler(task: "asyncio.Task[Any] | None") -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    # Refuse to serve with a guessable env access key (M4). Deliberately at
    # startup and not at kb.config import: the CLI imports that module too and
    # must not die because the server's environment is misconfigured.
    enforce_access_key_strength()
    async with mcp_server.session_manager.run():
        # Eagerly build the mesh and seed its in-memory activity counters from
        # persistent source state so a redeploy does not look like the graph reset.
        # get_mesh() then returns this already-seeded instance. Best-effort: a failed
        # rehydrate must never block startup.
        mesh = MeshState()
        app.state.mesh = mesh
        # Fail loud at boot if a cognee bump moved the private symbols dataset
        # attribution depends on: without this the ImportError is swallowed and
        # every scoped caller's vault silently blanks (fail-closed). Log ERROR,
        # do not block startup — bypass callers still work, and the read-side is
        # best-effort by design.
        try:
            assert_cognee_dataset_api()
        except Exception:
            logger.error(
                "cognee dataset-attribution internals are missing (likely a cognee "
                "version bump); ADR-0009 isolation will fail closed and hide content "
                "for scoped callers until fixed",
                exc_info=True,
            )
        try:
            await mesh.rehydrate(get_citadel().config)
        except Exception:
            logger.exception("Mesh rehydrate failed; starting with empty counters")
        try:
            await ensure_session_traces_dataset(get_citadel())
        except Exception:
            logger.exception(
                "Failed to bootstrap %s dataset; seat search/share may fail until provisioned",
                SESSION_TRACES_DATASET,
            )
        try:
            await backfill_seat_datasets(get_citadel(), get_access_store())
        except Exception:
            logger.exception(
                "Seat dataset backfill failed; seats created before provisioning "
                "may still fail every search (#147)"
            )
        evolve_task = _start_evolve_scheduler()
        repo_stats_task = _start_repo_stats_scheduler()
        try:
            yield
        finally:
            await _stop_evolve_scheduler(evolve_task)
            # Same cancel-and-await shutdown; the helper is not evolve specific.
            await _stop_evolve_scheduler(repo_stats_task)


# Single-source the service version so /.well-known/citadel.json and the CLI
# never drift. Prefer installed package metadata; fall back to the in-source
# kb.__version__ because the Railway node runs from source (not dist-installed),
# where importlib.metadata raises and a hardcoded version would mislead.
try:
    _SERVICE_VERSION = _pkg_version("citadel-archive")
except PackageNotFoundError:
    from kb import __version__ as _SERVICE_VERSION

app = FastAPI(
    title="Citadel Archive",
    version=_SERVICE_VERSION,
    description="Self-hosted Organization Vault.",
    lifespan=lifespan,
)
STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(
    request: Request, exc: RequestValidationError
) -> Any:
    """Say WHY a 422 happened, in the log, without echoing the payload.

    FastAPI answers the caller with the field-level detail but the server side
    records nothing, so a rejected write appears in the deploy log as a bare
    "422 Unprocessable Entity". A real client looping on a malformed request is
    then undiagnosable from the node: seen in production as ten consecutive
    PUT /api/access/seats/{slug}/capture-roots 422s with no way to tell whether
    the payload was wrong, too large, or the wrong shape.

    Field locations and error types only. The values are the caller's data,
    which on these routes means filesystem paths and note content, and a log
    line is exactly the wrong place for it.
    """
    problems = [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('type', 'invalid')}"
        for error in exc.errors()
    ]
    logger.warning(
        "Rejected %s %s as invalid: %s",
        request.method,
        request.url.path,
        "; ".join(problems) or "no detail",
    )
    return await request_validation_exception_handler(request, exc)

# The Next.js static export (see docs/adr/0014-nextjs-frontend-static-export.md).
# Its own directory rather than a corner of static/, which holds the fonts, the
# favicon and the hand-written pages: one is generated and replaced wholesale on
# every build, the other is source, and mixing them makes `rm -rf` dangerous.
#
# Built with `npm run build:web` and committed, because a self-hoster installs a
# wheel and has no Node. A checkout that has never run the build simply has no
# /next, which is why the mount is conditional rather than an import-time crash.
WEBUI_DIR = Path(__file__).with_name("webui")
WEBUI_ASSETS_DIR = WEBUI_DIR / "_next"
if WEBUI_ASSETS_DIR.is_dir():
    app.mount("/next/_next", StaticFiles(directory=WEBUI_ASSETS_DIR), name="webui")


def webui_page(name: str, status_code: int = 200) -> FileResponse:
    """Serve one exported page, or 404 if the frontend was never built.

    `name` is always a literal from this module, never a path parameter, so it
    cannot be steered out of the directory.
    """
    page = WEBUI_DIR / f"{name}.html"
    if not page.is_file():
        # A source checkout that has not run `npm run build:web`. Not an error
        # worth a stack trace: the built site is simply not present.
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(page, status_code=status_code)


@app.api_route("/mcp", methods=["GET", "POST", "DELETE", "OPTIONS"], include_in_schema=False)
async def mcp_trailing_slash_redirect() -> RedirectResponse:
    """Keep legacy /mcp configs working without emitting an absolute http:// redirect."""
    return RedirectResponse(url=MCP_ENDPOINT_PATH, status_code=307)


app.mount("/mcp", _McpAcceptShim(mcp_app))
ADMIN_COOKIE = "citadel_admin"
MCP_TOOL_HEADER = "x-citadel-mcp-tool"
AUDIT_VIEWS = frozenset({"all", "mcp", "access", "failures"})
AUDIT_LIMIT_MAX = 500
PUBLIC_CACHE_HEADERS = {"Cache-Control": "public, max-age=300"}
PRIVATE_CACHE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PUBLIC_CACHE_PATHS = frozenset({"/.well-known/citadel.json", "/skills"})
PUBLIC_CACHE_PREFIXES = ("/skills/", "/static/", "/next/_next/")
PUBLIC_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$")
def _content_security_policy(style_src: str) -> str:
    """Build the policy from one template so only `style-src` can ever differ."""
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        f"style-src {style_src}; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )


CONTENT_SECURITY_POLICY = _content_security_policy("'self'")

# The relaxed variant is kept so a page can only ever reach it through the
# explicit opt-in set below, never by accident. No page uses it today.
#
# The landing page's React Flow diagram was the one candidate: it positions
# every node and the viewport by writing inline `transform` styles, and the
# library cannot be configured out of that. But it writes them through the
# CSSOM (element.style), which style-src does not govern; the directive
# covers <style> elements and style attributes arriving in markup. Measured
# before the exemption was removed: under style-src 'self' the diagram
# rendered identically, with zero securitypolicyviolation events (Chrome;
# the CSSOM carve-out is spec behaviour). Script execution stays restricted
# to same-origin files on every page under both policies.
CONTENT_SECURITY_POLICY_INLINE_STYLE = _content_security_policy("'self' 'unsafe-inline'")

# Exact paths, not prefixes, and this set is the only way to reach the relaxed
# policy. It is empty: every route, / and its React Flow diagram included,
# gets the strict policy above. Adding a path here is a security decision;
# the tests pin this set so it cannot grow in passing.
CSP_INLINE_STYLE_PATHS: frozenset[str] = frozenset()


def content_security_policy_for(path: str) -> str:
    """Strict by default. A path opts in; it can never be opted in by accident."""
    if path in CSP_INLINE_STYLE_PATHS:
        return CONTENT_SECURITY_POLICY_INLINE_STYLE
    return CONTENT_SECURITY_POLICY


SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

LOGIN_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Sign in to Citadel</title>
    <meta name="description" content="Sign in to your Citadel seat." />
    <link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />
    <link rel="stylesheet" href="/static/info.css">
  </head>
  <body>
    <!-- The nav has to sit inside a .band-hero exactly as it does on every
         other public page. Bare, it falls through to the base .topnav rules:
         sticky, filled, bottom border, and 860px wide instead of 940px. The
         result was the nav visibly changing shape when you clicked Sign in. -->
    <div class="band band-hero">
      <div class="hero-glow" aria-hidden="true"></div>
    <nav class="topnav" aria-label="Main">
      <div class="topnav-in">
        <a class="topnav-brand" href="/">
          <span class="mark" id="mark" aria-hidden="true"></span>
          <span class="wordmark">Citadel</span>
        </a>
        <div class="topnav-links">
          <a href="/">Home</a>
          <a href="/info">Status</a>
          <a href="/use-cases">Use cases</a>
          <a href="/contact">Contact</a>
          <a href="/login" aria-current="page">Sign in</a>
        </div>
        <button class="themebtn" id="themebtn" type="button" aria-label="Toggle light or dark theme">theme</button>
        <a class="navicon" href="https://github.com/masumi-network/Citadel" aria-label="GitHub repository" target="_blank" rel="noopener noreferrer"><svg viewBox="0 0 16 16" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path></svg></a>
      </div>
    </nav>
    <main class="auth">
      <div class="auth-in">
        <p class="eyebrow">Seat access</p>
        <h1>Open your vault.</h1>
        <p class="auth-lede">
          Your Node stays private. Signing in opens your own seat. Central is
          shared, and nobody reads another seat's notes.
        </p>
        <form id="loginForm" class="auth-form">
          <label class="auth-label" for="adminKey">Seat token</label>
          <input
            id="adminKey"
            class="auth-input"
            name="accessKey"
            type="password"
            autocomplete="current-password"
            required
            autofocus
            placeholder="ctdl_…"
          />
          <p class="auth-hint">
            Paste the seat token from your admin (or run
            <code>citadel seat token</code>). Operators can still use the env admin key.
          </p>
          <p id="loginError" class="auth-error" role="alert"></p>
          <button id="loginSubmit" class="auth-submit" type="submit">Open workspace</button>
        </form>
        <p class="auth-alt">No token yet? <a href="/">Read what Citadel is</a>, or <a href="/contact">ask us for one</a>.</p>
      </div>
    </main>
    </div>
    <script src="/static/login.js" type="module"></script>
  </body>
</html>
"""


class ContactBody(BaseModel):
    """An enquiry from the public /contact page.

    Every field is length capped at the model boundary so an oversized body is
    rejected before it reaches the scrubber or the Chat gateway.
    """

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    organization: str = Field(default="", max_length=160)
    # Honeypot. Hidden from humans, so a filled value means a bot.
    website: str = Field(default="", max_length=200)


# Partner contact: best-effort per-IP bucket plus a hard global ceiling. The
# per-IP part is spoofable behind a proxy, so the global cap is what actually
# bounds the damage. In-process only, which is enough for a single Railway
# instance and fails toward refusing rather than accepting.
CONTACT_PER_IP_LIMIT = 3
CONTACT_PER_IP_WINDOW_SECONDS = 900
CONTACT_GLOBAL_LIMIT = 30
CONTACT_GLOBAL_WINDOW_SECONDS = 3600
CONTACT_THREAD_KEY = "citadel-partner-contact"
_contact_hits: dict[str, list[float]] = {}
_contact_global_hits: list[float] = []
_contact_lock = threading.Lock()


def contact_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    client = request.client
    return client.host if client else "unknown"


def contact_rate_limit_ok(client_ip: str) -> bool:
    now = time.monotonic()
    with _contact_lock:
        recent_global = [t for t in _contact_global_hits if now - t < CONTACT_GLOBAL_WINDOW_SECONDS]
        _contact_global_hits[:] = recent_global
        if len(recent_global) >= CONTACT_GLOBAL_LIMIT:
            return False
        recent = [t for t in _contact_hits.get(client_ip, []) if now - t < CONTACT_PER_IP_WINDOW_SECONDS]
        if len(recent) >= CONTACT_PER_IP_LIMIT:
            _contact_hits[client_ip] = recent
            return False
        recent.append(now)
        _contact_hits[client_ip] = recent
        _contact_global_hits.append(now)
        # Bound the bucket map so a spray of spoofed IPs cannot grow it forever.
        if len(_contact_hits) > 2000:
            for stale_ip, hits in list(_contact_hits.items()):
                if not hits or now - hits[-1] >= CONTACT_PER_IP_WINDOW_SECONDS:
                    _contact_hits.pop(stale_ip, None)
        return True


def scrub_contact_field(value: str) -> str:
    """Make submitted text safe to paste into a Google Chat message.

    Chat renders ``<url|label>`` as a link and ``*_~``` as formatting, so a
    submitter could otherwise forge an official looking message in the org's
    space. Angle brackets and formatting characters are dropped rather than
    escaped, since this text is only ever read by a human.
    """
    cleaned = "".join(ch for ch in value if ch == "\n" or (ch.isprintable() and ch not in "<>*_~`"))
    lines = [line.strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def contact_gateway() -> Any:
    """The Chat gateway for partner enquiries, or None when unconfigured.

    Enquiries go to their own thread, never into the organization digest
    thread, so a partner message cannot be mistaken for generated content.
    """
    try:
        gateway = GoogleChatDelivery.from_config(get_citadel().config)
    except GoogleChatConfigError:
        logger.warning("Partner contact gateway is misconfigured; the form will report 503")
        return None
    if gateway is not None:
        gateway.thread_key = CONTACT_THREAD_KEY
    return gateway


class IngestBody(BaseModel):
    data: str = Field(min_length=1)
    dataset: str | None = None
    tags: list[str] = Field(default_factory=list)
    session_id: str | None = None
    # When true, cognify the written dataset inline (server-side, where it holds
    # the single Kuzu writer) and block until done — so a writer's ingest is
    # immediately searchable without needing the admin-only cognify endpoint.
    cognify: bool = False


# Upper bound on a search query, in the same spirit as SearchBody's other
# string fields (repo 200, path 400, mode 32) — it was the one that could grow
# without limit. Sized against the longest queries this repo actually asks: 99
# characters in the bench corpus (scripts/bench/golden_questions.json, "What is
# the full postgres connection string ...") and 38 in the test suite. Queries
# are typed or composed by an agent, never built by concatenating file content,
# so 2000 leaves a pasted paragraph plenty of room while still being a bound.
MAX_SEARCH_QUERY_LENGTH = 2000


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    dataset: str | None = None
    session_id: str | None = None
    top_k: int = Field(default=10, ge=1, le=100)
    # Client/MCP filters — applied after retrieval so telemetry sees the same view.
    types: list[str] | None = None
    repo: str | None = Field(default=None, max_length=200)
    path: str | None = Field(default=None, max_length=400)
    canonical_only: bool = False
    exclude_ambient: bool = False
    mode: str | None = Field(default=None, max_length=32)

    def cleaned_types(self) -> list[str] | None:
        if not self.types:
            return None
        cleaned = [str(item).strip()[:64] for item in self.types if str(item).strip()]
        return cleaned[:20] or None

    def cleaned_mode(self) -> str | None:
        if not isinstance(self.mode, str) or not self.mode.strip():
            return None
        normalized = self.mode.strip().lower()
        return normalized if normalized in {"docs"} else None

    def filter_kwargs(self) -> dict[str, Any]:
        mode = self.cleaned_mode()
        exclude_ambient = bool(self.exclude_ambient) or mode == "docs"
        return {
            "types": self.cleaned_types(),
            "repo": self.repo.strip() if isinstance(self.repo, str) and self.repo.strip() else None,
            "path": self.path.strip() if isinstance(self.path, str) and self.path.strip() else None,
            "canonical_only": bool(self.canonical_only),
            "exclude_ambient": exclude_ambient,
        }

    def telemetry_filters(self) -> dict[str, Any]:
        return compact_search_filters(
            **self.filter_kwargs(),
            mode=self.cleaned_mode(),
            dataset=self.dataset,
            top_k=self.top_k,
        )


class ShareSessionBody(BaseModel):
    """Compact Session Context from client distill + redact."""

    data: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    capture_roots: list[str] = Field(min_length=1)
    has_tool_errors: bool = False
    repo: str | None = None
    branch: str | None = None


class FeedbackBody(BaseModel):
    qa_id: str | None = Field(default=None, min_length=1)
    result_id: str | None = Field(default=None, min_length=1)
    score: int | None = Field(default=None, ge=-1, le=1)
    text: str | None = None
    session_id: str | None = None
    dataset: str | None = None
    # Optional thumbs helper: true→1, false→-1 (overridden by explicit score).
    correct: bool | None = None

    def resolved_qa_id(self) -> str:
        value = (self.qa_id or self.result_id or "").strip()
        if not value:
            raise ValueError("qa_id or result_id is required")
        return value

    def resolved_score(self) -> int | None:
        if self.score is not None:
            return self.score
        if self.correct is True:
            return 1
        if self.correct is False:
            return -1
        return None


class ImproveBody(BaseModel):
    dataset: str | None = None
    session_ids: list[str] | None = None


class AdminSessionBody(BaseModel):
    access_key: str | None = Field(default=None, min_length=1)
    admin_key: str | None = Field(default=None, min_length=1)


class GitHubSyncBody(BaseModel):
    force: bool = False


class LinearSyncBody(BaseModel):
    force: bool = False


class RepoContentSyncBody(BaseModel):
    force: bool = False
    dry_run: bool = False


class LearningAgentRunBody(BaseModel):
    force: bool = False
    dry_run: bool = False
    post_to_chat: bool = False
    include_digest_preview: bool = True


class GoogleChatTestBody(BaseModel):
    message: str | None = Field(default=None, min_length=1, max_length=400)


class BackupMirrorRunBody(BaseModel):
    dry_run: bool = True


class PromoteRunBody(BaseModel):
    dataset: str = Field(min_length=1)
    dry_run: bool = True
    max_items: int | None = Field(default=None, ge=1, le=200)


class PromotionDecisionBody(BaseModel):
    note: str | None = Field(default=None, max_length=400)


class CognifyRunBody(BaseModel):
    dataset: str | None = None
    verify: bool = False
    force: bool = False


class GraphCleanupBody(BaseModel):
    # Default to a non-destructive dry run: the caller must POST {"dry_run": false}
    # to actually delete, after reviewing the listed candidates (#15).
    dry_run: bool = True


class AccessTokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="reader")
    kind: str = Field(default="service_account")
    scopes: list[str] | None = None
    team_id: str | None = None
    expires_at: str | None = None
    default_dataset: str | None = None
    default_session: str | None = None
    allowed_datasets: list[str] | None = None


class CreateSeatBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=63)
    email: str | None = Field(default=None, max_length=320)
    role: str = Field(default="writer")
    issue_token: bool = True
    token_name: str | None = Field(default=None, max_length=120)


class IssueSeatTokenBody(BaseModel):
    token_name: str | None = Field(default=None, max_length=120)
    # Declared only to be rejected. Pydantic silently drops fields the model
    # does not declare, so without this a caller who POSTs {"role": "admin"}
    # gets a 200 and a writer token — they believe they hold an admin
    # credential and every admin call 403s. A seat token always carries the
    # seat's own role, so any supplied value, matching or not, is an error the
    # endpoint must say out loud (see issue_access_seat_token).
    role: str | None = None


class CapturePolicyBody(BaseModel):
    deny_globs: list[str] = Field(default_factory=list, max_length=200)


class CaptureRootsBody(BaseModel):
    roots: list[str] = Field(
        default_factory=list, max_length=MAX_APPROVED_CAPTURE_ROOTS
    )


class ObsidianVaultBody(BaseModel):
    vault_name: str | None = Field(default=None, min_length=1, max_length=180)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    team_id: str | None = Field(default=None, max_length=120)
    plugin_version: str | None = Field(default=None, max_length=80)


class ObsidianPushDocumentBody(BaseModel):
    path: str = Field(min_length=1, max_length=600)
    content: str = ""
    base_rev: int | None = Field(default=None, ge=0)
    deleted: bool = False
    tags: list[str] = Field(default_factory=list)
    dataset: str | None = None


class ObsidianPushBody(BaseModel):
    vault_id: str = Field(min_length=1)
    documents: list[ObsidianPushDocumentBody] = Field(min_length=1)
    dataset: str | None = None
    session_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class ObsidianConflictResolveBody(BaseModel):
    resolution: str = Field(pattern="^(accept_local|accept_remote|save_both|manual)$")
    body: str | None = None


class KnowledgeConflictResolveBody(BaseModel):
    resolution_note: str = Field(min_length=1, max_length=400)


class ContributeBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = Field(default=None, max_length=1000)
    dataset: str | None = None


class OptimizeBody(BaseModel):
    dry_run: bool = False
    max_items: int | None = Field(default=None, ge=1, le=50)


def get_citadel() -> Citadel:
    if not hasattr(app.state, "citadel"):
        app.state.citadel = Citadel.from_env()
    return app.state.citadel


def get_mesh() -> MeshState:
    if not hasattr(app.state, "mesh"):
        app.state.mesh = MeshState()
    return app.state.mesh


def get_github_syncer() -> GitHubOrgSyncer:
    if hasattr(app.state, "github_syncer"):
        return app.state.github_syncer
    return GitHubOrgSyncer(get_citadel())


def get_repo_content_syncer() -> RepoContentSyncer:
    if hasattr(app.state, "repo_content_syncer"):
        return app.state.repo_content_syncer
    return RepoContentSyncer(get_citadel())


def get_linear_syncer() -> LinearSyncer:
    if hasattr(app.state, "linear_syncer"):
        return app.state.linear_syncer
    return LinearSyncer(get_citadel(), access_store=get_access_store())


def get_learning_agent() -> LearningAgent:
    if hasattr(app.state, "learning_agent"):
        return app.state.learning_agent
    return LearningAgent(
        get_citadel(),
        github_syncer=get_github_syncer(),
        repo_content_syncer=get_repo_content_syncer(),
    )


def get_backup_mirror() -> BackupMirror:
    return BackupMirror(get_citadel().config)


def get_promotion_engine() -> PromotionEngine:
    citadel = get_citadel()
    return PromotionEngine(
        citadel,
        get_learning_process(),
        get_access_store(),
        citadel.config,
    )


def get_access_store() -> AccessStore:
    existing = getattr(app.state, "access_store", None)
    if isinstance(existing, AccessStore):
        return existing
    config = get_citadel().config
    app.state.access_store = AccessStore(
        config.access_store_path,
        max_audit_events=config.audit_max_events,
    )
    return app.state.access_store


def get_contact_store() -> ContactStore:
    existing = getattr(app.state, "contact_store", None)
    if isinstance(existing, ContactStore):
        return existing
    app.state.contact_store = ContactStore(get_citadel().config.contact_store_path)
    return app.state.contact_store


def get_obsidian_sync() -> ObsidianSyncStore:
    existing = getattr(app.state, "obsidian_sync", None)
    if isinstance(existing, ObsidianSyncStore):
        return existing
    app.state.obsidian_sync = ObsidianSyncStore(get_citadel().config.obsidian_sync_state_path)
    return app.state.obsidian_sync


def get_conflict_store() -> KnowledgeConflictStore:
    existing = getattr(app.state, "conflict_store", None)
    if isinstance(existing, KnowledgeConflictStore):
        return existing
    config = get_citadel().config
    app.state.conflict_store = KnowledgeConflictStore(
        config.conflicts_store_path,
        max_records=config.conflicts_max_records,
    )
    return app.state.conflict_store


def get_learning_process() -> LearningProcess:
    return LearningProcess(
        get_citadel(),
        mesh=get_mesh(),
        conflicts=get_conflict_store(),
    )


def get_knowledge_mesh() -> KnowledgeMesh:
    existing = getattr(app.state, "knowledge_mesh", None)
    if isinstance(existing, KnowledgeMesh):
        return existing
    return KnowledgeMesh(getattr(get_citadel(), "cognee", None))


def sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def configured_access_keys() -> list[tuple[str, str]]:
    config = get_citadel().config
    entries: list[tuple[str, str]] = []
    if config.admin_key:
        entries.append(("admin", config.admin_key))
    entries.extend(("writer", key) for key in config.writer_keys)
    entries.extend(("reader", key) for key in config.reader_keys)
    return entries


# An env access key is a bearer credential on every endpoint (see
# bearer_identity below), and unlike a minted ctdl_ token it is whatever string
# an operator typed. docs/operations.md used to suggest "owner-admin-key".
# 32 chars is well under a token_urlsafe(32) and well over anything hand-written.
WEAK_ACCESS_KEY_MIN_LENGTH = 32


def weak_access_keys() -> list[tuple[str, int]]:
    """(env var name, length) for every configured env key that is too short."""
    config = get_citadel().config
    candidates: list[tuple[str, str]] = []
    if config.admin_key:
        candidates.append(("CITADEL_ADMIN_KEY", config.admin_key))
    candidates.extend(("CITADEL_WRITER_KEYS", key) for key in config.writer_keys)
    candidates.extend(("CITADEL_READER_KEYS", key) for key in config.reader_keys)
    return [
        (name, len(key))
        for name, key in candidates
        if len(key) < WEAK_ACCESS_KEY_MIN_LENGTH
    ]


def allow_weak_access_keys() -> bool:
    return os.getenv("CITADEL_ALLOW_WEAK_ACCESS_KEYS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def enforce_access_key_strength() -> None:
    """Refuse to serve with a brute-forceable env access key (M4).

    A hard stop rather than a warning, on purpose. A warning is the shape of
    signal that let the Kuzu lock run for weeks: present in the logs, read by
    nobody. The escape hatch is explicit and greppable, so an operator who needs
    a short key in development makes a deliberate, auditable choice.
    """
    if allow_weak_access_keys():
        return
    weak = weak_access_keys()
    if not weak:
        return
    listed = ", ".join(f"{name} ({length} chars)" for name, length in weak)
    raise SystemExit(
        f"Refusing to start: access key too short — {listed}. "
        f"Minimum is {WEAK_ACCESS_KEY_MIN_LENGTH} characters, because an env key "
        "authenticates as a bearer token on every endpoint.\n"
        '  Mint one:  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
        "  Override:  CITADEL_ALLOW_WEAK_ACCESS_KEYS=true"
    )


def env_identity(role: str) -> AccessIdentity:
    return AccessIdentity(
        role=role,
        actor_id=f"bootstrap:{role}",
        actor_kind="bootstrap_key",
        actor_name=f"{role.title()} bootstrap key",
        source="env",
        scopes=default_scopes(role),
    )


def session_token(role: str, access_key: str) -> str:
    message = f"citadel-session:v2:{role}".encode("utf-8")
    return hmac.new(access_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def cookie_value(role: str, access_key: str) -> str:
    return f"{role}:{session_token(role, access_key)}"


def token_session_signature(role: str, token_id: str, token_hash: str) -> str:
    message = f"citadel-session:v2:token:{role}:{token_id}".encode("utf-8")
    return hmac.new(token_hash.encode("utf-8"), message, hashlib.sha256).hexdigest()


def token_cookie_value(identity: AccessIdentity, token_hash: str) -> str:
    if not identity.token_id:
        raise ValueError("Token identity missing token ID.")
    signature = token_session_signature(identity.role, identity.token_id, token_hash)
    return f"token:{identity.role}:{identity.token_id}:{signature}"


def access_key_identity(access_key: str) -> tuple[AccessIdentity, str] | None:
    for role, key in configured_access_keys():
        if secrets.compare_digest(access_key, key):
            return env_identity(role), cookie_value(role, access_key)
    token_session = get_access_store().authenticate_token(access_key)
    if token_session:
        return token_session.identity, token_cookie_value(
            token_session.identity,
            hash_api_token(access_key),
        )
    return None


def _mcp_tools_list_session(token: str) -> dict[str, Any] | None:
    """In-process token→session for MCP tools/list (no nested HTTP / no deadlock)."""
    pair = access_key_identity(token)
    if not pair:
        return None
    identity, _ = pair
    return {"ok": True, "role": identity.role, "seat_slug": identity.seat_slug}


set_tools_list_session_resolver(_mcp_tools_list_session)


def session_identity(request: Request) -> AccessIdentity | None:
    session = request.cookies.get(ADMIN_COOKIE)
    if not session:
        return None
    for role, key in configured_access_keys():
        if secrets.compare_digest(session, cookie_value(role, key)):
            return env_identity(role)
    parts = session.split(":")
    if len(parts) != 4 or parts[0] != "token":
        return None
    _, role, token_id, signature = parts
    token_session = get_access_store().token_session(token_id)
    if not token_session or token_session.identity.role != role:
        return None
    expected = token_session_signature(role, token_id, token_session.token_hash)
    if not secrets.compare_digest(signature, expected):
        return None
    return token_session.identity


def bearer_identity(request: Request) -> AccessIdentity | None:
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        return None
    identity_with_cookie = access_key_identity(token.strip())
    if not identity_with_cookie:
        return None
    identity, _ = identity_with_cookie
    return identity


def request_identity(request: Request) -> AccessIdentity | None:
    return bearer_identity(request) or session_identity(request)


def session_role(request: Request) -> str | None:
    identity = session_identity(request)
    return identity.role if identity else None


# Failed-auth throttle (M4). Counted ONLY on failure, so a caller holding a
# valid credential never touches it and cannot be locked out no matter how much
# noise an attacker generates. Per-IP is spoofable behind the Railway proxy, so
# as with the contact form the global ceiling is what actually bounds it.
AUTH_FAIL_PER_IP_LIMIT = 10
AUTH_FAIL_PER_IP_WINDOW_SECONDS = 300
AUTH_FAIL_GLOBAL_LIMIT = 200
AUTH_FAIL_GLOBAL_WINDOW_SECONDS = 3600
_auth_fail_hits: dict[str, list[float]] = {}
_auth_fail_global_hits: list[float] = []
_auth_fail_lock = threading.Lock()


def reset_auth_throttle() -> None:
    """Clear the buckets. For tests, and for an operator digging out of a flood."""
    with _auth_fail_lock:
        _auth_fail_hits.clear()
        _auth_fail_global_hits.clear()


def record_auth_failure(client_ip: str) -> bool:
    """Record one failed auth. False when the caller is now over a limit.

    A blocked attempt is deliberately NOT counted. Our own clients retry 429
    (kb/retry.py TRANSIENT_HTTP_STATUSES) but never 401, so counting the blocked
    retries would let a client with a stale token extend its own ban forever and
    hide a plain wrong-credential behind "rate limited" — which is exactly how
    the GitHub-Sync key drift got misdiagnosed before.
    """
    now = time.monotonic()
    try:
        with _auth_fail_lock:
            recent_global = [
                hit
                for hit in _auth_fail_global_hits
                if now - hit < AUTH_FAIL_GLOBAL_WINDOW_SECONDS
            ]
            _auth_fail_global_hits[:] = recent_global
            recent = [
                hit
                for hit in _auth_fail_hits.get(client_ip, [])
                if now - hit < AUTH_FAIL_PER_IP_WINDOW_SECONDS
            ]
            if len(recent) >= AUTH_FAIL_PER_IP_LIMIT or len(recent_global) >= AUTH_FAIL_GLOBAL_LIMIT:
                _auth_fail_hits[client_ip] = recent
                return False
            recent.append(now)
            _auth_fail_hits[client_ip] = recent
            _auth_fail_global_hits.append(now)
            if len(_auth_fail_hits) > 2000:
                for stale_ip, hits in list(_auth_fail_hits.items()):
                    if not hits or now - hits[-1] >= AUTH_FAIL_PER_IP_WINDOW_SECONDS:
                        _auth_fail_hits.pop(stale_ip, None)
            return True
    except Exception:
        # Fail OPEN. This is an in-process dict, so it should not raise, but a
        # throttle that breaks must not become an outage for the whole team.
        logger.exception("Auth throttle failed; allowing the request")
        return True


def _reject_unauthenticated(request: Request) -> None:
    """401, or 429 once this source has failed too many times."""
    client_ip = contact_client_ip(request)
    if not record_auth_failure(client_ip):
        logger.warning(
            "Throttled repeated auth failures from %s: %s %s",
            client_ip,
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Try again later.",
            headers={"Retry-After": str(AUTH_FAIL_PER_IP_WINDOW_SECONDS)},
        )
    logger.warning(
        "Rejected unauthenticated request: %s %s", request.method, request.url.path
    )
    raise HTTPException(status_code=401, detail="Access key required.")


def require_role(request: Request, minimum_role: str) -> AccessIdentity:
    identity = request_identity(request)
    if not identity:
        _reject_unauthenticated(request)
    if ROLE_ORDER[identity.role] < ROLE_ORDER[minimum_role]:
        logger.warning(
            "Denied %s %s for actor %s: role %s below required %s",
            request.method,
            request.url.path,
            identity.actor_id,
            identity.role,
            minimum_role,
        )
        raise HTTPException(status_code=403, detail=f"{minimum_role.title()} access required.")
    return identity


def effective_scopes(identity: AccessIdentity) -> tuple[str, ...]:
    if identity.scopes:
        return identity.scopes
    if identity.source == "env":
        return default_scopes(identity.role)
    return ()


def require_access(request: Request, minimum_role: str, scope: str) -> AccessIdentity:
    identity = require_role(request, minimum_role)
    if scope not in effective_scopes(identity):
        raise HTTPException(status_code=403, detail=f"Scope required: {scope}.")
    return identity


def can_bypass_dataset_allowlist(identity: AccessIdentity) -> bool:
    if identity.source == "env":
        return True
    if identity.role == "admin":
        return True
    return "access:manage" in effective_scopes(identity)


def env_exclude_patterns() -> tuple[str, ...]:
    return get_citadel().config.exclude_patterns


def require_capture_policy_read(request: Request, slug: str) -> tuple[AccessIdentity, str]:
    try:
        normalized = validate_seat_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    identity = require_access(request, "reader", "kb:read")
    if identity.role == "admin" or "access:manage" in effective_scopes(identity):
        return identity, normalized
    if identity.seat_slug == normalized:
        return identity, normalized
    raise HTTPException(status_code=403, detail="Capture policy is not available for this seat.")


def seat_capture_policy_response(slug: str) -> dict[str, Any]:
    store = get_access_store()
    if not store.find_seat_by_slug(slug):
        raise HTTPException(status_code=404, detail=f"Seat not found: {slug}")
    baseline = store.get_capture_policy(slug)
    return capture_policy_payload(
        seat_slug=slug,
        baseline=baseline,
        env_exclude_patterns=env_exclude_patterns(),
    )


def require_capture_roots_write(request: Request, slug: str) -> tuple[AccessIdentity, str]:
    try:
        normalized = validate_seat_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    identity = require_access(request, "writer", "kb:ingest")
    if identity.role == "admin" or "access:manage" in effective_scopes(identity):
        return identity, normalized
    if identity.seat_slug == normalized:
        return identity, normalized
    raise HTTPException(
        status_code=403,
        detail="Approved Capture Roots may only be updated by the seat holder or an admin.",
    )


def seat_capture_roots_response(slug: str) -> dict[str, Any]:
    store = get_access_store()
    if not store.find_seat_by_slug(slug):
        raise HTTPException(status_code=404, detail=f"Seat not found: {slug}")
    roots = store.get_approved_capture_roots(slug)
    return {
        "ok": True,
        "seat_slug": slug,
        "roots": list(roots.paths),
        "updated_at": roots.updated_at,
        "updated_by": roots.updated_by,
    }


def enforce_share_capture_root(actor: AccessIdentity, cwd: str) -> None:
    if not actor.seat_slug:
        raise HTTPException(
            status_code=403,
            detail="Shared Session Traces may only be volunteered by a seat holder.",
        )
    approved = get_access_store().get_approved_capture_roots(actor.seat_slug)
    if matched_capture_root(cwd, approved.paths) is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "Share refused: cwd is not under a server-approved Capture Root for "
                "this seat. Register roots with `citadel setup` (synced to the Node) "
                "and share from inside them."
            ),
        )


def scan_share_payload_or_raise(data: str, *, block_severity: str) -> None:
    scan = scan_text_entries(
        [SecurityScanEntry(source="share_session", location=SESSION_TRACES_DATASET, text=data)],
        block_severity=block_severity,
    )
    if scan.get("blocked"):
        raise SecretContentError(
            dataset=SESSION_TRACES_DATASET,
            highest_severity=scan.get("highest_severity"),
            block_severity=block_severity,
            findings=scan.get("findings", []),
        )


def can_run_promotion(identity: AccessIdentity, seat_dataset: str) -> bool:
    if not is_seat_dataset(seat_dataset):
        return False
    seat_slug = seat_dataset.removeprefix("seat:")
    if identity.role == "admin" or "sources:sync" in effective_scopes(identity):
        return True
    if identity.role == "writer" and identity.seat_slug == seat_slug:
        return True
    return False


def can_decide_promotion_item(
    identity: AccessIdentity,
    item_seat_slug: str,
) -> tuple[bool, bool]:
    if identity.role == "admin" or "access:manage" in effective_scopes(identity):
        delegate = identity.seat_slug is not None and identity.seat_slug != item_seat_slug
        if identity.seat_slug is None:
            delegate = True
        return True, delegate
    if identity.seat_slug == item_seat_slug:
        return True, False
    return False, False


def promotion_pending_filter_seat(identity: AccessIdentity) -> str | None:
    if identity.role == "admin" or "access:manage" in effective_scopes(identity):
        return None
    if identity.seat_slug:
        return identity.seat_slug
    raise HTTPException(
        status_code=403,
        detail="Promotion queue is only available to seat holders and admins.",
    )


def redact_pending_item(item: dict[str, Any]) -> dict[str, Any]:
    """Strip the candidate body, and make sure a secret-scan verdict is present.

    Items queued before the scan existed carry ``secret_scan: None``. The Review
    view states "secret scan passed" per candidate, so serving None would either
    render as a claim nobody checked or force the UI to invent one. Scanning
    here closes that: the candidate body is still on the item at this point,
    just before it is dropped, so the verdict is real for every item rather than
    only for newly queued ones.

    Deliberately not persisted. This is a GET, and a read path that writes to
    the access store to memoise a regex result buys little and costs a lock.
    """
    from datetime import datetime, timezone

    redacted = dict(item)
    text = redacted.pop("candidate_text", None)
    if not redacted.get("secret_scan") and text:
        try:
            scan = scan_candidate(
                text,
                block_severity=get_citadel().config.content_scan_block_severity,
            )
            scan["scanned_at"] = datetime.now(timezone.utc).isoformat()
            scan["deferred"] = True  # scanned on read, not at enqueue
            redacted["secret_scan"] = scan
        except Exception:
            # Never fail a queue listing over the scanner. None keeps reading as
            # "not scanned", which the UI must not show as a pass.
            logger.exception("Deferred secret scan failed for promotion item")
    return redacted


def enforce_dataset_allowlist(identity: AccessIdentity, dataset: str) -> None:
    if can_bypass_dataset_allowlist(identity):
        return
    if dataset == SESSION_TRACES_DATASET:
        # Org-wide consultable prior work: readable by every authenticated caller;
        # writes are gated by the share-session endpoint instead.
        return
    if dataset in identity.allowed_datasets:
        return
    # Seat nodes are private memory: the seat: namespace is default-deny even for
    # callers that carry no allowlist at all. Without this, any legacy or non-seat
    # token (whose allowed_datasets is empty) could read or write another seat's
    # node by naming it explicitly. Non-seat datasets stay open for unscoped tokens
    # to preserve backward compatibility.
    if is_seat_dataset(dataset):
        raise HTTPException(status_code=403, detail=f"Dataset not allowed: {dataset}.")
    if not identity.allowed_datasets:
        return
    raise HTTPException(status_code=403, detail=f"Dataset not allowed: {dataset}.")


def dataset_visible_to(identity: AccessIdentity, dataset: str) -> bool:
    """Boolean twin of enforce_dataset_allowlist for read-side projections.

    Used where a hidden dataset should silently disappear from a payload
    (e.g. /api/mesh/graph attribution) instead of rejecting the request.
    Delegates to enforce_dataset_allowlist so the two can never drift.
    """
    try:
        enforce_dataset_allowlist(identity, dataset)
    except HTTPException:
        return False
    return True


def scope_override_active(
    identity: AccessIdentity,
    datasets: list[str] | tuple[str, ...],
) -> bool:
    """True when a bypassing caller that carries an explicit allowlist reaches a
    dataset outside it — the auditable "admin overrode scope" case. Callers with
    no allowlist (env/bootstrap) were never scope-bound, so they are not flagged.
    """
    if not identity.allowed_datasets:
        return False
    if not can_bypass_dataset_allowlist(identity):
        return False
    return any(dataset not in identity.allowed_datasets for dataset in datasets)


ORG_BOUND_TAGS = frozenset(
    {
        "vault-contribution",
        "org-ready",
        "repo-content",
        "product-knowledge",
        "github",
        "github-daily",
    }
)
PROMOTION_TAGS = frozenset({"org-ready", "vault-contribution"})
IngestTier = str


@dataclass(frozen=True)
class WriteTarget:
    dataset: str
    tier: IngestTier


def central_dataset(config: CitadelConfig) -> str:
    return config.github_sync_dataset or CENTRAL_DATASET


def is_org_bound(tags: list[str] | tuple[str, ...]) -> bool:
    return bool(set(normalize_tags(tags)) & ORG_BOUND_TAGS)


def is_promotion(tags: list[str] | tuple[str, ...]) -> bool:
    return bool(set(normalize_tags(tags)) & PROMOTION_TAGS)


def seat_safe_tags(identity: AccessIdentity, tags: list[str] | tuple[str, ...]) -> list[str]:
    """Drop org/promotion tags for seat writers (ADR-0007 Node-only writes)."""
    normalized = list(normalize_tags(tags))
    if not is_seat_identity(identity) or can_bypass_dataset_allowlist(identity):
        return normalized
    blocked = ORG_BOUND_TAGS | PROMOTION_TAGS
    return [tag for tag in normalized if tag not in blocked]


def resolve_search_datasets(
    identity: AccessIdentity,
    requested: str | None,
    config: CitadelConfig,
) -> list[str]:
    if requested:
        enforce_dataset_allowlist(identity, requested)
        return [requested]

    node_dataset = identity.default_dataset if is_seat_dataset(identity.default_dataset) else None
    if node_dataset:
        enforce_dataset_allowlist(identity, node_dataset)
        datasets = [node_dataset]
        central = central_dataset(config)
        if central != node_dataset:
            if can_bypass_dataset_allowlist(identity) or (
                not identity.allowed_datasets or central in identity.allowed_datasets
            ):
                enforce_dataset_allowlist(identity, central)
                datasets.append(central)
        enforce_dataset_allowlist(identity, SESSION_TRACES_DATASET)
        if SESSION_TRACES_DATASET not in datasets:
            datasets.append(SESSION_TRACES_DATASET)
        return datasets

    dataset = identity.default_dataset or config.search_default_dataset or config.default_dataset
    enforce_dataset_allowlist(identity, dataset)
    return [dataset]


def resolve_search_dataset(
    identity: AccessIdentity,
    requested: str | None,
    config: CitadelConfig,
) -> str:
    return resolve_search_datasets(identity, requested, config)[0]


def is_seat_identity(identity: AccessIdentity) -> bool:
    # A seat is a private-memory boundary, identified by a seat: node either as
    # the default target or anywhere in the allowlist. Keying only off
    # default_dataset would let a seat token whose default is Central slip the
    # curation gate, so the allowlist is the authoritative signal.
    if is_seat_dataset(identity.default_dataset):
        return True
    return any(is_seat_dataset(dataset) for dataset in identity.allowed_datasets)


def seat_node_dataset(identity: AccessIdentity) -> str | None:
    if is_seat_dataset(identity.default_dataset):
        return identity.default_dataset
    for dataset in identity.allowed_datasets:
        if is_seat_dataset(dataset):
            return dataset
    return None


def guard_seat_write_policy(
    identity: AccessIdentity,
    *,
    operation: str,
    dataset: str | None,
    tags: list[str] | tuple[str, ...],
) -> None:
    """ADR-0007: seat-scoped callers write to their Node only; Central is read-only."""
    if not is_seat_identity(identity) or can_bypass_dataset_allowlist(identity):
        return

    node = seat_node_dataset(identity)
    if not node:
        raise HTTPException(
            status_code=403,
            detail="Seat identity has no personal node configured.",
        )

    if operation == "contribute":
        raise HTTPException(
            status_code=403,
            detail=(
                "Seat holders cannot contribute directly to Central. Add durable notes "
                "to your personal node; Central is updated via Promotion and org sync."
            ),
        )

    normalized_tags = normalize_tags(tags)
    if is_org_bound(normalized_tags) or is_promotion(normalized_tags):
        raise HTTPException(
            status_code=403,
            detail=(
                "Seat writes cannot use org or promotion tags. Content stays in your "
                "personal node; Central is updated via Promotion and org sync."
            ),
        )

    if dataset and dataset != node:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Seat writes may only target your personal node ({node}). "
                "Central is read-only; use Promotion to share org knowledge."
            ),
        )


def guard_curated_central(
    identity: AccessIdentity,
    dataset: str,
    tags: list[str] | tuple[str, ...],
    config: CitadelConfig,
) -> None:
    # Central is curated: a seat-holder cannot drop raw content straight into it.
    # Writes to Central from a seat must carry an org tag (which routes through
    # promotion/dual-write) or go through /api/contribute. Admin/env callers
    # bypass this, and non-seat service accounts keep their direct Central path.
    if (
        dataset == central_dataset(config)
        and is_seat_identity(identity)
        and not is_org_bound(tags)
        and not can_bypass_dataset_allowlist(identity)
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Direct writes to Central require an org tag "
                "(org-ready / vault-contribution) or /api/contribute."
            ),
        )


def resolve_write_targets(
    identity: AccessIdentity,
    requested: str | None,
    tags: list[str],
    config: CitadelConfig,
) -> list[WriteTarget]:
    normalized_tags = list(normalize_tags(tags))

    if is_seat_identity(identity) and not can_bypass_dataset_allowlist(identity):
        node = seat_node_dataset(identity)
        if not node:
            raise HTTPException(
                status_code=403,
                detail="Seat identity has no personal node configured.",
            )
        guard_seat_write_policy(
            identity,
            operation="ingest",
            dataset=requested,
            tags=normalized_tags,
        )
        enforce_dataset_allowlist(identity, node)
        return [WriteTarget(node, "light")]

    if requested == SESSION_TRACES_DATASET:
        raise HTTPException(
            status_code=403,
            detail=(
                "Direct ingest to session-traces is not allowed. "
                "Use citadel_share_session to volunteer a Shared Session Trace."
            ),
        )

    if requested:
        dataset = requested
        guard_curated_central(identity, dataset, tags, config)
        enforce_dataset_allowlist(identity, dataset)
        tier: IngestTier = "light" if is_seat_dataset(dataset) and not is_org_bound(tags) else "full"
        return [WriteTarget(dataset, tier)]

    node_dataset = identity.default_dataset if is_seat_dataset(identity.default_dataset) else None
    central = central_dataset(config)

    if is_promotion(normalized_tags) and node_dataset:
        targets = [
            WriteTarget(node_dataset, "light"),
            WriteTarget(central, "full"),
        ]
        for target in targets:
            enforce_dataset_allowlist(identity, target.dataset)
        return targets

    if is_org_bound(normalized_tags):
        enforce_dataset_allowlist(identity, central)
        return [WriteTarget(central, "full")]

    dataset = identity.default_dataset or config.default_dataset
    guard_curated_central(identity, dataset, normalized_tags, config)
    enforce_dataset_allowlist(identity, dataset)
    tier = "light" if is_seat_dataset(dataset) else "full"
    return [WriteTarget(dataset, tier)]


def resolve_write_targets_for_share(
    identity: AccessIdentity,
    config: CitadelConfig,
) -> list[WriteTarget]:
    """Dual-write a volunteered trace to the seat Node (light) and session-traces (shared)."""
    if not is_seat_identity(identity) or can_bypass_dataset_allowlist(identity):
        raise HTTPException(
            status_code=403,
            detail="Shared Session Traces may only be volunteered by a seat holder.",
        )
    node = seat_node_dataset(identity)
    if not node:
        raise HTTPException(
            status_code=403,
            detail="Seat identity has no personal node configured.",
        )
    targets = [
        WriteTarget(node, "light"),
        WriteTarget(SESSION_TRACES_DATASET, "shared"),
    ]
    for target in targets:
        enforce_dataset_allowlist(identity, target.dataset)
    return targets


def resolve_write_dataset(
    identity: AccessIdentity,
    requested: str | None,
    config: CitadelConfig,
) -> str:
    return resolve_write_targets(identity, requested, [], config)[0].dataset


# Private marker set during cross-dataset merge; never emitted to callers.
SHARED_TRACE_MARKER = "_citadel_shared_trace"


def search_result_dedup_key(result: Any) -> str:
    if isinstance(result, dict):
        text = first_string(
            result.get("text"),
            result.get("content"),
            result.get("chunk"),
            result.get("body"),
            result.get("summary"),
            result.get("title"),
            result.get("query"),
        )
        if text:
            return text.strip().lower()
        return json.dumps(
            {key: value for key, value in result.items() if key != "_citadel"},
            sort_keys=True,
            default=str,
        )
    return str(result)


async def search_across_datasets(
    citadel: Citadel,
    *,
    query: str,
    datasets: list[str],
    sessions: Mapping[str, str | None],
    top_k: int,
) -> list[tuple[str, Any]]:
    # Query every dataset before merging so a result-rich primary node can never
    # short-circuit (and thereby silently drop) Central. The primary still wins
    # dedup and takes the bulk of the slots; a reserved slice keeps room for the
    # secondary datasets when more than one is in scope. Sessions are resolved per
    # dataset: a seat's private session must not scope shared datasets like Central
    # (see resolve_search_sessions), or it would hide org-wide hits.
    # Query datasets concurrently (the reads are independent and touch no Kuzu
    # writer), so a 2-dataset seat search costs ~one recall, not two (#50). gather
    # preserves order, so the primary-wins merge below is unchanged.
    results_per = await asyncio.gather(
        *[
            citadel.search(
                query,
                dataset=dataset,
                session_id=sessions.get(dataset),
                top_k=top_k,
            )
            for dataset in datasets
        ]
    )
    per_dataset: list[tuple[str, list[Any]]] = [
        (dataset, list(results)) for dataset, results in zip(datasets, results_per)
    ]

    merged: list[tuple[str, Any]] = []
    seen: set[str] = set()
    # A volunteered trace is dual-written to the author's Node and to
    # session-traces (resolve_write_targets_for_share). The Node copy wins dedup
    # below, and ``reference-only`` is stamped off the dataset alone — so without
    # this the author's own dead-end trace comes back as ordinary knowledge.
    # Carry the marker across the dedup so the lower trust always survives.
    trace_keys = {
        search_result_dedup_key(result)
        for dataset, results in per_dataset
        if dataset == SESSION_TRACES_DATASET
        for result in results
    }

    def take(dataset: str, results: list[Any], budget: int) -> None:
        for result in results:
            if budget <= 0 or len(merged) >= top_k:
                return
            key = search_result_dedup_key(result)
            if key in seen:
                continue
            seen.add(key)
            if (
                dataset != SESSION_TRACES_DATASET
                and key in trace_keys
                and isinstance(result, dict)
            ):
                result = {**result, SHARED_TRACE_MARKER: True}
            merged.append((dataset, result))
            budget -= 1

    if not per_dataset:
        return merged

    reserve = max(1, top_k // 5) if len(per_dataset) > 1 else 0
    primary_dataset, primary_results = per_dataset[0]
    take(primary_dataset, primary_results, top_k - reserve)
    for dataset, results in per_dataset[1:]:
        take(dataset, results, top_k - len(merged))
    # Backfill any slots the secondaries left unused from the primary node.
    take(primary_dataset, primary_results, top_k - len(merged))
    return merged


async def execute_learning_writes(
    learning: LearningProcess,
    *,
    data: str,
    targets: list[WriteTarget],
    tags: list[str],
    session_id: str | None,
    operation: str,
    detect_conflicts: bool = True,
    run_improve: bool = False,
    defer_cognify: bool = False,
) -> tuple[LearningOutcome, list[LearningOutcome]]:
    outcomes: list[LearningOutcome] = []
    primary: LearningOutcome | None = None
    for target in targets:
        outcome = await learning.learn(
            data,
            dataset=target.dataset,
            tags=tags,
            session_id=session_id,
            operation=operation,
            detect_conflicts=detect_conflicts and target.tier == "full",
            run_improve=run_improve and target.tier == "full",
            tier=target.tier,
            defer_cognify=defer_cognify,
        )
        outcomes.append(outcome)
        if primary is None or target.tier == "full":
            primary = outcome
    if primary is None:
        raise RuntimeError("execute_learning_writes requires at least one target")
    return primary, outcomes


async def retry_failed_learning_writes(
    learning: LearningProcess,
    *,
    data: str,
    targets: list[WriteTarget],
    outcomes: list[LearningOutcome],
    tags: list[str],
    session_id: str | None,
    operation: str,
    detect_conflicts: bool = True,
    run_improve: bool = False,
    defer_cognify: bool = False,
) -> list[LearningOutcome]:
    """Retry once for targets whose initial write was not accepted."""
    updated = list(outcomes)
    for index, (target, outcome) in enumerate(zip(targets, outcomes, strict=True)):
        if outcome.ingest.accepted:
            continue
        logger.warning(
            "%s write to %s was not accepted; retrying once",
            operation,
            target.dataset,
        )
        updated[index] = await learning.learn(
            data,
            dataset=target.dataset,
            tags=tags,
            session_id=session_id,
            operation=operation,
            detect_conflicts=detect_conflicts and target.tier == "full",
            run_improve=run_improve and target.tier == "full",
            tier=target.tier,
            defer_cognify=defer_cognify,
        )
    return updated


def assert_requested_session_allowed(identity: AccessIdentity, requested: str | None) -> None:
    # A session id is private context, and session-scoped recall ignores the
    # dataset allowlist (Cognee recalls by session without a dataset constraint).
    # Seat sessions are `seat-{slug}` — derived from a guessable slug — so a caller
    # who could name another seat's session would read that seat's private node,
    # sidestepping node isolation. A non-bypass caller may therefore only name
    # their own default_session; admin/env callers keep full session reach (org
    # sync sessions, cross-seat support).
    if not requested:
        return
    if can_bypass_dataset_allowlist(identity):
        return
    if requested == identity.default_session:
        return
    raise HTTPException(status_code=403, detail="Session not allowed.")


def resolve_session_id(identity: AccessIdentity, requested: str | None) -> str | None:
    assert_requested_session_allowed(identity, requested)
    return requested or identity.default_session


def resolve_search_sessions(
    identity: AccessIdentity,
    requested: str | None,
    datasets: list[str],
) -> dict[str, str | None]:
    # A session is private node memory. Scope it to the caller's own node only, so
    # a seat session can never filter (and thereby hide org-wide hits in) a shared
    # dataset like Central — even when the seat passes its own session explicitly.
    # Admin/env callers may target any session across whatever they searched.
    assert_requested_session_allowed(identity, requested)
    if requested and can_bypass_dataset_allowlist(identity):
        return {dataset: requested for dataset in datasets}
    session = requested or identity.default_session
    if not session:
        return {dataset: None for dataset in datasets}
    owned = identity.default_dataset
    return {
        # The session scopes the caller's own node; for a caller with no node of
        # its own, a single-dataset search still scopes to that one dataset.
        dataset: (
            session
            if dataset == owned or (owned is None and len(datasets) == 1)
            else None
        )
        for dataset in datasets
    }


def node_label(dataset: str | None) -> str | None:
    # A friendly, human label for the caller's private Node dataset. Only seat
    # nodes get a label; shared datasets like Central are not a personal Node.
    if not is_seat_dataset(dataset):
        return None
    slug = dataset[len(SEAT_DATASET_PREFIX) :]
    return f"{slug}'s private Node"


def resolved_memory_scope(
    identity: AccessIdentity,
    config: CitadelConfig,
) -> dict[str, Any]:
    search_datasets = resolve_search_datasets(identity, None, config)
    # Reflect ONLY the authenticated caller's own identity: seat_slug is read
    # straight off this identity (null for non-seat callers), never another seat.
    seat_slug = identity.seat_slug
    return {
        "default_dataset": search_datasets[0],
        "default_session": identity.default_session,
        "allowed_datasets": list(identity.allowed_datasets) or None,
        "search_datasets": search_datasets if len(search_datasets) > 1 else None,
        "seat_slug": seat_slug,
        "node_label": node_label(identity.default_dataset),
    }


_AUTHOR_TAG_RE = re.compile(r"[^a-z0-9]+")


def _author_tag(actor: AccessIdentity) -> str | None:
    if not actor.actor_name:
        return None
    slug = _AUTHOR_TAG_RE.sub("-", actor.actor_name.strip().lower()).strip("-")
    return slug or None


def _contribution_tags(body_tags: list[str], actor: AccessIdentity) -> list[str]:
    tags = list(dict.fromkeys([*body_tags, "vault-contribution"]))
    if not any(tag.startswith("author:") for tag in tags):
        author = _author_tag(actor)
        if author:
            tags.append(f"author:{author}")
    return tags


def role_payload(role: str, identity: AccessIdentity | None = None) -> dict[str, Any]:
    scopes = set(effective_scopes(identity)) if identity else set(default_scopes(role))
    payload: dict[str, Any] = {
        "role": role,
        "capabilities": {
            "read": ROLE_ORDER[role] >= ROLE_ORDER["reader"]
            and bool({"kb:read", "kb:search", "sources:read", "obsidian:sync:pull"} & scopes),
            "write": ROLE_ORDER[role] >= ROLE_ORDER["writer"]
            and bool({"kb:ingest", "kb:feedback", "obsidian:sync:push"} & scopes),
            "admin": ROLE_ORDER[role] >= ROLE_ORDER["admin"]
            and bool({"sources:sync", "access:manage", "audit:read"} & scopes),
        },
        "actor": None
        if identity is None
        else {
            "id": identity.actor_id,
            "kind": identity.actor_kind,
            "name": identity.actor_name,
            "source": identity.source,
            "token_id": identity.token_id,
            "scopes": list(effective_scopes(identity)),
        },
    }
    if identity is not None:
        payload.update(resolved_memory_scope(identity, get_citadel().config))
    return payload


def mcp_tool_name(request: Request) -> str | None:
    tool_name = (request.headers.get(MCP_TOOL_HEADER) or "").strip()
    if tool_name not in TOOL_POLICIES:
        return None
    return tool_name


async def capture_search_feedback(
    *,
    mesh_state: MeshState,
    config: CitadelConfig,
    request: Request,
    actor: AccessIdentity,
    query: str,
    results: list[Any],
    search_datasets: list[str],
    primary_dataset: str,
    top_k: int,
    latency_ms: float,
    timed_out: bool,
    session_id: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Best-effort implicit search telemetry into the feedback mesh index.

    Never raises — search UX must not depend on feedback write success.
    """
    try:
        client_hint = (request.headers.get("user-agent") or "").strip()[:80] or None
        telemetry = build_search_telemetry(
            query=query,
            results=results,
            datasets=search_datasets,
            primary_dataset=primary_dataset,
            top_k=top_k,
            latency_ms=latency_ms,
            timed_out=timed_out,
            truncated=timed_out,
            tool_name=mcp_tool_name(request),
            client_hint=client_hint,
            seat_slug=actor.seat_slug,
            actor_id=actor.actor_id,
            session_id=session_id,
            filters=filters,
        )
        # search_id_for hashes query + result_count + datasets only, so the
        # same query with and without mode=docs (or any filter) produced the
        # SAME search_id and telemetry could not tell the two calls apart.
        # Suffix a digest of the filters: still deterministic (identical calls
        # still share an id, so explicit follow-up feedback keeps linking), but
        # differently-filtered calls no longer collide.
        base_search_id = telemetry.get("search_id")
        if filters and isinstance(base_search_id, str):
            filters_digest = hashlib.sha256(
                json.dumps(filters, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:8]
            telemetry["search_id"] = f"{base_search_id}.f{filters_digest}"
        # ADR-0009 (presence for all, content per caller): the row lands on the
        # caller's own Node when they have one, so query text and hit ids stay
        # private to them. A row that has to land on a shared dataset — a
        # seat-less token, or a seat narrowing with an explicit ``dataset=`` —
        # is written presence-only. Scoping is by ``details.dataset`` alone
        # (mesh.timeline / scope_mesh_snapshot), so tagging a seat's search with
        # Central would publish their query to every reader.
        node_dataset = (
            actor.default_dataset if is_seat_dataset(actor.default_dataset) else None
        )
        await mesh_state.record_search_telemetry(
            config,
            telemetry=telemetry if node_dataset else presence_only_telemetry(telemetry),
            dataset=node_dataset or primary_dataset,
        )
        return telemetry
    except Exception as exc:  # noqa: BLE001 - feedback must never fail search
        logger.warning("search telemetry write failed: %s", exc)
        return None


def record_mcp_audit(
    request: Request,
    *,
    actor: AccessIdentity | None,
    success: bool,
    dataset: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    tool_name = mcp_tool_name(request)
    if not tool_name:
        return
    policy = TOOL_POLICIES[tool_name]
    event_detail: dict[str, Any] = {
        "surface": "mcp",
        "tool": tool_name,
        "method": request.method,
        "path": request.url.path,
        "required_role": policy.role,
        "required_scope": policy.scope,
        "risk": policy.risk,
    }
    if detail:
        event_detail.update(detail)
    get_access_store().record_event(
        action=f"mcp.{tool_name}",
        actor=actor,
        success=success,
        dataset=dataset,
        detail=event_detail,
    )
    request.state.mcp_audit_recorded = True


def is_mcp_audit_event(event: dict[str, Any]) -> bool:
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    return str(event.get("action") or "").startswith("mcp.") or detail.get("surface") == "mcp"


def audit_events_for_view(events: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    if view == "all":
        return list(events)
    if view == "mcp":
        return [event for event in events if is_mcp_audit_event(event)]
    if view == "access":
        return [event for event in events if not is_mcp_audit_event(event)]
    if view == "failures":
        return [event for event in events if event.get("success") is False]
    raise HTTPException(status_code=422, detail=f"Unsupported audit view: {view}.")


def audit_actor_key(event: dict[str, Any]) -> str | None:
    for key in ("actor_id", "actor_name"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def audit_summary(
    *,
    all_events: list[dict[str, Any]],
    returned_events: list[dict[str, Any]],
) -> dict[str, int]:
    mcp_events = [event for event in all_events if is_mcp_audit_event(event)]
    failure_events = [event for event in all_events if event.get("success") is False]
    mcp_failures = [event for event in mcp_events if event.get("success") is False]
    mcp_actors = {actor for event in mcp_events if (actor := audit_actor_key(event))}
    return {
        "total_events": len(all_events),
        "returned_events": len(returned_events),
        "mcp_events": len(mcp_events),
        "access_events": len(all_events) - len(mcp_events),
        "failure_events": len(failure_events),
        "mcp_failures": len(mcp_failures),
        "mcp_actors": len(mcp_actors),
    }


def audit_limit_value(limit: int | None) -> int | None:
    if limit is None:
        return None
    if limit < 1 or limit > AUDIT_LIMIT_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Audit limit must be between 1 and {AUDIT_LIMIT_MAX}.",
        )
    return limit


def enforce_ingest_size(text: str) -> None:
    """Reject oversized write payloads at the HTTP boundary.

    Mirrors the MCP write tools' byte cap (kb.mcp_server._validate_ingest_size) so a
    direct HTTP caller (or autosync) that bypasses the MCP layer cannot push an
    unbounded body into seat-scoped storage. Shares the MCP limit/env so the two
    surfaces never drift.
    """
    max_bytes = _max_ingest_bytes()
    byte_count = len(text.encode("utf-8"))
    if byte_count > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Payload is {byte_count} bytes; limit is {max_bytes} bytes.",
        )


SESSION_TRACES_BOOTSTRAP_MARKER = (
    "Citadel bootstrap marker for the shared session-traces dataset (ADR-0011)."
)


async def ensure_session_traces_dataset(citadel: Citadel) -> None:
    """Ensure the Cognee dataset exists before seat search/share include it.

    PR #93 adds session-traces to default seat search and share dual-write, but
    existing production nodes may never have received a write to that dataset name.
    A missing dataset makes Cognee search fail with DatasetNotFoundError and
    share_session partial_write_failure on the shared tier.
    """
    try:
        from cognee import datasets as cognee_datasets

        existing = await cognee_datasets.list_datasets()
        names = {
            str(getattr(item, "name", "") or "").strip()
            for item in existing
        }
        if SESSION_TRACES_DATASET in names:
            return
    except Exception:
        logger.debug(
            "Could not list Cognee datasets before %s bootstrap; ingesting marker",
            SESSION_TRACES_DATASET,
            exc_info=True,
        )

    result = await citadel.ingest(
        SESSION_TRACES_BOOTSTRAP_MARKER,
        dataset=SESSION_TRACES_DATASET,
        tags=["citadel-bootstrap", "shared-session-traces"],
        defer_cognify=True,
    )
    if result.accepted:
        logger.info("Bootstrapped Cognee dataset %s", SESSION_TRACES_DATASET)
    else:
        logger.warning(
            "Bootstrap ingest for %s was not accepted: %s",
            SESSION_TRACES_DATASET,
            result.reason,
        )


async def backfill_seat_datasets(citadel: Citadel, store: AccessStore) -> dict[str, int]:
    """Give every existing seat the cognee Dataset row its searches need (#147).

    Seats created before provisioning existed have a dataset NAME and no row,
    so every search for them raises DatasetNotFoundError. Six of eleven live
    seats were in that state and had never had a working vault.

    A boot pass rather than a script, for the same reason
    ``ensure_session_traces_dataset`` above is one: it needs cognee's event
    loop, and running it here makes the repair self-healing instead of
    something an operator has to remember. ``ensure_dataset`` is idempotent and
    also repairs a row whose ACLs are missing, so re-running every boot is both
    safe and the point.

    Relational store only, so this never opens the graph and cannot contend
    with the single Kuzu writer during startup.

    Per-seat failures are swallowed deliberately: one seat that cannot be
    provisioned must not stop the other ten, and must not stop the node
    booting. They are counted and logged.
    """
    counts = {"seats": 0, "created": 0, "failed": 0}
    cognee_client = getattr(citadel, "cognee", None)
    ensure_dataset = getattr(cognee_client, "ensure_dataset", None)
    if not callable(ensure_dataset):
        return counts

    for slug in store.seat_slugs():
        counts["seats"] += 1
        try:
            if await ensure_dataset(seat_dataset(slug)):
                counts["created"] += 1
        except Exception:
            counts["failed"] += 1
            logger.exception("Seat dataset backfill failed for seat:%s", slug)

    if counts["created"] or counts["failed"]:
        logger.info(
            "Seat dataset backfill: seats=%d created=%d failed=%d",
            counts["seats"],
            counts["created"],
            counts["failed"],
        )
    return counts


def known_datasets(config: Any) -> list[str]:
    """Datasets a caller can target, in preference order, deduplicated."""
    ordered: list[str] = []
    for dataset in (
        config.search_default_dataset,
        config.github_sync_dataset,
        SESSION_TRACES_DATASET,
        config.default_dataset,
    ):
        if dataset and dataset not in ordered:
            ordered.append(dataset)
    return ordered


def string_value(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def first_string(*values: Any) -> str | None:
    for value in values:
        normalized = string_value(value)
        if normalized:
            return normalized
    return None


def result_provenance(result: dict[str, Any]) -> dict[str, str]:
    """Provenance for one hit: explicit keys first, then the parsed content header.

    cognee's chunk payloads carry none of the explicit keys (117/117 production
    hits shipped ``provenance == {}``), but the repo-content and Linear syncers
    write a structural header as the first lines of every document they ingest.
    ``parse_content_header`` recovers repo/path/source_url/commit/blob (and the
    Linear issue id) from a header at the START of the chunk only — a header
    quoted mid-body is never credited — and only on the document's FIRST chunk
    (``chunk_index`` 0 or absent), because chunk 1+ starts are author-controlled
    body text. Header-derived values are body text and therefore
    author-controlled even then, so they are marked ``basis: content-header``
    and must never feed ``trust_tier``.
    """
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    provenance = {
        "source": first_string(
            result.get("source"),
            result.get("source_type"),
            metadata.get("source"),
            metadata.get("source_type"),
        ),
        "source_url": first_string(
            result.get("source_url"),
            result.get("url"),
            result.get("uri"),
            metadata.get("source_url"),
            metadata.get("url"),
            metadata.get("uri"),
        ),
        "path": first_string(
            result.get("path"),
            result.get("normalized_path"),
            metadata.get("path"),
            metadata.get("normalized_path"),
        ),
        "title": first_string(result.get("title"), metadata.get("title")),
        "session_id": first_string(result.get("session_id"), metadata.get("session_id")),
    }
    header = parse_content_header(
        first_string(
            result.get("text"),
            result.get("content"),
            result.get("chunk"),
            result.get("body"),
        ),
        # Chunk 1+ starts are author-controlled mid-document text: a header
        # there is forgeable, so only the document's first chunk is credited.
        chunk_index=result.get("chunk_index", metadata.get("chunk_index")),
    )
    if header:
        header_used = False
        for key in ("repo", "path", "source_url", "commit", "blob", "issue", "title"):
            value = header.get(key)
            if value and not provenance.get(key):
                provenance[key] = value
                header_used = True
        if header_used:
            if not provenance.get("source"):
                provenance["source"] = header.get("kind")
            provenance["basis"] = "content-header"
    return {key: value for key, value in provenance.items() if value}


def document_endpoint_for_result(result_id: str) -> str | None:
    # Any real id is now drillable (#28): ghsync:/doc_ as before, plus native
    # cognee node/chunk UUIDs that /api/documents resolves via the graph engine.
    # Only synthetic content-hash ids (chunk:<sha>, given to id-less results) have
    # no backing store, so they stay honestly non-drillable.
    #
    # `result_id` here is the hit's chunk-level `id` (see with_result_id), not
    # its `document_id`. This still resolves because /api/documents walks
    # chunk -> parent document, so a caller passing either id "works" — which
    # hides that they are different ids for different things.
    if not result_id or result_id.startswith("chunk:"):
        return None
    return f"/api/documents/{result_id}"


def result_content_sha256(result: dict[str, Any]) -> str:
    content_basis = {key: value for key, value in result.items() if key != "_citadel"}
    encoded = json.dumps(content_basis, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def with_result_id(result: dict[str, Any]) -> dict[str, Any]:
    """Ensure a search result dict carries a stable ``id`` for drill-down.

    Results that already supply an id (e.g. the GitHub digest fallback) are left
    untouched. Other dict results get a content-derived id for traceability.

    ``id`` here is CHUNK-level: for cognee's CHUNKS query type a hit is the raw
    chunk payload (see ``CogneeClient.recall``), and that payload also carries
    its own ``document_id`` — the parent document's id, the same id
    ``citadel ingest`` reports as ``data_id`` for the write. The two are
    different granularities and neither field documents that on the wire
    (verified live: a hit's ``id`` and ``document_id`` are always distinct
    UUIDs). ``/api/documents/{id}`` resolves a chunk id by walking chunk ->
    parent, so passing ``id`` "works" and hides the mismatch — a caller that
    dedups or cites on ``id`` and later compares against a fetched document's
    own ``.id`` (which is the document id, not the chunk id) will never match.
    Use ``document_id`` for anything that needs to key off the document.

    The field is optional, not guaranteed: the GitHub digest fallback above
    (``search_github_sync_state``) supplies its own ``id`` and no
    ``document_id``, because a digest section is not a stored document.
    """
    if result.get("id"):
        return result
    basis = json.dumps(result, sort_keys=True, default=str)
    derived = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return {"id": f"chunk:{derived}", **result}


def _result_retriever_score(result: dict[str, Any]) -> float | None:
    """A numeric relevance value the retriever itself attached, if any.

    cognee 1.2.2's CHUNKS retriever returns chunk payload dicts without one —
    the vector engine's ScoredResult carries a cosine distance, but the
    retriever hands back ``found_chunk.payload`` only, so the distance is
    dropped upstream of this repo's client boundary. For cognee hits this is
    therefore always None. It is checked anyway so that the moment the client
    boundary starts merging the distance into the payload, hits surface it here
    without another change. Never invented: absent stays absent.

    The one live producer of a ``score`` today is the github digest fallback
    (``search_github_sync_state``), whose value is a token-overlap COUNT — an
    unbounded integer in a different unit. Passing it through here would
    surface it as ``retriever_score`` and flip ``retriever_scores_available``
    on exactly the pages whose only signal is lexical, so that path is
    excluded rather than mislabelled.
    """
    if result.get("source") == "github_sync_state":
        return None
    for key in ("score", "distance", "similarity"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def with_result_metadata(
    result: Any,
    index: int,
    dataset: str,
    *,
    drilldown_predicate: Callable[[str], bool] | None = None,
    query: str | None = None,
) -> Any:
    """Attach a reserved Citadel provenance envelope to dict search results.

    ``drilldown_predicate`` (when supplied) decides, per result id, whether
    ``/api/documents`` would actually return 200 for THIS caller. The
    ``document_drilldown_available`` hint and the ``document_endpoint`` URL are
    then emitted only when the drill-down is honestly reachable, so an agent
    that follows the hint never lands on an ADR-0009 404. Without a predicate
    the flag falls back to "any id with a backing endpoint" (non-caller-scoped
    callers/tests). Synthetic ``chunk:<hash>`` ids stay non-drillable either way.

    ``query`` (when supplied) adds ``_citadel.relevance``: the retriever's own
    score when the payload carries one (it does not today — see
    ``_result_retriever_score``), observable lexical term coverage, and a
    ``match_context`` window around the densest query-term cluster so that a
    caller which truncates ``text`` at the head still shows the part of a long
    document that actually matched.
    """
    if not isinstance(result, dict):
        return result
    # Set when this hit is the Node-side copy of a volunteered session trace that
    # won dedup against the shared one (search_across_datasets). Strip it before
    # hashing so the content hash stays stable across both copies.
    shared_trace = bool(result.get(SHARED_TRACE_MARKER))
    if shared_trace:
        result = {key: value for key, value in result.items() if key != SHARED_TRACE_MARKER}
    normalized = with_result_id(result)
    result_id = str(normalized["id"])
    document_endpoint = document_endpoint_for_result(result_id)
    if not document_endpoint:
        drilldown_available = False
    elif drilldown_predicate is None:
        drilldown_available = True
    else:
        drilldown_available = bool(drilldown_predicate(result_id))
    metadata: dict[str, Any] = {
        "rank": index + 1,
        "dataset": dataset,
        "result_id": result_id,
        "content_sha256": result_content_sha256(normalized),
        "provenance": result_provenance(normalized),
        "retrieval": {
            "untrusted_context": True,
            "citation_required": True,
            "document_drilldown_available": drilldown_available,
        },
    }
    if drilldown_available:
        metadata["document_endpoint"] = document_endpoint
    if query is not None:
        terms = query_terms(query)
        coverage, matched = hit_term_coverage({**normalized, "_citadel": metadata}, terms)
        relevance: dict[str, Any] = {
            "term_coverage": round(coverage, 3),
            "matched_terms": matched,
        }
        retriever_score = _result_retriever_score(normalized)
        if retriever_score is not None:
            relevance["retriever_score"] = retriever_score
        full_text = first_string(
            normalized.get("text"),
            normalized.get("content"),
            normalized.get("chunk"),
            normalized.get("body"),
        )
        if full_text and terms:
            window = best_match_window(full_text, terms)
            if window and window[0] > 0:
                relevance["match_context"] = {"offset": window[0], "text": window[1]}
        metadata["relevance"] = relevance
    # Classify BEFORE deciding trust, because the two questions have different
    # authorities and conflating them is what broke `mode="docs"`.
    #
    # `dataset == SESSION_TRACES_DATASET` is attested: the hit was genuinely read
    # out of the traces dataset, so `reference-only` is honest (ADR-0012).
    # `shared_trace` is NOT attested — it is assigned by matching chunk TEXT
    # against that dataset (search_across_datasets), so any document a trace
    # quotes verbatim inherits it. Source-linked repository documentation carries
    # a full Repository/Source/Commit/Blob header the repo-content syncer wrote,
    # which a text collision cannot fake, so it keeps its own identity.
    #
    # ADR-0017 applied that exception to `doc_type` but left `trust` demoted
    # unconditionally, so Central documentation still came back labelled as a
    # trace's trust tier. This finishes it: the exception now governs both.
    preview = {**normalized, "_citadel": metadata}
    inferred = infer_doc_type(preview)
    source_linked = inferred == DOC_TYPE_CANONICAL
    if dataset == SESSION_TRACES_DATASET or (shared_trace and not source_linked):
        metadata["trust"] = "reference-only"
        author_seat = _trace_author_seat(normalized)
        if author_seat:
            metadata["author_seat"] = author_seat
        created_at = _trace_created_at(normalized)
        if created_at:
            metadata["created_at"] = created_at
        # Re-derive against the now-populated trust so downstream inference sees it.
        preview = {**normalized, "_citadel": metadata}
    doc_type = (
        DOC_TYPE_TRACE
        if metadata.get("trust") == "reference-only" and not source_linked
        else inferred
    )
    metadata["doc_type"] = doc_type
    metadata["content_hint"] = infer_content_hint(preview, doc_type)
    metadata["trust_tier"] = infer_trust_tier(preview, doc_type)
    return {**normalized, "_citadel": metadata}


def _trace_author_seat(result: dict[str, Any]) -> str | None:
    for key in ("Author-Seat", "author_seat"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = first_string(result.get("text"), result.get("content"))
    if not text:
        return None
    match = re.search(r"^Author-Seat:\s*(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _trace_created_at(result: dict[str, Any]) -> str | None:
    for key in ("Created-At", "created_at"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = first_string(result.get("text"), result.get("content"))
    if not text:
        return None
    match = re.search(r"^Created-At:\s*(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def split_search_sections(
    normalized: list[Any],
    *,
    central_dataset: str,
    node_dataset: str | None,
) -> dict[str, list[Any]]:
    sections: dict[str, list[Any]] = {
        "central": [],
        "session_traces": [],
        "node": [],
    }
    for item in normalized:
        if not isinstance(item, dict):
            continue
        envelope = item.get("_citadel")
        dataset = envelope.get("dataset") if isinstance(envelope, dict) else None
        if dataset == SESSION_TRACES_DATASET:
            sections["session_traces"].append(item)
        elif dataset == node_dataset:
            sections["node"].append(item)
        elif dataset == central_dataset:
            sections["central"].append(item)
    return sections


def public_base_url(request: Request) -> str:
    configured = os.getenv("CITADEL_PUBLIC_BASE_URL") or os.getenv("CITADEL_HTTP_BASE_URL")
    if configured:
        return configured.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_host and forwarded_proto:
        host = forwarded_host.split(",", 1)[0].strip()
        proto = forwarded_proto.split(",", 1)[0].strip().lower()
        if host and PUBLIC_HOST_RE.fullmatch(host) and proto in {"http", "https"}:
            return f"{proto}://{host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower() == "https"
    return request.url.scheme == "https"


def public_cacheable_path(path: str) -> bool:
    return path in PUBLIC_CACHE_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_CACHE_PREFIXES)


def public_skill_rows(request: Request) -> list[dict[str, Any]]:
    base = public_base_url(request)
    return [
        {
            **entry,
            "url": f"{base}/skills/{entry['slug']}",
        }
        for entry in skill_catalog()
    ]


def public_mcp_tool_rows() -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "role": policy.role,
            "scope": policy.scope,
            "risk": policy.risk,
        }
        for name, policy in sorted(TOOL_POLICIES.items())
    ]


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    # Set first so it wins the setdefault below. content_security_policy_for()
    # returns the strict policy for every path but the ones that opted in, so
    # deleting this line would tighten the site, never loosen it.
    response.headers.setdefault(
        "Content-Security-Policy", content_security_policy_for(request.url.path)
    )
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if "cache-control" not in response.headers:
        cache_headers = PUBLIC_CACHE_HEADERS if public_cacheable_path(request.url.path) else PRIVATE_CACHE_HEADERS
        for header, value in cache_headers.items():
            response.headers.setdefault(header, value)
    if request_is_https(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.middleware("http")
async def audit_forwarded_mcp_call(request: Request, call_next: Any) -> Response:
    if not mcp_tool_name(request):
        return await call_next(request)

    try:
        response = await call_next(request)
    except Exception as exc:
        if not getattr(request.state, "mcp_audit_recorded", False):
            record_mcp_audit(
                request,
                actor=request_identity(request),
                success=False,
                detail={"error_type": exc.__class__.__name__},
            )
        raise

    if not getattr(request.state, "mcp_audit_recorded", False):
        record_mcp_audit(
            request,
            actor=request_identity(request),
            success=response.status_code < 400,
            detail={"status_code": response.status_code},
        )
    return response


@app.get("/", include_in_schema=False)
async def landing_page() -> FileResponse:
    # The root is the landing page for everyone, signed in or not. The app
    # lives at /app, so one URL means one body: a member can read the landing
    # page and send the link on without being bounced into the dashboard.
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/next", include_in_schema=False)
async def next_preview() -> Response:
    # The Next.js rebuild of the public site, served alongside the hand-written
    # pages rather than instead of them. /, /info, /use-cases, /contact and
    # /login are untouched and stay the real site; these five routes are where
    # the port is checked in a browser until it is good enough to take over.
    #
    # It sends the site's strict CSP, unchanged and by default: the export
    # carries no inline <script> and no inline <style>. That is the whole reason
    # the Pages Router was chosen over the App Router, whose RSC payload is
    # emitted as executable inline script a static export cannot nonce. See
    # web/next.config.ts.
    return webui_page("index")


# The rebuilt dashboard views and the minimum role each needs, mirroring the
# `data-min-role` attributes the current dashboard carries in its markup.
#
# The difference is where the gate runs. The current dashboard ships every
# page's markup to every seat and hides what the role cannot use, so a writer
# does receive the Admin markup. A static export cannot vary its HTML by role at
# all, so the gate has to be the route: a seat that cannot open a view is served
# the locked page instead, and the view's markup never leaves the server.
WEBUI_APP_VIEWS: dict[str, str] = {
    "search": "reader",
    "review": "writer",
    "admin": "admin",
}


def next_app_page(request: Request, view: str, minimum_role: str) -> Response:
    """Serve one dashboard view, behind the same door /app uses."""
    role = session_role(request)
    if not role:
        # Anonymous callers go to the sign-in page rather than the landing page:
        # they asked for the app by name. Same as /app.
        return RedirectResponse("/login", status_code=303)
    if ROLE_ORDER[role] < ROLE_ORDER[minimum_role]:
        return webui_page("app/locked", status_code=403)
    return webui_page(view)


@app.get("/next/app", include_in_schema=False)
async def next_app_home(request: Request) -> Response:
    return next_app_page(request, "app", "reader")


@app.get("/next/app/{view}", include_in_schema=False)
async def next_app_view(view: str, request: Request) -> Response:
    minimum_role = WEBUI_APP_VIEWS.get(view)
    if minimum_role is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return next_app_page(request, f"app/{view}", minimum_role)


# The theme bootstrap. Every exported page loads it from <head> before paint,
# but it is neither a page (the `{page}` allow-list below serves HTML by exact
# name and stays closed to other filenames) nor a build asset (the static
# mount covers only /next/_next). So: one literal route, no path parameter.
#
# The media type is pinned rather than guessed from the filename, because the
# site sends X-Content-Type-Options: nosniff and a browser refuses to execute
# a script that arrives as anything but JavaScript.
@app.get("/next/theme.js", include_in_schema=False)
async def next_theme_js() -> FileResponse:
    script = WEBUI_DIR / "theme.js"
    if not script.is_file():
        # A source checkout that has not run `npm run build:web`.
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(script, media_type="text/javascript; charset=utf-8")


# The rebuilt public pages, one preview route each. Exact names, not a path
# parameter used as a filename: the export is a directory and "../" is a
# filename too.
#
# Registered last, because `{page}` would otherwise swallow /next/app: FastAPI
# matches routes in declaration order, and a path parameter matches anything.
WEBUI_PAGES = frozenset({"info", "use-cases", "contact", "login"})


@app.get("/next/{page}", include_in_schema=False)
async def next_preview_page(page: str) -> Response:
    if page not in WEBUI_PAGES:
        raise HTTPException(status_code=404, detail="Not Found")
    return webui_page(page)


@app.get("/app", include_in_schema=False)
async def ui(request: Request) -> Response:
    if not session_role(request):
        # The dashboard stays behind auth. Anonymous callers go to the sign-in
        # page rather than the landing page: they asked for the app by name.
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
async def login() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> Response:
    """Keep crawlers on the marketing pages and off everything else.

    This node serves a public site (/, /info, /use-cases) from the same origin
    as the app and the whole API, so without this a crawler is free to walk
    /app, /next and /api. Those all require auth and answer 401, but a 401 is
    still a request that reaches the event loop, and #105 is about how little
    it takes to saturate that.

    Was showing up as 404s in the production log, which is how it was noticed.
    """
    body = "\n".join(
        [
            "User-agent: *",
            "Allow: /$",
            "Allow: /info",
            "Allow: /use-cases",
            "Disallow: /api/",
            "Disallow: /app",
            "Disallow: /next/",
            "Disallow: /mcp",
            "Disallow: /search",
            "Disallow: /ingest",
            "",
        ]
    )
    return Response(content=body, media_type="text/plain")


@app.get("/info", include_in_schema=False)
async def info_page() -> FileResponse:
    # Public "State of the Vault" report. Static shell; live tiles hydrate from
    # /api/state so the numbers stay current without redeploying the page.
    return FileResponse(STATIC_DIR / "info.html")


@app.get("/use-cases", include_in_schema=False)
async def use_cases_page() -> FileResponse:
    # What teams run Citadel for, then the partnering profile for EU consortia.
    # Shares info.css/info.js with /info; the live health pill hydrates from the
    # same public /api/state.
    return FileResponse(STATIC_DIR / "use-cases.html")


@app.get("/partners", include_in_schema=False)
async def partners_page() -> RedirectResponse:
    # /partners was the page's first home and is already quoted in commits,
    # docs, and anything sent to a coordinator. Keep the URL alive.
    return RedirectResponse("/use-cases", status_code=301)


@app.get("/contact", include_in_schema=False)
async def contact_page() -> FileResponse:
    # The form that POSTs to /contact below. Its own page rather than a section
    # at the bottom of another one, because it is a destination, not a footer.
    return FileResponse(STATIC_DIR / "contact.html")


@app.post("/contact", include_in_schema=False)
async def partner_contact(body: ContactBody, request: Request) -> dict[str, Any]:
    """Deliver a partnering enquiry to the org's Google Chat space.

    This is the only unauthenticated write path in the service, so it is
    deliberately narrow: it never touches the vault, never becomes Structured
    Knowledge, and never echoes submitted text back into a response. A missing
    gateway is a 503, not a silent drop, so an enquiry is never accepted into
    a void.
    """
    if body.website.strip():
        # Honeypot. A human never sees this field, so anything in it is a bot.
        # Answer 200 so the bot does not learn it was filtered.
        logger.info("Partner contact rejected: honeypot field was filled")
        return {"delivered": True}

    client_ip = contact_client_ip(request)
    if not contact_rate_limit_ok(client_ip):
        raise HTTPException(status_code=429, detail="Too many messages. Try again later.")

    # Persist BEFORE attempting delivery (ADR-0013, amended). Google Chat is
    # unconfigured on this node, so the old "no gateway is a 503" rule was
    # turning every partner enquiry away and keeping no record of it. The ADR's
    # actual requirement is that an enquiry is never accepted into a void; a
    # capped file on the state volume is not a void. The vault is still not a
    # destination — unauthenticated text must not reach the substrate agents
    # read as authority, which is the part of ADR-0013 that stands unchanged.
    from datetime import datetime, timezone

    stored = False
    try:
        get_contact_store().append(
            {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "name": scrub_contact_field(body.name),
                "email": scrub_contact_field(body.email),
                "organization": scrub_contact_field(body.organization),
                "message": scrub_contact_field(body.message),
            }
        )
        stored = True
    except Exception:
        logger.exception("Partner contact could not be stored")

    gateway = contact_gateway()
    if gateway is None:
        if stored:
            logger.info("Partner contact stored from %s (no Chat gateway configured)", client_ip)
            return {"delivered": True, "stored": True}
        raise HTTPException(
            status_code=503,
            detail="The contact channel is not configured on this node. Please email us instead.",
        )

    text = (
        f"*Enquiry via /contact*\n"
        f"*From:* {scrub_contact_field(body.name)}\n"
        f"*Email:* {scrub_contact_field(body.email)}\n"
        f"*Organization:* {scrub_contact_field(body.organization) or 'not given'}\n"
        f"\n{scrub_contact_field(body.message)}"
    )
    try:
        await asyncio.to_thread(gateway.post_digest, text)
    except Exception:
        logger.exception("Partner contact delivery failed")
        if stored:
            # Chat is down but the enquiry is on disk, so it is not lost and the
            # sender should not be told to try again and send it twice.
            return {"delivered": True, "stored": True}
        raise HTTPException(
            status_code=502,
            detail="We could not deliver that right now. Please email us instead.",
        ) from None
    logger.info("Partner contact delivered from %s", client_ip)
    return {"delivered": True, "stored": stored}


@app.get("/api/contact/enquiries")
async def list_contact_enquiries(request: Request, limit: int = 50) -> dict[str, Any]:
    """Read the stored partner enquiries. Admin only.

    The queue exists because Google Chat is unconfigured (ADR-0013, amended).
    Once the gateway is set up this stays useful as the durable record behind a
    Chat message that someone scrolls past.
    """
    # access:manage, not access:read. The latter is defined nowhere: it is in no
    # role's DEFAULT_SCOPES, and validate_role_scopes raises on any scope outside
    # the role defaults, so a token carrying it cannot be minted either. The gate
    # therefore refused every possible caller, including the account owner, and
    # the enquiry queue that ADR-0013 added so partner messages would survive an
    # unconfigured Chat gateway was itself unreadable.
    require_access(request, "admin", "access:manage")
    store = get_contact_store()
    return {
        "enquiries": store.recent(min(max(1, limit), 200)),
        "total": store.count(),
    }


@app.post("/admin/session")
async def create_admin_session(
    body: AdminSessionBody, response: Response, request: Request
) -> dict[str, Any]:
    access_key = body.access_key or body.admin_key
    if not configured_access_keys() and not get_access_store().has_tokens():
        raise HTTPException(status_code=503, detail="Access keys are not configured.")
    if not access_key:
        raise HTTPException(status_code=422, detail="Access key is required.")
    identity_with_cookie = access_key_identity(access_key)
    if not identity_with_cookie:
        # Same throttle as the bearer path (M4): this endpoint accepts the same
        # env keys, so limiting only one of the two would leave the door open.
        client_ip = contact_client_ip(request)
        if not record_auth_failure(client_ip):
            logger.warning("Throttled repeated login failures from %s", client_ip)
            raise HTTPException(
                status_code=429,
                detail="Too many failed authentication attempts. Try again later.",
                headers={"Retry-After": str(AUTH_FAIL_PER_IP_WINDOW_SECONDS)},
            )
        logger.warning("Admin session login rejected: access key did not match any credential")
        raise HTTPException(status_code=401, detail="Access key was rejected.")
    identity, session_cookie = identity_with_cookie
    response.set_cookie(
        ADMIN_COOKIE,
        session_cookie,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return {"ok": True, **role_payload(identity.role, identity)}


@app.post("/admin/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ADMIN_COOKIE)
    return {"ok": True}


@app.get("/api/session")
async def current_session(request: Request) -> dict[str, Any]:
    identity = require_access(request, "reader", "kb:read")
    record_mcp_audit(
        request,
        actor=identity,
        success=True,
        detail={"role": identity.role},
    )
    return {"ok": True, **role_payload(identity.role, identity)}


def _captured_last_7d(node: str | None) -> int | None:
    """Successful ingests into this seat's Node in the last seven days.

    Counted from the AccessStore audit trail, the same durable rows
    ``last_ingest_at`` already reads. The dashboard derives this today by
    filtering the mesh event page, which is capped and rebuilt in memory, so the
    figure silently drops after every redeploy and is bounded by however many
    events the mesh happens to be holding.

    Scoped by ``dataset == node``, so it counts only the caller's own Node.
    Returns None (not 0) for a caller with no seat or on any failure: zero is a
    claim that nothing was captured.
    """
    from datetime import datetime, timedelta, timezone

    if not node:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    total = 0
    try:
        for audit_event in get_access_store().snapshot().get("audit_events") or []:
            if not isinstance(audit_event, dict):
                continue
            if audit_event.get("dataset") != node or not audit_event.get("success"):
                continue
            action = str(audit_event.get("action") or "")
            if action != "ingest" and not action.endswith("citadel_ingest"):
                continue
            detail = audit_event.get("detail") if isinstance(audit_event.get("detail"), dict) else {}
            if detail.get("accepted") is False:
                continue
            created = audit_event.get("created_at")
            if not isinstance(created, str):
                continue
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                total += 1
    except Exception:
        logger.exception("me/summary captured_last_7d failed")
        return None
    return total


@app.get("/api/me/summary")
async def me_summary(request: Request) -> dict[str, Any]:
    """Seat-home aggregates for the authenticated caller (Phase 1 portal).

    Reflects only the caller's seat Node — never another seat's content.
    Non-seat callers get ``seat_slug: null`` and empty Node stats.
    """
    identity = require_access(request, "reader", "kb:read")
    config = get_citadel().config
    scope = resolved_memory_scope(identity, config)
    node = seat_node_dataset(identity)
    seat_slug = identity.seat_slug

    pending_count = 0
    if seat_slug:
        pending_count = len(
            get_access_store().list_promotion_pending(
                seat_slug=seat_slug,
                status="pending",
            )
        )

    document_count = 0
    recent_activity: list[dict[str, Any]] = []
    last_ingest_at: str | None = None
    if node:
        try:
            snapshot = await get_mesh().snapshot(config)
            scoped = scope_mesh_snapshot(snapshot, identity)
            for mesh_node in scoped.get("nodes") or []:
                if not isinstance(mesh_node, dict):
                    continue
                if mesh_node.get("type") != "document":
                    continue
                meta = mesh_node.get("metadata") if isinstance(mesh_node.get("metadata"), dict) else {}
                if meta.get("dataset") == node:
                    document_count += 1
        except Exception:
            logger.exception("me/summary mesh document count failed")

        # Scope the timeline page to this seat Node *before* the limit slice.
        # Filtering only after a mixed visible page (Central + shared traces)
        # can drop all Node activity when org traffic is busy.
        def _node_visible(dataset: str | None) -> bool:
            return dataset == node

        try:
            timeline = await get_mesh().timeline(limit=12, visible=_node_visible)
            for event in timeline.get("events") or []:
                if not isinstance(event, dict):
                    continue
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                recent_activity.append(
                    {
                        "id": event.get("id"),
                        "type": event.get("type"),
                        "message": event.get("message"),
                        "created_at": event.get("created_at") or event.get("at"),
                        "dataset": details.get("dataset"),
                    }
                )
                if last_ingest_at is None and event.get("type") == "ingest":
                    last_ingest_at = event.get("created_at") or event.get("at")
        except Exception:
            logger.exception("me/summary timeline failed")

        # Mesh is ephemeral across process restarts. Prefer live mesh document
        # nodes for the Documents count; use durable AccessStore audit only for
        # presence, last_ingest_at, and recent activity so Seat home does not
        # contradict itself after a redeploy.
        audit_ingests: list[dict[str, Any]] = []
        for audit_event in reversed(get_access_store().snapshot().get("audit_events") or []):
            if not isinstance(audit_event, dict):
                continue
            if audit_event.get("dataset") != node or not audit_event.get("success"):
                continue
            action = str(audit_event.get("action") or "")
            if action != "ingest" and not action.endswith("citadel_ingest"):
                continue
            detail = audit_event.get("detail") if isinstance(audit_event.get("detail"), dict) else {}
            if detail.get("accepted") is False:
                continue
            audit_ingests.append(audit_event)

        if last_ingest_at is None and audit_ingests:
            last_ingest_at = audit_ingests[0].get("created_at")

        # Only synthesize activity from audit when the live mesh page is empty
        # (e.g. post-restart). Never prepend audit rows over current mesh traffic.
        if not recent_activity and audit_ingests:
            recent_activity = [
                {
                    "id": audit_event.get("id"),
                    "type": "ingest",
                    "message": "Node ingest",
                    "created_at": audit_event.get("created_at"),
                    "dataset": node,
                }
                for audit_event in audit_ingests[:8]
            ]

    # Gap 1: "Notes you can read". document_count above is Node-only and comes
    # from the mesh, which empties on restart. This counts the datasets this
    # caller actually searches, from the durable relational store.
    #
    # Read isolation: the sum is taken over resolve_search_datasets() for THIS
    # identity, so a dataset the caller cannot search is never added in. A seat
    # sees its own Node plus Central, never another seat's Node.
    readable_datasets = resolve_search_datasets(identity, None, config)
    readable_document_count: int | None = None
    try:
        cognee_client = getattr(get_citadel(), "cognee", None)
        counts_by_dataset = getattr(cognee_client, "document_counts_by_dataset", None)
        if callable(counts_by_dataset):
            counts = await counts_by_dataset()
            readable_document_count = sum(
                int(counts.get(name) or 0) for name in readable_datasets
            )
            # Prefer the durable count for the Node total too. `document_count`
            # above walks the mesh projection, which is rebuilt in memory and
            # empties on every process restart, so on a service that redeploys
            # per merge it reads 0 for a seat holding thousands of notes. That
            # zero then propagated into `capture_done` below and flipped `empty`
            # true, so the seat home rendered its "nothing captured yet" state
            # over a full Node.
            #
            # Read isolation is unchanged: `node` is THIS caller's own seat Node
            # from seat_node_dataset(identity), so no other seat's dataset is
            # ever read here.
            if node:
                document_count = int(counts.get(node) or 0) or document_count
    except Exception:
        # Null, not zero. Zero is a claim that the caller can read nothing.
        logger.exception("me/summary readable document count failed")

    # Gap 2: "Captured this week", from the durable audit trail rather than the
    # mesh event page, which is bounded and resets on redeploy.
    captured_last_7d = _captured_last_7d(node)

    # Capture proof is documents or a durable ingest timestamp — not search
    # timeline rows, which can appear on an empty Node.
    capture_done = document_count > 0 or last_ingest_at is not None
    empty = bool(seat_slug) and not capture_done
    return {
        "ok": True,
        "seat_slug": seat_slug,
        "node_label": scope.get("node_label"),
        "node_dataset": node,
        "search_datasets": scope.get("search_datasets") or [scope.get("default_dataset")],
        "document_count": document_count,
        "readable_document_count": readable_document_count,
        "captured_last_7d": captured_last_7d,
        "pending_promotions": pending_count,
        "last_ingest_at": last_ingest_at,
        "recent_activity": recent_activity[:8],
        "empty": empty,
        "checklist": [
            {
                "id": "capture",
                "label": "Run capture or MCP ingest into your Node",
                "done": capture_done,
            },
            {
                "id": "search",
                "label": "Search — Central is available even while your Node is empty",
                "done": False,
            },
            {
                "id": "promote",
                # Phase 1: approve/reject is admin-only (`sources:sync`); members
                # see pending count as a status signal, not a self-serve action.
                "label": "Promotions to Central are clear (admin approves in Phase 1)",
                "done": pending_count == 0 and capture_done,
            },
        ]
        if seat_slug
        else [],
    }


ACCESS_AUDIT_DEFAULT_LIMIT = 200
ACCESS_AUDIT_MAX_LIMIT = 1000


@app.get("/api/access")
async def access_snapshot(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Access snapshot, with the audit trail paged newest first.

    ``audit_events`` used to be the entire array, which the dashboard sliced to
    twelve. That grows without bound: every ingest, search and MCP call appends
    a row, so the payload gets heavier every day for a view that shows a dozen.

    Paging is additive. ``audit_events`` keeps its name, its shape and its
    existing oldest-first ordering, so a consumer that reads it unchanged still
    works. ``limit`` defaults to the newest 200 rather than to everything, which
    is far more than any current consumer renders. ``audit_events_total`` and
    ``next_cursor`` are there so a caller can tell it received a page rather
    than the lot.

    ``cursor`` is an event id: the page continues from immediately older than
    that event. An unknown cursor is treated as "start from the newest" rather
    than as an error, so a stale cursor cannot wedge the Admin view.
    """
    require_access(request, "admin", "access:manage")
    bootstrap_counts = {"reader": 0, "writer": 0, "admin": 0}
    for role, _ in configured_access_keys():
        bootstrap_counts[role] += 1

    snapshot = get_access_store().snapshot()

    # Gap 9, the other half: state the seat on the token instead of making every
    # consumer join tokens against principals to discover it is missing.
    principals_by_id = {principal["id"]: principal for principal in snapshot.get("principals") or []}
    tokens: list[dict[str, Any]] = []
    for token in snapshot.get("tokens") or []:
        principal = principals_by_id.get(token.get("principal_id")) or {}
        seat_slug = principal.get("seat_slug")
        tokens.append(
            {
                **token,
                "seat_slug": seat_slug,
                "seatless": not seat_slug,
            }
        )
    snapshot["tokens"] = tokens

    all_events = list(snapshot.get("audit_events") or [])
    total = len(all_events)

    if limit is None:
        page_size = ACCESS_AUDIT_DEFAULT_LIMIT
    else:
        page_size = max(1, min(int(limit), ACCESS_AUDIT_MAX_LIMIT))

    end = total
    if cursor:
        for index in range(total - 1, -1, -1):
            event = all_events[index]
            if isinstance(event, dict) and event.get("id") == cursor:
                end = index
                break
    start = max(0, end - page_size)
    page = all_events[start:end]

    snapshot["audit_events"] = page
    return {
        "ok": True,
        "bootstrap_keys": bootstrap_counts,
        **snapshot,
        "audit_events_total": total,
        "audit_events_returned": len(page),
        # The oldest id on this page. Pass it back as `cursor` for the next,
        # older page. Null when there is nothing older left.
        "next_cursor": (page[0].get("id") if start > 0 and page and isinstance(page[0], dict) else None),
    }


CORPUS_DEFAULT_LIMIT = 200
CORPUS_MAX_LIMIT = 1000
CORPUS_INFLIGHT_LIMIT = 2
# A census cursor is (created_at, id) and only moves forward. One row with a
# future-dated created_at would otherwise become a cursor no real row can ever
# exceed, stalling every later page while the endpoint reports ok. Clamp the
# timestamp to now+skew when a cursor is BUILT and again when one is READ BACK
# (a stored cursor outlives the request that built it). The cost is bounded:
# rows inside the skew window can repeat across pages, which beats never
# seeing a row again.
CORPUS_CURSOR_MAX_SKEW_SECONDS = 300.0


def _encode_corpus_cursor(created_at_iso: str, document_id: str) -> str:
    import base64

    payload = json.dumps(
        {"t": created_at_iso, "id": document_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_corpus_cursor(cursor: str | None) -> "tuple[Any, str] | None":
    """(created_at, id) out of an opaque cursor, or None to start from the top.

    Malformed and stale cursors restart the walk rather than 4xx — the same
    stance /api/access takes, so a saved cursor can never wedge the caller.
    """
    import base64
    from datetime import datetime, timezone

    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        created_at = datetime.fromisoformat(str(payload["t"]))
        document_id = str(payload["id"])
    except Exception:  # noqa: BLE001 - any malformed cursor means start over
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at, document_id


def _clamp_corpus_cursor_time(created_at: Any) -> Any:
    from datetime import datetime, timedelta, timezone

    ceiling = datetime.now(timezone.utc) + timedelta(
        seconds=CORPUS_CURSOR_MAX_SKEW_SECONDS
    )
    return min(created_at, ceiling)


@app.get("/api/corpus")
async def corpus_census(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Row-level census of the corpus, from the durable relational store.

    Nothing else on the node can enumerate the corpus: /api/mesh/graph caps at
    1000 nodes and returns labels only, and per-source "documents" counters
    are sync-state bookkeeping. This pages over the rows cognee writes when it
    accepts content, and marks each row with whether the vector store
    (``chunk_count``) and the graph (``in_graph``) have actually seen it —
    the difference between "accepted but never indexed" and "indexed".

    Presence flags degrade to null with a top-level note, never to 0: a 0 is a
    measurement ("we looked, nothing there") and null is "this node could not
    look", and cleanup decisions hang on that difference. Totals report both
    the unscoped corpus and the default-owner slice so rows under another
    owner_id surface instead of silently vanishing from the count.
    """
    from datetime import datetime, timezone

    require_access(request, "admin", "audit:read")

    cognee_client = getattr(get_citadel(), "cognee", None)
    corpus_page = getattr(cognee_client, "corpus_page", None)
    corpus_totals = getattr(cognee_client, "corpus_totals", None)
    if not callable(corpus_page) or not callable(corpus_totals):
        raise HTTPException(
            status_code=503, detail="Corpus census is unavailable on this node."
        )

    if limit is None:
        page_size = CORPUS_DEFAULT_LIMIT
    else:
        page_size = max(1, min(int(limit), CORPUS_MAX_LIMIT))

    after = _decode_corpus_cursor(cursor)
    after_created_at: str | None = None
    after_id: str | None = None
    if after is not None:
        after_created_at = _clamp_corpus_cursor_time(after[0]).isoformat()
        after_id = after[1]

    with _SearchSlot(CORPUS_INFLIGHT_LIMIT, "_corpus_inflight"):
        # One extra row decides has_more, so next_cursor is null exactly at the
        # end instead of costing every caller a final empty page.
        rows = list(
            await corpus_page(
                after_created_at=after_created_at,
                after_id=after_id,
                limit=page_size + 1,
            )
            or []
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        totals = await corpus_totals()

        notes: list[str] = []
        document_ids = [str(row["id"]) for row in rows if row.get("id")]

        chunk_counts: dict[str, int] | None = None
        try:
            chunk_counts_read = getattr(cognee_client, "corpus_chunk_counts", None)
            if callable(chunk_counts_read):
                chunk_counts = await chunk_counts_read(document_ids)
        except Exception:  # noqa: BLE001 - degrade to "not measured", never to 0
            logger.exception("corpus census: chunk-count lookup failed")
        if chunk_counts is None:
            notes.append(
                "chunk_count was not determined for this page (vector store "
                "lookup unavailable); null means not measured, not zero"
            )

        in_graph_ids: set[str] | None = None
        try:
            graph_presence_read = getattr(cognee_client, "corpus_graph_presence", None)
            if callable(graph_presence_read):
                in_graph_ids = await graph_presence_read(document_ids)
        except Exception:  # noqa: BLE001 - degrade to "not measured", never to 0
            logger.exception("corpus census: graph presence lookup failed")
        if in_graph_ids is None:
            notes.append(
                "in_graph was not determined for this page (graph lookup "
                "unavailable); null means not measured, not absent"
            )
        elif document_ids and not in_graph_ids:
            # The graph adapter answers a FAILED batch lookup and a no-match
            # batch identically (empty), so an all-absent page is a weaker
            # claim than a mixed one. Say so instead of letting false read as
            # fully attested.
            notes.append(
                "no document on this page was found in the graph; the graph "
                "adapter reports a failed lookup the same way as no matches, "
                "so treat an all-absent page as weakly attested"
            )

        for row in rows:
            row_id = str(row.get("id"))
            row["chunk_count"] = (
                None if chunk_counts is None else int(chunk_counts.get(row_id, 0))
            )
            row["in_graph"] = (
                None if in_graph_ids is None else (row_id in in_graph_ids)
            )

        next_cursor: str | None = None
        if has_more and rows:
            last = rows[-1]
            last_created_at = last.get("created_at")
            last_id = last.get("id")
            if last_created_at and last_id:
                parsed = datetime.fromisoformat(str(last_created_at))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                next_cursor = _encode_corpus_cursor(
                    _clamp_corpus_cursor_time(parsed).isoformat(), str(last_id)
                )
            else:
                notes.append(
                    "pagination stopped early: the page ends on a row without "
                    "created_at/id, which cannot form a cursor"
                )

        other_owner_rows = int(totals.get("documents_other_owners") or 0)
        if other_owner_rows > 0:
            notes.append(
                f"{other_owner_rows} rows belong to owners other than the "
                "default user and are invisible to owner-scoped reads"
            )

    return {
        "ok": True,
        "documents": rows,
        "documents_returned": len(rows),
        "documents_total": totals.get("documents"),
        "next_cursor": next_cursor,
        "totals": totals,
        "notes": notes,
    }


@app.get("/api/audit")
async def audit_snapshot(
    request: Request,
    view: str = "all",
    limit: int | None = None,
) -> dict[str, Any]:
    require_access(request, "admin", "audit:read")
    if view not in AUDIT_VIEWS:
        raise HTTPException(status_code=422, detail=f"Unsupported audit view: {view}.")
    limit = audit_limit_value(limit)
    events = get_access_store().snapshot()["audit_events"]
    filtered_events = audit_events_for_view(events, view)
    returned_events = filtered_events[-limit:] if limit is not None else filtered_events
    return {
        "ok": True,
        "view": view,
        "audit_events": returned_events,
        "summary": audit_summary(all_events=events, returned_events=returned_events),
    }


@app.get("/api/access/seats")
async def list_access_seats(request: Request) -> dict[str, Any]:
    require_access(request, "admin", "access:manage")
    snapshot = get_access_store().snapshot()
    tokens_by_principal: dict[str, list[dict[str, Any]]] = {}
    for token in snapshot["tokens"]:
        tokens_by_principal.setdefault(token["principal_id"], []).append(token)
    seats: list[dict[str, Any]] = []
    # A seat is a principal that carries a seat_slug; derive the list purely from
    # the existing snapshot (no new store schema), joining each seat to its tokens.
    for principal in snapshot["principals"]:
        if not principal.get("seat_slug"):
            continue
        seat_tokens = sorted(
            tokens_by_principal.get(principal["id"], []),
            key=lambda token: token.get("created_at") or "",
        )
        token_rows = [
            {
                "id": token["id"],
                "name": token["name"],
                "role": token["role"],
                # prefix is the masked, non-secret head already stored at rest.
                "prefix": token["prefix"],
                "revoked": bool(token.get("revoked_at")),
                "revoked_at": token.get("revoked_at"),
                "last_used_at": token.get("last_used_at"),
                "created_at": token.get("created_at"),
            }
            for token in seat_tokens
        ]
        active_tokens = sum(1 for token in token_rows if not token["revoked"])
        seats.append(
            {
                "principal_id": principal["id"],
                "name": principal["name"],
                "seat_slug": principal["seat_slug"],
                "node_dataset": principal.get("default_dataset"),
                "email": principal.get("email"),
                "role": principal["role"],
                "disabled": bool(principal.get("disabled_at")),
                "active_token_count": active_tokens,
                "token_count": len(token_rows),
                "tokens": token_rows,
            }
        )
    seats.sort(key=lambda seat: seat["seat_slug"])

    # Seat-less tokens, as a first-class list rather than an absence.
    #
    # A token whose principal carries no seat_slug authenticates perfectly well
    # and then cannot search: it has no Node and no default dataset, so a search
    # raises DatasetNotFoundError, which reads to the operator as "invalid
    # token". That has cost real debugging time. Today the Admin view can only
    # infer this state by joining /api/access/seats against /api/access
    # client-side and noticing a token in neither, which is why it does not.
    seatless: list[dict[str, Any]] = []
    principals_by_id = {principal["id"]: principal for principal in snapshot["principals"]}
    for token in snapshot["tokens"]:
        principal = principals_by_id.get(token["principal_id"]) or {}
        if principal.get("seat_slug"):
            continue
        seatless.append(
            {
                "id": token["id"],
                "name": token["name"],
                "prefix": token["prefix"],
                "role": token["role"],
                "principal_id": token["principal_id"],
                "principal_name": principal.get("name"),
                "principal_kind": principal.get("kind"),
                "seat_slug": None,
                "default_dataset": token.get("default_dataset")
                or principal.get("default_dataset"),
                "revoked": bool(token.get("revoked_at")),
                "revoked_at": token.get("revoked_at"),
                "last_used_at": token.get("last_used_at"),
                "created_at": token.get("created_at"),
                # The consequence, stated rather than implied. A token with no
                # seat and no default dataset cannot run a search at all.
                "can_search": bool(
                    token.get("default_dataset") or principal.get("default_dataset")
                ),
            }
        )
    seatless.sort(key=lambda token: token.get("created_at") or "")

    return {
        "ok": True,
        "seats": seats,
        "seatless_tokens": seatless,
        "seatless_token_count": len(seatless),
    }


@app.post("/api/access/seats")
async def create_access_seat(body: CreateSeatBody, request: Request) -> dict[str, Any]:
    actor = require_access(request, "admin", "access:manage")
    try:
        created = get_access_store().create_seat(
            name=body.name,
            slug=body.slug,
            email=body.email,
            role=body.role,
            issue_token=body.issue_token,
            token_name=body.token_name,
            central_dataset=central_dataset(get_citadel().config),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # create_seat only computes the seat's dataset NAME; cognee creates the row
    # itself lazily, and only on the write path. Until something is ingested the
    # row does not exist, and every search for this seat dies on
    # DatasetNotFoundError (#147). Provision it here so a seat works from the
    # moment it is handed over.
    #
    # Deliberately not fatal. The seat and its token are already persisted by
    # this point, so raising would leave a half-created seat behind and the
    # admin no way to tell which half. Report the outcome instead: the audit
    # event and the response both carry it, and the backfill can pick up any
    # seat that landed unprovisioned.
    provisioned: bool | None = None
    try:
        cognee_client = getattr(get_citadel(), "cognee", None)
        ensure_dataset = getattr(cognee_client, "ensure_dataset", None)
        if callable(ensure_dataset):
            await ensure_dataset(created.principal.default_dataset)
            provisioned = True
    except Exception:
        provisioned = False
        logger.exception(
            "seat dataset provisioning failed for %s", created.principal.default_dataset
        )

    get_access_store().record_event(
        action="access.seat.create",
        actor=actor,
        success=True,
        dataset=created.principal.default_dataset,
        detail={
            "principal_id": created.principal.id,
            "seat_slug": created.principal.seat_slug,
            "token_id": created.api_token.id if created.api_token else None,
            "role": created.principal.role,
            "node_dataset_provisioned": provisioned,
        },
    )
    payload: dict[str, Any] = {
        "ok": True,
        "principal": jsonable_encoder(created.principal),
        "node_dataset_provisioned": provisioned,
    }
    if created.token and created.api_token:
        payload["token"] = created.token
        payload["api_token"] = jsonable_encoder(
            {key: value for key, value in created.api_token.__dict__.items() if key != "token_hash"}
        )
    return payload


@app.post("/api/access/seats/{slug}/tokens")
async def issue_access_seat_token(
    slug: str, body: IssueSeatTokenBody, request: Request
) -> dict[str, Any]:
    """Mint a fresh token for an EXISTING seat (e.g. to re-link a lost token)."""
    actor = require_access(request, "admin", "access:manage")
    if body.role is not None:
        # Reject rather than ignore: a silently dropped role hands the caller a
        # credential different from the one they asked for. A seat token always
        # carries the seat's role; admin in particular is forbidden because an
        # admin token bypasses the seat's dataset allowlist and dissolves its
        # private-memory boundary (AccessStore.create_seat enforces the same).
        raise HTTPException(
            status_code=422,
            detail=(
                "Seat tokens derive their role from the seat; 'role' cannot be "
                "set here. Mint admin tokens without a seat via POST "
                "/api/access/tokens."
            ),
        )
    try:
        normalized = validate_seat_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = get_access_store()
    if not store.find_seat_by_slug(normalized):
        raise HTTPException(status_code=404, detail=f"Seat not found: {normalized}")
    try:
        created = store.issue_seat_token(
            slug=normalized,
            token_name=body.token_name,
            central_dataset=central_dataset(get_citadel().config),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.record_event(
        action="access.seat.token.create",
        actor=actor,
        success=True,
        dataset=created.api_token.default_dataset,
        detail={
            "principal_id": created.principal.id,
            "seat_slug": normalized,
            "token_id": created.api_token.id,
        },
    )
    return {
        "ok": True,
        "token": created.token,
        "principal": jsonable_encoder(created.principal),
        "api_token": jsonable_encoder(
            {key: value for key, value in created.api_token.__dict__.items() if key != "token_hash"}
        ),
    }


@app.get("/api/access/capture-baseline")
async def org_capture_baseline(request: Request) -> dict[str, Any]:
    require_access(request, "admin", "access:manage")
    return capture_policy_payload(
        seat_slug=None,
        baseline=SeatCapturePolicy(),
        env_exclude_patterns=env_exclude_patterns(),
    )


@app.get("/api/access/seats/{slug}/capture-policy")
async def get_seat_capture_policy(slug: str, request: Request) -> dict[str, Any]:
    require_capture_policy_read(request, slug)
    return seat_capture_policy_response(slug)


@app.put("/api/access/seats/{slug}/capture-policy")
async def update_seat_capture_policy(
    slug: str,
    body: CapturePolicyBody,
    request: Request,
) -> dict[str, Any]:
    actor = require_access(request, "admin", "access:manage")
    try:
        normalized = validate_seat_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = get_access_store()
    seat = store.find_seat_by_slug(normalized)
    if not seat:
        raise HTTPException(status_code=404, detail=f"Seat not found: {normalized}")
    try:
        baseline = store.set_capture_policy(
            normalized,
            deny_globs=body.deny_globs,
            actor_id=actor.actor_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.record_event(
        action="access.capture_policy.update",
        actor=actor,
        success=True,
        dataset=seat.default_dataset,
        detail={
            "seat_slug": normalized,
            "deny_glob_count": len(baseline.deny_globs),
        },
    )
    return capture_policy_payload(
        seat_slug=normalized,
        baseline=baseline,
        env_exclude_patterns=env_exclude_patterns(),
    )


@app.get("/api/access/seats/{slug}/capture-roots")
async def get_seat_capture_roots(slug: str, request: Request) -> dict[str, Any]:
    require_capture_policy_read(request, slug)
    return seat_capture_roots_response(slug)


@app.put("/api/access/seats/{slug}/capture-roots")
async def update_seat_capture_roots(
    slug: str,
    body: CaptureRootsBody,
    request: Request,
) -> dict[str, Any]:
    actor, normalized = require_capture_roots_write(request, slug)
    store = get_access_store()
    seat = store.find_seat_by_slug(normalized)
    if not seat:
        raise HTTPException(status_code=404, detail=f"Seat not found: {normalized}")
    try:
        roots = store.set_approved_capture_roots(
            normalized,
            paths=body.roots,
            actor_id=actor.actor_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.record_event(
        action="access.capture_roots.update",
        actor=actor,
        success=True,
        dataset=seat.default_dataset,
        detail={
            "seat_slug": normalized,
            "root_count": len(roots.paths),
        },
    )
    return seat_capture_roots_response(normalized)


@app.post("/api/access/tokens")
async def create_access_token(body: AccessTokenBody, request: Request) -> dict[str, Any]:
    actor = require_access(request, "admin", "access:manage")
    try:
        created = get_access_store().create_principal_token(
            name=body.name,
            kind=body.kind,
            role=body.role,
            scopes=body.scopes,
            team_id=body.team_id,
            expires_at=body.expires_at,
            default_dataset=body.default_dataset,
            default_session=body.default_session,
            allowed_datasets=body.allowed_datasets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    get_access_store().record_event(
        action="access.token.create",
        actor=actor,
        success=True,
        detail={
            "principal_id": created.principal.id,
            "token_id": created.api_token.id,
            "role": created.api_token.role,
            "kind": created.principal.kind,
        },
    )
    return {
        "ok": True,
        "token": created.token,
        "principal": jsonable_encoder(created.principal),
        "api_token": jsonable_encoder(
            {key: value for key, value in created.api_token.__dict__.items() if key != "token_hash"}
        ),
    }


@app.post("/api/access/tokens/{token_id}/revoke")
async def revoke_access_token(token_id: str, request: Request) -> dict[str, Any]:
    actor = require_access(request, "admin", "access:manage")
    revoked = get_access_store().revoke_token(token_id)
    if not revoked:
        get_access_store().record_event(
            action="access.token.revoke",
            actor=actor,
            success=False,
            detail={"token_id": token_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail="Token not found.")
    get_access_store().record_event(
        action="access.token.revoke",
        actor=actor,
        success=True,
        detail={"token_id": token_id},
    )
    redacted = {key: value for key, value in revoked.__dict__.items() if key != "token_hash"}
    return {"ok": True, "api_token": jsonable_encoder(redacted)}


@app.get("/healthz")
async def healthz() -> dict[str, str | bool]:
    return {"ok": True, "service": "citadel"}


@app.get("/api/state")
async def public_state(request: Request, response: Response) -> dict[str, Any]:
    """Public, no-secrets snapshot for the /info page — safe aggregates only.

    Never exposes vault content, per-seat data, graph node dumps, internal
    source URLs, or tokens. A syncer hiccup degrades to empty, never a 500.
    """
    from datetime import datetime, timezone

    response.headers.update(PUBLIC_CACHE_HEADERS)

    async def _safe(coro: Any) -> dict[str, Any]:
        try:
            return await coro
        except Exception:  # best-effort: the public page must never 500 on a sync blip
            return {}

    sources: list[dict[str, Any]] = []
    gh = await _safe(get_github_syncer().status())
    if gh:
        sources.append(
            {
                "name": gh.get("org"),
                "type": "github",
                "documents": gh.get("tracked_repositories", 0),
                "status": "tracked" if gh.get("last_checked_at") else "ready",
                "last_synced_at": gh.get("last_checked_at"),
            }
        )
    rc = await _safe(get_repo_content_syncer().status())
    if rc:
        sources.append(
            {
                "name": rc.get("org"),
                "type": "repo_content",
                "documents": rc.get("tracked_files", 0),
                "status": "tracked" if rc.get("last_checked_at") else "ready",
                "last_synced_at": rc.get("last_checked_at"),
            }
        )
    ln = await _safe(get_linear_syncer().status())
    if ln:
        sources.append(
            {
                "name": "Linear workspace",
                "type": "linear",
                "documents": ln.get("issue_count", 0),
                "status": "tracked" if ln.get("last_synced_at") else "ready",
                "last_synced_at": ln.get("last_synced_at"),
            }
        )

    # Repo figures for the public pages: weekly commits from the daily GitHub
    # refresh, plus counts derived from the source tree. Cache-only, so this
    # adds no outbound request to a public endpoint, and it degrades to an
    # empty block rather than raising.
    try:
        from kb import repo_stats

        repo_block = repo_stats.public_payload(get_citadel().config)
    except Exception:
        logger.debug("Repo stats unavailable for /api/state", exc_info=True)
        repo_block = {
            "adrs": None,
            "mcp_tools": None,
            "weeks": [],
            "commits_total": None,
            "commits_window_weeks": 0,
            "refreshed_at": None,
            "stale": True,
            "source": "unavailable",
        }

    docs_total = sum(int(s.get("documents") or 0) for s in sources)
    return {
        "ok": True,
        "service": "Citadel Archive",
        "version": app.version,
        "healthy": True,
        "sources": sources,
        "totals": {
            "documents": docs_total,
            "github_repositories": int(gh.get("tracked_repositories", 0) or 0) if gh else 0,
            "linear_issues": int(ln.get("issue_count", 0) or 0) if ln else 0,
        },
        "repo": repo_block,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/.well-known/citadel.json")
async def citadel_discovery_manifest(request: Request, response: Response) -> dict[str, Any]:
    """Public agent discovery document with no vault content or secrets."""
    response.headers.update(PUBLIC_CACHE_HEADERS)
    base = public_base_url(request)
    tools = public_mcp_tool_rows()
    return {
        "ok": True,
        "service": {
            "name": "Citadel Archive",
            "kind": "organization_vault",
            "version": app.version,
            "base_url": base,
        },
        "public_endpoints": {
            "health": f"{base}/healthz",
            "skills": f"{base}/skills",
            "discovery": f"{base}/.well-known/citadel.json",
        },
        "mcp": {
            "endpoint": f"{base}{MCP_ENDPOINT_PATH}",
            "transport": "streamable_http",
            "authentication": {
                "required": True,
                "scheme": "bearer",
                "token_prefix": "ctdl_",
                "header": "Authorization",
            },
            "tools": tools,
            "approval_recommended_for": [
                row["name"]
                for row in tools
                if row["risk"] in {"additive_write", "admin_job"}
            ],
            "audit": {
                "event_action": "mcp.<tool_name>",
                "admin_tool": "citadel_audit_events",
            },
        },
        "skills": public_skill_rows(request),
        "security": {
            "public_data": [
                "application code and documentation",
                "hosted skill markdown and content hashes",
                "MCP endpoint URL and tool policy metadata",
            ],
            "private_data": [
                "ctdl_ access tokens",
                "vault search results and source documents",
                "Obsidian sync contents",
                "backup mirror repository contents",
            ],
            "token_handling": [
                "give each human or agent a distinct token",
                "store tokens only in local secret stores or environment variables",
                "rotate any token pasted into chat, logs, issues, pull requests, or public repos",
            ],
            "scope_model": {
                "roles": ["reader", "writer", "admin"],
                "custom_scopes": "Custom scopes can only reduce permissions within the selected role.",
            },
            "seat_mcp_write_policy": {
                "personal_node_only": True,
                "central_read_only_for_seats": True,
                "contribute_via_mcp": False,
                "secret_scan_on_all_writes": True,
                "client_must_require_user_approval_for": [
                    "citadel_ingest",
                    "citadel_contribute",
                    "citadel_record_feedback",
                ],
                "central_updates_via": [
                    "github_sync_cron",
                    "linear_sync_cron",
                    "promotion_engine",
                    "curated_contribute_non_mcp",
                ],
            },
        },
    }


@app.get("/skills")
async def list_skills(request: Request) -> dict[str, Any]:
    """Public index of shareable agent skill URLs (no auth)."""
    return {"ok": True, "skills": public_skill_rows(request)}


@app.get("/skills/{slug}")
async def get_skill(slug: str) -> FileResponse:
    """Serve a bundled agent skill as markdown (no auth)."""
    path = skill_path(slug)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    integrity = skill_integrity(path)
    headers = {
        **PUBLIC_CACHE_HEADERS,
        "ETag": f"\"sha256-{integrity['sha256']}\"",
        "X-Citadel-Skill-SHA256": str(integrity["sha256"]),
        "X-Citadel-Skill-Integrity": str(integrity["integrity"]),
    }
    return FileResponse(path, media_type="text/markdown; charset=utf-8", headers=headers)


async def _corpus_health() -> dict[str, Any]:
    """Data-plane volume gate: are tracked sources actually indexed? (#27)

    Fail-soft — any error returns ok=True with a ``degraded`` note so readiness
    never flaps on a transient graph read; the real signal is "many sources
    tracked but the graph is empty".
    """
    try:
        tracked = 0
        github_status = await get_github_syncer().status()
        tracked += int(github_status.get("tracked_repositories") or 0)
        repo_content_status = await get_repo_content_syncer().status()
        tracked += int(repo_content_status.get("tracked_files") or 0)
        linear_status = await get_linear_syncer().status()
        tracked += int(linear_status.get("issue_count") or 0)
        counts = await get_citadel()._graph_counts()
        indexed = int(counts.get("nodes") or 0)
        # `_graph_counts` already reads the whole graph for `nodes`; `edges` comes
        # back in the same call for free. /api/mesh used to publish the in-memory
        # projection's edge count at the top level instead (24x understated live),
        # so this is the real total that field needs.
        edges = int(counts.get("edges") or 0)
        ok = not (tracked >= _MIN_TRACKED_FOR_CORPUS and indexed < _INDEXED_FLOOR)
        return {
            "ok": ok,
            "tracked_sources": tracked,
            "indexed_docs": indexed,
            "indexed_edges": edges,
        }
    except Exception as exc:  # noqa: BLE001 - readiness must not flap on a transient read
        logger.warning("corpus health check degraded (fail-soft to ok): %s", exc)
        return {
            "ok": True,
            "tracked_sources": None,
            "indexed_docs": None,
            "indexed_edges": None,
            "degraded": str(exc),
        }


@app.get("/readyz")
async def readyz(request: Request) -> Any:
    require_access(request, "reader", "kb:read")
    config = get_citadel().config
    corpus = await _corpus_health()
    canary = _LAST_CANARY
    # RED when the corpus gate trips or the last end-to-end canary failed.
    ok = corpus["ok"] and (canary is None or bool(canary.get("ok", True)))
    payload = {
        "ok": ok,
        "service": "citadel",
        "tenant_id": config.tenant_id,
        "default_dataset": config.default_dataset,
        "auto_improve": config.auto_improve,
        "build_global_context_index": config.build_global_context_index,
        "corpus": corpus,
        "canary": canary,
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


def _mesh_dataset_visible(
    identity: AccessIdentity, dataset: Any, cache: dict[str, bool]
) -> bool:
    if not isinstance(dataset, str) or not dataset:
        return True
    if dataset not in cache:
        cache[dataset] = dataset_visible_to(identity, dataset)
    return cache[dataset]


def scope_mesh_snapshot(
    snapshot: dict[str, Any], identity: AccessIdentity
) -> dict[str, Any]:
    """Strip other seats' content from the runtime-activity projection (ADR-0009).

    The /api/mesh projection records each document's first line and each raw
    search-query string as node labels keyed by ``metadata.dataset``, so an
    unscoped read leaked every seat's Node content to any reader token (and, via
    the citadel_get_mesh MCP tool that proxies /api/mesh with the caller's
    bearer, to any agent). Bypass callers (admin/env) still see everything.
    Scoped callers keep only nodes/edges/events whose dataset they may read.
    Seat *presence* stays universal: ``dataset``-type nodes (the seat hub, whose
    label is only the seat slug) are retained; only the content-bearing nodes
    are dropped.
    """
    if can_bypass_dataset_allowlist(identity):
        return snapshot
    cache: dict[str, bool] = {}
    dropped: set[str] = set()
    kept_nodes: list[dict[str, Any]] = []
    for node in snapshot.get("nodes", []):
        dataset = (node.get("metadata") or {}).get("dataset")
        if node.get("type") != "dataset" and not _mesh_dataset_visible(
            identity, dataset, cache
        ):
            dropped.add(node.get("id"))
            continue
        kept_nodes.append(node)
    kept_edges = [
        edge
        for edge in snapshot.get("edges", [])
        if edge.get("source") not in dropped and edge.get("target") not in dropped
    ]
    kept_events = [
        event
        for event in snapshot.get("events", [])
        if _mesh_dataset_visible(
            identity, (event.get("details") or {}).get("dataset"), cache
        )
    ]
    return {
        **snapshot,
        "nodes": kept_nodes,
        "edges": kept_edges,
        "events": kept_events,
    }


@app.get("/api/mesh")
async def mesh(request: Request) -> Any:
    identity = require_access(request, "reader", "kb:read")
    citadel = get_citadel()
    # Pass the authoritative corpus figures, or the dashboard reports the
    # in-memory counters as though they were totals (ADR-0018). `snapshot` grew a
    # `corpus` parameter for exactly this, but this endpoint, the one the
    # dashboard actually reads, was never updated to supply it. In production it
    # still returned `documents: 1, indexed_chunks: 1` against a real 17991
    # indexed, which is what makes a healthy vault look empty on login.
    # `_corpus_health` is the same source /readyz and `citadel status` use, and
    # is fail-soft: on a transient read error it returns None totals and
    # `snapshot` falls back to the in-memory values rather than raising here.
    snapshot = await get_mesh().snapshot(citadel.config, corpus=await _corpus_health())
    return jsonable_encoder(scope_mesh_snapshot(snapshot, identity))


@app.get("/api/knowledge/events")
async def knowledge_events(
    request: Request,
    after_id: int | None = None,
    limit: int = 50,
    event_type: str | None = Query(default=None, alias="type"),
    kind: str | None = None,
) -> Any:
    identity = require_access(request, "reader", "kb:read")
    if after_id is not None and after_id < 0:
        raise HTTPException(status_code=422, detail="after_id must be zero or greater.")
    if not 1 <= limit <= 160:
        raise HTTPException(status_code=422, detail="Timeline limit must be between 1 and 160.")
    # ADR-0009: the timeline carries Node content (event messages, dataset names,
    # and error operations/reasons), so scope it to the caller exactly as the two
    # sibling projections do — /api/mesh via scope_mesh_snapshot and /events via
    # _mesh_dataset_visible. This endpoint previously discarded the identity and
    # returned every seat's events to any reader token.
    visible_cache: dict[str, bool] = {}
    visible = (
        None
        if can_bypass_dataset_allowlist(identity)
        else (lambda dataset: _mesh_dataset_visible(identity, dataset, visible_cache))
    )
    timeline = await get_mesh().timeline(
        after_id=after_id,
        limit=limit,
        event_type=event_type,
        kind=kind,
        visible=visible,
    )
    return jsonable_encoder({"ok": True, **timeline})


def mesh_presence_hubs() -> list[dict[str, str]]:
    """Universal Seat Presence list for the Knowledge Mesh (ADR-0009).

    Every seat principal appears by its seat dataset name — slug ONLY,
    presence metadata never carries member names or emails — plus Central,
    so the hub inventory comes from the seat inventory, independent of which
    content survives caller scoping.

    Never breaks the graph endpoint: any failure reading the access store
    degrades to a Central-only presence list instead of raising.
    """
    central = central_dataset(get_citadel().config)
    entries: list[dict[str, str]] = [{"dataset": central, "label": central}]
    seen = {central}
    try:
        # Principals-only read: never materializes the audit list or token
        # roster on this latency-watched endpoint (#50).
        for slug in get_access_store().seat_slugs():
            name = f"{SEAT_DATASET_PREFIX}{slug}"
            if name in seen:
                continue
            seen.add(name)
            entries.append({"dataset": name, "label": name})
    except Exception as exc:
        logger.warning(
            "Seat presence read failed with %s; degrading to Central-only presence",
            exc.__class__.__name__,
        )
        return [{"dataset": central, "label": central}]
    return entries


@app.get("/api/mesh/graph")
async def mesh_graph(request: Request, limit: int | None = None) -> Any:
    """The real Knowledge Mesh graph from Cognee (not the dashboard projection).

    Never fails hard: returns an empty graph with ``fallback: true`` when
    Cognee has no data or graph access is unavailable.
    """
    identity = require_access(request, "reader", "kb:search")
    if limit is not None and not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="Graph limit must be between 1 and 1000.")
    effective_limit = limit or get_citadel().config.mesh_graph_max_nodes
    # ADR-0009 read isolation: content follows the caller's search scope (own
    # Node + Central + non-seat datasets), while every seat always appears as
    # a presence hub. Bypass callers (admin/env) pass dataset_visible=None and
    # see all content for support and audit.
    #
    # The graph read + per-caller shaping is the heaviest read-path endpoint and
    # is now the default dashboard view, so cap it: at capacity it returns the
    # 429 + Retry-After contract instead of piling full-graph reads onto the
    # already-starved loop (#50). It uses its OWN budget (not search's) so a
    # ~15-seat login burst on the default Knowledge Mesh view can't starve
    # /search — and vice versa; graph_data() is TTL-cached + single-flight so a
    # small dedicated cap is enough.
    with _SearchSlot(
        get_citadel().config.mesh_graph_max_concurrency, counter="_mesh_graph_inflight"
    ):
        graph = await get_knowledge_mesh().graph(
            limit=effective_limit,
            dataset_visible=(
                None
                if can_bypass_dataset_allowlist(identity)
                else lambda name: dataset_visible_to(identity, name)
            ),
            presence=mesh_presence_hubs(),
            collapse_orphans=True,
        )
    return jsonable_encoder({**graph, "limit": effective_limit})


@app.get("/api/conflicts")
async def list_knowledge_conflicts(request: Request, status: str | None = None) -> Any:
    actor = require_access(request, "reader", "kb:read")
    if status not in {None, "open", "resolved"}:
        raise HTTPException(status_code=422, detail="Unsupported conflict status filter.")
    store = get_conflict_store()
    conflicts = store.list(status=status)
    get_access_store().record_event(
        action="conflicts.list",
        actor=actor,
        success=True,
        detail={"status": status or "all", "returned": len(conflicts)},
    )
    return {
        "ok": True,
        "status": status or "all",
        "conflicts": jsonable_encoder(conflicts),
        "open_count": store.open_count(),
    }


@app.post("/api/conflicts/{conflict_id}/resolve")
async def resolve_knowledge_conflict(
    conflict_id: str,
    body: KnowledgeConflictResolveBody,
    request: Request,
) -> Any:
    actor = require_access(request, "writer", "kb:ingest")
    try:
        resolved = get_conflict_store().resolve(
            conflict_id,
            resolution_note=body.resolution_note,
            resolved_by=actor.actor_id,
        )
    except KeyError as exc:
        get_access_store().record_event(
            action="conflicts.resolve",
            actor=actor,
            success=False,
            detail={"conflict_id": conflict_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail="Conflict not found.") from exc
    get_access_store().record_event(
        action="conflicts.resolve",
        actor=actor,
        success=True,
        detail={"conflict_id": conflict_id, "kind": resolved.get("kind")},
    )
    return {"ok": True, "conflict": jsonable_encoder(resolved)}


@app.get("/api/indexes")
async def indexes(request: Request) -> Any:
    require_access(request, "reader", "kb:read")
    citadel = get_citadel()
    # Pass the authoritative corpus figures so the dashboard reports the vault's
    # real size rather than whatever has happened since the last deploy.
    snapshot = await get_mesh().snapshot(citadel.config, corpus=await _corpus_health())
    return jsonable_encoder({"indexes": snapshot["indexes"], "stats": snapshot["stats"]})


@app.get("/api/github-sync")
async def github_sync_status(request: Request) -> Any:
    require_access(request, "reader", "sources:read")
    return jsonable_encoder(await get_github_syncer().status())


@app.get("/api/repo-content-sync")
async def repo_content_sync_status(request: Request) -> Any:
    require_access(request, "reader", "sources:read")
    return jsonable_encoder(await get_repo_content_syncer().status())


# Audit actions that mean "this source tried to do its job". The most recent
# unsuccessful one is what the UI shows as the source's failure state.
_SOURCE_ERROR_ACTIONS: dict[str, tuple[str, ...]] = {
    "github": ("learning_agent.run", "github_webhook."),
    "github_repo_content": ("repo_content_sync.",),
    "linear": ("linear_sync.",),
    "obsidian_vault": ("obsidian.sync.", "obsidian_sync"),
}


def _last_source_error(source_type: str) -> tuple[str | None, str | None]:
    """Most recent failed run for a source, as (message, timestamp).

    Until this existed, "failing" was inferred from ``open_conflicts > 0`` plus
    a GitHub-only security-scan flag, so a source that failed for any other
    reason, an expired token, a 500 from an upstream API, a timeout, was
    indistinguishable in the UI from one that was healthy.

    Read from the AccessStore audit trail, which is durable and already records
    every sync attempt with its outcome. Messages are truncated and passed
    through the secret redactor: an upstream error string can carry a URL with a
    token in it, and /api/sources is read by any reader.
    """
    prefixes = _SOURCE_ERROR_ACTIONS.get(source_type)
    if not prefixes:
        return None, None
    try:
        events = get_access_store().snapshot().get("audit_events") or []
    except Exception:
        logger.exception("sources last_error lookup failed")
        return None, None

    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        action = str(event.get("action") or "")
        if not action.startswith(prefixes):
            continue
        # The newest matching attempt decides. A success clears the error,
        # otherwise a source that failed once would look broken forever.
        if event.get("success"):
            return None, None
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        message = detail.get("error") or detail.get("error_type") or detail.get("reason")
        text = str(message) if message else action
        return redact_secrets(text[:300]), event.get("created_at")
    return None, None


@app.get("/api/sources")
async def sources(request: Request, type: str | None = None) -> Any:
    require_access(request, "reader", "sources:read")
    sources_payload: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    if type in {None, "github"}:
        github_status = await get_github_syncer().status()
        github_error, github_error_at = _last_source_error("github")
        # A corrupt state file outranks the audit trail: the source is not
        # "ready", it is refusing to run until an operator intervenes (#148).
        if github_status.get("state_error"):
            github_error = redact_secrets(str(github_status["state_error"])[:300])
        sources_payload.append(
            {
                "id": "github-org",
                "source_type": "github",
                "name": github_status.get("org"),
                "status": "error"
                if github_status.get("state_error")
                else ("tracked" if github_status.get("last_checked_at") else "ready"),
                "url": github_status.get("source_url"),
                "last_checked_at": github_status.get("last_checked_at"),
                "documents": github_status.get("tracked_repositories", 0),
                "open_conflicts": 0,
                "last_error": github_error,
                "last_error_at": github_error_at,
                "metadata": github_status,
            }
        )
        summary["github_repositories"] = github_status.get("tracked_repositories", 0)

    if type in {None, "github_repo_content"}:
        repo_content_status = await get_repo_content_syncer().status()
        repo_content_error, repo_content_error_at = _last_source_error("github_repo_content")
        if repo_content_status.get("state_error"):
            repo_content_error = redact_secrets(str(repo_content_status["state_error"])[:300])
        sources_payload.append(
            {
                "id": "github-repo-content",
                "source_type": "github_repo_content",
                "name": repo_content_status.get("org"),
                "status": "error"
                if repo_content_status.get("state_error")
                else ("tracked" if repo_content_status.get("last_checked_at") else "ready"),
                "last_checked_at": repo_content_status.get("last_checked_at"),
                "documents": repo_content_status.get("tracked_files", 0),
                "open_conflicts": 0,
                "last_error": repo_content_error,
                "last_error_at": repo_content_error_at,
                "metadata": repo_content_status,
            }
        )
        summary["repo_content_files"] = repo_content_status.get("tracked_files", 0)

    if type in {None, "linear"}:
        linear_status = await get_linear_syncer().status()
        linear_error, linear_error_at = _last_source_error("linear")
        if linear_status.get("last_error"):
            linear_error = redact_secrets(str(linear_status["last_error"])[:300])
            linear_error_at = linear_status.get("last_synced_at") or linear_error_at
        if linear_status.get("state_error"):
            linear_error = redact_secrets(str(linear_status["state_error"])[:300])
        sources_payload.append(
            {
                "id": "linear-workspace",
                "source_type": "linear",
                "name": "Linear workspace",
                "status": "error"
                if linear_status.get("state_error")
                else ("tracked" if linear_status.get("last_synced_at") else "ready"),
                "last_checked_at": linear_status.get("last_synced_at"),
                "documents": linear_status.get("issue_count", 0),
                "open_conflicts": 0,
                # Linear is the one syncer that already persists its own failure,
                # so prefer that over the audit trail: it is more specific.
                "last_error": linear_error,
                "last_error_at": linear_error_at,
                "metadata": linear_status,
            }
        )
        summary["linear_issues"] = linear_status.get("issue_count", 0)

    if type in {None, "obsidian_vault"}:
        obsidian_status = get_obsidian_sync().source_status(source_type="obsidian_vault")
        # One vault-wide error: pushes are audited per sync, not per vault, so
        # the same last-failure applies to every vault entry rather than being
        # attributed to one of them arbitrarily.
        obsidian_error, obsidian_error_at = _last_source_error("obsidian_vault")
        for vault in obsidian_status["sources"]:
            vault.setdefault("last_error", obsidian_error)
            vault.setdefault("last_error_at", obsidian_error_at)
        sources_payload.extend(obsidian_status["sources"])
        summary.update(obsidian_status["summary"])

    if type not in {None, "github", "github_repo_content", "linear", "obsidian_vault"}:
        raise HTTPException(status_code=422, detail="Unsupported source type.")

    return {"ok": True, "sources": sources_payload, "summary": summary}


@app.post("/api/github-sync/run")
async def run_github_sync(body: GitHubSyncBody, request: Request) -> Any:
    require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    mesh_state = get_mesh()
    try:
        result = await get_github_syncer().run(force=body.force)
    except Exception as exc:  # pragma: no cover - depends on GitHub and runtime Cognee config.
        logger.error("GitHub sync run failed: %s", exc.__class__.__name__)
        await mesh_state.record_error(citadel.config, operation="github_sync", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await mesh_state.record_github_sync(citadel.config, result)
    return jsonable_encoder(result)


def verify_github_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` over the raw body.

    GitHub signs the request body with HMAC-SHA256 keyed by the shared webhook
    secret and sends ``sha256=<hexdigest>``. A missing/empty/malformed header, an
    empty secret, or any mismatch returns ``False``. The body is UNTRUSTED; this
    runs before any parsing.
    """
    if not secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    provided = signature_header[len(prefix):]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def webhook_identity(dataset: str) -> AccessIdentity:
    """Synthetic service-account identity for auditing accepted webhook triggers."""
    return AccessIdentity(
        role="admin",
        actor_id="github-webhook",
        actor_kind="service_account",
        actor_name="github-webhook",
        source="env",
        default_dataset=dataset,
    )


async def _run_webhook_reingest(syncer: GitHubOrgSyncer) -> None:
    """Background org re-ingest kicked off by an accepted PR-merge webhook.

    Fire-and-forget: the webhook returns 202 immediately (GitHub times out in
    seconds; the full sync is ~26min) and this runs detached. Never raises.
    """
    try:
        await syncer.run(force=True)
    except Exception as exc:  # pragma: no cover - depends on GitHub + Cognee runtime.
        logger.error(
            "GitHub webhook re-ingest failed: %s: %s",
            exc.__class__.__name__,
            exc,
        )
        await get_mesh().record_error(
            get_citadel().config, operation="github_sync", error=str(exc)
        )


@app.post("/api/webhooks/github")
async def github_webhook(request: Request) -> Response:
    """GitHub PR-merge webhook -> non-blocking org re-ingest (ADR-0005 step 3).

    Gated off by default (``github_webhook_enabled`` -> 404 when disabled). The
    raw body is verified against ``X-Hub-Signature-256`` BEFORE parsing and is
    treated as untrusted. An invalid/missing signature -> 401. Only a closed,
    merged ``pull_request`` event triggers work (returns 202); everything else
    (ping, other events, non-merge closes) is acknowledged with 204 and no work.
    """
    config = get_citadel().config
    if not config.github_webhook_enabled:
        raise HTTPException(status_code=404, detail="Not found.")

    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_github_signature(config.github_webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return Response(status_code=204)

    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    pull_request = payload.get("pull_request")
    merged = isinstance(pull_request, dict) and pull_request.get("merged") is True
    if payload.get("action") != "closed" or not merged:
        return Response(status_code=204)

    repository = payload.get("repository")
    repo_full_name = repository.get("full_name") if isinstance(repository, dict) else None
    task = asyncio.create_task(_run_webhook_reingest(get_github_syncer()))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    get_access_store().record_event(
        action="github_webhook.merge",
        actor=webhook_identity(config.github_sync_dataset),
        success=True,
        dataset=config.github_sync_dataset,
        detail={
            "event": event,
            "action": "closed",
            "merged": True,
            "pr_number": pull_request.get("number"),
            "repository": repo_full_name,
            "triggered": "github_sync",
        },
    )
    return Response(status_code=202)


@app.get("/api/linear-sync")
async def linear_sync_status(request: Request) -> Any:
    require_access(request, "reader", "sources:read")
    return jsonable_encoder(await get_linear_syncer().status())


@app.get("/api/linear-sync/issues")
async def linear_sync_issues(
    request: Request,
    scope: str = Query(default="my", pattern="^(my|org)$"),
) -> dict[str, Any]:
    identity = require_access(request, "reader", "kb:read")
    syncer = get_linear_syncer()
    seat_dataset_name = identity.default_dataset if is_seat_dataset(identity.default_dataset) else None
    if scope == "my" and not seat_dataset_name:
        return {"ok": True, "scope": scope, "issues": [], "count": 0}
    issues = syncer.issues_for_scope(scope=scope, seat_dataset_name=seat_dataset_name)
    return {"ok": True, "scope": scope, "issues": issues, "count": len(issues)}


@app.post("/api/linear-sync/run")
async def run_linear_sync(body: LinearSyncBody, request: Request) -> Any:
    require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    mesh_state = get_mesh()
    try:
        result = await get_linear_syncer().run(force=body.force)
    except Exception as exc:  # pragma: no cover - depends on Linear API and runtime Cognee config.
        logger.error("Linear sync run failed: %s", exc.__class__.__name__)
        await mesh_state.record_error(citadel.config, operation="linear_sync", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("reason", "Linear sync unavailable"))
    return jsonable_encoder(result)


@app.post("/api/repo-content-sync/run")
async def run_repo_content_sync(body: RepoContentSyncBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    mesh_state = get_mesh()
    try:
        result = await get_repo_content_syncer().run(force=body.force, dry_run=body.dry_run)
    except Exception as exc:  # pragma: no cover - depends on GitHub and runtime Cognee config.
        logger.error("Repo content sync run failed: %s", exc.__class__.__name__)
        await mesh_state.record_error(
            citadel.config,
            operation="repo_content_sync",
            error=str(exc),
        )
        get_access_store().record_event(
            action="repo_content_sync.run",
            actor=actor,
            success=False,
            detail={
                "force": body.force,
                "dry_run": body.dry_run,
                "error_type": exc.__class__.__name__,
            },
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # ``skipped`` means another pass held the state file and this call did no
    # work. Recording it would stamp the source "synced" with a null
    # checked_at and null counts, which is the bookkeeping-success failure
    # mode, not a sync.
    if not body.dry_run and result.get("enabled") is not False and not result.get("skipped"):
        await mesh_state.record_repo_content_sync(citadel.config, result)
    get_access_store().record_event(
        action="repo_content_sync.run",
        actor=actor,
        success=True,
        detail={
            "force": body.force,
            "dry_run": body.dry_run,
            "files_ingested": result.get("files_ingested"),
            "files_skipped": result.get("files_skipped"),
            "improved": result.get("improved"),
        },
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=citadel.config.repo_content_sync_dataset,
        detail={
            "operation": "repo_content_sync.run",
            "force": body.force,
            "dry_run": body.dry_run,
            "files_ingested": result.get("files_ingested"),
            "files_skipped": result.get("files_skipped"),
        },
    )
    return jsonable_encoder(result)


@app.post("/api/obsidian/vaults")
async def register_obsidian_vault(body: ObsidianVaultBody, request: Request) -> Any:
    actor = require_access(request, "writer", "obsidian:sync:push")
    vault_name = body.vault_name or body.name
    if not vault_name:
        raise HTTPException(status_code=422, detail="Vault name is required.")
    try:
        vault = get_obsidian_sync().register_vault(
            name=vault_name,
            team_id=body.team_id,
            plugin_version=body.plugin_version,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_access_store().record_event(
        action="obsidian.vault.register",
        actor=actor,
        success=True,
        detail={"vault_id": vault.id, "team_id": body.team_id},
    )
    return {"ok": True, "vault": jsonable_encoder(vault)}


def assert_obsidian_vault_owned(identity: AccessIdentity, vault_id: str) -> None:
    """Fail closed unless the caller owns the vault (ADR-0009).

    A vault's ``owner_actor_id`` is recorded at registration but was never read,
    so any token holding the obsidian sync scopes could address another seat's
    vault by id — and ids are disclosed by /api/sources. Mirrors the cognee
    drill-down rule at /api/documents: 404, never 403, so a scoped caller cannot
    use the status code as an existence oracle. Admin/env callers bypass.
    """
    if can_bypass_dataset_allowlist(identity):
        return
    try:
        owner = get_obsidian_sync().vault_owner(vault_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Vault not found.") from exc
    if owner != identity.actor_id:
        raise HTTPException(status_code=404, detail="Vault not found.")


@app.get("/api/obsidian/manifest")
async def obsidian_manifest(request: Request, vault_id: str, cursor: int | None = None) -> Any:
    identity = require_access(request, "reader", "obsidian:sync:pull")
    assert_obsidian_vault_owned(identity, vault_id)
    try:
        manifest = get_obsidian_sync().manifest(vault_id=vault_id, cursor=cursor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Vault not found.") from exc
    return {"ok": True, **jsonable_encoder(manifest)}


@app.post("/api/obsidian/sync/push")
async def push_obsidian_sync(body: ObsidianPushBody, request: Request) -> Any:
    actor = require_access(request, "writer", "obsidian:sync:push")
    # The dataset was already resolved; the vault was not — actor was recorded on
    # the record but never compared to owner_actor_id, so a writer could push
    # revisions into another seat's vault.
    assert_obsidian_vault_owned(actor, body.vault_id)
    citadel = get_citadel()
    mesh_state = get_mesh()
    push_dataset = resolve_write_dataset(actor, body.dataset, citadel.config)
    push_session_id = resolve_session_id(actor, body.session_id)
    push_documents = [
        SyncPushDocument(
            path=document.path,
            content=document.content,
            base_rev=document.base_rev,
            deleted=document.deleted,
            tags=tuple(document.tags),
            dataset=document.dataset,
        )
        for document in body.documents
    ]
    try:
        result = get_obsidian_sync().push(
            vault_id=body.vault_id,
            actor=actor,
            documents=push_documents,
            dataset=push_dataset,
        )
        manifest = get_obsidian_sync().manifest(vault_id=body.vault_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Vault not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    documents_by_path = {}
    for document in body.documents:
        try:
            documents_by_path[normalize_path(document.path)] = document
        except ValueError:
            continue

    learning = get_learning_process()
    ingest_results: list[dict[str, Any]] = []
    written_datasets: set[str] = set()
    for accepted in result["accepted"]:
        if accepted.get("deleted"):
            continue
        source_document = documents_by_path.get(accepted["path"])
        if not source_document:
            continue
        document_tags = seat_safe_tags(
            actor,
            [*body.tags, *source_document.tags, "obsidian", "obsidian_vault"],
        )
        document_targets = resolve_write_targets(
            actor,
            source_document.dataset or body.dataset,
            document_tags,
            citadel.config,
        )
        # Enforce the same byte cap as /ingest and /api/contribute on the per-document
        # obsidian write path (#51): an oversized note is rejected individually so it
        # cannot bloat the index, without failing the rest of the vault sync.
        try:
            enforce_ingest_size(source_document.content)
        except HTTPException as exc:
            ingest_results.append(
                {
                    "document_id": accepted["document_id"],
                    "accepted": False,
                    "reason": exc.detail,
                    "dataset": document_targets[0].dataset,
                    "tags": list(document_tags),
                }
            )
            continue
        try:
            outcome, _ = await execute_learning_writes(
                learning,
                data=source_document.content,
                targets=document_targets,
                tags=document_tags,
                session_id=push_session_id,
                operation="obsidian_sync",
                detect_conflicts=False,
            )
        except SecretContentError as exc:
            get_access_store().record_event(
                action="obsidian_sync",
                actor=actor,
                success=False,
                dataset=document_targets[0].dataset,
                detail={
                    "operation": "obsidian_sync",
                    "blocked": "secret_content",
                    "highest_severity": exc.highest_severity,
                    "finding_count": len(exc.findings),
                },
            )
            raise HTTPException(status_code=422, detail=exc.public_message) from exc
        except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        ingest_result = outcome.ingest
        ingest_results.append(
            {
                "document_id": accepted["document_id"],
                "accepted": ingest_result.accepted,
                "reason": ingest_result.reason,
                "dataset": ingest_result.dataset,
                "tags": list(ingest_result.tags),
            }
        )
        # A promotion writes more than the primary outcome's dataset, so record
        # every target (node + Central) for an accurate audit trail.
        written_datasets.update(target.dataset for target in document_targets)

    # Keep every push conflict visible as a Knowledge Conflict (never silently
    # overwritten) and surface detection in the activity stream.
    conflict_store = get_conflict_store()
    for sync_conflict in result["conflicts"]:
        conflict_record = conflict_store.record(
            obsidian_push_conflict_candidate(
                sync_conflict,
                vault_name=manifest["vault"].get("name"),
            )
        )
        await mesh_state.record_conflict(citadel.config, conflict=conflict_record)

    await mesh_state.record_obsidian_sync(
        citadel.config,
        vault=manifest["vault"],
        result=result,
        dataset=push_dataset,
    )
    get_access_store().record_event(
        action="obsidian.sync.push",
        actor=actor,
        success=True,
        dataset=push_dataset,
        detail={
            "vault_id": body.vault_id,
            "accepted": len(result["accepted"]),
            "skipped": len(result["skipped"]),
            "conflicts": len(result["conflicts"]),
            # push_dataset is the vault's home binding; tag routing can additionally
            # land a note in Central (and a promotion dual-writes node + Central),
            # so record every dataset that actually received content.
            "written_datasets": sorted(written_datasets),
        },
    )
    return {"ok": True, **jsonable_encoder(result), "ingest_results": ingest_results}


@app.get("/api/obsidian/sync/pull")
async def pull_obsidian_sync(request: Request, vault_id: str, cursor: int | None = None) -> Any:
    identity = require_access(request, "reader", "obsidian:sync:pull")
    assert_obsidian_vault_owned(identity, vault_id)
    try:
        result = get_obsidian_sync().pull(vault_id=vault_id, cursor=cursor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Vault not found.") from exc
    return {"ok": True, **jsonable_encoder(result)}


@app.post("/api/obsidian/conflicts/{conflict_id}/resolve")
async def resolve_obsidian_conflict(
    conflict_id: str,
    body: ObsidianConflictResolveBody,
    request: Request,
) -> Any:
    actor = require_access(request, "writer", "obsidian:sync:push")
    obsidian = get_obsidian_sync()
    try:
        conflict_vault = obsidian.conflict_vault_id(conflict_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conflict not found.") from exc
    # actor was passed through for attribution only; resolving a conflict writes
    # a document body, so it needs the same vault ownership gate as push.
    assert_obsidian_vault_owned(actor, conflict_vault or "")
    try:
        result = obsidian.resolve_conflict(
            conflict_id=conflict_id,
            actor=actor,
            resolution=body.resolution,
            body=body.body,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conflict not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_access_store().record_event(
        action="obsidian.conflict.resolve",
        actor=actor,
        success=True,
        detail={"conflict_id": conflict_id, "resolution": body.resolution},
    )
    return {"ok": True, "conflict": jsonable_encoder(result)}


async def load_node_dataset_map(
    *, warn_unavailable: bool = True
) -> dict[str, list[str]] | None:
    """Fetch the gateway's cached node->dataset map ONCE for reuse across many
    ADR-0009 visibility checks in a single request.

    Returns the (possibly empty) mapping, or ``None`` when the map is
    unavailable — the gateway exposes no ``node_dataset_map`` callable, or the
    read raised. ``None`` and ``{}`` both fail closed for scoped callers; they
    are kept distinct only so the empty-map case can be logged separately.
    ``warn_unavailable=False`` silences the warnings on hot read-projection
    paths (e.g. every /search) that degrade quietly rather than 404.
    """
    node_dataset_map = getattr(get_knowledge_mesh().gateway, "node_dataset_map", None)
    if not callable(node_dataset_map):
        if warn_unavailable:
            logger.warning(
                "Document drill-down denied: gateway exposes no node_dataset_map "
                "(fail-closed for scoped callers)"
            )
        return None
    try:
        return await node_dataset_map()
    except Exception:  # noqa: BLE001 - any failure fails closed
        if warn_unavailable:
            logger.warning(
                "Document drill-down denied: node dataset map read failed "
                "(fail-closed for scoped callers)",
                exc_info=True,
            )
        return None


def node_ids_visible_in_map(
    identity: AccessIdentity,
    node_ids: list[str],
    mapping: dict[str, list[str]] | None,
) -> bool:
    """ADR-0009 visibility decision over an ALREADY-fetched node_dataset_map.

    Pure and synchronous so a caller that fetched the (cached) map once can
    reuse it across many ids without an await per id. Fail-closed: a
    missing/empty ``mapping`` ({} or None), an id absent from the map, or an id
    whose datasets are all hidden from ``identity`` all deny. This is exactly
    the rule ``cognee_document_visible`` — and therefore /api/documents —
    applies, so the search drill-down hint cannot drift from the endpoint.
    """
    if not mapping:
        return False
    datasets: set[str] = set()
    for node_id in node_ids:
        datasets.update(mapping.get(str(node_id), []))
    if not datasets:
        return False
    return any(dataset_visible_to(identity, dataset) for dataset in datasets)


async def cognee_document_visible(identity: AccessIdentity, node_ids: list[str]) -> bool:
    """ADR-0009 drill-down isolation for cognee-resolved documents.

    Resolves the document's datasets through the gateway's cached
    ``node_dataset_map``: a direct map hit on the document node id, or (for
    chunk ids) the map entry of the ``is_part_of``-linked document that
    ``get_document`` already resolved into ``node_ids``. Fail-closed for
    scoped callers: a missing/failed/empty map and unmappable nodes all deny —
    the endpoint then serves the same 404 as a nonexistent id.
    """
    mapping = await load_node_dataset_map()
    if mapping is None:
        return False  # already logged: no callable / read failed
    if not mapping:
        # The cached/timed-out lookup degrades to {} on failure (#50) —
        # indistinguishable from an empty store, so scoped callers are denied.
        logger.warning(
            "Document drill-down denied: empty node dataset map "
            "(fail-closed for scoped callers)"
        )
        return False
    return node_ids_visible_in_map(identity, node_ids, mapping)


@app.get("/api/documents/{document_id}")
async def source_document(document_id: str, request: Request) -> Any:
    identity = require_access(request, "reader", "kb:read")
    if document_id.startswith(f"{GITHUB_DOC_ID_PREFIX}:"):
        github_document = github_section_document(document_id, get_citadel().config)
        if github_document is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        return {"ok": True, "document": jsonable_encoder(github_document)}
    try:
        obsidian = get_obsidian_sync()
        document = obsidian.document(document_id)
        # document() is a flat global lookup returning the full body and every
        # revision, so it needs the same ownership gate as manifest/pull. The
        # cognee branch below already 404s foreign documents; this one didn't.
        assert_obsidian_vault_owned(identity, obsidian.document_vault_id(document_id) or "")
    except KeyError:
        # Not a github/obsidian doc — fall back to resolving a native cognee
        # search-hit id against the graph store (#28).
        cognee_document = await get_citadel().get_document(document_id)
        if cognee_document is None:
            raise HTTPException(status_code=404, detail="Document not found.") from None
        # dataset_node_ids is internal plumbing (ADR-0009 scope check), never
        # part of the response — copy so cached fakes/documents stay intact.
        cognee_document = dict(cognee_document)
        owner_node_ids = cognee_document.pop("dataset_node_ids", None) or [document_id]
        if not can_bypass_dataset_allowlist(identity) and not await cognee_document_visible(
            identity, owner_node_ids
        ):
            # Same status, detail, and shape as a nonexistent id: a scoped
            # caller must not learn whether a foreign document exists.
            raise HTTPException(status_code=404, detail="Document not found.") from None
        return {"ok": True, "document": jsonable_encoder(cognee_document)}
    return {"ok": True, "document": jsonable_encoder(document)}


@app.get("/api/learning-agent")
async def learning_agent_status(request: Request) -> Any:
    require_access(request, "reader", "sources:read")
    return jsonable_encoder(await get_learning_agent().status())


@app.get("/api/backup-mirror")
async def backup_mirror_status(request: Request) -> Any:
    require_access(request, "admin", "sources:sync")
    return jsonable_encoder(get_backup_mirror().status())


@app.post("/api/backup-mirror/run")
async def run_backup_mirror(body: BackupMirrorRunBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    try:
        result = get_backup_mirror().run(dry_run=body.dry_run)
    except BackupMirrorDisabled as exc:
        get_access_store().record_event(
            action="backup_mirror.run",
            actor=actor,
            success=False,
            detail={"dry_run": body.dry_run, "reason": "disabled"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupMirrorPublishError as exc:
        logger.error("Backup mirror publish failed: %s", exc.__class__.__name__)
        get_access_store().record_event(
            action="backup_mirror.run",
            actor=actor,
            success=False,
            detail={"dry_run": body.dry_run, "reason": "publish_failed"},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    manifest = result.get("manifest") if isinstance(result, dict) else {}
    summary = manifest.get("summary") if isinstance(manifest, dict) else {}
    get_access_store().record_event(
        action="backup_mirror.run",
        actor=actor,
        success=True,
        detail={
            "dry_run": body.dry_run,
            "written": result.get("written"),
            "published": result.get("published"),
            "snapshot_id": result.get("snapshot_id"),
            "tracked_files": summary.get("tracked_files"),
            "available_files": summary.get("available_files"),
            "missing_files": summary.get("missing_files"),
        },
    )
    return jsonable_encoder(result)


@app.get("/api/promote")
async def promotion_status(request: Request) -> Any:
    require_access(request, "admin", "sources:sync")
    return jsonable_encoder(get_promotion_engine().status())


@app.post("/api/promote/run")
async def run_promotion(body: PromoteRunBody, request: Request) -> Any:
    # A seat previewing its own promotable candidates is the intended flow and
    # stays at writer (can_run_promotion below also refuses another seat's
    # dataset). But dry_run comes from the request body, and with it false this
    # same call writes durably into the shared Central dataset under a
    # synthetic admin identity, with no further approval on the auto-promote
    # path. A caller may not grant itself that by flipping a flag, so the write
    # half now requires what GET /api/promote already requires. The scheduled
    # promotion path is unaffected.
    actor = require_access(request, "writer", "kb:ingest")
    if not body.dry_run:
        actor = require_access(request, "admin", "sources:sync")
    if not is_seat_dataset(body.dataset):
        raise HTTPException(
            status_code=400,
            detail="dataset must be a seat node (seat:<slug>) to promote from.",
        )
    if not can_run_promotion(actor, body.dataset):
        raise HTTPException(
            status_code=403,
            detail="Not allowed to run promotion for this seat dataset.",
        )
    try:
        result = await get_promotion_engine().run(
            body.dataset,
            dry_run=body.dry_run,
            max_items=body.max_items,
        )
    except SecretContentError as exc:
        get_access_store().record_event(
            action="promotion.run",
            actor=actor,
            success=False,
            dataset=body.dataset,
            detail={
                "dry_run": body.dry_run,
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
            },
        )
        raise HTTPException(status_code=422, detail=exc.public_message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - depends on Cognee runtime.
        logger.error("Promotion run failed: %s", exc.__class__.__name__)
        get_access_store().record_event(
            action="promotion.run",
            actor=actor,
            success=False,
            dataset=body.dataset,
            detail={"dry_run": body.dry_run, "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    get_access_store().record_event(
        action="promotion.run",
        actor=actor,
        success=True,
        dataset=body.dataset,
        detail={
            "dry_run": body.dry_run,
            "enabled": result.get("enabled"),
            "candidates": result.get("candidates"),
            "proposed": result.get("proposed"),
            "promoted": result.get("promoted"),
            "pending_approval": result.get("pending_approval"),
            "queued": result.get("queued"),
            "max_items": result.get("max_items"),
        },
    )
    return jsonable_encoder(result)


@app.get("/api/promotion/pending")
async def list_promotion_pending(
    request: Request,
    status: str = PENDING_STATUS,
) -> dict[str, Any]:
    identity = require_access(request, "reader", "kb:read")
    seat_slug = promotion_pending_filter_seat(identity)
    if status not in {PENDING_STATUS, REJECTED_STATUS, APPROVED_STATUS}:
        raise HTTPException(status_code=422, detail=f"Unsupported status filter: {status}")
    items = get_access_store().list_promotion_pending(seat_slug=seat_slug, status=status)
    return {
        "ok": True,
        "status": status,
        "items": [redact_pending_item(item.to_dict()) for item in items],
        "count": len(items),
    }


@app.post("/api/promotion/pending/{item_id}/approve")
async def approve_promotion_pending(
    item_id: str,
    request: Request,
    body: PromotionDecisionBody | None = None,
) -> dict[str, Any]:
    # Approving commits a candidate into Central, so it requires admin — a
    # seat-writer is rejected with 403 BEFORE the item lookup, closing the gap
    # where a seat could self-promote its own pending item into Central (#48).
    identity = require_access(request, "admin", "sources:sync")
    item = get_access_store().get_promotion_pending(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Promotion item not found: {item_id}")
    allowed, delegate = can_decide_promotion_item(identity, item.seat_slug)
    if not allowed:
        raise HTTPException(status_code=403, detail="Not allowed to approve this promotion item.")
    try:
        result = await get_promotion_engine().approve_pending(
            item_id,
            identity,
            delegate=delegate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body and body.note:
        get_access_store().record_event(
            action="promotion.approve.note",
            actor=identity,
            success=True,
            dataset=item.seat_dataset,
            detail={"item_id": item_id, "note": body.note[:400]},
        )
    return jsonable_encoder(result)


@app.post("/api/promotion/pending/{item_id}/reject")
async def reject_promotion_pending(
    item_id: str,
    request: Request,
    body: PromotionDecisionBody | None = None,
) -> dict[str, Any]:
    # Symmetric with approve: deciding a Central-bound promotion is admin-only, so
    # a seat-writer is 403'd before the item lookup (#48).
    identity = require_access(request, "admin", "sources:sync")
    item = get_access_store().get_promotion_pending(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Promotion item not found: {item_id}")
    allowed, delegate = can_decide_promotion_item(identity, item.seat_slug)
    if not allowed:
        raise HTTPException(status_code=403, detail="Not allowed to reject this promotion item.")
    try:
        result = await get_promotion_engine().reject_pending(
            item_id,
            identity,
            delegate=delegate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body and body.note:
        get_access_store().record_event(
            action="promotion.reject.note",
            actor=identity,
            success=True,
            dataset=item.seat_dataset,
            detail={"item_id": item_id, "note": body.note[:400]},
        )
    return jsonable_encoder(result)


@app.post("/api/learning-agent/run")
async def run_learning_agent(body: LearningAgentRunBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    mesh_state = get_mesh()
    try:
        result = await get_learning_agent().run(
            force=body.force,
            dry_run=body.dry_run,
            post_to_chat=body.post_to_chat,
            include_digest_preview=body.include_digest_preview,
        )
    except Exception as exc:  # pragma: no cover - depends on external sources and Cognee config.
        logger.error("Learning agent run failed: %s", exc.__class__.__name__)
        await mesh_state.record_error(citadel.config, operation="learning_agent", error=str(exc))
        get_access_store().record_event(
            action="learning_agent.run",
            actor=actor,
            success=False,
            detail={
                "force": body.force,
                "dry_run": body.dry_run,
                "post_to_chat": body.post_to_chat,
                "error": str(exc),
            },
        )
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=get_citadel().config.github_sync_dataset,
            detail={
                "operation": "learning_agent.run",
                "force": body.force,
                "dry_run": body.dry_run,
                "post_to_chat": body.post_to_chat,
                "error_type": exc.__class__.__name__,
            },
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    github_result = result.get("sources", {}).get("github")
    if isinstance(github_result, dict):
        await mesh_state.record_github_sync(citadel.config, github_result)
    repo_content_result = result.get("sources", {}).get("repo_content")
    if (
        isinstance(repo_content_result, dict)
        and repo_content_result.get("enabled") is not False
        and not repo_content_result.get("skipped")
        and not body.dry_run
    ):
        await mesh_state.record_repo_content_sync(citadel.config, repo_content_result)
    get_access_store().record_event(
        action="learning_agent.run",
        actor=actor,
        success=True,
        detail={
            "force": body.force,
            "dry_run": body.dry_run,
            "post_to_chat": body.post_to_chat,
            "ingested": result.get("ingested"),
            "improved": result.get("improved"),
            "files_ingested": (
                repo_content_result.get("files_ingested") if isinstance(repo_content_result, dict) else None
            ),
            "digest_meaningful": (result.get("organization_digest") or {}).get("meaningful"),
            "google_chat_sent": (
                (result.get("notifications") or {}).get("google_chat") or {}
            ).get("sent"),
        },
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=citadel.config.github_sync_dataset,
        detail={
            "operation": "learning_agent.run",
            "force": body.force,
            "dry_run": body.dry_run,
            "post_to_chat": body.post_to_chat,
            "ingested": result.get("ingested"),
            "improved": result.get("improved"),
            "digest_meaningful": (result.get("organization_digest") or {}).get("meaningful"),
            "google_chat_sent": (
                (result.get("notifications") or {}).get("google_chat") or {}
            ).get("sent"),
        },
    )
    return jsonable_encoder(result)


@app.post("/api/cognify/run")
async def run_cognify(body: CognifyRunBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    dataset = body.dataset or citadel.config.default_dataset
    try:
        result = await citadel.cognify_dataset(dataset=dataset, verify=body.verify, force=body.force)
    except Exception as exc:  # pragma: no cover - depends on Cognee config.
        logger.error("Cognify run failed: %s", exc.__class__.__name__)
        get_access_store().record_event(
            action="cognify.run",
            actor=actor,
            success=False,
            dataset=dataset,
            detail={"verify": body.verify, "force": body.force, "error": str(exc)},
        )
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=dataset,
            detail={
                "operation": "cognify.run",
                "verify": body.verify,
                "force": body.force,
                "error_type": exc.__class__.__name__,
            },
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    verification = result.get("verification") or {}
    get_access_store().record_event(
        action="cognify.run",
        actor=actor,
        success=True,
        dataset=dataset,
        detail={
            "verify": body.verify,
            "force": body.force,
            "graph_grew": result.get("graph_grew"),
            "graph_before": result.get("graph_before"),
            "graph_after": result.get("graph_after"),
            "verification_ok": verification.get("ok") if body.verify else None,
        },
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=dataset,
        detail={
            "operation": "cognify.run",
            "verify": body.verify,
            "force": body.force,
            "graph_grew": result.get("graph_grew"),
            "verification_ok": verification.get("ok") if body.verify else None,
        },
    )
    return jsonable_encoder(result)


@app.post("/api/admin/graph/cleanup")
async def cleanup_graph(body: GraphCleanupBody, request: Request) -> Any:
    """Purge legacy [DataItem]/marker/session-cache garbage from the graph (#15).

    Admin-only and dry-run-by-default: the dry run lists every candidate id +
    preview so a human verifies before POSTing {"dry_run": false} to delete.
    """
    actor = require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    try:
        result = await citadel.cleanup_legacy_nodes(dry_run=body.dry_run)
    except Exception as exc:  # pragma: no cover - depends on Cognee config.
        logger.error("Graph cleanup failed: %s", exc.__class__.__name__)
        get_access_store().record_event(
            action="graph.cleanup",
            actor=actor,
            success=False,
            detail={"dry_run": body.dry_run, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    get_access_store().record_event(
        action="graph.cleanup",
        actor=actor,
        success=True,
        detail={
            "dry_run": body.dry_run,
            "counts_by_kind": result.get("counts_by_kind"),
            "deleted": result.get("deleted"),
        },
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        detail={
            "operation": "graph.cleanup",
            "dry_run": body.dry_run,
            "deleted": result.get("deleted"),
        },
    )
    return jsonable_encoder({"ok": True, **result})


@app.post("/api/learning-agent/google-chat/test")
async def test_learning_agent_google_chat(body: GoogleChatTestBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    result = await get_learning_agent().test_google_chat_delivery(message=body.message)
    detail = {
        "sent": result.get("sent"),
        "reason": result.get("reason"),
        "status_category": result.get("status_category"),
        "status_code": result.get("status_code"),
        "message_name": result.get("message_name"),
        "thread_name": result.get("thread_name"),
    }
    get_access_store().record_event(
        action="learning_agent.google_chat_test",
        actor=actor,
        success=bool(result.get("sent")),
        detail={key: value for key, value in detail.items() if value is not None},
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=bool(result.get("sent")),
        dataset=get_citadel().config.github_sync_dataset,
        detail={
            "operation": "learning_agent.google_chat_test",
            **{key: value for key, value in detail.items() if value is not None},
        },
    )
    return jsonable_encoder(result)


@app.post("/api/learning-agent/gateways/{gateway_name}/test")
async def test_learning_agent_gateway(
    gateway_name: str,
    body: GoogleChatTestBody,
    request: Request,
) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    if not re.fullmatch(r"[a-z0-9_-]{1,80}", gateway_name):
        raise HTTPException(status_code=400, detail="Gateway names may contain a-z, 0-9, _, and -.")
    result = await get_learning_agent().test_gateway_delivery(gateway_name, message=body.message)
    detail = {
        "gateway": gateway_name,
        "sent": result.get("sent"),
        "reason": result.get("reason"),
        "status_category": result.get("status_category"),
        "status_code": result.get("status_code"),
        "message_name": result.get("message_name"),
        "thread_name": result.get("thread_name"),
    }
    get_access_store().record_event(
        action="learning_agent.gateway_test",
        actor=actor,
        success=bool(result.get("sent")),
        detail={key: value for key, value in detail.items() if value is not None},
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=bool(result.get("sent")),
        dataset=get_citadel().config.github_sync_dataset,
        detail={
            "operation": "learning_agent.gateway_test",
            **{key: value for key, value in detail.items() if value is not None},
        },
    )
    return jsonable_encoder(result)


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    identity = require_access(request, "reader", "kb:read")
    mesh_state = get_mesh()
    queue = mesh_state.subscribe()
    # ADR-0009: the SSE stream serves the same content-leaking projection as
    # /api/mesh, so scope the initial snapshot and every live event to the
    # caller (bypass callers see all). Cache visibility across events.
    bypass = can_bypass_dataset_allowlist(identity)
    visible_cache: dict[str, bool] = {}

    async def stream() -> Any:
        try:
            snapshot = await mesh_state.snapshot(get_citadel().config)
            yield sse("snapshot", scope_mesh_snapshot(snapshot, identity))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if not bypass and not _mesh_dataset_visible(
                    identity, (event.get("details") or {}).get("dataset"), visible_cache
                ):
                    continue
                yield sse("mesh-event", event)
        finally:
            mesh_state.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/ingest")
async def ingest(body: IngestBody, request: Request) -> Any:
    actor = require_access(request, "writer", "kb:ingest")
    enforce_ingest_size(body.data)
    citadel = get_citadel()
    learning = get_learning_process()
    write_targets = resolve_write_targets(actor, body.dataset, body.tags, citadel.config)
    session_id = resolve_session_id(actor, body.session_id)
    primary_dataset = write_targets[0].dataset
    try:
        outcome, _ = await execute_learning_writes(
            learning,
            data=body.data,
            targets=write_targets,
            tags=body.tags,
            session_id=session_id,
            operation="ingest",
        )
    except SecretContentError as exc:
        get_access_store().record_event(
            action="ingest",
            actor=actor,
            success=False,
            dataset=primary_dataset,
            detail={
                "operation": "ingest",
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
                "finding_count": len(exc.findings),
            },
        )
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=primary_dataset,
            detail={
                "operation": "ingest",
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
            },
        )
        raise HTTPException(status_code=422, detail=exc.public_message) from exc
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=primary_dataset,
            detail={"operation": "ingest", "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = outcome.ingest
    # Durable HTTP ingest audit (MCP path already records via record_mcp_audit).
    # Only accepted writes count as capture proof for Seat home.
    if result.accepted and not mcp_tool_name(request):
        get_access_store().record_event(
            action="ingest",
            actor=actor,
            success=True,
            dataset=outcome.dataset,
            detail={
                "operation": "ingest",
                "accepted": True,
                "reason": result.reason,
                "data_bytes": len(body.data.encode("utf-8")),
                "tag_count": len(body.tags),
                "write_targets": [target.dataset for target in write_targets],
                "scope_override": scope_override_active(
                    actor, [target.dataset for target in write_targets]
                ),
            },
        )
    record_mcp_audit(
        request,
        actor=actor,
        success=bool(result.accepted),
        dataset=outcome.dataset,
        detail={
            "operation": "ingest",
            "accepted": result.accepted,
            "reason": result.reason,
            "data_bytes": len(body.data.encode("utf-8")),
            "tag_count": len(body.tags),
            "write_targets": [target.dataset for target in write_targets],
            "scope_override": scope_override_active(
                actor, [target.dataset for target in write_targets]
            ),
        },
    )
    payload = jsonable_encoder(result)
    # Inline cognify (opt-in) so the just-written data is immediately searchable.
    # The write already succeeded; a cognify failure must NOT fail the ingest.
    if body.cognify and result.accepted:
        try:
            await citadel.cognify_dataset(dataset=outcome.dataset)
            payload["cognified"] = True
        except Exception as exc:  # pragma: no cover - depends on runtime Cognee state
            logger.error("inline cognify after ingest failed: %s", exc.__class__.__name__)
            payload["cognified"] = False
    return payload


@app.post("/api/share-session")
async def share_session(body: ShareSessionBody, request: Request) -> Any:
    """Volunteer a Shared Session Trace (explicit MCP share only in v1)."""
    actor = require_access(request, "writer", "kb:ingest")
    enforce_share_capture_root(actor, body.cwd)
    enforce_ingest_size(body.data)
    citadel = get_citadel()
    learning = get_learning_process()
    write_targets = resolve_write_targets_for_share(actor, citadel.config)
    if learning.config.content_scan_enabled:
        try:
            scan_share_payload_or_raise(
                body.data,
                block_severity=learning.config.content_scan_block_severity,
            )
        except SecretContentError as exc:
            get_access_store().record_event(
                action="share_session",
                actor=actor,
                success=False,
                dataset=SESSION_TRACES_DATASET,
                detail={
                    "operation": "share_session",
                    "blocked": "secret_content",
                    "highest_severity": exc.highest_severity,
                },
            )
            raise HTTPException(status_code=422, detail=exc.public_message) from exc
    data = force_shared_trace_author_seat(body.data, actor.seat_slug)
    data = enrich_shared_trace(data, has_tool_errors=body.has_tool_errors)
    data = force_shared_trace_author_seat(data, actor.seat_slug)
    tags = share_session_tags_from_body(actor.seat_slug, body)
    session_id = resolve_session_id(actor, None)
    try:
        outcome, all_outcomes = await execute_learning_writes(
            learning,
            data=data,
            targets=write_targets,
            tags=tags,
            session_id=session_id,
            operation="share_session",
            detect_conflicts=False,
            defer_cognify=True,
        )
    except SecretContentError as exc:
        get_access_store().record_event(
            action="share_session",
            actor=actor,
            success=False,
            dataset=SESSION_TRACES_DATASET,
            detail={
                "operation": "share_session",
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
            },
        )
        raise HTTPException(status_code=422, detail=exc.public_message) from exc
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=SESSION_TRACES_DATASET,
            detail={"operation": "share_session", "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    failed_targets = [
        target.dataset
        for target, item in zip(write_targets, all_outcomes, strict=True)
        if not item.ingest.accepted
    ]
    if failed_targets:
        all_outcomes = await retry_failed_learning_writes(
            learning,
            data=data,
            targets=write_targets,
            outcomes=all_outcomes,
            tags=tags,
            session_id=session_id,
            operation="share_session",
            detect_conflicts=False,
            defer_cognify=True,
        )
        failed_targets = [
            target.dataset
            for target, item in zip(write_targets, all_outcomes, strict=True)
            if not item.ingest.accepted
        ]
    if failed_targets:
        failure_detail = {
            "operation": "share_session",
            "error_type": "partial_write_failure",
            "failed_targets": failed_targets,
            "write_targets": [target.dataset for target in write_targets],
            "retried": True,
        }
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=SESSION_TRACES_DATASET,
            detail=failure_detail,
        )
        get_access_store().record_event(
            action="share_session",
            actor=actor,
            success=False,
            dataset=SESSION_TRACES_DATASET,
            detail={
                **failure_detail,
                "author_seat": actor.seat_slug,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Shared Session Trace could not be fully written.",
                **failure_detail,
            },
        )

    cognify_datasets = [
        target.dataset
        for target, item in zip(write_targets, all_outcomes, strict=True)
        if item.ingest.accepted
    ]
    if cognify_datasets:
        citadel.cognee.schedule_cognify(list(dict.fromkeys(cognify_datasets)))

    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=SESSION_TRACES_DATASET,
        detail={
            "operation": "share_session",
            "accepted": outcome.ingest.accepted,
            "write_targets": [target.dataset for target in write_targets],
            "has_tool_errors": body.has_tool_errors,
        },
    )
    get_access_store().record_event(
        action="share_session",
        actor=actor,
        success=True,
        dataset=SESSION_TRACES_DATASET,
        detail={
            "write_targets": [target.dataset for target in write_targets],
            "author_seat": actor.seat_slug,
        },
    )
    return jsonable_encoder(
        {
            "ok": True,
            "accepted": outcome.ingest.accepted,
            "dataset": SESSION_TRACES_DATASET,
            "write_targets": [target.dataset for target in write_targets],
            "cognify": "deferred",
            "message": "Shared Session Trace accepted; searchable after coalesced cognify.",
        }
    )


def share_session_tags_from_body(seat_slug: str, body: ShareSessionBody) -> list[str]:
    tags = ["shared-session-trace", f"author:{seat_slug}"]
    if body.repo and body.repo.strip():
        tags.append(body.repo.strip())
    if body.branch and body.branch.strip():
        tags.append(body.branch.strip())
    return tags


@app.get("/api/contributions/recent")
async def recent_contributions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    mine: bool = Query(default=False),
) -> Any:
    actor = require_access(request, "reader", "kb:read")
    actor_id = actor.actor_id if mine else None
    events = get_access_store().recent_audit_events(
        action="contribute",
        actor_id=actor_id,
        limit=limit,
    )
    return {
        "ok": True,
        "contributions": events,
        "filter": {"mine": mine, "limit": limit},
    }


@app.post("/api/contribute")
async def contribute(body: ContributeBody, request: Request) -> Any:
    """Simple write path for teammates and agents.

    Routes through the Learning Process (with LLM enrichment when enabled)
    and keeps conflict detection on, so a Vault Contribution behaves exactly
    like any other accepted Source Material.
    """
    actor = require_access(request, "writer", "kb:ingest")
    enforce_ingest_size(body.content)
    guard_seat_write_policy(
        actor,
        operation="contribute",
        dataset=body.dataset,
        tags=body.tags,
    )
    citadel = get_citadel()
    learning = get_learning_process()
    contribution_tags = _contribution_tags(body.tags, actor)
    write_targets = resolve_write_targets(actor, body.dataset, contribution_tags, citadel.config)
    parts = [f"# {body.title.strip()}", "", body.content.strip()]
    if body.source_url and body.source_url.strip():
        parts.extend(["", f"Source: {body.source_url.strip()}"])
    if actor.actor_name:
        parts.extend(["", f"Author: {actor.actor_name.strip()}"])
    data = "\n".join(parts)
    try:
        outcome, _ = await execute_learning_writes(
            learning,
            data=data,
            targets=write_targets,
            tags=contribution_tags,
            session_id=None,
            operation="contribute",
            run_improve=citadel.config.contribute_run_improve,
        )
    except SecretContentError as exc:
        get_access_store().record_event(
            action="contribute",
            actor=actor,
            success=False,
            dataset=write_targets[0].dataset,
            detail={
                "operation": "contribute",
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
                "finding_count": len(exc.findings),
            },
        )
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=write_targets[0].dataset,
            detail={
                "operation": "contribute",
                "blocked": "secret_content",
                "highest_severity": exc.highest_severity,
            },
        )
        raise HTTPException(status_code=422, detail=exc.public_message) from exc
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        get_access_store().record_event(
            action="contribute",
            actor=actor,
            success=False,
            dataset=write_targets[0].dataset,
            detail={"error_type": exc.__class__.__name__},
        )
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=write_targets[0].dataset,
            detail={"operation": "contribute", "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    accepted = any(result.accepted for result in outcome.all_ingests)
    get_access_store().record_event(
        action="contribute",
        actor=actor,
        success=accepted,
        dataset=outcome.dataset,
        detail={
            "accepted": accepted,
            "chunks": outcome.accepted_chunks,
            "conflict": bool(outcome.conflict),
            "reason": outcome.ingest.reason,
            "title": body.title.strip(),
            "tags": contribution_tags,
            "tag_count": len(contribution_tags),
            "content_bytes": len(body.content.encode("utf-8")),
        },
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=accepted,
        dataset=outcome.dataset,
        detail={
            "operation": "contribute",
            "accepted": accepted,
            "chunks": outcome.accepted_chunks,
            "conflict": bool(outcome.conflict),
            "scope_override": scope_override_active(
                actor, [target.dataset for target in write_targets]
            ),
        },
    )
    return jsonable_encoder(
        {
            "ok": True,
            "accepted": accepted,
            "chunks": outcome.accepted_chunks,
            "conflict": outcome.conflict,
            "dataset": outcome.dataset,
            "reason": outcome.ingest.reason,
            "enrichment": outcome.enrichment,
        }
    )


def flat_knowledge_result(result: Any) -> dict[str, Any]:
    """Flatten one search hit into the agent-friendly knowledge shape."""
    if not isinstance(result, dict):
        return {"text": str(result), "source": None}
    provenance = result_provenance(result)
    text = first_string(
        result.get("text"),
        result.get("content"),
        result.get("chunk"),
        result.get("body"),
        result.get("summary"),
        result.get("title"),
    )
    if not text:
        text = json.dumps(
            {key: value for key, value in result.items() if key != "_citadel"},
            sort_keys=True,
            default=str,
        )[:500]
    payload: dict[str, Any] = {
        "text": text,
        "source": provenance.get("source_url")
        or provenance.get("source")
        or provenance.get("path"),
    }
    score = result.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        payload["score"] = score
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    tags = result.get("tags") or metadata.get("citadel_tags") or metadata.get("tags")
    if isinstance(tags, list):
        payload["tags"] = [str(tag) for tag in tags if isinstance(tag, (str, int, float))]
    return payload


@app.get("/api/knowledge")
async def knowledge(
    request: Request,
    response: Response,
    q: str,
    limit: int = 10,
    dataset: str | None = None,
) -> Any:
    """Thin reader alias over /search with a flat, agent-friendly shape."""
    identity = require_access(request, "reader", "kb:search")
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query q must not be empty.")
    if not 1 <= limit <= 50:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 50.")
    citadel = get_citadel()
    mesh_state = get_mesh()
    search_datasets = resolve_search_datasets(identity, dataset, citadel.config)
    search_sessions = resolve_search_sessions(identity, None, search_datasets)
    max_concurrency = citadel.config.search_max_concurrency
    timed_out = False
    started = time.perf_counter()
    with _SearchSlot(max_concurrency) as slot:  # 429 here if at capacity
        response.headers["X-RateLimit-Limit"] = str(max_concurrency)
        response.headers["X-RateLimit-Remaining"] = str(slot.remaining)
        try:
            merged, timed_out = await _search_within_budget(
                citadel,
                query=query,
                datasets=search_datasets,
                sessions=search_sessions,
                top_k=limit,
            )
        except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
            await mesh_state.record_error(citadel.config, operation="search", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    latency_ms = (time.perf_counter() - started) * 1000.0
    for search_dataset, _ in merged:
        await mesh_state.record_search(
            citadel.config,
            query=query,
            dataset=search_dataset,
            result_count=sum(1 for ds, _ in merged if ds == search_dataset),
        )
    primary_dataset = search_datasets[0]
    flat_results = [flat_knowledge_result(result) for _, result in merged]
    telemetry = await capture_search_feedback(
        mesh_state=mesh_state,
        config=citadel.config,
        request=request,
        actor=identity,
        query=query,
        results=flat_results,
        search_datasets=search_datasets,
        primary_dataset=primary_dataset,
        top_k=limit,
        latency_ms=latency_ms,
        timed_out=timed_out,
        filters={"limit": limit, "dataset": dataset} if dataset else {"limit": limit},
    )
    payload: dict[str, Any] = {
        "ok": True,
        "query": query,
        "dataset": primary_dataset,
        "datasets": search_datasets if len(search_datasets) > 1 else None,
        "results": flat_results,
    }
    if telemetry:
        payload["search_id"] = telemetry.get("search_id")
        payload["feedback"] = {
            "automatic": True,
            "kind": "search_telemetry",
            "explicit_tool": "citadel_record_feedback",
        }
    return jsonable_encoder(payload)


@app.post("/api/learning-agent/optimize")
async def optimize_learning_agent(body: OptimizeBody, request: Request) -> Any:
    """Bounded self-improvement pass. Admin only; never deletes knowledge."""
    actor = require_access(request, "admin", "sources:sync")
    citadel = get_citadel()
    mesh_state = get_mesh()
    optimizer = SelfImprovement(
        citadel,
        mesh=mesh_state,
        learning=get_learning_process(),
        access_store=get_access_store(),
    )
    try:
        result = await optimizer.run(
            dry_run=body.dry_run,
            max_items=body.max_items,
            actor=actor,
        )
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        await mesh_state.record_error(citadel.config, operation="self_improve", error=str(exc))
        get_access_store().record_event(
            action="learning_agent.optimize",
            actor=actor,
            success=False,
            detail={"dry_run": body.dry_run, "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=result.get("dataset"),
        detail={
            "operation": "learning_agent.optimize",
            "reviewed": result.get("reviewed"),
            "optimized": result.get("optimized"),
            "dry_run": body.dry_run,
        },
    )
    return jsonable_encoder(result)


@app.post("/search")
async def search(body: SearchBody, request: Request, response: Response) -> Any:
    actor = require_access(request, "reader", "kb:search")
    citadel = get_citadel()
    mesh_state = get_mesh()
    search_datasets = resolve_search_datasets(actor, body.dataset, citadel.config)
    search_sessions = resolve_search_sessions(actor, body.session_id, search_datasets)
    filter_kw = body.filter_kwargs()
    filters_active = any(filter_kw.values())
    # Filters run AFTER retrieval, so on a fixed candidate page they can only
    # shrink it: top_k=10 with a repo filter used to return 4 hits. Over-fetch
    # candidates when filters are active and trim back to top_k after
    # filtering; 100 is SearchBody's own top_k ceiling, so a filtered call can
    # never fetch more than an unfiltered one is allowed to ask for.
    fetch_k = min(max(body.top_k * 3, body.top_k + 10), 100) if filters_active else body.top_k
    limit = citadel.config.search_max_concurrency
    timed_out = False
    started = time.perf_counter()
    with _SearchSlot(limit) as slot:  # 429 here if at capacity
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(slot.remaining)
        try:
            merged, timed_out = await _search_within_budget(
                citadel,
                query=body.query,
                datasets=search_datasets,
                sessions=search_sessions,
                top_k=fetch_k,
            )
        except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
            await mesh_state.record_error(citadel.config, operation="search", error=str(exc))
            record_mcp_audit(
                request,
                actor=actor,
                success=False,
                dataset=search_datasets[0],
                detail={
                    "operation": "search",
                    "query_sha256": hashlib.sha256(body.query.encode("utf-8")).hexdigest(),
                    "query_length": len(body.query),
                    "error_type": exc.__class__.__name__,
                },
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    if timed_out:
        await mesh_state.record_error(
            citadel.config, operation="search", error="search budget exceeded"
        )

    # Shaping order: envelope -> rank -> filter -> trim -> drilldown. The
    # drill-down hints are resolved LAST, over the final page only, because
    # each scoped resolution costs a get_document call and over-fetching for
    # filters would otherwise triple that cost for hits the caller never sees.
    bypass_drilldown = can_bypass_dataset_allowlist(actor)
    normalized = [
        with_result_metadata(
            result,
            index,
            dataset,
            # Scoped callers: deny drill-down for now and patch the survivors
            # up after the trim. The safe failure direction — a missed patch
            # under-promises (a 200 the hint did not advertise), never an
            # ADR-0009 404 the hint promised was a document.
            drilldown_predicate=None if bypass_drilldown else (lambda _result_id: False),
            query=body.query,
        )
        for index, (dataset, result) in enumerate(merged)
    ]
    cleaned_mode = body.cleaned_mode()
    docs_mode = is_docs_mode_query(body.query, mode=cleaned_mode)
    if docs_mode or is_spec_mode_query(body.query):
        normalized = apply_query_ranking(normalized, body.query, mode=cleaned_mode)
    candidates_fetched = len(normalized)
    if filters_active:
        dict_hits = [item for item in normalized if isinstance(item, dict)]
        other = [item for item in normalized if not isinstance(item, dict)]
        normalized = filter_hits(dict_hits, **filter_kw) + other
    candidates_matched = len(normalized)
    if len(normalized) > body.top_k:
        normalized = normalized[: body.top_k]
    # Refresh rank numbers after re-order / filter / trim so agents see
    # consistent ordering.
    for index, item in enumerate(normalized):
        if isinstance(item, dict) and isinstance(item.get("_citadel"), dict):
            item["_citadel"]["rank"] = index + 1

    # Honest drill-down hint (ADR-0009): the document_drilldown_available flag
    # (and the document_endpoint URL) must be TRUE only when /api/documents would
    # actually return 200 for THIS caller. Bypass callers reach any resolvable id,
    # so they skip the check. For a scoped caller we resolve each id to the SAME
    # owner ids the /api/documents cognee branch would gate on —
    # resolve_document_owner_ids, the seed-only read that recovers
    # ``dataset_node_ids`` (a CHUNKS hit's datasets live on its is_part_of parent
    # document, NOT the chunk node itself, so a raw-id map lookup would always
    # miss) WITHOUT assembling the document body get_document would build and
    # this boolean would discard — then the SAME visibility rule over the SAME
    # once-fetched node_dataset_map, so the hint can never drift from the
    # endpoint it points at. Resolved once per UNIQUE drillable id on the final
    # page; the shared map is fetched once and reused across ids.
    drilldown_ms: float | None = None
    if not bypass_drilldown:
        drilldown_started = time.perf_counter()
        drilldown_map = await load_node_dataset_map(warn_unavailable=False)
        # This pass runs OUTSIDE _search_within_budget and the _SearchSlot, so
        # nothing else bounds it: give it its own deadline. On expiry the
        # remaining ids deny — the safe under-promise, never a hung request.
        drilldown_deadline = drilldown_started + citadel.config.search_timeout_seconds

        async def _resolve_drilldown(result_id: str) -> bool:
            if result_id.startswith(f"{GITHUB_DOC_ID_PREFIX}:"):
                # github drill-down has no ADR-0009 scope gate and resolves via a
                # different endpoint branch (github_section_document, not
                # get_document); the endpoint returns 200 for any reader with a
                # resolvable id.
                return True
            # Native cognee id: an unresolvable id (None), a cold/empty map, or
            # an id whose owner nodes are all hidden each deny (fail-closed) so
            # the flag never promises a 404 — textless entities and foreign-seat
            # docs/chunks fall out here.
            try:
                owner_node_ids = await get_citadel().resolve_document_owner_ids(
                    result_id
                )
            except Exception:  # noqa: BLE001 - any failure fails closed, like a 404
                return False
            if owner_node_ids is None:
                return False
            return node_ids_visible_in_map(actor, owner_node_ids, drilldown_map)

        drilldown_hint: dict[str, bool] = {}
        for item in normalized:
            if not isinstance(item, dict) or not isinstance(item.get("_citadel"), dict):
                continue
            envelope = item["_citadel"]
            result_id = str(envelope.get("result_id") or "")
            document_endpoint = document_endpoint_for_result(result_id)
            if not document_endpoint:
                # Synthetic chunk:<hash> id with no backing store — honestly
                # non-drillable, nothing to resolve.
                continue
            if result_id not in drilldown_hint:
                remaining = drilldown_deadline - time.perf_counter()
                if remaining <= 0:
                    drilldown_hint[result_id] = False
                else:
                    try:
                        drilldown_hint[result_id] = await asyncio.wait_for(
                            _resolve_drilldown(result_id), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        drilldown_hint[result_id] = False
            if drilldown_hint[result_id]:
                envelope["document_endpoint"] = document_endpoint
                retrieval = envelope.get("retrieval")
                if isinstance(retrieval, dict):
                    retrieval["document_drilldown_available"] = True
        drilldown_ms = (time.perf_counter() - drilldown_started) * 1000.0

    # Measured AFTER the drill-down pass: taking it at the _SearchSlot exit made
    # the audit row and search telemetry under-report exactly the per-hit cost
    # the pass adds. drilldown_ms below breaks out the pass's own share.
    latency_ms = (time.perf_counter() - started) * 1000.0

    for search_dataset in search_datasets:
        await mesh_state.record_search(
            citadel.config,
            query=body.query,
            dataset=search_dataset,
            # Counted from the page the caller actually receives, not from the
            # over-fetched candidate pool.
            result_count=sum(
                1
                for item in normalized
                if isinstance(item, dict)
                and isinstance(item.get("_citadel"), dict)
                and item["_citadel"].get("dataset") == search_dataset
            ),
        )
    audit_detail: dict[str, Any] = {
        "operation": "search",
        "query_sha256": hashlib.sha256(body.query.encode("utf-8")).hexdigest(),
        "query_length": len(body.query),
        "result_count": len(normalized),
        "top_k": body.top_k,
        "datasets": search_datasets,
        "scope_override": scope_override_active(actor, search_datasets),
        "latency_ms": round(latency_ms, 1),
        "timed_out": timed_out,
    }
    if drilldown_ms is not None:
        audit_detail["drilldown_ms"] = round(drilldown_ms, 1)
    if filters_active:
        audit_detail["candidates_fetched"] = candidates_fetched
        audit_detail["candidates_matched"] = candidates_matched
    record_mcp_audit(
        request,
        actor=actor,
        # A search that blew its budget returned empty-fast; the caller got no
        # hits. Recording it as a success meant /api/audit?view=failures never
        # showed it and any success-rate metric read 100% while users saw
        # nothing — which is how the "~20% silent failure" in #50 stayed
        # unquantified. The detail dict already carried timed_out; nothing read it.
        success=not timed_out,
        dataset=search_datasets[0],
        detail=audit_detail,
    )
    primary_dataset = search_datasets[0]
    node_dataset = (
        actor.default_dataset if is_seat_dataset(actor.default_dataset) else None
    )
    telemetry = await capture_search_feedback(
        mesh_state=mesh_state,
        config=citadel.config,
        request=request,
        actor=actor,
        query=body.query,
        results=normalized,
        search_datasets=search_datasets,
        primary_dataset=primary_dataset,
        top_k=body.top_k,
        latency_ms=latency_ms,
        timed_out=timed_out,
        session_id=body.session_id,
        filters=body.telemetry_filters(),
    )
    # Response-level relevance honesty (see lexical_relevance_summary): the
    # retriever exposes no score at this boundary, so the page reports the one
    # signal it CAN attest — lexical term overlap — and flags a page in which
    # no hit contains a single query term as not confidently matched.
    coverages: list[float] = []
    retriever_scores_seen = False
    for item in normalized:
        if not isinstance(item, dict) or not isinstance(item.get("_citadel"), dict):
            continue
        hit_relevance = item["_citadel"].get("relevance")
        if not isinstance(hit_relevance, dict):
            continue
        coverage = hit_relevance.get("term_coverage")
        if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
            coverages.append(float(coverage))
        if hit_relevance.get("retriever_score") is not None:
            retriever_scores_seen = True
    relevance_summary = lexical_relevance_summary(
        body.query, coverages, scores_available=retriever_scores_seen
    )
    payload: dict[str, Any] = {
        "results": normalized,
        "dataset": primary_dataset,
        "sections": split_search_sections(
            normalized,
            central_dataset=central_dataset(citadel.config),
            node_dataset=node_dataset,
        ),
        "docs_mode": docs_mode,
        "spec_mode": is_spec_mode_query(body.query) and not docs_mode,
        "relevance": relevance_summary,
    }
    if cleaned_mode:
        payload["mode"] = cleaned_mode
    if filters_active:
        payload["filtering"] = {
            "applied": {key: value for key, value in filter_kw.items() if value},
            "candidates_fetched": candidates_fetched,
            "candidates_matched": candidates_matched,
            "returned": len(normalized),
        }
    if len(search_datasets) > 1:
        payload["datasets"] = search_datasets
    if timed_out:
        payload["note"] = (
            f"Search exceeded the {citadel.config.search_timeout_seconds:.0f}s budget; "
            "returning truncated results — retry or narrow the query."
        )
        payload["timed_out"] = True
        payload["truncated"] = True
        payload["code"] = CODE_TIMEOUT
    elif not normalized and body.dataset is None and (
        not filters_active or candidates_fetched == 0
    ):
        # With filters active this fires only when retrieval itself came back
        # empty — an empty page whose candidates the filters excluded gets the
        # filter warning below instead, not dataset advice.
        payload["note"] = (
            "No results in the default dataset. Pass an explicit \"dataset\" to search a "
            "specific source; see known_datasets."
        )
        payload["known_datasets"] = known_datasets(citadel.config)
    warnings: list[str] = []
    if (
        filters_active
        and candidates_matched < body.top_k
        # Only when the filters actually excluded candidates: with
        # candidates_matched == candidates_fetched (including 0 fetched) the
        # page is short because retrieval found nothing else, and saying the
        # opposite would be literally false.
        and candidates_matched < candidates_fetched
        and not timed_out
    ):
        warnings.append(
            f"Filters matched {candidates_matched} of {candidates_fetched} fetched "
            "candidates; the page is short because post-retrieval filters excluded "
            "the rest, not because the search found nothing else."
        )
    if relevance_summary["no_lexical_match"]:
        warnings.append(NO_LEXICAL_MATCH_WARNING)
    authority = token_asset_authority_warning(body.query)
    if authority:
        warnings.append(authority)
    if warnings:
        payload["warnings"] = warnings
    if telemetry:
        payload["search_id"] = telemetry.get("search_id")
        payload["feedback"] = {
            "automatic": True,
            "kind": "search_telemetry",
            "explicit_tool": "citadel_record_feedback",
            "hint": (
                "Automatic search telemetry was recorded. After reading hits, optionally "
                "call citadel_record_feedback with qa_id=<hit id or search_id> and "
                "score 1 (useful) or -1 (not useful)."
            ),
        }
    return jsonable_encoder(payload)


@app.post("/feedback")
async def feedback(body: FeedbackBody, request: Request) -> Any:
    actor = require_access(request, "writer", "kb:feedback")
    try:
        qa_id = body.resolved_qa_id()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    score = body.resolved_score()
    # Feedback text lands in durable storage on a cache miss, so it needs the same
    # byte cap as /ingest — FeedbackBody.text carries no max_length of its own.
    if body.text:
        enforce_ingest_size(body.text)
    citadel = get_citadel()
    mesh_state = get_mesh()
    # Feedback is a durable write on a cache miss, so the caller-supplied dataset
    # and session MUST go through the same resolvers as /ingest and /api/contribute.
    # Passing body.dataset through unresolved let any writer token write into (and
    # emit mesh events attributed to) another seat's node, which
    # enforce_dataset_allowlist is default-deny for.
    dataset = resolve_write_dataset(actor, body.dataset, citadel.config)
    session_id = resolve_session_id(actor, body.session_id)
    try:
        result = await citadel.feedback(
            FeedbackRequest(
                qa_id=qa_id,
                score=score,
                text=body.text,
                session_id=session_id,
                dataset=dataset,
            )
        )
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        await mesh_state.record_error(citadel.config, operation="feedback", error=str(exc))
        record_mcp_audit(
            request,
            actor=actor,
            success=False,
            dataset=dataset,
            detail={"operation": "feedback", "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await mesh_state.record_feedback(
        citadel.config,
        qa_id=qa_id,
        dataset=dataset,
        result=result,
    )
    record_mcp_audit(
        request,
        actor=actor,
        success=True,
        dataset=dataset,
        detail={
            "operation": "feedback",
            "qa_id_sha256": hashlib.sha256(qa_id.encode("utf-8")).hexdigest(),
            "score": score,
            "has_text": bool(body.text),
            "recorded": result.recorded,
            "improved": result.improved,
            "kind": "explicit",
        },
    )
    return jsonable_encoder(result)


@app.post("/improve")
async def improve(body: ImproveBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    return await run_improve(body, request=request, actor=actor)


@app.post("/api/self-upgrade")
async def self_upgrade(body: ImproveBody, request: Request) -> Any:
    actor = require_access(request, "admin", "sources:sync")
    return await run_improve(body, request=request, actor=actor)


async def run_improve(
    body: ImproveBody,
    *,
    request: Request | None = None,
    actor: AccessIdentity | None = None,
) -> Any:
    citadel = get_citadel()
    mesh_state = get_mesh()
    dataset = body.dataset or citadel.config.default_dataset
    try:
        result = await citadel.improve(
            dataset=body.dataset,
            session_ids=body.session_ids,
        )
    except Exception as exc:  # pragma: no cover - depends on runtime Cognee configuration.
        await mesh_state.record_error(citadel.config, operation="improve", error=str(exc))
        if request:
            record_mcp_audit(
                request,
                actor=actor,
                success=False,
                dataset=dataset,
                detail={"operation": "improve", "error_type": exc.__class__.__name__},
            )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await mesh_state.record_upgrade(
        citadel.config,
        dataset=dataset,
        session_ids=body.session_ids,
    )
    if request:
        record_mcp_audit(
            request,
            actor=actor,
            success=True,
            dataset=dataset,
            detail={
                "operation": "improve",
                "session_count": len(body.session_ids or []),
            },
        )
    return jsonable_encoder({"result": result})
