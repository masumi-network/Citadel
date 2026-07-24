# Changelog

All notable changes to `citadel-archive` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Agent-facing search shaping + feedback.** Search hits carry a stable schema
  (`doc_type`, `content_hint`, `trust_tier`, `rank`, provenance) with
  spec/docs/asset-ID ranking and `canonical_only` / `exclude_ambient` /
  `types` / `repo` / `path` filters, shared by the CLI (`citadel search
  --json`) and the MCP `citadel_search` tool. Every search records non-blocking
  telemetry to the caller's **own seat Node** (a seat-less caller writes a
  presence-only row); `citadel_record_feedback` / `citadel feedback` add an
  explicit 1/-1 signal. New `citadel verify` / `citadel prepare-pr-context`
  helpers return `doc_shaped_sources` (hits that read like documentation — a
  starting point to verify, never an authority). See
  [ADR-0012](docs/adr/0012-attested-trust-vs-content-hint.md).
- **Public `/info` "State of the Vault" page + `GET /api/state`.** A node-served
  report at `/info` (`kb/static/info.html` + `info.css` + `info.js`): current
  metrics, shipped releases (v0.2 → v0.4), an architecture diagram, a
  commit-velocity chart, and the roadmap — with progressive `Go deeper`
  expanders and a light/dark toggle. Live metric tiles hydrate from a new public
  `GET /api/state` that returns **safe aggregates only** (version, health,
  per-source doc/repo counts, last-sync, totals), modeled on the
  `/.well-known/citadel.json` precedent — never vault content, per-seat data,
  internal source URLs, or tokens, and it degrades to empty rather than 500.
  CSP-clean (external CSS/JS; chart bars via CSSOM). Linked from the README with
  a Masumi-magenta badge.

### Changed

- **`/info` aligned to the AGENTIC / Masumi design system**
  (`masumi-network/sokosumi-landing` → `apps/sokosumi/DESIGN.md`): Inter-only,
  weight lightens as size grows (Inter Light headings, negative tracking,
  sentence case), a neutral ramp + a single Iris-magenta `#FF51FF` accent, flat
  elevation, and the signature segmented-line section headers. The layout widens
  on large screens while running text stays at a comfortable measure.

### Fixed

