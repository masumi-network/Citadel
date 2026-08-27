# Citadel as a governed data substrate

**Date:** 2026-08-03
**Status:** decided in session, not yet implemented
**Supersedes:** nothing. Extends the product framing in `CONTEXT.md` and `README.md`.

This records what was decided, why, and what evidence each decision rests on. Claims are tagged **VERIFIED** (measured or read in the session that produced this document, with the command or file path given), **REPORTED** (a source said so and it was not independently reproduced), or **INFERRED**.

Nothing here characterises the current enforcement strength of any access-control path. That belongs in the private audit, not in a public repository.

---

## 1. Positioning

Citadel is a **governed data substrate**: an ingest gate, an evidence store, access control with an audit trail, and a query interface, with pluggable connectors in and pluggable surfaces out.

Organisation memory is the first application built on it. Regulatory reporting is the second. Both run on one core.

The alternative framings were considered and rejected. Staying an org-memory product with compliance as a feature leaves nothing for a white-label partner to wrap. Repositioning hard onto reporting throws away the agent-native angle and puts us on established compliance vendors' ground. Splitting into two products doubles the narrative and support surface for one small team.

**Consequence for language:** the product is general and the pilot is specific. A proposal or a pitch names one vertical as the demonstration and keeps the substrate as the innovation. These are different sections of the same document, not a choice between them.

## 2. Deployment model

**One organisation, one instance. Always.**

Three ways a partner gets one:

1. They self-host on their own infrastructure.
2. We host a dedicated instance for them under a data processing agreement.
3. An association, accountancy, or consultancy stands up one instance per member organisation under its own brand.

Organisation signup automatically provisions a dedicated instance. Organisations are never co-located.

**Why this matters more than it looks:** separation between customers becomes a property of the deployment rather than a claim about the code. A procurement officer can verify a deployment boundary. Verifying a code-level guarantee requires an audit they will not commission.

**What it does not solve:** isolation between seats inside a single organisation is unchanged by this decision. Per-organisation instances remove the organisation-to-organisation question entirely and leave the within-organisation one exactly where it was.

## 3. The control plane is a separate system

Signup, provisioning, per-instance lifecycle (create, upgrade, suspend, delete, export), domain assignment, secret management, billing, and fleet telemetry live **outside** Citadel.

Citadel stays a single-organisation application and must never learn that other instances exist. The moment the application knows the fleet exists, "organisations are never mixed" reverts from a deployment property to a code claim, which is the property this whole model exists to avoid.

**Hardest ongoing problem, named now:** fleet upgrades. One instance is a deploy. Two hundred instances is a migration campaign. `requirements.txt` already records that the cognee pin exists because a minor version bump could move private internals that ADR-0009 dataset attribution depends on, failing closed and blanking a caller's vault. That is a support ticket at one instance and an incident at fleet scale. It is an independent argument for §5.

**Placement:** v1.0 territory, after the dogfood and benchmark gate. Not in v0.5.0.

## 4. Store model: index in place, retain what matters

Citadel reads from the systems a partner already runs. Nothing has to be migrated first.

Durable retention of the evidence behind a cited claim becomes core rather than deferred. `CONTEXT.md` already defines Source Snapshot in two forms, a v1 minimal pointer and a target full form holding retained evidence for reprocessing, and explicitly defers the second. That deferral ends: the full form is what the audit trail rests on.

**Correction to an assumption made during the session:** Citadel does not currently hold pointers only. `RepoContentFile.content` and the Obsidian sync's `SourceRevision(... body=body ...)` copy full text at sync time, unconditionally. **VERIFIED** by source inspection. The design decision is therefore about *which* retained copy is attested and citable, not about introducing retention.

## 5. The retrieval engine sits behind our own interface

cognee stays as the first implementation of an interface Citadel defines, rather than being called directly.

The decisive evidence is that ranking cannot come from upstream. In cognee v1.4.1, `retrieve()` still hardcodes `score=0` and `SearchResultPayload` is byte-identical to our pinned version; the upstream PR that would have exposed real scores and a distance cutoff was closed unmerged. **VERIFIED** by byte-diffing v1.4.1 against the installed 1.2.2.

