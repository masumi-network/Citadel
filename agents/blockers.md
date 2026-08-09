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
Status: Blocked
Evidence: `docs/interfaces.md` CITADEL-INT-LIFECYCLE-01 and CITADEL-INT-RETRIEVAL-01; `.local-review/research/lifecycle-implementation-audit.md`; candidate branch readback on 2026-08-09.
