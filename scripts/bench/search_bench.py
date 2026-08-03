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
    python scripts/bench/search_bench.py run --repeats 3 --out scripts/bench/runs/latest.json
    python scripts/bench/search_bench.py run --baseline previous_run.json
    python scripts/bench/search_bench.py lint
    python scripts/bench/search_bench.py compare run_a.json run_b.json
    python scripts/bench/search_bench.py report scripts/bench/runs/latest.json --markdown

Write run JSONs only under scripts/bench/runs/ (gitignored): a run JSON
enumerates every served hit identity and must never be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
HERE = Path(__file__).resolve().parent
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
    if spans:
        for entry in collapsed:
            bodies = [normalize(body) for body in entry["bodies"]]
            if any(span in body for body in bodies for span in spans):
                answer_rank = entry["effective_rank"]
                break

    raw_page_pass = False
    if spans:
        for hit in hits[:K_ANSWER]:
            body = normalize(split_header_body(hit_text(hit))[1])
            if any(span in body for span in spans):
                raw_page_pass = True
                break

    doc_rank: int | None = None
    for entry in collapsed:
        identity = entry["identity"]
        matched = identity.kind in ("repo", "linear") and identity.source in expected
        if not matched:
            for repo_path in expected:
                if repo_path not in gt_shingles:
                    continue
                for body in entry["bodies"]:
                    if len(shingles(body) & gt_shingles[repo_path]) >= MIN_SHARED_SHINGLES:
                        matched = True
                        break
                if matched:
                    break
        if matched:
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
        "answer_pass_at_5": answer_rank is not None and answer_rank <= K_ANSWER,
        "raw_page_pass_at_5": raw_page_pass,
        "doc_rank": doc_rank,
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
    span_rows = [row for row in positives if row["has_spans"]]
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

    return {
        "quality": {
            "answer_recall_at_5": round(rate(span_rows, "answer_pass_at_5"), 4),
            "raw_page_recall_at_5": round(rate(span_rows, "raw_page_pass_at_5"), 4),
            "doc_recall_at_5": round(rate(positives, "doc_pass_at_5"), 4),
            "mrr_body": round(mrr_body, 4),
            "header_credit_rate": round(header_credit / len(span_rows), 4),
            "negative_hit_rate": round(negative_hits / len(probes), 4),
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
        "stability": {
            "hit_stability": round(statistics.fmean(stability), 4) if stability else None,
        },
        "counts": {
            "questions_total": len(rows),
            "questions_positive": len(positives),
            "questions_with_spans": len(span_rows),
            "questions_excluded_from_answer_recall": len(positives) - len(span_rows),
            "questions_blocked_probe": len(probes),
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


def build_fingerprint(
    node_url: str,
    token: str,
    timeout: float,
    questions_path: Path,
    repo_state: Path | None,
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
    return {
        "api": api_fingerprint(node_url, token, timeout),
        "census": corpus_census(make_corpus_fetcher(node_url, token, timeout)),
        "content": content,
        "harness_git_sha": harness_git_sha(),
        "questions_sha256": hashlib.sha256(questions_path.read_bytes()).hexdigest(),
        "python_version": platform.python_version(),
    }


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


def compare_fingerprints(
    current: dict[str, Any], baseline: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Decide whether two runs are comparable. Returns (comparable, verdicts)."""
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
    if current.get("questions_sha256") != baseline.get("questions_sha256"):
        comparable = False
        verdicts.append("QUESTIONS CHANGED: golden sets differ. NOT COMPARABLE.")
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
            if norm_span in normalize(question.get("question", "")):
                problems.append(f"{label}: appears in the question text")
            for other, body in cached_norm.items():
                if other in expected:
                    continue
                if norm_span in body:
                    problems.append(
                        f"{label}: not unique, also present in cached body {other}"
                    )
    return problems, notes


# --------------------------------------------------------------------------
# Benchmark execution
# --------------------------------------------------------------------------

Searcher = Callable[[str, int], tuple[dict[str, Any] | None, float, str | None]]


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


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["questions"])


def execute_benchmark(
    questions: list[dict[str, Any]],
    searcher: Searcher,
    *,
    repeats: int = 1,
    quiet: bool = False,
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

    for question in questions:
        gt_shingles = load_ground_truth_shingles(list(question.get("expect_any") or []))
        probe_pattern = probe_patterns.get(str(question.get("id")))
        attempt_rows: list[dict[str, Any]] = []
        attempt_errors: list[str] = []
        for _ in range(max(1, repeats)):
            body, elapsed_ms, error = searcher(question["question"], k_request)
            latencies.append(elapsed_ms)
            if error or body is None:
                attempt_errors.append(error or "empty")
                errors_total += 1
                attempt_rows.append(
                    score_question(question, [], gt_shingles, probe_pattern)
                    | {"error": error or "empty"}
                )
                continue
            hits = [item for item in body.get("results") or [] if isinstance(item, dict)]
            attempt_rows.append(score_question(question, hits, gt_shingles, probe_pattern))
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
        "errors": errors_total,
    }
    summary["repeats"] = repeats
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

    searcher = make_http_searcher(args.node_url, token, args.timeout)
    try:
        result = execute_benchmark(questions, searcher, repeats=args.repeats)
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    fingerprint = build_fingerprint(
        args.node_url, token, args.timeout, questions_path, repo_state
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
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    problems, notes = lint_questions(Path(args.questions), GROUND_TRUTH)
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
}

# (summary section, metric key, sample-count source). The report refuses any
# key missing from METRIC_DEFINITIONS.
REPORT_METRICS: list[tuple[str, str, str]] = [
    ("quality", "answer_recall_at_5", "spans"),
    ("quality", "mrr_body", "spans"),
    ("quality", "doc_recall_at_5", "positives"),
    ("quality", "negative_hit_rate", "probes"),
    ("duplication", "duplicate_blob_rate_at_10", "dup_rows"),
    ("quality", "header_credit_rate", "spans"),
]


def _metric_sample_count(run: dict[str, Any], n_source: str) -> Any:
    counts = (run.get("summary") or {}).get("counts") or {}
    if n_source == "spans":
        return counts.get("questions_with_spans")
    if n_source == "positives":
        return counts.get("questions_positive")
    if n_source == "probes":
        return counts.get("questions_blocked_probe")
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

    lines = [
        "<!-- Generated by `scripts/bench/search_bench.py report`. "
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
        section_values = summary.get(section) or {}
        if key not in section_values:
            raise BenchError(
                f"run JSON summary has no {section}.{key}; refusing to print "
                "a table row without its measured value"
            )
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
            "Regenerate: `python scripts/bench/search_bench.py run --out "
            "scripts/bench/runs/latest.json` then `python "
            "scripts/bench/search_bench.py report "
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
    parser = argparse.ArgumentParser(description=__doc__)
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
    run_parser.set_defaults(func=cmd_run)

    lint_parser = sub.add_parser("lint", help="validate every answer span")
    lint_parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    lint_parser.set_defaults(func=cmd_lint)

    compare_parser = sub.add_parser("compare", help="compare two saved runs")
    compare_parser.add_argument("run_a")
    compare_parser.add_argument("run_b")
    compare_parser.set_defaults(func=cmd_compare)

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
