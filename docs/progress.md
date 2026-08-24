# Citadel Progress

Last updated: 2026-08-22.

## 2026-08-22 - Production retrieval incident audit

**Status:** Blocked at backup and restore. The process is live, but basic
production search returns no result.

- [VERIFIED] Three read-only MCP and CLI search probes returned HTTP `504` with
  `SEARCH_TIMEOUT`. Narrow repository and path filters did not change the
  result.
- [VERIFIED] Source inventory still responds. The current graph and vector
  indexes report stale state.
- [VERIFIED] A read-only lifecycle snapshot reported `16` completed jobs,
  `617` pending jobs, `1` running job, and `61` stale jobs in the current
  generation.
- [CORRECTED] Attempt-zero jobs prove an unclaimed backlog. Later snapshots
  show the worker changing jobs, so they do not prove a scheduler deadlock.
- [VERIFIED] `/health/ready` returned HTTP `503`. The false-green surface is
  `/api/state`, which hardcodes `healthy: true`, and the information page that
  displays `Live` from that field.
- [VERIFIED] A new generation can run forced Cognee work once per source job
  while Cognee receives a dataset scope. Existing vectors can also satisfy new
  receipts. A provider or embedding change is blocked until restore and
  generation-isolation gates pass.
- [NOT DETERMINED] Search relevance and citation quality cannot be measured
  because no result returned.

Detailed evidence:
`.local-review/research/2026-08-22-production-retrieval-audit.md`.

Sequenced plan:
`.local-review/plans/2026-08-22-retrieval-recovery.md`.

### Current hold

1. [PLANNED] Do not change provider, model, embedding dimensions, or generation.
2. [PLANNED] Do not run forced Cognify, mass requeue, new ingest, or a canary.
3. [PLANNED] Start with the read-only baseline and tested restore checkpoints.
4. [REPORTED] The owner approved the first Railway quiesce and resume window.
   [VERIFIED] It completed. Checkpoint 1 remains blocked until restore is
   tested.

### Checkpoint 0 read-only result

- [VERIFIED] Read-only capture recorded repository HEAD
  `a910f4f37a758dd033c370b7a55c5cf7467d58b9`, Railway deployment
  `b34851a4-580a-4e20-b241-61190f324943`, and the safe relevant variables.
- [VERIFIED] `/healthz` returned `200`, `/health/ready` returned `503`, and
  `/api/state` returned `200` with `healthy=true` and lifecycle `ok=false`.
- [VERIFIED] The current-generation API census reported `634` source heads,
  `16` completed jobs, `617` pending jobs, and `1` running job. Searchable
  current receipts were graph `20`, relational `295`, and vector `16`.
- [VERIFIED] Citadel status reported valid authentication but
  `healthy=false`, a failed `LLMQuotaExceededError` canary, and `10/579`
  sampled documents fully indexed.
- [VERIFIED] The normal web deployment was restored as
  `e760e1b1-4539-4a01-a9f3-c86f444251f2` with
  `startCommand="python -m kb.lite_runtime"` and the `/data` volume.
- [CORRECTED] The existing generation backup helper does not follow the live
  paths. It reads `cognee-system` and `data-storage`, while production uses
  `.cognee_system` and `.data_storage`.
- [VERIFIED] A raw local-state archive and six Qdrant snapshots exist at
  `/data/citadel-state/backups/incident-20260822-cp1-raw/`. The raw archive
  SHA-256 is
  `f11be98518725ae41bed1094fb28cef6048ed75ce2bdf4e5ab6b0e382029e688`.
- [VERIFIED] Railway SSH loopback after resume returned `/healthz=200`,
  `/health/ready=503`, and `/api/state=200`. `/api/state` still reports
  `healthy=true`, lifecycle `ok=false`, and
  `current_generation_searchable_census_mismatch`.
- [VERIFIED] The resumed worker changed source revisions from `1177` to `1178`
  and projection jobs from `7282` to `7283`. This proves activity only.
- [NOT DETERMINED] No isolated restore or complete count comparison has passed.

## 2026-08-21 - WIP PR checkpoint

**Status:** WIP. The scoped implementation is pushed, but the autonomous
knowledge graph goal is not complete.

- [VERIFIED] Branch `fix/cognee-projection-readiness` was created.
- [VERIFIED] Commit `722df50` contains 13 scoped files for Cognee projection,
  lifecycle scheduling, graph retrieval, improvement policy, and progress
  evidence.
- [VERIFIED] The branch was pushed to `origin`.
- [VERIFIED] Draft PR `#309` was opened for the autonomous knowledge graph goal.
- [VERIFIED] No local tests ran during the commit and push step.
- [NOT DETERMINED] Vector projection, graph projection, citations, connected
  seat nodes, and graph UI growth remain unproved until the provider reset.

### Next checkpoint

1. [PLANNED] Poll the existing Central and seat canaries after
   `2026-08-22T00:00:00Z`.
2. [PLANNED] Verify searchable relational, vector, and graph receipts.
3. [PLANNED] Run cited retrieval and verify the current seat graph scope.
4. [PLANNED] Mark PR `#309` ready only if the positive and negative proof
   directions pass.

## 2026-08-21 - Fresh-eyes correction and projection recovery

**Status:** In progress. Liveness is healthy, but projection readiness is not
proven.

[CORRECTED] The earlier diagnosis treated OpenRouter quota exhaustion as the
full blocker. The fresh-eyes review found that quota explains the seat canary
vector and graph failures only. It does not explain the full current-generation
backlog or the census mismatch.

- [VERIFIED] Railway deployment `8189ac7e-b48f-4962-ad16-65234ef51d99`
  contains the five-second lifecycle wakeup bound. The seat canary was later
  claimed at `attempt=1`, and its relational receipt became `searchable`.
- [VERIFIED] Seat canary job `64de2086-5861-5e20-89f1-86c8bce96231` has
  pending vector and graph receipts with `error_code=provider_quota_exhausted`.
- [VERIFIED] The provider returned HTTP `422` with
  `OpenRouter free-model daily quota is exhausted` and reset time
  `2026-08-22T00:00:00Z`.
- [VERIFIED] A Railway lifecycle SQLite snapshot showed `617` graph receipts
  pending, `619` vector receipts pending, and `427` relational receipts
  pending. It showed `13` graph, `11` vector, and `202` relational receipts
  searchable.
- [VERIFIED] The oldest sampled pending jobs had
  `available_at=2026-08-21T06:39:23.870713Z`, `attempt=0`, and no recorded
  error. This proves an unclaimed backlog separate from the provider error.
- [VERIFIED] Deployment `b34851a4-580a-4e20-b241-61190f324943` defaults
  Cognee search to `GRAPH_COMPLETION` unless `CHUNKS` is selected explicitly.
- [VERIFIED] `/healthz` returns HTTP `200`. `/readyz` remains `ok=false` due
  to corpus timeout and `current_generation_searchable_census_mismatch`.
- [VERIFIED] `auto_improve=false`. Automatic improvement stays disabled.
- [NOT DETERMINED] Cited graph retrieval and full graph UI growth remain
  unproved until vector and graph projection receipts become searchable.

### Recovery plan

1. [PLANNED] Wait for the provider reset. Do not create another ingest job.
2. [PLANNED] Poll the existing seat and Central canaries.
3. [PLANNED] Require searchable relational, vector, and graph receipts for
   both canaries.
4. [PLANNED] Run a cited graph search and verify Central plus the current seat,
   connected nodes, and the scrollable document list.
5. [PLANNED] If the census still differs, capture receipt-level pending work
   and fix scheduling only from that evidence.

### Branch and PR

- [PLANNED] Candidate branch: `fix/cognee-projection-readiness`.
- [PLANNED] Keep the PR focused on the projection readiness fix and its
  evidence records.
- [PENDING APPROVAL] Push and PR creation require `/code-review ultra` and
  explicit approval in the current session.

## 2026-08-21 - Free-router recovery and production readiness

**Status:** In progress. The service is live. Seat vector and graph completion
remain unproved because the approved canary failed before the OpenRouter free
quota reset.

- [VERIFIED] Railway deployment `1df5144a-78b6-4bd6-8232-02d3d3e77e6b` returned
  `SUCCESS` at `2026-08-21 00:23:47 +02:00`.
- [VERIFIED] The liveness probe returned HTTP `200` with
  `{"ok":true,"service":"citadel"}` after deployment `173ba4f0-d4a7-4e2d-a142-04092a1f95db`.
- [VERIFIED] Railway now uses `/healthz` as its deploy probe. `/health/ready`
  can return `503` while bounded corpus or lifecycle checks time out on a live
  queue.
- [VERIFIED] Local and Railway model routes now use `openrouter/free` for direct
  calls and `openrouter/openrouter/free` for Cognee's LiteLLM form. The embedding
  route remains `nvidia/nemotron-3-embed-1b:free` through OpenRouter.
- [VERIFIED] The approved canary operation is `failed` in generation
  `citadel-railway-v053-dots-free-20260820`. Its relational receipt is
  `searchable`; its Qdrant vector and Ladybug graph receipts are `failed` with
  `provider_non_retryable`.
- [VERIFIED] Production logs show the free-model quota at zero and the provider
  reset at `2026-08-21 02:00:00 CEST`.
- [INFERRED] Daily quota errors now schedule delayed lifecycle retries. Invalid
  credentials, permissions, and missing models remain terminal errors.
- [NOT DETERMINED] No post-reset seat canary has yet proved vector recall,
  graph connection, or graph UI growth. The existing canary must not be reused
  without explicit approval for a retry.

### Next gate

1. Approve one retry of the existing seat canary after the provider reset.
2. Confirm all three receipts are `searchable`.
3. Search the canary and verify the seat-scoped graph connection.

## 2026-08-19 — Cognee contract: write accepted vs projection ready

**Status:** confirmed. Writes are accepted into source storage before graph projection. Presence hubs can appear while content nodes are still empty.

- `/ingest` and hook capture still route to `LearningProcess -> Citadel -> cognee`.
- [VERIFIED] `/api/ingest` requires `cognify=true` for seat writes; explicit opt-out is rejected.
- A write can be `accepted` while graph projection is still queued, then appears after `queued_not_confirmed` → `ok` completion.
- `/api/mesh/graph` renders projected projection + dataset scope filtering (`visible_nodes`) and is not a raw ingest log.
- [VERIFIED] The Next graph UI has a scrollable visible-node list beside the canvas.
- [VERIFIED] The graph page polls `GET /api/mesh/projection-status`; full operation details remain available at `GET /api/operations/{projection_job_id}`.
- [VERIFIED] `GET /api/documents?query=` remains a searchable-content check; the graph page now exposes the returned projection nodes in a scrollable list.
- Graph projection visibility drops fast when dataset attribution fails (`node_dataset_map` read errors) because ADR-0009 is fail-closed; this matches design and explains “only presence hubs” behavior.

### Open TODOs from this cycle

1. [VERIFIED] Keep `projection_job_id` visible after ingest and show completion polling in the same view.
2. Add a seat graph health line on `/api/access` showing `projection_lag_ms`, `visible_nodes`, and `projection_state` for the seat dataset.
3. [VERIFIED] Add a seat-scoped document list in Graph with pagination/scroll so users can verify growth without leaving the page.
4. Add a troubleshooting path for `visible_nodes=0` with `fallback=true`: check operation log + dataset map before assuming ingestion failure.
5. Document in onboarding that commit snapshot is one node per commit, while branch/file context is metadata only unless explicit sources are ingested.
6. Track and publish local indexers for the seat projection lag signal after a deploy, then connect it to graph UI state.

## 2026-08-17 — inside bench: CLI 0.4.0 vs 0.5.1

Recorded on `docs/performance.md`. Raw JSON (gitignored):
`scripts/bench/runs/2026-08-17-cli-0.4.0-vs-0.5.1.json`.

**VERIFIED** this session against `https://citadel.utxo.ag`, golden q01,
n=20, warm, `top_k=5`:

- Deciding metric: successful search wall time p50.
- 0.5.1: 20/20 ok. p50 25049 ms. p95 26488 ms.
- 0.4.0: 0/20 ok. Hard 20 s timeout. stderr `citadel search: The read operation timed out`.
- q01 span hit: 0/20 on 0.5.1. Top 5 were git commit snapshots plus a GitHub daily update.
- Railway 24h averages: about $23/month. Egress not determined.
- Frozen 105-question `citadel bench run` was **not** run (about 45 min at 25 s/query).

Outside products were not measured. Recommended harness for Mem0 / Zep /
Letta / Cognee: OmniMemEval `--lib` (conversation-memory datasets, not our
vault). Citadel needs a custom adapter. Document retrieval vs Elasticsearch
belongs on BEIR, not LoCoMo.

## 2026-08-17 — later brainstorm, locked (not this pass)

Positioning locked for a later brainstorm, not for this landing/info update.
Citadel as multi-agent context: each agent gets a seat/node with its own
memory; those memories sync to Central; the org result is multi-agent memory.
Stack logos (Cognee, Qdrant) belong on the architecture React Flow later, not
a separate strip. Knowledge-base screenshot and moving architecture off
landing are also later.

## 2026-08-17 — citadel-archive 0.5.1 published to PyPI

PyPI package name stays `citadel-archive`. Public node is
https://citadel.utxo.ag (MCP `/mcp/`). Source is
https://github.com/masumi-network/Citadel. Railway hostname is historical.

What landed (measured this session unless tagged [REPORTED]):

- PR #301 MERGED. Merge commit `caf8eb99520b9eaf459c3009924238c242bd0df1`.
  Tag `v0.5.1` points at that SHA. Tag `v0.5.0` stays
  `fb47665522d67256d2f36f1cf80f1800d324d1da`. Do not retag `v0.5.0`.
- PyPI `citadel-archive` version `0.5.1` (pypi.org JSON;
  wheel upload `2026-08-17T17:44:26`). pipx venv reports `0.5.1`. The
  `pipx upgrade citadel-archive` 0.4.0 → 0.5.1 step is [REPORTED] by the
  user.
- GitHub Release: https://github.com/masumi-network/Citadel/releases/tag/v0.5.1
- Publish dispatch https://github.com/masumi-network/Citadel/actions/runs/32051600953
  succeeded. PyPI job success. OCI stage / attest / smoke / promote SKIPPED
  on `workflow_dispatch`. `docker buildx imagetools inspect
  ghcr.io/masumi-network/citadel:0.5.1` returned not found. Do not claim
  GHCR `:0.5.1` is live.
- PR #304 MERGED (`a6d8919`, attest docker login). PR #305 MERGED
  (`51c9776`, fetch main without `--depth=1`). `origin/main` is `51c9776`.
- Local leftover branch: `fix/publish-main-fetch-depth`.
- TUI + URL sweep shipped in #301 (Release body: default Node
  `https://citadel.utxo.ag`, role-aware TTY home).
- [REPORTED by user] `citadel mcp add all` after upgrade: Claude/Cursor/
  Windsurf unchanged; Codex/Gemini wrote; Cline/Zed/Pi paste-only. Cursor
  already writes `~/.cursor/mcp.json` to citadel.utxo.ag.

Still open (not this cut): GitHub issues #228 and #247. #128 still OPEN.
GHCR `:0.5.1` not promoted.

## 2026-08-14 — Onboard wires Claude / Cursor / Codex (and the macOS GUI env)

`citadel mcp add` already wrote client configs. Interactive `citadel onboard`
already offered a coding-tools checkbox. `--non-interactive` skipped that step,
so a headless onboard left `~/.cursor/mcp.json` unwired. Dock-launched Cursor
also never saw `CITADEL_MCP_ACCESS_TOKEN` from `~/.zshrc`, which is the
`CallMcpTool` timeout we hit in Cursor while Codex MCP on the same Node worked.

What shipped in this tree (not a PyPI/Railway cut):

- Non-interactive onboard calls the same `tool_detect.apply` path as
  `citadel mcp add` for every detected write-tier client: Claude Code, Cursor,
  Codex, Gemini, Windsurf. `--no-tools` still skips. Cline/Zed stay snippet-only
  unless you pick them on the interactive checkbox.
- Interactive checkbox is unchanged (write-tier preselected). Those results now
  show up in the onboard summary instead of a one-off print above it.
- On macOS, a token the Node did not reject is published with
  `launchctl setenv CITADEL_MCP_ACCESS_TOKEN` (until logout). Not a LaunchAgent.
  The next-steps block says to Cmd-Q Cursor and run `cursor .` from that shell.
- Tests: `uv run pytest tests/test_onboard.py tests/test_cli_commands.py tests/test_headless.py tests/test_tool_detect.py` → `162 passed` (VERIFIED this session, outside the sandbox). Autouse fixture stubs `launchctl` so the suite cannot write the developer GUI env.

Out of scope here: corpus `failed_projection` / census mismatch, discovery still
advertising `*.up.railway.app` via `X-Forwarded-Host`, `citadel doctor --fix`
writing `~/.cursor/mcp.json`.

## 2026-07-29 (evening) — The Kuzu lock is dead, and six things that reported success while doing nothing

The hourly evolve cycle had been failing for weeks and saying it was fine. Fixing
that turned into finding the reason it went unnoticed, which was a pattern rather
than a bug.

### #88 — the Kuzu lock — FIXED and verified live

Every hour, `github_sync` and `linear_sync` died with `Could not set lock on file:
cognee_graph_kuzu (Lock is held by PID 44)`.

The root cause is not what the issue title said. cognee 1.2.2 opens Kuzu read-write
with an exclusive OS file lock and holds it for the lifetime of whichever process
opens it, and in this deployment that is always the web. The evolve subprocess could
therefore never acquire it. The subprocess existed to "free the Kuzu lock on exit"
(`kb/server.py:253`), which was backwards: it was the one that could not get in.

The add-only guard never helped either, because `cognee.add` is itself a Kuzu opener.
`cognee/modules/pipelines/operations/run_tasks.py:166` calls `get_graph_engine()` at
the end of every pipeline run purely to evaluate `hasattr(graph_engine,
"push_to_s3")`. The comment at `kb/cognee_client.py:335` saying add does not touch
the graph is false for 1.2.2, and the guard sat fourteen lines after the call that
threw.

Phase 1 now runs in the web's own event loop (#140). One process, one Kuzu owner,
which also retires the loop-binding hazard of #69 rather than working around it.
First cycle after deploy:

```
22:55:57  Evolve stage github_sync: starting
22:57:34  Evolve stage github_sync: ok
22:58:00  Evolve stage repo_content_sync: ok
22:58:09  Evolve stage self_improve: ok
```

`/healthz` answered in ~150 ms during that pass, three probes, so moving the work
onto the request loop did not starve it. That was the main risk in the change and it
is measured, not assumed.

`linear_sync` has still not been observed green, so #46 stays open. See #153 for why
that is hard to verify right now.

### The pattern: success reported for work that did not happen

Six independent places, all fixed today. Each was a "nothing configured" or
"everything failed" path falling through to the success branch, while the ordinary
failure paths around them were handled carefully.

| What lied | Fix |
| --- | --- |
| evolve exited 0 with two dead stages | #137 (#89) |
| promotion printed `failures=0` while all 22 searches died | #138 |
| the cognify canary passed because the graph grew, not because anything was retrievable | #139 (#114) |
| a dying scheduler task was swallowed by a bare `set.discard` done-callback | #141 |
| an unconfigured Chat gateway made both `/contact` and the org digest do nothing, silently | #142, and #151 |
| a timed-out search was audited as `success=True` | #152 |

The promotion one is the clearest: the `except` was fine, and the bug lived at the
boundary where a caller counted the emptied result as a clean zero.

A spec for a bounded sweep of the same shape is at
`docs/superpowers/specs/2026-07-30-exception-flattened-to-empty-audit-design.md`.
It found four more instances, filed as #148.

### Security

Two audit findings closed, neither filed publicly since the repo is public.

