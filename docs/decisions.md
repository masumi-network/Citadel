# Architecture Decisions

## DEC-2026-08-10-02: Publish one digest-first multi-platform Citadel image

Date: 2026-08-10
Owner: architect

Context:

- VERIFIED: `.github/workflows/publish.yml` publishes Python artifacts and a GitHub Release but contains no OCI build, registry login, image push, digest receipt, SBOM, provenance, or attestation step.
- VERIFIED: `docker-compose.yml` accepts `CITADEL_IMAGE`, and `kb/local_deploy.py:24,221` rejects a published image unless it contains `@sha256:<64 lowercase hexadecimal characters>`.
- REPORTED: the user approved finishing and shipping the production release on 2026-08-10.

Options:

- Publish mutable `latest`, major, and minor image aliases. Rejected because deployments can change without a configuration diff.
- Build Python and OCI releases in separate tag workflows. Rejected because partial ordering and source-identity drift become harder to prove.
- Stage one multi-platform image by commit digest inside the existing release workflow, publish PyPI, then promote the same index to the exact version tag and create the GitHub Release last. Selected.

Decision:

- Publish `ghcr.io/masumi-network/citadel` for `linux/amd64` and `linux/arm64` from Docker target `production`.
- Build once as `sha-${GITHUB_SHA}`, verify the index and both runtime imports, emit BuildKit max provenance plus SBOM, and create a GitHub OIDC provenance attestation for the index digest.
- After PyPI succeeds, copy the same index to the exact package version, such as `0.5.0`. Never publish `latest`, major, or minor aliases.
- The GitHub Release is last and includes wheel, sdist, and a receipt containing the exact image version, index digest, and source SHA.

Consequences:

- A failure before exact-version promotion leaves only a commit-stage image. A failure after promotion is corrected by a forward patch release; the version tag is never moved or overwritten.
- Compose and local deploy continue to consume a version plus digest reference. No Compose interface change is required.
- GitHub attestation is the v0.5 signing mechanism. A separate Cosign signature is deferred until a consumer requires it.
- Provenance does not make unpinned Python transitive dependencies reproducible. Release dependencies still require exact pins or reviewed bounds.

Evidence:

