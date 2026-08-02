from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from kb.config import CitadelConfig
from kb.llm_enrichment import default_llm_model, openrouter_api_key, openrouter_endpoint
from kb.retry import run_with_retries
from kb.secure_http import open_secure
from kb.security_scan import redact_secrets

logger = logging.getLogger(__name__)

# litellm's provider-routing form ("openrouter/<vendor>/<model>") that cognee
# requires in LLM_MODEL. OpenRouter's own API rejects it as a model id.
LITELLM_OPENROUTER_PREFIX = "openrouter/"

_OPERATION = "organization_digest.llm_agent_read"
_MODEL_LOG_CHARS = 160
_ERROR_BODY_LOG_CHARS = 500


def _github_result(result: dict[str, Any]) -> dict[str, Any]:
    sources = result.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get("github"), dict):
        return sources["github"]
    return result


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _short(value: Any, *, length: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= length:
        return text
    return f"{text[: length - 1]}."


def organization_digest_summary(result: dict[str, Any]) -> dict[str, Any]:
    github = _github_result(result)
    sources = result.get("sources") if isinstance(result.get("sources"), dict) else {}
    vault = sources.get("vault") if isinstance(sources.get("vault"), dict) else {}
    active_repositories = _items(github.get("active_repositories"))
    return {
        "org": github.get("org"),
        "checked_at": github.get("checked_at"),
        "window_started_at": github.get("window_started_at"),
        "repos_scanned": int(github.get("repos_scanned") or 0),
        "private_repositories": int(github.get("private_repo_count") or 0),
        "contains_private_repositories": bool(github.get("contains_private_repositories")),
        "changed_repositories": int(github.get("changed_count") or 0),
        "events": int(github.get("event_count") or 0),
        "commits": int(github.get("commit_count") or 0),
        "open_pull_requests": int(github.get("open_pull_request_count") or 0),
        "merged_pull_requests": int(github.get("merged_pull_request_count") or 0),
        "vault_context_items": len(_items(vault.get("recent_context"))),
        "active_repositories": [row.get("repo") for row in active_repositories[:5] if row.get("repo")],
    }


def has_meaningful_source_changes(result: dict[str, Any]) -> bool:
    summary = organization_digest_summary(result)
    return any(
        int(summary[key] or 0) > 0
        for key in (
            "changed_repositories",
            "events",
            "commits",
            "open_pull_requests",
            "merged_pull_requests",
        )
    )


def build_source_packet(result: dict[str, Any], config: CitadelConfig) -> dict[str, Any]:
    github = _github_result(result)
    max_items = max(1, config.organization_digest_max_items)
    return {
        "kind": "organization_update_digest_source_packet",
        "rules": {
            "tone": "constructive, source-linked, action-oriented",
            "avoid": [
                "people productivity rankings",
                "blame",
                "secret values",
                "raw chat transcript summaries",
                "claims without source pointers",
            ],
        },
        "summary": organization_digest_summary(result),
        "source_url": github.get("source_url"),
        "changed_repositories": _items(github.get("changed_repositories"))[:max_items],
        "open_pull_requests": _items(github.get("open_pull_requests"))[:max_items],
        "merged_pull_requests": _items(github.get("merged_pull_requests"))[:max_items],
        "recent_commits": _items(github.get("recent_commits"))[:max_items],
        "recent_events": _items(github.get("recent_events"))[:max_items],
        "active_repositories": _items(github.get("active_repositories"))[:max_items],
        "vault_context": _items(
            (
                (result.get("sources") or {}).get("vault")
                if isinstance(result.get("sources"), dict)
                else {}
            ).get("recent_context")
        )[:max_items],
    }


def deterministic_agent_read(packet: dict[str, Any]) -> list[str]:
    summary = packet.get("summary") or {}
    active = packet.get("active_repositories") or []
    open_prs = packet.get("open_pull_requests") or []
    merged_prs = packet.get("merged_pull_requests") or []
    commits = packet.get("recent_commits") or []
    vault_context = packet.get("vault_context") or []
    lines: list[str] = []

    active_names = [row.get("repo") for row in active if isinstance(row, dict) and row.get("repo")]
    if active_names:
        lines.append(
            "Momentum appears concentrated in "
            f"{', '.join(active_names[:3])}; review the linked PRs and commits for details."
        )
    if open_prs:
        first = open_prs[0]
        lines.append(
            "Open PR attention starts with "
            f"{first.get('repo')}#{first.get('number')}: {_short(first.get('title'), length=100)}."
        )
    if merged_prs:
        lines.append(
            f"{len(merged_prs)} pull request(s) merged in the window; check whether follow-up "
            "docs, deploy notes, or review comments need closure."
        )
    if not lines and commits:
        lines.append(
            "Recent commits exist, but no active or merged PRs stood out from the source packet."
        )
    if vault_context:
        lines.append(
            "The Organization Vault has recent decision or ongoing-work context; use the linked "
            "items to connect repository movement to team intent."
        )
    if not lines:
        lines.append("No meaningful source-linked repository updates were found in this window.")

    if summary.get("events") and not open_prs and not merged_prs:
        lines.append(
            "There is repository activity without clear PR movement; verify whether any work "
            "needs to be converted into reviewable pull requests."
        )
    return lines[:3]


def resolve_openrouter_model() -> tuple[str, str]:
    """Resolve the digest model id as ``(configured, sent)``.

    ``configured`` is the raw value of CITADEL_ORG_DIGEST_LLM_MODEL,
    LLM_MODEL, or the enrichment default, in that order. ``sent`` is the id
    actually sent to OpenRouter: cognee needs LLM_MODEL in litellm form
    ("openrouter/<vendor>/<model>"), which OpenRouter's own API rejects, so
    that prefix is stripped here instead of asking operators to keep two
    variables in sync. A native id whose vendor happens to be "openrouter"
    (no second slash, e.g. "openrouter/auto") is left untouched.
    """
    configured = (
        os.getenv("CITADEL_ORG_DIGEST_LLM_MODEL")
        or os.getenv("LLM_MODEL")
        or default_llm_model()
    ).strip()
    sent = configured
    if sent.lower().startswith(LITELLM_OPENROUTER_PREFIX):
        remainder = sent[len(LITELLM_OPENROUTER_PREFIX) :]
        if "/" in remainder:
            sent = remainder
    return configured, sent


def _redacted_line(value: Any, *, length: int) -> str:
    """One log-safe line: whitespace collapsed, secrets redacted, then cut.

    Redaction runs on the full text before truncation so the cut can never
    split a credential out of the redaction patterns' reach.
    """
    collapsed = " ".join(str(value or "").split())
    return redact_secrets(collapsed)[:length]


def _http_error_body(exc: HTTPError) -> str:
    """Best-effort read of the error response body; never raises."""
    try:
        raw = exc.read()
    except Exception:  # pragma: no cover - a closed/absent fp must not mask the 4xx
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _openrouter_chat_diagnosable(
    messages: list[dict[str, str]],
    *,
    model: str,
    configured_model: str,
    max_tokens: int,
    timeout: int,
) -> str | None:
    """One OpenRouter chat completion whose failures are diagnosable.

    Unlike :func:`kb.llm_enrichment.openrouter_chat`, an HTTP failure here
    logs the resolved model id and the redacted response body. Production
    logged only "HTTP Error 400: Bad Request" once per evolve pass, which
    cannot distinguish a rejected model id from a context-length rejection.
    """
    api_key = openrouter_api_key()
    if not api_key:
        return None
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    request = Request(
        f"{openrouter_endpoint()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "citadel-llm",
        },
        method="POST",
    )

    def fetch() -> dict[str, Any]:
        with open_secure(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    try:
        body = run_with_retries(fetch, operation=_OPERATION)
    except HTTPError as exc:
        logger.warning(
            "%s LLM call failed with HTTP %s for model %s (configured %s); response body: %s",
            _OPERATION,
            exc.code,
            _redacted_line(model, length=_MODEL_LOG_CHARS),
            _redacted_line(configured_model, length=_MODEL_LOG_CHARS),
            _redacted_line(_http_error_body(exc), length=_ERROR_BODY_LOG_CHARS) or "<empty>",
        )
        return None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning(
            "%s LLM call failed with %s for model %s: %s",
            _OPERATION,
            exc.__class__.__name__,
            _redacted_line(model, length=_MODEL_LOG_CHARS),
            _redacted_line(str(exc), length=_ERROR_BODY_LOG_CHARS),
        )
        return None

    if not isinstance(body, dict):
        return None
    choices = body.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def llm_agent_read(packet: dict[str, Any]) -> list[str] | None:
    if not openrouter_api_key():
        return None
    configured, model = resolve_openrouter_model()
    if model != configured:
        logger.info(
            "%s using OpenRouter model %s (stripped litellm provider prefix from %s)",
            _OPERATION,
            _redacted_line(model, length=_MODEL_LOG_CHARS),
            _redacted_line(configured, length=_MODEL_LOG_CHARS),
        )
    message = _openrouter_chat_diagnosable(
        [
            {
                "role": "system",
                "content": (
                    "You write a constructive, source-linked organization update digest. "
                    "Use only the supplied source packet. Do not rank people. Do not mention "
                    "secrets. If evidence is weak, say 'appears' or 'unclear'. Return exactly "
                    "three concise bullet lines without Markdown headings."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(packet, sort_keys=True, default=str),
            },
        ],
        model=model,
        configured_model=configured,
        max_tokens=420,
        timeout=30,
    )
    if message is None:
        logger.warning(
            "Organization digest LLM read failed; falling back to deterministic read"
        )
        return None
    lines = []
    for raw_line in message.splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        if line:
            lines.append(line)
    return lines[:3] or None


def format_organization_digest_text(packet: dict[str, Any], agent_read: list[str]) -> str:
    summary = packet.get("summary") or {}
    checked_at = summary.get("checked_at") or "unknown time"
    window_started_at = summary.get("window_started_at") or "the previous window"
    lines = [
        "Masumi Org Digest - Last 24h",
        f"Source window: {window_started_at} to {checked_at}",
        "",
        "Agent read",
        *[f"- {line}" for line in agent_read],
        "",
        "Open PRs worth attention",
    ]

    open_pull_requests = packet.get("open_pull_requests") or []
    if open_pull_requests:
        for item in open_pull_requests:
            lines.append(
                "- "
                f"{item.get('repo')}#{item.get('number')} by {item.get('author') or 'unknown'}: "
                f"{_short(item.get('title'), length=120)}. {item.get('url') or ''}".rstrip()
            )
    else:
        lines.append("- No open PRs were active in the source window.")

    lines.extend(["", "Merged work"])
    merged_pull_requests = packet.get("merged_pull_requests") or []
    if merged_pull_requests:
        for item in merged_pull_requests:
            lines.append(
                "- "
                f"{item.get('repo')}#{item.get('number')} by {item.get('author') or 'unknown'}: "
                f"{_short(item.get('title'), length=120)}. {item.get('url') or ''}".rstrip()
            )
    else:
        lines.append("- No PRs were merged in the source window.")

    lines.extend(["", "Repository momentum"])
    active_repositories = packet.get("active_repositories") or []
    if active_repositories:
        for item in active_repositories:
            lines.append(
                "- "
                f"{item.get('repo')}: PRs {item.get('pull_requests', 0)}, "
                f"commits {item.get('commits', 0)}, events {item.get('events', 0)}"
            )
    else:
        lines.append("- No active repositories stood out from the source packet.")

    source_url = packet.get("source_url")
    if source_url:
        lines.extend(["", "Links", f"- GitHub org activity: {source_url}"])

    vault_context = packet.get("vault_context") or []
    if vault_context:
        lines.extend(["", "Vault context"])
        for item in vault_context:
            label = item.get("title") or item.get("source") or item.get("id") or "Vault item"
            lines.append(f"- {_short(label, length=140)}")
    return "\n".join(lines).strip()


def build_organization_digest(
    result: dict[str, Any],
    config: CitadelConfig,
    *,
    include_preview: bool,
) -> dict[str, Any]:
    if not config.organization_digest_enabled:
        return {
            "enabled": False,
            "meaningful": False,
            "summary": organization_digest_summary(result),
        }
    packet = build_source_packet(result, config)
    meaningful = has_meaningful_source_changes(result)
    agent_read_source = "none"
    agent_read = []
    if meaningful:
        contains_private = bool(packet["summary"].get("contains_private_repositories"))
        llm_allowed = not contains_private or config.organization_digest_llm_allow_private
        if config.organization_digest_llm_enabled and llm_allowed:
            agent_read = llm_agent_read(packet) or []
            agent_read_source = "llm" if agent_read else "deterministic_fallback"
        elif config.organization_digest_llm_enabled and contains_private:
            agent_read_source = "deterministic_private_metadata"
        if not agent_read:
            agent_read = deterministic_agent_read(packet)
            if agent_read_source == "none":
                agent_read_source = "deterministic_fallback"
    else:
        agent_read = deterministic_agent_read(packet)
        agent_read_source = "deterministic_fallback"

    text = format_organization_digest_text(packet, agent_read)
    logger.info(
        "Organization digest built: meaningful=%s, agent_read_source=%s",
        meaningful,
        agent_read_source,
    )
    payload: dict[str, Any] = {
        "enabled": True,
        "meaningful": meaningful,
        "agent_read_source": agent_read_source,
        "summary": packet["summary"],
    }
    if include_preview:
        payload["preview"] = text
    payload["_text"] = text
    return payload
