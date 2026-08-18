# Performance

Citadel publishes its benchmark numbers, including the unflattering ones. This page records dated measurements and how to reproduce them. A later snapshot does not erase an earlier one.

## 2026-08-17: CLI 0.4.0 vs 0.5.1 on live production (inside)

**VERIFIED** this session. Deciding metric: successful search wall time, p50, with p95 beside it. Cost and retrieval quality are secondary. Both CLIs hit the same node, `https://citadel.utxo.ag`. The 0.4.0 *node* was not re-run; engine quality is a node property.

Conditions: machine `macs.fritz.box`; warm node (one 0.5.1 search already completed before the loop); query is golden q01, `What is the structure of the Sokosumi monorepo?`; `top_k=5`; n=20 per CLI; window 2026-08-17T18:26:22Z to 18:41:36Z. 0.5.1 used `--timeout 90` (shipped default is 35 s; measured p95 still fits). 0.4.0 has a hardcoded 20 s client timeout and no timeout flag. PyPI `citadel-archive==0.5.1` via pipx; 0.4.0 via isolated venv `/tmp/citadel-040`.

Raw run JSON (gitignored, do not commit): `scripts/bench/runs/2026-08-17-cli-0.4.0-vs-0.5.1.json`.

| Metric | 0.5.1 | 0.4.0 | What it measures |
|---|---|---|---|
| Successful searches | 20 / 20 | 0 / 20 | `ok=true` and not timed out |
| Wall p50 (all runs) | 25049 ms | 20405 ms | Client round trip. 0.4.0 number is time-to-timeout, not a finished search |
| Wall p95 (all runs) | 26488 ms | 20709 ms | Same |
| Wall min / max (0.5.1) | 23822 / 26645 ms | n/a | 0.4.0 min/max 20349 / 20823 ms, all failures |
| Span hit rate (q01) | 0 / 20 | unmeasurable | Expected body quote `This monorepo contains all core services` in any of the top 5 |
| Mean hits | 5.0 | 0 | 0.4.0 returned no JSON payload |
| Client timeout | 90 s this run (shipped 35 s) | 20 s shipped | 0.4.0 stderr every run: `citadel search: The read operation timed out` |

Every successful 0.5.1 page was four `# Git commit snapshot` hits plus `# masumi-network GitHub daily update`. Max `term_coverage` stuck at 0.667.

Verdict in the metric's units: 0.4.0 never returns hits on current production. 0.5.1 returns in 25.0 s p50. 0.4.0 is not faster; it fails about 5 s earlier.

### Cost (secondary, same node)

Railway project `Citadel` (`b1f41db2-93cf-4fa4-88de-173dd87c0f85`), environment production (`4e8b4a2a-8ba6-41be-831b-8555636443b2`). **VERIFIED** 24h `service_metrics` averages on 2026-08-17:

| Service | vCPU | RAM GB | disk GB |
|---|---|---|---|
| Citadel-Archive (`03d39d74-…`) | 0.0317 | 1.0727 | 2.8499 |
| Qdrant (`30827f38-…`) | 0.0031 | 1.1117 | 1.6592 |

Using Railway list prices fetched 2026-07-31 and rechecked 2026-08-14 (`scripts/bench/cost_model.py`): about **$23 / month**. Memory is about $22 of that. `NETWORK_TX_GB` averaged 0.0000, so egress is **not determined**.

Marginal CPU cost of one successful 25 s search, assuming a full vCPU for the whole wait: about **$0.00021** (~$0.21 per 1k). A 0.4.0 attempt still burns ~20 s on the node, then throws the answer away. Monthly infra is not a 0.4.0 vs 0.5.1 delta; both CLIs share this deployment.

The 2026-08-14 cost-model total of about $38 and the 2026-07-31 search p50 of 0.2695 s are **REPORTED** from `scripts/bench/cost_model.py`. They were not re-run today. Do not treat 0.2695 s as today's baseline.

### Blind spots

- One query, not the 105-question frozen harness (`citadel bench run`, about 45 min at current latency).
- Warm only. Cold start is not in this n=20.
- top-k=5 cannot enumerate the corpus.
- `/readyz` census mismatch (#228 / #247) is still OPEN. This run does not measure unindexed documents.

### Outside products: scientific harness (not run)

No outside product was measured on 2026-08-17. Vendor blog numbers stay **REPORTED**.

The tool that already has adapters for the products we listed, under one `add()` / `search()` pipeline, is **OmniMemEval** ([MemTensor/OmniMemEval](https://github.com/MemTensor/OmniMemEval)). `--lib` includes `mem0`, `zep`, `graphiti`, `letta`, `cognee`. Benchmarks: LoCoMo, LongMemEval, BEAM, PersonaMem v2, HaluMem. Citadel is not in that table; a custom adapter would map ingest to `POST /ingest` and search to `POST /search`.

Caveats: MemTensor also ships MemOS, so they score their own product in the same harness. OmniMemEval's datasets are conversation memory, not an org document vault. Scoring uses an LLM-as-judge, which is not the same as Citadel's verbatim body-span scorer.

Closest academic paper protocol for conversation memory: **LongMemEval** (ICLR 2025), [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval), plus [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2).

Closest academic protocol for *document* retrieval (Citadel's actual job, and Elasticsearch): **BEIR**. That is a different task than LoCoMo.

For our own corpus, keep `citadel bench` ([#122](https://github.com/masumi-network/Citadel/issues/122)). It is the only scorer that already knows our span rules and duplicate-identity collapse.

A second multi-vendor runner with Mem0 and Zep adapters is [maximem-ai/memory_and_context_eval_harness](https://github.com/maximem-ai/memory_and_context_eval_harness). Its published leaderboard is owned by Synap. Same adapter-pattern, same conversation-memory mismatch.

## Previous full harness (2026-08-03)

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

One limit is not in the table because it was found after this run. Chunks are built to a token budget far larger than the embedding model's input window, and the model truncates without raising, so an indexed document is embedded only at its head, roughly the first 1,500 characters. 1,754 of 2,869 documents hold content past that point, and those documents carry 97.7% of corpus bytes ([#227](https://github.com/masumi-network/Citadel/issues/227)). Read alongside the 892 documents with no chunks at all ([#228](https://github.com/masumi-network/Citadel/issues/228)), this means no recall figure on this page describes the whole corpus. The numbers are accurate for what the harness asked; they do not measure text the system never embedded.

To reproduce the quality rows: [`scripts/bench/README.md`](../scripts/bench/README.md) documents the harness. `python scripts/bench/search_bench.py run --out scripts/bench/runs/latest.json` runs the 69 frozen questions against a node, `run --baseline scripts/bench/runs/latest.json` reports the delta against an earlier run, and `lint` validates the question set offline. `report scripts/bench/runs/latest.json --markdown` regenerates the table above, and refuses to emit any metric that has no stated definition. Keep run JSONs in `scripts/bench/runs/` (the gitignored location) and never commit one: it enumerates every served hit identity and tracked source name. The latency, write, freshness, and concurrency rows came from one-off probe scripts in the measuring session, so treat them as a dated snapshot rather than something the repo regenerates. Tracking issue: [#122](https://github.com/masumi-network/Citadel/issues/122).

