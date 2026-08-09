# Status
Last updated: 2026-08-09
Updated by: coordinator
Current phase: lifecycle contract approval and candidate branch sync
Current sprint: CITADEL-QDRANT-2026-08-09

## Completed
- [x] CITADEL-QDRANT-2026-08-08-12, owner: implementer. VERIFIED: secure Cognee `1.4.1` source build shipped in candidate commit `420be9d`; draft PR 256 CI is green. Exact source SHA-256 is `9206075539935ef0adfab82cf410af6799e83c42969ba7c8fae5065de9aba7c9`; wheel SHA-256 is `2c1bec17b0ed9563ffa4f6ccdd4a02939cdec6dfd93db9faf852266ce3231a91`. Local full suite returned `1759 passed, 1 skipped, 11 warnings in 22.26s`.
- [x] CITADEL-QDRANT-2026-08-08-13, owner: implementer. VERIFIED: Qdrant `1.19.0` real-server adapter and Cognee `1.4.1` SQLite restart tests returned `2 passed, 11 warnings in 25.65s`. Central and Alice retrieval stayed dataset-scoped across a fresh Python process. Blind spot: LLM and embeddings were mocked.
- [x] CITADEL-RESEARCH-2026-08-07-01, owner: coordinator. VERIFIED: four upstream repositories pinned and audited, Citadel baseline audited, three independent verification reports completed, and ranked architecture roadmap written under `.local-review/research/`. Verification evidence: Citadel baseline `239 passed, 11 warnings`; Citadel lifecycle `152 passed, 10 warnings`; Citadel access `65 passed, 208 deselected`; Cognee retrieval `70 passed, 1 deselected, 10 warnings`; Cognee lifecycle `18 passed, 10 warnings`; Mem0 focused run `88 passed`; Graphiti focused run `51 passed, 1 warning`; Hindsight dependency-free retrieval check `3 assertions passed`.
- [x] CITADEL-RELEASE-2026-08-07-01, owner: architect. VERIFIED: recovery path A selected. Keep production pinned on Cognee 1.2.2 during containment, fix Citadel-owned adapter and queue contracts first, then test repair and alternative retrieval only in disposable storage. Evidence: `.local-review/wayfinder/tickets/001-choose-recovery-path.md`.
- [x] CITADEL-RELEASE-2026-08-07-02, owner: coordinator. CORRECTED: the original keep-goal decision preserved useful release gates, but the active objective still names Cognee `1.2.2`. Repository tracking now follows Cognee `1.4.1`; ticket 002 is reopened until the goal text is replaced.
- [x] CITADEL-RELEASE-2026-08-07-03, owner: implementer. VERIFIED: corrected local adapter checkpoint implemented in isolated worktree `/private/tmp/citadel-release-audit-20260807`. Final full `pytest -q` returned `1743 passed, 1 skipped, 11 warnings in 30.34s`; final relevant slice returned `550 passed, 11 warnings in 20.03s`; Ruff returned `All checks passed!`; `git diff --check` returned no output. Blind spot: local tests do not prove PostgreSQL, PGVector, Kuzu, Railway, or production behavior.
- [x] CITADEL-RELEASE-2026-08-07-04, owner: reviewer. REPORTED: both fresh-eyes and security reviewers returned final GO with no unresolved P0 or P1 after the security reviewer found and the implementer fixed three queue and force follow-up defects. Evidence: `.local-review/wayfinder/tickets/004-finish-adapter-checkpoint.md`.
- [x] CITADEL-MEMORY-RETRIEVAL-02, owner: researcher. VERIFIED: provenance-bound disposable pgvector and Qdrant benchmark completed across 4,200 measured queries. Every lane returned zero errors, zero underfill, zero visibility violations, and exact oracle parity. Qdrant exact p50 was `1.052230 ms`; pgvector exact p50 was `2.921750 ms`. Evidence: `.local-review/wayfinder/tickets/007-benchmark-pgvector-qdrant.md` and `/private/tmp/citadel-vector-store-bench-20260808-h.json`. Blind spot: this 49-file warm loopback fixture cannot select a production provider.
- [x] CITADEL-QDRANT-2026-08-08-01, owner: architect. CORRECTED: the original exact Cognee `1.2.2` route was superseded by user direction. Current route is Cognee `1.4.1` plus an audited patch based on official adapter commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29` in a fresh whole generation. Current production stays unchanged as rollback archive. Evidence: `docs/decisions.md` DEC-2026-08-08-05 and ticket 009.
- [x] CITADEL-QDRANT-2026-08-08-03, owner: researcher. VERIFIED: official Qdrant search, deployment, authorization, persistence, snapshot, monitoring, and version guidance is recorded in `.local-review/research/qdrant-search-engineering-deployment.md`; detector and provenance checks passed; SHA-256 `8a908f8d42ea561abec8ebf975d4df6a329cf4d05c3b385a944d90d77fbb5899`.
- [x] CITADEL-QDRANT-2026-08-08-06, owner: architect. VERIFIED: Milestone 0 contracts now distinguish the portable self-hosted Railway shadow from the research recommendation for a later Qdrant Cloud production topology. Evidence: `docs/decisions.md` DEC-2026-08-08-02 and `plans/roadmap.md`.
- [x] CITADEL-RESEARCH-2026-08-08-07, owner: researcher. VERIFIED: exact source audits of `topoteretes/cognee-rs` and `topoteretes/awesome-ai-memory` were recorded in `.local-review/research/cognee-rs-awesome-ai-memory.md`; detector score `0`, provenance check clean, report SHA-256 `12fa41946eb596fdc2208e45b65d700bec8786328afb53db8f835cf17454769a`. Blind spot: static source inspection did not build either repository or run Qdrant.
- [x] CITADEL-RESEARCH-2026-08-08-08, owner: researcher. VERIFIED: exact Cognee `1.2.2` to `1.4.1` feature, API, migration, security, telemetry, and Qdrant compatibility audit completed in `.local-review/research/cognee-1.4.1-feature-audit.md`; provenance passed, detector score was `1` with no critical issue, dash scan and diff check were clean, SHA-256 `165189d0ac418e138685755e18930360a6f7e5bd7123807d70f6f9b59f70f022`. Blind spot: no live Qdrant E2E, production migration, or deployment ran.
- [x] CITADEL-RESEARCH-2026-08-08-09, owner: researcher. VERIFIED: LoCoMo and LongMemEval audit completed in `.local-review/research/long-memory-benchmarks.md`; detector found zero P0, P1, or P2 issue; SHA-256 `7d9ae2b6b86d6e4f15671aa4c746e66629c1b29c334d9049fe00e630e88f6775`. INFERRED decision: keep Citadel v5 primary, add a deterministic 70-instance LongMemEval retrieval shadow, defer LoCoMo, and keep seat isolation separate. Blind spot: no benchmark run occurred.
- [x] CITADEL-RESEARCH-2026-08-08-10, owner: researcher. VERIFIED: official Railway template, variables, private networking, health, volume, backup, rollback, and publication audit completed in `.local-review/research/railway-one-click-template.md`; detector and whitespace checks were clean; SHA-256 `3f5c0ef830f5ea9964c82a968873941baa1c5661408fbcf2cedf93639156078c`. Blind spot: no Railway account or resource was accessed.
- [x] CITADEL-RESEARCH-2026-08-08-11, owner: researcher. VERIFIED: the Cognee `1.4.1` secure package audit found a one-line `<50` to `<51` metadata patch that retained `cryptography==50.0.0`; direct Cognee crypto and OAuth tests returned `15 passed, 10 warnings in 0.07s`. Evidence: `.local-review/research/cognee-secure-packaging.md`, SHA-256 `69ab2e56dcf01db1ef1fc80d7f7c5c4c7778d4d4d9e48d2d3425f4eaf185f426`. Blind spot: no reviewable remote fork commit or public package exists.
- [x] CITADEL-PORTABLE-2026-08-08-01, owner: architect. VERIFIED: exact Cognee `1.4.1` source supports SQLite and PostgreSQL; Railway documents SQLite on volumes; DigitalOcean App Platform has no persistent volumes. REPORTED: the user selected SQLite Lite plus Qdrant for the v0.5 Railway release.
- [x] CITADEL-QDRANT-2026-08-09-01, owner: implementer. VERIFIED: local candidate commits `a0d5c02` and `92ce11a` preserve the final adapter, Cognee compatibility, Qdrant census, Lite runtime, and deploy work. Local merge commit `f3e92ff` integrates PR 254 corpus readiness diagnostics. After that merge, focused tests returned `65 passed, 10 warnings in 5.51s`; the full suite returned `1781 passed, 3 skipped, 11 warnings in 32.97s`; Ruff returned `All checks passed!`.
- [x] CITADEL-QDRANT-2026-08-09-02, owner: release. VERIFIED: the disposable local stack accepted authenticated marker `citadel-v050-final-acceptance-14948765-cee0-454f-a85b-bf02c47f9360`, completed background cognify, and returned the full marker through authenticated search. SQLite backup and restore plus Qdrant snapshot restore passed. Blind spot: this was one document in a disposable generation.

## In Progress
- [ ] CITADEL-LIFECYCLE-2026-08-09-01, owner: architect, next action: obtain approval for the working v1 lifecycle and retrieval contract, file scope: `docs/interfaces.md`, `plans/roadmap.md`, and Wayfinder tickets 013 and 014. VERIFIED: the current `CognifyJob` stores dataset names and lease state only. It has no source revision, generation identity, or per-backend receipt.
- [ ] CITADEL-QDRANT-2026-08-09-03, owner: implementer, next action: sync local commits `a0d5c02`, `92ce11a`, and merge commit `f3e92ff` to remote PR 256 after explicit push approval, file scope: `/private/tmp/citadel-v050-qdrant`. VERIFIED: the worktree is clean and four commits ahead of remote head `420be9d`.
- [ ] CITADEL-QDRANT-2026-08-08-02, owner: reviewer, next action: close the remaining graph aggregation and destructive adapter-path evidence gaps before BLK-2026-08-08-02 can complete. VERIFIED: same-ID count, retrieve, search, and exhaustive scroll stayed dataset-scoped in the local provider receipt.

## Blocked
- [ ] BLK-2026-08-07-01, owner: release, severity: Critical, next escalation: obtain explicit approval to rotate the exposed database credential, then verify old credential rejection and service health. Evidence: `agents/blockers.md`.
- [ ] BLK-2026-08-08-01, owner: architect, severity: High, next escalation: retire in-place force repair from the production path and replace it with a full shadow generation plus verified cutover. Evidence: `agents/blockers.md` and ticket 005.

## Next priorities
- [ ] CITADEL-LIFECYCLE-2026-08-08-01, owner: architect, exit criteria: source revision, projection job, per-backend receipt, retrieval hit, and trace contracts are approved before schema implementation.
- [ ] CITADEL-LIFECYCLE-2026-08-08-02, owner: implementer, exit criteria: accepted source and queued projection work are atomic; second-process fault tests converge with exact per-backend receipts and census.
- [ ] CITADEL-QDRANT-2026-08-09-03, owner: implementer, exit criteria: remote PR 256 contains the local candidate commits and current-head CI passes.
- [ ] CITADEL-QDRANT-2026-08-08-04, owner: implementer, exit criteria: `pipx install citadel-archive==0.5.0` plus `citadel deploy local` and the Railway Template boot the same empty build and generation using one image, environment contract, and smoke script.
- [ ] CITADEL-QDRANT-2026-08-08-05, owner: release, exit criteria: GitHub Central passes source and projection census, then every seat passes zero visibility violations before the next seat import.

## Daily log
### 2026-08-07
- Owner: coordinator
- Completed: CITADEL-RESEARCH-2026-08-07-01. VERIFIED: 2,815 lines across source audits, independent verification, comparison, architecture, recommendations, and roadmap.
- CORRECTED: CITADEL-RELEASE-2026-08-07-03 and CITADEL-RELEASE-2026-08-07-04 were marked complete too early. A later security review reproduced two P1 failures that the fresh-eyes review missed: disjoint enqueue reset failed-job backoff, and force repair did not require graph presence. The earlier full-suite result was real but false-green for those untested cases.
- CORRECTED AGAIN: security re-review then found stale-lease recovery could leave two same-dataset jobs. VERIFIED: its new regression failed before the source fix; final queue suite returned `22 passed in 0.60s`. REPORTED: both reviewers returned final GO after the fix.
- Completed: corrected local adapter checkpoint. Final full suite returned `1743 passed, 1 skipped, 11 warnings in 30.34s`. The branch remains uncommitted and undeployed.
- In Progress: CITADEL-RELEASE-2026-08-07-05. Three read-only reviews are inspecting the canary contract, available disposable infrastructure, and repair safety boundaries. No provider service has been started.
- Blocked: BLK-2026-08-07-01. Database credential rotation needs explicit approval.
- Next: design and run the disposable PostgreSQL, PGVector, and Kuzu repair canary. Do not deploy, migrate, repair production data, push, or open a pull request without a new explicit approval.

### 2026-08-08
- Owner: coordinator
- Completed: secure Cognee build checkpoint and real Qdrant plus SQLite restart isolation proof.
- In Progress: uncommitted two-service Lite container stack and fail-closed single-process runtime.
- Blocked: Qdrant chunk budget census, Docker image boot, CLI and MCP E2E, backup restore, durable source and projection receipts, then Railway shadow.
- Next: resume in `/private/tmp/citadel-v050-qdrant`; build the Docker image before adding the local deploy CLI or changing Railway.
- Owner: researcher
- Completed: CITADEL-MEMORY-RETRIEVAL-02. VERIFIED: immutable artifact `/private/tmp/citadel-vector-store-bench-20260808-h.json` binds runner SHA-256 `d1d0ce0e1f42af38bcf0e1634a53b20eb45d4e08af7d7fa705617d57c050ab84`; all four lanes completed 1,050 measured queries with zero errors and identical quality.
- Completed: disposable Qdrant on port `6333` and PostgreSQL 17 on port `55432` were stopped after the run. VERIFIED: `lsof` returned no listener for either port; the existing PostgreSQL service on port `5432` remained listening. Temporary data directories were retained.
- REPORTED: fresh-eyes reviewer returned GO for the local benchmark checkpoint and NO-GO for provider choice, with no active P0 finding. Remaining gaps are unobserved Qdrant query traversal, unequal storage accounting, and small warm synthetic scope.
- In Progress: CITADEL-RELEASE-2026-08-07-05. Production repair remains gated by whole-dataset mutation, shared semantic artifacts, and crash recovery gaps.
- Blocked: BLK-2026-08-07-01. Database credential rotation still needs explicit approval.
- Next: keep Qdrant out of production scope; use the benchmark runner for a larger disposable same-corpus proof only after the source ledger and retrieval contracts exist.
- CORRECTED: the user selected Qdrant as Citadel's next vector backend after reviewing the benchmark. The earlier no-adoption direction is no longer current. The benchmark limits remain valid and are not evidence for a production cutover.
- In Progress: CITADEL-QDRANT-2026-08-08-01. Two parallel read-only audits are checking the official Cognee community adapter and Citadel's migration boundary. No Qdrant production service, collection, data copy, dependency change, or deployment has occurred.
- VERIFIED: official `topoteretes/cognee-community` main commit `f7c30c9c9f48275cfd758d57f3f8458bbd9e43e5` packages Qdrant adapter `0.3.0` with `cognee==1.1.0`, while Citadel requires Cognee `>=1.2.2,<1.3.0`. Compatibility is unresolved.
- VERIFIED: Cognee's official security documentation lists Qdrant as unsupported by Cognee backend access control. Citadel will not disable seat or dataset isolation; the migration needs a Citadel-owned fail-closed authorization boundary.
- Blocked: ticket 005 in-place repair. Changing the vector store does not contain Cognee's whole-dataset graph and relational mutations.
- Next: finish the source audit, write the migration contract, then run a disposable compatibility spike before proposing dependency edits.
- REPORTED: user selected a fresh Qdrant-backed Railway project and an OSS release that can also run on Render and local devices.
- VERIFIED: official Qdrant advisor guidance was fetched live for deployment, multitenancy, monitoring, and version upgrades. It requires tenant filters, persistent storage, backups, monitoring, and matched server and client minor versions.
- VERIFIED: real server probe used Qdrant `1.18.1` and client `1.18.0`. Unscoped search returned `2` private-seat points; scoped `seat:a` search returned `1`. The service was stopped after the probe.
- Completed: the migration decision, provider interface, whole-generation interface, stack record, roadmap, progress record, and Wayfinder route were updated. No code or external deployment changed.
- In Progress: official Qdrant search-engineering and installation research is being written to `.local-review/research/qdrant-search-engineering-deployment.md` by the researcher.
- Blocked: raw upstream Cognee Qdrant adoption. BLK-2026-08-08-02 owns authorization, payload, error, and ownership-upsert failures.
- Next: complete research review, then start ticket 009 only after the replacement goal is active.
- Completed: CITADEL-QDRANT-2026-08-08-03. VERIFIED: official Qdrant research artifact completed with detector exit 0, provenance `claim_lines=105 untagged=0`, and SHA-256 `8a908f8d42ea561abec8ebf975d4df6a329cf4d05c3b385a944d90d77fbb5899`.
- Completed: CITADEL-QDRANT-2026-08-08-06. VERIFIED: DEC-2026-08-08-02 selects pinned self-hosted Qdrant for the portable Railway shadow while preserving Qdrant Cloud as a later optional production topology.
- In Progress: CITADEL-QDRANT-2026-08-08-02. A read-only adapter implementation map is inspecting exact Cognee 1.2.2 methods, registration, payloads, score direction, dependency pins, and the minimum red-test boundary.
- Completed: Qdrant scope decision. VERIFIED: DEC-2026-08-08-03 rejects Cognee backend access control and NodeSet authorization for the first generation. INFERRED: task-local scope plus physical per-dataset collections preserves the global Kuzu graph and prevents cross-dataset same-ID overwrite.
- Next: implement the adapter contract in an isolated current-main worktree after the method map passes review. No deployment or production mutation is authorized.
- CORRECTED: user direction superseded the Cognee `1.2.2` custom-adapter route. The active implementation target is Cognee `1.4.1` plus official community adapter PR `#149` commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29`.
- VERIFIED: the combined resolver with Citadel's `cryptography>=50,<51` floor failed because Cognee `1.4.1` declares `<50`. The upstream set resolved `cryptography==49.0.0`; `pip-audit` reported high `CVE-2026-69247`, fixed in `50.0.0`.
- VERIFIED: a disposable forced runtime using Cognee `1.4.1`, adapter `0.3.0`, Qdrant client `1.19.0`, and cryptography `50.0.0` imported successfully, passed Citadel's private API contract, and returned `10 passed, 10 warnings` from the official adapter unit suite.
- VERIFIED: Citadel's focused Cognee client suite under that runtime returned `80 passed, 1 failed, 10 warnings`. The one failure expects at most one `True` result from concurrent dataset creation; Cognee `1.4.1` returned `True` to all three callers after converging on one dataset.
- VERIFIED: the active goal still names a Citadel-owned Cognee `1.2.2` adapter. The goal tool cannot edit an active objective. Repository tracking follows the user's later Cognee `1.4.1` instruction; the objective text needs user replacement or a new goal after this one is closed.
- In Progress: CITADEL-RESEARCH-2026-08-08-07. A researcher owns a code-level fact check of `cognee-rs` and `awesome-ai-memory` in a separate research file.
- Next: fix the dependency metadata path without lowering the security floor, then make the two known Cognee API adjustments and run the full local suite.
- REPORTED: user changed execution order to research-first. Implementation is paused until two source-backed Cognee `1.4.1` audits produce migration gates.
- VERIFIED: local source changes now call `run_migrations`, reject both raised and returned migration failures, pass `data_cache=False` with forced cognify, and load the official adapter registration only for Qdrant. The focused Cognee client plus dependency suite under Cognee `1.4.1` returned `86 passed, 10 warnings`.
- VERIFIED: a clean Python `3.12.12` environment using a locally built Cognee `1.4.1` wheel with only the `cryptography<50` metadata cap changed to `<51` returned `uv pip check: All installed packages are compatible`. Wheel SHA-256: `2c1bec17b0ed9563ffa4f6ccdd4a02939cdec6dfd93db9faf852266ce3231a91`. Blind spot: the wheel is local, unpublished, and not a portable dependency source.
- VERIFIED: the unmodified official community Qdrant adapter failed a two-dataset same-ID probe. Alice search changed from one hit to zero after Bob upserted the same UUID, and Alice raw retrieve returned Bob's `database_name` and text. Method: Qdrant local mode with adapter commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29`. Blind spot: local mode proves adapter point and filter semantics, not server performance or full Citadel routing.
- Next: complete CITADEL-RESEARCH-2026-08-08-08 and the official adapter integration audit, then revise the migration gates before more code changes.
- Completed: CITADEL-RESEARCH-2026-08-08-08. VERIFIED: exact Cognee `1.4.1` research completed with report SHA-256 `165189d0ac418e138685755e18930360a6f7e5bd7123807d70f6f9b59f70f022` and preserved live-test blind spots.
- CORRECTED: the first architecture reconciliation required physical per-dataset collections. DEC-2026-08-08-06 reopened the tenant storage boundary after reviewers identified dataset-namespaced stored IDs as a smaller tested alternative.
- CORRECTED: the raw official adapter is a source baseline, not a release dependency. Its dataset handler does not prevent same-ID overwrite or scope raw retrieve and delete.
- REPORTED: implementation-map reviewer proposed a smaller Qdrant-native boundary: one collection per generation and logical type, stored point IDs namespaced by dataset, mandatory tenant filters, and raw Cognee IDs preserved in payload.
- REPORTED: independent reviewer accepted namespaced IDs as an alternative if retrieve, delete, prune, rollback, and migration paths pass adversarial tests.
- REPORTED: goal continuation resumed without an A or B response. INFERRED: candidate B is the working decision because it was the recorded recommendation and does not mutate external state.
- Completed: candidate B unit boundary. VERIFIED: adapter tests first returned `8 failed, 5 passed, 10 warnings`, then returned `15 passed, 10 warnings` after the source patch. Covered generation-shared collections, dataset-namespaced IDs, payload parity, mandatory filters, retrieve, delete, count, scroll, prune, typed failures, EBAC-bound engines, and Citadel registration.
- In Progress: secure packaging. VERIFIED: the combined adapter, Cognee client, and dependency command returned `1 failed, 106 passed, 10 warnings in 9.89s`. The only failure is Cognee `1.4.1` metadata rejecting `cryptography==50.0.0`. Ruff returned `All checks passed!` for the five changed source and test files.
- HELD: no code from the isolated worktree has been committed or deployed. No production service or data was touched.
- REPORTED: user narrowed v0.5 portability to a one-click Railway Template and one-command local Docker deployment. Render moved to later scope.
- INFERRED: `pipx` remains the primary CLI installer. Working local command is `citadel deploy local`; existing `citadel setup` and `citadel onboard` meanings remain unchanged.
- VERIFIED: Wayfinder now has explicit tickets for secure Cognee packaging, typed retrieval and receipt contracts, durable lifecycle implementation, Railway template, and local deploy CLI. The source-lifecycle implementation milestone was missing before this audit and is now recorded.
- VERIFIED: `git diff --check` returned exit `0`. The writing detector returned no high or critical issue across the ten updated tracking files. The added-line dash scan and all-Wayfinder dash scan returned no match. Blind spot: these checks validate record formatting, not adapter behavior.
- Next: pin a reviewable Cognee `1.4.1` metadata fix, make plain pip and built-wheel installs green, then run candidate B against a real Qdrant server.
- Completed: relational profile decision. VERIFIED: Cognee `1.4.1` SQLite uses WAL and a 120 second busy timeout; Railway documents SQLite files on mounted volumes and backup support; DigitalOcean App Platform cannot persist the required app and Qdrant state.
- CORRECTED: the user selected SQLite Lite for Railway. Local Docker, Railway, and a single DigitalOcean Droplet now share SQLite plus Qdrant as the v0.5 default. PostgreSQL remains later optional.
- VERIFIED: current Railway usage pricing is `$10/GB-month` RAM, `$20/vCPU-month`, and `$0.15/GB-month` volume storage. PostgreSQL savings cannot be stated until a real idle plus ingest profile measures its resource use.
- Next: pin the secure Cognee source, then run the real-Qdrant E2E and restore matrix on SQLite Lite before publishing the Railway template.
- REPORTED: the user confirmed Qdrant plus SQLite as the low-cost v0.5 choice and authorized commits, pushes, and pull requests. Merge and deletion remain unauthorized.
- VERIFIED: `git diff --check` returned exit `0`. The writing detector returned no high or critical findings for the six new coordination files. Existing tracked operations and architecture prose still contains pre-existing detector findings outside this task's added lines.
- CORRECTED: release-plan commits were rewritten only to add the required DCO trailers. Current commits are `1f83a10` and `d054edb`; draft PR 255 remains stacked on PR 254.
- CORRECTED: adapter commits were rewritten only to add DCO trailers after a SQLite compatibility follow-up. Current commits are `ab635b0` and `d223481`; draft PR 256 remains based on `main`.
- VERIFIED: CI exposed an async Cognee `get_max_chunk_tokens` API and a PGVector-only boot assertion after the PostgreSQL extra was removed. The two focused regressions returned `2 passed, 10 warnings in 15.85s` after the fixes.
- VERIFIED: the post-fix full suite returned `1 failed, 1751 passed, 1 skipped, 11 warnings in 35.67s`. The remaining failure is the preserved secure Cognee metadata gate.
- VERIFIED: removing the Cognee PostgreSQL extra from the candidate lock removed `asyncpg 0.31.0`, `pgvector 0.3.6`, and `psycopg2-binary 2.9.12`. This proves dependency removal only, not SQLite runtime success.
- HELD: no Railway app deploy, migration, source import, production mutation, service deletion, merge, or release ran. The unused PostgreSQL service in the fresh Railway project remains intact pending separate approval.
- Next: supply the reviewed Cognee metadata patch, make the plain-pip gate green, then run empty SQLite plus real Qdrant E2E before changing Railway configuration.
- CORRECTED 2026-08-08: resumed Qdrant census patch testing in `/private/tmp/citadel-v050-qdrant`. `tests/test_cognee_client.py -k 'stored_chunk_budget_check_qdrant'` returned `5 passed`; `tests/test_cognee_client.py tests/test_lite_runtime.py` returned `93 passed`; `ruff` clean on touched files. `tests/test_qdrant_adapter_live.py` and `tests/test_cognee_qdrant_sqlite_live.py` remain skipped without explicit Qdrant/rediscovery env.
- Next in this thread: build/run the portable Lite image startup and restore sequence, then advance `CITADEL-QDRANT-2026-08-08-02` when real-provider census execution lands.
- CORRECTED 2026-08-08 (portable start): compose config validates when required env keys from `.env.lite.example` are supplied. `docker compose build` failed on this machine with local builder path permission (`/Users/sarthiborkar/.docker/buildx/activity/...` denied) and legacy cred-helper error `One or more parameters passed to the function were not valid. (-50)`.
- VERIFIED 2026-08-08 (local task scope): `tests/test_cognee_client.py -k 'stored_chunk_budget_check_qdrant'` and `tests/test_cognee_client.py` + `tests/test_lite_runtime.py` remain green (`5 passed`, `93 passed`) with `ruff` clean. `./.venv/bin/pytest -q tests/test_qdrant_adapter_live.py tests/test_cognee_qdrant_sqlite_live.py` returned `2 skipped` without `CITADEL_QDRANT_LIVE_URL`.

