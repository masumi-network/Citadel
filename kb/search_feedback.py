"""Implicit search telemetry for ranking / trust improvement.

Every vault search (MCP ``citadel_search``, CLI HTTP search, ``/api/knowledge``)
should emit a structured, redacted payload into the mesh feedback pipeline.
This is automatic and non-blocking — never fail the search if the write fails.

Explicit agent ratings still go through ``POST /feedback`` /
``citadel_record_feedback`` (score / text on a result or QA id).
"""

from __future__ import annotations

from hashlib import sha256
import math
from typing import Any

from kb.security_scan import redact_secrets

SCHEMA_VERSION = 1
TOP_N_DEFAULT = 10
LOW_SCORE_THRESHOLD = 0.15
MAX_QUERY_CHARS = 500
MAX_URL_CHARS = 400
MAX_ID_CHARS = 200

# Stable keys agents / ranking jobs can rely on.
PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "search_id",
        "query",
        "filters",
        "result_count",
        "top_results",
        "latency_ms",
        "timed_out",
        "truncated",
        "empty",
        "low_score",
        "tool_name",
        "client_hint",
        "seat_slug",
        "actor_id",
        "session_id",
        "datasets",
        "primary_dataset",
    }
)


# ADR-0009: a telemetry row that lands anywhere other than the caller's own Node
# carries presence only — never query text, hit summaries, or caller identity.
PRESENCE_SAFE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "search_id",
        "result_count",
        "latency_ms",
        "timed_out",
        "truncated",
        "empty",
        "low_score",
        "tool_name",
        "datasets",
        "primary_dataset",
    }
)


