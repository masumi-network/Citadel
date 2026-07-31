# ADR 0005 — Self-evolving memory + policy-gated ingestion (incl. Scout PR agent)

- **Status:** Proposed (2026-06-26)
- **Supersedes/relates:** ADR-0003 (seat/node/Central private memory), ADR-0004
  (Linear Seat-Scoped Mirror), `docs/internal-update-agent-architecture.md` (Scout
  boundary).

## Context

Goal: Citadel should be a **persistent, incremental, self-evolving** org memory.
- **Personal nodes** (`seat:{slug}`) are each dev's private memory; they accumulate
  as the dev works and never reset.
- **Central** (`masumi-network`) is the shared org knowledge base; it should stay in
  sync with personal nodes and keep evolving — but it must remain **curated and
  clean**, not a dump of everyone's raw notes.
- Ingestion should be **event-driven** (a big PR merges, or a person contributes),
  not only time-based, with a periodic fallback so nothing is missed.
- **No secrets / sensitive values** may ever land in the shared vault.

What already exists (verified 2026-06-26):
- Incremental, additive cognify (graph persists + accumulates; one global Kuzu graph
  after `ENABLE_BACKEND_ACCESS_CONTROL=false`). Text embeddings (`fastembed`
  `bge-small-en-v1.5` in pgvector). Seat/node/Central isolation (ADR-0003). Autosync
  (git-push + SessionEnd hooks) feeds personal nodes. Curated promotion
  (`org-ready`/`vault-contribution` tags or `/api/contribute`). cognee `improve()`
  on the daily GitHub cron. Per-tool MCP auth/scope/risk policies.

Gaps (as of the original writing; see the amendment below for what has since
closed):
- ~~**Secret scanning runs ONLY on the GitHub sync path** (`kb/github_sync.py`);
  `/ingest`, `/api/contribute`, and autosync have **no scan** — unsafe for
  auto-promotion.~~ **Closed.** Verified 2026-07-31: the scan is now a single
  gate on every write path. `Citadel._guard_content` runs it before enrichment
  and before chunking, and `/ingest`, `/api/contribute`, the capture hooks
  (`sync_push`, `sync_session`, `citadel capture`) and the **Promotion Agent**
  all route through it.
- Promotion is **manual/curated**, not smart-selective.
- No GitHub **webhook** (cron-only). No frequent evolve cron.
- cognee's deeper self-improvement (`auto_improve`, `build_global_context_index`) and
  graph-aware retrieval are gated off; search is plain vector `CHUNKS`.

## Decision

1. **One content-policy gate in the Citadel service layer.** A single
   secret/sensitivity scan + redaction (reuse `kb/security_scan.py`) runs on **every**
   write path — `/ingest`, `/api/contribute`, autosync, and promotion — blocking on
   `high` severity. MCP tools inherit it automatically (no per-tool policy drift).
2. **Smart, selective auto-promotion (personal → Central).** A promotion pass
   classifies personal-node content as **org-relevant AND non-sensitive**, and promotes
   only the qualifying subset to Central through the existing curated-Central path,
   with an audit record. Not everything; not manual-tag-only. Borderline items can
   require human approval (open question).
3. **Event-driven ingestion + fallback.** A GitHub **PR-merge webhook** and manual/`contribute`
   actions and autosync events drive ingestion/promotion in real time; a **6h evolve
   cron** (per-node incremental cognify + `improve()` + promote-tagged → Central +
   Central cognify) catches anything not event-driven. (6h default to bound LLM cost;
   tighten later.)
4. **Scout as the PR-knowledge ingestion agent.** On a PR (merge), Scout fetches the
   diff + description, **searches Citadel** for related context, produces a **brief
   knowledge summary** of what changed and how it links to existing knowledge, and
   **ingests it into the shared KB** via Citadel's policy-gated API using a dedicated
   writer **service-account token**. Scout **does not comment on the PR** (bug/review
   bots already do that). Scout keeps its existing outbound-gateway role; Citadel stays
   the vault + policy. This makes Scout the primary driver of org-knowledge ingestion.
5. **Turn on the self-evolving depth.** Enable cognee `auto_improve` /
   `build_global_context_index` on the evolve cycle and add graph-aware retrieval
   (`GRAPH_COMPLETION`/`AUTO`) + cited references so search uses the graph that cognify
   builds.
   > **NOT SHIPPED as of 2026-07-31.** Verified live on `/readyz`:
   > `auto_improve: false`, `build_global_context_index: false`, and the search
   > type is still plain vector `CHUNKS`. The "Gaps" section above therefore
   > still describes current behaviour for this item, and the graph that cognify
   > builds is not yet what search reads. Treat this as an open intention, not a
   > delivered decision.

## Consequences

- **+** Persistent, incremental, genuinely self-evolving memory; secret-safe by
  construction (one gate); clean Citadel/Scout boundary; Central stays curated via smart
  selection rather than manual tagging.
- **−** LLM cost per promotion/improve cycle and per PR-summary; Scout needs a deployment
  + a webhook (or polling) + a scoped writer token; a misclassifying promoter could
  still surface borderline-personal content to Central (mitigated by the secret gate +
  approval-on-borderline).

## Open questions

- Promotion classifier: LLM relevance/sensitivity score + threshold; auto-promote above
  X, human-approve between X and Y, drop below Y?
- Scout trigger: GitHub App webhook vs polling; where Scout is deployed (Railway service).
- Privacy: confirm only clearly-org content leaves a personal node (never raw personal
  notes), independent of the secret scan.
- Should promotion be reversible (un-promote / forget) — ties to adopting cognee `delete`.

## Build order

1. Secret/sensitivity gate on all ingest + promotion paths (foundational, security-critical).
2. Smart promotion engine (classify → promote → audit).
3. Event triggers: GitHub PR-merge webhook + 6h evolve cron.
4. Scout inbound PR-ingestion (new role in the Scout repo, writing via #1's gate).
5. Self-evolving depth: cognee `auto_improve` + graph-aware retrieval.
