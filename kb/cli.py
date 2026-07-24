from __future__ import annotations

import argparse
import asyncio
import difflib
import functools
import getpass
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, NoReturn

# Lightweight client modules only — the heavy server stack (kb.service,
# kb.github_sync, …) is imported lazily inside the server handlers so the base
# `citadel-archive` install (onboard/status/capture) needs no server deps.
from kb.capture_config import (
    DEFAULT_NODE_URL,
    DEFAULT_ROOT_TAG,
    PRESET_ROOT_TAGS,
    CaptureConfig,
    capture_config_path,
    load_capture_config,
    normalize_path,
    normalize_tags,
    save_capture_config,
)
from kb.capture import build_capture_payload, capture_token, post_capture
from kb.capture_roots_sync import sync_local_capture_roots_to_server, sync_warning_message
from kb.onboard import (
    TOKEN_ENV,
    claude_user_settings_path,
    detect_shell_rc,
    diagnose_mcp_config,
    ensure_env_in_rc,
    ensure_token_in_rc,
    format_claude_mcp_next_steps,
    git_root_or_cwd,
    install_agent_policies,
    install_pre_push_hook,
    mask_token,
    merge_claude_settings,
    merge_mcp_config,
    read_token_from_rc,
)
from kb.banner import (
    HERO_WIDTH,
    SKIP,
    WARN,
    banner,
    mark,
    paint,
    print_banner_animated,
    print_banner_cascade,
    supports_color,
)
from kb.access_client import (
    AccessClientError,
    create_seat,
    create_token,
    issue_seat_token,
    list_seats,
    revoke_token,
)
from kb.promotion_client import (
    PromotionClientError,
    approve_pending,
    list_pending,
    node_base_url,
    reject_pending,
    run_promotion,
)
from kb.status import (
    MCP_STATE_MISSING,
    MCP_STATE_NEEDS_AUTH,
    MCP_STATE_READY_BUT_UNCONFIGURED,
    assess_mcp_setup,
    fetch_events,
    fetch_mesh,
    fetch_presence,
    gather_status,
    render_text,
    render_verdict,
    seatless_token_hint,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, default=str, indent=2))


class _Spinner:
    """An animated stdlib progress indicator on stderr (so stdout stays clean).

    A cyan knight-rider bar that bounces while work runs, with an elapsed-seconds
    readout once an op is slow. Active only on a real stderr TTY; a no-op
    otherwise (CI, pipes, --json), so it never pollutes captured output.
    """

    _INTERVAL = 0.11
    # A 7-cell bounce: a bright block sweeps back and forth over dim cells.
    _WIDTH = 7
    _POSITIONS = list(range(_WIDTH)) + list(range(_WIDTH - 2, 0, -1))

    def __init__(self, message: str) -> None:
        self.message = message
        self.enable = sys.stderr.isatty()
        self._color = supports_color(sys.stderr)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _frame(self, step: int) -> str:
        pos = self._POSITIONS[step % len(self._POSITIONS)]
        cells = "".join("▰" if i == pos else "▱" for i in range(self._WIDTH))
        return paint(cells, "cyan", enable=self._color)

    def _run(self) -> None:
        step = 0
        while not self._stop.is_set():
            elapsed = int(step * self._INTERVAL)
            tail = paint(f"  {elapsed}s", "dim", enable=self._color) if elapsed >= 2 else ""
            sys.stderr.write(f"\r{self._frame(step)} {self.message}{tail} ")
            sys.stderr.flush()
            step += 1
            self._stop.wait(self._INTERVAL)

    def __enter__(self) -> "_Spinner":
        if self.enable:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            sys.stderr.write("\r\033[K")  # erase the indicator line
            sys.stderr.flush()


def _needs_server(
    fn: Callable[[argparse.Namespace], Awaitable[Any]],
) -> Callable[[argparse.Namespace], Awaitable[Any]]:
    """Wrap a server-only handler: turn a missing [server] dep into a clean hint."""

    @functools.wraps(fn)
    async def wrapper(args: argparse.Namespace) -> Any:
        cmd = getattr(args, "command", "") or ""
        try:
            return await fn(args)
        except ImportError as exc:
            missing = getattr(exc, "name", None) or str(exc)
            print(
                f"`citadel {cmd}` needs the server extra:\n"
                "  pip install 'citadel-archive[server]'"
                "    (pipx: pipx install --force 'citadel-archive[server]')\n"
                f"  (missing dependency: {missing})",
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            # Operational failure (network, cognee, bad dataset): a clean stderr
            # line and a nonzero exit — never a raw traceback dumped at the user.
            print(f"citadel {cmd}: {exc}", file=sys.stderr)
            return 1

    return wrapper


def _prompt(text: str) -> str:
    """input() that treats Ctrl-D (EOF) as an empty answer, not a traceback."""
    try:
        return input(text)
    except EOFError:
        print()
        return ""


def _stale_env_hint(status_code: int) -> str | None:
    """The fix-it line for an auth-rejected command run from a stale shell.

    After `citadel onboard`/`token set` rotate the token in the shell rc, the
    *current* shell still exports the old value — and env is what commands
    send. When the Node rejects it (401/403) and the rc disagrees with env,
    the fix is `source <rc>`, so say exactly that instead of a bare 401.
    """
    if status_code not in (401, 403):
        return None
    rc_path = detect_shell_rc()
    rc_token = read_token_from_rc(rc_path)
    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if "$" in rc_token:
        # Variable indirection (export TOKEN="$OTHER") — we can't evaluate it,
        # so a textual mismatch proves nothing; stay quiet rather than send
        # the user on a `source` loop that can never fix a real 401.
        return None
    if rc_token and rc_token != env_token:
        return (
            f"this shell's {TOKEN_ENV} is out of date with {rc_path} — "
            f"run `source {rc_path}` (or open a new shell) and retry."
        )
    return None


def _print_auth_hint(command: str, status_code: int) -> None:
    hint = _stale_env_hint(status_code)
    if hint:
        print(f"citadel {command}: hint: {hint}", file=sys.stderr)


# Agent-safe wording: lead with the env var, and when we name onboard use the
# NON-interactive form — a bare `citadel onboard` in an agent/CI session hangs on
# prompts, so never nudge toward it from a machine-facing error path.
_NO_TOKEN_MSG = (
    "no token — set CITADEL_MCP_ACCESS_TOKEN, "
    "or run `citadel onboard --non-interactive --token ctdl_...`."
)


def _emit_no_token(command: str, *, as_json: bool) -> int:
    """Uniform no-token error: a JSON object under ``--json``, else plain text.

    Keeps `--json` machine-readable on the failure path (matches `status` /
    `onboard`) so an agent piping the output never chokes on a plain-text line.
    """
    if as_json:
        from kb.status import CODE_AUTH_REQUIRED

        _print_json({"ok": False, "error": _NO_TOKEN_MSG, "code": CODE_AUTH_REQUIRED})
    else:
        print(f"citadel {command}: {_NO_TOKEN_MSG}", file=sys.stderr)
    return 1


def _emit_error(
    command: str,
    message: str,
    *,
    as_json: bool,
    code: str,
    exit_code: int = 1,
    extra: dict[str, Any] | None = None,
) -> int:
    """Uniform failure emit: a JSON object under ``--json``, else a stderr line.

    Under ``--json`` a scripted/agent caller always gets a parseable object on
    stdout with a stable ``code`` — previously the network/HTTP failure paths
    printed only to stderr and left stdout empty, so the caller got nothing.
    """
    if as_json:
        payload: dict[str, Any] = {"ok": False, "error": message, "code": code}
        if extra:
            payload.update(extra)
        _print_json(payload)
    else:
        print(f"citadel {command}: {message}", file=sys.stderr)
    return exit_code


def _result_exit(value: Any) -> int:
    """Exit 1 when a result payload carries ``ok: False``; else 0.

    Closes the 'JSON says failed but exit 0' gap without each handler having to
    special-case it. Values without an ``ok`` key (lists, plain results) → 0.
    """
    data = value if isinstance(value, dict) else getattr(value, "__dict__", {})
    if isinstance(data, dict) and (data.get("ok") is False or data.get("accepted") is False):
        return 1
    return 0


async def _ingest(args: argparse.Namespace) -> int:
    """Add a note to your Node over HTTP (like MCP citadel_ingest). `--local`
    runs the in-process server stack (needs the [server] extra)."""
    if getattr(args, "local", False):
        return await _ingest_local(args)
    _reject_local_only_flags(args, "ingest")
    base_url = node_base_url(getattr(args, "node_url", None))
    token = capture_token()
    if not token:
        return _emit_no_token("ingest", as_json=getattr(args, "json", False))
    from kb.status import ingest_node

    # Cognify inline (server-side) by default so the note is immediately
    # searchable; the one request blocks until cognify finishes (--no-cognify skips).
    cognify = not getattr(args, "no_cognify", False)
    as_json = getattr(args, "json", False)
    spinner_msg = "Ingesting + building the graph…" if cognify else "Ingesting to your Node…"
    try:
        with _Spinner(spinner_msg):
            result = await asyncio.to_thread(ingest_node, base_url, token, args.data, args.tag, cognify)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200] if exc.fp else exc.reason
        if not as_json:
            _print_auth_hint("ingest", exc.code)
        return _emit_error(
            "ingest",
            f"HTTP {exc.code} {detail}",
            as_json=as_json,
            code="HTTP_ERROR",
            extra={"http_status": exc.code},
        )
    except TimeoutError:
        return _emit_error(
            "ingest",
            "the Node is still working (cognify can be slow). Your note is saved — it'll "
            "be searchable shortly; check `citadel search`.",
            as_json=as_json,
            code="TIMEOUT",
            extra={"saved": True},
        )
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        return _emit_error("ingest", str(exc), as_json=as_json, code="NODE_UNREACHABLE")
    if not isinstance(result, dict):  # a misconfigured Node could return non-dict JSON
        result = {"accepted": False, "reason": "unexpected response from the Node"}
    accepted = result.get("accepted", True)
    # A duplicate is a benign, idempotent no-op — not a failure.
    duplicate = (not accepted) and "duplicate" in str(result.get("reason") or "")
    cognified = result.get("cognified")  # True/False from the Node, or None (not requested / old Node)
    color = supports_color()
    dataset = result.get("dataset") or "your node"
    scope = "your private seat" if str(dataset).startswith("seat:") else "shared org vault"

    if as_json:
        _print_json(result)
        return 0 if (accepted or duplicate) else 1

    if accepted:
        print(
            f"  {mark(True, enable=color)} ingested to {paint(dataset, 'cyan', enable=color)}  "
            f"{paint('(' + scope + ')', 'dim', enable=color)}"
        )
        if cognified is True:
            print(f"  {mark(True, enable=color)} cognified — now searchable")
        elif cognified is False:
            print(f"  {paint(SKIP, 'yellow', enable=color)} ingested, but cognify didn't finish — the next Node sync will pick it up")
        elif cognify:
            # Requested cognify but the Node didn't report it (older Node, pre inline-cognify).
            print(paint("  (graph update will happen on the next Node sync)", "dim", enable=color))
    elif duplicate:
        print(
            f"  {paint(SKIP, 'yellow', enable=color)} already in your vault (duplicate) — nothing new to add"
        )
    else:
        reason = str(result.get("reason") or "rejected").replace("_", " ")
        print(f"  {mark(False, enable=color)} not accepted: {reason}", file=sys.stderr)
    return 0 if (accepted or duplicate) else 1


@_needs_server
async def _ingest_local(args: argparse.Namespace) -> int:
    from kb.service import Citadel

    kb = Citadel.from_env()
    result = await kb.ingest(
        args.data,
        dataset=args.dataset,
        tags=args.tag,
        session_id=args.session,
    )
    _print_json(result.__dict__)
    return _result_exit(result)


_SEARCH_SECTION_LABELS = {
    "central": "Central",
    "session_traces": "Session traces (reference-only — verify before acting)",
    "node": "Node",
}
_SEARCH_SECTION_ORDER = ("central", "session_traces", "node")


def _search_item_text(item: Any) -> str:
    if isinstance(item, dict):
        text = (
            item.get("text") or item.get("content") or item.get("summary")
            or item.get("title") or item.get("name") or json.dumps(item, default=str)
        )
    else:
        text = str(item)
    text = " ".join(str(text).split())
    return text[:300] + ("…" if len(text) > 300 else "")


def _search_item_meta(item: dict[str, Any], *, color: bool) -> str:
    envelope = item.get("_citadel")
    if not isinstance(envelope, dict):
        return ""
    parts: list[str] = []
    trust = envelope.get("trust")
    if trust:
        parts.append(f"trust: {trust}")
    author = envelope.get("author_seat")
    if author:
        parts.append(f"author: {author}")
    if not parts:
        return ""
    return paint(f"({' · '.join(parts)}) ", "yellow", enable=color)


def _render_search(payload: dict[str, Any], query: str) -> None:
    color = supports_color()
    results = payload.get("results") or []
    if not isinstance(results, list):
        results = []
    if not results:
        print(paint(f'No results for "{query}".', "dim", enable=color))
        return
    print(f'{len(results)} result(s) for "{query}":\n')
    sections = payload.get("sections")
    if isinstance(sections, dict) and any(sections.get(key) for key in _SEARCH_SECTION_ORDER):
        index = 1
        for key in _SEARCH_SECTION_ORDER:
            items = sections.get(key) or []
            if not items:
                continue
            print(paint(_SEARCH_SECTION_LABELS[key], "bold", enable=color))
            for item in items:
                meta = _search_item_meta(item, color=color) if isinstance(item, dict) else ""
                print(f"  {paint(f'{index}.', 'cyan', enable=color)} {meta}{_search_item_text(item)}")
                index += 1
            print()
        return
    for index, item in enumerate(results, 1):
        meta = _search_item_meta(item, color=color) if isinstance(item, dict) else ""
        print(f"  {paint(f'{index}.', 'cyan', enable=color)} {meta}{_search_item_text(item)}")