So the interface is not architecture for its own sake. It is where scoring has to live because upstream will not provide it, and it is where provenance stamping belongs for the same reason.

It also derisks §3: a pinned upstream behind our own contract is far safer to upgrade across a fleet than a pinned upstream called directly from forty places.

## 6. Graph store moves to PostgreSQL, and it is one project with dataset scoping

**Correction, recorded because the wrong version circulated first.** An earlier claim in this session was that Citadel runs the archived `kuzudb/kuzu` in production. **That was wrong.** cognee resolves `GRAPH_DATABASE_PROVIDER=kuzu` to `LadybugAdapter`; the installed distribution requires `ladybug>=0.16.0,<0.18` and no kuzu at all, and the `kuzu` package inside cognee's wheel is a two-file shim whose body is `from ladybug import *`. Ladybug is the MIT-licensed successor. **VERIFIED** by reading the installed distribution metadata and source.

The `kuzu` string in `.env.example` and `docs/operations.md` is a stale label. Renaming it to `ladybug` costs nothing and stops the false alarm recurring.

**The real case for moving is the lock.** Ladybug keeps embedded single-writer file-lock semantics. Upstream issue #3708, *"Kuzu worker crash leaves orphaned database lock, blocking all subsequent recall() calls"*, names our exact pinned version. The merged fix has several components and our pin has only some of them. **VERIFIED** by reading both the issue and the installed source. `PostgresAdapter` uses an in-process lock only, so that failure class disappears rather than being mitigated.

**Cheapest next measurement, not yet taken:** grep production logs for `Lock is held by PID`. Until that runs, #3708 is a well-matched candidate for the observed stall history and not a diagnosis.

**Why it is one project with dataset scoping, corrected.** The first version of this argument was that upstream declares PostgreSQL as one of the graph backends supporting multi-user partitioning, which implies partitioning is a capability the move buys. **That does not hold.** Upstream registers a per-dataset database handler for the current backend too: `supported_dataset_database_handlers` maps `ladybug` and `kuzu` to the same `LadybugDatasetDatabaseHandler`, alongside `postgres_graph` for the PostgreSQL provider. **VERIFIED** by reading `cognee/infrastructure/databases/dataset_database_handler/supported_dataset_database_handlers.py` in the installed distribution. Partitioning is therefore reachable without moving the store.

They are one project for a different reason: both changes rewrite the same five graph call sites and both land on the same search result shape, so doing them separately means doing the work twice and re-verifying every caller against a shape that pass two invalidates. The reason to move the store is still the lock, per the paragraph above. Per-dataset context on an embedded graph gives one file lock per dataset, which multiplies the failure class this move exists to remove rather than removing it.

**Costs, priced before commitment:** five graph read sites in `kb/` resolve the default engine and would need to run inside a per-dataset context; the org-wide read becomes O(datasets) sequential queries with node-id dedup; the search result shape changes; `DATABASE_MAX_LRU_CACHE_SIZE` defaults to 6 and would thrash with thirteen datasets, tearing down a connection pool per dataset per read; per-dataset database creation at runtime needs `CREATEDB` on the application role, which must be checked against managed PostgreSQL before anything else; and the data side is a full re-cognify rather than a migration. **VERIFIED** by source inspection.

Plan the re-cognify as one job with the existing unreachable-document backlog, since it is the same operation.

## 7. Three traces, all in v1

**Evidence trace.** A figure resolves to the document, to the retained copy, to a fingerprint attested at capture time.

**Process trace.** Who did what, when, with what outcome, durably rather than as a live view that resets.

**Agent trace.** Which sources an assistant retrieved and which shaped its output. This is the differentiated one; very little on the market answers "which document made the model say that".

**Slice-1 contract:** `2026-08-19-agent-decision-trace-schema.md` defines the first durable event model for autonomous decision loops. It links model calls, arbitration outcomes, replacement events, and repair attempts in one trace.

**Integrity mechanism:** a hand-rolled SHA-256 signed hash chain, with the W3C PROV entity/activity/agent model as the vocabulary. The `prov` Python library is MIT and actively released. Marquez, DataHub, and OpenMetadata were all rejected: Marquez has no tagged release in nearly two years, DataHub needs Kafka plus MySQL plus OpenSearch and phones home by default, and OpenMetadata's Python client is under a non-OSI licence. **VERIFIED** by licence and release-date checks.

