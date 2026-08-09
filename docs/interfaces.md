# Interfaces

## CITADEL-INT-SELFHOST-01: One-command local deployment

Interface ID: CITADEL-INT-SELFHOST-01
Owner: architect
Provider: Citadel CLI, pinned OCI image, and pinned Docker Compose bundle
Consumers: local operators, Railway template users, and DigitalOcean Droplet operators
Status: In Progress

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
- VERIFIED on 2026-08-09: candidate commit `92ce11a` packages the Lite runtime, local deploy command, Dockerfile, Compose bundle, smoke path, and release identity checks. `uv build --wheel --sdist` produced installable `0.5.0` artifacts, and the full candidate suite after local integration returned `1781 passed, 3 skipped, 11 warnings in 32.97s`.
- VERIFIED on 2026-08-09: the local Compose stack passed authenticated readiness, HTTP ingest, background cognify, exact marker retrieval, process restart, SQLite backup and restore, and Qdrant snapshot restore. Blind spot: the running image predates the local merge of PR 254 corpus readiness diagnostics, and Railway has not been deployed.

## CITADEL-INT-QDRANT-01: Authorized vector projection

Interface ID: CITADEL-INT-QDRANT-01
Owner: architect
Provider: Citadel Qdrant adapter backed by Qdrant
Consumers: `kb.cognee_client`, retrieval orchestration, corpus census, repair and export tools
Status: In Progress

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
- VERIFIED on 2026-08-09: local commit `a0d5c02` contains the final Qdrant census, embedding-probe, nested CHUNKS result, and regression-test changes. Adapter and client tests returned `111 passed, 10 warnings in 11.29s` before commit.
- VERIFIED on 2026-08-09: the disposable real-provider gate used one raw UUID in `seat:alice` and `seat:bob`. Exact count was `1` per dataset, and retrieve, search, and exhaustive scroll returned only the requested dataset. Blind spot: this receipt does not prove every graph aggregation or destructive adapter path.

## CITADEL-INT-LIFECYCLE-01: Durable source and projection lifecycle

Interface ID: CITADEL-INT-LIFECYCLE-01
Owner: architect
Provider: Citadel source ledger and projection coordinator
Consumers: ingest surfaces, Cognee orchestration, Qdrant and graph adapters, CLI, MCP, census, rebuild, and release tooling
Status: In Progress

Request or input:

- INFERRED working contract version: `1`.
- CORRECTED working contract: one authorized dataset resolved before storage, one stable source key assigned by a connector or Citadel, retained evidence bytes or a Citadel-owned durable content reference, optional source locator for manual notes, media type, capture actor, capture time, and optional previous revision ID.
- One immutable generation ID, projection version, required backend set, and configuration digest for projection work.
- One idempotency key derived from source revision ID, generation ID, backend, and projection version. Provider-generated IDs are not idempotency keys.

Response or output:

- `SourceRevision`: `schema_version`, `source_revision_id`, `source_key`, `dataset`, `content_sha256`, `byte_length`, `retained_content_ref`, `source_locator`, `media_type`, `previous_revision_id`, `capture_actor_id`, `capture_run_id`, `captured_at`, `accepted_at`, and `tombstone`.
- `ProjectionJob`: `schema_version`, `projection_job_id`, `source_revision_id`, `generation_id`, `dataset`, `projection_version`, `config_digest`, `required_backends`, `idempotency_key`, `state`, `attempt`, lease fields, timestamps, and bounded last error.
- One `ProjectionReceipt` per required backend: `schema_version`, `projection_receipt_id`, `projection_job_id`, `source_revision_id`, `generation_id`, `dataset`, `backend`, `provider`, `projection_version`, `state`, `attempt`, provider operation ID, exact affected IDs or count, model and dimension identity when applicable, timestamps, and typed error fields.
- Required v1 backend names are provider-neutral: `relational`, `vector`, and `graph`. Provider fields record SQLite or PostgreSQL, Qdrant, and the selected graph engine.
- Receipt states are `pending`, `running`, `completed`, `searchable`, `failed`, or `stale`. `completed` means the provider call returned. `searchable` requires a bounded read check. Whole-job success is derived from every required receipt and is never written from one provider result.
- The ingest response returns `accepted`, the source revision ID, projection job ID, and current derived state. CLI and MCP can poll the same bounded operation record.

Errors:

- Authorization failure, source retention failure, transaction failure, idempotency conflict, lease loss, provider timeout, provider failure, census mismatch, and searchability timeout are typed failures.
- A successful provider call cannot overwrite a failed or stale receipt from another backend.
- An accepted source revision without projection work is an invariant violation. The source revision and initial projection job are written in one relational transaction.

Events:

- Source accepted, projection queued, lease acquired, backend completed, backend searchable, retry scheduled, projection failed, receipt stale, and rebuild reconciled.

Compatibility rule:

- Source revisions are generation-independent. Projection jobs and receipts are generation-specific.
- Retained content is mandatory under ADR-0022. An external URL alone is not a retained evidence reference.
- SQLite Lite and PostgreSQL use the same schema and state machine. Provider-specific fields stay inside receipt metadata.
- The current content-free `CognifyJob` queue is a compatibility input only. It does not become a projection receipt because it stores dataset names and lease metadata without a source revision or per-backend outcome.
- Existing Obsidian `SourceRevision` records require an explicit compatibility mapper. Their connector-local revision number is not the cross-source ledger ID.
- Erasure writes a new tombstone revision and preserves chain verification. It does not edit a historical receipt in place.
- A newly accepted revision updates one current head and marks predecessor projection receipts stale. Retrieval accepts current heads only. Physical deletion is separate cleanup and cannot decide acceptance success.
- Manual HTTP, CLI, and MCP notes without an external key use a Citadel key derived from dataset plus content SHA-256.

Verification:

- Contract tests serialize and reject unknown schema versions for every record.
- Crash injection after source retention, transaction commit, lease acquisition, each backend write, each receipt write, and searchability check converges in a second process.
- Duplicate submission produces one source revision, one job per generation and projection version, and one receipt per required backend.
- Empty-generation rebuild produces matching source, job, receipt, vector, and graph censuses.
- Expected implementation scope: new lifecycle model and store modules, then bounded changes to `kb/service.py`, `kb/cognee_client.py`, `kb/server.py`, `kb/mcp_server.py`, `kb/cli.py`, and focused unit, crash, serialization, provider, HTTP, CLI, and MCP tests.
- Approval gate: schema and migration implementation starts only after this v1 field set, state machine, file scope, and destructive test boundaries are approved.

## CITADEL-INT-RETRIEVAL-01: Citadel-owned retrieval evidence

Interface ID: CITADEL-INT-RETRIEVAL-01
Owner: architect
Provider: Citadel retrieval boundary with Cognee and Qdrant as implementation details
Consumers: HTTP search, CLI, MCP, ranking, agent trace, benchmarks, and document drill-down
Status: In Progress

Request or input:

- Authenticated identity, authorized dataset set, generation ID, query text or normalized vector, result limit, optional source filter, timeout, and one versioned `RetrievalProfile`.
- `RetrievalProfile`: `schema_version`, `profile_id`, `profile_version`, `query_type`, provider, logical collection, raw score kind, raw score direction, normalization rule, ranking rule, filter contract, limit, timeout, model, dimensions, and configuration digest.

Response or output:

- `RetrievalCandidate`: candidate ID, generation, dataset, source revision, projection receipt, document, chunk, raw provider point ID, evidence text, provider, raw score, raw score kind and direction, provider rank, and profile identity.
- `RetrievalHit`: candidate ID plus final rank, normalized Citadel score when defined, ranking reason fields, source locator, capture fingerprint, trust tier, content hint, and document drill-down identity.
- `RetrievalTrace`: trace ID, operation ID, identity reference, generation, authorized datasets, query digest, profile identity, provider attempts, candidate IDs, exclusion decisions, returned hit IDs, partial-failure fields, timestamps, and duration. Raw query retention follows instance policy and never leaves the instance as fleet telemetry.
- Empty success means every authorized provider path completed and returned zero candidates. A partial provider failure returns `partial=true`, typed failures, and only the hits from completed paths.

Errors:

- Missing scope, conflicting dataset, unknown generation, invalid profile, provider timeout, malformed candidate, missing source revision, stale projection receipt, and score-direction mismatch are typed failures.
- Authorization failure occurs before any provider request. Provider exceptions do not become empty results.

Events:

- Retrieval started, authorization resolved, provider completed, provider failed, candidate excluded, hit ranked, trace completed, and trace failed.

Compatibility rule:

- Cognee `1.4.1` CHUNKS envelopes are unwrapped at the adapter boundary. Nested payload text, `document_id`, document name, chunk index, source chunk ID, importance weight, and stored dataset identity map into candidates.
- Qdrant cosine similarity is converted to Cognee distance as `1 - similarity`, with `lower_is_better`. Citadel normalization is a separate named rule. Raw distance is never labelled as a normalized relevance score.
- A candidate is provider output. A hit has passed authorization, projection-receipt, provenance, and ranking checks. The two types are not aliases.
- Existing `_citadel` response fields remain a compatibility surface until HTTP, CLI, and MCP consumers move to the versioned hit and trace schema.

Verification:

- Serialization fixtures cover Cognee dataset envelopes, flat CHUNKS payloads, missing optional fields, malformed nested payloads, and typed provider failures.
- Two-seat tests cover search, hydrate, direct retrieve, delete, prune, graph aggregation, CLI, and MCP with same and distinct raw IDs.
- Score tests prove Qdrant similarity, Cognee distance, normalized score, and final rank use named directions and do not swap units.
- Benchmark output records the profile, candidate census, exclusions, partial failures, returned hits, and trace ID for every query.
- Approval gate: retrieval schema implementation starts with the lifecycle schema because candidates must reference durable source revisions and projection receipts.

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
