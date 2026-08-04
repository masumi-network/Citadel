# Activity Counters Are Named By Their Scope, And One Event Is One Counter

- Status: Accepted
- Date: 2026-08-03
- Relates to: [ADR-0018](0018-corpus-totals-are-authoritative-not-uptime.md),
  [ADR-0015](0015-one-process-owns-the-graph.md). Amends ADR-0018's consequence
  that the activity counters "stay where they are".
- Fixes: #196, #197.

ADR-0018 fixed the corpus totals: `/api/mesh` reports authoritative
`documents`/`indexed_chunks` and the in-memory values moved under
`stats.since_restart`. The activity counters did not get the same treatment.
`searches`, `feedback`, `upgrades` and `errors` kept restart-scoped values at
the top level of the stats payload, where position implies "lifetime total".
Measured live on 2026-08-02, a day with several redeploys: `stats.searches` and
`stats.since_restart.searches` were both 294 — the top-level number was the
since-restart number under a totals-shaped name. Any consumer reading it as a
total repeats the exact mistake ADR-0018 fixed, one row down.

A second name told a worse lie. `failed_chunks` was incremented once per event
whose timeline status is `failed`, and the only such events are the ones
`record_error` emits — the same code path that increments `errors`. The two
fields could never diverge, and neither counted chunks. A production reading of
"12 failed chunks" decomposed to 11 search timeouts plus one
`DatasetNotFoundError`: zero ingestion failures, presented as twelve. The
dashboard even summed the two fields, alerting on every error twice.

**Decision**

- Activity counters stay process-scoped and in-memory. **Vault Activity** is
  defined (CONTEXT.md) as ephemeral — "it resets with the service" — and
  ADR-0018 already rejected persisting mesh counters: a durable counter file is
  a second bookkeeping store that can drift from the events it claims to count,
  and it would add a write to every search. If lifetime activity figures are
  ever wanted, derive them from a durable record (the feedback index, the event
  log), not from a mutable counter.
- Scope is expressed by position: restart-scoped counters (`searches`,
  `feedback`, `upgrades`, `errors`, `pending_chunks`) are published ONLY under
  `stats.since_restart`. The top level of `stats` carries corpus figures and
  durable-state-derived values (`last_indexed_at` is seeded from source state at
  rehydrate).
- `since_restart.started_at` reports when the process began counting, so every
  consumer can see the window and a quiet vault is distinguishable from a
  recent deploy.
- `failed_chunks` is removed, not renamed: it was `errors` surfaced twice. The
  surviving counter is `errors` — failed operation events of any kind, since
  restart. The events-timeline stats block reports `errors` in its place. A
  real chunk-failure counter may exist someday, but only fed by the indexing
  path itself; until something counts chunks, nothing may claim to.

**Consequences**

- `/api/mesh` and `/api/indexes` no longer publish top-level `searches`,
  `feedback`, `upgrades`, `errors`, `pending_chunks`, or `failed_chunks`. This
  is a deliberate breaking change: a consumer that read them as totals was
  getting wrong numbers, and a missing field fails loudly where a wrong value
  fails silently.
- The dashboard and `citadel status` read the activity counters from
  `since_restart` and label them with their window.
- The rule generalizes ADR-0018's: a counter's name and position must state its
  scope, and one underlying event may surface as one counter.
