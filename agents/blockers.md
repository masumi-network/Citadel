# Blockers

ID: BLK-2026-08-07-01
Date: 2026-08-07
Owner: release
Severity: Critical
Description: REPORTED: a Cognee Alembic migration log emitted a database URL containing credentials in Railway logs. VERIFIED: Cognee 1.2.2 source renders the URL with `hide_password=False` and logs it from `cognee/alembic/env.py:97-99` in the pinned upstream source checkout.
Proposed resolution: obtain explicit approval, rotate the database credential, verify the old credential is rejected, then verify the service is healthy with the new credential. Keep runtime log redaction in Citadel even after rotation.
Status: Blocked
Evidence: REPORTED Railway observation at 2026-08-07 14:22:55Z. Local source evidence: `.local-review/research/cognee/cognee/alembic/env.py:97-99`. Secret value is intentionally omitted.

ID: BLK-2026-08-08-01
Date: 2026-08-08
Owner: architect
Severity: High
Description: VERIFIED: Citadel's current in-place repair invokes dataset-wide Cognee force cognify after deleting candidate chunks. The operation can rewrite healthy same-dataset and shared semantic state, while rollback snapshots cover candidate `DocumentChunk` projections only. Changing the vector provider does not remove this graph and relational mutation boundary.
Proposed resolution: replace production in-place repair with a full shadow generation and verified cutover. Ticket 008 owns the Qdrant vector migration contract; ticket 005 remains a preserved falsification path.
Status: Blocked
Evidence: `kb/service.py:1423-1457`, `kb/cognee_client.py:2867-2952`, `kb/cognee_client.py:3752-3869`, and `.local-review/wayfinder/tickets/005-prove-disposable-repair.md`.

ID: BLK-2026-08-08-02
Date: 2026-08-08
Owner: architect
Severity: Critical
Description: VERIFIED: Citadel disables Cognee backend access control and its Cognee search call supplies no Qdrant ownership filter. A real Qdrant `1.18.1` probe with two private seat points returned both seats when unscoped. The raw community adapter also drops CHUNKS reference fields, overwrites same-ID ownership payloads, and converts search exceptions to empty results.
CORRECTED: the earlier proposed resolution named Cognee `1.2.2`. The user later selected Cognee `1.4.1`, and the exact-version research completed on 2026-08-08.
Proposed resolution: close DEC-2026-08-08-06, then patch the exact official Cognee `1.4.1` community adapter source. Require backend access control plus the selected tenant storage boundary, authorization on search, retrieve, delete, prune, count, scroll, and hydration, complete CHUNKS payloads, typed provider failures, and authorized graph aggregation. Run same-ID and distinct-ID two-seat contracts against a pinned real server before any hosted deployment.
Status: In Progress
Evidence: CORRECTED: candidate branch `agent/citadel-v050-qdrant` now has a Citadel-owned adapter. VERIFIED: `env CITADEL_QDRANT_LIVE_URL=http://127.0.0.1:6333 uv run pytest tests/test_qdrant_adapter_live.py tests/test_cognee_qdrant_sqlite_live.py -q` returned `2 passed, 11 warnings in 25.65s` against Qdrant `1.19.0`. The restart worker used Cognee `1.4.1`, SQLite, Ladybug, and the Citadel adapter. Central reads returned only Central text and Alice reads returned only Alice text after a fresh Python process. Blind spot: mocked LLM and embeddings do not prove real provider quality, CLI or MCP, snapshot restore, or hosted networking.

Checkpoint 2026-08-09: VERIFIED: local commit `a0d5c02` preserves nested Cognee `1.4.1` CHUNKS text and fail-closed Qdrant scope handling. Adapter and client tests returned `111 passed, 10 warnings in 11.29s`. The local same-ID provider receipt returned exact count `1` for each of `seat:alice` and `seat:bob`; retrieve, search, and exhaustive scroll returned only the requested dataset after restart and image replacement. Status remains In Progress because the recorded runtime receipt does not cover authorized graph aggregation and every destructive adapter path.

