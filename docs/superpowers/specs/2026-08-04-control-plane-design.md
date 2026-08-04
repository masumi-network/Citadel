# The control plane, and what one organisation instance costs

**Date:** 2026-08-04
**Status:** design, not implemented. Placement is v1.0 territory per the substrate design doc §3.
**Decides:** the shape of the system [ADR-0023](../../adr/0023-control-plane-outside-the-application.md) said had to exist outside Citadel, and the cost model that picks its hosting substrate.
**Source of truth for decisions:** [`docs/superpowers/specs/2026-08-03-citadel-substrate-design.md`](2026-08-03-citadel-substrate-design.md).

Claims are tagged **VERIFIED** (read or run in the session that produced this document, with the file and line given), **REPORTED** (a source said so and it was not independently reproduced), or **INFERRED** (reasoned from something else stated here). An untagged sentence is a design statement, not a claim about the world.

Nothing here describes how any access-control, isolation, or scoping path currently behaves under attack. That belongs in the private audit.

---

# Part one: the design

## 1. The boundary

ADR-0023 chose a dedicated deployment per organisation, provisioned automatically, with the provisioning system living outside the application (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:13`). This section says exactly where the line falls.

**The control plane owns:** signup and organisation records; provisioning and deprovisioning; the per-organisation artifact list in §2 and its versioning; region placement; domain assignment; generation, storage, and rotation of per-instance bootstrap secrets; the upgrade campaign in §4; fleet telemetry collection in §5; billing; the lifecycle state machine in §3; the export and deletion obligations at exit.

**Citadel owns:** everything a single **Organization Vault** does for one organisation. Ingest, the **Learning Process**, **Structured Knowledge**, the **Knowledge Index** and **Knowledge Mesh**, retrieval, **Seat** and **Node** structure, **Central**, activity, the dashboard, MCP, CLI. It owns its own configuration surface, and every value in it is meaningful to a deployment that has never heard of another deployment.

**The invariant, stated as a rule that can be applied to a pull request:**

> Citadel gains no configuration and no code for any of this. A setting that names another instance, a credential valid at more than one instance, a registry or directory the application can query, and any code path whose behaviour depends on how many instances exist are all forbidden.

That prohibition is ADR-0023's, restated here because it is the thing this design is most likely to erode by accident (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:32`).

The reason is not tidiness. Separation between organisations is a property of the deployment only while the application has no fleet awareness. The moment Citadel can enumerate, address, or reason about other instances, the property reverts to a claim about code, and a claim about code is the thing a small buyer cannot check and will not pay to have audited. The boundary is the product feature.

**The test for a new configuration key.** If the value must differ per organisation *and* Citadel never reads it, it belongs to the provisioner's contract. If Citadel reads it, it has to mean something to a single-organisation deployment reading it in isolation. `CITADEL_DEFAULT_DATASET` passes: one vault, one default. A hypothetical `CITADEL_FLEET_REGISTRY_URL` fails on both halves.

**How the control plane acts on an instance without the application knowing.** Through the hosting substrate (create service, set variables, deploy, snapshot, destroy) and through Citadel's own already-public HTTP surface using one instance-scoped credential minted during provisioning. The application sees an ordinary authenticated caller. It learns nothing about where the call came from, and it must not be given a way to ask.

**What the control plane never holds:** vault content. Its own store holds organisation records, instance records, region, plan, current version, lifecycle state, secrets for its own operations, and billing. No **Source Snapshot**, no document text, no query text, no per-member identifiers beyond what billing genuinely requires. §5 says the same thing about telemetry, and the two constraints are the same constraint applied at two moments.

## 2. What one instance is

This is the provisioner's unit of work, enumerated from what the repository asks a human to do today. Every row is **VERIFIED** by reading the cited file in this session.