### 2026-08-09
- Owner: coordinator
- Completed: local candidate changes were split into `a0d5c02` and `92ce11a`, then PR 254 readiness diagnostics were integrated by merge commit `f3e92ff`. The post-merge full suite returned `1781 passed, 3 skipped, 11 warnings in 32.97s`; Ruff returned `All checks passed!`.
- In Progress: CITADEL-LIFECYCLE-2026-08-09-01 and CITADEL-QDRANT-2026-08-09-03. Working v1 lifecycle and retrieval contracts are recorded; local PR 256 work remains four commits ahead of the remote branch.
- Blocked: BLK-2026-08-07-01 and BLK-2026-08-08-01. Railway deploy, push, merge, release, schema migration, and production mutation still require their named gates.
- Next: approve or revise CITADEL-INT-LIFECYCLE-01 and CITADEL-INT-RETRIEVAL-01, then implement the source ledger and projection receipts before rebuilding or deploying the candidate.

## Key metrics
CORRECTED: the prior claim of six Node hits was not supported by this session. VERIFIED: `citadel_search` for the architecture query returned zero hits after `exclude_ambient=true`; 14 candidates were fetched and all were filtered out. Blind spot: this filtered top-k search cannot enumerate the vault or prove absence.

VERIFIED: Research contains 2,815 lines across 12 audit, verification, comparison, architecture, recommendation, and roadmap files. Method blind spot: line count measures artifact size, not correctness or coverage.