CORRECTED 2026-08-09: the candidate now applies dataset context to graph presence checks and runs connector deletion through lifecycle tombstones. The pinned Qdrant `1.19.0` live adapter test covered same-ID writes, count, retrieve, search, dataset-scoped delete, and prune, returning `1 passed, 11 warnings in 5.85s`. The Cognee, SQLite, Ladybug, Qdrant, lifecycle, and fresh-process retrieval test returned `1 passed in 34.55s`.
Status: Completed
Evidence: candidate commit `5bdcf89`; `tests/test_qdrant_adapter_live.py`; `tests/test_cognee_qdrant_sqlite_live.py`. Blind spot: the disposable test does not prove hosted networking, production corpus recall, or coherent whole-generation backup.

ID: BLK-2026-08-08-03
Date: 2026-08-08
Owner: implementer
Severity: High
Description: VERIFIED: Cognee `1.4.1` requires `cryptography>=43,<50`, while Citadel requires `cryptography>=50,<51`. The combined resolver returned `No solution found`. The upstream set resolves to vulnerable `cryptography==49.0.0`; `pip-audit` reports high `CVE-2026-69247`, fixed in `50.0.0`.
Proposed resolution: keep Citadel's security floor, prove Cognee `1.4.1` with `cryptography==50.0.0`, then use a minimal reviewed Cognee metadata patch through an upstream release or exact Citadel fork commit. Do not publish an override that ordinary pip installs cannot reproduce.
Status: In Progress
Evidence: `/private/tmp/citadel-cognee-141-compat`; `uv pip install ... cognee==1.4.1 ... cryptography>=50,<51` exit `1`; `uvx pip-audit --path ...` exit `1`; adapter unit suite `10 passed, 10 warnings` after the disposable runtime override.

CORRECTED 2026-08-08: the secure package blocker is resolved for the candidate build path.
Status: Completed
Evidence: commit `420be9d` builds exact Cognee `1.4.1` source SHA-256 `9206075539935ef0adfab82cf410af6799e83c42969ba7c8fae5065de9aba7c9` with the audited one-line cryptography cap patch. The resulting wheel SHA-256 is `2c1bec17b0ed9563ffa4f6ccdd4a02939cdec6dfd93db9faf852266ce3231a91`. Draft PR 256 CI is green. Local full suite returned `1759 passed, 1 skipped, 11 warnings in 22.26s`; `pip-audit` returned `No known vulnerabilities found, 1 ignored`.

ID: BLK-2026-08-08-04
Date: 2026-08-08
Owner: implementer
Severity: High
Description: VERIFIED: the real Cognee `1.4.1` plus SQLite plus Qdrant cognify flow logs `stored chunk budget was not measured after cognify: VECTOR_DB_PROVIDER is not pgvector`. The existing persisted chunk budget gate is provider-specific and does not attest Qdrant data.
Proposed resolution: add a dataset-scoped Qdrant chunk census using the Citadel adapter's filtered scroll path. Require every processed source to have measured chunks and zero over-budget payloads. Add real-server regression coverage before Railway import.
Status: Blocked
Evidence: `/private/tmp/citadel-v050-qdrant` now has unit coverage for the Qdrant census branch; `./.venv/bin/pytest -q tests/test_cognee_client.py -k 'stored_chunk_budget_check_qdrant'` returned `5 passed` and `./.venv/bin/pytest -q tests/test_cognee_client.py tests/test_lite_runtime.py` returned `93 passed`. `./.venv/bin/ruff check` for touched files is clean. Real-server Qdrant regression remains skipped pending explicit runtime provider env (`tests/test_qdrant_adapter_live.py`, `tests/test_cognee_qdrant_sqlite_live.py` return `2 skipped` without `CITADEL_QDRANT_LIVE_URL`).

