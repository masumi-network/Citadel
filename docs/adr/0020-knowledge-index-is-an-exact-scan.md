# The Knowledge Index Is An Exact Scan, And Backend Changes Are Measurement-Gated

- Status: Accepted
- Date: 2026-08-04
- Relates to: [ADR-0010](0010-structured-knowledge-durable-source-of-truth.md),
  which mandated that a retrieval backend earn its place "by measurement (a
  frozen-fixture eval harness)". This ADR defines that harness and records what
  the current backend actually does.
- Also relates to: [ADR-0009](0009-mesh-read-isolation-presence-vs-content.md),
  [ADR-0015](0015-one-process-owns-the-graph.md).

The **Knowledge Index** is assumed by nearly everyone who reads this codebase to
be an approximate-nearest-neighbour index. It is not. VERIFIED against the
installed cognee 1.2.2 (`cognee-1.2.2.dist-info`):

- The strings `hnsw` and `ivfflat` appear nowhere in the installed cognee
  package: not in the adapter, not in `cognee/alembic`, not in
  `cognee/migration`. The only `CREATE INDEX` occurrences are test fixtures
  (`cognee/tests/test_data/Chinook_PostgreSql.sql`) and a graph-store Cypher
  index on node id (`cognee/modules/migration/formats.py:121`); neither touches
  a vector table. Every SQLAlchemy `Index()` in the package is on cache tables
  or graph tables. The `pgvector` Python package exposes HNSW/IVFFlat classes
  only under `pgvector/django/indexes.py`; its SQLAlchemy surface defines none,
  and the `Vector()` column type carries no index of its own.
- `create_collection()` (`PGVectorAdapter.py:218`) builds a table whose columns
  are `id` (primary key), `payload = Column(JSON)`, and
  `vector = Column(Vector(vector_size))` (`:245-247`). The method whose name
  suggests an index, `create_vector_index()` (`:381-383`), calls
  `create_collection()`. It creates a *table*.
- `search()` (`:463`) issues `select(...).order_by(cosine_distance(query_vector))`
  with a `LIMIT`.

The **vector column** is unindexed (the table does have an implicit primary-key
btree, which no distance query can use). An unindexed vector column under
`ORDER BY <distance>` gives the planner no ordered access path, so it computes a
distance per row and top-N heapsorts. **Every Citadel search is an exact full
scan of the whole chunk collection**, possibly a parallel scan, but a full one.
Two properties follow, and both are the opposite of what a reader would assume:

- Recall is 100% by construction. There is no approximation to lose.
- Latency is linear in row count. This is the real exposure, and it grows with
  the corpus rather than appearing as a failure.

Scope note: this describes what cognee creates. Whether someone has since added
an index to the production database by hand is NOT DETERMINED. No repo-side DDL
exists, and confirming prod requires querying it.

A second absence compounds it. The vector layer is never given a filter.
VERIFIED: the only search call Citadel makes is
`{"query_text", "datasets": [dataset], "session_id", "top_k"}`
(`kb/cognee_client.py:654-658`); with `ENABLE_BACKEND_ACCESS_CONTROL=false`
(`.env.example:237`) cognee's `search.py:341-344` takes the else branch and sets
no dataset context, so `node_name` arrives at `PGVectorAdapter.py:529-534` as
`None` and the adapter's `belongs_to_set` filter branch (`:505`, literal at
`:524`) is unreachable. Every search therefore scans every **Seat**'s chunks in
one collection.

CORRECTED 2026-08-04, later the same day. An earlier version of this paragraph
concluded that "dataset scoping is not resolved anywhere: not by the Index and
not by cognee above it". **That was wrong, and a live measurement refutes it.**
One `citadel_search` produced three per-dataset searches returning 2, 1 and 0
results respectively, and the merged hits carried different document ids from
different datasets. Byte-identical queries against one unfiltered collection
would return the same rows. They did not, so something above the adapter does
resolve the dataset.

What survives is narrower and still worth recording: the **Index** itself is
given no filter and scans the whole collection regardless of dataset, so
whatever scoping exists is applied above it rather than by it. The error was
inferring, from the absence of a filter argument at one boundary, that no
filtering happened at any boundary. Absence of evidence at the layer you
inspected is not evidence of absence elsewhere.

