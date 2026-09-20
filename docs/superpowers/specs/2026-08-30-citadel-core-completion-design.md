# Citadel Core Completion Design

- Status: PLANNED
- Date: 2026-08-30
- Scope: search injection, projection sync, feedback, promotion, UI access, seat isolation, production checks, and records

## Approval

[VERIFIED] The user approved the five design sections in this session. The user selected task-aware search injection, sync or cron for “sink chop”, canary-only production writes, and PR handoff without agent commit or push.

## Current system

[VERIFIED] SQLite lifecycle state is the authority for source revisions, projection jobs, and backend receipts. `kb/lifecycle.py:420-500` defines the schema. `kb/lifecycle_worker.py:718-924` writes and verifies relational, vector, and graph projections.

[VERIFIED] The deployed cutover receipt reports these exact values. Full evidence: `.local-review/production-verification.md:20-31`.

```text
schema=citadel.projection-cutover
generation_id=citadel-railway-v058-stage-attest-20260828
archived_pipeline_run_count=5264
cleared_data_count=128
remapped_dataset_count=8
archived_graph_count=16
```

[VERIFIED] The target Cognee and lifecycle SQLite files returned `PRAGMA integrity_check` as `ok` through read-only SQLite URIs. Evidence: `.local-review/production-verification.md:33-35`.

[VERIFIED] An authenticated production search returned this exact redacted summary. Evidence: `.local-review/production-verification.md:41-53`.

```text
http=200
result_count=3
search_id_present=true
first_dataset=masumi-network
first_source_type=repo-content
first_document_id_present=true
result_id_present=true
provenance_present=true
retrieval_present=true
```

[VERIFIED] The Qdrant target collection returned this exact summary. Evidence: `.local-review/production-verification.md:55`.

```text
collection=citadel_g_d98abd89d9f6_DocumentChunk_text_e8bbe7045b
status=green
points_count=64085
vector_size=384
distance=Cosine
```

[VERIFIED] The current-head graph census returned these exact counts. Evidence: `.local-review/production-verification.md:126-135`.

```text
graph searchable=128 pending_or_running=11514
relational searchable=11642 pending_or_running=0
vector searchable=11642 pending_or_running=0
tombstone searchable=900 for each backend
```

[VERIFIED] An authenticated graph request for `seat:citadel-dev-team` returned this exact summary. Evidence: `.local-review/production-verification.md:57`.

```text
ok=true
fallback=false
nodes=26
edges=57
truncated=true
```

[VERIFIED] The existing SessionStart hook injects static policy and optional recent activity. Its input fields remain unused for task search. `kb/hooks/sync_start.py:26-28,102-146`.

[VERIFIED] The existing HTTP and MCP feedback paths work at the server boundary. Implicit telemetry is held in restart-local MeshState. `kb/server.py:2889-3008,8603-8633,8980-9068`; `kb/mesh.py:180-330,979-1085`.

[VERIFIED] `/app` is the canonical authenticated UI. Env keys and opaque `ctdl_` tokens authenticate through `/admin/session`. Seat datasets are logically isolated. A seat can read its Node, Central, and shared session traces by design. `kb/server.py:1896-1956,4140-4198`; `kb/access.py:458-578`; `web/README.md:1-60`.

## Goals

[PLANNED]

1. Inject bounded, task-relevant search context into agent sessions.
2. Keep each active source revision convergent across SQLite, Cognee relational data, Qdrant, and Ladybug.
3. Persist feedback once, link implicit and explicit feedback, and produce bounded autonomous decisions.
4. Run promotion only after fresh projection evidence and preserve all approval gates.
5. Give users a clear token login flow and visible seat scope.
6. Prove each feature in production, one at a time, with canary-only writes.
7. Keep public documentation and private evidence records current.

## Non-goals

[PLANNED] Do not build the ADR-0021 `RetrievalBackend` seam before the current vertical slices work.

[PLANNED] Do not add a second Railway graph-writing process. Ladybug remains a single-writer store.

[PLANNED] Do not treat all-store sync as a distributed transaction. Receipt-backed convergence is the contract.

[VERIFIED] “Sink chop” is user shorthand for the sync or cron cycle. It is not a new domain term.

## Design decisions

### 1. Task-aware search injection

[PLANNED] Add a prompt hook that receives the submitted task. Keep static policy in SessionStart.

[PLANNED] Build the query without an LLM. Remove fenced code, long logs, URLs, and command wrappers. Preserve issue IDs, paths, repository names, and domain terms. Bound the query and timeout.

[PLANNED] Call the existing authenticated `/search` path with the caller token. Inject at most three hits. Include title, bounded snippet, dataset, result ID, search ID, trust class, and provenance. Mark hit text as untrusted context.

[PLANNED] On timeout, invalid credentials, or search failure, inject only static policy.

### 2. Projection and sync cycle

[PLANNED] Use one capture run ID for each scheduled source-sync cycle. Propagate the ID through GitHub, repository-content, Linear, and future sync workers.

[PLANNED] Run source ingestion with inline projection suppressed. Drain the exact relational and vector jobs. Run selected-data graph Cognify for those source IDs. Verify provider readback before marking receipts searchable.

[PLANNED] Run feedback, promotion, and self-improvement only after this barrier. Drain jobs created by those stages through a second barrier.

[PLANNED] Keep vector search available while graph work is deferred. Full sync requires all three backend receipts. Search readiness and graph readiness remain separate states.

[PLANNED] The existing web-process scheduler is the cron owner. Its Phase 2 dataset-wide Cognify stays disabled during the clean rebuild. Its post-pass `include_deferred=true` resume starts the selected graph lane.

