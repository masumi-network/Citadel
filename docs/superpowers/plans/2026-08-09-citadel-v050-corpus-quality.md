# Citadel v0.5 corpus, isolation, promotion, and quality plan

> **REQUIRED SUB-SKILL:** Execute this plan with `superpowers:subagent-driven-development`. Root owns contracts, Railway secret selection, Docker ownership, benchmark comparability, and GitHub confirmation gates.

**Goal:** Sync live GitHub content into Central, prove current-head projection coverage, prove two-seat and session-trace isolation, require admin-approved promotion, and compare the immutable Docker candidate against a same-corpus baseline.

**Architecture:** Ring 3 adds live GitHub content only after deterministic seed and recovery pass. Ring 4 operates one seat at a time and treats Citadel, not direct Qdrant credentials, as the authorization boundary. Benchmark quality and Docker p95 are separate gates with one strict comparison identity.

**Tech Stack:** Citadel lifecycle v1, GitHub API, Qdrant 1.19.0, SQLite, Ladybug, HTTP, CLI, MCP, Docker Compose, retrieval benchmark harness.

**Global Constraints:**

- Worktree: `/private/tmp/citadel-v050-qdrant`.
- Rings 0 through 2 must pass first.
- All tests, syncs, probes, and benchmarks run inside Docker.
- The same runtime owner retains Docker mutation and both log streams.
- Pull only an allowlisted Railway secret. Never print or persist a secret in tracked files or evidence.
- Start GitHub sync without a token. Add only `CITADEL_GITHUB_TOKEN` after the named gate proves unauthenticated access is insufficient.
- Central dataset is `masumi-network`. Seats are `seat:alice` and `seat:bob`. Shared traces use `session-traces`.
- A top-k search is never a census.
- Direct Qdrant adapter checks prove provider scoping, not end-user authorization. HTTP, CLI, and MCP prove the Citadel boundary.
- Every Central promotion waits for explicit admin approval.
- Do not mutate Railway, production, or GitHub in this plan.

## Task 1: Pin Ring 3 and Ring 4 contracts

**Owner:** architect

**Files:** tracking `docs/interfaces.md`, `docs/decisions.md`

**Dependency:** Compose runtime plan contracts are current.

1. Extend `CITADEL-INT-LIFECYCLE-01` with current-head evidence. Given a dataset and exact source keys, the result returns each current source revision, its current-generation job, three receipts, and searchable state. Missing source, job, receipt, generation mismatch, or stale head is an explicit failure.

2. Extend `CITADEL-INT-RETRIEVAL-01` with canonical parity fields:

```text
result_id
document_id
dataset
generation_id
source_revision_id
projection_receipt_id
content_sha256
trust_tier
```

HTTP, CLI, and MCP must agree for the same exact hit. Response presentation may differ.

3. Extend the promotion contract. Every qualifying item enters `pending_approval`. A Central write occurs only after an admin with `sources:sync` approves it. The promoted copy records pending item ID, source dataset, source content SHA-256, approver identity, and approval timestamp.

4. Record the benchmark decision: the preserved previous candidate image is the first baseline attempt. Both baseline and candidate receive independent restored copies of the same completed corpus. One fixed client image runs both benchmarks. If the preserved image cannot boot that state, the comparison is not valid and the release remains blocked until a reproducible baseline is created.

## Task 2: Pass optional GitHub inputs through the portable Compose path

**Owner:** implementer `CITADEL-GITHUB-INPUTS-01`

**Files:** `docker-compose.yml`, `kb/deploy_assets/docker-compose.yml`, `.env.lite.example`, `kb/local_deploy.py`, `tests/test_compose_contract.py`, `tests/test_local_deploy.py`, `tests/test_docker_workflow.py`

**Dependency:** Compose snapshot commit.

1. Add red tests that require:

- Both Compose files pass `CITADEL_GITHUB_TOKEN` without a default value.
- Both pass nonsecret `CITADEL_GITHUB_ORG`, `CITADEL_PROMOTION_ENABLED`, and `CITADEL_PROMOTION_DRY_RUN` with safe defaults.
- `_new_environment()` copies `CITADEL_GITHUB_TOKEN` only when present.
- An unrelated process environment variable never enters generated `.env`.
- Generated `.env` remains mode `0600`.

2. Run red:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_compose_contract.py tests/test_local_deploy.py tests/test_docker_workflow.py \
  -k 'github or promotion or environment_allowlist'