| Artifact | Detail | Source |
|---|---|---|
| Build | `builder = "RAILPACK"`; dependencies install from `requirements.txt`, so a new runtime dependency has to be added there | `railway.toml:2`; `docs/operations.md:28-29` |
| Web service | one, `startCommand = "python -m scripts.run_railway"`, `CITADEL_RUN_MODE=web` (the default), which runs `uvicorn kb.server:app --host 0.0.0.0 --port $PORT` | `railway.toml:5-10`; `docs/operations.md:25-28` |
| Health gate | `healthcheckPath = "/healthz"`, `healthcheckTimeout = 30` | `railway.toml:11-12` |
| Restart policy | repo default `ON_FAILURE` with `restartPolicyMaxRetries = 3`; any cron-style service must be set to `NEVER`, because an evolve pass exits nonzero when any stage failed and would otherwise re-run the whole pass up to three more times | `railway.toml:13-14`; `docs/operations.md:108-111` |
| Scheduled roles | same image and entry command, role selected by `CITADEL_RUN_MODE`: `pipeline`, `github-sync`, `learning-agent`, `linear-sync`, `backup-mirror`, `evolve`; recommended first shape is a daily `learning-agent` and a `backup-mirror` export, with `evolve` documented as a six-hourly cron service mounting the same `/data` volume and carrying its own stage toggle | `railway.toml:5-9`; `docs/operations.md:25-27`, `:34-35`, `:94-97` |
| Database | one PostgreSQL dedicated to the instance, `pgvector` enabled, with `CREATE EXTENSION IF NOT EXISTS vector` applied before production ingest | `docs/operations.md:36`, `:51-55` |
| Database binding | `DATABASE_URL`; Citadel derives cognee's split `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`, `DB_PASSWORD` from it, and maps them into `VECTOR_DB_*` when `VECTOR_DB_PROVIDER=pgvector` | `docs/operations.md:39-43` |
| Volume | one, mounted at `/data`, with `SYSTEM_ROOT_DIRECTORY=/data/.cognee_system` and `DATA_ROOT_DIRECTORY=/data/.data_storage` | `docs/operations.md:37`, `:45-49` |
| What lives on the volume | the embedded graph files; the access store at `CITADEL_ACCESS_STORE_PATH=/data/.citadel/access.json`; `github_sync_state.json` and `backup_mirror/`, which is why any scheduled service also mounts `/data` | `docs/operations.md:37`, `:113-116`, `:164` |
| Dataset defaults | `CITADEL_TENANT_ID`, `CITADEL_DEFAULT_DATASET`, `CITADEL_SEARCH_DEFAULT_DATASET` | `docs/operations.md:126-130` |
| Graph provider | `GRAPH_DATABASE_PROVIDER` | `docs/operations.md:46` |
| Model binding | `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_MODEL`, `LLM_API_KEY`, with the provider-prefixed model id the operations reference insists on | `docs/operations.md:137-142` |
| Bootstrap secrets | `CITADEL_READER_KEYS`, `CITADEL_WRITER_KEYS`, `CITADEL_ADMIN_KEY`, each generated random (the reference gives `python -c "import secrets; print(secrets.token_urlsafe(32))"`), with the server refusing to start on an env key shorter than 32 characters | `docs/operations.md:150-166` |
| Audit sizing | `CITADEL_AUDIT_MAX_EVENTS` | `docs/operations.md:165` |
| Optional feature group | the promotion variables (`CITADEL_PROMOTION_ENABLED`, `CITADEL_PROMOTION_DRY_RUN`, `CITADEL_PROMOTION_RELEVANCE_THRESHOLD`, `CITADEL_PROMOTION_MAX_ITEMS`) and `CITADEL_EVOLVE_PROMOTION_ENABLED` | `docs/operations.md:84-97` |
| Domain | one per instance | `docs/adr/0023-control-plane-outside-the-application.md:27` |
| Region | chosen at creation, not a deployment default, because a stated region is contractual | `docs/adr/0023-control-plane-outside-the-application.md:29` |

Two consequences of that table are worth pulling out.

**Secret generation is a provisioning step with a floor, not a form field.** Three key groups have to be generated per instance and never typed, and the server enforces a minimum length on them (**VERIFIED**, `docs/operations.md:154-166`). A provisioner that generates them is strictly better than a human who copies them, and it is the only version of this that scales past the first few organisations.

**Co-tenancy of app and database is already an operational instruction.** The operations reference tells the operator to keep app and database in the same project and environment so the database stays private to Citadel (**VERIFIED**, `docs/operations.md:114-116`). Under ADR-0023 that instruction becomes a provisioning invariant rather than advice.

### 2.1 The volume dependency on ADR-0020

[ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md) moves the graph store onto PostgreSQL and states that the `/data` volume stops being where the graph lives, leaving open whether the volume is needed for anything else (**VERIFIED**, `docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md:35`).

**Marked as a dependency:** whether the provisioner creates a volume at all is settled by ADR-0020, not by this document (**VERIFIED**, ADR-0023 says exactly this at `:27`).

It does not settle itself, because the volume currently carries three residents beyond the graph: the access store, the sync state file, and the backup mirror export directory (**VERIFIED**, `docs/operations.md:113-116`, `:164`). Removing the volume therefore requires a decision about each of those, not only about the graph. **INFERRED** from those two reads: after ADR-0020 lands, the volume question becomes "do the access store and the sync state move into PostgreSQL", which is a separate change with its own migration.

ADR-0020 adds one provisioning-contract item of its own. `DATABASE_MAX_LRU_CACHE_SIZE` has to be raised and has to be set in the deployment environment, because it must be set before cognee is imported (**VERIFIED**, `docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md:30`). ADR-0020 also retires the `GRAPH_DATABASE_PROVIDER=kuzu` label as a stale name (**VERIFIED**, same file `:15`, `:35`), so the provisioner's value for that key changes when it lands.

**The provisioning contract is a versioned artifact.** Because the table above reproduces what a human is currently asked to do, any change to it is a release of the contract, with a version number the control plane records per instance. That is what makes "instances are identical by construction" checkable rather than aspirational.

## 3. Lifecycle states

Five states. For each: what exists, what runs, what the customer can do, what we owe them.

