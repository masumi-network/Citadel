# Citadel benchmark harness

Repeatable measurements for the Citadel node. Every number here is meant to be
re-runnable by someone who does not trust it, which is the bar for anything that
ends up on a public page.

Read-only. Nothing in this directory writes to the vault.

## Running it

```bash
export CITADEL_MCP_ACCESS_TOKEN=...          # a seat token with kb:search
export GITHUB_TOKEN=...                      # only for refreshing ground truth

python scripts/bench/fetch_ground_truth.py       # once, or when the corpus changes
python scripts/bench/search_bench.py lint        # validate the golden set offline
python scripts/bench/search_bench.py run --out scripts/bench/runs/latest.json
python scripts/bench/search_bench.py run --baseline scripts/bench/runs/latest.json   # later, for a delta
python scripts/bench/search_bench.py compare run_a.json run_b.json
python scripts/bench/search_bench.py report scripts/bench/runs/latest.json --markdown
```

Write run JSONs only under `scripts/bench/runs/`. That directory is
gitignored; a bare `runs/` from the repo root is not the same place, and a
run JSON enumerates every served hit identity, so it must never be committed
(see Baselines below).

Tracking issue for the harness: #122.

### What a first run needs

`lint` needs nothing but the repo. `run` needs, in this order:

1. **The credential.** `CITADEL_MCP_ACCESS_TOKEN`, falling back to
   `CITADEL_ACCESS_TOKEN`; nothing else is read. It must be a token that
   authenticates over **HTTPS** against the node's `POST /search` and (for the
   API fingerprint) `GET /api/state` and `GET /api/indexes`. A token that only
   works over the MCP transport is not enough, and the value in the repo's
   gitignored `.env` is stale and 401s. Without it: `CITADEL_MCP_ACCESS_TOKEN
   is not set`, exit 2.
2. **The node URL**, defaulting to
   `https://citadel-archive-production.up.railway.app`; override with
   `--node-url` or `CITADEL_NODE_URL`.
3. **The content fingerprint, to make the run comparable to a later one.**
   `--repo-state PATH` or `CITADEL_REPO_STATE_PATH`, else
   `./.citadel/repo_content_sync_state.json`. The authoritative copy is the
   node's own `/data/.cognee_system/repo_content_sync_state.json` (per
   `/api/sources`); the local file in this repo has an empty `files` map. A
   state file with an empty map records `sha256: null` with a reason, exactly
   like a missing file: a fingerprint over zero files is the sha256 of the
   empty string, identical on every such run, and the 2026-08-03 baseline
   recorded it and would have compared as "same corpus" while attesting
   nothing. Without a real state file the run still completes, but `compare`
   says NOT COMPARABLE on content and withholds deltas. Old run JSONs that
   still carry the empty-string sha are treated as unavailable by `compare`.
4. **For the corpus census: a token with `admin` or `audit:read`.** `run`
   walks `GET /api/corpus` (about 1.8 s over 3 pages at the 2026-08-03 corpus
   size) and records how many documents the index cannot reach. With a
   search-only token the census records `unavailable` and the run continues;
   nothing else degrades.

`--repeats N` costs N searches per question (105 questions in v5) and feeds only
latency and `hit_stability`. Start at `--repeats 1`.

## What it measures