**The threat model the chain does not close:** a signed chain proves nobody outside altered the evidence. It does not prove the operator did not rewrite and re-sign it. The cheap upgrade is submitting the chain head to the **public Rekor transparency log**, whose Python client is Apache-2.0 and currently released. Self-hosting Rekor means Trillian plus a database plus a signer, to prove what a signed chain plus one public submission proves.

**On-chain anchoring is out of v1.** It buys the same property as Rekor with a crypto dependency on the compliance-critical path and worse reception from conservative buyers. Masumi is instead investigated for **agent identity**, since agent trace needs a credential model and seat tokens are not one. That investigation must verify against Masumi's live API rather than its skill description.

## 8. Component choices

Each slot has one named component rather than a shortlist.

| Slot | Choice | Rejected, and why |
|---|---|---|
| Document parsing | **Docling** (MIT, actively released, local-first, native OCR and table structure) | Unstructured: vendor states new features go to the paid API. Tika: JVM sidecar. markitdown: no native OCR. pymupdf4llm: AGPL. |
| Connectors | **dlt** (Apache-2.0, in-process) | Airbyte: Elastic Licence 2.0 core, Kubernetes-first. Meltano: CLI and subprocess model. |
| Lineage vocabulary | **W3C PROV** via the `prov` library | See §7. |
| Telemetry | **OTel GenAI semantic conventions** plus **OpenLLMetry** instrumentation | Langfuse: multi-service stack, and enterprise-tier compliance telemetry that cannot be disabled. Phoenix: ELv2, not OSI open source. |
| Inference | **Ollama or llama.cpp** self-hosted, **Mistral La Plateforme** (paid tier, contractual no-training) as EU hosted fallback | Aleph Alpha: acquired by Cohere, so the independent-EU-provider claim no longer holds. Nebius: EU is one region of three. |

All licence and release-date claims **VERIFIED** against the repositories and package indexes on 2026-08-03.

**This replaces the current external model path.** Five features call an external model service today: graph construction on ingest, optional enrichment, promotion classification, the organisation digest, and the self-improvement pass. Only two are behind a switch. A single deployment-wide switch covering every call site is required work, not a nicety, because the sovereignty claim depends on it. **VERIFIED** by call-site inspection.

## 9. Scope decisions

**Core, must be excellent:** ingest gate, evidence store, governance, the query interface, activity and telemetry.

**Modules, kept and demoted:** connectors, surfaces (MCP, CLI, web), the retrieval engine, evolve.

**Folded in:** Vault Backup Mirror. A manifest-only export is strictly weaker than retained evidence plus a signed chain, and maintaining both gives two competing answers to "where is the durable copy".

**Parked, not extended:** shared session traces (worth nothing to a reporting deployment), and the Knowledge Mesh concept canvas (the hardest piece of the frontend migration, and under this positioning an *evidence* graph earns its place where a *concept* graph is decoration).

**Frontend:** finish `/next`, retire `kb/static`. The duplication is why four published figures drifted and then went stale, and a white-label theme cannot be maintained twice.

**Evolve:** keeps running, on derived structure only. It is forbidden from touching retained evidence or anything a report cites, so the evidence layer is append-only by construction and an auditor's trace is immune to it.

## 10. Connector and extension model

Common sources are configured declaratively: endpoint, auth, field mapping, schedule, against a small set of adapter types. A non-technical partner adds a source without shipping code.

**Webhooks and an HTTP contract are in v1**, not deferred. A connector can be any service that speaks the contract, which gives partner isolation and language independence.

Python entry points remain the escape hatch for anything exotic.

## 11. Roadmap and release gates

**Stage one, stabilisation.** Upgrade cognee to the current upstream release, absorbing the `get_max_chunk_tokens` sync-to-async breaking change that the release notes deny (**VERIFIED** by diff). Re-measure the chunk budget in the embedder's own token units, since upstream fixed the wordpiece/BPE tokenizer mismatch that mis-sized every chunk. Move the graph store and dataset scoping together per §6. Re-cognify the unreachable backlog. *Exit: every open issue closed, and a full end-to-end run on both MCP and CLI.*

