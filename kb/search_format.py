"""Agent-friendly search result shaping and lightweight relevance helpers.

Used by the CLI (`citadel search --json`) and optionally by ranking passes.
Keeps a stable hit schema agents can filter on without a second fetch.
"""

from __future__ import annotations

import re
from typing import Any

SPEC_QUERY_RE = re.compile(
    r"\b(endpoint|openapi|mip-?\d*|request\s*body|schema|postman|status\s*enum)\b",
    re.IGNORECASE,
)
SPEC_PATH_RE = re.compile(
    r"(mip-?\d+|openapi|\.ya?ml$|/docs/|postman|swagger|SKILL\.md)",
    re.IGNORECASE,
)
ACTIVITY_RE = re.compile(
    r"(daily\s+digest|organization\s+update|github\s+org|linear\s+sync)",
    re.IGNORECASE,
)
# Cardano policy IDs are 56 hex chars; asset names / units are often longer hex.
HEX_ASSET_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{56,})(?![0-9a-fA-F])")
TOKEN_ASSET_QUERY_RE = re.compile(
    r"\b("
    r"usdcx|usdm|tusdm|payment\s*token|asset\s*id|policy\s*id|token\s*unit|"
    r"payment\s*unit|mainnet\s+asset|fingerprint|policy\s*\+?\s*asset"
    r")\b",
    re.IGNORECASE,
)

# Structured error codes shared with CLI / status readiness.
CODE_TIMEOUT = "TIMEOUT"
CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
CODE_SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"

DOC_TYPE_SPEC = "spec"
DOC_TYPE_SKILL = "skill"
DOC_TYPE_CANONICAL = "canonical-docs"
DOC_TYPE_ISSUE = "issue"
DOC_TYPE_ACTIVITY = "activity"
DOC_TYPE_TRACE = "session-trace"
DOC_TYPE_OTHER = "other"

# ``trust_tier`` carries ATTESTED facts only — things the server itself knows
# about where a hit came from. Nothing derived from a hit's body may appear
# here: ingested text is author-controlled (a public GitHub issue title reaches
# the org digest), so a body-derived tier is forgeable by anyone who can get
# text into the vault. What the text *looks like* is reported separately as
# ``content_hint``, which makes no authority claim.
TRUST_REFERENCE = "reference-only"
TRUST_UNATTESTED = "unattested"

# Retained so older parsers and stored telemetry keep resolving; never assigned.
TRUST_CANONICAL = "canonical"
TRUST_VERIFIED = "verified"
TRUST_DERIVED = "derived"
TRUST_AMBIENT = "ambient"

HINT_UNCLASSIFIED = "unclassified"
# doc_types that describe shaped documentation rather than chatter/activity.
DOC_SHAPED_TYPES = frozenset({DOC_TYPE_SPEC, DOC_TYPE_CANONICAL, DOC_TYPE_SKILL})
# doc_types that are activity/pointer material rather than reference material.
AMBIENT_DOC_TYPES = frozenset({DOC_TYPE_ACTIVITY, DOC_TYPE_ISSUE, DOC_TYPE_TRACE})


def is_spec_mode_query(query: str) -> bool:
    return bool(SPEC_QUERY_RE.search(query or ""))


def extract_hex_needles(query: str) -> list[str]:
    """Hex-like policy/asset substrings (56+ hex) from a query, lowercased."""
    return [match.group(1).lower() for match in HEX_ASSET_RE.finditer(query or "")]


def is_token_asset_query(query: str) -> bool:
    """True when the query is about payment tokens / Mainnet asset IDs / units."""
    text = query or ""
    return bool(TOKEN_ASSET_QUERY_RE.search(text)) or bool(extract_hex_needles(text))


def is_docs_mode_query(query: str, *, mode: str | None = None) -> bool:
    """Explicit ``docs`` mode, or auto when the query looks like token/asset IDs."""
    if isinstance(mode, str) and mode.strip().lower() == "docs":
        return True
    return is_token_asset_query(query)