VERIFIED: All authored research Markdown returned zero substantive findings from the `avoid-ai-writing` detector after manual correction. Informational low-diversity and provenance-format findings remain in long evidence ledgers.

VERIFIED: the first checkpoint regression command collected eight named tests. Before source edits it returned `8 failed, 1 warning`; after source edits it returned `8 passed, 1 warning in 1.13s`. The next review regressions returned `7 failed, 1 passed`, then `8 passed`; the final two regressions returned `2 failed`, then `5 passed` with adjacent force and queue tests. Method blind spot: mocks prove Citadel behavior only. They do not prove real Cognee migration, PostgreSQL, PGVector, Kuzu, or Railway behavior.

VERIFIED: later security regressions returned `3 failed`, then their focused post-fix set returned `6 passed`; stale recovery returned `1 failed`, then the queue suite returned `22 passed`; alternate Alembic formatting returned `1 failed`, then logging plus queue returned `40 passed`. Method blind spot: these are mocked and local checks.

VERIFIED: the final full local test suite returned `1743 passed, 1 skipped, 11 warnings in 30.34s`. The skipped test and warnings were not investigated in this checkpoint. A green local suite does not prove deployment or production data safety.

VERIFIED: the disposable vector benchmark ran 4,200 measured queries over identical normalized vectors. Every lane returned oracle recall at 5 and 10 of `1.0`, zero errors, and zero underfilled queries. Qdrant exact p50 was `1.052230 ms`; pgvector exact p50 was `2.921750 ms`. Method blind spot: 49 cached files, synthetic 25 percent dataset selectivity, warm loopback providers, and no production corpus or network.