| metric | meaning |
|---|---|
| `answer_recall_at_5` | **headline.** fraction of span questions where a verbatim body quote appears in the top 5 DISTINCT document identities |
| `raw_page_recall_at_5` | same check over the literal first 5 served slots; the gap to the headline is the duplication tax |
| `doc_recall_at_5` | the expected document appears in the top 5 distinct identities (path/linear identity or content overlap) |
| `mrr_body` | mean reciprocal effective rank of the first identity whose body answers |
| `header_credit_rate` | fraction of span questions the OLD header-based scorer passes where the body scorer fails; credit that was never an answer |
| `negative_hit_rate` | fraction of probes that registered a hit: a pattern probe whose scanner rule matched a served chunk, or a negative control whose out-of-corpus target came back. Anything above 0.0 is a finding to investigate, not a score to report |
| `duplicate_blob_rate_at_10` | mean (slots - distinct identities) / slots over the top 10 |
| `distinct_files_at_10` | mean distinct source files over the top 10 slots |
| `distinct_source_ratio_mean` | distinct RESOLVABLE source paths per page divided by ALL hits. 1.00 means every slot is a different document; lower means one document occupies slots the caller pays context for. A hit whose source path cannot be resolved counts against this exactly like a duplicate does |
| `distinct_source_ratio_resolvable_only` | the same ratio over hits whose source path resolved. Report both: the first is the pessimistic bound, this is what the resolvable evidence supports |
| `head_recall_at_5` | share of embedding-window pairs whose HEAD quote (from the first 2000 characters) retrieves its own document |
| `tail_recall_at_5` | the same for the TAIL quote (>= 40% depth) of the SAME document. Pessimistic: counts documents that may never have been indexed |
| `tail_recall_given_head_at_5` | **quote this one.** `tail_recall_at_5` restricted to documents whose head quote DID retrieve, so every document counted is one the index demonstrably holds |
| `window_penalty` | `head_recall_at_5 - tail_recall_at_5`: how much of a document stops being reachable purely because the text sits later in it |
| `rank_inversion_rate` | share of answers ranked BELOW a hit the node itself reports matched a strictly smaller share of query terms. Ranking, not retrieval |
| `chunks_with_unstable_trust_tier` | chunks served more than once at the same `content_sha256` that reported different `trust_tier` values |
| `hit_stability` | with `--repeats N`: agreement of repeat outcomes with attempt 1 |
| `latency p50/p95` | separate block; informational, never gates quality. **Client round-trip**, timed around `urlopen`, so it includes DNS, TCP, TLS and the network path from wherever you ran it. It is NOT server-side latency: on 2026-07-31 a run measuring 269ms from a laptop corresponded to 107-141ms in the Railway edge logs. Quote it as "round-trip from our client" and read server-side timing from the platform logs |

## How a hit is scored

1. Every hit resolves to an **identity**: `(source path, Blob sha)` for
   repo-content chunks (parsed only when the header starts the chunk),
   `linear:<ID>` for Linear notes, otherwise the hit's `document_id`. The
   ranked list is collapsed to distinct identities, first occurrence kept;
   `effective_rank` is the position in the collapsed list. Ten slots of one
   file under two blobs are two identities and can pass a question once.
2. A question with `answer_spans` passes only if one of its 1-4 verbatim body
   quotes appears in a retrieved chunk's body AFTER the sync header block
   (through the `---` separator, including any legacy `Retrieved:` line) and
   any leading `# Linear ...` line are stripped. Both sides are normalized:
   lowercase, whitespace collapsed, backticks and emphasis markers removed.
3. A header line quoted mid-body is body text. It creates no identity and no
   credit. The old scorer was fooled by exactly that, which is why
   `header_credit_rate` exists: it quantifies the old scorer's phantom credit
   instead of repeating it.

Quality always comes from the first attempt. `--repeats` feeds latency and
`hit_stability` only; there is no best-of-repeats path.

## The frozen set

The question set is FROZEN. `golden_questions.json` carries a `frozen` block
whose `questions_sha256` pins the canonical question list, and `lint`
recomputes it and fails when it moves. Editing a question is therefore a
deliberate re-freeze (update the pin, bump `version`, re-take the baseline),
never a silent drift between two runs that both call themselves the baseline.

Two hashes appear in a run's fingerprint and they are not the same thing.
`questions_sha256` is over the FILE bytes, so reindenting moves it.
`questions_pin` is over the canonical question list, so it moves only when a
question really changes. The pin says which frozen set a run answered, and it
is the authority `compare` gates on: two runs whose pin matches stay comparable
even if the file bytes differ, and the file difference is reported as a note.
A run predating the pin has none, so the file hash still gates there. `report`
prints the pin above the metric table, and says so plainly when a run carries
none.

The fingerprint also records `ground_truth`, a hash of the cached bodies the run
scored against. `ground_truth/` is gitignored and `fetch_ground_truth.py` pulls
each file from GitHub at HEAD with no ref, then skips anything already on disk,
so two machines bake in whatever HEAD was current the day they first fetched.
That cache is not decoration: it feeds `doc_rank`'s shingle fallback and
`legacy_rank`, so it moves `doc_recall_at_5` and `header_credit_rate`. `compare`
notes a difference rather than gating on it, because every run taken before the
key existed would otherwise be refused. Re-fetch by deleting the cache, not by
re-running the script over it.

