# Citadel v0.5 Compose runtime implementation plan

> **REQUIRED SUB-SKILL:** Execute this plan with `superpowers:subagent-driven-development`. Root serializes shared contracts and overlapping files. One runtime owner runs Docker and follows all Citadel plus Qdrant logs.

**Goal:** Prove one deterministic generation through seed, lifecycle, HTTP, CLI, MCP, graph, capture, restart, Qdrant replacement, provider outage, convergence, stress, whole-generation backup, and same-image restore.

**Architecture:** A versioned seed fixture drives all surfaces. Every accepted source produces one immutable source revision, one generation-bound job, and three backend receipts. Actual empty search remains HTTP `200`. Timeout and Qdrant failure become typed failures. A separate Qdrant snapshot volume joins the existing application and primary vector volumes.

**Tech Stack:** Python 3.12, FastAPI, MCP Streamable HTTP, Cognee 1.4.1, SQLite, Ladybug, Qdrant 1.19.0, Docker Compose, pytest.

**Global Constraints:**

- Worktree: `/private/tmp/citadel-v050-qdrant`.
- Ring 0 must be committed first.
- Root updates `docs/interfaces.md` before Tasks 1 through 3 because they change cross-boundary responses and storage.
- Every Python test, CLI probe, API probe, MCP probe, and benchmark runs in Docker.
- One runtime owner mutates Docker. Other agents edit source or review read-only.
- Attach timestamped log followers before each application or Qdrant container starts. A replaced container gets a new follower before start.
- Use one generation ID for source and restored stacks: `citadel-v050-ring12-g1`.
- Keep LLM enrichment off for the deterministic seed. Freeze current embedding model and chunk budget.
- Do not infer a semantic graph total. Exact `/api/corpus` rows prove DocumentChunk graph presence. The small seed graph also proves named structural edges through `/api/mesh/graph`.
- No GitHub writes, Railway mutation, deployment, public release, merge, or tag in this plan.

## Task 1: Define typed retrieval failure contracts

**Owner:** architect, then fresh implementer `CITADEL-RETRIEVAL-ERRORS-01`

**Files:** tracking `docs/interfaces.md`; implementation `kb/server.py`, `kb/cli.py`, `kb/mcp_server.py`, focused adapter code only if provider classification needs it; `tests/test_server.py`, `tests/test_cli_commands.py`, `tests/test_mcp_server.py`, `tests/test_qdrant_adapter.py`

**Dependency:** Ring 0 search-empty commit.

1. Add this response contract to `CITADEL-INT-RETRIEVAL-01` before source edits:

```json
{
  "detail": {
    "code": "SEARCH_TIMEOUT",
    "message": "Search exceeded the configured time budget.",
    "retryable": true
  }
}
```

HTTP status is `504`. A reachable Citadel node with an unavailable Qdrant returns the same shape with HTTP `503` and code `QDRANT_UNAVAILABLE`. A healthy provider with no data returns HTTP `200`, `results: []`, no error code.

CLI JSON mirrors `{"ok":false,"code":...,"http_status":...}` and exits nonzero. MCP returns a tool result with `isError=true` and JSON text containing the same code. Do not expose provider exception text.

2. Update the existing timeout tests first:

- Replace `test_search_degrades_to_empty_on_timeout_budget` with a `504` assertion for `SEARCH_TIMEOUT`.
- Replace `test_search_timeout_returns_truncated_json` with a nonzero CLI assertion and `ok:false`.
- Add an MCP timeout assertion for `isError=true` plus the same code.
- Add provider-outage tests for HTTP `503`, nonzero CLI, and MCP `isError=true`.
- Keep the Ring 0 absent-dataset tests green.

3. Run the Docker red gate:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q \
  tests/test_server.py tests/test_cli_commands.py tests/test_mcp_server.py tests/test_qdrant_adapter.py \
  -k 'search_timeout or qdrant_unavailable or absent_dataset'
```

Expected red: current HTTP path returns `200` on timeout, CLI exits `0`, and stable codes are absent.

4. Add `SearchBudgetExceeded` beside `_search_within_budget()`. Convert `asyncio.TimeoutError` to that exception instead of returning `([], True)`:

```python
class SearchBudgetExceeded(RuntimeError):
    pass

