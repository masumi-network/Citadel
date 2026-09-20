# SPEC

## §G GOAL
A2 readiness → bounded truthful corpus gate (≤20,000), additive stage health surfaces, no false-green public web state; plus bounded local chunk-boundary proof for #247, with no production repair claim.

## §C CONSTRAINTS
- A2 scope plus one local-only #247 chunk-boundary and repair-test slice; no unrelated route, auth, liveness, deployment, or external-write changes.
- Corpus cap finite positive: default/deployment `20,000`; env override MAY lower it, MUST remain finite positive; reject non-positive, malformed, or unlimited values. [VERIFIED: `CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS` currently env-configurable; user-approved cap decision]
- Strict `/readyz` gate: `process_alive=ready & source_searchable=ready & projection_searchable=ready & mesh_ready=ready & corpus.ok & lifecycle.ok & (canary absent ∨ canary.ok)`; 200 only when full gate passes, else 503 with diagnostic payload. [APPROVED TARGET; current `kb/server.py:5541-5545` still gates only `corpus.ok`, `lifecycle.ok`, and `canary`]
- Preserve `/healthz` liveness. [VERIFIED: `kb/server.py:4866-4868`] `/health/ready` uses combined readiness payload gate. [APPROVED TARGET; current `kb/server.py:5567-5574` still uses weaker `dependency_ok` predicate]
- Preserve public `/api/state` boundary: safe aggregates/stages only; no vault content, tokens, per-seat data, internal URLs, or exception text. [VERIFIED: `kb/server.py:4980-4986`]
- Preserve ADR-0018/0020: authoritative counts; names match counted scope; unknown/missing ≠ `0`; no restart counter as corpus truth. [VERIFIED: `docs/adr/0018*`, `0020*`]
- Preserve module-scope vault-state cache; settled cached `null` MUST NOT retry or become green in same page load. [VERIFIED: `web/src/lib/vault-state.ts:54-67`]
- Local #247 work uses synthetic fixtures only; no live reprocessing, deployment, production benchmark, graph rebuild, or external data write.

## §I INTERFACES
- api: `GET /readyz` (authenticated) → existing fields `{ok, service, tenant_id, default_dataset, auto_improve, build_global_context_index, corpus, lifecycle, canary}` plus additive `stages`.
- api: `/readyz.stages` → `{process_alive, source_searchable, projection_searchable, mesh_ready}`; each value `{status, reason}`, status ∈ `{ready, pending, failed, unavailable}`. `reason` safe diagnostic; unknown count never coerced to `0`.
- api: `GET /api/state` (public) → existing safe snapshot plus same safe `stages` block; `ok`/`healthy` reflect full readiness gate, not lifecycle alone; retain `{service, version, sources, totals, repo, updated_at}` compatibility fields.
- api: `GET /health/ready` → detail-free `{ok, service}`; `ok` equals combined readiness payload `.ok`, status 200 iff true else 503.
- web: `useVaultState()` → settled false = pending; settled true + state `null` = failed cached read; cached null never retried.
- web: `SectionIndex` health pill → pending `Loading`; healthy `Live · <version>`; unhealthy `Degraded · <version>`; failed `Unavailable · reload page`; failed state uses warning styling; null state ! green.
- test: backend covers cap validation/boundary, full gate precedence, additive stages/status/reasons, public redaction, `/api/state` failed readiness, and `/health/ready` combined gate; frontend covers pending/failure labels and null→Live regression.
- test: synthetic long-document fixture → local repair produces bounded chunks and tail marker retrieves from a non-head chunk.