A third property multiplies both. **One user query issues N full scans, not
one.** VERIFIED: `kb/server.py:1969-1979` fans out with `asyncio.gather`, one
`citadel.search()` per readable dataset (three for a writer seat), and merges
the results above. The comment there reasons that concurrency makes a
two-dataset search "cost ~one recall, not two"; that is true of wall-clock and
false of database work. The scans are concurrent, unfiltered, and against the
same collection, so they contend for the same buffers and CPU. Since the dataset
parameter never reaches the vector layer, an earlier version of this ADR
suspected the N scans might return identical rows. **Measured 2026-08-04: they
do not.** One search returned 2, 1 and 0 results across its three arms, with
different document ids. The scans are redundant in *work* (each is a full scan
of the same collection) but not in *result*. Both halves of that matter: the
N-fold cost is real, and the arms cannot simply be merged into one without
replacing whatever scoping currently distinguishes them.

Neither property was chosen. All three were inherited from a default and have
gone unrecorded, which is why the corpus can grow into a latency problem that no
counter reports and no code review would surface. The defect is an absence.

**Decision**

- The exact-scan property is recorded as a known constraint of the current
  **Knowledge Index**, not a bug to be silently "fixed" by whoever notices it
  next. Adding an ANN index changes recall from exact to approximate; that is a
  trade, and it is made by measurement, not by reflex.
- **Two interventions are tried before any index arm, because both dominate the
  index arms on every axis.** Neither changes the store:
  1. **Collapse the query fan-out.** N concurrent full scans per user query is
     an N-fold multiplier on every latency number in this document, and it needs
     no index and no new service. AMENDED after measurement: an earlier version
     claimed this "costs no recall to remove". It is not that simple. The arms
     return different results (2, 1 and 0 on the measured query), so collapsing
     them means replacing whatever distinguishes them, not deleting a redundant
     loop. The win is still real and still the cheapest available, but it is a
     change with a correctness question attached rather than pure waste
     removal.
  2. **Filter or partition by dataset.** This shrinks each scan by roughly the
     dataset count and keeps recall exact. It was previously demoted to a
     "cost-to-fix probe", which was wrong: a rule that measures only index arms
     can select HNSW when a filter beats it on every axis.
     AMENDED by [ADR-0021](0021-seat-nodes-are-mutually-readable.md), which
     withdraws BOTH of this intervention's justifications for `org-work`
     content. The security ground goes first: that content is meant to be
     mutually readable, so a filter no longer closes an isolation gap for it.
     The latency ground goes with it, and an earlier version of this amendment
     missed that. A dataset filter shrinks a scan only when the reader's
     entitlement is narrower than the collection; once `org-work` is readable by
     everyone, filtering to it removes almost nothing. Both justifications
     survive ONLY for the private partition ADR-0021 requires, whose reader set
     genuinely is narrower. Consequence for this ADR's own numbers: the
     post-flip search arity is unsettled (ADR-0021 puts the floor at two scans,
     not one), and every latency figure here assumes an arity nobody has fixed.
     The arity must be decided before this harness's results mean anything.
  Sequencing matters and runs this way round: adopting HNSW first makes filtered
  retrieval harder, since a filtered ANN search must over-fetch and loses recall
  in a way a filtered exact scan does not.