def _hit_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("path"),
        item.get("text"),
        item.get("content"),
        item.get("summary"),
        item.get("source"),
        item.get("url"),
    ]
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    # ``dataset`` is deliberately NOT part of the content haystack: seat datasets
    # are "seat:<slug>", and a team seat innocently named "devhub" or "mip-003"
    # would relabel every personal note in it as documentation.
    parts.extend(
        [
            provenance.get("path"),
            provenance.get("source_url"),
            provenance.get("title"),
        ]
    )
    return " ".join(str(p) for p in parts if p)


def infer_doc_type(item: dict[str, Any]) -> str:
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    dataset = str(envelope.get("dataset") or "")
    if dataset == "session-traces":
        return DOC_TYPE_TRACE
    text = _hit_text(item)
    lowered = text.lower()
    # Activity/issue material is checked FIRST. A digest aggregates titles written
    # by anyone (a public-repo issue called "MIP-003 endpoint schema" lands in the
    # org digest verbatim), so testing the spec patterns first let ambient
    # material relabel itself as documentation and slip past exclude_ambient.
    if ACTIVITY_RE.search(text) or "digest" in lowered:
        return DOC_TYPE_ACTIVITY
    if "linear.app" in lowered or "linear issue" in lowered:
        return DOC_TYPE_ISSUE
    if "skill.md" in lowered or "/skills/" in lowered:
        return DOC_TYPE_SKILL
    if SPEC_PATH_RE.search(text) or "mip-" in lowered:
        return DOC_TYPE_SPEC
    if "devhub" in lowered or "docs.masumi" in lowered or "/dev/" in lowered:
        return DOC_TYPE_CANONICAL
    return DOC_TYPE_OTHER


def infer_content_hint(item: dict[str, Any], doc_type: str | None = None) -> str:
    """What the hit's text LOOKS like — a relevance signal, not an authority claim.

    Derived from the body, so a note can steer it by containing the right words.
    That is acceptable here precisely because nothing may act on it as trust:
    it orders results and labels them for a reader.
    """
    kind = doc_type or infer_doc_type(item)
    if kind == DOC_TYPE_OTHER:
        return HINT_UNCLASSIFIED
    return f"looks-like-{kind}"


def infer_trust_tier(item: dict[str, Any], doc_type: str | None = None) -> str:
    """Attested provenance only. Body text can never raise this.

    ``reference-only`` is the one tier the server can actually attest today: it
    comes from the dataset a hit was read out of (session traces), not from the
    hit's content. Everything else is ``unattested`` — the vault stores no
    per-document provenance yet, so no hit can honestly claim more.
    """
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    if envelope.get("trust") == TRUST_REFERENCE:
        return TRUST_REFERENCE
    if str(envelope.get("dataset") or "") == "session-traces":
        return TRUST_REFERENCE
    if (doc_type or infer_doc_type(item)) == DOC_TYPE_TRACE:
        return TRUST_REFERENCE
    return TRUST_UNATTESTED


def spec_mode_boost(item: dict[str, Any]) -> float:
    """Higher is better — used to re-order hits for API/spec verification queries."""
    kind = infer_doc_type(item)
    boost = {
        DOC_TYPE_SPEC: 4.0,
        DOC_TYPE_SKILL: 3.0,
        DOC_TYPE_CANONICAL: 2.5,
        DOC_TYPE_OTHER: 1.0,
        DOC_TYPE_ISSUE: 0.4,
        DOC_TYPE_ACTIVITY: 0.2,
        DOC_TYPE_TRACE: 0.3,
    }.get(kind, 1.0)
    score = item.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        boost += float(score)
    return boost


def docs_mode_boost(item: dict[str, Any]) -> float:
    """Prefer canonical/skills docs; downrank Linear/session/digest noise."""
    kind = infer_doc_type(item)
    boost = {
        DOC_TYPE_CANONICAL: 4.5,
        DOC_TYPE_SKILL: 4.0,
        DOC_TYPE_SPEC: 3.5,
        DOC_TYPE_OTHER: 1.0,
        DOC_TYPE_ISSUE: 0.25,
        DOC_TYPE_ACTIVITY: 0.15,
        DOC_TYPE_TRACE: 0.2,
    }.get(kind, 1.0)
    score = item.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        boost += float(score) * 0.25
    return boost