VERIFIED: the official Cognee community Qdrant package at main commit `f7c30c9c9f48275cfd758d57f3f8458bbd9e43e5` is version `0.3.0` and pins `cognee==1.1.0`. Citadel pins `cognee>=1.2.2,<1.3.0`. Method blind spot: source metadata proves a resolver conflict, not runtime incompatibility; the disposable compatibility spike has not run.

CORRECTED: the active source baseline is adapter PR `#149` commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29` with Cognee `1.4.1`. VERIFIED: targeted Citadel tests returned `92 passed, 10 warnings in 5.92s`, but plain pip remains unsatisfiable and the official adapter fails the same-ID isolation contract. Method blind spot: targeted tests do not exercise a real Qdrant server, graph aggregation, CLI/MCP, or deployment.

VERIFIED: the real Qdrant authorization probe used server `1.18.1` and client `1.18.0`. Unscoped search returned both `seat:a` and `seat:b`; applying `node_name=['seat:a']` returned only `seat:a`. Method blind spot: this direct adapter probe did not execute the full Cognee ingestion and Citadel HTTP paths.

## Risks
- Upstream main branches can change after research, owner: researcher, impact: stale citations, mitigation: exact commit pins are recorded in every verification report, status: Completed.
- Hindsight focused pytest was not completed, owner: reviewer, impact: selected behavior remains source-inspection evidence plus three direct assertions, mitigation: rerun the recorded focused command after freeing disk, status: Planned.
- CORRECTED: provider-backed vector retrieval was exercised in disposable pgvector and Qdrant. Provider-backed repair, crash recovery, transaction parity, production latency, and cost remain unmeasured, owner: reviewer, impact: a provider or repair decision would still be unsupported, mitigation: retain the disposable repair and larger same-corpus gates, status: Planned.
- CORRECTED: Qdrant is now the selected vector direction, owner: architect, impact: the official adapter dependency and access-control contracts do not currently satisfy Citadel, mitigation: require a Citadel-owned fail-closed adapter boundary plus disposable full-generation and operations proof before any production change, status: In Progress.
- CORRECTED: Cognee `1.4.1` is now the selected isolated candidate. Its cryptography metadata conflicts with Citadel's floor, owner: implementer, impact: ordinary pip resolution fails or falls back to a version with a high advisory, mitigation: require a reviewed metadata patch and keep `cryptography==50.0.0`, status: In Progress.
- Full production repair mutates more than the selected candidate set, owner: release, impact: healthy projections can change without exact rollback, mitigation: require full backup, candidate isolation proof, crash injection, and explicit production authorization, status: Planned.
- Cognee source storage and the file queue are separate writes, owner: architect, impact: process death after `cognee.add` and before queue persistence can still leave unindexed data, mitigation: disposable crash proof plus reconciliation or transactional outbox decision before closing the durable-work goal, status: Planned.
- Direct admin, Evolve, Linear, and repair cognify paths remain outside the queue execution owner, owner: release, impact: cross-process duplicate graph work remains possible during maintenance, mitigation: exclusive write freeze for repair and later shared durable ownership, status: Planned.
- Raw official Qdrant adapter omits Citadel authorization and provenance contracts, owner: architect, impact: private seat data can enter an unscoped candidate set and provider outages can look empty, mitigation: audited patch based on the exact official source, physical per-dataset collections, and adversarial real-server tests, status: Blocked.

## Checklist
- [x] Contracts current for implementation. CITADEL-INT-QDRANT-01 records candidate B as active and candidate A as the automatic fallback on any isolation failure.
- [x] Deployment route decided. VERIFIED: `docs/decisions.md` DEC-2026-08-08-02 records the self-hosted portable shadow boundary and keeps managed Qdrant outside the first proof.
- [x] Proposed architecture decisions are recorded in `.local-review/research/target-architecture.md`.
- [x] Blockers owned. BLK-2026-08-07-01 is recorded in `agents/blockers.md`.
- [x] Verification recorded. Research evidence remains in the verification reports; checkpoint evidence is recorded above.
- [x] Handoff written. Resume from `status.md` and `.local-review/wayfinder/citadel-v0-5-release-map.md`.

## Lifecycle implementation audit: 2026-08-09

Owner: architect
Status: In Progress

- VERIFIED: current `Citadel.ingest` and `CogneePublicClient.remember` persist Cognee source before the content-free JSON cognify queue. Evidence: candidate `kb/service.py:70-144`, `kb/cognee_client.py:701-806`, and `kb/cognify_queue.py:1-9`.
- VERIFIED: Cognee `1.4.1` accepts an explicit `DataItem.data_id`, and Citadel already preserves that field while attaching metadata. Evidence: candidate `.venv/lib/python3.12/site-packages/cognee/tasks/ingestion/data_item.py:6-10` and `kb/cognee_client.py:659-680`.
- CORRECTED: v1 receipts are `relational`, `vector`, and `graph`, not PostgreSQL, Qdrant, and graph. Provider identity belongs in receipt metadata so SQLite Lite and later PostgreSQL share one state machine.
- INFERRED: dedicated Citadel SQLite with retained bytes, source head, job, and initial receipts in one transaction is the simplest v0.5 boundary. Deterministic Cognee data IDs are the proposed retry seam after an uncertain provider return.
- VERIFIED: detailed evidence, file scope, worker sequence, API surface, backup impact, and stop conditions are indexed in `.local-review/research/lifecycle-implementation-audit.md`.
- Next: approve `CITADEL-INT-LIFECYCLE-01` and `CITADEL-INT-RETRIEVAL-01` with `yes, implement lifecycle v1`. Schema work remains paused until that approval.

## 2026-08-09 Citadel v0.5 local runtime gate
Owner: coordinator
Status: In Progress

### VERIFIED evidence
- `./.venv/bin/ruff check .` in `/private/tmp/citadel-v050-qdrant`: `All checks passed!` and exit `0`.
- Focused version and MCP suite: `72 passed in 2.73s`.
- Cognee and Lite suite: `94 passed, 10 warnings in 10.90s`.
- Local deploy suite: `6 passed in 0.14s`.
- Secure HTTP and status suite: `59 passed in 0.26s`.
- `citadel deploy local` rebuilt the source image and returned `ok: true`, `created: false`, generation `citadel-local-6e7632d3e6434e34931c49768ad62301`, and authenticated readiness `ok: true`.
- Authenticated `citadel status --node-url http://localhost:18000 --no-search --no-recent --json` returned `healthy: true`, admin read/write/admin capabilities, and corpus `0 indexed / 0 tracked`.
- MCP `initialize` returned protocol `2025-06-18`, server `Citadel Archive`, version `0.5.0`.
- Provider smoke generation `provider-smoke-a271342180424218be6d48ea781dbbe3` used one raw UUID in `seat:alice` and `seat:bob`. Exact count was `1` per dataset. Retrieve, search, and exhaustive scroll returned only the dataset's own payload.
- Both Citadel and Qdrant containers restarted. Citadel health returned `healthy`; the same provider receipt passed all read checks after restart and again after the v0.5.0 image replacement.
- SQLite online backup and additive restore returned `integrity: ok` with matching logical SHA-256 `ba62674a3bee014289f53ba5c47b4dd42ac15472cbbf7564bc4ab42df5d2214c`.
- Qdrant snapshot SHA-256 `56b2819fc4ee04d83f52f093e66235537077ee7b121908e80f718831c119d942` restored `2` exact rows into additive collection `citadel_g_f838a38a96db_DocumentChunk_4a0474601d-restore-c1ec593d`.

