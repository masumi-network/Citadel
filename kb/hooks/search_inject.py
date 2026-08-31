#!/usr/bin/env python3
"""Task-aware search context for the Claude Code ``UserPromptSubmit`` hook."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit

from kb.capture_config import DEFAULT_NODE_URL
from kb.hooks.sync_start import AGENT_POLICY_REMINDER, read_hook_payload
from kb.security_scan import redact_secrets

TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
BASE_URL_ENV = "CITADEL_BASE_URL"
SEARCH_MODULE = "kb.hooks.search_inject"
HTTP_TIMEOUT_SECONDS = 5
MAX_QUERY_CHARS = 1000
MAX_CONTEXT_CHARS = 6000
MAX_SNIPPET_CHARS = 500
MAX_FIELD_CHARS = 300
MAX_RESULTS = 3
MAX_LOG_LINE_CHARS = 400
MAX_LOG_TOKEN_CHARS = 200

_URL_RE = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
_GITHUB_TASK_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues?|pulls?)/(?P<number>\d+)[^\s<>'\"`]*",
    re.IGNORECASE,
)
_LINEAR_TASK_URL_RE = re.compile(
    r"https?://(?:www\.)?linear\.app/[^\s<>'\"`]*/issue/"
    r"(?P<issue>[A-Z][A-Z0-9]*-\d+)[^\s<>'\"`]*",
    re.IGNORECASE,
)
_INLINE_COMMAND_RE = re.compile(
    r"`\s*(?:\$\s*)?(?:bash|sh|zsh|fish|pwsh|powershell|cmd|python|python3|uv|pytest|"
    r"npm|npx|yarn|pnpm|git|curl|wget|docker|kubectl|make)\b[^`]*`",
    re.IGNORECASE,
)
_COMMAND_LINE_RE = re.compile(
    r"^(?:[$>]\s+|(?:bash|sh|zsh|fish|pwsh|powershell|cmd)\b(?:\s+-[lc])?\s+|"
    r"(?:python|python3|uv|pytest|npm|npx|yarn|pnpm|git|curl|wget|docker|kubectl|make)\b\s+)",
    re.IGNORECASE,
)
_LOG_LINE_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ][^ ]+\s+)?"
    r"(?:DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL)\b(?:\s*[:|\-]|\s+)",
    re.IGNORECASE,
)
_TRACEBACK_START_RE = re.compile(r"^Traceback \(most recent call last\):\s*$")
_TRACEBACK_FRAME_RE = re.compile(r'^File\s+[\"\']')
_TRACEBACK_EXCEPTION_RE = re.compile(
    r"^(?:[\w.]+(?:Error|Exception|Warning)|KeyboardInterrupt|SystemExit|GeneratorExit)"
    r"(?:\s*:\s*.*)?$",
    re.IGNORECASE,
)
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the seat token never reaches another URL."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _clean_line(line: str) -> str:
    line = _GITHUB_TASK_URL_RE.sub(
        lambda match: f"{match.group('repo')} #{match.group('number')}",
        line,
    )
    line = _LINEAR_TASK_URL_RE.sub(lambda match: match.group("issue"), line)
    line = _URL_RE.sub(" ", line)
    line = _INLINE_COMMAND_RE.sub(" ", line)
    return line


def extract_task_query(prompt: str, *, max_chars: int = MAX_QUERY_CHARS) -> str | None:
    """Extract a bounded, human task query without logs or executable wrappers."""
    if not isinstance(prompt, str) or max_chars <= 0:
        return None

    lines: list[str] = []
    in_fence = False
    in_traceback = False
    traceback_exception_seen = False
    for raw_line in prompt.splitlines():
        stripped = raw_line.strip()
        if (
            len(stripped) > MAX_LOG_LINE_CHARS
            and any(len(token) > MAX_LOG_TOKEN_CHARS for token in stripped.split())
        ):
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TRACEBACK_START_RE.match(stripped):
            in_traceback = True
            traceback_exception_seen = False
            continue
        if in_traceback:
            if not stripped:
                continue
            if stripped.startswith(("During ", "...")):
                continue
            if _TRACEBACK_FRAME_RE.match(stripped):
                continue
            if _TRACEBACK_EXCEPTION_RE.match(stripped):
                traceback_exception_seen = True
                continue
            if not traceback_exception_seen:
                continue
            in_traceback = False
            traceback_exception_seen = False
        if not stripped or _LOG_LINE_RE.match(stripped) or _COMMAND_LINE_RE.match(stripped):
            continue
        cleaned = _clean_line(raw_line).strip()
        if cleaned:
            lines.append(cleaned)

    query = " ".join(" ".join(lines).split())
    return query[:max_chars] or None


def _search_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("invalid Citadel base URL")
    base_url = base_url.strip()
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("refusing non-HTTPS Citadel base URL")
    return f"{base_url.rstrip('/')}/search"


def _bounded_text(value: Any, *, limit: int = MAX_FIELD_CHARS) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def _first_text(*values: Any, limit: int = MAX_FIELD_CHARS) -> str:
    for value in values:
        text = _bounded_text(value, limit=limit)
        if text:
            return text
    return ""


def _provenance(result: Mapping[str, Any], envelope: Mapping[str, Any]) -> str:
    value = result.get("provenance")
    if not isinstance(value, Mapping):
        value = envelope.get("provenance")
    if isinstance(value, str):
        return _bounded_text(value)
    if not isinstance(value, Mapping):
        value = {}
    parts: list[str] = []
    for key in ("source", "repo", "path", "title", "source_url", "source_locator", "commit", "blob"):
        text = _bounded_text(value.get(key))
        if text:
            parts.append(f"{key}={text}")
    return "; ".join(parts)[:MAX_FIELD_CHARS]


def format_task_context(payload: Mapping[str, Any], *, limit: int = MAX_RESULTS) -> str:
    """Render a small, explicitly untrusted context block from a search payload."""
    if not isinstance(payload, Mapping):
        return ""
    results = payload.get("results")
    if not isinstance(results, list):
        results = payload.get("matches")
    if not isinstance(results, list):
        return ""

    bounded_limit = max(0, min(int(limit), MAX_RESULTS))
    selected = [item for item in results if isinstance(item, Mapping)][:bounded_limit]
    if not selected:
        return ""

    search_id = _first_text(payload.get("search_id"), payload.get("id")) or "unknown"
    default_dataset = _first_text(payload.get("dataset"), payload.get("primary_dataset")) or "unknown"
    lines = [
        "# Citadel task context (untrusted context)",
        f"Search ID: {search_id}",
        "",
    ]
    for index, result in enumerate(selected, start=1):
        envelope = result.get("_citadel")
        if not isinstance(envelope, Mapping):
            envelope = {}
        result_id = _first_text(result.get("id"), result.get("result_id"), envelope.get("result_id")) or "unknown"
        title = _first_text(result.get("title"), result.get("name"), envelope.get("title")) or "Untitled result"
        snippet = _first_text(
            result.get("snippet"), result.get("summary"), result.get("text"), result.get("content"),
            limit=MAX_SNIPPET_CHARS,
        )
        dataset = _first_text(result.get("dataset"), envelope.get("dataset"), default_dataset) or "unknown"
        trust_tier = _first_text(
            result.get("trust_tier"), envelope.get("trust_tier"), envelope.get("trust")
        ) or "unattested"
        provenance = _provenance(result, envelope) or "unavailable"
        lines.extend(
            [
                f"Result {index}:",
                f"- Result ID: {result_id}",
                f"- Title: {title}",
                f"- Dataset: {dataset}",
                f"- Trust tier: {trust_tier}",
                f"- Provenance: {provenance}",
                f"- Snippet: {snippet or 'unavailable'}",
                "",
            ]
        )

    return redact_secrets("\n".join(lines)[:MAX_CONTEXT_CHARS])[:MAX_CONTEXT_CHARS]


def fetch_task_hits(base_url: str, token: str, query: str, *, limit: int = MAX_RESULTS) -> dict[str, Any]:
    """POST a task query to the authenticated Node ``/search`` endpoint."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("missing Citadel access token")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("missing task query")
    url = _search_url(base_url)
    bounded_limit = max(1, min(int(limit), MAX_RESULTS))
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": query, "top_k": bounded_limit}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token.strip()}",
        },
        method="POST",
    )
    with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", None)
        if isinstance(status, int) and not 200 <= status < 300:
            raise ValueError("unexpected search response status")
        body = response.read().decode("utf-8")
    payload = json.loads(body) if body else None
    if not isinstance(payload, dict):
        raise ValueError("malformed search response")
    results = payload.get("results")
    if results is None:
        results = payload.get("matches")
    if not isinstance(results, list):
        raise ValueError("malformed search response")
    payload["results"] = results
    return payload


def _base_url() -> str:
    configured = os.getenv(BASE_URL_ENV)
    return configured.rstrip("/") if configured else DEFAULT_NODE_URL


def run(stream_in: Any) -> int:
    """Hook entrypoint. Always emits policy and always returns zero."""
    try:
        payload = read_hook_payload(stream_in)
        prompt = payload.get("prompt")
        query = extract_task_query(prompt) if isinstance(prompt, str) else None
        token = os.getenv(TOKEN_ENV)
        if query and token:
            # Redact BEFORE transmission: a prompt can carry live credentials,
            # and the caller's own token must never ride its own query.
            safe_query = redact_secrets(query, token)
            hits = fetch_task_hits(_base_url(), token, safe_query, limit=MAX_RESULTS)
            context = format_task_context(hits, limit=MAX_RESULTS)
            if context:
                context = redact_secrets(context, token)
                sys.stdout.write(context + "\n\n")
    except Exception:
        pass
    sys.stdout.write(AGENT_POLICY_REMINDER + "\n")
    return 0


def main() -> None:
    sys.exit(run(sys.stdin))


if __name__ == "__main__":
    main()