def asset_id_boost(item: dict[str, Any], needles: list[str]) -> float:
    """Exact/substring hex matches on id/url/path/snippet rank above fuzzy chat."""
    if not needles:
        return 0.0
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    haystack_parts = [
        item.get("id"),
        envelope.get("result_id"),
        item.get("url"),
        item.get("path"),
        item.get("source"),
        item.get("title"),
        item.get("text"),
        item.get("content"),
        item.get("summary"),
        item.get("snippet"),
    ]
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    haystack_parts.extend(
        [provenance.get("path"), provenance.get("source_url"), provenance.get("title")]
    )
    haystack = " ".join(str(part) for part in haystack_parts if part).lower()
    boost = 0.0
    for needle in needles:
        if not needle:
            continue
        if needle in haystack:
            # Prefer id/url/path hits over body-only mentions.
            id_blob = " ".join(
                str(part)
                for part in (
                    item.get("id"),
                    envelope.get("result_id"),
                    item.get("url"),
                    item.get("path"),
                )
                if part
            ).lower()
            boost += 12.0 if needle in id_blob else 8.0
    return boost


def query_rank_score(item: dict[str, Any], query: str, *, mode: str | None = None) -> float:
    """Combined ranking key for spec / docs / asset-ID queries."""
    needles = extract_hex_needles(query)
    score = asset_id_boost(item, needles)
    if is_docs_mode_query(query, mode=mode):
        score += docs_mode_boost(item)
    elif is_spec_mode_query(query):
        score += spec_mode_boost(item)
    return score


def apply_spec_mode_ranking(results: list[Any]) -> list[Any]:
    dict_hits = [item for item in results if isinstance(item, dict)]
    other = [item for item in results if not isinstance(item, dict)]
    dict_hits.sort(key=spec_mode_boost, reverse=True)
    return dict_hits + other


def apply_query_ranking(
    results: list[Any],
    query: str,
    *,
    mode: str | None = None,
) -> list[Any]:
    """Re-order hits for spec, docs/token, and/or hex asset-ID queries."""
    if not (
        extract_hex_needles(query)
        or is_docs_mode_query(query, mode=mode)
        or is_spec_mode_query(query)
    ):
        return list(results)
    dict_hits = [item for item in results if isinstance(item, dict)]
    other = [item for item in results if not isinstance(item, dict)]
    dict_hits.sort(key=lambda item: query_rank_score(item, query, mode=mode), reverse=True)
    return dict_hits + other


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_search_hit(item: Any, *, index: int = 0) -> dict[str, Any]:
    """Stable agent hit schema for CLI --json output."""
    if not isinstance(item, dict):
        text = str(item)
        return {
            "id": None,
            "title": text[:80],
            "url": None,
            "repo": None,
            "path": None,
            "doc_type": DOC_TYPE_OTHER,
            "updated_at": None,
            "score": None,
            "snippet": text[:500],
            "content_hint": HINT_UNCLASSIFIED,
            "trust_tier": TRUST_UNATTESTED,
            "rank": index + 1,
        }

    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}

    path = _first_str(item.get("path"), provenance.get("path"), metadata.get("path"))
    url = _first_str(
        item.get("url"),
        item.get("source_url"),
        provenance.get("source_url"),
        item.get("source"),
    )
    title = _first_str(item.get("title"), provenance.get("title"), metadata.get("title"))
    text = _first_str(
        item.get("text"),
        item.get("content"),
        item.get("summary"),
        item.get("chunk"),
        title,
    ) or ""
    if not title:
        title = text.split("\n", 1)[0][:120] if text else (path or url or "untitled")

    repo = _first_str(item.get("repo"), metadata.get("repo"), metadata.get("full_name"))
    if not repo and isinstance(url, str) and "github.com/" in url:
        match = re.search(r"github\.com/([^/]+/[^/]+)", url)
        if match:
            repo = match.group(1)

    doc_type = infer_doc_type(item)
    # Always recompute rather than inheriting ``_citadel.trust_tier``: rows
    # stored by an older build carry body-derived tiers like "canonical", and
    # echoing those back would reintroduce exactly the claim this schema drops.
    trust_tier = infer_trust_tier(item, doc_type)
    score = item.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = None

    return {
        "id": item.get("id") or envelope.get("result_id"),
        "title": title,
        "url": url,
        "repo": repo,
        "path": path,
        "doc_type": doc_type,
        "updated_at": _first_str(
            item.get("updated_at"),
            envelope.get("created_at"),
            metadata.get("updated_at"),
        ),
        "score": score,
        "snippet": " ".join(text.split())[:500],
        # Alias kept for older agent parsers that read ``text``.
        "text": " ".join(text.split())[:500],
        "content_hint": infer_content_hint(item, doc_type),
        "trust_tier": trust_tier,
        "rank": envelope.get("rank") or (index + 1),
        "dataset": envelope.get("dataset"),
        "_citadel": envelope or None,
    }