**provisioning**
- *Exists:* the organisation record, an instance record with region and contract version, and the artifacts of §2 as they are created.
- *Runs:* the provisioner. The application may boot mid-sequence; it is not serving anyone yet.
- *Customer can:* watch progress. Nothing else.
- *We owe:* a bounded outcome. Either the instance reaches **active** or the provisioner rolls the partial instance back and leaves no orphaned database, volume, domain, or secret. A half-provisioned instance that lingers is the failure mode that turns a fleet into a set of snowflakes, which is the objection that killed hand provisioning in ADR-0023 (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:19`).

**active**
- *Exists:* everything in §2.
- *Runs:* the web service, the scheduled roles, the **Learning Process**.
- *Customer can:* everything the product does. Add **Seats**, connect sources, search, promote, export.
- *We owe:* the stated region, the availability commitment, the upgrade campaign in §4, the sub-processor list current, and the retention behaviour the contract names. The partner proposal already commits that data lives in standard PostgreSQL and the customer can take a dump whenever they like (**VERIFIED**, `docs/eu-partner-proposal.md:79`).

**suspended**
- *Exists:* the database, the volume if any, and the instance record. Compute is stopped.
- *Runs:* nothing. No scheduled roles, no evolve pass, no ingest, no external model calls.
- *Customer can:* reactivate, or request an export. They cannot search, because nothing is serving.
- *We owe:* a stated retention window before suspension can become termination, communicated before suspension starts, plus the ability to export during the window. Suspension is reversible by definition; if it is not reversible it is termination and has to be called that.

**terminating**
- *Exists:* the data, plus a running export job.
- *Runs:* the export only. Ingest is off, scheduled roles are off, external model calls are off.
- *Customer can:* collect the export.
- *We owe:* the exit obligation the partner proposal already makes: *"On termination of a hosted instance you get a final export and written confirmation of deletion"* (**VERIFIED**, `docs/eu-partner-proposal.md:79`). Termination does not complete until the export has been produced and made available. The export is a product artifact with a defined shape, not a database dump handed over informally, because the same proposal sells export packages carrying source links, timestamps, and fingerprints (**VERIFIED**, `docs/eu-partner-proposal.md:110`).
- *Open:* retention obligations can outlive the customer relationship. The partner proposal already flags that erasure either propagates to retained copies or records a legal-hold justification (**VERIFIED**, `docs/eu-partner-proposal.md:75`), and [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md) makes retention an obligation as well as a feature (**VERIFIED**, `docs/adr/0022-evidence-is-retained-and-attested-at-capture.md:29`). Which of the two applies at termination is contract-specific and is not decided here.

**terminated**
- *Exists:* the instance record, marked terminated, with the deletion confirmation and its timestamp. Nothing else: no database, no volume, no domain, no secrets.
- *Runs:* nothing.
- *Customer can:* nothing. A new organisation signup is a new instance.
- *We owe:* the written confirmation of deletion, and a record on our side that is auditable later without holding anything that was deleted.

**Two rules across the whole machine.** Every transition is recorded with actor, time, and reason, because the control plane's own log is the only place a lifecycle dispute can be settled. And the state a customer is in is visible to them, since a suspended instance that looks like an outage produces a support ticket instead of a payment.

## 4. Fleet upgrade strategy

This is the hardest ongoing problem in the model, and ADR-0023 says the strategy has to exist before the fleet does (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:25`).

### 4.1 Why an unattended bump changes severity with scale

`requirements.txt:1-6` pins cognee to the 1.2.x line and records why in a comment: dataset attribution reads private cognee internals that a 1.3.x could move, and the pin exists so the deploy platform cannot pull one unattended on the next build (**VERIFIED**, read this session). The comment also names the guard: `assert_cognee_dataset_api()`, described as a boot self-check plus a test that catches a bump.

Three facts about that guard, all **VERIFIED** by reading the source in this session:

1. It exists and it is called at boot, from the startup path at `kb/server.py:458`, and it is defined at `kb/cognee_client.py:97`.
2. It does more than import symbols. It also pins the *existence* of a field on cognee's `PipelineRunInfo`, added after a shape change was read as a dict key, got nothing, and left a sync unable to converge without failing loudly (`kb/cognee_client.py:114-121`).
3. **It logs and does not block startup.** The call site wraps it in `try/except` and emits `logger.error(...)` on failure, deliberately, so that startup is never blocked (`kb/server.py:457-465`).

Point three is the one that matters for a fleet. `GET /healthz` returns `{"ok": True, "service": "citadel"}` (**VERIFIED**, `kb/server.py:4132-4134`), so a healthy health check is not evidence that the self-check passed, and `healthcheckPath = "/healthz"` is what the platform gates a deploy on (**VERIFIED**, `railway.toml:11`). A rollout that watches only process liveness will report success on every instance in the ring while the condition the pin exists to prevent is being logged on all of them.

At one instance that is a support ticket someone notices. Across a fleet it is a simultaneous incident with no single rollback, which is ADR-0023's exact formulation (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:26`).

Two live facts make this concrete rather than theoretical. Issue #150 records that `pyproject.toml` says `<2.0.0` while `requirements.txt` says `<1.3.0` (**REPORTED**, from the issue list supplied for this document), and `requirements.txt` is the deploy path (**VERIFIED**, `docs/operations.md:29`), so the two files disagree about which versions are permitted and only one of them governs what ships. Separately, GitHub reports six Dependabot vulnerability alerts on the default branch, three high and three moderate: four against `postcss` and one against `sharp` in `package-lock.json`, and one against `diskcache` in `uv.lock` (**VERIFIED** by `gh api repos/masumi-network/Citadel/dependabot/alerts --paginate` on 2026-08-04). Dependency movement across a fleet is therefore not a hypothetical future problem; it is a queue that already exists.

