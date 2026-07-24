# UAT + trust audit — findings, effects, and resolution (2026-07-23)

A user-acceptance pass (five agents acting as real MCP / CLI / skill / ingest /
search users, each finding independently reproduced) and a trust-metadata
forgery audit (47 agents) ran against the hosted node and this checkout. This
records what was found, why it matters, and how each was resolved. Every fix was
verified locally; every claim below is grounded in code or a command that was run.

Findings are tracked as GitHub issues #100–#106. Fixes landed on
`feat/agent-search-hardening`.

## Resolution status

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| — | Trust tier forged from body text | high | **Fixed** — ADR-0012 (attested-only tier + `content_hint`) |
| — | Dedup strips `reference-only` from a shared trace | — | **Fixed** |
| — | `Author-Seat` pinning rewrote only the first line | — | **Fixed** |
| — | Cross-seat search-telemetry leak (prior session) | — | **Test added** (fix re-verified) |
| — | `citadel_record_feedback` `qa_id` wrongly required | — | **Fixed** |
| #103 | `--dataset`/`--session` silently ignored without `--local` | medium | **Fixed** |
| #102 | `--json` emits nothing on failure paths | medium | **Fixed** |
| #102 | Client search budgets below server latency | high | **Fixed** |
| #101 | CLI renders swallowed timeouts as facts | blocker | **Fixed** |
| #100 | Hosted MCP `tools/list` ~91s | blocker | **Fixed + confirmed on prod** (0.2s) |
| #105 | One seat's search wedges the node (event-loop starvation) | blocker | **Diagnosed; fix needs cognee validation** |
| #106 | Exact match buried at #2 by section grouping | high | **Documented; fix is a UX decision** |
| #104 | Ingest stores no provenance | — | **Studied; blocked on a cognee spike** |

## Fixed — root cause and effect

### #103 — `--dataset`/`--session` silently ignored without `--local`
- **Effect:** a user narrowing a search to one dataset got the full unscoped
  result set with no signal the filter did nothing — a silent failure in the
  direction users don't check.
- **Root cause:** the HTTP `_search`/`_ingest` handlers never forwarded
  `args.dataset`/`args.session`; only the `--local` handlers did. argparse
  accepted the flags regardless.
- **Fix:** `_reject_local_only_flags` errors with exit 2 (the CLI's existing
  `✗ … requires --local` style). The `--local` path is untouched.

### #102 — `--json` failure paths + client timeout budgets
- **Effect (a):** under `--json`, a network/HTTP error printed only to stderr and
  left stdout empty, so a scripted/agent caller got 0 bytes to parse.
- **Effect (b):** the client search budget (`_SEARCH_TIMEOUT = 20.0`) equalled the
  server's own soft-timeout budget (`search_timeout_seconds = 20.0`), so the
  client aborted at the exact moment the server was about to return — a normal
  13–20s search was killed just before it produced results, and
  `status --check-search` (3.0s vs 6–12s real latency) failed on every healthy node.
- **Fix:** `_emit_error` emits `{ok:false, error, code}` on every failure path;
  client budgets raised to 35s (search) and 15s (smoke), above the server's 20s
  soft cap, so the client prefers the server's recoverable timeout envelope.

### #101 — CLI renders swallowed timeouts as confident facts
- **Effect:** on a slow node, `activity --global` printed "No seats visible."
  (team has 12 seats) and `status` printed "This token has no seat" for a working
  seat-bound token, sending the user to ask an admin for a token they didn't need.
- **Root cause:** `fetch_presence`/`fetch_events`/`fetch_mesh` returned a bare `{}`
  on *no token*, *request failed*, and *genuinely empty* alike; and the seatless
  hint fired whenever `seat_slug` was absent, which a timed-out `/api/session`
  also produces.
- **Fix:** readers return `{"error": …}` on the network-failure branch only; the
  seatless hint is gated on the auth check succeeding; renderers show "Couldn't
  reach the Node: …". Same swallowed-error class as an earlier missing-token
  misdiagnosis.

### #100 — hosted MCP `tools/list` hung ~91s
- **Effect:** MCP clients reported "connected · tools fetch failed" and no
  `citadel_*` tools reached the agent — the documented primary path was unusable.
- **Not #105:** it reproduces at a fixed ~91s on an idle node, and locally every
  code path is fast (`list_tools()` 0.00s / 22 tools).