def _hit_envelope(hit: dict[str, Any]) -> dict[str, Any]:
    envelope = hit.get("_citadel")
    return envelope if isinstance(envelope, dict) else {}


def _hit_provenance(hit: dict[str, Any]) -> dict[str, Any]:
    provenance = _hit_envelope(hit).get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _hit_doc_type(hit: dict[str, Any]) -> str:
    return str(hit.get("doc_type") or _hit_envelope(hit).get("doc_type") or "").lower()


def _hit_trust_tier(hit: dict[str, Any]) -> str:
    envelope = _hit_envelope(hit)
    return str(
        hit.get("trust_tier") or envelope.get("trust_tier") or envelope.get("trust") or ""
    ).lower()


def _hit_blob(hit: dict[str, Any], *extra_keys: str) -> str:
    """Lowercased haystack for substring filters (shaped hits + server envelopes)."""
    provenance = _hit_provenance(hit)
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    parts: list[Any] = [
        hit.get("repo"),
        hit.get("path"),
        hit.get("url"),
        hit.get("source"),
        hit.get("snippet"),
        hit.get("text"),
        hit.get("content"),
        hit.get("title"),
        provenance.get("path"),
        provenance.get("source_url"),
        provenance.get("title"),
        metadata.get("repo"),
        metadata.get("path"),
        metadata.get("full_name"),
        # NOT envelope["dataset"]: Central is literally named after the org, so
        # repo="masumi-network" matched every hit in it, and a seat dataset
        # matched its own slug. Scoping filters must not be satisfied by the
        # name of the dataset a hit happens to live in.
    ]
    for key in extra_keys:
        parts.append(hit.get(key))
    return " ".join(str(part).lower() for part in parts if part)


