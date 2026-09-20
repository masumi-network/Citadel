#!/usr/bin/env python3
"""Warm-start context for Citadel — SessionStart hook.

Invoked by a Claude Code ``SessionStart`` hook (matcher ``startup|resume``).
Searches for bounded workspace-continuity candidates and prints them to stdout,
which Claude Code injects as session context.

Design contract:

* **Workspace identity only.** The hook uses the payload's ``cwd`` to resolve a
  Git root and current branch. It does not inspect the transcript or session.
* **Token from env only.** ``CITADEL_MCP_ACCESS_TOKEN`` is read solely from the
  environment and never printed.
* **Fail-silent / non-blocking.** Any identity, network, or response problem
  produces no continuity output and always exits 0.
* **HTTPS only, no redirects.** The token is never sent over plaintext.
* **Stdlib only.**
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any
from kb.capture_config import DEFAULT_NODE_URL as DEFAULT_BASE_URL

TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
HTTP_TIMEOUT_SECONDS = 5
SEARCH_TOP_K = 5
MAX_CANDIDATES = 3
MAX_DISPLAY_CHARS = 180
MAX_RESPONSE_BYTES = 1_000_000
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


def _git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=HTTP_TIMEOUT_SECONDS,
        check=False,
    )


def _workspace_identity(cwd: Any) -> tuple[str, str] | None:
    """Return ``(repo basename, branch)`` only for a strict Git identity."""
    if not isinstance(cwd, str) or not cwd.strip() or not os.path.isdir(cwd):
        return None
    try:
        root_result = _git_run(cwd, "rev-parse", "--show-toplevel")
        branch_result = _git_run(cwd, "branch", "--show-current")
    except Exception:
        return None
    if root_result.returncode != 0 or branch_result.returncode != 0:
        return None
    root = root_result.stdout.strip()
    branch = branch_result.stdout.strip()
    if not root or not branch or not os.path.isdir(root):
        return None
    repo_name = os.path.basename(os.path.normpath(root))
    return (repo_name, branch) if repo_name else None


def _shape_candidate_page(value: Any) -> dict[str, Any] | None:
    """Copy documented candidate-page fields, preserving explicit unknowns."""
    if not isinstance(value, dict):
        return None
    shaped: dict[str, Any] = {}
    for key in ("limit", "fetched", "matched", "returned"):
        field = value.get(key)
        if field is None or (isinstance(field, int) and not isinstance(field, bool)):
            if key in value:
                shaped[key] = field
    for key in ("selection_trimmed", "upstream_truncation"):
        field = value.get(key)
        if field is None or isinstance(field, bool):
            if key in value:
                shaped[key] = field
    return shaped


def _shape_receipt(value: Any) -> dict[str, Any] | None:
    """Validate and shallow-copy the safe receipt sections used by the hook."""
    if not isinstance(value, dict):
        return None
    candidate_page = _shape_candidate_page(value.get("candidate_page"))
    absence = value.get("absence")
    if candidate_page is None or not isinstance(absence, dict):
        return None
    if absence.get("proven") is not False:
        return None
    reason = absence.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return {
        "candidate_page": candidate_page,
        "absence": {"proven": False, "reason": " ".join(reason.split())[:120]},
    }


def _repo_matches(hit: dict[str, Any], repo_name: str) -> bool:
    """Match only explicit repo identity metadata, never searchable body text."""
    values: list[Any] = [hit.get("repo")]
    citation = hit.get("citation")
    if isinstance(citation, dict):
        values.extend([citation.get("repo"), citation.get("provenance")])
    provenance = hit.get("provenance")
    if isinstance(provenance, dict):
        values.append(provenance.get("repo"))
    envelope = hit.get("_citadel")
    if isinstance(envelope, dict):
        envelope_provenance = envelope.get("provenance")
        if isinstance(envelope_provenance, dict):
            values.append(envelope_provenance.get("repo"))
    target = repo_name.strip().lower()
    for value in values:
        if isinstance(value, dict):
            value = value.get("repo")
        if not isinstance(value, str):
            continue
        identity = value.strip().strip("/").lower()
        if identity == target or identity.endswith(f"/{target}"):
            return True
    return False


def _search_workspace(
    base_url: str,
    token: str,
    *,
    repo_name: str,
    branch: str,
) -> dict[str, Any]:
    if not base_url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS Citadel base URL")
    body = json.dumps(
        {
            "query": f"Repo: {repo_name} Branch: {branch}",
            "repo": repo_name,
            "top_k": SEARCH_TOP_K,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/search",
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise ValueError("search response too large")
    raw = raw_bytes.decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("unexpected search response")
    results = payload.get("results")
    receipt = _shape_receipt(payload.get("retrieval_receipt"))
    if (
        not isinstance(results, list)
        or any(not isinstance(item, dict) for item in results)
        or receipt is None
    ):
        raise ValueError("unexpected search response")
    matching_results = [item for item in results if _repo_matches(item, repo_name)]
    return {
        "results": matching_results,
        "candidate_page": receipt["candidate_page"],
        "absence": receipt["absence"],
    }


def _bounded_text(value: Any, limit: int = MAX_DISPLAY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]
def _receipt_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)[:32]
    return "unknown"



def _format_continuity(payload: dict[str, Any]) -> str:
    candidate_page = payload["candidate_page"]
    absence = payload["absence"]
    absence_proven = absence["proven"]
    absence_reason = _bounded_text(absence["reason"], 120)
    lines = [
        "# Citadel workspace continuity",
        "",
        "Task prompt was unavailable; these workspace continuity candidates are not exhaustive.",
        "",
        "Bounded retrieval: "
        + "; ".join(
            f"{key.replace('_', ' ')}={_receipt_value(candidate_page.get(key))}"
            for key in (
                "limit",
                "fetched",
                "matched",
                "returned",
                "selection_trimmed",
                "upstream_truncation",
            )
        )
        + f"; absence proven={'yes' if absence_proven else 'no'}"
        + f"; absence reason={absence_reason}; "
        + ("absence is not proven." if absence_proven is False else "absence state unknown."),
    ]
    for hit in payload["results"][:MAX_CANDIDATES]:
        citation = hit.get("citation")
        envelope = hit.get("_citadel")
        provenance = envelope.get("provenance") if isinstance(envelope, dict) else None
        label = ""
        for source in (hit, citation, provenance):
            if not isinstance(source, dict):
                continue
            for key in ("title", "snippet", "text", "content", "chunk"):
                label = _bounded_text(source.get(key))
                if label:
                    break
            if label:
                break
        if not label:
            continue
        metadata: list[str] = []
        dataset = hit.get("dataset")
        trust_tier = hit.get("trust_tier")
        if isinstance(envelope, dict):
            dataset = dataset or envelope.get("dataset")
            trust_tier = trust_tier or envelope.get("trust_tier") or envelope.get("trust")
        dataset_text = _bounded_text(dataset, 80)
        trust_text = _bounded_text(trust_tier, 80)
        if dataset_text:
            metadata.append(f"dataset={dataset_text}")
        if trust_text:
            metadata.append(f"trust={trust_text}")
        suffix = f" ({'; '.join(metadata)})" if metadata else ""
        lines.append(f"- {label}{suffix}")
    return "\n".join(lines)


def run(stream_in: Any) -> int:
    """Hook entrypoint. ALWAYS returns 0 — fail-silent, non-blocking."""
    try:
        payload = read_hook_payload(stream_in)
        token = os.getenv(TOKEN_ENV)
        identity = _workspace_identity(payload.get("cwd")) if token else None
        if token and identity:
            try:
                search_payload = _search_workspace(
                    _base_url(),
                    token,
                    repo_name=identity[0],
                    branch=identity[1],
                )
                sys.stdout.write(_format_continuity(search_payload) + "\n\n")
            except Exception:
                pass
        sys.stdout.write(AGENT_POLICY_REMINDER + "\n")
    except Exception:
        pass
    return 0




def main() -> None:
    sys.exit(run(sys.stdin))


if __name__ == "__main__":
    main()
