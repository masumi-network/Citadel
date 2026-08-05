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

from kb import chunk_window
from kb.secure_http import open_secure

from kb.retry import run_with_retries
from kb.security_scan import SecurityScanEntry, redact_secrets, scan_text_entries

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_THRESHOLD_CHARS = 4000
DEFAULT_MAX_CHUNK_CHARS = 4000
DEFAULT_DETERMINISTIC_CHUNK_CHARS = 1200
DETERMINISTIC_CHUNK_CHARS_ENV = "CITADEL_DETERMINISTIC_CHUNK_CHARS"
DETERMINISTIC_BPE_SAFETY_MARGIN_TOKENS = 32
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


def deterministic_chunk_chars() -> int:
    """Return the character backstop used by the lossless pre-ingest splitter.

    The actual chunk boundary is chosen in Cognee's GPT-4o BPE units. This cap
    prevents a tokenizer failure from producing an unbounded piece.
    """
    return max(
        1,
        _int_env(
            DETERMINISTIC_CHUNK_CHARS_ENV,
            default=DEFAULT_DETERMINISTIC_CHUNK_CHARS,
        ),
    )


def deterministic_chunk_bpe_budget() -> int:
    """Return the BPE budget the pre-ingest splitter must fit.

    Leave room for small counter differences between Citadel's lossless replica
    and the installed Cognee tokenizer path. Lower explicit values still take
    effect through :func:`chunk_window.resolve_chunk_budget`.
    """
    safe_observed_budget = max(
        1,
        chunk_window.OBSERVED_CHUNK_BUDGET_TOKENS
        - DETERMINISTIC_BPE_SAFETY_MARGIN_TOKENS,
    )
    return max(
        1,
        min(
            chunk_window.resolve_chunk_budget(),
            safe_observed_budget,
        ),
    )


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
        pieces = _split_oversized_paragraph(paragraph, max_chars=max_chars)
        if len(pieces) > 1:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(pieces)
            continue
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


_SAFE_SPLIT_CHARS = ("\n", "\r", " ", "\t", ".", ",", ";", "!", "?", ")", "]", "}")


def _split_oversized_paragraph(paragraph: str, *, max_chars: int) -> list[str]:
    """Split one paragraph without silently dropping its bytes.

    Cognee's paragraph fallback leaves a long table row, code block, or minified
    line intact. Prefer a nearby structural boundary, then use a hard boundary
    when the input has none. The whole source was security-scanned before this
    function runs, so splitting never acts as a secret-scan bypass.
    """
    if len(paragraph) <= max_chars:
        return [paragraph]

    pieces: list[str] = []
    start = 0
    while start < len(paragraph):
        limit = min(start + max_chars, len(paragraph))
        if limit == len(paragraph):
            cut = limit
        else:
            boundary = max(
                (paragraph.rfind(char, start, limit) for char in _SAFE_SPLIT_CHARS),
                default=-1,
            )
            cut = boundary + 1 if boundary > start else limit
        pieces.append(paragraph[start:cut])
        start = cut
    return pieces


def _count_bpe(text: str) -> int | None:
    encoding = chunk_window._bpe_encoding()
    if encoding is None:
        return None
    try:
        # Cognee's chunk_by_sentence sums token counts for each segment from
        # chunk_by_word. Counting the whole string would undercount merges at
        # segment boundaries and let Cognee split the piece again downstream.
        return sum(
            len(encoding.encode(word))
            for word in chunk_window.split_cognee_words(text)
        )
    except Exception:  # pragma: no cover - tokenizer failure is fail-soft.
        return None


