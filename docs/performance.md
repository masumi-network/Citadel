# Performance

Citadel publishes its benchmark numbers, including the unflattering ones. This page records the most recent full measurement and how to reproduce it.


Measured 2026-08-03 against commit `a66cba8` on the production node, from a laptop over the public API, while five agents used the node concurrently. Every figure is a client round trip, not an idle server-side benchmark. Quality numbers come from a golden harness pinned to the same commit: 69 questions, of which 61 expect a specific document (39 of those carry validated answer spans; the 22 Linear questions have no cached bodies to quote from) and 8 are blocked probes. No quality row is measured over all 69, so each states its own question count.

| Metric | Value | What it measures |
|---|---|---|
| Search p50, direct API | 311 ms admin, 472 ms writer | Median round trip for one search: 24 runs per surface, same 12 queries |
| Search p50, MCP and CLI | 504 ms, 627 ms | Same queries through hosted MCP (32 ms added by the extra hop) and the CLI (155 ms added, about 95 ms of it process start) |
| Search p99 | 365 to 905 ms | Slowest end per surface: admin 365, writer 684, MCP 905, CLI 784 |
| `answer_recall@5` | 0.8974 (n=39) | Share of the 39 span-bearing questions where a top-5 distinct hit quotes a verbatim span from the expected document's body after the sync header is stripped. Harness lint rejects any span from a document's first line, so header matches earn no credit; residual header credit is 0.026 |
| `doc_recall@5` | 0.9508 (n=61) | Share of the 61 document-expecting questions where the expected document shows up in the top 5 at all |
| `mrr_body` | 0.7521 (n=39) | Mean reciprocal effective rank of the first distinct identity whose stripped body contains an answer span; 0 when none does. Duplicates collapse before ranking, and the 39 span-bearing questions are the sample |
| Blocked probes | 8 of 8 empty | Queries for content that should never be retrievable, such as secret-shaped strings the ingest scanner blocks; all came back with nothing |
| `duplicate_blob_rate@10` | 0.45 | Share of top-10 slots holding a duplicate of another hit's content |
| Ranking inversion rate | 0.541 | Result pairs where the lower-ranked hit covers more of the query's terms (33 of 61 pairs). 0.5 is random ordering, so ranking currently does slightly worse than random on this measure. A separate 16-query suite still found the correct document in the top 5 for 14 of 16, with mean top-5 term coverage 0.679 |
| Unindexed documents | 892 of 2867 (31.1%) | Corpus documents accepted but never vector-indexed (chunk count 0), which search cannot surface |
| Digest freshness | 0 of 10 queries | Digest-relevant queries where the newest daily digest reached the top 10. Stale digests took 30 of 50 top-5 slots; the most common served age was 33 days |
| Write latency | 0.5 to 0.7 s fast path, 100 s median inline | Time for an ingest to return. Inline graph processing ranged 27.5 to 146.8 s over 5 runs; all 9 write markers were retrievable on the first poll afterwards |
| Concurrency | p50 562 ms at 1, 1226 ms at 4, 2264 ms at 8 | Search latency under burst load. The node runs 8 searches at once and immediately answers 429 beyond that; no 20 s budget timeouts across about 250 searches |

Do not quote the recall figure without its definition. `answer_recall` counts only verbatim body spans, and the harness rejects any span that also appears in the document's first line. An earlier 0.95 figure counted those first-line matches and overstated retrieval quality.

The bad numbers are in the table on purpose. Nearly a third of the corpus is invisible to vector search, ranking does slightly worse than a coin flip on term coverage, the newest daily digest never reached the top 10, and inline writes take minutes.

To reproduce the quality rows: [`scripts/bench/README.md`](../scripts/bench/README.md) documents the harness. `python scripts/bench/search_bench.py run --out scripts/bench/runs/latest.json` runs the 69 frozen questions against a node, `run --baseline scripts/bench/runs/latest.json` reports the delta against an earlier run, and `lint` validates the question set offline. `report scripts/bench/runs/latest.json --markdown` regenerates the table above, and refuses to emit any metric that has no stated definition. Keep run JSONs in `scripts/bench/runs/` (the gitignored location) and never commit one: it enumerates every served hit identity and tracked source name. The latency, write, freshness, and concurrency rows came from one-off probe scripts in the measuring session, so treat them as a dated snapshot rather than something the repo regenerates. Tracking issue: [#122](https://github.com/masumi-network/Citadel/issues/122).

