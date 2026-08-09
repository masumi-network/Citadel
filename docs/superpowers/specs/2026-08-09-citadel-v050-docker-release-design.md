# Citadel v0.5 Docker release-readiness design

**Date:** 2026-08-09
**Owner:** coordinator
**Status:** Approved design. Implementation and final release evidence remain in progress.
**Implementation worktree:** `/private/tmp/citadel-v050-qdrant`

Claims use the repository evidence vocabulary. **VERIFIED** means the command ran or the named source was read during this coordination session. **REPORTED** means the user or a delegated reviewer supplied the claim and root has not reproduced the whole claim. **INFERRED** means the design derives the statement from verified inputs. A test result proves only the named command and covered behavior.

## 1. Goal

**REPORTED:** The approved goal is to finish Citadel v0.5 as a portable Docker release candidate using Cognee `1.4.1`, Qdrant `1.19.0`, SQLite, Ladybug, and lifecycle v1. Every core function must pass inside the production Docker Compose shape while Citadel and Qdrant logs are followed continuously.

**REPORTED:** The functional proof starts with a deterministic seed. It then runs a live GitHub Central sync through the same gates. Search must be exercised through HTTP, CLI, and MCP. Later gates cover graph construction, capture, shared session traces, seat isolation, promotion, restart, outage, stress, backup, and restore.

**REPORTED:** After local gates, root may update PR 256 and perform evidence-backed GitHub issue or pull request maintenance. Merge, tag, release publication, deployment, Railway mutation, production mutation, and production data deletion remain outside this design and require a later approval.

**INFERRED:** “Release” in this document first means an immutable local release candidate. Public v0.5 publication happens only after benchmark evidence, independent review, current-head CI, and a separate release approval.

## 2. Approved decisions

1. **REPORTED:** Use a staged, immutable release candidate. Do not test a moving image across acceptance phases.
2. **REPORTED:** Run deterministic seed acceptance before live GitHub sync. This gives exact expected markers and a smaller failure surface.
3. **REPORTED:** Import only an allowlisted set of Railway variables. Add another variable only when a Docker gate proves it is required. Do not copy the whole production environment.
4. **REPORTED:** Docker proves packaging, runtime wiring, persistence, and reproducibility. Docker does not prove that a model is better.
5. **REPORTED:** Keep the current model only when the frozen same-corpus quality gate passes. A quality failure blocks v0.5 for a stronger-model comparison. Docker latency alone never selects a model.

## 3. Runtime architecture

### 3.1 Compose topology

**VERIFIED:** `docs/interfaces.md` CITADEL-INT-SELFHOST-01 and DEC-2026-08-08-07 define SQLite Lite as one host, one Citadel application process, one scheduler, and zero replicas. SQLite plus Ladybug plus Qdrant is not accepted as a multi-replica topology.

**INFERRED:** The v0.5 Compose project contains:

- One Citadel application container built from the exact candidate source.
- One pinned Qdrant `1.19.0` container at digest `sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`.
- An application volume containing Cognee SQLite, lifecycle SQLite, Ladybug, retained source data, and runtime configuration.
- Separate Qdrant primary-data and snapshot volumes.
- A private Compose network. Qdrant is not publicly exposed. Loopback-only diagnostics may use plain HTTP in a disposable test.

**INFERRED:** Citadel is the only public authorization boundary. Direct Qdrant credentials are operator credentials and bypass Citadel dataset authorization, so they never reach ordinary clients.

### 3.2 Source and projection flow

**VERIFIED:** CITADEL-INT-LIFECYCLE-01 defines one authorized dataset before storage, retained source bytes or a Citadel-owned durable content reference, one immutable `SourceRevision`, one generation-bound `ProjectionJob`, and one receipt for each required backend.

**INFERRED:** The write flow is:

1. Authenticate the caller and resolve exactly one writable dataset.
2. Retain the accepted bytes and atomically write the source revision plus initial projection job.
3. Acquire the single writer lease under an immutable generation and dataset scope.
4. Project to the relational, vector, and graph backends.
5. Mark each receipt `searchable` only after a bounded read check proves that backend's output can be read.

**VERIFIED:** A provider call returning successfully is not whole-job success. One backend cannot overwrite another backend's failed or stale receipt.

### 3.3 Retrieval flow

**VERIFIED:** CITADEL-INT-RETRIEVAL-01 resolves the authenticated identity, authorized datasets, generation, and retrieval profile before provider access. Managed hits must bind to the current source head and a searchable projection receipt.

**INFERRED:** HTTP, CLI, and MCP use the same Citadel retrieval boundary. Surface-specific formatting may differ, but dataset scope, generation, source identity, receipt identity, result identity, and error classification must agree.

