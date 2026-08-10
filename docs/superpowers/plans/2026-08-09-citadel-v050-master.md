# Citadel v0.5 Docker release master plan

> **REQUIRED SUB-SKILL:** Execute the linked implementation plans with `superpowers:subagent-driven-development`. Invoke Fresh Eyes and Red Team only at their indexed review gates.

**Goal:** Deliver one evidence-backed Citadel v0.5 Docker release candidate, update PR 256, and stop before merge or publication.

**Architecture:** Four serialized plan groups move from blocker integration to deterministic runtime, live corpus and quality, then independent review and PR integration. Root owns shared contracts and commits. Fresh implementers own bounded source scopes. One runtime owner mutates Docker and follows Citadel plus Qdrant logs.

**Tech Stack:** Python 3.12, Citadel 0.5.0, Cognee 1.4.1, Qdrant 1.19.0, SQLite, Ladybug, lifecycle v1, Docker Compose, GitHub Actions.

**Global Constraints:**

- Implementation worktree: `/private/tmp/citadel-v050-qdrant`.
- Tracking repository: `/Users/sarthiborkar/masumi/Citadel Archive`.
- All Python tests, lint, functional probes, and benchmarks run inside Docker.
- One runtime owner may mutate Docker. All live phases follow both logs.
- Root reproduces every reported P0 or P1 before recording it as verified.
- Commits are sequential and use `sarthib7 <sarthiborkar7@gmail.com>`.
- PR 256 and evidence-backed tracker maintenance are allowed only after local gates and reviews.
- Merge, tag, release publication, deployment, Railway mutation, production mutation, and production deletion remain blocked.

## Execution order

1. [Ring 0 blockers](./2026-08-09-citadel-v050-ring0-blockers.md)

   Integrate backup generation and containment, honest empty search, all-throttled stress rejection, Docker test target, CI parity, and non-root production packaging.

2. [Compose runtime](./2026-08-09-citadel-v050-compose-runtime.md)

   Fix typed retrieval failures and receipt exposure, add snapshot storage, integrate runtime-hardening hunks, then prove deterministic seed, restart, Qdrant replacement, outage recovery, stress, backup, and restore.

3. [Corpus and quality](./2026-08-09-citadel-v050-corpus-quality.md)

   Import only the proven-needed GitHub token, sync Central, attest current heads, prove two-seat and trace isolation, require admin-approved promotion, and enforce same-corpus quality plus p95.

4. [Review and PR](./2026-08-09-citadel-v050-review-pr.md)

   Reconcile PR 245 behavior, freeze the candidate, run separate Fresh Eyes Corroborate, Fresh Eyes Refute, and Red Team reviews, reproduce and fix findings, update PR 256, wait for current-head CI, maintain trackers, and stop for release approval.

## Dependency graph

```text
Ring 0 search + backup + Docker
                |
                v
typed errors + receipts + snapshot storage
                |
                v
deterministic seed -> restart -> outage -> restore
                |
                v
GitHub Central -> seats/traces -> promotion -> benchmark
                |
                v
PR 245 behavior audit -> immutable candidate
                |
                v
Fresh Eyes Corroborate + Fresh Eyes Refute + Red Team
                |
                v
reproduced fixes -> full affected Docker rerun
                |
                v
PR 256 push -> current-head CI -> tracker maintenance
                |
                v
stop before merge, tag, publication, or deployment
```

## Root confirmation points

1. Before a shared interface edit, root updates `docs/interfaces.md` and records the decision when strategy changes.
2. Before Docker execution, root assigns one runtime owner and records container, volume, network, and log-follower ownership.
3. Before benchmark comparison, root proves both stacks share corpus, questions, ground truth, generation, model, runtime, resources, warm-up, repeats, and client image.
4. Before GitHub writes, root verifies the reviewed local candidate and refreshes the remote head.
5. Before any merge, tag, publication, deployment, Railway mutation, or production mutation, root stops for separate user approval.

## Release-ready exit

- Exact candidate identity, versions, non-root UID, dependencies, and Compose configuration recorded.
- Ring 0 Docker test, lint, live Qdrant, and production smoke gates pass.
- Deterministic seed and live GitHub Central have current searchable relational, vector, and graph receipts.
- HTTP, CLI, and MCP agree on exact-hit identity. Healthy empty search remains distinct from timeout and provider outage.
- Restart, Qdrant replacement, outage convergence, stress, backup, fresh restore, and same-image boot pass.
- Two seats, shared trace, capture, hydrate, graph, delete, prune, and admin-approved promotion have zero visibility violations.
- Frozen quality and Docker p95 gates pass, or v0.5 remains blocked for a stronger-model comparison.
- Independent reviews leave no unresolved reproduced P0 or P1.
- PR 256 contains the reviewed commits and current-head CI is green.
- Remaining blind spots are recorded before the release-approval stop.

## Plan self-check

- Every implementation group names exact files, red tests, source behavior, Docker greens, and commit order.
- The empty-search contract preserves the existing zero-result status canary.
- No plan uses top-k retrieval as corpus evidence.
- No plan treats Docker latency as a model-quality result.
- No plan authorizes release publication or production mutation.
