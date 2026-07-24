# Citadel — Operations & Self-Hosting

Operational reference for running Citadel as a self-hosted Organization Vault:
deployment, environment, integrations, and the full API surface. For the product
overview and quick start, see the [README](../README.md).

## Contents

- [Deployment (Railway)](#deployment-railway)
- [Environment & LLM provider](#environment--llm-provider)
- [Access roles & tokens](#access-roles--tokens)
- [HTTP API reference](#http-api-reference)
- [MCP server](#mcp-server)
- [GitHub organization sync](#github-organization-sync)
- [Linear workspace sync](#linear-workspace-sync)
- [Google Chat update digest](#google-chat-update-digest)
- [Obsidian vault sync](#obsidian-vault-sync)
- [Knowledge conflicts](#knowledge-conflicts)
- [Vault backup mirror](#vault-backup-mirror)

---

## Deployment (Railway)

The repo includes `railway.toml`. The entry command is `python -m
scripts.run_railway`; `CITADEL_RUN_MODE` selects the role (`web` default,
`pipeline`, `learning-agent`, `linear-sync`, `backup-mirror`). The web service
runs `uvicorn kb.server:app --host 0.0.0.0 --port $PORT`. Dependencies install
from `requirements.txt`, so a new runtime dependency must be added there.

Recommended first deployment shape:

- One web service for this repository.
- One cron service for daily GitHub syncs (`CITADEL_RUN_MODE=learning-agent`).
- One cron service for Vault Backup Mirror manifest export (`backup-mirror`).
- One Railway Postgres dedicated to Citadel, with `pgvector` enabled.
- One Railway volume mounted at `/data` for Cognee's embedded Kuzu graph files.

Use Railway's private Postgres `DATABASE_URL` as the app database binding. At
runtime Citadel derives Cognee's split `DB_*` settings from `DATABASE_URL`, and
maps `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`, `DB_PASSWORD` into
`VECTOR_DB_*` when `VECTOR_DB_PROVIDER=pgvector`. Set explicit `VECTOR_DB_*` only
when the vector store uses a different Postgres target.

```bash
GRAPH_DATABASE_PROVIDER=kuzu
SYSTEM_ROOT_DIRECTORY=/data/.cognee_system
DATA_ROOT_DIRECTORY=/data/.data_storage
```

Enable pgvector before production ingest:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### Redeploy after a code change

From a machine with Railway CLI linked to the **Citadel Archive** project
(service that serves `https://citadel-archive-production.up.railway.app`):

```bash
# Confirm link
railway status
railway service   # pick the web/Citadel-Archive service if prompted

# Deploy current git HEAD (commit first — Railway builds from git / linked root)
git status        # ensure intended commits are pushed if deploy tracks remote
railway up --service Citadel-Archive
# or, if the project auto-deploys from GitHub main:
git push origin main
```

Do not paste seat tokens or DB URLs into chat. Env vars stay in Railway
(`railway variables --service Citadel-Archive`). After deploy, restart Cursor so
MCP re-fetches `tools/list` from the new Node.

### Promotion Agent (ADR-0007 P5/P6)

After the promotion code is deployed, enable the governed Node→Central path on the
**Citadel-Archive** web service:

```bash
railway variables --service Citadel-Archive \
  --set "CITADEL_PROMOTION_ENABLED=true" \
  --set "CITADEL_PROMOTION_DRY_RUN=false" \
  --set "CITADEL_PROMOTION_RELEVANCE_THRESHOLD=0.7" \
  --set "CITADEL_PROMOTION_MAX_ITEMS=20"
```

- **On demand:** seat writers call `POST /api/promote/run` or `citadel promotion run --execute`.
- **Approval queue:** `GET /api/promotion/pending`, dashboard **Promotion Queue**, MCP
  `citadel_promotion_*`, or `citadel promotion list|approve|reject`.
- **6h cron:** add a Railway cron service with `CITADEL_RUN_MODE=evolve`, the same
  `/data` volume as the web service, and schedule `0 */6 * * *` (UTC). The evolve
  pipeline runs GitHub sync → repo-content → self-improve → **promotion** → cognify.
  Toggle the promotion stage alone with `CITADEL_EVOLVE_PROMOTION_ENABLED=true|false`.

Cron services should also mount `/data` so `github_sync_state.json` and
`backup_mirror/` persist between runs. Keep app and database in the same
project/environment so the database stays private to Citadel. The graph store
can later move to Neo4j or Memgraph without changing Citadel's wrapper code.

**Operational checks:**

- `railway status --json` — service deployments, cron schedules, domains, volumes.
- `railway logs --service Citadel-Archive --environment production --http --status '>=400' --lines 50 --json` — recent web errors.

## Environment & LLM provider

```bash
CITADEL_TENANT_ID=personal
CITADEL_DEFAULT_DATASET=personal
# Dataset a request without `dataset` should search (e.g. masumi-network).
CITADEL_SEARCH_DEFAULT_DATASET=masumi-network
```

For OpenRouter, Cognee expects the custom-provider form. **The model id must be
`openrouter/`-prefixed** — a bare/invalid id (e.g. `openrouter/free`, or
`google/gemini-2.5-flash` without the prefix) silently breaks every cognify call
(`litellm: "LLM Provider NOT provided"`).

```bash
LLM_PROVIDER=custom
LLM_ENDPOINT=https://openrouter.ai/api/v1
LLM_MODEL=openrouter/deepseek/deepseek-v4-flash   # the default
LLM_API_KEY=sk-or-...                             # or OPENROUTER_API_KEY
```

When adding teammates, keep the same wrapper and change tenant/user configuration
at deployment or request boundaries — the service layer accepts dataset, session,
and tenant-aware config without changing Cognee internals.

## Access roles & tokens

Bootstrap environment keys plus a persistent access store for teammate/agent tokens:

```bash
CITADEL_READER_KEYS=alice-reader-key,bob-reader-key
CITADEL_WRITER_KEYS=teammate-writer-key
CITADEL_ADMIN_KEY=owner-admin-key
CITADEL_ACCESS_STORE_PATH=/data/.citadel/access.json
CITADEL_AUDIT_MAX_EVENTS=1000
```

| Role | Permissions |
|---|---|
| Reader | view mesh, sources, indexes, events, and search |
| Writer | reader + ingest and feedback |
| Admin | writer + GitHub sync, self-upgrade, token create/revoke, audit |

Tokens are checked by **both role and scope**. Custom scopes can only *reduce* a
token's permissions within its role; scopes that exceed the role are rejected
(e.g. a writer token with only `kb:ingest` can ingest but not search). Create
tokens on the Access page (or `POST /api/access/tokens`); the token is shown
once — Citadel stores only its hash, prefix, role, scopes, expiry, and last-used
timestamp. Pass it as `Authorization: Bearer <token>`.

## HTTP API reference

Health: `GET /healthz`, `GET /readyz` (authed).

Core:

- `GET /api/session`, `GET /api/knowledge`, `GET /api/knowledge/events`
- `GET /api/mesh`, `GET /api/mesh/graph`, `GET /api/indexes`
- `GET /api/sources`, `GET /api/documents/{id}`, `GET /events`
- `GET /api/conflicts?status=open|resolved`, `POST /api/conflicts/{id}/resolve`
- `POST /ingest`, `POST /search`, `POST /feedback`, `POST /improve`, `POST /api/contribute`
- `POST /api/self-upgrade`, `POST /api/github-sync/run`, `POST /api/learning-agent/run`
- Obsidian: `POST /api/obsidian/vaults`, `GET /api/obsidian/manifest`,
  `POST /api/obsidian/sync/push`, `GET /api/obsidian/sync/pull`,
  `POST /api/obsidian/conflicts/{id}/resolve`

Admin:

- `GET /api/access`, `POST /api/access/tokens`, `POST /api/access/tokens/{id}/revoke`
- `GET /api/audit?view=all|mcp|access|failures&limit=50`
- `GET /api/backup-mirror`, `POST /api/backup-mirror/run`
- `GET /api/linear-sync`, `POST /api/linear-sync/run`

`GET /api/mesh/graph` (reader+) returns `{nodes, edges, ...}` from Cognee's graph
engine with a node cap (`CITADEL_MESH_GRAPH_MAX_NODES`, default 200, or `?limit=`).
With no data or no graph access it returns an empty graph with `fallback: true`.

ADR-0009 read isolation applies to non-admin tokens: content is scoped to the
caller's datasets (own seat + Central + non-seat datasets), while every seat is
always present as a synthetic presence hub (id `dataset:<name>`, not a real
graph node and not drillable). Scoped payloads add `visible_nodes` (caller
scope) alongside `total_nodes`/`total_edges` (org-wide), and per-node
`dataset`/`datasets`/`internal_name`/`chunk_count` where known. `/api/documents`
drill-down is scoped the same way, so a **404 can mean "not yours"** rather than
"does not exist". Admin/env tokens bypass scoping. The same scoping applies to
the `/api/mesh` and `/events` activity projection and, transitively, the
`citadel_get_mesh` / `citadel_get_document` MCP tools that proxy them.

Attribution/isolation tuning env vars (all optional): `CITADEL_GRAPH_DATA_CACHE_TTL_SECONDS`
(raw graph read cache, default 30), `CITADEL_NODE_DATASET_MAP_TTL_SECONDS`
(default 60), `CITADEL_NODE_DATASET_MAP_TIMEOUT_SECONDS` (default 5), and
`CITADEL_NODE_DATASET_MAP_FAILURE_TTL_SECONDS` (short negative-cache TTL,
default 5, so a transient attribution stall serves stale-but-safe data instead
of blanking scoped vaults). Note `CITADEL_MESH_GRAPH_MAX_NODES` is a UI cap, not
a server-CPU bound.

## MCP server

Citadel serves a **hosted, streamable-HTTP MCP endpoint** mounted into the same
FastAPI process (`kb/server.py` mounts `kb/mcp_server.py` at `/mcp/`). Each
request is authenticated by the caller's `ctdl_` bearer token — the same
reader/writer/admin tokens as the UI. Forwarded calls carry `X-Citadel-MCP-Tool`
and produce persistent audit events (`mcp.<tool_name>`) capturing actor, role,
tool, path, scope, dataset, status, and safe counts/hashes — never raw tokens,
queries, or note bodies.

```text
https://citadel-archive-production.up.railway.app/mcp/
Authorization: Bearer ctdl_<token>
```

```json
{
  "mcpServers": {
    "citadel": {
      "type": "http",
      "url": "https://citadel-archive-production.up.railway.app/mcp/",
      "headers": { "Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}" }
    }
  }
}
```

A **local stdio** server is available for offline/dev use and points at the
hosted API:

```bash
CITADEL_HTTP_BASE_URL=https://citadel-archive-production.up.railway.app
CITADEL_MCP_ACCESS_TOKEN=ctdl_...
CITADEL_MCP_DEFAULT_DATASET=masumi-network
uv run python -m kb.mcp_server
```

Hosted-MCP environment (Railway web service):

```bash
CITADEL_MCP_SELF_BASE_URL=http://127.0.0.1:8000   # forwarded calls hit the API in-process
CITADEL_MCP_ALLOWED_HOSTS=citadel-archive-production.up.railway.app  # optional Host/Origin allow-list
```

**Safe defaults:** use a reader service-account token for normal agent work;
require client approval for `citadel_ingest` / `citadel_record_feedback`; keep
`citadel_run_learning_agent` / `citadel_run_backup_mirror` / `citadel_improve`
approval-gated or disabled; HTTPS only for hosted URLs (plain `http://` is
allowed only for localhost unless `CITADEL_MCP_ALLOW_INSECURE_HTTP=true`); keep
`CITADEL_MCP_MAX_INGEST_BYTES` low so agents can't push large logs/secrets into
durable memory; review `/api/audit` from an admin session when validating a rollout.

Exposed tools include `citadel_discovery`, `citadel_session`, `citadel_search`,
`citadel_get_document`, `citadel_get_mesh`, `citadel_list_sources`,
`citadel_ingest`, `citadel_contribute`, `citadel_record_feedback`,
`citadel_linear_my_issues`, `citadel_linear_search`, `citadel_run_learning_agent`,
`citadel_backup_mirror_status`, `citadel_run_backup_mirror`, `citadel_audit_events`,
and `citadel_improve`. `citadel_search` hits carry an additive `_citadel`
provenance envelope (`rank`, `dataset`, `result_id`, `content_sha256`,
`provenance`, `retrieval`, `doc_type`, `content_hint`, `trust_tier`);
`citadel_get_document` takes the `id` from a hit when
`_citadel.retrieval.document_drilldown_available` is true. `content_hint`
describes what the hit's text looks like and is body-derived, so it is a
relevance signal only; `trust_tier` reports attested provenance
(`reference-only` or `unattested`) and is never derived from content — see
[ADR-0012](adr/0012-attested-trust-vs-content-hint.md). Every search also
records **implicit search telemetry** into the mesh feedback index (query,
filters, top hit ids/doc_types/trust_tiers/scores, latency, empty/low-score
flags, MCP tool name when present) — non-blocking and approval-free. Telemetry
rows land on the caller's own seat Node; a caller without a seat writes a
presence-only row. Writers may still call `citadel_record_feedback` after
reading hits for explicit scores (pass `qa_id` **or** `result_id`).

## GitHub organization sync

Citadel fetches GitHub organization activity, formats a daily digest, adds recent
commit summaries, ingests it into Cognee, and runs improvement for the sync
session. A separate connector ingests **product knowledge** (READMEs, `SKILL.md`,
docs trees) from allowlisted repos and runs each file through the Learning
Process + Cognee cognify. Default Sokosumi repos: `sokosumi`, `Sokosumi-MCP`,
`sokosumi-cli`, `sokosumi-docs`.

> When a GitHub token can see private repositories, treat the sync as sensitive
> metadata — see [`private-github-sync-security.md`](private-github-sync-security.md).

```bash
CITADEL_GITHUB_ORG=masumi-network
CITADEL_GITHUB_SYNC_DATASET=masumi-network
CITADEL_GITHUB_SYNC_SESSION=masumi-github-daily
CITADEL_GITHUB_SYNC_STATE_PATH=/data/.citadel/github_sync_state.json
CITADEL_GITHUB_SYNC_MAX_COMMITS_PER_REPO=5
CITADEL_GITHUB_SYNC_MAX_PULL_REQUESTS_PER_REPO=5
CITADEL_GITHUB_SYNC_INCLUDE_COMMITS=true
CITADEL_GITHUB_SYNC_INCLUDE_PRIVATE=true
CITADEL_GITHUB_SYNC_REPO_ALLOWLIST=
CITADEL_GITHUB_SYNC_REPO_DENYLIST=
CITADEL_GITHUB_SYNC_SECURITY_SCAN_ENABLED=true
CITADEL_GITHUB_SYNC_SECURITY_BLOCK_SEVERITY=high
CITADEL_GITHUB_TOKEN=github_pat_...   # read-only; fine-grained to the org repos
```

The cron output defaults to a sanitized summary; the pre-ingest scanner blocks
high-severity secret/phishing/corruption indicators before ingest. Org digest LLM
summarization is disabled for private-repo metadata unless
`CITADEL_ORG_DIGEST_LLM_ALLOW_PRIVATE=true`. Run via `citadel sync-github`,
`citadel sync-repo-content`, or `POST /api/repo-content-sync/run`.

For Railway, create a cron service with `CITADEL_RUN_MODE=learning-agent` and a
schedule (e.g. `0 8 * * *` — 08:00 UTC daily).

## Linear workspace sync

Syncs the Linear workspace read-only into **Central** (`masumi-network`) and
**Seat-Scoped Mirrors** assignee issues into each dev's **Node**. A Linear
personal API key with **Read** scope is sufficient.

```bash
CITADEL_LINEAR_API_KEY=lin_api_...
CITADEL_LINEAR_SYNC_DATASET=masumi-network
CITADEL_LINEAR_SYNC_SESSION=masumi-linear
CITADEL_LINEAR_USER_MAP=    # optional {"linear-user-uuid":"seat-slug"}
```

Admin: `GET /api/linear-sync`, `POST /api/linear-sync/run`. For Railway, a cron
service with `CITADEL_RUN_MODE=linear-sync` (suggested `0 */6 * * *`). Agents read
via `citadel_linear_my_issues` / `citadel_linear_search`. See
[ADR-0004](adr/0004-linear-seat-scoped-mirror.md).

## Google Chat update digest

Citadel can post an outbound-only Organization Update Digest to one dedicated
Google Chat space after the learning-agent cron runs (Google Chat API app auth,
not incoming webhooks). See
[ADR-0002](adr/0002-google-chat-app-auth-for-update-digests.md) and the
[digest plan](google-chat-organization-update-digest-plan.md).

```bash
CITADEL_ORG_DIGEST_ENABLED=true
CITADEL_ORG_DIGEST_WINDOW_HOURS=24
CITADEL_ORG_DIGEST_POST_TO_CHAT=true
CITADEL_GOOGLE_CHAT_ENABLED=true
CITADEL_GOOGLE_CHAT_SPACE_NAME=spaces/...
CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
```

Send one controlled test before enabling cron posting:

```bash
curl -fsS -X POST "$CITADEL_BASE_URL/api/learning-agent/google-chat/test" \
  -H "Authorization: Bearer $CITADEL_ADMIN_KEY" -H "Content-Type: application/json" \
  --data '{"message":"Citadel Google Chat delivery test"}'
```

Keep only one production poster enabled at a time. The long-term shape is a
separate update-agent repo — see
[internal-update-agent-architecture.md](internal-update-agent-architecture.md).

## Obsidian vault sync

An Obsidian-compatible source path for team vaults. The server stores vault
registration, document hashes, revisions, sync cursors, and conflicts in
`CITADEL_OBSIDIAN_SYNC_STATE_PATH`. The first sync mode is **explicit push** from
an Obsidian plugin/API client — it does not crawl a full vault and does not
overwrite local Obsidian files. The private-beta plugin scaffold lives in
`plugins/obsidian-citadel/`.

## Knowledge conflicts

A Knowledge Conflict is a visible disagreement between pieces of structured
knowledge or their source snapshots. Citadel prefers newer source-linked
repository truth but never silently overwrites: conflicts are recorded in a
bounded store (`CITADEL_CONFLICTS_STORE_PATH`), surfaced as `conflict` events,
and stay open until resolved. Detected today: Obsidian push conflicts (stale base
revision) and ingest-time title matches against the latest GitHub digest or synced
Obsidian notes with a differing content hash. APIs:
`GET /api/conflicts?status=open|resolved` (reader+),
`POST /api/conflicts/{id}/resolve` (writer+) — both audited.

## Vault backup mirror

A manifest-only exporter for the private `masumi-network/Vault-Backup-Mirror`
repo. It tracks state files by path, size, timestamp, and SHA-256 — it does **not**
copy raw source bodies, token stores, embeddings, vector/graph indexes, or large
binaries. See [`vault-backup-mirror.md`](vault-backup-mirror.md).

```bash
CITADEL_BACKUP_MIRROR_REPO=masumi-network/Vault-Backup-Mirror
CITADEL_BACKUP_MIRROR_ROOT_PATH=/data/.citadel/backup_mirror
CITADEL_BACKUP_MIRROR_ENABLED=false        # non-dry-run local writes
CITADEL_BACKUP_MIRROR_PUSH_ENABLED=false   # + CITADEL_BACKUP_MIRROR_TOKEN for GitHub push
```

Admin: `GET /api/backup-mirror`, `POST /api/backup-mirror/run` (`{"dry_run": true}`
by default). Cron via `CITADEL_RUN_MODE=backup-mirror`.
