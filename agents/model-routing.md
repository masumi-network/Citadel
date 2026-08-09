# Agent model routing

Last updated: 2026-08-09
Owner: coordinator
Status: In Progress

## Available models

- VERIFIED from current agent runtime metadata: `gpt-5.6-sol` is the frontier agentic coding model. Route architecture, cross-boundary reasoning, difficult debugging, security review, Fresh Eyes, Red Team, and integration disputes here.
- VERIFIED from current agent runtime metadata: `gpt-5.6-terra` is the balanced everyday agentic coding model. Route bounded implementation, Docker operation, focused test execution, log classification, and tracker updates here.

Do not use model choice as ownership. Every task still needs one role owner, exact scope, dependencies, acceptance command, and handoff.

## Routing policy

| Task type | Default model | Effort | Use when | Avoid when |
|---|---|---:|---|---|
| `reason` | `gpt-5.6-sol` | `high` | Architecture, interface choice, root-cause analysis across modules | Pure command execution or mechanical edits |
| `resolve` | `gpt-5.6-sol` | `high` or `xhigh` | P0 or P1 defect, data safety, rollback, auth, concurrency | Well-specified low-risk patch |
| `execute` | `gpt-5.6-terra` | `high` | Bounded code change with settled contract and exact tests | Unsettled architecture or destructive recovery design |
| `runtime` | `gpt-5.6-terra` | `medium` or `high` | Docker build, Compose operation, log following, benchmarks, evidence capture | Interpreting a new security or consistency failure alone |
| `review` | `gpt-5.6-sol` | `high` or `xhigh` | Fresh Eyes, Red Team, security, contract and release review | Editing the reviewed files |
| `track` | `gpt-5.6-terra` | `medium` | GitHub read-only refresh, status index, evidence normalization | Making closure or release decisions |

Use `fork_turns: "none"` for fresh-context tasks. Put all needed repository paths, task state, and contracts in the assignment. Use a small positive fork only when the task depends on the last few user decisions and repeating them would risk drift.

## Current Docker release task index

