# Architecture

Citadel is a FastAPI application with several subsystems around a knowledge engine. It is not a thin wrapper over one dependency.


## Current runtime

[VERIFIED] The Lite profile uses SQLite for the relational store, Qdrant for vectors, and Ladybug for the graph. It requires authenticated access (`kb/lite_runtime.py:103-120`).

[VERIFIED] Lifecycle SQLite is authoritative for retained source revisions, current source heads, projection jobs, and backend receipts. One acceptance transaction writes the revision, head, job, and pending relational, vector, and graph receipts (`kb/lifecycle.py:451-528`, `kb/lifecycle.py:548-786`).

[VERIFIED] A lifecycle worker projects each retained source to relational, vector, and graph backends. It verifies provider readback before it marks each receipt searchable (`kb/lifecycle_worker.py:752-958`). An operation is searchable only when its required backend receipts are searchable. This is a receipt contract, not a distributed transaction (`kb/service.py:856-877`, `kb/projection_barrier.py:101-136`).

[VERIFIED] The web process is the only graph writer. When `CITADEL_EVOLVE_SCHEDULER_ENABLED` is true, its event loop runs the source stages and one Cognify pass. The first projection barrier then gates self-improvement, promotion, and feedback. A second barrier waits for jobs created by those stages (`kb/server.py:560-573`, `kb/server.py:768-1040`). Do not add a separate evolve or Cognify process that shares the graph store (`kb/server.py:1073-1081`, `scripts/run_railway.py:147-164`).

[VERIFIED] Feedback events use a separate SQLite ledger under the state root. Search telemetry is redacted and written in a detached task. Explicit `/feedback` calls and `citadel_record_feedback` use the same durable event path (`kb/config.py:95-98`, `kb/server.py:3169-3259`, `kb/service.py:1512-1594`).

[VERIFIED] Promotion is disabled by default and dry-run is the default. A candidate needs a clear secret scan, organization reference, and valid LLM relevance and sensitivity result before a write. A standalone promotion stage skips when it has no capture watermark and exits zero (`kb/config.py:248-255`, `kb/promotion.py:368-510`, `scripts/run_railway.py:167-196`).

[VERIFIED] A seat is created before its tokens. A seat token carries the seat Node, Central, and Shared Session Traces in its readable dataset scope. `/admin/session` accepts an environment credential or a `ctdl_` token and sets a secure cookie. `/api/session` returns the role, seat, scopes, datasets, and labels (`kb/access.py:458-543`, `kb/server.py:4452-4508`).

[VERIFIED] A seat-scoped caller can read its own Node and shared datasets. A `seat:` dataset for another seat is denied. Admin and environment identities can bypass the dataset allowlist (`kb/server.py:2494-2512`).

[REPORTED] This revision is in an unmerged PR stack. Production may run an earlier revision. These sections describe the current code, not a deployed state.
[REPORTED] The public status label stays `Degraded` while the health payload reports an unresolved failure. Keep that label until failed graph jobs are classified. The status wording is unchanged in this revision (`kb/static/info.js:176-187`).


## Historical storage sketch

The diagram below is a historical Postgres and pgvector sketch. It is not the current Lite deployment shape.

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

[CORRECTED] The diagram is a historical Postgres, pgvector, and Kuzu sketch. The current Lite profile is SQLite, Qdrant, and Ladybug. The web service starts `python -m kb.lite_runtime` (`railway.toml:13`, `kb/lite_runtime.py:103-120`). The older shape remains in [`operations.md`](operations.md) as a self-host option.


| Layer | Role |
|---|---|
| **Seat** | One licensed team member (Principal). Admin creates the seat before any tokens. |
| **Node** | That seat's own working memory (`seat:{slug}`). Default target for capture and agent writes. |
| **Central** | Org-wide shared knowledge (`masumi-network`). Read-only for seats; evolves via sync + promotion. |
| **Session traces** | Third dataset (`session-traces`), voluntarily shared prior work, reference-only. |
| **Learning Process** | Citadel's governed pipeline: security scan, optional LLM enrichment, structuring, and index. |
| **Cognee** | Upstream knowledge engine (Apache-2.0) for embeddings and graph operations. Citadel imports it; storage, access, sync, and UI are Citadel's. |

## Public pages

The node serves five public pages, each owning one subject: `/` (what Citadel is, and how to start), `/info` (live numbers, releases, roadmap), `/use-cases` (what teams run it for, then the partnering profile for EU consortia), `/contact` (the enquiry form), and `/login`. The dashboard itself lives at `/app`, behind a seat token.

Domain language is defined in [`CONTEXT.md`](../CONTEXT.md). Decisions are recorded in [`docs/adr/`](adr/). The longer plan is in [`docs/organization-vault-plan.md`](organization-vault-plan.md).
