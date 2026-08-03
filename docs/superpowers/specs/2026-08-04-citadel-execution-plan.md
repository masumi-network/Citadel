# Citadel execution plan: the four stages against the open issues

**Date:** 2026-08-04
**Status:** sequencing only. No decision here is new.
**Source of truth:** [`2026-08-03-citadel-substrate-design.md`](2026-08-03-citadel-substrate-design.md)

Claims are tagged **VERIFIED** (a command run or a file read in the session that wrote this document, with the path or command named), **REPORTED** (a source said so and it was not reproduced here), or **INFERRED** (reasoned from something else). An untagged sentence reads as verified, so guesses are tagged.

---

## 1. How to read this

The design document holds the decisions and the evidence under them: positioning, deployment model, store model, the retrieval interface, the three traces, the component choices, and the scope cut. Its section 11 names four stages and their exit criteria but does not say which open issue goes where or in what order. This document is only that: the ordering, the dependencies between issues, and what counts as done as an observation rather than as a merged pull request. If the two disagree on a decision, the design document wins and this one is wrong. Related decision records: [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md), [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md), [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md), [ADR-0023](../../adr/0023-control-plane-outside-the-application.md). Domain terms are defined in [`CONTEXT.md`](../../../CONTEXT.md) and bolded here on first use.

**Issue count.** The working list handed to this session carried 26 issues under a header of 27. `gh issue list --state open --limit 100` returns 27, and the missing one is **#115** (collapse duplicated `--node-url` and `--json` argparse flags into parent parsers, `good first issue`). **VERIFIED** by running that command on 2026-08-04. All 27 are accounted for in sections 2, 4, 5 and 7.

---

## 2. Stage one, stabilisation, exits v0.5.0

The design document's exit for this stage is "every open issue closed, and a full end-to-end run on both MCP and CLI". Taken literally that never ships, because seven of the 27 are hygiene items with no bearing on whether the substrate works (section 7). The exit that holds is: the fifteen issues below closed, plus one clean end-to-end run on the **MCP** surface and the CLI against the live **Organization Vault**.

Order below is execution order. The reason column is why it sits there, not what the issue says.

### 2.1 First, make the system observable and keep it up

**1. #153, the evolve scheduler never fires on an active development day.**
Position: first, because it is the instrument every later sync-side fix is verified through. The scheduler loop sleeps a full interval before its first pass (`kb/server.py:286-287`, and the docstring above it states the intent: "the first pass waits one full interval so a redeploy never triggers a heavy cycle on boot"). **VERIFIED** by reading `kb/server.py` on 2026-08-04. A deploy inside the interval restarts the clock, so on a day with several merges the pass can fail to run at all.
Unblocks: #46, #117, #149, #151, #90. Every one of them is a sync-stage behaviour whose fix can only be observed after a pass completes.
Done looks like: a pass logged within a bounded time of a deploy, on a day with at least two deploys, plus a surfaced staleness value a reader can check without opening the logs. A merged PR proves nothing here.

**2. #105, sequential search load wedges the node and the health endpoints hang.**
Position: second, because it is a P0 availability defect and because the latency numbers stage two has to publish are meaningless while it stands. `AccessStore.authenticate_token` performs a full store load and a full store save on every successful authentication, purely to stamp `last_used_at` (`kb/access.py:319-338`). **VERIFIED** by reading `kb/access.py` on 2026-08-04. PR #169, `fix/access-store-concurrent-writes`, is open against exactly this ("serialize access-store writes, stop rewriting the store per request"). **VERIFIED** from `gh pr list` on 2026-08-04.
Unblocks: honest measurement of #229, and the stage two latency figures.
Done looks like: `/healthz` and `/readyz` answering under a stated bound while a seat drives sequential search, measured on the node rather than in a test.

