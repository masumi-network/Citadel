"""Agent-friendly search result shaping and lightweight relevance helpers.

Used by the CLI (`citadel search --json`) and optionally by ranking passes.
Keeps a stable hit schema agents can filter on without a second fetch.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
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
    # `linear\s+\w*\s*sync` rather than `linear\s+sync`: the digest this is meant
    # to catch is titled "Linear workspace sync", and the intervening word made
    # it classify as `other`, which is not ambient — so the one document holding
    # 120 issue titles was never excluded from docs-mode results.
    r"(daily\s+digest|organization\s+update|github\s+org|linear(\s+\w+)?\s+sync)",
    re.IGNORECASE,
)
# The header `format_repo_content_document` writes. It is the one structural
# provenance marker any hit actually carries: hits expose no tags, an empty
# `provenance`, and null `source_node_set`/`source_pipeline`, so body text is
# the only signal available. A document that names its repo, its source URL,
# its commit and its blob is source-linked repository documentation, whatever
# else its text may coincidentally match.
REPO_CONTENT_HEADER_RE = re.compile(
    # The title line's trailing whitespace is `[^\S\n]*` (horizontal whitespace)
    # rather than `\s*` so it cannot compete with the `\n` right after it for
    # the same newline. With `\s*` a run of blank lines could be divided between
    # the two in many ways and the engine tried them all, so matching cost grew
    # faster than the document; pinning the split keeps it proportional. The
    # PARSE variant below already spells this junction the same way. The set of
    # accepted headers is unchanged: \r, \f and \v still match here, only \n is
    # excluded, and \n is what the literal that follows consumes.
    r"^#\s+[\w.-]+/[\w.-]+/\S+[^\S\n]*\n\s*\n"
    r"Repository:\s*\S+\s*\n"
    r"Source:\s*https?://\S+\s*\n"
    r"Commit:\s*\S+\s*\n"
    r"Blob:\s*\S+",
    re.IGNORECASE,
)
# Same structural header, but as a PARSER: named groups recover the repo, the
# per-file commit, the blob and the source URL that cognee's chunk payloads
# never carry as keys. ``\A`` anchors at the very start of the chunk, because a
# document that merely QUOTES another document's header mid-body must not be
# credited with that document's identity — a benchmark once scored quoting
# documents as if they were the quoted ones on exactly this confusion.
REPO_CONTENT_HEADER_PARSE_RE = re.compile(
    r"\A\s*#\s+(?P<header_path>\S+)[ \t]*\n\s*\n"
    r"Repository:[ \t]*(?P<repo>\S+)[ \t]*\n"
    r"Source:[ \t]*(?P<source_url>https?://\S+)[ \t]*\n"
    r"Commit:[ \t]*(?P<commit>\S+)[ \t]*\n"
    r"Blob:[ \t]*(?P<blob>\S+)[ \t]*(?:\n|\Z)",
    re.IGNORECASE,
)
# The title line format_issue_note writes: "# Linear SOK-123: title". The
# workspace digest ("# Linear workspace sync") deliberately does not match —
# it aggregates 120 issues and identifies none of them.
LINEAR_HEADER_PARSE_RE = re.compile(
    r"\A\s*#\s+Linear[ \t]+(?P<issue>[A-Z][A-Z0-9]*-\d+):[ \t]*(?P<title>[^\n]+?)[ \t]*(?:\n|\Z)"
)
# One "- **Key:** value" bullet from the block right under the Linear title.
LINEAR_HEADER_FIELD_RE = re.compile(r"^-\s+\*\*(?P<key>[A-Za-z ]+):\*\*[ \t]*(?P<value>.+?)[ \t]*$")


def parse_content_header(text: Any, *, chunk_index: Any = None) -> dict[str, str]:
    """Provenance from the structural header a syncer wrote at the START of a chunk.

    cognee stores no per-document metadata (hits expose empty ``provenance`` and
    ``text_<md5>`` document names), but the repo-content and Linear syncers each
    render a machine-readable header as the first lines of the documents they
    ingest. Parsing it back is the only provenance available.

    Only a header at position zero is credited (leading whitespace aside), and
    only for the document's FIRST chunk: every chunk starts at position zero of
    its own payload, so "the start" of chunk 1+ is still mid-document,
    author-controlled text — a contributor to any synced repo could open a
    later chunk with a forged header and gain another document's identity (and
    membership in repo=/path= filtered results). Pass the hit's ``chunk_index``
    when the payload carries one; a numeric index other than 0 refuses to
    parse, while absent/None keeps crediting (whole-document payloads and
    pre-chunk sources carry no index). The values are still body text and
    therefore author-controlled — callers must label them as content-derived,
    never as attested trust.
    """
    if (
        isinstance(chunk_index, (int, float))
        and not isinstance(chunk_index, bool)
        and chunk_index != 0
    ):
        return {}
    if not isinstance(text, str) or not text:
        return {}
    match = REPO_CONTENT_HEADER_PARSE_RE.match(text)
    if match:
        repo = match.group("repo")
        header_path = match.group("header_path")
        parsed: dict[str, str] = {
            "kind": "repo-content",
            "repo": repo,
            "source_url": match.group("source_url"),
            "commit": match.group("commit"),
            "blob": match.group("blob"),
            "title": header_path,
        }
        # The title line is "org/repo/path"; strip the repo prefix to get the
        # in-repo path. If the two lines disagree the path claim is dropped
        # rather than guessed.
        if header_path.lower().startswith(repo.lower() + "/"):
            parsed["path"] = header_path[len(repo) + 1 :]
        return parsed
    match = LINEAR_HEADER_PARSE_RE.match(text)
    if match:
        parsed = {
            "kind": "linear-issue",
            "issue": match.group("issue"),
            "title": match.group("title"),
        }
        # Consume only the contiguous bullet block right under the title. The
        # issue DESCRIPTION follows a blank line, so a "- **URL:**" line quoted
        # inside a description is never credited.
        rest = text[match.end() :].splitlines()
        index = 0
        while index < len(rest) and not rest[index].strip():
            index += 1
        while index < len(rest):
            field = LINEAR_HEADER_FIELD_RE.match(rest[index])
            if not field:
                break
            if field.group("key").strip().lower() == "url":
                parsed["source_url"] = field.group("value")
            index += 1
        return parsed
    return {}


# Cardano policy IDs are 56 hex chars; asset names / units are often longer hex.
HEX_ASSET_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{56,})(?![0-9a-fA-F])")
TOKEN_ASSET_QUERY_RE = re.compile(
    r"\b("
    r"usdcx|usdm|tusdm|payment\s*token|asset\s*id|policy\s*id|token\s*unit|"
    # `policy\s*(?:\+\s*)?asset` rather than `policy\s*\+?\s*asset`: the optional
    # `+` is grouped with the whitespace that may follow it, so a run of spaces
    # between the two words has exactly one possible split instead of one per
    # space. Every alternative here is now proportional to the length of the
    # query rather than growing faster than it. The accepted strings are
    # identical: policyasset, policy asset, policy+asset, policy + asset and
    # policy  +  asset all still match.
    r"payment\s*unit|mainnet\s+asset|fingerprint|policy\s*(?:\+\s*)?asset"
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


# --- lexical relevance ------------------------------------------------------
#
# The retriever returns chunk payload dicts with NO score: cognee's CHUNKS
# retriever hands back ``found_chunk.payload`` only, and the vector engine's
# ScoredResult.score (a raw cosine distance) is dropped inside cognee before
# the client boundary ever sees it. Surfacing the real distance is a
# cognee-client change; until then the only relevance signal this layer can
# offer HONESTLY is observable lexical overlap between the query and the hit.
# It is labelled as exactly that — never presented as a retriever score.

_QUERY_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]*")
_QUERY_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "these", "those", "from",
        "what", "how", "where", "when", "which", "who", "whose", "why", "are",
        "was", "were", "does", "did", "doing", "not", "you", "your", "has",
        "have", "had", "can", "could", "should", "would", "will", "shall",
        "may", "might", "must", "about", "into", "onto", "over", "under",
        "between", "our", "their", "they", "them", "its", "his", "her",
        "also", "but", "nor", "either", "any", "all", "some", "than", "then",
        "there", "here", "been", "being", "because", "just", "only", "very",
        "much", "more", "most", "such", "each", "per", "via", "own", "off",
        "out", "too", "get", "got", "let", "see", "say", "said", "tell", "show",
        "find", "use", "used", "using", "one", "two", "way", "make", "made",
        "need", "want", "know", "like", "work", "works", "please",
    }
)
MAX_QUERY_TERMS = 12
NO_LEXICAL_MATCH_WARNING = (
    "No result contains any query term. The retriever always returns the "
    "nearest chunks it stores, even when nothing in the vault is genuinely "
    "close, so these hits may be unrelated to the query — verify their content "
    "before relying on them."
)


def query_terms(query: str) -> list[str]:
    """Distinct informative query tokens, lowercased, order preserved."""
    seen: set[str] = set()
    terms: list[str] = []
    for raw in _QUERY_TOKEN_RE.findall((query or "").lower()):
        token = raw.strip("_./-")
        if len(token) < 3 or token in _QUERY_STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


@lru_cache(maxsize=1024)
def _term_pattern(term: str) -> re.Pattern[str]:
    # Token boundaries, not substring: "cli" must not match "client".
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")


def text_term_matches(text: str, terms: list[str]) -> list[str]:
    """Which of ``terms`` occur (token-bounded) in ``text``."""
    if not text or not terms:
        return []
    lowered = text.lower()
    return [term for term in terms if _term_pattern(term).search(lowered)]


def best_match_window(
    text: str, terms: list[str], *, width: int = 400
) -> tuple[int, str] | None:
    """(offset, window) around the densest cluster of query terms.

    Returns None when no term occurs. Exists because truncating a long document
    at its HEAD hides exactly the part that matched the query — the head of a
    repo file is its provenance header and imports, and the answer usually
    lives thousands of characters in.
    """
    if not text or not terms:
        return None
    lowered = text.lower()
    positions: list[tuple[int, str]] = []
    for term in terms:
        for count, match in enumerate(_term_pattern(term).finditer(lowered)):
            positions.append((match.start(), term))
            if count >= 7:
                break
    if not positions:
        return None
    positions.sort()
    best_start = positions[0][0]
    best_count = 0
    for index, (pos, _term) in enumerate(positions):
        distinct: set[str] = set()
        for later_pos, later_term in positions[index:]:
            if later_pos > pos + width:
                break
            distinct.add(later_term)
        if len(distinct) > best_count:
            best_count = len(distinct)
            best_start = pos
    start = max(0, best_start - max(40, width // 8))
    return start, text[start : start + width]


def lexical_relevance_summary(
    query: str,
    coverages: list[float],
    *,
    scores_available: bool = False,
) -> dict[str, Any]:
    """Response-level honesty block about how relevant the page CAN be known to be.

    ``no_lexical_match`` is the explicit no-confident-match marker: true when
    the page is non-empty yet no hit contains a single query term. It is
    phrased as low confidence, not as proof of irrelevance — a semantic match
    through synonyms would also score zero coverage.
    """
    terms = query_terms(query)
    max_coverage = max(coverages, default=0.0)
    return {
        "basis": "lexical-term-overlap",
        "retriever_scores_available": bool(scores_available),
        "query_terms": terms,
        "max_term_coverage": round(float(max_coverage), 3),
        "no_lexical_match": bool(terms) and bool(coverages) and max_coverage <= 0.0,
    }


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


def hit_term_coverage(item: dict[str, Any], terms: list[str]) -> tuple[float, list[str]]:
    """(fraction of query terms present, the matched terms) for one hit."""
    if not terms:
        return 0.0, []
    matched = text_term_matches(_hit_text(item), terms)
    return len(matched) / len(terms), matched


def infer_doc_type(item: dict[str, Any]) -> str:
    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    dataset = str(envelope.get("dataset") or "")
    if dataset == "session-traces":
        return DOC_TYPE_TRACE
    text = _hit_text(item)
    lowered = text.lower()
    # Checked before the ambient patterns, and only this one may be, because it
    # is structural rather than keyword-based: the full Repository/Source/Commit/
    # Blob header is written by the repo-content syncer and cannot be produced by
    # a digest quoting an issue title. Without it, a README that merely mentions
    # "linear sync" classified as ambient activity and was filtered out of
    # docs-mode results.
    if REPO_CONTENT_HEADER_RE.search(text):
        return DOC_TYPE_CANONICAL
    # Activity/issue material is checked next. A digest aggregates titles written
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
    """Re-order hits for docs/spec queries and single-token literal searches."""
    terms = query_terms(query)
    literal_query = len(terms) == 1
    if not (
        extract_hex_needles(query)
        or is_docs_mode_query(query, mode=mode)
        or is_spec_mode_query(query)
        or literal_query
    ):
        return list(results)
    dict_hits = [item for item in results if isinstance(item, dict)]
    other = [item for item in results if not isinstance(item, dict)]
    # Class boost first, then lexical term coverage as the tie-breaker. Without
    # the second key a page of same-class hits (ten repo-content chunks, all
    # `canonical-docs`) sorted into exactly its input order and mode=docs was
    # indistinguishable from not passing it. Coverage can only reorder WITHIN a
    # class — it never lifts an issue above documentation.
    def rank_key(item: dict[str, Any]) -> tuple[float, float]:
        coverage, _matched = hit_term_coverage(item, terms)
        return query_rank_score(item, query, mode=mode), coverage

    dict_hits.sort(key=rank_key, reverse=True)
    return dict_hits + other


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# Epoch windows for _first_timestamp. A number is read in whichever unit puts
# it inside [2001-09-09, 2096-10-02]; the two windows are three orders of
# magnitude apart, so no value is valid in both and a unit can never be
# guessed wrong by decades.
_EPOCH_SECONDS_MIN = 1_000_000_000  # 2001-09-09T01:46:40+00:00
_EPOCH_SECONDS_MAX = 4_000_000_000  # 2096-10-02T07:06:40+00:00
_EPOCH_MILLIS_MIN = _EPOCH_SECONDS_MIN * 1000
_EPOCH_MILLIS_MAX = _EPOCH_SECONDS_MAX * 1000


def _first_timestamp(*values: Any) -> str | None:
    """First usable timestamp as an ISO-8601 UTC string.

    Strings pass through like ``_first_str``. Numbers are epoch seconds or
    epoch millis (cognee DataPoint ``updated_at``/``created_at`` are millis),
    disambiguated by the windows above. Anything outside both windows — 0,
    negatives, small counters, far-future junk — yields None rather than a
    date: a missing date is recoverable, a wrong one gets trusted. ``bool``
    is an ``int`` subclass and is never a timestamp.
    """
    for value in values:
        if isinstance(value, str):
            if value.strip():
                return value.strip()
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if _EPOCH_SECONDS_MIN <= value <= _EPOCH_SECONDS_MAX:
            seconds = float(value)
        elif _EPOCH_MILLIS_MIN <= value <= _EPOCH_MILLIS_MAX:
            seconds = value / 1000.0
        else:
            continue
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    return None


def _hit_chunk_index(hit: dict[str, Any]) -> Any:
    """The hit's own chunk_index (CHUNKS payloads carry one), else None."""
    if "chunk_index" in hit:
        return hit.get("chunk_index")
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    return metadata.get("chunk_index")