- **Root cause (two layers, found in this order):**
  1. The transport answered over an **SSE stream** (`json_response` defaulted to
     `False`), which the Railway proxy buffers. Fixed with `json_response=True`
     (PR #107) — the transport now returns immediate `application/json`.
  2. That deploy did **not** fix the 91s, and the prod logs showed why: the
     tools/list **role filter** ran a *synchronous* `GET /api/session` self-call
     on the event loop. It must be served by the same loop it blocks, so it
     times out (30s) and the HTTP client retries 3× ≈ 90s — and the SSE body
     looked "buffered" because the handler was blocked the whole time.
- **Real fix:** resolve the caller's role **in-process** via
  `access_key_identity` (~0.2s, no loop), with a bounded off-loop
  `wait_for(to_thread(...), 2.5s)` HTTP fallback so the handler can never block
  on the self-deadlock (PR #108). Fails open; server-side 403s remain the real
  enforcement.
- **Shipped + confirmed:** both as minimal hotfixes isolated from this branch —
  PR #107 (`df89899`) and PR #108 (`a305300`), merged to `main`. Verified on the
  live node after deploy `9699cedf`: `initialize` 0.2s, `tools/list` **0.2s**
  (was ~91s), `content-type: application/json`, 13 tools for a writer seat (admin
  tools correctly hidden — the role filter now actually runs).
- **Lesson:** the first diagnosis (SSE buffering) fit the symptom — an SSE body
  silent for 91s — but the cause was the blocked handler, not the proxy. The prod
  deploy logs, not the local trace, revealed it. Recorded in memory.

### Trust-metadata fixes (audit)
See [ADR-0012](adr/0012-attested-trust-vs-content-hint.md). `trust_tier` is now
attested-only (`reference-only` / `unattested`); body-derived shape moved to
`content_hint`; the dedup no longer strips `reference-only`; `Author-Seat` pinning
rewrites every occurrence; and `_hit_blob` no longer folds the dataset name into
repo/path scoping.

## Diagnosed — fix needs an environment we don't have here

### #105 — event-loop starvation
- **Effect:** ~25–35 sequential searches from one seat wedge the whole node;
  `/healthz` and `/readyz` hang 25–40s while `/api/session` stays at 0.3s; hosted
  MCP `tools/list` takes ~91s.
- **Root cause (located):** the HTTP `/search` handler awaits the cognee recall
  **on the event loop** — `search()` (server.py:4978) → `_search_within_budget`
  (server.py:157) → `search_across_datasets` → `citadel.search` (service.py:131) →
  `cognee.recall` (cognee_client.py:401), none offloaded. If cognee does
  synchronous native work (embedded DB reads, or the AUTO_FEEDBACK LLM call) the
  coroutine holds the loop for the whole recall. `/healthz` is a bare
  `return {"ok": True}`, so it can only hang if the loop itself is blocked.
  `_SearchSlot` caps concurrency (429) but not single-request starvation, and
  `asyncio.wait_for` can't fire while the loop is blocked.
- **Fix (not shipped):** offload the recall to a worker thread
  (`asyncio.to_thread`), as the MCP path already does for its outbound call.
  **Blocked:** cognee has loop-binding constraints (cognify must run on the single
  loop; whether reads are thread-safe is unverified) and cognee is not installed
  in this checkout, so the offload can't be validated here. Needs a cognee dev
  instance / staging node before deploy. Diagnosis recorded on #105.

## Documented — fix is a product decision

### #106 — exact unique-token match buried at #2
- **Effect:** an exact unique-token match shows at #2 behind an unrelated Central
  hit in the human view. (A second "returns nothing" variant is a cognee recall
  gap, out of repo scope.)
- **Finding:** the API `results` array is already relevance-ordered — the exact
  match is `results[0]`, and `--json` is correct. Only the CLI human view
  re-groups hits into a **fixed** `central → session_traces → node` section order
  and renumbers, so a Node hit is always numbered after every Central hit.
- **Why not blind-fixed:** reordering the sections by relevance is a UX judgment
  (is "Central first" intentional as org-authoritative?), and the dedicated study
  for this finding failed to produce a blast-radius analysis. This is a maintainer
  decision, not a clear bug fix — deferred rather than guessed.

## Studied — blocked on a spike

### #104 — provenance at ingest
- **Effect:** nothing records where a document came from, so `trust_tier` is
  structurally capped at `unattested` (ADR-0012 follow-up).
- **Studied plan (verified):** staged — add locator params to `Citadel.ingest`,
  store them where they survive into a hit, wire one or two sync writers, and let
  `infer_trust_tier` grant a tier from that record.
- **Blocked at Stage 0:** whether cognee surfaces `external_metadata` on a CHUNKS
  hit is unknown (ADR-0012's judges couldn't confirm; 0/6 live hits carried it),
  and answering it needs a cognee dev instance — not the prod node, and not this
  checkout. The storage choice (external_metadata vs a server-side sidecar keyed
  by `content_sha256`) depends on that outcome.