**3. #229, inline ingest blocks for minutes and one surface reports a timeout on a write that succeeded.**
Position: after #105, so whatever blocking remains is attributable to the ingest path instead of to per-request store writes. A non-deferred ingest schedules a background cognify that serialises on the writer lock inside the same process (`kb/cognee_client.py:526-540`). **VERIFIED** by reading `kb/cognee_client.py` on 2026-08-04.
Done looks like: an ingest through **MCP** that returns a result the caller can act on, and a caller that is never told a write failed when it landed. The second half is the part that matters: a false timeout teaches an agent to retry a write that already succeeded.

**Do #105 and #229 share a cause? Not determined.** They are the same class of problem, single-process event-loop occupancy, which [ADR-0015](../../adr/0015-one-process-owns-the-graph.md) forces by design. They have two distinct measured contributors: a synchronous store rewrite on the request path (#105, VERIFIED above) and a long-running graph write scheduled into the same loop (#229, VERIFIED above). A third contributor has already landed: PR #231, "run repo-content GitHub calls off the event loop", merged into `main` on 2026-08-03 (**VERIFIED** by `gh pr view 231` on 2026-08-04), so any loop-occupancy measurement taken before that merge no longer describes the deployed path. Whether one root cause explains both is **not determined**. Cheapest measurement that would settle it: land PR #169 alone, then re-run the #229 reproduction and read asyncio slow-callback logging on both paths. If the blocking frames are the same, they are one defect; if #229 survives a fixed #105 with different frames, they are two.

### 2.2 Then fix retrieval at the bottom, before re-indexing anything

**4. #227, chunk size exceeds the embedder's input window.**
Position: strictly before #228. Re-indexing 892 documents at a chunk budget the embedder cannot consume produces 892 documents that are indexed and still not retrievable, and the work has to be done a second time at the corrected budget. **INFERRED** from the two issue titles plus the deployed embedder setting: `.env.example:238-240` sets `EMBEDDING_PROVIDER=fastembed`, `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`, `EMBEDDING_DIMENSIONS=384` (**VERIFIED** by reading `.env.example` on 2026-08-04). The chunk budget itself is not set anywhere in `kb/`: a grep for `chunk_size`, `max_chunk_tokens` and `CHUNK` across `kb/` returns only enrichment character limits (`kb/llm_enrichment.py:36`, `DEFAULT_MAX_CHUNK_CHARS = 4000`) and search-format prose (**VERIFIED**, same grep). So the effective budget comes from the retrieval engine's default, which makes this issue and the cognee upgrade in section 3 the same conversation.
Unblocks: #228, and any honest before/after on retrieval quality.
Done looks like: a measured chunk budget expressed in the embedder's own token units, and a sampled document whose tail text is retrievable by a token that appears only in that tail.

**5. #228, 892 of 2867 documents were accepted but never vector-indexed.**
Position: after #227, and planned as one job with the re-cognify [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md) requires. ADR-0020 says so directly in its consequences: the graph move "is a full re-cognify, not a migration ... Plan it as one job with the existing re-index backlog, because re-cognifying documents that were accepted and never indexed is the same operation on a different set of rows." **VERIFIED** by reading `docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md` on 2026-08-04.
Done looks like: a census run that returns zero documents with a chunk count of zero, and a spot check where a document previously in that set answers a query naming a token unique to it. Note the blind spot next to the figure: similarity search cannot enumerate a corpus, so the census endpoint is the instrument here, not a search.

**6. #118, `CogneeGateway` does not cover `writer_lock` or `node_dataset_map`, so the engine is not swappable.**
Position: before the graph store move, not after. ADR-0020 states that under per-dataset context the search result shape gains per-dataset entries and that "with that interface in place the new result shape stops at the adapter, and these callers migrate once rather than twice" (**VERIFIED**, same file). Doing the move first means migrating `kb/search_format.py`, the `/search` envelope and the seed-query enumeration in `kb/promotion.py` twice.
Unblocks: #106, #125, and the whole of [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md).
Done looks like: a contract test suite the adapter passes, and no new `import cognee` outside the adapter, checkable by a grep that a CI job runs.