SNIPPET_CHARS = 500


def normalize_search_hit(item: Any, *, index: int = 0, query: str | None = None) -> dict[str, Any]:
    """Stable agent hit schema for CLI --json output.

    With ``query`` given, each hit also carries ``term_coverage`` /
    ``matched_terms`` (observable lexical overlap — see
    ``lexical_relevance_summary`` for why there is no retriever score), and the
    snippet of a LONG text is a window around the densest query-term cluster
    instead of the head: the head of a repo-content chunk is its provenance
    header, which is exactly where the answer is not.
    """
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
            "snippet": text[:SNIPPET_CHARS],
            "content_hint": HINT_UNCLASSIFIED,
            "trust_tier": TRUST_UNATTESTED,
            "rank": index + 1,
        }

    envelope = item.get("_citadel") if isinstance(item.get("_citadel"), dict) else {}
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}

    raw_text = _first_str(
        item.get("text"),
        item.get("content"),
        item.get("summary"),
        item.get("chunk"),
    )
    # Parse the structural header BEFORE the snippet collapses whitespace: it
    # is the only provenance a bare chunk carries, and identity filters below
    # (repo=/path=) depend on it when the server envelope is absent.
    header = parse_content_header(raw_text, chunk_index=_hit_chunk_index(item))

    path = _first_str(
        item.get("path"), provenance.get("path"), metadata.get("path"), header.get("path")
    )
    url = _first_str(
        item.get("url"),
        item.get("source_url"),
        provenance.get("source_url"),
        item.get("source"),
        header.get("source_url"),
    )
    title = _first_str(
        item.get("title"),
        provenance.get("title"),
        metadata.get("title"),
        header.get("title"),
    )
    text = raw_text or title or ""
    if not title:
        title = text.split("\n", 1)[0][:120] if text else (path or url or "untitled")

    repo = _first_str(
        item.get("repo"),
        provenance.get("repo"),
        metadata.get("repo"),
        metadata.get("full_name"),
        header.get("repo"),
    )
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

    terms = query_terms(query) if query else []
    snippet_source = text
    if terms and len(text) > SNIPPET_CHARS:
        window = best_match_window(text, terms, width=SNIPPET_CHARS)
        if window and window[0] > 0:
            snippet_source = "…" + window[1]
    snippet = " ".join(snippet_source.split())[:SNIPPET_CHARS]

    normalized: dict[str, Any] = {
        "id": item.get("id") or envelope.get("result_id"),
        "title": title,
        "url": url,
        "repo": repo,
        "path": path,
        "doc_type": doc_type,
        "updated_at": _first_timestamp(
            item.get("updated_at"),
            envelope.get("created_at"),
            metadata.get("updated_at"),
        ),
        "score": score,
        "snippet": snippet,
        # Alias kept for older agent parsers that read ``text``.
        "text": snippet,
        "content_hint": infer_content_hint(item, doc_type),
        "trust_tier": trust_tier,
        "rank": envelope.get("rank") or (index + 1),
        "dataset": envelope.get("dataset"),
        "_citadel": envelope or None,
    }
    if terms:
        coverage, matched = hit_term_coverage(item, terms)
        normalized["term_coverage"] = round(coverage, 3)
        normalized["matched_terms"] = matched
    return normalized


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