```

3. Add explicit Compose entries:

```yaml
CITADEL_GITHUB_TOKEN: ${CITADEL_GITHUB_TOKEN:-}
CITADEL_GITHUB_ORG: ${CITADEL_GITHUB_ORG:-masumi-network}
CITADEL_PROMOTION_ENABLED: ${CITADEL_PROMOTION_ENABLED:-false}
CITADEL_PROMOTION_DRY_RUN: ${CITADEL_PROMOTION_DRY_RUN:-true}
```

4. In `_new_environment()`, copy only the optional GitHub token:

```python
github_token = os.getenv("CITADEL_GITHUB_TOKEN", "").strip()
if github_token:
    environment["CITADEL_GITHUB_TOKEN"] = github_token
```

Do not add `GITHUB_TOKEN`, webhook secrets, or arbitrary Railway variables.

5. Rebuild and rerun. Commit:

```bash
git add docker-compose.yml kb/deploy_assets/docker-compose.yml .env.lite.example \
  kb/local_deploy.py tests/test_compose_contract.py tests/test_local_deploy.py \
  tests/test_docker_workflow.py
git diff --cached --check
git commit -m "feat: pass approved GitHub release inputs"
```

## Task 3: Add exact current-head release evidence

**Owner:** implementer `CITADEL-CURRENT-HEAD-EVIDENCE-01`

**Files:** `kb/lifecycle.py`, `kb/service.py`, `kb/release_acceptance.py`, `tests/test_lifecycle.py`, `tests/test_service.py`, `tests/test_release_acceptance.py`, focused sync tests

**Dependency:** Task 1 contract.

1. Add red tests for `LifecycleStore.current_head_evidence(dataset, source_keys)`:

- Returns exactly one current revision and current-generation operation per source key.
- Requires relational, vector, and graph receipts.
- Rejects a stale-generation job, missing receipt, non-searchable receipt, missing key, and tombstoned head.
- Does not accept a historical searchable revision when the current head is unsearchable.

2. Run red:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_lifecycle.py tests/test_service.py tests/test_release_acceptance.py \
  -k current_head_evidence
```

3. Implement the store query using `source_heads`, `source_revisions`, `projection_jobs`, and `projection_receipts`. Compare generation, projection version, and config digest to the active projection identity.

4. Add a bounded `Citadel.lifecycle_current_head_evidence()` wrapper. The release probe reads GitHub daily-digest and repository-content source keys from the two sync state files and requests exact evidence.

5. Rebuild and rerun. Commit:

```bash
git add kb/lifecycle.py kb/service.py kb/release_acceptance.py \
  tests/test_lifecycle.py tests/test_service.py tests/test_release_acceptance.py \
  tests/test_github_sync.py tests/test_repo_content_sync.py
git diff --cached --check
git commit -m "feat: attest current GitHub source heads"
```

## Task 4: Run live GitHub Central sync

**Owner:** runtime, `CITADEL-DOCKER-RUNTIME-01`

**Files:** Docker resources, private env, and ignored evidence only

**Dependency:** Rings 1 and 2 green, Tasks 2 and 3 committed.

1. Keep the exact Ring 2 candidate image, generation, application volume, Qdrant volumes, and log followers.

2. Run an unauthenticated bounded repository-content dry run through the existing application process:

```bash
docker compose -p citadel-v050-r12-src \
  --env-file /private/tmp/citadel-r12.env \
  exec -T citadel python -c 'import json,os; from urllib.request import Request,urlopen; request=Request("http://127.0.0.1:8000/api/repo-content-sync/run",data=b"{\"force\":false,\"dry_run\":true}",headers={"Authorization":"Bearer "+os.environ["CITADEL_ADMIN_KEY"],"Content-Type":"application/json"},method="POST"); print(json.dumps(json.load(urlopen(request,timeout=900)),sort_keys=True))'
```

Record the exit and typed result. This performs GitHub reads but no ingest. If it completes within GitHub limits and reports every required repository readable, do not import another secret. If it reports unauthenticated access, rate limiting, or inability to read a required repository, the named allowlist gate is satisfied.

3. After that failure only, read `CITADEL_GITHUB_TOKEN` from the existing Railway `Citadel` project, `Citadel-Archive` service, production environment into a private `0600` temporary file. Do not print the Railway JSON or token. Merge only that key into the Ring 3 env file. Delete neither Railway nor production state.

4. Recreate only the local Citadel container so it receives the optional token. Preserve its image and app volume. Attach its replacement log follower before start. Do not rebuild.

5. Run the real syncs through the existing Citadel process. The helper process performs HTTP only and never opens SQLite or Ladybug:

```bash
docker compose -p citadel-v050-r12-src \
  --env-file /private/tmp/citadel-r12-ring3.env \
  exec -T citadel python -c 'import json,os; from urllib.request import Request,urlopen; request=Request("http://127.0.0.1:8000/api/github-sync/run",data=b"{\"force\":true}",headers={"Authorization":"Bearer "+os.environ["CITADEL_ADMIN_KEY"],"Content-Type":"application/json"},method="POST"); print(json.dumps(json.load(urlopen(request,timeout=1800)),sort_keys=True))'
docker compose -p citadel-v050-r12-src \
  --env-file /private/tmp/citadel-r12-ring3.env \
  exec -T citadel python -c 'import json,os; from urllib.request import Request,urlopen; request=Request("http://127.0.0.1:8000/api/repo-content-sync/run",data=b"{\"force\":true,\"dry_run\":false}",headers={"Authorization":"Bearer "+os.environ["CITADEL_ADMIN_KEY"],"Content-Type":"application/json"},method="POST"); print(json.dumps(json.load(urlopen(request,timeout=3600)),sort_keys=True))'
```

Require `ok:true`, authenticated operation, zero repository errors, and zero unexplained blocked source. A blocked file is a security result that needs path and scanner category review, not a silent skip.

6. Freeze copies and SHA-256 values of GitHub and repo-content state files. Build the exact expected source-key set from `github:masumi-network:daily-digest` plus every tracked repository-content key. Live GitHub count is intentionally discovered at run time and then frozen. Do not hard-code a stale count.

7. Run `python -m kb.release_acceptance ring3` inside the candidate. Require:

- Every frozen source key has a current head, current-generation job, and three searchable receipts.
- Complete `/api/corpus` pagination with `documents_walked == documents_total`.
- Zero unmeasured chunk counts and zero zero-chunk GitHub documents.
- Every expected GitHub source is `in_graph=true` under dataset `masumi-network`.
- Frozen exact-answer markers resolve through HTTP, CLI, and MCP with canonical identity parity.
- No seat token returns a foreign `seat:*` dataset.

8. Restart Citadel without rebuild. Repeat current-head evidence, census, graph presence, and all three search surfaces.

9. Classify both logs. A silent sync is insufficient. Pair each phase with state SHA, source keys, receipt counts, and exact hits.

## Task 5: Complete the two-seat and shared-trace matrix

**Owner:** implementer for missing tests, then runtime for black-box execution

**Files:** `kb/release_acceptance.py`, `tests/test_release_acceptance.py`, `tests/test_qdrant_adapter_live.py`, `tests/test_cognee_qdrant_sqlite_live.py`, focused server and MCP tests

**Dependency:** Ring 3 green.

1. Add Docker red tests for any missing provider operation: same raw ID and distinct IDs across two datasets, `search`, `retrieve`, `count_data_points`, `scroll_data_points`, `delete_data_points`, and `prune`. Each destructive check uses disposable test collections only.

2. Add black-box visibility assertions:

- Alice may read `seat:alice`, `masumi-network`, and `session-traces`, never Bob Node content.
- Bob may read `seat:bob`, `masumi-network`, and `session-traces`, never Alice Node or capture content.
- A foreign private document hydrate returns `404`, not `403`.
- The shared trace deduplicates to one visible result per caller and reports `trust_tier=reference-only`.
- The Alice private trace copy remains Alice-scoped.
- Graph content follows the same dataset visibility. Org-wide aggregate labels are not treated as content.

3. Run focused Docker tests before and after missing source changes:

```bash
docker run --rm --network citadel-v050-r12-src_default citadel-archive:v050-test \
  python -m pytest -q \
  tests/test_qdrant_adapter_live.py tests/test_cognee_qdrant_sqlite_live.py \
  tests/test_release_acceptance.py tests/test_server.py tests/test_mcp_server.py \
  -k 'same_id or distinct_id or seat_isolation or session_trace'
```

4. Commit only if source or reusable test coverage changed:

```bash
git add kb/release_acceptance.py tests/test_release_acceptance.py \
  tests/test_qdrant_adapter_live.py tests/test_cognee_qdrant_sqlite_live.py \
  tests/test_server.py tests/test_mcp_server.py
git diff --cached --check
git commit -m "test: enforce seat and shared-trace isolation"
```

5. Run `python -m kb.release_acceptance ring4-isolation` inside the production candidate. Provision and complete Alice before Bob. After Bob is added, rerun both directions. Require zero visibility or mutation violations before promotion starts.

