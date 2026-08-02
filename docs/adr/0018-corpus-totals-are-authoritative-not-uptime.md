# Corpus Totals Come From The Corpus, Not From Process Uptime

- Status: Accepted
- Date: 2026-07-31
- Relates to: [ADR-0015](0015-one-process-owns-the-graph.md).
- Amended by: [ADR-0019](0019-activity-counters-are-named-by-their-scope.md) —
  the activity counters no longer "stay where they are"; they publish only
  under `since_restart`.

Any figure a surface presents as a size of the **Organization Vault** must be
read from the vault. A counter that resets when the process restarts may be
reported as activity, and must be labelled as such.

**Vault Activity** is defined in CONTEXT.md as ephemeral and restart-transient,
and `MeshState` implements it correctly: in-memory counters, deliberately not
reseeded on rehydrate so a later sync cannot double-count. The error was in what
was published from them. `documents` and `indexed_chunks` were served from those
same in-memory counters through `/api/indexes`, where they read as corpus
totals.

On a service that redeploys on every merge to `main`, those totals were
near-permanently near zero. Measured on 2026-07-31: `/api/indexes` reported
`documents: 1` and `indexed_chunks: 1` against a real 15641 indexed / 308
tracked, and `nodes: 547` against a graph of 15754 nodes and 110268 edges —
understated by roughly four orders of magnitude, in the direction that makes a
working vault look dead. Watched live across one benchmark run, `searches` went
0 → 386 and `latest_event_id` 3 → 519 while `documents` and `indexed_chunks`
stayed at 1 throughout: the counters were measuring uptime, and only uptime.

`_corpus_health()` already computes the authoritative figures, and `/readyz` and
`citadel status` already report them. Nothing new is derived; the projection
just stops publishing its own accumulators as totals.

**Considered Options**

- **Persist the mesh counters:** contradicts ADR-0015 and the **Vault Activity**
  definition, and reintroduces the double-counting the no-reseed rule exists to
  prevent. The counters are not wrong; their labelling was.
- **Seed the counters from source state at rehydrate:** the option the rehydrate
  docstring already rejects, for the same double-counting reason.
- **Leave the endpoint and fix the dashboard client:** the wrong seam. Every
  consumer of `/api/indexes` would need the same correction, and a public
  landing page reading it would advertise a dead product.
- **Report authoritative totals, keep uptime values under `since_restart`
  (chosen):** each number says what it measures.

**Consequences**

- `/api/indexes` `stats.nodes`, `stats.documents` and `stats.indexed_chunks` are
  corpus figures. `stats.since_restart` carries the in-memory values, including
  the projection's own node and edge counts.
- Genuinely uptime-scoped counters — `searches`, `feedback`, `upgrades`,
  `errors` — stay where they are and keep their meaning.
- `_corpus_health()` fails soft and may return null counts; the snapshot then
  falls back to the in-memory values rather than publishing null. A test pins
  that, because a surface reporting `null` size is a worse failure than one
  reporting a stale size.
- Any future surface quoting a vault size takes it from the same source. The
  rule is about where a number comes from, not about this one endpoint.