**Stage two, validation.** Benchmark and stress test against the Masumi organisation's live vault. *Exit: published numbers for search latency, ingest throughput, and cost of operating, generated by a named harness.*

**Stage three, the evidence layer.** Capture-time attested fingerprints, per-item provenance, contradiction and staleness detection, export packages. *Exit: a figure resolves end to end to a record a third party can verify without trusting the node.*

**Stage four, the white-label product.** Control plane, connector and webhook contract, single-switch EU or on-premise inference, theming. *Exit: a partner deploys, brands, and connects their own source without us writing code.*

**v0.5.0** ships when stage one is complete, every issue is closed, and end to end works on MCP and CLI. **v1.0.0** comes after stage two's validation, not before.

## 12. Research findings that drove these decisions

**EU reporting obligations.** CSRD is finished as an SME pain: Directive (EU) 2026/470 raised thresholds to EUR 450 million turnover and 1,000 employees, both required, and made the voluntary SME standard a legal ceiling on what a large customer may demand from a supplier under 1,000 employees, with an explicit right to decline. **VERIFIED** against the EUR-Lex text. E-invoicing is the largest genuine SME pain, mandatory in several member states with no size exemption, but the incumbent field is mature and consolidating.

**Recommended pilot vertical: agriculture, anchored on EUDR.** It is the only candidate where the regulation names micro and small enterprises and dates them separately, at 30 June 2027, with large and medium operators at 30 December 2026. **VERIFIED** from a Commission source. It is a provenance problem by construction: a due diligence statement is valid only if plot geolocation, evidence against a fixed baseline date, supplier attestations, and the chain of reliance on upstream statements all hold together and can be re-examined. Lower-risk alternative: energy under EED Article 11, cleanest overlap profile, though its key figures are REPORTED and unverified against the directive text.

**Funding.** A Digital Europe call on regulatory compliance through data closes 1 October 2026 and its scope text asks for audit trails, access control, a data governance framework, open source, and interoperability with government systems. It requires three beneficiaries in three countries, one named high-burden vertical, and a live demonstration under realistic operational conditions. **VERIFIED** from the call fiche. Nothing has been committed to it.

## 13. Not decided

- The pilot vertical, formally. Agriculture is recommended, not confirmed.
- Hosting substrate for the control plane. Decided by measured unit cost of one organisation instance per month, across Hetzner, Scaleway, and Railway, at the reference workload from the production benchmark.
- Inference topology, which will dominate that unit cost: Mistral API per instance, one shared self-hosted service (cheapest, but content crosses the instance boundary), GPU per instance (probably prices SMEs out), or CPU-only quantized models per instance.
- Pricing bands. The shape is settled: free software, charge for hosted instances annually per instance, connector development at fixed scope, and support.
- The seat and licensing model under per-organisation instances.
- What the organisation-memory product becomes for the Masumi team once Citadel is a substrate.

## 14. Related records

Architecture decisions arising from this document: ADR-0020 (graph store and dataset-scoped reads), ADR-0021 (retrieval interface owns ranking and provenance), ADR-0022 (evidence layer), ADR-0023 (control plane boundary). All four are indexed in [`docs/adr/README.md`](../../adr/README.md).

Plans that execute this document, and where to go next:

- [`2026-08-04-citadel-execution-plan.md`](2026-08-04-citadel-execution-plan.md) orders every open issue into the four stages of section 11 and states an exit criterion for each. Start here.
- [`2026-08-04-graph-store-migration-runbook.md`](2026-08-04-graph-store-migration-runbook.md) executes ADR-0020 as nine gates, beginning with the privilege check that decides whether the plan holds its shape.
- [`2026-08-04-control-plane-design.md`](2026-08-04-control-plane-design.md) designs the system ADR-0023 puts outside the application, and carries the cost model that decides where instances run.

Existing records this builds on: ADR-0003 (seat, node, central), ADR-0009 (mesh read isolation), ADR-0010 (structured knowledge as durable source of truth), ADR-0012 (attested trust versus content hint), ADR-0015 (one process owns the graph), ADR-0017 (structural provenance outranks inherited trust).

Partner-facing summary of the same positioning: `docs/eu-partner-proposal.md`.