## Task 6: Require explicit approval for every promotion

**Owner:** architect, then fresh implementer `CITADEL-PROMOTION-APPROVAL-01`

**Files:** `kb/promotion.py`, `kb/learning.py` only if attestation transport needs it, `tests/test_promotion.py`, `tests/test_server.py`, `tests/test_mcp_server.py`, `kb/release_acceptance.py`

**Dependency:** Task 1 promotion contract and Task 5 isolation green.

1. Add red tests:

- A qualifying `known_org_work` proposal is queued, not written to Central.
- A qualifying `new_org_project` proposal is queued.
- No Central write occurs before approval.
- Seat writers cannot approve or reject.
- Admin approval passes pending item ID into `_promote()`.
- Promoted attestation persists `promotion_id`, `source_dataset`, `source_content_sha256`, `promoted_by`, and `promoted_at`.
- Central search and hydrate return those fields. The source Node remains scoped.

2. Run red:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_promotion.py tests/test_server.py tests/test_mcp_server.py \
  -k 'pending or approval or promotion_id or known_org_work'
```

3. Change `decide()` so every qualifying proposal returns `pending_approval`. Keep reference status and reason, but remove the automatic Central-write decision.

4. Extend `_promote()`:

```python
attestation = {
    "promotion_id": promotion_id,
    "source_dataset": seat_dataset,
    "source_content_sha256": candidate_hash(proposal.candidate),
    "promoted_by": approver_id,
    "promoted_at": now_iso(),
}
```

`approve_pending()` passes `item.id`. An automatic run has no path to `_promote()`.

5. Rebuild and rerun. Commit:

```bash
git add kb/promotion.py kb/learning.py tests/test_promotion.py tests/test_server.py \
  tests/test_mcp_server.py kb/release_acceptance.py
git diff --cached --check
git commit -m "fix: require approval for every Central promotion"
```

Omit `kb/learning.py` when its existing attestation parameter already preserves the new fields.

6. Rebuild the immutable candidate and rerun affected Ring 1 through Ring 3 gates because the candidate SHA changed.

7. In production Compose, enable promotion and disable dry-run through nonsecret local env values. Run promotion for Alice. Require at least one pending item and zero Central write. Attempt approval with Alice and require `403`. Approve one item with admin through MCP, then verify the Central copy and private source through HTTP, CLI, MCP, hydrate, lifecycle evidence, and graph presence.

## Task 7: Extend the release benchmark gate

**Owner:** implementer `CITADEL-BENCH-RELEASE-01`

**Files:** `kb/retrieval_eval.py`, `tests/test_search_bench.py`, `kb/release_acceptance.py`

**Dependency:** Retrieval profile and promotion contracts current.

1. Add a `--release-context` JSON argument to `citadel bench run`. Validate this exact nonsecret shape:

```json
{
  "runtime_id": "sha256:...",
  "docker_resource_digest": "sha256:...",
  "warmup_count": 2,
  "retrieval_profile": "v0.5-default-top10",
  "generation_id": "citadel-v050-ring12-g1",
  "model": "BAAI/bge-small-en-v1.5",
  "dimensions": 384,
  "chunk_budget_tokens": 256
}
```

The runtime ID is a one-way digest of the machine and Docker engine identity, not a raw hostname. The release probe cross-checks generation and config digest against `/api/state` and model plus dimensions against searchable operation receipts before the run.

2. Preserve `build_id` and lifecycle current-generation identity in `api_fingerprint()`. Add release-context fields, repeat count, requested top-k, and benchmark client image digest to `build_fingerprint()`.

3. Make `compare_fingerprints()` reject release comparisons when runtime ID, Docker resources, warm-up, retrieval profile, generation, model, dimensions, chunk budget, question pin, ground-truth SHA, content SHA, repeat count, or client image differ.

4. Treat timeout, truncation, partial response, embedded provider error, malformed success, and transport error as failed attempts. Record requested slots, slots served, underfilled successes, and foreign-dataset hits.

5. Add `citadel bench enforce --release --max-p95-regression-percent 20`. Keep existing generic enforce behavior when `--release` is absent. Release mode requires:

- Zero transport, provider, timeout, partial, malformed, or visibility errors.
- Zero underfilled successful responses when the eligible corpus has at least requested top-k documents.
- No question whose baseline `answer_pass_at_5` is true becomes false.
- Existing aggregate metrics do not regress.
- Candidate chunk-count zero is zero and conditioned tail recall is `1.0`.
- Equal nonzero latency samples and repeat counts.
- Candidate p95 is no more than baseline p95 multiplied by `1.20`.

6. Add red tests for every new failure and a comparable pass. Run:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_search_bench.py \
  -k 'release or p95 or underfilled or answer_preservation or visibility'
```