def _search_type_filters(args: argparse.Namespace) -> list[str] | None:
    raw = getattr(args, "type", None)
    if not raw:
        return None
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _search_timeout_seconds(args: argparse.Namespace) -> float:
    """Resolve ``--budget-ms`` / ``--timeout`` (budget-ms wins when both set)."""
    from kb.status import _SEARCH_TIMEOUT

    budget_ms = getattr(args, "budget_ms", None)
    if budget_ms is not None:
        return max(0.5, float(budget_ms) / 1000.0)
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        return max(0.5, float(timeout))
    return float(_SEARCH_TIMEOUT)


def _shape_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    mode = getattr(args, "mode", None)
    exclude_ambient = bool(getattr(args, "exclude_ambient", False))
    if isinstance(mode, str) and mode.strip().lower() == "docs":
        exclude_ambient = True
    return {
        "query": args.query,
        "types": _search_type_filters(args),
        "repo": getattr(args, "repo", None),
        "path": getattr(args, "path", None),
        "canonical_only": bool(getattr(args, "canonical_only", False)),
        "exclude_ambient": exclude_ambient,
        "mode": mode.strip().lower() if isinstance(mode, str) and mode.strip() else None,
    }


def _emit_search_timeout(
    args: argparse.Namespace,
    *,
    note: str,
    partial: dict[str, Any] | None = None,
) -> int:
    """Prefer truncated JSON (optionally with partial hits) over a hard agent fail."""
    from kb.search_format import shape_search_payload

    empty: dict[str, Any] = {
        "query": args.query,
        "results": (partial or {}).get("results") or [],
        "timed_out": True,
        "truncated": True,
        "note": note or "search timed out",
    }
    if isinstance(partial, dict):
        for key in ("sections", "dataset", "datasets", "took_ms"):
            if key in partial:
                empty[key] = partial[key]
    shaped = shape_search_payload(empty, **_shape_kwargs(args))
    if getattr(args, "json", False):
        _print_json(shaped)
        return 0
    print("citadel search: timed out — returning truncated results", file=sys.stderr)
    if shaped.get("results"):
        _render_search({**empty, "results": shaped["results"], "sections": None}, args.query)
    return 0