async def _search_within_budget(citadel: Citadel, **kwargs: Any) -> list[tuple[str, Any]]:
    try:
        return await asyncio.wait_for(
            search_across_datasets(citadel, **kwargs),
            timeout=citadel.config.search_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise SearchBudgetExceeded from exc
```

5. In both search routes, map `SearchBudgetExceeded` and `QdrantProviderError` to the stable public error envelope. Keep internal provider text in classified logs only.

6. Make CLI HTTP error parsing read `detail.code` when present. Remove the successful `_emit_search_timeout` path for remote search. Preserve the existing nonzero local timeout behavior under the same code.

7. Extend `CitadelMcpError` with a stable `code`. Parse the HTTP error envelope in `CitadelHttpClient._request`. The tool wrapper must emit a JSON error payload so the MCP client sees `isError=true` and the same code.

8. Rebuild the test image and rerun the focused command. Expected green: every selected test passes, actual empty remains `200 []`.

9. Commit:

```bash
git add kb/server.py kb/cli.py kb/mcp_server.py kb/qdrant_adapter.py \
  tests/test_server.py tests/test_cli_commands.py tests/test_mcp_server.py tests/test_qdrant_adapter.py
git diff --cached --check
git commit -m "fix: preserve typed retrieval failures across clients"
```

Stage only relevant adapter hunks. Omit `kb/qdrant_adapter.py` when no source change is required.

## Task 2: Expose lifecycle identities for share and capture

**Owner:** architect, then fresh implementer `CITADEL-RECEIPT-EXPOSURE-01`

**Files:** tracking `docs/interfaces.md`; implementation `kb/server.py`, `kb/cli.py`, `tests/test_server.py`, `tests/test_capture.py`, `tests/test_mcp_server.py`

**Dependency:** Task 1 committed because files overlap.

1. Add the response contract. `/api/share-session` returns one bounded operation row per write target:

```json
{
  "operations": [
    {
      "dataset": "session-traces",
      "source_revision_id": "...",
      "projection_job_id": "...",
      "projection_state": "pending"
    }
  ]
}
```

`citadel capture --json` preserves `source_revision_id`, `projection_job_id`, and `projection_state` from each `/ingest` response inside its per-root result. IDs are omitted only when the server did not return them.

2. Add red tests:

- Dual-write share returns two operation rows whose datasets equal `write_targets`.
- MCP `citadel_share_session` preserves those rows.
- Capture JSON preserves lifecycle identities for an accepted root.
- Rejected and timed-out capture results never invent IDs.

3. Run red:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_server.py tests/test_capture.py tests/test_mcp_server.py \
  -k 'share_session and operation or capture and projection'
```

4. Build operation rows directly from each `LearningOutcome.ingest`:

```python
operations = [
    {
        "dataset": item.dataset,
        "source_revision_id": item.ingest.source_revision_id,
        "projection_job_id": item.ingest.projection_job_id,
        "projection_state": item.ingest.projection_state,
    }
    for item in all_outcomes
]
```

Return only bounded identity and state fields. Do not return source content.

5. In `_capture()`, copy the three lifecycle fields from `response` into the accepted root result.

6. Rebuild and rerun. Expected green: all selected tests pass.

7. Commit:

```bash
git add kb/server.py kb/cli.py tests/test_server.py tests/test_capture.py tests/test_mcp_server.py
git diff --cached --check
git commit -m "feat: expose lifecycle receipts for shared writes"
```

## Task 3: Add separate Qdrant snapshot storage

**Owner:** implementer `CITADEL-COMPOSE-SNAPSHOT-01`

**Files:** `docker-compose.yml`, `kb/deploy_assets/docker-compose.yml`, new `tests/test_compose_contract.py`, focused `tests/test_local_deploy.py`

**Contract:** `CITADEL-INT-BACKUP-01` and `CITADEL-INT-SELFHOST-01`

**Dependency:** Ring 0 Docker test target.

1. Add a structural test that loads both Compose files and requires identical Qdrant storage contracts. It must fail when snapshot and primary storage resolve to the same mount or named volume.

2. Run red:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_compose_contract.py tests/test_local_deploy.py \
  -k qdrant_snapshot
```

3. Add to both Compose files:

```yaml
environment:
  QDRANT__STORAGE__SNAPSHOTS_PATH: /qdrant/snapshots
volumes:
  - qdrant-data:/qdrant/storage
  - qdrant-snapshots:/qdrant/snapshots
```

Declare `qdrant-snapshots` under top-level volumes.

4. Rebuild and rerun the focused tests. Then render both Compose files with Docker Compose and inspect the Qdrant mount targets.

5. Commit:

```bash
git add docker-compose.yml kb/deploy_assets/docker-compose.yml \
  tests/test_compose_contract.py tests/test_local_deploy.py
git diff --cached --check
git commit -m "build: isolate Qdrant snapshots from primary storage"
```

## Task 4: Integrate existing runtime-hardening hunks

**Owner:** one bounded implementer per commit

**Dependency:** Tasks 1 through 3 committed. Files overlap earlier dirty work, so serialize.

### 4.1 Secure Docker loopback clients

**Files:** `kb/capture.py`, `kb/promotion_client.py`, `tests/test_capture.py`, `tests/test_promotion_client.py`.

Run focused Docker tests before and after. Require loopback and private service HTTP acceptance, public HTTP rejection, and HTTPS acceptance. Commit:

```text
fix: allow secure private-service capture clients
```

### 4.2 Permit a cold source build

**Files:** `kb/local_deploy.py`, `tests/test_local_deploy.py`.

Pin the dirty `_run_checked(..., timeout_seconds)` change with tests that source builds receive `1800` seconds and no-build starts receive `600` seconds. Commit:

```text
fix: allow cold Docker source builds to finish
```

### 4.3 Rebind restored datasets to current Qdrant

**Files:** `kb/qdrant_adapter.py`, `tests/test_qdrant_adapter.py`, live Qdrant tests.

Require restored SQLite dataset rows to use the current `VECTOR_DB_URL` and key before search, delete, or prune. Run real Qdrant retrieval after restore. Commit:

```text
fix: rebind restored datasets to current Qdrant
```

### 4.4 Keep corpus graph presence dataset-scoped

**Files:** the corpus census hunk in `kb/server.py`, graph-presence helper hunk in `kb/cognee_client.py`, focused `tests/test_server.py` and `tests/test_cognee_client.py` hunks.

Require each dataset lookup to run within that dataset and reject a global union as evidence. Commit:

```text
fix: scope graph census to each dataset
```

## Task 5: Add the versioned deterministic seed

**Owner:** implementer `CITADEL-RELEASE-PROBE-01`

**Files:** new `tests/fixtures/citadel_v050_seed_v1.json`, new `kb/release_acceptance.py`, new `tests/test_release_acceptance.py`

**Dependency:** Tasks 1 through 4.

1. Add schema version `1`, generation `citadel-v050-ring12-g1`, and these fixed markers:

| Source | Surface | Dataset | Marker |
|---|---|---|---|
| Central seed | HTTP ingest, submitted twice | `masumi-network` | `CITADEL_V050_R12_CENTRAL_V1` |
| Alice note | CLI ingest | token-resolved `seat:alice` | `CITADEL_V050_R12_ALICE_V1` |
| Bob note | MCP `citadel_ingest` | token-resolved `seat:bob` | `CITADEL_V050_R12_BOB_V1` |
| Shared trace | MCP `citadel_share_session`, two targets | `seat:alice`, `session-traces` | `CITADEL_V050_R12_TRACE_V1` |
| Capture root | CLI capture | `seat:alice` | `CITADEL_V050_R12_CAPTURE_V1` and `CITADEL_V050_R12_PROMOTION_V1` |
| Lifecycle probe | HTTP ingest | `masumi-network` | `CITADEL_V050_R12_LIFECYCLE_V1` |
| Backup probe | HTTP ingest | `masumi-network` | `CITADEL_V050_R12_BACKUP_V1` |

The shared trace creates two source revisions. Expected pre-outage totals are eight sources, eight current sources, eight jobs, and twenty-four receipts.

2. Validate unique markers, exact allowed datasets, one expected surface per write, source arithmetic, receipt arithmetic, generation equality, and absence of secret-shaped literals.

3. Keep every content body below one configured chunk. Add `expected_chunks_per_source: 1`, but treat it as unverified until the exact candidate preflight measures it. A model or chunk-budget change requires a new fixture version and generation.

4. Add a seatless reader token fixture with only `release-empty-v1`. Use it for healthy-provider empty searches over HTTP, CLI, and MCP.

5. Add the capture config shape:

```json
{
  "version": 1,
  "node_url": "http://127.0.0.1:8000",
  "roots": [
    {"path": "/data/release-seed/capture-root", "tags": ["org-work"]}
  ],
  "updated_at": null
}
```

6. Add fixture-validator red tests, implement the parser and validator, then run:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_release_acceptance.py -k manifest
```

7. Commit:

```bash
git add tests/fixtures/citadel_v050_seed_v1.json kb/release_acceptance.py \
  tests/test_release_acceptance.py
git diff --cached --check
git commit -m "test: define the v0.5 deterministic release seed"
```

## Task 6: Add Docker runtime probe and phase-aware log classifier

**Owner:** implementer `CITADEL-RELEASE-PROBE-01`

**Files:** `kb/release_acceptance.py`, new `scripts/classify_docker_logs.py`, `tests/test_release_acceptance.py`, new `tests/test_log_classifier.py`

**Dependency:** Task 5.

1. Add testable clients for HTTP and MCP. Use the official installed MCP Python client against `/mcp/`, or raw protocol only where the official client cannot expose `isError`. CLI checks execute inside the application container or a client container sharing its network namespace.

2. Canonicalize an exact matching hit to:

```json
{
  "result_id": "...",
  "document_id": "...",
  "dataset": "...",
  "generation_id": "...",
  "source_revision_id": "...",
  "projection_receipt_id": "...",
  "content_sha256": "...",
  "trust_tier": "...",
  "marker": "..."
}
```

HTTP, CLI, and MCP must agree on these fields for the exact marker. Do not compare unrelated approximate hits or raw response formatting.

3. Add operation polling until `state=searchable`. Require relational, vector, and graph receipt state `searchable` with matching generation.

4. Add full `/api/corpus` pagination. Require `documents_walked == documents_total`, no missing page, `chunk_count == 1`, and `in_graph == true` for every seed source.

5. For the eight-source seed only, call `/api/mesh/graph` with a limit above the measured seed graph size. Require the expected DocumentChunk-to-document structural relation for each source. Do not record its capped counts as a corpus census.

6. Add a phase-aware classifier. Each allowed error must name the phase and expected status. Fail on an undeclared warning, error, panic, fatal, OOM, corruption, recovery below 100 percent, unexpected non-2xx data-plane response, secret value, authorization header, or missing paired functional evidence.

7. Run Docker reds before implementation and greens after:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q tests/test_release_acceptance.py tests/test_log_classifier.py
```

8. Commit:

```bash
git add kb/release_acceptance.py scripts/classify_docker_logs.py \
  tests/test_release_acceptance.py tests/test_log_classifier.py
git diff --cached --check
git commit -m "test: add Docker release probes and log classification"
```

## Task 7: Execute Ring 1 deterministic seed

**Owner:** runtime, `CITADEL-DOCKER-RUNTIME-01`

**Files:** Docker resources and ignored evidence only

**Dependency:** Tasks 1 through 6 committed.

1. Create a private `0600` environment file under `/private/tmp`. Set generated admin, Qdrant, Alice, Bob, and empty-reader tokens; generation `citadel-v050-ring12-g1`; current embedding identity; `CITADEL_LLM_ENRICHMENT_ENABLED=false`; and no GitHub token.

2. Build exact candidate images and render Compose:

```bash
cd /private/tmp/citadel-v050-qdrant
docker build --target production --tag citadel-archive:v0.5.0-r12 .
docker build --target test --tag citadel-archive:v0.5.0-r12-test .
docker compose -p citadel-v050-r12-src \
  --env-file /private/tmp/citadel-r12.env config
docker compose -p citadel-v050-r12-src \
  --env-file /private/tmp/citadel-r12.env create
```

3. Attach timestamped followers to the stopped Citadel and Qdrant containers. Start Qdrant, wait for readiness, then start Citadel.

4. Run the release probe from the test image on the Compose network. It creates Alice and Bob, registers the capture root, creates the empty reader, executes each specified seed surface, repeats the Central request byte-identically, and polls every returned operation.

5. Require exact totals:

```text
source revisions: 8
current sources: 8
projection jobs: 8
receipts: 24
relational searchable: 8
vector searchable: 8
graph searchable: 8
corpus documents: 8
```

6. Require HTTP, CLI, and MCP exact-hit identity parity plus healthy-provider empty success. Alice must never see Bob Node content. Bob must never see Alice Node or capture content. Both may see Central and the shared trace.

7. Run `citadel status --check-search` after the populated Central marker exists. Require top-level healthy, search subcheck healthy, and a positive exact result. This is separate from Ring 0 zero-result status evidence.

8. Classify both logs and save redacted evidence.

## Task 8: Execute Ring 2 restart, replacement, outage, and convergence

**Owner:** same runtime owner

**Files:** Docker resources and ignored evidence only

**Dependency:** Ring 1 green.

1. Restart only Citadel. Record unchanged image ID and named app volume. Rerun exact search, empty search, operation, census, graph-presence, and isolation gates.

2. Replace only the Qdrant container while preserving both Qdrant named volumes. Attach a new Qdrant log follower before start. Record changed container ID, unchanged image ID, and unchanged volume names. Rerun the same gates.

3. Stop Qdrant. Run exact search through HTTP, CLI, and MCP. Require `QDRANT_UNAVAILABLE`, HTTP `503`, CLI nonzero, MCP `isError=true`. No surface may report successful empty.

4. While Qdrant is stopped, submit a ninth Central outage marker with `cognify=false`. Require accepted source plus a pending or retrying lifecycle job whose error code names Qdrant unavailability. Do not claim it is searchable.

5. Start Qdrant after the first failed projection attempt and before the fifth terminal attempt. The default retry delay is five seconds. Poll until searchable. Do not require an exact attempt number greater than one.

6. Require final totals:

```text
source revisions: 9
current sources: 9
projection jobs: 9
receipts: 27
all three searchable backend counts: 9
pending: 0
running: 0
failed: 0
```

7. Rerun exact searches and visibility checks. Classify outage errors only inside the declared outage phase. Any post-recovery provider error fails.

8. Run live stress against an exact marker. Require at least one `200`, correct marker on every `200`, zero `5xx`, valid `Retry-After` on every `429`, zero transport errors, and nonzero successful count. Capture p50, p95, CPU, memory, open files, and volume use.

## Task 9: Execute whole-generation backup and restore

**Owner:** same runtime owner

**Dependency:** Ring 2 green and Ring 0 backup containment green.

1. Stop the source Citadel application. Leave source Qdrant available for snapshot creation. Run the exact candidate image as a one-shot backup client with the source application volume and a dedicated backup volume.

2. Require the manifest to name schema `1`, generation `citadel-v050-ring12-g1`, offline single-writer mode, every local file with size and SHA-256, and every generation Qdrant collection with snapshot name, point count, size, and SHA-256. Record the adjacent seal as accidental-corruption evidence only.

3. Create project `citadel-v050-r12-restore` with fresh empty app, Qdrant primary, and Qdrant snapshot volumes. Attach followers before starting the restore Qdrant container.

4. Run the exact candidate image as a one-shot restore client. Start the restored Citadel service with `--no-build`.

5. Require source and restored Citadel containers to report the same image ID. Rerun every seed and outage marker search, all lifecycle totals, corpus pages, graph presence, HTTP/CLI/MCP parity, and seat isolation. Require the same source revision, job, receipt, result, and generation IDs.

6. Classify source, restore, and Qdrant logs. Any partial residue, count mismatch, missing collection, stale endpoint, or marker loss fails.

## Task 10: Ring 1 and 2 root gate

**Owner:** coordinator

**Dependency:** Tasks 7 through 9 complete.

1. Validate every evidence artifact against the candidate commit and image digest.

2. Reproduce each reported P0 or P1 before recording it as `VERIFIED`.

3. Correct stale claims instead of replacing them silently. In particular, record that zero-result search status remains unavailable while populated exact search is healthy.

4. Update contracts, blockers, `agents/model-routing.md`, `status.md`, and the handoff. Release Docker ownership only after container IDs, volumes, and log follower state are preserved.

## Plan self-check

- Timeout and Qdrant outage cannot masquerade as successful empty search.
- Share and capture expose lifecycle identities needed by black-box evidence.
- Snapshot storage is separate from Qdrant primary storage.
- Eight-source and nine-source arithmetic is explicit.
- Structural graph presence is exact. Semantic graph totals are not claimed.
- Restart, replacement, outage, recovery, stress, backup, and restore use the same candidate image and generation.
- All tests and probes run in Docker.
