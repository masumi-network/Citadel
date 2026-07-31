"""Measure redundancy in a result set, and what a per-source cap would fix.

Fetches a deeper candidate pool once per question, then scores recall@5 under
several caps from the SAME responses. Because every variant is computed from
one set of hits, corpus drift cannot explain a difference between them — which
matters on this node, where recall@5 moved 0.40 -> 0.70 in a morning with
nothing deployed.

What this found on 2026-07-31, and why the headline metric is not recall:

    cap    recall@5    MRR
    none      0.750  0.725
    1         0.750  0.725
    2         0.750  0.725

    worst-case single-source share of the uncapped top-5: mean 0.49, max 1.00

A cap changes recall by nothing, because when one source monopolises the slots
it is usually the source that answers the question — it is already at rank 1.
The damage is elsewhere: on average HALF the returned context is repeats of one
file, and at worst all five slots are the same file. An agent asking for five
documents receives about two and a half distinct ones.

So recall@5 = 0.75 and the context is still half wasted. Report unique-source
share beside recall or the number flatters itself.

The cause is upstream of ranking: `format_repo_content_document` stamps
`Retrieved: <timestamp>` into the ingested body, so re-syncing an unchanged
file yields textually distinct content and a fresh document row. That defeats
content-hash dedup at ingest and the text-keyed `search_result_dedup_key` at
query time, and every forced sync multiplies the corpus again.

Usage:
    python scripts/bench/simulate_doc_cap.py --pool 20 --repeats 2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from search_bench import (  # noqa: E402
    DEFAULT_NODE,
    hit_paths,
    load_ground_truth,
    load_questions,
    rank_of_expected,
    search,
    shingles,
)


def source_key(item: dict) -> str:
    """Identify the SOURCE a hit came from, not the row it is stored in.

    `document_id` is the wrong key and measuring with it gave a false all-clear.
    `format_repo_content_document` stamps `Retrieved: <timestamp>` into the
    ingested body, so every re-sync of an unchanged file produces textually
    distinct content and a fresh document row. One README was occupying 8 of 8
    slots under 8 different document_ids, and a document_id-keyed cap called
    that perfectly diverse.

    The first line carries `# owner/repo/path` for repo content, which survives
    re-ingestion. Fall back to document_id only when that is absent.
    """
    text = item.get("text") or ""
    first_line = text.split("\n", 1)[0].strip()
    if first_line.startswith("# ") and "/" in first_line:
        return first_line[2:].strip()
    return str(item.get("document_id") or id(item))


def apply_cap(results: list[dict], cap: int | None, final_k: int) -> list[dict]:
    """Take the top `final_k` hits, allowing at most `cap` per source."""
    if cap is None:
        return results[:final_k]
    seen: dict[str, int] = {}
    kept: list[dict] = []
    for item in results:
        key = source_key(item)
        if seen.get(key, 0) >= cap:
            continue
        seen[key] = seen.get(key, 0) + 1
        kept.append(item)
        if len(kept) >= final_k:
            break
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=os.getenv("CITADEL_NODE_URL", DEFAULT_NODE))
    parser.add_argument("--questions", default=str(HERE / "golden_questions.json"))
    parser.add_argument("--pool", type=int, default=20, help="candidates to fetch")
    parser.add_argument("--final-k", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    token = os.getenv("CITADEL_MCP_ACCESS_TOKEN") or os.getenv("CITADEL_ACCESS_TOKEN")
    if not token:
        print("CITADEL_MCP_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2

    questions = [q for q in load_questions(Path(args.questions)) if q.get("expected_recall") == 1]
    caps: list[int | None] = [None, 1, 2, 3]
    ranks: dict[str, list[int | None]] = {str(cap): [] for cap in caps}
    slot_share: list[float] = []

    for question in questions:
        fingerprints = load_ground_truth(question["expect_any"])
        per_cap_best: dict[str, list[int]] = {str(cap): [] for cap in caps}
        for _ in range(args.repeats):
            body, _elapsed, error = search(
                args.node_url, token, question["question"], args.pool, args.timeout
            )
            if error or body is None:
                continue
            results = list(body.get("results") or [])
            if results:
                # How much of the untruncated top-5 one document already owns.
                head = results[: args.final_k]
                docs = [source_key(r) for r in head]
                slot_share.append(max(docs.count(d) for d in set(docs)) / len(head))
            for cap in caps:
                kept = apply_cap(results, cap, args.final_k)
                sub = {"results": kept}
                rank, _how = rank_of_expected(
                    hit_paths(sub),
                    [(item.get("text") or "") for item in kept],
                    question["expect_any"],
                    fingerprints,
                )
                if rank is not None:
                    per_cap_best[str(cap)].append(rank)
        for cap in caps:
            best = per_cap_best[str(cap)]
            ranks[str(cap)].append(min(best) if best else None)

    print(f"questions={len(questions)} pool={args.pool} final_k={args.final_k} "
          f"repeats={args.repeats}\n")
    print(f"{'cap':>6}  {'recall@5':>9}  {'MRR':>6}")
    summary = {}
    for cap in caps:
        rows = ranks[str(cap)]
        hits = sum(1 for r in rows if r is not None and r <= args.final_k)
        recall = hits / len(rows) if rows else 0.0
        mrr = statistics.fmean([1.0 / r if r else 0.0 for r in rows]) if rows else 0.0
        label = "none" if cap is None else str(cap)
        print(f"{label:>6}  {recall:>9.3f}  {mrr:>6.3f}")
        summary[label] = {"recall_at_5": round(recall, 4), "mrr": round(mrr, 4)}

    if slot_share:
        print(f"\nworst-case single-document share of the uncapped top-{args.final_k}: "
              f"mean {statistics.fmean(slot_share):.2f}, max {max(slot_share):.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