## §V INVARIANTS
V1: ∀ corpus probe → examined documents ≤ configured finite cap ≤ 20,000; cap breach → `probe_cap_exceeded=true`, corpus `ok=false`, readiness ! green.
V2: ∀ cap config → positive finite integer; malformed, ≤0, or unlimited value → explicit configuration failure; no unbounded corpus probe.
V3: `/readyz` 200 ⇔ `process_alive=ready & source_searchable=ready & projection_searchable=ready & mesh_ready=ready & corpus.ok & lifecycle.ok & (canary absent ∨ canary.ok)`; failed required evidence → 503; failed stage reason retained.
V4: `/readyz.stages` always exposes four named `{status, reason}` values; statuses derive from stage-local evidence, never copied from aggregate `ok`, `corpus.ok`, `lifecycle.ok`, or `canary.ok`; classify in precedence order: missing/timed-out/errored required evidence → `unavailable`; measured terminal failure → `failed`; incomplete expected work (`pending`/`running`/`deferred` or incomplete census) → `pending`; complete positive evidence only → `ready`. Predicates: `process_alive=ready` ⇔ `_readiness_payload()` emits schema-valid payload; `source_searchable=ready` ⇔ complete census identifies all current non-tombstoned source heads and retained-source lexical/document-read succeeds for every head, with N=0 ready only when census complete; `projection_searchable=ready` ⇔ active `(generation_id, projection_version, config_digest)` matches and every current non-tombstoned head has vector receipt `state=searchable`; `mesh_ready=ready` ⇔ same identity matches and every current non-tombstoned head has graph receipt `state=searchable`. Partial `process_alive/source_searchable/projection_searchable=ready` + `mesh_ready=pending` → aggregate `ok=false` → `/readyz` 503; stage reasons retain safe evidence. Anchors: `CONTEXT.md:31-41`; `kb/server.py:4866-4868,5532-5556,5578-5582`; `kb/lifecycle.py:140-157,1650-1763,2179-2231,2514-2528,2530-2532,2578-2597`; `kb/service.py:683-701`.
V5: `/api/state` carries same safe stage truth; full readiness failure → `ok=false` & `healthy=false`; public response never serializes secrets or exception text.
V6: `/health/ready.ok` equals combined readiness payload `.ok`; it cannot report dependency-green while `/readyz` gate is red.
V7: ∀ authoritative/public count → source scope named; unknown/missing/unmeasured ≠ `0`; restart-scoped counters never become corpus totals.
V8: ∀ vault-state consumers → settled false renders `Loading`; settled true + cached null renders `Unavailable` + reload guidance; neither renders `Live`/healthy.
V9: Existing `/readyz` auth, `/api/state` public access, and healthy compatibility fields remain unchanged except additive `stages` and truthful gate status.
V10: ∀ bounded-search HTTP error → full bounded body redacted before 500-char display trim; CLI/MCP preserve typed code/status/message & allowlisted retrieval_receipt; body >500 → no secret leak & no classification loss; absence.proven=false & upstream_truncation=null
V11: ∀ local #247 fixture → repair splits oversized content into chunks ≤ configured embedder budget & distinctive tail content retrieves from a non-head chunk; local proof ≠ production corpus proof.

## §T TASKS
id|status|task|cites
T1|x|set default/deployment corpus cap `20,000`; retain finite positive env override; reject malformed, non-positive, and unlimited values|V1,V2,I.api
T2|~|extend readiness payload with additive `stages` statuses/reasons; preserve corpus/lifecycle/canary subgates while adding strict four-stage conjunction and `/readyz` status codes|V3,V4,V9,I.api
T3|.|make `/api/state` expose redacted `stages` and full readiness `ok`/`healthy`; keep authoritative unknown counts absent/null, never zero|V5,V7,V9,I.api
T4|.|make `/health/ready` return combined readiness payload `.ok` while retaining detail-free response|V3,V6,I.api
T5|.|add backend regressions for cap validation/boundary, stage status/reasons, strict four-stage gate precedence, lifecycle health true with pending current-generation jobs, Mesh pending → `/readyz` 503, failed `/api/state`, `/health/ready`, redaction, and unknown-count behavior|V1,V2,V3,V4,V5,V6,V7
T6|.|update vault-state result handling: settled false pending; settled true cached null failure; never retry cached null|V8,I.web
T7|.|fix SectionIndex null-state false-green: render `Loading` pending, `Unavailable · reload page` after failure, warning state on unavailable|V8,V9,I.web
T8|.|add frontend/static-export regressions for pending/failure labels and removal of null→Live fallback|V8
T9|x|repair local oversized-chunk fixture into bounded chunks & prove tail retrieval; bounded repair selection/journal proof; no production reprocessing|V11,I.test

## §B BUGS
id|date|cause|fix
B1|2026-09-11|CLI/MCP sliced error text before parse/redact → timeout metadata loss & secret prefix leak|V10
B2|2026-09-15|pytest launcher shebang points at a missing historical virtualenv, so focused verification cannot start|external test runner
B3|2026-09-15|offline vector regression compared marker tokenization without its preceding source-space token, masking a valid tail match|test fixture
B4|2026-09-15|non-monotonic prefix count can place over-limit fallback piece into current without guard|V11
## Least confident decisions
1. Stage shape: additive top-level `stages` map with four named `{status, reason}` values chosen; [INFERRED: user specified field names/status vocabulary, while current code has no finalized schema].
2. `pending` vs `unavailable`: unmeasured-but-expected stages use `pending`; dependency/read path unavailable uses `unavailable`; [INFERRED: distinguishes work not finished from no measurement, consistent with ADR-0020 missing ≠ zero].
3. Env cap override: lower positive overrides remain allowed, values above `20,000` rejected; [VERIFIED: current env override exists; INFERRED: “cap 20,000” means hard maximum while preserving operator tuning].
4. `/api/state` transport status: readiness failure remains JSON 200 with `ok=false`/`healthy=false`; [VERIFIED: current public endpoint degrades dependency failures without 500; user requested truthful failed-state regression, not public 503].
5. Current `current_sources` counts all `source_heads`, including tombstoned heads (`kb/lifecycle.py:140-157,2530-2532`); `current_searchable_by_backend` counts are aggregate, not per-head proof (`kb/lifecycle.py:2578-2597`). [NOT DETERMINED: implementation must add bounded per-head evidence or equivalent authoritative aggregate before V4 predicates can be measured.]
6. #247 local repair: synthetic chunk repair and tail retrieval chosen as local proof; [INFERRED: diagnosis identified historical oversized chunks, but provider behavior and production state remain unmeasured].
