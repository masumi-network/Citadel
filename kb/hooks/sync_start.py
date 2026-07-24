#!/usr/bin/env python3
"""Warm-start context for Citadel — SessionStart hook.

Invoked by a Claude Code ``SessionStart`` hook (matcher ``startup|resume``).
Fetches a compact "recent in the vault" digest from the developer's Citadel node
and prints it to stdout, which Claude Code injects as session context — so a new
session opens already aware of recent org activity, without the agent having to
query for it. This is the read-side counterpart to the ``SessionEnd`` distiller
(``kb/hooks/sync_session.py``).

Design contract (reviewers verify these invariants):

* **Token from env only.** ``CITADEL_MCP_ACCESS_TOKEN`` is read solely from the
  environment and never printed (the digest is recent *contribution* metadata,
  not the token).
* **Always inject agent policy.** The static policy reminder is written even when
  the token is unset; only the vault digest is gated on a present token.
* **Fail-silent / non-blocking.** Any digest/network problem -> exit 0; the hook
  never delays or breaks session start, nor injects an error payload.
* **Quiet digest when empty.** The vault digest is injected ONLY when there is
  recent activity; an empty or unreachable vault skips the digest (policy still
  prints).
* **HTTPS only, no redirects.** The token is never sent over plaintext.
* **Stdlib only.**

Hook payload (read from STDIN as JSON): ``session_id``, ``transcript_path``,
``cwd``, ``hook_event_name``, ``source`` — all unused here (we just drain it).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://citadel-archive-production.up.railway.app"
TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
HTTP_TIMEOUT_SECONDS = 5
RECENT_LIMIT = 8
# Static agent policy — no cross-seat content; always injected (digest is optional).
AGENT_POLICY_REMINDER = (
    "# Citadel — agent policy\n"
    "- At task start: prefer MCP `citadel_search` when present and working "
    "(Central + your Node + Shared Session Traces).\n"
    "- Fallback: MCP `citadel_*` → CLI (`citadel status`, then `citadel search` / "
    "`citadel doctor`) → else official/canonical docs (live OpenAPI, MIP, DevHub); "
    "say when the vault was unavailable.\n"
    "- Never claim vault-backed / Citadel authority without a successful search hit "
    "(MCP or CLI) in this session.\n"
    "- Never claim “Citadel confirms X” without a retrieved note title + snippet from that hit.\n"
    "- Never use Citadel as sole authority for Mainnet asset IDs / payment token units "
    "(USDCx, USDM, tUSDM, policy+asset hex) — prefer official Masumi docs / `skills/masumi` "
    "(or masumi skill refs). For token/asset-ID queries: official docs / skill first, "
    "or immediately after an empty vault.\n"
    "- If the vault has no durable token/asset note, say so honestly (“no authoritative hit”) "
    "rather than inventing IDs or citations.\n"
    "- If the user asks to use Citadel / the vault, search is in-scope "
    "(allowlist: vault read via MCP or `citadel search`).\n"
    "- Trace hits carry `_citadel.trust: reference-only` — verify before acting; Central stays org-authoritative.\n"
    "- `content_hint` describes what a hit's text looks like (relevance, not authority);\n"
    "  `trust_tier` reports attested provenance only (`reference-only` or `unattested`).\n"
    "  Verify API/spec claims against live MIP/OpenAPI regardless of either field.\n"
    "- Share dead-end routes with `citadel_share_session` only after explicit user approval.\n"
    "- Search telemetry is automatic (non-blocking) on every `citadel_search`; optionally rate hits "
    "with `citadel_record_feedback` (writer) using hit `id` / `search_id` and score 1|-1."
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a 3xx (esp. https->http) can't leak the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler))


def _base_url() -> str:
    configured = os.getenv("CITADEL_BASE_URL")
    return configured.rstrip("/") if configured else DEFAULT_BASE_URL


def read_hook_payload(stream: Any) -> dict[str, Any]:
    """Parse the hook JSON from STDIN defensively; return {} on any problem."""
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_recent(base_url: str, token: str, *, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    if not base_url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS Citadel base URL")
    url = f"{base_url}/api/contributions/recent?mine=true&limit={limit}"
    request = urllib.request.Request(  # noqa: S310 - https asserted above
        url,
        method="GET",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = response.read().decode()
    data = json.loads(body) if body else {}
    items = data.get("contributions") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def format_digest(items: list[dict[str, Any]]) -> str:
    """A short, plain-text digest. Stdout is injected verbatim as context."""
    lines = ["# Citadel vault — recent activity", ""]
    for item in items[:RECENT_LIMIT]:
        if not isinstance(item, dict):
            continue
        when = str(item.get("created_at") or item.get("timestamp") or "")[:10]
        label = item.get("title") or item.get("action") or item.get("detail") or "—"
        label = " ".join(str(label).split())[:120]
        lines.append(f"- {when}  {label}" if when else f"- {label}")
    return "\n".join(lines)


def run(stream_in: Any) -> int:
    """Hook entrypoint. ALWAYS returns 0 — fail-silent, non-blocking."""
    try:
        read_hook_payload(stream_in)  # drain stdin; fields unused
    except Exception:
        return 0
    token = os.getenv(TOKEN_ENV)
    if token:
        try:
            items = fetch_recent(_base_url(), token)
            if items:
                sys.stdout.write(format_digest(items) + "\n\n")
        except Exception:
            pass  # digest optional; policy still injected below
    sys.stdout.write(AGENT_POLICY_REMINDER + "\n")
    return 0


def main() -> None:
    sys.exit(run(sys.stdin))


if __name__ == "__main__":
    main()