### 4.2 The approach

**Pin the resolved set, not a range.** The provisioning artifact records an exact dependency resolution and an exact application version, and an instance is upgraded by moving it to a new artifact version. A range in a manifest means two instances provisioned a week apart are not the same instance, which destroys the property that makes staged rollout meaningful.

**Rings.**

- **Ring 0, the canary:** one instance we own, carrying real load. The Masumi organisation's own vault is the obvious candidate, since stage two of the roadmap already benchmarks against it (**REPORTED**, from the substrate design doc §11). A canary that carries no load proves nothing about an upgrade.
- **Ring 1:** instances that have opted into early upgrades, typically design partners, in exchange for a direct line when something breaks.
- **Ring 2:** everyone else, upgraded in batches, with a soak period between batches long enough for the slowest failure signal to appear.

The soak length is set by the slowest signal, not by convenience. A retrieval regression shows up on the next search; a **Learning Process** regression may only show up on the next scheduled pass. Note that the scheduler's real period is not the configured interval alone, and issue #153 records that every deploy resets it (**REPORTED**, from the issue list), so a rollout that redeploys a ring can delay the very signal it is waiting for. Fixing #153 is a precondition for a soak period to mean anything.

**The boot self-check as the gate.** The check already knows the failure it is looking for. To use it as a rollout gate, two things change, and neither of them is fleet awareness inside the application:

1. The result becomes readable from outside: a readiness surface that reports self-check status, and a version field so the control plane can tell what is actually running. Neither exists today (**VERIFIED**: `/healthz` returns a two-field literal at `kb/server.py:4132-4134`, and the version constant `__version__ = "0.4.0"` lives at `kb/__init__.py:17` with no route reading it in that handler).
2. The control plane promotes a ring only when every instance in the previous ring reports the check passing, not merely that the process is up.

Both are single-instance features. A single-organisation self-hoster benefits from the same readiness surface, which is the test that the boundary is intact.

**Version skew is a supported condition.** ADR-0023 already says so (**VERIFIED**, `:25`). It follows that the export format, the connector contract, and the webhook contract have to tolerate more than one application version in the fleet at once, and that a rollout stalled at ring 1 for a fortnight is a normal state rather than an incident.

**Rollback needs a data answer, not only a code answer.** Rolling an instance's code back is easy. Rolling back a change that rebuilt derived structure is not: ADR-0020 records that moving the graph store is a full re-cognify rather than a migration, since there is no export path from the embedded graph into the PostgreSQL adapter (**VERIFIED**, `docs/adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md:32`). For any upgrade that touches derived structure, the recovery plan is a snapshot taken before the campaign plus a rebuild, and the rebuild's duration per instance has to be measured before the campaign, not during it.

**Why [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md) is what makes this tractable.** Today the engine is called from many places: `kb/cognee_client.py` carries 45 `import cognee` statements across 19 functions, several of them on private paths (**REPORTED**, ADR-0021 states this at `:12-19`; the count was not re-run here). Validating a version bump against that surface means reviewing every call site, per release, forever. Behind the interface ADR-0021 defines, the question "does this version still work" becomes "does the adapter still pass its contract tests", which is a fixed amount of work that does not grow with the fleet (**VERIFIED**, `docs/adr/0021-retrieval-interface-owns-ranking-and-provenance.md:110-114`). ADR-0021 also notes that the boot self-check then guards one surface instead of standing in for a review of every call site, which is precisely the property a rollout gate needs.

**Sequencing that follows from all of the above:** the interface (ADR-0021) lands before the fleet exists, not after. Upgrading a fleet that was built on direct engine calls means paying the call-site review on every instance of every release.

## 5. Fleet telemetry

ADR-0023 fixes the policy: operational by default, never content, anything richer opt-in per instance and time-boxed (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:31`). This section says what each tier carries.

**Tier one, operational, always on.** Instance identity (the instance record's id, not the organisation's name), application version and provisioning contract version, region, lifecycle state, process health and restart count, resource use (CPU, memory, database size, volume size if a volume exists), HTTP error rates by status class, scheduled job outcomes with start time, end time, and result, and boot self-check status per §4. Aggregate counts where a count is genuinely operational: documents ingested, documents indexed, searches served. Counts, not subjects.

That last point needs care rather than good intentions. A counter named for what it appears to count is a recurring defect class in this codebase, which is why [ADR-0019](../../adr/0019-activity-counters-are-named-by-their-scope.md) exists. Every telemetry field is named for its scope, and a field whose meaning depends on where it was collected is a field that will be misread on a dashboard covering two hundred instances.

**Tier two, diagnostics, opt-in and time-boxed.** Enabled per instance, with an explicit expiry, and it turns itself off when the window ends rather than when someone remembers (**VERIFIED**, ADR-0023 `:31`). It carries what a support engineer needs and nothing more: stack traces with content redacted, timing breakdowns per stage, query shapes without query text, and identifiers that resolve only inside the instance. Enabling it is recorded in the instance's own audit trail, so the customer can see afterwards that it was on and for how long.

**Never, at any tier:** vault content, query text, **Node** or **Central** documents, **Structured Knowledge** pages, **Source Snapshot** bodies, and per-member identifiers. ADR-0023 names exactly this list (**VERIFIED**, `:31`).

**What the data processing agreement must disclose.** The categories in tier one, in the same words used here. That tier two exists, what it carries, that it is off unless enabled, how it is enabled, and that it expires. The sub-processor list covering the hosting substrate and any external model service, which ADR-0023 already makes product work rather than paperwork done later (**VERIFIED**, `:30`). The region per instance, and that it does not move. The retention period for telemetry, separately from the retention period for vault data, because they are different obligations with different lawful bases. Anything the inference topology in §9 changes about who processes what, which is why option B there is a document-level decision and not an implementation detail.

## 6. What we are not building

**This is not a multi-tenant application.** There is no organisation key on rows, no organisation predicate on queries, no tenant middleware, and no shared runtime process serving two organisations. ADR-0023 considered and rejected that option, on the ground that it converts every read path in the system into a security-critical path permanently (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:17`).