**VERIFIED:** Empty success and provider failure are different contracts. A missing dataset may return an honest successful result with count `0`. Permission, timeout, malformed response, or provider failure remains a typed failure and must not become an empty list.

### 3.4 Visibility and promotion

**VERIFIED:** Current MCP and onboarding source define three reader-visible areas: Central, shared `session-traces`, and the caller's `seat:<slug>` Node. Seat-writer MCP tokens write personal notes only to their own Node. Central is read-only from seat MCP.

**INFERRED:** The release isolation invariant is:

- Every authenticated seat may read Central.
- Every authenticated seat may read approved shared session traces as reference-only material.
- A seat may read and mutate only its own Node.
- A shared session action stores a distilled trace in `session-traces` and a private Node copy only after user approval.
- Promotion creates a curated Central copy only after an authorized admin approval. Seat tags alone never promote content.

## 4. Immutable generation and recovery

**VERIFIED:** CITADEL-INT-GENERATION-01 defines an immutable generation identity containing the configuration digest, relational namespace, Qdrant collection identity, graph namespace, model, dimensions, chunk budget, Cognee version, and adapter version.

**INFERRED:** One release run uses one generation. Seed, GitHub, seats, traces, promotion, benchmark, restart, outage, and restore evidence all name that generation. A test that silently creates another generation cannot satisfy the release gate.

**VERIFIED:** CITADEL-INT-BACKUP-01 defines one offline whole-generation cut. It includes Cognee SQLite, lifecycle SQLite, Ladybug, retained data, and every Qdrant collection in the generation. Restore accepts downloaded artifacts only and targets an empty application root plus an empty Qdrant instance.

**INFERRED:** Recovery acceptance requires all of the following:

- Reject a wrong generation before any write.
- Reject symlinked or nested destinations before any write.
- Use private directory and file modes.
- Roll back application and Qdrant state after any partial recovery or count mismatch.
- Boot the exact candidate image from restored state and retrieve exact seed, lifecycle, Central, seat, and session-trace markers.

**REPORTED:** An adjacent unkeyed manifest checksum detects accidental corruption only. Authenticated artifact integrity needs a key, signature, or trusted out-of-band digest and is not silently added under the current backup interface. The final release review must state this residual boundary.

## 5. Execution sequence

### Ring 0: integrate release blockers

**VERIFIED:** The missing-dataset regression is green in the Docker test image with `3 passed, 93 deselected in 0.16s`, but the production candidate image has not been rebuilt for its black-box status proof.

**VERIFIED:** Wrong-generation, partial failure, and count-mismatch backup regressions returned `3 passed, 1 warning in 0.68s`. The real Qdrant rollback regression returned `1 passed, 5 warnings in 0.87s`. Destination containment, private modes, base-install CLI handling, successful restored boot, and exact-marker retrieval remain open.

**VERIFIED:** The current workflow still uses the incomplete `container-smoke` job. It omits `CITADEL_QDRANT_SERVER_IMAGE`, disables lifecycle, and lacks the dedicated Docker test target required by `tests/test_docker_workflow.py`.

**INFERRED:** Ring 0 completes when search, backup containment, non-root image execution, stress acceptance, and the Docker test target are integrated and green inside Docker.

### Ring 1: deterministic seed proof

**INFERRED:** Create one versioned seed manifest containing unique markers for Central, two seats, shared session traces, capture, promotion, lifecycle, and backup. Record expected source, projection job, receipt, document, chunk, graph, and visibility counts.

**INFERRED:** Run graph construction, capture, ingest, operation polling, and exact search through HTTP, CLI, and MCP. Repeat duplicate submission to prove idempotency. Test both empty search and populated exact-marker search.

### Ring 2: persistence and failure proof

**INFERRED:** Restart Citadel and replace Qdrant while preserving named volumes. Inject Qdrant outage during read and write paths. Require typed failure, bounded recovery, queue convergence, unchanged dataset scope, and exact-marker retrieval after recovery.

**INFERRED:** Run whole-generation backup and restore into fresh volumes. Boot the same image against restored state and repeat the seed search and census.

### Ring 3: live GitHub Central proof

**REPORTED:** Import the approved Railway variable allowlist first. Expand it only after a named gate fails because a required value is missing. Do not print secret values in commands, logs, receipts, status files, or artifacts.

**INFERRED:** Run GitHub sync into Central only. Require source census, projection census, current-head receipts, graph presence, exact frozen-answer retrieval, restart persistence, and zero seat leakage. A top-k search cannot substitute for the source or projection census.

### Ring 4: traces, seats, promotion, and benchmark

