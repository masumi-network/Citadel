# Architecture

Citadel is a FastAPI application with several subsystems around a knowledge engine. It is not a thin wrapper over one dependency.


```
  ┌─────────────────────────────────────────────────────────────┐
  │                     Citadel (FastAPI)                       │
  ├──────────────┬──────────────┬──────────────┬──────────────┤
  │  CLI client  │  Hosted MCP  │   HTTP API   │   Web UI     │
  │  (stdlib)    │  /mcp/       │              │  Mesh + Activity│
  ├──────────────┴──────────────┴──────────────┴──────────────┤
  │  Access control · audit · tiered ingestion · conflicts     │
  ├──────────────┬──────────────┬──────────────┬──────────────┤
  │ GitHub sync  │ Linear sync  │ Session trace│ Promotion    │
  │ Learning     │ Repo content │ Capture hooks│ Backup mirror│
  │ agent        │ Obsidian     │ Shared traces│ Self-improve │
  ├──────────────┴──────────────┴──────────────┴──────────────┤
  │  Structured knowledge · Knowledge Index · Knowledge Mesh   │
  ├──────────────────────────────┬──────────────────────────────┤
  │  PostgreSQL + pgvector       │  Kuzu graph (embedded)       │
  │  (vectors, metadata, access) │  (relationships, mesh)       │
  └──────────────────────────────┴──────────────────────────────┘
                              │
                    Cognee (knowledge engine)
```

| Layer | Role |
|---|---|
| **Seat** | One licensed team member (Principal). Admin creates the seat before any tokens. |
| **Node** | That seat's own working memory (`seat:{slug}`). Default target for capture and agent writes. |
| **Central** | Org-wide shared knowledge (`masumi-network`). Read-only for seats; evolves via sync + promotion. |
| **Session traces** | Third dataset (`session-traces`) for voluntarily shared prior work — consultable, reference-only. |
| **Learning Process** | Citadel's governed pipeline: security scan → optional LLM enrichment → structuring → index. |
| **Cognee** | Upstream knowledge engine (Apache-2.0) for embeddings and graph operations. Citadel imports it; storage, access, sync, and UI are Citadel's. |

## Public pages

The node serves five public pages, each owning one subject: `/` (what Citadel is, and how to start), `/info` (live numbers, releases, roadmap), `/use-cases` (what teams run it for, then the partnering profile for EU consortia), `/contact` (the enquiry form), and `/login`. The dashboard itself lives at `/app`, behind a seat token.

Domain language is defined in [`CONTEXT.md`](../CONTEXT.md). Decisions are recorded in [`docs/adr/`](adr/). The longer plan is in [`docs/organization-vault-plan.md`](organization-vault-plan.md).

## Qdrant shadow generation

Status: Planned
Owner: architect

REPORTED: the user selected Qdrant as Citadel's next vector backend and selected a new Railway project for the first hosted candidate.

The current production diagram above remains accurate until cutover. The candidate uses a separate whole generation:

```text
GitHub sync, seat capture, and direct ingest
  -> Citadel policy and authorization
  -> SourceRevision plus projection outbox
  -> exact Cognee 1.4.1 plus audited Qdrant adapter patch
       -> generation-scoped SQLite on the app volume
       -> generation-scoped Qdrant collections
       -> candidate graph store
  -> projection receipts and census

Authorized query
  -> Citadel scope resolver
  -> Qdrant query with mandatory generation and dataset filters
  -> source ownership recheck
  -> Citadel retrieval candidate, hit, and trace
```

- VERIFIED: the official community adapter is not install-compatible with Citadel's current Cognee declaration and drops CHUNKS reference fields used by Citadel. It also converts search exceptions into empty results.
- VERIFIED: a real Qdrant `1.18.1` probe returned both private seat points when no dataset filter was supplied. It returned only `seat:a` after the ownership filter was supplied.
- INFERRED: authorization must enter every Qdrant query. Qdrant's `is_tenant=true` index setting improves placement; it does not authorize a request.
- INFERRED: the generation boundary includes relational state, Qdrant, graph state, source revisions, receipts, model, chunk budget, Cognee version, and adapter version. A Qdrant collection alias alone is not a whole-system cutover.
- INFERRED: GitHub Central is imported first. Seats are imported serially and each seat must pass the authorization matrix before the next seat begins.

See [`docs/decisions.md`](decisions.md), [`docs/interfaces.md`](interfaces.md), and [`plans/roadmap.md`](../plans/roadmap.md).

## Lifecycle v1 implementation

Status: Completed locally
Owner: implementer

- VERIFIED: candidate commit `275e433d08251f4642d26e2136d8fa9e5e2193c1` stores retained source bytes, current heads, projection jobs, and three initial receipts in one dedicated SQLite transaction. Provider writes remain outside that transaction and receive deterministic source IDs.
- VERIFIED: the worker records relational, vector, and graph completion separately, then requires bounded provider reads before marking each receipt searchable. Five failed attempts produce a terminal failed job with bounded error text.
- VERIFIED: source replacement marks predecessor work stale. Managed retrieval accepts only the current head with a searchable vector receipt. Tombstones create new retained revisions and provider-neutral exclusion receipts.
- VERIFIED: empty-generation rebuild queues current heads idempotently and reports exact current-source, job, receipt, backend, and searchable counts. The lifecycle SQLite file is included in backup inventory and online restore checks.
- CORRECTED: commit `275e433` introduced lifecycle v1, then review found missing replay, tombstone, lease, generation, legacy-result, and dataset graph-context cases. Commit `5bdcf89` closes those reviewed gaps.
- VERIFIED: local verification at `5bdcf89` returned `1867 passed, 3 skipped, 11 warnings in 42.65s`; Ruff returned `All checks passed!`. Pinned Qdrant live tests returned `1 passed, 11 warnings in 5.85s` for tenant isolation plus delete and prune, and `1 passed in 34.55s` for lifecycle retrieval across a fresh process. Blind spot: no Railway service or production data was changed.