### Blind spots and next action
- Search was not probed through `citadel status`; provider search was verified directly through `CitadelQdrantAdapter`.
- The isolated worktree lacks the local pre-push hook. This does not measure container health.
- LLM-backed ingest, cognify, and user-facing search were not run. Runtime uses placeholder `LLM_API_KEY=local-no-model-call-key`; an external model call needs explicit approval and a valid configured key.
- Next: release owner runs one authenticated ingest, cognify, and search gate after user approval, then records release readiness. No Railway deploy, merge, production mutation, or data deletion is approved.

## 2026-08-09 Citadel v0.5 release acceptance attempt
Owner: release
Status: In Progress

### VERIFIED evidence
- Interactive shell exposes `OPENROUTER_API_KEY`; its value was not printed or written to tracked files.
- Disposable Citadel service was recreated with that value mapped to `LLM_API_KEY`; SQLite and Qdrant volumes were preserved.
- Container identity returned Citadel `0.5.0`; authenticated CLI status returned `healthy: true` with admin read/write/admin capabilities.
- Provider identity: `LLM_PROVIDER=custom`, `LLM_ENDPOINT=https://openrouter.ai/api/v1`, `LLM_MODEL=openrouter/deepseek/deepseek-v4-flash`, `EMBEDDING_PROVIDER=fastembed`, `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`, dimensions `384`.
- OpenRouter models endpoint returned HTTP `200` in `158 ms` from the container. Network reachability is verified only for that endpoint.
- Key-shape check returned `openrouter_prefix: false` and `minimum_length: false`; the configured value does not match the expected OpenRouter key shape. This check does not reveal the key.
- Approved authenticated ingest with inline cognify returned exit `1`, HTTP `500`: `LLM connection test timed out after 30s. Check that your LLM endpoint is reachable and responding.`
- Post-failure status returned `0 indexed / 0 tracked`. Exact-marker search returned `max_term_coverage: 0.0` and `no_lexical_match: true`; the one nearest-vector result had empty text and did not prove ingestion.