| Task ID | Status | Type | Owner | Model and effort | Scope | Depends on | Linked record | Acceptance |
|---|---|---|---|---|---|---|---|---|
| CITADEL-SEARCH-EMPTY-01 | In Progress | `resolve` | implementer | `gpt-5.6-sol`, `high`. Needs semantic distinction between no data and provider failure. | `kb/cognee_client.py`, focused tests only | None | `BLK-2026-08-09-03` | Dockerized red test fails before fix and passes after fix; empty-dataset `/search` returns HTTP `200` with zero results; `citadel status --check-search` keeps top-level health true and records an honest zero count without a provider exception. The existing zero-result canary remains unavailable by design. |
| CITADEL-BACKUP-GEN-01 | Completed | `resolve` | implementer | `gpt-5.6-sol`, `xhigh`. Restore identity and rollback can destroy or orphan state. | `kb/generation_backup.py`, backup CLI, focused tests | Backup contract CITADEL-INT-BACKUP-01 | `BLK-2026-08-09-02` | Wrong-generation restore fails before writes; failed count verification leaves target Qdrant and filesystem empty |
| CITADEL-BACKUP-CONTAINMENT-01 | Planned | `resolve` | implementer | `gpt-5.6-sol`, `xhigh`. Symlink cleanup and nested destination traversal can remove or contaminate operator state. | `kb/generation_backup.py`, backup CLI, focused backup tests | CITADEL-BACKUP-GEN-01 and approved Docker release design | `BLK-2026-08-09-02` | Symlink and nested destinations fail before writes; base-install CLI failure is typed; backup directories are `0700` and files are `0600` |
| CITADEL-DOCKER-TEST-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. Contract is known and work is bounded Docker and CI plumbing. | Dockerfile test target, `.dockerignore`, workflow, Docker workflow tests | Search and backup red tests defined; approved Docker release design | `BLK-2026-08-09-04` | Non-live suite, Ruff, and three live Qdrant tests run inside Docker with nonzero collection and zero failures |
| CITADEL-DOCKER-RUNTIME-01 | Planned | `runtime` | runtime | `gpt-5.6-terra`, `high`. Long bounded Compose and evidence run. | Disposable Docker resources and evidence only | Prior three tasks integrated | Docker handoff gate 4 | Production Compose passes auth, ingest, receipts, search, census, CLI, MCP, capture, restart, outage, restore, stress, resource, security, and classified-log gates |
| CITADEL-FRESH-EYES-CORROBORATE-01 | Planned | `review` | reviewer | `gpt-5.6-sol`, `high`. Independent evidence-support review. | Immutable integrated diff, contracts, Docker evidence. Read-only | Runtime and benchmark gates green | Fresh Eyes Corroborate | Supported claims returned with exact evidence and blind spots; root reproduces P0 and P1 findings |
| CITADEL-FRESH-EYES-REFUTE-01 | Planned | `review` | reviewer | `gpt-5.6-sol`, `high`. Independent falsification review. | Same immutable candidate and evidence. Read-only | Runtime and benchmark gates green | Fresh Eyes Refute | Attempted falsifications returned with exact evidence; root reproduces P0 and P1 findings |
| CITADEL-RED-TEAM-01 | Planned | `review` | reviewer | `gpt-5.6-sol`, `xhigh`. Adversarial data-loss, isolation, auth, restore, and concurrency review. | Integrated diff and final Docker evidence. Read-only | Fresh Eyes fixes integrated | Red Team skill | No unresolved reproduced P0 or P1; every accepted finding has Docker regression evidence |
| CITADEL-GITHUB-REFRESH-01 | Planned | `track` | release | `gpt-5.6-terra`, `medium`. Read-only tracker normalization after evidence changes. | GitHub issues and PRs, `.local-review/GITHUB_INDEX.md`, `status.md` | Reviews green and local commits exist | Issues 128, 228, 247; PRs 254, 255, 256 | Exact live counts, heads, checks, dispositions, blind spots, and correction entries recorded. No external write |
| CITADEL-GITHUB-AUDIT-01 | Completed | `track` | release | `gpt-5.6-terra`, `medium`. Live issue and PR classification is bounded read-only tracker work. | GitHub issues and pull requests; `.local-review/GITHUB_INDEX.md` | Revised Docker release design input | User GitHub lane direction on 2026-08-09 | Every open issue and pull request classified as v0.5 blocking, future-valid, superseded, duplicate, or obsolete with live URL, state, head/check evidence, and no external write |
| CITADEL-PLAN-RING0-01 | Completed | `reason` | researcher | `gpt-5.6-sol`, `high`. Four release blockers cross backup, Docker, stress, and search boundaries. | Read-only mapping of Ring 0 source and tests | Approved Docker release design | Design sections 5 and 6; BLK-2026-08-09-02, `-03`, `-04` | REPORTED handoff returned exact boundaries, Docker commands, commit sequence, status-canary correction, and prerequisites; no files changed |
| CITADEL-PLAN-RUNTIME-01 | Completed | `reason` | researcher | `gpt-5.6-sol`, `high`. Compose runtime proof spans lifecycle, all surfaces, outage, restart, and restore. | Read-only mapping of Rings 1 and 2 runtime paths | Approved Docker release design and Ring 0 interfaces | Design sections 3 through 6 | REPORTED handoff returned seed arithmetic, typed-error conflict, receipt gap, snapshot gap, commands, and evidence shape; no files changed |
| CITADEL-PLAN-CORPUS-01 | Completed | `reason` | researcher | `gpt-5.6-sol`, `high`. GitHub, seats, traces, promotion, and benchmark need comparable identities and strict visibility gates. | Read-only mapping of Rings 3 and 4 | Approved Docker release design and runtime seed contract | Design sections 5 through 9 | REPORTED handoff returned exact files, Railway allowlist, current-head evidence, isolation matrix, promotion gap, and benchmark gaps; no files changed |
| CITADEL-STRESS-429-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. One bounded test and predicate change. | `scripts/stress_qdrant_search.py`, focused test | Docker test target available | Ring 0 stress gate | Docker red returns `assert 0 == 1`; green rejects all-429 and preserves mixed 200/429 acceptance |
| CITADEL-RETRIEVAL-ERRORS-01 | Planned | `resolve` | implementer | `gpt-5.6-sol`, `high`. Timeout and provider failures currently conflict with retrieval contracts across three surfaces. | `kb/server.py`, `kb/cli.py`, `kb/mcp_server.py`, focused adapter and tests | Ring 0 search commit and architect contract update | CITADEL-INT-RETRIEVAL-01 | HTTP 504 `SEARCH_TIMEOUT`; HTTP 503 `QDRANT_UNAVAILABLE`; CLI nonzero and MCP `isError=true` with same code; healthy empty remains 200 |
| CITADEL-RECEIPT-EXPOSURE-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. Response fields and source data already exist. | `kb/server.py`, `kb/cli.py`, focused share, capture, MCP tests | Retrieval-errors task serialized; architect contract update | CITADEL-INT-LIFECYCLE-01 | Share returns one operation identity per target; capture JSON preserves source, job, and state without inventing IDs |
| CITADEL-COMPOSE-SNAPSHOT-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. Bounded Compose storage change. | Both Compose files and contract tests | Ring 0 Docker target | CITADEL-INT-BACKUP-01 | Primary and snapshot volumes are distinct and mounted at Qdrant's pinned paths in both Compose files |
| CITADEL-RELEASE-PROBE-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. Versioned fixture and mechanical evidence tooling. | Seed fixture, `kb/release_acceptance.py`, log classifier, focused tests | Typed errors, receipts, snapshot, runtime hardening | Runtime plan Tasks 5 and 6 | Eight-source manifest validates; HTTP, CLI, MCP, lifecycle, census, graph, visibility, and phase log assertions emit redacted evidence |
| CITADEL-GITHUB-INPUTS-01 | Planned | `execute` | implementer | `gpt-5.6-terra`, `high`. Bounded optional environment passthrough. | Both Compose files, `.env.lite.example`, local deploy and tests | Snapshot Compose task | Approved Railway allowlist | Only optional `CITADEL_GITHUB_TOKEN` is copied; unknown env stays out; generated env remains 0600 |
| CITADEL-GITHUB-SMOKE-01 | In Progress | `runtime` | runtime | `gpt-5.6-terra`, `high`. User requested the next live local check and the operation must stay inside one app process. | Preserved disposable Citadel and Qdrant containers; no source edits | Current preserved container health and approved read-only GitHub access | User direction on 2026-08-09 | Read status and run repo-content dry-run through admin API; if inputs are sufficient, run one local GitHub sync through the same app process; preserve exact response and both logs; label old-image evidence exploratory |
| CITADEL-CURRENT-HEAD-EVIDENCE-01 | Planned | `resolve` | implementer | `gpt-5.6-sol`, `high`. Current-head joins must reject historical searchable projections. | Lifecycle store, service, release acceptance, focused tests | Architect lifecycle contract | CITADEL-INT-LIFECYCLE-01 | Every frozen GitHub source key resolves to the active head, current generation, one job, and three searchable receipts |
| CITADEL-PROMOTION-APPROVAL-01 | Planned | `resolve` | implementer | `gpt-5.6-sol`, `xhigh`. Current known-org path can write Central without pending approval. | Promotion engine, attestation transport if needed, focused tests | Isolation green and architect promotion contract | Ring 4 promotion invariant | Every qualifying proposal queues; only admin approval writes Central with pending ID, source hash, approver, and time |
| CITADEL-BENCH-RELEASE-01 | Planned | `reason` | implementer | `gpt-5.6-sol`, `high`. Comparison identity and quality versus latency require careful separation. | Retrieval evaluator, benchmark tests, release acceptance | Immutable corpus and retrieval profile contract | Approved benchmark decision | Release mode rejects identity drift, hidden partials, underfill, visibility errors, lost exact answers, and p95 above 20 percent |
| CITADEL-OBSERVED-OUTCOMES-01 | Planned | `review` | reviewer then implementers | `gpt-5.6-sol`, `high`. PR 245 predates current architecture and must be reconciled semantically. | PR 245 behavior inventory and bounded current-source ports | Rings 0 through 4 implementation assembled | PR 245 and PR 256 | Every PR 245 behavior classified and covered; no wholesale conflicting merge; required ports pass Docker |