Checkpoint 2026-08-08: VERIFIED: `/private/tmp/citadel-v050-qdrant/kb/cognee_client.py` now contains a tested Qdrant census branch using authorized dataset contexts and filtered scroll. `./.venv/bin/pytest -q tests/test_cognee_client.py -k 'stored_chunk_budget_check_qdrant'` returned `5 passed`; `tests/test_cognee_client.py` + `tests/test_lite_runtime.py` returned `93 passed`. `./.venv/bin/ruff check` returns clean for touched files. Blocker remains for real-server Qdrant branch coverage. GPT-5.6-Sol owns contract and evidence review under `architect` and `reviewer`. GPT-5.3-Codex-Spark owns scoped implementation and command execution under `implementer`.

CORRECTED 2026-08-09: the Qdrant census blocker is resolved for the disposable candidate.
Status: Completed
Evidence: local commit `a0d5c02` contains the scoped Qdrant census and its regressions. The candidate image built after those changes, authenticated ingest accepted marker `citadel-v050-final-acceptance-14948765-cee0-454f-a85b-bf02c47f9360`, background cognify completed, and authenticated search returned the full marker from the dataset-isolated Qdrant provider. Before commit, adapter and client tests returned `111 passed, 10 warnings in 11.29s`; after local PR 254 integration, the full suite returned `1781 passed, 3 skipped, 11 warnings in 32.97s`. Blind spot: one accepted runtime document is not a corpus-scale census, and the container has not been rebuilt from local merge commit `f3e92ff`.

ID: BLK-2026-08-09-01
Date: 2026-08-09
Owner: architect
Severity: High
Description: REPORTED: lifecycle v1 schema and migration work requires explicit approval, and the approval phrase was not supplied across three consecutive goal turns. VERIFIED: the candidate remains clean at `f3e92ff`, four commits ahead of remote PR 256. `rg -n "projection_job_id|source_revision_id|projection_receipt_id|class Lifecycle" kb tests` returned exit `1` with no output in that candidate.
Proposed resolution: user replies `yes, implement lifecycle v1`. This authorizes schema work in the disposable candidate only. Push, runtime rebuild, restart, deployment, merge, release, production mutation, and deletion keep their separate gates.
RESOLVED 2026-08-09: REPORTED: the user replied `yes, implement lifecycle v1` in the current task. This clears schema work in `/private/tmp/citadel-v050-qdrant` only.
Status: Completed
Evidence: current task user approval on 2026-08-09; `docs/interfaces.md` CITADEL-INT-LIFECYCLE-01 and CITADEL-INT-RETRIEVAL-01; `.local-review/research/lifecycle-implementation-audit.md`. Push, runtime rebuild, restart, deployment, merge, release, production mutation, and deletion remain unapproved.
VERIFIED implementation follow-up: candidate commit `275e433d08251f4642d26e2136d8fa9e5e2193c1` completed lifecycle v1 locally. `uv run pytest -q` returned `1847 passed, 3 skipped, 11 warnings in 25.27s`; Ruff returned `All checks passed!`.

ID: BLK-2026-08-09-02
Date: 2026-08-09
Owner: release
Severity: High
Description: VERIFIED: the existing backup smoke copies Cognee SQLite and lifecycle SQLite sequentially, snapshots one direct-smoke Qdrant collection, and omits Ladybug graph state. It does not define one quiesced generation cut, inventory every production collection, or boot Citadel against restored state.
Proposed resolution: pause ingestion and projection work, record one generation manifest, back up both SQLite databases, every generation Qdrant collection, and Ladybug graph files or a documented graph rebuild input. Restore only downloaded artifacts into empty storage, boot Citadel, verify receipts and exact markers, then resume writes.
Status: Blocked
Evidence: `scripts/smoke_qdrant_provider.py:315-380`; `kb/lite_runtime.py:48-69`; Docker audit on 2026-08-09. VERIFIED narrow proof: one downloaded `DocumentChunk_text` snapshot restored to a new collection with equal config, payload schema, vectors, payloads, and three rows. Blind spot: one collection restore is not a Citadel generation restore.