def _largest_fitting_prefix(
    text: str,
    *,
    prefix: str,
    max_chars: int,
    budget: int,
) -> int:
    """Find the largest exact character prefix that fits ``budget`` BPE tokens.

    Walk Cognee's segments once. Only the segment that crosses the budget needs
    a character search, which avoids retokenizing the whole candidate for every
    binary-search probe.
    """
    upper = min(len(text), max_chars)
    if upper <= 0:
        return 0
    encoding = chunk_window._bpe_encoding()
    if encoding is None:
        return upper

    candidate = prefix + text[:upper]
    prefix_length = len(prefix)
    candidate_length = len(candidate)
    consumed = 0
    cursor = 0
    for word in chunk_window._iter_cognee_words(candidate):
        word_start = cursor
        word_end = word_start + len(word)
        cursor = word_end
        word_tokens = len(encoding.encode(word))
        if consumed + word_tokens <= budget:
            consumed += word_tokens
            if word_end > prefix_length:
                fitting_end = min(word_end, candidate_length) - prefix_length
            else:
                fitting_end = 0
            if fitting_end >= upper:
                return upper
            continue

        if word_end <= prefix_length:
            return 0

        body_start = max(0, prefix_length - word_start)
        body_end = min(len(word), candidate_length - word_start)
        available = max(0, body_end - body_start)
        low = 0
        high = available
        best = 0
        while low <= high:
            middle = (low + high) // 2
            word_tokens = len(encoding.encode(word[: body_start + middle]))
            if consumed + word_tokens <= budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return max(0, word_start + body_start + best - prefix_length)

    return upper


def _prefer_safe_boundary(text: str, *, start: int, end: int) -> int:
    """Move a fitting cut left to a nearby boundary without dropping text."""
    boundary = max(
        (text.rfind(char, start, end) for char in _SAFE_SPLIT_CHARS),
        default=-1,
    )
    return boundary + 1 if boundary > start else end


def _lossless_chunks(
    data: str,
    *,
    prefix: str = "",
    max_chars: int,
    bpe_budget: int,
) -> list[str]:
    """Split exact text while accounting for an optional repeated prefix."""
    if not data:
        return [prefix] if prefix else []
    body_budget = max(1, max_chars - len(prefix))
    chunks: list[str] = []
    start = 0
    while start < len(data):
        fitting = _largest_fitting_prefix(
            data[start:],
            prefix=prefix,
            max_chars=body_budget,
            budget=bpe_budget,
        )
        cut = (
            fitting
            if start + fitting >= len(data)
            else _prefer_safe_boundary(data, start=start, end=start + fitting)
        )
        if cut <= start:
            cut = min(start + 1, len(data))
        chunks.append(prefix + data[start:cut])
        start = cut
    return chunks


def deterministic_source_chunks(
    data: str,
    *,
    max_chars: int | None = None,
) -> list[str]:
    """Create lossless, BPE-bounded chunks for every ingest path.

    Repo-content documents carry a sync header before ``---``. That header is
    repeated on every piece so a later search can still resolve each piece to
    its repository path and blob. Other inputs use the paragraph splitter
    directly. The body text is sliced, never stripped or regrouped.
    """
    limit = deterministic_chunk_chars() if max_chars is None else max_chars
    limit = max(1, limit)
    bpe_budget = deterministic_chunk_bpe_budget()
    marker = "\n---\n\n"
    marker_index = data.find(marker)
    header = data[:marker_index] if marker_index >= 0 else ""
    is_repo_header = data.startswith("# ") and all(
        f"\n{field}:" in header
        for field in ("Repository", "Source", "Commit", "Blob")
    )
    if is_repo_header:
        prefix_end = marker_index + len(marker)
        prefix = data[:prefix_end]
        body = data[prefix_end:]
        if body.strip():
            if len(prefix) >= limit:
                raise ValueError(
                    "repo content provenance header exceeds the deterministic "
                    f"chunk limit ({len(prefix)} >= {limit} characters)"
                )
            return _lossless_chunks(
                body,
                prefix=prefix,
                max_chars=limit,
                bpe_budget=bpe_budget,
            )
    return _lossless_chunks(
        data,
        max_chars=limit,
        bpe_budget=bpe_budget,
    )


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