- Only if those are exhausted does the **Knowledge Index** backend question
  arise, gated by the frozen-fixture eval harness ADR-0010 mandated. It compares
  three arms over identical vectors: **pgvector-exact** (today's baseline),
  **pgvector-HNSW** (same service, one migration), and **qdrant-HNSW** (new
  service, community adapter). pgvector-HNSW is a first-class candidate, not a
  strawman: if it closes the gap, a second datastore has to justify itself on
  capability rather than speed. Pre-registered: **arms 2 and 3 do not run unless
  arm 1 breaches the gate.**
- **Arms are compared at matched recall, with a pre-registered floor.** Without
  one, "faster" is undefined, since `ef` can be tuned downward until any arm
  wins.
  The floor is recall@10 ≥ 0.98 against exhaustive cosine; latency below that
  floor is not a result. Ground truth is computed in float32 to match pgvector's
  storage, because a float64 reference reorders ties and would score the exact
  arm at something like 0.997 against itself. Each arm publishes
  `EXPLAIN (ANALYZE, BUFFERS)`, since an HNSW index built with a mismatched
  opclass silently falls back to a sequential scan and otherwise looks
  legitimate. The qdrant arm crosses a network boundary the pgvector arms do
  not; "store-level" must be defined identically for all three or the harness
  measures Railway topology.
- **The frozen fixture is a fixture and never a migration path.** It is a
  read-only copy of the vectors currently in the Index, held constant so that any
  measured difference is attributable to the index structure and not to
  re-embedding. Populating a production **Knowledge Index** by copying vectors
  sideways between backends is forbidden: it would re-establish the inversion
  ADR-0010 exists to prevent, where the retrieval engine holds the only durable
  copy. A real backend change re-projects from **Structured Knowledge**.
- Extraction runs under a dedicated Postgres role holding `SELECT` only, using
  plain SQL. cognee is not loaded in the extraction path: it opens its stores
  read-write and takes the graph lock ADR-0015 describes, and reaching for it to
  perform a "read" is how this repo previously wrote to production from a test.
  The guarantee is enforced by the server, not by the script's good intentions.
- The harness scope is one collection, **conditional on the configured search
  mode**. VERIFIED: `_configured_search_type` (`kb/cognee_client.py:426`) reads
  `CITADEL_COGNEE_SEARCH_TYPE`, defaulting to `CHUNKS`, which `.env.example:31`
  also sets explicitly; that reaches cognee's `ChunksRetriever`, which selects
  its collection at `chunks_retriever.py:117`. Other values route elsewhere:
  `SUMMARIES` to `SummariesRetriever`, `CHUNKS_LEXICAL` to `BM25ChunksRetriever`,
  and `AUTO`/`RECALL` to `None`, which sends searches down `cognee.recall(...)`
  at `kb/cognee_client.py:627` entirely. One-collection scope is a property of
  the mode we run, not of Citadel in general. (`kb/cognee_client.py:1574`'s
  `_CHUNK_VECTOR_COLLECTION` names the same collection but is the drill-down
  fallback constant, not the search path; it is not evidence for this claim.)
  The **Knowledge Mesh** is out of scope entirely, since ADR-0010 keeps cognee as the
  Mesh builder regardless of the retrieval outcome.
- Ground truth for recall is exhaustive cosine over the frozen fixture. It
  requires no human labelling and it is exact by definition. It replaces the
  prior recall figure, which was invalid: REPORTED from the 2026-08-03 audit,
  recall@5 of 0.95 matched on the document's path header line in 56 of 61 cases,
  so it measured header formatting rather than retrieval.
- **Recall is not a decision input.** The incumbent scores 100% by definition,
  so a rule that weighs recall across arms cannot be lost by the incumbent and
  is decision-theatre. Recall's role here is to *price the cost of switching*,
  meaning how much exactness an ANN arm gives up, and it is reported alongside latency
  rather than traded against it. Only latency and capability can decide.
- **The arms are parameter families, not points.** `m`, `ef_construct` and `ef`
  trade recall against latency continuously (qdrant's documented defaults: `m`
  16, `ef_construct` 100, `ef` defaulting to `ef_construct`). Comparing one
  setting of pgvector-HNSW against one setting of qdrant-HNSW is riggable in
  either direction and means little. The comparison is a Pareto frontier: sweep
  `ef` at fixed `m`/`ef_construct` and compare recall-versus-latency curves.
  A single-point comparison may not be published as a result.
- **The query set is drawn from real logged queries**, which exist. See
  Consequences: cognee has been writing every query to a Postgres table since
  before this ADR. Corpus-derived queries are the fallback only if that table
  proves too sparse, and if used their bias must be stated wherever the numbers
  appear, because corpus-derived queries occupy tighter embedding neighbourhoods than
  real ones, so ANN recall reads optimistic.
- Filtered search is measured as a cost-to-fix probe, not as a comparison of two
  implementations. Neither backend filters today, so a head-to-head would compare
  two things that do not run. The probe answers one question per backend: what
  would a dataset filter cost? For pgvector that requires an expression index
  matching the runtime `cast(payload, JSONB)`, since `payload` is `Column(JSON)`.
  For qdrant the documented mechanism is an `is_tenant` payload index on a
  `keyword` or `uuid` field. The output attaches to the isolation work, not to a
  winner.
- **Corpus growth is extrapolated, not faked.** The obvious method,
  replicating the fixture to 2x/4x, is invalid and biased. Duplicated vectors sit at
  distance 0 from each other, which collapses an HNSW proximity graph into
  cliques that consume the `m` neighbour budget and destroy the long-range links
  the index navigates by, and it makes top-k a handful of distinct documents
  repeated with arbitrary tie ordering. It also flatters the incumbent, since
  identical rows compress better and are more cache-friendly than real ones.
  The bias runs toward "change nothing", which is already this rule's default.
  Instead: measure every arm at 1x on real vectors, and extrapolate the
  exact-scan arm analytically, which is defensible precisely because a full scan
  is linear in rows. Validate that linearity with one perturbed 2x run (small
  Gaussian noise on the copies), since linearity survives duplication where HNSW
  does not. The HNSW arms are never projected from duplicated data; if the gate
  breaches, a real synthetic corpus is built then.
- Three thresholds, not one, because a target and a tripwire are different
  things. **Target: p95 ≤ 25ms** store-level, which is what a competent ANN index does at
  this scale, deliberately derived from nothing that can move, triggering no
  action. **Ceiling: p95 ≥ 400ms** at any corpus size, meaning act immediately and do not
  wait for a projection. **Gate: store p95 ≥ 100ms at CURRENT corpus size**, with
  the production fan-out multiplier applied, comparing p95 against p95.
  An earlier draft set the gate at 150ms at a projected 4x and justified it as
  ~2% of a 6-9s end-to-end. That reasoning is void twice over. REPORTED from the
  2026-08-03 production bench, end-to-end p50 is 311-627ms by surface, so 150ms
  would have been 24-48% of the entire user-visible request rather than 2%. And
  since the store may already account for most of that, the gate belongs at
  today's corpus size, not at a projection. The permissive-gate asymmetry
  inverts with it: when the store *is* the latency, a rule tolerating 150ms
  blesses a regression users can feel, so being wrong stops being cheap. The
  earlier draft also compared a store p95 against an end-to-end p50, which is
  not a ratio. All three numbers stay provisional until the measurements in
  Consequences run.

**Consequences**

- Search latency is a function of corpus size with no inflection point to warn
  us. Until the harness runs, we do not know how much headroom remains. The
  first deliverable is a row count: `SELECT count(*)` on the chunk collection
  through the read-only role. Every threshold above is provisional on it, and
  the reasoning that produced them assumed a scale nobody has measured.
- **Correction, recorded rather than silently fixed: this ADR first claimed that
  search telemetry never persists query text and that no real query set existed.
  That was wrong.** cognee's `log_query`
  (`cognee/modules/search/operations/log_query.py:9-24`) constructs
  `Query(text=query_text, ...)` and commits it through the relational engine
  (our Postgres, `.env.example:244`) into a `queries` table with a `text`
  column (`cognee/modules/search/models/Query.py:8,12`). It is the first
  statement of cognee's search body (`cognee/modules/search/methods/search.py:79`),
  reached from `kb/cognee_client.py:663`. Its only gate is
  `COGNEE_LOG_SEARCH_HISTORY`, which defaults to `"true"` (`log_query.py:7`) and
  which this repo never sets. Upstream even ships an index migration for that
  table (`cognee/alembic/versions/b2c3d4e5f6a7_add_search_history_indexes.py`).
  The error was not a missing file: `kb/cognee_client.py:650-652` already states
  in a comment that "the per-search writes that remain (log_query/log_result
  history) are unconditional". The earlier finding examined Citadel's own
  telemetry path, found it ephemeral (which it is: `kb/mesh.py:449-458` writes
  no `query` key and `MeshState` is in-process), then generalised from Citadel's
  code to the system's behaviour without checking the engine underneath it. Two
  decisions were built on the false claim and are reversed above.
