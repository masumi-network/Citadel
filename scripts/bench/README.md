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
python scripts/bench/search_bench.py run --out run.json
python scripts/bench/search_bench.py run --baseline run.json   # later, for a delta
python scripts/bench/search_bench.py compare run_a.json run_b.json
```

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
   `/api/sources`); the local file in this repo has an empty `files` map, so a
   run pointed at it records a fingerprint over zero files. Without a real one
   the run still completes, but records `NOT COMPARABLE` on content.

`--repeats N` costs N searches per question (69 questions in v4) and feeds only
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

## The golden set

`golden_questions.json` v4. 39 questions carry validated `answer_spans`; 22
Linear questions (`l01-l22`) are explicitly unconverted
(`answer_spans_unconverted_reason`) because `fetch_ground_truth.py` skips
`linear:` targets, so there is no cached body to quote. They count for
`doc_recall_at_5` but are EXCLUDED from `answer_recall_at_5`, never silently
counted. 8 questions are probes (`expected_recall: 0`, below).

`lint` validates every span offline against `ground_truth/`: present in the
cached body, absent from the sync header and the document's first line (a
head-line match is title credit, not an answer), absent from the question
text, and unique across every other cached body. It also validates probes:
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

- an API snapshot (`documents_tracked`, per-source counts, node version). This
  is what the node CLAIMS; sync bookkeeping has reported success while content
  was absent from the index, so treat it as a change signal.
- a content fingerprint: sha256 over sorted `key:blob_sha` lines from the
  repo-content syncer's per-file checkpoint
  (`.citadel/repo_content_sync_state.json`, `--repo-state` or
  `CITADEL_REPO_STATE_PATH` to point at it). Without that file the run is
  recorded as not comparable on content.
- the harness git sha, the questions-file sha256, and the Python version.

`--baseline previous_run.json` prints COMPARABLE, CORPUS MOVED, or NOT
COMPARABLE and withholds metric deltas for non-comparable runs.

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
Repository/Source/Commit/Blob header, no top-level `dataset` key).
