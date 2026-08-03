# Graph store migration runbook

**Date:** 2026-08-04
**Status:** not started. No gate below has been executed.
**Decides nothing.** [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md) already decided that the graph half of the **Knowledge Index** moves to cognee's PostgreSQL graph provider and that graph reads resolve inside a per-dataset context. This document is how that gets done, in what order, and what has to be true before each step is allowed to start.

Every factual claim is tagged **VERIFIED** (read or run in the session that produced this file, with the path given), **REPORTED** (a named source said so and it was not independently reproduced here), or **INFERRED**. An untagged sentence is an instruction, not a claim.

Two reading environments are involved and they are not the same:

- Repository paths (`kb/...`, `docs/...`, `tasks.md`) are in this repository and every line number below was read on branch `docs/substrate-adrs`. Those citations are reproducible from a checkout.
- Upstream paths (`cognee/...`) were read in the installed distribution, cognee 1.2.2, which is not vendored in this repository. It lives in the project virtual environment, under `.venv/lib/python3.12/site-packages/cognee`, and `cognee-1.2.2.dist-info` sits beside it. **VERIFIED** on 2026-08-04 by listing both and importing `cognee` from `.venv/bin/python`. The system `python3` has no cognee on its path, so an upstream citation checked with the wrong interpreter returns `ModuleNotFoundError` and proves nothing. Those citations are reproducible only against an install of the same pinned version, read through that interpreter.

Nothing here describes the runtime access-control configuration of any deployment. Where an upstream code path branches on a mode flag, this document says the branch exists and stops there. The mode a deployment runs in is settled in the deployment record, not in a public file.

---

## 1. What this project touches

### 1.1 The five graph operations

All five live in `kb/cognee_client.py` and all five reach the store through cognee's `get_graph_engine()`. **VERIFIED** by reading the file.

| # | Operation | Enclosing function | Call site |
|---|---|---|---|
| 1 | `get_graph_data()`, whole-graph read | `_read_graph_data` (`kb/cognee_client.py:728`) | `kb/cognee_client.py:736` |
| 2 | `get_nodes(...)`, batched node presence | `corpus_graph_presence` (`kb/cognee_client.py:1080`) | `kb/cognee_client.py:1096` |
| 3 | `get_node(...)`, single node | `_document_graph` (`kb/cognee_client.py:1221`) | `kb/cognee_client.py:1251` |
| 4 | `get_connections(...)`, incident edges | `_document_graph` (`kb/cognee_client.py:1221`) | `kb/cognee_client.py:1255` |
| 5 | `delete_nodes(...)` | `delete_graph_nodes` (`kb/cognee_client.py:1171`) | `kb/cognee_client.py:1189` |

Two of them resolve the engine directly (`kb/cognee_client.py:735` and `1187`); operations 2, 3 and 4 go through the shared seam `_graph_engine` (`kb/cognee_client.py:1211`), which resolves at `kb/cognee_client.py:1219` and does the same thing. Operation 2 enters the seam at `kb/cognee_client.py:1092` and operations 3 and 4 at `kb/cognee_client.py:1240`. **VERIFIED** by reading all five on 2026-08-04.

Operation 1 sits behind a TTL cache and a single-flight lock in `graph_data` (`kb/cognee_client.py:701`, double-checked at `714` and `718`). Under a per-dataset loop that cache stops being an optimisation and starts being the thing that keeps an O(datasets) read affordable. **INFERRED** from the loop shape decided in ADR-0020.

Operation 5 runs inside `self.writer_lock` (`kb/cognee_client.py:1188`, lock created at `kb/cognee_client.py:329`) and pairs the graph delete with a vector-store delete at `kb/cognee_client.py:1190`. **VERIFIED**.

### 1.2 The graph-count read, the evolve short-circuit, and the failure that looks like success

**This is the single most important warning in this document. Read it before writing any code.**

`CitadelService._graph_counts` at `kb/service.py:242` calls `self.cognee.graph_data()` at `kb/service.py:243` and returns `{"nodes": len(nodes), "edges": len(edges)}`. **VERIFIED**.

`CitadelService.improve` at `kb/service.py:206` calls it at `kb/service.py:216` and then does this, at `kb/service.py:217`:

```python
counts = await self._graph_counts()
if counts["nodes"] == 0 and counts["edges"] == 0:
    return {
        "ok": True,
        "skipped": "empty_graph",
        "dataset": target_dataset,
        "reason": "graph is empty; nothing to improve",
    }
```

**VERIFIED** by reading `kb/service.py:216-223`. The comment above it records why the short-circuit exists: `cognee.improve` raises a raw `EntityNotFoundError` on an empty graph, and returning a clean no-op was preferred to a traceback.

Now trace what that produces if the scoped read is wrong.

A per-dataset read that resolves no datasets, or resolves them against the wrong context, or swallows a per-dataset failure into an empty list, returns `([], [])`. `_graph_counts` reports zero nodes and zero edges. `improve` returns `ok: True, skipped: "empty_graph"`. The **Learning Process** does not run.

Nothing on any surface says anything is wrong:

- `run_improve` at `kb/server.py:6854` only records a mesh error and raises HTTP 500 inside its `except` branch (`kb/server.py:6868-6878`). A returned dict is not an exception, so it goes back through `jsonable_encoder` as a 200 with `ok: true`. **VERIFIED**.
- The self-improvement pass calls `self.citadel.improve(...)` at `kb/self_improve.py:225`, inside a `try` whose `except` logs "improve step failed with %s; continuing" at `kb/self_improve.py:228`. A skip raises nothing, so it does not even produce that warning. **VERIFIED**.