_GITHUB_REPO_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)")


def _hit_raw_text(hit: dict[str, Any]) -> str | None:
    return _first_str(hit.get("text"), hit.get("content"), hit.get("chunk"), hit.get("body"))


def _hit_repo_identity(hit: dict[str, Any]) -> str | None:
    """Which repository this hit IS from — never which repositories it mentions.

    Sources, in order: explicit repo keys, the server-parsed provenance, a
    github.com source URL, and finally the structural header at the start of
    the chunk. Body prose is deliberately not consulted: repo="sokosumi-cli"
    used to match a sokosumi-docs file because its install instructions
    contained that string. Neither is the envelope dataset — Central is
    literally named after the org, so repo=<org> would match every hit in it.
    """
    provenance = _hit_provenance(hit)
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    identity = _first_str(
        hit.get("repo"),
        provenance.get("repo"),
        metadata.get("repo"),
        metadata.get("full_name"),
    )
    if identity:
        return identity
    url = _first_str(hit.get("url"), hit.get("source_url"), provenance.get("source_url"))
    if url:
        match = _GITHUB_REPO_URL_RE.search(url)
        if match:
            return match.group(1)
    header = parse_content_header(_hit_raw_text(hit), chunk_index=_hit_chunk_index(hit))
    return header.get("repo")


