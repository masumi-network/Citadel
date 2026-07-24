"""Citadel connectivity + setup status — the shared core.

One stdlib-only place that gathers "am I connected and set up?" so it can be
surfaced two ways:
  * `citadel status` / `citadel status --json`  (humans + AI agents via Bash)
  * any agent/script parsing the JSON.

All network calls are HTTPS-only and never follow redirects (the seat token is
sent to /api/session and /search). Every check captures its own failure instead
of raising, so the report always renders.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kb.capture_config import DEFAULT_NODE_URL, load_capture_config

TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
MCP_SERVER_NAME = "citadel"
SESSION_HOOK_MARKER = "kb.hooks.sync_session"
_TIMEOUT = 8.0
# Full vault search. The server caps its own recall at ``search_timeout_seconds``
# (config default 20s) and then answers HTTP 200 with a structured
# ``{timed_out:true, code:"TIMEOUT"}`` envelope. The client budget MUST sit above
# that server budget plus the server's post-recall work (drilldown, telemetry,
# mesh, audit) plus network, or the client aborts at the exact moment the server
# is about to return — turning a recoverable soft-timeout into a hard client
# failure, and killing normal 13-20s searches just before they'd return. 35s
# leaves ~15s of slack over the 20s server budget.
_SEARCH_TIMEOUT = 35.0
# Status smoke must not inherit the full search budget — search never gates
# `healthy`. But 3s sat below the FLOOR of real cognee recall latency (6-12s), so
# `--check-search` reported "timed out — node warming up" on every healthy node.
# 15s clears typical latency while staying under the server's 20s soft cap.
_SMOKE_SEARCH_TIMEOUT = 15.0
_INGEST_TIMEOUT = 60.0  # /ingest does real write work (and cold nodes are slow)
_SMOKE_QUERY = "citadel status connectivity smoke"

# Distinguishes MCP wiring for agents (P0 discovery clarity).
MCP_STATE_MISSING = "missing"
MCP_STATE_NEEDS_AUTH = "needsAuth"
MCP_STATE_READY_BUT_UNCONFIGURED = "readyButUnconfigured"
MCP_STATE_READY = "ready"
MCP_REMEDIATION = "citadel mcp add cursor"
# Shown when MCP is not ready — agents must not invent vault authority.
MCP_AGENT_FALLBACK = (
    "Until MCP works: `citadel status` then `citadel search` / `citadel doctor`; "
    "never claim vault-backed authority without a successful search hit; "
    "never claim “Citadel confirms X” without a retrieved title + snippet. "
    "If CLI is unhealthy: official/canonical docs (live OpenAPI, MIP, DevHub). "
    "Mainnet asset IDs / payment tokens (USDCx, USDM, …): prefer official Masumi docs "
    "/ skills/masumi — Citadel is not sole authority."
)

# Structured codes for agent readiness / error payloads (CLI JSON + check data).
CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
CODE_SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
CODE_TIMEOUT = "TIMEOUT"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT,
) -> dict[str, Any]:
    if not url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS Node URL")
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _humanize_net_error(exc: Exception) -> str:
    """Human words for the common network failures — raw urllib reasons read
    like C errno dumps ('nodename nor servname provided, or not known')."""
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, urllib.error.HTTPError):
        return text
    if "nodename nor servname" in lowered or "name or service not known" in lowered or "getaddrinfo" in lowered:
        return "cannot resolve host"
    if "connection refused" in lowered:
        return "connection refused"
    if isinstance(exc, TimeoutError) or "timed out" in lowered:
        return "timed out"
    return text


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    latency_ms: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusReport:
    node_url: str
    healthy: bool
    identity: dict[str, Any]
    checks: list[Check]
    recent: list[dict[str, Any]]
    repo: str | None = None

    def readiness(self) -> dict[str, Any]:
        """Compact agent probe: ``{authenticated, search, reason}`` (+ ``code``)."""
        by_name = {c.name: c for c in self.checks}
        auth = by_name.get("auth")
        search = by_name.get("search")
        mcp = by_name.get("mcp")
        authenticated = bool(auth and auth.ok)
        search_probed = search is not None
        search_ok = bool(search and search.ok)
        code: str | None = None
        if not authenticated:
            code = CODE_AUTH_REQUIRED
            detail = (auth.detail if auth else "not authenticated") or "not authenticated"
            reason = detail
        elif not search_probed:
            reason = "search not probed; pass --check-search"
        elif (search.data or {}).get("timed_out") or (search.data or {}).get("code") == CODE_TIMEOUT:
            code = CODE_TIMEOUT
            reason = search.detail or "search timed out"
        elif not search_ok:
            code = CODE_SEARCH_UNAVAILABLE
            reason = search.detail or "search unavailable"
        else:
            reason = "ok"
        out: dict[str, Any] = {
            "authenticated": authenticated,
            "search": search_ok,
            "search_probed": search_probed,
            "reason": reason,
        }
        if code:
            out["code"] = code
        if mcp and isinstance(mcp.data, dict) and mcp.data.get("state"):
            out["mcp_state"] = mcp.data["state"]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_url": self.node_url,
            "healthy": self.healthy,
            "identity": self.identity,
            "checks": [asdict(c) for c in self.checks],
            "recent": self.recent,
            "repo": self.repo,
            "readiness": self.readiness(),
        }


def _mask(token: str | None) -> str:
    if not token:
        return "(none)"
    token = token.strip()
    # Only the last 4 chars — never contiguous bytes from the secret's start.
    return f"…{token[-4:]}" if len(token) > 10 else "****"


def check_node_health(base_url: str, *, timeout: float = _TIMEOUT) -> Check:
    started = time.monotonic()
    try:
        data = _request("GET", f"{base_url.rstrip('/')}/healthz", timeout=timeout)
    except Exception as exc:
        return Check("node", ok=False, detail=_humanize_net_error(exc))
    latency = int((time.monotonic() - started) * 1000)
    ok = bool(data.get("ok"))
    return Check("node", ok=ok, detail="healthy" if ok else "unhealthy", latency_ms=latency)


def check_auth(base_url: str, token: str | None, *, timeout: float = _TIMEOUT) -> Check:
    if not token:
        return Check(
            "auth",
            ok=False,
            detail="no token (CITADEL_MCP_ACCESS_TOKEN unset)",
            data={"code": CODE_AUTH_REQUIRED},
        )
    started = time.monotonic()
    try:
        data = _request(
            "GET", f"{base_url.rstrip('/')}/api/session", token=token, timeout=timeout
        )
    except Exception as exc:
        detail = _humanize_net_error(exc)
        return Check("auth", ok=False, detail=detail, data={"code": CODE_AUTH_REQUIRED})
    latency = int((time.monotonic() - started) * 1000)
    identity = {
        "seat_slug": data.get("seat_slug"),
        "node_label": data.get("node_label"),
        "role": data.get("role"),
        "capabilities": data.get("capabilities", {}),
        "actor": (data.get("actor") or {}).get("name"),
    }
    ok = bool(data.get("ok"))
    auth_data: dict[str, Any] = dict(identity)
    if not ok:
        auth_data["code"] = CODE_AUTH_REQUIRED
    return Check("auth", ok=ok, detail="valid" if ok else "invalid session", latency_ms=latency, data=auth_data)


def check_search(
    base_url: str, token: str | None, *, timeout: float = _SMOKE_SEARCH_TIMEOUT
) -> Check:
    if not token:
        return Check(
            "search",
            ok=False,
            detail="skipped (no token)",
            data={"code": CODE_AUTH_REQUIRED},
        )
    started = time.monotonic()
    try:
        data = _request(
            "POST",
            f"{base_url.rstrip('/')}/search",
            token=token,
            payload={"query": _SMOKE_QUERY, "top_k": 1},
            timeout=timeout,
        )
    except TimeoutError:
        return Check(
            "search",
            ok=False,
            detail=f"timed out after {timeout:g}s — node warming up",
            data={"timed_out": True, "code": CODE_TIMEOUT},
        )
    except Exception as exc:
        # urllib may wrap a socket timeout as URLError("timed out") — treat like
        # TimeoutError so the smoke budget reads as warm-up, not a cryptic net error.
        if "timed out" in str(exc).lower():
            return Check(
                "search",
                ok=False,
                detail=f"timed out after {timeout:g}s — node warming up",
                data={"timed_out": True, "code": CODE_TIMEOUT},
            )
        return Check(
            "search",
            ok=False,
            detail=_humanize_net_error(exc),
            data={"code": CODE_SEARCH_UNAVAILABLE},
        )
    latency = int((time.monotonic() - started) * 1000)
    results = data.get("results")
    if results is None:
        results = data.get("matches")
    count = len(results) if isinstance(results, list) else 0
    # A zero-result smoke search means the read path is up but the data plane is
    # empty/broken — report it honestly instead of always-green (#27).
    search_data: dict[str, Any] = {"count": count}
    if count <= 0:
        search_data["code"] = CODE_SEARCH_UNAVAILABLE
    return Check(
        "search",
        ok=count > 0,
        detail=f"{count} result(s)",
        latency_ms=latency,
        data=search_data,
    )


def check_corpus(base_url: str, token: str | None, *, timeout: float = _TIMEOUT) -> Check | None:
    """Data-plane corpus health from the Node's honest /readyz (#27).

    Returns None (non-gating) when there is no token or /readyz is unreachable;
    /readyz answers 503 with a body when the corpus gate or canary is RED, so we
    parse the body off the HTTPError too.
    """
    if not token:
        return None
    url = f"{base_url.rstrip('/')}/readyz"
    started = time.monotonic()
    try:
        data = _request("GET", url, token=token, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            return None
        try:
            data = json.loads(exc.read().decode() or "{}")
        except (ValueError, OSError):
            return None
    except Exception:
        return None
    latency = int((time.monotonic() - started) * 1000)
    corpus = data.get("corpus") or {}
    canary = data.get("canary")
    indexed = corpus.get("indexed_docs")
    tracked = corpus.get("tracked_sources")
    ok = bool(corpus.get("ok", True)) and (canary is None or bool(canary.get("ok", True)))
    detail = "ok" if indexed is None else f"{indexed} indexed / {tracked} tracked"
    return Check("corpus", ok=ok, detail=detail, latency_ms=latency, data={"canary": canary})


def search_node(
    base_url: str,
    token: str,
    query: str,
    top_k: int = 10,
    *,
    timeout: float = _SEARCH_TIMEOUT,
    dataset: str | None = None,
    session_id: str | None = None,
    types: list[str] | None = None,
    repo: str | None = None,
    path: str | None = None,
    canonical_only: bool = False,
    exclude_ambient: bool = False,
    mode: str | None = None,
) -> dict[str, Any]:
    """POST a query to the Node's /search (the same endpoint MCP citadel_search uses).

    Zero-dep, HTTPS-only. The Node resolves the dataset from the token's seat by
    default. Optional filter fields (``types`` / ``repo`` / ``path`` /
    ``canonical_only`` / ``exclude_ambient`` / ``mode``) are forwarded so
    server-side telemetry sees the same narrowing the CLI applies. Returns the
    full search payload (``results``, ``sections``, ``dataset``, …) like MCP
    ``citadel_search`` — not a flattened list.
    """
    payload: dict[str, Any] = {"query": query, "top_k": top_k}
    if dataset:
        payload["dataset"] = dataset
    if session_id:
        payload["session_id"] = session_id
    if types:
        cleaned = [str(item).strip() for item in types if str(item).strip()]
        if cleaned:
            payload["types"] = cleaned
    if repo and str(repo).strip():
        payload["repo"] = str(repo).strip()
    if path and str(path).strip():
        payload["path"] = str(path).strip()
    if canonical_only:
        payload["canonical_only"] = True
    if exclude_ambient:
        payload["exclude_ambient"] = True
    if mode and str(mode).strip():
        payload["mode"] = str(mode).strip().lower()
    data = _request(
        "POST",
        f"{base_url.rstrip('/')}/search",
        token=token,
        payload=payload,
        timeout=timeout,
    )
    results = data.get("results")
    if results is None:
        results = data.get("matches")
    data["results"] = results if isinstance(results, list) else []
    return data


def seatless_token_hint(identity: dict[str, Any]) -> str | None:
    """Actionable warning when auth succeeded but the token has no seat."""
    if identity.get("seat_slug"):
        return None
    return (
        "This token has no seat — personal Node search/ingest, capture hooks, and "
        "citadel_share_session will fail (403). Ask an admin for a seat-bound token "
        "(dashboard → Create Seat, or `citadel seat create`)."
    )


def ingest_node(
    base_url: str,
    token: str,
    data: str,
    tags: list[str] | tuple[str, ...] = (),
    cognify: bool = False,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST a note to the Node's /ingest (same endpoint MCP citadel_ingest uses).

    Sends no dataset, so the seat token routes the write to the dev's private
    node (personal-by-default), mirroring the SessionEnd hook. With cognify=True
    the Node builds the graph inline and blocks until done (so the note is
    immediately searchable). Returns the response (``accepted``, ``reason``,
    ``dataset``, and ``cognified`` when cognify was requested).
    """
    payload: dict[str, Any] = {"data": data, "tags": list(tags)}
    if cognify:
        payload["cognify"] = True
    resolved = timeout if timeout is not None else (_COGNIFY_TIMEOUT if cognify else _INGEST_TIMEOUT)
    return _request(
        "POST",
        f"{base_url.rstrip('/')}/ingest",
        token=token,
        payload=payload,
        timeout=resolved,
    )