So the visible signature of a broken scoped read is a green evolve pass, a 200 response, an `ok: true` field, and no log line. That is indistinguishable from a healthy pass over a vault that happens to have nothing to improve. This repository has a documented history of exactly this class: `kb/service.py:306-311` carries a note that `graph_grew` was demoted from a pass condition because production reported "grew=True canary_ok=True" every hour while ingest stages were dead. **VERIFIED** by reading that comment.

**The required design response, and it is not optional:** the scoped read must be able to distinguish "walked zero datasets" from "walked N datasets and all of them were empty". Concretely, the loop returns the number of datasets it enumerated and the number it read successfully, `_graph_counts` carries those numbers up, and the `empty_graph` short-circuit fires only when at least one dataset was read successfully. Enumerating zero datasets, or failing every dataset, raises. A partial failure is reported as a partial, never merged into the empty case.

### 1.3 The empty-graph fallback in the Knowledge Mesh

`kb/knowledge_mesh.py` is the **Knowledge Mesh** reader. (`kb/mesh.py` is a different module; issue #121 is open on that name collision, so open the right file.)

`KnowledgeMesh.graph` has three distinct degraded outcomes, all routed through `fallback_graph` at `kb/knowledge_mesh.py:650`. **VERIFIED** by reading the file:

- `graph_access_unavailable` at `kb/knowledge_mesh.py:702`, when the gateway exposes no `graph_data` attribute.
- `graph_engine_error:<ExceptionClass>` at `kb/knowledge_mesh.py:710`, when the read raises. Logged as a warning at `kb/knowledge_mesh.py:706`.
- `graph_empty` at `kb/knowledge_mesh.py:743`, when `raw_nodes` is falsy after a successful read.

Under a single whole-graph read those three are cleanly separable. Under a loop they collapse unless the loop is written to keep them apart, because a loop that catches per-dataset exceptions and continues turns "every dataset failed" into `graph_empty`, which renders as a healthy but empty vault. The **Seat Presence** hubs keep rendering in all three cases, per ADR-0009 and the docstring at `kb/knowledge_mesh.py:653-658`, so the page still looks populated while the content half is missing. **INFERRED** from the code paths above; the loop does not exist yet, so this is a prediction about the change, not an observation of it.

The new definition of empty: a **Vault Member** sees `graph_empty` only when every enumerated dataset was read successfully and every one of them returned no nodes. Any dataset that failed makes the payload a partial with a named reason, and zero enumerated datasets is an error.

### 1.4 The corpus presence check and the document drill-down

**`corpus_graph_presence`** (`kb/cognee_client.py:1080`) answers which document ids exist as graph nodes, which is how the census tells "accepted" apart from "cognified". Its degradation is already careful: an engine with no callable `get_nodes` returns `None` rather than an empty set (`kb/cognee_client.py:1093-1095`), and the consumer at `kb/server.py:3671-3692` turns `None` into `in_graph: null` with an explicit note that null means not measured rather than absent. **VERIFIED**.

That consumer also already carries the exact caveat this migration makes sharper, at `kb/server.py:3683-3692`: the graph adapter answers a failed batch lookup and a no-match batch identically, so an all-absent page is a weaker claim than a mixed one. Under a per-dataset loop that ambiguity gets one level worse, because a page of documents can now be absent from the one dataset that was searched while present in another. Presence must be asked per dataset and unioned, and the union must record how many datasets answered.

**`_document_graph`** (`kb/cognee_client.py:1221`) is the drill-down: one node plus its incident edges, used by `get_document` at `kb/cognee_client.py:1327` and again at `kb/cognee_client.py:1491`. Its degradation is a fallback to the full `graph_data()` read at `kb/cognee_client.py:1246`, taken when the engine is missing or lacks the primitives (`kb/cognee_client.py:1239-1245`). Its docstring is explicit that a node absent from the graph returns an empty targeted result *without* triggering the fallback, because a full read cannot contain a node the graph lacks (`kb/cognee_client.py:1232-1237`). **VERIFIED**.

That reasoning is correct today and becomes false under scoping. Once reads are per-dataset, "not in the dataset I looked at" and "not in the graph" are different statements, and the second one no longer follows from the first. A drill-down for a document id must resolve the document's dataset before it reads, or walk datasets until it finds the node. Failing to do that turns a correct-but-slow degradation into a wrong answer, and the wrong answer is a dead end on a document that exists. Issue #186, "Graph node inspector dead-ends on entity nodes", is open on an adjacent symptom, so this surface already has a live defect and should be re-checked after the change rather than assumed unchanged.

### 1.5 The delete path, and what a silent no-op accumulates

`delete_graph_nodes` (`kb/cognee_client.py:1171`) deletes from the graph at `kb/cognee_client.py:1189` and from the `DocumentChunk_text` vector collection at `kb/cognee_client.py:1190`, under the writer lock. Its docstring records why both: the same UUID identifies a chunk in the graph and in the vector collection that CHUNKS search reads, so deleting only the graph node leaves the chunk searchable. **VERIFIED**.

Two callers feed it, both in `kb/service.py`:

- `_delete_marker_node` (`kb/service.py:326`) reads the whole graph at `kb/service.py:329`, filters for the cognify verify marker, and deletes at `kb/service.py:337`. Wrapped in a bare `except` that logs a warning, because cleanup must never fail the cognify (`kb/service.py:338-339`). **VERIFIED**.
- `cleanup_legacy_nodes` (`kb/service.py:341`) reads the whole graph at `kb/service.py:355`, classifies garbage, and deletes at `kb/service.py:400` when `dry_run` is false. **VERIFIED**.

Both find their candidates by scanning the output of the whole-graph read. A scoped read that returns fewer nodes than it should, or none, therefore produces zero candidates, zero deletions, and a successful-looking result: `cleanup_legacy_nodes` returns `{"dry_run": ..., "counts_by_kind": {}, "candidates": [], "deleted": 0}`, which is the same payload a clean vault produces. **VERIFIED** by reading `kb/service.py:401-406`.

What accumulates is specific, because the classes are named at `kb/service.py:344-350`: `COGNIFY_TEST_MARKER` canaries, `[DataItem]` and session-scaffold blobs, session-cache node types, pre-ADR-0016 repo-content fossils, and pre-fix GitHub digest fossils. Each cognify verification cycle mints one more canary. Each of those is a node and a searchable chunk. The cost is not a stalled cleanup job; it is a slow contamination of search results by fossils, on a node whose maintenance tooling reports it is clean. The vector half compounds it: the graph delete and the vector delete are paired at one call site, so a graph node the scan never saw also keeps its chunk, and the chunk is what search reads.

The sweep at `kb/service.py:377-396` partly compensates, because it also probes the vector store by search for five literal fossil strings and adds whatever it finds. That path does not depend on the graph read, so it survives the migration. It is a backstop, not a substitute: it catches five known strings, and the graph scan is the part that generalises. **INFERRED** from reading both branches.

### 1.6 The relational reads, and why the graph provider does not reach them

Five reads carry dataset attribution and the corpus census. All five are in `kb/cognee_client.py` and none of them opens the graph:

| Read | Definition |
|---|---|
| `node_dataset_map` | `kb/cognee_client.py:739` |
| `document_counts_by_dataset` | `kb/cognee_client.py:837` |
| `corpus_page` | `kb/cognee_client.py:874` |
| `corpus_totals` | `kb/cognee_client.py:968` |
| `corpus_chunk_counts` | `kb/cognee_client.py:1030` |

`node_dataset_map`'s docstring states the mechanism directly: a TextDocument graph node's id *is* the relational `Data.id`, and dataset membership lives only in the relational store, through `datasets` to `dataset_data` to `data`. It is a read-only relational query, which is why it takes no `writer_lock` (`kb/cognee_client.py:742-746`). **VERIFIED**.

That is the whole reason the graph provider is irrelevant to them. Changing which engine stores nodes and edges does not change which SQLAlchemy tables record that a `Data` row belongs to a dataset. The ADR-0009 attribution that the **Knowledge Mesh** applies, and the census that ADR-0018 rests on, read those tables and keep reading them. `corpus_graph_presence` is the one census component that crosses over, and it crosses over because it asks the graph a question rather than because it is part of the census.

Practical consequence for the migration: after the store move and before the re-cognify, `corpus_totals` and `corpus_page` still report the corpus correctly while `in_graph` goes false or null for everything. That combination is the expected mid-migration reading, not a new defect, and whoever runs the census during the window needs to be told so in advance.

The re-index backlog is measured on those same relational and vector reads, which is why issue #228 (892 of 2867 documents accepted but never vector-indexed) and the re-cognify in gate 9 are one job rather than two.

### 1.7 Where `GRAPH_DATABASE_PROVIDER` appears

Five occurrences across the tree that are configuration, code or test, plus three lines of prose in the ADR and the 2026-08-03 design document, which are the last row below. Prose in the 2026-08-04 specification documents, including this one, is not counted. **VERIFIED** by `grep -rn "GRAPH_DATABASE_PROVIDER" .` on branch `docs/substrate-adrs` on 2026-08-04:

| Path | What it is |
|---|---|
| `.env.example:258` | `GRAPH_DATABASE_PROVIDER=kuzu`, the template every operator copies |
| `docs/operations.md:46` | `GRAPH_DATABASE_PROVIDER=kuzu`, in the documented operations environment |
| `tasks.md:776` | the deployed-configuration narrative, listing it alongside `EMBEDDING_PROVIDER=fastembed`, `VECTOR_DB_PROVIDER=pgvector` and `CITADEL_SEARCH_DEFAULT_DATASET=masumi-network` |
| `kb/cognee_client.py:383` | the only functional read: when the value lowercases to `postgres`, copy `DB_*` into `GRAPH_DATABASE_*` (`kb/cognee_client.py:384-388`) |
| `tests/test_cognee_client.py:1381` | sets it to `postgres` to exercise that branch |
| `docs/adr/0020-...md` and `docs/superpowers/specs/2026-08-03-...md` | prose about the label |

Two things follow. First, the plumbing for the PostgreSQL provider already exists in the client and already has a test, so the provider switch is a configuration change against an existing branch rather than a new one. Be precise about which half is tested: `test_cognee_public_client_derives_postgres_graph_env` (`tests/test_cognee_client.py:1375-1389`) sets the provider value and the five `DB_*` variables, calls `_prepare_cognee_environment()`, and asserts only that the five `GRAPH_DATABASE_*` names received the copied values. It does not assert that the resulting configuration opens a graph, so the environment mapping is covered and the connection is not. **VERIFIED** by reading `kb/cognee_client.py:383-388` and that test on 2026-08-04.

Second, the string `kuzu` is a label for something else. `kb/cognee_client.py:139` imports `LadybugAdapter` from `cognee.infrastructure.databases.graph.ladybug.adapter`, and the boot self-check at `kb/cognee_client.py:173-179` asserts that `LadybugAdapter` still exposes `get_nodes`. **VERIFIED**. So the code already names the engine correctly and only the configuration does not.

---

## 2. The gates

Each gate states what passes and what fails. A gate that fails stops the project at that gate; it does not get worked around.

### Gate 1: confirm the application database role can create databases

**Do this before anything else in this project, including the label rename in gate 2.** The per-dataset handler creates one PostgreSQL database per dataset at runtime. `PostgresGraphDatasetDatabaseHandler.create_dataset` builds the name as `f"{dataset_id}"` and calls `create_pg_database_if_not_exists`, which connects to the cluster's `postgres` maintenance database in AUTOCOMMIT mode and runs `CREATE DATABASE "<name>"` behind an existence check on `pg_database`. **VERIFIED** by reading `cognee/infrastructure/databases/graph/postgres/PostgresGraphDatasetDatabaseHandler.py` and `cognee/infrastructure/databases/postgres/admin.py:60-90` in the installed 1.2.2.

Two privileges are needed, and they are separate: the role must be able to connect to the maintenance database, and it must have `CREATEDB`. A managed PostgreSQL that hands out a single database and a non-superuser role may permit neither.

**Check:**

```
psql "$DATABASE_URL" -c "SELECT current_user, rolcreatedb FROM pg_roles WHERE rolname = current_user;"
```

Then confirm the maintenance connection separately, by running any trivial statement through the same connection URL with the database name replaced by `postgres`. Both halves have to answer; the first one alone tells you the role is allowed to create a database somewhere it may not be allowed to connect.

**Why first:** because a negative answer changes the shape of the whole plan rather than delaying it. ADR-0020 names the consequence: if the privilege is unavailable, the scoped-read half is blocked and the store move has to ship on its own, which is the sequencing that ADR rejects on cost. Both records this runbook executes put the check at the front. ADR-0020 says to verify it "before any other work in this project" (`docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md:31`) and the substrate design says it "must be checked against managed PostgreSQL before anything else" (`docs/superpowers/specs/2026-08-03-citadel-substrate-design.md:81`). **VERIFIED** by reading both on 2026-08-04.

An earlier draft of this runbook put this gate at position three, behind the label rename and the log grep, and argued that the two gates ahead of it write nothing to the graph. That argument is recorded rather than deleted, because it is the one anyone re-ordering these gates will reach for again. It is wrong for one reason: the label rename does write, to three committed files and to a deployment environment value, and spending that change before knowing whether the plan it belongs to can proceed is the cost the ADR's wording exists to avoid.

**Passes when:** `rolcreatedb` is true and the maintenance connection succeeds. Proceed to gate 2.

**Fails when:** either is unavailable. **The whole plan changes.** Do not write the loop, and do not spend the label rename either, before this answer exists.

**Also record:** whether database creation counts against a plan limit on database count, and what the per-database overhead is on the managed instance. Not determined here. Cheapest measurement: the provider's own limits page plus one `CREATE DATABASE` on the scratch instance in gate 4.

### Gate 2: rename the provider label to its true name

**Do:** change `kuzu` to `ladybug` in `.env.example:258`, `docs/operations.md:46` and `tasks.md:776`, and set the same value in the deployment environment.

**Why here:** it is the cheapest change in the project and it removes a recurring false alarm, so it goes as early as any change that writes something can go, which is directly behind gate 1's measurement. Upstream registers both strings and maps them to the same handler class: `supported_dataset_database_handlers` maps `"ladybug"` and `"kuzu"` to `LadybugDatasetDatabaseHandler`, alongside `"postgres_graph"` to `PostgresGraphDatasetDatabaseHandler`. **VERIFIED** by reading `cognee/infrastructure/databases/dataset_database_handler/supported_dataset_database_handlers.py` in the installed 1.2.2. An engineer who reads `kuzu` in the configuration and then greps the codebase finds `LadybugAdapter`, and spends time reconciling the two. The design session recorded that this exact confusion produced a wrong claim before it was corrected (`docs/superpowers/specs/2026-08-03-citadel-substrate-design.md:69`). **VERIFIED**.

**What is established is that the two keys share a handler class, not that they behave identically.** They are two separate registry entries pointing at one class, and each carries its own `handler_provider` string, `"kuzu"` against `"ladybug"`. **VERIFIED** in the same file. Whether any consumer reads `handler_provider` is **not determined**, and that is what this gate's failure condition is for. Cheapest measurement, if the answer is wanted before the change rather than after it: grep the installed distribution for `handler_provider` and read every consumer.

**Passes when:** the deployment starts, the graph opens, a search returns hits, and the mesh graph endpoint returns a payload with `fallback` absent or false. Nothing else changed.

**Fails when:** the process will not start, or the graph read falls back. That means the two strings are not interchangeable in the pinned version after all, in which case revert the value and record the finding, because the ADR's option analysis assumed they were.

### Gate 3: grep production logs for the orphaned-lock signature

**Do:** search the deployment's logs for `Lock is held by PID` across the retained window, and record the count, the timestamps, and whether they correlate with the observed stall history.

**State this plainly, because it is currently being carried the wrong way round:** upstream issue 3708, "Kuzu worker crash leaves orphaned database lock, blocking all subsequent recall() calls", names the version line this deployment pins, and the merged fix has more components than the pin carries. **REPORTED** in `docs/superpowers/specs/2026-08-03-citadel-substrate-design.md:73` and `docs/adr/0020-...:19`, read from the upstream issue against the installed source on 2026-08-03; the issue is not a file in this repository and the reading is not reproducible from it. Until the log grep runs, that issue is a well-matched candidate for the stalls and **it is not a diagnosis**. It has not been shown that the observed stalls are this failure. No document, including this one, should say it has.

**Passes when:** the signature appears, with timestamps that line up with known stalls. The lock story becomes a measured cause and the migration's primary justification is confirmed.

**Fails when:** the signature does not appear anywhere in the retained window. That does not cancel the migration, because the operational arguments in ADR-0020 stand on their own (one datastore per instance instead of a database plus a durable volume, and no OS-level file lock to orphan). It does cancel the claim that this migration fixes the observed stalls, and it means the stalls have an unfound cause that will survive the migration. In that case open the stall investigation as its own issue before shipping, or the migration will be credited with a fix it did not deliver.

**If the log window is too short to answer:** say "not determined" and name the retention limit. Do not infer absence from an empty grep. An empty result has at least one boring explanation, starting with logs that rotated.

### Gate 4: the throwaway experiment

**Do:** on a scratch PostgreSQL database that nothing depends on, with the PostgreSQL graph provider configured, create two datasets and ingest one document into each. Then observe three things, in this order.

**Observation A, per-dataset databases get created.** After both ingests, list databases on the scratch cluster. Expect one per dataset, named by dataset id, per the handler's `graph_db_name = f"{dataset_id}"` above.

**Observation B, reproduce the failure deliberately.** Issue a naive whole-graph read, the shape the code uses today: `engine = await get_graph_engine()` then `await engine.get_graph_data()`, outside any per-dataset context. Expect nothing, or expect the contents of whichever database the default configuration points at, and specifically *not* the union of both documents. Write down what it actually returns.

This observation is the point of the experiment. Confirming the new path works proves less than confirming the old path breaks in the way the design predicts, because a new path can pass for reasons unrelated to the change. If the naive read returns both documents, the model behind ADR-0020 is wrong about how the read resolves, and the loop being planned may be unnecessary. That is a finding, and it is cheap here and expensive after the code is written.

**Observation C, the per-dataset context loop returns both documents.** Wrap the read as `async with set_database_global_context_variables(dataset, user_id):` and iterate, resolving the engine handle inside the block. Expect one document per iteration and both after deduplication by node id.

`user_id` is not free-floating: upstream wants cognee's own user, and the path this repository already uses is `cognee.modules.users.methods.get_default_user`, imported at `kb/cognee_client.py:112` inside `assert_cognee_dataset_api`. **VERIFIED** by reading that file on 2026-08-04. Use the same symbol in the experiment, so the experiment and the code that follows it resolve the user the same way.

The context manager is `DatabaseContextManager` (`cognee/context_global_variables.py:107`), entered through `set_database_global_context_variables` (`cognee/context_global_variables.py:281`). Its `apply_database_context_variables` (`cognee/context_global_variables.py:138`) calls `get_or_create_dataset_database` at `cognee/context_global_variables.py:170`, then sets the `graph_db_config` and `vector_db_config` ContextVars at `cognee/context_global_variables.py:240-241`. The per-dataset branch is guarded by a mode check at `cognee/context_global_variables.py:151`; which mode a deployment runs in is a runtime configuration matter settled in the deployment record and deliberately not written here. Note also that the `await` form of the call is deprecated in favour of `async with`, because only the block form releases the dataset queue slot on exit. **VERIFIED**, all of it, by reading `cognee/context_global_variables.py` in the installed 1.2.2.

**Time:** roughly thirty minutes once the scratch database exists. **INFERRED**, from the number of steps rather than from having run it.

**Passes when:** A, B and C all match. Then the loop in gate 5 is being written against observed behaviour rather than against a reading of upstream source.

**Fails when:** any of the three does not match. Stop and re-read upstream. In particular, if C does not return both documents, the loop cannot be written yet and the failure needs a name before anything ships.

**Teardown:** drop the scratch databases. This experiment must not touch the production cluster and must not run under the production role, because `create_pg_database_if_not_exists` creates databases on whatever cluster the configuration points at, with no confirmation step. Confirm which cluster the environment resolves to before the first ingest, not after.

### Gate 5: the code change

**Do:** rewrite the five call sites from §1.1 to run inside a per-dataset context, and change the whole-graph read into a loop.

Three requirements, and the third is the one that will be got wrong:

1. **All five sites, in one change.** ADR-0020 rejected splitting the store move from the scoping precisely because both passes land on the same call sites, so a split means editing them twice and re-verifying every caller against a shape the second pass invalidates.

2. **The loop deduplicates by node id.** A document reachable from two datasets returns twice. The `graph_data` TTL cache and single-flight lock (`kb/cognee_client.py:701-726`) now amortise N queries rather than one, so leave them in place and re-check the TTL against the new cost.

3. **Resolve the engine handle inside the context, never hoisted out of the loop.** `get_graph_engine()` snapshots the context config once and stores it on the handle: `config = get_graph_context_config()` then `handle = _GraphEngineHandle(config)` (`cognee/infrastructure/databases/graph/get_graph_engine.py:110-114`), and `_GraphEngineHandle.__getattr__` (`:103`) resolves through `_engine()` (`:89`), which calls `create_graph_engine(**self._config)` against that frozen dict. **VERIFIED** in the installed 1.2.2.

   Read that carefully, because the lazy resolution is real but it solves a different problem than the one it looks like it solves. The handle re-resolves on every attribute access so that it survives cache eviction, which the class docstring at `:59-81` states explicitly: `prune_system` clears the cache, `delete_dataset` evicts entries, and the context manager's `__aexit__` evicts subprocess-mode engines, and a handle that held a direct proxy would become a dead object. What the handle does *not* do is re-read the ContextVar. `get_graph_context_config` reads `graph_db_config` (`cognee/infrastructure/databases/graph/config.py:191-199`), but only at the moment `get_graph_engine()` is called. A handle obtained before or outside the loop is therefore bound to the configuration that was live at that instant, and every iteration will query the same database while appearing to iterate. **VERIFIED** by reading both files.

   The failure mode of getting this wrong is the one in §1.2: a loop that runs N times, returns one dataset's nodes or none, and reports success everywhere.

**Passes when:** contract tests cover, at minimum, a two-dataset read returning the union with duplicates removed; a read where one dataset raises, returning a partial with a named reason rather than an empty union; a read that enumerates zero datasets, raising rather than returning `([], [])`; and an `improve` call against a zero-dataset enumeration that raises rather than returning `skipped: empty_graph`. Prove each in both directions: assert the test fails when the guard is removed. A test that passes against a stubbed empty union proves nothing.

**Fails when:** any of those tests cannot be made to fail by removing the code it covers. That means the test asserts a shape the system produces for another reason, and the guard it is meant to cover does nothing. A guard that does nothing and a guard with nothing to do produce identical output, so the only way to tell them apart is to break the guard on purpose and watch the test go red.

**Also update:** the `_document_graph` docstring at `kb/cognee_client.py:1232-1237`, whose "a full graph read cannot contain a node the graph lacks" reasoning stops holding under scoping (§1.4), and the `graph_empty` definition in `kb/knowledge_mesh.py` (§1.3).

### Gate 6: the search result shape migration

Under the per-dataset mode, upstream's `_backwards_compatible_search_results` (`cognee/modules/search/methods/search.py:385`) returns one entry per dataset carrying `dataset_id`, `dataset_name` and `dataset_tenant_id`, with the results nested under `search_result` or under the verbose triple (`:394-408`). Outside that mode it returns the flat shape. **VERIFIED** by reading the installed 1.2.2.

`CogneePublicClient.recall` (`kb/cognee_client.py:591`, protocol at `kb/cognee_client.py:274`) returns a flat list, and its callers are `kb/service.py:147` and two internal sites at `kb/cognee_client.py:612` and `kb/cognee_client.py:628`. **VERIFIED**. Downstream, `kb/search_format.py` parses hits (its lexical scoring reports `retriever_scores_available` at `kb/search_format.py:340`), the `/search` envelope reads them, and `kb/promotion.py` enumerates a **Seat** dataset through semantic recall using the broad seed queries described at `kb/promotion.py:55-56`. **VERIFIED**.

**Land this behind the interface from [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md), not in front of it.** With the adapter in place, the new shape stops at the adapter and each caller migrates once. Without it, every caller migrates now for the shape change and again when the interface arrives, and the second migration re-verifies work that was verified against a shape that no longer exists. That is the same double-work argument that made the store move and the scoping one project, applied one level up.

There is a second reason the ordering matters. **Promotion** issues no graph reads of its own, so the store move does not touch it, but the shape change does. A caller that is untouched by half a project and broken by the other half is exactly the caller that gets missed in a two-pass migration.

**Passes when:** no module outside the adapter parses a per-dataset result envelope, and `kb/search_format.py`, the `/search` envelope, and `kb/promotion.py` all read the adapter's own hit type. Issue #118 is the existing record that `CogneeGateway` does not yet cover `writer_lock` or `node_dataset_map`, so treat that issue as part of this gate's scope rather than adjacent to it.

**Fails when:** the adapter is not ready and the shape change is needed anyway. Then stop and finish the adapter. Shipping the shape change bare is the option ADR-0020 costed and rejected.

### Gate 7: the connection-pool cache size

`DATABASE_MAX_LRU_CACHE_SIZE` defaults to **6**. **VERIFIED** by reading `cognee/shared/lru_cache.py:10` in the installed 1.2.2: `int(os.getenv("DATABASE_MAX_LRU_CACHE_SIZE", "6"))`. Note that the module's own docstring says the default is 128, at line 5 of the same file against the code at line 10, so the docstring and the code disagree and the code is what runs. Anyone who checks this by reading the docstring will get the wrong number.

It caps three engine factory caches: the graph engine's `closing_lru_cache` (`cognee/infrastructure/databases/graph/get_graph_engine.py:244`), the vector engine's (`cognee/infrastructure/databases/vector/create_vector_engine.py:153`), and the relational engine's plain `lru_cache` (`cognee/infrastructure/databases/relational/create_relational_engine.py:9`). **VERIFIED**.

The dataset count on the production node is **not determined** in this session. The design document states thirteen (**REPORTED**, `docs/superpowers/specs/2026-08-03-citadel-substrate-design.md:81`). Cheapest measurement that settles it: `SELECT count(*) FROM datasets;` against the node's relational store.

If the count exceeds the cache size, every org-wide read thrashes. What each eviction costs is specific, and it is worse than a cache miss: `closing_lru_cache` closes the adapter it evicts, which is the behaviour the `_GraphEngineHandle` docstring at `get_graph_engine.py:59-81` exists to work around. So an eviction tears down a live connection pool and the next iteration builds a new one, per dataset, per read. With N datasets and a cache of 6, a single walk evicts and rebuilds roughly N minus 6 pools, and the walk runs again on every cache miss of the `graph_data` TTL. **INFERRED** from the cache semantics; the per-eviction wall-clock cost is not determined, and the cheapest way to get it is to time one loop at cache size 6 and one at cache size N in the gate 4 scratch environment.

**Do:** set `DATABASE_MAX_LRU_CACHE_SIZE` in the deployment environment to comfortably exceed the dataset count, with headroom for the vector and relational factories that share the same constant. It must be set before cognee is imported, because the module reads it at import time (`cognee/shared/lru_cache.py:10`), so it belongs in the deployment environment and not in application code.

**Passes when:** the variable is set in the deployment, its value exceeds the measured dataset count, and a timed org-wide read shows no per-iteration pool rebuild.

**Fails when:** the value cannot be set early enough, or the dataset count grows past it in normal operation. A **Seat** is created per **Vault Member**, so the count grows with the team and a fixed number will be outgrown. Record the ceiling and what happens when it is crossed, rather than picking a number and moving on.

### Gate 8: the provider switch

**Do:** set `GRAPH_DATABASE_PROVIDER` in the deployment environment to the value that selects the PostgreSQL graph provider, and redeploy. This is the step that actually moves the store. Everything before it prepares for it and everything after it depends on it having happened.

**Settle which value that is in gate 4, not here.** Two strings are in play and they are consumed by different code. `kb/cognee_client.py:383` branches on the lowercased value being exactly `postgres`, and copies `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME` and `DB_PASSWORD` into the matching `GRAPH_DATABASE_*` names on that branch alone (`kb/cognee_client.py:384-388`). Upstream's `supported_dataset_database_handlers` keys the per-dataset handler as `postgres_graph`, not `postgres`, with `handler_provider` set to `postgres` inside that entry. **VERIFIED** by reading both on 2026-08-04, the second in the installed 1.2.2. Whether one value satisfies both consumers is **not determined**.

Cheapest measurement: in the gate 4 scratch environment, set each value in turn and record which one produces both the `GRAPH_DATABASE_*` copy and a per-dataset database. Do not proceed until one value is known to do both, or until a two-value configuration has been written down. This is the trap in the whole runbook that fails quietly: the wrong string gives a process that starts, a configuration that looks set, and a graph read that resolves somewhere other than where the plan believes, which is §1.2's failure exactly.

**Passes when:** the deployment starts, the graph read reports a nonzero dataset enumeration per the gate 5 contract, and the census reports `in_graph` null or false for everything while `corpus_totals` is unchanged. That combination is §1.6's expected mid-migration reading, not a defect.

**Fails when:** the process will not start, or the graph read raises. Revert the environment value, which rollback condition 2 requires to be sufficient on its own.

### Gate 9: the re-cognify

**This is a full rebuild, not a data migration.** There is no export path from the embedded graph into the PostgreSQL adapter. The graph is a rebuildable projection of **Structured Knowledge**, which ADR-0010 establishes as the durable source of truth, so rebuilding it is the supported operation rather than a workaround. **REPORTED** from `docs/adr/0020-...:32`, which is a decision this runbook executes rather than a measurement.

**Plan it as one job with issue #228** (892 of 2867 documents accepted but never vector-indexed). Re-cognifying documents that were accepted and never indexed is the same operation on a different set of rows, and running it twice costs twice.

**Sequence it after issue #227** (chunk size exceeds the embedder's input window). Re-cognifying before that fix rebuilds the graph with the same chunking defect, and the rebuild has to be paid again. **INFERRED** from the issue titles; whether #227's fix changes chunk boundaries for already-correct documents is not determined, and the cheapest check is to read the fix's diff before scheduling the rebuild.

**Watch the runtime.** Issue #229 records that inline ingest blocks for minutes and that one surface reports a timeout on a write that succeeded. A full-corpus rebuild is that path at corpus scale. Before scheduling, answer: can the job be resumed from where it stopped, and does the reported outcome of each item distinguish a successful write from a timed-out one. If it cannot be resumed, the rebuild is one long transaction with no safe interruption point, and that is a property to know before starting rather than during.

**Estimate the runtime before scheduling, because the rollback plan hangs on it.** Rollback condition 5 in §3 requires the migration window to have a stated end, and no other gate supplies a basis for stating one. Get it this way: time one re-cognify of a representative document on the path #229 describes, multiply by the document count from `corpus_totals`, and state the window from that number with the multiplication shown. The figure is **not determined** here, and a window stated without it is a guess that a half-migrated node will be left sitting in.

**Passes when:** the census reports `in_graph` true for the documents that carry chunks, the count of documents with zero chunks has dropped to the level #228 targets, and a search for content known to be in a re-cognified document returns it. Use a query that provably matched before the migration, not a new one.

**Fails when:** the rebuild completes and `in_graph` is still null. Null is "not measured", not "absent" (`kb/server.py:3679-3682`), so that is a measurement failure and not a rebuild failure, and it must be resolved before the rebuild is called done. An all-absent census page is a weaker claim than a mixed one, for the reason the code itself records at `kb/server.py:3683-3692`.

---

## 3. Rollback

**What the system looks like if this is abandoned partway, gate by gate.**

Abandoning after gates 1, 2 or 3 leaves nothing to undo. Gates 1 and 3 are measurements and write nothing. Gate 2 is one string in three files plus one environment value, revertible in a commit.

Abandoning after gate 4 leaves a scratch cluster with two per-dataset databases on it. Drop them. Production is untouched, provided the experiment was run against a scratch cluster as instructed, which is the reason that instruction is in the gate rather than in a footnote.

Abandoning after gate 5 but before gate 8, the provider switch, is the state to think hardest about. The code now resolves reads inside a per-dataset context while the store is still the embedded engine. Upstream supports that combination, since `supported_dataset_database_handlers` maps `ladybug` and `kuzu` to `LadybugDatasetDatabaseHandler` (**VERIFIED**, §Gate 2), so it will run. It also gives one file lock per dataset instead of one, which multiplies the failure class this project exists to remove. ADR-0020 rejected this configuration as a destination. Do not park here. If the project stops at this point, revert the loop rather than leaving it deployed against the embedded store.

Abandoning between gate 8, the provider switch, and a completed gate 9 re-cognify is the dangerous half-state, and it is the one a rollback plan has to be written for. The graph is empty because nothing has been rebuilt into it. The relational and vector halves are unaffected (§1.6), so the corpus census still reports the true document count and search over chunks still returns hits. The **Knowledge Mesh** renders the empty-graph fallback with **Seat Presence** hubs and no content, evolve reports `skipped: empty_graph` with `ok: true` on every pass, and `cleanup_legacy_nodes` reports zero candidates. Every one of those looks like a healthy quiet system. Anyone who walks in during this window without being told will read a clean vault.

**What has to be true for that rollback to be safe:**

1. **The old graph data still exists.** The mounted volume holding the embedded graph is not deleted, repurposed, or resized until the re-cognify has been verified by gate 9's exit condition. ADR-0020 notes that the volume stops being where the graph lives and leaves it explicitly open whether the volume is needed for anything else. Answer that question after the rebuild, never before it.
2. **The provider switch is one reversible change.** Reverting the environment value must be sufficient to point back at the embedded store, without also requiring a code revert. That means the loop from gate 5 has to keep a single-context path reachable by configuration, or the whole change has to be revertible as one merge. Decide which of those two before writing the code, not after.
3. **Structured Knowledge is intact.** The rebuild is only possible because the graph is a projection (ADR-0010). If the source that the re-cognify reads from is itself incomplete, then reverting the provider does not restore the system, it just returns to an old graph while the new one is empty, and the two are not reconcilable. Confirm the source is complete before the switch, not after the failure.
4. **The mid-migration state is announced.** Before the switch, every surface that will report a green empty result during the window is listed and the people who read those surfaces are told what the window looks like and when it ends. Otherwise the failure that looks like success (§1.2) gets discovered by someone who trusts it.
5. **The window has a stated end.** A half-migrated node is a node whose maintenance tooling reports clean while fossils accumulate (§1.5). If the re-cognify cannot start within the planned window, roll back to the embedded store rather than leaving the graph empty and waiting.

---

## 4. What this runbook does not cover

- Revisiting [ADR-0015](../../adr/0015-one-process-owns-the-graph.md). The migration makes a second worker and moving the **Learning Process** out of the web loop possible. Both are separate changes with their own verification and neither rides this one.
- The `/data` volume's fate after the rebuild. Open by ADR-0020, and gated on rollback condition 1 above.
- Issue #153, the evolve scheduler resetting its interval on every deploy. It is not part of this project, but it interacts with it: a scheduler that does not fire during the migration window means the `skipped: empty_graph` signal from §1.2 is never even produced, so the absence of that signal proves nothing while #153 is open.
- The six Dependabot vulnerability alerts on the default branch, three high and three moderate. **VERIFIED** by `gh api repos/masumi-network/Citadel/dependabot/alerts --paginate` on 2026-08-04. They are unrelated to this project and should not be sequenced behind it.

## 5. Related records

- [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md), the decision this runbook executes.
- [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md), which gate 6 depends on.
- [ADR-0009](../../adr/0009-mesh-read-isolation-presence-vs-content.md), [ADR-0010](../../adr/0010-structured-knowledge-durable-source-of-truth.md), [ADR-0015](../../adr/0015-one-process-owns-the-graph.md), [ADR-0018](../../adr/0018-corpus-totals-are-authoritative-not-uptime.md).
- [The substrate design session](2026-08-03-citadel-substrate-design.md), §6, which is the source of truth for why this project exists.
- `CONTEXT.md` for every bolded term above.