7. Implement minimally, rebuild, and rerun. Then run the complete benchmark self-check in Docker.

8. Commit:

```bash
git add kb/retrieval_eval.py tests/test_search_bench.py kb/release_acceptance.py
git diff --cached --check
git commit -m "feat: enforce release identity and Docker p95"
```

## Task 8: Run the same-corpus baseline and candidate benchmark

**Owner:** runtime

**Files:** Docker resources and ignored evidence only

**Dependency:** Tasks 4 through 7 green and a new immutable candidate frozen.

1. Resolve the preserved baseline image `citadel-archive:qdrant-core-final3-20260809` to its full image ID. If missing or changed, stop. Do not substitute an unrecorded image.

2. Take one completed Ring 4 whole-generation backup. Restore it twice with the final candidate restore tool into independent fresh app, Qdrant primary, and Qdrant snapshot volumes. Boot one stack with the preserved baseline image and one with the final candidate. Both must report generation `citadel-v050-ring12-g1`, equal content fingerprint, equal lifecycle census, equal model, equal dimensions, and equal chunk budget.

3. Fetch the frozen ground-truth bodies once inside the fixed benchmark client image using the allowlisted GitHub token. The fetch script receives the same value under its required temporary `GITHUB_TOKEN` name only inside that client process. Do not add `GITHUB_TOKEN` to Compose or a tracked file. Save the cache SHA-256. Both runs mount the same read-only cache and the same question file.

4. Normalize Docker CPU and memory settings. Record a SHA-256 over the relevant `HostConfig` fields. Run only one benchmark target at a time. Use the same client image, network path shape, two warm-ups, five measured repeats, and Central-restricted token.

5. Run baseline and candidate:

```bash
citadel bench run --repeats 5 \
  --node-url http://citadel:8000 \
  --release-context /data/release-evidence/release-context.json \
  --repo-state /data/.citadel/repo_content_sync_state.json \
  --out /data/release-evidence/baseline.json
citadel bench run --repeats 5 \
  --node-url http://citadel:8000 \
  --release-context /data/release-evidence/release-context.json \
  --repo-state /data/.citadel/repo_content_sync_state.json \
  --out /data/release-evidence/candidate.json
citadel bench enforce --release --max-p95-regression-percent 20 \
  /data/release-evidence/baseline.json \
  /data/release-evidence/candidate.json
```

Each command executes inside the fixed benchmark client container attached to the target Compose network.

6. Classify both logs and pair every benchmark phase with request counts and run JSON. A p95 result is client round-trip latency for this fixed path, not server execution time.

7. Decision:

- If quality and p95 pass, keep the current model.
- If quality fails, block v0.5. Inspect supported FastEmbed models inside the exact candidate image, select a stronger supported candidate through an architect decision, create a new generation, rebuild the same frozen corpus, and rerun every affected gate.
- If only p95 fails, diagnose Docker resources, indexing, network path, or runtime overhead. Do not select another model from latency alone.

## Task 9: Ring 3 and Ring 4 root gate

**Owner:** coordinator

**Dependency:** Task 8 complete.

1. Validate sync state hashes, source-key manifest, current-head evidence, corpus census, graph presence, canonical parity, seat matrix, shared trace, promotion attestation, benchmark fingerprints, and both log classifications against the exact candidate.

2. Reproduce every reported P0 or P1. Delegate a fresh bounded fix only after reproduction.

3. Update contracts, decisions, blockers, model routing, `status.md`, and the handoff. Correct any earlier count or model claim whose measurement method changed.

4. Hand the immutable candidate and evidence bundle to the separate review plan.

## Plan self-check

- Railway access is read-only and allowlisted. Only a proven-needed GitHub token enters local Docker.
- GitHub counts are discovered and frozen at run time, not guessed in the plan.
- Current-head lifecycle evidence is distinct from corpus or top-k search.
- Seat isolation covers same IDs, distinct IDs, hydrate, graph, CLI, MCP, delete, and prune.
- Promotion cannot reach Central before admin approval.
- Benchmark identity includes corpus, questions, ground truth, generation, model, runtime, resources, warm-up, repeats, and client image.
- Quality failure selects a model comparison. Latency-only failure does not.