**INFERRED:** Import or seed one seat at a time. After each seat, run same-ID and distinct-ID search, direct retrieve, hydrate, count, scroll, delete, prune, graph aggregation, CLI, and MCP checks. Do not start the next seat until the current seat has zero visibility violations.

**INFERRED:** Share a distilled session trace, prove reference-only trust, prove private Node copy, and prove another seat cannot read the private copy. Create one promotion candidate, require explicit admin approval, then prove its Central copy carries promotion metadata and the source Node remains correctly scoped.

**INFERRED:** Run the frozen benchmark against the immutable candidate and its same-corpus baseline. Apply the quality and latency decisions in section 7.

### Ring 5: adversarial review and PR integration

**REPORTED:** Run separate fresh-context Fresh Eyes Corroborate, Fresh Eyes Refute, and Red Team reviews after all integrated Docker gates pass. Reviewers are read-only.

**INFERRED:** Root reproduces every reported P0 or P1 before recording it as VERIFIED. Each reproduced finding gets a fresh bounded implementer, a Docker red test, the smallest source fix, a Docker green test, and a rerun of affected Compose gates.

**REPORTED:** Commit sequentially with `sarthib7 <sarthiborkar7@gmail.com>`. After local evidence is complete, update PR 256 and wait for current-head CI. Evidence-backed PR cleanup may follow. No merge, tag, publication, deployment, Railway mutation, or production mutation occurs.

## 6. Docker acceptance matrix

Every row runs inside Docker. Citadel and Qdrant logs remain attached from before startup until after shutdown. A green unit suite does not replace the black-box rows.

| Gate | Required evidence | Failure condition |
|---|---|---|
| Image identity | Build ID, image digest, Cognee `1.4.1`, Qdrant `1.19.0`, dependency check, non-root UID | Version drift, broken dependency, root runtime |
| Startup and auth | Health and readiness, expected `401`, `403`, and authenticated `200` | Unauthenticated success, wrong role accepted, unready provider reported ready |
| Lifecycle ingest | Source revision, job, three receipts, bounded searchable read-back | Accepted source without job, missing receipt, stale generation, retry divergence |
| Search surfaces | Exact marker and empty success through HTTP, CLI, MCP | Provider error becomes empty, surface identities disagree, marker missing |
| Graph | Dataset-scoped presence and expected seeded relationships | Global graph read used as a source receipt, foreign seat data visible |
| Capture and traces | Loopback capture, distilled trace, private Node copy, shared reference-only copy | Raw transcript leak, missing approval, wrong dataset |
| Seat isolation | Same-ID and distinct-ID read and mutation matrix across two seats | Any cross-seat hit or mutation, missing scope reaching Qdrant |
| Promotion | Pending, approve, Central copy, metadata, source isolation | Automatic tag promotion, non-admin decision, missing attribution |
| Persistence and outage | App restart, Qdrant replacement, Qdrant outage and recovery, queue drain | Lost marker, incorrect empty result, stuck lease, mixed generation |
| Backup and restore | Offline manifest, every provider, fresh targets, exact restored boot | Wrong generation accepted, partial residue, missing provider, marker loss |
| Stress and resources | At least one `200`, exact marker on every `200`, zero `5xx`, valid `Retry-After` on every `429`, p50 and p95, resource samples | All `429` passes, any transport error, marker mismatch, severe log line |
| Static and test gates | Nonzero pytest collection, full non-live suite, Ruff, live Qdrant adapter, lifecycle, backup suites | Zero collection, skipped required live gate, host pytest used |

## 7. Benchmark and model decision

**VERIFIED:** `citadel bench` uses the frozen question pin, corpus content fingerprint, ground-truth fingerprint, and first-attempt quality. Repeats feed latency and hit stability only. `citadel bench enforce` rejects incomparable fingerprints, quality regressions, nonzero unreachable chunk count, and conditioned tail recall below `1.0`.

**VERIFIED:** The harness documents client round-trip p50 and p95 as informational for retrieval quality. It does not enforce a latency threshold.

**REPORTED:** The approved v0.5 benchmark adds these release decisions:

1. Zero visibility violations, transport errors, provider errors hidden as empty, and underfilled successful responses.
2. Frozen-answer quality must meet the existing `citadel bench enforce` gate and must not lose an exact answer that the same-corpus baseline returned.
3. Docker p95 must be no more than `20%` above a same-machine, same-corpus, same-question, same-repeat baseline. This is a packaging and performance gate separate from quality.
4. If quality fails, v0.5 blocks for a stronger embedding or reranking model comparison using the same corpus and frozen questions.
5. If only Docker p95 fails, investigate runtime, indexing, resource, or network overhead first. Do not select a new model from latency alone.

