# ADR-0015: one process owns the graph

- Status: Accepted
- Date: 2026-07-29
- Relates to: ADR-0010 (Structured Knowledge is the durable source of truth),
  ADR-0005 (self-evolving memory, policy-gated ingestion).
- Supersedes the two-phase subprocess split introduced for #47.

## Context

cognee opens the Kuzu graph read-write with an exclusive OS file lock and holds
it for the lifetime of whichever process opens it. There is no read-only mode in
practice: `read_only=True` exists on the underlying engine but cognee never
passes it, and forcing it through gets past the lock and then fails in
`_ensure_schema` with `Cannot execute write operations in a read-only database`.

Two facts follow, and both were learned the hard way.

**A second process cannot open the graph at all.** Not for writes, not for
reads. The evolve pipeline used to run its heavy stages in a subprocess
specifically to "free the Kuzu lock on exit" (`kb/server.py:253`). That reasoning
was backwards: the subprocess was the one locked out, because the web process had
already opened the graph and never let go. `github_sync` and `linear_sync`
therefore failed every hour for weeks with `Could not set lock on file:
cognee_graph_kuzu (Lock is held by PID 44)` (#88, #46).

**`cognee.add` is a graph opener, not just a relational write.**
`cognee/modules/pipelines/operations/run_tasks.py:166` calls `get_graph_engine()`
at the end of every pipeline run, purely to evaluate
`hasattr(graph_engine, "push_to_s3")`. The comment in `kb/cognee_client.py`
asserting that add "does NOT touch the Kuzu graph" was false, and the add-only
guard built on it sat fourteen lines after the call that actually threw.

The mitigation was therefore aimed at the wrong operation and positioned after
the failure it was meant to prevent.

## Decision

**One process owns the graph. Everything that touches it runs in that process.**

For the deployed node that process is the FastAPI web service, because it is the
one serving reads and it opens the graph first.

Consequences that follow directly, all of which had previously been discovered
separately and painfully:

- **The evolve pipeline runs in the web event loop**, not a subprocess. Phase 1
  stages are awaited on the caller's loop under the existing writer lock; Phase 2
  cognify follows in the same loop.
- **The loop-binding workaround is unnecessary.** #69 existed because stages ran
  on throwaway loops in a separate process and cognee binds its cached async
  engine to the first loop that touches it. One process and one loop means it
  binds once. `stage_loop()` remains for the standalone CLI entrypoints, which
  are genuinely separate processes and never run while the node is up.
- **`--workers 2` is not available.** A second uvicorn worker cannot open the
  graph. Horizontal scaling of the node is not a configuration change; it
  requires a graph backend that supports concurrent openers.
- **A separate evolve cron service is wrong**, even though it mounts the same
  `/data` volume and looks reasonable. `docs/operations.md` recommended exactly
  that and now carries a warning against it.

## Consequences

**Accepted cost: the web process does heavy work.** A full pass runs roughly two
minutes of GitHub fetching, ingest and cognify on the same loop that serves
requests. Measured during a live pass, `/healthz` stayed at ~150 ms across three
probes, because the blocking calls are inside `asyncio.to_thread` and the rest
are real awaits. That margin is not guaranteed as the corpus grows, and #105 is
the standing issue for loop starvation.

**Accepted cost: the writer lock is held for the length of a pass.** Interactive
ingest cognify is deferred for that window each cycle. Searches are unaffected,
because reads do not take the writer lock.

**Add-only mode is now a context variable, not an environment variable.** On a
subprocess an env var was the right tool. In the web process it is process-wide,
so a teammate's ingest arriving mid-pass would silently go add-only too. A
context variable is scoped to the task tree and propagates into child tasks and
`asyncio.to_thread`, which is exactly the reach the stages need.

**This constraint is a property of the retrieval engine, not of Citadel.** It is
the clearest argument yet for ADR-0010: while cognee is the sole owner of the
graph, its locking model dictates Citadel's process architecture. If Structured
Knowledge becomes the durable source of truth and the index is a rebuildable
projection, the engine can be swapped for one that permits concurrent readers and
this ADR can be revisited.

## What would reverse this

A graph backend supporting concurrent openers (a server-mode Kuzu, Neo4j,
Memgraph, or cognee's own `kuzu-remote` adapter). At that point the web process
stops being the sole owner and the evolve pipeline can move back out, which is
also what unblocks running more than one uvicorn worker.

Not sufficient on its own: cognee's `SHARED_KUZU_LOCK` with Redis. It is the only
supported cross-process sharing mode today, but it adds an infrastructure
dependency and makes every query pay an open and close of the database, on the
interactive search path that #50 is already about.
