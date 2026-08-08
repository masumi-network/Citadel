# Interfaces

## CITADEL-INT-SELFHOST-01: One-command local deployment

Interface ID: CITADEL-INT-SELFHOST-01
Owner: architect
Provider: Citadel CLI, pinned OCI image, and pinned Docker Compose bundle
Consumers: local operators, Railway template users, and DigitalOcean Droplet operators
Status: Planned

Request or input:

- INFERRED command: `citadel deploy local`.
- Exact CLI install: `pipx install citadel-archive==0.5.0`. The release guide may use the final approved version instead of this candidate value.
- Optional `--database sqlite|postgres`, `--data-dir`, `--version`, `--non-interactive`, and `--dry-run` arguments.
- `sqlite` is the local default. The selected value is written to deployment state and never inferred from the host.
- Docker Engine and Docker Compose v2 already installed by the operator.

Response or output:

- Version-pinned Citadel app and Qdrant services from one Compose project. The PostgreSQL profile adds a pinned PostgreSQL service.
- A generated configuration directory with restrictive file permissions, generated secrets, image and generation identity, and persistent volume locations.
- A bounded readiness result and the path to a smoke-test receipt. Secret values are not printed in ordinary output.

Errors:

- Missing Docker or Compose, unsupported platform, occupied ports, insufficient disk, invalid existing config, image digest mismatch, unhealthy service, migration failure, or smoke-test failure returns nonzero with the failed stage.
- Existing config or data is never overwritten without an explicit operator confirmation. Non-interactive mode fails instead of overwriting.

Events:

- Preflight passed, config created, images verified, services started, migrations passed, readiness passed, smoke passed, and setup failed.

Compatibility rule:

- `citadel setup` keeps its existing capture-root meaning. `citadel onboard` keeps its existing teammate-client meaning.
- The base CLI remains zero-dependency. It invokes the installed Docker CLI and does not install Docker, modify system package managers, or request root privileges.
- `pipx` is the primary CLI installer because Citadel already documents it and it keeps a persistent isolated environment. `uvx` remains an optional exact-version CI or disposable test path.
- SQLite is accepted only for one host, one app process, one scheduler, and zero replicas. PostgreSQL remains available through the same image and schema contract.
- A repeated `citadel deploy local` with the same version and config is idempotent. Version changes require an explicit upgrade path and snapshot evidence.
- Railway v0.5 uses the SQLite Lite template. A later PostgreSQL profile uses the same image. DigitalOcean uses a Droplet and the same Compose bundle because App Platform has no persistent volumes.

Verification:

- Fresh Linux and macOS-compatible Docker environments create one healthy empty generation.
- Second invocation changes no secret, volume, generation, or service identity.
- Restart preserves data and receipts.
- SQLite concurrent ingest, scheduled work, abrupt-stop recovery, online backup, and restore pass before the Lite profile is published.
- PostgreSQL migration and functional parity remain required before a later PostgreSQL profile is published.
- Missing prerequisite, occupied port, image mismatch, migration failure, Qdrant outage, and smoke failure produce typed nonzero exits.
- No secret appears in terminal output, process arguments, committed files, or smoke receipts.

## CITADEL-INT-QDRANT-01: Authorized vector projection

Interface ID: CITADEL-INT-QDRANT-01
Owner: architect
Provider: Citadel Qdrant adapter backed by Qdrant
Consumers: `kb.cognee_client`, retrieval orchestration, corpus census, repair and export tools
Status: Planned

Request or input:

- A generation ID.
- One exact authorized dataset resolved from the authenticated Citadel identity.
- A task-local operation mode: `read`, `write`, or `admin`.
- Query text or a normalized vector.
- Collection name, result limit, and score profile.
- Stable source revision and point identity for writes, deletes, census, and hydration.

Response or output:

- Retrieval candidates with point ID, source revision ID, dataset, generation, evidence text, Cognee CHUNKS reference fields, raw Qdrant similarity, normalized Citadel score, and rank.
- Projection receipts with exact affected point IDs and counts.
- Exact count and scroll pages for administrative census. Top-k similarity search is not a census.

Errors:

- Provider timeout, unavailable service, malformed payload, incomplete scope, unknown generation, and dimension mismatch are typed failures.
- Search and batch search must not convert provider exceptions to `[]`.
- Missing authorization scope must fail before a Qdrant query runs.

Events:

- Projection accepted, searchable, stale, failed, snapshot created, snapshot restored, and generation activated.

Compatibility rule:

- CORRECTED: exact Cognee `1.4.1` and official community adapter commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29` are the active compatibility target. The prior `1.2.2` custom-adapter target was superseded by user direction.
- CORRECTED: the official Qdrant dataset handler is not a complete isolation boundary. A verified same-ID probe showed one dataset overwriting another dataset's point while raw retrieve crossed the requested dataset boundary.
- INFERRED: Cognee backend access control is mandatory for dataset authorization. DEC-2026-08-08-06 selects candidate B as the working storage boundary.
- INFERRED: Citadel must normalize Cognee's dataset-wrapped CHUNKS results and aggregate graph data only across datasets authorized for the caller.
- Active candidate B: every logical Cognee collection maps to one physical collection per generation. Stored point IDs derive from generation, dataset, and raw Cognee ID. Payload preserves the raw ID. Every Qdrant operation applies the matching dataset constraint.
- Fallback candidate A: every logical Cognee collection maps to one physical collection per generation and dataset.
- Candidate B fails acceptance if any search, retrieve, delete, prune, count, scroll, rollback, or migration path can operate without generation and dataset scope. Candidate A then becomes mandatory.
- `belongs_to_set` remains Cognee NodeSet metadata. It is not accepted as proof of dataset authorization.
- Missing, empty, conflicting, or multi-dataset scope fails before a provider request.
- Preserve `DocumentChunk_text` payload fields used by Citadel, including document identity, name, chunk index, source chunk ID, importance weight, and dataset ownership.
- Preserve Cognee's lower-is-better distance contract when translating Qdrant cosine similarity.
- Upserting an existing point must preserve the union of dataset ownership tags or use a physical model that prevents shared-ID overwrite.
- Official adapter registration is not release acceptance. Citadel registers only an audited patch that satisfies this interface.
- Prune enumerates and mutates only collections bound to the current generation and authorized dataset. Missing scope fails before listing collections.

Verification:

- VERIFIED current red probe: Qdrant `1.18.1`, client `1.18.0`, two same-collection seat points. Unscoped result count was `2`; scoped `seat:a` result count was `1`.
- VERIFIED dependency probe: Cognee `1.4.1` plus adapter PR `#149` cannot resolve with Citadel's cryptography floor. The disposable runtime override to `cryptography==50.0.0` passed adapter unit tests with `10 passed, 10 warnings`.
- VERIFIED isolation probe: after the same UUID was written under Bob, Alice search returned zero and Alice raw retrieve returned Bob's dataset and text. Blind spot: Qdrant local mode did not exercise a real server or Citadel HTTP paths.
- Planned gate: two-seat adversarial search, direct-ID lookup, delete, prune, retry, outage, restart, snapshot restore, payload roundtrip, score direction, exact count, and scroll tests.
- Planned gate: a single queue lease containing multiple datasets executes separate scoped cognify calls and produces no cross-dataset point or receipt.
- Planned migration gate: fresh database, idempotent second run, second-process restart, returned failure IDs, injected provider migration failure, and write rejection after any failure.

## CITADEL-INT-GENERATION-01: Whole-generation bootstrap and activation

Interface ID: CITADEL-INT-GENERATION-01
Owner: architect
Provider: Citadel generation controller
Consumers: release tooling, GitHub sync, seat import, CLI and MCP state reporting
Status: Planned

Request or input:

- Immutable generation ID and configuration digest.
- Relational provider and generation-scoped database path or namespace, Qdrant collection prefix or aliases, graph namespace, model, dimensions, chunk budget, Cognee version, and adapter version.
- Frozen source manifest or SourceRevision high-water mark.

Response or output:

- Generation state: `building`, `shadow`, `active`, `retired`, or `failed`.
- Relational, vector, graph, source, queue, and receipt censuses.
- One durable activation record and a rollback target.

Errors:

- Source manifest drift, missing projection receipt, census mismatch, visibility violation, unhealthy provider, and partial alias or generation switch.

Events:

- Generation created, GitHub bootstrap complete, seat bootstrap complete, verification passed, activated, reverted, and retired.

Compatibility rule:

- A Qdrant alias can switch Qdrant collections, but it cannot attest relational or graph activation. Citadel's generation record is the whole-system authority.
- Old production remains readable until the new generation passes its rollback window. New writes after cutover must be retained in the new Source Ledger.

Verification:

- GitHub Central passes first. Each seat then passes zero visibility violations before the next seat starts.
- CLI and MCP report the same build ID, deployment ID, generation ID, adapter, model, dimensions, and collection identity.

## CITADEL-INT-QDRANT-MCP-01: Optional Qdrant MCP diagnostics

Interface ID: CITADEL-INT-QDRANT-MCP-01
Owner: release
Provider: official `qdrant/mcp-server-qdrant`
Consumers: operators only
Status: Planned

Request or input:

- Private Qdrant URL, API key, explicit diagnostic collection, and `QDRANT_READ_ONLY=true`.

Response or output:

- Operator-only semantic inspection of the selected collection.

Errors:

- Any public binding, write-enabled mode, missing authentication, or access to Citadel production collections blocks enablement.

Events:

- None in the Citadel memory lifecycle. This service is not a source writer or retrieval authority.

Compatibility rule:

- It remains separate from Citadel's hosted MCP. It cannot replace Citadel tools because it does not enforce Citadel seat scope, provenance, graph, source ledger, or projection receipts.

Verification:

- Private-network access only, read-only tool list, no store tool, and no foreign collection access.