## Embedding-window pairs

18 pairs, one per document, added in v5. Each pair quotes the SAME document
twice: once from inside the first 2000 characters (`window_head`), once from at
least 40% through it (`window_tail`). Both quotes are verbatim, and the quote IS
the query, which is the strongest retrieval signal available. `lint` proves each
quote unique across the CACHED ground-truth bodies, which is roughly 49 files,
never across the corpus: a second render of a file or an uncached document
quoting the same sentence would also score the pass. `run` counts that case as
`quality.answers_from_unexpected_documents`, so the blind spot is readable per
run rather than assumed away.

The control that makes a tail miss mean something: a tail quote qualifies only
when at least 3 of its distinctive terms are ABSENT from that document's head.
Without it, a passing tail could be the head embedding answering the query and
the comparison would prove nothing about how much of a document is reachable.

`lint` enforces the depth threshold, the head window and the novelty control on
a MEASURED offset (`body.find(quote)`), never on the fixture's declared
`source_offset_chars`, and it requires every declared novel term to be a term of
the quote itself. A declared offset that disagrees with the measured one is
reported as a note: `ground_truth/` is refetched from an unpinned upstream HEAD,
so a few characters of whitespace drift are ordinary and must not fail lint on
every machine that fetched on a different day.

Window questions are scored in their own block and are EXCLUDED from
`answer_recall_at_5`. They fail by design against current behaviour; folding
them into the headline would move it for a reason that has nothing to do with
the node changing.

That exclusion covers `span_rows` and nothing else. Precisely:
`answer_recall_at_5`, `raw_page_recall_at_5`, `mrr_body` and
`header_credit_rate` are unmoved by v5. `doc_recall_at_5`,
`duplicate_blob_rate_at_10`, `distinct_files_at_10`, both
`distinct_source_ratio` figures, `hit_stability` and `rank_inversion_rate` all
moved when the 36 window questions landed, and none of them is comparable
across the v5 boundary. `compare` is what enforces that: two runs answering
different question sets differ in `questions_pin` and are refused outright.

Read `tail_recall_given_head_at_5` rather than `tail_recall_at_5`. A document
that missed on BOTH sides may simply never have been indexed, so its tail miss
is not evidence about position. Conditioning on a head that retrieved keeps only
documents the index demonstrably holds.

What that conditioning does NOT control for: the pair holds the DOCUMENT
constant and replaces the QUERY entirely. Head and tail are different sentences
with different tokenisations facing different corpus competition, so a tail miss
is positional OR lexical and this measurement cannot separate the two. On the
2026-08-04 set the confound runs the other way in aggregate (head queries
averaged 7.1 extracted terms against the tails' 6.8, and faced more cached-body
competitors, 8.9 against 6.3), so it does not explain that day's 0 of 11.

Every published window rate is computed over COMPLETE pairs only, so
`head_recall_at_5`, `tail_recall_at_5`, `window_penalty` and the `pairs_complete`
printed as their sample count stay consistent by construction.

## Ranking is not retrieval

`answer_recall_at_5` asks whether the answering document came back.
`rank_inversion_rate` asks whether the node put it in the right place. Search
currently reports `retriever_scores_available: false` and orders by lexical term
overlap, so these are genuinely different questions and one number cannot answer
both.

An inversion is: a hit ranked ABOVE the one that verifiably contains the answer,
while the node ITSELF reports that hit matched a strictly smaller share of the
query terms (`_citadel.relevance.term_coverage`).

**What the path header can and cannot do here.** The answer SLOT is
header-immune: it is bound to a body span match with the sync header stripped,
so a hit whose only overlap is the path never becomes the answer.
`term_coverage` is NOT header-immune. The node computes it over a haystack that
includes each hit's own `path`, `source`, `url`, provenance fields and the sync
header still sitting in the chunk text (`kb/search_format.py`, `_hit_text`), and
every hit on a page carries a different path, so a filename moves either side of
the comparison: a decoy whose path matches three query terms stops being an
inversion, and an answer whose path matches becomes one. Both directions are
executed in `tests/test_search_bench.py`. Read `rank_inversion_rate` as the
node's published relevance signal disagreeing with the node's own ordering, not
as a body-only measure of relevance.

