# Re-index runbook

**Date:** 2026-08-04
**Status:** not started. Nothing below has been executed. This document exists so the run can be scheduled from evidence rather than from optimism.
**Decides nothing about the graph store.** [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md) already decided that, and the [graph store migration runbook](2026-08-04-graph-store-migration-runbook.md) already turns it into nine gates. Its gate 9 is the re-cognify. **This document is gate 9's detail and nothing else.** Gates 1 through 8 stay where they are; do not re-litigate them here.

Gate 9 as written answers four questions and explicitly leaves five open: it says the rebuild is one job with issue #228, that it sequences after #227, that the runtime must be estimated before scheduling, and that the figure "is **not determined** here". This document supplies the estimate, the scope argument, the progress signal, the verification command and the rollback position. Where it and gate 9 disagree, gate 9 wins on sequencing and this document wins on arithmetic, because the arithmetic was measured after gate 9 was written.

Every factual claim is tagged **VERIFIED** (read or run in the session that produced this file, with the path given), **REPORTED** (a named source said so and it was not independently reproduced here), or **INFERRED**. An untagged sentence is an instruction, not a claim.

Two reading environments, and they are not the same:

- Repository paths (`kb/...`, `scripts/...`, `docs/...`) were read on `a7376dd`, the tip of `origin/main` and the commit the node is running. Those citations are reproducible from a checkout of that commit. Note that the migration runbook's repository line numbers were read on branch `docs/substrate-adrs` and have drifted: its `_delete_marker_node` at `kb/service.py:326` is `kb/service.py:379` on `a7376dd`. Check the symbol, not the line.
- Upstream paths (`cognee/...`) were read in the installed distribution, cognee 1.2.2, at `.venv/lib/python3.12/site-packages/cognee`. **VERIFIED** on 2026-08-04 by importing `cognee` from `.venv/bin/python` and reading `cognee_version=1.2.2` off its own startup log. The system `python3` has no cognee on its path, so an upstream citation checked with the wrong interpreter proves nothing.

---

## 1. Scope: every document, not the 892

**Decision: the run covers every document in every dataset. The 892 are a subset that needs no special handling.**

The argument, in the order the evidence arrived.

**The 892 is a symptom count, not the population.** Issue #228 measured 892 of 2,867 documents at `chunk_count == 0` on 2026-08-03 (**REPORTED**, from the issue body). Those rows are unreachable and re-indexing has to include them. But they are not where the retrieval failure lives.

**The metric that names the failure was measured on documents that are already indexed.** The frozen #122 baseline, production, 2026-08-04T19:51:37Z, questions pin `022b6b4f66c73af1`, harness `ea2948a`, stable across three runs and two merges (**REPORTED**, from the #122 comment on issue #228):

```
head_recall_at_5             0.6111   (11 of 18)
tail_recall_at_5             0.0      (0 of 18)
tail_recall_given_head_at_5  0.0      (0 of 11)
rank_inversion_rate          0.1489   (47 ranked answers, worst slot 10)
answer_recall_at_5           0.8974
latency p50 592.2 ms / p95 791.5 ms   (client round-trip, NOT server-side)
```

`tail_recall_given_head_at_5` conditions on documents whose HEAD quote retrieved. Every document it counts is one the index demonstrably holds and returns. Zero of eleven retrieve from a verbatim quote taken at 40% depth or deeper. **None of those eleven is in the 892**, because a document with no chunks cannot retrieve on its head quote either and is excluded before the metric is computed. So a run scoped to the 892 leaves that number at 0 of 11 by construction, and the most direct evidence of the defect goes untouched.

**What each of those numbers does and does not attest**, after an adversarial pass over the harness on 2026-08-05:

- The three window figures **stand**. Every one of the 36 shipped window fixtures was re-derived from the real cached bodies without trusting a single declared field: the quote is present verbatim in all 36, all 18 heads really sit inside the first 2,000 characters, all 18 tails really sit past 0.40 depth, every declared novel term really is a term of its own quote and really is absent from the head, and all 18 pairs are complete and back 18 distinct documents. Zero threshold violations. **VERIFIED** by recomputation on 2026-08-05.
- Seven fixtures declare an offset a few characters off from where the quote now sits, and one (`w05t`, declared 4435, measured 4679) is off by 244 because its upstream document was edited on 2026-08-03. The thresholds were never satisfied by the declared values, so the figures do not move: `w05t`'s measured depth is 0.8330 against a 0.40 threshold. `lint` now measures instead of trusting and reports each drift as a note.
- `rank_inversion_rate 0.1489` **must not be quoted as a body-relevance figure.** It reads `_citadel.relevance.term_coverage`, which the node computes over a haystack including each hit's own path and sync header, and every hit carries a different path. A filename can both hide a real inversion and manufacture one; both directions were executed against the node's own coverage function on 2026-08-05. The number is a correct measurement of what it measures (the node's published relevance signal disagreeing with the node's own ordering) and the earlier claim that a path-header match "can neither manufacture nor hide an inversion" was wrong. It is not a gate for this run; it is context.
- `answer_recall_at_5 0.8974` is scored on the first served identity whose body contains a span, without checking that identity is one the question named. Span uniqueness is proven only across the ~49 cached ground-truth files, never across the corpus. Runs now publish `quality.answers_from_unexpected_documents`; read it before quoting the recall figure.

