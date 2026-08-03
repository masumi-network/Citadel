# A Total Is Named For What It Counts, And A Missing Number Is Not Zero

- Status: Accepted
- Date: 2026-08-03
- Relates to: [ADR-0018](0018-corpus-totals-are-authoritative-not-uptime.md),
  [ADR-0019](0019-activity-counters-are-named-by-their-scope.md). Amends
  ADR-0018's consequence that `stats.documents` and `stats.indexed_chunks` are
  corpus figures.

ADR-0018 moved the mesh totals onto authoritative corpus reads. ADR-0019 moved
the activity counters under `since_restart`. Three names survived both passes
without anyone checking what they were assigned, and a fourth number was being
read from the wrong field on the client.

`edges` was `len(self.edges)`: the in-memory graph projection, the same
expression already published honestly as `since_restart.projection_edges`,
sitting at the top level of `stats` beside authoritative corpus totals. Position
made it read as a corpus figure. Measured live on 2026-08-03 it was 5511 against
131843 real graph edges, off by 24x, in the direction that makes the vault look
small.

`indexed_chunks` was assigned `corpus["indexed_docs"]`, the same value as
`nodes`. The two fields were equal by construction and neither counted a chunk.

`documents` was assigned `corpus["tracked_sources"]`: tracked github repos plus
repo-content files plus linear issues, 318 live, against roughly 2876 rows in
the durable corpus store at the same moment. A name that promises a document
count delivered a source count.

Separately, the dashboard's "Notes you can read" tile had no honest source at
all. It preferred `/api/mesh` `stats.documents` (a source count) and fell back to
`/api/me/summary` `document_count` (Node-only, so it silently excludes Central).
`readable_document_count`, computed from `resolve_search_datasets(identity)` over
every dataset the caller can actually search, already existed on the same payload
and was already rendered by the Next port.

**Decision**

- A published total is named for what it counts. `documents` becomes
  `tracked_sources`, because that is what the underlying figure is. Nothing on
  this hot, reader-scoped surface can cheaply count document rows; the
  admin-scoped corpus census is where a real document total comes from.
- `indexed_chunks` is removed from the top level rather than renamed, the same
  call ADR-0019 made for `failed_chunks`: it was a second copy of `nodes`, and
  until something on this surface counts chunks, nothing may claim to. The
  restart-scoped accumulator `since_restart.indexed_chunks` is unaffected and
  keeps its meaning.
- `edges` reports the real graph total when one is available. `_corpus_health()`
  already reads the whole graph for `nodes`, so `edges` comes back in the same
  call at no extra cost. When the corpus read is degraded it falls back to the
  projection, exactly as `nodes` does, and `since_restart.projection_edges`
  continues to publish the projection under a name that says so.
- A number a surface cannot compute renders as an em-dash placeholder with a
  stated reason, never as `0`. Zero is a claim; absence is not. Both dashboards
  distinguish "the request failed" (`Unavailable`) from "the endpoint answered
  without the field" (`Not reported by this node yet`).
- A tile is fed by the field built for it. "Notes you can read" reads
  `readable_document_count`, not an adjacent number that happens to be present.

**Consequences**

- `/api/mesh` and `/api/indexes` no longer publish top-level `documents` or
  `indexed_chunks`. As in ADR-0019 this is a deliberate breaking change: a
  missing field fails loudly where a wrong value fails silently.
- `stats.edges` changes magnitude on deploy for anyone who normalized against
  the projection value. It was wrong before, not now.
- `tests/test_mesh_stats_readers.py` guards the renamed and removed names across
  every reader surface (`kb/static`, `web/src`, and the committed `kb/webui`
  export production serves) for the same reason it already guards the ADR-0019
  counters: `(stats || {}).documents` and `stats?.indexed_chunks` evaluate to
  `undefined`, coerce to `0`, and paint a plausible number. The scan is
  scope-aware, because both names stay correct under `since_restart`.
- The generalized rule now reads: a figure's name states what it counts, its
  position states its scope, one underlying event surfaces as one counter, and a
  figure that cannot be computed is absent rather than zero.
