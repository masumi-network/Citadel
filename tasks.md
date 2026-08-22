# Citadel Tasks

## Current (2026-08-19, after 0.5.1 PyPI)

PyPI `citadel-archive==0.5.1` is live. Public node:
https://citadel.utxo.ag. Source: https://github.com/masumi-network/Citadel.
Do not retag `v0.5.0` or `v0.5.1`.

Inside bench 0.4.0 vs 0.5.1 is recorded in `docs/performance.md`
(2026-08-17 section). Raw JSON is gitignored under `scripts/bench/runs/`.

### Do now

1. **Write-to-mesh gap visibility for seats.** Ingest writes and graph growth are
   different stages. Keep one action list for operators: capture `projection_job_id`
   from `/ingest`, then poll `/api/operations/{projection_job_id}`, then validate
   `outcome.projection_state` and `/api/mesh` node growth for `seat:<slug>`.
2. **GHCR `:0.5.1`.** [VERIFIED] `docker buildx imagetools inspect
   ghcr.io/masumi-network/citadel:0.5.1` returned not found. Publish run
   `32051600953` skipped OCI stage/attest/smoke/promote on dispatch.
   Promote only after owner yes.
3. **Data-plane #228 and #247.** [VERIFIED] both OPEN this session
   (accepted-but-unindexed documents; tail recall 0 of 11).
4. **Local leftover branch.** [VERIFIED] worktree is
   `fix/publish-main-fetch-depth`. `origin/main` already has #304 and #305.
   Checkout `main` when done with that branch.
5. **Outside bench (not started).** OmniMemEval can run Mem0 / Zep / Letta /
   Cognee on LongMemEval and LoCoMo. Citadel needs a custom `add`/`search`
   adapter. Pick a rival before spending an afternoon. Elasticsearch belongs
   on BEIR, not those chat-memory sets. The 105-question `citadel bench run`
   against our own corpus is still the missing inside quality number.

### Done this cut (do not re-open as next-up)

- PR #301 MERGED (`caf8eb9`). TUI + URL sweep is in that merge.
- Tag `v0.5.1` at `caf8eb9`. GitHub Release
  https://github.com/masumi-network/Citadel/releases/tag/v0.5.1
- PyPI `citadel-archive` 0.5.1. [REPORTED] user ran `pipx upgrade
  citadel-archive` (0.4.0 → 0.5.1). [VERIFIED] pipx venv version is 0.5.1.
- PR #304 and #305 MERGED on `origin/main`.

The 2026-07-29 queue below is historical. Several items there already
shipped. Do not treat it as the live sprint.

## Next up (as of 2026-07-29 end of day)

Ordered by what actually costs the org something today. Every root cause below was
measured, not inferred; the issue carries the evidence.

### Do first

1. **`AUTO_FEEDBACK=false`** (#50) — environment only, no code, reversible by
   unsetting. cognee defaults `auto_feedback = True`, adding one structured-output
   LLM call per `cognee.search`, and a seat query fans out to three datasets, so
   **three LLM calls per user query**, each shipping ~2,263 fixed tokens. This is
   the 6-9s. Proven by running the real cognee code, not by reading it.
2. **#153 evolve scheduler resets its interval on every deploy.** The loop sleeps a
   full hour *before* its first pass, so any deploy inside that hour restarts the
   clock. Exactly one cycle ran in the fourteen hours of 2026-07-29. Everything else
   we fix gets deployed and then does not run. Nothing reports the staleness, so a
   node that has not evolved in a week looks identical to one that just did.
3. **#148 corrupt state read as "first run".** Four `_load_state` sites flatten a
   `JSONDecodeError` to an empty state, and every `_save_state` is a non-atomic
   `write_text`. A restart mid-write makes the next `github_sync` mark every repo
   changed, compose a full "everything changed" digest, **ingest it into the vault
   and post it to Chat**, then overwrite the evidence. All four already guard
   first-run with `state_path.exists()`, so raising is safe. Copy the atomic
   temp-file-and-rename from `kb/access.py:904-910`.

### Then

4. **#147 six of eleven seats have no cognee Dataset row.** `create_seat` writes the
   `seat:<slug>` string to JSON and nothing ever creates the dataset; cognee only
   creates it on the write path. Those six have never had a working vault. Fix is
   provision-on-create, backfill the six, and make promotion distinguish "no dataset"
   from "search failed".
5. **#105 loop starvation.** `/healthz` does no work of its own. The measured
   blocker is `AccessStore.authenticate_token` doing a full load plus pretty-printed
   dump plus rename of the ~1 MB access store on **every authenticated request**,
   35 ms, purely to stamp `last_used_at`. Roughly 15 lines. Note `--workers 2` is not
   an option (ADR-0015).
6. **#149** the organization digest LLM call fails 400 every cycle. A 400 is a
   request problem, so retries cannot help.
7. **#151** configure Google Chat, and make "no gateway configured" an explicit skip
   reason rather than an empty success.
8. **#150** align the cognee pin in `pyproject.toml` with `requirements.txt`. Do
   **not** run `cognee-cli stamp base`: the migration warning is a false alarm from a
   28-hour accidental 1.3.0 upgrade, and the database is healthy.

### Open decisions, not code

- **#129** governance PR: two required CI gates pass vacuously (the DCO check exits 0
  when zero commits are enumerated; the linked-issue check matches the PR template's
  own example text), and the branch would revert the `datamodel-code-generator` CVE
  pin. When it lands, re-add the `CI gate` and `DCO sign-off` required contexts only
  after fixing both. A required gate that always passes is worse than no gate.
- **#99** dashboard accent: close it. Main already shipped the same tokens and a
  better theme toggle; merging would add a duplicate `#themeToggle` id and a second
  localStorage key.
- **#46** cannot be verified until #153 is fixed, because the cycle rarely completes.
- Rotate the Postgres password. It appears in plaintext in the alembic boot log and
  in `VECTOR_DB_URL`.

### Deferred deliberately

The silent-empty audit spec
(`docs/superpowers/specs/2026-07-30-exception-flattened-to-empty-audit-design.md`)
scopes one of the four shapes that defect class takes. The other three, empty
aggregates reported as success, verdicts asserted from a proxy signal, and discarded
return values, are real and unswept. That was a deliberate finite-over-systemic
choice, recorded so nobody assumes the sweep was exhaustive.


## "Not just a cognee wrapper" — differentiation roadmap (2026-07-15) — GRILLED → ADR-0010

Source: 6-stream research synthesis (Karpathy "LLM Wiki" gist + 4 repos —
Link, echowiki, llm-wiki, obsidian-wiki-system — + cognee-coupling audit +
differentiation-modules audit). Grilled 2026-07-15 (`/grill-with-docs`) → decision
recorded in [`docs/adr/0010-structured-knowledge-durable-source-of-truth.md`](docs/adr/0010-structured-knowledge-durable-source-of-truth.md);
glossary sharpened in `CONTEXT.md` (Structured Knowledge, Knowledge Conflict,
Tiered Ingestion + a 2026-07-15 Flagged Ambiguity).

**Premise correction:** Citadel is NOT a thin wrapper today — `import cognee`
lives only in `kb/cognee_client.py`, behind a `CogneeGateway` protocol. Real
moat = governance cognee will never have: seat/Node read-isolation (ADR-0009,
the 4-pass cross-dataset visibility algo), the multi-gate promotion engine, the
secret-scan gate. **Problem:** that governance is bolted onto a commodity, leaky
retrieval path (`CHUNKS` default; `auto_improve`/`build_global_context_index`
default OFF → the graph is used for the mesh viz, barely for reads), inheriting
cognee's worst failure modes and adding no retrieval quality of its own.