**The cause is the budget every pre-`a7376dd` document was chunked at, not a pipeline that skipped some rows.** A 13,477-character source file is stored as ONE chunk (`chunk_index: 0`, `text_full_chars: 13790`), and a 34,152-character document ingested at 19:11Z became TWO chunks of 19,529 and 14,616 characters (**REPORTED**, measured in production on 2026-08-04). The embedder window is 512 wordpieces. That is not an accident that befell some documents. It is what the shipped budget of 8191 GPT-4o BPE tokens produces, applied uniformly to everything ingested before `a7376dd`.

**Measured multiplier, this session.** Running cognee's own `chunk_by_paragraph` over 132 files of this repository (`docs/**/*.md`, `*.md`, `kb/*.py`), 2,225,267 characters total, through `.venv/bin/python`:

```
budget= 8191  docs=132  chars=2225267  chunks= 204  chars_per_chunk=10908.2  chunks_per_doc= 1.55
budget=  256  docs=132  chars=2225267  chunks=4494  chars_per_chunk=  495.2  chunks_per_doc=34.05
```

**VERIFIED** on 2026-08-04. The multiplier is 4494 / 204 = **22.0x**. The sweep already committed in `kb/chunk_window.py:76-88` reports 274 chunks at 8191 and 6,064 at 256 over a different 172-document corpus, which is 22.1x (**REPORTED**, that module's own measurement, not re-run here). Two samples of different composition agree to within a rounding error.

Read that as a statement about the corpus: the roughly 1,975 documents that do carry chunks are holding about one twenty-second of the vector rows their text warrants. They are indexed in the sense that a row exists. They are not indexed in the sense that their content is reachable.

**Three populations, one operation.** The #122 comment on #228 splits the corpus into never-indexed (892, this issue), indexed as one or two oversized chunks (#247), and correctly chunked (new writes only, since `a7376dd` set `OBSERVED_CHUNK_BUDGET_TOKENS = 256` at `kb/chunk_window.py:91`). **VERIFIED** that the constant is on `a7376dd`. Populations one and two are the same rows-level operation on different sets of rows, which is exactly ADR-0020's wording at `docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md:32`:

> Plan it as one job with the existing re-index backlog, because re-cognifying documents that were accepted and never indexed is the same operation on a different set of rows.

**How the scope is expressed mechanically.** `run_tasks_data_item` dispatches on the flag (`cognee/modules/pipelines/operations/run_tasks_data_item.py:244-256`): `incremental_loading=True` enters `run_tasks_data_item_incremental` (`:33`), which skips any document whose `pipeline_status[pipeline_name][dataset.id]` is `DATA_ITEM_PROCESSING_COMPLETED` (`:90-99`, set at `:127`); `False` enters `run_tasks_data_item_regular` (`:159`), which has no such check. **VERIFIED**. `CogneePublicClient.cognify` passes `incremental_loading=not force` (`kb/cognee_client.py:1760`), so `force=true` is precisely "ignore the completed flag and reprocess everything". **VERIFIED**. One `force` run per dataset covers both populations with no filtering logic to write.

**What this scope does not buy, and say it out loud.** This runbook has no root cause for the 892. Issue #228 asks for one and it is still open. If a document reached `chunk_count == 0` for a reason other than the incremental skip, `force=true` re-runs it through the same code and it lands at zero again, and the corpus regenerates the population over time. Repair is not diagnosis. Section 4 step 2 is the cheap probe that tells the two apart before the full cost is paid.

---

## 2. What must be true before it starts

### 2.1 The CREATEDB answer is null, and that is a sequencing decision, not a blocker

Gate 1 of the migration runbook is the `CREATEDB` privilege check on the application role, and ADR-0020 puts it "before any other work in this project" (`docs/adr/0020-...:31`). **As of this document the result is null: the check has not been executed.** Not "false", not "unavailable". Not run.

That does not block this job by itself, because the re-index does not create per-dataset databases. It forces a choice about when to pay for it, and the choice is sarthi's:

**Option A, run the re-index now, on the current store.** The benchmark can move this week. The cost, whatever section 3's measurement turns it into, is paid twice, because ADR-0020:32 records that the store move has no export path and is a full rebuild. Every chunk built now is rebuilt again after gate 8.

**Option B, settle gate 1 first, run the re-index once as gate 9.** The cost is paid once. `tail_recall_given_head_at_5` stays at 0 of 11 until the whole migration lands, which is nine gates away.

**Recommendation: B, unless the store move is being deferred past a date sarthi can name.** The cost in section 3 is measured in node-degraded hours, not dollars, and paying that twice is the specific waste ADR-0020's option analysis exists to prevent. A is defensible only if the migration is not happening soon, in which case say so in writing and accept the second pass.

Either way, gate 1 is one `psql` command (migration runbook, gate 1) and answering it costs less than deciding without it.

### 2.2 The chunk budget must be provably in force on the running node

This is the precondition that turns the entire run into wasted money if it is skipped, and it cannot be checked by reading the code alone.

`_ensure_chunk_budget` calls `chunk_window.apply_chunk_budget()` on every cognee operation (`kb/cognee_client.py:402-417`). **VERIFIED.** But `resolve_chunk_budget` has a precedence chain (`kb/chunk_window.py:141-153`): `CITADEL_CHUNK_BUDGET_TOKENS` first, then cognee's own `EMBEDDING_MAX_COMPLETION_TOKENS`, then the 256 constant last. **VERIFIED.** An operator-set value in the deployment environment beats the fix. If either variable is set to 8191 anywhere in the deploy, a full-corpus `force` run rebuilds the entire corpus at the old budget, changes nothing measurable, and costs the full price.

**Check it without reading secrets.** `apply_chunk_budget` logs the number it actually applied (`kb/chunk_window.py:187-194`):

```
Chunk budget set to %s BPE tokens via %s (was %s). This is an observed value, not a bound: ...
```

Grep the deployment's logs for `Chunk budget set to` and read the integer. It must be 256, or a number an operator deliberately chose and can defend. Do not enumerate deployment variables to answer this.

### 2.3 A backup of the vector store exists

Section 6 is the reason. The short version: the re-index overwrites chunk rows in place and nothing in the pipeline snapshots them. Without a backup taken before the first `force` call there is no route back to the current state. This is a hard precondition, not a nice-to-have.

### 2.4 The harness ref is pinned and reachable

The baseline was produced by harness `ea2948a`. **VERIFIED** on 2026-08-04: `git merge-base --is-ancestor ea2948a origin/main` answers NO, and `git branch -a --contains ea2948a` lists only `feat/retrieval-eval-harness`. The metrics that name this defect (`head_recall_at_5`, `tail_recall_at_5`, `tail_recall_given_head_at_5`) do not exist in `scripts/bench/search_bench.py` on `a7376dd`; grep returns nothing. **VERIFIED.**

So the "after" run cannot be produced from main as it stands. Either merge that branch first, or run the harness from a checkout of `ea2948a`. Decide which before the re-index starts, because discovering it afterwards means the run cannot be evaluated against the number it was scheduled to move.

That premise is true only while the harness PR is open. The instant it merges, main carries the metrics and this section is satisfied by doing nothing. Re-check with `git merge-base --is-ancestor` before acting on it rather than assuming it still holds.

### 2.5 The window is announced

Migration runbook rollback condition 4, and it applies here for the same reason. The people who read the census, the mesh graph and the evolve result need to be told in advance what a mid-run reading looks like, or the failure that looks like success gets discovered by someone who trusts it.

### 2.6 Issue #153 interacts with the window

The evolve scheduler resets its interval on every deploy (**REPORTED**, migration runbook §4). A deploy during the run restarts the scheduler and may start an evolve pass that contends with the re-index for the writer lock. Freeze deploys for the window, or accept that the run takes longer for a reason that will not be in any log.

---

## 3. Cost and duration

**Method first, number second, because the number moves and the method does not.**

### 3.1 What the pipeline actually spends per chunk

The default cognify pipeline is five tasks (`cognee/api/v1/cognify/cognify.py:325-350`). **VERIFIED.** The one that costs money is `extract_graph_and_summarize`, which is `asyncio.gather` over two independent LLM passes (`cognee/tasks/graph/extract_graph_and_summarize.py:21-33`):

- `extract_graph_from_data` gathers one `extract_content_graph` per chunk (`cognee/tasks/graph/extract_graph_from_data.py:166-170`), and each is one `LLMGateway.acreate_structured_output` (`cognee/infrastructure/llm/extraction/knowledge_graph/extract_content_graph.py:41`).
- `summarize_text` gathers one `extract_summary` per chunk (`cognee/tasks/summarization/summarize_text.py:68-70`), and each is one `LLMGateway.acreate_structured_output` (`cognee/infrastructure/llm/extraction/extract_summary.py:30`).

All **VERIFIED**. So: **two LLM calls per chunk**, not per document.

System prompt sizes, **VERIFIED** by `wc -c`: `generate_graph_prompt.txt` is 2,445 bytes and is the default (`cognee/infrastructure/llm/config.py:61`, `graph_prompt_path: str = "generate_graph_prompt.txt"`); `summarize_content.txt` is 944 bytes (`extract_summary.py:28`). The model is `LLM_MODEL=openrouter/deepseek/deepseek-v4-flash` (`.env.example:236`, `docs/operations.md:140`). **VERIFIED.**

Embedding is local and free of API cost: `EMBEDDING_PROVIDER=fastembed` (`.env.example:244`). It is not free of wall clock, which is section 3.3.

### 3.2 Token arithmetic

Per chunk at budget 256 BPE tokens, using the measured 495.2 characters per chunk from section 1 and a rounded 4 characters per token:

```
graph call    input ~= 2,445 prompt chars +   495 chunk chars =  2,940 chars ~=   735 tokens
summary call  input ~=   944 prompt chars +   495 chunk chars =  1,439 chars ~=   360 tokens
                                                        per chunk ~= 1,095 input tokens
```

Output tokens are **not determined**. The graph call returns a structured node and edge list whose size varies with the content; the summary returns short text. They add on top of the figure below and are not estimated here, because guessing them would put an invented number next to measured ones.

Corpus scale. The one input this arithmetic needs and does not have is the corpus character total, or equivalently today's chunk-row count. **Not determined.** Cheapest measurement, one query against the node's relational store:

```sql
SELECT count(*) FROM "DocumentChunk_text";
```

Then `chunks_after ~= 22 x chunks_today`, using the multiplier from section 1.

Worked illustration, and it is an illustration. Scaling this repository's sample mean of 16,858 characters per document (2,225,267 / 132) to the corpus's 2,867 documents gives roughly 48.3M characters, which at 495.2 characters per chunk is roughly **97,500 chunks** and therefore **195,000 LLM calls** and roughly **107M input tokens**. The blind spot is stated where the figure is: this repository's markdown and Python is not the vault's document-size distribution, and the number could be off by a factor of two in either direction. It is here to show the order of magnitude, which is hundreds of thousands of calls and not thousands.

Dollars: multiply 107M by the current published per-million input price for `deepseek/deepseek-v4-flash` on OpenRouter, add output. That price is deliberately not written here, because a price copied from memory into a runbook is the kind of number that gets quoted back six months later. Look it up at scheduling time, or read the node's own OpenRouter usage after a timed sample of 100 documents, which gives the real figure including output tokens and needs no price lookup at all.

### 3.3 Duration, and why the node is degraded throughout

The wall clock is not dominated by the LLM calls. It is dominated by two things that run on the web process's own event loop.

**Local embedding blocks the loop.** `FastembedEmbeddingEngine.embed_text` is an `async def` (`cognee/infrastructure/databases/vector/embeddings/FastembedEmbeddingEngine.py:94`) that calls `self.embedding_model.embed(...)` synchronously at line 121, with no `asyncio.to_thread` and no `run_in_executor` anywhere in that method. **VERIFIED.** Every embedding batch therefore holds the loop for the duration of local ONNX inference. This is the same shape as the search-path finding already recorded at `kb/cognee_client.py:430-434`, where an on-loop await "does not merely make one search slow, it blocks every other request for its duration".

**Cognify serializes on the writer lock and runs in the web process.** `CogneePublicClient.cognify` takes `self.writer_lock` around the call (`kb/cognee_client.py:1759-1760`), and `kb/server.py:311` records that evolve Phase 1 "runs HERE, in the web's own loop, not in a subprocess (#88)". **VERIFIED.** So a re-index and any interactive ingest, evolve pass or `/api/cognify/run` are mutually exclusive for the whole window.

The duration formula:

```
window_hours = chunks_after x seconds_per_chunk / 3600
```

`seconds_per_chunk` is **not determined** and must be measured, not assumed. Take it this way, and this is step 3 of section 4: pick one representative document, ingest it into a scratch dataset with the budget at 256, time the whole `force` cognify, divide by the chunk count the census then reports for it. That single measurement is what makes the window statable, and migration runbook rollback condition 5 requires the window to have a stated end.

The shape of the answer, at the illustrative 97,500 chunks:

| seconds per chunk | window |
|---|---|
| 0.5 | ~13.5 hours |
| 2.0 | ~54 hours |
| 5.0 | ~135 hours |

**INFERRED** arithmetic over a **not determined** input. The point of the table is not the numbers, it is that the plausible range spans half a day to most of a week, so this is a scheduled operation with a maintenance posture, not something to start on a Friday afternoon and watch.

### 3.4 The risk that is larger than the cost

The vector store has no approximate index. `PGVectorAdapter` contains no `ivfflat`, no `hnsw` and no `CREATE INDEX`, and its search is `ORDER BY` a `cosine_distance` expression (`cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py:518`, `:527`, `:532-533`). **VERIFIED.** That is an exact scan, so scan cost is linear in row count, and section 1's multiplier says the row count goes up about 22x.

The baseline is p50 592.2 ms client round-trip. What share of that is scan time is **not determined**, and the harness README is explicit that its latency block is client-side and that server-side timing must be read from the platform logs. If the scan is a large share, a 22x row increase makes the node meaningfully slower at exactly the moment its recall improves, and that trade is sarthi's to make knowingly rather than to discover afterwards.

Cheapest measurement that settles it before the run: read server-side request duration for `/search` out of the Railway logs for a handful of requests and compare against the 592.2 ms client figure. The gap is network. What is left is the node, and the scan is inside it. Second cheapest: build a scratch pgvector table at 1x and 22x the row count and time the same query against both.

This is a reason to schedule the re-index alongside a decision about the index, not a reason to skip it. Recall of 0 of 11 is a worse defect than latency.

---

## 4. The steps

Each step states what it produces. A step that cannot produce its output stops the run; it does not get worked around.

### Step 1: record the "before" state, in full

Walk the corpus census and keep the file. `GET /api/corpus` (`kb/server.py:3652`) pages the durable relational store and marks each row with `chunk_count` and `in_graph` (`kb/server.py:3663-3666`). It requires `admin` or `audit:read` (`kb/server.py:3675`). Page size defaults to 200 and caps at 1000 (`kb/server.py:3599-3600`), so the current corpus is three pages. **VERIFIED** by reading all of those.

Keep every row, not the summary. The per-document `chunk_count` diff is the only progress signal that means anything (section 5), and it cannot be reconstructed after the fact.

Also record: `SELECT count(*) FROM "DocumentChunk_text";` for the section 3 arithmetic, and the current dataset list, because the run is one call per dataset.

### Step 2: the cheap diagnosis probe, before any bulk cost

Pick the smallest dataset that contains some of the 892. Run an **incremental** cognify against it, meaning `force: false`, and re-read the census for that dataset only.

- If some of the 892 in that dataset gain chunks, their cause was the incremental skip and the full `force` run repairs them.
- If none of them gain chunks, the incremental skip is not their cause, `force` will very likely reproduce the same zero, and #228 needs its root cause before the corpus-wide run is worth paying for. Say so and stop.

This costs one small dataset's cognify and can save the whole budget.

### Step 3: measure `seconds_per_chunk`

Section 3.3. One representative document, scratch dataset, budget confirmed at 256 by the log line from section 2.2, timed end to end, divided by the chunk count the census reports for it afterwards. Write the number down with the document it came from, because a per-chunk time measured on a 500-character note does not predict a 34,000-character one.

Multiply out and state the window. This is the artifact migration runbook rollback condition 5 requires.

### Step 4: read section 5 before starting, and set the failure check up

The next section is the one that decides whether a broken run is noticed. It is a step, not a footnote.

### Step 5: run it, one dataset at a time

```
POST /api/cognify/run
{"dataset": "<name>", "force": true, "verify": false}
```

Body model at `kb/server.py:987-990`, handler at `kb/server.py:5765-5771`, scope `admin` or `sources:sync` (`kb/server.py:5767`). **VERIFIED.**

Three operational facts about that call:

1. **The CLI can now do this.** `citadel cognify` accepts `--dataset`, `--verify`, and `--force`; `_cognify` passes all three to `cognify_dataset` (`kb/cli.py:853-863`, `kb/cli.py:3180-3194`). **VERIFIED.** `--force` reprocesses the whole selected dataset, so use the same backup, canary, and census controls as the HTTP path. The HTTP endpoint remains available for admin-scoped remote recovery.
2. **The response's `ok` field proves nothing when `verify` is false.** `cognify_dataset` returns `"ok": True if verification is None else bool(verification["ok"])` (`kb/service.py:370`). **VERIFIED.** With `verify: false` it is unconditionally true. Read `graph_before` and `graph_after` in the same payload for a number that is at least a measurement, and read the census for one that is authoritative.
3. **One request will not survive the window.** At the section 3.3 timings a single dataset's force cognify runs for hours, and a client timeout says nothing about whether the write succeeded. Issue #229 recorded that exact confusion and is closed, but the property remains: a timed-out client and a failed write look the same from the client.

   Issue that request from something that outlives your terminal, for example:

   ```bash
   nohup curl -sS -X POST "$CITADEL_NODE_URL/api/cognify/run" \
     -H "Authorization: Bearer $CITADEL_MCP_ACCESS_TOKEN" \
     -H "Content-Type: application/json" \
     --max-time 0 \
     -d '{"dataset": "<name>", "force": true, "verify": false}' \
     > cognify-<name>.json 2>&1 &
   ```

   **NOT DETERMINED: whether the server-side write survives a client disconnect.** `run_cognify` awaits `cognify_dataset` inline with no handler-level timeout (`kb/server.py:5765-5771`), so nothing in this document establishes what the platform's proxy does to an in-flight handler when the client goes away. Treat that as unknown, and never retry on a timeout: a second `force: true` restarts the whole dataset from zero and pays the full price again. Judge the outcome from the census, per section 7.

One dataset at a time, because the writer lock serializes them anyway and a per-dataset boundary is the only resumable checkpoint this path has.

### Step 6: verify, per section 7

---

## 5. The failure that looks like success

**This is the step that decides whether a broken run is noticed. The migration runbook calls its version "the single most important warning in this document" (§1.2). It applies here unchanged.**

The mechanism, quoted from that runbook and re-verified against `a7376dd`:

> `CitadelService.improve` ... calls it at `kb/service.py:216` and then does this, at `kb/service.py:217`:
>
> ```python
> counts = await self._graph_counts()
> if counts["nodes"] == 0 and counts["edges"] == 0:
>     return {
>         "ok": True,
>         "skipped": "empty_graph",
>         "dataset": target_dataset,
>         "reason": "graph is empty; nothing to improve",
>     }
> ```

Same code on `a7376dd` at `kb/service.py:269-275`, with `_graph_counts` at `kb/service.py:295-297`. **VERIFIED.**

> So the visible signature of a broken scoped read is a green evolve pass, a 200 response, an `ok: true` field, and no log line. That is indistinguishable from a healthy pass over a vault that happens to have nothing to improve.

And its required response, which the migration runbook states is not optional:

> **The required design response, and it is not optional:** the scoped read must be able to distinguish "walked zero datasets" from "walked N datasets and all of them were empty". Concretely, the loop returns the number of datasets it enumerated and the number it read successfully, `_graph_counts` carries those numbers up, and the `empty_graph` short-circuit fires only when at least one dataset was read successfully. Enumerating zero datasets, or failing every dataset, raises. A partial failure is reported as a partial, never merged into the empty case.

**Why it belongs in this runbook and not only in that one.** If the re-index runs as gate 9, the per-dataset loop from gate 5 is already deployed, and a re-index is the largest volume of graph writes this node will ever do. A loop that enumerates zero datasets during the rebuild produces: a rebuild that appears to complete, an `improve` that answers `ok: true, skipped: "empty_graph"` on every pass, a `cleanup_legacy_nodes` that reports zero candidates, and a census whose `in_graph` reads null. Every one of those is also what a healthy quiet node looks like mid-migration, which §1.6 of the migration runbook already warns is the expected reading in that window. The two states are indistinguishable at exactly the moment they overlap.

**Make it a check, before step 5.** Before the first `force` call, confirm the enumeration count is nonzero and exposed. Concretely:

1. Call whatever surface the gate 5 contract exposes the dataset enumeration on and confirm it reports a count greater than zero.
2. Call `improve` against a dataset known to have content and confirm it does not answer `skipped: "empty_graph"`.
3. If either answers zero or empty, **stop**. Do not start the rebuild. A rebuild into a read that cannot see its own datasets produces a corpus that looks rebuilt and is not, and the census cannot tell that state apart from the mid-migration reading.

If the re-index runs standalone on the current store, ahead of the migration, step 5's `graph_before` and `graph_after` are the equivalent check: `graph_before` must be nonzero before the run and `graph_after` must exceed it. A `graph_before` of zero on a store that should hold content is the same warning wearing different clothes.

---

## 6. Observing progress while it runs

**"Accepted" is not progress.** Two things say accepted and neither says landed. The HTTP 200 from an ingest means the row reached the relational store. cognee's own `PipelineRunAlreadyCompleted` (`cognee/modules/pipelines/operations/run_tasks_data_item.py:96`) means the document was skipped, and under `force` it should never appear at all, so seeing it is itself a finding that the force flag did not take.

**The signal that a document landed is its chunk row count, read from the vector table.** `corpus_chunk_counts` (`kb/cognee_client.py:1050-1064`) is one grouped `count(*)` over the `DocumentChunk_text` collection keyed on the `document_id` each row carries in its payload. Its docstring is precise about the thing that matters: "None and 0 are different answers: 0 claims cognee accepted a document and indexed no chunk for it, None says this node could not look". **VERIFIED.** `/api/corpus` surfaces it per row as `chunk_count` and refuses to collapse null into 0 (`kb/server.py:3722-3757`). **VERIFIED.**

**The acceptance test is a delta, not a threshold.** `chunk_count > 0` is the wrong test, because a document that was already stored as one oversized chunk still has exactly one chunk if the re-chunk silently did not happen. The test is:

```
chunk_count_after >= 2 x chunk_count_before,  for every document longer than about 1,500 characters
```

with the expected ratio around 22 from section 1. Documents shorter than one chunk's worth of text legitimately stay at 1 and must be excluded from the check rather than counted as failures. This is why step 1 keeps every row: without the before-census there is no delta to compute.

**First signal, the pre-storage chunk-budget validator.** Normal Citadel ingest
replays the pinned Cognee chunker before `cognee.add` and rejects a final chunk
whose reported additive size or exact joined text exceeds the configured BPE
budget (`kb/chunk_window.py:validate_cognee_chunk_budget`). **VERIFIED** by the
focused test that returns `chunk_budget_violation` without calling `remember`.
This protects new writes only. Existing rows still require the census and
controlled repair described below.

**Second signal, the embed-window detector.** `install_embed_window_detector` wraps the fastembed engine's `embed_text` and logs each overflow (`kb/chunk_window.py:390`, log line at `:294-302`):

```
embed window exceeded: %d tokens against a %d window (%.2fx), %d characters, chunk %s.
```

**VERIFIED.** During a healthy re-index at budget 256 this should be nearly silent; `kb/chunk_window.py:76-88` records that only three chunks in a 172-document sweep survive over-window at 256, and all three are the same un-splittable minified line. A flood of these lines means the budget is not in force and the run is rebuilding at the old size. Note the log is capped at 50 lines per process (`_MAX_WARNINGS = 50`, `kb/chunk_window.py:105`) and the counters in `kb.chunk_window.embed_window_report()` stay exact, so treat the log as a sample and the counters as the measurement.

**Third signal, and the weakest.** `graph_before` and `graph_after` in the `/api/cognify/run` response. Useful as a liveness check between datasets. Not proof for any particular document, for the reason `kb/service.py:357-364` already records in a comment: any concurrent write grows the graph, so growth is no evidence that a specific thing landed.

**Cadence.** Re-walk the census between datasets, not continuously. It is three paged reads against the same relational store the run is writing to, and `/api/corpus` sits behind an in-flight limiter (`kb/server.py:3697`), so polling it hard during a window where the event loop is already blocked by local embedding makes the node worse for no extra information.

---

## 7. Verifying it worked

**The success criterion is `tail_recall_given_head_at_5` moving off 0 of 11.** Not `head_recall_at_5`, and not `answer_recall_at_5`. That metric counts only documents the index demonstrably holds and returns, so it isolates the defect this run repairs from every other cause of a miss. If it is still 0 of 11 after the run, the run did not work, whatever else improved.

Re-run the frozen set at the same pin against the same node:

```bash
# Run from a tree that carries the harness. Once this PR merges, main does,
# and nothing extra is needed. Before it merges, work from a separate checkout
# of the branch instead of pulling files into your current index:
#
#   git worktree add /tmp/bench-ea2948a ea2948a && cd /tmp/bench-ea2948a
#
# `git checkout <ref> -- scripts/bench/` also works but writes the index as
# well as the working tree, which leaves a dirty staged state mid-maintenance
# window with no instruction to undo it, and becomes a silent no-op the moment
# this PR is merged.

export CITADEL_MCP_ACCESS_TOKEN=...        # kb:search, plus admin or audit:read for the census block
export CITADEL_NODE_URL=https://citadel-archive-production.up.railway.app

python scripts/bench/search_bench.py run \
  --questions scripts/bench/golden_questions.json \
  --repeats 1 \
  --out scripts/bench/runs/$(date +%F)-after-reindex.json

python scripts/bench/search_bench.py compare \
  scripts/bench/runs/<the-frozen-baseline>.json \
  scripts/bench/runs/$(date +%F)-after-reindex.json
```

Run JSONs go under `scripts/bench/runs/` only. That directory is gitignored and a run JSON enumerates every served hit identity, so it must never be committed (`scripts/bench/README.md`). **VERIFIED** by reading it.

**Confirm the pin before reading any delta.** The run's fingerprint carries `questions_pin` and it must read `022b6b4f66c73af1`. A comparison against a different question set is not a comparison.

Three surfaces show it, and an earlier revision of this document named two of them wrongly. The correction is recorded here rather than silently overwritten, because the wrong version was tagged VERIFIED and would otherwise be quoted again:

- `run` prints it at the end of the run (`cmd_run`). **VERIFIED**: that was true then and is true now.
- `report` prints `Frozen question set \`<pin>\`` directly above the metric table, and prints `NOT RECORDED` when a run predates the pin. **VERIFIED** by `tests/test_search_bench.py::TestReportNamesItsFrozenSet`. The earlier revision cited `:1645` for this; that line is inside `cmd_run`, and `build_markdown_report` contained no reference to the pin at all, so an operator following the old instruction would have found nothing and fallen back to trusting `compare`.
- `compare` gates on `questions_pin` and refuses on a mismatch. **VERIFIED** by `tests/test_search_bench.py::TestCompareGatesOnThePinNotTheFileBytes`. The earlier revision cited `:1400`, which is inside `_lint_freeze_pin` (the `lint` command). `compare_fingerprints` did not read the pin; it gated on `questions_sha256`, the FILE-bytes hash. Two runs of the byte-identical frozen set whose JSON had been reindented between them were declared `QUESTIONS CHANGED: golden sets differ. NOT COMPARABLE.` and the whole delta was withheld, which is the exact case `questions_pin` was introduced to fix.

`compare` also now notes when the two runs used different `ground_truth/` caches. That cache is gitignored and refetched from an unpinned upstream HEAD, and it feeds `doc_recall_at_5` and `header_credit_rate`, so those two can move without the node changing. A note, not a gate.

Compare against:

```
head_recall_at_5             0.6111   (11 of 18)
tail_recall_at_5             0.0      (0 of 18)
tail_recall_given_head_at_5  0.0      (0 of 11)
rank_inversion_rate          0.1489   (47 ranked answers, worst slot 10)
answer_recall_at_5           0.8974
latency p50 592.2 ms / p95 791.5 ms   (client round-trip, NOT server-side)
```

Read section 2's caveats before quoting any row of that block. In particular `rank_inversion_rate` is context, not a gate, and it is not a body-relevance figure.

**Passes when:** `tail_recall_given_head_at_5` is above 0.0, and the census reports the expected chunk-count delta from section 6 for a sampled set of documents. Both, not either. The metric alone could move for an unrelated reason; the census alone proves rows exist without proving they are reachable.

**Read these three alongside it, because any of them can go the wrong way:**

- `answer_recall_at_5`, currently 0.8974, **can fall**. Chunking at 256 tokens splits text that used to sit inside one chunk, and a verbatim quote that straddles a new boundary appears in no single chunk body. That is a real cost of finer chunking and it needs watching, not assuming away.
- Latency p50, currently 592.2 ms client round-trip, is expected to rise for the exact-scan reason in section 3.4. A large rise is a finding about the index, not a failure of the re-index.
- `head_recall_at_5`, currently 0.6111, is #228's own number and should rise as the 892 become reachable. The #122 comment on #228 is explicit that some of the seven current head misses are ordering (#106) rather than a missing index, so treat 0.6111 as the number to beat and not as a measurement of this defect's size.

**Fails when:** `tail_recall_given_head_at_5` is still 0 of 11 and the census shows the expected chunk delta. That combination means the chunks were rebuilt and retrieval still cannot reach them, which is a different defect from the one this run repairs, and it needs a name before anything else is scheduled.

---

## 8. Rollback

**There is no rollback to the pre-run vector state unless a backup was taken first. Take the backup.**

The mechanism, and it is worth reading rather than trusting the summary.

Chunk ids are derived two different ways. `TextChunker` yields most chunks with a position-derived id, `uuid5(NAMESPACE_OID, f"{document_id}-{chunk_index}")` (`cognee/modules/chunking/TextChunker.py:49-50` and `:76`), and yields a single over-budget paragraph with a content-derived id, `chunk_data["chunk_id"]`, which is `uuid5(NAMESPACE_OID, current_chunk)` over the chunk text (`TextChunker.py:29`, from `cognee/tasks/chunks/chunk_by_paragraph.py:45`, `:70`, `:90`). All **VERIFIED**.

The vector write is an upsert keyed on that id. `PGVectorAdapter.create_data_points` says so in its own docstring, "Upsert DataPoints into `collection_name`, merging belongs_to_set on conflict" (`cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py:272-273`), with `id` as the primary key at `:301`. **VERIFIED.** Nothing in the cognify pipeline deletes a document's prior chunks before writing new ones; the only delete path in this codebase is `delete_graph_nodes` (`kb/cognee_client.py`), which the re-index does not call.

Three consequences, all **INFERRED** from those verified facts:

1. **Chunk 0 is overwritten in place.** A document currently stored as one 13,477-character chunk at `chunk_index: 0` has id `uuid5(NAMESPACE_OID, "<document_id>-0")`. Re-chunking produces a new chunk 0 with the same id and a much shorter body. The upsert replaces it. The original long body is gone from the store at that moment, and there is no copy.
2. **Chunks 1 through N are new rows.** They add; they do not replace anything.
3. **Any chunk whose id the new run does not produce is orphaned, not deleted.** It keeps its row, keeps its embedding, and stays searchable. Whether that happens depends on whether a document crosses between the position-derived and content-derived branch between the two budgets, which is **not determined**. Cheapest measurement: re-cognify one document on a scratch dataset and compare the set of chunk ids before and after; any id present before and absent after is an orphan the run will leave behind.

So the honest rollback position: **the run is not reversible by re-running anything.** Reverting the budget to 8191 and cognifying again does not restore the old rows, it writes a third generation on top. The only route back to today's state is a database backup of the vector store taken before the first `force` call.

**What to back up:** the `DocumentChunk_text` collection at minimum, and the graph tables if the run happens after gate 8. A managed snapshot of the whole Postgres instance is simpler than a selective dump and is the recommended form.

**What does not need a rollback.** The relational store is untouched. `corpus_totals`, `corpus_page` and the dataset attribution read SQLAlchemy tables that no part of this run writes (migration runbook §1.6). The document text is intact throughout, which is the property ADR-0010 establishes and the reason a rebuild is possible at all. So the worst realistic outcome is a vector store that is worse than today's and a corpus that can be rebuilt again from the same source, at the same cost, in the same window. That is recoverable and expensive, which is the correct way to describe it.

**If the run happens as gate 9, the two halves roll back differently.** The graph half has a rollback: migration runbook rollback condition 2 requires the provider switch to be reversible by one environment value, and condition 1 requires the old volume to survive until gate 9's exit condition passes. The vector half has none, because the vector store does not move in that migration and the re-chunk overwrites it in place regardless of which graph provider is configured. Do not let the graph half's rollback plan imply the whole job has one.

---

## 9. What this runbook does not cover

- **The root cause of the 892.** Issue #228 asks for one and this document does not supply it. Section 4 step 2 is a probe that distinguishes two candidate causes, not a diagnosis. If the cause is still live, the population regenerates after the run.
- **Whether the index should become approximate.** Section 3.4 predicts a 22x row increase against a verified exact scan and names the measurement that sizes the consequence. Choosing an index is a separate decision with its own verification.
- **Issue #247's ordering component.** `rank_inversion_rate` at 0.1489 is a ranking defect. Finer chunks change what is available to rank; they do not fix how ranking works.
- **Anything in migration runbook gates 1 through 8.** Read that document for those. This one starts where it ends.

## 10. Related records

- [The graph store migration runbook](2026-08-04-graph-store-migration-runbook.md), whose gate 9 this document details, and whose §1.2 section 5 above quotes.
- [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md), which decided the rebuild is one job with the re-index backlog.
- [ADR-0010](../../adr/0010-structured-knowledge-durable-source-of-truth.md), which is why rebuilding is the supported operation rather than a workaround.
- [ADR-0018](../../adr/0018-corpus-totals-are-authoritative-not-uptime.md), which the census in sections 4 and 6 rests on.
- Issue #228 (the 892 never indexed), issue #247 (a verbatim quote from the middle of an indexed document retrieves nothing), issue #227 (closed, the chunk budget), issue #229 (closed, inline ingest blocking), issue #122 (the benchmark harness).
- `kb/chunk_window.py`, which carries the budget sweep and the argument for why no integer is a bound.
- `CONTEXT.md` for every bolded term in the documents above.