def compact_search_filters(
    *,
    types: list[str] | None = None,
    repo: str | None = None,
    path: str | None = None,
    canonical_only: bool = False,
    exclude_ambient: bool = False,
    mode: str | None = None,
    dataset: str | None = None,
    top_k: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Stable filter dict for /search request bodies and search telemetry."""
    filters: dict[str, Any] = {}
    if types:
        cleaned = [str(item).strip() for item in types if str(item).strip()]
        if cleaned:
            filters["types"] = cleaned
    if isinstance(repo, str) and repo.strip():
        filters["repo"] = repo.strip()
    if isinstance(path, str) and path.strip():
        filters["path"] = path.strip()
    if canonical_only:
        filters["canonical_only"] = True
    if exclude_ambient:
        filters["exclude_ambient"] = True
    if isinstance(mode, str) and mode.strip():
        filters["mode"] = mode.strip().lower()
    if isinstance(dataset, str) and dataset.strip():
        filters["dataset"] = dataset.strip()
    if top_k is not None:
        filters["top_k"] = int(top_k)
    if limit is not None:
        filters["limit"] = int(limit)
    return filters


def filter_hits(
    hits: list[dict[str, Any]],
    *,
    types: list[str] | None = None,
    repo: str | None = None,
    path: str | None = None,
    canonical_only: bool = False,
    exclude_ambient: bool = False,
) -> list[dict[str, Any]]:
    filtered = hits
    if types:
        wanted = {t.strip().lower() for t in types if t.strip()}
        filtered = [h for h in filtered if _hit_doc_type(h) in wanted]
    if repo:
        needle = repo.lower()
        filtered = [h for h in filtered if needle in _hit_blob(h)]
    if path:
        # Treat as substring; callers may pass glob-ish **/MIP-003/** which still
        # matches as plain text on path/snippet for agent convenience.
        needle = path.replace("**/", "").replace("/**", "").replace("*", "").lower()
        if needle:
            filtered = [h for h in filtered if needle in _hit_blob(h)]
    if canonical_only:
        # Content-shaped, NOT a trust filter: it keeps hits whose text reads like
        # documentation. It cannot vouch for any of them — the tier that could
        # is attested-only now, so this deliberately no longer consults it.
        filtered = [h for h in filtered if _hit_doc_type(h) in DOC_SHAPED_TYPES]
    if exclude_ambient:
        filtered = [
            h
            for h in filtered
            if _hit_doc_type(h) not in AMBIENT_DOC_TYPES
            and _hit_trust_tier(h) != TRUST_REFERENCE
        ]
    return filtered


def token_asset_authority_warning(query: str) -> str | None:
    """Hint when agents must not treat Citadel as SoT for payment token units."""
    if not is_token_asset_query(query):
        return None
    return (
        "Payment token / Mainnet asset IDs: prefer official Masumi docs and "
        "skills/masumi — Citadel is not sole authority for policy+asset hex; "
        "say “no authoritative hit” if the vault lacks a durable token note."
    )


def shape_search_payload(
    payload: dict[str, Any],
    *,
    query: str,
    types: list[str] | None = None,
    repo: str | None = None,
    path: str | None = None,
    canonical_only: bool = False,
    exclude_ambient: bool = False,
    mode: str | None = None,
    apply_spec_ranking: bool | None = None,
) -> dict[str, Any]:
    raw_results = payload.get("results") if isinstance(payload.get("results"), list) else []
    docs_mode = is_docs_mode_query(query, mode=mode)
    if isinstance(mode, str) and mode.strip().lower() == "docs":
        exclude_ambient = True
    if apply_spec_ranking is None:
        apply_spec_ranking = is_spec_mode_query(query) and not docs_mode
    if docs_mode or extract_hex_needles(query) or apply_spec_ranking:
        ordered = apply_query_ranking(raw_results, query, mode=mode)
    else:
        ordered = list(raw_results)
    hits = [normalize_search_hit(item, index=i) for i, item in enumerate(ordered)]
    hits = filter_hits(
        hits,
        types=types,
        repo=repo,
        path=path,
        canonical_only=canonical_only,
        exclude_ambient=exclude_ambient,
    )
    warnings: list[str] = []
    if payload.get("note"):
        warnings.append(str(payload["note"]))
    timed_out = bool(payload.get("timed_out"))
    truncated = timed_out or bool(payload.get("truncated"))
    if timed_out:
        warnings.append("search timed out; results may be incomplete")
    authority = token_asset_authority_warning(query)
    if authority:
        warnings.append(authority)
    out: dict[str, Any] = {
        "query": query,
        "took_ms": payload.get("took_ms"),
        "results": hits,
        "sections": payload.get("sections"),
        "dataset": payload.get("dataset"),
        "datasets": payload.get("datasets"),
        "timed_out": timed_out,
        "truncated": truncated,
        "spec_mode": bool(apply_spec_ranking) and not docs_mode,
        "docs_mode": docs_mode,
        "warnings": warnings,
        "ok": True,
    }
    if timed_out:
        out["code"] = CODE_TIMEOUT
    elif payload.get("code"):
        out["code"] = payload["code"]
    return out
