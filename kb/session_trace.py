"""Shared Session Trace server-side enrichment, metadata, and withdrawal.

Author-Seat and Trace-Id pinning and the optional dead-end LLM pass are the
original ADR-0011 concerns. This module also owns Shared Session Trace metadata
and a durable withdrawal tombstone store (v1 release contract section 10):
withdrawal hides a trace from search and drilldown, records a minimal audit
tombstone, and lets search and graph paths filter it before it resurfaces.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from kb.llm_enrichment import (
    content_flagged_by_security_scan,
    enrichment_enabled,
    openrouter_chat,
)
from kb.model_routing import route_for

logger = logging.getLogger(__name__)

SHARED_TRACE_HEADER = "# Shared Session Trace"
TRACE_ID_PREFIX = "trace:"
TRACE_TRUST = "reference-only"

__all__ = [
    "enrich_shared_trace",
    "force_shared_trace_author_seat",
    "force_shared_trace_id",
    "mint_trace_id",
    "extract_trace_id",
    "result_trace_id",
    "result_source_revision_id",
    "SharedTraceMetadata",
    "TraceTombstone",
    "SessionTraceStore",
    "prune_withdrawn_graph",
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
    route = route_for("session_trace")
    content = openrouter_chat(
        [{"role": "user", "content": prompt}],
        model=route.model,
        operation="session_trace_dead_ends",
        max_tokens=800,
        temperature=0.1,
        plugins=route.plugins,
    )
    if not content:
        return data
    if content_flagged_by_security_scan(content):
        logger.warning("shared trace LLM output blocked by security scan; using deterministic text")
        return data
    return content.strip()


# The marker must stay on one line: ``\s`` would let the value span a newline,
# so an empty ``Trace-Id:`` line could steal the next content line as the ID.
# Restrict the gap to horizontal whitespace and the value to non-CR/LF text.
_TRACE_ID_LINE = re.compile(r"^Trace-Id:[ \t]*([^\r\n]+)\r?$", re.MULTILINE)
_RESULT_TEXT_KEYS = ("text", "content", "chunk", "body")


def mint_trace_id(seat_slug: str, session_id: str, data: str) -> str:
    """Deterministic stable trace ID from author, session, and content.

    Deterministic so re-sharing identical content mints the same ID and the
    withdrawal tombstone still matches. The ID is opaque: it carries no source
    or query text.
    """
    material = f"{seat_slug.strip()}\n{session_id.strip()}\n{data}"
    digest = sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{TRACE_ID_PREFIX}{digest}"


def extract_trace_id(text: str | None) -> str | None:
    """Return the ``Trace-Id:`` value embedded in trace markdown, if any."""
    if not text:
        return None
    match = _TRACE_ID_LINE.search(text)
    return match.group(1).strip() if match else None


def force_shared_trace_id(data: str, trace_id: str) -> str:
    """Pin one ``Trace-Id:`` line so every downstream chunk carries the ID.

    Mirrors ``force_shared_trace_author_seat``: rewrite every occurrence so a
    tail chunk cannot carry a stale or spoofed trace ID.
    """
    line = f"Trace-Id: {trace_id.strip()}"
    if _TRACE_ID_LINE.search(data):
        return _TRACE_ID_LINE.sub(line, data)
    lines = data.splitlines()
    if lines and lines[0].strip() == SHARED_TRACE_HEADER:
        return "\n".join([lines[0], line, *lines[1:]])
    return f"{line}\n{data}"


def _direct_trace_id(value: Any) -> str | None:
    """Return a direct ``Trace-Id``/``trace_id`` mapping value if single-line.

    A direct field is line-oriented like the marker: an embedded CR/LF would
    let a spoofed second line ride along, so reject any value that carries one.
    """
    if not isinstance(value, str):
        return None
    if "\r" in value or "\n" in value:
        return None
    stripped = value.strip()
    return stripped or None


def result_trace_id(result: Any) -> str | None:
    """Extract a trace ID from a search result or graph node, dict or string."""
    if isinstance(result, str):
        return extract_trace_id(result)
    if not isinstance(result, Mapping):
        return None
    for key in ("Trace-Id", "trace_id"):
        found = _direct_trace_id(result.get(key))
        if found:
            return found
    metadata = result.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("Trace-Id", "trace_id"):
            found = _direct_trace_id(metadata.get(key))
            if found:
                return found
    for key in _RESULT_TEXT_KEYS:
        found = extract_trace_id(result.get(key) if isinstance(result.get(key), str) else None)
        if found:
            return found
    if isinstance(metadata, Mapping):
        for key in _RESULT_TEXT_KEYS:
            candidate = metadata.get(key)
            found = extract_trace_id(candidate if isinstance(candidate, str) else None)
            if found:
                return found
    return None


def result_source_revision_id(result: Any) -> str | None:
    """Extract lifecycle source identity from a raw hit or graph node."""
    if not isinstance(result, Mapping):
        return None
    containers: list[Mapping[str, Any]] = [result]
    for key in ("metadata", "_citadel", "_lifecycle", "properties", "props"):
        value = result.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        value = container.get("source_revision_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class SharedTraceMetadata:
    """Server-held Shared Session Trace metadata (v1 contract section 10)."""

    trace_id: str
    author_principal: str
    author_kind: str = "human"
    seat_scope: str | None = None
    captured_at: str | None = None
    shared_at: str | None = None
    source_references: tuple[str, ...] = ()
    redaction_result: str = "clean"
    visibility: str = "seat-shared"
    citations: tuple[str, ...] = ()
    trust: str = TRACE_TRUST
    source_revision_ids: tuple[str, ...] = ()
    withdrawn: bool = False
    withdrawn_at: str | None = None
    withdrawal_reason: str | None = None
    withdrawn_by: str | None = None


class TraceStoreCorruptionError(RuntimeError):
    """Raised when withdrawal state cannot be trusted for privacy filtering."""


@dataclass(frozen=True)
class TraceTombstone:
    """Minimal durable withdrawal record: no source or query text."""

    trace_id: str
    actor_principal: str
    withdrawn_at: str
    reason_code: str


def _utc_now_text(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item.strip())


class SessionTraceStore:
    """Durable JSON-backed trace metadata and withdrawal tombstones.

    Withdrawal is idempotent: the first call writes the tombstone and the
    stored timestamp never moves on repeat. A tombstone is recorded even for a
    trace that was never registered, so a filter path can hide it regardless.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return {"version": 1, "traces": {}, "tombstones": {}}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TraceStoreCorruptionError(
                "Shared Session Trace withdrawal state is unreadable."
            ) from error
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("traces"), dict)
            or not isinstance(data.get("tombstones"), dict)
        ):
            raise TraceStoreCorruptionError(
                "Shared Session Trace withdrawal state has an invalid schema."
            )
        return data

    def _save(self, data: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def register(self, metadata: SharedTraceMetadata) -> SharedTraceMetadata:
        """Persist trace metadata. Preserves an existing withdrawal."""
        data = self._load()
        traces = data["traces"]
        existing = traces.get(metadata.trace_id)
        record = dict(asdict(metadata))
        if isinstance(existing, Mapping) and existing.get("withdrawn"):
            record.update(
                {
                    "withdrawn": True,
                    "withdrawn_at": existing.get("withdrawn_at"),
                    "withdrawal_reason": existing.get("withdrawal_reason"),
                    "withdrawn_by": existing.get("withdrawn_by"),
                }
            )
        traces[metadata.trace_id] = record
        self._save(data)
        return self._metadata_from_record(record)

    def get(self, trace_id: str) -> SharedTraceMetadata | None:
        record = self._load()["traces"].get(trace_id)
        if not isinstance(record, Mapping):
            return None
        return self._metadata_from_record(record)

    def withdraw(
        self,
        trace_id: str,
        *,
        actor_principal: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> TraceTombstone:
        """Record an idempotent, durable withdrawal tombstone."""
        clean_id = str(trace_id).strip()
        if not clean_id:
            raise ValueError("trace_id must be a non-empty string")
        clean_reason = str(reason_code).strip()
        if not clean_reason:
            raise ValueError("reason_code must be a non-empty string")
        clean_actor = str(actor_principal).strip()
        if not clean_actor:
            raise ValueError("actor_principal must be a non-empty string")
        data = self._load()
        tombstones = data["tombstones"]
        existing = tombstones.get(clean_id)
        if isinstance(existing, Mapping):
            return TraceTombstone(
                trace_id=clean_id,
                actor_principal=str(existing.get("actor_principal", clean_actor)),
                withdrawn_at=str(existing.get("withdrawn_at", _utc_now_text(now))),
                reason_code=str(existing.get("reason_code", clean_reason)),
            )
        withdrawn_at = _utc_now_text(now)
        tombstone = TraceTombstone(
            trace_id=clean_id,
            actor_principal=clean_actor,
            withdrawn_at=withdrawn_at,
            reason_code=clean_reason,
        )
        tombstones[clean_id] = asdict(tombstone)
        record = data["traces"].get(clean_id)
        if isinstance(record, dict):
            record.update(
                {
                    "withdrawn": True,
                    "withdrawn_at": withdrawn_at,
                    "withdrawal_reason": clean_reason,
                    "withdrawn_by": clean_actor,
                }
            )
        self._save(data)
        return tombstone

    def tombstone(self, trace_id: str) -> TraceTombstone | None:
        record = self._load()["tombstones"].get(str(trace_id).strip())
        if not isinstance(record, Mapping):
            return None
        return TraceTombstone(
            trace_id=str(record.get("trace_id", trace_id)),
            actor_principal=str(record.get("actor_principal", "")),
            withdrawn_at=str(record.get("withdrawn_at", "")),
            reason_code=str(record.get("reason_code", "")),
        )

    def is_withdrawn(self, trace_id: str | None) -> bool:
        if not trace_id:
            return False
        return str(trace_id).strip() in self._load()["tombstones"]

    def withdrawn_trace_ids(self) -> frozenset[str]:
        return frozenset(self._load()["tombstones"].keys())

    def withdrawn_source_revision_ids(self) -> frozenset[str]:
        """Return source revisions bound to every durably withdrawn trace."""
        data = self._load()
        withdrawn = set(data["tombstones"])
        revisions: set[str] = set()
        for trace_id, record in data["traces"].items():
            if trace_id not in withdrawn and not (
                isinstance(record, Mapping) and record.get("withdrawn")
            ):
                continue
            if isinstance(record, Mapping):
                revisions.update(_as_str_tuple(record.get("source_revision_ids")))
        return frozenset(revisions)

    @staticmethod
    def _metadata_from_record(record: Mapping[str, Any]) -> SharedTraceMetadata:
        return SharedTraceMetadata(
            trace_id=str(record.get("trace_id", "")),
            author_principal=str(record.get("author_principal", "")),
            author_kind=str(record.get("author_kind", "human")),
            seat_scope=record.get("seat_scope"),
            captured_at=record.get("captured_at"),
            shared_at=record.get("shared_at"),
            source_references=_as_str_tuple(record.get("source_references")),
            redaction_result=str(record.get("redaction_result", "clean")),
            visibility=str(record.get("visibility", "seat-shared")),
            citations=_as_str_tuple(record.get("citations")),
            trust=str(record.get("trust", TRACE_TRUST)),
            source_revision_ids=_as_str_tuple(record.get("source_revision_ids")),
            withdrawn=bool(record.get("withdrawn", False)),
            withdrawn_at=record.get("withdrawn_at"),
            withdrawal_reason=record.get("withdrawal_reason"),
            withdrawn_by=record.get("withdrawn_by"),
        )


def _node_carries_withdrawn_trace(
    node: Mapping[str, Any],
    withdrawn: Collection[str],
    withdrawn_source_revisions: Collection[str] = (),
) -> bool:
    trace_id = result_trace_id(node)
    if trace_id and trace_id in withdrawn:
        return True
    source_revision_id = result_source_revision_id(node)
    if source_revision_id and source_revision_id in withdrawn_source_revisions:
        return True
    # A graph node may expose its raw properties rather than trace text.
    for key in ("properties", "props", "metadata"):
        value = node.get(key)
        if isinstance(value, Mapping):
            found = result_trace_id(value)
            if found and found in withdrawn:
                return True
            source_revision_id = result_source_revision_id(value)
            if source_revision_id and source_revision_id in withdrawn_source_revisions:
                return True
    return False


def prune_withdrawn_graph(
    payload: Mapping[str, Any],
    withdrawn: Collection[str] | None,
    withdrawn_source_revisions: Collection[str] | None = None,
) -> dict[str, Any]:
    """Drop withdrawn trace nodes and their dangling edges."""
    result = dict(payload)
    withdrawn_set = {str(item).strip() for item in (withdrawn or ()) if str(item).strip()}
    source_revision_set = {
        str(item).strip()
        for item in (withdrawn_source_revisions or ())
        if str(item).strip()
    }
    if not withdrawn_set and not source_revision_set:
        return result
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return result
    removed_ids: set[str] = set()
    kept_nodes: list[Any] = []
    for node in nodes:
        if isinstance(node, Mapping) and _node_carries_withdrawn_trace(
            node,
            withdrawn_set,
            source_revision_set,
        ):
            node_id = node.get("id")
            if isinstance(node_id, str):
                removed_ids.add(node_id)
            continue
        kept_nodes.append(node)
    result["nodes"] = kept_nodes
    edges = payload.get("edges")
    if isinstance(edges, list) and removed_ids:
        kept_edges: list[Any] = []
        for edge in edges:
            if isinstance(edge, Mapping):
                source = edge.get("source")
                target = edge.get("target")
                if source in removed_ids or target in removed_ids:
                    continue
            kept_edges.append(edge)
        result["edges"] = kept_edges
    return result