### Release handoff
Task ID: CITADEL-V050-ACCEPTANCE
From owner: release
To owner: release
Status: In Progress
Scope: disposable local Citadel Lite runtime only
Files changed: `status.md`
Interfaces changed: None
Decisions recorded: None
Verification command and result: authenticated `citadel ingest ... --json` returned HTTP `500`; acceptance did not pass
Known blockers: valid OpenRouter API key is not available to the runtime
Next action: replace the interactive shell `OPENROUTER_API_KEY` with a valid `sk-or-v1-...` key, then recreate only the Citadel service and rerun one unique-marker ingest, cognify, and exact search

### 2026-08-09 release acceptance continuation
- Owner: release
- Status: In Progress
- VERIFIED: interactive-shell `OPENROUTER_API_KEY` remains present but fails both expected-shape checks: `openrouter_prefix: false`, `minimum_length: false`.
- VERIFIED: assignment source is `/Users/sarthiborkar/.zshrc:145`; assignment value was not printed or copied.
- Next: owner updates that shell assignment with a valid OpenRouter key and replies `ready`; release reruns the bounded local acceptance gate.

### 2026-08-09 release acceptance blocked
- Owner: release
- Status: Blocked
- VERIFIED: third consecutive interactive-shell check returned `present: true`, `openrouter_prefix: false`, `minimum_length: false`.
- Blocking condition: `/Users/sarthiborkar/.zshrc:145` does not provide a valid OpenRouter credential, so authenticated model-backed cognify cannot be verified.
- Resume condition: owner installs a valid `sk-or-v1-...` key at that assignment, runs `source ~/.zshrc`, and replies `ready`.

