"""Retrieval benchmark for the Citadel search path.

Measures whether the BODY of retrieved text answers the question, over
DISTINCT document identities, so duplicate copies of one file cannot inflate
the score. Read-only: it issues searches and writes nothing to the vault.

Why the previous harness was replaced (2026-08-01):
- It scored a hit by source-path header BEFORE any content check, and the
  header regex matched a header line ANYWHERE in a chunk, so any document
  quoting another document's header scored as that document.
- ``--repeats`` took the best rank across repeats, so a 1-in-3 flaky hit
  scored as a full hit.
- The blocked-probe list was empty and an empty list printed as a passing 0.0.
- There was no duplication or precision measure at all, while the corpus had a
  measured duplicate_blob_rate of 0.41.

Scoring model:
- identity = (source path, blob sha) for repo content, ``linear:<ID>`` for
  Linear issues, else the document id. The ranked hit list is collapsed to
  distinct identities (first occurrence kept); effective_rank is the position
  in that collapsed list.
- A question with ``answer_spans`` passes only if one of its verbatim body
  quotes appears in a retrieved chunk's body AFTER stripping the repo-content
  header block (through the ``---`` separator) and any leading ``# Linear``
  line. Both sides are normalized: lowercase, collapsed whitespace, backticks
  and emphasis markers stripped.
- Repeats feed latency and hit_stability ONLY. Quality metrics use the first
  attempt. There is no best-of-repeats path.

Usage:
    export CITADEL_MCP_ACCESS_TOKEN=...
    citadel bench run --repeats 3 --out scripts/bench/runs/latest.json
    citadel bench run --baseline previous_run.json
    citadel bench lint
    citadel bench lint --ground-truth scripts/bench/ground_truth_ci
    citadel bench compare run_a.json run_b.json
    citadel bench report scripts/bench/runs/latest.json --markdown

The legacy ``python scripts/bench/search_bench.py`` path remains a compatibility
launcher for source checkouts. The implementation lives in this module so the
installed ``citadel`` command does not depend on the repository layout.

Write run JSONs only under scripts/bench/runs/ (gitignored): a run JSON
enumerates every served hit identity and must never be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_NODE = "https://citadel-archive-production.up.railway.app"
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_BENCH_DIR = PACKAGE_ROOT.parent / "scripts" / "bench"
# Source checkouts keep the full frozen set and private ground-truth cache under
# scripts/bench. An installed package may receive an explicit --questions path;
# keep the fallback deterministic instead of silently inventing a fixture.
HERE = REPO_BENCH_DIR if REPO_BENCH_DIR.is_dir() else PACKAGE_ROOT / "bench"
GROUND_TRUTH = HERE / "ground_truth"
DEFAULT_QUESTIONS = HERE / "golden_questions.json"

K_ANSWER = 5          # answer_recall@5 / doc_recall@5 window
K_DUP = 10            # duplicate_blob_rate@10 / distinct_files@10 window
MIN_SPAN_CHARS = 15   # normalized; shorter spans match too promiscuously
MAX_SPANS = 4

# Identity parsing is anchored at the START of the chunk. The old harness used
# re.MULTILINE .search, so a header line quoted anywhere in a body counted as
# that document. Only a chunk that BEGINS with the header carries an identity.
REPO_HEADER_FIRSTLINE = re.compile(r"\A#\s+([\w.-]+/[\w.-]+/\S+)\s*(?:\n|\Z)")
LINEAR_HEADER_FIRSTLINE = re.compile(r"\A#\s+Linear\s+([A-Z][A-Z0-9]*-\d+)\b")
BLOB_LINE = re.compile(r"^Blob:\s*([0-9a-fA-F]{6,64})\s*$", re.MULTILINE)
HEADER_META_LINE = re.compile(r"^(Repository|Source|Commit|Blob|Retrieved):")

# The OLD scorer, kept verbatim so header_credit_rate can report how often it
# awarded credit that the body scorer refuses. Never used for the headline.
LEGACY_SOURCE_HEADER = re.compile(r"^#\s+([\w.-]+/[\w.-]+/\S+)", re.MULTILINE)
LEGACY_LINEAR_HEADER = re.compile(r"^#\s+Linear\s+([A-Z]+-\d+)", re.MULTILINE)
SHINGLE_WORDS = 12
MIN_SHARED_SHINGLES = 3

UNCONVERTED_KEY = "answer_spans_unconverted_reason"

# --------------------------------------------------------------------------
# Embedding-window pairs (v5)
# --------------------------------------------------------------------------
# A pair quotes ONE document twice: once from inside the first HEAD_WINDOW_CHARS
# characters, once from at least TAIL_MIN_DEPTH through it. Both quotes are
# verbatim and unique across every cached body, so exactly one document can
# answer either. The pair is a controlled comparison: same document, same corpus,
# same query shape, only the position of the quoted text changes.
#
# The control that makes a failing tail mean something: a tail quote qualifies
# only when at least TAIL_MIN_NOVEL_TERMS of its distinctive terms are ABSENT
# from the document head. Without that rule a tail could be answered by the head
# embedding alone, and a pass would prove nothing about how much of the document
# is reachable.
#
# These questions are scored in their own block and are EXCLUDED from
# answer_recall_at_5 so the existing headline stays comparable with baselines
# taken before v5.
WINDOW_HEAD_CATEGORY = "window_head"
WINDOW_TAIL_CATEGORY = "window_tail"
WINDOW_CATEGORIES = frozenset({WINDOW_HEAD_CATEGORY, WINDOW_TAIL_CATEGORY})
HEAD_WINDOW_CHARS = 2000
TAIL_MIN_DEPTH = 0.40
TAIL_MIN_NOVEL_TERMS = 3
NOVEL_TERM_RE = re.compile(r"[a-z][a-z0-9]{3,}")
NOVEL_TERM_STOPWORDS = frozenset(
    "the a an and or of to in for is are be it this that with as on by from at "
    "not you your we they if then than so but can will each any all when what "
    "which who how why into out up down over under only same other more most "
    "some such no nor too very just also its per".split()
)


def is_window_question(question: dict[str, Any]) -> bool:
    """True for anything `summarize` will bucket as a window row.

    Keyed on BOTH surfaces deliberately. `summarize` buckets on `window_role`
    (score_question copies that field straight through), while the fixture
    contract is declared as `category`. Keying lint on `category` alone left a
    bypass: a question carrying `window_role` and no category was invisible to
    `_lint_window_question` and still entered head/tail recall and the
    `tail_recall_given_head_at_5` arithmetic with zero validation. Whatever
    either surface treats as a window question has to satisfy the contract,
    and the contract itself requires the two to agree.
    """
    if str(question.get("category", "")) in WINDOW_CATEGORIES:
        return True
    return question.get("window_role") in ("head", "tail")


def distinctive_terms(text: str) -> set[str]:
    """Lowercase content words used for the head/tail novelty control."""
    return {
        word
        for word in NOVEL_TERM_RE.findall(text.lower())
        if word not in NOVEL_TERM_STOPWORDS
    }


def questions_pin(questions: list[dict[str, Any]]) -> str:
    """sha256 over the canonical question list.

    The pin is what makes the set FROZEN. Any edit to any question changes it,
    so re-freezing is a deliberate act (update the pin, bump the version) rather
    than a silent drift that would make two runs answer different questions
    while both called themselves the baseline.
    """
    canonical = json.dumps(
        questions, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class BenchError(RuntimeError):
    """A configuration or honesty violation that must stop the run."""


# --------------------------------------------------------------------------
# Normalization and header handling
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase, strip backticks and emphasis markers, collapse whitespace.

    Applied to BOTH the span and the chunk body, so markdown emphasis around a
    quoted phrase cannot break a verbatim match.
    """
    text = text.lower().replace("`", "")
    text = re.sub(r"[*_]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def hit_text(hit: dict[str, Any]) -> str:
    for key in ("text", "content", "chunk", "body"):
        value = hit.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def split_header_body(text: str) -> tuple[str, str]:
    """Split a chunk into (sync header, body).

    Repo-content chunks begin ``# org/repo/path`` and carry a
    Repository/Source/Commit/Blob block (legacy copies also a ``Retrieved:``
    line) terminated by a ``---`` separator; everything through the first
    ``---`` is header. Linear chunks begin ``# Linear SOK-123: ...``; only that
    first line is header. Anything else is all body.

    Only a header at the START of the chunk is stripped. A header line quoted
    mid-body stays in the body: it is exactly the text the old scorer was
    fooled by, and the body scorer must see it as ordinary body text, not
    credit it as an identity.
    """
    if LINEAR_HEADER_FIRSTLINE.match(text):
        parts = text.split("\n", 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")
    if not REPO_HEADER_FIRSTLINE.match(text):
        return "", text
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[: index + 1]), "\n".join(lines[index + 1 :])
    # No separator (malformed or truncated chunk): strip the contiguous header
    # prefix (title line, blanks, known meta lines) and keep the rest as body.
    index = 1
    while index < len(lines) and (
        not lines[index].strip() or HEADER_META_LINE.match(lines[index])
    ):
        index += 1
    return "\n".join(lines[:index]), "\n".join(lines[index:])


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    kind: str  # "repo" | "linear" | "doc"
    source: str  # repo path, "linear:<ID>", or document id
    blob: str = ""  # blob sha for repo content, "" otherwise


def parse_identity(hit: dict[str, Any]) -> Identity:
    """Resolve a hit to its document identity.

    repo content -> (source path, blob sha); two copies of one file under two
    blobs are two identities, N slots of one blob are one. Linear -> the issue
    id. Anything else -> document_id (falls back to the hit id, then to a
    content hash so the function is total).
    """
    text = hit_text(hit)
    linear = LINEAR_HEADER_FIRSTLINE.match(text)
    if linear:
        return Identity("linear", f"linear:{linear.group(1)}")
    repo = REPO_HEADER_FIRSTLINE.match(text)
    if repo:
        header, _ = split_header_body(text)
        blob = BLOB_LINE.search(header)
        return Identity("repo", repo.group(1), blob.group(1).lower() if blob else "")
    doc_id = hit.get("document_id") or hit.get("id")
    if doc_id:
        return Identity("doc", str(doc_id))
    return Identity(
        "doc", "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    )


def collapse_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse ranked hits to distinct identities, keeping first occurrence.

    Returns entries of {identity, effective_rank, first_slot, bodies}. Later
    duplicates of an identity contribute their bodies as evidence but never a
    new rank, so one file served in every slot fills exactly one rank.
    """
    order: list[dict[str, Any]] = []
    by_identity: dict[Identity, dict[str, Any]] = {}
    for slot, hit in enumerate(hits, start=1):
        identity = parse_identity(hit)
        entry = by_identity.get(identity)
        if entry is None:
            entry = {
                "identity": identity,
                "effective_rank": len(order) + 1,
                "first_slot": slot,
                "bodies": [],
            }
            by_identity[identity] = entry
            order.append(entry)
        entry["bodies"].append(split_header_body(hit_text(hit))[1])
    return order


# --------------------------------------------------------------------------
# Legacy scorer (measured, not trusted)
# --------------------------------------------------------------------------


def shingles(text: str, size: int = SHINGLE_WORDS) -> set[str]:
    words = re.sub(r"\s+", " ", text.lower()).split(" ")
    if len(words) < size:
        return set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def load_ground_truth_shingles(expected: list[str]) -> dict[str, set[str]]:
    fingerprints: dict[str, set[str]] = {}
    for repo_path in expected:
        cached = GROUND_TRUTH / repo_path.replace("/", "__")
        if cached.exists():
            fingerprints[repo_path] = shingles(cached.read_text(encoding="utf-8"))
    return fingerprints


def legacy_rank(
    hits: list[dict[str, Any]],
    expected: list[str],
    gt_shingles: dict[str, set[str]],
) -> int | None:
    """The OLD scorer: header-anywhere path match, then shingle overlap.

    Kept only to compute header_credit_rate (old passes where the body scorer
    fails). Its known defect -- crediting a quoted header -- is deliberate.
    """
    for index, hit in enumerate(hits, start=1):
        text = hit_text(hit)
        linear = LEGACY_LINEAR_HEADER.search(text)
        if linear and f"linear:{linear.group(1)}" in expected:
            return index
        source = LEGACY_SOURCE_HEADER.search(text)
        if source and source.group(1) in expected:
            return index
        hit_shingles = shingles(text)
        if not hit_shingles:
            continue
        for repo_path in expected:
            if repo_path not in gt_shingles:
                continue
            if len(hit_shingles & gt_shingles[repo_path]) >= MIN_SHARED_SHINGLES:
                return index
    return None


# --------------------------------------------------------------------------
# Blocked-content probes
# --------------------------------------------------------------------------

# The generic secret_assignment rule only blocks ingest after
# _is_credential_like() filters out env lookups and placeholders. The bare
# regex without that filter matches ordinary documentation
# ("api_key = process.env.KEY"), so a probe keyed on it would cry leak on
# legitimately ingested content. Probes may only reference rules whose match
# ALONE blocks.
BLOCKED_PATTERN_EXCLUDED = frozenset({"secret_assignment"})
BLOCKED_PATTERN_KEY = "blocked_pattern"


def load_scanner_patterns() -> dict[str, re.Pattern[str]]:
    """Compiled blocking regexes from the LIVE ingest scanner, by category.

    Imported from kb.security_scan at run time, never copied into this file: a
    pattern probe exists to test the scanner's contract ("content matching
    this rule never reaches the vault"), so when a rule is renamed or deleted
    the probe must fail loudly instead of silently matching nothing.
    kb/__init__ is lazy (PEP 562), so this pulls in stdlib-only code, not the
    server stack.
    """
    repo_root = HERE.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from kb.security_scan import SECRET_PATTERNS  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - any import failure must stop the run
        raise BenchError(
            "cannot import kb.security_scan to resolve blocked-pattern probes: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    return {
        category: pattern
        for category, _severity, pattern in SECRET_PATTERNS
        if category not in BLOCKED_PATTERN_EXCLUDED
    }


def resolve_probe_patterns(
    questions: list[dict[str, Any]],
) -> dict[str, re.Pattern[str]]:
    """Map question id -> compiled scanner pattern for pattern probes.

    Raises BenchError for a probe referencing a rule the scanner no longer
    carries, and for a probe that could never hit (neither blocked_pattern nor
    expect_any): a probe that cannot detect anything only pads the probe count,
    which is the fake-pass shape this harness exists to refuse.
    """
    probes = [q for q in questions if q.get("expected_recall", 1) == 0]
    pattern_probes = [q for q in probes if q.get(BLOCKED_PATTERN_KEY)]
    patterns = load_scanner_patterns() if pattern_probes else {}
    resolved: dict[str, re.Pattern[str]] = {}
    for probe in pattern_probes:
        category = str(probe[BLOCKED_PATTERN_KEY])
        if category not in patterns:
            raise BenchError(
                f"{probe.get('id', '?')}: blocked_pattern '{category}' is not "
                "a blocking rule in kb.security_scan.SECRET_PATTERNS (allowed: "
                f"{', '.join(sorted(patterns))}). A probe derived from a rule "
                "that no longer exists tests nothing; update or remove it."
            )
        resolved[str(probe.get("id"))] = patterns[category]
    for probe in probes:
        if not probe.get(BLOCKED_PATTERN_KEY) and not probe.get("expect_any"):
            raise BenchError(
                f"{probe.get('id', '?')}: a probe needs blocked_pattern or a "
                "non-empty expect_any; with neither it can never register a "
                "hit and only pads the probe count."
            )
    return resolved


# --------------------------------------------------------------------------
# Per-question scoring
# --------------------------------------------------------------------------


def hit_term_coverage(hit: dict[str, Any]) -> float | None:
    """The node's OWN reported share of query terms this hit matched.

    Read rather than recomputed on purpose: ranking is the node's decision, so
    the inversion metric has to be expressed in the node's own units. A number
    the harness derived itself would only prove the harness and the node
    disagree about tokenization.
    """
    citadel = hit.get("_citadel")
    if not isinstance(citadel, dict):
        return None
    relevance = citadel.get("relevance")
    if not isinstance(relevance, dict):
        return None
    coverage = relevance.get("term_coverage")
    return float(coverage) if isinstance(coverage, (int, float)) else None


def trust_observations(hits: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(result_id, content_sha256, trust_tier) for every hit that carries all three.

    Free byproduct of a run: the same chunk is served across many questions, so
    disagreement between two observations of the SAME id at the SAME content
    hash is per-request metadata instability, caught without a single extra
    query.
    """
    out: list[tuple[str, str, str]] = []
    for hit in hits:
        citadel = hit.get("_citadel")
        if not isinstance(citadel, dict):
            continue
        result_id = citadel.get("result_id") or hit.get("id")
        sha = citadel.get("content_sha256")
        tier = citadel.get("trust_tier")
        if isinstance(result_id, str) and isinstance(sha, str) and isinstance(tier, str):
            out.append((result_id, sha, tier))
    return out


def entry_matches_expected(
    entry: dict[str, Any],
    expected: list[str],
    gt_shingles: dict[str, set[str]],
) -> bool:
    """Is this collapsed identity one of the question's expect_any documents?

    Identity first (path+blob or Linear key), then the cached-body shingle
    fallback for a later chunk that carries no sync header. One definition
    shared by `doc_rank` and by the answer-slot provenance check, so the two
    can never disagree about what "the expected document" means.
    """
    identity = entry["identity"]
    if identity.kind in ("repo", "linear") and identity.source in expected:
        return True
    for repo_path in expected:
        if repo_path not in gt_shingles:
            continue
        for body in entry["bodies"]:
            if len(shingles(body) & gt_shingles[repo_path]) >= MIN_SHARED_SHINGLES:
                return True
    return False


def score_question(
    question: dict[str, Any],
    hits: list[dict[str, Any]],
    gt_shingles: dict[str, set[str]],
    probe_pattern: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    """Score one question against one served page (attempt 1 only)."""
    spans = [normalize(span) for span in question.get("answer_spans") or []]
    expected = list(question.get("expect_any") or [])
    collapsed = collapse_hits(hits)

    answer_rank: int | None = None
    # Whether the document that supplied the span is one this question named.
    # `answer_pass_at_5` deliberately does NOT gate on it: that metric asks
    # whether the answer TEXT came back, `doc_recall_at_5` asks whether the
    # named document did, and blending them is what a single recall number
    # already does wrong. But span uniqueness is enforced by `lint` only across
    # the ~49 files in the gitignored ground-truth cache, never across the
    # corpus, so a second render of a file, an org digest, a session trace or
    # any uncached document quoting the sentence would score the pass. Counting
    # the fact makes that blind spot visible per run instead of assumed away.
    answer_from_expected: bool | None = None
    if spans:
        for entry in collapsed:
            bodies = [normalize(body) for body in entry["bodies"]]
            if any(span in body for body in bodies for span in spans):
                answer_rank = entry["effective_rank"]
                answer_from_expected = entry_matches_expected(
                    entry, expected, gt_shingles
                )
                break

    raw_page_pass = False
    if spans:
        for hit in hits[:K_ANSWER]:
            body = normalize(split_header_body(hit_text(hit))[1])
            if any(span in body for span in spans):
                raw_page_pass = True
                break

    # ---- Ranking, measured separately from retrieval -----------------------
    # Retrieval asks whether the answering document came back at all. Ranking
    # asks whether the node put it in the right place. They are different
    # questions and a single recall number silently blends them, so the answer
    # slot is anchored to a BODY span match (header stripped) and then compared
    # against the node's own term_coverage for everything served above it.
    #
    # An inversion is: some hit ranked ABOVE the one that verifiably contains
    # the answer, while the node itself reports that hit matched a STRICTLY
    # SMALLER share of the query terms.
    #
    # BLIND SPOT, stated where the number is produced. The answer SLOT is
    # header-immune: it is bound to a body span match with the sync header
    # stripped, so a hit whose only overlap is the path can never become the
    # answer. `term_coverage` is NOT header-immune. The node computes it over
    # a haystack that includes the hit's own path, source url, provenance and
    # the sync header still sitting in the chunk text
    # (`kb/search_format.py:_hit_text`), and every hit on a page carries a
    # DIFFERENT path, so a filename can move either side of this comparison:
    # a decoy whose path matches three query terms stops being an inversion,
    # and an answer whose path matches becomes one. Both directions were
    # executed against the node's own coverage function. The metric is
    # therefore an honest statement about the node's published relevance
    # signal disagreeing with the node's own ordering, and NOT a body-only
    # measure of relevance. Do not quote it as one.
    answer_slot: int | None = None
    answer_coverage: float | None = None
    outranked_by_coverage: float | None = None
    outranked_by_slot: int | None = None
    if spans:
        for slot, hit in enumerate(hits, start=1):
            body = normalize(split_header_body(hit_text(hit))[1])
            if any(span in body for span in spans):
                answer_slot = slot
                answer_coverage = hit_term_coverage(hit)
                break
    if answer_slot is not None and answer_coverage is not None:
        for slot, hit in enumerate(hits[: answer_slot - 1], start=1):
            coverage = hit_term_coverage(hit)
            if coverage is not None and coverage < answer_coverage:
                outranked_by_coverage = coverage
                outranked_by_slot = slot
                break

    doc_rank: int | None = None
    for entry in collapsed:
        if entry_matches_expected(entry, expected, gt_shingles):
            doc_rank = entry["effective_rank"]
            break

    # Blocked-content probes: a hit whose RAW chunk text (header included — a
    # secret is a leak wherever it sits) matches the scanner rule is a leak.
    # The matched text is deliberately never copied into the row: a leaked
    # secret must not propagate into benchmark artifacts.
    probe_pattern_slot: int | None = None
    if probe_pattern is not None:
        for slot, hit in enumerate(hits, start=1):
            if probe_pattern.search(hit_text(hit)):
                probe_pattern_slot = slot
                break
        if probe_pattern_slot is not None and doc_rank is None:
            matched_identity = parse_identity(hits[probe_pattern_slot - 1])
            for entry in collapsed:
                if entry["identity"] == matched_identity:
                    doc_rank = entry["effective_rank"]
                    break

    dup_slots = hits[:K_DUP]
    dup_collapsed = collapse_hits(dup_slots)
    duplicate_blob_rate = (
        (len(dup_slots) - len(dup_collapsed)) / len(dup_slots) if dup_slots else None
    )
    distinct_files = len(
        {(entry["identity"].kind, entry["identity"].source) for entry in dup_collapsed}
    )

    # PR #183's path-based diversity view, kept ALONGSIDE the blob-based rate
    # above because the two answer different questions and neither replaces the
    # other. This counts distinct PATHS, so two renders of one README score as
    # ONE source. duplicate_blob_rate counts distinct (path, blob) identities,
    # so that same pair scores as TWO. The first asks whether the agent is
    # seeing varied content; the second asks whether the corpus is duplicated.
    # Deleting either as redundant loses a real signal.
    #
    # Baseline recorded 2026-07-31 by the path method: recall@5 was 0.93 while
    # the mean distinct-source ratio was 0.62, with 22 of 60 queries returning
    # only one or two distinct documents.
    resolvable_slots = [
        hit for hit in dup_slots if parse_identity(hit).kind in ("repo", "linear")
    ]
    distinct_resolvable = len({parse_identity(hit).source for hit in resolvable_slots})
    # The ratio over ALL slots is the pessimistic bound: a hit whose source path
    # could not be resolved counts against diversity exactly like a duplicate
    # does. The resolvable-only ratio is what the attributable evidence
    # supports. Report both rather than choose — quoting one without the other
    # invites the obvious rebuttal. Measured 2026-07-31: 15 of 304 hits were
    # unresolvable (4.9%), worth 0.61 against 0.65.
    # Numerator is distinct RESOLVABLE paths, not distinct_files_at_10 — that
    # one counts every identity kind, so a page of unattributable hits would
    # read as highly diverse. #183's rule is deliberately harsher: an
    # unresolvable hit contributes nothing to the numerator while still
    # counting in the denominator.
    distinct_source_ratio = distinct_resolvable / len(dup_slots) if dup_slots else None
    distinct_source_ratio_resolvable_only = (
        distinct_resolvable / len(resolvable_slots) if resolvable_slots else None
    )

    return {
        "id": question.get("id"),
        "category": question.get("category"),
        "question": question.get("question"),
        "expected_recall": question.get("expected_recall", 1),
        "blocked_pattern": question.get(BLOCKED_PATTERN_KEY),
        "probe_pattern_hit_slot": probe_pattern_slot,
        "has_spans": bool(spans),
        "answer_rank": answer_rank,
        "answer_pass_at_1": answer_rank == 1,
        "answer_pass_at_5": answer_rank is not None and answer_rank <= K_ANSWER,
        "answer_from_expected_document": answer_from_expected,
        "raw_page_pass_at_5": raw_page_pass,
        "expect_any": expected,
        "window_role": question.get("window_role"),
        "window_pair": question.get("window_pair"),
        "depth_fraction": question.get("depth_fraction"),
        "answer_slot": answer_slot,
        "answer_term_coverage": answer_coverage,
        "outranked_by_coverage": outranked_by_coverage,
        "outranked_by_slot": outranked_by_slot,
        "trust_observations": trust_observations(hits),
        "doc_rank": doc_rank,
        "doc_pass_at_1": doc_rank == 1,
        "doc_pass_at_5": doc_rank is not None and doc_rank <= K_ANSWER,
        "legacy_rank": legacy_rank(hits, expected, gt_shingles),
        "duplicate_blob_rate_at_10": duplicate_blob_rate,
        "distinct_files_at_10": distinct_files if dup_slots else None,
        "distinct_source_ratio": distinct_source_ratio,
        "distinct_source_ratio_resolvable_only": distinct_source_ratio_resolvable_only,
        "hits_with_unresolvable_source": (
            len(dup_slots) - len(resolvable_slots) if dup_slots else None
        ),
        "identities": [
            {
                "kind": entry["identity"].kind,
                "source": entry["identity"].source,
                "blob": entry["identity"].blob,
                "effective_rank": entry["effective_rank"],
                "first_slot": entry["first_slot"],
            }
            for entry in collapsed
        ],
        "slots_served": len(hits),
    }


def attempt_outcome(row: dict[str, Any]) -> bool:
    """The boolean an attempt contributes to hit_stability."""
    return row["answer_pass_at_5"] if row["has_spans"] else row["doc_pass_at_5"]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def summarize(rows: list[dict[str, Any]], stability: list[float]) -> dict[str, Any]:
    positives = [row for row in rows if row["expected_recall"] == 1]
    probes = [row for row in rows if row["expected_recall"] == 0]
    window_rows = [row for row in positives if row.get("window_role") in ("head", "tail")]
    # Window pairs are deliberately kept out of the SPAN-scored headline. They
    # were added in questions v5 and they fail by design against current
    # behaviour; folding them in would move answer_recall_at_5 for a reason
    # that has nothing to do with the node changing.
    #
    # Scope of that protection, stated exactly rather than as "the headline".
    # `span_rows` is the only exclusion, so only the metrics computed from it
    # are unmoved by v5: answer_recall_at_5, raw_page_recall_at_5, mrr_body,
    # header_credit_rate. Every metric computed from `positives` or from `rows`
    # DID move when the 36 window questions landed -- doc_recall_at_5,
    # duplicate_blob_rate_at_10, distinct_files_at_10, the two
    # distinct_source_ratio figures, hit_stability and rank_inversion_rate.
    # None of those is comparable across the v5 boundary. `compare` is what
    # enforces this: two runs answering different question sets differ in
    # `questions_pin` and are refused outright.
    span_rows = [
        row for row in positives if row["has_spans"] and row.get("window_role") is None
    ]
    if not probes:
        raise BenchError(
            "The blocked-probe/negatives list is empty (no expected_recall=0 "
            "questions). Refusing to print a metric over zero probes as a pass: "
            "add probes to the golden set first."
        )
    if not span_rows:
        raise BenchError(
            "No question carries answer_spans; answer_recall@5 would be a "
            "metric over nothing. Convert questions or fix the questions file."
        )

    def rate(rows_: list[dict[str, Any]], key: str) -> float:
        return sum(1 for row in rows_ if row[key]) / len(rows_)

    mrr_body = statistics.fmean(
        1.0 / row["answer_rank"] if row["answer_rank"] else 0.0 for row in span_rows
    )
    dup_rates = [
        row["duplicate_blob_rate_at_10"]
        for row in rows
        if row["duplicate_blob_rate_at_10"] is not None
    ]
    file_counts = [
        row["distinct_files_at_10"] for row in rows if row["distinct_files_at_10"] is not None
    ]
    source_ratios = [
        row["distinct_source_ratio"] for row in rows if row["distinct_source_ratio"] is not None
    ]
    source_ratios_resolvable = [
        row["distinct_source_ratio_resolvable_only"]
        for row in rows
        if row["distinct_source_ratio_resolvable_only"] is not None
    ]
    unresolvable_hits = sum(
        row["hits_with_unresolvable_source"] or 0
        for row in rows
        if row["hits_with_unresolvable_source"] is not None
    )
    header_credit = sum(
        1
        for row in span_rows
        if row["legacy_rank"] is not None
        and row["legacy_rank"] <= K_ANSWER
        and not row["answer_pass_at_5"]
    )
    negative_hits = sum(1 for row in probes if row["doc_rank"] is not None)

    # ---- Embedding window --------------------------------------------------
    heads = [row for row in window_rows if row["window_role"] == "head"]
    tails = [row for row in window_rows if row["window_role"] == "tail"]
    head_by_pair = {row["window_pair"]: row for row in heads}
    tail_by_pair = {row["window_pair"]: row for row in tails}
    complete_pairs = sorted(set(head_by_pair) & set(tail_by_pair))
    # Every published window rate is computed over COMPLETE pairs only, so the
    # figure really is a within-document difference and never a comparison
    # across two different document populations. The comment used to claim
    # that while `head_recall`/`tail_recall` were taken over ALL head rows and
    # ALL tail rows: one dangling side (a tail dropped, a head added without
    # its tail) silently turned window_penalty into a cross-population
    # difference, and the report still printed `pairs_complete` as its n.
    # Restricting here keeps the rate, the penalty and the printed n consistent
    # by construction. With every pair complete the numbers are identical, so
    # this does not move a baseline taken while the set was whole.
    paired_heads = [head_by_pair[pair] for pair in complete_pairs]
    paired_tails = [tail_by_pair[pair] for pair in complete_pairs]
    head_recall = rate(paired_heads, "answer_pass_at_5") if paired_heads else None
    tail_recall = rate(paired_tails, "answer_pass_at_5") if paired_tails else None
    head_only_pass = sum(
        1
        for pair in complete_pairs
        if head_by_pair[pair]["answer_pass_at_5"] and not tail_by_pair[pair]["answer_pass_at_5"]
    )
    both_pass = sum(
        1
        for pair in complete_pairs
        if head_by_pair[pair]["answer_pass_at_5"] and tail_by_pair[pair]["answer_pass_at_5"]
    )
    neither_pass = sum(
        1
        for pair in complete_pairs
        if not head_by_pair[pair]["answer_pass_at_5"]
        and not tail_by_pair[pair]["answer_pass_at_5"]
    )
    tail_only_pass = len(complete_pairs) - head_only_pass - both_pass - neither_pass

    # ---- Ranking, separate from retrieval ----------------------------------
    ranked = [
        row
        for row in positives
        if row["answer_slot"] is not None and row["answer_term_coverage"] is not None
    ]
    inverted = [row for row in ranked if row["outranked_by_coverage"] is not None]
    zero_coverage_inversions = sum(
        1 for row in inverted if row["outranked_by_coverage"] == 0.0
    )
    # rank_inversion_rate blends two populations that answer_recall_at_5 keeps
    # apart: real questions and the 36 verbatim-sentence window queries, whose
    # exact-quote form is not how anyone searches. Report the split so a reader
    # can see which set a movement came from instead of inferring it.
    ranked_window = [row for row in ranked if row.get("window_role") is not None]
    ranked_plain = [row for row in ranked if row.get("window_role") is None]
    inverted_window = [row for row in ranked_window if row["outranked_by_coverage"] is not None]
    inverted_plain = [row for row in ranked_plain if row["outranked_by_coverage"] is not None]

    # ---- Per-request metadata stability ------------------------------------
    tiers_seen: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        for result_id, sha, tier in row.get("trust_observations") or []:
            tiers_seen.setdefault((result_id, sha), set()).add(tier)
    unstable = {key: sorted(v) for key, v in tiers_seen.items() if len(v) > 1}

    return {
        "quality": {
            "answer_recall_at_1": round(rate(span_rows, "answer_pass_at_1"), 4),
            "answer_recall_at_5": round(rate(span_rows, "answer_pass_at_5"), 4),
            "raw_page_recall_at_5": round(rate(span_rows, "raw_page_pass_at_5"), 4),
            "doc_recall_at_1": round(rate(positives, "doc_pass_at_1"), 4),
            "doc_recall_at_5": round(rate(positives, "doc_pass_at_5"), 4),
            "mrr_body": round(mrr_body, 4),
            "header_credit_rate": round(header_credit / len(span_rows), 4),
            "negative_hit_rate": round(negative_hits / len(probes), 4),
            # How many answer passes came off a document the question did NOT
            # name. `lint` proves a span unique across the ~49 cached
            # ground-truth files and cannot say anything about the corpus, so
            # this is the run-time reading of that blind spot. Non-zero does
            # not mean the pass was wrong; it means the pass was scored off
            # text lint never checked for uniqueness, and those rows should be
            # read before the recall figure is quoted.
            "answers_from_unexpected_documents": sum(
                1
                for row in positives
                if row["answer_pass_at_5"]
                and row.get("answer_from_expected_document") is False
            ),
        },
        "duplication": {
            "duplicate_blob_rate_at_10": (
                round(statistics.fmean(dup_rates), 4) if dup_rates else None
            ),
            "distinct_files_at_10": (
                round(statistics.fmean(file_counts), 2) if file_counts else None
            ),
            # 1.00 means every slot on the page was a different document. Lower
            # means one document is occupying slots the agent pays context for.
            # Read this NEXT TO recall: high recall with a low ratio is a page
            # that answers once and then repeats itself.
            "distinct_source_ratio_mean": (
                round(statistics.fmean(source_ratios), 4) if source_ratios else None
            ),
            "distinct_source_ratio_resolvable_only": (
                round(statistics.fmean(source_ratios_resolvable), 4)
                if source_ratios_resolvable
                else None
            ),
            "hits_with_unresolvable_source": unresolvable_hits,
        },
        "window": {
            "head_recall_at_5": round(head_recall, 4) if head_recall is not None else None,
            "tail_recall_at_5": round(tail_recall, 4) if tail_recall is not None else None,
            # The figure that survives the obvious objection. A document whose
            # HEAD quote also missed may simply not be in the index at all, and
            # its tail miss says nothing about the window. Restricting to pairs
            # whose head retrieved keeps only documents proven reachable.
            #
            # What it does NOT control for, stated beside the number: the pair
            # holds the DOCUMENT constant and replaces the QUERY entirely. Head
            # and tail are different sentences with different tokenisations
            # facing different corpus competition, so a tail miss is positional
            # OR lexical and this metric cannot separate the two. Measured on
            # the 2026-08-04 set the confound runs the other way in aggregate
            # (head queries averaged 7.1 extracted terms against the tails' 6.8
            # and faced MORE cached-body competitors, 8.9 against 6.3), so it
            # does not explain that day's 0 of 11 -- but "positional and
            # nothing else" is a stronger claim than the design supports.
            "tail_recall_given_head_at_5": (
                round(both_pass / (both_pass + head_only_pass), 4)
                if (both_pass + head_only_pass)
                else None
            ),
            "pairs_head_reachable": both_pass + head_only_pass,
            "window_penalty": (
                round(head_recall - tail_recall, 4)
                if head_recall is not None and tail_recall is not None
                else None
            ),
            "pairs_complete": len(complete_pairs),
            "pairs_head_only": head_only_pass,
            "pairs_both": both_pass,
            "pairs_neither": neither_pass,
            "pairs_tail_only": tail_only_pass,
            # Distinct expect_any DOCUMENTS, not distinct pair ids. Two pairs
            # quoting one document used to report `documents: 2`, overstating
            # how much of the corpus the window measurement covers -- the
            # name-versus-what-it-attests failure this harness exists to catch.
            "documents": len(
                {doc for row in window_rows for doc in (row.get("expect_any") or [])}
            ),
        },
        "ranking": {
            "answers_ranked": len(ranked),
            "rank_inversion_rate": (
                round(len(inverted) / len(ranked), 4) if ranked else None
            ),
            "inversions_by_zero_coverage": zero_coverage_inversions,
            "answer_worst_slot": max((row["answer_slot"] for row in ranked), default=None),
            # The split behind the headline rate. `answer_recall_at_5` excludes
            # window rows and this one does not, so the two denominators differ
            # and the blended figure is dominated by whichever set is larger.
            "answers_ranked_excluding_window": len(ranked_plain),
            "rank_inversion_rate_excluding_window": (
                round(len(inverted_plain) / len(ranked_plain), 4) if ranked_plain else None
            ),
            "answers_ranked_window_only": len(ranked_window),
            "rank_inversion_rate_window_only": (
                round(len(inverted_window) / len(ranked_window), 4)
                if ranked_window
                else None
            ),
        },
        "metadata_stability": {
            "chunks_observed": len(tiers_seen),
            "chunks_with_unstable_trust_tier": len(unstable),
            "unstable_examples": [
                {"result_id": key[0], "content_sha256": key[1], "tiers": value}
                for key, value in sorted(unstable.items())[:5]
            ],
        },
        "stability": {
            "hit_stability": round(statistics.fmean(stability), 4) if stability else None,
        },
        "counts": {
            "questions_total": len(rows),
            "questions_positive": len(positives),
            "questions_with_spans": len(span_rows),
            "questions_excluded_from_answer_recall": len(positives) - len(span_rows),
            "questions_blocked_probe": len(probes),
            "questions_window": len(window_rows),
        },
    }


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------


def content_fingerprint(state_path: Path) -> dict[str, Any]:
    """Content-derived corpus fingerprint from the syncer's per-file checkpoint.

    sha256 over sorted ``key:blob_sha`` lines from the ``files`` map of
    repo_content_sync_state.json (kb/repo_content_sync.py writes entries of
    {sha, content_hash, last_ingested_at, cognee_data_ids} keyed by
    ``org/repo/path``). Order-independent, so two dumps of the same corpus
    fingerprint identically.
    """
    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    lines = sorted(
        f"{key}:{(entry or {}).get('sha', '') if isinstance(entry, dict) else ''}"
        for key, entry in files.items()
    )
    if not lines:
        # An empty files map hashes to sha256("") on EVERY run, so two runs
        # pointed at empty state files would compare as the same corpus while
        # attesting nothing (this happened on 2026-08-03 and made
        # duplicate_blob_rate formally not comparable). Fail closed instead.
        return {
            "sha256": None,
            "files": 0,
            "source": str(state_path),
            "reason": (
                "the state file's files map is empty; a fingerprint over zero "
                "files attests nothing about the corpus, so this run is NOT "
                "comparable on content (point --repo-state at the node's real "
                "repo_content_sync_state.json)"
            ),
        }
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"sha256": digest, "files": len(lines), "source": str(state_path)}


def find_repo_state(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    env = os.getenv("CITADEL_REPO_STATE_PATH")
    candidates = [Path(env)] if env else []
    candidates += [
        Path.cwd() / ".citadel" / "repo_content_sync_state.json",
        HERE.parent.parent / ".citadel" / "repo_content_sync_state.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def harness_git_sha() -> str:
    try:
        return (
            subprocess.run(
                ["git", "-C", str(HERE), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:  # noqa: BLE001 - fingerprinting must not kill a run
        return "unknown"


def api_fingerprint(node_url: str, token: str, timeout: float) -> dict[str, Any]:
    """The API-derived corpus snapshot (documents_tracked, sources, version).

    Kept from the old harness: it is what caught a 0.40 -> 0.60 recall jump on
    2026-07-31 being corpus movement, not a code improvement. It reports what
    the node CLAIMS; the content fingerprint reports what the checkpoint holds.
    """
    fingerprint: dict[str, Any] = {}
    for name, path in (("state", "/api/state"), ("indexes", "/api/indexes")):
        request = urllib.request.Request(
            f"{node_url.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            fingerprint[name] = {"error": exc.__class__.__name__}
            continue
        if name == "state":
            # The image and the generation the run was taken against, both
            # already published on /api/state and both previously dropped.
            # `_release_identity_verdicts` READS them in release mode: a run
            # taken against a different build or a re-derived generation is
            # refused rather than reported as a retrieval delta. Recording
            # them without a reader would have been the same defect wearing a
            # comment that claimed otherwise.
            fingerprint["build_id"] = body.get("build_id")
            lifecycle = body.get("lifecycle")
            generation = (
                lifecycle.get("current_generation")
                if isinstance(lifecycle, dict)
                else None
            )
            fingerprint["lifecycle_generation"] = (
                {
                    key: generation.get(key)
                    for key in ("generation_id", "projection_version", "config_digest")
                }
                if isinstance(generation, dict)
                else None
            )
            fingerprint["documents_tracked"] = (body.get("totals") or {}).get("documents")
            fingerprint["sources"] = [
                {
                    "name": source.get("name"),
                    "type": source.get("type"),
                    "documents": source.get("documents"),
                    "last_synced_at": source.get("last_synced_at"),
                }
                for source in body.get("sources") or []
            ]
            fingerprint["node_version"] = body.get("version")
        else:
            fingerprint["indexes_stats"] = body.get("stats") or {}
    return fingerprint


# --------------------------------------------------------------------------
# Corpus census (/api/corpus)
# --------------------------------------------------------------------------

CENSUS_PAGE_LIMIT = 1000  # /api/corpus CORPUS_MAX_LIMIT
CENSUS_MAX_PAGES = 100    # 100k documents; a ceiling, not an expectation


def make_corpus_fetcher(
    node_url: str, token: str, timeout: float
) -> Callable[[str | None], dict[str, Any]]:
    """One page of GET /api/corpus. Raises on HTTP/network failure; the
    census turns that into an explicit "unavailable", never a silent zero."""

    def fetch(cursor: str | None) -> dict[str, Any]:
        params = {"limit": str(CENSUS_PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        request = urllib.request.Request(
            f"{node_url.rstrip('/')}/api/corpus?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    return fetch


def corpus_census(
    fetch_page: Callable[[str | None], dict[str, Any]],
    max_pages: int = CENSUS_MAX_PAGES,
) -> dict[str, Any]:
    """Walk the whole corpus and count documents the index cannot reach.

    /api/corpus is the only endpoint that can enumerate the corpus (the mesh
    graph caps at 1000 nodes; per-source counters are sync bookkeeping). A row
    with chunk_count 0 was accepted into the durable store but never
    vector-indexed, so search cannot return it; a recall figure that ignores
    that share overstates what retrieval covers. chunk_count null means "not
    measured", never zero, and is counted separately.

    chunk_count_zero_ratio divides by every walked document (matching the
    published "892 of 2867" definition); unmeasured rows make the zero count a
    floor, which the report says next to the number.

    Degrades to {"error", "reason"} when the endpoint is unreachable or the
    token lacks admin/audit:read: the bench must still run, but the report
    then states the census is unavailable instead of inventing a count.
    """
    started = time.perf_counter()
    walked = zero = unmeasured = 0
    documents_total: Any = None
    cursor: str | None = None
    pages = 0
    truncated = False
    notes: list[str] = []
    while True:
        if pages >= max_pages:
            truncated = True
            notes.append(
                f"census stopped at the {max_pages}-page ceiling; counts are a floor"
            )
            break
        try:
            body = fetch_page(cursor)
        except urllib.error.HTTPError as exc:
            return {
                "error": f"HTTP {exc.code}",
                "reason": (
                    "corpus census unavailable (GET /api/corpus needs admin or "
                    "audit:read); the never-indexed share was not measured"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - census must not kill the run
            return {
                "error": exc.__class__.__name__,
                "reason": (
                    "corpus census failed mid-walk; the never-indexed share "
                    "was not measured"
                ),
            }
        pages += 1
        for row in body.get("documents") or []:
            if not isinstance(row, dict):
                continue
            walked += 1
            count = row.get("chunk_count")
            if count is None:
                unmeasured += 1
            elif int(count) == 0:
                zero += 1
        if body.get("documents_total") is not None:
            documents_total = body.get("documents_total")
        next_cursor = body.get("next_cursor")
        if not next_cursor:
            break
        if next_cursor == cursor:
            truncated = True
            notes.append("census stopped: next_cursor did not advance")
            break
        cursor = next_cursor
    result: dict[str, Any] = {
        "documents_total": documents_total,
        "documents_walked": walked,
        "chunk_count_zero": zero,
        "chunk_count_unmeasured": unmeasured,
        "chunk_count_zero_ratio": round(zero / walked, 4) if walked else None,
        "pages": pages,
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }
    if notes:
        result["notes"] = notes
    return result


def ground_truth_fingerprint(cache_dir: Path | None = None) -> dict[str, Any]:
    """sha256 over the cached ground-truth bodies the run scored against.

    `ground_truth/` is gitignored and `fetch_ground_truth.py` pulls each file
    from GitHub at HEAD with no ref, then skips anything already on disk. Two
    machines therefore bake in whatever HEAD was on the day they first fetched.
    The cache is not decoration: `load_ground_truth_shingles` feeds `doc_rank`'s
    shingle fallback and `legacy_rank`, so it moves `doc_recall_at_5` and
    `header_credit_rate`. Recording its hash is what lets `compare` say so.

    Drift is not hypothetical: on 2026-08-05 a re-fetch of the w05 fixture's
    document came back 5617 chars against the 5373 the fixture recorded, with
    the quote at 4679 rather than 4435.
    """
    # Resolved at call time, not bound as a default, so the directory the run
    # actually read is the one that gets hashed.
    cache_dir = GROUND_TRUTH if cache_dir is None else cache_dir
    if not cache_dir.exists():
        return {
            "sha256": None,
            "files": 0,
            "reason": (
                f"ground-truth cache not found at {cache_dir}; runs are NOT "
                "comparable on the bodies doc_rank fell back to"
            ),
        }
    digests: dict[str, str] = {}
    for path in sorted(cache_dir.iterdir()):
        if path.is_file():
            digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not digests:
        return {
            "sha256": None,
            "files": 0,
            "reason": (
                f"ground-truth cache at {cache_dir} is empty; runs are NOT "
                "comparable on the bodies doc_rank fell back to"
            ),
        }
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(canonical).hexdigest(), "files": len(digests)}


# --------------------------------------------------------------------------
# Release-run identity
# --------------------------------------------------------------------------
#
# Two runs can agree on corpus and questions and still not be one measurement:
# a different machine, a different Docker resource envelope, a different
# warm-up, a different embedding model or chunk budget all move latency and
# retrieval without anything in the vault changing. The generic gate cannot see
# any of it. Release mode records the identity and refuses to compare across a
# difference, so a v0.5 verdict is about the candidate and nothing else.
#
# `runtime_id` is a one-way digest of machine plus Docker engine identity, NOT
# a hostname: this object is written into a run JSON, and a run JSON is an
# artifact that travels.
DIGEST_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


def _release_digest(name: str, value: Any) -> str:
    if not isinstance(value, str) or not DIGEST_PATTERN.match(value):
        raise BenchError(
            f"release context {name} must be a 'sha256:<64 hex>' digest, got {value!r}"
        )
    return value


def _release_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchError(f"release context {name} must be a nonempty string, got {value!r}")
    return value


def _release_count(name: str, value: Any, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BenchError(
            f"release context {name} must be an integer >= {minimum}, got {value!r}"
        )
    return value


RELEASE_CONTEXT_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "runtime_id": lambda value: _release_digest("runtime_id", value),
    "docker_resource_digest": lambda value: _release_digest(
        "docker_resource_digest", value
    ),
    "warmup_count": lambda value: _release_count("warmup_count", value, minimum=0),
    "retrieval_profile": lambda value: _release_text("retrieval_profile", value),
    "generation_id": lambda value: _release_text("generation_id", value),
    "model": lambda value: _release_text("model", value),
    "dimensions": lambda value: _release_count("dimensions", value, minimum=1),
    "chunk_budget_tokens": lambda value: _release_count(
        "chunk_budget_tokens", value, minimum=1
    ),
}

# What `compare` refuses to look past in release mode. The first eight come
# from --release-context; the last three are recorded by the run itself, so a
# top-5 page cannot be compared against a top-10 page and a 1-repeat run cannot
# be compared against a 5-repeat one.
RELEASE_IDENTITY_FIELDS = tuple(RELEASE_CONTEXT_VALIDATORS) + (
    "repeat_count",
    "requested_top_k",
    "client_image_digest",
)


def parse_release_context(raw: str) -> dict[str, Any]:
    """Validate the exact nonsecret release-context shape. Fail closed.

    Unknown keys are refused rather than ignored: this object is copied into a
    published run JSON, and "ignore what you do not recognise" is how a
    hostname or a token ends up in an artifact that travels.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchError(f"--release-context is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchError("--release-context must be a JSON object")
    unknown = sorted(set(payload) - set(RELEASE_CONTEXT_VALIDATORS))
    if unknown:
        raise BenchError(
            "--release-context carries unknown field(s): " + ", ".join(unknown)
        )
    missing = sorted(set(RELEASE_CONTEXT_VALIDATORS) - set(payload))
    if missing:
        raise BenchError(
            "--release-context is missing required field(s): " + ", ".join(missing)
        )
    return {
        name: validator(payload[name])
        for name, validator in RELEASE_CONTEXT_VALIDATORS.items()
    }


def build_fingerprint(
    node_url: str,
    token: str,
    timeout: float,
    questions_path: Path,
    repo_state: Path | None,
    *,
    release_context: dict[str, Any] | None = None,
    repeats: int = 1,
    requested_top_k: int | None = None,
    client_image_digest: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any]
    if repo_state is None:
        content = {
            "sha256": None,
            "reason": (
                "repo_content_sync_state.json not found; pass --repo-state or "
                "set CITADEL_REPO_STATE_PATH (runs without it are NOT "
                "comparable on content)"
            ),
        }
    else:
        content = content_fingerprint(repo_state)
    fingerprint: dict[str, Any] = {
        "api": api_fingerprint(node_url, token, timeout),
        "census": corpus_census(make_corpus_fetcher(node_url, token, timeout)),
        "content": content,
        "harness_git_sha": harness_git_sha(),
        # Two different hashes on purpose. questions_sha256 is over the FILE
        # bytes, so reformatting or a comment moves it. questions_pin is over
        # the canonical question list, so it moves only when a question really
        # changes. A run records both: the pin says which frozen set was
        # answered, the file hash says whether the file was touched at all.
        "questions_sha256": hashlib.sha256(questions_path.read_bytes()).hexdigest(),
        "questions_pin": questions_pin(load_questions(questions_path)),
        "ground_truth": ground_truth_fingerprint(),
        "python_version": platform.python_version(),
    }
    if release_context is not None:
        fingerprint["release"] = dict(release_context) | {
            "repeat_count": repeats,
            "requested_top_k": (
                max(K_ANSWER, K_DUP) if requested_top_k is None else requested_top_k
            ),
            "client_image_digest": client_image_digest,
        }
    return fingerprint


# sha256 of the empty string: what content_fingerprint recorded for an empty
# files map before the fail-closed fix. Old run JSONs (the 2026-08-03 baseline
# included) carry it, and two of them would otherwise compare as the same
# corpus while attesting nothing.
EMPTY_MAP_SHA256 = hashlib.sha256(b"").hexdigest()


def usable_content_sha(sha: Any) -> Any:
    """The empty-file-map sha attests nothing; treat it as unavailable.

    Single point of judgement shared by compare and report, so the two can
    never disagree about whether a run is comparable on content.
    """
    return None if sha == EMPTY_MAP_SHA256 else sha


def _release_identity_verdicts(
    current: dict[str, Any], baseline: dict[str, Any]
) -> list[str]:
    """Every release identity difference that makes two runs one measurement no
    longer. Empty means the two runs were taken under the same identity."""
    current_release = current.get("release")
    baseline_release = baseline.get("release")
    missing = [
        label
        for label, value in (
            ("candidate", current_release),
            ("baseline", baseline_release),
        )
        if not isinstance(value, dict) or not value
    ]
    if missing:
        return [
            "NOT COMPARABLE: release identity is missing on "
            + " and ".join(missing)
            + "; run the benchmark with --release-context"
        ]
    verdicts: list[str] = []
    for field in RELEASE_IDENTITY_FIELDS:
        if field not in current_release or field not in baseline_release:
            verdicts.append(
                f"NOT COMPARABLE: release identity field {field} was never recorded"
            )
        elif current_release[field] != baseline_release[field]:
            verdicts.append(
                f"NOT COMPARABLE: release identity {field} differs "
                f"({baseline_release[field]!r} -> {current_release[field]!r})"
            )

    current_api = current.get("api")
    baseline_api = baseline.get("api")
    current_api = current_api if isinstance(current_api, dict) else {}
    baseline_api = baseline_api if isinstance(baseline_api, dict) else {}
    runs = (
        ("baseline", baseline_release, baseline_api),
        ("candidate", current_release, current_api),
    )

    # `client_image_digest` is the one identity field the operator supplies
    # outside the validated --release-context, and it defaults to an
    # environment variable. Nobody setting it on EITHER run recorded None on
    # both sides, and None == None passed as a matched identity: the likely
    # misconfiguration read as evidence. Validate it like the digests it sits
    # beside, per run, so an absent client image is unmeasured and not equal.
    for label, release_block, _api_block in runs:
        digest = release_block.get("client_image_digest")
        if not isinstance(digest, str) or not DIGEST_PATTERN.match(digest):
            verdicts.append(
                f"NOT COMPARABLE: {label} client_image_digest is not a "
                f"'sha256:<64 hex>' digest ({digest!r}); the benchmark client "
                "image is unmeasured, not matched"
            )

    # What the NODE said it was, against what the other run's node said. The
    # eight context fields above are typed by an operator; these two are
    # observed, so they are what stops a run taken against a different build or
    # a re-derived generation from being compared as the same measurement.
    for field in ("build_id", "lifecycle_generation"):
        current_value = current_api.get(field)
        baseline_value = baseline_api.get(field)
        if current_value is None or baseline_value is None:
            verdicts.append(
                f"NOT COMPARABLE: node {field} was never recorded on at least "
                "one run; the node identity behind these numbers is unmeasured"
            )
        elif current_value != baseline_value:
            verdicts.append(
                f"NOT COMPARABLE: node {field} differs "
                f"({baseline_value!r} -> {current_value!r})"
            )

    # The generation an operator TYPED against the generation the node
    # REPORTED. Without this the typed value attests only that somebody typed
    # it, and a typo would name a generation that never served the run.
    for label, release_block, api_block in runs:
        observed = api_block.get("lifecycle_generation")
        observed_id = observed.get("generation_id") if isinstance(observed, dict) else None
        typed_id = release_block.get("generation_id")
        if observed_id is not None and typed_id != observed_id:
            verdicts.append(
                f"NOT COMPARABLE: {label} release generation_id {typed_id!r} "
                f"does not match the generation the node reported ({observed_id!r})"
            )
    return verdicts


def compare_fingerprints(
    current: dict[str, Any], baseline: dict[str, Any], *, release: bool = False
) -> tuple[bool, list[str]]:
    """Decide whether two runs are comparable. Returns (comparable, verdicts).

    ``release`` layers the v0.5 release-run identity over the generic corpus
    and question checks. It never relaxes them.
    """
    verdicts: list[str] = []
    comparable = True
    current_sha = (current.get("content") or {}).get("sha256")
    baseline_sha = (baseline.get("content") or {}).get("sha256")
    if EMPTY_MAP_SHA256 in (current_sha, baseline_sha):
        verdicts.append(
            "note: a recorded content fingerprint is the sha256 of an empty "
            "file map (written before the fail-closed fix); it attests "
            "nothing and is treated as unavailable"
        )
        current_sha = usable_content_sha(current_sha)
        baseline_sha = usable_content_sha(baseline_sha)
    if current_sha is None or baseline_sha is None:
        comparable = False
        verdicts.append(
            "NOT COMPARABLE: content fingerprint unavailable on "
            + ("both runs" if current_sha is None and baseline_sha is None else "one run")
        )
    elif current_sha != baseline_sha:
        comparable = False
        verdicts.append(
            "CORPUS MOVED: content fingerprints differ "
            f"({baseline_sha[:12]} -> {current_sha[:12]}). NOT COMPARABLE: any "
            "metric delta includes corpus movement, not just code."
        )
    # The PIN is the authority on which frozen set was answered, not the file
    # hash. `questions_pin` is over the canonical question list, so it moves
    # when a question really changes; `questions_sha256` is over the file bytes,
    # so reindenting the JSON or bumping `version` moves it while both runs
    # answered exactly the same questions. Gating on the file hash withheld the
    # whole delta in precisely the case the pin was introduced to fix. A run
    # predating the pin has no pin, so the file hash still gates there: an
    # artifact that cannot say which set it answered is not comparable on
    # anybody's word.
    current_pin = current.get("questions_pin")
    baseline_pin = baseline.get("questions_pin")
    if current_pin and baseline_pin:
        if current_pin != baseline_pin:
            comparable = False
            verdicts.append(
                "QUESTIONS CHANGED: frozen sets differ "
                f"({str(baseline_pin)[:16]} -> {str(current_pin)[:16]}). NOT "
                "COMPARABLE."
            )
        elif current.get("questions_sha256") != baseline.get("questions_sha256"):
            verdicts.append(
                "note: the questions FILE differs but questions_pin is "
                f"identical ({str(current_pin)[:16]}); the two runs answered "
                "the same frozen set and the delta stands"
            )
    elif current.get("questions_sha256") != baseline.get("questions_sha256"):
        comparable = False
        verdicts.append(
            "QUESTIONS CHANGED: golden sets differ and at least one run "
            "predates questions_pin, so the file hash is all there is. NOT "
            "COMPARABLE."
        )
    # The ground-truth cache is refetched from an unpinned upstream HEAD and
    # feeds doc_rank's shingle fallback and legacy_rank. A note, not a gate:
    # every run taken before this key existed would otherwise be refused.
    current_gt = (current.get("ground_truth") or {}).get("sha256")
    baseline_gt = (baseline.get("ground_truth") or {}).get("sha256")
    if current_gt is None or baseline_gt is None:
        verdicts.append(
            "note: ground-truth cache fingerprint unavailable on at least one "
            "run; doc_recall_at_5 and header_credit_rate are not attested to "
            "have been scored against the same cached bodies"
        )
    elif current_gt != baseline_gt:
        verdicts.append(
            "note: ground-truth cache differs "
            f"({baseline_gt[:12]} -> {current_gt[:12]}); doc_recall_at_5 and "
            "header_credit_rate use it as a fallback and can move without the "
            "node changing"
        )
    # A note in generic mode, a GATE in release mode. Every run taken before
    # the key existed would be refused by a blanket gate, but two runs claiming
    # to be a v0.5 release pair have no such excuse: scored against different
    # cached bodies they are two measurements, not one.
    if release and (
        current_gt is None or baseline_gt is None or current_gt != baseline_gt
    ):
        comparable = False
        verdicts.append(
            "NOT COMPARABLE: release runs must be scored against one "
            "ground-truth cache"
        )
    if release:
        identity_verdicts = _release_identity_verdicts(current, baseline)
        if identity_verdicts:
            comparable = False
            verdicts.extend(identity_verdicts)
    if current.get("harness_git_sha") != baseline.get("harness_git_sha"):
        verdicts.append(
            "note: harness git sha differs "
            f"({baseline.get('harness_git_sha')} -> {current.get('harness_git_sha')})"
        )
    # The content fingerprint covers repo-content files only. The census
    # covers the WHOLE corpus (digests, session notes, everything), so totals
    # can drift while repo-content stands still; whole-corpus metrics
    # (duplication, digest staleness) move with it. A note, not a gate: digest
    # count grows daily by design and gating on it would block every compare.
    current_total = (current.get("census") or {}).get("documents_total")
    baseline_total = (baseline.get("census") or {}).get("documents_total")
    if (
        current_total is not None
        and baseline_total is not None
        and current_total != baseline_total
    ):
        verdicts.append(
            "note: corpus census document totals differ "
            f"({baseline_total} -> {current_total}); repo-content is unchanged "
            "but whole-corpus metrics moved with the corpus"
        )
    if comparable:
        verdicts.insert(0, "COMPARABLE: corpus and questions unchanged.")
    return comparable, verdicts


# --------------------------------------------------------------------------
# Lint
# --------------------------------------------------------------------------


def _synthetic_header(repo_path: str) -> str:
    org, repo, sub = repo_path.split("/", 2)
    return normalize(
        "\n".join(
            [
                f"# {repo_path}",
                f"Repository: {org}/{repo}",
                f"Source: https://github.com/{org}/{repo}/blob/HEAD/{sub}",
                "Commit:",
                "Blob:",
                "Retrieved:",
                "---",
            ]
        )
    )


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def lint_questions(
    questions_path: Path, ground_truth_dir: Path
) -> tuple[list[str], list[str]]:
    """Validate every answer span. Returns (problems, notes).

    Rules per span: present in a cached expect_any body; absent from the
    synthetic sync header and from the document's first content line (a span
    matching only the head line is the old head-of-document credit, not an
    answer); absent from the question text; unique across every OTHER cached
    body. Every question must carry either non-empty answer_spans or an
    explicit unconverted marker -- never neither, never both.
    """
    data = json.loads(questions_path.read_text(encoding="utf-8"))
    questions = list(data.get("questions") or [])
    problems: list[str] = []
    notes: list[str] = []

    cached_raw: dict[str, str] = {}
    if ground_truth_dir.exists():
        for path in sorted(ground_truth_dir.iterdir()):
            if path.is_file():
                cached_raw[path.name.replace("__", "/")] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
    cached_norm = {key: normalize(value) for key, value in cached_raw.items()}
    if not cached_raw:
        problems.append(
            f"ground-truth cache is empty at {ground_truth_dir}; run "
            "fetch_ground_truth.py first (lint cannot validate spans without it)"
        )

    probes = [q for q in questions if q.get("expected_recall", 1) == 0]
    if not probes:
        notes.append(
            "NOTE: zero blocked-probe/negative questions (expected_recall=0). "
            "`run` will refuse to execute until probes are added."
        )

    scanner_patterns: dict[str, re.Pattern[str]] | None = None
    for question in questions:
        qid = question.get("id", "?")
        spans = question.get("answer_spans")
        reason = question.get(UNCONVERTED_KEY)
        has_spans = bool(spans)
        if question.get("expected_recall", 1) == 0:
            if has_spans:
                problems.append(f"{qid}: a blocked probe must not carry answer_spans")
            category = question.get(BLOCKED_PATTERN_KEY)
            if not category and not question.get("expect_any"):
                problems.append(
                    f"{qid}: a probe needs blocked_pattern or a non-empty "
                    "expect_any; with neither it can never register a hit"
                )
            if category:
                if scanner_patterns is None:
                    try:
                        scanner_patterns = load_scanner_patterns()
                    except BenchError as exc:
                        problems.append(str(exc))
                        scanner_patterns = {}
                if scanner_patterns and category not in scanner_patterns:
                    problems.append(
                        f"{qid}: blocked_pattern '{category}' is not a blocking "
                        "rule in kb.security_scan.SECRET_PATTERNS; a probe "
                        "derived from a deleted rule tests nothing"
                    )
                elif scanner_patterns and scanner_patterns[str(category)].search(
                    question.get("question", "")
                ):
                    problems.append(
                        f"{qid}: the question text itself matches the "
                        f"'{category}' scanner rule; a probe query must never "
                        "carry a matching secret"
                    )
            continue
        if has_spans and reason:
            problems.append(f"{qid}: has both answer_spans and {UNCONVERTED_KEY}")
        if not has_spans:
            if not reason:
                problems.append(
                    f"{qid}: no answer_spans and no {UNCONVERTED_KEY} marker; "
                    "unconverted questions must be excluded explicitly"
                )
            continue
        if not isinstance(spans, list) or len(spans) > MAX_SPANS:
            problems.append(f"{qid}: answer_spans must be a list of 1-{MAX_SPANS} quotes")
            continue

        expected = list(question.get("expect_any") or [])
        expected_cached = [p for p in expected if p in cached_norm]
        if not expected_cached:
            problems.append(
                f"{qid}: has answer_spans but no expect_any document is cached "
                "in ground_truth/"
            )
            continue

        for position, span in enumerate(spans, start=1):
            label = f"{qid} span {position}"
            if not isinstance(span, str) or not span.strip():
                problems.append(f"{label}: empty span")
                continue
            norm_span = normalize(span)
            if len(norm_span) < MIN_SPAN_CHARS:
                problems.append(
                    f"{label}: too short after normalization "
                    f"({len(norm_span)} < {MIN_SPAN_CHARS} chars)"
                )
            if not any(norm_span in cached_norm[p] for p in expected_cached):
                problems.append(f"{label}: not found in any cached ground-truth body")
            for repo_path in expected_cached:
                if norm_span in _synthetic_header(repo_path):
                    problems.append(
                        f"{label}: appears in the sync header block for {repo_path}"
                    )
                first_line = normalize(_first_content_line(cached_raw[repo_path]))
                if first_line and norm_span in first_line:
                    problems.append(
                        f"{label}: appears in the first line of {repo_path} "
                        "(head-of-document credit, not an answer)"
                    )
            # A window question IS its own quote: the query is the verbatim
            # sentence, which is the strongest retrieval signal that exists. If
            # that fails, retrieval failed. Every other question keeps the rule,
            # because there the span appearing in the query would hand the
            # scorer the answer.
            if norm_span in normalize(question.get("question", "")) and not is_window_question(
                question
            ):
                problems.append(f"{label}: appears in the question text")
            for other, body in cached_norm.items():
                if other in expected:
                    continue
                if norm_span in body:
                    problems.append(
                        f"{label}: not unique, also present in cached body {other}"
                    )

        if is_window_question(question):
            window_problems, window_notes = _lint_window_question(question, cached_raw)
            problems.extend(window_problems)
            notes.extend(window_notes)

    problems.extend(_lint_freeze_pin(data))
    return problems, notes


def _lint_window_question(
    question: dict[str, Any], cached_raw: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The contract that makes a window pair a controlled comparison.

    Without these checks a failing tail is just a failing question. With them a
    failing tail means: an exact, corpus-unique sentence, sitting past the depth
    threshold, carrying terms of its own that the head does not have, did not
    retrieve the one document that contains it.

    Every threshold below is enforced on a MEASURED value, never on a declared
    one. `source_offset_chars` and `depth_fraction` are claims a fixture author
    writes; `body.find(quote)` is where the sentence actually sits. Trusting
    the claim let a quote inside the head window ship declared as a deep tail,
    which lands in `tail_recall_at_5` and in the `tail_recall_given_head_at_5`
    denominator while every artifact says it tested the tail.

    Returns (problems, notes).
    """
    qid = question.get("id", "?")
    role = question.get("window_role")
    problems: list[str] = []
    notes: list[str] = []
    if role not in ("head", "tail"):
        return [f"{qid}: window question needs window_role of 'head' or 'tail'"], notes

    expected_category = (
        WINDOW_HEAD_CATEGORY if role == "head" else WINDOW_TAIL_CATEGORY
    )
    if str(question.get("category", "")) != expected_category:
        problems.append(
            f"{qid}: window_role {role!r} needs category {expected_category!r} "
            f"(got {str(question.get('category', ''))!r}); lint and summarize "
            "must agree about which questions are window questions"
        )
    if not question.get("window_pair"):
        problems.append(f"{qid}: window question needs a window_pair id")

    expected = list(question.get("expect_any") or [])
    body = next((cached_raw[p] for p in expected if p in cached_raw), None)
    if body is None:
        problems.append(f"{qid}: no expect_any document is cached; cannot verify offset")
        return problems, notes

    declared = question.get("source_offset_chars")
    if not isinstance(declared, int):
        problems.append(f"{qid}: window question needs an integer source_offset_chars")
        return problems, notes

    quote = str(question.get("question", ""))
    offset = body.find(quote.strip())
    if offset < 0:
        problems.append(f"{qid}: the quote is not present verbatim in the cached body")
        return problems, notes

    depth = offset / len(body) if body else 0.0
    # A declared offset that disagrees with the measured one is a NOTE, not a
    # problem: `ground_truth/` is refetched from an unpinned upstream HEAD, so
    # ordinary whitespace drift moves it by a few characters and hard-failing
    # would make lint unusable on any machine that fetched on a different day.
    # The thresholds below never read the declared value, so a stale claim
    # cannot change a verdict; it can only be out of date in the artifact.
    if declared != offset:
        notes.append(
            f"NOTE: {qid} declares source_offset_chars {declared} but the quote "
            f"sits at {offset} in the cached body (depth {depth:.4f}); the "
            "cached ground truth has drifted from the fixture. Thresholds were "
            "checked against the measured offset."
        )
    declared_depth = question.get("depth_fraction")
    if isinstance(declared_depth, (int, float)) and abs(depth - declared_depth) > 0.005:
        notes.append(
            f"NOTE: {qid} declares depth_fraction {declared_depth} but the "
            f"measured depth is {depth:.4f}"
        )

    if role == "head":
        if offset >= HEAD_WINDOW_CHARS:
            problems.append(
                f"{qid}: head quote sits at offset {offset}, outside the first "
                f"{HEAD_WINDOW_CHARS} characters; it is not a head control"
            )
    else:
        if depth < TAIL_MIN_DEPTH:
            problems.append(
                f"{qid}: tail quote sits at depth {depth:.2f}, BELOW the "
                f"{TAIL_MIN_DEPTH} threshold; too shallow to test the window"
            )
        novel = question.get("novel_terms_absent_from_head")
        if not isinstance(novel, list) or len(novel) < TAIL_MIN_NOVEL_TERMS:
            problems.append(
                f"{qid}: tail quote needs at least {TAIL_MIN_NOVEL_TERMS} "
                "novel_terms_absent_from_head; without them a pass could be the "
                "head embedding answering the query"
            )
        else:
            declared_novel = set(map(str, novel))
            # A word the quote does not contain cannot make the quote novel.
            # Without this, three arbitrary dictionary words absent from the
            # head satisfy the control while saying nothing about this sentence.
            not_in_quote = sorted(declared_novel - distinctive_terms(quote))
            if not_in_quote:
                problems.append(
                    f"{qid}: terms declared novel are not terms of the quote "
                    f"({', '.join(not_in_quote)}); a word the quote does not "
                    "contain cannot make it novel"
                )
            head_terms = distinctive_terms(body[:HEAD_WINDOW_CHARS])
            leaked = sorted(declared_novel & head_terms)
            if leaked:
                problems.append(
                    f"{qid}: terms declared novel DO occur in the document head "
                    f"({', '.join(leaked)}); the control is void"
                )
    return problems, notes


def _lint_freeze_pin(data: dict[str, Any]) -> list[str]:
    """The fixtures are frozen or they are not. This is what enforces it."""
    frozen = data.get("frozen")
    if not isinstance(frozen, dict):
        return [
            "questions file has no `frozen` block; without a pin the set can "
            "drift between two runs that both call themselves the baseline"
        ]
    pinned = frozen.get("questions_sha256")
    actual = questions_pin(list(data.get("questions") or []))
    if not pinned:
        return [
            "frozen.questions_sha256 is missing; set it to the current pin "
            f"({actual}) to freeze this set"
        ]
    if pinned != actual:
        return [
            "FROZEN SET CHANGED: frozen.questions_sha256 is "
            f"{pinned} but the questions hash to {actual}. A baseline taken "
            "before this edit answered different questions. Bump `version`, "
            "update the pin deliberately, and re-take the baseline."
        ]
    return []


# --------------------------------------------------------------------------
# Benchmark execution
# --------------------------------------------------------------------------

Searcher = Callable[[str, int], tuple[dict[str, Any] | None, float, str | None]]

# --------------------------------------------------------------------------
# Attempt classification
# --------------------------------------------------------------------------
#
# A 200 with a body is NOT a served page. The loop below used to count only
# `error or body is None`, so a stream that stopped mid-JSON, a provider error
# rendered into a successful envelope, or a `results` key that never arrived
# all scored as a clean miss: the run lost a question's worth of retrieval and
# reported zero errors while doing it. Every one of those is now named, counted
# and separable, because "recall moved" and "the request never completed" are
# different findings and only one of them is about the node's quality.
ATTEMPT_OK = "ok"
ATTEMPT_TIMEOUT = "timeout"
ATTEMPT_TRUNCATION = "truncation"
ATTEMPT_PARTIAL = "partial"
ATTEMPT_PROVIDER = "provider"
ATTEMPT_MALFORMED = "malformed"
ATTEMPT_TRANSPORT = "transport"

FAILED_ATTEMPT_KINDS = (
    ATTEMPT_TIMEOUT,
    ATTEMPT_TRUNCATION,
    ATTEMPT_PARTIAL,
    ATTEMPT_PROVIDER,
    ATTEMPT_MALFORMED,
    ATTEMPT_TRANSPORT,
)

# Matched against the searcher's error string, lowercased. 408 and 504 are the
# node's own way of reporting the deadline the client would otherwise report as
# a socket timeout; both are the same finding.
TIMEOUT_MARKERS = ("timeout", "timed out", "timedout", "http 408", "http 504")
# A body that stopped arriving mid-JSON reaches the searcher as a decode or
# short-read failure, never as an HTTP status.
TRUNCATION_MARKERS = (
    "jsondecodeerror",
    "incompleteread",
    "contenttooshort",
    "chunkedencoding",
)
# Keys a 200 body uses to carry an upstream failure it did not raise.
PROVIDER_ERROR_KEYS = ("error", "detail", "errors")


def classify_attempt(body: Any, error: str | None) -> str:
    """Name what one search attempt actually returned.

    Order is deliberate and fail-closed: a body that declares a provider error
    is a provider failure even when it also happens to be malformed, because
    the provider error is the cause and the missing results are the symptom.
    """
    if error:
        lowered = str(error).lower()
        if any(marker in lowered for marker in TIMEOUT_MARKERS):
            return ATTEMPT_TIMEOUT
        if any(marker in lowered for marker in TRUNCATION_MARKERS):
            return ATTEMPT_TRUNCATION
        return ATTEMPT_TRANSPORT
    if body is None:
        return ATTEMPT_TRANSPORT
    if not isinstance(body, dict):
        return ATTEMPT_MALFORMED
    if any(body.get(key) for key in PROVIDER_ERROR_KEYS):
        return ATTEMPT_PROVIDER
    results = body.get("results")
    if not isinstance(results, list):
        return ATTEMPT_MALFORMED
    if any(not isinstance(item, dict) for item in results):
        return ATTEMPT_MALFORMED
    if body.get("truncated") is True:
        return ATTEMPT_TRUNCATION
    if body.get("partial") is True or body.get("complete") is False:
        return ATTEMPT_PARTIAL
    return ATTEMPT_OK


def hit_dataset(hit: dict[str, Any]) -> str | None:
    """The dataset a hit says it came from, or None when it says nothing.

    None is not "the expected dataset". An unlabelled hit is unattributable and
    is counted in its own bucket, never folded into a zero foreign-hit count.
    """
    for source in (hit, hit.get("_citadel")):
        if not isinstance(source, dict):
            continue
        value = source.get("dataset")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def make_http_searcher(node_url: str, token: str, timeout: float) -> Searcher:
    def searcher(query: str, top_k: int) -> tuple[dict[str, Any] | None, float, str | None]:
        payload = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
        request = urllib.request.Request(
            f"{node_url.rstrip('/')}/search",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body, (time.perf_counter() - started) * 1000.0, None
        except urllib.error.HTTPError as exc:
            return None, (time.perf_counter() - started) * 1000.0, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - a benchmark records, not raises
            return None, (time.perf_counter() - started) * 1000.0, exc.__class__.__name__

    return searcher


def make_ci_searcher(
    questions: list[dict[str, Any]], ground_truth_dir: Path
) -> Searcher:
    """Serve the tracked synthetic fixture through the real scoring path.

    This is a harness self-check, not a live retrieval measurement. It proves
    that the frozen question contract, body matching, identity parsing, and
    negative probe remain wired together in a clean checkout without a token or
    a network request.
    """
    answers: dict[str, dict[str, Any]] = {}
    for question in questions:
        if question.get("expected_recall", 1) == 0:
            continue
        expected = list(question.get("expect_any") or [])
        if len(expected) != 1:
            raise BenchError(
                f"{question.get('id', '?')}: CI fixture searcher requires exactly "
                "one expect_any document"
            )
        source = expected[0]
        fixture = ground_truth_dir / source.replace("/", "__")
        if not fixture.is_file():
            raise BenchError(
                f"{question.get('id', '?')}: CI ground-truth fixture is missing: "
                f"{fixture}"
            )
        body = fixture.read_text(encoding="utf-8")
        text = "\n".join(
            [
                f"# {source}",
                "",
                "Repository: citadel-ci/fixtures",
                f"Source: https://github.com/citadel-ci/fixtures/blob/main/{source.rsplit('/', 1)[-1]}",
                "Commit: ci-fixture",
                "Blob: cccccccccccccccccccccccccccccccccccccccc",
                "",
                "---",
                "",
                body,
            ]
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        answers[str(question["question"])] = {
            "id": f"chunk:{digest}",
            "document_id": f"doc:{source}",
            "text": text,
            "_citadel": {
                "rank": 1,
                "result_id": f"chunk:{digest}",
                "relevance": {"term_coverage": 1.0},
            },
        }

    def searcher(query: str, top_k: int) -> tuple[dict[str, Any], float, str | None]:
        hit = answers.get(query)
        return {"results": [hit] if hit is not None else []}, 0.0, None

    return searcher


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["questions"])


def execute_benchmark(
    questions: list[dict[str, Any]],
    searcher: Searcher,
    *,
    repeats: int = 1,
    quiet: bool = False,
    expect_dataset: str | None = None,
) -> dict[str, Any]:
    """Run every question. Quality from attempt 1; repeats feed latency and
    hit_stability only. Raises BenchError before any search if the probe or
    span preconditions fail (never a fake 0.0)."""
    positives = [q for q in questions if q.get("expected_recall", 1) == 1]
    probes = [q for q in questions if q.get("expected_recall", 1) == 0]
    if not probes:
        raise BenchError(
            "The blocked-probe/negatives list is empty (no expected_recall=0 "
            "questions). Refusing to run: an empty probe list printed as 0.0 is "
            "a fake pass. Add probes to the golden set first."
        )
    if not any(q.get("answer_spans") for q in positives):
        raise BenchError(
            "No positive question carries answer_spans; nothing would feed "
            "answer_recall@5. Convert the golden set first (see lint)."
        )
    # Resolves every blocked_pattern against the live scanner and raises
    # BEFORE any search when a probe references a deleted rule or could never
    # hit at all.
    probe_patterns = resolve_probe_patterns(questions)

    # Stamped when the searches begin, so every saved run carries its date and
    # a later reader never has to reconstruct it from file mtimes.
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    k_request = max(K_ANSWER, K_DUP)
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    stability: list[float] = []
    errors_total = 0
    attempt_counts = {kind: 0 for kind in (ATTEMPT_OK,) + FAILED_ATTEMPT_KINDS}
    slots_served_total = 0
    underfilled_total = 0
    foreign_dataset_total = 0
    without_dataset_total = 0

    for question in questions:
        gt_shingles = load_ground_truth_shingles(list(question.get("expect_any") or []))
        probe_pattern = probe_patterns.get(str(question.get("id")))
        attempt_rows: list[dict[str, Any]] = []
        attempt_errors: list[str] = []
        for _ in range(max(1, repeats)):
            body, elapsed_ms, error = searcher(question["question"], k_request)
            latencies.append(elapsed_ms)
            kind = classify_attempt(body, error)
            attempt_counts[kind] += 1
            if error or body is None:
                attempt_errors.append(error or "empty")
                errors_total += 1
                attempt_rows.append(
                    score_question(question, [], gt_shingles, probe_pattern)
                    | {
                        "error": error or "empty",
                        "attempt_kind": kind,
                        "slots_requested": k_request,
                        "underfilled": False,
                        "foreign_dataset_hits": None if expect_dataset is None else 0,
                    }
                )
                continue
            results = body.get("results")
            hits = [
                item
                for item in (results if isinstance(results, list) else [])
                if isinstance(item, dict)
            ]
            slots_served_total += len(hits)
            underfilled = kind == ATTEMPT_OK and len(hits) < k_request
            if underfilled:
                underfilled_total += 1
            datasets = [hit_dataset(hit) for hit in hits]
            without_dataset = sum(1 for value in datasets if value is None)
            without_dataset_total += without_dataset
            foreign = (
                None
                if expect_dataset is None
                else sum(
                    1
                    for value in datasets
                    if value is not None and value != expect_dataset
                )
            )
            if foreign:
                foreign_dataset_total += foreign
            attempt_rows.append(
                score_question(question, hits, gt_shingles, probe_pattern)
                | {
                    "attempt_kind": kind,
                    "slots_requested": k_request,
                    "underfilled": underfilled,
                    "foreign_dataset_hits": foreign,
                }
            )
        first = attempt_rows[0]
        first["errors"] = attempt_errors
        rows.append(first)
        if repeats > 1:
            reference = attempt_outcome(first)
            agreement = sum(
                1 for row in attempt_rows if attempt_outcome(row) == reference
            ) / len(attempt_rows)
            stability.append(agreement)
        if not quiet:
            if question.get("expected_recall", 1) == 0:
                state = "PROBE HIT" if first["doc_rank"] is not None else "probe miss (good)"
            elif first["has_spans"]:
                state = (
                    f"answer rank {first['answer_rank']}"
                    if first["answer_rank"]
                    else "ANSWER MISS"
                )
            else:
                state = (
                    f"doc rank {first['doc_rank']} (no spans)"
                    if first["doc_rank"]
                    else "DOC MISS (no spans)"
                )
            print(f"{question.get('id', '?'):>4}  {state:<24}  {question['question'][:54]}")

    summary = summarize(rows, stability)
    summary["latency"] = {
        # Reported in its own block and NEVER gates quality: a fast wrong
        # answer is still wrong.
        "p50_ms": round(percentile(latencies, 0.50), 1),
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "samples": len(latencies),
        # TRANSPORT-level only, kept at its original meaning so a baseline taken
        # before the attempt census still compares. `attempts.failed` is the
        # complete count: it also carries the 200s that were never a served
        # page (truncated, partial, provider error, malformed).
        "errors": errors_total,
    }
    summary["repeats"] = repeats
    attempts_total = sum(attempt_counts.values())
    summary["attempts"] = {
        "total": attempts_total,
        "ok": attempt_counts[ATTEMPT_OK],
        "failed": attempts_total - attempt_counts[ATTEMPT_OK],
        "by_kind": {kind: attempt_counts[kind] for kind in FAILED_ATTEMPT_KINDS},
        "requested_top_k": k_request,
        # What was asked for across every attempt, against what came back. A
        # failed attempt serves zero, so the two only agree on a clean run.
        "slots_requested": attempts_total * k_request,
        "slots_served": slots_served_total,
        "underfilled": underfilled_total,
        # None, not 0, when no expected dataset was declared: nobody said which
        # dataset the token was scoped to, so zero foreign hits would be an
        # unmeasured value printed as a pass. The release gate refuses it.
        #
        # WHAT THIS ATTESTS, stated where the number is produced. It counts
        # hits LABELLED with a dataset other than the expected one. The node
        # stamps that label from the fan-out arm the row was taken from
        # (`kb/server.py:7299` enumerates `(dataset, result)` pairs out of the
        # merged arms), so the label names the arm that served the row, not a
        # property of the document. A non-zero count is therefore real evidence
        # that the page mixed arms; a zero count is NOT evidence that content
        # stayed inside one boundary. Do not quote it as an isolation proof.
        "foreign_dataset_hits": (
            None if expect_dataset is None else foreign_dataset_total
        ),
        # What the count above was measured AGAINST. A number with no referent
        # cannot be compared across two runs.
        "expected_dataset": expect_dataset,
        # BLIND SPOT, stated where the number is produced. A hit carrying no
        # dataset label is unattributable, so it can be neither counted as
        # foreign nor treated as clean. It lands here instead of silently
        # improving the foreign count.
        "hits_without_dataset": without_dataset_total,
    }
    return {"run_at": run_at, "summary": summary, "rows": rows}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n--- quality (answer_recall_at_5 is the headline) ---")
    for key, value in summary["quality"].items():
        print(f"{key:>28}: {value}")
    gap = summary["quality"]["answer_recall_at_5"] - summary["quality"]["raw_page_recall_at_5"]
    print(f"{'duplication_tax':>28}: {round(gap, 4)} (answer - raw_page)")
    print("\n--- duplication ---")
    for key, value in summary["duplication"].items():
        print(f"{key:>28}: {value}")
    window = summary.get("window") or {}
    if window.get("pairs_complete"):
        print("\n--- embedding window (same document, head quote vs tail quote) ---")
        for key, value in window.items():
            print(f"{key:>28}: {value}")
        print(
            f"{'reads as':>28}: a tail miss is an exact, corpus-unique sentence "
            "failing to retrieve the one document containing it"
        )
    ranking = summary.get("ranking") or {}
    if ranking.get("answers_ranked"):
        print("\n--- ranking (separate question from retrieval) ---")
        for key, value in ranking.items():
            print(f"{key:>28}: {value}")
        print(
            f"{'reads as':>28}: share of answers the node ranked BELOW a hit it "
            "itself scored as matching fewer query terms"
        )
    meta = summary.get("metadata_stability") or {}
    if meta.get("chunks_observed"):
        print("\n--- per-request metadata stability ---")
        print(f"{'chunks_observed':>28}: {meta['chunks_observed']}")
        print(
            f"{'unstable_trust_tier':>28}: "
            f"{meta.get('chunks_with_unstable_trust_tier')} "
            "(same chunk id + same content_sha256, different trust_tier)"
        )
    print("\n--- stability ---")
    for key, value in summary["stability"].items():
        print(f"{key:>28}: {value}")
    print("\n--- counts ---")
    for key, value in summary["counts"].items():
        print(f"{key:>28}: {value}")
    print("\n--- latency (informational; never gates quality) ---")
    for key, value in summary["latency"].items():
        print(f"{key:>28}: {value}")


def cmd_run(args: argparse.Namespace) -> int:
    token = os.getenv("CITADEL_MCP_ACCESS_TOKEN") or os.getenv("CITADEL_ACCESS_TOKEN")
    if not token:
        print("CITADEL_MCP_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2
    questions_path = Path(args.questions)
    questions = load_questions(questions_path)
    repo_state = find_repo_state(args.repo_state)
    if args.repo_state and repo_state is None:
        print(f"--repo-state {args.repo_state} does not exist", file=sys.stderr)
        return 2
    # Validated BEFORE any search: an invalid identity makes the whole run
    # unusable as release evidence, and finding that out after the searches is
    # a wasted benchmark against a live node.
    release_context: dict[str, Any] | None = None
    if getattr(args, "release_context", None):
        try:
            release_context = parse_release_context(args.release_context)
        except BenchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        # Required, not optional, once a run claims to be release evidence.
        # The flag defaults to an environment variable, so the quiet failure
        # is nobody setting it on either run: two Nones then compare equal and
        # an unmeasured client image reads as a matched one.
        client_digest = getattr(args, "client_image_digest", None)
        if not client_digest:
            print(
                "ERROR: --client-image-digest is required with "
                "--release-context (or set CITADEL_BENCH_CLIENT_IMAGE_DIGEST); "
                "an unrecorded client image compares as a matched one",
                file=sys.stderr,
            )
            return 2
        if not DIGEST_PATTERN.match(client_digest):
            print(
                "ERROR: --client-image-digest must be a 'sha256:<64 hex>' "
                f"digest, got {client_digest!r}",
                file=sys.stderr,
            )
            return 2

    searcher = make_http_searcher(args.node_url, token, args.timeout)
    try:
        result = execute_benchmark(
            questions,
            searcher,
            repeats=args.repeats,
            expect_dataset=getattr(args, "expect_dataset", None),
        )
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    fingerprint = build_fingerprint(
        args.node_url,
        token,
        args.timeout,
        questions_path,
        repo_state,
        release_context=release_context,
        repeats=args.repeats,
        requested_top_k=(result.get("summary") or {})
        .get("attempts", {})
        .get("requested_top_k"),
        client_image_digest=getattr(args, "client_image_digest", None),
    )
    result["fingerprint"] = fingerprint
    result["node_url"] = args.node_url
    _print_summary(result["summary"])

    print("\n--- corpus fingerprint ---")
    content = fingerprint["content"]
    print(f"{'content_sha256':>28}: {content.get('sha256')}")
    if content.get("sha256") is None:
        print(f"{'reason':>28}: {content.get('reason')}")
    print(f"{'documents_tracked':>28}: {fingerprint['api'].get('documents_tracked')}")
    print(f"{'node_version':>28}: {fingerprint['api'].get('node_version')}")
    print(f"{'harness_git_sha':>28}: {fingerprint['harness_git_sha']}")
    print(f"{'questions_sha256':>28}: {fingerprint['questions_sha256'][:16]}...")
    print(f"{'questions_pin (frozen set)':>28}: {fingerprint.get('questions_pin', '')[:16]}...")

    census = fingerprint.get("census") or {}
    print("\n--- corpus census (/api/corpus) ---")
    if census.get("error"):
        print(f"{'census':>28}: unavailable ({census['error']})")
        print(f"{'reason':>28}: {census.get('reason')}")
    else:
        ratio = census.get("chunk_count_zero_ratio")
        pct = f" ({ratio * 100:.1f}%)" if isinstance(ratio, (int, float)) else ""
        print(f"{'documents_total':>28}: {census.get('documents_total')}")
        print(
            f"{'chunk_count_zero':>28}: {census.get('chunk_count_zero')}{pct} "
            "accepted but never vector-indexed; search cannot reach these"
        )
        if census.get("chunk_count_unmeasured"):
            print(
                f"{'chunk_count_unmeasured':>28}: "
                f"{census.get('chunk_count_unmeasured')} (zero count is a floor)"
            )
        for note in census.get("notes") or []:
            print(f"{'note':>28}: {note}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        comparable, verdicts = compare_fingerprints(
            fingerprint, baseline.get("fingerprint") or {}
        )
        print("\n--- baseline comparison ---")
        for verdict in verdicts:
            print(verdict)
        if comparable:
            base_quality = (baseline.get("summary") or {}).get("quality") or {}
            for key, value in result["summary"]["quality"].items():
                previous = base_quality.get(key)
                delta = (
                    round(value - previous, 4) if isinstance(previous, (int, float)) else "n/a"
                )
                print(f"{key:>28}: {previous} -> {value} (delta {delta})")
        else:
            print("metric deltas withheld: the runs do not measure the same corpus.")

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    attempts = len(questions) * max(1, args.repeats)
    errors = (result.get("summary") or {}).get("latency", {}).get("errors", 0)
    if attempts > 0 and errors >= attempts:
        print(
            f"ERROR: all {attempts} benchmark search attempts failed; "
            "reported quality is transport-only",
            file=sys.stderr,
        )
        return 1
    metadata = (result.get("summary") or {}).get("metadata_stability") or {}
    unstable = metadata.get("chunks_with_unstable_trust_tier", 0)
    if isinstance(unstable, int) and unstable > 0:
        examples = metadata.get("unstable_examples") or []
        print(
            "ERROR: benchmark found "
            f"{unstable} chunk(s) with unstable trust_tier for the same "
            "(result_id, content_sha256); see metadata_stability.unstable_examples "
            f"{json.dumps(examples, sort_keys=True)}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    problems, notes = lint_questions(
        Path(args.questions), Path(args.ground_truth)
    )
    for note in notes:
        print(note)
    if problems:
        print(f"\nLINT FAILED: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    data = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    questions = list(data.get("questions") or [])
    converted = sum(1 for q in questions if q.get("answer_spans"))
    unconverted = sum(
        1
        for q in questions
        if q.get("expected_recall", 1) == 1 and not q.get("answer_spans")
    )
    print(
        f"lint OK: {converted} question(s) with validated answer_spans, "
        f"{unconverted} explicitly unconverted, "
        f"{sum(1 for q in questions if q.get('expected_recall', 1) == 0)} probe(s)"
    )
    return 0


def cmd_ci(args: argparse.Namespace) -> int:
    """Run the tracked, network-free benchmark harness self-check."""
    questions_path = Path(args.questions)
    ground_truth_dir = Path(args.ground_truth)
    problems, notes = lint_questions(questions_path, ground_truth_dir)
    for note in notes:
        print(note)
    if problems:
        print(f"\nCI BENCH FAILED: {len(problems)} fixture problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    try:
        questions = load_questions(questions_path)
        result = execute_benchmark(
            questions, make_ci_searcher(questions, ground_truth_dir), quiet=True
        )
    except BenchError as exc:
        print(f"CI BENCH FAILED: {exc}", file=sys.stderr)
        return 1

    quality = result["summary"]["quality"]
    expected = {
        "answer_recall_at_1": 1.0,
        "answer_recall_at_5": 1.0,
        "doc_recall_at_1": 1.0,
        "doc_recall_at_5": 1.0,
        "negative_hit_rate": 0.0,
    }
    failures = [
        f"{key}={quality.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if quality.get(key) != value
    ]
    if failures:
        print("CI BENCH FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(
        "ci benchmark OK: "
        + " ".join(f"{key}={quality[key]}" for key in expected)
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    run_a = json.loads(Path(args.run_a).read_text(encoding="utf-8"))
    run_b = json.loads(Path(args.run_b).read_text(encoding="utf-8"))
    comparable, verdicts = compare_fingerprints(
        run_b.get("fingerprint") or {}, run_a.get("fingerprint") or {}
    )
    for verdict in verdicts:
        print(verdict)
    if not comparable:
        print("metric deltas withheld: the runs do not measure the same corpus.")
        return 1
    quality_a = (run_a.get("summary") or {}).get("quality") or {}
    quality_b = (run_b.get("summary") or {}).get("quality") or {}
    for key, value in quality_b.items():
        previous = quality_a.get(key)
        delta = round(value - previous, 4) if isinstance(previous, (int, float)) else "n/a"
        print(f"{key:>28}: {previous} -> {value} (delta {delta})")
    return 0


# `compare` is descriptive. It prints deltas after checking fingerprints, but
# it deliberately accepts incomplete run artifacts and does not define a
# release verdict. `enforce` is the bounded v0.5 gate: both artifacts must
# carry complete evidence, and the candidate must not regress the listed
# retrieval or indexing measures.
ENFORCE_METRICS = (
    ("quality", "answer_recall_at_5"),
    ("quality", "doc_recall_at_5"),
    ("quality", "mrr_body"),
    ("window", "tail_recall_given_head_at_5"),
)


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _nonnegative_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _enforce_run_evidence(
    run: dict[str, Any], label: str
) -> tuple[list[str], dict[str, float], int | None]:
    """Validate evidence required before accepting one saved benchmark run."""
    failures: list[str] = []
    metrics: dict[str, float] = {}
    zero_chunks: int | None = None

    fingerprint = run.get("fingerprint")
    if not isinstance(fingerprint, dict):
        failures.append(f"{label} fingerprint is missing")
        fingerprint = {}

    census = fingerprint.get("census")
    if not isinstance(census, dict):
        failures.append(f"{label} census is missing; corpus coverage is unmeasured")
    else:
        if census.get("error"):
            failures.append(
                f"{label} census failed: {census.get('error')}"
            )
        if census.get("truncated") is not False:
            failures.append(f"{label} census is incomplete or truncated")
        unmeasured = census.get("chunk_count_unmeasured")
        if not _nonnegative_count(unmeasured):
            failures.append(
                f"{label} census chunk_count_unmeasured is missing or invalid"
            )
        elif unmeasured:
            failures.append(
                f"{label} census has {unmeasured} unmeasured document(s)"
            )
        documents_total = census.get("documents_total")
        documents_walked = census.get("documents_walked")
        if not _nonnegative_count(documents_total) or not _nonnegative_count(
            documents_walked
        ):
            failures.append(
                f"{label} census total/walked document counts are missing or invalid"
            )
        elif documents_total != documents_walked:
            failures.append(
                f"{label} census is incomplete: walked {documents_walked} of "
                f"{documents_total} document(s)"
            )
        zero_value = census.get("chunk_count_zero")
        if not _nonnegative_count(zero_value):
            failures.append(
                f"{label} census chunk_count_zero is missing or invalid"
            )
        else:
            zero_chunks = zero_value

    ground_truth = fingerprint.get("ground_truth")
    ground_truth_sha = (
        ground_truth.get("sha256") if isinstance(ground_truth, dict) else None
    )
    if not isinstance(ground_truth_sha, str) or not ground_truth_sha:
        failures.append(f"{label} ground-truth fingerprint is missing")

    summary = run.get("summary")
    if not isinstance(summary, dict):
        failures.append(f"{label} summary is missing")
        summary = {}

    latency = summary.get("latency")
    errors = latency.get("errors") if isinstance(latency, dict) else None
    if not _nonnegative_count(errors):
        failures.append(f"{label} search error count is missing or invalid")
    elif errors:
        failures.append(f"{label} contains {errors} search error(s)")

    quality = summary.get("quality")
    if not isinstance(quality, dict):
        failures.append(f"{label} quality metrics are missing")
        quality = {}
    window = summary.get("window")
    if not isinstance(window, dict):
        failures.append(f"{label} window metrics are missing")
        window = {}
    sections = {"quality": quality, "window": window}
    for section, key in ENFORCE_METRICS:
        value = sections[section].get(key)
        if not _finite_number(value) or not 0.0 <= float(value) <= 1.0:
            failures.append(f"{label} {key} is missing or invalid: {value!r}")
        else:
            metrics[key] = float(value)

    negative_hit_rate = quality.get("negative_hit_rate")
    if not _finite_number(negative_hit_rate) or not 0.0 <= float(
        negative_hit_rate
    ) <= 1.0:
        failures.append(
            f"{label} negative_hit_rate is missing or invalid: {negative_hit_rate!r}"
        )
    elif negative_hit_rate:
        failures.append(
            f"{label} contains negative hits: negative_hit_rate={negative_hit_rate}"
        )

    metadata = summary.get("metadata_stability")
    unstable = metadata.get("chunks_with_unstable_trust_tier") if isinstance(
        metadata, dict
    ) else None
    if not _nonnegative_count(unstable):
        failures.append(
            f"{label} unstable trust count is missing or invalid"
        )
    elif unstable:
        failures.append(
            f"{label} contains unstable trust metadata on {unstable} chunk(s)"
        )

    return failures, metrics, zero_chunks


DEFAULT_MAX_P95_REGRESSION_PERCENT = 20.0


def _release_attempt_evidence(
    run: dict[str, Any], label: str
) -> tuple[list[str], dict[str, Any]]:
    """Validate one run's attempt census. Missing evidence is a failure, never
    a zero: an unmeasured count and a clean count look identical."""
    failures: list[str] = []
    summary = run.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    attempts = summary.get("attempts")
    if not isinstance(attempts, dict):
        failures.append(
            f"{label} attempt census is missing; failed attempts and slot "
            "coverage are unmeasured"
        )
        return failures, {}

    by_kind = attempts.get("by_kind")
    if not isinstance(by_kind, dict) or any(
        not _nonnegative_count(by_kind.get(kind)) for kind in FAILED_ATTEMPT_KINDS
    ):
        failures.append(
            f"{label} attempt census by_kind is missing or invalid; failed "
            "attempts are unmeasured"
        )
    else:
        for kind in FAILED_ATTEMPT_KINDS:
            count = by_kind[kind]
            if count:
                failures.append(f"{label} contains {count} {kind} attempt failure(s)")

    # The expected dataset names what the foreign count was measured AGAINST.
    # Without it the count is a number with no referent, and two runs that
    # expected different datasets produce foreign counts that are not one
    # measurement.
    expected_dataset = attempts.get("expected_dataset")
    if not isinstance(expected_dataset, str) or not expected_dataset.strip():
        failures.append(
            f"{label} declares no expected dataset, so its foreign-hit count "
            "has no referent; re-run with --expect-dataset"
        )
        expected_dataset = None

    # SCOPE, decided rather than assumed. This gate is written for the
    # Central-restricted single-scope token the release run uses, where every
    # fan-out arm is the expected dataset and a differently labelled hit is an
    # anomaly. A seat token carrying the multi-arm central reserve produces
    # cross-arm hits BY DESIGN and would fail here: such a token is out of
    # scope for a release run, not a bug in the node.
    foreign = attempts.get("foreign_dataset_hits")
    if foreign is None:
        failures.append(
            f"{label} foreign-dataset hit count is unmeasured; re-run with "
            "--expect-dataset to record which fan-out arm served each hit"
        )
    elif not _nonnegative_count(foreign):
        failures.append(f"{label} foreign-dataset hit count is invalid: {foreign!r}")
    elif foreign:
        failures.append(
            f"{label} served {foreign} hit(s) labelled with a fan-out arm "
            f"other than {expected_dataset!r}"
        )

    # The declared blind spot, now READ. An unlabelled hit cannot be counted as
    # foreign, so a node that stops stamping labels drives the foreign count to
    # a structural zero and the check above passes on entirely unattributable
    # pages. That is the same unmeasured state this gate already refuses when
    # the foreign count itself is absent, so it fails the same way.
    unlabelled = attempts.get("hits_without_dataset")
    if not _nonnegative_count(unlabelled):
        failures.append(
            f"{label} hits_without_dataset is missing or invalid, so the "
            "foreign-hit count cannot be read as attributable"
        )
    elif unlabelled:
        failures.append(
            f"{label} served {unlabelled} unattributable hit(s) carrying no "
            "dataset label; its zero foreign-hit count is unmeasured, not clean"
        )

    underfilled = attempts.get("underfilled")
    if not _nonnegative_count(underfilled):
        failures.append(f"{label} underfilled response count is missing or invalid")
    requested_top_k = attempts.get("requested_top_k")
    if not _nonnegative_count(requested_top_k) or not requested_top_k:
        failures.append(f"{label} requested top-k is missing or invalid")

    return failures, {
        "underfilled": underfilled,
        "requested_top_k": requested_top_k,
        "expected_dataset": expected_dataset,
    }


# Which recorded pass field decides a question, keyed by whether it carries
# answer spans. This is `attempt_outcome`'s rule (the harness's own definition
# of a pass) applied to a SAVED row: gating on answer_pass_at_5 alone left
# every span-less question invisible, and the shipped set carries 22 of them,
# free to swap which document they found while the aggregate stood still.
RELEASE_ROW_CRITERION = {True: "answer_pass_at_5", False: "doc_pass_at_5"}


def _release_row_outcome(row: dict[str, Any]) -> bool | None:
    """The pass this row records under its OWN criterion, or None when the row
    does not say which criterion scores it."""
    has_spans = row.get("has_spans")
    if not isinstance(has_spans, bool):
        return None
    value = row.get(RELEASE_ROW_CRITERION[has_spans])
    return value if isinstance(value, bool) else None


def _release_rows(run: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Rows keyed by question id, or None when the artifact cannot supply
    per-question evidence at all."""
    rows = run.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier:
            return None
        keyed[identifier] = row
    return keyed if len(keyed) == len(rows) else None


def _release_gate_failures(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    max_p95_regression_percent: float,
) -> list[str]:
    """The v0.5 release layer: run identity holds, every attempt completed,
    every page was served in full, no question lost its answer, and the
    candidate is no more than the allowed fraction slower.

    These checks run whether or not the fingerprints compare. A pair that is
    not comparable has already failed; suppressing the reason would leave an
    operator with one line where there are several.
    """
    failures: list[str] = []
    baseline_summary = baseline.get("summary")
    candidate_summary = candidate.get("summary")
    baseline_summary = baseline_summary if isinstance(baseline_summary, dict) else {}
    candidate_summary = candidate_summary if isinstance(candidate_summary, dict) else {}

    baseline_failures, baseline_attempts = _release_attempt_evidence(
        baseline, "baseline"
    )
    candidate_failures, candidate_attempts = _release_attempt_evidence(
        candidate, "candidate"
    )
    failures.extend(baseline_failures)
    failures.extend(candidate_failures)

    baseline_dataset = baseline_attempts.get("expected_dataset")
    candidate_dataset = candidate_attempts.get("expected_dataset")
    if (
        baseline_dataset is not None
        and candidate_dataset is not None
        and baseline_dataset != candidate_dataset
    ):
        failures.append(
            "release runs declare a different expected dataset "
            f"({baseline_dataset!r} -> {candidate_dataset!r}); their "
            "foreign-hit counts are not one measurement"
        )

    # Underfilled successes, with the carve-out the plan names: a corpus
    # holding fewer documents than the requested top-k CANNOT fill a page, and
    # gating there would gate on corpus size rather than on retrieval.
    census = (candidate.get("fingerprint") or {}).get("census")
    documents_total = census.get("documents_total") if isinstance(census, dict) else None
    underfilled = candidate_attempts.get("underfilled")
    requested_top_k = candidate_attempts.get("requested_top_k")
    if (
        _nonnegative_count(underfilled)
        and underfilled
        and _nonnegative_count(requested_top_k)
        and _nonnegative_count(documents_total)
        and documents_total >= requested_top_k
    ):
        failures.append(
            f"candidate returned {underfilled} underfilled successful "
            f"response(s) while the eligible corpus holds {documents_total} "
            f"document(s) for a requested top-k of {requested_top_k}"
        )

    baseline_latency = baseline_summary.get("latency")
    candidate_latency = candidate_summary.get("latency")
    baseline_latency = baseline_latency if isinstance(baseline_latency, dict) else {}
    candidate_latency = candidate_latency if isinstance(candidate_latency, dict) else {}

    baseline_samples = baseline_latency.get("samples")
    candidate_samples = candidate_latency.get("samples")
    if not _nonnegative_count(baseline_samples) or not _nonnegative_count(
        candidate_samples
    ):
        failures.append("release latency sample count is missing or invalid")
    elif not baseline_samples or not candidate_samples:
        failures.append(
            "release latency sample count is zero; a percentile over nothing "
            "is not a measurement"
        )
    elif baseline_samples != candidate_samples:
        failures.append(
            "release latency sample counts differ: "
            f"{baseline_samples} -> {candidate_samples}"
        )

    baseline_repeats = baseline_summary.get("repeats")
    candidate_repeats = candidate_summary.get("repeats")
    if not _nonnegative_count(baseline_repeats) or not _nonnegative_count(
        candidate_repeats
    ):
        failures.append("release repeat count is missing or invalid")
    elif baseline_repeats != candidate_repeats:
        failures.append(
            f"release repeat counts differ: {baseline_repeats} -> {candidate_repeats}"
        )

    baseline_p95 = baseline_latency.get("p95_ms")
    candidate_p95 = candidate_latency.get("p95_ms")
    if (
        not _finite_number(baseline_p95)
        or not _finite_number(candidate_p95)
        or float(baseline_p95) <= 0.0
    ):
        failures.append(
            "release p95 latency is missing or unusable: "
            f"baseline={baseline_p95!r}, candidate={candidate_p95!r}"
        )
    else:
        ceiling = float(baseline_p95) * (1.0 + max_p95_regression_percent / 100.0)
        if float(candidate_p95) > ceiling:
            failures.append(
                f"candidate p95 regressed beyond {max_p95_regression_percent:g}%: "
                f"{float(baseline_p95):g} ms -> {float(candidate_p95):g} ms "
                f"(ceiling {ceiling:g} ms)"
            )

    # Per-question retrieval preservation, each question judged by its OWN
    # criterion. An aggregate can hold while a set of questions stops being
    # answered and an equal set starts, so the headline cannot see this and
    # only the rows can.
    baseline_rows = _release_rows(baseline)
    candidate_rows = _release_rows(candidate)
    if baseline_rows is None or candidate_rows is None:
        failures.append(
            "release row evidence is missing; per-question retrieval "
            "preservation is unmeasured"
        )
    elif set(baseline_rows) != set(candidate_rows):
        failures.append(
            "release row identities differ; per-question retrieval "
            "preservation is unmeasured"
        )
    else:
        unreadable = sorted(
            identifier
            for identifier, row in baseline_rows.items()
            if _release_row_outcome(row) is None
            or _release_row_outcome(candidate_rows[identifier]) is None
        )
        if unreadable:
            failures.append(
                f"{len(unreadable)} release row(s) do not state which pass "
                "criterion scores them, so per-question retrieval preservation "
                "is unmeasured: " + ", ".join(unreadable[:10])
            )
        else:
            lost = sorted(
                identifier
                for identifier, row in baseline_rows.items()
                if _release_row_outcome(row) is True
                and _release_row_outcome(candidate_rows[identifier]) is not True
            )
            if lost:
                failures.append(
                    f"candidate lost {len(lost)} baseline retrieval pass(es): "
                    + ", ".join(lost)
                )

    return failures


def enforce_acceptance(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    release: bool = False,
    max_p95_regression_percent: float = DEFAULT_MAX_P95_REGRESSION_PERCENT,
) -> tuple[bool, list[str], list[str]]:
    """Return ``(comparable, fingerprint_verdicts, gate_failures)``.

    This function only reads the two already-produced JSON objects. It does
    not contact a node, run a search, alter the vault, or write an artifact.
    ``baseline`` is the first positional run and ``candidate`` is the second.
    """
    baseline_fingerprint = baseline.get("fingerprint")
    candidate_fingerprint = candidate.get("fingerprint")
    baseline_fingerprint = (
        baseline_fingerprint if isinstance(baseline_fingerprint, dict) else {}
    )
    candidate_fingerprint = (
        candidate_fingerprint if isinstance(candidate_fingerprint, dict) else {}
    )
    comparable, verdicts = compare_fingerprints(
        candidate_fingerprint, baseline_fingerprint, release=release
    )
    failures: list[str] = []
    if not comparable:
        failures.append("fingerprints are not comparable")

    baseline_failures, baseline_metrics, baseline_zero = _enforce_run_evidence(
        baseline, "baseline"
    )
    candidate_failures, candidate_metrics, candidate_zero = _enforce_run_evidence(
        candidate, "candidate"
    )
    failures.extend(baseline_failures)
    failures.extend(candidate_failures)

    if comparable:
        for _section, key in ENFORCE_METRICS:
            previous = baseline_metrics.get(key)
            current = candidate_metrics.get(key)
            if previous is None or current is None:
                continue
            if current < previous:
                failures.append(
                    f"candidate {key} regressed: {previous:g} -> {current:g}"
                )
        if baseline_zero is not None and candidate_zero is not None:
            if candidate_zero > baseline_zero:
                failures.append(
                    "candidate chunk_count_zero regressed: "
                    f"{baseline_zero} -> {candidate_zero}"
                )
        if candidate_zero != 0:
            failures.append(
                f"candidate chunk_count_zero must be 0, got {candidate_zero}"
            )
        candidate_tail = candidate_metrics.get("tail_recall_given_head_at_5")
        if candidate_tail != 1.0:
            failures.append(
                "candidate tail_recall_given_head_at_5 must be 1.0, "
                f"got {candidate_tail}"
            )

        baseline_gt = (baseline_fingerprint.get("ground_truth") or {}).get("sha256")
        candidate_gt = (candidate_fingerprint.get("ground_truth") or {}).get("sha256")
        if baseline_gt != candidate_gt:
            failures.append(
                "ground-truth fingerprints differ; benchmark scores are not comparable"
            )

    if release:
        failures.extend(
            _release_gate_failures(baseline, candidate, max_p95_regression_percent)
        )

    return comparable, verdicts, failures


def cmd_enforce(args: argparse.Namespace) -> int:
    """Enforce the bounded, read-only v0.5 acceptance gate."""
    try:
        baseline = json.loads(Path(args.run_a).read_text(encoding="utf-8"))
        candidate = json.loads(Path(args.run_b).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ENFORCE FAILED: unable to read run JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        print("ENFORCE FAILED: run JSON root must be an object", file=sys.stderr)
        return 2
    release = bool(getattr(args, "release", False))
    max_p95 = float(
        getattr(args, "max_p95_regression_percent", DEFAULT_MAX_P95_REGRESSION_PERCENT)
    )
    if not math.isfinite(max_p95) or max_p95 < 0.0:
        print(
            "ENFORCE FAILED: --max-p95-regression-percent must be a finite "
            f"percentage >= 0, got {max_p95!r}",
            file=sys.stderr,
        )
        return 2

    _comparable, verdicts, failures = enforce_acceptance(
        baseline, candidate, release=release, max_p95_regression_percent=max_p95
    )
    for verdict in verdicts:
        print(verdict)
    if failures:
        print("ENFORCE FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    mode = "v0.5 release" if release else "v0.5 benchmark"
    print(f"ENFORCE PASSED: {mode} acceptance gate")
    return 0


# --------------------------------------------------------------------------
# Markdown report
# --------------------------------------------------------------------------

# Every metric the report prints MUST state what it matches on, in the same
# table row as the number. The old harness published "recall@5 0.95" that was
# head-of-document header credit; the figure travelled without its definition
# and was quoted as content quality. A definition column makes that class of
# misquote impossible to produce from this tool. Definitions are single-line
# and contain no "|" (they live in a markdown table cell).
METRIC_DEFINITIONS: dict[str, str] = {
    "answer_recall_at_1": (
        "a verbatim span from the expected document's BODY appears in the top 1 "
        "distinct identity; the sync header is stripped before matching and lint "
        "rejects spans found in the document's first line"
    ),
    "answer_recall_at_5": (
        "a verbatim span from the expected document's BODY appears in the top "
        "5 distinct identities; the sync header is stripped before matching "
        "and lint rejects spans found in the document's first line, so "
        "head-of-document credit cannot count"
    ),
    "mrr_body": (
        "mean reciprocal effective rank of the first distinct identity whose "
        "stripped body contains an answer span; 0 when none does"
    ),
    "doc_recall_at_1": (
        "the expected document reached the top 1 distinct identity, matched by "
        "identity or cached-body shingle overlap; no answer span is required"
    ),
    "doc_recall_at_5": (
        "the expected document reached the top 5 distinct identities, matched "
        "by identity (source path plus blob sha, or linear id) or by 12-word "
        "shingle overlap with the cached ground-truth body; no answer span "
        "required, so this is weaker evidence than answer_recall_at_5"
    ),
    "negative_hit_rate": (
        "share of probes that registered a hit: a served chunk's raw text "
        "matched a live ingest-scanner rule, or an out-of-corpus control "
        "identity came back; anything above 0.0 is a finding to investigate, "
        "not a score"
    ),
    "duplicate_blob_rate_at_10": (
        "mean share of the top 10 slots occupied by a repeat of an "
        "already-served (source path, blob sha) identity; 0 means every slot "
        "was a distinct document identity"
    ),
    "header_credit_rate": (
        "share of span questions where the OLD header-anywhere scorer awards "
        "a pass that the body scorer refuses; phantom credit measured so it "
        "can never silently re-enter recall"
    ),
    "head_recall_at_5": (
        "share of window pairs where a verbatim quote taken from the first "
        f"{HEAD_WINDOW_CHARS} characters of a document retrieves that document "
        "into the top 5 distinct identities; the quote is the whole query, and "
        "lint proves it unique across the CACHED ground-truth bodies only, "
        "never across the corpus"
    ),
    "tail_recall_at_5": (
        "the same measurement with the quote taken from at least "
        f"{int(TAIL_MIN_DEPTH * 100)}% through the SAME document, and only "
        f"where at least {TAIL_MIN_NOVEL_TERMS} of the quote's distinctive "
        "terms are absent from that document's head, so a pass cannot be the "
        "head embedding answering the query"
    ),
    "window_penalty": (
        "head_recall_at_5 minus tail_recall_at_5 over the same documents: how "
        "much of a document stops being reachable purely because the text sits "
        "later in it. 0 means position does not matter"
    ),
    "tail_recall_given_head_at_5": (
        "tail_recall_at_5 restricted to documents whose head quote DID "
        "retrieve, so every document counted is one the index demonstrably "
        "holds. This is the figure to quote: a document missing from both "
        "sides may simply never have been indexed, and its tail miss is not "
        "evidence about position"
    ),
    "rank_inversion_rate": (
        "share of answers the node ranked BELOW a hit that the node ITSELF "
        "reports matched a strictly smaller share of the query terms. The "
        "answer slot is header-immune (a body span match, header stripped) but "
        "the comparison reads _citadel.relevance.term_coverage, which the node "
        "computes over a haystack including each hit's own path and sync "
        "header, so a filename can move either side. Read it as the node's "
        "published relevance disagreeing with the node's own ordering, NOT as "
        "a body-only measure of relevance"
    ),
}

# (summary section, metric key, sample-count source). The report refuses any
# key missing from METRIC_DEFINITIONS.
REPORT_METRICS: list[tuple[str, str, str]] = [
    ("quality", "answer_recall_at_1", "spans"),
    ("quality", "answer_recall_at_5", "spans"),
    ("quality", "mrr_body", "spans"),
    ("quality", "doc_recall_at_1", "positives"),
    ("quality", "doc_recall_at_5", "positives"),
    ("quality", "negative_hit_rate", "probes"),
    ("duplication", "duplicate_blob_rate_at_10", "dup_rows"),
    ("quality", "header_credit_rate", "spans"),
    ("window", "head_recall_at_5", "window_pairs"),
    ("window", "tail_recall_at_5", "window_pairs"),
    ("window", "tail_recall_given_head_at_5", "head_reachable"),
    ("window", "window_penalty", "window_pairs"),
    ("ranking", "rank_inversion_rate", "ranked"),
]


def _metric_sample_count(run: dict[str, Any], n_source: str) -> Any:
    counts = (run.get("summary") or {}).get("counts") or {}
    if n_source == "spans":
        return counts.get("questions_with_spans")
    if n_source == "positives":
        return counts.get("questions_positive")
    if n_source == "probes":
        return counts.get("questions_blocked_probe")
    if n_source == "window_pairs":
        return ((run.get("summary") or {}).get("window") or {}).get("pairs_complete")
    if n_source == "head_reachable":
        return ((run.get("summary") or {}).get("window") or {}).get("pairs_head_reachable")
    if n_source == "ranked":
        return ((run.get("summary") or {}).get("ranking") or {}).get("answers_ranked")
    if n_source == "dup_rows":
        rows = run.get("rows")
        if not isinstance(rows, list):
            return None
        return sum(
            1
            for row in rows
            if isinstance(row, dict) and row.get("duplicate_blob_rate_at_10") is not None
        )
    return None


def _census_paragraph(census: dict[str, Any] | None) -> str:
    if not census:
        return (
            "Corpus census unavailable for this run (not recorded); the share "
            "of never-indexed documents was not measured."
        )
    if census.get("error"):
        return (
            f"Corpus census unavailable for this run ({census['error']}): the "
            "share of never-indexed documents was not measured. The census "
            "needs a token with admin or audit:read."
        )
    total = census.get("documents_total")
    if total is None:
        total = census.get("documents_walked")
    zero = census.get("chunk_count_zero")
    ratio = census.get("chunk_count_zero_ratio")
    pct = f" ({ratio * 100:.1f}%)" if isinstance(ratio, (int, float)) else ""
    text = (
        f"Corpus at run time: {total} documents in the durable store; "
        f"{zero}{pct} have chunk_count 0. Those were accepted but never "
        "vector-indexed, so search cannot return them; the recall figures "
        "above are measured over the golden questions and say nothing about "
        "content stranded outside the index."
    )
    unmeasured = census.get("chunk_count_unmeasured") or 0
    if unmeasured:
        text += (
            f" {unmeasured} documents had no chunk_count measurement, so the "
            "zero count is a floor."
        )
    if census.get("truncated"):
        text += (
            f" The census walk was truncated after {census.get('pages')} "
            "pages, so every count here is a floor."
        )
    return text


def build_markdown_report(run: dict[str, Any]) -> str:
    """A README-ready markdown block from one saved run.

    Every number travels with its date, the commit it was measured against,
    its sample count, and a one-line statement of what it matches on, all in
    the same table row, so a row copied out of the table alone stays honest.
    """
    summary = run.get("summary") or {}
    if not summary:
        raise BenchError("run JSON has no summary; nothing to report")
    fingerprint = run.get("fingerprint") or {}
    counts = summary.get("counts") or {}
    latency = summary.get("latency") or {}

    run_at = str(run.get("run_at") or "unknown")
    run_date = run_at.split("T")[0] if "T" in run_at else run_at
    sha = str(fingerprint.get("harness_git_sha") or "unknown")
    short_sha = sha[:12] if sha not in ("", "unknown") else "unknown"
    node_version = (fingerprint.get("api") or {}).get("node_version")

    # WHICH frozen set produced these numbers. Printed here because it is the
    # only surface an operator reads before quoting a delta, and a table of
    # metrics that cannot name its question set is exactly the artifact the pin
    # exists to prevent.
    pin = str(fingerprint.get("questions_pin") or "")
    pin_line = (
        f"Frozen question set `{pin[:16]}` (`questions_pin`)."
        if pin
        else (
            "Frozen question set: NOT RECORDED. This run predates "
            "`questions_pin`, so which questions produced these numbers is not "
            "determinable from the artifact."
        )
    )

    lines = [
        "<!-- Generated by `citadel bench report`. "
        "Regenerate from a run JSON instead of hand-editing numbers. -->",
        "",
        "### Retrieval benchmark",
        "",
        (
            f"Run {run_at}, harness commit `{short_sha}`"
            + (f", node version {node_version}" if node_version else "")
            + f". {counts.get('questions_total')} questions: "
            f"{counts.get('questions_with_spans')} with validated answer "
            f"spans, {counts.get('questions_blocked_probe')} blocked probes, "
            f"repeats {summary.get('repeats')}."
        ),
        "",
        pin_line,
        "",
        "| metric | value | date | commit | n | what it matches on |",
        "|---|---|---|---|---|---|",
    ]
    for section, key, n_source in REPORT_METRICS:
        definition = METRIC_DEFINITIONS.get(key)
        if definition is None:
            raise BenchError(
                f"metric '{key}' has no entry in METRIC_DEFINITIONS. A metric "
                "published without a statement of what it matches on is how "
                "the head-line-credit incident happened; add the definition "
                "before reporting it."
            )
        if section not in summary:
            # A run taken before this metric existed did not measure it. Saying
            # so is accurate; refusing the whole report would make every older
            # baseline unreportable, and printing a bare "n/a" would blur "we
            # measured nothing" into "we measured and got nothing".
            lines.append(
                f"| {key} | not measured in this run | {run_date} | "
                f"`{short_sha}` | n/a | {definition} |"
            )
            continue
        section_values = summary.get(section) or {}
        if key not in section_values:
            lines.append(
                f"| {key} | not measured in this run | {run_date} | "
                f"`{short_sha}` | n/a | {definition} |"
            )
            continue
        value = section_values.get(key)
        value_cell = "n/a (not measured)" if value is None else value
        n = _metric_sample_count(run, n_source)
        n_cell = "n/a" if n is None else n
        lines.append(
            f"| {key} | {value_cell} | {run_date} | `{short_sha}` | {n_cell} "
            f"| {definition} |"
        )

    lines += ["", _census_paragraph(fingerprint.get("census"))]

    p50 = latency.get("p50_ms")
    samples = latency.get("samples")
    if p50 is not None:
        lines += [
            "",
            (
                f"Latency: p50 {p50} ms over {samples} searches, client "
                "round-trip from the machine that ran the bench (network path "
                "included; this is not server-side timing). Latency never "
                "gates quality."
            ),
        ]

    raw_content_sha = (fingerprint.get("content") or {}).get("sha256")
    content_sha = usable_content_sha(raw_content_sha)
    if content_sha is None and raw_content_sha is not None:
        lines += [
            "",
            (
                "WARNING: this run's content fingerprint is the sha256 of an "
                "empty file map (recorded before the fail-closed fix). It "
                "attests nothing, so the run is NOT comparable on content to "
                "any baseline; compare treats it as unavailable."
            ),
        ]
    elif content_sha is None:
        lines += [
            "",
            (
                "WARNING: this run has no content fingerprint and is NOT "
                "comparable on content to any baseline (see "
                "fingerprint.content.reason in the run JSON)."
            ),
        ]
    if run_at == "unknown":
        lines += [
            "",
            (
                "WARNING: this run JSON predates run_at stamping; the date "
                "column is unknown. Re-run with the current harness."
            ),
        ]

    lines += [
        "",
        (
            "Regenerate: `citadel bench run --out "
            "scripts/bench/runs/latest.json` then `citadel bench report "
            "scripts/bench/runs/latest.json --markdown`. Only "
            "scripts/bench/runs/ is gitignored; a run JSON enumerates every "
            "served hit identity and must never be committed. Compare against "
            "a baseline with `compare`; quote deltas only when it prints "
            "COMPARABLE."
        ),
        "",
    ]
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    run = json.loads(Path(args.run_json).read_text(encoding="utf-8"))
    try:
        markdown = build_markdown_report(run)
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    executable = Path(sys.argv[0]).name
    prog = "citadel bench" if executable == "citadel" else sys.argv[0]
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the benchmark against a node")
    run_parser.add_argument(
        "--node-url", default=os.getenv("CITADEL_NODE_URL", DEFAULT_NODE)
    )
    run_parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    run_parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="extra attempts feed latency and hit_stability ONLY; quality uses attempt 1",
    )
    run_parser.add_argument("--timeout", type=float, default=120.0)
    run_parser.add_argument("--out", default=None, help="write JSON results here")
    run_parser.add_argument(
        "--repo-state",
        default=None,
        help="path to repo_content_sync_state.json for the content fingerprint",
    )
    run_parser.add_argument(
        "--baseline",
        default=None,
        help="previous --out JSON; prints CORPUS MOVED / NOT COMPARABLE on drift",
    )
    run_parser.add_argument(
        "--release-context",
        default=None,
        help=(
            "JSON object recording the run identity: runtime_id, "
            "docker_resource_digest, warmup_count, retrieval_profile, "
            "generation_id, model, dimensions, chunk_budget_tokens. Nonsecret "
            "values only; unknown fields are refused"
        ),
    )
    run_parser.add_argument(
        "--client-image-digest",
        default=os.getenv("CITADEL_BENCH_CLIENT_IMAGE_DIGEST"),
        help="digest of the benchmark client image, recorded in the fingerprint",
    )
    run_parser.add_argument(
        "--expect-dataset",
        default=None,
        help=(
            "dataset this run's token is scoped to; without it the "
            "foreign-dataset hit count stays unmeasured rather than zero"
        ),
    )
    run_parser.set_defaults(func=cmd_run)

    lint_parser = sub.add_parser("lint", help="validate every answer span")
    lint_parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    lint_parser.add_argument(
        "--ground-truth",
        default=str(GROUND_TRUTH),
        help="directory of cached source bodies used to validate spans",
    )
    lint_parser.set_defaults(func=cmd_lint)

    ci_parser = sub.add_parser(
        "ci", help="Run the tracked, network-free benchmark harness self-check"
    )
    ci_parser.add_argument(
        "--questions", default=str(REPO_BENCH_DIR / "golden_questions_ci.json")
    )
    ci_parser.add_argument(
        "--ground-truth", default=str(REPO_BENCH_DIR / "ground_truth_ci")
    )
    ci_parser.set_defaults(func=cmd_ci)

    compare_parser = sub.add_parser("compare", help="compare two saved runs")
    compare_parser.add_argument("run_a")
    compare_parser.add_argument("run_b")
    compare_parser.set_defaults(func=cmd_compare)

    enforce_parser = sub.add_parser(
        "enforce",
        help="enforce the bounded v0.5 gate (run_a baseline, run_b candidate)",
    )
    enforce_parser.add_argument("run_a", help="baseline run JSON")
    enforce_parser.add_argument("run_b", help="candidate run JSON")
    enforce_parser.add_argument(
        "--release",
        action="store_true",
        help=(
            "layer the v0.5 release gate over the generic one: identical run "
            "identity, zero failed attempts, zero foreign-dataset hits, no "
            "underfilled page, no lost answer, and a bounded p95"
        ),
    )
    enforce_parser.add_argument(
        "--max-p95-regression-percent",
        type=float,
        default=DEFAULT_MAX_P95_REGRESSION_PERCENT,
        help="release mode only; candidate p95 ceiling as a percentage over baseline",
    )
    enforce_parser.set_defaults(func=cmd_enforce)

    report_parser = sub.add_parser(
        "report", help="emit a README-ready markdown block from a saved run"
    )
    report_parser.add_argument("run_json", help="a run JSON written by run --out")
    report_parser.add_argument(
        "--markdown",
        action="store_true",
        help="markdown is the only output format; the flag makes intent explicit",
    )
    report_parser.add_argument(
        "--out", default=None, help="write the block here instead of stdout"
    )
    report_parser.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
