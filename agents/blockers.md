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
Status: Blocked
Evidence: `kb/cognee_client.py:780-842`, `.env.example:246-250`, adapter PR `#149` commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29`, local same-ID probe output `alice_before 1`, `alice_after 0`, `bob_after 1`, `alice_raw_retrieve 1 dataset-bob bob-private`, and `.local-review/research/cognee-1.4.1-feature-audit.md`. Blind spot: the same-ID probe used Qdrant local mode, not a real server or complete Citadel request path.

ID: BLK-2026-08-08-03
Date: 2026-08-08
Owner: implementer
Severity: High
Description: VERIFIED: Cognee `1.4.1` requires `cryptography>=43,<50`, while Citadel requires `cryptography>=50,<51`. The combined resolver returned `No solution found`. The upstream set resolves to vulnerable `cryptography==49.0.0`; `pip-audit` reports high `CVE-2026-69247`, fixed in `50.0.0`.
Proposed resolution: keep Citadel's security floor, prove Cognee `1.4.1` with `cryptography==50.0.0`, then use a minimal reviewed Cognee metadata patch through an upstream release or exact Citadel fork commit. Do not publish an override that ordinary pip installs cannot reproduce.
Status: In Progress
Evidence: `/private/tmp/citadel-cognee-141-compat`; `uv pip install ... cognee==1.4.1 ... cryptography>=50,<51` exit `1`; `uvx pip-audit --path ...` exit `1`; adapter unit suite `10 passed, 10 warnings` after the disposable runtime override.