The rate blends the 36 verbatim-sentence window queries with the real questions,
while `answer_recall_at_5` deliberately keeps them apart. `run` therefore also
publishes `rank_inversion_rate_excluding_window` and
`rank_inversion_rate_window_only` with their own denominators, so a movement can
be attributed to a set instead of guessed at.

## The golden set

`golden_questions.json` v5. 39 non-window questions carry validated
`answer_spans`, plus 36 window questions (18 pairs); 22
Linear questions (`l01-l22`) are explicitly unconverted
(`answer_spans_unconverted_reason`) because `fetch_ground_truth.py` skips
`linear:` targets, so there is no cached body to quote. They count for
`doc_recall_at_5` but are EXCLUDED from `answer_recall_at_5`, never silently
counted. 8 questions are probes (`expected_recall: 0`, below).

`lint` validates every span offline against `ground_truth/`: present in the
cached body, absent from the sync header and the document's first line (a
head-line match is title credit, not an answer), absent from the question
text, and unique across every other cached body. The "absent from the question
text" rule is waived for window questions ONLY, because a window question is
its verbatim quote by design; every other question still rejects a span that
appears in its own query. It also validates probes:
every `blocked_pattern` must resolve against the live scanner, and a probe's
question text must never itself match its rule. Lint fails loudly; run it
after every edit to the golden set.

## Probes are mandatory

`run` refuses to execute while the golden set has zero `expected_recall: 0`
questions. The previous harness printed `blocked_probe_hit_rate: 0.0` over an
empty probe list, which read as a pass and tested nothing. An empty probe list
is now an error, not a metric.

Two probe kinds, both scored into `negative_hit_rate`:

- **Blocked-content pattern probes** (`p01-p05`, `blocked_pattern`). The
  former path probes (`b01-b04`) became positives when #172 unblocked their
  files, and an enumeration on 2026-08-02 (the syncer's own discovery replayed
  over the corpus repos, then `kb.security_scan.scan_text_entries` at
  `block_severity=high` on all 69 candidates) found ZERO currently blocked
  files, so no real path can serve as a blocked-file target. A pattern probe
  instead names a rule from `kb.security_scan.SECRET_PATTERNS`; the harness
  imports the live rule at run and lint time and flags a hit when a served
  chunk's RAW text (header included) matches it. That text is exactly what
  every ingest gate rejects, so a hit means pre-scanner fossil content, a gate
  that ran with scanning disabled, or a rule weakened after ingest. Treat any
  hit as a security finding. A probe whose rule was renamed or deleted fails
  the run loudly (`BenchError`) instead of silently matching nothing, and the
  matched text is never copied into results. `secret_assignment` is not
  probe-eligible: its `_is_credential_like` carve-outs mean the bare regex
  over-matches legitimate documentation.
- **Out-of-corpus negative controls** (`n01-n03`). Real GitHub files
  verifiably outside the syncer's selection (wrong extension or prefix, or a
  repo absent from the tracked set with autojoin disabled). A hit resolving to
  one of those identities means the corpus quietly widened (convert the
  control to a positive, exactly the `b01-b04` lifecycle) or identity parsing
  broke.

`fetch_ground_truth.py` never caches `expect_any` targets of
`expected_recall: 0` questions: a blocked-content target could carry exactly
the secret-bearing text the scanner rejects, and `ground_truth/` ships in a
public repo.

## Corpus fingerprints

Two runs are only comparable if the corpus did not move between them. Each run
records:

- `run_at`: a UTC timestamp stamped when the searches begin, so no one ever
  reconstructs a run's date from file mtimes.
- an API snapshot (`documents_tracked`, per-source counts, node version). This
  is what the node CLAIMS; sync bookkeeping has reported success while content
  was absent from the index, so treat it as a change signal.
- a corpus census from `GET /api/corpus` (below): whole-corpus document total
  and the count of documents with `chunk_count` 0.
