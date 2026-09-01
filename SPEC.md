# SPEC

## §G GOAL
Org knowledge vault: capture → SQLite lifecycle → project to relational+vector+graph → receipt-gated search for humans & agents. Central shared, seat Nodes private, canary-only prod writes.

## §C CONSTRAINTS
- Python 3.12, FastAPI, SQLite (lifecycle authoritative), Qdrant 1.19 vectors, Ladybug/Kuzu graph, Cognee 1.4.1.
- One graph writer: web process. ⊥ second graph-writing cron/process.
- Promotion: code defaults off & dry-run; secret-scan/reference/relevance/LLM/dry-run/admin gates always. ⊥ canary content → Central.
- Prod write tests → `seat:canary` only.
- ⊥ tokens, raw bodies, source text ∈ `.local-review` | durable feedback rows.
- `.local-review/`, `.superpowers/` gitignored; evidence only.
- Stacked PRs: one causal slice, independent pass, declared parent, rollback.
- Commits user-authored & DCO signed (`-s`); agent push/check only on explicit user instruction; user merges; merge commits (⊥ squash) for stacked PRs.

## §I INTERFACES
- api: POST /search → hits + `search_id` + `_lifecycle` receipt meta
- api: POST /feedback (writer, kb:feedback) → FeedbackResult; links search_id/result_id
- api: POST /ingest, /api/contribute → lifecycle accept, projection job
- api: GET /api/session → role, seat_slug, scopes, datasets, dataset_labels
- api: POST /admin/session → env creds | `ctdl_` token → secure cookie
- api: GET /readyz, /healthz, /health/ready, /api/mesh/projection-status, /api/mesh/graph?dataset=
- mcp: /mcp/ streamable HTTP; tools citadel_search, citadel_record_feedback, citadel_contribute, citadel_share_session
- cli: `kb/cli.py` (ingest file-content, capture-only default; reconcile)
- hooks: `kb/hooks/search_inject.py` UserPromptSubmit; `sync_start.py` SessionStart
- run modes: `CITADEL_RUN_MODE` = web | evolve | pipeline | github-sync | backup-mirror | cognify | linear-sync
- env: CITADEL_LIFECYCLE_ENABLED, CITADEL_FEEDBACK_STORE_PATH, CITADEL_EVOLVE_*, CITADEL_PROMOTION_*, CITADEL_STATE_DIRECTORY, CITADEL_MCP_ACCESS_TOKEN, CITADEL_BASE_URL
- file: `feedback.sqlite3`, `lifecycle.sqlite3` @ state root

## §V INVARIANTS
V1: ∀ lifecycle search hit ! bind current receipt (retrieval_binding current & ⊥tombstone).
V2: "in sync" = relational & vector & graph receipts searchable for active revision. ≠ distributed txn.
V3: graph writes ∈ web process only (Kuzu single-writer lock).
V4: capture cycle keyed by `capture_run_id`. ⊥ infer from timestamps | collection counts.
V5: watermark query joins `source_heads` → superseded same-run revision ∉ barrier.
V6: barrier poll = 1 batched SQLite read / ≥0.5s. ⊥ per-ID lifecycle_operation loop.
V7: exact graph job filter owner-token scoped; ordinary resume ⊥ clears foreign filter; clear in `finally`.
V8: feedback event rows bounded & redacted. ⊥ query text, hit summaries, source text, tokens, provider bodies.
V9: event identity = sha256(JSON[kind, typed(search), typed(actor), typed(result), score]); typed = ("raw",v) len≤256 else ("sha256",digest) computed pre-truncation. Retry idempotent; score change → new event.
V10: shared/Central telemetry rows presence-only; `_identity_result_id` ⊥ reach Mesh/SSE/response.
V11: consumer decisions ∈ {no_action, ranking_eval_candidate, projection_repair_candidate}; repair ! missing required receipt on active ⊥tombstone revision (exact lookup, no dataset cap); ⊥ LLM, ranking mutation, memory rewrite, promotion.
V12: feedback & promotion stages run post-first-barrier only; standalone unbarriered → skip fail-closed, exit 0.
V13: promotion pending item preserves `secret_scan` across list/approve/reject; gates (secret-scan, reference, relevance, LLM, dry-run, admin) intact.
V14: durable feedback writes best-effort & detached. ⊥ block search response (30s busy-timeout isolated to worker thread).
V15: graph UI selection reconciled vs rendered nodes after refresh/filter/aggregate; Fit-selection disabled w/o rendered selection; hidden target → clear | "Not shown in this map" + recovery.
V16: preview routes ! role & capability & API scope; dataset allowlist default-deny; ⊥ auth weakening.
V17: search_inject hook: ⊥ LLM; HTTPS only, ⊥ redirects; ∀ failure → exit 0 silent; bounded query/output; secrets redacted.
V18: recovery generation pick: COMPLETED rows counted directly; equal completion timestamps → abort (fail closed).

## §T TASKS
id|status|task|cites
T1|x|recovery + cutover slice (patch 01)|V18
T2|x|task-aware search injection (patch 02)|V17
T3|x|projection watermarks & barriers (patch 03)|V4,V5,V6,V7
T4|x|graph UI interactivity + a11y (patch 04)|V15
T5|x|durable feedback + bounded decisions (patch 05)|V8,V9,V10,V11,V12,V14
T6|x|promotion evidence + second barrier|V12,V13
T7|x|UI token login + seat clarity (2 P2 fixed: /api/session reader gate, Central via dataset_labels)|V16
T8|x|regen `kb/webui` (`npm run build:web`, 26 files)|V16
T9|x|build patches 07 (T6) & 08 (T7+T8) onto stack|§C.stacked
T10|~|user merges per-feature ladder S1-S9 → slice prod checks between merges|§C.stacked
T11|.|prod verification steps 1-10 → local evidence ledger (gitignored, not in clones)|V1,V2
T12|x|docs: architecture, operations, mcp README, CONTEXT, web/README + root README|-
T13|.|issue batch update per audit (12 solved, 3 obsolete; comments prefixed AI-triage)|-
T14|.|graph generation rebuild per local graph-generation plan (gitignored evidence ledger) ?approval|V3,V4
T15|.|classify 7 failed prod graph jobs ? then health wording|V2
T16|.|cleanup /tmp worktrees + Docker cache post-merge ?approval|-
T17|x|docs refresh packaged as patch 09 (`docs/refresh-core-completion`)|T12

## §B BUGS
id|date|cause|fix
B1|2026-08-30|recovery tie-rank fail-open on equal timestamps|V18
B2|2026-08-31|watermark matched every capture-run revision → superseded job stalled barrier|V5
B3|2026-08-31|barrier per-ID poll @50ms → 20k reads/s @1k IDs|V6
B4|2026-08-31|event key truncated pre-hash + unescaped `\|` join → collisions|V9
B5|2026-08-31|score ∉ identity → rating change dropped|V9
B6|2026-08-31|bare digest ↔ raw hex collision (no domain sep)|V9
B7|2026-08-31|`_identity_result_id` leaked into Mesh/SSE payloads|V10
B8|2026-08-31|Fit-selection enabled w/o rendered selection; stale after kind/aggregate filters|V15
B9|2026-08-31|ordinary queue resume cleared barrier's exact graph filter|V7
B10|2026-08-31|unbarriered standalone evolve ran promotion/feedback pre-sync|V12
B11|2026-08-31|scope-only tokens pass preview gate, 403 on /api/session|V16
B12|2026-08-31|seatless default_dataset mislabeled Central in /app search scope|V16