**There is no shared control database holding customer content.** The control plane's store holds organisation and instance records, lifecycle state, versions, region, billing, and its own secrets. It holds no vault content, no retained evidence, no search history, and no per-member content. A migration that moves customer content into it is a change of product model and would need its own ADR superseding ADR-0023.

**There is no shared component on the retrieval path.** No shared vector store, no shared graph, no shared cache holding results from more than one organisation.

**Named because it is genuinely tempting:** one shared inference service is the single cheapest line in the cost model and the one place a shared component would actually save real money. §9 option B prices it and states the tradeoff in the open rather than burying it.

**Not solved by any of this:** separation between **Seats** inside one organisation, which stays with [ADR-0003](../../adr/0003-seat-node-central-private-memory.md) and [ADR-0009](../../adr/0009-mesh-read-isolation-presence-vs-content.md) and is unchanged by anything in this document (**VERIFIED**, ADR-0023 states the scope limit at `:9-11` and `:13`).

---

# Part two: the cost model

## 7. The reference workload

The reference workload is **one organisation instance at the load the existing production node actually carries**.

**The figure is not determined in this document.** It comes from the benchmark harness the roadmap already requires: stage two exits on published numbers for search latency, ingest throughput, and cost of operating, generated by a named harness (**REPORTED**, substrate design doc §11), and the harness itself is issue #122, a retrieval eval harness with frozen question fixtures (**REPORTED**, from the issue list supplied for this document). Everything below is a rate card and a set of formulas. Multiplying them by a load figure is a later step and requires that measurement.

What the workload has to state, at minimum, for the four lines in §8 to resolve: sustained and peak concurrent searches, searches per day, documents and bytes ingested per day, total retained bytes and their growth rate, and tokens sent to and received from the model service per day, broken down by call site.

Two things are known about the shape and are worth recording so the measurement is not designed naively.

**Sizing compute is a measurement, not a guess.** Issue #105 reports that single-seat sequential search load wedges the whole node, with `/healthz` and `/readyz` hanging (**REPORTED**, from the issue list). Until that is fixed, a benchmark measures the defect rather than the workload, so #105 is a precondition for a compute number that means anything.

**Storage is not the corpus.** Issue #228 reports 892 of 2867 documents accepted but never vector-indexed (**REPORTED**, from the issue list). Documents that exist and are not indexed still occupy storage, and re-indexing them changes the index size without changing the document count. A storage figure taken today would be measuring a corpus mid-repair. Under [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md) the retained evidence layer grows with captured bytes rather than with index size (**VERIFIED**, `docs/adr/0022-evidence-is-retained-and-attested-at-capture.md:13`, `:29`), so the storage line has two independent growth drivers and has to be modelled as two.

## 8. The four cost lines