**7. #106, results ordered by dataset group so an exact unique-token match lands at #2.**
Position: with #118, because ADR-0021 puts ranking in Citadel's layer on the evidence that no upstream version emits a usable score. Fixing the ordering above a boundary that is about to move means fixing it twice.
Done looks like: a query whose only exact match is one document returns that document first, recorded as a frozen fixture in #122 so it cannot regress silently.

**Take the #122 baseline here, before #227, #228 and #106 change anything.** The harness exits in stage two, but its fixtures have to be frozen while the current behaviour is still running, or there is no before to compare the after to. `tasks.md` already says this: P0-2 is marked to be done first, on the grounds that it baselines cognee and unlocks the coexistence bake-off (`tasks.md:117`, **VERIFIED** by reading it on 2026-08-04; paraphrased rather than quoted, because the source line uses a dash this document does not).

### 2.3 Then the dependency and surface correctness work

**8. #150, cognee pin drift.**
`pyproject.toml:33` declares `cognee[fastembed,postgres-binary]>=1.2.2,<2.0.0` while `requirements.txt:6` declares `>=1.2.2,<1.3.0`. **VERIFIED** by reading both files on 2026-08-04. PR #205, `fix/pin-drift-and-unused-force`, "align cognee pin with requirements.txt; make linear-sync force real", covers this and #90 in one branch (**VERIFIED** from `gh pr list` on 2026-08-04).
Position: before the cognee upgrade in section 3, so there is one pin to move rather than two that disagree. Two declared ranges mean the deploy path and the packaged distribution can install different versions.
Done looks like: both files carrying the same range, and a check that fails CI when they diverge again.

**9. #90, `linear-sync` accepts `body.force` and never uses it, and the evolve stage comment claims an incremental sync that does not exist.**
Position: rides with #150 on PR #205. It is here rather than in section 7 because a parameter that is accepted and ignored is a lie an operator acts on.
Done looks like: a forced resync that observably differs from an unforced one, and a stage comment that describes what the stage does.

**10. #116, seven copies of the token-sending HTTP helper have drifted and the #39 timeout fix was never back-ported.**
Position: before the end-to-end CLI run, which is half of this stage's exit. A CLI path still carrying the un-backported timeout will fail that run for a reason nobody is trying to test.
Done looks like: one helper, and the timeout behaviour demonstrated on a path that previously lacked it.

**11. #117, `LinearSyncer` skips the change-detection and secret-scan phases its sibling syncers run.**
Position: after #153 (so a pass completes and the fix is observable) and before #46 (which reads what this syncer writes). The secret-scan phase is part of the ingest gate the product is sold on, so a connector that bypasses it is a governance defect, not a tidy-up.
Done looks like: a planted test credential in a Linear issue body blocked at ingest, and a second sync of an unchanged issue set doing measurably less work than the first. A green scan with nothing planted proves only that nothing was there.

**12. #46, Linear seat mirror stays empty although `linear_sync` succeeds.**
Position: after #153 and #117. `tasks.md` records that #46 "cannot be verified until #153 is fixed, because the cycle rarely completes" (**VERIFIED** by reading `tasks.md`). Root cause of the empty mirror is **not determined** in this session; the cheapest measurement is to run one completed cycle after #153 lands and read what the mapping step received as input.
Done looks like: a non-zero mirror count with the mirrored items visible in the mapped **Seat**'s **Node** and nowhere else.

**13. #149, `organization_digest` LLM call fails with HTTP 400 on every cycle.**
Position: near the end, cheap and self-contained. A 400 is a malformed request, so retry logic cannot help and no amount of scheduler work changes it.
Done looks like: one digest produced end to end from a real pass, and the request shape pinned by a test.

