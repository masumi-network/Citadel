"""LLM-assisted chunking and enrichment for the Learning Process.

An OpenRouter-backed enricher splits large Source Material into semantically
coherent chunks, each with a one-line summary and a handful of tags. The
Learning Process treats this as a best-effort optimization: when enrichment is
disabled, the material is below the size threshold, the security scan flags
the content, the API key is missing, or the model output is unusable, callers
get a deterministic fallback and ingestion proceeds unchanged. Ingestion never
fails because of the LLM.

This module also owns the shared OpenRouter chat helper so other callers
(:mod:`kb.organization_digest`, :mod:`kb.self_improve`) do not duplicate HTTP
plumbing. All logged LLM input/output passes through
:func:`kb.security_scan.redact_secrets`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from kb.secure_http import open_secure

from kb.retry import run_with_retries
from kb.security_scan import SecurityScanEntry, redact_secrets, scan_text_entries

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_THRESHOLD_CHARS = 4000
DEFAULT_MAX_CHUNK_CHARS = 4000
MAX_CHUNKS = 20
MAX_TAGS_PER_CHUNK = 6
MIN_TAGS_PER_CHUNK = 3
SUMMARY_MAX_CHARS = 200
LOG_PREVIEW_CHARS = 160

ENRICHMENT_SYSTEM_PROMPT = (
    "You split raw source material into semantically coherent chunks for a "
    "knowledge index. Return ONLY a JSON object shaped as "
    '{"chunks": [{"text": "...", "summary": "...", "tags": ["..."]}]}. '
    "Each chunk keeps the original wording (no rewriting), carries a one-line "
    "summary, and 3-6 short lowercase tags. Preserve all of the source text "
    "across the chunks. Never invent content and never include secrets."
)


def _bool_env(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def openrouter_api_key() -> str | None:
    """The OpenRouter credential, reusing the existing digest/Cognee env vars."""
    return os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY") or None


def openrouter_endpoint() -> str:
    return (os.getenv("LLM_ENDPOINT") or "https://openrouter.ai/api/v1").rstrip("/")


def default_llm_model() -> str:
    return os.getenv("CITADEL_LLM_MODEL") or DEFAULT_LLM_MODEL


def enrichment_enabled() -> bool:
    return _bool_env("CITADEL_LLM_ENRICHMENT_ENABLED", default=False)


def enrichment_threshold_chars() -> int:
    return max(1, _int_env(
        "CITADEL_LLM_ENRICHMENT_THRESHOLD_CHARS",
        default=DEFAULT_THRESHOLD_CHARS,
    ))


def redacted_preview(text: str, *, length: int = LOG_PREVIEW_CHARS) -> str:
    """A short, secret-redacted, single-line preview safe for logs."""
    collapsed = " ".join(str(text or "").split())
    return redact_secrets(collapsed[:length])


def openrouter_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    operation: str,
    max_tokens: int = 1600,
    temperature: float = 0.2,
    timeout: int = 60,
) -> str | None:
    """One OpenRouter chat completion; returns content text or None on failure.

    Shared by the organization digest, enrichment, and self-improvement
    callers. Transient failures retry via :func:`kb.retry.run_with_retries`;
    everything logged here is redacted.
    """
    api_key = openrouter_api_key()
    if not api_key:
        return None
    payload = {
        "model": model,
        "temperature": temperature,
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
        body = run_with_retries(fetch, operation=operation)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning(
            "%s LLM call failed with %s: %s",
            operation,
            exc.__class__.__name__,
            redacted_preview(str(exc)),
        )
        return None

    choices = body.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    logger.debug("%s LLM response preview: %s", operation, redacted_preview(content))
    return content


def parse_json_payload(content: str) -> Any | None:
    """Parse model output defensively: tolerate fences and surrounding prose."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        first_newline = text.find("\n")
        if first_newline != -1 and text[:first_newline].strip().lower() in {"json", ""}:
            text = text[first_newline + 1 :]
    for candidate in (text, _bracketed(text, "{", "}"), _bracketed(text, "[", "]")):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _bracketed(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    end = text.rfind(close_char)
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


@dataclass(frozen=True)
class EnrichedChunk:
    """One ingest-ready chunk of Source Material."""

    text: str
    summary: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnrichmentOutcome:
    chunks: tuple[EnrichedChunk, ...]
    used_llm: bool
    reason: str
    model: str | None = None

    @property
    def chunked(self) -> bool:
        return len(self.chunks) > 1 or any(
            chunk.summary or chunk.tags for chunk in self.chunks
        )


def paragraph_chunks(data: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
    """Deterministic fallback: group paragraphs into chunks up to ``max_chars``."""
    max_chars = max(1, max_chars)
    paragraphs = [part.strip() for part in data.split("\n\n") if part.strip()]
    if not paragraphs:
        return [data] if data.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        added = len(paragraph) + (2 if current else 0)
        if current and current_len + added > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += added if current_len else len(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _clean_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip().lower()
        if tag and tag not in tags:
            tags.append(tag[:60])
        if len(tags) >= MAX_TAGS_PER_CHUNK:
            break
    return tuple(tags)


def _clean_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    summary = " ".join(value.split())
    return summary[:SUMMARY_MAX_CHARS] or None


def parse_enriched_chunks(content: str) -> list[EnrichedChunk]:
    """Parse the model's chunk JSON; skip malformed entries instead of failing."""
    parsed = parse_json_payload(content)
    if isinstance(parsed, dict):
        raw_chunks = parsed.get("chunks")
    elif isinstance(parsed, list):
        raw_chunks = parsed
    else:
        raw_chunks = None
    if not isinstance(raw_chunks, list):
        return []
    chunks: list[EnrichedChunk] = []
    for entry in raw_chunks[:MAX_CHUNKS]:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        chunks.append(
            EnrichedChunk(
                text=text.strip(),
                summary=_clean_summary(entry.get("summary")),
                tags=_clean_tags(entry.get("tags")),
            )
        )
    return chunks


def _fallback(data: str, reason: str) -> EnrichmentOutcome:
    chunks = tuple(EnrichedChunk(text=chunk) for chunk in paragraph_chunks(data))
    if not chunks:
        chunks = (EnrichedChunk(text=data),)
    return EnrichmentOutcome(chunks=chunks, used_llm=False, reason=reason)


def _passthrough(data: str, reason: str) -> EnrichmentOutcome:
    return EnrichmentOutcome(
        chunks=(EnrichedChunk(text=data),),
        used_llm=False,
        reason=reason,
    )


def content_flagged_by_security_scan(data: str) -> bool:
    """True when the pre-ingest security scan blocks the material."""
    try:
        scan = scan_text_entries(
            [SecurityScanEntry(source="learning_process", location="pre_ingest", text=data)],
            block_severity="high",
        )
    except Exception:  # pragma: no cover - defensive; scan is best-effort.
        return True
    return bool(scan.get("blocked"))


def enrich_source_material(data: str) -> EnrichmentOutcome:
    """Chunk + enrich Source Material; never raises.

    - Disabled or below threshold: single pass-through chunk (no behavior
      change versus pre-enrichment ingestion).
    - Enabled but the security scan flags the content: deterministic
      paragraph-boundary chunking; the content is never sent to the LLM.
    - Enabled but the key is missing, the call fails, or the output is
      unusable: deterministic paragraph-boundary chunking.
    """
    if not enrichment_enabled():
        return _passthrough(data, "disabled")
    if len(data) < enrichment_threshold_chars():
        return _passthrough(data, "below_threshold")
    if content_flagged_by_security_scan(data):
        logger.warning(
            "LLM enrichment skipped: security scan flagged the source material"
        )
        return _fallback(data, "security_flagged")
    if not openrouter_api_key():
        return _fallback(data, "no_api_key")

    model = default_llm_model()
    logger.info(
        "LLM enrichment starting: model=%s, chars=%d, preview=%s",
        model,
        len(data),
        redacted_preview(data),
    )
    content = openrouter_chat(
        [
            {"role": "system", "content": ENRICHMENT_SYSTEM_PROMPT},
            {"role": "user", "content": data},
        ],
        model=model,
        operation="llm_enrichment.chunk",
    )
    if content is None:
        return _fallback(data, "llm_failed")
    chunks = parse_enriched_chunks(content)
    if not chunks:
        logger.warning(
            "LLM enrichment returned unusable output; using paragraph fallback: %s",
            redacted_preview(content),
        )
        return _fallback(data, "unparseable_output")
    logger.info("LLM enrichment produced %d chunk(s) with model %s", len(chunks), model)
    return EnrichmentOutcome(
        chunks=tuple(chunks),
        used_llm=True,
        reason="llm",
        model=model,
    )