**Line 1, application compute.** One always-on web service per organisation (`railway.toml:10`, `docs/operations.md:25-28`, **VERIFIED**), plus scheduled roles that run on their own cadence (`docs/operations.md:34-35`, **VERIFIED**). The floor is set by the always-on service, since ADR-0023 accepts that an organisation ingesting nothing that month still pays for a web process, a database, a volume, and whatever inference topology is chosen (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:21`). Scheduled roles add duty-cycle cost on a substrate that bills by the second and nothing on a substrate that bills by the month.

**Line 2, PostgreSQL with pgvector, including storage growth.** One database per instance (`docs/operations.md:36`, **VERIFIED**), carrying the relational and vector halves of the **Knowledge Index**, and under ADR-0020 the graph half as well. Priced as a fixed node cost plus a per-GB-month storage cost that grows. Model the growth as: retained evidence bytes (ADR-0022), plus index and embedding bytes, plus backup or snapshot bytes, each with its own rate.

**Line 3, egress.** Search and dashboard responses, MCP and CLI traffic, the **Vault Backup Mirror** export while it exists, and the final export at termination, which is a single large transfer at the least convenient moment. Egress pricing differs structurally between the three candidates, not just in rate, which is why it gets its own line rather than being folded into compute.

**Line 4, model inference.** The substrate design doc records five features calling an external model service today: graph construction on ingest, optional enrichment, promotion classification, the organisation digest, and the self-improvement pass (**REPORTED**, substrate design doc §8, which tags it verified by call-site inspection there). This line dominates, and it is priced separately in §9 because the topology choice changes it by orders of magnitude.

## 9. Provider list prices

Every figure below is **REPORTED**: it is what the vendor published on the page named, fetched on **2026-08-04**. List prices change, and a price quoted here is evidence of what was published on that date and nothing more. Monthly figures marked *derived* are arithmetic on the published hourly rate at 730 hours per month and are **INFERRED**.

### Hetzner Cloud

Source: `https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/`, fetched 2026-08-04. The page states the adjustment applies to new orders and cloud instance rescales from 15 June 2026, 08:00 CEST. Its own note is "All prices are excluding VAT", and the cloud server figures are additionally quoted excluding IPv4. The page covers four regions, not two: Germany (FSN/NBG), Finland (HEL), USA (ASH/HIL) and Singapore (SIN), the last two with region-specific variation. The table below is the Germany and Finland column only, given as hourly and monthly cap.

| Plan | New price EUR (hour / month cap) |
|---|---|
| CX23 | 0.0088 / 5.49 |
| CX33 | 0.0136 / 8.49 |
| CX43 | 0.0256 / 15.99 |
| CX53 | 0.0473 / 29.49 |
| CAX11 | 0.0096 / 5.99 |
| CAX21 | 0.0168 / 10.49 |
| CAX31 | 0.0336 / 20.99 |
| CAX41 | 0.0657 / 40.99 |
| CPX22 | 0.0312 / 19.49 |
| CPX32 | 0.0569 / 35.49 |
| CCX13 | 0.0689 / 42.99 |
| CCX23 | 0.1378 / 85.99 |

Specifications, from `https://www.hetzner.com/cloud/cost-optimized/` fetched 2026-08-04 (that page renders its prices client-side and no price figures were readable, but the specification table was): CX23 and CAX11 are 2 vCPU, 4 GB RAM, 40 GB disk; CX33 and CAX21 are 4 vCPU, 8 GB, 80 GB; CX43 and CAX31 are 8 vCPU, 16 GB, 160 GB; CX53 and CAX41 are 16 vCPU, 32 GB, 320 GB. The CAX line is ARM.

**Not determined for Hetzner:** the price per GB per month for Volumes, the included traffic allowance per plan, and the overage price per TB. The volumes documentation states only that volumes have a monthly price cap and are billed hourly, with a 10 GB minimum and a 10 TB maximum per volume, and refers the reader to a pricing page whose figures did not render (`https://docs.hetzner.com/cloud/volumes/overview`, fetched 2026-08-04). *Cheapest measurement that would settle it:* open `hetzner.com/cloud/block-storage` in a browser that executes JavaScript, or read the authenticated Cloud API pricing endpoint with an account token, and record both figures with the date.

**Also not determined:** whether Hetzner publishes a managed PostgreSQL service. The pages read here list servers, volumes, and traffic. Until that is settled, a Hetzner-based model has to assume a self-operated PostgreSQL on a server, which moves backup, patching, and failover into our operational cost rather than the vendor's, and that labour is a real line in the model even though it is not a list price.

### Scaleway

Sources: `https://www.scaleway.com/en/pricing/virtual-instances/`, `https://www.scaleway.com/en/pricing/managed-databases/`, `https://www.scaleway.com/en/pricing/gpu/`, all fetched 2026-08-04, all quoted for the PAR-1 (Paris) zone. Prices are EUR before tax.

Instances:

| Instance | vCPU / RAM | EUR per hour | EUR per month (derived, 730 h) |
|---|---|---|---|
| STARDUST1-S | 1 / 1 GB | 0.0006 | 0.44 |
| DEV1-S | 2 / 2 GB | 0.00898 | 6.56 |
| DEV1-M | 3 / 4 GB | 0.0202 | 14.75 |
| DEV1-L | 4 / 8 GB | 0.04284 | 31.27 |
| BASIC2-A2C-4G | 2 / 4 GB | 0.023 | 16.79 |
| PRO2-XXS | 2 / 8 GB | 0.0561 | 40.95 |

Managed Database for PostgreSQL:

| Node | vCPU / RAM | EUR per hour (main node) | EUR per month (derived, 730 h) |
|---|---|---|---|
| DB-DEV-S | 2 / 2 GB | 0.0156 | 11.39 |
| DB-PLAY2-PICO | 1 / 2 GB | 0.0233 | 17.01 |
| DB-DEV-M | 3 / 4 GB | 0.0382 | 27.89 |
| DB-POP2-2C-8G | 2 / 8 GB | 0.1434 | 104.68 |

Database storage, using the page's own labels: Block Storage 5K at EUR 0.0993 per GB per month, Block Storage 15k at EUR 0.1489 per GB per month, and Backups/Snapshots at EUR 0.03 per GB per month.

Egress: the instances pricing page states that list prices include egress and IPv6 addresses, with no separate egress charge.

GPU, for §10: L4-1-24G, one L4 GPU with 8 vCPU and 48 GB RAM, EUR 0.79 per hour, which is EUR 576.70 per month derived at 730 hours. Larger L4 configurations scale close to linearly (2 GPU at 1.58/h, 4 at 3.15/h, 8 at 6.30/h). L40S, H100, and B300-SXM were listed as unavailable in PAR-1 and available in PAR-2, with no price shown in the PAR-1 view read here.

### Railway

Source: `https://railway.com/pricing`, fetched 2026-08-04. Prices are USD.

Plans: Free at $0 per month with $1 of monthly credits; Hobby at $5 per month including $5 of credits; Pro at $20 per month per workspace including $20 of credits; Enterprise custom. A 30-day trial gives $5 of credits.

Usage rates, published per second:

| Resource | Published rate | Per hour (derived) | Per month at 730 h (derived) |
|---|---|---|---|
| CPU | $0.00000772 per vCPU-second | $0.027792 | $20.29 per vCPU |
| Memory | $0.00000386 per GB-second | $0.013896 | $10.14 per GB |
| Volume | $0.00000006 per GB-second | $0.000216 | $0.158 per GB |

Egress: $0.05 per GB, stated as applying to services. Object storage: $0.015 per GB-month with free egress.

**Note on comparability, INFERRED:** Railway bills per second with no idle markup, Hetzner bills hourly with a monthly cap, and Scaleway bills hourly. For an always-on web service and an always-on database the three converge on a monthly figure. For the scheduled roles they do not, and a per-second substrate is structurally cheaper for a `learning-agent` that runs for minutes a day. Any comparison that models everything as always-on will overstate Railway and understate the value of the duty cycle.

**Not determined for any provider:** the actual instance sizes required. That is the §7 measurement, and picking a plan from the tables above before it exists would be a guess dressed as a model.

## 10. The inference variants

This line dominates the unit cost, so all four options are priced honestly rather than ranked by preference. The substrate design doc chose Ollama or llama.cpp self-hosted with Mistral La Plateforme (paid tier, contractual no-training) as the EU hosted fallback (**REPORTED**, substrate design doc §8).

Mistral list prices, from `https://mistral.ai/pricing/api` fetched 2026-08-04, in USD per million tokens (**REPORTED**): Mistral Small 4 at 0.15 in and 0.6 out; Mistral Large 3 at 0.5 in and 1.5 out; Mistral Medium 3.5 at 1.5 in and 7.5 out; Ministral 3 (3B) at 0.1 in and 0.1 out; Magistral Small at 0.5 in and 1.5 out. Mistral Embed at 0.1 per million input tokens. The page also states that batch processing gets a 50% discount.

### A. EU-hosted model API per instance

*Cost:* tokens per month per instance multiplied by the rate above. No fixed floor, no idle cost, no GPU.

*Status:* token volume per instance is **not determined**. *Cheapest measurement:* instrument the five call sites named in §8 line 4 and count input and output tokens per call site for one week on the production node, then divide by seven. The breakdown matters as much as the total, because the batch discount applies to the non-interactive paths (graph construction on ingest, the organisation digest, the self-improvement pass) and not to a search a human is waiting on.

*Separation:* content leaves the instance to a third party. That is a sub-processor to disclose, not a boundary violation, and it is the same relationship a self-hosting partner can decline by choosing option D.

*Note:* the ingest path is the volume driver, not search. Graph construction runs over document text; a search runs over a query. A model that assumes search dominates will be wrong by a large factor in whichever direction ingest happens to sit.

### B. One shared self-hosted inference service

*Cost:* one GPU amortised across N instances. At the Scaleway L4 rate above, EUR 0.79 per hour is EUR 576.70 per month derived at 730 hours, so EUR 5.77 per instance per month at 100 instances and EUR 57.67 at 10, before the throughput question of how many instances one L4 can actually serve, which is **not determined**.

*This is the cheapest option per instance at any meaningful fleet size, and it is the one this design cannot adopt quietly.*

**The tradeoff, stated here rather than buried.** Content from every instance crosses into a component shared by every organisation. Prompts carry document text, and under the ingest path they carry a lot of it. That is exactly the shape ADR-0023 rejected when it rejected a shared runtime substrate: anything genuinely shared makes the separation a property of configuration again, which is the property the whole model exists to avoid (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:18`). A separation claim that holds for storage, compute, and retrieval but not for the component every document passes through on its way in is a weaker claim than the one this design rests on, and a procurement officer who reads the sub-processor list will find it.

Three positions are available, and the choice is a product decision rather than an engineering one:

1. Do not build it. The separation claim stays whole and the inference line stays at option A, C, or D.
2. Build it, and describe it accurately everywhere the separation claim appears, including the data processing agreement, the sub-processor list, and any proposal. "Organisations are never co-located, except on the inference service" is a sentence that has to survive being read aloud to a buyer.
3. Build it as an opt-in per instance, defaulting off, so an organisation that wants the lower price can take it and an organisation that cannot, does not. This keeps the default claim whole at the cost of two topologies to operate.

*Not decided here.* It needs the §7 measurement first, because if option A is cheap at the real token volume, the tradeoff is being taken for a small saving.

### C. GPU per instance

*Cost:* the full GPU line per organisation. EUR 576.70 per month derived at the Scaleway L4 hourly rate, against an application and database line in the tens of euros. That is one to two orders of magnitude above every other line combined.

*Verdict:* this prices SMEs out at an always-on duty cycle, which is what the substrate design doc already suspected (**REPORTED**, §13). Whether a scheduled or scale-to-zero GPU changes that is **not determined**; none of the pages read here described a per-second or scale-to-zero GPU billing mode. *Cheapest measurement:* read each provider's GPU billing documentation for a minimum billing increment and a stop-billing-when-stopped guarantee, then decide whether the ingest workload can be batched into a window short enough to matter.

*Separation:* strongest of the four. Nothing crosses the instance boundary at all.

### D. CPU-only quantized models per instance

*Cost:* no separate inference line. The model runs inside the application compute line, which means the compute plan is larger: something in the CX43 or CAX31 class (8 vCPU, 16 GB) rather than the CX23 class, so EUR 15.99 for a CX43 or EUR 20.99 for a CAX31, against EUR 5.49 per month at the Hetzner list prices above. That is the cheapest option that keeps the separation claim whole.

*The catch:* quality and latency. **Not determined**, and this is the measurement that decides whether option D is real or wishful.

*Cheapest measurement that would settle it:* run the frozen question fixtures from the eval harness (#122) against a quantized model on a CX43-class instance, and report answer quality against the current path plus p50 and p95 latency for each of the five call sites. The harness has to exist first, which is another reason #122 sits early in the roadmap. Note that the two halves may split: a quantized model may be adequate for promotion classification and the digest while being inadequate for graph construction on ingest, in which case the honest answer is a hybrid and the model needs a per-call-site line rather than one number.

*Separation:* whole. Nothing leaves the instance.

## 11. What the number decides

The unit cost of one organisation instance per month sets the minimum viable price. ADR-0023 accepted a fixed floor per organisation regardless of usage and stated the consequence plainly: that floor sets the minimum viable price and rules out a free hosted tier (**VERIFIED**, `docs/adr/0023-control-plane-outside-the-application.md:21`).

The price then decides whether the SME market is reachable. An instance whose floor is dominated by a GPU is a product for organisations that can absorb a four-figure annual line item without a procurement process. An instance whose floor is an application service, a database, and a metered API call is a product an accountancy can resell to its members, which is the third route to a deployment in the substrate design doc's §2 and the one that reaches the most organisations per unit of our effort.

**The decision is not taken.** It cannot be taken before the §7 measurement exists, and taking it earlier would mean choosing a market on the strength of a rate card rather than a number. What this document fixes is the shape of the calculation, the rate cards as published on 2026-08-04, and the four inference variants with their tradeoffs stated where a reader will find them.

## 12. Not determined, with the measurement that would settle each

1. **The reference workload.** Searches per day and at peak, ingest bytes per day, retained bytes and growth, tokens per day per call site. *Settled by:* the benchmark harness (#122) run against the production node, after #105.
2. **Instance sizing on each provider.** *Settled by:* the workload above, then a plan chosen from the §9 tables.
3. **Hetzner volume price per GB-month, included traffic per plan, and traffic overage per TB.** *Settled by:* reading `hetzner.com/cloud/block-storage` in a JavaScript-capable browser, or the authenticated Cloud API pricing endpoint.
4. **Whether Hetzner publishes a managed PostgreSQL.** *Settled by:* one read of their product index. If not, add self-operated database labour to the model as an explicit line.
5. **Throughput of one shared inference GPU in instances served.** *Settled by:* a load test against one L4 with the real ingest mix, once the workload is known.
6. **Whether any candidate offers scale-to-zero or per-second GPU billing.** *Settled by:* reading each provider's GPU billing documentation for the minimum billing increment.
7. **Quality and latency of CPU-only quantized inference.** *Settled by:* the frozen fixtures from #122 against a quantized model on a CX43-class instance, reported per call site.
8. **Whether the `/data` volume survives ADR-0020.** *Settled by:* deciding where the access store and the sync state files live once the graph leaves the volume (§2.1).
9. **Which retention obligations outlive termination.** *Settled by:* the contract, per instance, not by engineering.

## 13. Related records

Decisions this implements: [ADR-0023](../../adr/0023-control-plane-outside-the-application.md) (the boundary), [ADR-0020](../../adr/0020-graph-store-on-postgres-and-dataset-scoped-reads.md) (the volume dependency and the provisioning-contract items it adds), [ADR-0021](../../adr/0021-retrieval-interface-owns-ranking-and-provenance.md) (what makes fleet upgrades tractable), [ADR-0022](../../adr/0022-evidence-is-retained-and-attested-at-capture.md) (why the storage line has two growth drivers and why exit carries an obligation).

Scope limit: separation between **Seats** inside one organisation stays with [ADR-0003](../../adr/0003-seat-node-central-private-memory.md) and [ADR-0009](../../adr/0009-mesh-read-isolation-presence-vs-content.md), and nothing here touches it.

Source of truth for the decisions this elaborates: [`2026-08-03-citadel-substrate-design.md`](2026-08-03-citadel-substrate-design.md), sections 2, 3, 8, 11, and 13.

Partner-facing statements this document is bound by: [`docs/eu-partner-proposal.md`](../../eu-partner-proposal.md), specifically the exit obligation at `:79` and the retention framing at `:75`.