## Dependency path

```text
CITADEL-SEARCH-EMPTY-01 ----+
                            +--> CITADEL-DOCKER-TEST-01 --> CITADEL-DOCKER-RUNTIME-01
CITADEL-BACKUP-GEN-01 -----+                                      |
                                                                  v
                                                   CITADEL-FRESH-EYES-01
                                                                  |
                                                                  v
                                                    CITADEL-RED-TEAM-01
                                                                  |
                                                                  v
                                                 local commits and tracker refresh
```

Root may run the first two implementers in parallel because their file scopes are disjoint. Docker test infrastructure starts after both red-test contracts are known. Only the runtime owner mutates containers. Reviews start after the integrated Docker gate is green.

## Spawn examples

Deep defect resolution:

```json
{
  "task_name": "backup_generation",
  "fork_turns": "none",
  "model": "gpt-5.6-sol",
  "reasoning_effort": "xhigh",
  "message": "Read the active handoff and BLK-2026-08-09-02. Own only the listed backup files. Add wrong-generation red test first. Run acceptance inside Docker. Return the repository handoff contract."
}
```

Bounded Docker execution:

```json
{
  "task_name": "docker_runtime",
  "fork_turns": "none",
  "model": "gpt-5.6-terra",
  "reasoning_effort": "high",
  "message": "Read the active handoff and CITADEL-DOCKER-RUNTIME-01. Own Docker mutations and both log followers. Do not edit source. Return exact commands, results, classified logs, resource samples, blind spots, and preserved resources."
}
```