**INFERRED:** A valid benchmark pair must name the same corpus fingerprint, question pin, ground-truth fingerprint, retrieval profile, generation, model, dimensions, chunk budget, machine, Docker resource settings, warm-up, and repeat count. If any required identity differs, the delta is not accepted as a model or Docker comparison.

## 8. Error handling and log gate

**VERIFIED:** CITADEL-INT-QDRANT-01 requires provider exceptions to remain typed. Missing, empty, conflicting, or multi-dataset scope must fail before a Qdrant request.

**INFERRED:** Each functional phase classifies both logs for unexpected warning, error, panic, fatal, OOM, corruption, failure, recovery shortfall, and non-2xx data-plane responses. Expected `401`, `403`, injected outage responses, and loopback TLS-disabled notices are recorded separately and do not pass through the unexpected-error count.

**INFERRED:** Log silence is not proof that a feature ran. Every log classification is paired with a functional receipt, exact marker, count, or provider request that proves the phase executed.

**INFERRED:** Any unexplained severe line, unexpected non-2xx provider response, collection recovery below 100 percent, or missing phase evidence fails the current ring. The runtime operator preserves containers and logs for diagnosis instead of rerunning over the evidence.

## 9. GitHub integration and issue policy

**VERIFIED:** The latest root read returned `19` open issues and `16` open pull requests. No open item had a milestone. This count is a snapshot and must be refreshed before external changes.

**REPORTED:** The read-only audit classified PR 256 as the sole v0.5 integration candidate. PR 245 contains relevant v0.5 behavior that must be reconciled. PRs 246, 254, and 255 are closure candidates only after their replacement is visible in PR 256. No issue currently meets full closure criteria.

**INFERRED:** GitHub work follows this order:

1. Finish local Docker evidence and independent reviews.
2. Commit the implementation delta sequentially with the configured identity.
3. Update PR 256 with the reviewed commits and an evidence-based description.
4. Wait for checks on the new PR head. Old green checks do not attest the new head.
5. Refresh every open issue and pull request. Close only items whose acceptance criteria are now met or whose replacement is visible and linked. Record a correction when an earlier claim became stale.

**REPORTED:** Merge, tag, release publication, deployment, Railway mutation, production mutation, and production data deletion remain separate confirmation gates.

## 10. Orchestration and ownership

**VERIFIED:** `agents/model-routing.md` indexes task model, effort, dependencies, file ownership, linked blocker, and acceptance command.

**INFERRED:** Root owns the goal, plan, contracts, task allocation, shared records, Docker resource ownership, evidence validation, integration order, commits, GitHub confirmation gates, and final release stop.

**INFERRED:** Fresh-context `gpt-5.6-sol` agents own difficult P0 or P1 fixes, architecture, security, Fresh Eyes, and Red Team. Fresh-context `gpt-5.6-terra` agents own bounded Docker infrastructure, runtime operation, mechanical verification, and tracker refresh.

**INFERRED:** Only one runtime agent may mutate Docker. That agent follows both provider logs. Other reviewers remain read-only. No two implementers edit the same file at the same time.

## 11. Estimated execution window

**INFERRED:** If the current four local blockers need only their already-defined fixes, Ring 0 and Ring 1 should take about one focused day. Full persistence, outage, restore, GitHub sync, seat, trace, promotion, and benchmark gates should take another one to two days. Independent reviews, reproduced fixes, PR update, and current-head CI should take about one day if no new P0 or P1 appears.

**INFERRED:** A quality failure that requires a model comparison adds at least one day because every candidate must use the same corpus, frozen questions, generation contract, and three search surfaces. A backup containment or isolation failure can add more because release evidence must be rerun after the fix.

## 12. Release-ready exit

Citadel v0.5 is release-ready only when every item below has current evidence:

- The exact candidate image and Compose bundle pass the full Docker acceptance matrix.
- Deterministic seed and live GitHub Central pass the same source, projection, graph, retrieval, restart, and recovery contracts.
- HTTP, CLI, and MCP return compatible identities and exact answers.
- Shared traces, two-seat isolation, and promotion pass with zero visibility violations.
- Whole-generation backup restores into fresh storage and the exact image retrieves every required marker.
- Frozen quality and the separate Docker p95 gate pass, or a stronger model wins the same-corpus comparison and the full affected matrix is rerun.
- Fresh Eyes Corroborate, Fresh Eyes Refute, and Red Team leave no unresolved reproduced P0 or P1.
- PR 256 contains the reviewed commits and current-head CI is green.
- Root has recorded every remaining blind spot and asks for release approval before merge, tag, publication, deployment, Railway mutation, or production mutation.

Until all items pass, the goal remains active and no release-ready claim is made.