- a content fingerprint: sha256 over sorted `key:blob_sha` lines from the
  repo-content syncer's per-file checkpoint
  (`.citadel/repo_content_sync_state.json`, `--repo-state` or
  `CITADEL_REPO_STATE_PATH` to point at it). Without that file, or with an
  empty `files` map, the run is recorded as not comparable on content.
- the harness git sha, the questions-file sha256, and the Python version.

`--baseline previous_run.json` prints COMPARABLE, CORPUS MOVED, or NOT
COMPARABLE and withholds metric deltas for non-comparable runs. The content
fingerprint covers repo-content files only, so `compare` also prints a note
when the census document totals differ between two otherwise comparable runs:
whole-corpus metrics (duplication, digest staleness) move with the corpus
even while repo-content stands still. It stays a note rather than a gate
because the digest count grows daily by design.

## The corpus census

`run` walks `GET /api/corpus` (needs `admin` or `audit:read`) and records, in
`fingerprint.census`: `documents_total`, `documents_walked`,
`chunk_count_zero`, `chunk_count_unmeasured`, `chunk_count_zero_ratio`, page
count, and whether the walk was truncated.

A document with `chunk_count` 0 was accepted into the durable store but never
vector-indexed, so search cannot return it, ever. On 2026-08-03 that was 892
of 2867 documents (31.1%). A recall figure quoted without that share
overstates coverage: recall is measured over golden questions whose targets
are reachable, and says nothing about content stranded outside the index. The
census line therefore appears in both `run` output and the `report` block,
with the plain-language consequence attached. `chunk_count` null means the
vector store could not be asked, and is counted as unmeasured rather than
zero; the zero count is then a floor.

## Publishing numbers: `report`

```bash
python scripts/bench/search_bench.py report scripts/bench/runs/latest.json --markdown
python scripts/bench/search_bench.py report scripts/bench/runs/latest.json --markdown --out block.md
```

Turns a saved run JSON into a README-ready markdown block. Every table row
carries the value, the run date, the harness commit, the sample count, and a
one-line statement of WHAT THE METRIC MATCHES ON, so a row copied out of the
table alone stays honest. The definition column is mandatory by construction:
`report` refuses (BenchError) to print any metric that has no entry in
`METRIC_DEFINITIONS`. That rule exists because a recall@5 of 0.95 was once
published without its definition, and it turned out to be head-of-document
header credit, not content quality.

The block also states the census share of never-indexed documents (or that
the census was unavailable), labels latency as client round-trip, and warns
when the run has no content fingerprint. Regenerating the block is the whole
loop:

```bash
python scripts/bench/search_bench.py run --out scripts/bench/runs/latest.json
python scripts/bench/search_bench.py report scripts/bench/runs/latest.json --markdown
```

## Baselines

Run JSONs live in `scripts/bench/runs/`, which is gitignored, and long-lived
baselines belong in private storage outside the repo. They are never
committed: `rows` records the identity (source path, blob, document id) of
every hit served for every question, and `fingerprint.api.sources` lists
every tracked source by name. That enumerates what the vault contains, which
does not belong in a public repo even though no secret values appear.

To compare against a baseline later: keep the baseline JSON wherever you keep
private artifacts, then `run --baseline path/to/baseline.json` or
`compare baseline.json new.json`. Quote deltas only when the output says
COMPARABLE.

## Reading the results honestly

- `_citadel.dataset` on a hit echoes an explicitly requested dataset. Never
  derive isolation claims from it.
- `_citadel.provenance` is `{}` on every repo-content hit observed so far;
  that is why identity is parsed from the chunk header and duplicate detection
  keys on the `Blob:` line.
- Latency here excludes CLI process startup; `citadel search` adds roughly
  0.4 s on top.

## Tests

`tests/test_search_bench.py` covers identity collapsing, span matching, header
stripping, the lint rules, the empty-probe refusal, first-attempt-only
scoring, and fingerprint comparability, with fixtures that mirror the real
`/search` hit shape (`id` distinct from `document_id`, full
Repository/Source/Commit/Blob header, no top-level `dataset` key). It also
covers the census walk (pagination, degraded modes, stuck cursors), the
empty-file-map fail-closed path on both new and legacy run JSONs, the census
drift note in `compare`, and the markdown report (definition present in every
row, refusal on an undefined metric, census and comparability warnings).