- What remains NOT DETERMINED about the query set: whether the `queries` table
  has usable rows in production, and over what period. The settling measurement
  is one line: `SELECT count(*), min(created_at), max(created_at) FROM queries;`
  Run it before writing the harness, because it decides whether the query set is
  real or corpus-derived.
- That table is also a privacy surface nobody chose. Every search string any
  seat has ever issued sits in the org's Postgres, unredacted, attributed by
  `user_id`, with no retention policy, while Citadel's own telemetry path was
  carefully built to strip query text (`kb/search_feedback.py`). Using it as a
  benchmark input is reasonable; leaving it undocumented is not. Out of scope
  here, and it needs its own decision.
- **The end-to-end latency figure this ADR originally reasoned from is stale.**
  It claimed p50 of 6-9s dominated by one AUTO_FEEDBACK LLM call per query. That
  measurement predates `kb/cognee_client.py:423`, which does
  `os.environ.setdefault("AUTO_FEEDBACK", "false")`; the commit is an ancestor of
  HEAD (VERIFIED via `git merge-base --is-ancestor`), and the docstring at
  `:400-407` states the 6-9s was measured with the flag on. So the argument that
  "a store-level win is imperceptible against an LLM call" no longer stands on a
  live number. Whether production sets `AUTO_FEEDBACK=true` explicitly, which
  the setdefault permits, is NOT DETERMINED. Current end-to-end p50 must be
  re-measured before the gate threshold is set, and if the LLM call is genuinely
  gone, the store may now be a large fraction of search latency rather than a
  rounding error. That would raise the stakes of this whole exercise rather than
  lower them.
