# Citadel Defines The Retrieval Interface; The Engine Is An Implementation

cognee is a library Citadel calls rather than a component Citadel contains.
`kb/cognee_client.py` is 1784 lines carrying 45 separate `import cognee` /
`from cognee ...` statements spread across 19 distinct functions and methods,
and most of them reach past the public API into `cognee.modules.data.models`,
`cognee.modules.users.methods`, `cognee.infrastructure.databases.graph`, and the
pgvector adapter. `requirements.txt` pins the dependency to the 1.2.x line for
precisely that reason: its comment records that [ADR-0009](0009-mesh-read-isolation-presence-vs-content.md)
dataset attribution "reads cognee PRIVATE internals ... that a 1.3.x could
move", and that the pin exists to stop the deploy platform pulling an unattended
bump on the next build. A pinned upstream whose private internals a caller
depends on is a hazard that grows with the number of instances running it: one
deployment is a support ticket, a fleet is an incident. **Citadel therefore
declares its own ingest and query interface, and cognee becomes the first
implementation behind it.** Ranking and provenance stamping live in Citadel's
layer, not upstream's.

The decisive evidence is that ranking cannot come from upstream at any version.
In the pinned release, `PGVectorAdapter.retrieve()` returns
`ScoredResult(..., score=0)` for every row
(`cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py:459`), and
`ChunksRetriever.get_completion_from_context()` hands back
`found_chunk.payload` only, dropping the vector engine's real cosine distance
before it can reach a caller. Both are still true in cognee 1.4.1, the current
upstream release: the hardcoded zero moves to line 464 of the same file and
`chunks_retriever.py` is byte-identical between the two versions, as is
`SearchResultPayload`, which carries `result_object`, `context`, `completion`,
and dataset identifiers, and no score field of any kind. The community pull
request that would have exposed a real result score and a `max_distance` cutoff
for chunks and summaries, topoteretes/cognee#2945, was closed on 2026-07-07
without being merged. Upgrading the pin does not fix this. Upgrading twice does
not fix it either.

So the interface is not architecture for its own sake. It is where scoring has
to live, because no upstream version puts a score where a caller can read one,
and it is where per-hit provenance belongs for the same reason: the payload that
crosses the boundary today carries no field that says where a hit came from, so
[ADR-0017](0017-structural-provenance-outranks-inherited-trust.md) had to
recover origin from a structural header written into the body text by a governed
sync. That is a workaround for a missing interface field. `CONTEXT.md` already
states the boundary in words, in the **Structured Knowledge** glossary entry:
the **Knowledge Index** and **Knowledge Mesh** "are rebuildable projections of
it, and the retrieval engine that produces them is replaceable." This ADR turns
that sentence into a code boundary.
[ADR-0010](0010-structured-knowledge-durable-source-of-truth.md) named a
`RetrievalBackend` interface and made the engine swappable "in principle"; no
such type exists in `kb/` today, which is why in principle is where it stayed.

**Considered Options**

- **Keep calling cognee directly and fix the output defects in place:** cheapest
  in the short run, and it is what the codebase does now. It fails on the
  evidence above. A score that upstream never emits cannot be patched into a
  return value at the call site, so every caller would keep inventing its own
  relevance signal from whatever the payload happens to contain. It also leaves
  45 import sites, several of them on private paths, as the surface a version
  bump has to be validated against.
- **Replace the engine outright now:** removes the pin, the private-internals
  dependency, and the single-writer constraint
  ([ADR-0015](0015-one-process-owns-the-graph.md)) in one move. Rejected on
  sequencing, not on merit. There is no measurement yet that says a replacement
  retrieves better than what runs today, and ADR-0010 already committed to
  deciding that by a frozen-fixture eval rather than by argument. Replacing
  first and measuring afterwards inverts that.
- **Wrap it behind an interface Citadel owns, then decide (chosen):** costs one
  adapter layer and a period where the interface has exactly one implementation,
  which looks like ceremony until the second one arrives. It buys the thing
  neither other option does: a place to put scoring and provenance that survives
  a change of engine, and a single boundary for the pin to move behind.
- **Run two retrieval paths, fuzzy for memory and deterministic lookup for
  evidence:** attractive, because a **Source Snapshot** cited in a report needs
  exact resolution and a memory question does not. Rejected as a starting point,
  kept as a target. Two paths without a shared interface is two integrations,
  and the deterministic path is a special case of the interface rather than a
  parallel system. It becomes a second implementation once the interface exists.

**Consequences**

- The interface exposes four things and is judged on them: **ingest** (accept a
  document with its identity and dataset, report what happened to it), **query**
  (a query plus scope, returning hits), **provenance** (each hit says which
  source, which dataset, and which snapshot it came from, as fields rather than
  as prose in the body), and **scores** (each hit carries a relevance value with
  a named derivation, so a consumer can tell a retriever distance from a lexical
  overlap estimate).
- The interface deliberately does not expose engine-native objects, graph engine
  handles, dataset row models, or any type imported from the implementation.
  Nothing above the boundary may learn which engine is behind it. Concretely,
  new code importing `cognee` outside the adapter is forbidden, and the one
  remaining direct import outside the client (`kb/server.py:2421`) moves behind
  it.
- A second implementation becomes benchmarkable rather than hypothetical. BM25
  over **Structured Knowledge**, the alternative ADR-0010 deferred to
  measurement, becomes a class that satisfies the same interface and runs
  against the same fixtures, instead of a rewrite that has to be finished before
  it can be compared.
- Upgrading the pinned dependency becomes a change behind one boundary rather
  than across many. The boot self-check `assert_cognee_dataset_api()` guards one
  surface instead of standing in for a review of every call site, and the
  question "does this version still work" becomes "does the adapter still pass
  its contract tests".
- Ranking quality becomes Citadel's responsibility, and therefore Citadel's to
  measure and to publish. `kb/search_format.py` is currently honest about this:
  it computes lexical overlap, labels it as exactly that, and reports
  `retriever_scores_available: false`. Under the interface the flag stops being
  a permanent property of the system and starts being a property of an
  implementation, which is a thing that can be fixed by writing one.
- Provenance stamping happens at the boundary, at the moment a hit crosses it,
  which is the only place that knows both the retrieved item and the scope the
  request was made under. ADR-0017's structural header keeps working and stops
  being the strongest origin signal available.
- Engine constraints become declared capabilities instead of assumptions baked
  into unrelated code. ADR-0015 exists because a locking property of the engine
  dictated Citadel's process architecture. An implementation that permits
  concurrent openers should be able to say so through the interface, and an
  operator should not have to read the retrieval library to find out whether a
  second worker is possible.
- Accepted cost: an interface defined too generously becomes the union of every
  engine's features and constrains nothing. It stays narrow, and a capability
  only one implementation can satisfy stays out of it until a second one needs
  the same thing. The **Knowledge Mesh** builder is out of scope here; ADR-0010
  keeps that duty with cognee regardless of the retrieval outcome, so it is a
  separate seam.