def presence_only_telemetry(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Drop query text, hit summaries, filters, and caller identity (ADR-0009).

    Allowlist, not denylist: a new content field added to the payload stays out
    of shared rows until it is deliberately declared presence-safe.
    """
    return {key: value for key, value in telemetry.items() if key in PRESENCE_SAFE_KEYS}


def search_id_for(*, query: str, result_count: int, datasets: list[str] | None = None) -> str:
    """Deterministic-enough id for linking explicit follow-up feedback."""
    basis = "|".join(
        [
            (query or "")[:MAX_QUERY_CHARS],
            str(int(result_count)),
            ",".join(datasets or ()),
        ]
    )
    digest = sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"search:{digest}"


def _safe_str(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text = redact_secrets(str(value)).strip()
    if not text:
        return None
    return text[:limit]


def _redacted_str(value: Any) -> str | None:
    if value is None:
        return None
    text = redact_secrets(str(value)).strip()
    return text or None


def _hit_score(item: dict[str, Any]) -> float | None:
    for key in ("score", "relevance", "similarity"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    value = envelope.get("score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def summarize_hit(item: Any, *, rank: int) -> dict[str, Any] | None:
    """Compact, secret-free hit summary for telemetry (no body text)."""
    if not isinstance(item, dict):
        return None
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    provenance = (
        envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    )
    raw_result_id = _redacted_str(
        envelope.get("result_id") or item.get("id") or item.get("url")
    )
    result_id = raw_result_id[:MAX_ID_CHARS] if raw_result_id is not None else None
    source_revision_id = _safe_str(
        envelope.get("source_revision_id") or item.get("source_revision_id"),
        limit=MAX_ID_CHARS,
    )
    source_dataset = (
        _safe_str(envelope.get("dataset") or item.get("dataset"), limit=120)
        if source_revision_id
        else None
    )
    summary: dict[str, Any] = {
        "rank": rank,
        "id": result_id,
        "url": _safe_str(item.get("url") or provenance.get("source_url"), limit=MAX_URL_CHARS),
        "doc_type": _safe_str(envelope.get("doc_type") or item.get("doc_type"), limit=64),
        "trust_tier": _safe_str(
            envelope.get("trust_tier") or envelope.get("trust") or item.get("trust_tier"),
            limit=64,
        ),
        "dataset": _safe_str(envelope.get("dataset") or item.get("dataset"), limit=120),
        "source_revision_id": source_revision_id,
        "source_dataset": source_dataset,
        "score": _hit_score(item),
    }
    if raw_result_id is not None and len(raw_result_id) > MAX_ID_CHARS:
        summary["_identity_result_id"] = raw_result_id
    # Drop nulls for a stable, compact shape.
    return {key: value for key, value in summary.items() if value is not None}


def build_search_telemetry(
    *,
    query: str,
    results: list[Any],
    datasets: list[str] | None = None,
    primary_dataset: str | None = None,
    top_k: int | None = None,
    latency_ms: float | None = None,
    timed_out: bool = False,
    truncated: bool = False,
    tool_name: str | None = None,
    client_hint: str | None = None,
    seat_slug: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
    filters: dict[str, Any] | None = None,
    top_n: int = TOP_N_DEFAULT,
) -> dict[str, Any]:
    """Build the implicit search-feedback payload (schema_version=1)."""
    safe_query = redact_secrets((query or "").strip())[:MAX_QUERY_CHARS]
    dataset_list = [str(d) for d in (datasets or []) if d]
    primary = primary_dataset or (dataset_list[0] if dataset_list else None)
    capped_n = max(1, min(int(top_n), 25))
    top_results: list[dict[str, Any]] = []
    for index, item in enumerate(results[:capped_n]):
        summary = summarize_hit(item, rank=index + 1)
        if summary:
            top_results.append(summary)

    scores = [h["score"] for h in top_results if isinstance(h.get("score"), (int, float))]
    empty = len(results) == 0
    low_score = (not empty) and (not scores or max(scores) < LOW_SCORE_THRESHOLD)

    clean_filters: dict[str, Any] = {}
    allowed_filter_keys = {
        "type",
        "types",
        "repo",
        "path",
        "canonical_only",
        "exclude_ambient",
        "mode",
        "limit",
        "top_k",
        "dataset",
    }
    if isinstance(filters, dict):
        # Prefer explicit ``types`` over singular ``type`` when both appear.
        ordered_items = sorted(
            filters.items(),
            key=lambda item: 0 if item[0] == "types" else 1 if item[0] == "type" else 2,
        )
        for key, value in ordered_items:
            if key not in allowed_filter_keys:
                continue
            if value is None or value is False or value == "" or value == []:
                continue
            if key in {"canonical_only", "exclude_ambient"}:
                clean_filters[key] = bool(value)
                continue
            if key == "mode" and isinstance(value, str) and value.strip():
                clean_filters[key] = value.strip().lower()[:32]
                continue
            if key in {"limit", "top_k"} and isinstance(value, (int, float)) and not isinstance(
                value, bool
            ):
                clean_filters[key] = int(value)
                continue
            if key in {"types", "type"}:
                if key == "type" and "types" in clean_filters:
                    continue
                if isinstance(value, list):
                    cleaned_list = [
                        item
                        for item in (_safe_str(part, limit=64) for part in value)
                        if item
                    ]
                    if cleaned_list:
                        clean_filters["types"] = cleaned_list[:20]
                    continue
                safe = _safe_str(value, limit=64)
                if safe:
                    clean_filters["types"] = [safe]
                continue
            if isinstance(value, str):
                safe = _safe_str(value, limit=200)
                if safe:
                    clean_filters[key] = safe
                continue
    if top_k is not None and "top_k" not in clean_filters and "limit" not in clean_filters:
        clean_filters["top_k"] = int(top_k)

    search_id = search_id_for(
        query=safe_query,
        result_count=len(results),
        datasets=dataset_list,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "search_telemetry",
        "search_id": search_id,
        "query": safe_query,
        "filters": clean_filters,
        "result_count": len(results),
        "top_results": top_results,
        "latency_ms": round(float(latency_ms), 1) if latency_ms is not None else None,
        "timed_out": bool(timed_out),
        "truncated": bool(truncated or timed_out),
        "empty": empty,
        "low_score": low_score,
        "tool_name": _safe_str(tool_name, limit=80),
        "client_hint": _safe_str(client_hint, limit=80),
        "seat_slug": _safe_str(seat_slug, limit=80),
        "actor_id": _safe_str(actor_id, limit=120),
        "session_id": _safe_str(session_id, limit=120),
        "datasets": dataset_list or None,
        "primary_dataset": primary,
    }
    return {key: value for key, value in payload.items() if value is not None}


def feedback_note_from_telemetry(telemetry: dict[str, Any]) -> str:
    """Human-readable durable note body if a writer path ever persists telemetry."""
    top = telemetry.get("top_results") or []
    ids = ", ".join(
        str(hit.get("id") or hit.get("url") or "?") for hit in top[:5] if isinstance(hit, dict)
    )
    return (
        f"search_telemetry search_id={telemetry.get('search_id')} "
        f"results={telemetry.get('result_count')} "
        f"empty={telemetry.get('empty')} low_score={telemetry.get('low_score')} "
        f"tool={telemetry.get('tool_name') or '-'} "
        f"top=[{ids}] "
        f"query={telemetry.get('query') or ''}"
    )


def durable_search_event(
    telemetry: dict[str, Any],
    *,
    dataset: str,
    actor_id: str | None,
    presence_only: bool = False,
) -> dict[str, Any]:
    """Select only safe fields for the durable feedback ledger."""
    top_results = telemetry.get("top_results")
    first = top_results[0] if isinstance(top_results, list) and top_results else {}
    display_result_id = first.get("id") if isinstance(first, dict) else None
    identity_result_id = (
        first.get("_identity_result_id") if isinstance(first, dict) else None
    )
    trust_tier = first.get("trust_tier") if isinstance(first, dict) else None
    score = first.get("score") if isinstance(first, dict) else None
    source_revision_id = first.get("source_revision_id") if isinstance(first, dict) else None
    source_dataset = first.get("source_dataset") if isinstance(first, dict) else None
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not -1.0 <= float(score) <= 1.0
    ):
        score = None
    if presence_only:
        actor_id = None
        display_result_id = None
        identity_result_id = None
        trust_tier = None
        score = None
        source_revision_id = None
        source_dataset = None
    result_id = (
        identity_result_id
        if isinstance(identity_result_id, str)
        else display_result_id
    )
    reason = (
        "empty_search"
        if telemetry.get("empty")
        else "low_score_search"
        if telemetry.get("low_score")
        else "search_telemetry"
    )
    event: dict[str, Any] = {
        "kind": "implicit_search",
        "search_id": telemetry.get("search_id"),
        "result_id": result_id,
        "actor_id": actor_id,
        "dataset": dataset,
        "score": score,
        "trust_tier": trust_tier,
        "reason": reason,
        "occurred_at": telemetry.get("occurred_at"),
    }
    if not presence_only and source_revision_id:
        event["source_revision_id"] = source_revision_id
        event["source_dataset"] = source_dataset or (
            first.get("dataset") if isinstance(first, dict) else None
        )
    return event


def durable_explicit_event(
    *,
    qa_id: str,
    result_id: str | None,
    search_id: str | None,
    actor_id: str | None,
    dataset: str,
    score: int | None,
    text_present: bool,
) -> dict[str, Any]:
    """Build a durable explicit event without storing free-form feedback text."""
    normalized_qa_id = qa_id.strip()
    normalized_search_id = (
        search_id.strip()
        if isinstance(search_id, str) and search_id.strip()
        else normalized_qa_id
        if normalized_qa_id.startswith("search:")
        else None
    )
    normalized_result_id = (
        result_id.strip()
        if isinstance(result_id, str) and result_id.strip()
        else None
        if normalized_qa_id.startswith("search:")
        else normalized_qa_id
    )
    return {
        "kind": "explicit_feedback",
        "search_id": normalized_search_id,
        "result_id": normalized_result_id,
        "actor_id": actor_id,
        "dataset": dataset,
        "score": score,
        "reason": "explicit_feedback_text_present" if text_present else "explicit_feedback",
    }