def repo_filter_matches(identity: str | None, wanted: str) -> bool:
    """Repo IDENTITY match: exact, name-only, or org-only — never substring.

    Fail-closed: a hit that cannot state which repo it is from does not satisfy
    a repo filter, whatever its body text happens to contain.
    """
    if not identity:
        return False
    identity = identity.strip().strip("/").lower()
    needle = (wanted or "").strip().strip("/").lower()
    if not needle:
        return True
    return (
        identity == needle
        or identity.endswith("/" + needle)
        or identity.startswith(needle + "/")
    )


def _hit_path_identity(hit: dict[str, Any]) -> str | None:
    """The path this hit IS — from path keys or the parsed start-of-chunk header."""
    provenance = _hit_provenance(hit)
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    path = _first_str(hit.get("path"), provenance.get("path"), metadata.get("path"))
    if path:
        return path
    header = parse_content_header(_hit_raw_text(hit), chunk_index=_hit_chunk_index(hit))
    # The header title line is "org/repo/path", a superset of the path — fine
    # for substring path filters, still an identity claim rather than body text.
    return header.get("path") or header.get("title")


def _hit_source_identity(hit: dict[str, Any]) -> str | None:
    """WHICH syncer wrote this hit — from the server envelope or the header.

    The kind of source a hit IS, not what its body discusses: a repository's
    ``docs/agents/issue-tracker.md`` is about Linear on every line and is still
    repo content. Prefer the server's resolved provenance, fall back to parsing
    the structural header the syncer wrote (start of chunk 0 only, so a quoted
    or mid-document header cannot claim another source's identity).

    Header-derived, therefore author-controlled: this scopes a search, it does
    not attest anything, and it must never feed ``trust_tier``.
    """
    provenance = _hit_provenance(hit)
    source = _first_str(provenance.get("source"), hit.get("source"), hit.get("source_type"))
    if source:
        return source.strip().lower()
    header = parse_content_header(_hit_raw_text(hit), chunk_index=_hit_chunk_index(hit))
    kind = header.get("kind")
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else None