**14. #151, no delivery gateway configured, so the org digest posts nowhere and `/contact` reports `delivered: true` regardless.**
Position: with #149, same surface. The reported-success half is the defect that matters; a missing gateway is a configuration state, while a success signal that does not track delivery is a bookkeeping figure being read as an outcome. The issue itself asks for exactly that: make "no gateway configured" an explicit skip reason rather than an empty success (**VERIFIED** by reading the issue body on 2026-08-04). PR #232, "stop publishing counters and flags that misreport what they measure", is open against the same class on the **Knowledge Mesh** surface. It does not close this issue and is not a fix for it (**VERIFIED**, `gh pr view 232 --json closingIssuesReferences` returns an empty list on 2026-08-04); it is named here so whoever takes #151 reads that branch first rather than re-deriving the same argument.
Done looks like: an explicit "no gateway configured" skip reason in the response and in **Vault Activity**, and `delivered` reflecting an actual delivery.

**15. #128, cut the 0.5.0 release for the CHANGELOG `[Unreleased]` block.**
Position: last in the stage by definition. `CHANGELOG.md:7` carries the `[Unreleased]` block and the last tagged section is `[0.4.0]` dated 2026-07-22 (**VERIFIED** by reading `CHANGELOG.md` on 2026-08-04).
Done looks like: a tag, a published distribution installed from a clean environment, and the end-to-end run from this stage's exit repeated against that installed build rather than against the working tree.

### 2.4 Stage one prerequisite chains, stated once

- #227 is upstream of #228. Re-indexing at a wrong chunk budget means indexing twice. **INFERRED**, reasoning in item 4.
- #118 is upstream of the graph store move and of #106. **VERIFIED** from ADR-0020's consequences.
- #153 is upstream of #46, #117, #149, #151 and #90 as far as verification goes. Their fixes can be written at any time; they cannot be observed until a pass runs.
- #150 is upstream of the cognee upgrade. One pin, then move it.
- #105 is upstream of any published latency figure, and probably upstream of #229 (**not determined**, see 2.1).
- ADR-0020's re-cognify and #228's re-index are the same job. Do not schedule them separately.

---

## 3. The cognee upgrade

**Where the pin is.** `requirements.txt:6` pins `cognee[fastembed,postgres-binary]>=1.2.2,<1.3.0`, and the comment block above it (`requirements.txt:1-5`) records why: [ADR-0009](../../adr/0009-mesh-read-isolation-presence-vs-content.md) dataset attribution reads cognee private internals (`cognee.modules.data.models`, `get_default_user`) that a 1.3.x could move; `assert_cognee_dataset_api()` catches a bump as a boot self-check and a test; the pin stops the deploy platform pulling one unattended on the next build. **VERIFIED** by reading `requirements.txt` on 2026-08-04.

**What the self-check actually does about a bump.** `assert_cognee_dataset_api()` is defined at `kb/cognee_client.py:97` and imports the private symbols directly, then also asserts that `PipelineRunInfo` and `PipelineRunCompleted` still declare a `data_ingestion_info` field, raising `RuntimeError` if not (`kb/cognee_client.py:114-131`). At boot it is called inside a `try` that logs at ERROR level and does not stop startup (`kb/server.py:457-464`). The gate that actually stops a bump is the test, `tests/test_cognee_client.py:751`. **VERIFIED** by reading all three files on 2026-08-04. The practical consequence for the upgrade: put the bump on a branch, let CI fail, and treat the failure list as the work item. Do not expect a bad bump to be caught by a boot log nobody is watching.

**Where upstream is, and what the move buys.** The following three claims are **REPORTED**. They come from the design document (§5, §11) and ADR-0021, which record a byte-diff of cognee 1.4.1 against the installed 1.2.2 performed on 2026-08-03. They were **not** re-verified in this session, because the version they are about is not here to read: 1.4.1 is not vendored in this repository and is not installed anywhere on this machine. What is installed is 1.2.2, in the project virtual environment at `.venv/lib/python3.12/site-packages`, which imports (**VERIFIED** on 2026-08-04 by listing `cognee-1.2.2.dist-info` and importing `cognee` from that interpreter; the system `python3` has no cognee on its path at all, which is a path fact and not a statement about availability). The migration runbook shipping alongside this document reads that same 1.2.2 install, so the two documents are reading the same thing. Cheapest measurement that would settle any of the three claims below: `pip download cognee==1.4.1 --no-deps`, unpack, and diff the named files against that install.