Checkpoint 2026-08-09: CORRECTED: an uncommitted whole-generation implementation now exists at `/private/tmp/citadel-v050-qdrant/kb/generation_backup.py`. VERIFIED earlier in this task: its focused live test returned `1 passed, 2 warnings in 52.63s`. Status remains Blocked until the same source passes inside a Docker test image, restores into fresh Qdrant and Lite storage, boots the restored production image, retrieves exact markers, and passes independent review.

Fresh review 2026-08-09: REPORTED by backup reviewer and verified only by source locations at handoff: restore does not bind target runtime generation to artifact generation; the public CLI imports server-only Qdrant dependencies before its guard; count-verification failure can leave a restored collection outside rollback tracking; destination containment and symlink rollback boundaries are incomplete; the adjacent checksum does not authenticate an attacker-modified artifact. Add wrong-generation, rollback-after-real-recovery, destination containment, permissions, and production Compose orchestration regressions before closing this blocker. Evidence: candidate `kb/generation_backup.py:18,214-280,341-402`; `kb/cli.py:145-183`; `kb/local_deploy.py:153`; `tests/test_generation_backup_live.py:80`.

ID: BLK-2026-08-09-03
Date: 2026-08-09
Owner: implementer
Severity: High
Description: VERIFIED: exact image `sha256:c2fdaebc720e22eb5926c8fd15e5f900d22ea46244ac3f6d99756eba87877cae` returned HTTP `500` for the CLI search probe against configured default dataset `masumi-network` when that dataset had no row. App logs recorded `DatasetNotFoundError: No datasets found. (Status code: 404)`. Exact search against populated dataset `lifecycle-live` returned HTTP `200` and the marker.
Proposed resolution: add a regression that makes Cognee `DatasetNotFoundError` an honest no-data result at the client boundary, confirm explicit permission errors still fail, then prove empty-dataset `/search` returns HTTP `200` with zero results in the production Docker image. `citadel status --check-search` may retain its existing unavailable canary result for zero hits, but it must record the zero count without a provider exception and must not make top-level health false.
Status: Blocked
Evidence: `/private/tmp/citadel-v050-qdrant/kb/cognee_client.py:657-658`; Docker command `citadel status --node-url http://127.0.0.1:8000 --json --check-search --no-recent` returned `SEARCH_UNAVAILABLE`; followed Citadel logs returned the exact exception and `POST /search HTTP/1.1 500 Internal Server Error`.

ID: BLK-2026-08-09-04
Date: 2026-08-09
Owner: release
Severity: High
Description: VERIFIED: the uncommitted `container-smoke` raw app run omits mandatory `CITADEL_QDRANT_SERVER_IMAGE`, while Lite validates it before startup. The job also sets `CITADEL_LIFECYCLE_ENABLED=false` and bypasses production Compose wiring. Its static test does not check the complete required environment.
Proposed resolution: add a reproducible Docker test target, run non-live and live suites inside it, then use production `docker-compose.yml` for authenticated lifecycle ingest, operation receipts, search, CLI, MCP, capture, restart, outage, restore, security, resource, and log-classification gates.
Status: Blocked
Evidence: `/private/tmp/citadel-v050-qdrant/.github/workflows/test.yml:278-301`; `/private/tmp/citadel-v050-qdrant/kb/lite_runtime.py:85-93`; `/private/tmp/citadel-v050-qdrant/tests/test_docker_workflow.py:7-19`. Reviewer reproduction: `LiteConfigurationError: CITADEL_QDRANT_SERVER_IMAGE must not be empty`.