def _is_timeout_exc(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return "timed out" in str(exc).lower()


def _reject_local_only_flags(args: argparse.Namespace, prog: str) -> None:
    """`--dataset`/`--session` only apply to the in-process `--local` stack.

    On the default HTTP path the Node picks the dataset from the seat token, so
    these flags were silently ignored — a scoped search quietly returned
    everything. Reject them (exit 2) instead of dropping them, matching how the
    CLI already errors on a missing query or a bad `--top-k`.
    """
    if getattr(args, "local", False):
        return
    offending = [
        flag
        for flag, attr in (("--dataset", "dataset"), ("--session", "session"))
        if getattr(args, attr, None)
    ]
    if not offending:
        return
    color = supports_color(sys.stderr)
    joined = ", ".join(offending)
    verb = "requires" if len(offending) == 1 else "require"
    sys.stderr.write(
        "\n".join(
            [
                paint(f"✗ citadel {prog}: {joined} {verb} --local", "red", enable=color),
                f"  the Node scopes {prog} by your seat token; pass --local to target a "
                "specific dataset/session.",
                "  run " + paint(f"citadel {prog} --help", "cyan", enable=color) + " for details.",
            ]
        )
        + "\n"
    )
    raise SystemExit(2)


async def _search(args: argparse.Namespace) -> int:
    """Search the Organization Vault over HTTP (the Node), like MCP citadel_search.

    Zero-dep: hits the Node's /search with the seat token, so base-install
    teammates can query the same vault their agent sees. `--local` runs the
    in-process server stack instead (needs the [server] extra).
    """
    if getattr(args, "local", False):
        return await _search_local(args)
    _reject_local_only_flags(args, "search")
    base_url = node_base_url(getattr(args, "node_url", None))
    token = capture_token()
    if not token:
        return _emit_no_token("search", as_json=getattr(args, "json", False))
    from kb.search_format import apply_query_ranking, is_docs_mode_query, is_spec_mode_query, shape_search_payload
    from kb.status import search_node

    top_k = getattr(args, "top_k", None)
    if top_k is None:
        top_k = 10
    timeout = _search_timeout_seconds(args)
    shape_kw = _shape_kwargs(args)
    filters_on = bool(
        shape_kw["types"]
        or shape_kw["repo"]
        or shape_kw["path"]
        or shape_kw["canonical_only"]
        or shape_kw["exclude_ambient"]
        or shape_kw.get("mode")
    )

    try:
        with _Spinner("Searching the vault…"):
            payload = await asyncio.to_thread(
                search_node,
                base_url,
                token,
                args.query,
                top_k,
                timeout=timeout,
                types=shape_kw["types"],
                repo=shape_kw["repo"],
                path=shape_kw["path"],
                canonical_only=shape_kw["canonical_only"],
                exclude_ambient=shape_kw["exclude_ambient"],
                mode=shape_kw.get("mode"),
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200] if exc.fp else exc.reason
        as_json = getattr(args, "json", False)
        if not as_json:
            _print_auth_hint("search", exc.code)
        return _emit_error(
            "search",
            f"HTTP {exc.code} {detail}",
            as_json=as_json,
            code="HTTP_ERROR",
            extra={"http_status": exc.code},
        )
    except (TimeoutError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # TimeoutError and urllib URLError("timed out") share one soft-fail path.
        if _is_timeout_exc(exc):
            return _emit_search_timeout(args, note=str(exc) or "search timed out")
        return _emit_error(
            "search", str(exc), as_json=getattr(args, "json", False), code="NODE_UNREACHABLE"
        )

    raw = payload if isinstance(payload, dict) else {"results": []}
    # Server may already mark soft timeout with partial/empty results.
    if raw.get("timed_out") or raw.get("truncated"):
        shaped = shape_search_payload(raw, **shape_kw)
        if getattr(args, "json", False):
            _print_json(shaped)
            return 0
        for warning in shaped.get("warnings") or []:
            print(paint(f"warning: {warning}", "yellow"), file=sys.stderr)
        display_results = shaped["results"] if filters_on or shaped.get("spec_mode") else raw.get("results")
        _render_search({**raw, "results": display_results or [], "sections": None}, args.query)
        return 0

    shaped = shape_search_payload(raw, **shape_kw)
    if getattr(args, "json", False):
        _print_json(shaped)
        return 0

    # Human view: always apply filters; for API/spec or docs/token queries also
    # flatten to ranked results so section grouping cannot hide boosts.
    display = dict(raw)
    if filters_on:
        display = {**display, "results": shaped["results"], "sections": None}
    elif is_docs_mode_query(args.query, mode=shape_kw.get("mode")) or is_spec_mode_query(args.query):
        ranked = apply_query_ranking(
            list(raw.get("results") or []), args.query, mode=shape_kw.get("mode")
        )
        display = {**display, "results": ranked, "sections": None}
    _render_search(display, args.query)
    if shaped.get("warnings"):
        for warning in shaped["warnings"]:
            print(paint(f"warning: {warning}", "yellow"), file=sys.stderr)
    return 0


@_needs_server
async def _search_local(args: argparse.Namespace) -> int:
    from kb.agent_workflows import normalize_local_search_results
    from kb.search_format import shape_search_payload
    from kb.service import Citadel

    kb = Citadel.from_env()
    top_k = getattr(args, "top_k", None) or 10
    try:
        results = await kb.search(
            args.query,
            dataset=args.dataset,
            session_id=args.session,
            top_k=top_k,
        )
    except TimeoutError as exc:
        return _emit_search_timeout(args, note=str(exc) or "local search timed out")

    payload = normalize_local_search_results(results)
    shaped = shape_search_payload(payload, **_shape_kwargs(args))
    _print_json(shaped)
    return 0 if shaped.get("ok") else 1


async def _verify(args: argparse.Namespace) -> int:
    """MVP: compare a local skill/reference file against nearest vault canonicals."""
    from kb.agent_workflows import build_verify_query, shape_verify_report
    from kb.status import search_node

    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"citadel verify: file not found: {path}", file=sys.stderr)
        return 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"citadel verify: {exc}", file=sys.stderr)
        return 1

    query = getattr(args, "query", None) or build_verify_query(path, text)
    top_k = getattr(args, "top_k", None) or 12
    base_url = node_base_url(getattr(args, "node_url", None))
    token = capture_token()
    if not token:
        return _emit_no_token("verify", as_json=True)

    try:
        with _Spinner("Verifying against the vault…"):
            payload = await asyncio.to_thread(
                search_node,
                base_url,
                token,
                query,
                top_k,
                timeout=_search_timeout_seconds(args),
            )
    except (TimeoutError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        if _is_timeout_exc(exc):
            report = shape_verify_report(
                path=path,
                file_text=text,
                search_payload={
                    "query": query,
                    "results": [],
                    "timed_out": True,
                    "truncated": True,
                    "note": str(exc),
                },
                query=query,
            )
            _print_json(report)
            return 0
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read().decode(errors="replace")[:200] if exc.fp else exc.reason
            print(f"citadel verify: HTTP {exc.code} {detail}", file=sys.stderr)
            _print_auth_hint("verify", exc.code)
            return 1
        print(f"citadel verify: {exc}", file=sys.stderr)
        return 1

    report = shape_verify_report(
        path=path,
        file_text=text,
        search_payload=payload if isinstance(payload, dict) else {"results": []},
        query=query,
    )
    _print_json(report)
    return 0


async def _prepare_pr_context(args: argparse.Namespace) -> int:
    """Thin PR brief: ranked vault hits for a repo + topic (JSON for agents)."""
    from kb.agent_workflows import shape_prepare_pr_context
    from kb.status import search_node

    repo = str(args.repo).strip()
    topic = str(args.topic).strip()
    if not repo or not topic:
        print("citadel prepare-pr-context: --repo and --topic are required", file=sys.stderr)
        return 1

    query = f"{topic} {repo} schema endpoint MIP"
    top_k = getattr(args, "top_k", None) or 12
    base_url = node_base_url(getattr(args, "node_url", None))
    token = capture_token()
    if not token:
        return _emit_no_token("prepare-pr-context", as_json=True)

    try:
        with _Spinner("Preparing PR context…"):
            payload = await asyncio.to_thread(
                search_node,
                base_url,
                token,
                query,
                top_k,
                timeout=_search_timeout_seconds(args),
            )
    except (TimeoutError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        if _is_timeout_exc(exc):
            brief = shape_prepare_pr_context(
                repo=repo,
                topic=topic,
                search_payload={
                    "query": query,
                    "results": [],
                    "timed_out": True,
                    "truncated": True,
                    "note": str(exc),
                },
            )
            _print_json(brief)
            return 0
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read().decode(errors="replace")[:200] if exc.fp else exc.reason
            print(f"citadel prepare-pr-context: HTTP {exc.code} {detail}", file=sys.stderr)
            _print_auth_hint("prepare-pr-context", exc.code)
            return 1
        print(f"citadel prepare-pr-context: {exc}", file=sys.stderr)
        return 1

    brief = shape_prepare_pr_context(
        repo=repo,
        topic=topic,
        search_payload=payload if isinstance(payload, dict) else {"results": []},
    )
    _print_json(brief)
    return 0


@_needs_server
async def _feedback(args: argparse.Namespace) -> None:
    from kb.models import FeedbackRequest
    from kb.service import Citadel

    kb = Citadel.from_env()
    result = await kb.feedback(
        FeedbackRequest(
            qa_id=args.qa_id,
            score=args.score,
            text=args.text,
            session_id=args.session,
            dataset=args.dataset,
        )
    )
    _print_json(result.__dict__)
    return _result_exit(result)


@_needs_server
async def _improve(args: argparse.Namespace) -> None:
    from kb.service import Citadel

    kb = Citadel.from_env()
    result = await kb.improve(dataset=args.dataset, session_ids=args.session_id)
    _print_json(result)
    return _result_exit(result)


@_needs_server
async def _cognify(args: argparse.Namespace) -> None:
    from kb.service import Citadel

    kb = Citadel.from_env()
    result = await kb.cognify_dataset(dataset=args.dataset, verify=args.verify)
    _print_json(result)
    return _result_exit(result)


@_needs_server
async def _sync_github(args: argparse.Namespace) -> None:
    from kb.github_sync import GitHubOrgSyncer
    from kb.service import Citadel

    syncer = GitHubOrgSyncer(
        Citadel.from_env(),
        org=args.org,
        state_path=args.state_path,
        max_repos=args.max_repos,
        max_events=args.max_events,
        max_commits_per_repo=args.max_commits_per_repo,
        include_commits=not args.skip_commits,
        ingest_unchanged=not args.skip_unchanged,
        run_improve=not args.skip_improve,
    )
    result = await syncer.run(force=args.force, dry_run=args.dry_run)
    _print_json(result)
    return _result_exit(result)


@_needs_server
async def _sync_repo_content(args: argparse.Namespace) -> None:
    from kb.repo_content_sync import RepoContentSyncer
    from kb.service import Citadel

    syncer = RepoContentSyncer(Citadel.from_env())
    result = await syncer.run(force=args.force, dry_run=args.dry_run)
    _print_json(result)
    return _result_exit(result)


@_needs_server
async def _learn(args: argparse.Namespace) -> None:
    from kb.learning_agent import LearningAgent

    agent = LearningAgent.from_env()
    result = await agent.status() if args.status else await agent.run(
        force=args.force,
        dry_run=args.dry_run,
        post_to_chat=args.post_to_chat,
        include_digest_preview=not args.hide_digest_preview,
    )
    _print_json(result)
    return _result_exit(result)


def _parse_root_arg(raw: str) -> tuple[str, tuple[str, ...]]:
    """Parse a non-interactive ``--root`` value: ``PATH`` or ``PATH=tag1,tag2``."""
    path, sep, tags_str = raw.partition("=")
    tags = tuple(tags_str.split(",")) if sep else ()
    return path, tags


def _ask_root_tags() -> tuple[str, ...]:
    tags_raw = _prompt(f"  Tags [{DEFAULT_ROOT_TAG}]: ").strip()
    return tuple(tags_raw.split(",")) if tags_raw else (DEFAULT_ROOT_TAG,)


def _wizard_roots(config: CaptureConfig, default_root: str | None = None) -> CaptureConfig:
    print("\nApproved Capture Roots — folders auto-captured to your Node.")
    # Tag semantics explained once up front, so the per-root Tags prompt can
    # stay a one-liner with a press-Enter default.
    print(f"  Tag presets: {', '.join(PRESET_ROOT_TAGS)} — 'personal' never promotes.")
    # The dir the user ran `citadel` from is offered as an explicit yes/no —
    # Enter accepts, `n` declines. A separate question (not a prefilled path
    # prompt) so declining is always possible and "empty to finish" below
    # keeps meaning finish.
    default = normalize_path(default_root) if default_root else None
    if default and any(root.path == default for root in config.roots):
        default = None  # already approved — nothing to offer
    if default:
        answer = _prompt(f"  Add {default} (this folder)? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            tags = _ask_root_tags()
            config = config.with_root(default, tags)
            print(f"  + {default}  [{', '.join(normalize_tags(tags))}]")
    while True:
        raw = _prompt("  Root path (empty to finish): ").strip()
        if not raw:
            break
        normalized = normalize_path(raw)
        if not Path(normalized).exists():
            # A leading-slash typo for a home dir ("/masumi" for ~/masumi) is the
            # common miss — offer the home-relative match instead of silently
            # recording a root that will never capture anything.
            guess = Path.home() / raw.lstrip("/") if raw.startswith("/") else None
            if guess is not None and guess.exists():
                answer = _prompt(
                    f"  ! {normalized} does not exist — did you mean {guess}? [Y/n]: "
                ).strip().lower()
                if answer in ("", "y", "yes"):
                    normalized = normalize_path(str(guess))
                else:
                    print(f"  ! keeping {normalized} (does not exist yet)")
            else:
                print(f"  ! {normalized} does not exist yet (added anyway)")
        tags = _ask_root_tags()
        config = config.with_root(normalized, tags)
        print(f"  + {normalized}  [{', '.join(normalize_tags(tags))}]")
    return config


def _load_config_or_exit(args: argparse.Namespace, command: str) -> CaptureConfig | None:
    """Load config, printing a clean error (and signalling exit 1) on corruption."""
    config_path = Path(args.config).expanduser() if args.config else capture_config_path()
    try:
        return load_capture_config(config_path)
    except ValueError as exc:
        print(f"citadel {command}: {exc}", file=sys.stderr)
        return None


async def _setup(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else capture_config_path()
    config = _load_config_or_exit(args, "setup")
    if config is None:
        return 1

    if args.show:
        _print_json(config.to_dict())
        return 0

    node_url = (args.node_url or config.node_url or DEFAULT_NODE_URL).rstrip("/")
    interactive = (
        not args.non_interactive
        and not args.root
        and not getattr(args, "json", False)
        and sys.stdin.isatty()
    )

    if interactive:
        prompt = f"Node URL [{node_url}]: "
        node_url = (input(prompt).strip() or node_url).rstrip("/")

    config = CaptureConfig(node_url=node_url, roots=config.roots, version=config.version)

    if args.root:
        for raw in args.root:
            path, tags = _parse_root_arg(raw)
            config = config.with_root(path, tags or (DEFAULT_ROOT_TAG,))
    elif interactive:
        config = _wizard_roots(config, default_root=str(Path.cwd()))

    written = save_capture_config(
        config, path=config_path, updated_at=datetime.now(timezone.utc).isoformat()
    )
    sync_result = sync_local_capture_roots_to_server(config) if config.roots else None
    warning = sync_warning_message(sync_result) if sync_result else None
    if getattr(args, "json", False):
        payload = load_capture_config(config_path).to_dict()
        if sync_result is not None:
            payload["capture_roots_sync"] = {
                "status": sync_result.status,
                "detail": sync_result.detail,
                "seat_slug": sync_result.seat_slug,
            }
        _print_json(payload)
    else:
        print(f"\nSaved {len(config.roots)} approved root(s) to {written}")
        if warning:
            print(f"Warning: {warning}", file=sys.stderr)
    return 0


async def _capture(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args, "capture")
    if config is None:
        return 1

    if not config.roots:
        print("No approved roots to capture. Run `citadel setup` first.", file=sys.stderr)
        return 1

    roots = config.roots
    if args.root:
        wanted = {normalize_path(raw) for raw in args.root}
        roots = tuple(root for root in config.roots if root.path in wanted)
        if not roots:
            print(f"No configured root matches: {', '.join(args.root)}", file=sys.stderr)
            return 1

    payloads = [(root, build_capture_payload(root)) for root in roots]

    if args.dry_run:
        _print_json(
            [
                {
                    "root": root.path,
                    "tags": payload["tags"],
                    "chars": len(payload["data"]),
                    "preview": payload["data"][:500],
                }
                for root, payload in payloads
            ]
        )
        return 0

    token = capture_token()
    if not token:
        return _emit_no_token("capture", as_json=getattr(args, "json", False))

    as_json = getattr(args, "json", False)
    results: list[dict[str, Any]] = []
    failures = 0
    auth_fail_code = 0
    for root, payload in payloads:
        try:
            response = post_capture(config.node_url, token, payload)
            status = response.get("cognee_result", {}).get("status") or response.get("status")
            results.append({"root": root.path, "ok": True, "status": status, "tags": payload["tags"]})
            if not as_json:
                print(f"OK  {root.path} ({status})")
        except urllib.error.HTTPError as exc:
            failures += 1
            auth_fail_code = auth_fail_code or exc.code
            detail = exc.read().decode(errors="replace")[:200]
            results.append({"root": root.path, "ok": False, "error": f"HTTP {exc.code} {detail}"})
            if not as_json:
                print(f"FAIL {root.path}: HTTP {exc.code} {detail}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Node unreachable / DNS / timeout / non-HTTPS URL — isolate per root.
            failures += 1
            results.append({"root": root.path, "ok": False, "error": str(exc)})
            if not as_json:
                print(f"FAIL {root.path}: {exc}", file=sys.stderr)
    if not as_json and auth_fail_code:
        _print_auth_hint("capture", auth_fail_code)  # once, not per failing root
    if as_json:
        _print_json({"ok": failures == 0, "results": results})
    # Human mode already printed a per-root OK/FAIL line during the loop.
    return 1 if failures else 0


def _promotion_base_url(args: argparse.Namespace) -> str:
    return node_base_url(getattr(args, "node_url", None))


def _promotion_exit(exc: PromotionClientError, *, as_json: bool) -> int:
    if as_json:
        _print_json({"ok": False, "error": str(exc), "status": exc.status, "body": exc.body})
    else:
        print(f"citadel promotion: {exc}", file=sys.stderr)
    return 1


@_needs_server
async def _promotion_list(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = list_pending(
            base_url=_promotion_base_url(args),
            status=args.status,
        )
    except PromotionClientError as exc:
        return _promotion_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
    else:
        items = result.get("items") or []
        print(f"Promotion queue ({result.get('status', args.status)}): {len(items)} item(s)")
        for item in items:
            preview = item.get("preview") or "(no preview)"
            print(
                f"  {item.get('id')}  seat={item.get('seat_slug')}  "
                f"ref={item.get('reference_status')}  {preview}"
            )
    return 0


@_needs_server
async def _promotion_approve(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = approve_pending(
            args.item_id,
            base_url=_promotion_base_url(args),
            note=args.note,
        )
    except PromotionClientError as exc:
        return _promotion_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
    else:
        promoted = result.get("promoted")
        print(f"Approved {args.item_id} (promoted={promoted})")
    return 0 if result.get("ok") else 1


@_needs_server
async def _promotion_reject(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = reject_pending(
            args.item_id,
            base_url=_promotion_base_url(args),
            note=args.note,
        )
    except PromotionClientError as exc:
        return _promotion_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
    else:
        print(f"Rejected {args.item_id}")
    return 0 if result.get("ok") else 1


@_needs_server
async def _promotion_run(args: argparse.Namespace) -> int:
    as_json = args.json
    dry_run = not args.execute
    try:
        result = run_promotion(
            base_url=_promotion_base_url(args),
            dataset=args.dataset,
            dry_run=dry_run,
            max_items=args.max_items,
        )
    except PromotionClientError as exc:
        return _promotion_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
    else:
        mode = "dry-run" if dry_run else "execute"
        print(
            f"Promotion {mode} on {result.get('dataset')}: "
            f"candidates={result.get('candidates')} "
            f"proposed={result.get('proposed')} "
            f"promoted={result.get('promoted')} "
            f"queued={result.get('queued')}"
        )
    return 0 if result.get("ok") else 1


def _access_exit(exc: AccessClientError, *, as_json: bool) -> int:
    if as_json:
        _print_json({"ok": False, "error": str(exc), "status": exc.status, "body": exc.body})
    else:
        print(f"citadel: {exc}", file=sys.stderr)
    return 1


def _print_minted_token(token: str, api_token: dict[str, Any], *, color: bool) -> None:
    """Print a freshly minted token once, with its write-scope + adopt steps."""
    print()
    print(paint("  Token (shown once — copy it now, it cannot be retrieved later):", "yellow", enable=color))
    print("    " + paint(token, "bold", enable=color))
    dataset = api_token.get("default_dataset")
    if dataset and str(dataset).startswith("seat:"):
        scope = f"ingests go to {dataset} (their private seat) only"
    elif dataset:
        scope = f"ingests go to {dataset}"
    else:
        scope = "ingests go to the org default dataset — NOT a private seat"
    print(paint(f"  scope: {scope}  ·  role={api_token.get('role')}", "dim", enable=color))
    print(paint("  Adopt:  citadel onboard --token <token-above>   ·   share over a private channel", "dim", enable=color))


async def _seat_list(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = list_seats(base_url=node_base_url(args.node_url))
    except AccessClientError as exc:
        return _access_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
        return 0
    color = supports_color()
    seats = result.get("seats") or []
    print(f"Seats ({len(seats)}):")
    for seat in seats:
        disabled = paint("  [disabled]", "red", enable=color) if seat.get("disabled") else ""
        slug = paint(str(seat.get("seat_slug") or "—"), "cyan", enable=color)
        print(
            f"  {slug}  {seat.get('name', '')}  role={seat.get('role')}  "
            f"tokens={seat.get('active_token_count', 0)}{disabled}"
        )
    return 0


async def _seat_create(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = create_seat(
            base_url=node_base_url(args.node_url),
            name=args.name,
            slug=args.slug,
            email=args.email,
            role=args.role,
            issue_token=not args.no_token,
        )
    except AccessClientError as exc:
        return _access_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
        return 0
    color = supports_color()
    principal = result.get("principal", {})
    print(
        paint(
            f"Seat created: {principal.get('seat_slug')}  (dataset {principal.get('default_dataset')})",
            "green",
            enable=color,
        )
    )
    token = result.get("token")
    if token:
        _print_minted_token(token, result.get("api_token", {}), color=color)
    else:
        print("  (no token issued — re-run without --no-token to mint one)")
    return 0


async def _seat_token(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = issue_seat_token(args.slug, base_url=node_base_url(args.node_url))
    except AccessClientError as exc:
        return _access_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
        return 0
    color = supports_color()
    principal = result.get("principal", {})
    print(
        paint(
            f"New token for seat {principal.get('seat_slug')}  (dataset {principal.get('default_dataset')})",
            "green",
            enable=color,
        )
    )
    if result.get("token"):
        _print_minted_token(result["token"], result.get("api_token", {}), color=color)
    return 0


class _PickerAborted(Exception):
    """The user cancelled the interactive seat picker (empty answer / EOF)."""


def _active_seats(base_url: str) -> list[dict[str, Any]]:
    result = list_seats(base_url=base_url)
    return [seat for seat in (result.get("seats") or []) if not seat.get("disabled")]


def _pick_seat(seats: list[dict[str, Any]]) -> str | None:
    """Interactive seat picker for `token create` — a slug, or None for standalone."""
    color = supports_color()
    print("Assign this token to a seat (its writes land in that seat's private node):")
    for index, seat in enumerate(seats, start=1):
        slug = paint(str(seat.get("seat_slug")), "cyan", enable=color)
        print(f"  {index}) {slug}  {seat.get('name', '')}  role={seat.get('role')}")
    print("  0) standalone service-account token (no seat)")
    while True:
        answer = _prompt(f"Select [0-{len(seats)}]: ").strip()
        if not answer:
            raise _PickerAborted
        if answer.isdigit() and int(answer) <= len(seats):
            choice = int(answer)
            return None if choice == 0 else str(seats[choice - 1].get("seat_slug"))
        print(f"  enter a number between 0 and {len(seats)} (empty to cancel)")


def _print_seat_token_result(result: dict[str, Any]) -> None:
    color = supports_color()
    principal = result.get("principal", {})
    print(
        paint(
            f"New token for seat {principal.get('seat_slug')}  (dataset {principal.get('default_dataset')})",
            "green",
            enable=color,
        )
    )
    if result.get("token"):
        _print_minted_token(result["token"], result.get("api_token", {}), color=color)


async def _token_create(args: argparse.Namespace) -> int:
    as_json = args.json
    base_url = node_base_url(args.node_url)
    seat_slug = args.seat
    dataset = args.dataset

    # Explicit-but-blank values (--seat "$SLUG" with the var unset) must not read
    # as "flag omitted" — that would silently mint a standalone token with exit 0.
    if seat_slug is not None and not seat_slug.strip():
        print("citadel token create: --seat needs a seat slug.", file=sys.stderr)
        return 2
    if dataset is not None and not dataset.strip():
        print("citadel token create: --dataset needs a dataset name.", file=sys.stderr)
        return 2
    if seat_slug and dataset:
        print("citadel token create: choose --seat or --dataset, not both.", file=sys.stderr)
        return 2
    if seat_slug and (args.role or args.kind or args.expires_at):
        print(
            "citadel token create: seat tokens inherit the seat's role — "
            "--role/--kind/--expires-at only apply to standalone tokens.",
            file=sys.stderr,
        )
        return 2

    try:
        # Interactive picker: TTY, human output, and no explicit target given.
        # --role/--kind/--expires-at signal standalone intent (and minted
        # immediately before seat binding existed) — never route them into the
        # picker, where a picked seat would silently drop them.
        if (
            not seat_slug
            and not dataset
            and not (args.role or args.kind or args.expires_at)
            and not as_json
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        ):
            try:
                seat_slug = _pick_seat(_active_seats(base_url))
            except _PickerAborted:
                print("citadel token create: cancelled.", file=sys.stderr)
                return 1

        if seat_slug:
            known = {seat.get("seat_slug") for seat in _active_seats(base_url)}
            if seat_slug not in known:
                listing = ", ".join(sorted(str(slug) for slug in known)) or "none yet — `citadel seat create`"
                print(
                    f"citadel token create: no seat '{seat_slug}'. Seats: {listing}",
                    file=sys.stderr,
                )
                return 1
            result = issue_seat_token(seat_slug, base_url=base_url, token_name=args.name)
            if as_json:
                _print_json(result)
            else:
                _print_seat_token_result(result)
            return 0

        if dataset:
            # The seat: namespace is private memory — seat tokens must come from
            # --seat so they carry the seat identity and allowlist, not a bare
            # default_dataset that the Node will 403.
            slug = dataset.removeprefix("seat:")
            known = {seat.get("seat_slug") for seat in _active_seats(base_url)}
            if dataset.startswith("seat:") or slug in known:
                if slug in known:
                    print(
                        f"citadel token create: '{dataset}' is a seat — mint a seat-bound token: "
                        f"citadel token create \"{args.name}\" --seat {slug}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"citadel token create: no seat '{slug}' — create it first: "
                        f"citadel seat create \"Name\" {slug}",
                        file=sys.stderr,
                    )
                return 1

        result = create_token(
            base_url=base_url,
            name=args.name,
            role=args.role or "reader",
            kind=args.kind or "service_account",
            default_dataset=dataset,
            expires_at=args.expires_at,
        )
    except AccessClientError as exc:
        return _access_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
        return 0
    color = supports_color()
    print(paint(f"Token created  (principal {result.get('principal', {}).get('id')})", "green", enable=color))
    if result.get("token"):
        _print_minted_token(result["token"], result.get("api_token", {}), color=color)
    # Standalone tokens are NOT seat-scoped — steer teammate tokens to seats.
    api_token = result.get("api_token", {})
    if not str(api_token.get("default_dataset") or "").startswith("seat:"):
        print(
            paint(
                "  Note: this is a standalone token (not a private seat). For a teammate, use "
                "`citadel seat create \"Name\" slug` so their writes land in their own seat.",
                "yellow",
                enable=color,
            )
        )
    return 0


async def _token_revoke(args: argparse.Namespace) -> int:
    as_json = args.json
    try:
        result = revoke_token(args.token_id, base_url=node_base_url(args.node_url))
    except AccessClientError as exc:
        return _access_exit(exc, as_json=as_json)
    if as_json:
        _print_json(result)
        return 0
    print(f"Revoked {args.token_id}")
    return 0 if result.get("ok") else 1


async def _token_set(args: argparse.Namespace) -> int:
    """Set/rotate the seat token this machine uses — verify it, then write the rc.

    The teammate-facing counterpart to the admin mint commands: paste a new
    token (e.g. after `citadel seat token <slug>`) without re-running the whole
    onboard flow. A token the Node rejects is NOT written (--skip-verify
    overrides), so rotation can't silently break a working setup.
    """
    color = supports_color()
    node_url = node_base_url(getattr(args, "node_url", None))
    rc_path = Path(args.shell_rc).expanduser() if args.shell_rc else detect_shell_rc()

    token = (args.token or "").strip()
    if not token:
        if not sys.stdin.isatty():
            print("citadel token set: pass the token as an argument (no TTY to prompt on).", file=sys.stderr)
            return 2
        token = _prompt_hidden("Paste the new seat token (ctdl_…): ")
    if not token:
        print("citadel token set: no token given.", file=sys.stderr)
        return 1

    if not getattr(args, "skip_verify", False):
        from kb.status import check_auth

        with _Spinner("Verifying the token against your Node…"):
            auth = await asyncio.to_thread(check_auth, node_url, token)
        if not auth.ok:
            print(
                f"  {mark(False, enable=color)} token not verified ({auth.detail}) — nothing written.",
                file=sys.stderr,
            )
            print(paint("  (pass --skip-verify to write it anyway)", "dim", enable=color), file=sys.stderr)
            return 1
        print()
        print(_render_identity(auth, node_url, color))
        print()

    previous = read_token_from_rc(rc_path)
    status = ensure_token_in_rc(rc_path, token)
    detail = status + (f" · replaced {mask_token(previous)}" if previous and previous != token else "")
    print(f"  {mark(True, enable=color)} token {mask_token(token)} → {rc_path}  {paint(detail, 'dim', enable=color)}")
    if (os.environ.get(TOKEN_ENV) or "").strip() != token:
        print(
            paint(f"  ! this shell still has the old value — run `source {rc_path}` or open a new shell.", "yellow", enable=color)
        )
    return 0


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _wire_detected_tools(node_url: str, *, color: bool) -> None:
    """Interactive: one checkbox list of detected tools, then wire the selection.

    Write-tier tools (token stays in the rc via an env reference) are merged;
    snippet-tier tools print a paste-in block; Pi gets a note. Preselection
    mirrors the old per-tool defaults: write-tier on, snippet-tier off. Used
    only on an interactive `citadel onboard`.
    """
    from kb import tool_detect
    from kb.prompt import checkbox_select

    detected = tool_detect.detect()
    if not detected:
        return
    selectable = [name for name in detected if tool_detect.SPECS[name].mode != "note"]
    notes = [name for name in detected if tool_detect.SPECS[name].mode == "note"]

    chosen: list[str] = []
    if selectable:
        labels = [
            tool_detect.SPECS[name].label
            + (paint("  (paste-in snippet)", "dim", enable=color) if tool_detect.SPECS[name].mode == "snippet" else "")
            for name in selectable
        ]
        preselected = {i for i, name in enumerate(selectable) if tool_detect.SPECS[name].mode == "write"}
        print()
        picked = checkbox_select(
            paint("Coding tools", "bold", enable=color)
            + paint(" — add Citadel MCP to:", "dim", enable=color),
            labels,
            preselected,
        )
        chosen = [selectable[i] for i in sorted(picked)] if picked else []

    results: list[tuple[str, Any]] = []
    if chosen:
        with _Spinner(f"Wiring {len(chosen)} tool(s)…"):
            results = [(name, tool_detect.apply(name, node_url=node_url)) for name in chosen]
    for name, result in results:
        spec = tool_detect.SPECS[name]
        if spec.mode == "write":
            sigil = mark(result.action != "error", enable=color)
            print(f"  {sigil} {spec.label}  {paint(f'{result.action} · {result.detail}', 'dim', enable=color)}")
        else:  # snippet
            print(f"  {paint(spec.label, 'bold', enable=color)} — paste into {paint(spec.config_hint, 'dim', enable=color)}:")
            print(_indent(result.snippet or ""))
    for name in notes:
        result = tool_detect.apply(name, node_url=node_url)
        spec = tool_detect.SPECS[name]
        print(f"  {paint('•', 'dim', enable=color)} {spec.label}: {paint(result.detail, 'dim', enable=color)}")


async def _mcp_add(args: argparse.Namespace) -> int:
    from kb import tool_detect

    node_url = node_base_url(args.node_url)
    color = supports_color()
    targets = tool_detect.ALL_TOOLS if args.tool == "all" else [args.tool]
    rc = 0
    for name in targets:
        if name not in tool_detect.SPECS:
            print(f"citadel mcp add: unknown tool {name!r} "
                  f"(choose from: {', '.join(tool_detect.ALL_TOOLS)}, all)", file=sys.stderr)
            rc = 1
            continue
        spec = tool_detect.SPECS[name]
        result = tool_detect.apply(name, node_url=node_url)
        if spec.mode == "write":
            ok = result.action != "error"
            rc = rc or (0 if ok else 1)
            print(f"  {mark(ok, enable=color)} {spec.label}  "
                  f"{paint(f'{result.action} · {result.detail}', 'dim', enable=color)}")
        elif spec.mode == "snippet":
            print(paint(f"{spec.label} — paste into {spec.config_hint}:", "bold", enable=color))
            print(_indent(result.snippet or ""))
        else:
            print(f"{spec.label}: {result.detail}")
    return rc


async def _mcp_list(args: argparse.Namespace) -> int:
    from kb import tool_detect

    color = supports_color()
    detected = tool_detect.detect()
    if not detected:
        print("No known coding tools detected.")
        return 0
    print(f"Detected coding tools ({len(detected)}):")
    for name in detected:
        spec = tool_detect.SPECS[name]
        mode = {"write": "auto-write", "snippet": "snippet", "note": "note"}[spec.mode]
        print(f"  {paint(name.ljust(9), 'cyan', enable=color)} {mode:<10} {paint(spec.config_hint, 'dim', enable=color)}")
    return 0


async def _status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser() if args.repo else git_root_or_cwd()
    config_path = Path(args.config).expanduser() if args.config else capture_config_path()
    try:
        node_url = args.node_url or load_capture_config(config_path).node_url
    except ValueError:
        node_url = args.node_url or DEFAULT_NODE_URL
    token = capture_token() or None

    async def _gather() -> Any:
        return await asyncio.to_thread(
            gather_status,
            node_url,
            token,
            repo=repo,
            config_path=config_path,
            # Search smoke is opt-in: it never gates `healthy` and often costs
            # multi-second Railway/cognee latency (or the full smoke budget).
            with_search=bool(getattr(args, "check_search", False))
            and not bool(getattr(args, "no_search", False)),
            with_recent=not args.no_recent,
        )

    if args.json:
        report, mesh = await asyncio.gather(
            _gather(), asyncio.to_thread(fetch_mesh, node_url, token)
        )
        payload = report.to_dict()
        payload["mesh"] = mesh
        # Mirror the human path's drift hint onto the JSON surface — agents parse
        # `--json` and would otherwise miss the most actionable 401 diagnostic.
        auth_c = next((c for c in payload["checks"] if c.get("name") == "auth"), None)
        if auth_c is not None and not auth_c.get("ok"):
            detail = str(auth_c.get("detail", ""))
            code = 401 if "401" in detail else 403 if "403" in detail else 0
            hint = _stale_env_hint(code) if code else None
            if hint:
                auth_c.setdefault("data", {})["hint"] = hint
                payload["hint"] = hint
        _print_json(payload)
    else:
        # --check-search can take a few seconds cold — spin so it doesn't look hung.
        with _Spinner("Checking Citadel…"):
            report, mesh = await asyncio.gather(
                _gather(), asyncio.to_thread(fetch_mesh, node_url, token)
            )
        use_color = supports_color()
        if sys.stdout.isatty():
            # Piped output is for parsing/paging — skip the mark.
            print_banner_cascade(color=use_color)
            print()
        # Verdict last — the bottom line belongs at the bottom, not buried
        # above the mesh block.
        print(render_text(report, color=use_color, verdict=False))
        mesh_block = _render_mesh(mesh, use_color)
        if mesh_block:
            print()
            print(mesh_block)
        print()
        print(render_verdict(report, color=use_color))
        auth = next((c for c in report.checks if c.name == "auth"), None)
        if auth is not None and not auth.ok:
            code = 401 if "401" in auth.detail else 403 if "403" in auth.detail else 0
            hint = _stale_env_hint(code) if code else None
            if hint:
                print(paint(f"  hint: {hint}", "yellow", enable=use_color))
    return 0 if report.healthy else 1


def _render_mesh(mesh: dict[str, Any], color: bool) -> str:
    """Compact knowledge-mesh summary for `citadel status` (the 'your data' view)."""
    stats = (mesh or {}).get("stats")
    if not isinstance(stats, dict) or not stats:
        return ""

    def num(key: str) -> int:
        value = stats.get(key)
        return value if isinstance(value, int) else 0

    parts = [
        f"{paint(str(num('documents')), 'bold', enable=color)} documents",
        f"{num('nodes')} nodes",
        f"{num('edges')} edges",
        f"{num('searches')} searches",
    ]
    last = stats.get("last_indexed_at")
    if last:
        parts.append(paint(f"last indexed {str(last)[:16].replace('T', ' ')}", "dim", enable=color))
    return (
        paint("Knowledge mesh", "bold", enable=color)
        + "\n  "
        + " · ".join(parts)
    )


def _render_event(event: dict[str, Any], color: bool) -> str:
    """One Vault Activity line: `HH:MM  <type>  <message>  (dataset)`."""
    created = str(event.get("created_at") or "")
    stamp = created[11:16] if len(created) >= 16 else created[:5]
    etype = str(event.get("type") or "event")
    message = str(event.get("message") or "").strip()
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    timeline = event.get("timeline") if isinstance(event.get("timeline"), dict) else {}
    dataset = details.get("dataset") or timeline.get("dataset") or ""
    line = (
        paint(f"{stamp:>5}", "dim", enable=color)
        + "  "
        + paint(f"{etype:<12}", "bold", enable=color)
        + " "
        + message
    )
    if dataset:
        line += paint(f"  ({dataset})", "dim", enable=color)
    return line


def _activity_local(limit: int) -> int:
    """Print the tail of the local capture-receipt log (offline, no server)."""
    from kb.hooks.receipt import activity_log_path

    path = activity_log_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print("No local capture receipts yet — capture something (git push / session close) first.")
        return 0
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        print(line)
    if not lines:
        print("No local capture receipts yet.")
    return 0


def _render_presence(board: dict[str, Any], color: bool) -> str:
    """The team-presence board: one row per seat, `seat:<slug>  <n> docs`.

    Renders only Seat Presence (slug + contribution count) — no Node content.
    """
    if board.get("error"):
        return paint(f"Couldn't reach the Node: {board['error']}", "yellow", enable=color)
    seats = [s for s in (board.get("seats") or []) if isinstance(s, dict)]
    if not seats:
        return paint("No seats visible.", "dim", enable=color)
    # Central and higher-contribution seats first; slug is the only identity.
    seats.sort(key=lambda s: (-(s.get("documents") or 0), str(s.get("seat") or "")))
    width = max((len(str(s.get("seat") or "")) for s in seats), default=0)
    rows = [paint("Team presence", "bold", enable=color)]
    for seat in seats:
        slug = str(seat.get("seat") or "")
        docs = seat.get("documents")
        count = f"{docs} docs" if isinstance(docs, int) else "—"
        rows.append(f"  {slug.ljust(width)}  {paint(count, 'dim', enable=color)}")
    return "\n".join(rows)


async def _activity_global(node_url: str, token: str | None, color: bool, watch: bool) -> int:
    """Live team-presence broadcast — Seat Presence only (ADR-0009), no content."""
    board = await asyncio.to_thread(fetch_presence, node_url, token)
    print(_render_presence(board, color))
    if not token and not board.get("error") and not board.get("seats"):
        print(paint("  (no token configured — run `citadel onboard`)", "yellow", enable=color))
    if not watch:
        return 0
    print(paint("— watching team presence (Ctrl-C to stop) —", "dim", enable=color))
    while True:
        await asyncio.sleep(10)  # gentle: /api/mesh/graph is TTL-cached + latency-watched
        board = await asyncio.to_thread(fetch_presence, node_url, token)
        print()
        print(_render_presence(board, color))


async def _activity(args: argparse.Namespace) -> int:
    """Show the caller's own Node Vault Activity — recent, or live-tailed (--watch).

    Own-Node events carry content; the server scopes the feed per caller
    (ADR-0009), so this never shows another seat's Node content. ``--global``
    switches to the org Seat Presence board (counts only, no content).
    """
    limit = max(1, min(int(args.limit or 20), 160))
    if args.local:
        return _activity_local(limit)

    config_path = Path(args.config).expanduser() if args.config else capture_config_path()
    try:
        node_url = args.node_url or load_capture_config(config_path).node_url
    except ValueError:
        node_url = args.node_url or DEFAULT_NODE_URL
    token = capture_token() or None
    use_color = supports_color()

    if getattr(args, "global_broadcast", False):
        return await _activity_global(node_url, token, use_color, args.watch)

    data = await asyncio.to_thread(fetch_events, node_url, token, limit=limit, event_type=args.type)
    if args.json and not args.watch:
        _print_json(data)
        return 0

    events = list(reversed(data.get("events") or []))  # oldest -> newest reads like a growing feed
    for event in events:
        print(_render_event(event, use_color))
    last_id = data.get("latest_event_id")
    if last_id is None:
        last_id = events[-1]["id"] if events else 0

    if not args.watch:
        if not events:
            if data.get("error"):
                print(paint(f"Couldn't reach the Node: {data['error']}", "yellow", enable=use_color))
            else:
                print(paint("No recent activity.", "dim", enable=use_color))
                if not token:
                    print(paint("  (no token configured — run `citadel onboard`)", "yellow", enable=use_color))
        return 0

    print(paint("— watching your Node (Ctrl-C to stop) —", "dim", enable=use_color))
    while True:
        await asyncio.sleep(3)
        data = await asyncio.to_thread(
            fetch_events, node_url, token, after_id=last_id, limit=160, event_type=args.type
        )
        fresh = list(reversed(data.get("events") or []))
        for event in fresh:
            print(_render_event(event, use_color))
        if fresh:
            last_id = fresh[-1]["id"]
        elif data.get("latest_event_id") is not None:
            last_id = max(last_id, data["latest_event_id"])


def _mcp_node_url(path: Path) -> str | None:
    """The Node base URL wired into .mcp.json (citadel server), sans /mcp/ suffix."""
    try:
        servers = json.loads(path.read_text()).get("mcpServers", {})
        url = (servers.get("citadel") or {}).get("url", "")
    except (OSError, ValueError, AttributeError):
        return None
    for suffix in ("/mcp/", "/mcp"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url or None


async def _doctor(args: argparse.Namespace) -> int:
    """Diagnose common Citadel misconfigs and (with --fix) repair the safe ones."""
    repo = Path(args.repo).expanduser() if args.repo else git_root_or_cwd()
    config_path = Path(args.config).expanduser() if args.config else capture_config_path()
    try:
        cap_node = load_capture_config(config_path).node_url
    except ValueError:
        cap_node = None
    node_url = args.node_url or cap_node or DEFAULT_NODE_URL
    token = capture_token() or None
    as_json = getattr(args, "json", False)
    color = supports_color() and not as_json

    async def _gather() -> Any:
        return await asyncio.to_thread(
            gather_status, node_url, token, repo=repo, config_path=config_path,
            with_search=False, with_recent=False,
        )

    if as_json:
        report = await _gather()
    else:
        with _Spinner("Diagnosing Citadel…"):
            report = await _gather()
    checks = {c.name: c for c in report.checks}

    issues: list[dict[str, Any]] = []
    env_token = bool(os.getenv(TOKEN_ENV))
    rc_path = detect_shell_rc()
    try:
        rc_has = rc_path.exists() and f"{TOKEN_ENV}=" in rc_path.read_text()
    except OSError:
        rc_has = False
    if not env_token and rc_has:
        issues.append(
            {
                "problem": (
                    f"token is in {rc_path} but not this shell's env — "
                    f"Claude Code cannot expand ${{{TOKEN_ENV}}} in .mcp.json headers"
                ),
                "fix": (
                    f"source {rc_path} && claude  (local CLI), or add {TOKEN_ENV} "
                    "in Claude cloud environment settings"
                ),
            }
        )
    elif not env_token and not rc_has:
        issues.append({"problem": "no seat token configured", "fix": "citadel onboard"})

    node = checks.get("node")
    if node and not node.ok:
        issues.append({"problem": f"Node unreachable at {node_url} ({node.detail})",
                       "fix": "check the Node URL / network"})
    auth = checks.get("auth")
    # Only call it a token rejection when the Node is actually reachable — when
    # the Node is down, auth fails too, and we must not tell the user to rotate
    # a perfectly valid token.
    if node and node.ok and token and auth and not auth.ok:
        issues.append({"problem": f"Node rejected the token ({auth.detail})",
                       "fix": "token revoked/expired or wrong Node — re-mint (`citadel seat create`) or re-onboard"})

    # Data-plane gate: a reachable, authenticated Node whose corpus check is RED
    # (sources tracked but graph empty, or the cognify canary failed) is a real
    # problem, not "No problems found" (#27). Manual issue (no kind) → stays
    # unresolved so doctor exits nonzero.
    corpus = checks.get("corpus")
    if node and node.ok and auth and auth.ok and corpus and not corpus.ok:
        issues.append({"problem": f"data plane broken ({corpus.detail}) — Node up but retrieval is empty",
                       "fix": "check the evolve scheduler / cognify; run `citadel cognify --verify`"})

    mcp_node = _mcp_node_url(repo / ".mcp.json")
    if mcp_node and cap_node and mcp_node.rstrip("/") != cap_node.rstrip("/"):
        issues.append({"problem": f".mcp.json Node ({mcp_node}) disagrees with capture config ({cap_node})",
                       "fix": f"citadel onboard --node-url {cap_node}"})

    if checks.get("pre_push_hook") and not checks["pre_push_hook"].ok:
        issues.append({"problem": "git pre-push autosync hook missing", "fix": "citadel doctor --fix", "kind": "pre_push"})
    if checks.get("session_hook") and not checks["session_hook"].ok:
        issues.append({"problem": "Claude SessionEnd/SessionStart hooks missing", "fix": "citadel doctor --fix", "kind": "session"})
    if checks.get("mcp") and not checks["mcp"].ok:
        mcp_check = checks["mcp"]
        state = (mcp_check.data or {}).get("state") or MCP_STATE_MISSING
        next_step = (mcp_check.data or {}).get("next") or "citadel mcp add cursor"
        mcp_issue: dict[str, Any] = {
            "problem": f"MCP not ready ({state}): {mcp_check.detail}",
            "fix": next_step,
        }
        # Only wiring gaps are auto-fixable. needsAuth requires a seat token
        # doctor cannot mint — do not attach kind=mcp or --fix falsely greens.
        if state in (MCP_STATE_MISSING, MCP_STATE_READY_BUT_UNCONFIGURED):
            mcp_issue["kind"] = "mcp"
            if state == MCP_STATE_MISSING:
                mcp_issue["fix"] = "citadel doctor --fix"
        elif state == MCP_STATE_NEEDS_AUTH:
            mcp_issue["fix"] = (
                f"set {TOKEN_ENV} in the environment Cursor was launched from, "
                "then restart Cursor"
            )
        issues.append(mcp_issue)
    for mcp_issue in diagnose_mcp_config(repo):
        if not any(i.get("problem") == mcp_issue["problem"] for i in issues):
            issues.append(mcp_issue)

    fixed: list[str] = []
    fixed_kinds: set[str] = set()
    if getattr(args, "fix", False):
        for issue in issues:
            kind = issue.get("kind")
            try:
                if kind == "pre_push":
                    if not install_pre_push_hook(repo).startswith("skipped"):
                        fixed.append("pre-push hook")
                        fixed_kinds.add(kind)
                elif kind == "session":
                    merge_claude_settings(claude_user_settings_path())
                    fixed.append("Claude hooks")
                    fixed_kinds.add(kind)
                elif kind == "mcp":
                    merge_mcp_config(repo / ".mcp.json", node_url)
                    after = assess_mcp_setup(repo)
                    after_state = (after.data or {}).get("state")
                    if after.ok:
                        fixed.append("MCP server")
                        fixed_kinds.add(kind)
                    elif after_state == MCP_STATE_NEEDS_AUTH:
                        # Hosted HTTP is wired; token is still a manual step.
                        # Demote so --fix does not claim "all clear".
                        fixed.append("MCP server (hosted HTTP)")
                        issue.pop("kind", None)
                        issue["problem"] = (
                            f"MCP not ready ({after_state}): {after.detail}"
                        )
                        issue["fix"] = (
                            f"set {TOKEN_ENV} in the environment Cursor was "
                            "launched from, then restart Cursor"
                        )
                    # else still missing/unconfigured — leave kind unresolved
            except (ValueError, OSError):
                pass

    # After --fix, an auto issue whose fix was applied is resolved; manual issues
    # (no kind) always remain. Exit 0 only when nothing is left unresolved.
    unresolved = [i for i in issues if not i.get("kind") or i.get("kind") not in fixed_kinds]
    rc = 1 if unresolved else 0

    if as_json:
        _print_json({
            "ok": not issues,
            "node_url": node_url,
            "issues": [{k: v for k, v in i.items() if k != "kind"} for i in issues],
            "fixed": fixed,
            "resolved": not unresolved,
        })
        return rc

    if sys.stdout.isatty():
        print_banner_cascade(color=color)
        print()
    # Several checks are repo-relative — name the repo so a miss from the
    # wrong directory reads as "wrong directory", not "broken setup".
    print(paint(f"repo: {repo}", "dim", enable=color))
    if not issues:
        print(paint("✓ No problems found.", "green", enable=color))
        return 0
    print(paint(f"Found {len(issues)} issue(s):", "yellow", enable=color))
    for issue in issues:
        print(f"  {mark(False, enable=color)} {issue['problem']}")
        print(f"      {paint('fix: ' + issue['fix'], 'dim', enable=color)}")
    if fixed and not unresolved:
        print()
        print(paint(f"✓ Fixed: {', '.join(fixed)}. All clear.", "green", enable=color))
    elif fixed:
        print()
        print(paint(f"Applied: {', '.join(fixed)}. Remaining items need you (see above).", "yellow", enable=color))
    elif any(i.get("kind") for i in issues):
        print()
        print(paint("Run `citadel doctor --fix` to apply the auto-fixable ones.", "dim", enable=color))
    return rc


def _humanize_status(status: str) -> tuple[str, bool, bool]:
    """Map a raw onboard step status → (human text, ok, skipped).

    Step functions return machine tokens like ``skipped:not-git``; humans should
    never see those. Skipped steps are not failures (they render with ⊘).
    """
    if status.startswith("skipped:"):
        reason = status.split(":", 1)[1].replace("-", " ")
        reason = {"not git": "not a git repo"}.get(reason, reason)
        return f"skipped ({reason})", True, True
    return status, True, False


def _print_banner_animated(text: str, color: bool) -> None:
    """Reveal the banner line-by-line for a little ceremony (TTY + color only).

    Prefer ``print_banner_animated`` from ``kb.banner`` for new call sites;
    this wrapper keeps older call patterns working when ``text`` is pre-rendered.
    """
    if not (color and sys.stdout.isatty()):
        print(text)
        return
    import time

    for line in text.split("\n"):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        time.sleep(0.035)


def _render_identity(auth: Any, node_url: str, color: bool) -> str:
    """Who a verified token belongs to — seat, role, access (onboard/token set)."""
    ident = getattr(auth, "data", None) or {}
    seat = ident.get("seat_slug") or ident.get("actor") or "—"
    caps = ident.get("capabilities") or {}
    access = " · ".join(
        flag for flag, on in (("read", caps.get("read")), ("write", caps.get("write")), ("admin", caps.get("admin"))) if on
    ) or "—"
    writes = f"seat:{seat}" if ident.get("seat_slug") else "shared org dataset"
    lines = [
        paint(f"  {mark(True, enable=color)} authenticated", "green", enable=color),
        f"      seat     {paint(str(seat), 'cyan', enable=color)}",
        f"      role     {ident.get('role') or '—'}",
        f"      access   {access}",
        f"      writes   {writes}",
        paint(f"      node     {node_url}", "dim", enable=color),
    ]
    hint = seatless_token_hint(ident)
    if hint:
        lines.append(paint(f"      hint     {hint}", "yellow", enable=color))
    return "\n".join(lines)


def _prompt_hidden(prompt: str) -> str:
    """getpass wrapper: the pasted secret is never echoed; Ctrl-D → ""."""
    try:
        return getpass.getpass(prompt).strip()
    except EOFError:
        print()
        return ""


async def _resolve_onboard_token(
    args: argparse.Namespace, rc_path: Path, node_url: str, *, interactive: bool, color: bool
) -> tuple[str, bool]:
    """Pick the token to onboard with, then verify it.

    Returns ``(token, rejected)`` — ``token`` is "" when absent; ``rejected``
    is True when the Node answered 401/403 and the user chose to keep the
    token anyway, so the summary can warn instead of ending green.

    Interactive flow: an already-configured token (shell rc first — it's the
    durable value every new shell exports — then env) is shown masked with a
    keep-or-replace choice; a token the Node rejects offers an immediate
    re-paste instead of burying the failure at the end of the run.
    Non-interactive: --token or env only, no verification (scripts/CI stay
    offline).
    """
    token = (args.token or "").strip()
    if not token:
        env_token = (os.environ.get(TOKEN_ENV) or "").strip()
        # rc wins over env: a stale shell's env must not silently revert a
        # rotation that `citadel token set` already wrote to the rc.
        rc_token = read_token_from_rc(rc_path) if interactive else ""
        if rc_token:
            existing, source = rc_token, str(rc_path)
        else:
            existing, source = env_token, "this shell's env"
        if existing and interactive:
            print(
                f"\nAccess token already configured: "
                f"{paint(mask_token(existing), 'bold', enable=color)}  "
                f"{paint(f'(from {source})', 'dim', enable=color)}"
            )
            if env_token and rc_token and env_token != rc_token:
                print(
                    paint(
                        f"  (this shell's env exports a different token {mask_token(env_token)} — "
                        f"`source {rc_path}` after onboarding aligns it)",
                        "dim",
                        enable=color,
                    )
                )
            answer = _prompt("  Keep it? [Y/n]: ").strip().lower()
            token = existing
            if answer in ("n", "no"):
                token = _prompt_hidden("  Paste the new seat token (ctdl_…): ") or existing
                if token == existing:
                    print(paint("  (nothing pasted — keeping the existing token)", "dim", enable=color))
        else:
            token = existing
    if not token and interactive:
        # Guide the new user to the token instead of dead-ending on an error.
        print(
            f"\nGet your seat token from the Citadel dashboard:  "
            f"{paint(node_url, 'cyan', enable=color)}\n"
            "  (log in with the admin key → Create Seat → copy the ctdl_… token)\n"
        )
        token = _prompt_hidden("Paste your Citadel seat token (ctdl_…): ")
    if not token:
        return "", False

    # Verify the token + show its identity (seat / role / access) up front, so a
    # rejected token is fixable now — not discovered after every other prompt.
    auth = None
    if interactive:
        from kb.status import check_auth

        while True:
            with _Spinner("Verifying your token…"):
                auth = await asyncio.to_thread(check_auth, node_url, token)
            print()
            if auth.ok:
                print(_render_identity(auth, node_url, color))
                break
            detail = str(getattr(auth, "detail", "")) or "could not verify"
            if not any(code in detail for code in ("401", "403")):
                # Node unreachable / network error — a new token won't help.
                print(
                    f"  {paint('!', 'yellow', enable=color)} "
                    + paint(f"could not verify the token ({detail}) — continuing; run `citadel status` later.", "yellow", enable=color)
                )
                break
            print(f"  {mark(False, enable=color)} the Node rejected this token ({detail})")
            answer = _prompt("  Paste a different token? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print(paint("  (keeping it anyway — fix it later with `citadel token set`)", "dim", enable=color))
                break
            new_token = _prompt_hidden("  Paste the new seat token (ctdl_…): ")
            if not new_token:
                break
            token = new_token
    rejected = (
        auth is not None
        and not auth.ok
        and any(code in str(getattr(auth, "detail", "")) for code in ("401", "403"))
    )
    return token, rejected


async def _onboard(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser() if args.repo else git_root_or_cwd()
    as_json = getattr(args, "json", False)
    interactive = sys.stdin.isatty() and not args.non_interactive and not as_json
    color = supports_color() and not as_json
    node_url = (getattr(args, "node_url", None) or DEFAULT_NODE_URL).rstrip("/")
    rc_path = Path(args.shell_rc).expanduser() if args.shell_rc else detect_shell_rc()

    if interactive:
        print_banner_animated(color=color)

    token, token_rejected = await _resolve_onboard_token(
        args, rc_path, node_url, interactive=interactive, color=color
    )
    if not token:
        msg = "no token — pass --token or set CITADEL_MCP_ACCESS_TOKEN."
        if as_json:
            _print_json({"ok": False, "error": msg, "token_rejected": token_rejected})
        else:
            print(f"citadel onboard: {msg}", file=sys.stderr)
        return 1

    steps: list[tuple[str, str]] = []
    try:
        steps.append((f"token → {rc_path}", ensure_token_in_rc(rc_path, token)))
        steps.append(("git pre-push hook", install_pre_push_hook(repo)))
        # Session hooks go to the USER-scope settings so they fire across every
        # repo, not only the onboard repo (#38).
        steps.append(("SessionEnd hook", merge_claude_settings(claude_user_settings_path())))
        steps.extend(install_agent_policies(repo))
        if not args.no_mcp:
            steps.append(("MCP server (.mcp.json)", merge_mcp_config(repo / ".mcp.json", node_url)))
    except ValueError as exc:
        if as_json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"citadel onboard: {exc}", file=sys.stderr)
        return 1

    # A custom --node-url is persisted to the capture config so MCP and capture
    # target the same Node (no split-brain). Done before the roots wizard, which
    # preserves node_url when it re-saves.
    if getattr(args, "node_url", None):
        try:
            cfg_path = capture_config_path()
            existing = load_capture_config(cfg_path)
            save_capture_config(
                CaptureConfig(node_url=node_url, roots=existing.roots, version=existing.version),
                path=cfg_path,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            steps.append((f"node url → {cfg_path}", node_url))
        except ValueError:
            pass

    # Optionally collect an OpenRouter key so local `cognify`/`cognify --verify`
    # and proactive-ingest work out of the box (#35). Interactive-only; written
    # to the shell rc next to the seat token.
    if interactive:
        llm_key = _prompt_hidden(
            "\nOpenRouter API key for local cognify — Enter to skip: "
        )
        if llm_key:
            steps.append(
                (
                    f"OpenRouter key → {rc_path}",
                    ensure_env_in_rc(
                        rc_path,
                        "OPENROUTER_API_KEY",
                        llm_key,
                        comment="OpenRouter key for Citadel local cognify (added by `citadel onboard`)",
                    ),
                )
            )
            # Only relevant once a key exists — noise for everyone who skipped.
            print(
                "  (local cognify also needs: pipx install 'citadel-archive[server]')"
            )

    # Capture roots (unless --no-capture): the wizard asks about the repo
    # toplevel explicitly (declinable); if the config still ends up empty, it
    # is seeded with the repo toplevel so the pre-push hook is not a
    # guaranteed no-op out of the box (#43/#35).
    if not args.no_capture:
        cfg_path = capture_config_path()
        cfg = load_capture_config(cfg_path)
        if interactive:
            # Straight into the wizard — its first question is already a
            # declinable Y/n, so a separate "set up now?" gate was one prompt
            # of pure friction.
            cfg = _wizard_roots(cfg, default_root=str(repo))
        if not cfg.roots:
            cfg = cfg.with_root(str(repo), (DEFAULT_ROOT_TAG,))  # 'personal' never promotes
        save_capture_config(
            cfg, path=cfg_path, updated_at=datetime.now(timezone.utc).isoformat()
        )
        steps.append((f"capture roots → {cfg_path}", f"{len(cfg.roots)} root(s)"))
        sync_result = sync_local_capture_roots_to_server(
            cfg,
            base_url=node_url,
            token=token,
        )
        warning = sync_warning_message(sync_result)
        if warning:
            steps.append(("capture roots → Node", f"warning: {warning}"))
        elif sync_result.status == "synced":
            steps.append(
                (
                    "capture roots → Node",
                    f"synced {sync_result.merged_count} root(s) to seat {sync_result.seat_slug}",
                )
            )
        elif sync_result.status == "unchanged":
            steps.append(("capture roots → Node", "unchanged on Node"))
        else:
            steps.append(("capture roots → Node", sync_result.detail))

    if interactive and not getattr(args, "no_tools", False):
        _wire_detected_tools(node_url, color=color)

    if as_json:
        _print_json(
            {
                "ok": True,
                "repo": str(repo),
                "shell_rc": str(rc_path),
                "token_masked": mask_token(token),
                "steps": [{"name": name, "status": status} for name, status in steps],
            }
        )
        return 0

    if not interactive:
        print(banner(color=color))
    print(f"\nCitadel onboarding for {repo}  (token {mask_token(token)}):")
    for label, status in steps:
        text, ok, skipped = _humanize_status(status)
        sigil = paint(SKIP, "yellow", enable=color) if skipped else mark(ok, enable=color)
        if token_rejected and label.startswith("token → "):
            # The Node said 401/403 up front — the summary must not end green.
            sigil = paint(WARN, "yellow", enable=color)
            text += " — Node rejected this token"
        print(f"  {sigil} {label}  {paint(text, 'dim', enable=color)}")
    done = sum(1 for _, status in steps if not status.startswith("skipped"))
    skipped_count = len(steps) - done
    summary = f"Citadel configured — {done} step(s) wired"
    if skipped_count:
        summary += f", {skipped_count} skipped"
    print()
    print(paint(summary + ".", "green", enable=color))
    if token_rejected:
        print(
            paint(
                f"{WARN} The Node rejected this token — searches will fail until you set "
                "a valid one with `citadel token set`.",
                "yellow",
                enable=color,
            )
        )
    print(
        f"\nNext: restart your shell (or `source {rc_path}`), then in your agent:\n"
        '  • at task start — "use citadel_search before we code on <topic>"\n'
        '  • proactive capture — see /skills/proactive-ingest for mid-session ingest + autosync\n'
        '  • when a route is worth reusing — ask the user, then `citadel_share_session` '
        "(Shared Session Traces are reference-only, not org truth)\n"
        '  • smoke test — "use citadel_search to find what we decided about the vault"'
    )
    if not args.no_mcp:
        print(format_claude_mcp_next_steps(rc_path))
    return 0


def _cli_version() -> str:
    try:
        return _pkg_version("citadel-archive")
    except PackageNotFoundError:
        from kb import __version__

        return __version__


def _install_channel() -> tuple[str, str]:
    """How this CLI was installed: ("editable", src) | ("pipx", bin) | ("other", "").

    Drives `citadel update`: editable/source checkouts must never be clobbered
    by an upgrade, pipx installs know their own upgrade command, anything else
    gets printed instructions.
    """
    try:
        from importlib.metadata import distribution

        direct = distribution("citadel-archive").read_text("direct_url.json")
        if direct:
            info = json.loads(direct)
            if info.get("dir_info", {}).get("editable"):
                return "editable", str(info.get("url") or "")
    except (PackageNotFoundError, ValueError, OSError):
        pass
    pipx = shutil.which("pipx")
    if pipx and "pipx" in Path(sys.prefix).parts:
        return "pipx", pipx
    return "other", ""


async def _update(args: argparse.Namespace) -> int:
    """Update citadel in place — the answer to `pipx install` saying
    "already seems to be installed"; no --force incantations needed."""
    color = supports_color()
    channel, detail = _install_channel()
    if channel == "editable":
        src = urllib.parse.unquote(detail.removeprefix("file://")) or "the source checkout"
        print(f"You're running from source (editable install) — update with `git pull` in {src}.")
        return 0
    if channel != "pipx":
        print(
            "citadel update: this install isn't managed by pipx. Upgrade it with:\n"
            "  pipx:  pipx install citadel-archive\n"
            f"  pip:   {sys.executable} -m pip install --upgrade citadel-archive"
        )
        return 0
    current = _cli_version()
    try:
        with _Spinner("Checking PyPI for the latest release…"):
            proc = await asyncio.to_thread(
                subprocess.run,
                # --no-cache-dir: never "upgrade" onto a stale cached wheel.
                [detail, "upgrade", "--pip-args=--no-cache-dir", "citadel-archive"],
                capture_output=True,
                text=True,
                timeout=300,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"citadel update: pipx upgrade failed: {exc}", file=sys.stderr)
        return 1
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        print(f"citadel update: pipx upgrade failed:\n{out}", file=sys.stderr)
        return 1
    if "already at latest" in out:
        print(f"  {mark(True, enable=color)} already up to date — citadel {current}")
    else:
        upgraded = next((line for line in out.splitlines() if "upgraded" in line.lower()), out)
        print(f"  {mark(True, enable=color)} {upgraded.strip()}")
    return 0


_HOME_MENU = (
    ("Get started", (
        ("onboard", "one-command setup — token · hooks · MCP · capture roots"),
        ("status", "connection · identity · local setup (--json for agents)"),
        ("doctor", "diagnose setup problems · --fix to repair"),
        ("update", "update citadel to the latest release"),
    )),
    ("Capture", (
        ("setup", "declare Approved Capture Roots (~/.citadel/capture.json)"),
        ("capture", "push summaries of approved roots to your Node"),
        ("promotion", "list · approve · reject · run the Promotion Agent queue"),
    )),
    ("Knowledge", (
        ("search", "search the Organization Vault"),
        ("ingest", "add a durable note to your Node"),
        ("activity", "recent vault activity — captures · syncs · searches (--global for the team)"),
    )),
    ("Connect & admin", (
        ("mcp", "add Citadel MCP to Claude · Cursor · Codex · …"),
        ("seat", "create · list seats and mint tokens (admin)"),
        ("token", "set this machine's seat token · admin create/revoke"),
    )),
)


def _already_onboarded() -> bool:
    """Network-free check: has the user ever completed onboarding?

    Onboarding is "done" once the token is wired (any one ⇒ onboarded), checked
    offline so the home screen and the first-run gate stay instant and work
    without the Node:
      1. CITADEL_MCP_ACCESS_TOKEN in the environment (current shell)
      2. an ``export CITADEL_MCP_ACCESS_TOKEN=`` line in the shell rc — the signal
         that survives a fresh post-install shell, where the env var isn't set yet

    A capture config alone is NOT a signal: ``citadel setup`` writes one without
    ever wiring a token, so counting it would falsely suppress first-run
    onboarding and show "✓ set up" with no token.
    """
    if os.environ.get(TOKEN_ENV):
        return True
    try:
        rc = detect_shell_rc()
        if rc.exists() and f"{TOKEN_ENV}=" in rc.read_text():
            return True
    except OSError:
        pass
    return False


def _print_home() -> None:
    color = supports_color()
    cols = shutil.get_terminal_size((80, 24)).columns
    if cols >= HERO_WIDTH + 2:
        # Pixel Bastion only (cascade on TTY+color). banner()/cascade already
        # include CITADEL + tagline beside the mark — no figlet wordmark.
        print_banner_cascade(color=color)
    else:
        # Narrow terminal: compact Pixel Bastion (wordmark + tagline inline)
        # instead of a wrapped, mangled hero.
        print(banner(color=color))
    if _already_onboarded():
        state = mark(True, enable=color) + " " + paint("set up", "green", enable=color)
    else:
        state = (
            mark(False, enable=color)
            + " "
            + paint("not set up", "red", enable=color)
            + paint(" — run ", "dim", enable=color)
            + paint("citadel onboard", "cyan", enable=color)
        )
    print("  " + state + paint(f"  ·  v{_cli_version()}", "dim", enable=color))
    print()
    # Pad command names to the widest, so descriptions form a clean column.
    pad = max(len(name) for _, rows in _HOME_MENU for name, _ in rows) + 2
    cols = shutil.get_terminal_size((80, 24)).columns
    desc_budget = cols - 4 - pad - 1  # indent + name column + gap
    for title, rows in _HOME_MENU:
        print("  " + paint(title, "bold", enable=color))
        for name, desc in rows:
            # Truncate the RAW description (never the ANSI codes) so narrow
            # terminals don't wrap and collide the next row.
            if desc_budget > 12 and len(desc) > desc_budget:
                desc = desc[: desc_budget - 1] + "…"
            label = paint(name.ljust(pad), "cyan", enable=color)
            print(f"    {label} {paint(desc, 'dim', enable=color)}")
        print()
    print("  " + paint("Run `citadel <command> --help` for details.", "dim", enable=color))


class CitadelParser(argparse.ArgumentParser):
    """argparse parser with a friendly, suggestion-aware unknown-command error."""

    def error(self, message: str) -> NoReturn:
        color = supports_color(sys.stderr)
        top = self.prog == "citadel"

        # 1) Typo / unknown choice — at the top level OR inside any group.
        if "invalid choice:" in message:
            # A bad value to a --flag with choices= is NOT an unknown command;
            # only positionals/subparser dests are (their arg name has no dash).
            arg_match = re.search(r"argument ([^:]+): invalid choice:", message)
            if arg_match and arg_match.group(1).lstrip().startswith("-"):
                return super().error(message)
            bad_match = re.search(r"invalid choice: '([^']*)'", message)
            bad = bad_match.group(1) if bad_match else ""
            choices_match = re.search(r"\(choose from (.+)\)\s*$", message)
            choices = (
                [c.strip().strip("'\"") for c in choices_match.group(1).split(",")]
                if choices_match
                else []
            )
            noun = "command" if top else "subcommand"
            lines = [paint(f"✗ unknown {noun}: {bad!r}", "red", enable=color), ""]
            matches = difflib.get_close_matches(bad, choices, n=3, cutoff=0.5)
            if matches:
                lines.append("  did you mean?")
                lines += [
                    "    " + paint(f"{self.prog} {name}", "bold", "cyan", enable=color)
                    for name in matches
                ]
                lines.append("")
            elif choices:
                lines.append("  available: " + ", ".join(paint(c, "cyan", enable=color) for c in choices))
                lines.append("")
            if top:
                lines.append("  run " + paint("citadel", "cyan", enable=color) + " to see all commands.")
            else:
                lines.append("  run " + paint(f"{self.prog} --help", "cyan", enable=color) + " for options.")
            sys.stderr.write("\n".join(lines) + "\n")
            raise SystemExit(2)

        # 2) Missing required argument(s).
        req_match = re.search(r"the following arguments are required: (.+)$", message)
        if req_match:
            missing = [m.strip() for m in req_match.group(1).split(",")]
            sub_action = next(
                (a for a in self._actions if isinstance(a, argparse._SubParsersAction)), None
            )
            # A subcommand group was invoked bare → list its subcommands.
            if sub_action and any(m.endswith("command") for m in missing):
                help_by_name = {
                    a.dest: (a.help or "") for a in getattr(sub_action, "_choices_actions", [])
                }
                pad = max((len(n) for n in sub_action.choices), default=8) + 2
                lines = [paint(f"✗ {self.prog} needs a subcommand:", "red", enable=color), ""]
                for name in sub_action.choices:
                    label = paint(name.ljust(pad), "cyan", enable=color)
                    lines.append(f"  {label}{paint(help_by_name.get(name, ''), 'dim', enable=color)}")
                lines.append("")
                lines.append(
                    "  e.g. " + paint(f"{self.prog} {next(iter(sub_action.choices))}", "bold", "cyan", enable=color)
                )
                sys.stderr.write("\n".join(lines) + "\n")
                raise SystemExit(2)
            # Otherwise a positional is missing → show what + how.
            lines = [
                paint(f"✗ {self.prog}: missing {', '.join(missing)}", "red", enable=color),
                "  " + self.format_usage().strip(),
                "  run " + paint(f"{self.prog} --help", "cyan", enable=color) + " for details.",
            ]
            sys.stderr.write("\n".join(lines) + "\n")
            raise SystemExit(2)

        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CitadelParser(
        prog="citadel",
        description="Citadel — your Organization Vault. Search, capture, and share team knowledge.",
        epilog="Run `citadel` with no command for the guided home screen, "
        "or `citadel <command> --help` for any command.",
    )
    parser.add_argument("--version", action="version", version=f"citadel {_cli_version()}")
    parser.add_argument(
        "--no-onboard",
        action="store_true",
        help="Skip first-run onboarding on bare `citadel` (also via CITADEL_NO_ONBOARD)",
    )
    # Not required: bare `citadel` shows the banner + command list instead of an error.
    subcommands = parser.add_subparsers(dest="command")

    status = subcommands.add_parser(
        "status",
        help="Check Citadel connection, identity, and local setup (--json for agents)",
    )
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    status.add_argument("--node-url", help="Override Node URL (default: from config)")
    status.add_argument("--repo", help="Repo to check hooks/MCP in (default: git toplevel or cwd)")
    status.add_argument("--config", help="Override capture config path")
    status.add_argument(
        "--check-search",
        action="store_true",
        help="Also smoke-test /search (opt-in; short timeout; never gates health)",
    )
    status.add_argument(
        "--no-search",
        action="store_true",
        help="Skip the search smoke check (default; kept for script compatibility)",
    )
    status.add_argument("--no-recent", action="store_true", help="Skip recent-activity fetch")
    status.set_defaults(handler=_status)

    activity = subcommands.add_parser(
        "activity",
        help="Show your Node's vault activity — captures, syncs, promotions, searches",
    )
    activity.add_argument("--watch", action="store_true", help="Live-tail new activity (Ctrl-C to stop)")
    activity.add_argument(
        "--global",
        dest="global_broadcast",
        action="store_true",
        help="Live team presence — every seat's contribution count (Seat Presence only, no content)",
    )
    activity.add_argument(
        "--local",
        action="store_true",
        help="Show local capture receipts from ~/.citadel/activity.log (offline, no server)",
    )
    activity.add_argument("--limit", type=int, default=20, help="How many recent events to show (default 20)")
    activity.add_argument("--type", help="Filter by event type (ingest, search, promotion, github_sync, …)")
    activity.add_argument("--json", action="store_true", help="Machine-readable output")
    activity.add_argument("--node-url", help="Override Node URL (default: from config)")
    activity.add_argument("--config", help="Override capture config path")
    activity.set_defaults(handler=_activity)

    doctor = subcommands.add_parser(
        "doctor",
        help="Diagnose setup problems and suggest (or --fix) repairs",
    )
    doctor.add_argument("--fix", action="store_true", help="Apply safe auto-fixes (hooks, .mcp.json)")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output")
    doctor.add_argument("--node-url", help="Override Node URL")
    doctor.add_argument("--repo", help="Repo to check (default: git toplevel or cwd)")
    doctor.add_argument("--config", help="Override capture config path")
    doctor.set_defaults(handler=_doctor)

    update = subcommands.add_parser(
        "update",
        aliases=["upgrade"],
        help="Update citadel to the latest release (pipx-aware)",
    )
    update.set_defaults(handler=_update)

    onboard = subcommands.add_parser(
        "onboard",
        help="One-shot teammate setup: token + hooks + MCP + capture roots",
    )
    onboard.add_argument("--token", help="Seat token (else prompt, or use env)")
    onboard.add_argument("--repo", help="Repo root (default: git toplevel or cwd)")
    onboard.add_argument("--shell-rc", help="Shell rc file for the token export")
    onboard.add_argument(
        "--node-url",
        help="Node URL to wire into MCP/capture (default: the built-in Node)",
    )
    onboard.add_argument("--no-mcp", action="store_true", help="Skip writing .mcp.json")
    onboard.add_argument(
        "--no-capture", action="store_true", help="Skip Approved Capture Roots setup"
    )
    onboard.add_argument(
        "--no-tools", action="store_true", help="Skip detecting/adding MCP to other coding tools"
    )
    onboard.add_argument(
        "--non-interactive", action="store_true", help="No prompts; requires --token"
    )
    onboard.add_argument(
        "--json", action="store_true", help="Machine-readable output (implies no prompts)"
    )
    onboard.set_defaults(handler=_onboard)

    setup = subcommands.add_parser(
        "setup",
        help="Configure local Approved Capture Roots (~/.citadel/capture.json)",
    )
    setup.add_argument("--node-url", help=f"Seat Node URL (default {DEFAULT_NODE_URL})")
    setup.add_argument(
        "--root",
        action="append",
        metavar="PATH[=tag1,tag2]",
        help="Add/replace an approved root (repeatable). Implies non-interactive.",
    )
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip prompts; only apply --node-url / --root flags",
    )
    setup.add_argument("--show", action="store_true", help="Print current config and exit")
    setup.add_argument("--json", action="store_true", help="Machine-readable output")
    setup.add_argument("--config", help="Override config path (testing)")
    setup.set_defaults(handler=_setup)

    capture = subcommands.add_parser(
        "capture",
        help="Summarize Approved Capture Roots and POST to your Node",
    )
    capture.add_argument(
        "--root",
        action="append",
        metavar="PATH",
        help="Capture only this configured root (repeatable; default: all)",
    )
    capture.add_argument(
        "--dry-run", action="store_true", help="Print payloads without posting"
    )
    capture.add_argument("--json", action="store_true", help="Machine-readable output")
    capture.add_argument("--config", help="Override config path (testing)")
    capture.set_defaults(handler=_capture)

    promotion = subcommands.add_parser(
        "promotion",
        help="Promotion Agent queue — list, approve, reject, or run",
    )
    promotion_sub = promotion.add_subparsers(dest="promotion_command", required=True)

    promo_list = promotion_sub.add_parser("list", help="List pending promotion items")
    promo_list.add_argument(
        "--status",
        default="pending",
        choices=("pending", "approved", "rejected"),
        help="Queue filter (default: pending)",
    )
    promo_list.add_argument("--json", action="store_true", help="Machine-readable output")
    promo_list.add_argument("--node-url", help="Override Node URL")
    promo_list.set_defaults(handler=_promotion_list)

    promo_approve = promotion_sub.add_parser("approve", help="Approve a pending item")
    promo_approve.add_argument("item_id", help="Promotion item id (promo_…)")
    promo_approve.add_argument("--note", help="Optional audit note")
    promo_approve.add_argument("--json", action="store_true")
    promo_approve.add_argument("--node-url", help="Override Node URL")
    promo_approve.set_defaults(handler=_promotion_approve)

    promo_reject = promotion_sub.add_parser("reject", help="Reject a pending item")
    promo_reject.add_argument("item_id", help="Promotion item id (promo_…)")
    promo_reject.add_argument("--note", help="Optional audit note")
    promo_reject.add_argument("--json", action="store_true")
    promo_reject.add_argument("--node-url", help="Override Node URL")
    promo_reject.set_defaults(handler=_promotion_reject)

    promo_run = promotion_sub.add_parser(
        "run",
        help="Run the Promotion Agent for your seat (dry-run by default)",
    )
    promo_run.add_argument(
        "--execute",
        action="store_true",
        help="Actually promote / queue (default is dry-run)",
    )
    promo_run.add_argument(
        "--dataset",
        help="Seat dataset to scan (default: token's seat)",
    )
    promo_run.add_argument("--max-items", type=int, help="Cap candidates per run")
    promo_run.add_argument("--json", action="store_true")
    promo_run.add_argument("--node-url", help="Override Node URL")
    promo_run.set_defaults(handler=_promotion_run)

    seat = subcommands.add_parser(
        "seat",
        help="Manage seats (admin — reads the admin key from CITADEL_ADMIN_KEY)",
    )
    seat_sub = seat.add_subparsers(dest="seat_command", required=True)

    seat_list = seat_sub.add_parser("list", help="List seats, roles, and active token counts")
    seat_list.add_argument("--json", action="store_true", help="Machine-readable output")
    seat_list.add_argument("--node-url", help="Override Node URL")
    seat_list.set_defaults(handler=_seat_list)

    seat_create = seat_sub.add_parser(
        "create", help="Create a seat and mint its writer token (printed once)"
    )
    seat_create.add_argument("name", help='Human name, e.g. "Alice Smith"')
    seat_create.add_argument("slug", help="Seat slug, e.g. alice (a-z, 0-9, hyphen)")
    seat_create.add_argument("--email", help="Optional contact email")
    seat_create.add_argument(
        "--role", default="writer", choices=("writer", "reader"), help="Seat role (default: writer)"
    )
    seat_create.add_argument(
        "--no-token", action="store_true", help="Create the seat without issuing a token"
    )
    seat_create.add_argument("--json", action="store_true", help="Machine-readable output")
    seat_create.add_argument("--node-url", help="Override Node URL")
    seat_create.set_defaults(handler=_seat_create)

    seat_token = seat_sub.add_parser(
        "token",
        help="Mint a fresh token for an EXISTING seat (alias of `citadel token create --seat <slug>`)",
    )
    seat_token.add_argument("slug", help="Seat slug, e.g. sarthi")
    seat_token.add_argument("--json", action="store_true", help="Machine-readable output")
    seat_token.add_argument("--node-url", help="Override Node URL")
    seat_token.set_defaults(handler=_seat_token)

    token = subcommands.add_parser(
        "token",
        help="Set this machine's seat token, or manage standalone tokens (admin)",
    )
    token_sub = token.add_subparsers(dest="token_command", required=True)

    token_set = token_sub.add_parser(
        "set",
        help="Set/rotate the seat token this machine uses (verifies, then writes your shell rc)",
    )
    token_set.add_argument("token", nargs="?", help="Seat token (omit to paste it hidden)")
    token_set.add_argument("--shell-rc", help="Shell rc file for the token export")
    token_set.add_argument("--node-url", help="Override Node URL")
    token_set.add_argument(
        "--skip-verify", action="store_true", help="Write without checking the token against the Node"
    )
    token_set.set_defaults(handler=_token_set)

    token_create = token_sub.add_parser(
        "create",
        help="Issue a token, printed once — seat-bound (--seat, or interactive picker) or standalone",
    )
    token_create.add_argument("name", help="Token name/label")
    token_create.add_argument(
        "--seat", help="Bind the token to an EXISTING seat by slug (omit for an interactive picker)"
    )
    token_create.add_argument(
        "--role", default=None, choices=("reader", "writer", "admin"),
        help="Standalone-token role (default: reader; seat tokens inherit the seat's role)",
    )
    token_create.add_argument(
        "--kind", default=None, choices=("service_account", "user"),
        help="Standalone-token principal kind (default: service_account)",
    )
    token_create.add_argument(
        "--dataset", help="Default dataset for a standalone token (for seats use --seat)"
    )
    token_create.add_argument("--expires-at", help="ISO 8601 expiry timestamp (optional)")
    token_create.add_argument("--json", action="store_true", help="Machine-readable output")
    token_create.add_argument("--node-url", help="Override Node URL")
    token_create.set_defaults(handler=_token_create)

    token_revoke = token_sub.add_parser("revoke", help="Revoke a token by id (token_…)")
    token_revoke.add_argument("token_id", help="Token id to revoke (token_…)")
    token_revoke.add_argument("--json", action="store_true", help="Machine-readable output")
    token_revoke.add_argument("--node-url", help="Override Node URL")
    token_revoke.set_defaults(handler=_token_revoke)

    mcp = subcommands.add_parser(
        "mcp",
        help="Add the Citadel MCP server to your other coding tools",
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)

    mcp_add = mcp_sub.add_parser(
        "add",
        aliases=["install"],
        help="Add Citadel MCP to a tool (or 'all') — writes config or prints a snippet",
    )
    mcp_add.add_argument(
        "tool", help="Tool to wire: claude, cursor, codex, gemini, windsurf, cline, zed, pi, or all"
    )
    mcp_add.add_argument("--node-url", help="Override Node URL")
    mcp_add.set_defaults(handler=_mcp_add)

    mcp_list = mcp_sub.add_parser("list", help="List detected coding tools and how each is wired")
    mcp_list.set_defaults(handler=_mcp_list)

    ingest = subcommands.add_parser(
        "ingest", help="Add a durable note to your Node (HTTP; --local for the server stack)"
    )
    ingest.add_argument("data", help="Text (or a path) to ingest")
    ingest.add_argument("--tag", action="append", default=[], help="Tag to attach (repeatable)")
    ingest.add_argument(
        "--no-cognify",
        action="store_true",
        help="Skip the post-ingest cognify (faster; data appears in search later)",
    )
    ingest.add_argument("--json", action="store_true", help="Machine-readable output")
    ingest.add_argument("--node-url", help="Override Node URL")
    ingest.add_argument(
        "--local",
        action="store_true",
        help="Ingest via the in-process server stack instead of the Node (needs the server extra)",
    )
    ingest.add_argument("--dataset", help="(--local only) dataset to write to")
    ingest.add_argument("--session", help="(--local only) session id")
    ingest.set_defaults(handler=_ingest)

    search = subcommands.add_parser("search", help="Search the Organization Vault (via the Node)")
    search.add_argument("query", help="Search query")
    search.add_argument(
        "--top-k",
        "--limit",
        type=int,
        default=10,
        dest="top_k",
        help="Max results (default: 10); --limit is an agent-friendly alias",
    )
    search.add_argument("--json", action="store_true", help="Machine-readable output")
    search.add_argument(
        "--type",
        help="Comma-separated doc types to keep: spec,skill,canonical-docs,issue,activity,session-trace,other",
    )
    search.add_argument("--repo", help="Filter hits whose repo/url/snippet contain this string")
    search.add_argument(
        "--path",
        help="Filter hits whose path/url/snippet contain this string (glob markers stripped)",
    )
    search.add_argument(
        "--canonical-only",
        action="store_true",
        help="Keep spec/skill/canonical-docs (and canonical/verified trust tiers) only",
    )
    search.add_argument(
        "--exclude-ambient",
        action="store_true",
        help="Drop ambient/digest/Linear/session-trace hits (same as --mode docs filtering)",
    )
    search.add_argument(
        "--mode",
        choices=["docs"],
        default=None,
        help="docs: boost canonical/skills and exclude ambient (auto for USDCx/USDM/asset-ID queries)",
    )
    search.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Client soft-timeout seconds (default: Node search timeout); returns truncated JSON on expiry",
    )
    search.add_argument(
        "--budget-ms",
        type=int,
        default=None,
        help="Agent-friendly alias for --timeout in milliseconds (wins if both set)",
    )
    search.add_argument("--node-url", help="Override Node URL")
    search.add_argument(
        "--local",
        action="store_true",
        help="Search the local server stack instead of the Node (needs the server extra)",
    )
    search.add_argument("--dataset", help="(--local only) dataset to search")
    search.add_argument("--session", help="(--local only) session id")
    search.set_defaults(handler=_search)

    verify = subcommands.add_parser(
        "verify",
        help="Compare a local skill/reference file to nearest vault canonical pointers (JSON)",
    )
    verify.add_argument(
        "--file",
        "-f",
        required=True,
        help="Path to a local markdown/reference file (e.g. skills/.../references/*.md)",
    )
    verify.add_argument("--query", help="Override auto-built vault query from file cues")
    verify.add_argument(
        "--top-k",
        "--limit",
        type=int,
        default=12,
        dest="top_k",
        help="Max vault hits to consider (default: 12)",
    )
    verify.add_argument("--timeout", type=float, default=None, help="Client soft-timeout seconds")
    verify.add_argument("--budget-ms", type=int, default=None, help="Soft-timeout in milliseconds")
    verify.add_argument("--node-url", help="Override Node URL")
    verify.set_defaults(handler=_verify)

    prepare_pr = subcommands.add_parser(
        "prepare-pr-context",
        help="Brief JSON context for a repo+topic (canonical sources + org hits)",
    )
    prepare_pr.add_argument("--repo", required=True, help="Repo name or org/repo substring")
    prepare_pr.add_argument("--topic", required=True, help="PR/topic focus (e.g. masumi payment)")
    prepare_pr.add_argument(
        "--top-k",
        "--limit",
        type=int,
        default=12,
        dest="top_k",
        help="Max vault hits (default: 12)",
    )
    prepare_pr.add_argument("--timeout", type=float, default=None, help="Client soft-timeout seconds")
    prepare_pr.add_argument("--budget-ms", type=int, default=None, help="Soft-timeout in milliseconds")
    prepare_pr.add_argument("--node-url", help="Override Node URL")
    prepare_pr.set_defaults(handler=_prepare_pr_context)

    feedback = subcommands.add_parser("feedback", help="Attach feedback to a Cognee QA entry")
    feedback.add_argument(
        "qa_id",
        help="QA entry id to rate (the `id` from a `citadel search --local` hit)",
    )
    feedback.add_argument(
        "--score", type=int, choices=[-1, 0, 1], help="1 useful, -1 not useful, 0 neutral"
    )
    feedback.add_argument("--text", help="Optional free-text note on the rating")
    feedback.add_argument("--dataset", help="Dataset the QA entry belongs to")
    feedback.add_argument("--session", help="Session id the QA entry belongs to")
    feedback.set_defaults(handler=_feedback)

    improve = subcommands.add_parser("improve", help="Run Cognee improvement")
    improve.add_argument("--dataset", help="Dataset to improve")
    # Accept --session as an alias for parity with ingest/search/feedback.
    improve.add_argument("--session-id", "--session", action="append", dest="session_id",
                         help="Session id to improve (repeatable)")
    improve.set_defaults(handler=_improve)

    cognify = subcommands.add_parser(
        "cognify",
        help="Cognify already-added data in a dataset (recover uncognified data)",
    )
    cognify.add_argument("--dataset")
    cognify.add_argument(
        "--verify",
        action="store_true",
        help="Ingest a unique marker, cognify, and confirm it lands in the graph",
    )
    cognify.set_defaults(handler=_cognify)

    sync_github = subcommands.add_parser(
        "sync-github",
        help="Fetch GitHub organization activity and ingest a daily digest",
    )
    sync_github.add_argument("--org")
    sync_github.add_argument("--state-path")
    sync_github.add_argument("--max-repos", type=int)
    sync_github.add_argument("--max-events", type=int)
    sync_github.add_argument("--max-commits-per-repo", type=int)
    sync_github.add_argument("--force", action="store_true")
    sync_github.add_argument("--dry-run", action="store_true")
    sync_github.add_argument("--skip-improve", action="store_true")
    sync_github.add_argument("--skip-commits", action="store_true")
    sync_github.add_argument("--skip-unchanged", action="store_true")
    sync_github.set_defaults(handler=_sync_github)

    sync_repo_content = subcommands.add_parser(
        "sync-repo-content",
        help="Fetch READMEs, skills, and docs from allowlisted repos and cognify them",
    )
    sync_repo_content.add_argument("--force", action="store_true")
    sync_repo_content.add_argument("--dry-run", action="store_true")
    sync_repo_content.set_defaults(handler=_sync_repo_content)

    learn = subcommands.add_parser(
        "learn",
        help="Run the source learning agent across configured sources",
    )
    learn.add_argument("--status", action="store_true")
    learn.add_argument("--force", action="store_true")
    learn.add_argument("--dry-run", action="store_true")
    learn.add_argument("--post-to-chat", action="store_true")
    learn.add_argument("--hide-digest-preview", action="store_true")
    learn.set_defaults(handler=_learn)

    return parser


def main() -> None:
    from kb.logging_utils import configure_logging

    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    # One Ctrl-C guard around every dispatch path (first-run onboarding + handlers)
    # so quitting a prompt exits cleanly (130) instead of dumping a traceback.
    # SystemExit (carrying handler return codes) is a different type and propagates.
    try:
        if not getattr(args, "command", None):
            # Bare `citadel`: on a brand-new interactive install, drop straight into
            # guided onboarding once; afterwards show the branded home screen. The
            # opt-outs (--no-onboard / CITADEL_NO_ONBOARD / any non-TTY) keep cron,
            # CI, agents, and pipes safe — they never block on a prompt.
            opted_out = bool(os.getenv("CITADEL_NO_ONBOARD")) or getattr(args, "no_onboard", False)
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
            if interactive and not opted_out and not _already_onboarded():
                onboard_args = parser.parse_args(["onboard"])
                asyncio.run(onboard_args.handler(onboard_args))
                print()
            # Branded home screen (hero + curated command menu).
            _print_home()
            raise SystemExit(0)
        # Handlers may return an int exit code (capture/setup); others return None.
        raise SystemExit(asyncio.run(args.handler(args)) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
