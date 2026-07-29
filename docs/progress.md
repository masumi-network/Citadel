# Citadel Progress

Last updated: 2026-07-22.

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

**Action: rotate `CITADEL_ADMIN_KEY`** (surfaced in-session during ops).

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
- **Security reminder still open:** rotate `CITADEL_ADMIN_KEY`, the GitHub PAT,
  the OpenRouter key, and the Postgres password (surfaced in-session during ops).

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

- **Merged [PR #19](https://github.com/masumi-network/Citadel-Archive/pull/19)** → `main`
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
    `masumi-network/Citadel-Archive`; active automation is Railway, not GitHub
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
  - `npx skills add masumi-network/Citadel-Archive` installs the root
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
  - [Citadel-Archive](https://github.com/masumi-network/Citadel-Archive) is public
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