## Release acceptance handoff: 2026-08-09

Owner: release
Status: Completed
Scope: Local Citadel v0.5 acceptance and approved Railway credential copy. No deployment was performed.

### Completed

- VERIFIED: Copied only `LLM_API_KEY` and `OPENROUTER_API_KEY` from Railway project `Citadel`, service `Citadel-Archive`, production environment, to project `Citadel (qdrant)`, service `@citadel/web`, production environment. Railway CLI used `variables --skip-deploys`.
- VERIFIED: Source and target comparisons returned `LLM_API_KEY_equal=true` and `OPENROUTER_API_KEY_equal=true`. Both target values returned `present=true` and `openrouter_shape=true`. Secret values were not printed or written to this file.
- VERIFIED: Target deployment remained `36d65cbf-3699-4ba8-bedf-daffb9002e4a`, status `FAILED`, created `2026-08-08T07:12:40.667Z`, stopped `true`. The credential copy did not trigger a deployment.
- VERIFIED: Fixed Cognee 1.4.1 embedding probe construction without weakening Qdrant data-operation dataset enforcement. Command result: `21 passed, 10 warnings in 22.31s`.
- VERIFIED: Fixed Cognee 1.4.1 access-controlled CHUNKS response handling. Citadel now unwraps dataset envelopes and preserves nested chunk payload text. Command `uv run pytest -q tests/test_cognee_client.py` returned `90 passed, 10 warnings in 13.58s`.
- VERIFIED: Command `uv run ruff check .` returned `All checks passed!`.
- VERIFIED: No-cache local image build completed. Image manifest list: `sha256:1b6f6c7400f0ac66d0271c595a988e11837ee8a701567f11244953f99ec54d12`. Docker Compose reported both `citadel-lite-qdrant-1` and `citadel-lite-citadel-1` healthy.
- VERIFIED: Authenticated ingest accepted marker `citadel-v050-final-acceptance-14948765-cee0-454f-a85b-bf02c47f9360` in dataset `masumi-network`. Add pipeline run `b82739f7-60f3-4d6f-abb3-879711771f01` completed with data ID `d8f127ff-f5fa-55ad-983c-04ee92849a96`.
- VERIFIED: Background cognify pipeline `4f6042fe-0441-5c0c-9423-98cfb6533d84` completed. Runtime log recorded `background cognify finished for datasets ('masumi-network',)`.
- VERIFIED: Authenticated `POST http://localhost:18000/search` returned one result whose `text` contains the full marker. Result ID: `cfc5a400-7ee9-56db-8688-632f4e85bcb4`. The returned text was `citadel-v050-final-acceptance-14948765-cee0-454f-a85b-bf02c47f9360. Citadel Lite release acceptance verifies authenticated HTTP ingestion, synchronous Cognee cognify through OpenRouter, FastEmbed vector indexing, and exact marker retrieval from the dataset-isolated Qdrant provider.`