- Qdrant's multi-tenancy advantage is real but not currently reachable. cognee's
  adapter interface decides whether a filter is passed, and cognee passes none,
  so an `is_tenant` index would sit unused until Citadel's call path changes.
  The constraint lives upstream of the store: swapping the Index backend cannot
  by itself fix isolation.
- Qdrant is not a drop-in. VERIFIED: cognee 1.2.2 ships no qdrant adapter:
  `cognee/infrastructure/databases/vector/supported_databases.py` is literally
  `supported_databases = {}`, and `create_vector_engine.py` branches only on
  pgvector (`:207`), neptune_analytics (`:271`), and lancedb (`:299`); qdrant is
  a `cognee-community` adapter registered through `use_vector_adapter`
  (`vector/use_vector_adapter.py:4`). Our pin is
  `cognee[fastembed,postgres-binary]>=1.2.2,<1.3.0` (`requirements.txt:6`)
  because ADR-0009 dataset attribution reads cognee private internals, so the
  qdrant arm carries a version-coupling cost the pgvector-HNSW arm does not.
- **"Cheap to fix later" is an assumption, and the harness must measure it.**
  The reasoning behind a permissive gate is that `CREATE INDEX ... USING hnsw`
  stays available whenever we need it. That is NOT DETERMINED. A plain
  `CREATE INDEX` holds a lock that blocks writes for the whole build;
  `CONCURRENTLY` avoids that but runs longer and can leave an invalid index
  behind on failure; and HNSW build time and `maintenance_work_mem` headroom on
  our Railway Postgres are unmeasured. Index build cost is therefore a harness
  output, not a reassurance in this document. If the build turns out to be
  expensive or risky on a live table, the asymmetry argument that justifies a
  permissive gate weakens and the gate should tighten.
- **An HNSW index would live outside cognee's schema, so `citadel reindex`
  silently reverts it.** ADR-0010 makes drop-and-rebuild-from-Structured-
  Knowledge a first-class operation. An index we add by hand is not part of what
  cognee recreates, so a reindex would drop back to exact scan with no counter
  reporting the change, the same shape as the green-stage defect class. Whether
  cognee's `create_collection` drops an existing table is NOT DETERMINED and is
  a required check before any index arm is adopted. If we adopt HNSW, recreating
  it becomes part of the reindex procedure or the procedure is broken.
- **Measurement conditions are decisive and must be pre-registered, not chosen
  after.** Cold versus warm cache, host RAM, `shared_buffers`, Postgres and
  pgvector versions, parallel workers, and above all concurrency. REPORTED burst
  behaviour from the 2026-08-03 bench: 1 concurrent query 562ms, 4 → 1226ms,
  8 → 2264ms, then 429s. A solo-query p95 is not the quantity this decision
  needs, because the fan-out means a single user query is already several
  concurrent scans.
- **This ADR must be committed before the harness runs.** A pre-registered rule
  that lives in an uncommitted file or in a chat transcript can be revised after
  seeing the data, which is the failure the pre-registration exists to prevent.
  Thresholds, recall floor, host spec, percentiles, and cold/warm policy are all
  fixed at commit time.
- The `queries` table is a traffic log, not an eval set, and needs a published
  sampling rule before use: it is agent-issued rather than human, it repeats
  automated probes (one hot query will own p95 unless sampling is stratified by
  distinct text), it carries no relevance labels, and it is polluted by benchmark
  and UAT traffic that must be excluded by time window and `user_id`.
- Harness implementation note: `PGVectorAdapter.py:484-487` runs a `count(*)`
  full pass first when `limit` is `None`. Citadel always passes `top_k`
  (`kb/cognee_client.py:658`) so production never hits it, but a harness calling
  the adapter directly must set a limit or it will measure two scans as one.