- **Hosted MCP `tools/list` hung ~91s → "connected · tools fetch failed"
  (#100).** Two layers. (1) The streamable-HTTP transport answered over an SSE
  stream that the Railway proxy buffered — now `json_response=True` returns an
  immediate `application/json` body (our tools stream nothing). (2) The real
  ~91s: the tools/list role filter ran a *synchronous* `GET /api/session`
  self-call on the event loop, which must be served by the same loop it blocks,
  so it timed out (30s) and retried 3× ≈ 90s. It now resolves the caller's role
  **in-process** (`access_key_identity`, ~0.2s) with a bounded off-loop fallback,
  so the handler never blocks on the self-deadlock.
- **`citadel status` / `activity --global` rendered swallowed timeouts as facts
  (#101).** A slow Node made the CLI print "No seats visible." (with 12 seats)
  and "This token has no seat" for a working token. `fetch_presence` /
  `fetch_events` / `fetch_mesh` now return an error marker on network failure
  (distinct from a genuinely-empty result), the seatless hint is gated on the
  auth check succeeding, and renderers say "Couldn't reach the Node".
- **`citadel search` aborted just before results and `--check-search` never
  passed (#102).** The client search budget (20s) equalled the server's own
  soft-timeout budget, so the client killed a normal 13–20s search at the exact
  moment the server would have returned; the 3s smoke budget sat below real
  latency. Client budgets raised to 35s / 15s so the client prefers the server's
  recoverable timeout envelope.
- **`--json` emitted nothing on failure (#102).** Network/HTTP errors in
  `citadel search` / `ingest` printed only to stderr; scripted callers now get
  `{ok:false, error, code}` on stdout on every failure path.
- **`--dataset` / `--session` silently ignored without `--local` (#103).** They
  were accepted then dropped on the HTTP path, so a scoped search quietly
  returned everything. They now error with exit 2 unless `--local` is set.
- **`citadel_record_feedback` rejected the documented call.** `qa_id` had no
  default, so it was schema-required and the documented `result_id`-only call
  failed validation before reaching the server (which accepts `result_id`
  alone).

### Security

- **`trust_tier` is now attested-only; body-derived shape moved to
  `content_hint` (ADR-0012).** It was computed by grepping a hit's own body, so
  any ingested text could mint its own authority — a public-repo GitHub issue
  title reached the org digest and flipped it to `canonical`, and
  `canonical_only` (which agents are told to trust) kept it. `trust_tier` now
  reports only what the server attests (`reference-only` / `unattested`);
  `content_hint` (`looks-like-spec`, …) carries the steerable shape and makes no
  authority claim; `canonical_only` filters on shape, not trust.
- **Cross-seat search-telemetry leak closed and guarded.** Telemetry rows were
  tagged with `primary_dataset`, so a seat passing an explicit `dataset` exposed
  its query text, `seat_slug`, and `actor_id` to every other seat (ADR-0009
  violation). Rows now land on the caller's own seat; a regression test proves
  it by reverting the fix.
- **Session-trace attribution hardened.** Dedup no longer strips `reference-only`
  from a volunteered trace (a dead end was coming back to its author as
  `verified` knowledge); `Author-Seat` pinning rewrites every line (a tail chunk
  could attribute a trace to a colleague); and the dataset name no longer
  satisfies `repo=` / `path=` scoping (Central is named after the org).

## [0.4.0] — 2026-07-22

### Added

- **Seat-scoped portal Phase 1.** Members log in with a seat `ctdl_…` token and
  land on **My Node** (Seat home): session chrome shows `seat_slug` + Node label;
  `GET /api/me/summary` drives doc counts, recent Node activity, empty checklist,
  and links to search / graph / activity. Admin nav (Access / Audit / Settings /
  Overview) stays hidden from non-admin; search badges distinguish My Node vs
  Central. Optional portal path documented in teammate rollout. Phase 2
  (analytics table, Access deep-links, graph “you” / hub context) remains.
- **Pixel Bastion brand kit.** Canonical 7×7 mark (`kb/banner.py`) across CLI
  (TTY cascade + idle blink), GitHub README banner (`docs/brand/readme-banner.svg`),
  favicon (`kb/static/favicon.svg`), login/sidebar lockup
  (`kb/static/pixel-bastion.svg` + CSP-safe `.brand-pixel--cN` grid), and self-hosted
  Inter / JetBrains Mono. Dashboard chrome restyled sidebar-first to match the
  Interface design canvas (14px cards, seat footer). Bare `citadel` home shows
  Pixel Bastion only (no legacy ASCII wordmark).
- **README product screenshot.** Dashboard Overview + Knowledge Mesh hero image
  (`docs/brand/readme-dashboard.jpg`) under the intro pitch.
- **Overview / Activity analytics panels.** Volume, ops/type, and outcome charts
  on the Overview and Vault Activity pages (SVG bars; horizontal widths are
  CSP-safe — no inline `style=`). Knowledge Mesh `#graphCanvas` unchanged.

- **Shared Session Traces v1 (ADR-0011).** Explicit in-session share via MCP
  `citadel_share_session` and `POST /api/share-session`: **Compact Session
  Context** (client distill + redaction, server LLM dead-end refinement only when
  tool-error pairs exist) dual-writes to the seat **Node** (light tier) and the
  `session-traces` dataset (shared tier), with deferred + coalesced cognify
  (~5–15 min). Share requires an **Approved Capture Root** (server-side `cwd`
  check). Default **`citadel_search`** includes `session-traces` with split
  results and **`reference-only` trust demotion**; traces never promote to
  **Central** and never feed the daily improve loop.
- **Multi-agent proactive policy on `citadel onboard`.** `install_agent_policies`
  writes the same three-rule policy everywhere teammates work: **`AGENTS.md`**
  (always — Codex, Pi, Cline, Zed, and other AGENTS.md-aware tools), **Cursor**
  `.cursor/rules/citadel-agent-policy.mdc` and **Windsurf**
  `.windsurf/rules/citadel-agent-policy.md` when those tools are detected,
  **`GEMINI.md`** when Gemini CLI is detected, and **Claude Code** via the
  existing **SessionStart** hook (`kb.hooks.sync_start`). Idempotent merge;
  re-run safe.
- **`citadel activity` now appears on the home-screen menu** (bare `citadel`),
  under Knowledge alongside `search` and `ingest`.

### Changed

- **`citadel status` no longer smoke-tests `/search` by default.** The search
  check never gated `healthy`, but it often dominated wall time (Railway/cognee
  ~4–20s, or a full 20s timeout). Use `--check-search` when you want it; smoke
  timeout is 3s (full `citadel search` still uses 20s). `--no-search` remains a
  no-op alias for existing scripts.
- **`SKILL.md` reworked for cold-agent onboarding (validated by a fresh-agent
  audit).** Adds an **Agent Fast Start** runbook (`install → set token →
  status --json verify → search`) and a **How Citadel Works** 30-second model
  (datasets = `seat:<slug>` Node vs `masumi-network` Central, caller-scoped
  search, two-stage write/cognify, activity-vs-mesh, roles). Explicit "never run
  bare `citadel onboard` in an agent/CI session" warning (use
  `--non-interactive --token`); the auth-failure contract agents rely on
  (`auth.ok==false` while `node.ok==true` ⇒ token problem, not install; exit
  codes); the `activity --local/--global/--watch` flags; and the `search --json`
  payload shape. All documented commands verified against the shipped CLI.
- **`SKILL.md` now mandates SEAT-BOUND tokens for teammates.** New admin warning
  (Team Onboarding) plus a fix to "Connecting a New Agent" step 1, which
  previously told admins to hand over a *service-account* token — a seat-less
  token has no default dataset, so the teammate's searches fail with
  `DatasetNotFoundError` and writes route to the shared org dataset. Documents
  the mint (`citadel seat token <slug>` / dashboard *Assign to seat*) and the
  `status --json` signature of a correctly-provisioned token.

### Security

- **Obsidian vaults now enforce ownership (ADR-0009).** `owner_actor_id` was
  recorded at vault registration and read nowhere, so `/api/obsidian/manifest`,
  `/api/obsidian/sync/pull`, `/api/obsidian/sync/push`,
  `/api/obsidian/conflicts/{id}/resolve`, and the Obsidian branch of
  `/api/documents/{id}` were gated only by scope — and both
  `obsidian:sync:pull` and `kb:read` are in the default reader set. Any token
  could therefore read another seat's full note bodies and revision history, or
  push revisions into their vault, given a vault id that `/api/sources`
  discloses. All five now fail closed with **404, never 403**, matching the
  cognee drill-down rule so a scoped caller cannot use the status code as an
  existence oracle. Admin/env callers are unaffected.
- **`GET /api/knowledge/events` is now caller-scoped (ADR-0009).** The handler
  called `require_access` and discarded the identity, returning every seat's
  events — message, dataset, and error operation/reason — to any reader token,
  while its two sibling projections (`/api/mesh`, `/events`) both scoped. This
  was visible in plain `citadel activity` output, which printed other seats'
  ingests under the caller's own token. `Mesh.timeline()` gains an optional
  `visible` predicate, applied before the limit slice so a caller still gets a
  full page of their own events; `latest_event_id` stays global so `--watch`
  resumption cannot loop. Admin/env tokens are unaffected.
- **`POST /feedback` now resolves the caller-supplied dataset and session.**
  The handler passed `body.dataset` and `body.session_id` straight through to
  the durable write, skipping `resolve_write_dataset` / `resolve_session_id` —
  the only write-scoped route that did (`/ingest` and `/api/contribute` both
  gate them). A writer token could therefore write feedback into, and emit mesh
  events attributed to, a dataset outside its allowlist, including another
  seat's node. Feedback text is now also byte-capped like `/ingest`
  (`FeedbackBody.text` carries no `max_length` of its own).

### Fixed

- **CI dependency audit.** GitHub Actions runs `pip-audit` on every PR/push;
  `[tool.uv] override-dependencies` pins transitive packages with known CVEs
  (`pillow`, `pypdf`, `python-multipart`) until upstream (cognee/FastAPI stack)
  catches up; `PYSEC-2026-2447` is ignored where no fix exists yet.
- **`sync_session.py` lint** — ruff clean on the SessionEnd distiller.
- **`--json` error paths are now valid JSON across the read/write CLI.**
  `citadel onboard` (no-token + hook-install), `citadel search`, `citadel ingest`,
  and `citadel capture` previously printed a plain-text line on the no-token
  failure path under `--json`, so an agent piping the output choked. They now
  emit `{"ok": false, "error": ...}`, matching `status`/`promotion`.
- **No-token error no longer nudges toward the interactive wizard.** The message
  led with `run \`citadel onboard\`` (bare = interactive, hangs a headless agent);
  it now reads `citadel onboard --non-interactive --token ctdl_...`.
- **`citadel status --json` surfaces the stale-token drift hint** (env vs shell
  rc) on the JSON surface — as `checks[].data.hint` on the `auth` check and a
  top-level `hint` — so agents (which parse `--json`) get the same actionable
  401 diagnostic the human path already printed.

## [0.3.0] — 2026-07-16

### Added

- **Overview Knowledge Mesh reads as a concept map.** The default overview
  aggregates the raw document/chunk cloud into per-hub counts and renders the
  concept skeleton (dataset/seat hubs + entity types + well-connected entities);
  the force layout fits on settle and spreads legibly instead of collapsing into
  a hairball. Cognee-internal `text_<md5>` document nodes gain a fallback label
  (nearest summary → source basename → NodeSet name), unnamed orphan
  session-cache documents collapse into their NodeSet hub with a count, and the
  summariser's "This chunk is about …" boilerplate is stripped from labels.
- **`citadel activity` — dev-side visibility into the vault.** A new CLI command
  shows your Node's **Vault Activity** (captures, syncs, promotions, searches):
  `--watch` live-tails, `--local` shows offline capture receipts, `--global`
  shows a team **Seat Presence** board (every seat's contribution count —
  presence only, never another seat's Node content), `--json` for agents. The
  fail-silent git-push / SessionEnd hooks now leave a one-line capture receipt in
  `~/.citadel/activity.log` (and stderr when `CITADEL_HOOK_VERBOSE` is set).
- **Document drill-down in the Knowledge Mesh.** Clicking a graph node fetches
  and renders its document text in the inspector; textless `TextDocument` nodes
  are assembled from their linked `DocumentChunk` neighbors.
- **Seat presence + graph legibility.** Every seat renders as a presence hub;
  document nodes are labeled from their first line of text (not `text_<hash>`);
  a color-coded legend filters node kinds (chunks hidden by default); the
  inspector shows kind, seat, and clickable neighbors; edges carry relationship
  tooltips. The canvas opens on the **Knowledge Mesh** (the durable graph)
  rather than the restart-transient **Vault Activity** projection, and the
  header has a **Log out** button.
- **Seat-assignment dropdown on token creation.** The dashboard's "Create access
  token" form gains an *Assign to seat* picker that mints a seat-scoped token
  (scope derives from the seat); "No seat" keeps the service-account path.
- **CI.** GitHub Actions runs `pytest` + `ruff` on every PR and push.

### Changed

- **Agent skills default to the headless CLI.** `SKILL.md` and the vault /
  proactive-ingest skills now teach CLI-first access (`citadel search --json`,
  `citadel ingest`) with the hosted MCP as an optional in-session accelerator —
  and an explicit "if no `citadel_*` tools registered, fall back to the CLI,
  don't retry MCP" rule.
- **BREAKING (ADR-0009 mesh read isolation).** `/api/mesh/graph`, the
  `/api/mesh` + `/events` activity projection, and `/api/documents` drill-down
  are now caller-scoped for non-admin tokens: content is limited to the caller's
  datasets (own seat + Central + non-seat datasets) and foreign-seat drill-down
  returns 404 ("not yours"). Seat presence — the seat roster and per-seat
  document counts — stays universal. Admin/env tokens are unaffected. The hosted
  MCP tools `citadel_get_mesh` / `citadel_get_document` inherit the new scoping,
  so reader/agent tokens that previously received whole-org activity now receive
  only their scope. New `/api/mesh/graph` payload fields: `visible_nodes`,
  per-node `dataset`/`datasets`/`internal_name`/`chunk_count`, `presence`, and
  synthetic `dataset:<name>` hubs (not real graph nodes/edges, not drillable).

### Performance

- Document drill-down reads only the target node + its connections instead of
  the whole graph; `/api/mesh/graph` shaping runs off the event loop and is
  concurrency-capped, the raw graph read and dataset-attribution map are
  TTL-cached with single-flight, and attribution now uses one joined relational
  query. Attribution failures negative-cache briefly and prefer last-known-good
  (stale-while-error) instead of blanking scoped vaults.

### Fixed

- Dashboard graph now surfaces a degraded/empty banner on fallback instead of
  rendering a broken engine as a healthy empty mesh, labels the server node cap,
  and no longer mis-pins an arbitrary node as the org Central hub.
- **Hosted `/mcp` public client targets `$PORT`, not `localhost:8000`.** The
  no-fallback public path (and the `CitadelHttpClient` default) now resolve to
  `_self_base_url()`, so public MCP resource reads reach the in-process API on
  Railway instead of a refused `localhost:8000` connection. (The separate
  event-loop-starvation cause of `tools/list` timeouts under load — issue #50 —
  is not addressed here.)
- Search results no longer advertise `document_drilldown_available` for an id
  that `/api/documents` would 404 for the caller; the hint now reuses the same
  visibility gate as the drill-down endpoint, per caller.

## [0.2.3] — 2026-07-07

### Added

- **Seat-bound `citadel token create`** — `--seat <slug>` mints a token bound
  to an existing seat (it inherits the seat's role and private dataset;
  `--role`/`--kind`/`--expires-at` are standalone-only and rejected alongside
  `--seat`). On a TTY with no `--seat`/`--dataset` and none of the standalone
  flags, an interactive picker offers the active seats or a standalone
  service-account token (`0`, empty to cancel). `citadel seat token <slug>`
  stays as the re-mint shortcut for an existing seat.

### Changed

- **`citadel status` is faster** — the network checks (node, auth, search,
  data plane, recent activity, mesh) run concurrently, so wall time is the
  slowest check instead of the sum of all of them.
- **`citadel status` is clearer** — the verdict line always prints last;
  latencies form an aligned dim column (`4.9s` above a second, yellow when
  slow); the identity line shows where the token writes (`writes: seat:X` /
  `shared org dataset`); **Local setup names the repo it checked**, so a ✗
  from the wrong directory reads as "wrong directory" rather than "broken
  setup" (`citadel doctor` names it too); the knowledge mesh is one compact
  line; network errors are humanized ("cannot resolve host" instead of the
  urllib errno dump); a failing search renders as a yellow `!` (it never
  gates health) instead of a red ✗ contradicting a green verdict; the
  stale-shell 401 hint (`source ~/.zshrc`) now prints on status like it does
  on ingest/search; the banner is skipped when output is piped.
- **`citadel onboard` has less friction** — the token keep-or-replace prompt
  is a plain `Keep it? [Y/n]`; the capture-roots wizard drops the redundant
  "set up now?" gate (its first question was already declinable) and asks for
  tags as a one-line `Tags [personal]:` with the presets explained once up
  front; the local-cognify pipx hint only prints when an OpenRouter key was
  actually entered; the step summary reads `N step(s) wired, M skipped`.
- **`citadel token create --dataset <value>`** — a value that names a seat (or
  uses the `seat:` prefix) is now rejected with a redirect to `--seat`, since a
  bare `default_dataset` pointing at seat-private memory would only mint a
  token the Node 403s. Explicitly empty `--seat ""`/`--dataset ""` (the unset
  shell-variable footgun) are usage errors (exit 2) instead of silently
  minting a standalone token.

### Fixed

- **Onboarding no longer ends green with a dead token** — keeping a token the
  Node rejected (401/403) marks the token step with a yellow `!` and closes
  with a warning pointing at `citadel token set`, instead of an all-green
  "configured" summary whose very next suggestion would fail.

## [0.2.2] — 2026-07-02

### Added

- **`citadel token set [TOKEN]`** — set/rotate the seat token this machine uses
  without re-running onboard: verifies the token against the Node first (a
  rejected token writes **nothing**; `--skip-verify` overrides), then updates
  the shell rc in place and reminds you to `source` it.
- **`citadel update`** (alias `upgrade`) — self-update that answers pipx's
  "already seems to be installed" dead end: pipx installs run
  `pipx upgrade --pip-args=--no-cache-dir`, editable/source checkouts are left
  alone (told to `git pull`), anything else gets printed instructions.

- **Checkbox tool selection on onboard** — the coding-tools step is one
  arrow-key multi-select (↑/↓ · space · enter; numeric fallback off-TTY)
  instead of a Y/n question per tool, with the spinner while the selection is
  wired.
- **Stale-shell auth hint** — when `ingest`/`search`/`capture` get a 401/403
  and the shell rc holds a different token than the env, the error now says
  the actual fix: `source ~/.zshrc`.

### Changed

- **`citadel onboard` token flow** — an already-configured token (env or shell
  rc, detected even in a fresh shell) is shown masked with a keep-or-replace
  prompt instead of being silently reused; verification + the identity panel
  moved to the *front* of the run; a Node-rejected token offers an immediate
  re-paste loop instead of "saved anyway" after all the other prompts.
- **Capture-roots wizard defaults** — the dir you ran `citadel` from (repo
  toplevel on onboard, cwd on setup) is offered as an explicit press-Enter
  yes/no (declinable), and a root like `/masumi` that doesn't exist offers the
  home-relative dir that does (`~/masumi`) instead of recording a dead root.
- **Brand-color hero** — the opening art is now just the CITADEL wordmark in
  brand colors: a Masumi-magenta → cyan gradient on truecolor terminals, bold
  cyan elsewhere, and the "the organization vault" tagline highlighted in
  brand magenta. The compact castle banner (the mark) stays as the in-command
  header and gains an arched gate; the home screen falls back to it on narrow
  terminals and shows the installed version.

## [0.2.1] — 2026-06-29

### Added

- **`citadel ingest` cognifies inline by default** — server-side cognify on the
  ingest path so the note is immediately searchable. `--no-cognify` skips it
  (faster; data appears in search later). Ingest prints the destination + scope
  (private seat vs shared org dataset).
- **`citadel seat token <slug>`** — mint a fresh seat-scoped token for an
  **existing** seat (re-link a lost/rotated token), distinct from `seat create`.
- **Write-scope clarity on every mint** — `seat create`, `seat token`, and
  `token create` each print the token's write-scope; `token create` warns that
  standalone tokens are **not** seat-scoped.
- **`citadel doctor` (+`--fix`)** — diagnoses setup drift (token-in-rc-not-env,
  MCP/capture Node mismatch, missing hooks/`.mcp.json`, Node-rejected token);
  `--fix` repairs the safe ones.
- **`citadel onboard` token verify + identity panel** — verifies the pasted
  token against the Node and shows seat / role / access before wiring anything.

### Changed

- **`citadel ingest` / `citadel search` are HTTP-backed by default** — they
  route to your seat via the token (no `[server]` extra needed). `--local` runs
  the in-process server stack instead (needs `[server]`). Both keep `--json`.
- **Friendlier failure modes** — narrow-terminal truncation, friendly errors for
  bare subcommand groups / typos / missing args, and clean Ctrl-C (exit 130).
- **`install.sh` upgrade path** uses `pipx install --force … --pip-args=--no-cache-dir`
  so upgrades never pull a stale cached wheel.

### Removed

- **TUI removed entirely** — there is no `citadel tui` command and the `[tui]`
  (textual) extra is dropped. Its data moved into `citadel status` as a
  "Knowledge mesh" section (documents / nodes / edges / searches).

### Server

- **`/ingest` gained an inline `cognify` flag** so the Node can cognify on the
  ingest request (backing `citadel ingest`'s default).
- **Evolve auto-sync interval shortened 6h → 1h** (`CITADEL_EVOLVE_INTERVAL_SECONDS=3600`):
  GitHub / Linear / repo sync + cognify now run hourly.

## [0.2.0] — 2026-06-29

### Added

- **Guided first-run onboarding** — bare `citadel` on an interactive TTY
  auto-enters onboarding once, then shows the home screen on later runs. Skip
  with `--no-onboard` / `CITADEL_NO_ONBOARD`; `install.sh` runs onboarding via
  `/dev/tty`.
- **Multi-tool MCP wiring** — `citadel mcp add <tool>` / `citadel mcp list`.
  Auto-writes Cursor, Codex, Gemini, and Windsurf (token stays in the shell rc
  via an env reference) and prints a paste-in snippet for Claude user-scope,
  Cline, and Zed (which store the token in plaintext). Pi has no native MCP
  (info note only).
- **Admin `seat` / `token` commands** (need `CITADEL_ADMIN_KEY`) —
  `citadel seat create "Name" slug` mints a seat + a seat-scoped writer token
  (a teammate ingests only into their `seat:slug`); `citadel seat list`;
  `citadel token create` mints standalone/service-account tokens and
  `citadel token revoke <id>` revokes by id.
- **`citadel onboard --node-url`** — target a custom Node during onboarding. The
  onboard now also installs the Claude `SessionStart` hook alongside the
  existing `SessionEnd` hook, the git pre-push hook, and `.mcp.json`.
- **`citadel --version` / `citadel version`**.

### Changed

- **Stdlib CLI UX overhaul** — shared ✓/✗ glyphs, an animated cyan spinner +
  banner reveal, and hardened argparse error/exit handling across the CLI.

## [0.1.3] — 2026-06-28

### Added

- **`citadel promotion`** — drive seat → Central promotion from the teammate CLI
  (zero-dep, like `citadel capture`): `run` triggers an on-demand promotion pass
  for your seat, `list` shows the pending approval queue, and `approve` / `reject`
  act on a queued item. All support `--json` for agents and CI.

### Server

- **Promotion Agent + Approval queue** now ship in the `[server]` extra: the
  capture → Central promotion engine (GitHub-org / Central reference checks,
  Capture Root Tag gate, secret scan + LLM on every candidate, reject dedupe,
  promotion-metadata tags), seat-scoped `POST /api/promote/run`,
  `GET /api/promotion/pending` + approve/reject, the dashboard Promotion Queue
  panel, and MCP tools `citadel_promotion_pending` / `_approve` / `_reject`.

## [0.1.2] — 2026-06-27

### Changed

- **Friendly unknown-command error** — a mistyped command (e.g. `citadel stauts`)
  now shows `✗ unknown command` + a fuzzy "did you mean? `citadel status`"
  suggestion, instead of the raw argparse usage dump.

## [0.1.1] — 2026-06-27

### Added

- **Branded home screen** — bare `citadel` now shows the large castle hero
  (figlet `CITADEL`) plus a curated, colorized command menu, replacing the raw
  argparse usage dump.
- **`install.sh` bootstrap** — `curl … | sh` entry point that detects Python
  3.10+, **asks before installing it** if missing (brew/apt/dnf/pacman), then
  installs pipx + the CLI.

## [0.1.0] — 2026-06-27

First published release. Ships the lightweight teammate CLI alongside the
self-hosted Organization Vault server.

### Added

- **`citadel onboard`** — one-command, idempotent teammate setup: writes the
  seat token to your shell rc (masked, env-only), installs the git pre-push and
  Claude Code `SessionEnd` autosync hooks, adds the Citadel MCP server to
  `.mcp.json`, and offers Approved Capture Roots. Self-contained — no vendored
  skill directory required.
- **`citadel status`** — connection + identity + local-setup health check
  (Node `/healthz`, `/api/session` whoami, search smoke, hooks/MCP/capture
  roots). `--json` for AI agents; exits non-zero when not connected.
- **`citadel tui`** — live terminal dashboard (optional `[tui]` extra).
- **`citadel setup` / `citadel capture`** — declare Approved Capture Roots
  (`~/.citadel/capture.json`) with Capture Root Tags (`personal` / `org-work`),
  and POST per-root summaries to your Node.
- **Bundled autosync hooks** (`kb.hooks.sync_push`, `kb.hooks.sync_session`) —
  stdlib-only, fail-silent, HTTPS-only, personal-by-default; installed by
  `citadel onboard` and runnable as `python -m kb.hooks.*`.
- Server **Capture Policy** baseline API + admin UI; seat **Node Write Policy**
  enforced on all HTTP + MCP write paths.

### Packaging

- Distribution renamed to **`citadel-archive`** (the installed command stays
  `citadel`). Base install is a lightweight client (`python-dotenv` only); the
  server stack is the **`[server]`** extra and the dashboard the **`[tui]`**
  extra. Importing the client never pulls the server stack (guarded by test).
- PyPI **Trusted Publishing** workflow (`.github/workflows/publish.yml`) — tag
  `v*` to build + publish, no stored tokens. See `PUBLISHING.md`.

### Security

- `post_capture` / hooks enforce HTTPS-only and refuse redirects (the seat
  Bearer token is never re-sent to another host); payloads are size-capped.
- The seat token lives in exactly one place (the shell rc); `.mcp.json`
  references it as `${CITADEL_MCP_ACCESS_TOKEN}` and it is never echoed.
- The pre-push allowlist fails **closed** on a corrupt config.

[0.4.0]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.4.0
[0.3.0]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.3.0
[0.2.1]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.2.1
[0.2.0]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.2.0
[0.1.3]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.1.3
[0.1.2]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.1.2
[0.1.1]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.1.1
[0.1.0]: https://github.com/masumi-network/Citadel-Archive/releases/tag/v0.1.0
