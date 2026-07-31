"""Retrieval benchmark for the Citadel search path.

Runs a golden question set against a Node and reports recall@k, MRR and latency.
Read-only: it issues searches and writes nothing to the vault.

Ground truth is the source path of the document that should answer a question.
Repo-content chunks carry that path on the first line of the chunk body, as
``# masumi-network/<repo>/<path>``, so a hit can be resolved back to its file
without relying on structured metadata (which repo-content hits do not carry).

Usage:
    export CITADEL_MCP_ACCESS_TOKEN=...
    python scripts/bench/search_bench.py --repeats 3 --top-k 5

    # compare against a floor: same questions, no vault
    python scripts/bench/search_bench.py --baseline-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_NODE = "https://citadel-archive-production.up.railway.app"
HERE = Path(__file__).resolve().parent
GROUND_TRUTH = HERE / "ground_truth"
SOURCE_HEADER = re.compile(r"^#\s+([\w.-]+/[\w.-]+/\S+)", re.MULTILINE)
# Linear notes are written as `# Linear SOK-658: <title>`. Normalising them to
# `linear:SOK-658` lets one golden set span both sources with one ground-truth
# field, instead of a second matching path per source type.
LINEAR_HEADER = re.compile(r"^#\s+Linear\s+([A-Z]+-\d+)", re.MULTILINE)
SHINGLE_WORDS = 12
# Near-duplicate documents (two AGENTS.md files from the same template) share
# boilerplate runs, so a single shared shingle produced a false positive during
# calibration. Three independent shared runs did not.
MIN_SHARED_SHINGLES = 3


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["questions"])


def shingles(text: str, size: int = SHINGLE_WORDS) -> set[str]:
    """Word n-grams, normalised, for content-overlap matching.

    Only the first chunk of a document carries its source path, so path matching
    alone scores every later chunk as a miss. A shared 12-word run is specific
    enough that a coincidental match between unrelated documents is negligible.
    """
    words = re.sub(r"\s+", " ", text.lower()).split(" ")
    if len(words) < size:
        return set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def load_ground_truth(expected: list[str]) -> dict[str, set[str]]:
    fingerprints: dict[str, set[str]] = {}
    for repo_path in expected:
        cached = GROUND_TRUTH / repo_path.replace("/", "__")
        if cached.exists():
            fingerprints[repo_path] = shingles(cached.read_text(encoding="utf-8"))
    return fingerprints


def hit_paths(result: dict[str, Any]) -> list[str]:
    """Resolve each hit to the source path it came from, preserving rank order."""
    paths: list[str] = []
    for item in result.get("results") or []:
        text = item.get("text") or ""
        linear = LINEAR_HEADER.search(text)
        if linear:
            paths.append(f"linear:{linear.group(1)}")
            continue
        match = SOURCE_HEADER.search(text)
        paths.append(match.group(1) if match else "")
    return paths


def search(
    node_url: str, token: str, query: str, top_k: int, timeout: float
) -> tuple[dict[str, Any] | None, float, str | None]:
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
    except Exception as exc:  # noqa: BLE001 - a benchmark should record, not raise
        return None, (time.perf_counter() - started) * 1000.0, exc.__class__.__name__


def rank_of_expected(
    paths: list[str],
    texts: list[str],
    expected: list[str],
    fingerprints: dict[str, set[str]],
) -> tuple[int | None, str]:
    """1-indexed rank of the first hit that is one of the expected documents.

    A hit counts by source path (first chunks) or by sharing a 12-word run with
    the cached ground-truth file (later chunks). Returns the rank and how it was
    matched, so a run can be audited rather than trusted.
    """
    for index, (path, text) in enumerate(zip(paths, texts), start=1):
        if path in expected:
            return index, "path"
        hit_shingles = shingles(text)
        if not hit_shingles:
            continue
        for repo_path in expected:
            if repo_path not in fingerprints:
                continue
            if len(hit_shingles & fingerprints[repo_path]) >= MIN_SHARED_SHINGLES:
                return index, "content"
    return None, "none"


def corpus_fingerprint(node_url: str, token: str, timeout: float) -> dict[str, Any]:
    """Record what the corpus looked like for this run.

    Two runs of this benchmark are only comparable if the corpus did not move
    underneath them. On 2026-07-31 recall@5 went 0.40 -> 0.60 between two runs
    with no code deployed: an evolve cognify pass and a partially-completed sync
    had both landed in between, and without this the jump was indistinguishable
    from a real improvement. Always report these alongside the metrics.
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
            fingerprint["version"] = body.get("version")
        else:
            stats = body.get("stats") or {}
            # Reported as-is. These reset on process restart, so they measure
            # uptime as much as content; they are a change signal, not a total.
            fingerprint["indexes_stats"] = stats
    return fingerprint


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=os.getenv("CITADEL_NODE_URL", DEFAULT_NODE))
    parser.add_argument("--questions", default=str(HERE / "golden_questions.json"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=1, help="runs per question")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default=None, help="write JSON results here")
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "apply a search mode (e.g. 'docs') the way the MCP layer does. "
            "POST /search takes no mode; it is applied by shape_search_payload, "
            "so this calls the server's own function on the raw hits."
        ),
    )
    args = parser.parse_args()

    shape = None
    if args.mode:
        sys.path.insert(0, str(HERE.parent.parent))
        from kb.search_format import shape_search_payload as shape  # noqa: PLC0415

    token = os.getenv("CITADEL_MCP_ACCESS_TOKEN") or os.getenv("CITADEL_ACCESS_TOKEN")
    if not token:
        print("CITADEL_MCP_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2

    questions = load_questions(Path(args.questions))
    corpus = corpus_fingerprint(args.node_url, token, args.timeout)
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []

    for question in questions:
        expected = question["expect_any"]
        fingerprints = load_ground_truth(expected)
        ranks: list[int | None] = []
        errors: list[str] = []
        observed: list[str] = []
        matched_by = "none"
        datasets: list[str] = []
        for _ in range(args.repeats):
            body, elapsed_ms, error = search(
                args.node_url, token, question["question"], args.top_k, args.timeout
            )
            latencies.append(elapsed_ms)
            if error or body is None:
                errors.append(error or "empty")
                ranks.append(None)
                continue
            if shape is not None:
                body = shape(body, query=question["question"], mode=args.mode)
            paths = hit_paths(body)
            texts = [(item.get("text") or "") for item in body.get("results") or []]
            datasets = [
                (item.get("_citadel") or {}).get("dataset") or ""
                for item in body.get("results") or []
            ]
            observed = paths
            rank, how = rank_of_expected(paths, texts, expected, fingerprints)
            if rank is not None:
                matched_by = how
            ranks.append(rank)
        best = [rank for rank in ranks if rank is not None]
        rows.append(
            {
                "id": question["id"],
                "category": question["category"],
                "question": question["question"],
                "expected_recall": question.get("expected_recall", 1),
                "expect_any": expected,
                "ground_truth_cached": sorted(fingerprints),
                "ranks": ranks,
                "best_rank": min(best) if best else None,
                "matched_by": matched_by,
                "hit_paths": observed,
                "hit_datasets": datasets,
                # Recall says the right document was on the page. It says nothing
                # about the other four slots. Measured on 2026-07-31, recall@5 was
                # 0.93 while the mean distinct-source ratio was 0.62: 22 of 60
                # queries returned one or two distinct documents, the rest of the
                # page being more chunks of the same file. "What is the Hermes
                # orchestrator actor" returned that one file five times. An agent
                # pays full context for a page that answers once.
                "distinct_sources": len({path for path in observed if path}),
                "hits_returned": len(observed),
                "errors": errors,
            }
        )
        state = f"rank {min(best)} ({matched_by})" if best else "MISS"
        print(f"{question['id']:>4}  {state:<18}  {question['question'][:58]}")

    positives = [row for row in rows if row["expected_recall"] == 1]
    probes = [row for row in rows if row["expected_recall"] == 0]

    def recall_at(rows_: list[dict[str, Any]], k: int) -> float:
        if not rows_:
            return 0.0
        hits = sum(
            1 for row in rows_ if row["best_rank"] is not None and row["best_rank"] <= k
        )
        return hits / len(rows_)

    mrr = (
        statistics.fmean(
            [1.0 / row["best_rank"] if row["best_rank"] else 0.0 for row in positives]
        )
        if positives
        else 0.0
    )

    summary = {
        "node_url": args.node_url,
        "top_k": args.top_k,
        "repeats": args.repeats,
        "questions_total": len(rows),
        "questions_positive": len(positives),
        "questions_blocked_probe": len(probes),
        "recall_at_1": round(recall_at(positives, 1), 4),
        "recall_at_3": round(recall_at(positives, 3), 4),
        "recall_at_5": round(recall_at(positives, 5), 4),
        "mrr": round(mrr, 4),
        # 1.00 means every slot on the page was a different document. Lower means
        # one document is occupying slots the agent is paying context for. Track
        # this NEXT TO recall: a high recall with a low ratio is a page that
        # answers once and repeats itself four times.
        "distinct_source_ratio_mean": round(
            statistics.fmean(
                [
                    row["distinct_sources"] / row["hits_returned"]
                    for row in rows
                    if row["hits_returned"]
                ]
            ),
            4,
        )
        if any(row["hits_returned"] for row in rows)
        else 0.0,
        "queries_with_one_distinct_source": sum(
            1 for row in rows if row["hits_returned"] and row["distinct_sources"] <= 1
        ),
        "blocked_probe_hit_rate": round(recall_at(probes, args.top_k), 4),
        "latency_ms_p50": round(percentile(latencies, 0.50), 1),
        "latency_ms_p95": round(percentile(latencies, 0.95), 1),
        "latency_ms_mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "latency_samples": len(latencies),
        "errors": sum(len(row["errors"]) for row in rows),
    }

    print("\n--- summary ---")
    for key, value in summary.items():
        print(f"{key:>26}: {value}")

    print("\n--- corpus at run time (compare before trusting a delta) ---")
    print(f"{'documents_tracked':>26}: {corpus.get('documents_tracked')}")
    for source in corpus.get("sources") or []:
        print(
            f"{source['type']:>26}: {source['documents']} docs, "
            f"synced {source['last_synced_at']}"
        )

    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "corpus": corpus, "rows": rows}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
