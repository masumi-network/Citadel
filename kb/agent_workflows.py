"""Thin agent workflow helpers built on search shaping.

These are CLI-facing conveniences (`citadel verify`, `citadel prepare-pr-context`),
not a separate platform — they reuse Node search and ``shape_search_payload``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kb.search_format import (
    DOC_SHAPED_TYPES,
    filter_hits,
    is_spec_mode_query,
    is_token_asset_query,
    shape_search_payload,
    token_asset_authority_warning,
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{2,}")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "your",
        "into",
        "when",
        "then",
        "than",
        "also",
        "are",
        "was",
        "were",
        "have",
        "has",
        "been",
        "will",
        "shall",
        "should",
        "could",
        "would",
        "must",
        "may",
        "not",
        "but",
        "use",
        "using",
        "used",
        "via",
        "api",
        "http",
        "https",
        "json",
        "true",
        "false",
        "null",
        "none",
        "todo",
        "note",
        "see",
        "docs",
        "documentation",
        "reference",
        "references",
        "example",
        "examples",
    }
)


def extract_verify_cues(text: str, *, limit: int = 12) -> list[str]:
    """Pull query cues from a skill/reference markdown for vault search."""
    cues: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(text or ""):
        raw = match.group(0)
        lowered = raw.lower().strip("._/-")
        if len(lowered) < 3 or lowered in _STOP:
            continue
        if lowered.startswith("http"):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        cues.append(raw if raw.isupper() or "MIP" in raw.upper() else lowered)
        if len(cues) >= limit:
            break
    # Prefer MIP-/endpoint-ish tokens first.
    cues.sort(
        key=lambda c: (
            0 if re.search(r"mip-?\d+", c, re.I) else 1,
            0 if re.search(r"endpoint|openapi|schema|payment|purchase", c, re.I) else 1,
            c.lower(),
        )
    )
    return cues[:limit]


def build_verify_query(path: Path, text: str) -> str:
    stem = path.stem.replace("-", " ").replace("_", " ")
    cues = extract_verify_cues(text)
    parts = [stem, *cues[:8]]
    # Bias toward spec-mode so ranking prefers MIP/OpenAPI/skills.
    if not is_spec_mode_query(" ".join(parts)):
        parts.append("schema endpoint")
    return " ".join(parts)


def _doc_shaped_sources(hits: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    """Hits whose TEXT reads like documentation. Not a vouch for any of them.

    Selection is on shape alone. It used to also accept a canonical/verified
    ``trust_tier`` and, when nothing qualified, silently fall back to every hit —
    so a brief labelled "canonical sources" could be entirely chat notes.
    An empty list is the honest answer when nothing is shaped like a doc.
    """
    preferred = [h for h in hits if h.get("doc_type") in DOC_SHAPED_TYPES]
    out: list[dict[str, Any]] = []
    for hit in preferred[:limit]:
        out.append(
            {
                "name": hit.get("title") or hit.get("path") or hit.get("url") or "untitled",
                "url": hit.get("url"),
                "path": hit.get("path"),
                "repo": hit.get("repo"),
                "doc_type": hit.get("doc_type"),
                "content_hint": hit.get("content_hint"),
                "trust_tier": hit.get("trust_tier"),
                "snippet": hit.get("snippet") or hit.get("text"),
                "score": hit.get("score"),
            }
        )
    return out


def _known_overlaps(file_text: str, hits: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    """Surface vault hits that share tokens with the local file (overlap pointers)."""
    file_tokens = {t.lower() for t in extract_verify_cues(file_text, limit=40)}
    overlaps: list[dict[str, Any]] = []
    for hit in hits:
        blob = " ".join(
            str(hit.get(k) or "") for k in ("title", "path", "url", "snippet", "text", "repo")
        ).lower()
        shared = sorted(t for t in file_tokens if t in blob)[:8]
        if not shared:
            continue
        overlaps.append(
            {
                "title": hit.get("title"),
                "url": hit.get("url"),
                "path": hit.get("path"),
                "trust_tier": hit.get("trust_tier"),
                "shared_cues": shared,
                "note": "lexical overlap only — open canonical URL / live OpenAPI to confirm",
            }
        )
        if len(overlaps) >= limit:
            break
    return overlaps


def shape_verify_report(
    *,
    path: Path,
    file_text: str,
    search_payload: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    shaped = shape_search_payload(
        search_payload,
        query=query,
        canonical_only=False,
        apply_spec_ranking=True,
    )
    hits = shaped["results"]
    doc_shaped = _doc_shaped_sources(hits)
    overlaps = _known_overlaps(file_text, hits)
    warnings = list(shaped.get("warnings") or [])
    warnings.append(
        "Citadel is a context router — confirm mutable API shapes against live OpenAPI/Postman/MIP."
    )
    authority = token_asset_authority_warning(query)
    if authority and authority not in warnings:
        warnings.append(authority)
    if is_token_asset_query(query) or is_token_asset_query(file_text[:2000]):
        warnings.append(
            "For USDCx/USDM/tUSDM and policy+asset hex: use official Masumi docs / "
            "skills/masumi as source of truth; do not invent Mainnet asset IDs from vault noise."
        )
    return {
        "ok": True,
        "command": "verify",
        "file": str(path),
        "query": query,
        "spec_mode": True,
        "doc_shaped_sources": doc_shaped,
        "known_overlaps": overlaps,
        "org_context": [
            {
                "title": h.get("title"),
                "url": h.get("url"),
                "doc_type": h.get("doc_type"),
                "trust_tier": h.get("trust_tier"),
                "snippet": h.get("snippet"),
            }
            for h in hits[:5]
        ],
        "timed_out": shaped.get("timed_out"),
        "truncated": shaped.get("truncated"),
        "code": shaped.get("code"),
        "warnings": warnings,
        "agent_instruction": (
            "doc_shaped_sources are hits whose TEXT reads like documentation — "
            "a starting point, not an authority: the vault attests no provenance, "
            "so trust_tier is 'unattested' for all of them. Treat overlaps as "
            "pointers only. "
            "Always spot-check live MIP/OpenAPI before shipping API claims. "
            "Never use Citadel as sole authority for Mainnet payment token units."
        ),
    }


def shape_prepare_pr_context(
    *,
    repo: str,
    topic: str,
    search_payload: dict[str, Any],
) -> dict[str, Any]:
    query = f"{topic} {repo} schema endpoint MIP"
    shaped = shape_search_payload(
        search_payload,
        query=query,
        repo=repo,
        apply_spec_ranking=True,
    )
    # If repo filter emptied everything, fall back to unfiltered ranked hits.
    hits = shaped["results"]
    if not hits:
        shaped = shape_search_payload(
            search_payload,
            query=query,
            apply_spec_ranking=True,
        )
        hits = shaped["results"]
        shaped["warnings"] = list(shaped.get("warnings") or []) + [
            f"no hits matched --repo {repo!r}; showing unfiltered ranked results"
        ]
    doc_shaped = _doc_shaped_sources(hits)
    org_context = [
        {
            "title": h.get("title"),
            "url": h.get("url"),
            "path": h.get("path"),
            "doc_type": h.get("doc_type"),
            "trust_tier": h.get("trust_tier"),
            "snippet": h.get("snippet"),
        }
        for h in filter_hits(hits, types=None)[:8]
    ]
    warnings = list(shaped.get("warnings") or [])
    authority = token_asset_authority_warning(f"{topic} {repo}")
    if authority and authority not in warnings:
        warnings.append(authority)
    return {
        "ok": True,
        "command": "prepare-pr-context",
        "brief": f"{topic} accuracy / context brief for {repo}",
        "repo": repo,
        "topic": topic,
        "query": query,
        "doc_shaped_sources": doc_shaped,
        "org_context": org_context,
        "timed_out": shaped.get("timed_out"),
        "truncated": shaped.get("truncated"),
        "code": shaped.get("code"),
        "warnings": warnings,
        "agent_instruction": (
            "Start from doc_shaped_sources and org_context, then VERIFY: these are "
            "shape matches on vault text, not attested sources. Citadel does not "
            "replace live OpenAPI for mutable services. "
            "Payment token / asset IDs: prefer official Masumi docs / skills/masumi."
        ),
        "spec_mode": True,
    }


def normalize_local_search_results(results: Any) -> dict[str, Any]:
    """Wrap local Citadel.search list into a payload shape_search_payload understands."""
    if isinstance(results, dict) and "results" in results:
        return results
    if isinstance(results, list):
        return {"results": results, "timed_out": False, "truncated": False}
    return {"results": [], "note": "unexpected local search payload"}