_COGNIFY_TIMEOUT = 180.0  # /ingest cognifies inline; give the combined call room


def fetch_mesh(base_url: str, token: str | None, *, timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Best-effort GET /api/mesh snapshot (knowledge-graph stats). {} on any error."""
    if not token:
        return {}
    try:
        data = _request("GET", f"{base_url.rstrip('/')}/api/mesh", token=token, timeout=timeout)
    except Exception as exc:
        # A network failure is NOT an empty vault — mark it so the renderer says
        # "couldn't reach the Node" instead of silently showing nothing (#101).
        return {"error": _humanize_net_error(exc)}
    return data if isinstance(data, dict) else {}


def fetch_events(
    base_url: str,
    token: str | None,
    *,
    after_id: int | None = None,
    limit: int = 20,
    event_type: str | None = None,
    timeout: float = _TIMEOUT,
) -> dict[str, Any]:
    """Best-effort GET /api/knowledge/events (the caller-scoped Vault Activity
    timeline). Returns ``{}`` on any error / missing token. ``after_id`` resumes
    after the last seen event id (for --watch polling)."""
    if not token:
        return {}
    query = f"limit={max(1, min(int(limit), 160))}"
    if after_id is not None:
        query += f"&after_id={int(after_id)}"
    if event_type:
        query += f"&type={urllib.parse.quote(event_type)}"
    try:
        data = _request(
            "GET", f"{base_url.rstrip('/')}/api/knowledge/events?{query}", token=token, timeout=timeout
        )
    except Exception as exc:
        return {"error": _humanize_net_error(exc)}
    return data if isinstance(data, dict) else {}


def fetch_presence(base_url: str, token: str | None, *, timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Best-effort org **Seat Presence** board (ADR-0009): every seat's slug and
    contribution count, org-visible by design.

    Reads ONLY the ``dataset``-type presence hubs from ``/api/mesh/graph`` — seat
    slug + ``presence.documents`` — and ignores every content node, so this can
    never surface another seat's **Node** content. ``limit=1`` keeps content
    shaping minimal; presence hubs are appended regardless of the cap. ``{}`` on
    any error / missing token.
    """
    if not token:
        return {}
    try:
        data = _request(
            "GET", f"{base_url.rstrip('/')}/api/mesh/graph?limit=1", token=token, timeout=timeout
        )
    except Exception as exc:
        return {"error": _humanize_net_error(exc)}
    if not isinstance(data, dict):
        return {}
    seats: list[dict[str, Any]] = []
    for node in data.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "dataset":
            continue
        label = str(node.get("label") or node.get("dataset") or "").strip()
        if not label:
            continue
        presence = node.get("presence") if isinstance(node.get("presence"), dict) else {}
        docs = presence.get("documents")
        seats.append({"seat": label, "documents": docs if isinstance(docs, int) else None})
    return {"seats": seats}


def _mcp_block_from_path(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(servers, dict):
        return None
    block = servers.get(MCP_SERVER_NAME)
    return block if isinstance(block, dict) else None


def _mcp_block_usable(block: dict[str, Any] | None) -> bool:
    """True when the block is hosted HTTP Citadel (Cursor/Claude-compatible)."""
    if not block:
        return False
    from kb.onboard import is_http_citadel_mcp_block, is_legacy_stdio_mcp_block

    if is_legacy_stdio_mcp_block(block):
        return False
    return is_http_citadel_mcp_block(block) or bool(
        (block.get("url") or block.get("serverUrl") or "").strip()
    )


def _cursor_mcp_path() -> Path:
    override = (os.getenv("CITADEL_CURSOR_MCP_PATH") or "").strip()
    if override:
        return Path(override)
    return Path("~/.cursor/mcp.json").expanduser()


def assess_mcp_setup(repo: Path) -> Check:
    """Classify local MCP wiring: missing | needsAuth | readyButUnconfigured | ready.

    ``ok`` is true only for ``ready`` (hosted HTTP citadel entry + seat token in env).
    Ordering matters for agent remediation:

    1. **missing** — no citadel MCP block at all
    2. **readyButUnconfigured** — a block exists but is not hosted HTTP (e.g. legacy
       stdio). Checked *before* token so stdio-only + no token does not look like
       a mere auth gap.
    3. **needsAuth** — HTTP bridge is wired but ``CITADEL_MCP_ACCESS_TOKEN`` is unset
    4. **ready** — HTTP + token

    A bare ``.mcp.json`` presence is no longer enough — that was a false green
    when Cursor still only exposed mcp_auth.
    """
    token_present = bool((os.getenv(TOKEN_ENV) or "").strip())
    project_block = _mcp_block_from_path(repo / ".mcp.json")
    cursor_block = _mcp_block_from_path(_cursor_mcp_path())
    project_ok = _mcp_block_usable(project_block)
    cursor_ok = _mcp_block_usable(cursor_block)
    any_block = project_block is not None or cursor_block is not None
    any_usable = project_ok or cursor_ok

    if not any_block:
        state = MCP_STATE_MISSING
        detail = f"not configured — run `{MCP_REMEDIATION}` (or CLI search)"
        next_step: str | None = MCP_REMEDIATION
        ok = False
    elif not any_usable:
        # Prefer this over needsAuth when only legacy stdio is present — setting a
        # token alone will not expose hosted tools.
        state = MCP_STATE_READY_BUT_UNCONFIGURED
        token_hint = "" if token_present else f" (also set {TOKEN_ENV} after)"
        detail = (
            f"citadel entry present but not hosted HTTP — run `{MCP_REMEDIATION}` "
            f"(or `citadel doctor --fix`){token_hint}; else CLI search"
        )
        next_step = MCP_REMEDIATION
        ok = False
    elif not token_present:
        state = MCP_STATE_NEEDS_AUTH
        detail = (
            f"hosted MCP wired but {TOKEN_ENV} unset — set token then restart Cursor; "
            "else CLI search (no vault authority without a hit)"
        )
        # Wiring is fine; doctor --fix cannot mint a token.
        next_step = f"export {TOKEN_ENV}=… then restart Cursor"
        ok = False
    else:
        state = MCP_STATE_READY
        where = []
        if project_ok:
            where.append("project .mcp.json")
        if cursor_ok:
            where.append("Cursor ~/.cursor/mcp.json")
        detail = f"ready ({', '.join(where)})"
        next_step = None
        ok = True

    data: dict[str, Any] = {
        "state": state,
        "configured": any_usable and token_present,
        "project_mcp": project_ok,
        "cursor_mcp": cursor_ok,
        "token_present": token_present,
        "next": next_step,
    }
    if not ok:
        data["fallback"] = MCP_AGENT_FALLBACK

    return Check(
        "mcp",
        ok=ok,
        detail=detail,
        data=data,
    )


def check_local_setup(repo: Path, config_path: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check("token", ok=bool(os.getenv(TOKEN_ENV)), detail=_mask(os.getenv(TOKEN_ENV)))
    )
    checks.append(assess_mcp_setup(repo))

    pre_push = repo / ".git" / "hooks" / "pre-push"
    checks.append(
        Check("pre_push_hook", ok=pre_push.exists(), detail="installed" if pre_push.exists() else "missing")
    )

    # Session hooks live in user-scope ~/.claude/settings.json (cross-repo), not
    # the project's .claude/settings.json (#38).
    from kb.onboard import claude_user_settings_path

    settings = claude_user_settings_path()
    session_ok = False
    if settings.exists():
        try:
            session_ok = SESSION_HOOK_MARKER in settings.read_text()
        except OSError:
            session_ok = False
    checks.append(
        Check("session_hook", ok=session_ok, detail="installed" if session_ok else "missing")
    )

    try:
        config = load_capture_config(config_path) if config_path else load_capture_config()
    except ValueError as exc:
        # A corrupt capture.json must not break the whole status report.
        checks.append(Check("capture_roots", ok=False, detail=f"corrupt config ({exc})"))
        return checks
    if config.roots:
        tags = sorted({tag for root in config.roots for tag in root.tags})
        detail = f"{len(config.roots)} root(s): {', '.join(tags)}"
    else:
        detail = "none (push hook captures nothing until `citadel setup`)"
    checks.append(Check("capture_roots", ok=True, detail=detail, data={"count": len(config.roots)}))
    return checks


def fetch_recent(
    base_url: str, token: str | None, *, limit: int = 5, timeout: float = _TIMEOUT
) -> list[dict[str, Any]]:
    if not token:
        return []
    try:
        data = _request(
            "GET",
            f"{base_url.rstrip('/')}/api/contributions/recent?mine=true&limit={limit}",
            token=token,
            timeout=timeout,
        )
    except Exception:
        return []
    return data.get("contributions") or []


def gather_status(
    base_url: str = DEFAULT_NODE_URL,
    token: str | None = None,
    *,
    repo: Path | None = None,
    config_path: Path | None = None,
    with_search: bool = False,
    with_recent: bool = True,
    timeout: float = _TIMEOUT,
) -> StatusReport:
    base_url = base_url.rstrip("/")
    repo = repo or Path.cwd()

    # The network checks are independent, so they run concurrently — wall time
    # is the slowest check, not the sum of all of them. Search smoke is opt-in
    # (non-gating) and uses a short budget so status stays snappy by default.
    # Each check still captures its own failure, so a thread never raises.
    with ThreadPoolExecutor(max_workers=5) as pool:
        node_f = pool.submit(check_node_health, base_url, timeout=timeout)
        auth_f = pool.submit(check_auth, base_url, token, timeout=timeout)
        search_f = pool.submit(check_search, base_url, token) if with_search else None
        corpus_f = pool.submit(check_corpus, base_url, token, timeout=timeout)
        recent_f = (
            pool.submit(fetch_recent, base_url, token, timeout=timeout) if with_recent else None
        )
        node = node_f.result()
        auth = auth_f.result()
        checks = [node, auth]
        if search_f is not None:
            checks.append(search_f.result())
        corpus = corpus_f.result()
        if corpus is not None:
            checks.append(corpus)
        recent = recent_f.result() if recent_f is not None else []
    checks.extend(check_local_setup(repo, config_path))
    # A RED corpus gate (sources tracked but graph empty, or the canary failed)
    # makes the whole report unhealthy — no more green over a broken data plane (#27).
    corpus_ok = corpus.ok if corpus is not None else True
    return StatusReport(
        node_url=base_url,
        healthy=node.ok and auth.ok and corpus_ok,
        identity=auth.data,
        checks=checks,
        recent=recent,
        repo=str(repo),
    )


# Checks split into two sections; everything not here is "Local setup".
_CONNECTIVITY = ("node", "auth", "search", "corpus")
# Human labels — the raw snake_case names read like debug output.
_CHECK_LABELS = {
    "node": "Node",
    "auth": "Auth",
    "search": "Search",
    "corpus": "Data plane",
    "token": "Token",
    "mcp": "MCP server",
    "pre_push_hook": "Pre-push hook",
    "session_hook": "SessionEnd hook",
    "capture_roots": "Capture roots",
}


def _fmt_latency(latency_ms: int | None) -> str:
    """Humanized latency: '143ms' below a second, '5.8s' above."""
    if latency_ms is None:
        return ""
    if latency_ms >= 1000:
        return f"{latency_ms / 1000:.1f}s"
    return f"{latency_ms}ms"


# A check slower than this gets its latency called out instead of dimmed.
_SLOW_MS = 3000


def render_text(report: StatusReport, *, color: bool = False, verdict: bool = True) -> str:
    from kb.banner import WARN, mark, paint

    ident = report.identity
    seat = ident.get("seat_slug") or ident.get("actor") or "—"
    role = ident.get("role") or "—"
    identity_line = f"seat: {paint(str(seat), 'bold', enable=color)}   role: {role}"
    # Where this token writes — the onboard identity panel's "writes" fact,
    # surfaced here too so status answers it without a second command.
    if ident.get("capabilities", {}).get("write"):
        writes = f"seat:{ident['seat_slug']}" if ident.get("seat_slug") else "shared org dataset"
        identity_line += f"   writes: {writes}"
    lines = [
        identity_line,
        paint(f"node: {report.node_url}", "dim", enable=color),
    ]
    # Only claim "no seat" when auth actually SUCCEEDED. On a timeout/unreachable
    # node check_auth returns an identity with no seat_slug, which used to trip the
    # seatless hint and tell a user with a working token to go ask an admin (#101).
    # The Auth row below already shows the real failure, so stay silent here.
    auth_check = next((c for c in report.checks if c.name == "auth"), None)
    auth_ok = auth_check.ok if auth_check is not None else True
    seat_hint = seatless_token_hint(ident) if auth_ok else None
    if seat_hint:
        lines.append(paint(f"  hint: {seat_hint}", "yellow", enable=color))
    lines.append("")

    cols = shutil.get_terminal_size((80, 24)).columns

    def _row(check: Check, detail_width: int) -> str:
        label = _CHECK_LABELS.get(check.name, check.name)
        latency = _fmt_latency(check.latency_ms)
        detail = check.detail
        budget = cols - 22 - len(latency)  # 2 indent + glyph + space + 16 label + space + gap
        if budget > 8 and len(detail) > budget:
            detail = detail[: budget - 1] + "…"
        if latency:
            slow = (check.latency_ms or 0) >= _SLOW_MS
            pad = " " * max(detail_width - len(detail) + 2, 2)
            latency = pad + paint(latency, "yellow" if slow else "dim", enable=color)
        sigil = mark(check.ok, enable=color)
        if not check.ok and check.name == "search":
            # Search never gates health — a red ✗ next to a green verdict reads
            # as a contradiction, so the non-gating failure warns instead.
            sigil = paint(WARN, "yellow", enable=color)
        return f"  {sigil} {label:<16} {detail}{latency}"

    conn = [c for c in report.checks if c.name in _CONNECTIVITY]
    local = [c for c in report.checks if c.name not in _CONNECTIVITY]

    def _section(title: str, checks: list[Check], note: str = "") -> list[str]:
        # Latencies form one aligned (and dimmed) column per section, so the
        # details read as a block instead of a ragged mix of text and numbers.
        width = min(max((len(c.detail) for c in checks), default=0), max(cols - 30, 8))
        header = paint(title, "bold", enable=color) + (paint(note, "dim", enable=color) if note else "")
        return [header, *(_row(c, width) for c in checks)]

    if conn:
        lines.extend(_section("Connectivity", conn))
        lines.append("")
    if local:
        # Local checks are repo-relative — name the repo, so a ✗ from the wrong
        # directory reads as "wrong directory", not "broken setup".
        note = f"  — {report.repo}" if report.repo else ""
        lines.extend(_section("Local setup", local, note))

    if report.recent:
        lines.append("")
        lines.append(paint("Recent activity", "bold", enable=color))
        for item in report.recent[:5]:
            when = item.get("created_at") or item.get("timestamp") or ""
            label = item.get("title") or item.get("action") or item.get("detail") or "—"
            lines.append(f"  · {paint(str(when)[:19], 'dim', enable=color)}  {label}")

    if verdict:
        lines.append("")
        lines.append(render_verdict(report, color=color))
    return "\n".join(lines)


def render_verdict(report: StatusReport, *, color: bool = False) -> str:
    """The one-line (plus hints) bottom-line summary — always the last thing printed."""
    from kb.banner import paint

    lines: list[str] = []
    conn = [c for c in report.checks if c.name in _CONNECTIVITY]
    local = [c for c in report.checks if c.name not in _CONNECTIVITY]
    conn_fail = [c for c in conn if not c.ok]
    local_fail = [c for c in local if not c.ok]
    # A cold-node Search TIMEOUT is non-gating noise; a hard Search error is not.
    search_fail = [c for c in conn if c.name == "search" and not c.ok]
    search_warming = any((c.data or {}).get("timed_out") for c in search_fail)
    search_degraded = any(not (c.data or {}).get("timed_out") for c in search_fail)

    if report.healthy and not conn_fail and not local_fail:
        lines.append(paint("All systems go.", "green", enable=color))
    elif report.healthy:
        # node + auth are OK — say so plainly, then call out only real gaps.
        lines.append(paint("Connected to the Node.", "green", enable=color))
        if local_fail:
            labels = ", ".join(_CHECK_LABELS.get(c.name, c.name) for c in local_fail)
            lines.append(
                paint(
                    f"  Local setup incomplete ({len(local) - len(local_fail)}/{len(local)}) — {labels}.",
                    "yellow",
                    enable=color,
                )
            )
            lines.append(paint("  Run `citadel doctor --fix` to repair.", "dim", enable=color))
        if search_warming:
            lines.append(paint("  Search is still warming up — retry in a minute.", "dim", enable=color))
        elif search_degraded:
            detail = next((c.detail for c in search_fail), "")
            lines.append(paint(f"  Search degraded — {detail}. Not blocking.", "yellow", enable=color))
    else:
        node_auth_fail = [c for c in conn_fail if c.name in ("node", "auth")]
        corpus_fail = next((c for c in conn if c.name == "corpus" and not c.ok), None)
        if node_auth_fail:
            labels = ", ".join(_CHECK_LABELS.get(c.name, c.name) for c in node_auth_fail) or "Node/Auth"
            lines.append(
                paint(
                    f"Not connected — {labels} failing. Check the Node URL / token, or run `citadel onboard`.",
                    "yellow",
                    enable=color,
                )
            )
        elif corpus_fail is not None:
            # The Node is up, but the data plane is broken (sources tracked, graph
            # empty, or the cognify canary failed) — the exact #27 failure mode.
            lines.append(
                paint(
                    f"Data plane broken — {corpus_fail.detail}. The Node is up but search "
                    "returns nothing; ingested data is not being indexed.",
                    "red",
                    enable=color,
                )
            )
        else:
            labels = ", ".join(_CHECK_LABELS.get(c.name, c.name) for c in conn_fail) or "Node/Auth"
            lines.append(
                paint(
                    f"Not connected — {labels} failing. Check the Node URL / token, or run `citadel onboard`.",
                    "yellow",
                    enable=color,
                )
            )
    return "\n".join(lines)