ID: BLK-2026-08-09-05
Date: 2026-08-09
Owner: release
Severity: Critical
Description: VERIFIED: the host Data filesystem reached `100%` with `126MiB` available. Docker Desktop shut its Linux engine down after it could not write the VM initialization log, and the Docker socket was unavailable.
Proposed resolution: remove only the approved recoverable UV package cache, restart Docker Desktop, verify preserved final3 resources, then prune only unreferenced BuildKit cache.
Status: Completed
Evidence: REPORTED: the user approved the exact cleanup on 2026-08-09. VERIFIED: `/Users/sarthiborkar/.cache/uv` measured `4,667,272 KiB`, was deleted, and `test ! -e /Users/sarthiborkar/.cache/uv` returned exit `0`. The cache is recoverable by redownloading packages. Docker Desktop was restarted after stale user-level Docker processes were terminated. Preserved final3 app image remained `sha256:c2fdaebc720e22eb5926c8fd15e5f900d22ea46244ac3f6d99756eba87877cae`; authenticated readiness returned `200`; Qdrant retained six collections. `docker buildx prune --force` removed `3.852GB` of unreferenced cache. Images remained `15`, containers `23`, and volumes `17`. Host free space became `6.3GiB`.

ID: BLK-2026-08-09-06
Date: 2026-08-09
Owner: implementer
Severity: High
Description: VERIFIED: a fresh production Compose boot reached Uvicorn, then invoked OpenRouter without credentials despite `COGNEE_SKIP_CONNECTION_TEST=true`. The container healthcheck remained in `starting` because authenticated `/readyz` returned HTTP `503` repeatedly.
Proposed resolution: trace the exact post-startup caller, add a focused regression for an empty offline boot, apply the smallest CITADEL-INT-SELFHOST-01-compatible fix, then rerun production Compose with timestamped Citadel and Qdrant logs.
Status: In Progress
Evidence: `/private/tmp/citadel-ring0-recovered-app-20260809.log` records `litellm.AuthenticationError`, `OpenrouterException`, and `Missing Authentication header`; recovered image `sha256:80cea13a84657b50ec3af9ff34304062c895ebf400ae612a0e3bf894e07bcf38`; app classifier returned 274 lines, 24 severe lines, and 17 strict non-2xx lines; Qdrant returned 18 lines, zero severe lines, and zero non-2xx lines. Disposable resources were removed after classification; preserved final3 remained healthy.

ID: BLK-2026-08-09-07
Date: 2026-08-09
Owner: implementer
Severity: High
Description: VERIFIED by root source reproduction: restore checks an empty target, creates `citadel-state`, then acquires the shared Lite lock. If another process wins the lock race, failure cleanup can remove that process's active `citadel-state` or the entire target.
Proposed resolution: add a bounded concurrent regression that loses the lock race after the initial emptiness check, track only paths created by the restore operation, and prove failure cleanup preserves the winning runtime's state.
Status: In Progress
Evidence: `/private/tmp/citadel-v050-qdrant/kb/generation_backup.py:478-496,528-539`; `/private/tmp/citadel-v050-qdrant/kb/lite_runtime.py:110-118,260-265`; CITADEL-INT-BACKUP-01 failure cleanup ownership rule. Runtime race not yet executed, which is the acceptance blind spot.

ID: BLK-2026-08-09-08
Date: 2026-08-09
Owner: implementer
Severity: High
Description: VERIFIED by root source reproduction: both CI log classifiers match `error|panic|fatal|oom|corruption|recovery.*(shortfall|failed)` and therefore miss unexpected `WARN` and generic `failure` lines required by the approved release log contract.
Proposed resolution: add structural regressions for warning and generic-failure detection, preserve phase-specific expected-line handling, then run the classifier against Docker evidence from every gate.
Status: In Progress
Evidence: `/private/tmp/citadel-v050-qdrant/.github/workflows/test.yml:270-278,404-410`; approved release design log classifier contract. Blind spot: current source predicate was inspected but the revised classifier has not run against real provider startup logs.

