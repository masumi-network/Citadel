"""Shared Session Trace server-side enrichment (ADR-0011)."""

from __future__ import annotations

import logging
import re

from kb.llm_enrichment import (
    content_flagged_by_security_scan,
    default_llm_model,
    enrichment_enabled,
    openrouter_chat,
)
from kb.session_trace_distill import (
    DeadEnd,
    SessionTraceRecord,
    ToolErrorPair,
    distill_node_note,
    distill_trace,
    format_compact_context,
    iter_transcript_entries,
    redact_commands,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DeadEnd",
    "SessionTraceRecord",
    "ToolErrorPair",
    "distill_node_note",
    "distill_trace",
    "format_compact_context",
    "iter_transcript_entries",
    "redact_commands",
    "enrich_shared_trace",
    "force_shared_trace_author_seat",
    "share_session_tags",
]

_AUTHOR_SEAT_LINE = re.compile(r"^Author-Seat:\s*.+$", re.MULTILINE)


def force_shared_trace_author_seat(data: str, seat_slug: str) -> str:
    """Pin Author-Seat metadata to the authenticated seat (never caller-supplied)."""
    line = f"Author-Seat: {seat_slug.strip()}"
    if _AUTHOR_SEAT_LINE.search(data):
        # Rewrite EVERY occurrence, not just the first. The document is chunked
        # downstream and author_seat is re-read per chunk, so a second
        # "Author-Seat:" line further down survives into a tail chunk and
        # attributes the trace to whichever seat the author typed there.
        return _AUTHOR_SEAT_LINE.sub(line, data)
    lines = data.splitlines()
    if lines and lines[0].strip() == "# Shared Session Trace":
        return "\n".join([lines[0], line, *lines[1:]])
    return f"{line}\n{data}"


def enrich_shared_trace(data: str, *, has_tool_errors: bool) -> str:
    """Optional server LLM pass to refine dead-end wording when tool errors exist."""
    if not has_tool_errors or not enrichment_enabled():
        return data
    prompt = (
        "You refine a Shared Session Trace for teammates. Keep the same sections and "
        "metadata lines. Improve dead-end lines to be concise and actionable. "
        "Do not invent facts. Return markdown only.\n\n"
        f"{data}"
    )
    content = openrouter_chat(
        [{"role": "user", "content": prompt}],
        model=default_llm_model(),
        operation="session_trace_dead_ends",
        max_tokens=800,
        temperature=0.1,
    )
    if not content:
        return data
    if content_flagged_by_security_scan(content):
        logger.warning("shared trace LLM output blocked by security scan; using deterministic text")
        return data
    return content.strip()


def share_session_tags(record: SessionTraceRecord) -> list[str]:
    tags = ["shared-session-trace", f"author:{record.author_seat}"]
    if record.repo:
        tags.append(record.repo)
    if record.branch:
        tags.append(record.branch)
    return tags