**Thesis (grilled):** own the *representation*, keep the (good) *retrieval*.
Make **Structured Knowledge** the durable source of truth Citadel owns; cognee
becomes a rebuildable **Knowledge Index/Mesh** projection over it. **You stop
being a wrapper when cognee is no longer the *sole owner* of your knowledge —
not when you delete it.** Cognee is kept, coexists, and earns retrieval duty by
measurement.

**Grilled decisions (see ADR-0010):** no new "note" term — the artifact IS
**Structured Knowledge**, finally made durable/first-class. Source of truth on
live `/data`, synced to the **Vault Backup Mirror**. Synthesized at the full
(**Central**/promotion) tier only — **Nodes stay light** (Tiered Ingestion
unchanged). Canonical per-topic, **update-in-place**, **contradiction-gated**
(a contradicting revision raises a **Knowledge Conflict**, never a silent
overwrite). Page identity resolved by the **Promotion Agent** plan-then-write
against Central page briefs; **vault lint** is the safety net for bad
merges/dups. Dependency shifts: **P1-2 is a prerequisite of P0-1**; **P0-4 is a
companion of P0-1**.

### P0 — cheap, high-impact, cuts cognee exposure
- [ ] **P0-1 · Durable, first-class Structured Knowledge** *(the spine — was
      "canonical notes")* — the **Learning Process** writes **Structured
      Knowledge** as a durable artifact Citadel owns on live `/data` (synced to
      the **Vault Backup Mirror**), not only into cognee. Synthesized at
      **Central**/**Promotion** time (Nodes stay light); canonical per-topic;
      **update-in-place**; page identity resolved plan-then-write against Central
      briefs. **Gated by P1-2 (contradiction) + P0-4 (lint).** `kb/promotion.py`,
      `kb/service.py`, new `kb/structured_knowledge.py`.
- [ ] **P0-2 · Retrieval eval harness (`citadel bench`)** *(do FIRST — baselines
      cognee, unlocks the coexistence bake-off)* — 30–50 frozen org Q→expected
      fixtures + negatives, top-1/top-5 in CI. Fills the biggest gap: no
      retrieval eval today (feedback counted, never used). `kb/retrieval_eval.py`.
- [ ] **P0-3 · Harden `CogneeGateway` → `RetrievalBackend` interface + cognee
      rebuildable from Structured Knowledge** — push the last cognee leaks
      (`SearchType`, session recall, per-read history writes) behind the protocol
      so the engine is swappable *in principle* and `citadel reindex` is a safe
      rebuild. **Cognee stays the default + keeps the Mesh; a 2nd backend
      (BM25/SQLite over SK) is OPTIONAL — built only if P0-2 shows cognee
      underperforming.** `kb/cognee_client.py`, `kb/service.py`, new `kb/retrieval/`.
- [ ] **P0-4 · Vault lint (`citadel lint`)** *(companion of P0-1)* — orphans,
      dangling refs, near-dup pages (cosine bands), stale claims, "term
      everywhere but no page" → catches bad LLM identity-resolution from P0-1.
      Read-only; feeds promotion dedup. `kb/lint.py`. (All 4 repos + Karpathy.)
- [ ] **P0-5 · Profile + fix search latency (#50)** *(added 2026-07-16 — the plan
      does NOT otherwise fix the 6–9s search directly)* — instrument the read path
      (Q&A cache, remote-embedding round-trips, per-dataset recall), fix the worst
      offender. Independent of the SK work; measured against P0-2. `kb/cognee_client.py:435-483`.

### P1 — differentiation depth
- [ ] **P1-1 · Deterministic link/citation grounding ("no orphan claims")** —
      two link kinds per **Structured Knowledge** page: *cross-references* to other
      pages (grounded against the page set) and *source links* (grounded against a
      **Source Snapshot** pointer). Strip/downgrade anything unresolvable; attach
      `[confidence]` + match-type. Hard anti-hallucination guarantee; encodes
      fail-closed/no-oracle. (echowiki, Link, obsidian.)
- [ ] **P1-2 · Claim-level contradiction ledger** *(PREREQUISITE of P0-1 —
      update-in-place is unsafe without it)* — finish `kb/conflicts.py` (today
      title-overlap-only vs 2 JSON files): embedding-candidate + cheap LLM "do
      these disagree?", record both sides as a **Knowledge Conflict**, never
      overwrite; gates the in-place revision at promotion.
- [ ] **P1-3 · Knowledge Maturity signal** *(reframed 2026-07-15 — NOT a promotion
      gate)* — `seed→growing→stable` as a corroboration/trust attribute on **Central**
      **Structured Knowledge** pages (seed = 1 source or open conflict; stable =
      multiple corroborating sources, no open conflict). Composed with P1-1
      (confidence labels) + P1-2 (open conflict pins below `stable`); Promotion keeps
      its own gates. (obsidian, reframed.)
- [ ] **P1-4 · Legible history of the Structured Knowledge store** *(reframed
      2026-07-15 — no separate `log.md`)* — since SK is git-backed via the **Vault
      Backup Mirror** sync, its version history IS the append-only evolution log.
      Just make it legible: structured, source-linked change notes per synthesis
      (`synthesize <page> from <source>`, `conflict raised on <page>`) so `git log`
      over the SK store reads as the narrative. (Karpathy/llm-wiki/Link, reframed.)
- [ ] **P1-5 · Graph-aware retrieval, gated by P0-2** — only after the bench:
      try `GRAPH_COMPLETION`/`AUTO` + `build_global_context_index`, keep only if it
      beats the benchmark. `kb/config.py`, `kb/cognee_client.py`. (ADR-0005 §5, still off.)

### P2 — big bets
- [ ] **P2-1 · Canonical notes become source of truth; cognee fully rebuildable/swappable.**
- [ ] **P2-2 · Agent-facing memory contracts** — typed "what does the org know
      about X" (confidence + citations + conflict flags) as an MCP contract distinct from raw search.
- [ ] **P2-3 · Query-results-as-knowledge loop in evolve** — high-feedback answers
      synthesize back into canonical notes. `scripts/run_self_improve.py`, `kb/self_improve.py`.
- [ ] **P2-4 · Full-form Source Snapshot (retained evidence)** *(future plan —
      grilled 2026-07-15)* — beyond the v1 stable-pointer form, retain the source
      evidence itself so **Structured Knowledge** can be reproduced/reprocessed
      independent of the upstream connector. Fulfils the **Promotion Approval**
      "target: full Source Snapshot back-link". Not a prerequisite of P0-1.

### P1 addition — retrieval mechanics
- [ ] **P1-6 · Chunking lever** *(added 2026-07-16 — the SK plan does not touch
      chunking, which stays cognee's job)* — tune the chunker (size / overlap /
      semantic boundaries) and measure each change against P0-2. Only matters while
      cognee-CHUNKS is the retrieval path; less relevant if BM25-over-SK wins the
      P0-3 bake-off. `kb/llm_enrichment.py` (semantic chunking) + cognee config.

### DevEx track (grilled 2026-07-16 — transparency cluster resolved)
Problem: after `citadel onboard` it's a black box — the dev can't see what
captured, synced, promoted, or what the agent read. The server already produces
**Vault Activity** (+ SSE `/api/mesh/events`), audit events, the promotion queue,
and Node content; the CLI just never surfaces them. So this is "pipe existing
Vault Activity to the dev side + make the silent hooks legible," not new telemetry.

**Transparency (show what's under the hood):**
- [ ] **DX-5 · Capture receipts** — the fail-silent git-push/session hooks
      additionally write a one-line receipt to a local rolling log and print to
      stderr under an opt-in verbose flag (`citadel: captured 3 files → your Node`).
      MUST keep the hook contract: non-blocking, always exit 0, never surface the
      token. `kb/hooks/sync_push.py`, `kb/hooks/sync_session.py`.
- [ ] **DX-6 · `citadel activity [--watch]`** — surface **Vault Activity** in the
      terminal, recent + live-tailed via the existing SSE `/api/mesh/events`:
      captures, syncs, promotions (queued/approved/rejected for the caller's seat),
      searches. Own **Node** shows content. `citadel status` also gains a
      recent-activity block (already fetches `with_recent`).
- [ ] **DX-7 · `citadel activity --global` — live team presence broadcast** —
      org-wide ticker of **Seat Presence** ONLY (counts, timing, seat slug), content
      stripped. Own Node = content; other seats = presence. **Hard isolation line
      (ADR-0009): may say "seat:alice captured 3 items," never the file/title/text.**
      Sources the org **Vault Activity** stream + `mesh_presence_hubs`.

**Tooling + agent DX:**
- [ ] **DX-1 · First-class dev commands** — `citadel bench` (P0-2), `citadel lint`
      (P0-4), `citadel reindex` (P0-3) with `--json`, clear output, CI wiring.
- [ ] **DX-2 · Local dev harness (`citadel dev`)** — formalize the static+API-proxy
      trick used to verify the graph this session (serve `kb/static` + a
      fixture/mock retrieval backend) so frontend + retrieval changes are testable
      locally without prod. Kills the "cache old app.js / must hit prod" friction.
- [ ] **DX-3 · Retrieval explain (`citadel explain <query>`)** — why a result
      ranked (scores, backend, match-type, source pointer); reuses P1-1 labels.
- [ ] **DX-4 · Agent DevEx** — typed memory contracts (P2-2) + confidence/citation
      labels (P1-1) + clearer MCP tool docstrings/errors.

**Do first (grilled order):** P0-2 (bench, baseline cognee) → P1-2 + P0-4 (the
gates) → P0-1 (durable Structured Knowledge) → P0-3 (interface + rebuild-from-SK;
optional bake-off if the bench justifies it). Latency (P0-5) is independent and
can run in parallel. DevEx (DX-*) needs its own grill before building.

**Open questions:** (1) is org scale even large enough to justify cognee vs
synthesized-notes + BM25? (2) synthesis must respect ADR-0009 read scope
(Central notes only from Central + promoted, never cross-Node); (3) LLM cost of
synthesis-on-ingest + contradiction checks vs. cheap add-a-chunk.

## Public site: landing, status, use cases, contact, sign-in (2026-07-28) — BUILT, NOT DEPLOYED

Branch `feat/partners-page`. Five public pages under one nav and one design
system, one subject each, plus the first unauthenticated write path in the
service (decision record: [`docs/adr/0013-public-contact-endpoint.md`](docs/adr/0013-public-contact-endpoint.md)).

- [x] `/` landing page: TL;DR, the Node/Central boundary, capture-search-promote,
      get-started. Metrics and updates deliberately stay on `/info`
- [x] Dashboard moved `/` → `/app`; anonymous callers redirect to `/login`
- [x] `/login` rebuilt on `info.css` (was the dashboard theme)
- [x] Contact form → `POST /contact` → Google Chat, own thread, honeypot,
      per-IP + global rate limits, formatting scrubbed, fail-closed 503
- [x] One canonical Pixel Bastion: web mark had drifted from `kb/banner.py`;
      now generated into favicon, both brand SVGs, README banner, sidebar, CLI,
      pinned by a test so it cannot drift again
- [x] Square corners product-wide (radius tokens → 0); documented deviation
      from DESIGN.md xl=14
- [x] Dashboard accent `#FA008C` → Iris `#FF51FF`; one accent, one favicon
- [x] Light by default on public pages; dark is an explicit remembered toggle

**Second pass — one subject per page (same day):**

- [x] `/info` lost "The model" and "Join in": both were fuller on `/`. The
      architecture lane diagram and the access/data-model deep dive moved to `/`;
      `/info` is now current state, what's live, releases, roadmap
- [x] `/` hero lede rewritten so it stops restating its own TL;DR
- [x] `/partners` → `/use-cases`, leading with four team use cases, then the
      partnering profile unchanged. Old URL 301s rather than 404s
- [x] Contact form moved off the bottom of that page onto its own `/contact`
- [x] Nav is now Home, Status, Use cases, Contact, Sign in across all five pages,
      pinned by a test that walks every page and checks every link + aria-current
- [x] `test_each_public_page_owns_its_subject` guards the dedup: the arch diagram
      and install commands on `/` only, the live tiles on `/info` only
- [x] Four duplicated test functions in `tests/test_server.py` removed (a stash
      merge had left two copies; pytest only ever ran the stale second one)

**Third pass — home page redesign, app shell, and the stack decision (2026-07-29):**

Design sessions produced two specs, both stack-independent and still valid:
[`docs/superpowers/specs/2026-07-29-home-page-design.md`](docs/superpowers/specs/2026-07-29-home-page-design.md)
and [`2026-07-29-app-ui-design.md`](docs/superpowers/specs/2026-07-29-app-ui-design.md).

- [x] `/` rebuilt: dark-free light hero with a drifting Iris glow, rotating
      headline word, sticky section index, proof tiles, hairline "how it works"
      rows, two doors, get-started. No terminal in the hero, by decision
- [x] Rotating headline bug: `.roll` both clipped and animated, so the whole
      word list rendered as a stack. Window and track are now separate elements
      and the keyframe offsets are in `em`, not percentages, which silently
      drifted whenever a word was added. Pinned by a structural test
- [x] Pipeline diagram simplified from fourteen elements to four steps
      (`.spine`: capture → Node → promotion → Central), stacking on narrow
      screens. The guarantee sentence stays in every form of it
- [x] Interactive React Flow version of the same diagram, lazily fetched only
      when it scrolls into view; the four-step markup is the served default and
      the no-JS fallback. Bundle is committed (~330 KB raw, ~109 KB gzipped),
      built by esbuild on a developer machine, since there is no Node in CI
- [x] Per-route CSP: `style-src 'self' 'unsafe-inline'` on `/` only, because
      React Flow writes inline transforms. Exact-path opt-in, strict everywhere
      else, two tests pinning that `/app` and `/login` cannot inherit it
- [x] Removed a false claim before it shipped: "no data leaves your node" is not
      true, `kb/llm_enrichment.py` POSTs content to OpenRouter. Seat-isolation
      claims are fine; data-residency claims are not
- [x] Vendor name out of every user-facing surface, including the public
      OpenAPI `description` served at `/openapi.json`
- [x] App shell: `styles.css` restructured light-default with a dark override,
      converged onto the public token set, sharing the `citadel-info-theme` key
      so a theme picked on the landing page follows you into the dashboard
- [x] App nav collapsed seven entries to four (Home, Search, Review, Admin).
      Knowledge, Activity and Write left routable but out of the nav
- [x] **Stack decided: Next.js + TypeScript + Tailwind, statically exported**
      into `kb/static/` and served by FastAPI, matching the org stack in
      `masumi-network/sokosumi-landing` while keeping `pip install` self-hosting
      working with no Node at runtime. See
      [`docs/adr/0014-nextjs-frontend-static-export.md`](docs/adr/0014-nextjs-frontend-static-export.md)

**Next up (not started):**

- [ ] Scaffold the Next.js app in `web/`, port `info.css`'s light and dark token
      sets into the Tailwind theme
- [ ] Port the public pages, with React Flow as a native component rather than a
      bolt-on bundle
- [ ] Port the dashboard views (Home, Search, Review, Admin, Explore) and delete
      `kb/static/app.js`, 4,247 lines. Explore keeps `force-graph` on canvas
- [ ] Decide whether the app depends on `@summation/shared` for chrome

**Todos / carry-over:**

- [ ] Fill the `/contact` placeholders (NAME, EMAIL, REGISTERED ADDRESS) —
      `test_contact_page_has_no_unfilled_placeholders` fails until then, by design
- [x] ~~Confirm `CITADEL_GOOGLE_CHAT_*` is live on the Railway node, or the form
      renders and 503s on submit~~ — **resolved 2026-07-31, no longer a risk.**
      ADR-0013's store-then-relay amendment means `/contact` persists the
      enquiry BEFORE attempting any Chat relay and returns 200 with
      `stored:true` even when `CITADEL_GOOGLE_CHAT_*` is unset; it 503s only if
      the store AND the delivery both fail. Reads stay admin-only.
      **Google Chat is dropped (2026-07-31)**, so this never needs configuring:
      a connector will be chosen later and implements the existing
      `NotificationGateway` Protocol. `/contact` keeps working without one,
      because storing the enquiry is the part that matters.
- [ ] Nothing visually verified in a browser this session; click through all
      five pages in light and dark before merging
- [ ] `/info` and `/use-cases` prose still uses em dashes; sweep is pending a
      decision, since it means rewriting existing copy

## Dashboard graph + mesh read isolation + /mcp fix (2026-07-14) — SHIPPED + DEPLOYED

Merged to `main` (PR #76 dashboard/isolation, PR #77 /mcp) → Railway deploy
`f5bdccca`, clean boot, 707 tests. Full write-up: [`docs/progress.md`](docs/progress.md) ·
decision record: [`docs/adr/0009-mesh-read-isolation-presence-vs-content.md`](docs/adr/0009-mesh-read-isolation-presence-vs-content.md).

- [x] Node document drill-down (assemble textless `TextDocument` from chunks)
- [x] Seat presence hubs + dataset attribution in the Knowledge Mesh
- [x] Graph legibility: human labels, kind-filter legend (chunks off by default),
      neighbor inspector, edge tooltips, Knowledge-Mesh default view, Log out
- [x] Seat-assignment dropdown on token creation (seat-scoped mint)
- [x] **ADR-0009 read isolation**: `/api/mesh/graph`, `/api/mesh`+`/events`
      projection, `/api/documents` scoped per caller; fail-closed; no 404 oracle;
      presence stays universal (slug only). `CONTEXT.md` terms + ADR added.
- [x] Production-readiness audit (measured): 3 blockers + 12 majors fixed
      (targeted doc read, off-loop shaping + dedicated cap, TTL/single-flight
      caches, joined attribution query, projection-leak fix)
- [x] `/mcp` public-client base URL → `$PORT` (PR #77)
- [x] Skills default to headless CLI; MCP demoted to optional accelerator
- [x] CI: pytest + ruff on PR/push

**Todos / carry-over:**

- [ ] `/mcp` loop-starvation half (#50 / in-loop cognify): base-URL fixed, but
      `tools/list` can still time out under load — needs the systemic fix
- [ ] Three UI papercuts deferred (no browser harness this session): projection-
      event click → bogus inspector, spinner-after-fetch-failure, meta rebuild perf
- [ ] Cut `0.3.0` (CHANGELOG `[Unreleased]` carries the BREAKING notes)
- [ ] Verify isolation + MCP tools end-to-end against prod once a valid token exists

## CLI UX sprint (2026-07-02) — SHIPPED as v0.2.2 (PR #72 + tagline follow-up)

Full change list: [`CHANGELOG.md` § 0.2.2](CHANGELOG.md).

- [x] Onboard token flow: keep-or-replace for an existing token (rc wins over a
      stale env), verification + identity panel up front, 401 re-paste loop
- [x] `citadel token set` (verify-first rotation) · `citadel update`/`upgrade`
      (pipx-aware self-update)
- [x] Checkbox multi-select for onboard coding-tools (`kb/prompt.py`; raw-fd
      keys, Esc/CSI-safe, width-clipped repaint, numeric fallback)
- [x] Capture-roots wizard: declinable press-Enter default (repo/cwd),
      `/masumi`→`~/masumi` did-you-mean, post-wizard seed keeps #43 guarantee
- [x] Brand hero: CITADEL wordmark in magenta `#FA008C`→cyan gradient
      (truecolor/256/bold-cyan tiers) + magenta tagline; compact castle stays
      the in-command mark (arched gate)
- [x] Stale-shell 401/403 hint on ingest/search/capture (`source ~/.zshrc`)
- [x] Pre-merge workflow code review: 10 verified findings, all fixed
      (rc parsing via shlex + last-export-wins, EOF-safe prompts, termios
      degradation, repaint clipping). Suite 637 pass.

**Todos / carry-over:**

- [ ] sarthi's seat token rejected by prod (still open 2026-07-14) — confirmed
      cause: seat `sarthi` has **0 active tokens** in the prod access store.
      Mint fresh: `railway run -- citadel seat token sarthi` then
      `citadel token set <token>` (admin key stays in Railway env)
- [ ] Release gates from the read-side sprint: credential rotation (tracked
      privately) · verify #69 on the node · profile #50
- [ ] Deferred CLI refactors: global `--json`/`--node-url` parent-parser,
      did-you-mean for subparser typos
- [ ] Event-driven sync (GitHub/Linear webhooks → per-event ingest +
      incremental cognify) to replace polling — user-preferred next project

## Read-side hardening sprint (issues #25–#53) — ~SHIPPED (16 closed, 4 pending verify)

Plan + full status: [`docs/read-side-hardening-sprint.md`](docs/read-side-hardening-sprint.md)

First wave (closed): #26, #29, #30, #31, #32, #34, #37, #42, #49 (PRs #54–#62).

Batch 2 + follow-ups (PRs #64–#68), **closed + live-verified on the node:**
- [x] #51, #53 — MCP ingest inline cognify + byte cap (PR #64)
- [x] #45, #33 — MCP 406 `Accept` shim + role/seat `tools/list` filter (PR #64); 406→200 live
- [x] #28 — `get_document` drilldown for cognee chunk hits (PR #64); 200 live
- [x] #39, #48 — promotion read-timeout + **admin-gated** approve/reject (PR #64); reader→403 live
- [x] #40, #41 — durable feedback fallback + improve guards (PR #64)
- [x] #35, #36, #38, #43 — onboarding: capture root, LLM key, user-scope hooks, Claude/pi MCP (PR #64)
- [x] #27 — honest status/doctor/readyz + corpus-gate 503 (PR #64); readyz ok live
- [x] #44 — search timeout budget + parallel per-dataset recall (PR #64); live
- [x] #52 — Linear `[DataItem]` leak gone (PR #64 + #67 + #68); search returns 0 `[DataItem]` live
- [x] #15 — purge legacy garbage: **DONE + verified clean** (214 nodes/chunks purged across
      session cache + Kuzu graph + pgvector; PRs #64/#67/#68 + admin cleanup loop)

- [x] #47 — Kuzu single-writer lock (PR #65: subprocess add-only + web lock-guarded). **NODE-VERIFIED
      + CLOSED**: post-deploy hourly evolve pass ran clean (stages exit=0, zero `Lock is held by PID`,
      green verify canary).
- [x] #25 — umbrella diagnostic CLOSED: version skew + [DataItem] + health gates + ingest→index all
      resolved & verified.

Still open (root-caused; need node-testable fixes, not blind deploys):
- [ ] #69 (NEW) — evolve subprocess runs each stage in its own `asyncio.run()` → cognee loop-binding
      breaks `github_sync` + `linear_sync` every pass ("got Future attached to a different loop").
      The recurring GitHub/Linear sync isn't actually running. Fix: run stages in one event loop.
- [ ] #46 — Linear seat mirrors: auto-map deployed (PR #66) but blocked by #69 (recurring sync) +
      HTTP resync timeout (#52's 200 per-issue cognifies starve the request).
- [ ] #50 — search latency: backpressure/429 done; raw ~6–9s is cognee's per-search pipeline
      (Q&A caching + possibly remote embedding), needs node profiling.

**Action:** credential rotation — tracked privately in the ops runbook.

## ADR-0007 execution — seat capture, promotion, write policy (~100% — shipped)

**Plan:** [`docs/adr-0007-shipping-plan.md`](docs/adr-0007-shipping-plan.md)  
**ADR:** [`docs/adr/0007-seat-capture-promotion-write-policy.md`](docs/adr/0007-seat-capture-promotion-write-policy.md)

### Checkpoints

- [x] P0 Glossary + ADR-0007 + shipping plan + progress (2026-06-27)
- [x] **P0b Grill refinements** — promotion decision tree locked; CONTEXT + ADR-0007 refinements section (2026-06-27)
- [x] P2 MCP seat write guards + secret scan extensions (2026-06-27, prod via PR #19)
- [x] **P1 Seat write policy on all HTTP paths** (2026-06-27)
- [x] P3 Server capture policy API + admin baseline (2026-06-27)
- [x] P4 `citadel setup` + `citadel capture` + local allowlist (2026-06-27)
- [x] **CLI shipped** — `citadel onboard`/`status`/`tui`, headless `--json`,
  branded home screen; **published to PyPI** as `citadel-archive` v0.1.2 +
  bootstrap installer (beyond ADR-0007 scope; see progress.md) (2026-06-27)
- [x] P5 Promotion Agent (GitHub + Central refs, tags, on demand + evolve hook) — **PR #19 → prod** (2026-06-27)
- [x] P6 Promotion Approval queue (dashboard + MCP, admin delegate + audit) — **PR #19 → prod** (2026-06-27)

### P5 — Promotion Agent ✅ (PR #19, prod 2026-06-27)

- [x] `kb/promotion_refs.py` — GitHub org repo list + Central search reference checks
- [x] Capture Root Tag gate (`personal` never auto-promotes; `org-work` only for capture auto-promote)
- [x] **New Org Project** → `pending_approval` queue instead of auto-promote
- [x] Wired into `PromotionEngine.run` (evolve cron stage in `run_railway.py`)
- [x] **Grill parity:** masumi-org-only auto-promote (no LLM-only path for `no_reference_signal`)
- [x] **Grill parity:** no-repo-hint → Central match only, else skip (no queue)
- [x] **Grill parity:** secret scan + LLM always required on every candidate
- [x] **Grill parity:** promotion metadata on Central writes (traceability v1)
- [x] **Grill parity:** reject dedupe / candidate hash — no re-queue unchanged notes
- [x] Seat-scoped `POST /api/promote/run` (member own seat; admin any)
- [x] `citadel promotion run|list|approve|reject` CLI + `--json` — **on PyPI v0.1.3** (2026-06-29)
- [x] **Production:** `CITADEL_PROMOTION_ENABLED=true` on Railway **Citadel-Archive** ([PR #19](https://github.com/masumi-network/Citadel/pull/19))
- [x] **6h evolve scheduler** (2026-06-29) — NOT a separate Railway service (volume
  isn't shareable; promotion+cognify need the web's `/data` volume). Env-gated
  in-process scheduler in `kb/server.py` lifespan
  (`CITADEL_EVOLVE_SCHEDULER_ENABLED`, `CITADEL_EVOLVE_INTERVAL_SECONDS=21600`).
  Runs heavy stages as a subprocess (frees the Kuzu lock on exit) then cognifies
  **in-loop** on the web's own Citadel (`35e4c64`) — fixes the two cognify bugs
  (asyncio loop binding + Kuzu single-writer lock). Deployed + enabled + verified.
- [x] Production smoke (2026-06-29, via live admin key): admin `GET /api/promote`
  (enabled), `POST /api/promote/run` dry-run for `seat:sarthi` (HTTP 200, engine
  evaluated candidates end-to-end), `GET /api/promotion/pending` (200). The literal
  `citadel promotion run --json` CLI path calls the same endpoint (needs a seat token).

### P6 — Promotion Approval ✅ (PR #19, prod 2026-06-27)

- [x] `GET /api/promotion/pending` + `POST .../approve` + `POST .../reject`
- [x] Dashboard **Promotion Queue** panel + Access-style approve button
- [x] MCP: `citadel_promotion_pending`, `citadel_promotion_approve`, `citadel_promotion_reject`
- [x] Admin delegate audit on approve/reject
- [x] **Grill parity:** agent-proposes / member-responds (no manual queue add)
- [x] `citadel promotion approve|reject` CLI + `--json`
- [x] Promotion queue API verified in prod (pending/approve/reject deployed + 200). Browser approve/reject **click-through** is data-blocked: the queue is empty (no "New Org Project" candidate queued yet — promotion correctly skipped sarthi's content), so there's nothing to click-approve. Verifiable once a real candidate queues.
- [x] **Published PyPI v0.1.3** (2026-06-29) — tag `v0.1.3` → trusted-publish; `pipx install citadel-archive` includes `citadel promotion` (install-verified)

### P1 — Seat write policy ✅ (2026-06-27)

- [x] `guard_seat_write_policy` + `resolve_write_targets` seat branch
- [x] Seat org/Central/promotion tags → 403 on ingest
- [x] Seat `/api/contribute` → 403
- [x] Obsidian org tags stripped; push stays on **Node**
- [x] Admin / non-seat Central path unchanged; **385 tests** passing

### P3 — Capture policy ✅ (2026-06-27)

- [x] `GET/PUT /api/access/seats/{slug}/capture-policy` + org baseline endpoint
- [x] `merged_deny_globs` merges env excludes + org defaults + seat baseline
- [x] Settings + Access UI snippets for admin view/edit

### P4 — Capture CLI ✅ (2026-06-27)

- [x] 4.1 `citadel setup` wizard → `~/.citadel/capture.json` (roots + Capture Root Tags) (2026-06-27)
- [x] 4.2 `citadel capture` — summarize approved roots, POST to **Node** (2026-06-27)
- [x] 4.3 Git pre-push hook gates on local allowlist (skip + warn outside roots) (2026-06-27)
- [x] 4.4 Docs: teammate-rollout step 5 + proactive-ingest skill (2026-06-27)
- [x] Unit + API tests

---

## Phase 2 execution — sequential (~100% — shipped; per-dev rollout is operational)

**Plan:** [`docs/phase-2-shipping-plan.md`](docs/phase-2-shipping-plan.md)

M0–M5 shipped on `main` (`5f6c0ed`+). Graph Phase 2 uses a **unified org view**
(seat **Nodes** + **Central** together — no scope toggles). M6 done: graph
repopulated (280 nodes), Linear key set + synced (200 issues) + recurring via the
evolve scheduler. Only operational remainder: each dev runs `citadel onboard`.

### Checkpoints

- [x] M0 Graph Phase 1 merged (PR #5)
- [x] M1 Git push sync (`sync_push.py`, 7 tests)
- [x] M2 Session + IDE docs, `install_autosync.sh`
- [x] M3 Linear backend (ADR-0004, `/api/linear-sync`)
- [x] M4 MCP `citadel_linear_my_issues`, `citadel_linear_search`
- [x] M5 Graph unified org view / depth / spokes (scope toggles removed; depth
      0–3 + Central↔seat hub spokes)
- [x] M5.5 Browser QA (2026-06-25): prod healthz/login/asset 200, login renders
      desktop+mobile, graph toolbar (mode/depth/fit/pause) verified in code

### M6 — Production rollout

- [x] Phase 2 merged to `main` (`5f6c0ed`+)
- [x] Cognify **Central** unblocked (`LLM_MODEL` fix 2026-06-24; optional
      `POST /api/cognify/run?force=true` for stale graph store)
- [x] **Full graph repopulation** (2026-06-29) — rebuilt to **280 nodes / 514
      edges** (was ~25; past the ~214 target). Took fixing two evolve-cognify bugs:
      (1) `Future attached to a different loop` (cognee caches an async engine on
      the first `asyncio.run` loop) and (2) Kuzu single-writer lock contention
      (the evolve subprocess held the graph lock). Fix (`35e4c64`): scheduler runs
      heavy stages as a subprocess (cognify disabled → exits, frees the lock) then
      cognifies **in-loop** on the web's own Citadel. (Optional cleanup: remove the
      `COGNIFY_TEST_MARKER` node.)
- [x] M6.5 teammate one-pager: [`docs/onboarding/teammate-rollout.md`](docs/onboarding/teammate-rollout.md)
- [x] Set read-only `CITADEL_LINEAR_API_KEY` on Railway web (2026-06-29)
- [x] **Recurring Linear sync via the evolve scheduler** (2026-06-29) — added
      `_linear_sync_stage` to the evolve chain (`a77355f`) rather than a separate
      Railway service (which would hit the same single-writer-Kuzu / per-volume
      issues as the evolve cron). Lands in shared pgvector; the in-loop cognify
      folds it into the graph.
- [x] Verified sync: a forced run ingested **200 issues → Central**; `GET
      /api/linear-sync` → `enabled:true, issue_count:200, last_synced_at` set
      (2026-06-29). `mirror_count:0` until Linear users are mapped to seats.
- [ ] Per-dev rollout via `citadel onboard` (installs token + git-push/SessionEnd
      hooks + MCP + capture roots; replaces the removed `install_autosync.sh`).
      `seat:sarthi` already onboarded; remaining is operational (other devs).

---

## Done (2026-06-25 session — Phase 2 implementation)

- Graph Phase 1 production (PR #5). Git push sync, Linear sync + mirror, unified
  org graph UI, MCP tools, IDE onboarding. Tests 346 passing.

## Done (2026-06-25 session — planning)

- Fixed broken production ingest: the invalid `LLM_MODEL=openrouter/free` (every
  cognify call failed) -> `openrouter/openai/gpt-4o-mini`. Verified end-to-end.
- Upgraded cognee 1.1.2 -> 1.2.1 (PR #2), deployed + verified.
- Added re-cognify / verify recovery tooling: `POST /api/cognify/run`,
  `citadel cognify [--verify]`, `CITADEL_RUN_MODE=cognify` / `cognify-verify` (PR #2).
- Fixed the GitHub-Sync cron 502 (internal domain + `*_TIMEOUT_SECONDS=2400`).
- Shipped per-seat onboarding (PR #3): connect wizard, self-describing seat
  (`resolved_memory_scope` + tool docstrings), admin `GET /api/access/seats`.
- Shipped autonomous personal-KB sync (PR #4): `citadel-proactive-ingest` skill +
  a project-committed Claude Code `SessionEnd` hook (`sync_session.py`) +
  `docs/onboarding/citadel-autosync.md`. Teammates are headless (token + MCP +
  skill, no dashboard login).
- Backprop-fixed the time-dependent `test_github_sync` PR-window test.
- Completed knowledge-graph redesign Phase 1 (`feat/graph-logseq`): vendored 2D
  `force-graph` replacing the Three.js scene, shared Central pinned as the center hub,
  hover/click/labels, Activity + Knowledge graph modes.
- Tests 312 -> 328. See `docs/progress.md` (2026-06-24) for detail.

## Done

- Repo reset from Cognee fork -> clean Citadel wrapper.
- Cognee kept as dep only. No vendored upstream source.
- Python package `citadel`, import package `kb`.
- CLI added: ingest, search, feedback, improve.
- FastAPI service added.
- Railway config added.
- Hosted UI added at `/`.
- Live mesh UI added:
  - graph canvas
  - index panels
  - ingest form
  - search form
  - self-upgrade button
  - SSE live events
- Admin key gate added:
  - `/login`
  - `/admin/session`
  - `/admin/logout`
  - UI/API/SSE protected
  - `/healthz` public for Railway health
- Railway resources created/wired:
  - app service: `Citadel-Archive`
  - Postgres service: `Postgres`
  - app volume: `/data`
  - Postgres refs wired into app vars
  - Kuzu graph path -> `/data/.cognee_system`
  - data path -> `/data/.data_storage`
- Runtime deps made explicit via `requirements.txt`.
- Tests passing locally: `18 passed`.
- GitHub organization sync added:
  - fetches `masumi-network` repos and public org events
  - creates a daily digest
  - ingests digest into Citadel
  - runs improvement for `masumi-github-daily`
  - persists scan state at `/data/.citadel/github_sync_state.json`
  - admin API added: `/api/github-sync`, `/api/github-sync/run`
- UI pass added:
  - GitHub sync status/manual run panel
  - richer runtime stats
  - better loading/empty/error states
  - improved mobile layout and focus/interaction states
- Feedback UI added:
  - manual QA ID feedback form
  - score selection and optional note/dataset/session metadata
  - search-result helper button when a QA ID is present
  - mesh feedback counter/status updates
- OS dashboard redesign added:
  - top system bar and persistent status chrome
  - separate pages for overview, search, ingest, feedback, sources, events, and access
  - left workspace navigation rail
  - central mesh window and runtime metrics strip
  - responsive mobile/tablet layout smoke-checked with browser automation
- Role-based access keys added:
  - reader keys can view/search only
  - writer keys can ingest and record feedback
  - admin key can run GitHub sync, self-upgrade, and view access setup
  - `/api/session` exposes current role/capabilities to the UI
- Agent access research captured:
  - docs note: `docs/agent-access-model.md`
  - decision: build one secure Citadel MCP server as the shared capability layer
  - wrap the MCP server with thin Claude/Codex skills or plugins for workflows
  - keep Search and Ingest as separate read/write surfaces
- Persistent access-token foundation added:
  - JSON-backed access store at `CITADEL_ACCESS_STORE_PATH`
  - `User`/`ServiceAccount`-style principals
  - hashed API tokens with prefix, role, scopes, expiry, last-used timestamp,
    and revoked state
  - admin APIs for access snapshot, token creation, token revocation, and audit
  - Access page token creation/list/revoke/audit UI
  - tests passing locally: `20 passed`
- Production health verified on 2026-05-21:
  - `Citadel-Archive`, `Citadel-GitHub-Sync`, and `Postgres` all `SUCCESS`
  - `/healthz` returns `{"ok":true,"service":"citadel"}`
  - `/` redirects to `/login`
- Railway cron service created:
  - service: `Citadel-GitHub-Sync`
  - schedule: `0 3 * * *`
  - volume: `/data`
- Source learning-agent foundation added:
  - wraps GitHub source sync as `kb.learning_agent`
  - captures recent commit summaries for changed repositories
  - API added: `/api/learning-agent`, `/api/learning-agent/run`
  - CLI added: `citadel learn`
  - Railway run mode supports `CITADEL_RUN_MODE=learning-agent`
- MCP support added:
  - stdio server: `uv run python -m kb.mcp_server`
  - tools for search, mesh, sources, ingest, feedback, learning-agent run, and improve
  - resources for session, sources, indexes, and recent events
  - prompts for answer-from-KB, ingest decision, and source-change summaries
  - HTTP bearer tokens reuse Citadel reader/writer/admin access roles
  - project `.mcp.json` added with `CITADEL_MCP_ACCESS_TOKEN` env expansion
- Organization Vault dashboard build started:
  - added Knowledge, Agents, Audit, and Settings workspace pages
  - made Search the default reader page when no hash route is selected
  - surfaced repository daily update, source snapshot, index, and runtime event state
  - surfaced service-account tokens, MCP setup snippets, and role/tool matrix
  - surfaced access audit and runtime activity side by side
  - surfaced readiness and learning-agent runtime checks
  - reduced duplicate dashboard navigation chrome and made mobile content-first
  - rewrote the overview header around current vault state and primary actions
  - tests passing locally: `30 passed`
- Production rollout verified on 2026-06-02:
  - functional rollout commit: `cd33217` (`fix(mcp): default search to company dataset`)
  - Railway web deployment: `891c81ee-4c44-4303-8792-0a282d9d62be` (`SUCCESS`)
  - `/healthz` returns `{"ok":true,"service":"citadel"}`
  - `/skills` returns HTTPS URLs plus content hashes for hosted skills
  - `/.well-known/citadel.json` returns MCP, skill, auth, tool-policy, and
    boundary metadata without vault contents
  - `/search` returns additive `_citadel` provenance and retrieval metadata on
    dict results so agents can cite and decide when drill-down is available
  - Dashboard search cards surface `_citadel` provenance and only show source
    drill-down links when the backend marks them available
  - HTTP responses include baseline browser security headers; HSTS is limited to
    HTTPS/HTTPS-forwarded requests
  - Private/authenticated responses default to `Cache-Control: no-store`; public
    skill/discovery/static metadata is short-cacheable
  - failed deployment `7658403e-d79e-4d89-969b-34bb3aa45374` was caused by
    Railway health checks receiving `404` from `/healthz`; fixed in `68d729e`
  - `CITADEL_MCP_DEFAULT_DATASET=masumi-network` added for company MCP search
  - MCP `citadel_search` returns live results through the reader service account
  - local test suite passing: `53 passed`
- Company reader service-account token created for MCP bootstrap:
  - role: reader
  - scopes: `kb:read`, `kb:search`, `sources:read`, `obsidian:sync:pull`
  - raw token stored only in ignored local `.citadel/company-reader-mcp.env`
- Team-share flow verified on 2026-06-02:
  - share command: `npx skills add masumi-network/citadel`
  - production verification commit: `7a4a1d9`
  - hosted MCP `citadel_session`, `citadel_search`, and `citadel_ingest`
    succeed with a writer token
  - rotate any token that was pasted into chat or logs before team rollout
- MCP audit trail added:
  - forwarded MCP calls are tagged with `X-Citadel-MCP-Tool`
  - persistent audit events use `mcp.<tool_name>` actions
  - events capture actor, role, tool, required scope, dataset when known, and
    success/failure
  - audit details store safe counts and hashes instead of raw tokens, queries,
    note bodies, or feedback text
- Token scopes are enforced server-side:
  - protected API routes use role plus required scope checks
  - bootstrap env keys receive the default scopes for their role
  - service-account tokens can be narrowed to custom scopes
  - custom scopes that exceed the selected role are rejected
- Admin audit dashboard can filter MCP-originated events, non-MCP access/admin
  events, and failures; MCP summary counts are visible in the Audit page.
- Live learning-agent sync run on 2026-06-02:
  - scanned 41 repositories
  - processed 50 organization events and 198 commits
  - ingestion accepted for the `masumi-network` dataset
- Vault Backup Mirror initialized:
  - repo: `masumi-network/Vault-Backup-Mirror`
  - visibility: private
  - branch: `main`
  - scaffold commit: `deeb1c9`
- GitHub sync cron verified on 2026-06-03:
  - service: `Citadel-GitHub-Sync`
  - scheduled run logged at `2026-06-03T03:04:06Z`
  - result: `ingested=true`, `dry_run=false`, `improved=false`
  - next scheduled run: `2026-06-04T03:00:00Z`
- Hosted MCP/security rollout deployed on 2026-06-03:
  - production commit: `3c70e92`
  - `/healthz` returns `200` with private no-store cache policy and security headers
  - `/.well-known/citadel.json` returns public discovery metadata and advertises
    `/mcp/`
  - legacy `/mcp` redirects to `/mcp/` with relative `Location: /mcp/`
  - hosted MCP `initialize` returns `200`
  - hosted MCP `tools/list` returns 13 tools
  - hosted MCP `citadel_session` succeeds and writes an `mcp.citadel_session`
    audit event
  - backup-mirror dry-run returns `ok=true`, `written=false`, `published=false`

## Current Railway State

- Web service `Citadel-Archive` is live and auto-deploys `main`:
  - `https://citadel-archive-production.up.railway.app/healthz` (200)
  - `https://citadel-archive-production.up.railway.app/` and `/mcp/`
- Running **cognee 1.2.2** (bumped from 1.2.1 + deployed + verified 2026-06-29; boots clean).
- **6h in-process evolve scheduler enabled** (`CITADEL_EVOLVE_SCHEDULER_ENABLED=true`,
  `CITADEL_EVOLVE_INTERVAL_SECONDS=21600`): heavy stages as a subprocess → in-loop
  cognify. Runs github sync → repo-content → self-improve → promotion → Linear sync
  → cognify every 6h.
- **Linear sync live:** `CITADEL_LINEAR_API_KEY` set; `/api/linear-sync` `enabled:true`,
  200 issues in Central (synced 2026-06-29).
- `LLM_MODEL=openrouter/openai/gpt-4o-mini` on the web service (was the invalid
  `openrouter/free`, which had silently broken all cognify; fixed 2026-06-24).
  `EMBEDDING_PROVIDER=fastembed`, `VECTOR_DB_PROVIDER=pgvector`,
  `GRAPH_DATABASE_PROVIDER=ladybug`, `CITADEL_SEARCH_DEFAULT_DATASET=masumi-network`.
- **2026-08-19 recovery:** fixed live startup blocker by setting
  `GRAPH_DATABASE_PROVIDER=ladybug` in `Citadel-Archive` environment variables and
  redeploying. New deploy `f115ba73-3a1e-453f-9932-5b3cb42e13bc` is `SUCCESS`;
  `/health/ready` recovers to `200` after startup, and the previous
  `Lite profile requires GRAPH_DATABASE_PROVIDER=ladybug; got 'postgres'` refusal is
  absent from the redeploy logs.
- Cron `Citadel-GitHub-Sync` (schedule `0 3 * * *`): now targets the internal
  domain `http://citadel-archive.railway.internal:8080` with
  `CITADEL_GITHUB_SYNC_TIMEOUT_SECONDS=2400` to avoid the public-proxy 5-min 502 on
  the ~26-min sync (fixed 2026-06-24). Its own stale `LLM_MODEL=openrouter/free` is
  moot in this mode (cognify runs in the web service).
- Postgres healthy; pgvector working (ingest -> cognify -> search verified
  end-to-end 2026-06-24).
- Live deploy is now commit `f115ba73-3a1e-453f-9932-5b3cb42e13bc` (deployment
  status `SUCCESS`).
- **LLM_MODEL fixed (2026-06-26):** was the prefix-less `google/gemini-2.5-flash`
  (broke all cognify with litellm "LLM Provider NOT provided"); now
  `openrouter/deepseek/deepseek-v4-flash`. cognify verified to build a
  214-node / 385-edge graph.
- **cognee partitioning disabled:** `ENABLE_BACKEND_ACCESS_CONTROL=false` so the
  org-wide graph read and cognify share one global Kuzu graph (the prior default
  partitioned the built graph into a per-dataset `.pkl` the read never resolved).
- **Knowledge graph repopulated + verified (2026-06-29):** `/api/mesh/graph`
  returns **200 nodes / 369 edges, `fallback:false`** (200 = the
  `mesh_graph_max_nodes` display cap; the cognify pass built **280 nodes / 514
  edges**). Rebuilt by the evolve scheduler's in-loop cognify after fixing the two
  cognify bugs (asyncio loop binding + Kuzu single-writer lock). `seat:sarthi`
  exists; Linear sync `enabled:true` (200 issues).

## Needed From User

- ~~**Run the Cognee graph recovery**~~ — DONE (2026-06-29). Graph rebuilt to 280
  nodes by the evolve scheduler's in-loop cognify; `/api/mesh/graph` serves it.
- **Operational rollout of autonomous sync** (not code): provision a seat per dev
  via the connect wizard, then each dev runs `citadel onboard` (sets the token in
  their shell rc + installs git-push/SessionEnd hooks + MCP + capture roots —
  replaces the removed `install_autosync.sh`). `seat:sarthi` already onboarded.
  See [`docs/onboarding/teammate-rollout.md`](docs/onboarding/teammate-rollout.md).
- ~~**Linear rollout**~~ — DONE (2026-06-29). Key set, 200 issues synced to Central,
  recurring via the evolve scheduler's `linear_sync` stage. (Optional: map Linear
  users → seats via `CITADEL_LINEAR_USER_MAP` to populate Seat-Scoped Mirrors.)
- **Credential rotation** — tracked privately, not in this public repository.
  Operational credential state does not belong in a public repo; see the private
  ops runbook.
- OpenRouter model/key config (done):
  - `OPENROUTER_API_KEY` set on `Citadel-Archive`; `Citadel-GitHub-Sync` references
    the same key; Citadel maps it to Cognee's `LLM_API_KEY` at runtime.
  - `LLM_PROVIDER=custom`, `LLM_ENDPOINT=https://openrouter.ai/api/v1`,
    `LLM_MODEL=openrouter/openai/gpt-4o-mini` (web service).
- pgvector: working (cognify + search verified end-to-end). The earlier
  `CREATE EXTENSION IF NOT EXISTS vector` step is no longer outstanding.

## Research-Backed Direction

- Citadel should act like a workspace OS for the Organization Vault, not a
  single crowded dashboard page.
- MCP is the main integration surface for Claude Code, Codex, OpenAI
  Responses/Agents workflows, and future autonomous agents.
- Skills/plugins are distribution and workflow wrappers. They should not own
  authorization or duplicate Citadel business logic.
- Team access should move from shared env keys to durable principals:
  - users
  - service accounts
  - teams
  - memberships
  - role-based API tokens for Phase 1
  - scoped API tokens after the initial team workflow is proven
  - audit events
- Human access should use browser sessions. Agent access should use bearer
  tokens first, then OAuth/OIDC for hosted production.
- Sensitive agent tools must require approval:
  - source sync
  - self-improve
  - reindex/delete
  - invite/team changes
  - token creation
- Vault Backup Mirror private repo: `masumi-network/Vault-Backup-Mirror` (created).
  Citadel Archive is public. Large blobs should move to object storage if the
  mirror approaches GitHub repository limits.

## Backlog (after Phase 2 ships)

- **Better admin panel for access / tokens / keys / seats** (user request
  2026-06-26): turn the Access page into a complete admin console — create +
  revoke API tokens and bootstrap keys, full **seat lifecycle** (provision /
  view / revoke / rotate), and clear per-principal management. Basic
  create/list/revoke exists today (`kb/access.py`, `/api/access`,
  `/api/access/tokens`, `/api/access/seats`, Access page); this is the UX +
  completeness pass (rotation, key management, disable-principal). Ties into the
  UI-minimalization track (Access absorbs Agents + Audit into one admin home).
- Create the Railway `backup-mirror` cron service after deciding whether the
  first scheduled runs should stay dry-run or write local manifests.
- Multi-dataset search (own node + Central in one query).
- Tag routing for node vs Central lanes.
- Verify admin key unlocks UI; test hosted feedback with real Cognee QA ID.
- Issue fresh per-teammate/per-agent tokens for rollout.

## Next

ADR-0007 + Phase 2 are shipped. Remaining is operational/optional:
- **Credential rotation** — tracked privately in the ops runbook, not here.
- **Per-dev rollout:** each teammate runs `citadel onboard`. See
  [`docs/onboarding/teammate-rollout.md`](docs/onboarding/teammate-rollout.md).
- **Optional:** `CITADEL_LINEAR_USER_MAP` for Linear→seat mirrors; remove the
  `COGNIFY_TEST_MARKER` node; browser approve/reject once a candidate queues.

## Next: Team Access

- Keep Phase 1 whole-vault access constrained by reader/writer/admin role.
- Later add full team/membership scoping:
  - named teams
  - memberships between users/service accounts and teams
  - dataset-scoped grants
- Add token expiry validation UI and creation controls.
- Add token rotation flow.
- Add disabled principal flow.
- Add admin Access UI:
  - edit teammate/service-account role
  - assign dataset/team scope after scoped access is introduced
  - rotate token
  - disable principal
- Keep existing env role keys as bootstrap/local fallback.

## Next: Agent Integrations

- Add Codex skill or plugin package:
  - `SKILL.md` workflow instructions
  - bundled MCP server config
  - install/setup docs
- Add Claude Code skill:
  - search-before-answer workflow
  - ingest-project-decision workflow
  - source-sync/admin workflow

## Next: Dashboard

- Continue OS-style page deepening:
  - add richer Knowledge document/source drilldowns
  - add drill-down views for MCP audit events and linked source documents
  - add audit export and event detail pages on top of `/api/audit?view=...`
  - add editable Settings controls after backend policy modules exist
- Make Sources/Ingest the default writer workspace.
- Make Home/Access/Agents/Audit the admin workspace.
- Add model/provider state once the server exposes it safely.
- Add Vault Backup Mirror write controls and export history drill-downs.

## Later

- OAuth/OIDC login for hosted team deployments.
- Dataset-level and team-level ACLs.
- Approval queue for high-impact agent actions.
- Rate limiting per user/service account/tool.
- Structured audit export.
- Secret rotation reminders.
- Prompt-injection hardening for retrieved vault content:
  - mark retrieved text as untrusted context
  - keep source citations
  - reject tool instructions found inside retrieved content
- OAuth 2.1 + Protected Resource Metadata for remote hosted MCP.
- Secure MCP tunnel option for private/on-prem deployments.
- Mesh introspection:
  - pull real Cognee graph nodes
  - pull real vector index stats
  - show failed pipeline jobs
  - show memify/self-upgrade history

## Done (2026-06-10 improvement pass)

- Production hardening:
  - structured logging with secret redaction across `kb/` (`CITADEL_LOG_LEVEL`)
  - shared retry helper with jitter + Retry-After (`kb/retry.py`) applied to GitHub sync, backup mirror, Google Chat, digest LLM
  - expired/revoked tokens rejected centrally with `access.token.rejected` audit events
  - tests for security_scan, google_chat, access, mesh, obsidian_sync, learning_agent
- Knowledge Conflicts (per CONTEXT.md):
  - `kb/conflicts.py` store + detection (Obsidian push conflicts, ingest content-hash mismatch)
  - `GET /api/conflicts`, `POST /api/conflicts/{id}/resolve`, mesh `conflict` events
  - Conflicts page in dashboard with resolve flow
- Real Knowledge Mesh:
  - `GET /api/mesh/graph` pulls actual Cognee/Kuzu nodes + edges (Later item "pull real Cognee graph nodes" done)
  - dashboard Activity <-> Knowledge-graph toggle
- Architecture deepening:
  - Learning Process isolated into `kb/learning.py`
  - Repository Daily Update rules isolated into `kb/repository_update.py`
- Obsidian-style dashboard redesign (dark minimal theme, sidebar, design tokens).
- LLM-assisted learning:
  - `kb/llm_enrichment.py` semantic chunking + summaries/tags (default model `deepseek/deepseek-v4-flash`, deterministic fallback, secret-gated)
  - `kb/self_improve.py` bounded self-improvement pass + `POST /api/learning-agent/optimize`
- Cron pipeline mode (`CITADEL_RUN_MODE=pipeline`): github sync -> skills refresh -> self-improve -> backup mirror, per-stage toggles.
- Teammate/agent access:
  - `POST /api/contribute`, `GET /api/knowledge`, MCP `citadel_contribute` tool
  - README "For Teammates & Agents" section
- SKILL.md + skills/ updated for the current API surface.
- Tests: 262 passing.