ID: BLK-2026-08-09-09
Date: 2026-08-09
Owner: implementer
Severity: High
Description: VERIFIED by root source reproduction: production Citadel Compose runs as UID `10001` with capability and privilege restrictions, but neither Compose file sets `read_only: true` for the root filesystem.
Proposed resolution: set the Citadel service root filesystem read-only in both identical Compose files, retain explicit writable `/data` and `/tmp`, add a structural regression, then prove production startup and mutable Cognee paths in Docker.
Status: In Progress
Evidence: `/private/tmp/citadel-v050-qdrant/docker-compose.yml:15-66`; identical deploy asset; CITADEL-INT-SELFHOST-01. Blind spot: production Compose startup is already blocked by BLK-2026-08-09-06, so the runtime proof must follow that fix.

ID: BLK-2026-08-10-01
Date: 2026-08-10
Owner: implementer
Severity: High
Description: VERIFIED: production Compose now enforces a read-only root, but Ladybug warm-up tries to install its JSON extension below `/home/citadel/.lbdb/extension`. Directory creation fails, corpus health degrades, and `/readyz` returns HTTP `503`.
Proposed resolution: identify Ladybug's supported writable-home configuration, route only its mutable extension state below `/data`, add a regression, then rerun the exact source-reverted, focused, full, and production Compose gates.
Status: In Progress
Evidence: production image `sha256:e2b30a1645b84b64f6c06d11c08f7fb2f3498fc71a7a766e620642943b705ce4`; exact log `RuntimeError('IO exception: Failed to create directory /home/citadel/.lbdb/extension/0.18.1/linux_arm64/...')`; disposable app classifier found 24 warning/error/failure/degraded lines and 12 strict non-2xx lines. Model-auth matches were zero. Disposable project was removed; preserved final3 remained healthy.

ID: BLK-2026-08-10-02
Date: 2026-08-10
Owner: release
Severity: High
Description: VERIFIED: two clean deterministic builder proofs and one test-target attempt exhausted the host data volume. `df -h /System/Volumes/Data` returned `115Mi` available and `100%`; Docker Desktop then returned `unable to start`.
Proposed resolution: after explicit approval, delete only named recoverable user caches sufficient to restart Docker, verify preserved final3, prune only unreferenced BuildKit cache, then resume with cached builds instead of repeated `--no-cache` runs.
Status: Completed
Evidence: both builder runs reported patched wheel SHA-256 `890a5a5c7d4bce9053faa45e4ce5f19aa1f7dbce235c3d4ea6ab3c3b77bb873c`. VERIFIED 2026-08-10: explicit approval removed only `~/.cache/codex-runtimes`, `~/Library/Caches/pip`, and `~/.npm`; all three paths were absent afterward. Docker Desktop required a clean quit and reopen. Preserved Citadel image `sha256:c2fdaebc720e22eb5926c8fd15e5f900d22ea46244ac3f6d99756eba87877cae` returned healthy, Qdrant image `sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc` returned running with six authenticated collections, and timestamped followers saw HTTP `200` provider traffic. Approved BuildKit-only pruning removed `10.62GB`; `docker builder du` then reported `0B`, and host free space became `10GiB`. No image, container, volume, repository, or application data was deleted.

ID: BLK-2026-08-10-03
Date: 2026-08-10
Owner: release
Severity: High
Description: VERIFIED: corrected test image `sha256:7de749c08a5ab6d760a66652f1e0e5f35b5e8f7e7f702be58a5b63841c2e6d39` built successfully, then host free space fell to about `2.8GiB`, below the declared `5GiB` runtime floor. `docker buildx du` reports `3.817GB` fully reclaimable cache. No corrected-image test container, live Qdrant phase, or production Compose phase started.
Proposed resolution: after explicit approval, run `docker buildx prune --force`, confirm images, containers, and volumes are unchanged, then reuse the existing corrected image for all remaining Docker gates.
Status: Blocked
Evidence: build handoff reported BuildKit `5dvnvubjb9r880lctjgn0csbd`, image size `499212473`, and no Phase 1, 2, or 3 resources created. Protected final3, service E2E, and Lite containers remained running. Blind spot: final-image behavior is not determined until the blocked gates run.
