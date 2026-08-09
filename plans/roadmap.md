# Qdrant Migration Roadmap

Last updated: 2026-08-09
Owner: coordinator
Current phase: contract approval and candidate branch sync

## Milestone 0: Lock contracts and source policy

Owner: architect
Status: Completed
Deliverable: Qdrant adapter, authorization, whole-generation, source preservation, and deployment contracts.
Dependency: official Cognee and Qdrant source audit.
Exit criteria: `docs/decisions.md`, `docs/interfaces.md`, `docs/techstack.md`, Wayfinder ticket, blocker state, and research evidence agree on one route.
Verification: VERIFIED on 2026-08-08 for provider and deployment shape. INFERRED: DEC-2026-08-08-06 selects generation collections with dataset-namespaced IDs as the working boundary and physical per-dataset collections as the automatic fallback on any isolation failure.

## Milestone 1: Prove the adapter in disposable storage

Owner: implementer
Status: In Progress
Deliverable: exact Cognee `1.4.1`, official community Qdrant adapter commit, secure cryptography metadata patch, and Citadel provider contract suite.
Dependency: Milestone 0, BLK-2026-08-08-02, and BLK-2026-08-08-03.
Exit criteria: dependency resolution keeps `cryptography==50.0.0`; plain pip, uv, and built-wheel installs pass on Python 3.12; fresh, idempotent, second-process, and injected-failure migration tests pass; force-mode API changes pass; the audited adapter patch passes CHUNKS payload parity, the DEC-2026-08-08-06 tenant boundary, score direction, scoped retrieve, search, batch, delete, prune, scroll, count, typed outage, restart, and snapshot restore against a pinned real Qdrant server; authorized graph aggregation and flat CHUNKS compatibility pass; telemetry and external payload tracing stay disabled.
Verification checkpoint: VERIFIED on 2026-08-09. Local commits `a0d5c02` and `92ce11a`, plus merge commit `f3e92ff`, pass `1781 passed, 3 skipped, 11 warnings in 32.97s` and `uv run ruff check .` returned `All checks passed!`. Real Qdrant same-ID count, retrieve, search, scroll, restart, snapshot restore, authenticated ingest, cognify, and exact-marker retrieval passed in the disposable local stack. Blind spots: the local commits are not on remote PR 256, three skipped tests were not repeated as live gates, and graph aggregation plus every destructive adapter path are not covered by the recorded runtime receipt.
Estimate: less than one day for branch sync and current-head CI, excluding lifecycle work.

## Milestone 1A: Lock retrieval and lifecycle contracts

Owner: architect
Status: In Progress
Deliverable: versioned `SourceRevision`, `ProjectionJob`, `ProjectionReceipt`, `RetrievalCandidate`, `RetrievalHit`, `RetrievalTrace`, and `RetrievalProfile` interfaces.
Dependency: Milestone 0. Adapter observations from Milestone 1 inform compatibility fields, but schema implementation does not start here.
Exit criteria: authorization, provider, lifecycle, score, partial-failure, census, and Cognee CHUNKS compatibility rules are approved; files and tests are named; schema and migration work still require a separate task.
Verification checkpoint: INFERRED working v1 schemas and state rules are recorded in `docs/interfaces.md` under CITADEL-INT-LIFECYCLE-01 and CITADEL-INT-RETRIEVAL-01. Existing ADR-0010, ADR-0021, and ADR-0022 supply the accepted architecture. User approval of the field set, state machine, and file scope remains the exit gate before schema work.
Estimate: under one review session for contract approval, excluding implementation.

## Milestone 1B: Implement durable source and projection lifecycle

Owner: implementer
Status: Planned
Deliverable: append-only source revisions, durable projection jobs, per-backend receipts, bounded operation status, and restart convergence.
Dependency: Milestone 1A.
Exit criteria: fault injection after each durable stage converges in a second process; accepted source and queued work are atomic; SQLite, Qdrant, and graph receipts distinguish completion from searchability; an empty generation rebuild matches the source and projection census.
Estimate: three to five weeks. This remains uncertain until retained-source coverage and schema scope are measured.

## Milestone 2: Build the OCI and Compose portable package

Owner: implementer
Status: In Progress
Deliverable: multi-platform OCI image, Dockerfile, `citadel deploy local` Compose setup, SQLite Lite profile, environment reference, bootstrap command, and one shared smoke test. Keep PostgreSQL as a later optional profile.
Dependency: Milestone 1 and Milestone 1B.
Exit criteria: SQLite Lite boots from empty storage, reports matching build and generation identity, survives restart, restores backups, and passes the authorized write-read smoke test.
Verification checkpoint: VERIFIED on 2026-08-09. Candidate commit `92ce11a` contains the package and local runtime. The wheel and source distribution built, the Compose stack passed local HTTP, CLI, MCP initialization, restart, SQLite backup and restore, Qdrant snapshot restore, and authenticated marker retrieval. The package remains non-release-ready until Milestone 1B closes.
Estimate: less than one day for current-head rebuild and readiness recheck after lifecycle integration.

## Milestone 3: Deploy an empty Railway shadow

Owner: release
Status: In Progress
Deliverable: new Railway Lite project with independent app, SQLite on the app volume, Qdrant, graph storage, secrets, domains, and monitoring.
Dependency: Milestone 2 and current approval to deploy the new project.
Exit criteria: Qdrant `/readyz`, app `/healthz`, app `/api/state`, metrics, snapshot restore, provider-outage behavior, and identity checks pass. Existing production remains unchanged.
Verification checkpoint: VERIFIED on 2026-08-09. The fresh Railway project exists, two approved model-key variables were copied with `--skip-deploys`, and its stopped failed deployment ID remained unchanged. No candidate deployment has run. Deployment still requires explicit user approval after Milestone 1B and local current-head gates pass.
Estimate: about half a day after approval if service provisioning is available.

## Milestone 3A: Prove the DigitalOcean Droplet wrapper

Owner: release
Status: Planned
Deliverable: cloud-init or `doctl` wrapper that installs Docker, pulls the exact OCI image and Compose bundle, persists app and Qdrant state, and runs the shared smoke test.
Dependency: Milestone 2.
Exit criteria: a fresh Droplet boots the SQLite Lite profile, survives reboot, restores an exported backup, and exposes only Citadel through the selected TLS ingress.
Estimate: one day after Compose passes locally. App Platform is excluded because it has no persistent volumes.

## Milestone 4: Bootstrap GitHub, then seats

Owner: release
Status: Planned
Deliverable: Central GitHub corpus followed by one serialized import per seat.
Dependency: Milestone 3 and an approved source-preservation policy.
Exit criteria: GitHub source and projection censuses match; frozen Citadel v5 retrieval gates pass; the deterministic 70-instance LongMemEval retrieval shadow reconciles every input, receipt, and credited evidence coordinate; every seat sees Central, shared traces, and only its own Node; restart and queue drain converge before the next seat starts. LoCoMo remains post-v0.5 research.
Estimate: one to three days, dominated by cognify time and seat count.

## Milestone 5: Cut over and retain rollback

Owner: release
Status: Planned
Deliverable: client cutover to the new generation, old production retained read-only, release artifacts published after approval.
Dependency: Milestone 4, live CLI and MCP E2E, P0 and P1 disposition, and release approval.
Exit criteria: no visibility violations, complete source-to-search receipts, accepted retrieval threshold, one successful snapshot-restore cycle, and documented rollback. Old data deletion remains a separate approval.
Estimate: at least one monitored release cycle plus the chosen rollback window.