- **What the upgrade fixes:** the wordpiece/BPE tokenizer mismatch that mis-sized every chunk. This is the reason #227 and the upgrade are one piece of work rather than two.
- **What the upgrade does not fix:** ranking. `PGVectorAdapter.retrieve()` still returns `score=0` for every row in 1.4.1 (the hardcoded zero moves to line 464 of the same file), `chunks_retriever.py` is byte-identical between the versions, and `SearchResultPayload` carries no score field of any kind. The community pull request that would have exposed a real score and a `max_distance` cutoff, `topoteretes/cognee#2945`, was closed unmerged on 2026-07-07. Upgrading does not move this, and upgrading twice does not move it either. That is the evidence ADR-0021 rests on.
- **What the upgrade does not fix, second item:** the private-internals coupling. The symbols in the `requirements.txt` comment are still private after the bump, so the pin's reason survives the upgrade. What changes it is [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md)'s boundary, after which the question becomes whether the adapter passes its contract tests rather than whether 45 import sites still resolve.
- **The one breaking change:** `get_max_chunk_tokens` went from synchronous to asynchronous. The release notes deny that a breaking change occurred. **REPORTED** from the design document §11, which tags it VERIFIED by diff on 2026-08-03.

**Order within stage one:** #150 first (one pin), then the bump on a branch, then #227's re-measurement in the embedder's token units, then #228's re-index. Bumping before #227 wastes the re-index; re-indexing before the bump wastes it twice.

---

## 4. Stage two, the validation gate

**Exit (from the design document §11):** published numbers for search latency, ingest throughput, and cost of operating, generated by a named harness, measured against the Masumi organisation's live vault.

**#122, retrieval eval harness (`citadel bench`) with frozen question fixtures. P0, roadmap.** This is the instrument. Validation cannot be declared without it for a reason that has already bitten this project: a number produced by an ad-hoc query is not reproducible, and a recall figure computed the obvious way can measure the wrong thing entirely. A previously published recall@5 of 0.95 was later found to be dominated by matches on a structural header line rather than on document content, which means the figure was real and measured the header. A named harness with frozen fixtures fixes the question set, so a later run answers the same questions and a regression is visible as a delta rather than as a different measurement.

Numbers that must be published for this stage to exit:

1. **Retrieval quality:** top-1 and top-5 against the frozen fixture set, with the negative fixtures scored separately, and a stated note on what the matching method cannot see.
2. **Search latency:** a distribution, not a mean, per surface (**MCP**, CLI, HTTP), taken after #105 is closed.
3. **Ingest throughput:** documents accepted per unit time and documents *retrievable* per unit time. The gap between those two is what #228 was.
4. **Cost of operating:** measured unit cost of one organisation instance per month at the reference workload, which [ADR-0023](../../adr/0023-control-plane-outside-the-application.md) requires before the hosting substrate can be chosen, with inference topology priced inside the same measurement.

Each figure carries its method and its blind spot in the same place it is published, not in a footnote.

**#123, vault lint (`citadel lint`) for orphans, dangling refs and near-duplicate pages. P1, roadmap.** Supporting instrument. The harness measures whether the right thing comes back for a question; the lint measures whether the corpus underneath it is coherent. Near-duplicate detection is what stops a fixture set from silently measuring the same document twice, and orphan and dangling-ref detection is the safety net `tasks.md` assigns it for bad identity resolution once **Structured Knowledge** pages are written in place (**VERIFIED** by reading `tasks.md`, P0-4). It is a stage two item because it has to exist before stage three writes durable pages, and because its output is read as part of interpreting the harness numbers.