### Files changed

- VERIFIED: `/private/tmp/citadel-v050-qdrant/kb/qdrant_adapter.py` allows Cognee's unbound embedding probe while data operations remain fail-closed without a dataset.
- VERIFIED: `/private/tmp/citadel-v050-qdrant/tests/test_qdrant_adapter.py` covers the embedding probe and fail-closed data operation invariant.
- VERIFIED: `/private/tmp/citadel-v050-qdrant/kb/cognee_client.py` unwraps Cognee 1.4.1 dataset-scoped search envelopes.
- VERIFIED: `/private/tmp/citadel-v050-qdrant/tests/test_cognee_client.py` covers marker text preservation through the wrapper.

### Files intentionally not touched

- VERIFIED: No production database or Qdrant data was changed.
- VERIFIED: No Railway deployment, restart, merge, publish, or data deletion was performed.
- VERIFIED: No Railway variables other than `LLM_API_KEY` and `OPENROUTER_API_KEY` were copied.

### Residual risk and next action

- VERIFIED: Before the final search fix, the status endpoint reported corpus counts as null and indexes as `warming`, while direct ingest, cognify, indexing, and exact retrieval succeeded. This status behavior was not remeasured after the final fix and remains a separate follow-up.
- Planned: Owner `release`. Review this handoff, then obtain explicit user approval before any Railway deployment.

## GitHub direction and issue sync: 2026-08-09

Owner: coordinator
Status: Completed

- [VERIFIED] GitHub `main` and the public production discovery build both report `cc8fc026297b64f39f387b96e30da63f77ad57fb`, version `0.4.1`.
- [VERIFIED] Final tracker snapshot: 87 issues with 19 open and 68 closed; 171 pull requests with 15 open, 6 closed without merge, and 150 merged.
- [VERIFIED] Focused sync posted 17 issue comments and 7 pull request comments. Issues 122 and 248 were closed as completed. PR 251 was closed as superseded.
- [VERIFIED] Correction comments on issues 128 and 247, plus PR 256, record newer local evidence: Qdrant Lite acceptance passed in the private worktree, but the final adapter and client fixes are absent from remote PR 256 head `420be9de5e5105a45515cc1e849b9b3a5b8a1417`.
- [VERIFIED] Exhaustive snapshot and active dispositions are indexed in `.local-review/GITHUB_INDEX.md`.
- [PLANNED] Owner `release`. Port the final local fixes to PR 256, rerun focused tests and current-head CI, verify readiness status, then run hosted and comparable retrieval gates.
- [VERIFIED] No merge, tag, release, production deployment, restart, or data deletion was performed during this GitHub sync.

## PR 256 takeover review: 2026-08-09

Owner: reviewer
Status: In Progress

- [VERIFIED] Reviewed the release handoff, current contracts, roadmap, draft PR 256, and the private worktree at `/private/tmp/citadel-v050-qdrant`.
- [VERIFIED] The worktree is on `agent/citadel-v050-qdrant` at remote head `420be9de5e5105a45515cc1e849b9b3a5b8a1417`. It has 10 modified tracked files and 13 untracked paths.
- [VERIFIED] Correction: the handoff's four paths describe the final embedding-probe and nested-result fixes. They do not enumerate the full dirty worktree, which also contains Qdrant census, Lite runtime, Compose, local deploy CLI, status transport, version, smoke, and test work.
- [VERIFIED] `uv run pytest -q tests/test_qdrant_adapter.py tests/test_cognee_client.py` returned `111 passed, 10 warnings in 11.29s`.
- [VERIFIED] `uv run pytest -q tests/test_lite_runtime.py tests/test_local_deploy.py tests/test_status.py tests/test_version_identity.py` returned `63 passed, 10 warnings in 5.12s`.
- [VERIFIED] `uv run pytest tests/ -q` returned `1779 passed, 3 skipped, 11 warnings in 34.67s`. The three skipped tests mean this command did not repeat every live-provider gate.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`. `git diff --check HEAD` returned exit `0`.
- [VERIFIED] `uv build --wheel --sdist` built `citadel_archive-0.5.0` artifacts. The wheel contains `kb/lite_runtime.py`, `kb/local_deploy.py`, and `kb/deploy_assets/docker-compose.yml`.
- [VERIFIED] Docker reports `citadel-lite-citadel-1` healthy and `citadel-lite-qdrant-1` running. This readback does not repeat ingest, cognify, or retrieval.
- [VERIFIED] Draft PR 256 remains open, review-required, and blocked at remote head `420be9de5e5105a45515cc1e849b9b3a5b8a1417`. Its 18 remote checks attest only that head.
- [VERIFIED] Contract state has not caught up with the implementation: `docs/interfaces.md` still marks self-host and Qdrant interfaces `Planned`; `plans/roadmap.md` keeps lifecycle Milestone 1B and portable package Milestone 2 `Planned`; ticket 016 is blocked by the unfinished lifecycle task.
- [INFERRED] Stop decision: do not commit the full dirty worktree as one PR 256 update until the owner chooses whether to split the final adapter fix from deploy work or accepts a combined branch and updates the contract dependency state.
- [PLANNED] Owner `coordinator`. Recommended route: keep PR 256 focused on the adapter and Cognee compatibility fixes, preserve the deploy WIP in this worktree, then open a separate branch after lifecycle contract approval.
- [VERIFIED] No source file, commit, remote branch, pull request, deployment, production service, volume, or data was changed during this takeover review.