def compact_search_filters(
    *,
    types: list[str] | None = None,
    repo: str | None = None,
    path: str | None = None,
    source: str | None = None,
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
    if isinstance(source, str) and source.strip():
        filters["source"] = source.strip()
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
    source: str | None = None,
    canonical_only: bool = False,
    exclude_ambient: bool = False,
) -> list[dict[str, Any]]:
    filtered = hits
    if source:
        # Fail-closed, like repo=: a hit that cannot state which source it came
        # from does not satisfy a source filter. A tool that promises one
        # source must return nothing rather than something else.
        wanted_source = source.strip().lower()
        filtered = [h for h in filtered if _hit_source_identity(h) == wanted_source]
    if types:
        wanted = {t.strip().lower() for t in types if t.strip()}
        filtered = [h for h in filtered if _hit_doc_type(h) in wanted]
    if repo:
        # Identity, not substring: matching the whole hit blob credited any hit
        # whose BODY mentioned the repo name (install lines, cross-references).
        filtered = [h for h in filtered if repo_filter_matches(_hit_repo_identity(h), repo)]
    if path:
        # Substring over the hit's own path identity only; callers may pass
        # glob-ish **/MIP-003/** which still matches as plain text. Matching the
        # whole blob made path= another body-text filter.
        needle = path.replace("**/", "").replace("/**", "").replace("*", "").lower()
        if needle:
            filtered = [
                h for h in filtered if needle in (_hit_path_identity(h) or "").lower()
            ]
    if canonical_only:
        # Content-shaped, NOT a trust filter: it keeps hits whose text reads like
        # documentation. It cannot vouch for any of them — the tier that could
        # is attested-only now, so this deliberately no longer consults it.
        filtered = [h for h in filtered if _hit_doc_type(h) in DOC_SHAPED_TYPES]
    if exclude_ambient:
        # Content-shaped only, for the same reason canonical_only above stopped
        # consulting the tier: `reference-only` is the single tier the server can
        # attest today, so every hit carries it and the trust half of this
        # condition was unsatisfiable. `mode="docs"` therefore returned zero
        # results for every query, including ones whose answer was an ingested
        # .md file sitting in the vault.
        filtered = [h for h in filtered if _hit_doc_type(h) not in AMBIENT_DOC_TYPES]
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
    if docs_mode or extract_hex_needles(query) or apply_spec_ranking or len(query_terms(query)) == 1:
        ordered = apply_query_ranking(raw_results, query, mode=mode)
    else:
        ordered = list(raw_results)
    hits = [normalize_search_hit(item, index=i, query=query) for i, item in enumerate(ordered)]
    filters_active = bool(types or repo or path or canonical_only or exclude_ambient)
    before_filters = len(hits)
    hits = filter_hits(
        hits,
        types=types,
        repo=repo,
        path=path,
        canonical_only=canonical_only,
        exclude_ambient=exclude_ambient,
    )
    relevance = lexical_relevance_summary(
        query,
        [
            float(h["term_coverage"])
            for h in hits
            if isinstance(h.get("term_coverage"), (int, float))
        ],
        scores_available=any(h.get("score") is not None for h in hits),
    )
    warnings: list[str] = []
    if payload.get("note"):
        warnings.append(str(payload["note"]))
    timed_out = bool(payload.get("timed_out"))
    truncated = timed_out or bool(payload.get("truncated"))
    if timed_out:
        warnings.append("search timed out; results may be incomplete")
    if relevance["no_lexical_match"]:
        warnings.append(NO_LEXICAL_MATCH_WARNING)
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
        "relevance": relevance,
        "warnings": warnings,
        "ok": True,
    }
    if filters_active:
        # Filters run on the returned page, so they can only shrink it. Say how
        # much they did, so a short page reads as "filters excluded candidates",
        # not "the vault holds nothing else".
        out["filter_stats"] = {"before": before_filters, "after": len(hits)}
    if timed_out:
        out["code"] = CODE_TIMEOUT
    elif payload.get("code"):
        out["code"] = payload["code"]
    return out