**v1.0.0 comes after this stage, not before.** That is the design document's rule and it is the reason this gate is a gate rather than a milestone.

---

## 5. Stage three, the evidence layer

Decision record: [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md). Exit (design document §11): a figure resolves end to end to a record a third party can verify without trusting the node.

**#104, ingest stores no provenance, so no hit can earn a trust tier above "unattested". P1.** This is the load-bearing one. ADR-0022 records that `Citadel.ingest` already computes `sha256(data.encode("utf-8"))` (`kb/service.py:86`) and spends it immediately as half of an in-process duplicate key, never storing it with the record and never returning it to a reader; what a reader receives is computed at read time and is a transit-integrity check. **REPORTED** from ADR-0022, which cites those file positions; the ADR was read in this session but the call sites were not re-read. Closing #104 means the capture-time fingerprint is stored with the record and returned on every retrieval path, with a name that says which of the two values it is.
Done looks like: a hit that carries a fingerprint the node committed to before the query was made, and a second retrieval path returning the same value for the same document.

**#124, make Structured Knowledge a durable first-class artifact Citadel owns. P0, roadmap.** [ADR-0010](../../adr/0010-structured-knowledge-durable-source-of-truth.md) already decided this; the issue is the build. It sits in stage three rather than stage one because a durable artifact that cannot be attested is a second copy with the same evidential weight as the first. `tasks.md` records the dependency shape: P1-2 (contradiction ledger) is a prerequisite and P0-4 (#123) is a companion, because update-in-place without contradiction gating is a silent overwrite (**VERIFIED** by reading `tasks.md`).
Done looks like: a per-topic page revised in place, a contradicting revision raising a **Knowledge Conflict** instead of overwriting, and the prior version recoverable.

**#135, graph nodes carry no trust tier, and `promoted_by`/`promoted_at` do not exist anywhere. P1.** This is the **Knowledge Mesh** half of the same layer. Under the substrate positioning, an evidence graph earns its place where a concept graph is decoration (design document §9), and a graph node with no trust tier and no promotion record is a concept graph. It is downstream of #104: a tier above "unattested" needs something attested to rest on.
Done looks like: a promoted item whose graph node names who promoted it and when, and a reader that can tell an attested tier from a body-derived hint.

**Ordering inside the stage:** #104, then #135, then #124 lands on top of both with #123 already in place from stage two. The chain mechanism ADR-0022 specifies (SHA-256 over stored bytes at capture, linked as `hash(previous_chain_hash + document_hash)`, head signed on a schedule, W3C PROV as vocabulary) is one piece of work that #104 carries; the public transparency log submission is queued behind it, because there has to be a chain head before there is anything to submit.

---

## 6. Stage four, the white-label product

Decision record: [ADR-0023](../../adr/0023-control-plane-outside-the-application.md). Exit (design document §11): a partner deploys, brands, and connects their own source without us writing code.

No open issue maps to this stage. That is expected: the control plane is a second system and Citadel gains no configuration for it, which ADR-0023 states as a constraint rather than an omission. Forbidden inside Citadel by that ADR: a setting naming another instance, a credential valid at more than one instance, a registry the application can query, and any code path whose behaviour depends on how many instances exist.

The design work for the control plane is written separately, at [`2026-08-04-control-plane-design.md`](2026-08-04-control-plane-design.md), which ships in the same commit as this document. Read it for provisioning, lifecycle, fleet upgrade strategy and telemetry boundaries; do not duplicate them here.