- `docs/interfaces.md` CITADEL-INT-SELFHOST-01
- [GitHub container publishing](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
- [Docker build attestations](https://docs.docker.com/build/ci/github-actions/attestations/)

## DEC-2026-08-10-01: Bind Ladybug extension state through its connection setting

Date: 2026-08-10
Owner: architect

Context:

- VERIFIED: production image `sha256:e2b30a1645b84b64f6c06d11c08f7fb2f3498fc71a7a766e620642943b705ce4` runs UID `10001` with a read-only root. Fresh Compose boot logged `Failed to create directory /home/citadel/.lbdb/extension/0.18.1/linux_arm64/`, then `/readyz` returned HTTP `503`.
- VERIFIED: exact Ladybug `0.18.1` commit `1354081eb5528b3ca12e38dd4402cdd47215e57a` initializes each connection's `homeDirectory` from `HOME`, derives extension storage as `{homeDirectory}/.lbdb/extension/{version}/{platform}/`, and exposes the per-connection setting `home_directory`.
- VERIFIED: exact Cognee `1.4.1` opens throwaway, direct, and subprocess Ladybug connections and executes JSON extension install or load without setting `home_directory`. Evidence: `cognee_db_workers/_kuzu_helpers.py:33-69`, `cognee_db_workers/kuzu_worker.py:128-134,178-198`, and `cognee/infrastructure/databases/graph/ladybug/adapter.py:404-417,490-504` in `/private/tmp/cognee-core-v1.4.1-qdrant-audit`.

Options:

- Change process `HOME` to `/data`. Rejected because Ladybug has a narrower supported setting and changing `HOME` affects unrelated libraries and Citadel paths.
- Add a writable mount at `/home/citadel`. Rejected because it spreads mutable provider state outside the declared `/data` boundary and leaves direct image use dependent on an extra mount.
- Patch the exact Cognee wheel build to apply Ladybug `home_directory` before every install or load. Selected.

Decision:

- Lite runtime owns `LADYBUG_HOME_DIRECTORY`. Its default is `<CITADEL_LITE_DATA_ROOT>/ladybug-home`, and it must resolve inside the Lite data root.
- Citadel's exact Cognee wheel patch applies that value through Ladybug's connection setting before JSON extension installation or loading in throwaway, direct, and subprocess paths.
- Process `HOME` remains `/home/citadel`. Ladybug extension state belongs below `/data`; temporary warm-up databases remain below `/tmp`.

Consequences:

- The read-only production root remains intact while Ladybug extension state persists with the Citadel data volume.
- The Ladybug extension directory is rebuildable provider cache and is not part of the current generation backup manifest.
- Fresh extension installation still downloads an upstream artifact. Offline fresh-volume restore remains unproven until a separate reviewed image-packaging decision pins or preloads that artifact.

Correction:

- CORRECTED on 2026-08-10: the failed earlier image contained Ladybug `0.18.1`, but a clean candidate build resolved supported patch release `0.18.2`. Official comparison from `0.18.1` commit `1354081eb5528b3ca12e38dd4402cdd47215e57a` to `0.18.2` commit `d25703bd326a54f9f23e8a1c4879480798e72d9f` changes no client-context, setting, or extension-install source file. The release pins `0.18.2`; fresh Docker evidence must use that exact version.

Evidence:

- `docs/interfaces.md` CITADEL-INT-SELFHOST-01
- `agents/blockers.md` BLK-2026-08-10-01
- [Ladybug 0.18.1 client context](https://github.com/LadybugDB/ladybug/blob/1354081eb5528b3ca12e38dd4402cdd47215e57a/src/main/client_context.cpp)
- [Ladybug 0.18.1 settings](https://github.com/LadybugDB/ladybug/blob/1354081eb5528b3ca12e38dd4402cdd47215e57a/src/main/settings.cpp)

## DEC-2026-08-09-03: Provision required datasets without bootstrap content

Date: 2026-08-09
Owner: architect

Context:

- VERIFIED: production startup calls `ensure_session_traces_dataset` from `kb/server.py:550`. The missing-dataset path ingests a marker at `kb/server.py:2577-2582`.
- VERIFIED: lifecycle is enabled by default at `kb/config.py:477-480`. Lifecycle ingest starts projection at `kb/service.py:138-186`, and its worker calls cognify at `kb/lifecycle_worker.py:336-382`.
- VERIFIED: the recovered Compose log records the bootstrap marker at `21:35:03.483`, the cognify pipeline at `21:35:09.311`, repeated OpenRouter authentication failures, and repeated `/readyz` HTTP `503` responses.
- VERIFIED: `CogneePublicClient.ensure_dataset` at `kb/cognee_client.py:3099-3150` provisions the relational dataset row and permissions without opening the graph or ingesting content.

Options:

- Keep the bootstrap marker and require a real LLM key for empty readiness. Rejected because startup would create application data and make control-plane health depend on model output.
- Disable lifecycle or the projection worker during readiness. Rejected because that would test a different runtime contract and could hide queue defects.
- Provision the required dataset row and permissions directly. Selected.

Decision:

- Empty-generation startup calls the existing relational-only dataset provisioner for `session-traces`.
- Startup does not ingest a marker, create lifecycle source revisions, schedule projection, or call a model.
- Seeded functional gates remain responsible for proving ingestion, cognification, graph extraction, and model quality with the approved real model configuration.

Consequences:

- Fresh offline Compose readiness can use a dummy LLM key plus `COGNEE_SKIP_CONNECTION_TEST=true` without generating knowledge data.
- Existing generations that already contain the old marker are not rewritten or deleted by this decision.
- Shared trace behavior is unchanged after a user-approved share writes real trace content.

Evidence:

- `agents/blockers.md` BLK-2026-08-09-06
- `/private/tmp/citadel-ring0-recovered-app-20260809.log`
- `docs/interfaces.md` CITADEL-INT-SELFHOST-01

## DEC-2026-08-09-02: Use an offline manifest for SQLite Lite recovery

Date: 2026-08-09
Owner: architect

Context:

- VERIFIED: `scripts/smoke_qdrant_provider.py` copies two SQLite databases sequentially, snapshots one `DocumentChunk` collection, omits Ladybug state, and restores from a Qdrant server-local path.
- VERIFIED: the Lite runtime already rejects a second writer with a single-instance lock. Evidence: candidate `kb/lite_runtime.py` and its runtime tests.
- INFERRED: with one Citadel process and zero replicas, stopping the writer and acquiring that same lock creates the smallest coherent backup boundary.

Options:

- Keep online provider-by-provider copies. Rejected because writes can cross the separate SQLite, graph, and Qdrant capture times.
- Add a distributed snapshot epoch. Deferred because the Lite profile has one writer and does not need distributed coordination.
- Stop the writer, acquire its lock, inventory the whole generation, and seal one manifest. Selected.

Decision:

- INFERRED: v0.5 Lite backup is an offline operation. It copies both SQLite databases, Ladybug and retained-data files, and downloaded snapshots for every generation Qdrant collection.
- INFERRED: restore accepts a sealed manifest and empty targets. It verifies every digest before writing and refuses overwrite.
- INFERRED: operator-selected backup and restore paths are containment boundaries. Symbolic-link targets and backup destinations nested under copied source trees are rejected before writes; created directories and files use private modes.
- INFERRED: release acceptance requires booting a fresh Citadel process against restored local state and a fresh Qdrant volume, then retrieving exact markers.

Consequences:

- Operators get a short write outage during backup.
- Backup artifacts are private to the creating account by default. Operators must explicitly relax permissions when transferring them through another controlled mechanism.
- The implementation stays compatible with the one-process Lite constraint.
- A future multi-writer PostgreSQL profile needs its own coordinated backup decision.

Evidence:

- `docs/interfaces.md` CITADEL-INT-BACKUP-01
- `agents/blockers.md` BLK-2026-08-09-02

## DEC-2026-08-09-01: Make the Citadel lifecycle ledger the write authority

Date: 2026-08-09
Owner: architect

Context:

- VERIFIED: the legacy path writes Cognee source before a separate content-free JSON cognify queue. A process can stop between those writes. Evidence: the pre-implementation audit in `.local-review/research/lifecycle-implementation-audit.md`.
- VERIFIED: Cognee `1.4.1` accepts an explicit `DataItem.data_id`. Candidate tests show that a deterministic source revision ID lets a restarted worker reconcile an uncertain provider return.
- REPORTED: the user approved lifecycle v1 implementation in the disposable candidate on 2026-08-09.

Options:

- Keep Cognee source plus the JSON queue as acceptance authority. Rejected because the two writes cannot commit together and the queue has no source revision or backend receipt.
- Add lifecycle tables to Cognee's relational database. Rejected because Citadel would couple its contract and migrations to a private dependency schema.
- Use a dedicated Citadel SQLite ledger with retained source bytes and deterministic provider identities. Selected for the single-process Lite release.

Decision:

- Source revision, retained bytes, current-head update, projection job, and initial relational, vector, and graph receipts commit in one `BEGIN IMMEDIATE` transaction.
- One deterministic job identity covers source revision, generation, and projection version. Deterministic receipt identities add the backend. Provider operation IDs remain evidence only.
- Provider calls run outside the acceptance transaction. Each receipt becomes searchable only after a bounded read check. Five failed worker attempts produce a terminal failed job by default.
- Source replacement marks predecessor jobs and receipts stale. Retrieval accepts managed hits only when they reference the current head and a searchable vector receipt.
- Empty-generation rebuild reads current heads and queues target-generation work idempotently. Generation census reports current source, job, receipt, backend, and searchable counts.

Consequences:

- CORRECTED: commit `275e433d08251f4642d26e2136d8fa9e5e2193c1` is the initial lifecycle implementation checkpoint. Commit `5bdcf89` is the reviewed local checkpoint.
- The lifecycle SQLite file joins backup inventory and online restore verification.
- Versioned retrieval candidate, hit, profile, and trace records remain separate work. Lifecycle v1 exposes only current-head and receipt binding through the compatibility response.
- This decision does not authorize a runtime restart, remote push, Railway deployment, production migration, merge, release, or deletion.

Evidence:

- VERIFIED: `uv run pytest -q` returned `1847 passed, 3 skipped, 11 warnings in 25.27s` in `/private/tmp/citadel-v050-qdrant`.
- VERIFIED: `uv run ruff check .` returned `All checks passed!`; `git diff --check` returned no output.
- VERIFIED: implementation entry points are candidate `kb/lifecycle.py:373`, `kb/lifecycle.py:673`, `kb/lifecycle.py:1344`, `kb/lifecycle_worker.py:53`, and `kb/service.py:412`.
- CORRECTED: later review expanded the regression set. Candidate `5bdcf89` returned `1867 passed, 3 skipped, 11 warnings in 42.65s`; explicit Qdrant live commands returned `1 passed, 11 warnings in 5.85s` and `1 passed in 34.55s`.

## DEC-2026-08-08-07: Make SQLite Lite the v0.5 default

Date: 2026-08-08
Owner: architect

Context:

- VERIFIED: Cognee `1.4.1` defaults its relational provider to SQLite and also supports PostgreSQL. Its SQLite adapter uses WAL and a 120 second busy timeout. Evidence: exact Cognee source at `cognee/infrastructure/databases/relational/config.py:17-24`, `create_relational_engine.py:51-72`, and `sqlalchemy/SqlAlchemyAdapter.py:75-101` in `/private/tmp/cognee-core-v1.4.1-qdrant-audit`.
- VERIFIED: Railway documents mounted volumes as suitable for SQLite and includes SQLite files in volume backups. Railway volumes cannot be attached to replicas. Evidence: [Railway agent storage guidance](https://docs.railway.com/guides/running-agents-on-railway), [volume limits](https://docs.railway.com/volumes/reference), and [volume backups](https://docs.railway.com/volumes/backups).
- VERIFIED: Qdrant remains a separate vector service under either relational profile. The Cognee Qdrant adapter implements `VectorDBInterface`; it does not replace Cognee relational tables.
- VERIFIED: DigitalOcean App Platform does not support persistent volumes. Self-hosted Qdrant and the embedded graph therefore need a Droplet or another volume-capable target. Evidence: [DigitalOcean App Platform limits](https://docs.digitalocean.com/products/app-platform/details/limits/).
- VERIFIED: Railway charges by resource use. PostgreSQL adds its own memory, CPU, and volume use. SQLite adds storage to the app volume already required for graph state. Evidence: [Railway pricing](https://docs.railway.com/pricing/plans). Blind spot: no 24 hour Citadel run has measured the actual PostgreSQL resource delta.

Options:

- INFERRED: require PostgreSQL everywhere. This gives one relational behavior but adds a service and operator surface to local and small single-node installs.
- INFERRED: require SQLite everywhere. This is cheapest and simplest, but it does not cover multiple application processes or a future horizontally scaled control plane.
- INFERRED: keep one image and schema contract with explicit Lite and Standard relational profiles.

Decision:

- REPORTED: the user requires Qdrant in every deployment and wants the relational database selectable before deployment.
- REPORTED: the user selected the lower-cost Qdrant plus SQLite profile for Railway.
- INFERRED: Lite uses SQLite and is the v0.5 default for local Docker, Railway, and a single DigitalOcean Droplet. It permits one host, one Citadel process, one scheduler, and zero replicas.
- INFERRED: PostgreSQL remains an explicit later profile for multiple processes and higher write concurrency. It is not a v0.5 Railway release gate.
- INFERRED: selection is explicit through `--database sqlite|postgres` or a named platform template. Citadel never changes providers based on environment detection.

Consequences:

- The current fresh Railway project will use SQLite Lite. Its unused PostgreSQL service is not deleted without a separate destructive-action confirmation.
- Local and DigitalOcean Compose use SQLite by default and add PostgreSQL through an override profile.
- SQLite acceptance requires a startup refusal for replicas or a second app host, concurrent ingest and scheduler tests, abrupt-stop recovery, online backup plus restore, migration parity, and exact source and receipt census.
- PostgreSQL acceptance remains later work and does not block the SQLite Lite release.
- DigitalOcean App Platform is not a v0.5 target. The DigitalOcean target is a Droplet bootstrapped with the canonical Compose bundle.

Evidence:

Exact Cognee `1.4.1` source is in `/private/tmp/cognee-core-v1.4.1-qdrant-audit`. Platform evidence comes from [SQLite appropriate uses](https://www.sqlite.org/whentouse.html), [Railway pricing](https://docs.railway.com/pricing/plans), and [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets). The deployment contract is `docs/interfaces.md` CITADEL-INT-SELFHOST-01.

## DEC-2026-08-08-06: Choose Qdrant tenant storage shape

Date: 2026-08-08
Owner: architect

Context:

- VERIFIED: the raw official adapter stores the raw Cognee point ID in a shared logical collection and keeps dataset identity only in payload. This causes same-ID overwrite.
- REPORTED: the implementation reviewer identified a smaller fix than one physical collection per dataset. The adapter can derive the stored Qdrant ID from `(generation, dataset, raw_id)`, preserve the raw ID in payload, and translate it at every boundary.
- REPORTED: the independent reviewer accepted dataset-namespaced IDs as an alternative to removing the shared collection, provided retrieve, delete, prune, rollback, and migration paths pass adversarial tests.
- VERIFIED: the official adapter already creates a tenant payload index for `database_name` at pinned source lines 125-134. Blind spot: the current source does not namespace IDs or scope direct-ID operations.

Options:

- INFERRED: one physical collection per generation, dataset, and logical Cognee type. This gives the strongest storage separation but increases collection count and diverges further from the official adapter.
- INFERRED: one physical collection per generation and logical Cognee type, with dataset-namespaced point IDs plus mandatory tenant filters. This is a smaller upstream patch and avoids collection growth per seat. A missing filter remains a security defect, so direct operations and prune must be tested fail-closed.

Decision:

- REPORTED: goal continuation resumed without an A or B response.
- INFERRED: proceed with generation collections, dataset-namespaced point IDs, and mandatory tenant filters as the working decision. This follows the recorded recommendation and keeps work moving without external state changes.
- INFERRED: physical per-dataset collections are the automatic fallback if any same-ID, direct-operation, prune, rollback, migration, or visibility test fails.

Consequences:

- CITADEL-INT-QDRANT-01 records candidate B as active and candidate A as fallback.
- No adapter implementation is commit-ready until the adversarial isolation tests pass.

Evidence:

- Official adapter source: `/private/tmp/cognee-community-7311/packages/vector/qdrant/cognee_community_vector_adapter_qdrant/qdrant_adapter.py:125-134,159-215,247-306,400-403`
- `.local-review/research/cognee-1.4.1-feature-audit.md`
- Reviewer handoffs recorded in `status.md`
- VERIFIED 2026-08-09 implementation checkpoint: candidate commit `5bdcf89` keeps one physical collection per generation and logical Cognee type, derives stored IDs from generation plus dataset plus raw ID, and filters every operation by generation and dataset. New collections use `m=0`, `payload_m=16`, and a tenant keyword index.
- VERIFIED 2026-08-09 real-server checkpoint: same raw ID in Alice and Bob remained isolated across count, retrieve, search, delete, prune, container replacement, and fresh-process lifecycle retrieval on Qdrant `1.19.0`. Commands returned `1 passed, 11 warnings in 5.85s` and `1 passed in 34.55s`.
- VERIFIED official sources: [Qdrant multitenancy](https://qdrant.tech/documentation/guides/multiple-partitions/), [Qdrant Docker quickstart](https://qdrant.tech/documentation/quickstart/), and [Cognee Qdrant integration](https://docs.cognee.ai/setup-configuration/community-maintained/qdrant). Blind spot: production TLS, monitoring, coherent backup, and hosted operation remain unverified.

## DEC-2026-08-08-05: Patch the official Qdrant adapter before release

Date: 2026-08-08
Owner: architect

Context:

- VERIFIED: the exact Cognee `1.2.2` to `1.4.1` audit is recorded in `.local-review/research/cognee-1.4.1-feature-audit.md`, SHA-256 `165189d0ac418e138685755e18930360a6f7e5bd7123807d70f6f9b59f70f022`.
- VERIFIED: Cognee `1.4.1` adds useful migration, authorization-ordering, provenance, and force-mode behavior, but its per-dataset mutation lock remains process-local. Evidence: the exact-tag source links and blind spots in the audit.
- VERIFIED: official community adapter PR `#149` commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29` rebuilds indexed payloads without Citadel's required `DocumentChunk` reference fields, leaves raw-ID retrieve and delete operations without a dataset filter, and converts query exceptions to empty results. Evidence: `.local-review/research/cognee-1.4.1-feature-audit.md` and the pinned adapter source cited there.
- VERIFIED: a disposable Qdrant local-mode probe wrote the same point UUID under Alice and then Bob. Alice search changed from one hit to zero, while Alice raw retrieve returned Bob's dataset name and text. Blind spot: local mode proves point and filter semantics, not real-server performance or the complete Citadel path.
- VERIFIED: Cognee `1.4.1` declares `cryptography>=43,<50`; Citadel requires the fix in `50.0.0`. A locally rebuilt Cognee wheel with only the upper metadata bound changed to `<51` returned `All installed packages are compatible`. Blind spot: the wheel is local and unpublished.

Options:

- Import the official adapter unchanged. Rejected because the verified payload, isolation, and failure contracts violate the release gates.
- Return to the Cognee `1.2.2` custom-adapter plan. Rejected because it contradicts the user's selected core upgrade and would discard useful `1.4.1` fixes.
- Use the official adapter as source base, then maintain a narrow reviewed patch until upstream publishes equivalent fixes. Selected for the isolated shadow.

Decision:

- REPORTED: Cognee `1.4.1` remains the selected v0.5 shadow core.
- INFERRED: pin the official adapter source commit as the review baseline, but register only the audited Citadel patch in the release image.
- INFERRED: enable Cognee backend access control for per-dataset authorization. Treat it as necessary but insufficient.
- CORRECTED by DEC-2026-08-08-06: the earlier decision required one physical collection per generation and dataset. The active working boundary uses one collection per generation and logical type, dataset-namespaced stored IDs, raw Cognee IDs in payload, and mandatory dataset filters. Physical per-dataset collections remain the automatic fallback.
- INFERRED: normalize Cognee's dataset-wrapped CHUNKS results at the Citadel boundary and aggregate only authorized per-dataset graphs for existing consumers.
- INFERRED: preserve Citadel's durable queue, source and projection receipts, cross-process writer controls, and whole-generation rollback evidence.

Consequences:

- CORRECTED: DEC-2026-08-08-04's plan to test the official dataset handler before deciding on physical collections was too weak. The same-ID probe shows that handler metadata alone is not an isolation boundary.
- CORRECTED AGAIN: DEC-2026-08-08-06 identifies dataset-namespaced stored IDs as a smaller candidate boundary. Physical per-dataset collections remain the conservative fallback until the user decides.
- CORRECTED: DEC-2026-08-08-03's Cognee `1.2.2`, EBAC-disabled custom route remains research history, not the active implementation plan.
- The compatibility branch is not commit-ready until plain pip, the built wheel, and Railway-style installation resolve without a uv-only override.
- The adapter must preserve all Cognee CHUNKS fields, scope search, retrieve, delete, count, scroll, and hydration, and surface outages as typed failures.
- Prune must affect only the current generation and authorized dataset collections.
- Set `TELEMETRY_DISABLED=true` and keep external payload tracing disabled in every release target until a separate privacy review accepts them.
- No deployment, production migration, or old-data deletion follows from this decision.

Evidence:

- `.local-review/research/cognee-1.4.1-feature-audit.md`
- `.local-review/research/cognee-rs-awesome-ai-memory.md`
- `.local-review/wayfinder/tickets/009-prove-qdrant-adapter-and-portable-release.md`
- Disposable compatibility worktree: `/private/tmp/citadel-v050-qdrant`

## DEC-2026-08-08-04: Upgrade Cognee and use the official Qdrant adapter

Date: 2026-08-08
Owner: architect

Context:

- REPORTED: the user replaced the earlier Cognee `1.2.2` adapter choice with an explicit request to upgrade Cognee and use the official community Qdrant adapter.
- VERIFIED: PyPI reports Cognee `1.4.1` as the current release. Its metadata requires `cryptography>=43,<50`.
- VERIFIED: community adapter PR `topoteretes/cognee-community#149` pins adapter `0.3.0` to Cognee `1.4.1` at commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29`. The PR is open and marked draft. Its Qdrant integration and contract checks passed, while the overall PR still had an unrelated Valkey failure when inspected.
- VERIFIED: resolving Cognee `1.4.1`, the adapter commit, and Citadel's `cryptography>=50,<51` floor returned `No solution found`. The upstream set without Citadel's floor resolved to `cryptography==49.0.0`.
- VERIFIED: `pip-audit` reported `CVE-2026-69247`, severity high, against `cryptography==49.0.0`, with `50.0.0` as the fix. Citadel will not lower its security floor for this upgrade.
- VERIFIED: a disposable environment forced to `cryptography==50.0.0` imported Cognee `1.4.1`, registered the official adapter, passed Citadel's private API boot contract, and returned `10 passed, 10 warnings` from the adapter unit suite. Citadel's focused Cognee client suite returned `80 passed, 1 failed, 10 warnings`; the failure is a changed dataset-creation return contract, not an import failure.

Options:

- Keep Cognee `1.2.2` and finish the custom adapter. Superseded by the user's explicit dependency choice.
- Install the upstream `1.4.1` dependency set with `cryptography==49.0.0`. Rejected because the current audit reports a high vulnerability with a fixed version available.
- Use Cognee `1.4.1`, the exact official adapter commit, and a minimal Cognee metadata patch that permits tested `cryptography==50.0.0`. Selected for the isolated compatibility branch. The patch must be proposed upstream or pinned to a reviewable Citadel fork before release packaging.

Decision:

- REPORTED: upgrade Citadel to Cognee `1.4.1` and use the official community Qdrant adapter as the implementation base.
- INFERRED: keep the official adapter commit exact until PR `#149` merges and a compatible adapter release is published.
- INFERRED: preserve `cryptography>=50,<51`. Do not publish a dependency set that resolves to `49.0.0`.
- INFERRED: treat the adapter's dataset handler and Cognee backend access control as the first authorization candidate. It must pass Citadel's two-seat tests before replacing the current scope design.
- INFERRED: current production remains pinned and untouched. The upgrade applies only to the isolated v0.5 worktree and later shadow generation.

Correction:

- CORRECTED: DEC-2026-08-08-05 supersedes the official-adapter-first authorization experiment. DEC-2026-08-08-06 then selects dataset-namespaced stored IDs as the working boundary and physical per-dataset collections as the automatic fallback. An audited patch remains mandatory.

Consequences:

- DEC-2026-08-08-01's Cognee `1.2.2` adapter choice is superseded. Its fresh-generation and rollback decisions remain current.
- DEC-2026-08-08-03's task-local custom adapter design is a fallback, not the active implementation route.
- Cognee `1.4.1` adds `data_cache=True` to cognify. Citadel force mode must set both `incremental_loading=False` and `data_cache=False`.
- Cognee `1.4.1` exports `run_migrations`, not `run_startup_migrations`. Citadel startup must call the actual pinned symbol and fail closed.
- The official adapter still requires Citadel acceptance tests for search error propagation, CHUNKS payload fields, direct-ID scope, delete scope, and same-ID behavior. Passing upstream tests does not prove these Citadel contracts.

Evidence:

Evidence comes from [Cognee PyPI metadata](https://pypi.org/pypi/cognee/json), [community adapter PR #149](https://github.com/topoteretes/cognee-community/pull/149), and the [cryptography advisory](https://github.com/advisories/GHSA-g6cj-pr64-35w5). Disposable files remain at `/private/tmp/citadel-cognee-141-compat` and `/private/tmp/cognee-community-7311`.

## DEC-2026-08-08-01: Build a fresh Qdrant-backed Citadel generation

Date: 2026-08-08
Owner: architect

CORRECTED on 2026-08-08: Cognee `1.2.2` and PostgreSQL below record the original decision. Later user direction selected Cognee `1.4.1` with the audited adapter patch and SQLite Lite for the first Railway candidate. DEC-2026-08-08-05 and DEC-2026-08-08-07 are current.

Context:

- REPORTED: the user selected Qdrant as Citadel's next vector backend and selected a new Railway project as the first hosted target.
- VERIFIED: current production contains accepted memory that is not recoverable from the manifest-only backup mirror. Evidence: `kb/backup_mirror.py:188-194` and `kb/backup_mirror.py:285-320`.
- VERIFIED: the official Cognee Qdrant package cannot be installed with Citadel's declared Cognee range. Community adapter `0.3.0` pins `cognee==1.1.0`; Citadel declares `cognee>=1.2.2,<1.3.0`. Evidence: official community commit `f7c30c9c9f48275cfd758d57f3f8458bbd9e43e5`, `packages/vector/qdrant/pyproject.toml:1-11`, `pyproject.toml:41`, and `requirements.txt:6`.
- VERIFIED: the official guide says to call `register()`, but published package `0.2.4` exposes `register` as an importable module whose import performs registration. The disposable probe printed `register_type module` and calling it raised `TypeError: 'module' object is not callable`.
- VERIFIED: with Qdrant server `1.18.1` and Python client `1.18.0`, an unscoped search over two points returned both `seat:a` and `seat:b`; applying `node_name=['seat:a']` returned only `seat:a`. Citadel currently disables Cognee backend access control and does not pass `node_name`, so the raw adapter does not satisfy seat isolation.

Options:

- Use the published Cognee Qdrant adapter unchanged. Rejected because its dependency, authorization, payload, and failure contracts do not match Citadel.
- Upgrade Cognee and adopt a newer community adapter branch. Rejected for this migration because it combines two provider changes and conflicts with Citadel's current cryptography floor.
- Build an audited Citadel adapter against Cognee `1.2.2`, then start a new whole generation. Selected.

Decision:

- REPORTED: Qdrant is the selected vector direction.
- INFERRED: the candidate will use a Citadel-owned adapter compatible with exact Cognee `1.2.2`. It must preserve Cognee CHUNKS payloads and distance semantics, apply mandatory dataset filters, and raise provider failures.
- SUPERSEDED: the original hosted candidate used independent PostgreSQL, Qdrant, graph storage, secrets, and volumes. DEC-2026-08-08-07 replaces PostgreSQL with SQLite Lite for v0.5.
- INFERRED: bootstrap order is GitHub Central first, then one seat at a time. Each step must pass corpus, authorization, retrieval, restart, and identity gates.
- INFERRED: current production remains the rollback archive. No old database, vector store, graph, volume, project, or memory is deleted by this decision.

Consequences:

- The raw community adapter is research input, not a production dependency.
- A fresh relational generation avoids old Cognee pipeline statuses skipping the new Qdrant projection.
- Direct notes, seat notes, session traces, promoted memory, and generated memory require an explicit source export if they must survive cutover.
- `mcp-server-qdrant` may be offered later as an optional read-only diagnostic service. It must not bypass Citadel authorization, source lineage, retrieval policy, or audit.
- Deleting the old deployment needs a later explicit approval after restore rehearsal and a rollback window.

Evidence:

Evidence comes from the [Cognee Qdrant guide](https://docs.cognee.ai/setup-configuration/community-maintained/qdrant), Qdrant's [deployment](https://skills.qdrant.tech/qdrant-deployment-options/SKILL.md) and [multitenancy](https://skills.qdrant.tech/qdrant-multitenancy/SKILL.md) guidance, the [official Qdrant MCP server](https://github.com/qdrant/mcp-server-qdrant), and `.local-review/wayfinder/tickets/008-migrate-vector-projection-to-qdrant.md`.

## DEC-2026-08-08-02: Use self-hosted Qdrant for the portable shadow proof

Date: 2026-08-08
Owner: architect

Context:

- VERIFIED: Qdrant's installation guide recommends Qdrant Cloud for production and assigns persistence, security, recovery, and monitoring to self-hosted operators. Evidence: `.local-review/research/qdrant-search-engineering-deployment.md`.
- CORRECTED: the active user goal now requires an isolated Railway candidate, a one-click Railway Template, and a one-command local Docker Compose setup. Render moved to later scope.
- VERIFIED: the repository currently has no Qdrant service definition for Railway or local Docker. Evidence: `railway.toml` and `rg --files` on 2026-08-08.

Options:

- INFERRED: use Qdrant Cloud for the first shadow. This reduces storage operations but does not prove the portable self-hosted release requested by the user.
- INFERRED: use a pinned self-hosted Qdrant container for the Railway shadow and portable release proof. This exercises the required deployment shape but requires persistence, snapshot, restore, security, and monitoring evidence.
- INFERRED: support both in the first shadow. This expands the first proof before the adapter contract exists.

Decision:

- REPORTED: use a pinned self-hosted Qdrant container for the isolated Railway shadow and for the Railway and local portability proof.
- INFERRED: Qdrant Cloud remains an optional later production topology. It is not required to complete the portable shadow proof.
- INFERRED: the Railway shadow is not production evidence until its persistent volume, authenticated access, snapshot export, destructive disposable restore, restart, and provider-outage gates pass.

Consequences:

- INFERRED: pin the Qdrant image by version and digest, keep it off the public network, mount primary storage and snapshots separately, and disable remote snapshot URL recovery.
- INFERRED: do not promote the self-hosted shadow until the adapter and full recovery suite pass.
- INFERRED: keep current PGVector production unchanged during the shadow proof.

Evidence:

- `.local-review/research/qdrant-search-engineering-deployment.md`
- `plans/roadmap.md`
- `.local-review/wayfinder/tickets/009-prove-qdrant-adapter-and-portable-release.md`

## DEC-2026-08-08-03: Scope Qdrant with task-local context and physical collections

Date: 2026-08-08
Owner: architect

Context:

- VERIFIED: Cognee `1.2.2` says dataset search is available only when backend access control is enabled. Citadel keeps that feature disabled because its current Kuzu graph and organization mesh use one global graph. Evidence: pinned Cognee `modules/search/methods/search.py:54-77` and `.env.example:246-252`.
- VERIFIED: Cognee `belongs_to_set` stores NodeSet names, not dataset membership. Evidence: pinned Cognee `modules/graph/methods/delete_data_nodes_and_edges.py:81-88`.
- VERIFIED: re-adding an existing Cognee `Data` row replaces `external_metadata`. A reserved NodeSet marker can therefore drift when one content row belongs to more than one dataset. Evidence: pinned Cognee `tasks/ingestion/ingest_data.py:158-199`.
- VERIFIED: Python ContextVars propagate into the child tasks Cognee creates for vector indexing. Evidence: pinned Cognee `tasks/storage/index_data_points.py:30-68` and Python task-context behavior used by Citadel's existing `suppress_inline_cognify` guard.

Options:

- INFERRED: enable Cognee backend access control and use its dataset database handler. Rejected because it also changes graph storage and CHUNKS response shape, which expands the first vector migration into a graph and API migration.
- INFERRED: authorize from a reserved `belongs_to_set` marker. Rejected because NodeSet metadata is not durable dataset membership.
- INFERRED: make Citadel's already-authorized dataset a task-local adapter scope and store each dataset in separate physical Qdrant collections. Selected.

Decision:

- INFERRED: keep Cognee backend access control disabled for the first Qdrant generation.
- INFERRED: wrap each single-dataset cognify, search, retrieve, and delete operation in a Citadel Qdrant scope containing mode, generation, and exactly one authorized dataset.
- INFERRED: write Cognee's internal projection plus a separate physical dataset projection. Citadel reads only the physical dataset projection. Each physical collection and point payload records generation and dataset scope.
- INFERRED: reject missing, empty, nested-conflicting, or multi-dataset read and mutation scope before a Qdrant request.
- INFERRED: keep `belongs_to_set` for Cognee graph semantics and diagnostics. Never use it as the Citadel authorization authority.

Consequences:

- INFERRED: the cognify queue must execute one dataset at a time so one task context cannot stamp another dataset's projection.
- INFERRED: physical collections prevent same-ID writes in one dataset from replacing another dataset's vector or payload.
- INFERRED: direct Qdrant access remains operator-only because Qdrant credentials can bypass Citadel's scope resolver.
- INFERRED: collection count grows with dataset count and indexed Cognee types. The candidate census and operational limits must measure this before release.

Evidence:

- `docs/interfaces.md` CITADEL-INT-QDRANT-01
- `.local-review/wayfinder/tickets/009-prove-qdrant-adapter-and-portable-release.md`
