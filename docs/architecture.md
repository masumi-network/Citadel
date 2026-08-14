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

[CORRECTED 2026-08-14] The boxes above are the older Postgres + pgvector + Kuzu
sketch. The Railway web service at `railway.toml` starts `python -m
kb.lite_runtime`. Lite defaults (`kb/lite_runtime.py:77-82`) are SQLite,
Qdrant, and Ladybug. `.env.example` still shows pgvector and kuzu as examples.
Self-hosting that older shape is documented in [`operations.md`](operations.md).

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