Two things from earlier stages are prerequisites and are worth naming so they are not re-derived: the retrieval interface (#118, ADR-0021) is what makes a pinned engine upgradable one instance at a time across a fleet, and the unit cost measurement from stage two is what chooses the hosting substrate. Neither is optional for this stage.

---

## 7. Issues that belong to no stage

Seven of the 27 are not on this path. None is closed by this document; each needs a triage decision from a human. They are listed with the reason and with what would move them back on.

| Issue | Why it is off the path | What would put it back |
|---|---|---|
| **#186** Graph node inspector dead-ends on entity nodes (`needs-triage`) | A **Knowledge Mesh** viewer defect. The design document parks the concept canvas and says an evidence graph earns its place where a concept graph is decoration. | #135 makes the graph carry trust tiers and promotion records, at which point the inspector is showing evidence and the dead end matters. |
| **#126** Dashboard bogus graph inspector on projection click, spinner never clears on failed fetch (P3) | Same surface, and possibly a surface being deleted. The design document's frontend decision is to finish `/next` and retire `kb/static`. Fixing this in `kb/static` may be work thrown away. | Check first whether the affected view exists in `/next`. If it does, this is a real bug in the surviving frontend and should be fixed there once. |
| **#125** `citadel explain <query>` (P2) | Blocked by its inputs. There is nothing honest to explain until hits carry named scores (#118, ADR-0021) and per-item provenance (#104). | Both of those closed. Then this becomes a small, high-value command and arguably the best demo of the evidence layer. |
| **#121** `kb/mesh.py` and `kb/knowledge_mesh.py` collide on the name `mesh` (P3) | Naming hygiene with no behavioural consequence measured. | Bundle it into the next change that already edits both files, so it costs one review instead of two. |
| **#127** Adopt `ruff format` across the tree and enforce it in CI (P3) | A tree-wide reformat during stage one makes every diff in flight conflict, and there are 12 open pull requests (**VERIFIED**, §9). | Do it immediately after the 0.5.0 tag (#128), when the branch count is at its lowest. |
| **#115** Collapse duplicated `--node-url` and `--json` argparse flags (P2, `good first issue`) | Deliberately kept off the path: it is labelled `good first issue` and `help wanted`, and the repository is taking outside contributions. Doing it internally spends the onboarding ramp an outside contributor would use. | An outside contributor picks it up, or three months pass with nobody taking it. Note it overlaps #116's helper consolidation, so whoever takes #116 should say in the issue whether they touched the flags. |
| **#119** `kb/server.py` holds 81 routes plus six domain rules in 6,389 lines (P2, roadmap; both figures **REPORTED** from the issue title and not re-measured here) | The largest hygiene item, and the one most likely to be done for its own sake. A split now is a merge-conflict engine across stage one. | Revisit when #118 and #104 both start editing this file. The split is cheap when the seams are already being cut, and expensive when it is the whole change. |

**Nothing else is deferred.** The other 20 are placed: fifteen in stage one (§2), two in stage two (§4), three in stage three (§5).

---

## 8. The pilot vertical decision

**Status: NOT TAKEN.** The design document §13 lists the pilot vertical as undecided and records agriculture as recommended rather than confirmed.

**Who takes it:** sarthi. It is a positioning decision with funding and partner consequences, not an engineering one, and nothing in the codebase changes on either answer.

**When it is needed:** only when a funding proposal or a pilot partner conversation forces a named vertical into a document. Until then it stays open, and the cost of leaving it open is zero. The product is general under the substrate positioning (design document §1), the pilot is the demonstration, and those are different sections of the same proposal rather than a fork in the roadmap.

**Recommendation: agriculture, anchored on EUDR.** Two reasons, in order of weight.

1. It is the only candidate where the regulation names micro and small enterprises and dates them separately: 30 June 2027 for micro and small operators, 30 December 2026 for medium and large. **REPORTED**. The design document §12 records this as VERIFIED from a Commission source on 2026-08-03; it was not re-checked against the regulation text in this session. Cheapest measurement that would settle it: read the applicability dates in the consolidated EUDR text on EUR-Lex and quote the article.
2. The obligation is a provenance problem by construction rather than by our framing. A due diligence statement holds only if plot geolocation, evidence against a fixed baseline date, supplier attestations, and the chain of reliance on upstream statements all stand together and can be re-examined later. That is the same shape as [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md): retained evidence, attested at capture, resolvable by a third party. No repositioning of the product is needed to make the story fit, which is what distinguishes a real anchor from a chosen one.

**Lower-risk alternative: energy, under EED Article 11.** Cleanest overlap profile of the candidates examined. Its key figures are **REPORTED** and unverified against the directive text, per the design document §12, so it is the safer story with the weaker evidence behind it. Taking this branch means verifying those figures first.

**What does not depend on the answer:** stages one through four, every issue in section 2, and the evidence layer in section 5. If this decision is blocking someone, it is being asked in the wrong place.

---

## 9. Dependabot

**Where it sits:** inside stage one, before the 0.5.0 tag (#128). Not in front of the P0 defects, and not after the release.

**What is actually open.** Six vulnerability alerts on the default branch, three high and three medium. **VERIFIED** by `gh api repos/masumi-network/Citadel/dependabot/alerts --paginate` on 2026-08-04, which returns: `postcss` in `package-lock.json` (two high, two medium), `sharp` in `package-lock.json` (one high), and `diskcache` in `uv.lock` (one medium). Seven dependency pull requests are open: #214, #213, #212, #211 (GitHub Actions bumps), #168 and #165 (pip bumps), and #235, `build(deps): bump postcss and next`, which is the one that closes the four `postcss` alerts. **VERIFIED** by `gh pr list --state open` on 2026-08-04, which also shows five more open pull requests (#233, #232, #230, #205, #169), so a tree-wide reformat (#127) would collide with all twelve. Two cautions on those numbers. The alert count is the number a reviewer will read off the repository, so re-run the command rather than quoting this line. And the open-PR total moves as branches land, which it did while this document was being written: an earlier draft of this paragraph counted thirteen, because it listed #166 among the pip bumps when it had already been closed on 2026-08-03, and counted #231 among the others when it merged the same day.

**The nuance that changes the priority, in both directions.** None of the six alerts is in `requirements.txt`, which is the deploy path: the file's own comment records that "this file is the deploy path and `uv.lock` is not" (`requirements.txt:18-23`, **VERIFIED** by reading it on 2026-08-04). Five of the six are in the frontend build toolchain and one is in the lockfile CI audits. So the runtime install is not directly implicated by these six, which lowers the operational urgency.

It does not lower the obligation, for three separable reasons:

- A product sold on governance is judged by its own posture first. An open alert count is the cheapest thing a procurement reviewer checks, it is visible on a public repository without asking anyone, and "those are only build-time" is an argument made after the impression has formed.
- Build-time compromise reaches the runtime anyway. The frontend is built on a developer machine and the bundle is committed (`tasks.md` records the React Flow bundle as built by esbuild locally because there is no Node in CI, **VERIFIED**), so a compromised build dependency ships inside a committed artifact that no runtime scan looks at.
- The `datamodel-code-generator` floor in `requirements.txt:18-23` exists because ten CVEs were disclosed against 0.59.0 and the highest fix among them is 0.64.0 (**VERIFIED** by reading the file). PR #168 raises that floor to 0.71.0. Anything that reverts that floor reopens the set, and `tasks.md` already records one governance branch that would have done exactly that (**VERIFIED** by reading `tasks.md`). Whoever merges dependency PRs should check the resulting `requirements.txt` floor rather than the PR title.

**Done looks like:** zero open alerts on the default branch, the seven dependency PRs merged or closed with a stated reason, and the `datamodel-code-generator` floor at or above the merged value verified by reading `requirements.txt` after the merges rather than before.

---

## 10. What to do next

1. Land PR #169 (#105). It is open, it targets a P0, and it is the precondition for measuring #229 honestly.
2. Fix #153 in the same window. Small, and it is how every later sync fix gets observed.
3. Freeze the #122 fixtures against current behaviour before touching #227.

Everything after that follows section 2 in order.