### 3. Feedback and promotion

[PLANNED] Add durable feedback event and decision records to the existing SQLite state store. Store IDs, dataset, score, trust class, actor, bounded reason, and timestamps. Do not store tokens, raw source text, or raw provider bodies.

[PLANNED] Deduplicate with an idempotency key based on event kind, search ID, actor, and result ID.

[PLANNED] Keep `/feedback` and MCP `citadel_record_feedback` as the agent send paths. Link explicit ratings to implicit search telemetry when the search ID matches.

[PLANNED] The first autonomous consumer emits `no_action`, `ranking_eval_candidate`, or `projection_repair_candidate`. It may enqueue a repair only when lifecycle evidence proves a missing projection. It must not rewrite memory or promote content from low scores alone.

[PLANNED] Run PromotionEngine after the first projection barrier. Preserve secret-scan, reference, LLM, relevance, and admin approval gates. Preserve `secret_scan` when reconstructing pending items. Test promotion with a canary dry run and no Central write.

### 4. UI access and seat isolation

[PLANNED] Keep `/app` canonical during this work. Keep `/next/app*` preview-only until scope checks and browser tests pass.

[PLANNED] Keep `/admin/session`, secure HttpOnly cookies, env credentials, and `ctdl_` tokens. Do not weaken Secure cookies for local HTTP.

[PLANNED] Gate preview routes by effective capability and scope, not role alone.

[PLANNED] Show the active private Node, Central, and shared-trace scope in the UI. Show dataset and trust class on search, graph, and drill-down results.

[PLANNED] Prove default-deny cross-seat access with two seat tokens. Central and shared traces remain intentionally readable.

[PLANNED] Keep the ForceGraph canvas for pointer users and add a synchronized accessible node directory. Add visible zoom, fit, selection, and clear controls. Add readable, grouped connection rows with an explicit `Show all` action. Hidden targets must say `Not shown in this map` instead of receiving an inert focus action.

[PLANNED] Keep the document empty state honest about the loaded graph cap. Provide a retry or refresh action when the document is not reachable from the loaded slice.

[CORRECTED] Do not replace the `Degraded` status pill with `Rebuilding graph` in the graph UI slice. Production has seven failed graph jobs and a degraded repo-content state. Health wording needs a separate causal fix after those failures are classified and repaired.

### 5. Production verification and records

[PLANNED] Run feature checks sequentially. Start with read-only checks. Use unique records only in `seat:canary`. Never write test data to Central or real seats.

[PLANNED] Record operation ID, source revision ID, receipt states, search ID, result ID, graph evidence, HTTP status, and response hashes. Exclude credentials, raw bodies, and source text.

[PLANNED] Update `docs/architecture.md`, `docs/operations.md`, `docs/mcp/README.md`, `CONTEXT.md`, and UI documentation with dated provenance labels. Correct scheduler and provider statements before publishing them.

[PLANNED] Create `.local-review/production-verification.md` as an ignored local evidence ledger. It is not part of the PR.

[PLANNED] Audit GitHub issues after runtime proof. Add the required AI-triage disclaimer to issue comments. Update only proven solved issues. Do not delete issues.

[PLANNED] Prepare the PR title, body, verification list, and rollback path. The agent will not commit or push. The user must perform those actions.

## Implementation order

[PLANNED]

1. Finish and review the recovery hotfix already deployed.
2. Drain enough selected graph work to prove the canary path.
3. Implement task-aware search injection.
4. Implement capture-run watermarks and receipt barriers.
5. Implement durable feedback events and the bounded consumer.
6. Preserve promotion scan evidence and wire promotion after the barrier.
7. Fix UI capability checks and scope display.
8. Run the one-by-one production checks.
9. Update public docs and `.local-review`.
10. Audit and update issue states.
11. Prepare the PR handoff.

## Acceptance criteria

[PLANNED]

- One prompt produces one caller-scoped context pack or static-policy fallback.
- One canary source has one current lifecycle revision and three searchable receipts after projection.
- The same canary source is readable through vector search, document drill-down, and graph retrieval.
- Explicit agent feedback links to the search ID and survives a restart exactly once.
- The feedback consumer creates one bounded decision and never promotes from telemetry alone.
- Promotion dry run creates no Central write. Approval remains admin-only.
- A seat token logs into `/app`, shows its scope, and cannot retrieve another seat’s Node content.
- Production checks produce an evidence record without credentials or raw content.
- Public docs describe the deployed SQLite, Qdrant, Ladybug, lifecycle, scheduler, auth, and seat model.
- Full local verification remains green after each slice.

## Risks and controls

| Risk | Control |
| --- | --- |
| Retrieved prompt text contains instructions | Bound, redact, label as untrusted, and preserve provenance. |
| Graph backlog blocks every cycle | Keep vector readiness separate and use selected graph jobs. |
| Feedback causes self-reinforcing bad changes | Persist evidence first. Emit bounded decisions. Require gates. |
| Promotion uses stale data | Place it after the receipt barrier and run a second barrier. |
| Canary data pollutes shared knowledge | Reserve `seat:canary` and exclude it from Central promotion. |
| A second graph writer corrupts Ladybug | Keep all graph work in the web process. |

## Least confident decisions

1. [INFERRED] Claude Code `UserPromptSubmit` is the only prompt-hook client needed for the first injection slice. Other agent clients may need a context-pack endpoint later.
2. [INFERRED] A small SQLite feedback table is enough before a general outbox is needed.
3. [INFERRED] Keeping `/app` canonical is safer than making the Next preview canonical during this recovery.
4. [INFERRED] The existing scheduled cycle is sufficient as the sync or cron mechanism without a separate Railway cron service.