- **M3** (#143): six server modules sent bearer tokens through the bare `urlopen`,
  which follows redirects and replays every header at the target. `LLM_ENDPOINT` is
  the operator-configurable one, so pointing it at `http://` sent
  `OPENROUTER_API_KEY` in cleartext. One transport now refuses an untrusted scheme
  and strips credentials cross-origin. Redirects are still followed, because GitHub
  301s renamed repositories.
- **M4** (#144): nothing limited authentication attempts. Severity was lower than the
  audit implied for this deployment, since every credential here is already
  high-entropy, so the substance is a boot-time refusal of weak env keys plus a
  throttle counted only on failure. A valid credential never reaches the limiter, so
  an attacker cannot lock the team out.

### Filed, with root causes rather than symptoms

#147 six seats have no cognee Dataset row and have never had a working vault.
#148 corrupt state files read as "first run" and can post a false digest to Chat.
#149 the organization digest LLM call fails 400 every cycle.
#150 cognee pin drift, and the migration warning it caused is a false alarm.
#151 Google Chat was never configured, so the digest has never run.
#153 the evolve scheduler resets its interval on every deploy, so on an active day it
never fires at all. Exactly one cycle ran in fourteen hours.

#50 and #105 now carry measured root causes. Search costs one LLM call per
`cognee.search` from cognee's `AUTO_FEEDBACK` default, and a seat query fans out to
three datasets, so three calls per query. `/healthz` does no work of its own; the
measured blocker is `AccessStore.authenticate_token` rewriting the whole access store
on every authenticated request, 35 ms.

### Contributions

First outside contributions merged: #130 and #131 from @WAHIB-EL-KHADIRI, closing
#113 and #112.

## 2026-07-29 — Public site rebuilt and deployed, Next port reaches the dashboard

PR #132 merged and deployed. The public site is live on the new build, the
Next.js port now covers three dashboard views behind preview routes, and two
gaps found while building them are filed rather than papered over.

### Public site — SHIPPED (deploy `92707d02`, verified live)

Production route probe after the deploy reached SUCCESS:

| Route | Before | After |
| --- | --- | --- |
| `/` | 303 → `/login` | 200, landing page |
| `/use-cases` | 404 | 200 |
| `/contact` | 404 | 200 |
| `/partners` | 200 | 301 → `/use-cases` |
| `/app` | 200 | 303 → `/login` when anonymous |

CSP is scoped per path rather than loosened globally: `/` carries
`style-src 'self' 'unsafe-inline'` for the inline hero glow, `/info` and every
other page do not. That exception is temporary and disappears when `/` switches
to the Next build.

Also shipped in the same PR: the contact form relaying to Google Chat and never
into the vault ([ADR-0013](adr/0013-public-contact-endpoint.md)), the promotion
secret scan, live repo statistics on a daily refresh, and one stylesheet across
all five public pages.

### Next port — Search, Admin, Explore (committed, not pushed)

On `feat/partners-page` as `6485b87`. Verified locally: **955 passed, 1 skipped,
0 failed**, `ruff` clean, `tsc --noEmit` exit 0. The export grows 87.6 KB for
the three views, and `force-graph` is not in that number because Explore loads
it from `kb/static/vendor/` at runtime instead of bundling a second copy.

Only one line of the slice touches the running server, and it is additive:
`explore` joins `WEBUI_APP_VIEWS` at reader role. `/app` still serves the
hand-written dashboard.

Not built, and stated on the pages rather than stubbed: seat and token minting
(minting returns a secret exactly once and deserves a deliberate reveal-once
design), the capture-policy editor (a `window.prompt` today, and porting it
faithfully would be porting a bad UI), and Activity, which is blocked on a
product decision about what one seat may learn about another's work.

### Two gaps filed

- **[#134](https://github.com/masumi-network/Citadel/issues/134)**: mesh
  counters reset to zero on restart. Observed on production directly after the
  deploy: `stats.documents` 0 while the source nodes in the same payload sum to
  268 documents, all marked `rehydrated: true`. The node topology rehydrates on
  boot; the counters, derived from an in-memory event log, do not. No content is
  affected, but the dashboard renders those counters where a total belongs, so a
  redeploy is visually indistinguishable from an emptied vault. This was
  mistaken for data loss in practice, not in theory.
- **[#135](https://github.com/masumi-network/Citadel/issues/135)**: graph nodes
  carry no trust tier, and `promoted_by` / `promoted_at` do not exist anywhere
  in the tree. Distinct from #104: that one is ingest storing no provenance,
  this one is that the tier has no representation outside the search path, and
  that "who promoted this?" is unanswerable for every document in the vault
  today. Filed P1 on one ground: the promoter half cannot be backfilled, so
  every document promoted before it lands loses that record permanently.

### Open, and explicitly not claimed as fixed

- **#117**: `kb/linear_sync.py` still runs neither the secret scan nor change
  detection. The scan added in this PR covers `kb/promotion.py` only, which is
  easy to misread from the commit subject alone.
- **#126**: `kb/static/app.js` was rewritten by 1150 lines and `loadMesh` now
  has a failure path that flips the connection state to "Offline" and raises an
  alert, so a failed fetch is no longer indistinguishable from a slow load. But
  `updateGraphMeta` still prints "Loading Knowledge Mesh" whenever the payload
  is null, which is exactly the failed state, and the projection-inspector half
  is unchecked. There is no browser harness, so this stays open.

### Still queued

- Switch `/` and `/app` to the Next build. That is the commit where the
  `'unsafe-inline'` exception on `/` gets deleted.
- Verify the promotion secret scan against the real production queue. It has
  never run on live candidates; if any contain secrets, that is a finding.
- Give `repo_stats` a GitHub token on prod, or it serves cached fallbacks once
  the 60-request unauthenticated ceiling is hit.
- Gap 11, the Activity view, blocked on the cross-seat visibility decision.

## 2026-07-22 — Public `/info` state page, `/api/state`, Masumi design alignment

Shipped a public **State of the Vault** report and began aligning the Citadel
frontend to the AGENTIC / Masumi design system.

- **`/info` + `GET /api/state` — SHIPPED (deployed to prod).** A node-served
  report at `/info` (current metrics, releases v0.2 → v0.4, architecture diagram,
  commit-velocity chart, roadmap; progressive `Go deeper` expanders; light/dark
  toggle). Live tiles hydrate from a new **public, no-secrets** `GET /api/state`
  (version, health, per-source doc/repo counts, totals — safe aggregates only,
  modeled on `/.well-known/citadel.json`; best-effort, degrades to empty). Routes
  in `kb/server.py`; page in `kb/static/info.{html,css,js}`; CSP-clean (external
  assets, chart bars via CSSOM). Linked from the README (+ Masumi-magenta badge).
  Verified live: `/info` 200, `/api/state` returns real counts (48 repos, 200
  Linear issues), tiles hydrate in-browser.
- **`/info` restyled to Masumi (`apps/sokosumi/DESIGN.md`).** Inter-only, weight
  lightens as size grows (Inter Light hero + section heads, negative tracking,
  sentence case), neutral ramp + a single Iris-magenta `#FF51FF` accent, flat
  elevation, segmented-line section headers; widens on large screens.
- **Dashboard accent + light mode — PR #99 (open, not merged).** Swaps the
  dashboard accent `#fa008c → #FF51FF` (solid fills route through the deeper
  `#c010a0` so white text stays legible) and adds a light theme
  (`prefers-color-scheme` + a `data-theme` toggle in the sidebar, `theme.js`;
  applied on `/login` too). Token-level only — the CSS pixel-mark colors and ~9
  `rgba(255,255,255,…)` spots remain a follow-up. Awaiting review before deploy.

## 2026-07-21 — Merges: portal Phase 1, Pixel Bastion, fast `citadel status`

Three PRs landed on `main` the same day:

### Seat-scoped portal Phase 1 — SHIPPED (PR #95)

**PR #95** (`cursor/seat-scoped-portal-phase1`) ships Phase 1 of the
[seat-scoped portal plan](plans/seat-scoped-portal.md): members paste a seat
`ctdl_…` token on `/login` and land on **My Node** (Seat home).

**Shipped:**

- Session chrome surfaces `seat_slug` + Node label (not role-only).
- Seat home via `GET /api/me/summary` — doc counts, recent Node activity, empty
  checklist, links to search / graph / activity.
- Member-first login copy; admin nav hidden for non-admin; My Node vs Central
  search badges.
- Optional portal path in [`onboarding/teammate-rollout.md`](onboarding/teammate-rollout.md).

**Grill outcomes (locked, unchanged):** Option A token sessions; working
hypothesis B+C (visibility/UX, not write-router); “linked” = all surfaces (Phase 1
= chrome/home); analytics columns + audience **B** deferred to Phase 2;
Promotion-only Central UI; always presence graph; empty Node until capture;
admin-only token rotation in Phase 1.

**Phase 2 remaining:** seat activity analytics table, Access inventory
deep-links, graph “you” highlight / hub context panel, Vault Activity seat POV.

### Pixel Bastion brand — SHIPPED (PR #96)

**PR #96** ships the canonical **Pixel Bastion** 7×7 mark across CLI home
(cascade + idle blink), README banner (`docs/brand/readme-banner.svg`), favicon,
login/sidebar lockup, and self-hosted Inter / JetBrains Mono. Dashboard chrome
moves sidebar-first to match the Interface design canvas. Overview / Activity
gain CSP-safe SVG analytics bars (Knowledge Mesh canvas unchanged).

Portal UX note: brand polish only — seat login / Node isolation unchanged.

### CLI `citadel status` latency — SHIPPED (PR #97)

**PR #97** makes search smoke **opt-in**. Default `citadel status` no longer
calls `/search` (that check never gated `healthy` but often dominated wall time
on Railway/cognee). Use `--check-search` for a short (3s) smoke; full
`citadel search` still uses the longer timeout. `--no-search` remains a no-op
alias for existing scripts.

## 2026-07-20 — Shared Session Traces v1 + multi-agent policy onboard — SHIPPED (PR #93)

**PR #93** (`design/shared-session-index`) ships **Shared Session Traces v1**
(ADR-0011), closes **PR #91 as superseded** (three cross-seat auth fixes landed
here instead), and extends **`citadel onboard`** with multi-agent proactive
policy install. CI gains a **`pip-audit` gate** with uv dependency overrides.

**Shared Session Traces v1 (ADR-0011):**

- **`POST /api/share-session` + MCP `citadel_share_session`** — explicit
  in-session share only; SessionEnd still writes private **Node** traces (light
  tier). Share requires an **Approved Capture Root** (`cwd` server-side check).
- **Compact Session Context** — client `distill_trace()` + redaction, server
  LLM dead-end refinement only when tool-error pairs exist, then **dual-write**
  to the seat **Node** (deterministic) and `session-traces` (shared tier).
- **Deferred + coalesced cognify** (~5–15 min) on share — not inline before MCP
  returns; private **Node** memory is never enriched.
- **Default `citadel_search`** includes `session-traces` with split results and
  **`reference-only` trust demotion** (`_citadel.trust`); Central stays
  org-authoritative. Traces never promote to **Central** and never feed the
  daily improve loop.
- **Glossary + CONTEXT** updated for **Session Trace**, **Shared Session
  Trace**, **Compact Session Context**, amended **Seat Presence** and **Tiered
  Ingestion**.

**PR #91 superseded — cross-seat auth fixes (now in #93):**

- **Obsidian vault ownership (ADR-0009)** — five Obsidian routes fail closed
  with 404 (not 403) when the vault is not the caller's.
- **`GET /api/knowledge/events` caller-scoped** — `citadel activity` no longer
  leaks other seats' event text under a reader token.
- **`POST /feedback` write-scope parity** — resolves dataset/session like
  `/ingest`; feedback text byte-capped.

**Multi-agent policy onboard (`install_agent_policies`):**

- **`AGENTS.md`** at repo root — universal policy for Codex (CLI + app), Pi,
  Cline, Zed, and other AGENTS.md-aware tools (always installed).
- **Cursor** — `.cursor/rules/citadel-agent-policy.mdc` (`alwaysApply`) when
  Cursor is detected.
- **Windsurf** — `.windsurf/rules/citadel-agent-policy.md` (`always_on`) when
  detected.
- **Gemini CLI** — `GEMINI.md` when detected.
- **Claude Code** — same policy injected via the **SessionStart** hook
  (`kb.hooks.sync_start`); SessionEnd hook unchanged (private Node traces).
- Policy: search at task start; trace hits are reference-only; share dead ends
  with `citadel_share_session` only after explicit user approval.

**CI / hygiene:**

- **`pip-audit` in GitHub Actions** — uv `[tool.uv] override-dependencies` pins
  transitive CVEs (`pillow`, `pypdf`, `python-multipart`) until the cognee/FastAPI
  stack catches up; `PYSEC-2026-2447` ignored where no fix exists yet.
- **`sync_session.py` lint fix** — ruff clean.

**v1.1 deferred (ADR-0011 open):** `citadel_prior_work` overlap-ranked retrieval;
**`citadel unshare`**, ~90-day TTL, admin hard-delete; automatic
`CaptureRoot.share_traces` standing consent (blocked until unshare ships).

**Follow-ups:** production deploy + seat-token smoke of share/search demotion;
retraction controls and prior-work lookup remain v1.1.

## 2026-07-16 — Agent-onboarding hardening: seat-bound tokens, headless skill, `--json` error parity

Triggered by a real teammate incident: an admin minted a **seat-less
(service-account) token**, handed it to a teammate's Codex agent, and every
search failed with `DatasetNotFoundError` — misread by the agent as an "invalid
token." Root cause was provisioning, not auth: a token with no seat has no
default dataset. Two cold-agent usability passes (fresh subagent driving the
headless CLI + auditing the skill) drove the fixes below.

**Diagnosis confirmed against code:** tokens default to `kind="service_account"`
with no `seat_slug` / `default_dataset`; a seat-less token has no dataset to
search → `DatasetNotFoundError`, and its writes route to the shared org dataset.
Fix is to mint **seat-bound** (`citadel seat token <slug>` / dashboard *Assign to
seat*), which inherits the seat's role + `seat:<slug>` default dataset.

- **`SKILL.md` reworked for cold-agent onboarding (fresh-agent audit: "production
  ready").** New **Agent Fast Start** runbook (`install → set token →
  status --json verify → search`) and a **How Citadel Works** 30-second model
  (datasets/Node/Central, caller-scoped search, two-stage ingest/cognify,
  activity-vs-mesh, roles). Explicit "never run bare `citadel onboard` in an
  agent/CI session" warning; the auth-failure contract (`auth.ok==false` while
  `node.ok==true` ⇒ token problem, not install; exit codes); `activity
  --local/--global/--watch`; and the `search --json` payload shape.
- **Seat-bound token mandate.** New admin warning in Team Onboarding + fixed
  "Connecting a New Agent" step 1, which had told admins to hand over a
  *service-account* token (the exact footgun). Both name the
  `DatasetNotFoundError` symptom and the correct mint.
- **CLI `--json` error parity.** `onboard` (no-token + hook-install), `search`,
  `ingest`, and `capture` printed a plain-text line on the no-token failure path
  under `--json`, choking agents that pipe the output. All now emit
  `{"ok": false, "error": ...}`, matching `status`/`promotion`.
- **`status --json` surfaces the stale-token drift hint** (`checks[].data.hint` +
  top-level `hint`) so agents parsing `--json` get the `source <rc>` fix the human
  path already printed.
- **No-token message no longer nudges bare `onboard`** — now suggests
  `citadel onboard --non-interactive --token ctdl_...`.
- **`citadel activity` added to the bare-`citadel` home menu** (under Knowledge).

Verified: ruff clean; 731 tests pass (3 pre-existing `cognee`-extra failures,
unrelated). `[Unreleased]` CHANGELOG updated. **Follow-up (ops, not code):** the
teammate still needs an admin to mint her a seat-bound token and revoke the
tokens leaked in chat.

## 2026-07-14 — Dashboard graph, mesh read isolation, /mcp fix — SHIPPED + DEPLOYED

Started as a dashboard papercut ("clicking a node shows metadata but no text")
and grew into a read-side privacy release once a `/grill-with-docs` pass found
the graph surfaces were leaking seat Node content org-wide.

**PR #76 (dashboard + isolation) + PR #77 (/mcp fix)** merged to `main` →
Railway auto-deployed (deployment `f5bdccca`, clean boot, `/healthz` 200,
`assert_cognee_dataset_api` self-check passed). 707 tests green.

- **ADR-0009 mesh read isolation.** The whole-graph endpoint plus the
  `/api/mesh` + `/events` activity projection and the `/api/documents`
  drill-down served every seat's Node content (documents, chunk text, extracted
  entities, ingest/search event text) to any reader token — a violation of
  ADR-0003 that `/search` already enforced. Now caller-scoped (own Node +
  Central; admin bypass), fail-closed on attribution failure, with a
  404-equals-missing drill-down (no existence oracle) and layered entity
  visibility (a shared entity can't resurface a hidden document; an `is_a` edge
  alone never promotes a non-EntityType). **Seat presence stays universal** —
  every seat is a hub (slug + counts only, never names/emails). New glossary
  terms **Seat Presence** and **Vault Activity**; `CONTEXT.md` "Graph views"
  section rewritten.
- **Graph legibility + drill-down.** Human labels from first-chunk text, a
  kind-filter legend (chunks hidden by default), a richer inspector with
  clickable neighbors and document text, edge tooltips, Knowledge-Mesh default
  view, and a Log out button. Plus a seat-assignment dropdown on token minting.
- **Production-readiness pass (measured, 5-dimension audit).** Fixed 3 blockers
  + 12 majors before merge: `get_document` no longer reads the whole 34k-edge
  graph per click (targeted node read); `/api/mesh/graph` shaping runs off the
  event loop with a dedicated concurrency cap; the raw graph read and the
  dataset-attribution map are TTL-cached with single-flight; attribution is one
  joined query with stale-while-error. The audit also caught the projection
  leak above.
- **/mcp base-URL fix (PR #77).** The public MCP client defaulted to
  `localhost:8000` while Railway listens on `$PORT`, so public resource reads
  hit a refused connection. Now `_self_base_url()`. The deeper `tools/list`
  loop-starvation half (#50) is still open.
- **Skills → headless-CLI-first**, so agents stop retrying MCP when a session
  registers no tools.

**Verified in prod:** deploy health + clean boot. **Not verifiable in prod this
session:** isolation and MCP tool registration end-to-end — the local seat
token is rejected (seat `sarthi` has 0 active tokens; needs a fresh mint). All
isolation logic was verified live on a seeded localhost demo across
admin/seat/reader identities. **Follow-ups still open:** the `/mcp` loop
starvation (#50), three unverifiable UI papercuts (no browser harness this
session), and the `0.3.0` version cut.

## 2026-06-30 — Read-side hardening sprint (issues #25–#53) — SHIPPED

Heavy-user + pentest testing surfaced a broken read/write data plane behind green
dashboards. Root cause: durable writes were routed through cognee's per-session
cache, corrupting them to the literal `[DataItem]` and never indexing them.

**First wave (9 PRs, each tested):** the data-plane root-cause fix (#54, #56,
**node-verified**), MCP resource auth (#57), CLI false-green (#58), version
single-sourcing (#55, #59), input validation (#60), sync-auth surfacing (#61),
and repo auto-join (#62). Closed: #26, #29, #30, #31, #32, #34, #37, #42, #49.

**Batch 2 (PR #64) — all remaining issues, one reviewable branch, 598→601 tests.**
11 commits resolving #51/#53 (MCP ingest inline cognify + byte cap), #45/#33 (MCP
406 Accept shim + role/seat tools/list filter), #39/#48 (promotion read-timeout +
admin-gated approve/reject — closed a seat self-promote-to-Central hole), #40/#41
(durable feedback fallback + improve guards), #28 (get_document drilldown), #35/
#36/#38/#43 (onboarding completeness), #27 (honest status/doctor/readyz +
corpus-gate 503), #44/#50 (parallel search + timeout budget + 429/Retry-After/
X-RateLimit contract + client retry), #46/#52 (Linear per-issue→Central +
surfaced failures), #47 (Kuzu writer lock + cross-process cognify guard), #15
(admin dry-run-first graph cleanup). Merged to main → Railway auto-deploy →
live-verified on the node.

**Follow-ups from live prod testing (PRs #65–#68):** live verification exposed
gaps unit tests couldn't:
- **#65 — completed #47.** The real hourly `Lock is held by PID` cause was
  `remember()`'s per-ingest `cognee.remember(run_in_background=True)` cognify
  firing in BOTH the web and the evolve Phase-1 subprocess. Now: subprocess is
  add-only (`CITADEL_SUPPRESS_INLINE_COGNIFY`), web cognify is writer-lock-guarded.
- **#66 — completed #46.** Auto-map Linear assignees→seats by member email
  (`LinearClient.fetch_users`), no manual `CITADEL_LINEAR_USER_MAP`.
- **#67 — the real #15/#52 fix.** The `[DataItem]` *in search* was cognee's
  per-session QA cache (`source:session`), which `recall()` read FIRST; gated
  behind `CITADEL_COGNEE_SESSION_RECALL` (default OFF).
- **#68 — vector-store cleanup.** The scaffolds were also cognified into the
  `DocumentChunk_text` vector store; `delete_graph_nodes` now also deletes vector
  points + the cleanup adds a search sweep for orphaned chunks.

**#15 DONE + node-verified clean:** ran the admin cleanup loop until dry (214
garbage nodes/chunks purged across session cache + graph + vector); all prod
searches return 0 `[DataItem]`/marker/session, 746 real docs indexed. **Lesson:
the `[DataItem]` garbage lived in three distinct stores (session cache, Kuzu
graph, pgvector chunks); graph deletion ≠ vector deletion ≠ session-cache, and
live prod testing was essential — unit tests passed at every wrong layer.**

**Status (final):** **18 issues closed and live-verified** (#25, #27, #28, #33,
#35, #36, #38, #39, #40, #41, #43, #44, #45, #47, #48, #51, #52, #53) — incl. the
#25 umbrella diagnostic and **#47 (Kuzu lock), node-verified: the post-deploy
hourly evolve pass ran clean (`stages finished exit=0`, zero `Lock is held by
PID`, green verify canary).**

3 open, each root-caused (need node-testable fixes, not blind deploys):
- **#69 (NEW)** — verifying #46 exposed that the evolve subprocess runs each stage
  in its own `asyncio.run()`, so cognee's engine loop-binding makes `github_sync`
  AND `linear_sync` fail every pass (`got Future attached to a different loop`).
  The recurring GitHub/Linear sync isn't actually running.
- **#46 (Linear mirrors)** — auto-map deployed (PR #66) but blocked by #69 (recurring
  sync) and the HTTP resync timeout (#52's 200 per-issue cognifies starve the request).
- **#50 (search latency)** — backpressure/429 done; raw ~6–9s is cognee's per-search
  pipeline (Q&A caching + possibly remote embedding), needs node profiling.

**Action:** credential rotation — tracked privately in the ops runbook.

## 2026-06-29 — v0.2.0 + v0.2.1: CLI DX overhaul shipped (PyPI + Railway)

The client got a top-to-bottom UX pass and shipped as `citadel-archive` 0.2.0
then 0.2.1 — **published to PyPI, deployed to Railway, cut as a GitHub release.**
What changed for users:

- **Seat-scoped ingest that actually works, with inline cognify.** `citadel
  ingest` (and `citadel search`) are now HTTP-backed by default — they route to
  your seat via the token, no `[server]` extra needed (`--local` still runs the
  in-process stack). `ingest` cognifies **inline server-side** so the note is
  searchable immediately (`--no-cognify` to skip), and prints its destination +
  scope (private seat vs shared org dataset). `--json` on both.
- **Richer `citadel status`.** Absorbed the old TUI's data into a "Knowledge
  mesh" section (documents / nodes / edges / searches) — no separate command to
  launch.
- **`citadel doctor` (+ `--fix`).** Diagnoses setup drift — token-in-rc-not-env,
  MCP/capture Node mismatch, missing hooks/`.mcp.json`, Node-rejected token — and
  repairs the safe ones.
- **Seat / token minting.** `citadel seat create "Name" slug` mints a seat + a
  seat-scoped writer token (a teammate ingests ONLY into their `seat:slug`);
  `citadel seat token <slug>` mints a FRESH token for an EXISTING seat (re-link a
  lost token); `citadel token create` is for standalone/service-account tokens
  (warns it is NOT seat-scoped); `citadel token revoke <id>`. Every mint prints
  its write-scope; the seat token is the per-user "API key". Admin commands need
  `CITADEL_ADMIN_KEY`.
- **First-run onboarding.** Bare `citadel` on an interactive TTY auto-enters
  onboarding once, then shows the home screen (`--no-onboard` /
  `CITADEL_NO_ONBOARD` to skip; `install.sh` runs it via `/dev/tty`). `citadel
  onboard` verifies the pasted token, shows seat/role/access, and installs
  token→shell rc + git pre-push hook + Claude SessionEnd **and** SessionStart
  hooks + `.mcp.json` (+ optional capture roots); `--node-url` targets a custom
  Node.
- **Multi-tool MCP.** `citadel mcp add <tool>` / `citadel mcp list` auto-write
  Cursor, Codex, Gemini, Windsurf (token stays in the shell rc via an env
  reference); print a paste-in snippet for Claude user-scope, Cline, Zed (those
  store the token in plaintext). Pi has no native MCP (info note only).
- **Friendlier UX.** Shared ✓/✗ glyphs, animated cyan spinner + banner reveal,
  narrow-terminal truncation, friendly errors for bare subcommand groups / typos
  / missing args, clean Ctrl-C (exit 130), `--version`.
- **TUI removed entirely.** No more `citadel tui`, no `[tui]`/textual extra. The
  zero-dep stdlib client install is just `pipx install citadel-archive`. Upgrade
  with `pipx install --force citadel-archive --pip-args=--no-cache-dir` (plain
  `pipx upgrade` can pull a stale cached wheel).
- **One server change.** The Node's evolve auto-sync interval was shortened
  **6h → 1h** (`CITADEL_EVOLVE_INTERVAL_SECONDS=3600`), and the `/ingest` endpoint
  gained the inline `cognify` flag that the new CLI ingest relies on. Autonomous
  ingestion stays: SessionEnd hook (session → seat) + git pre-push hook (commit
  metadata → seat) + the hourly evolve cycle (GitHub/Linear/repo sync + cognify);
  personal-by-default → seat, promote to shared via `citadel promotion`.

**Deferred NEXT project:** event-driven sync — GitHub/Linear webhooks +
incremental cognify (replace the hourly poll with push-triggered ingest).

## 2026-06-29 (continued) — Cognee 1.2.2, Linear live, smoke verified, release closed

The remaining release tasks are done (verified via the live admin key through
`railway run`, since the Citadel MCP token was stale).

- **Cognee 1.2.1 → 1.2.2** (`7041563`) — patch bump (truth-subspace/retrieval,
  all opt-in, `DEFAULT_FEEDBACK_INFLUENCE=0.0`, no breaking changes). pyproject +
  `requirements.txt` (Railway installs from it) + uv.lock; 514 tests pass.
  **Deployed + boots healthy** (`/healthz` 200, scheduler re-armed, no cognee
  import/init errors).
- **Linear sync live** — `CITADEL_LINEAR_API_KEY` set; a forced sync ingested
  **200 issues → Central** (`central_ingested:true`, `last_synced_at` set).
  Recurring sync added as an **evolve stage** (`a77355f`, `_linear_sync_stage`
  before cognify) rather than a separate Railway service — it lands in shared
  pgvector and the in-loop cognify folds it into the graph. (`mirror_count:0` —
  no seat mirrors until Linear users are mapped to seats.)
- **Promotion smoke verified** — admin `GET /api/promote` (enabled), `POST
  /api/promote/run` dry-run for `seat:sarthi` (**HTTP 200**, engine evaluated
  candidates end-to-end → all `skip/not_relevant`), `GET /api/promotion/pending`
  (200, empty). The dashboard approve/reject click-through is data-blocked (queue
  empty — nothing queued to approve).
- **Graph served** — `/api/mesh/graph` returns **200 nodes / 369 edges**,
  `fallback:false` (200 = the `mesh_graph_max_nodes` display cap; actual 280).
- **Security follow-up:** credential rotation is tracked privately in the ops
  runbook, not here — operational credential state does not belong in a public
  repository.

## 2026-06-29 — Stable-release pass: PyPI v0.1.3, evolve scheduler, repopulation (cognify-blocked)

Shipped the ADR-0007 promotion CLI to PyPI and built the scheduled evolve path.
Graph repopulation is blocked on a cognee event-loop bug (below); everything else
landed.

- **PyPI v0.1.3** — bumped `pyproject` 0.1.2→0.1.3 + CHANGELOG; tag `v0.1.3`
  pushed → trusted-publish Action green. `pipx install citadel-archive` now lands
  the `citadel promotion {run,list,approve,reject}` subcommands (install-verified
  from PyPI). Commits `737bffa` (gitignore the personal workspace ingester),
  `c435bcc` (release).
- **Evolve scheduler (subprocess, in web service)** — the 6h evolve pass cannot be
  a separate Railway service: its promotion + cognify stages need the web
  service's `/data` volume (Kuzu graph + JSON access store), and Railway volumes
  attach to a single service. Built an env-gated scheduler in `kb/server.py`
  lifespan (`CITADEL_EVOLVE_SCHEDULER_ENABLED`, default off;
  `CITADEL_EVOLVE_INTERVAL_SECONDS=21600`) that runs `python -m scripts.run_railway`
  mode `evolve` as a **subprocess on the web container** each interval (a worker
  thread fails — cognee binds async resources to the loop that created them).
  Deployed (`69e9499`) + enabled on Railway web; boot log confirms
  `Evolve scheduler enabled`. 510 tests, ruff clean. Subprocess fix `8a52245`.
- **Cognify fix (two bugs, two-phase scheduler).** The first live pass ran
  `github_sync`/`repo_content_sync`/`self_improve`/`promotion` (now sees
  `seat:sarthi`) but `cognify` failed twice in a row, each a distinct bug:
  1. `got Future attached to a different loop` — each stage runs its own
     `asyncio.run()`; cognee caches a global async engine on the first stage's
     loop, dead by the cognify stage. (A worker thread and even a fresh
     subprocess both hit this.)
  2. `Could not set lock ... cognee_graph_kuzu (held by PID 123)` — Kuzu is a
     single-writer embedded DB; the evolve subprocess holds the graph lock during
     its add stages, so the web server can't cognify while it's alive.
  **Fix (`35e4c64`):** the scheduler now runs the heavy stages as a subprocess
  with `CITADEL_EVOLVE_COGNIFY_ENABLED=false` (it exits, releasing the Kuzu lock),
  then awaits cognify **in-loop** on the web's own Citadel — the sole writer, in
  the loop where cognee is happy. (Earlier `945e4a5` added a web-API cognify
  route, kept as a fallback for standalone `evolve` runs.)
- **Graph repopulated ✅** — a forced verification pass rebuilt the Kuzu graph to
  **280 nodes / 514 edges** (was ~25; past the ~214 target), `grew=True`, no loop
  or lock error. Steady state restored: 6h interval, incremental cognify.
- **Cron LLM_MODEL drift fixed** — `Citadel-GitHub-Sync` `LLM_MODEL` →
  `openrouter/deepseek/deepseek-v4-flash` (was prefix-less `openrouter/free`;
  moot in HTTP-endpoint mode but no longer a landmine).
- **Linear** — read-only `CITADEL_LINEAR_API_KEY` set on Railway web (the
  `linear-sync` cron + `GET /api/linear-sync` verify are still pending).
- **Security** — reading Railway vars surfaced live secrets (admin key, GitHub
  PAT, OpenRouter key, Postgres password) into the working session: **rotate
  them.** The Railway MCP token also went stale mid-session (the local CLI still
  works and was used for deploy/logs/vars).
- **Docs** — `install_autosync.sh` no longer exists (folded into `citadel
  onboard`); stale references corrected across `tasks.md` and the phase-2 plan.

## 2026-06-27 — ADR-0007 P5/P6 merged + promotion enabled in prod (PR #19)

- **Merged [PR #19](https://github.com/masumi-network/Citadel/pull/19)** → `main`
  (`a9aecbc`): Promotion Agent, approval queue, MCP tools, dashboard panel,
  `citadel promotion` CLI, grill-aligned docs/skills.
- **Production env** on Railway **Citadel-Archive**:
  `CITADEL_PROMOTION_ENABLED=true`, `CITADEL_PROMOTION_DRY_RUN=false`,
  `CITADEL_PROMOTION_RELEVANCE_THRESHOLD=0.7`, `CITADEL_PROMOTION_MAX_ITEMS=20`
  (see [`docs/operations.md`](operations.md)).
- **On demand live:** `POST /api/promote/run`, `/api/promotion/pending`, dashboard
  queue, MCP `citadel_promotion_*`. **Scheduled 6h pass** still needs a Railway
  `evolve` cron service.
- **Remaining:** evolve cron, browser QA, PyPI **v0.1.3** (`citadel promotion` for
  `pipx` users), production smoke with admin + seat tokens.

## 2026-06-27 — ADR-0007 P5/P6 promotion agent + approval (implemented)

- **Promotion engine** grill parity: masumi-org / Central reference checks, capture
  `org-work` gate, secret scan + LLM always, reject dedupe, promotion metadata tags.
- **API:** seat-scoped `POST /api/promote/run`, promotion pending approve/reject.
- **CLI:** `citadel promotion list|approve|reject|run` with `--json`.
- **MCP:** `citadel_promotion_pending|approve|reject`; dashboard Promotion Queue panel.
- **503 tests** passing locally before merge.

## 2026-06-27 — ADR-0007 promotion grill (design locked)

- **Grill-with-docs session** locked the **Promotion Agent** decision tree and
  **Promotion Approval** member model before P5/P6 code parity.
- **Decisions:** known masumi-org work auto-promotes after secret scan + LLM;
  **New Org Project** → member queue (agent proposes, member approves/rejects);
  hybrid **Capture Root Tags** (`org-work` only for capture auto-promote);
  no-repo-hint → **Central** match only; reject sticks; one-shot approval;
  surfaces = dashboard + MCP (human confirm) + `citadel promotion` CLI.
- **Docs updated:** `CONTEXT.md` glossary, ADR-0007 refinements section,
  shipping plan P5/P6 checklist, `tasks.md` code-gap list,
  `docs/agent-access-model.md`, proactive-ingest skill (removed stale
  `org-ready` → Central seat writes).
- **Next:** ~~align local promotion engine + CLI to grill spec; production enable
  `CITADEL_PROMOTION_ENABLED`.~~ Done in PR #19; follow-ups: evolve cron, PyPI v0.1.3, browser QA.

## 2026-06-27 — Published to PyPI + CLI polish (v0.1.0 → v0.1.2)

- **Published `citadel-archive` to PyPI** via GitHub Actions **trusted publishing**
  (OIDC, no stored tokens) — `pipx install citadel-archive`. Each tag →
  Action build+publish → GitHub Release. Shipped **v0.1.0, v0.1.1, v0.1.2**
  (latest release also carries the wheel + sdist as assets).
- **Professional README** rewrite (898 → 200 lines) + new
  [`docs/operations.md`](operations.md) for deploy/env/integrations; castle
  figlet hero. (PR #13)
- **Bootstrap installer** [`install.sh`](../install.sh) (`curl … | sh`): detects
  Python 3.10+, **prompts y/N to install it** (brew/apt/dnf/pacman) if missing,
  sets up pipx + the CLI, and ends by showing the home screen. (PR #14, #16)
- **Branded home screen** — bare `citadel` shows the large castle hero + a
  curated, colorized command menu instead of the argparse usage dump.
  (PR #15, v0.1.1)
- **Friendly unknown-command error** — `citadel stauts` → `✗ unknown command` +
  "did you mean? `citadel status`" (difflib). (PR #17, v0.1.2)
- **Verified live:** main `13eba2a` deployed on Railway (healthz 200, authed
  session OK); PyPI serving 0.1.2; **487 tests**, ruff + twine clean.

## 2026-06-27 — Teammate CLI shipped to prod (PR #11 merged + deployed)

- **Published-ready CLI** `citadel-archive` (command stays `citadel`): zero-dep
  client base; `[server]` / `[tui]` extras. `citadel onboard` (one-command,
  idempotent, self-contained bundled hooks), `citadel status` / `citadel tui`
  (dashboard replacement), `citadel setup` / `citadel capture`.
- **Headless** `--json` on onboard/setup/capture/status — agent- and CI-drivable
  (token from env, never argv); clean stdout, exit codes.
- **Brand:** castle banner + TTY-aware color (`kb/banner.py`); see `brand.md`.
- **Adversarial audit** (35 agents): 14 findings fixed feat-by-feat — incl. a
  HIGH TUI Rich-markup injection/crash, onboard foreign-hook backup + token
  rotation + shell-quote safety, status corrupt-config safety.
- **Merged PR #11 → main → Railway auto-deploy verified** (commit a53b1bb live,
  uvicorn up, /healthz 200, authed session OK). 484 tests, ruff + twine clean.
- Followed by the PyPI publish + CLI polish — see the entry above.

## 2026-06-27 — ADR-0007 design + security tightening

- **Design session (grill-with-docs):** locked **Seat Node Write Policy** — all
  seat-scoped writes → personal **Node** only; **Central** read-only for seats.
  **Central** updates via org sync, **Promotion Agent**, service accounts.
- **Capture model:** **Approved Capture Roots** (local) + **Capture Policy**
  (server hybrid); v1 triggers git push + `citadel capture`; preset **Capture Root
  Tags** (`personal` / `org-work`).
- **Promotion model:** **Promotion Agent** cross-refs GitHub org repos + **Central**;
  auto-promote known work; **New Org Project** → **Promotion Approval** (dashboard
  + MCP; admin delegate with audit). 6h cron + on demand.
- **ADR-0007** accepted; ADR-0003 partially superseded (seat org-tag → Central removed).
- **Code (local, partial):** MCP seat write guards, extended secret scan (`ctdl_`,
  DB URLs), skill/MCP doc updates. **384 tests** passing before P1 HTTP parity.
- **Next:** P5 Promotion Agent (GitHub + Central refs, tag rules, 6h cron).

### P4 shipped (same session) — capture CLI

- **`citadel setup`** — wizard (interactive + non-interactive `--root PATH[=tags]`)
  writes `~/.citadel/capture.json`: Node URL + Approved Capture Roots with
  **Capture Root Tags** (`personal` never promotes, `org-work` eligible). Seat
  token stays in env, never in the file. (`kb/capture_config.py`)
- **`citadel capture`** — summarizes each approved root (git metadata + README
  blurb, not raw files) and POSTs to the Node `/ingest`; `--dry-run`, `--root`,
  `--config`. (`kb/capture.py`)
- **Pre-push hook allowlist gate** — `sync_push.py` now only captures pushes
  from inside an Approved Capture Root once a config exists (skip + warn
  otherwise; back-compat always-on when no config). Matched root's tags ride
  along. Stdlib-only contract kept.
- **`citadel onboard`** — one-command teammate setup (`kb/onboard.py` + thin
  `citadel-onboard` skill): pastes token → shell rc (masked, written once),
  installs git-push + SessionEnd hooks, adds the Citadel MCP server
  (optional, default-on; token stays an env reference, never in `.mcp.json`),
  and offers Approved Capture Roots. Idempotent; merges into existing config.
- **`citadel status` + `citadel tui`** — teammate dashboard replacement
  (`kb/status.py` shared core): node `/healthz`, auth `/api/session` whoami
  (seat/role/capabilities), search smoke, local setup (token/MCP/hooks/capture
  roots), recent activity. `--json` is the AI-agent path (Claude/Codex/Cursor);
  `citadel tui` is a live textual dashboard (`textual` optional `[tui]` extra).
  Verified against prod (node 142ms, auth valid). MCP stays optional — sync is
  HTTP+token; MCP only for in-session search/ingest.
- **Self-contained hooks** — moved the autosync hooks into the package
  (`kb/hooks/sync_push.py`, `kb/hooks/sync_session.py`, run as `python -m
  kb.hooks.*`); `citadel onboard` now installs a self-contained `.git/hooks/
  pre-push` + SessionEnd hook with **no vendored skill** (verified end-to-end in
  a fresh repo). Removed the redundant `install_autosync.sh` + templates;
  consolidated all install docs to `citadel onboard`. `twine check` passes.
- **Packaged for publish** — renamed distribution to `citadel-archive`
  (command stays `citadel`); base install is the lightweight client
  (python-dotenv only), with `[server]` (cognee/fastapi/…) and `[tui]` (textual)
  extras; lazy `kb/__init__` + server-handler imports keep the client free of
  the server stack (subprocess boundary test guards it). PyPI Trusted Publishing
  workflow (`.github/workflows/publish.yml`) + `PUBLISHING.md`: tag `v*` →
  builds + publishes, no tokens. `pipx install citadel-archive`.
- Docs: teammate-rollout step 5 + fast-path + status/tui + proactive-ingest skill.
- **Production-hardening pass** (adversarial multi-agent audit, 47 confirmed
  findings): `post_capture` HTTPS-only + no-redirect + size cap (token-leak /
  unbounded-payload fixes, parity with `sync_push.post_ingest`); `citadel
  capture` catches node-down errors + returns real exit codes; allowlist gate
  **fails closed** on corrupt config (was fail-open) and matches symlinks via
  realpath; dropped admin-key token fallback; removed dead `find_root_for_path`.
- **435 tests** passing, ruff clean.

### P3 shipped (same session)

- **`GET/PUT /api/access/seats/{slug}/capture-policy`** — admin baseline per seat; seat token read-only.
- **`GET /api/access/capture-baseline`** — org env excludes + default deny globs merged view.
- **`kb/capture_policy.py`** — `merged_deny_globs()` merges `CITADEL_EXCLUDE_PATTERNS`, org defaults, seat baseline.
- Settings + Access UI snippets for admin view/edit.
- **396 tests** passing.

### P1 shipped (same session)

- **`guard_seat_write_policy`** on all channels (not MCP-only).
- Seat **`resolve_write_targets`** always → own **Node**; org/promotion tags → 403.
- Seat **`/api/contribute`** → 403; Obsidian org tags stripped on push.
- **385 tests** passing.

## 2026-06-26

- **Graph UI unified org view** (local, pending commit): removed All / My Node /
  Central scope toggles; the mesh always shows seat **Nodes** and **Central**
  together. Depth slider (0–3 hops) and Central↔seat hub spokes unchanged.
- **Central visibility fix:** `_ensure_base_graph` always seeds the
  `masumi-network` dataset node (not only `default_dataset`), so Central appears
  for admin and seat sessions alike.
- **Seat form UX:** `formatApiError` surfaces FastAPI validation messages; slug
  HTML pattern aligned with server `min_length=2`.
- **Docs pass:** progress, tasks, phase-2 plan, onboarding, CONTEXT, README,
  skills — aligned to autonomous sync layers, Linear → **Central** (read-only
  key OK), and agent sync policy (fail-silent; cron owns org sources).
- Tests **346 passing**.

## 2026-06-26 (continued) — committed, pushed, deployed + live prod audit

- **Committed the local batch in 5 sequential commits** and pushed
  `b9eccd3..6062e9c` to `main`: `fix(mesh)` always-seed-Central, `feat(ui)`
  unified org graph + seat-form validation, `fix(linear-sync)` AccessStore in
  cron mode, `docs:` 2026-06-26 pass, `chore(skills)` skills-lock.json.
- **Railway redeployed and healthy on the new commit:** web deployment
  `f7b9d2ad` = `6062e9c` reached `SUCCESS` (prior `b9eccd3` REMOVED);
  `/healthz` 200.
- **Live production assessment** (read-only reader-token probe):
  - **GitHub org sync healthy** — `/api/sources`: 45 documents tracked, 45
    repos, last sync `2026-06-25T09:00Z`, security scan passed.
  - **Vector search works** — `/search` returns real `masumi-network` chunks,
    but the first query after a redeploy takes **>45s** (cold-start model
    load; earlier attempts returned HTTP 000). A warmup/readiness ping is a
    follow-up.
  - **Knowledge graph EMPTY in prod** — `/api/mesh/graph` →
    `fallback_reason: "graph_empty"`, 0 nodes / 0 edges. Data is `add`-ed
    (vector index populated) but `cognify` has not built the Kuzu graph: the
    stranded-data recovery is still outstanding. **Action: run cognify
    (`POST /api/cognify/run?force=true` or `CITADEL_RUN_MODE=cognify`).**
  - **No seats provisioned** — mesh shows only Central (`masumi-network`);
    zero `seat:` nodes. Per-dev seat + token + `install_autosync.sh` pending.
  - **Linear sync disabled** — `/api/linear-sync` `enabled:false` (no key).
- Outstanding lint: `ruff` `F401` unused `fnmatchcase`,
  `kb/repo_content_sync.py:15` (pre-existing).

## 2026-06-26 (continued — cognify root-cause + LLM/graph fixes)

Two production bugs found and fixed; the knowledge graph is now *buildable* but
not yet *repopulated*. Status below is live-verified — no unverified claims.

- **LLM model outage (fixed + verified).** Prod `LLM_MODEL` was
  `google/gemini-2.5-flash` — a bare id with no litellm provider prefix — so
  every cognify LLM call 500'd (`litellm: LLM Provider NOT provided`). Set prod
  to `openrouter/deepseek/deepseek-v4-flash` (the repo default). **Verified:** a
  force cognify then built a **214-node / 385-edge** graph (HTTP 200,
  `graph_after.nodes=214`) where it previously hard-500'd. Documented the
  `openrouter/` prefix rule in README + `.env.example`; the enrichment var
  `CITADEL_LLM_MODEL` stays bare (it calls OpenRouter's HTTP API directly, not
  litellm). Commit `03fd27c`.
- **Graph not displaying — root-caused + fixed.** Despite cognify building 214
  nodes, `/api/mesh/graph` read 0. Cause (cognee-source investigation): cognee's
  `ENABLE_BACKEND_ACCESS_CONTROL` defaults ON for kuzu+pgvector, partitioning
  the graph into per-dataset/per-user Kuzu files
  (`<system>/databases/<user>/<dataset>.pkl`), while Citadel's org-wide
  `graph_data()` read resolves the global `cognee_graph_kuzu` DB. The built
  graph was real but stranded in the per-dataset partition. Set
  `ENABLE_BACKEND_ACCESS_CONTROL=false` (prod env + `.env.example`) so cognify
  and the read share one global graph — correct for a single-tenant org vault
  (Citadel enforces seat/dataset isolation at its own access layer). Commit
  `171f386`.
- **Graph display — fixed + verified end-to-end.** A re-ingest (force
  learning-agent run, which 502'd at ~3.5min through the public proxy but added
  content server-side first) re-added data under the new global context; a
  force+verify cognify then confirmed it: `/api/mesh/graph` now returns
  **25 nodes / 38 edges, `fallback:false`** across fresh requests (was
  `0 / graph_empty`), and a marker round-trips (ingest → cognify → search hit).
  The dashboard org graph renders. **Partial:** 25 nodes is the interrupted
  re-ingest + marker, not the full org corpus (~214). Full repopulation needs
  the complete GitHub re-sync, which 502s through the public proxy, so it must
  run via the internal cron (`Citadel-GitHub-Sync`, `*_TIMEOUT_SECONDS=2400`)
  or heal on the next scheduled run. A `COGNIFY_TEST_MARKER` node is present
  (harmless verify artifact).
- `/search` returns results (8 for `masumi`) — **vector retrieval works**.
  - deepseek-v4-flash (a reasoning model) shows `InstructorRetryException`
    JSON-validation retries during extraction; it mostly recovers. A/B to
    `openrouter/openai/gpt-4o-mini` is available if extraction needs to be
    cleaner.
- Earlier commits this session: live-audit log (`837961d`); README
  `openrouter/free` landmine fix + `citadel_run_repo_content_sync` SKILL doc +
  mesh error-event redaction + dropped unused import (`aa227ea`, `caffb95` —
  the latter clears the only ruff `F401`).

## 2026-06-25 (continued)

- **Phase 2 implementation batch** (merged on `main`, `5f6c0ed`+):
  - **M1** git push sync: `sync_push.py`, pre-push hook, 7 tests.
  - **M2** `install_autosync.sh`, Cursor/Codex doc, skill updates.
  - **M3–M4** Linear: `kb/linear_sync.py`, Central + Seat-Scoped Mirror,
    `/api/linear-sync`, `CITADEL_RUN_MODE=linear-sync`, MCP
    `citadel_linear_my_issues` + `citadel_linear_search`, ADR-0004.
  - **M5** graph UI: universal org view (seat nodes + Central together), depth
    slider 0–3, Central↔seat hub spokes.
  - Tests **340 passing** at merge; **346** after follow-up fixes.

## 2026-06-25

- **Graph Phase 1 merged & deployed** (PR #5 → `main` at `ffabc1f`). Production
  verified: `force-graph.min.js` 200, Three.js bundles 404, `/healthz` ok.
- **M1 git push sync shipped** (local, pending commit): `sync_push.py` +
  `git-pre-push.sh` template — commit snapshot on every push to seat **Node**;
  7 unit tests in `tests/test_sync_push.py`.
- **Knowledge-graph redesign — Phase 1 complete** (`feat/graph-logseq`, commit `a2770e0`).
  Replaced the Three.js 3D scene with a vendored 2D `force-graph` (Logseq-style):
  Central pinned at the centre, seat vaults tiered by size, hover neighbour dimming,
  click-to-inspect, labels-on-zoom, Fit/Pause controls, and Activity ↔ Knowledge
  graph toggle. Removed dead 3D layout code; timeline graph focus works in both modes.
  Pending: merge PR to `main` (M0.4).
- **Phase 2 design session — autonomous sync + graph.** Locked the execution plan
  in [`docs/phase-2-shipping-plan.md`](phase-2-shipping-plan.md) (~18% overall):
  - **Autonomous Node Sync** — background, fail-silent, zero extra dev steps.
  - **Git push** — universal commit snapshot → seat **Node** (Cursor, Codex, Claude).
  - **Session hooks** — supplementary (`SessionEnd` for Claude Code already shipped).
  - **Linear** — full workspace → **Central**; assignee issues **Seat-Scoped Mirror**
    → each seat's **Node** (John's tasks in his Node for "what do I need to do?").
  - **Graph UI Phase 2** — universal org view (seat **Nodes** + **Central**
    together), local depth, Central↔vault spokes (after Nodes have content from
    sync). Scope toggles were dropped in favour of one org-wide canvas.
  - Glossary updated: **Seat-Scoped Mirror** in `CONTEXT.md`.
  - Ship order: M0 merge → M1 git push → M3 Linear → M4 Linear MCP → M5 graph → M6 deploy.

## 2026-06-24

Major session: fixed broken ingest, upgraded the engine, shipped the per-seat
SaaS onboarding + autonomous sync, and started the knowledge-graph redesign.

- **Ingest was broken in production — root-caused and fixed.** `cognee.add`
  stored items but `cognee.cognify` failed on every one (empty knowledge graph,
  searches returned nothing). Cause: the Railway env var `LLM_MODEL=openrouter/free`
  is not a valid model id, so every litellm call during cognify returned
  `OpenrouterException - Invalid URL`. Fixed by setting
  `LLM_MODEL=openrouter/openai/gpt-4o-mini` on the web service (config only).
  Verified end-to-end: a marker note ingests (`cognee_result.status=completed`,
  `error=null`) and is found by search.
- **cognee 1.1.2 -> 1.2.1** (PR #2). Clean lock re-resolution; the `cognee_client`
  call surface is version-defensive and the breaking env renames in the window are
  unused by Citadel. Deployed and verified live (clean boot, no Kuzu/auth-flip
  errors, data survived the upgrade).
- **Re-cognify / verify recovery tooling** (PR #2). New admin `POST /api/cognify/run`,
  CLI `citadel cognify [--verify]`, and `CITADEL_RUN_MODE=cognify` / `cognify-verify`
  run-modes that re-cognify already-added-but-uncognified data and (in verify mode)
  ingest + cognify + search a marker as an end-to-end health check. An adversarial
  review caught a bug where verify skipped the recovery cognify; fixed so verify is
  a superset.
- **GitHub-Sync cron 502 fixed** (env only). The daily cron invoked a ~26-min sync
  as one synchronous HTTP call to the public domain (proxy kills idle connections at
  ~5 min). Pointed it at the internal domain `http://citadel-archive.railway.internal:8080`
  with `CITADEL_GITHUB_SYNC_TIMEOUT_SECONDS=2400`; cognify runs in the fixed web
  service. Heals the items stranded during the broken era on its next run.
- **Per-seat onboarding** (PR #3), on the existing seat/node/Central engine:
  - **Connect wizard** — Create Seat renders a ready-to-paste `.mcp.json` (Claude
    Code + Codex) with the seat's scoped writer token + origin-derived `/mcp/` URL +
    copy buttons + a personal-vs-shared explainer.
  - **Self-describing seat** — `resolved_memory_scope` surfaces the caller's own
    `seat_slug` + node label (out through `/api/session` + `citadel_session`);
    `citadel_ingest`/`search`/`contribute` docstrings state personal-by-default,
    tag-to-share.
  - **Seat inventory** — admin `GET /api/access/seats` + per-seat revoke in the UI.
- **Autonomous personal-KB sync** (PR #4). A project-committed Claude Code `SessionEnd`
  hook (`skills/citadel-proactive-ingest/`) runs a stdlib-only `sync_session.py` that
  distills a dev's session and POSTs it to their private seat node — reusing the one
  `CITADEL_MCP_ACCESS_TOKEN` they already set for MCP, personal-by-default, HTTPS-only,
  refuses redirects, fail-silent. Plus a proactive-ingest skill + dev onboarding docs.
  Zero per-session steps; the only one-time step is exporting the token (the wizard
  delivers it). Teammates are headless (token + MCP + skill, no dashboard login).
- **Knowledge-graph redesign — Phase 1 started** (`feat/graph-logseq`). See
  2026-06-25 entry for completion.
- **Backprop:** `test_github_sync_returns_open_and_merged_pull_requests` hardcoded
  absolute PR dates that aged out of the reporting window; made it time-relative.
- Tests: 312 -> 328 passing across the session; every adversarial-review finding fixed.

## 2026-06-17

- Reviewed the seat/node/central Phase 1+2 work (commit `2cd3ac9`,
  `feat(access): add seat provisioning and multi-dataset search`) against
  ADR-0003 and hardened six isolation/correctness gaps. Changes are local on
  `main`, verified but not yet committed/pushed.
- Closed the seat-isolation gaps in `kb/server.py` and `kb/access.py`:
  - **Default-deny `seat:` namespace.** `enforce_dataset_allowlist` no longer
    lets a token with an empty `allowed_datasets` reach a seat node by naming it.
    Previously any legacy/non-seat token could read or write another seat's
    `seat:{slug}` node; now only the owning seat (plus audited admin/env bypass)
    can. Ordinary (non-seat) datasets stay open for unscoped tokens for backward
    compatibility.
  - **Seats cannot be admin.** `create_seat(role="admin")` is rejected and the
    Admin option is removed from the seat form, because an admin token bypasses
    the allowlist and would dissolve the node boundary. Admin tokens are issued
    directly via token creation.
  - **Central allow-entry derived from config.** `create_seat` now takes the
    resolved `central_dataset(config)` instead of hardcoding `masumi-network`, so
    the seat allowlist can no longer drift from the dataset the router targets
    when `CITADEL_GITHUB_SYNC_DATASET` is overridden.
  - **Central is curated.** A seat-holder's explicit write to the Central dataset
    must carry an org tag (`org-ready` / `vault-contribution`) or go through
    `/api/contribute`; an untagged direct write to Central is rejected (403).
    Admin/env callers and non-seat service accounts keep their direct path.
- Hardened multi-dataset search merge: `search_across_datasets` now queries every
  allowed dataset before ranking, with a reserved slice for secondaries, so a
  result-rich node can no longer short-circuit and silently drop Central. Dedup
  still favors the node copy.
- Added scope-override auditing: when a bypassing caller that carries its own
  allowlist reaches outside it, search/ingest/contribute audit detail records
  `scope_override: true`.
- Documented the model changes in `docs/adr/0003-seat-node-central-private-memory.md`
  (three new Consequence bullets) and `docs/agent-access-model.md` (Read/Write
  Scope, Admin Override, Token Memory Scope, and Security Rules).
- Verified with `uv run pytest -q`: 301 passed (294 prior + 7 new tests covering
  cross-seat denial, unscoped-token denial of a seat node, admin-seat rejection,
  the curated-Central gate, scope-override auditing, and the configurable Central
  allow-entry).
- Addressed the PR #1 (Cursor Bugbot) review — three further seat-isolation gaps,
  shipped as `84fdde6` (fix), `fb5dd74` (test), `d88ec79` (docs):
  - **Seat session leaked to Central search.** `search_across_datasets` applied a
    single `session_id` to every dataset, so a seat's `default_session` scoped the
    Central leg and hid org-wide hits. Sessions are now resolved per dataset
    (`resolve_search_sessions`): the implicit `default_session` scopes only the
    caller's own node; shared datasets are searched session-wide. An explicit
    `session_id` still applies to whatever was searched.
  - **Curated-Central gate bypassable.** The gate keyed off `default_dataset`
    only, so a token defaulting to Central skipped the org-tag requirement and the
    default-target branch had no gate. Seat membership is now judged by storage
    scope (`is_seat_identity`: a `seat:` node in `default_dataset` or
    `allowed_datasets`) and the gate (`guard_curated_central`) runs on both
    explicit and default targets. Scope-based detection deliberately covers the
    agents scoped into a seat node — they are `service_account` principals with no
    `seat_slug`, so a principal-identity check would under-gate them.
  - **Obsidian push ignored tag routing.** `resolve_write_dataset` passed empty
    tags, trapping org-bound notes in the node. The push loop now routes per
    document with the real tags via `resolve_write_targets` +
    `execute_learning_writes`, matching `/ingest`.
- Recorded the resolved design decisions in ADR-0003 and `CONTEXT.md`: seat
  detection by storage scope (covering a human's tokens and their agents), the
  default-target gate, and per-dataset session isolation.
- Verified with `uv run pytest tests/test_server.py tests/test_obsidian_sync.py -q`:
  70 passed (3 new regression tests). Pre-existing unrelated failure
  `test_github_sync_returns_open_and_merged_pull_requests` (date-window assertion)
  is not from this work.
- Ran a full adversarial (Bugbot-style) audit of the PR and fixed the gaps it
  surfaced:
  - **Cross-seat session read (the notable one).** Nothing validated a
    caller-supplied `session_id`, and session-scoped recall ignores the dataset
    allowlist, so a seat could name another seat's guessable `seat-{slug}` session
    and read its private node. Added `assert_requested_session_allowed`: a
    non-bypass caller may name only its own `default_session` (else 403);
    admin/env keep full reach. Enforced in both `resolve_session_id` (writes) and
    `resolve_search_sessions` (search), and an explicit own session now scopes the
    node only — Central stays session-wide.
  - **Session-scoping edge.** `resolve_search_sessions` no longer drops a session
    when the caller has no node of its own — a single-dataset search still scopes
    to that one dataset.
  - **Obsidian audit clarity.** The push audit now records `written_datasets`
    (where tag routing actually landed content) alongside the vault's home
    binding.
  - Accepted as intentional: scope-based seat detection can gate a service
    account granted seat-node read (Option A trade-off), and Obsidian-promoted
    Central writes keep conflict detection off (Obsidian's revision model).
- Verified with `uv run pytest -q`: 304 passed (2 new session tests), only the
  pre-existing unrelated github-sync date-window test failing.

## 2026-06-11

- Shipped the Logseq-inspired Live Knowledge Timeline work in small commits:
  - `2ea4f46` (`docs: map live knowledge timeline`) captured the product map,
    fast read path, live update path, event model, and performance rules.
  - `e17d9af` (`feat(api): add knowledge event timeline`) added normalized mesh
    event envelopes and `GET /api/knowledge/events` with `after_id`, `limit`,
    `type`, and `kind` filters.
  - `b484817` (`feat(ui): add live knowledge timeline`) rebuilt the Activity
    page into a live timeline with chunk freshness counters, selectable event
    rows, an inspector, and graph focus for related dataset/source/vault/org
    nodes.
  - `a2f3a19` (`docs: document live knowledge timeline`) updated README and the
    timeline design doc after the feature shipped.
- Added timeline freshness state to `/api/mesh` snapshots:
  - `indexed_chunks`, `pending_chunks`, `failed_chunks`, `last_indexed_at`, and
    `latest_event_id` now give the UI a fast indexed/chunked status read without
    fetching raw source data.
  - Live SSE mesh events keep the existing `id`, `type`, `message`, `details`,
    and `created_at` fields and now include a compact `timeline` envelope.
- Verified the backend and UI changes before pushing:
  - `uv run pytest tests/test_mesh.py tests/test_server.py` passed.
  - `uv run ruff check kb/mesh.py kb/server.py tests/test_mesh.py tests/test_server.py` passed.
  - `node --check kb/static/app.js` passed.
  - `git diff --check` passed.
- Confirmed production data safety before running sync work:
  - Railway production services `Citadel-Archive`, `Citadel-GitHub-Sync`, and
    `Postgres` all reported `SUCCESS` and `stopped=false`.
  - Postgres still has its dedicated persistent `/var/lib/postgresql/data`
    volume; the web and GitHub sync services both have `/data` volumes.
  - The GitHub sync service has `DATABASE_URL` and
    `CITADEL_GITHUB_SYNC_TARGET_URL`, so the manual cron run targeted the
    production web API and production database path rather than local defaults.
- Ran the GitHub sync cron path manually through Railway production variables:
  - The run called `https://citadel-archive-production.up.railway.app/api/learning-agent/run`.
  - It completed with `ok=true`, `dry_run=false`, `ingested=true`, and
    `improved=false`.
  - It scanned 42 repositories, found 2 changed repositories, 50 organization
    events, 10 commits, 4 open PRs, and 6 merged PRs.
  - The security scanner returned `ok=true`, `blocked=false`, and
    `finding_count=0`; Google Chat remained disabled.
- Ran the Vault Backup Mirror cron wrapper safely through the production web API
  in dry-run mode:
  - The manifest dry run returned `ok=true`, tracked 3 files, found 2 available
    files, 1 missing Obsidian state file, and 105501 tracked bytes.
  - It wrote and published nothing because production backup mirror config still
    has `enabled=false` and push disabled.
  - The manifest policy still excludes raw tokens, secret values, source bodies,
    embeddings, vector indexes, graph databases, and large binaries.

## 2026-06-08

- Checked current Citadel automation and tightened the cron/gateway path:
  - GitHub reports no Actions workflows and no Actions runs for
    `masumi-network/Citadel`; active automation is Railway, not GitHub
    Actions.
  - Railway production has `Citadel-Archive`, `Citadel-GitHub-Sync`, and
    `Postgres` deployed successfully.
  - `Citadel-Archive` is running on
    `citadel-archive-production.up.railway.app`; recent logs show startup and a
    successful `/healthz` response, with no recent HTTP `>=400` logs returned.
  - `Citadel-GitHub-Sync` is scheduled at `0 3 * * *` UTC with next run
    `2026-06-09T03:00:00Z`. It still uses `CITADEL_RUN_MODE=github-sync`, which
    is a compatibility alias for the learning-agent cron wrapper.
  - The cron service has target URL, access key, and GitHub token configuration;
    Citadel Google Chat credentials are unset, matching the Scout-owned gateway
    boundary.
  - A dry-run invocation through Railway production variables completed with
    `ok=true`, scanned 42 repositories, found 7 changed repositories, 49 org
    events, 24 commits, 6 open PRs, and 12 merged PRs, and left ingestion plus
    gateway posting disabled.
  - Refactored learning-agent gateway delivery to post configured gateways
    concurrently and avoid recomputing gateway status in the status endpoint.
  - Updated cron logging to summarize sanitized generic gateway delivery status
    instead of only the legacy Google Chat compatibility field.
- Created and pushed the separate Scout update-agent repository:
  - Repository: `https://github.com/masumi-network/Scout.git`.
  - Commit `5bc78d9` (`Scaffold Scout update agent`) is on Scout `main`.
  - Scout owns update-agent orchestration and delivery gateways while Citadel
    remains the Organization Vault/source contract.
  - Added a Citadel client, modular gateway registry, Google Chat gateway,
    CLI entrypoint (`uv run scout status`, `uv run scout run --post`), config
    example, and focused tests.
  - Added Scout's gateway guide at `docs/gateway-guide.md` with Google Chat
    setup, local smoke tests, deployment rules, failure modes, and the adapter
    contract for future gateways.
  - Verified Scout with `uv run pytest` and `uv run ruff check .`.
- Added Citadel-side modular gateway support for the external-agent split:
  - Added `kb/notification_gateways.py` with a small `NotificationGateway`
    protocol and configured gateway registry.
  - Refactored `LearningAgent` to emit `notifications.gateways` while preserving
    the existing `notifications.google_chat` compatibility key.
  - Added generic admin-only gateway smoke testing at
    `/api/learning-agent/gateways/{gateway_name}/test`.
  - Updated cron summary output to include sanitized gateway delivery status.
  - Documented the repo boundary and migration path in
    `docs/internal-update-agent-architecture.md`.
  - Updated the Google Chat rollout plan and README to describe Scout as the
    long-term poster and Citadel's built-in Chat delivery as a compatibility
    path.
- Fixed a time-sensitive GitHub sync PR test whose hard-coded June 3 PR
  timestamps had fallen outside its 48-hour window by June 8, 2026.
- Corrected the Agent Messenger boundary:
  - Reverted the Citadel Agent Messenger bridge/API/config commits because
    Citadel should remain shared memory, not a messaging agent.
  - Moved Agent Messenger delivery responsibility to Scout, where the update
    agent owns outbound gateway communication.
  - Updated the external-agent architecture note to name Agent Messenger as a
    Scout-owned gateway and state that Citadel should not become an Agent
    Messenger actor.
- Verified Citadel with `uv run pytest` and focused `uv run ruff check`.

## 2026-06-04

- Committed and pushed private GitHub sync privacy/security hardening:
  - Commit `f95486f` (`feat(github): harden private sync digests`) is on
    `main`.
  - Verified before push with `.venv/bin/python -m pytest`,
    `.venv/bin/python -m ruff check .`, and `git diff --check`.
  - Added summary-only cron output so scheduled logs expose counts and scan
    status rather than raw private repository payloads.
- Verified Railway post-deployment state for commit `f95486f`:
  - `Citadel-Archive` deployment `4081a3ad-c8cc-4913-90f6-bb194b3d00f1`
    reached `SUCCESS`.
  - `Citadel-GitHub-Sync` deployment
    `027df285-2a4f-4499-a193-40d64d6c32d2` reached `SUCCESS`.
  - `Postgres` remained `SUCCESS`.
  - Live `/healthz` returned `{"ok":true,"service":"citadel"}`.
- Ran the GitHub sync cron path manually through Railway production variables:
  - `railway run --service Citadel-GitHub-Sync --environment production ...`
    called the hosted `/api/learning-agent/run` endpoint with summary-only
    output.
  - The run completed with `ok=true`, `dry_run=false`, `ingested=true`, and
    `improved=false`.
  - It scanned 42 repositories, saw 1 changed repository, 1 organization event,
    1 commit, 5 open PRs, and 4 merged PRs.
  - The security scanner returned `ok=true`, `blocked=false`, and
    `finding_count=0`.
  - Google Chat delivery was not attempted because production returned
    `google_chat_disabled`.

## 2026-06-03

- Added Google Chat Organization Update Digest support:
  - `kb/organization_digest.py` builds a constructive source-linked digest from
    GitHub PR/activity data and recent Citadel context, with an OpenRouter-backed
    agent read and deterministic fallback.
  - `kb/google_chat.py` posts outbound-only messages via Google Chat API app
    auth, bounded retries, thread keys, client message IDs, and sanitized
    delivery status.
  - The learning-agent run now supports preview-only manual runs and explicit
    `post_to_chat` delivery for scheduled or admin-triggered posts.
  - Added an admin-only Google Chat test endpoint for rollout smoke tests:
    `/api/learning-agent/google-chat/test`.
  - Updated the Source Sync dashboard action to run the learning-agent path, show
    digest preview and Google Chat status, and expose a separate Google Chat
    smoke-test button.
  - Added ADR 0002 and the rollout plan in
    `docs/google-chat-organization-update-digest-plan.md`.
  - Verified with `uv run ruff check .` and `uv run pytest`.
- Checked Railway rollout state for the digest:
  - Project `Citadel Archive`, production service `Citadel-GitHub-Sync` is still
    scheduled for `0 3 * * *`.
  - The cron service still has a start command override:
    `python -m kb.github_sync --org masumi-network`.
  - Target state is documented in the Google Chat rollout plan before mutating
    production Railway config.
- Installed this workspace's project MCP config against the hosted Citadel MCP
  endpoint:
  - `.mcp.json` now points to
    `https://citadel-archive-production.up.railway.app/mcp/`.
  - The config uses `${CITADEL_MCP_ACCESS_TOKEN}` and does not store a raw token.
- Added persistent MCP audit attribution:
  - MCP forwarded calls are recorded as `mcp.<tool_name>` audit events.
  - Events capture actor, role, tool, path, required role/scope, dataset when
    known, and success/failure.
  - Search queries, note bodies, feedback text, and tokens are not stored in the
    MCP audit detail; query and QA IDs are hashed where useful.
- Enforced token scopes server-side:
  - Protected API routes now require both a minimum role and the matching scope.
  - Bootstrap env keys use default role scopes.
  - Custom-scoped service-account tokens can only reduce permissions; scopes
    that exceed the selected role are rejected.
  - Session capabilities now reflect effective scopes, not only role labels.
- Added admin audit visibility for MCP operations:
  - Audit page has filters for all events, MCP events, non-MCP access/admin
    events, and failures.
  - The dashboard summarizes MCP event count, MCP failures, and distinct MCP
    actors.
  - Audit detail rendering redacts sensitive-looking fields by key.
- Added server-side audit views for admin/API clients:
  - `/api/audit` supports `view=all|mcp|access|failures` and a bounded `limit`.
  - Responses include summary counts for total events, returned events, MCP
    events, MCP failures, failed events, access events, and distinct MCP actors.
- Added a manifest-only Vault Backup Mirror tracking layer:
  - `kb/backup_mirror.py` tracks GitHub sync, Obsidian sync, and access/audit
    state files by path, size, timestamp, and SHA-256 hash without copying raw
    file bodies.
  - `/api/backup-mirror` and `/api/backup-mirror/run` expose admin status and
    dry-run/write flows; non-dry-run writes require
    `CITADEL_BACKUP_MIRROR_ENABLED=true`.
  - `scripts/run_backup_mirror.py` provides a cron-friendly wrapper for hosted
    API or in-process manifest export.
  - The Settings page now shows backup mirror status from the API.
  - Optional GitHub push publishes only `manifests/latest.json` and dated
    `snapshots/.../manifest.json` through the Contents API when
    `CITADEL_BACKUP_MIRROR_PUSH_ENABLED=true` and a dedicated mirror token is
    configured.
- Replaced Railway's inline shell start command with `scripts/run_railway.py`:
  - `web` execs Uvicorn.
  - `learning-agent`/`github-sync` run the GitHub learning cron wrapper.
  - `backup-mirror` runs the Vault Backup Mirror manifest cron wrapper.
- Added admin MCP tools for backup mirror operations:
  - `citadel_backup_mirror_status` inspects manifest status.
  - `citadel_run_backup_mirror` runs manifest export and defaults to dry-run.
- Added `citadel_audit_events`, an admin MCP tool for bounded
  `all|mcp|access|failures` audit views backed by the same `/api/audit` redaction
  path.
- Updated dashboard MCP setup snippets to use the hosted `/mcp/` endpoint instead
  of the older local `uv` wrapper path.
- Updated hosted MCP docs/templates so the no-clone `/mcp/` URL is the primary
  setup path, with the stdio wrapper left as a fallback/dev path.
- Added verifiable hosted skill metadata:
  - `/skills` now includes `size_bytes`, `sha256`, and SRI-style `integrity`
    values for each bundled skill.
  - `/skills/*` responses include matching digest headers and a content-derived
    ETag so agents can verify the markdown they loaded.
- Added a public well-known agent discovery manifest:
  - `/.well-known/citadel.json` lists the hosted MCP endpoint, token
    requirements, MCP tool policy metadata, approval recommendations, skill
    hashes, and public/private boundary rules.
  - The manifest is metadata-only and does not expose datasets, vault contents,
    Obsidian sync data, audit events, backup mirror contents, or raw tokens.
- Added MCP-native discovery:
  - `citadel_discovery` lets connected agents fetch the same safe discovery
    manifest after an authenticated `/api/session` probe.
  - `citadel://discovery` exposes the public manifest as a lightweight MCP
    resource without requiring vault/search reads.
- Added agent-facing search provenance metadata:
  - `/search` now adds an additive `_citadel` envelope to dict results with
    rank, dataset, stable result ID, content hash, source provenance hints, and
    retrieval safety flags.
  - Document drill-down is explicitly marked with
    `_citadel.retrieval.document_drilldown_available` so agents do not assume
    every generated chunk ID can be fetched as a full source document.
- Surfaced provenance in the dashboard search results:
  - Search cards now show source, path, session, dataset, content hash, and
    untrusted-context status before the raw JSON payload.
  - Full-source links only render when the backend marks document drill-down as
    available.
- Added baseline browser security headers:
  - HTTP responses now include a self-only CSP, `nosniff`, frame blocking,
    no-referrer policy, restrictive permissions policy, and same-origin
    cross-origin policies.
  - HSTS is sent only for HTTPS or HTTPS-forwarded requests.
  - Login JavaScript moved from inline HTML to `/static/login.js` so CSP does not
    require `unsafe-inline`.
- Added explicit cache policy:
  - Public skill/discovery/static metadata uses `Cache-Control: public,
    max-age=300`.
  - Health, login, authenticated API, vault search/document, audit, and MCP
    responses default to `Cache-Control: no-store` and `Pragma: no-cache`.
- Verified Railway GitHub sync cron state:
  - `Citadel-GitHub-Sync` ran at `2026-06-03T03:04:06Z`.
  - The run ended with ingestion accepted (`ingested=true`, `dry_run=false`).
  - Next scheduled run is `2026-06-04T03:00:00Z`.
- Dry-ran the backup-mirror cron path against production and confirmed rollout
  is still pending:
  - `scripts/run_backup_mirror.py` called
    `https://citadel-archive-production.up.railway.app/api/backup-mirror/run`.
  - Production returned `404 Not Found` because the live web service is still on
    the older commit without the backup-mirror API.
- Deployed hosted MCP/security hardening:
  - Commit `7c37c86` deployed the role/scope enforcement, MCP audit, discovery
    manifest, skill hashes, backup-mirror API, security headers, and cache policy.
  - Commit `3c70e92` made `/mcp/` the canonical hosted MCP endpoint and kept
    legacy `/mcp` as a relative redirect to avoid an absolute `http://` Location
    behind Railway.
  - Production `/.well-known/citadel.json` now advertises
    `https://citadel-archive-production.up.railway.app/mcp/`.
  - Hosted MCP `initialize` returns `200`, `tools/list` returns 13 tools, and a
    `citadel_session` tool call is recorded in MCP audit as
    `mcp.citadel_session`.
  - Backup mirror dry-run through the hosted API returns `ok=true`,
    `written=false`, and `published=false`.

## 2026-06-02

- Team-share readiness verified after commit `7a4a1d9`:
  - `npx skills add masumi-network/citadel` installs the root
    `citadel-archive` skill.
  - Production web service `Citadel-Archive` is `SUCCESS` and `RUNNING` on
    Railway at commit `7a4a1d9`.
  - Public endpoints `/healthz`, `/skills`, and `/skills/connect` return `200`.
  - Direct HTTP with a writer token verifies `/api/session`, `/search`, and
    `/ingest`.
  - Hosted MCP verifies `initialize`, `tools/list`, `citadel_session`,
    `citadel_search`, and `citadel_ingest`.
  - Fixed hosted MCP self-call timeouts by offloading forwarded HTTP API calls
    from the event loop.
  - Any token pasted into chat or logs should be rotated before team rollout.
- Production rollout checkpoint, verified after commit `cd33217`:
  - Railway web service `Citadel-Archive` deployment `891c81ee-4c44-4303-8792-0a282d9d62be`
    is `SUCCESS` and serves `/healthz`.
  - Hosted skill index serves HTTPS URLs for `/skills/connect`, `/skills/vault`,
    and `/skills/boundary`.
  - Reader service-account MCP token was created for company bootstrap and stored
    only in ignored local `.citadel/` files.
  - Local MCP `citadel_search` smoke test returns results when using
    `CITADEL_MCP_DEFAULT_DATASET=masumi-network`.
- Diagnosed the failed Railway deployment `7658403e-d79e-4d89-969b-34bb3aa45374`:
  - The app container started and Uvicorn served traffic, but Railway health checks
    requested `/healthz` and received `404 Not Found`.
  - Fixed by restoring the `/healthz` route and adding test coverage.
- Fixed hosted skill URL generation behind Railway:
  - `/skills` now prefers configured public base URLs or forwarded proxy headers,
    so shareable skill URLs use `https://citadel-archive-production.up.railway.app`.
- Updated MCP connector defaults:
  - Added `CITADEL_MCP_DEFAULT_DATASET`; hosted company configs use
    `masumi-network` so agents do not need to remember the dataset for normal
    company knowledge searches.
- Ran live source learning:
  - Forced learning-agent run scanned 41 repositories, 50 organization events, and
    198 commits.
  - GitHub activity ingestion was accepted.
  - Live fallback search against the `masumi-network` dataset returns results from
    `github_sync_state`.
- Initialized the private NAS-style backup repository:
  - [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror)
    is private, on `main`, and has initial scaffold commit `deeb1c9`.
  - Current scaffold includes `.gitignore`, `README.md`, `manifests/`, and
    `snapshots/`.
- Split repositories for production topology:
  - [Citadel](https://github.com/masumi-network/Citadel) is public
    (app, MCP, hosted agent skills).
  - [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror) is
    private (Phase 1 Vault Backup Mirror target).
- Documented mirror policy in `docs/vault-backup-mirror.md` and reserved
  `CITADEL_BACKUP_MIRROR_*` configuration for the export job.
- Published public/private boundary: `docs/public-and-private.md`, `SECURITY.md`,
  hosted `/skills/boundary`, and scrubbed personal paths from MCP templates.

## 2026-05-29

- Checked the Organization Vault plan against the local implementation state.
- Started the next dashboard build slice:
  - added Knowledge, Agents, Audit, and Settings workspace pages
  - added reader default routing to Search when no page hash is present
  - wired Knowledge to source, index, digest, and runtime event state
  - wired Agents to service-account access tokens and MCP setup snippets
  - wired Audit to access audit events and runtime vault events
  - wired Settings to readiness and learning-agent status
- Verified static JavaScript syntax with `node --check kb/static/app.js`.
- Verified backend and API behavior with `uv run pytest`.
- Improved dashboard UX:
  - reduced duplicated navigation chrome to a compact workspace ribbon
  - made mobile pages content-first by hiding the sidebar
  - rewrote the dashboard header around current vault state and primary actions
  - added direct dashboard actions for search, source sync, note creation, access,
    source review, and agent management
  - browser-checked desktop and mobile dashboard rendering

## 2026-05-28

- Captured the shareable Organization Vault product plan in
  `docs/organization-vault-plan.md` and started the canonical domain glossary in
  `CONTEXT.md`.
- Resolved Phase 1 access, Agent Messenger, source retention, repository daily
  update, knowledge conflict, and Vault Backup Mirror language across docs.
- Recorded the first architecture-deepening candidates in
  `docs/architecture-deepening-opportunities.md`.
- Rethemed the Citadel web UI toward an Obsidian-style shared vault with a left
  ribbon, vault navigation, linked panes, and darker Obsidian-compatible visual
  tokens.
- Researched the official `obsidianmd` GitHub organization and documented the
  sync/plugin integration path in `docs/obsidian-integration-plan.md`.
- Added the Obsidian vault sync API, source status endpoint, revision/conflict
  store, UI source panel, and private beta plugin scaffold.
- Verified the web UI with browser render checks and backend tests with
  `uv run pytest`.

## 2026-05-26

- Replaced the sensitive 2D knowledge mesh force simulation with a deterministic
  Three.js 3D scene.
- Added restrained orbit and zoom controls, fixed camera bounds, stable node
  placement, and WebGL labels for the mesh.
- Vendored the Three.js browser modules under `kb/static/vendor/` so the hosted
  UI does not depend on a runtime CDN.
- Verified backend tests with `uv run pytest` and checked the 3D canvas with
  Playwright on desktop and mobile viewports.

## 2026-08-21 v056 free-route checkpoint

- VERIFIED: Railway deployment `a26c2918-12b2-4e27-ade0-ef21ad98af4a` reached `SUCCESS`.
- VERIFIED: `GET https://citadel.utxo.ag/healthz` returned `{"ok":true,"service":"citadel"}` with HTTP 200.
- VERIFIED: The OpenRouter model catalog query returned `nvidia/nemotron-nano-9b-v2:free` as a zero-price model with `structured_outputs` support. This catalog check does not prove successful inference.
- VERIFIED: The approved MCP canary entered dataset `seat:sarthi` with source revision `d3ce0376-0279-5038-bdfd-3746ace35dc8` and projection job `1c060869-2af4-5988-90a0-151ba70b3453` under generation `citadel-railway-v056-free-quota-guard-20260821`.
- VERIFIED: The operation receipt reports `state=pending`; relational is `searchable` with `affected_count=1`; vector and graph are `pending` with `error_code=provider_quota_exhausted`.
- VERIFIED: Current deployment logs emitted `LLMQuotaExceededError` with `This is not retryable` and the OpenRouter daily reset epoch. The quota guard now stops the Cognee retry path.
- NOT DETERMINED: Vector recall and graph node or edge creation remain unproven until the provider quota resets.
- NOT DETERMINED: The authenticated graph UI view remains unproven for this new canary.
- VERIFIED: An MCP search for `production-cognee-v056-nemotron-20260821` scoped to `seat:sarthi` did not return within 20 seconds. This method cannot distinguish a vector-backend wait from a transport timeout.

## 2026-08-23 basic read-path correction

- [VERIFIED] The current working tree defaults `CITADEL_COGNEE_SEARCH_TYPE` to
  `CHUNKS` in `kb/cognee_client.py`. This mode returns raw matching passages
  without the implicit graph-completion LLM call.
- [VERIFIED] Explicit `CITADEL_COGNEE_SEARCH_TYPE=GRAPH_COMPLETION` still sends
  graph search with references.
- [VERIFIED] The corrected regression tests returned `2 passed in 0.16s`.
- [VERIFIED] The full suite returned
  `2314 passed, 6 skipped, 12 warnings in 80.30s (0:01:20)`.
- [NOT DETERMINED] Production does not use this change until a new deployment
  is approved. Live source coverage and projection readiness remain open.

## 2026-08-23 source coverage and live retrieval audit

- [VERIFIED] Local GitHub repository-content sync now supports explicit all-org
  discovery, reports its selected repository set, and fails closed on empty
  discovery. The local test file returned `44 passed`.
- [VERIFIED] Local Linear sync now fetches every issue page when
  `CITADEL_LINEAR_SYNC_MAX_ISSUES=0`. The focused sync and config tests returned
  `37 passed`.
- [VERIFIED] At `2026-08-23T00:27:20+0200`, Citadel status reported node and auth
  health as true, but search readiness was false with `code: SEARCH_TIMEOUT`.
- [VERIFIED] The CLI search probe returned HTTP `504` with
  `{"ok":false,"error":"Search exceeded the configured server budget.",
  "code":"SEARCH_TIMEOUT","http_status":504}`.
- [VERIFIED] Recent live ingest receipts still reported
  `accepted=true`, `cognified=false`, `projection_state=pending`, and
  `reason=queued_not_confirmed`.
- [REPORTED by delegated runtime audit] Accepted source storage and searchable
  provider receipts are separate lifecycle states. The audit also found a
  lifecycle search recall of `100` and a CLI operation command that exits `0`
  for pending or failed states.
- [NOT DETERMINED] The local source-coverage changes are not in production, and
  no successful post-reset model or embedding call is proven.

## 2026-08-23 local recovery slice

- [VERIFIED] Local basic retrieval defaults to `CHUNKS`, lifecycle search passes
  its bounded fetch page, and lifecycle provider fallback cannot change the
  active generation route.
- [VERIFIED] The MCP ingest contract records accepted pending projection state,
  and the CLI has `operation --require-searchable` for autonomous callers.
- [VERIFIED] `/api/state` reports lifecycle failure as `ok=false` and
  `healthy=false`, so process liveness does not hide projection failure.
- [VERIFIED] GitHub all-org discovery uses unbounded repository pagination, and
  Linear issues and users use cursor pagination when configured with zero limits.
- [VERIFIED] The full suite returned
  `2328 passed, 6 skipped, 12 warnings in 81.13s (0:01:21)`.
- [NOT DETERMINED] Production has not received this slice. The live MCP and CLI
  search probes still return `504 SEARCH_TIMEOUT` until a deployment and a
  post-deployment acceptance run prove otherwise.

## 2026-08-23 coverage boundary

- [VERIFIED] `gh api --paginate
  'orgs/masumi-network/repos?per_page=100&type=all' --jq 'length'` returned
  `57` repositories for the `masumi-network` organization.
- [VERIFIED] Local all-org discovery selects every non-archived repository from
  that paginated list. Repository content still follows configured context roots,
  documentation prefixes, file extensions, and per-repository size caps.
- [INFERRED] The current GitHub slice covers selected context and documentation
  files across the organization. It does not ingest every source file.
- [VERIFIED] Local Linear sync now fetches all paginated issues and users when
  their limits are zero. The issue query requests issue fields only. It does not
  fetch Linear comments, projects, or documents, and issue descriptions are
  rendered with a `4000` character limit.
- [INFERRED] The phrase "all Linear" must be split into a verified issue slice
  and a future entity slice. The current issue slice is not proof of complete
  Linear context.
- [NOT DETERMINED] The local environment has no `CITADEL_LINEAR_API_KEY`, so a
  live schema and permission check for comments, projects, and documents did not
  run. Exact output: `CITADEL_LINEAR_API_KEY=missing`.
- [PLANNED] Do not widen Linear ingestion until the live schema, access scope,
  retention policy, and entity-level acceptance checks are defined.

## 2026-08-23 final local verification correction

- [CORRECTED] The previous recovery checkpoint recorded `2328 passed`. The
  current full suite returned `2329 passed, 6 skipped, 12 warnings in 84.42s`.
- [VERIFIED] The focused source coverage suite returned `84 passed`.
- [VERIFIED] Ruff returned `All checks passed!`, `git diff --check` returned no
  output, and the documentation detector returned no findings.
- [CORRECTED] All-org mode ignores the explicit four-repository list when
  enabled. Empty repository discovery now fails closed and reports `ok=false`.
- [NOT DETERMINED] These changes are local only. No deployment, production
  ingest, provider call, restore, or post-reset retrieval check ran.

## 2026-08-23 OpenRouter route correction

- [VERIFIED] OpenRouter marks `nvidia/nemotron-nano-9b-v2:free` as going away
  on August 24, 2026: https://openrouter.ai/nvidia/nemotron-nano-9b-v2:free.
- [VERIFIED] OpenRouter documents that `openrouter/free` filters for request
  features such as structured outputs: https://openrouter.ai/openrouter/free.
- [VERIFIED] OpenRouter lists `nvidia/nemotron-3-embed-1b:free` as a zero-price
  embedding model: https://openrouter.ai/nvidia/nemotron-3-embed-1b%3Afree.
- [CORRECTED] Local Cognee defaults now use `openrouter/openrouter/free`, the
  LiteLLM form of the native `openrouter/free` route. Direct calls still use
  `openrouter/free`.
- [VERIFIED] Model route tests returned `5 passed`.
- [INFERRED] Free quota limits can interrupt a large autonomous sync. Durable
  retries and truthful pending or failed states remain required.
- [NOT DETERMINED] The route has not run in production. No deployment or live
  post-reset inference check ran.

## 2026-08-23 all-text source coverage slice

- [VERIFIED] Repository-content sync now has an opt-in
  `CITADEL_REPO_CONTENT_SYNC_ALL_TEXT` mode. With an empty extension override,
  it uses the built-in source and document extension list.
- [VERIFIED] All-text discovery uses a complete Git Trees walk when GitHub's
  recursive tree response is truncated. It refuses malformed entries and any
  truncated non-recursive child tree instead of indexing a partial repository.
- [VERIFIED] The path filter excludes dependency and generated directories,
  lock files, secret-shaped files, symlinks, and unsupported binary extensions.
  The existing content security scan still checks the downloaded text.
- [VERIFIED] `CITADEL_REPO_CONTENT_SYNC_MAX_FILES_PER_REPO=0` means every
  eligible path in all-text mode. `CITADEL_REPO_CONTENT_SYNC_MAX_BYTES_PER_FILE`
  remains a hard safety cap.
- [VERIFIED] All-text files use Learning Process `tier="light"`. This avoids
  one optional OpenRouter enrichment call per source file while retaining the
  normal ingest and projection path.
- [VERIFIED] The focused repository-content and config tests returned
  `60 passed, 10 warnings in 4.71s`.
- [INFERRED] This slice removes the current documentation-only selection limit,
  but it does not claim that every repository byte enters the vault. Binary
  files, secrets, generated files, symlinks, oversized files, and excluded lock
  files remain outside the index by policy.
- [NOT DETERMINED] No live GitHub all-text walk, production ingest, provider
  projection, or retrieval check ran for this slice.
- [PLANNED] After deployment approval, run a dry-run inventory first. Compare
  discovered, skipped, blocked, and oversized counts before allowing ingest.

## 2026-08-23 all-text final verification correction

- [CORRECTED] The earlier local recovery record ended at `2329 passed` before
  the all-text source tests were added.
- [VERIFIED] The full suite now returned
  `2338 passed, 6 skipped, 12 warnings in 81.11s (0:01:21)`.
- [VERIFIED] Ruff returned `All checks passed!`, `git diff --check` returned no
  output, and the documentation detector returned no findings.
- [NOT DETERMINED] Production has not received the all-text or provider-route
  changes. Live projection and retrieval remain unproven.

## 2026-08-23 Linear completeness gate

- [VERIFIED] The local Linear query fetches paginated issues and users. Its
  issue selection does not request comments, projects, project updates, or
  documents. `format_issue_note()` limits each description to `4000` characters.
- [VERIFIED] Linear's official GraphQL guide says the endpoint supports schema
  introspection and warns that GraphQL responses can contain partial data with
  an `errors` array. Source: https://linear.app/developers/graphql.
- [VERIFIED] Linear's official SDK guide shows paginated project reads,
  workspace comment reads, and issue comment reads. Source:
  https://linear.app/developers/sdk-fetching-and-modifying-data.
- [VERIFIED] Linear's webhook documentation lists issue comments, projects,
  project updates, and documents as distinct change resources. Source:
  https://linear.app/developers/webhooks.
- [INFERRED] The current sync is an issue and assignee mirror, not a complete
  Linear workspace context export. Comments, projects, project updates, and
  documents need separate source identities and state checkpoints.
- [NOT DETERMINED] The local environment has no `CITADEL_LINEAR_API_KEY`.
  Exact output from the audit: `CITADEL_LINEAR_API_KEY=missing`. The live
  schema, permission scope, entity counts, and retention policy were not
  queried.
- [PLANNED] Before implementing the entity slice, run a read-only schema and
  count probe with the workspace key. Then add one entity type at a time with
  pagination, tombstone, security-scan, and known-answer checks.
- [PLANNED] Required external input: a controlled Linear read key or approved
  Railway read-only execution context. Deployment still needs its own explicit
  approval.

## 2026-08-23 fresh live search probe

- [VERIFIED] At local time `2026-08-23 01:07:38 +0200`,
  `citadel status --json --check-search` reported `healthy=true`, node and auth
  checks `ok=true`, but the search check reported `ok=false` with
  `code=SEARCH_TIMEOUT` and JSON detail `timed out after 15s \u2014 node warming up`.
- [VERIFIED] The same status response reported readiness `search=false`, mesh
  `since_restart.searches=0`, `since_restart.errors=21`, graph index status
  `stale` with `records=7158`, and vector index status `stale` with `records=0`.
- [VERIFIED] The live mesh source nodes reported `github documents=8`,
  `repo_content documents=94`, and `linear documents=321`. These are live
  source counters, not proof that the corresponding documents are searchable.
- [VERIFIED] Recent live ingest receipts still reported
  `accepted=true`, `cognified=false`, `projection_state=pending`, and
  `reason=queued_not_confirmed`.
- [VERIFIED] `citadel search 'Masumi repository Linear ingestion retrieval'
  --top-k 5 --json --timeout 35` returned
  `{"ok":false,"error":"Search exceeded the configured server budget.",
  "code":"SEARCH_TIMEOUT","http_status":504}` with exit code `1`.
- [INFERRED] The process remains reachable, but the current live vector search
  surface is unavailable. The green node and auth checks do not prove retrieval.
- [NOT DETERMINED] The new provider route and local source-coverage changes are
  not deployed. The cause of the zero vector record count still needs an
  approved production diagnostic or restore check.

## 2026-08-23 coverage completeness guard

- [VERIFIED] All-text and all-org repository-content runs now return
  `ok=false` and `content_scan_complete=false` when any selected repository
  returns an error. This prevents a partial cross-repository corpus from
  reporting a complete sync.
- [VERIFIED] The focused repository-content and config tests returned
  `61 passed, 10 warnings in 5.59s`.
- [CORRECTED] The previous final local verification recorded `2338 passed`.
  The latest full suite returned
  `2339 passed, 6 skipped, 12 warnings in 80.46s (0:01:20)`.
- [VERIFIED] Ruff returned `All checks passed!` and `git diff --check`
  returned no output.
- [NOT DETERMINED] Production still has not received this guard. No deployment
  or production sync ran.

## 2026-08-23 Railway provider and retrieval correction

- [VERIFIED] Railway's latest successful production deployment was
  `e760e1b1-4539-4a01-a9f3-c86f444251f2`, started at
  `2026-08-22 21:10:46.068 UTC`. The service config reports environment
  `production`, source `masumi-network/Citadel`, and `59` defined variables.
- [VERIFIED] A read-only Railway variable inspection reported the live LLM
  route as `openrouter/nvidia/nemotron-nano-9b-v2:free` for `LLM_MODEL`,
  `LLM_EXTRACTION_MODEL`, `LLM_SUMMARIZATION_MODEL`, and `LLM_QUERY_MODEL`.
  It reported `LLM_PROVIDER=custom` and
  `CITADEL_GENERATION_ID=citadel-railway-v056-free-quota-guard-20260821`.
  The API key value was intentionally excluded from this record.
- [VERIFIED] The same inspection reported the live embedding route as
  `EMBEDDING_PROVIDER=openai_compatible`,
  `EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free`,
  `EMBEDDING_DIMENSIONS=2048`, and
  `EMBEDDING_ENDPOINT=https://openrouter.ai/api/v1`. It reported
  `CITADEL_NEMOTRON_EMBEDDINGS_ENABLED=true` and `VECTOR_DB_PROVIDER=qdrant`.
- [VERIFIED] Railway deploy logs at `2026-08-22T23:19:10.300Z` recorded the
  exact provider error:
  `LLMQuotaExceededError: LLM provider quota or billing limit was reached. This is not retryable. Check the provider billing/quota dashboard, raise the limit, or switch credentials. Provider error: OpenRouter free-model daily quota is exhausted; X-RateLimit-Reset=1787443200000 (Status code: 422)`.
  This failure occurred during Cognee graph extraction and classification.
- [VERIFIED] `date -r 1787443200 '+%Y-%m-%d %H:%M:%S %z'` returned
  `2026-08-23 02:00:00 +0200`. The local audit time was
  `2026-08-23 01:20:18 +0200`, so the recorded reset had not yet occurred.
- [VERIFIED] The latest production HTTP log sample recorded
  `POST /search 499 15002ms`, `POST /search 504 30565ms`, and
  `POST /mcp/ 200 84922ms`. Process health returned `GET /healthz 200`, but
  that does not prove search readiness.
- [CORRECTED] The previous record treated the local free-router route as the
  next production route. Production still uses the old fixed route. OpenRouter
  marks that model as going away on August 24, 2026, so the deployed route is
  both quota-blocked and short-lived. Source:
  https://openrouter.ai/nvidia/nemotron-nano-9b-v2:free.
- [INFERRED] The live logs confirm an LLM provider failure as one direct cause
  of failed projection. They do not prove that embedding requests succeeded.
  The earlier live vector record count was `0`, so the embedding path remains
  unproven until a controlled post-reset diagnostic or local FastEmbed recovery
  check runs.
- [VERIFIED] A fresh MCP `citadel_search` call for embedding, Cognee, and
  retrieval context returned HTTP `504` with
  `Search exceeded the configured server budget.` No Citadel hit is available
  for this checkpoint.
- [PLANNED] The next production gate is deployment-specific approval, followed
  by a read-only post-reset provider check, one bounded known-answer canary,
  and an operation receipt that reaches `searchable` before any broad sync.

## 2026-08-23 projection coupling and embedding red-team finding

- [VERIFIED] Installed Cognee `1.4.1` builds its default `cognify` pipeline in
  this order: `classify_documents`, `extract_chunks_from_documents`,
  `extract_graph_and_summarize`, then `add_data_points`. Source:
  `.venv/lib/python3.12/site-packages/cognee/api/v1/cognify/cognify.py:350-375`.
- [VERIFIED] The graph extraction task runs before the load task that persists
  graph nodes and vector embeddings. Railway's quota error occurred inside that
  graph extraction path. Therefore a Cognee graph-extraction failure can stop
  the later vector load for the same cognify run.
- [VERIFIED] Citadel's lifecycle worker marks vector and graph backends pending,
  calls one shared `gateway.cognify()` operation, then checks vector chunk count
  and graph presence. Source: `kb/lifecycle_worker.py:554-625`.
- [INFERRED] This explains how source rows can be accepted while the live vector
  index remains at `records=0`: the relational receipt is durable, but the
  shared projection reaches an LLM-dependent task before vector persistence.
- [VERIFIED] The installed OpenAI-compatible embedding engine sends
  `model`, raw `input`, and `encoding_format` to the embeddings endpoint. It
  does not send an `input_type` or add query and passage prefixes. Source:
  `.venv/lib/python3.12/site-packages/cognee/infrastructure/databases/vector/embeddings/OpenAICompatibleEmbeddingEngine.py:141-151`.
- [VERIFIED] NVIDIA's Nemotron 3 Embed model card says to use `query: ` for
  queries and `passage: ` for documents. It reports a 32768-token maximum and
  2048-dimensional output. Source:
  https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16.
- [INFERRED] The current embedding route may produce lower retrieval quality
  because Citadel sends raw text without the model-card prefixes. OpenRouter
  may add provider-specific handling, but this was not proven by a live request.
- [VERIFIED] Railway logs warned that
  `nvidia/nemotron-3-embed-1b:free` is not a valid Hugging Face tokenizer ID and
  that Cognee fell back to TikToken. The warning says token counts are
  approximate and can mis-size chunks. A live OpenRouter model ID is valid for
  the endpoint, but it is not a local Hugging Face tokenizer ID.
- [INFERRED] The tokenizer fallback is a chunk-size accuracy risk. It does not
  prove that embeddings failed or that every stored chunk was truncated.
- [VERIFIED] The container bakes only `BAAI/bge-small-en-v1.5` for offline
  FastEmbed, with `384` dimensions. Source: `Dockerfile:49-60`.
- [INFERRED] A local embedding fallback can avoid provider quota, but it uses a
  separate `384`-dimension Qdrant profile and must run in a new generation or
  collection. It cannot be treated as an in-place swap for the live `2048`
  profile.
- [PLANNED] The repair design must make chunk/vector retrieval independent of
  optional LLM graph extraction. Then graph enrichment can retry after quota
  reset without blocking basic search.

## 2026-08-23 lifecycle failure-state confirmation

- [REPORTED] A read-only subagent inspected the lifecycle write path and ran a
  temporary-store probe. The probe returned:
  `{"job_state":"pending","operation_state":"pending","raised":null,"receipt_states":{"graph":"pending","relational":"searchable","vector":"pending"},"vector_receipt_affected_count":null}`.
- [REPORTED] The lifecycle path commits source bytes and pending projection
  receipts before provider work. Relational storage completes before the
  shared `gateway.cognify()` call. Sources: `kb/service.py:253`,
  `kb/service.py:269`, `kb/lifecycle.py:678`, and
  `kb/lifecycle_worker.py:513`.
- [REPORTED] The zero-vector read check runs only after `cognify()` returns.
  A provider error before that check can leave vector records absent while the
  job remains pending. Source: `kb/lifecycle_worker.py:616-625`.
- [REPORTED] Active lifecycle mode disables local embedding fallback and LLM
  route fallback before classification. The post-cognify vector census also
  runs outside the fallback block. Sources: `kb/cognee_client.py:4685` and
  `kb/cognee_client.py:4708-4719`.
- [REPORTED] Non-lifecycle embedding fallback uses FastEmbed at `384`
  dimensions and persists a separate embedding profile. Sources:
  `kb/embedding_profile.py:129` and `kb/model_routing.py:219`.
- [REPORTED] Lifecycle receipts do not record the embedding model or
  dimensions during worker completion. The config digest also excludes the
  embedding endpoint. Sources: `kb/lifecycle_worker.py:605` and
  `kb/service.py:437`.
- [REPORTED] The inspected lifecycle-worker tests cover success and zero-chunk
  checks, but no test makes `cognify()` fail after relational completion and
  asserts the resulting receipt state and vector count.
- [REPORTED] The next read-only checks are `citadel operation <job-id> --json
  --require-searchable`, `/data/citadel-state/bootstrap.json`, and a Qdrant
  point count for the active collection and source document ID. The Qdrant
  count is required to convert the zero-vector inference into a live fact.
- [PLANNED] Repair sequence: add a deterministic vector-only projection path;
  expose vector-searchable state separately from graph enrichment; make search
  use only verified vector receipts; retry graph enrichment independently; run
  one known-answer canary through CLI and MCP before broad sync.
- [PLANNED] Do not switch the live embedding profile in place. A `384`
  dimension FastEmbed fallback requires a new generation or Qdrant collection.
- [NOT DETERMINED] The active Qdrant collection name, point count by source, and
  bootstrap embedding profile were not read in this checkpoint. Production
  diagnostic access remains pending explicit deployment-specific approval.

## 2026-08-23 supported vector-first boundary

- [VERIFIED] The official Cognee skill documents `run_custom_pipeline(...)` for
  explicit sequential task control. It also documents direct `DataPoint`
  insertion with `metadata = {"index_fields": [...]}` for vector fields.
  Source: https://github.com/topoteretes/cognee/blob/main/cognee/skill.md.
- [VERIFIED] The official Cognee `cognify.py` source defines a deterministic DLT
  task route that omits LLM graph extraction and still runs
  `extract_chunks_from_documents` followed by `add_data_points` for vector
  indexing. Source:
  https://github.com/topoteretes/cognee/blob/main/cognee/api/v1/cognify/cognify.py.
- [INFERRED] A vector-first source route is supported by the upstream task
  model. The local installed Cognee version and the upstream `main` branch can
  differ, so the first implementation task must prove the exact task imports
  and dataset behavior against the pinned local version.
- [PLANNED] Vector-first implementation checkpoint: run a local disposable
  dataset through classification, chunk extraction, and `add_data_points`
  without an LLM; verify chunk rows, vector points, source binding, and
  `CHUNKS` search before changing lifecycle states.
- [PLANNED] Only after that spike passes should lifecycle state names and search
  filters change. This keeps storage behavior and receipt semantics separate
  while the task boundary is verified.
- [NOT DETERMINED] The official source does not prove that the local pinned
  Cognee build accepts the same custom route or that its task runner preserves
  source revision metadata. Local execution must settle both points.

## 2026-08-23 local Cognee API compatibility check

- [VERIFIED] The local environment reports Cognee `1.4.1`.
- [VERIFIED] The command
  `uv run python -c "import inspect, cognee; import cognee.api.v1.cognify.cognify as c; print({'cognee_version': getattr(cognee, '__version__', 'unknown'), 'run_custom_pipeline': hasattr(cognee, 'run_custom_pipeline'), 'dlt_route': hasattr(c, 'get_dlt_tasks'), 'task_source': inspect.getsourcefile(c.get_dlt_tasks) if hasattr(c, 'get_dlt_tasks') else None})"`
  returned `{'cognee_version': '1.4.1', 'run_custom_pipeline': True,
  'dlt_route': False, 'task_source': None}`.
- [CORRECTED] The upstream DLT route cannot be copied directly into the local
  build. The local build exposes `run_custom_pipeline`, but it does not expose
  `get_dlt_tasks` from `cognee.api.v1.cognify.cognify`.
- [PLANNED] The local spike must import the pinned task functions directly and
  build a minimal task list. It must verify that source revision metadata
  survives `extract_chunks_from_documents` and `add_data_points`.

## 2026-08-23 approved repair sequence

Goal: basic source retrieval must remain searchable when optional LLM graph
enrichment is unavailable. The system must report graph delay as degraded state,
not as successful retrieval.

### Checkpoint P0: pinned local task proof

- Owner: implementer.
- Status: Planned.
- Task: run a disposable local dataset through the pinned Cognee `1.4.1`
  task functions for classification, chunk extraction, and `add_data_points`.
- Check: record chunk rows, vector point count, source revision binding, and
  `CHUNKS` search output.
- Stop condition: any missing source binding, zero vector points, or LLM call.

### Checkpoint P1: receipt and search state

- Owner: architect.
- Status: Planned.
- Task: define separate vector-searchable and graph-enrichment states. Update
  operation status, wait behavior, search filters, canary checks, and CLI/MCP
  response fields together.
- Check: a vector-searchable receipt can answer a known query while graph state
  is pending. A pending vector receipt cannot enter search results.
- Stop condition: any path can report searchable without a verified vector
  count and source binding.

### Checkpoint P2: embedding contract

- Owner: researcher.
- Status: Planned.
- Task: choose one embedding profile for the first recovery generation. Verify
  dimensions, tokenizer, query and passage formatting, collection name, and
  source metadata.
- Check: query and document probes use the same declared profile. A profile
  change creates a new generation or collection.
- Stop condition: any unrecorded model, dimension, tokenizer, or collection
  change.

### Checkpoint P3: deployment canary

- Owner: release.
- Status: Planned.
- Task: after separate deployment approval, run the read-only inventory, then a
  bounded canary. Use `citadel operation --require-searchable`, the bootstrap
  profile, Qdrant point counts, CLI search, MCP search, and Railway logs.
- Check: one known source returns the expected citation through both clients.
  `/readyz` and search return success only after this check.
- Stop condition: any provider error, pending receipt, zero point count, or
  client timeout.

### Checkpoint P4: source coverage

- Owner: researcher.
- Status: Planned.
- Task: run the all-org repository inventory and a read-only Linear schema and
  count probe. Add comments, projects, project updates, or documents only after
  the live schema and permissions are recorded.
- Check: discovered, skipped, blocked, and ingested counts reconcile. Partial
  coverage reports `content_scan_complete=false`.
- Stop condition: missing repository discovery, missing Linear permission
  evidence, or a source count treated as a searchable count.

### Checkpoint P5: acceptance and handoff

- Owner: coordinator.
- Status: Planned.
- Task: freeze known-answer checks for CLI, MCP, ingestion receipts, source
  coverage, degraded graph status, and restart recovery.
- Check: repeat the checks after a restart and after one forced provider quota
  failure in a test environment.
- Stop condition: green health without green retrieval, or an unrecorded
  failure path.

### Least confident decisions

1. The pinned Cognee task runner may require an application wrapper to preserve
   lifecycle source revision metadata across a custom pipeline.
2. A separate vector-searchable state may require a schema migration rather
   than a field-only change. This is not determined until the lifecycle schema
   and existing receipt consumers are mapped.
3. Remote Nemotron embeddings may remain unsuitable until prefix handling and
   tokenizer alignment are measured. Local FastEmbed avoids provider quota but
   requires a new `384`-dimension collection.

## 2026-08-23 post-reset live search and operation probe

- [VERIFIED] At `2026-08-23 08:02:14 +0200`, the provider reset window had
  passed. A fresh `citadel status --json --check-search` still reported node
  and auth `ok=true`, but search `ok=false` with
  `code=SEARCH_TIMEOUT` and detail `timed out after 15s: node warming up`.
- [VERIFIED] A fresh CLI search returned
  `{"ok":false,"error":"The read operation timed out","code":"SEARCH_TIMEOUT","http_status":504}`
  with exit code `1`.
- [VERIFIED] A fresh MCP search did not return a hit. The transport reported
  an HTTP request failure for `https://citadel.utxo.ag/mcp/`.
- [VERIFIED] The repository CLI entry point
  `uv run python -m kb.cli operation a2c4423e-d3d7-56b8-9088-d76468138261 --json --require-searchable`
  returned exit code `1` and `code=OPERATION_NOT_SEARCHABLE` with operation
  state `running`. Its live receipts were relational `searchable`, vector
  `running`, and graph `running`, under generation
  `citadel-railway-v056-free-quota-guard-20260821`.
- [CORRECTED] The reset did not restore retrieval. The previous audit recorded
  the provider quota reset as a future recovery point, but the post-reset
  probe still found the search surface unavailable.
- [VERIFIED] The shell `citadel` command resolves to
  `/Users/sarthiborkar/.local/bin/citadel`, while `uv run which citadel`
  resolves to the repository virtual environment. The global command's
  `operation --help` does not expose `--require-searchable`; the repository
  entry point does expose it.
- [INFERRED] CLI agents can receive different behavior depending on whether
  they invoke the global pipx command or the repository environment. The
  installation path must be made explicit and tested before CLI acceptance.
- [PLANNED] Keep using the repository entry point for local acceptance. Add a
  packaging and installation check so the agent-facing `citadel` command points
  to the same code and exposes the same operation guard.
- [NOT DETERMINED] The active Qdrant point count and bootstrap embedding
  profile remain unread. The operation probe proves receipt state, not the
  number of vector points in Qdrant.

## 2026-08-23 user-level LLM independence invariant

- [REPORTED] User requirement: an agent using Citadel through MCP or CLI must
  be able to ingest source material, search it, receive citations, inspect
  operation status, and use basic context retrieval without an LLM call.
- [PLANNED] Baseline ingestion will persist source bytes, metadata, chunks, and
  vector or lexical indexes through deterministic code. Embeddings may use a
  configured embedding model, but graph extraction must not gate the write.
- [PLANNED] Baseline search will use vector and lexical retrieval and return
  source-bound chunks and citations. Query expansion, graph traversal,
  summarization, answer synthesis, and learning improvements remain optional
  stages.
- [PLANNED] Provider quota, provider timeout, or missing LLM credentials will
  set degraded enrichment state. They must not turn source capture, basic
  retrieval, MCP, or CLI status into a false success or a timeout.
- [VERIFIED] The current live path violates this invariant. Its operation probe
  showed relational `searchable`, vector `running`, and graph `running`, while
  live search remained `SEARCH_TIMEOUT`.
- [PLANNED] The first acceptance test will run with LLM calls disabled. It must
  prove source capture, vector or lexical retrieval, citations, MCP output,
  CLI output, and truthful degraded graph status before any OpenRouter route is
  enabled.

## 2026-08-23 LLM-independent retrieval implementation checkpoint

- [VERIFIED] `uv run pytest -q` returned `2345 passed, 6 skipped, 12 warnings`
  in `54.77s`.
- [VERIFIED] Lifecycle search now reads current retained source heads with
  deterministic lexical scoring. It excludes tombstones and returns source
  revision IDs, source locators, snippets, and retrieval state.
  Evidence: `kb/lifecycle.py:1660-1795`.
- [VERIFIED] Service search uses lexical fallback when vector recall is empty
  or unavailable. It runs the SQLite scan in a worker thread.
  Evidence: `kb/service.py:974-1042`.
- [VERIFIED] Lifecycle document lookup falls back to retained UTF-8 bytes when
  Cognee cannot resolve the source revision. Evidence: `kb/service.py:1770-1805`.
- [VERIFIED] The vector-only Cognee route uses
  `classify_documents`, `extract_chunks_from_documents`, and
  `index_data_points`. It omits graph extraction and graph loading.
  Evidence: `kb/cognee_client.py:4629-4688`.
- [VERIFIED] Cognee 1.4.1 exposes the required custom pipeline arguments at
  `.venv/lib/python3.12/site-packages/cognee/modules/run_custom_pipeline/run_custom_pipeline.py:14-84`.
- [VERIFIED] The pinned chunk retriever searches the vector engine directly and
  returns chunk payloads. It does not generate an answer.
  Evidence: `.venv/lib/python3.12/site-packages/cognee/modules/retrieval/chunks_retriever.py:96-124`.
- [VERIFIED] Worker tests returned `6 passed`. They cover vector-first order,
  graph quota retry, vector receipt availability, and legacy gateway behavior.
- [INFERRED] Basic MCP and CLI retrieval no longer needs generative LLM success
  once the new worker path is deployed. The production node has not run it.
- [NOT DETERMINED] Production Qdrant point count, embedding profile alignment,
  and deployed MCP known-answer behavior remain unverified.
- [VERIFIED] No Railway deploy, migration, Qdrant write, or production ingest
  ran during this checkpoint.
- [PLANNED] Run read-only Railway and Qdrant diagnostics. Then run one approved
  canary through CLI and MCP.

## 2026-08-23 vector-scope timeout red-team correction

- [VERIFIED] `kb/service.py:986-1020` now skips Cognee recall when the current
  lifecycle vector scope contains zero searchable source revisions. It sends
  no unscoped Qdrant or embedding request in that state.
- [VERIFIED] The retained-source lexical fallback still runs for that empty
  vector scope and returns source-bound citations.
- [VERIFIED] The regression test recorded `fake.recall_calls == 0` while the
  pending source returned one lexical hit. Evidence:
  `tests/test_service.py:328-364`.
- [VERIFIED] The focused lifecycle search test returned
  `2 passed, 82 deselected, 10 warnings in 3.59s`.
- [VERIFIED] The light-tier ingestion and disabled-enrichment checks returned
  `4 passed, 20 deselected in 0.14s`. Seat-level writes continue to use
  `tier="light"`, which skips enrichment and improvement.
- [VERIFIED] Ruff returned `All checks passed!` for the changed service and
  service test files.
- [VERIFIED] A fresh full suite returned
  `2345 passed, 6 skipped, 12 warnings in 56.81s`.
- [INFERRED] This removes the known zero-vector provider timeout path for
  lifecycle search after deployment. It does not prove a deployed search
  result.
- [NOT DETERMINED] A vector provider that hangs after a non-empty vector scope
  can still consume the HTTP search budget. The deployed canary must test that
  degraded case.
- [VERIFIED] No Railway deploy, production ingest, Qdrant write, or migration
  ran for this correction.

## 2026-08-23 pinned task LLM audit

- [VERIFIED] Cognee `1.4.1` `classify_documents()` maps file extensions to
  document classes and copies metadata. Its source contains no LLM client or
  completion call. Evidence:
  `.venv/lib/python3.12/site-packages/cognee/tasks/documents/classify_documents.py:102-150`.
- [VERIFIED] `extract_chunks_from_documents()` reads documents through the
  selected chunker and updates token counts. Its source contains no LLM client
  or completion call. Evidence:
  `.venv/lib/python3.12/site-packages/cognee/tasks/documents/extract_chunks_from_documents.py:30-60`.
- [VERIFIED] `index_data_points()` obtains the vector engine and sends batches
  to its embedding engine. It does not call an LLM completion API. Evidence:
  `.venv/lib/python3.12/site-packages/cognee/tasks/storage/index_data_points.py:11-74`.
- [VERIFIED] The local vector-only task list therefore has one external model
  dependency, embeddings. Graph extraction and answer generation are absent.
- [INFERRED] Basic vector ingestion can run without a generative LLM when the
  embedding provider works. The lexical path remains the fallback when it does
  not.
- [NOT DETERMINED] This static task audit does not prove the live embedding
  endpoint, dimensions, tokenizer, or Qdrant collection profile.

## 2026-08-23 live MCP recheck

- [VERIFIED] At `2026-08-23 09:45:21 +0200`, the configured Citadel MCP
  `citadel_search` call for `Citadel vector-first retrieval without generative
  LLM lexical fallback` returned HTTP `504`.
- [VERIFIED] The MCP error was
  `Citadel returned HTTP 504: Search exceeded the configured server budget.
  (code=SEARCH_TIMEOUT, http_status=504)`.
- [CORRECTED] Local vector-first and lexical tests do not prove the deployed
  MCP route. The live search surface remains unavailable in this session.
- [NOT DETERMINED] The timeout does not identify whether the cause is Qdrant,
  the deployed code generation, the provider route, or node startup state.
- [PLANNED] Do not claim Citadel-backed context for this checkpoint. A
  deployment-specific read-only diagnostic must identify the active collection,
  point count, embedding profile, and operation receipts before a canary.

## 2026-08-23 MCP contract correction

- [CORRECTED] The MCP ingest tool description previously said that Cognee
  projection was required without separating vector retrieval from graph
  enrichment.
- [VERIFIED] `kb/mcp_server.py:1326-1333` now states that baseline capture and
  retrieval use chunking, embeddings, or lexical fallback without a generative
  LLM. It also states that graph enrichment may remain pending.
- [VERIFIED] The focused MCP checks returned
  `3 passed, 73 deselected in 0.53s`.
- [VERIFIED] Ruff returned `All checks passed!` for `kb/mcp_server.py`.

## 2026-08-23 Railway runtime red-team read

- [VERIFIED] Read-only Railway status returned deployment
  `e760e1b1-4539-4a01-a9f3-c86f444251f2` for service `Citadel-Archive` with
  deployment status `SUCCESS` and start command `python -m kb.lite_runtime`.
- [VERIFIED] The live bootstrap reports generation
  `citadel-railway-v056-free-quota-guard-20260821`, Cognee `1.4.1`, embedding
  provider `openai_compatible`, model
  `nvidia/nemotron-3-embed-1b:free`, and dimensions `2048`.
- [VERIFIED] The live bootstrap binds Qdrant collection
  `citadel_g_544df538f8b0_DocumentChunk_text_e8bbe7045b_nemotron-2048_ca06cec5`.
- [VERIFIED] A read-only Qdrant collection request returned status `green`,
  `points_count: 74`, `indexed_vectors_count: 0`, and vector size `2048`.
  The response also reported payload counts of `74` for `citadel_dataset_scope`,
  `citadel_generation_id`, and `document_id`.
- [VERIFIED] The live `/api/state` response reported top-level
  `"healthy": true` and nested lifecycle `"ok": false` with invariant error
  `current_generation_searchable_census_mismatch`.
- [VERIFIED] The same response reported `current_sources: 634`,
  `current_projection_jobs: 634`, `current_searchable_by_backend.vector: 19`,
  `current_searchable_by_backend.relational: 323`, and
  `current_searchable_by_backend.graph: 24`.
- [VERIFIED] The same response reported source counters of `49` GitHub
  repositories, `94` repository-content files, and `321` Linear issues. These
  are tracked-source counters, not searchable-vector counts.
- [VERIFIED] Railway logs contain repeated
  `LLMQuotaExceededError: LLM provider quota or billing limit was reached` with
  `OpenRouter free-model daily quota is exhausted` and status `422`.
- [CORRECTED] The active Qdrant point count and embedding profile were
  previously recorded as unread. This read-only check now confirms both.
- [CORRECTED] The production failure is not proven to be only an empty-vector
  scope. The active collection contains `74` points, while only `19` current
  lifecycle vector sources are searchable.
- [NOT DETERMINED] These counters do not prove why the deployed MCP request
  returns `504 SEARCH_TIMEOUT`. The active request path, provider latency, and
  relation between the `74` points and the `19` searchable sources remain to be
  measured with a known-answer search.
- [PLANNED] Fix the public health projection so nested lifecycle failure cannot
  coexist with a top-level green result. Then run one bounded CLI and MCP
  known-answer search against a source whose vector receipt is searchable.
- [VERIFIED] No Railway deployment, production ingest, Qdrant write, or schema
  migration ran during this read-only check.

## 2026-08-23 source coverage and status correction

- [VERIFIED] `gh api --paginate 'orgs/masumi-network/repos?per_page=100&type=all' --jq 'length'` returned `57`. This is the number of repositories visible to the local GitHub credential. It is not a count of ingested files or searchable vectors.
- [VERIFIED] The production `/api/state` snapshot reported `49` GitHub repositories, `94` repository-content files, and `321` Linear issues. The production counters therefore do not cover all `57` visible organization repositories.
- [CORRECTED] The earlier local Linear audit said that the sync fetched issues and users only. The local code now fetches projects, project updates, documents, comments, initiatives, and initiative updates through six separate GraphQL connections. Archived records remain excluded by default. Evidence: `kb/linear_sync.py:215-345` and `kb/linear_sync.py:441-510`.
- [VERIFIED] Linear context notes use deterministic formatting and `tier="light"`. The context write path sets `run_improve=False` and `defer_cognify=True`. Evidence: `kb/linear_sync.py:993-1014`.
- [VERIFIED] A denied or malformed context connection now leaves available records in the result, sets `context_listing_complete=false`, and records `context_error`. A capped listing also reports incomplete coverage. Evidence: `kb/linear_sync.py:441-510` and `tests/test_linear_sync.py` context coverage tests.
- [VERIFIED] Source status now reports issue and context counts together. A context fetch error reports source status `degraded` instead of `tracked`. Evidence: `kb/server.py` helpers `_linear_document_count` and `_linear_source_status`, plus `tests/test_server.py::test_linear_context_gap_is_visible_in_source_status`.
- [VERIFIED] `uv run pytest -q tests/test_linear_sync.py tests/test_config.py --maxfail=1` returned `46 passed, 10 warnings in 3.87s`.
- [VERIFIED] `uv run pytest -q tests/test_server.py -k 'linear_sync_api_endpoints or linear_context_gap_is_visible_in_source_status or public_state_reports_lifecycle_failure_as_degraded' --maxfail=1` returned `3 passed, 308 deselected, 11 warnings in 4.93s`.
- [VERIFIED] `ruff check kb/linear_sync.py kb/server.py tests/test_linear_sync.py tests/test_config.py tests/test_server.py` returned `All checks passed!`.
- [VERIFIED] The read-only Railway variable-name filter returned no entries for `CITADEL_LINEAR_SYNC_*`, `CITADEL_REPO_CONTENT_SYNC_*`, `CITADEL_BUILD_ID`, or `CITADEL_LLM_ENRICHMENT_ENABLED`. This proves those exact variable names are absent from the returned production variable set. It does not prove that equivalent behavior is disabled under another name or code default.
- [NOT DETERMINED] The live Linear permission and record counts remain unknown because this session did not call the Linear API. The production deployment also has not been proven to contain the local coverage and status changes.
- [PLANNED] Compare the deployed build identity with the local revision, then run a read-only Linear count probe and a known-answer search after deployment approval.

### Least confident decisions

1. Counting Linear source documents as issues plus non-issue context records may differ from the product's preferred dashboard definition.
2. Continuing after one denied Linear context connection is correct only if the partial result remains visibly degraded and never tombstones records from the failed connection.
3. The local GitHub repository count may differ from the Railway token's organization visibility, so production coverage still needs a server-side discovery result.

## 2026-08-23 live acceptance recheck

- [VERIFIED] `uv run pytest -q` returned `2353 passed, 6 skipped, 12 warnings in 57.49s`.
- [VERIFIED] Fresh `citadel status --json --check-search` returned node and auth `ok=true`, search `ok=false`, `code=SEARCH_TIMEOUT`, and detail `timed out after 15s - node warming up`.
- [VERIFIED] Fresh CLI search returned `{"ok":false,"error":"The read operation timed out","code":"SEARCH_TIMEOUT","http_status":504}` with exit code `1`.
- [VERIFIED] Fresh MCP `citadel_search` for `Citadel retained source lexical fallback` returned HTTP `504` with `code=SEARCH_TIMEOUT` and `Search exceeded the configured server budget.` No Citadel search hit was retrieved in this session.
- [VERIFIED] The same CLI status response reported corpus canary `search_hit: null`, `error: LLMQuotaExceededError`, lifecycle invariant `current_generation_searchable_census_mismatch`, searchable counts graph `24`, relational `323`, vector `19`, and a bounded probe with `0/601` fully indexed documents.
- [CORRECTED] Local tests now prove the fallback and partial-source behavior, but the live CLI and MCP checks still prove that the deployed service is not serving a known-answer search.
- [NOT DETERMINED] The deployed code revision, request path, and exact cause of the timeout remain unverified. The live result shows provider quota failure and projection mismatch, but it does not isolate which component holds the request budget.
- [PLANNED] Do not declare production ready. Deploy only after explicit approval, then repeat the same CLI and MCP checks against a known source and verify the response contains a citation.

## 2026-08-23 production state-file coverage read

- [VERIFIED] Read-only Railway SSH inspection of `/data/citadel-state/repo_content_sync_state.json` returned `file_count: 94`, `repo_count: 4`, and projection status counts `pending: 14`, `searchable: 80`.
- [VERIFIED] Read-only Railway SSH inspection of `/data/citadel-state/linear_sync_state.json` returned `issue_count: 321`, `listing_complete: false`, `context_record_count: 0`, and no `context_records` or `context_listing_complete` state keys.
- [VERIFIED] The production bootstrap reports build identity `507318f10c8368da9e6c5117cda7fcfe0667085b61e483875f8719cf72a5ed72`, while the local repository HEAD is `a910f4f37a758dd033c370b7a55c5cf7467d58b9`. The bootstrap build identity is not the local commit hash, so this difference alone does not identify the deployed source revision.
- [CORRECTED] Production does not currently contain the local Linear context state. The local six-connection implementation is therefore not live, regardless of the local test result.
- [CORRECTED] Production repo-content state covers four repositories and ninety-four files. The local GitHub inventory sees fifty-seven repositories. The 49 GitHub source counter and four repo-content repositories measure different sync surfaces.
- [NOT DETERMINED] The Railway service's effective sync defaults are not fully proven because the exact all-repository and Linear context environment names are absent, while code defaults may still select narrower behavior.

## 2026-08-23 production repository scope and default correction

- [VERIFIED] Read-only Railway SSH inspection of
  `/data/citadel-state/github_sync_state.json` returned `repo_count: 49` and
  the state file had no `listing_complete` field. The count is a stored state
  count, not proof of a complete current organization listing.
- [VERIFIED] The local GitHub inventory returned this aggregate:
  `{"total":57,"archived":0,"active":57,"forks":6,"disabled":0,"private":8}`.
  The credential therefore sees `57` repositories, all active, with `8`
  private repositories.
- [NOT DETERMINED] The numeric difference between the local `57` and Railway's
  `49` equals the local private count, but repository identity was not
  reconciled across the two credentials. The missing eight are not attributed
  to privacy without that comparison.
- [CORRECTED] The server-path config previously defaulted repository content
  to an allowlist, documentation-only selection, and a `40` file cap when the
  matching Railway variables were absent. `kb/config.py:497-525` now defaults
  `from_env()` to all repositories, all eligible text, and zero file cap.
  Explicit environment values still override these defaults.
- [CORRECTED] Direct `CitadelConfig` construction now defaults the repository
  content improvement pass to `false`. The `.env.example` baseline also sets
  optional LLM enrichment and sync improvement passes to `false`.
- [VERIFIED] The config and repository-content suite returned
  `63 passed, 10 warnings in 4.25s` after this correction.
- [NOT DETERMINED] No deployment or production sync ran after the default
  correction. Railway still has the previous state until an approved deploy
  and sync run.

### Least confident decisions

1. The deployed token may have a different organization view from the local
   credential, so `49` versus `57` needs a post-deploy name-level comparison.
2. All-text selection still excludes binary, generated, secret-shaped,
   symlink, lock, and oversized files by policy.

## 2026-08-23 local verification after coverage defaults

- [VERIFIED] `uv run pytest -q` returned
  `2354 passed, 6 skipped, 12 warnings in 59.19s`.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`.
- [VERIFIED] `git diff --check` returned no output and exit code `0`.
- [VERIFIED] The documentation detector returned no findings for
  `docs/progress.md`, `docs/operations.md`, and `.local-review/status.md`.
- [NOT DETERMINED] The local suite cannot prove Railway's deployed source
  revision, production sync coverage, provider response, or MCP retrieval.
- [PLANNED] Obtain fresh deployment approval, deploy the reviewed revision,
  then run the frozen CLI and MCP known-answer checks before declaring the
  production path ready.

### Least confident decisions

1. The deployment approval boundary remains open because production still
   reports the older state schema and search timeout.
2. The first all-org sync may expose private-repository permission gaps that
   local inventory alone cannot detect.

## 2026-08-23 autonomous scheduler and Linear schema check

- [VERIFIED] Read-only Railway variable inspection returned
  `CITADEL_EVOLVE_SCHEDULER_ENABLED=true`,
  `CITADEL_EVOLVE_INTERVAL_SECONDS=3600`,
  `CITADEL_EVOLVE_COGNIFY_FORCE=false`, and
  `CITADEL_LIFECYCLE_ENABLED=true`.
- [VERIFIED] The same prefix-filtered inspection returned no variables with
  the `CITADEL_PIPELINE_*`, `CITADEL_GITHUB_SYNC_*`,
  `CITADEL_LINEAR_SYNC_*`, `CITADEL_REPO_CONTENT_SYNC_*`, or
  `CITADEL_SELF_IMPROVE_*` prefixes. This is an exact-name result, not proof
  that the stages are disabled because `scripts/run_railway.py` has defaults.
- [VERIFIED] The latest filtered Railway logs contain
  `POST /search HTTP/1.1" 504 Gateway Timeout`,
  `Mesh recorded search failure: search budget exceeded`, and
  `Search attempt on an empty knowledge graph`.
- [INFERRED] The autonomous scheduler is configured to run, but the current
  evidence does not prove that its sync stages complete or that they use the
  local all-org and Linear-context implementation.
- [VERIFIED] Official Linear documentation lists Issues, Issue comments,
  Projects, Project updates, Documents, Initiatives, and Initiative Updates as
  supported data-change models. Sources:
  https://linear.app/developers/webhooks and
  https://linear.app/developers/graphql.
- [VERIFIED] Official Linear documentation says the GraphQL API supports
  introspection and can return partial data with an `errors` array. Source:
  https://linear.app/developers/graphql.
- [NOT DETERMINED] No authenticated Linear schema or permission probe ran in
  this session because no Linear API key is available locally.

### Least confident decisions

1. Stage defaults may make the scheduler run more work than the Railway
   variable list suggests, but its completion state remains unproven.
2. The six local Linear query shapes still need a live authenticated schema
   check before they can be called complete.

## 2026-08-23 GitHub repository-set reconciliation

- [VERIFIED] The local GitHub inventory returned `57` repositories: `49`
  public and `8` private. The private names were
  `masumi-network/Scout`, `masumi-network/Sokosumi-okx`,
  `masumi-network/Sokosumi_Agent_Pulse`,
  `masumi-network/Vault-Backup-Mirror`, `masumi-network/Velos`,
  `masumi-network/masumi-cli`, `masumi-network/nori-devrel-agent`, and
  `masumi-network/pheme`.
- [VERIFIED] A read-only name-level diff between the local public repository
  set and `/data/citadel-state/github_sync_state.json` returned no output and
  exit code `0`. Railway's stored `49` names exactly match the local public
  set.
- [CORRECTED] The earlier statement that the `49` versus `57` difference was
  unresolved is obsolete. The current production state excludes the `8`
  private repositories. The reason for that exclusion, such as token scope or
  an older sync setting, is still not identified.
- [NOT DETERMINED] The full all-repository requirement is not met in Railway
  until a reviewed deployment discovers and records the private repositories.

### Least confident decisions

1. The name comparison proves stored-state coverage, not the current token's
   ability to read every private repository after redeploy.
2. Private repository content still requires the production token to have the
   required read scope.

## 2026-08-23 MCP LLM-dependency boundary

- [VERIFIED] Seat writes route to the `light` tier in `kb/server.py:2109-2123`.
  `kb/learning.py:132-137` sets enrichment to `None` and improvement to `false`
  for that tier. This user-level path does not request generative LLM work.
- [VERIFIED] `kb/lifecycle.py:1651-1659` defines lexical search over retained
  source bytes without a vector or language model. `kb/service.py:998-1001`
  skips vector recall when the current vector scope is empty, and
  `kb/service.py:1030-1039` calls the lexical fallback.
- [VERIFIED] The current MCP contract still rejects `cognify=false` at
  `kb/mcp_server.py:1341-1342`. HTTP validation also restricts `cognify` to
  `true` at `kb/server.py:994-1001`. Every accepted MCP ingest therefore
  requests projection even when the caller only needs source capture.
- [VERIFIED] `kb/cognee_client.py:815-846` separates source storage from
  chunk, vector, and graph projection. `kb/lifecycle_worker.py:587-668` runs
  vector projection before graph projection. Railway logs contain
  `LLMQuotaExceededError` and `OpenRouter free-model daily quota is exhausted`.
- [CORRECTED] The earlier statement that basic capture and retrieval have no
  generative LLM dependency describes the intended light and lexical paths. It
  does not prove that the current MCP ingest contract is capture-only, or that
  production search works without entering a provider path.
- [NOT DETERMINED] The exact component that consumed the production search
  budget is still unisolated. The live MCP probe returned HTTP `504` and no
  Citadel search hit.
- [PLANNED] Add an explicit capture-only ingest mode, then verify source
  retention and lexical search with projection disabled. Keep vector embeddings
  and graph enrichment optional follow-up stages.

### Least confident decisions

1. `cognify=false` should defer projection while preserving a durable pending
   operation for later worker pickup.
2. A deployed lexical-only canary must prove that provider outages do not block
   agent search when retained source bytes exist.

## 2026-08-23 MCP and CLI capture-only correction

- [CORRECTED] The preceding MCP boundary section recorded the contract before
  the capture-only patch. It is historical evidence, not the current contract.
- [VERIFIED] `kb/mcp_server.py` defaults `citadel_ingest` to `cognify=false` and
  returns `COGNIFY_SCHEDULER_ONLY` before any HTTP call when a caller requests
  `cognify=true`.
- [VERIFIED] `kb/server.py` rejects `cognify=true` with HTTP `422`, always passes
  `defer_cognify=true`, and returns `projection_requested=false` for user
  ingest. The source remains durable. Lifecycle-enabled nodes leave the
  projection job pending for the scheduler.
- [VERIFIED] `kb/status.py` and `kb/cli.py` send `cognify=false` for user
  ingest. CLI `--cognify` now returns `COGNIFY_SCHEDULER_ONLY`.
- [VERIFIED] User retrieval passes `allow_generative=false` from
  `kb/service.py` to `kb/cognee_client.py`. The client selects `SearchType.CHUNKS`,
  skips Cognee `recall()` auto-routing, and forces `AUTO_FEEDBACK=false`.
- [VERIFIED] Focused contract checks returned `6 passed` for MCP ingest,
  `23 passed` for CLI ingest, and `17 passed` for server ingest. Ruff returned
  `All checks passed!`.
- [NOT DETERMINED] Railway still runs the previously deployed revision. No
  production deploy occurred in this pass, so live MCP search remains unproven.
- [PLANNED] Deploy only after a fresh approval. Then run capture, lexical
  retrieval, vector retrieval, CLI, and MCP known-answer checks separately.

### Least confident decisions

1. Rejecting explicit user `cognify=true` is the correct compatibility choice.
2. The current production revision remains unchanged until the deployment gate.

## 2026-08-23 Post-deploy production acceptance

- [VERIFIED] Railway deployment `586c8c09-e1e1-48da-9ad4-cd169b8db5d8` returned
  `SUCCESS` at `2026-08-23 11:48:40 +02:00`. The deploy command ended with
  `Deploy complete`.
- [VERIFIED] The deployment used the linked `Citadel` project, `production`
  environment, and `Citadel-Archive` service. It uploaded the current local
  worktree through `railway up`; the worktree was dirty before deployment.
- [VERIFIED] `curl -sS https://citadel.utxo.ag/healthz` returned
  `{"ok":true,"service":"citadel"}`. The unauthenticated
  `/health/ready` route returned `{"ok":false,"service":"citadel"}`.
- [VERIFIED] `citadel status --json --check-search` reported search
  `ok=true`, `detail="1 result(s)"`, and `latency_ms=13987`. The same check
  reported `lifecycle invariants failing:
  current_generation_searchable_census_mismatch`.
- [VERIFIED] The production corpus check reported `40/601 sampled documents
  fully indexed`, with searchable backends `graph 24`, `relational 323`, and
  `vector 20`. It reported `tracked_sources 464`, `indexed_docs 7382`, and
  `indexed_edges 28852`.
- [VERIFIED] `citadel_list_sources` reported `49` GitHub repositories, `94`
  repository-content files, `321` Linear issues, `7397` graph records, and
  `67` vector records. Its Linear metadata reported
  `listing_complete=false`, `mirror_count=0`, and
  `unresolved_assignee_count=185`.
- [VERIFIED] MCP `citadel_search` for `Sokosumi MCP payment` returned `10`
  hits. The returned titles included
  `linear:issue:fe26f897-c450-469c-9b8e-3d5cfb4ad0e2` and
  `github:masumi-network/Sokosumi-MCP:path:README.md`. The response reported
  `basis=lexical-term-overlap`.
- [VERIFIED] MCP `citadel_linear_search` for `subscription credits` returned
  zero hits and the warning
  `Filters matched 0 of 15 fetched candidates; the page is short because
  post-retrieval filters excluded the rest, not because the search found
  nothing else.` The main search still returned a Linear issue, so the scoped
  Linear route is a separate failure surface.
- [VERIFIED] Railway logs contain repeated
  `Failed to parse properties JSON for node ...`, then
  `No nodes found in the database`. Railway also reported
  `Messages dropped: 750` after its `500 logs/sec` replica limit.
- [INFERRED] Basic production retrieval now works through a lexical or
  relational fallback for the tested queries. The vector and readiness counts
  remain below the acceptance target.
- [NOT DETERMINED] The stored graph-property parse warnings may cause the
  projection census mismatch, but this session did not isolate the first bad
  record or prove the repair path.
- [PLANNED] Diagnose the stored graph-property format, add a bounded repair or
  skip policy, then rerun projection coverage and the same known-answer tests.
  Do not accept production until `/health/ready` is true, vector coverage is
  measured, and the scoped Linear query returns a known issue.

### Least confident decisions

1. The graph-property parse warnings may be the root cause rather than a
   startup symptom from an older stored database.
2. The current known-answer result may use fallback retrieval even when vector
   records exist; the response must expose backend evidence before acceptance.

## 2026-08-23 Scoped Linear filter repair

- [VERIFIED] Production `citadel_linear_search` fetched `15` candidates and
  matched `0` for `subscription credits`, while unfiltered MCP search returned
  a Linear issue for the same production corpus.
- [VERIFIED] `kb/lifecycle.py:1735-1749` preserves the connector
  `source_key` in lexical fallback metadata, while the result itself uses
  `source: lifecycle`.
- [VERIFIED] Before this patch, `kb/server.py:2788-2795` copied that generic
  lifecycle label into public provenance. The `linear-issue` filter therefore
  rejected real Linear fallback hits.
- [VERIFIED] The local patch maps strict `linear:issue:` source keys to the
  `linear-issue` scope and records `basis: lifecycle-source-key`.
- [VERIFIED] Regression tests cover public shaping and the `/search` endpoint.
  `.venv/bin/pytest -q` returned `2359 passed, 6 skipped, 12 warnings in
  90.93s (0:01:30)`.
- [INFERRED] The deployed zero-result Linear search was a provenance mapping
  defect, not evidence that the Linear issue was absent.
- [NOT DETERMINED] This local repair is not deployed yet. Projection coverage
  remains `40/601`, and the graph-property warning cause is still open.
- [PLANNED] Request approval for one deployment. Then rerun scoped Linear
  search, CLI search, MCP search, and readiness checks against the new revision.

### Least confident decisions

1. Strict source-key prefixes cover all connector records in the production
   store; an unrecognized legacy key will still fail closed.
2. The graph-property warnings may be independent of the census mismatch.

## 2026-08-23 Post-deploy source identity checkpoint

- [VERIFIED] Railway deployment `89a6a85c-8169-4409-976b-5f4f7db1c6c0`
  returned `SUCCESS` at `2026-08-23 12:23:38 +02:00`.
- [VERIFIED] The deployed scoped Linear query returned `2` hits with
  `candidates_fetched=15`, `candidates_matched=2`, and `scope_applied=true`.
  The first hit title was `linear:issue:5b464ec8-eed2-4b70-be11-2bdb2418b75a`,
  with text `# Linear DES-144: Subscription Credits missing` and source
  locator `https://linear.app/masumi/issue/DES-144/subscription-credits-missing`.
- [VERIFIED] A deployed repo-scoped MCP query for `MCP payment` and
  `masumi-network/Sokosumi-MCP` returned `0` of `19` candidates. This exposed a
  second identity-filter defect after the Linear source filter was repaired.
- [VERIFIED] The unscoped `Sokosumi MCP payment` query returned a Linear issue
  as its fifth result, but its first four results were local repository snapshot
  files. The response reported `basis=lexical-term-overlap`.
- [VERIFIED] Railway logs still contain repeated
  `Failed to parse properties JSON for node ...`, `No nodes found in the
  database`, and `Messages dropped: 752` after the `500 logs/sec` limit.
- [VERIFIED] The logs also contain
  `Repo content sync BLOCKED ...two-factor-section.tsx: severity=high
  findings=2 rules=secret_assignment`.
- [CORRECTED] The previous checkpoint identified the Linear source filter but
  did not include repo and path identity. Both filters also failed for lifecycle
  fallback results because public provenance lacked connector identity.
- [VERIFIED] The local repair derives strict GitHub repo and path identity from
  `github:<org>/<repo>:path:<path>` source keys, including chunk parent keys.
  The full suite returned `2361 passed, 6 skipped, 12 warnings in 84.72s`.
- [NOT DETERMINED] `/health/ready` remains false. The corpus probe timed out
  after `2s`; searchable counts were graph `25`, relational `323`, and vector
  `20`.
- [PLANNED] Request approval for a second deploy. Then verify repo, path, and
  Linear scopes, inspect general-search ranking, and rerun readiness checks.

### Least confident decisions

1. The strict GitHub source-key parser may not cover legacy source keys that do
   not use the current `:path:` format.
2. The blocked high-severity file may be an expected security decision, but its
   relationship to the reported source count is not determined.

## 2026-08-23 Fresh-eyes review of source identity repair

- [REPORTED] The first fresh-eyes review identified possible source-key
  forgery, legacy case variants, and delimiter ambiguity. It also said the
  production repo-filter failure was not determinable from candidate counts.
- [VERIFIED] Public `/ingest` has no `source_key` field in
  `kb/server.py:994-1002`. MCP `citadel_ingest` exposes no `source_key` field
  in `kb/mcp_server.py:1283-1290`. `/api/contribute` prefixes caller URLs
  with `contribution:` at `kb/server.py:7904-7907`.
- [INFERRED] The reviewed forgeability case is not a proven user-level exploit
  through these public write paths. Other internal or administrative writers
  were not exhaustively audited here.
- [VERIFIED] The parser now validates Linear IDs, GitHub repository names, and
  printable paths. It checks the lifecycle parent key before the chunk key.
  Tests cover malformed keys, C1 controls, chunk parents, repo, path, and
  endpoint filters.
- [REPORTED] The final fresh-eyes review returned `APPROVED` with no remaining
  real findings.
- [CORRECTED] The reviewer's statement that focused tests could not run because
  Cognee was unavailable was false for this session. `.venv/bin/pytest -q`
  returned `2363 passed, 6 skipped, 12 warnings in 113.35s`.
- [NOT DETERMINED] The repo-scoped production result remains `0/19` until this
  final parser revision is deployed.
- [PLANNED] Request fresh approval for one deployment. Rerun MCP and CLI repo,
  path, and Linear checks, then reassess readiness and graph warnings.

### Least confident decisions

1. Internal or administrative writers may expose source-key inputs outside the
   public contracts checked here.
2. Production repo candidates may lack lifecycle metadata for a reason other
   than the parser defect.

## 2026-08-23 Fresh-eyes plan review

- [REPORTED] Fresh-eyes returned `CHANGES REQUIRED` for the first deployment
  plan. It identified missing artifact identity, build identity, known-answer
  pass criteria, mandatory raw metadata inspection for the existing `0/19`
  result, and an independent deployment refute.
- [VERIFIED] The current worktree has `HEAD
  a910f4f37a758dd033c370b7a55c5cf7467d58b9`. The patch fingerprint for
  `kb/server.py` and `tests/test_search_response_shaping.py` is
  `9da920a1e1cf36fa8e8bc4b099c400f6c73505c023efe5460812690cd97bf4e6`.
- [VERIFIED] The Docker build writes the wheel SHA to the build identity file
  at `Dockerfile:14-16`. The public discovery response exposes `build_id` and
  `deployment_id` at `kb/server.py:4958-4965`.
- [PLANNED] Revised checkpoint sequence:
  1. Record the exact `HEAD`, worktree status, and patch fingerprint before
     deployment.
  2. Obtain fresh deployment approval. Deploy the recorded worktree and match
     the Railway deployment ID to `/.well-known/citadel.json` build identity.
  3. Check `/healthz` and `/health/ready`. Keep acceptance blocked if readiness
     is false.
  4. Run known-answer MCP and CLI checks for Linear, repo, path, and unscoped
     search. Record expected result IDs, `scope_applied`, candidate counts,
     backend, provenance, and citation fields. The existing repo `0/19` path
     must trigger raw candidate-metadata inspection before any conclusion.
  5. Ask fresh-eyes to refute the saved deployment evidence. Accept only when
     retrieval, citations, backend evidence, and readiness all pass.
- [NOT DETERMINED] The current production deployment does not expose the final
  parser fingerprint because this worktree revision is not deployed.

### Least confident decisions

1. The current Railway upload must preserve the recorded dirty-worktree patch.
2. A read-only route or diagnostic may be needed to expose raw candidates
   before public source filtering.

## 2026-08-23 Fresh-eyes plan review correction

- [REPORTED] The second fresh-eyes review returned `CHANGES REQUIRED`. It
  required the artifact freeze to run immediately before deployment, matching
  Railway identity to the frozen artifact, exact reproducible commands, and a
  raw-evidence review without an acceptance conclusion.
- [PLANNED] Immediately before deployment, rerun these exact checks:
  `git rev-parse HEAD` and
  `git diff --no-ext-diff --binary -- kb/server.py
  tests/test_search_response_shaping.py | shasum -a 256`. Expected values are
  `a910f4f37a758dd033c370b7a55c5cf7467d58b9` and
  `9da920a1e1cf36fa8e8bc4b099c400f6c73505c023efe5460812690cd97bf4e6`.
  A mismatch stops deployment.
- [PLANNED] Build the wheel in a fresh temporary directory with
  `python -m build --no-isolation --wheel --outdir <tmp-dir>`, hash the exact
  `citadel_archive-0.5.1` wheel, and compare it with the deployed
  `/.well-known/citadel.json` `build_id`. Match its `deployment_id` to Railway.
- [PLANNED] Give fresh-eyes the frozen identifiers and raw deployment outputs
  without the acceptance conclusion. Keep repo `0/19` blocked until raw
  candidate metadata is captured or the missing diagnostic is recorded as a
  blocker.

### Least confident decisions

1. Local and Railway wheel builds may differ if the wheel is not reproducible.
2. The current public search contract may not expose raw pre-filter candidates.

## 2026-08-23 Fresh-eyes plan review: build identity correction

- [REPORTED] The third fresh-eyes review found that the wheel-hash check did
  not match the runtime identity contract. `kb/build_identity.py:40` prefers
  `RAILWAY_GIT_COMMIT_SHA` over `CITADEL_BUILD_ID`.
- [VERIFIED] The current production discovery response returned
  `build_id: null` and
  `deployment_id: 89a6a85c-8169-4409-976b-5f4f7db1c6c0`.
- [CORRECTED] The plan no longer treats the wheel hash as the public `build_id`.
  It treats a missing build ID as an identity failure. A wheel hash is separate
  build evidence only when Railway exposes it through build or image evidence.
- [PLANNED] Immediately before deployment, record `pwd`, the exact Railway
  command, `HEAD`, status, and the parser patch hash. The upload source is the
  current worktree. If the worktree is dirty and the service exposes only a
  commit SHA or no build ID, stop. Use a clean source revision or add a source
  fingerprint before claiming artifact identity.
- [PLANNED] After deployment, give fresh-eyes the raw manifest, health
  responses, Railway deployment output, log evidence, and search captures. Do
  not include the acceptance conclusion in that evidence packet.

### Least confident decisions

1. Railway may omit both commit and wheel identity for local uploads.
2. A clean revision or source fingerprint may be required before deployment.

## 2026-08-23 Audit-record indexing checkpoint

- [VERIFIED] The CLI ingest path returned `NODE_UNREACHABLE` for all three
  audit-section attempts, although CLI search and MCP reads worked.
- [VERIFIED] MCP capture-only ingest accepted the fresh-eyes sections into
  `seat:sarthi`. It returned these source revisions and projection jobs:
  `faaa6830-9abe-5f3e-b122-8c6421d7835d` /
  `75b5177e-002f-54a6-8227-6ffe42e89d94` for `docs/progress.md`,
  `ccc70c2e-3127-5302-9eb3-823b014e2949` /
  `492edaf6-7524-591d-8992-159cfad2d5d5` for `docs/operations.md`, and
  `261c00e9-7912-5f07-9dbd-a2c4402e8126` /
  `7f8bb653-64d1-5e22-915f-852c3ae637be` for `.local-review/status.md`.
- [VERIFIED] Each MCP receipt returned `accepted: true`, `cognify: false`,
  `projection_requested: false`, and `projection_state: pending`.
- [VERIFIED] After a 35-second wait, all three operation records still had
  `state: pending` for relational, vector, and graph receipts. Exact searches
  returned older `# Git commit snapshot`, `py`, `js`, and `html` notes instead
  of the new audit sections.
- [INFERRED] Durable capture works. Searchable projection of these records is
  not proven and likely waits on the scheduled projector, but its cause is not
  determined by this measurement.
- [PLANNED] Keep this indexing checkpoint open. Inspect the projection worker
  and job state, then rerun operation and exact-text search checks. Do not call
  the audit records searchable until a result contains the recorded source text.

### Least confident decisions

1. The pending jobs may be delayed rather than failed.
2. The CLI writer and MCP writer may use different node configuration paths.

## 2026-08-23 Audit-record indexing follow-up

- [VERIFIED] At `2026-08-23T11:49:24Z`, all three projection jobs still had
  `state: pending`, `attempt: 0`, and `last_error_code: null`. The records were
  created at `2026-08-23T11:43:20Z`.
- [VERIFIED] The same source check reported vector index records `67`, graph
  index records `7656`, and `since_restart.indexed_chunks: 0`.
- [NOT DETERMINED] The worker may be waiting on a maintenance lock, an older
  queue head, or a scheduled pass. Current evidence does not distinguish these
  causes.
- [PLANNED] Treat audit-record searchability as blocked. Inspect the worker
  lease and maintenance state through a read-only diagnostic or logs before
  retrying writes.

### Least confident decisions

1. The zero-attempt state suggests the worker has not claimed these jobs, but
   the operation endpoint does not expose the queue head.
2. Graph record growth does not prove these three documents were projected.

## 2026-08-23 Live blocker refresh

- [VERIFIED] `citadel status --json --check-search` returned `healthy: false`.
  Its search smoke check returned `ok: true` with `1 result(s)`, but the corpus
  check returned `ok: false` with
  `current_generation_searchable_census_mismatch`.
- [VERIFIED] The same corpus check reported `61/601` fully indexed sampled
  documents, searchable backends graph `24`, relational `322`, and vector `21`.
  It also reported `corpus health timed out after 2s; serving cached result`.
- [CORRECTED] An earlier sample reported `56/601`. The later `61/601` value is
  a newer measurement, not proof that the audit records became searchable.
- [VERIFIED] Mesh status reported graph records `7662`, vector records `67`,
  and `since_restart.indexed_chunks: 0`. Graph record growth alone does not
  prove vector retrieval.
- [VERIFIED] Source collection itself is currently complete by its own fields:
  repo content reports `repo_discovery_complete: true` and `tracked_files: 4721`;
  Linear reports `1800` issues and `2867` context records with both listings
  complete. This does not prove projection completeness.
- [VERIFIED] The evolve summary reports last completion at
  `2026-08-22T23:49:20.458291Z` and `overdue: true`.
- [NOT DETERMINED] The three audit jobs remain at `attempt: 0` with no error.
  The worker may wait on a maintenance lock, an older queue head, or a scheduled
  pass. The public evidence does not identify which cause applies.
- [INFERRED] The green search smoke check is too weak for acceptance. It proves
  one query returned a result, while the corpus and new-document projection
  remain unhealthy.
- [PLANNED] Obtain read-only queue, lease, maintenance-lock, or Railway log
  evidence. Do not retry writes until the worker claim path is understood.

### Least confident decisions

1. The overdue evolve pass may be related to the pending queue, but no causal
   link is proven.
2. The two-second corpus timeout may hide a slower healthy read, but the cached
   census still fails the readiness contract.

## 2026-08-23 Projection lane repair checkpoint

- [CORRECTED] An earlier capture-only note said that accepted content deferred
  all indexing. The real Cognee gateway now runs the vector lane for
  `defer_cognify=True`; graph enrichment stays deferred. Evidence:
  `kb/service.py:276-280` and `kb/lifecycle_worker.py:648-650`.
- [VERIFIED] The vector route uses only `classify_documents`,
  `extract_chunks_from_documents`, and `index_data_points` in
  `kb/cognee_client.py:4680-4688`. Its final return is outside the maintenance
  context at `kb/cognee_client.py:4706-4709`.
- [VERIFIED] A deferred job requires searchable relational and vector receipts.
  The store writes `state = 'deferred'` and
  `last_error_code = 'graph_enrichment_deferred'` in
  `kb/lifecycle.py:2167-2189`.
- [VERIFIED] The graph lane can claim deferred jobs. Its queue order uses
  `available_at` for the deferred-only lane, while baseline seat work keeps its
  seat priority in `kb/lifecycle.py:1891-1900`.
- [VERIFIED] Phase 1 scheduler cancellation resumes both lanes through
  `kb/server.py:456-458`. The graph lane also starts after the canary's graph
  write at `kb/service.py:1943-1947`.
- [VERIFIED] The test suite covered the new queue and logging paths. The exact
  command returned `309 passed, 11 warnings in 28.98s` for the focused
  lifecycle, service, scheduler, Cognee, identity, and logging tests. Ruff
  returned `All checks passed!`. `git diff --check` returned no output.
- [CORRECTED] The focused count above predates the final Phase 2 cancellation
  regression test. The final full command returned `2373 passed, 6 skipped,
  12 warnings in 52.86s`.
- [VERIFIED] Explicit `DEBUG` logging can be enabled and then reversed. The
  code sets LiteLLM logger `disabled` from `resolved == "DEBUG"` at
  `kb/logging_utils.py:93-112`.
- [VERIFIED] Runtime identity prefers Railway or explicit environment identity,
  then accepts a valid 64-hex image marker at
  `kb/build_identity.py:64-86`. Missing or malformed markers stay absent.
- [NOT DETERMINED] This local test proves that the vector task can run while an
  application graph maintenance context is held. It does not prove that the
  installed Cognee relational and vector providers tolerate concurrent writes.
  A disposable real-provider acceptance run is still required.
- [NOT DETERMINED] Production still has no evidence for this local revision.
  The live deployment remains the earlier Railway deployment recorded above.
- [NOT DETERMINED] Repeated malformed graph-property warnings and the safe,
  non-destructive repair method remain open. The logging change reduces log
  volume; it does not repair existing graph records.
- [PLANNED] Before deployment, run the full suite, freeze the local artifact
  identity, and request fresh deployment approval. After deployment, match the
  service identity, check readiness, run known-answer CLI and MCP searches, and
  send raw results to fresh-eyes before any acceptance decision.

### Least confident decisions

1. The vector route's provider-level concurrent write safety remains unmeasured.
2. The malformed graph records may need a provider-specific migration after a
   bounded read-only diagnosis.

## 2026-08-23 Fresh-eyes final review

- [REPORTED] Fresh-eyes first reported a Phase 1 cancellation gap and an
  unavailable-Cognee test environment. The first claim was stale against the
  current worktree, and the primary environment has Cognee installed.
- [VERIFIED] Current `kb/server.py:456-458` passes
  `include_deferred=True` during Phase 1 cancellation. Current
  `kb/server.py:502-523` resumes the queue only when Phase 2 did not cancel.
- [REPORTED] Fresh-eyes rechecked the current files and returned `APPROVED`.
  Its remaining limits are provider-level concurrency and malformed graph data.
- [VERIFIED] Final full verification returned `2373 passed, 6 skipped,
  12 warnings in 52.86s`. Full Ruff returned `All checks passed!`.
- [NOT DETERMINED] Production behavior is still unverified for this local
  revision. Deployment and Railway runtime checks remain pending approval.

### Least confident decisions

1. Real Cognee provider concurrency still needs a disposable acceptance run.
2. Existing malformed graph records still need read-only diagnosis.

## 2026-08-23 Retrieval and graph-error repair checkpoint

- [VERIFIED] The local ranking path previously preserved provider order for
  multi-term general queries. `select_public_search_page` only enabled ranking
  for docs, spec, asset, or single-term queries at `kb/server.py:3189-3192`.
- [CORRECTED] General queries now use lexical term coverage when at least one
  candidate contains a query term. Pages with zero lexical coverage keep the
  provider order, so synonym-based vector matches are not reordered without
  evidence. The change is in `kb/search_format.py:540-575` and
  `kb/server.py:3189-3190`.
- [VERIFIED] Regression coverage proves both branches. The focused command
  `.venv/bin/pytest -q tests/test_search_format.py tests/test_search_response_shaping.py tests/test_mcp_server.py`
  returned `151 passed, 11 warnings in 7.09s`.
- [CORRECTED] The MCP ingest docstring and timeout message no longer claim that
  user writes run inline Cognify. They describe background relational and vector
  projection, with graph enrichment in the scheduled lane.
- [CORRECTED] `docs/mcp/README.md` now describes the same capture and projection
  contract. User-facing MCP and CLI paths reject inline Cognify requests.
- [VERIFIED] Railway read-only deployment output returned the latest successful
  deployment as `89a6a85c-8169-4409-976b-5f4f7db1c6c0` with status `SUCCESS` and
  creation time `2026-08-23T10:23:38.122Z`.
- [VERIFIED] Railway read-only logs returned `Messages dropped: 760`, repeated
  `Failed to parse properties JSON for node ...`, `No nodes found in the
  database`, and a structured-output failure at `edges.1.target_node_id` with
  `input_value=None`.
- [INFERRED] The live graph lane is still noisy and receives malformed model
  output. The local vector lane separation prevents that graph failure from
  gating retrieval, but it does not repair existing graph records.
- [NOT DETERMINED] The Railway deployment source revision is not exposed by the
  current read-only deployment output. Live behavior for this worktree remains
  unverified.
- [PLANNED] Run the full local suite and Ruff. After fresh deployment approval,
  verify health, readiness, known-answer MCP and CLI search, source filters,
  citations, and separate vector and graph receipt states.

### Least confident decisions

1. Lexical coverage improves weak general pages, but it cannot resolve semantic
   matches that share no query terms.
2. The malformed graph output may need a Cognee prompt or model change after a
   bounded provider reproduction. No provider data repair is authorized here.

## 2026-08-23 Local repair verification

- [CORRECTED] The earlier ranking repair caused a source-mixing regression for
  multi-dataset pages. `kb/server.py:3209-3229` now keeps the ranked order inside
  each dataset while reserving the existing secondary-source slot. The exact
  Central inclusion test passes again.
- [VERIFIED] The graph worker now recognizes the observed malformed structured
  output shape at `kb/lifecycle_worker.py:140-154`. The deferred-only graph lane
  records `graph_output_invalid`, returns the job to `deferred`, and waits one
  hour before retrying at `kb/lifecycle_worker.py:327-341`.
- [VERIFIED] The regression test proves vector search stays available while the
  graph receipt remains pending. It is in
  `tests/test_lifecycle_worker.py:556-626`.
- [VERIFIED] The lifecycle worker command returned `25 passed in 6.11s`.
- [VERIFIED] The search, server, and MCP command returned `41 passed, 11
  warnings in 5.94s`.
- [VERIFIED] The full command returned `2377 passed, 6 skipped, 12 warnings in
  76.96s (0:01:16)`. Full Ruff returned `All checks passed!`. `git diff --check`
  returned no output.
- [INFERRED] The local graph guard prevents the observed malformed graph output
  from turning vector-ready work into a terminal lifecycle failure. It does not
  repair malformed records already stored by Cognee.
- [NOT DETERMINED] Railway still runs an unverified revision for this worktree.
  The local repairs have not passed live MCP, CLI, health, and known-answer
  checks.
- [PLANNED] Run fresh-eyes on the latest diff. With current deployment approval,
  deploy the frozen revision, match its runtime identity, then run the live
  acceptance matrix.

### Least confident decisions

1. The graph error matcher covers the observed `KnowledgeGraph` validation shape,
   but other malformed graph response shapes remain unmeasured.
2. A one-hour graph retry delay needs live observation after deployment.

## 2026-08-23 Fresh-eyes latest review

- [REPORTED] Fresh-eyes reviewed the current ranking, source reservation, vector
  lane, and malformed graph-output guard. It returned `APPROVED` with no blocking
  local finding.
- [REPORTED] Fresh-eyes kept real provider behavior and existing malformed graph
  data as `[NOT DETERMINED]`. This matches the local evidence boundary.

### Least confident decisions

1. Railway still needs a deployment identity match before local results can be
   used as live evidence.
2. Existing graph records need a read-only diagnosis before repair.

- [CORRECTED] The full suite was rerun after the final test-file cleanup. The
  exact result was `2377 passed, 6 skipped, 12 warnings in 76.79s (0:01:16)`.

### Least confident decisions

1. Railway identity matching remains unverified for this worktree.
2. Existing graph records still need read-only diagnosis.

## 2026-08-23 Core-path live audit

- [VERIFIED] Citadel MCP `citadel_list_sources` reports GitHub activity for
  `masumi-network` with `tracked_repositories: 49` and
  `tracked_commit_repositories: 49`.
- [VERIFIED] The same MCP response reports repository-content sync with
  `all_repos: true`, `repo_discovery_complete: true`, `tracked_files: 4721`,
  and `refused_files: 2`. This proves source discovery and local sync state,
  not vector searchability.
- [VERIFIED] The same MCP response reports Linear with `issue_count: 1800`,
  `context_record_count: 2867`, `listing_complete: true`,
  `context_listing_complete: true`, and `last_error: null`.
- [VERIFIED] MCP `citadel_linear_search` for `SOK-563` returned two hits with
  `scope_applied: true`. One returned title was
  `linear:issue:df5cff71-6960-4cf9-9ea1-5073c32f06dd`.
- [CORRECTED] The live acceptance boundary is narrower. Source sync is
  verified, but projection coverage is not. `citadel status
  --json --check-search --no-recent` returned `search: false`,
  `detail: timed out after 15s - node warming up`, and `search canary failed
  (search_hit=None, error=RuntimeError); lifecycle invariants failing:
  current_generation_searchable_census_mismatch; searchable by backend:
  graph 25, relational 323, vector 22; corpus probe failed: 64/602 sampled
  documents fully indexed (602/602 sampled)`.
- [INFERRED] A green source status means that the connector fetched and
  recorded source state. It does not mean that the source is searchable.
- [NOT DETERMINED] The cause of the low vector projection count is not proven.
  Candidate causes include a stalled queue, provider latency, or an
  incremental pipeline that skips data already marked processed by Cognee.
  No production repair or reindex was run.
- [PLANNED] Core acceptance requires one citation-bearing known answer through
  CLI and MCP, plus a complete current-generation vector census. Source counts
  alone cannot pass this gate.

### Least confident decisions

1. The exact vector projection failure cause needs a read-only operation-level
   view or a deployment with the current local revision.
2. The `vector` index record count `70` is not treated as a searchable document
   count because the status contract does not define that field as a document
   census.

## 2026-08-23 Core projection repair checkpoint

- [VERIFIED] Authenticated Railway `GET /readyz` returned `"ok":false` and
  `"invariant_errors":["current_generation_searchable_census_mismatch"]`.
- [VERIFIED] The same response returned
  `"current_job_states":{"completed":21,"pending":9589,"running":1}` and
  `"current_searchable_by_backend":{"graph":25,"relational":323,"vector":22}`.
  This proves a current-generation queue backlog and a low vector receipt count.
- [VERIFIED] The response also returned
  `"current_projection_jobs":9611` and
  `"current_projection_receipts":28833`. These are lifecycle records, not a
  count of distinct Cognee relational documents.
- [VERIFIED] Railway logs returned the exact message
  `Railway rate limit of 500 logs/sec reached for replica, update your
  application to reduce the logging rate. Messages dropped: 8137`.
- [INFERRED] The graph LLM lane is producing enough output to hide useful
  operational logs. The log sample also contains a malformed graph edge with
  `target_node_id` set to `None`. This is separate from the vector path.
- [CORRECTED] The earlier cause list included a possible full-dataset scan and
  stale Cognee status. Local Cognee 1.4.1 source shows
  `body_data = data if data else await get_dataset_data(dataset_id=dataset.id)`
  at `.venv/lib/python3.12/site-packages/cognee/modules/pipelines/operations/pipeline.py:122`.
  Before this checkpoint, the vector path passed no `data`, so each lifecycle
  job asked Cognee to load the whole dataset.
- [VERIFIED] The local repair now loads only authorized `Data` rows joined
  through `DatasetData` and `Dataset` at `kb/cognee_client.py:1590-1639`.
  It passes those rows to `run_custom_pipeline` at `kb/cognee_client.py:4752-4775`.
  The lifecycle worker passes the claimed source ID at
  `kb/lifecycle_worker.py:642-647`.
- [VERIFIED] Document-scoped projection disables Cognee incremental and data
  cache flags. This prevents a stale per-data completion flag from skipping a
  source whose lifecycle receipt is not searchable.
- [VERIFIED] The focused command returned `115 passed, 10 warnings in 8.41s`.
  Ruff returned `All checks passed!` for the changed projection files.
- [NOT DETERMINED] The live deployment does not contain this local repair yet.
  No deployment, reindex, or production data write was run in this checkpoint.
- [NOT DETERMINED] The exact provider error behind the pending queue remains
  unknown. The ready check proves backlog state, but it does not expose the
  failing operation or the distribution of jobs by dataset.
- [PLANNED] The next local gate is a known-answer search over seeded Linear and
  repository records. The live gate still requires deployment approval, runtime
  identity matching, and CLI plus MCP searches with citations.

### Least confident decisions

1. Per-source projection removes the full-dataset input, but queue throughput
   still needs measurement after the repair runs on Railway.
2. The graph log flood needs a separate operational limit or scheduler change.

## 2026-08-23 Known-answer acceptance checkpoint

- [VERIFIED] CLI `citadel search "SOK-563" --json --top-k 1 --timeout 30`
  exited `0` with `"ok": true` and `"timed_out": false`.
- [VERIFIED] The CLI result title was
  `linear:issue:df5cff71-6960-4cf9-9ea1-5073c32f06dd`. Its response carried
  `"source": "linear-issue"`, `"citation_required": true`, and
  `"citations_available": true`. Its citation locator was
  `https://linear.app/masumi/issue/SOK-618/improvechat-remove-task-creation-from-welcome-screen`.
- [VERIFIED] MCP `citadel_search` for the same query and dataset returned the
  same title, `document_id` `e4ec6025-b6d8-54d8-aa83-55f5d7b35881`, and the
  same Linear source locator. The response reported `search_id` beginning
  `search:e82f30bbb99d4049` and `"no_lexical_match": false`.
- [CORRECTED] The previous statement that live search was unavailable was too
  broad. The 15-second status canary timed out while the 30-second CLI and MCP
  known-answer searches returned a cited hit. Readiness remains red because the
  current-generation census is incomplete.
- [NOT DETERMINED] This known answer does not prove complete vector coverage.
  It proves one Linear result is retrievable. Repository-content known answers
  and current-generation coverage still need deployment acceptance.

### Least confident decisions

1. The readiness timeout needs a separate latency decision after queue repair.
2. One Linear known answer cannot measure cross-repository recall.

## 2026-08-23 Vector batch cancellation checkpoint

- [VERIFIED] The vector-only lifecycle lane now claims a bounded batch of jobs
  from one dataset and sends one `vector_project` call with all source IDs.
  Each source still receives its own lifecycle receipt and lease result.
- [VERIFIED] The vector-only lane does not reconcile graph presence. Graph
  enrichment remains deferred to the separate graph worker.
- [VERIFIED] Batch cancellation during relational preparation reschedules every
  still-owned claimed lease before re-raising `asyncio.CancelledError`.
- [VERIFIED] Batch cancellation during vector projection cancels the provider
  awaitable and reschedules the current and remaining active leases.
- [VERIFIED] `uv run pytest -q tests/test_lifecycle_worker.py -x` returned
  `28 passed in 5.42s` after the cancellation tests were added.
- [VERIFIED] The bounded lifecycle, worker, service, and config suite returned
  `170 passed, 10 warnings in 17.22s` before the cancellation test additions.
- [PLANNED] Set `CITADEL_LIFECYCLE_PROJECTION_BATCH_SIZE=20` in the deployed
  revision only after explicit deployment approval. Then measure queue drain
  rate, vector receipt growth, and one repository known answer.
- [NOT DETERMINED] The batch size and provider throughput are not measured on
  Railway. This local test uses a fake projection gateway.
- [NOT DETERMINED] The local revision is not proven to be the active Railway
  deployment. No deployment or production reindex ran in this checkpoint.

### Least confident decisions

1. A batch size of 20 is a starting value, not a production capacity result.
2. The deployment still needs identity matching before its queue metrics can
   be attributed to this code.

## 2026-08-23 Local core acceptance checkpoint

- [VERIFIED] `uv run pytest -q` returned
  `2383 passed, 6 skipped, 12 warnings in 81.40s (0:01:21)`.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`.
- [VERIFIED] The avoid-AI-writing detector returned exit `0` with no output for
  `docs/progress.md`, `docs/operations.md`, and `.local-review/status.md`.
- [REPORTED] Fresh-eyes re-review of the batch cancellation and config changes
  returned the exact result `APPROVED`.
- [NOT DETERMINED] These local checks do not prove the active Railway revision,
  production queue drain, or live cross-repository vector recall.
- [PLANNED] After explicit deployment approval, match the deployed build
  identity, observe queue and receipt counts, then run CLI and MCP known-answer
  searches for one Linear record and one repository file.

### Least confident decisions

1. Railway throughput after deployment remains unmeasured.
2. Full cross-repository recall remains unproven until the live acceptance gate.

## 2026-08-23 Core functionality handoff checkpoint

- [VERIFIED] The final local full-suite run returned `2385 passed, 6 skipped,
  12 warnings in 54.65s`.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`, and
  `git diff --check` returned no output with exit code `0`.
- [VERIFIED] Lifecycle document drilldown now uses the retained source dataset
  for visibility. Central and admin access passed, while a foreign seat access
  returned `404` in `tests/test_server.py`.
- [VERIFIED] Public search metadata now preserves `vector` and
  `lexical_fallback` retrieval modes. The focused retrieval test passed.
- [VERIFIED] Capture-only ingest remains separate from scheduled projection.
  The full suite covers rejection of inline cognify requests.
- [VERIFIED] Live source status reports `49` GitHub repositories, `4721`
  repository-content files, `1800` Linear issues, and `2867` Linear context
  records. These totals describe sync state, not vector searchability.
- [CORRECTED] Live known-answer CLI and MCP searches return cited Linear and
  repository results, but the live status still reports
  `current_generation_searchable_census_mismatch` with vector `22` and
  `9589` pending jobs.
- [NOT DETERMINED] The public Railway manifest has `build_id: null`, so the
  active deployment cannot be attributed to this uncommitted local revision.
- [PLANNED] The next agent must review the worktree, obtain deployment
  approval, match the deployed build identity, measure queue progress, and
  rerun the CLI and MCP acceptance matrix.

### Least confident decisions

1. The failing operation behind the live pending queue remains unknown.
2. A live known answer may currently use lexical fallback rather than vector
   recall.
