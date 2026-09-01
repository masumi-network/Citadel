# Citadel: Operations and Self-Hosting


Operational reference for running Citadel as a self-hosted Organization Vault:
deployment, environment, integrations, and the full API surface. For the product
overview and quick start, see the [README](../README.md).
## Current runtime

[REPORTED] This revision is in an unmerged PR stack. Production may run an earlier revision. Verify the deployed revision before you use a production result as evidence.

[VERIFIED] The current Lite profile uses SQLite for relational data, Qdrant for vectors, and Ladybug for the graph. It requires authenticated access (`kb/lite_runtime.py:103-120`).

[VERIFIED] Lifecycle SQLite stores retained source revisions, current source heads, projection jobs, and one receipt per required backend. One acceptance transaction writes the source, head, job, and pending receipts (`kb/lifecycle.py:451-528`, `kb/lifecycle.py:548-786`).

[VERIFIED] A complete operation needs searchable relational, vector, and graph receipts for one active source revision. The barrier reads exact job IDs and returns pending or failed IDs without treating them as complete (`kb/projection_barrier.py:101-136`, `kb/projection_barrier.py:173-261`).

[VERIFIED] The web process owns graph writes. With `CITADEL_EVOLVE_SCHEDULER_ENABLED=true`, its event loop runs source stages and one Cognify pass, waits for the first projection barrier, runs self-improvement, promotion, and feedback, then waits for a second barrier (`kb/server.py:560-573`, `kb/server.py:768-1040`).

[VERIFIED] Search telemetry and explicit feedback use the durable feedback ledger under the state root. Ledger writes are redacted and detached from the search response (`kb/config.py:95-98`, `kb/server.py:3169-3259`, `kb/service.py:1512-1594`).

[VERIFIED] Promotion is disabled by default and dry-run is enabled by default. Secret scan, organization reference, LLM classification, and the configured score threshold must pass before a Central write (`kb/config.py:248-255`, `kb/promotion.py:368-510`).

[VERIFIED] Seat tokens identify a private Node and carry readable dataset labels for Private Node, Central, and Shared Session Traces. `/admin/session` sets a secure cookie. `/api/session` returns role, seat, scopes, datasets, and labels (`kb/access.py:458-543`, `kb/server.py:4452-4508`).

[REPORTED] The public status keeps `Degraded` while health reports an unresolved failure. Keep this label until failed graph jobs are classified. This revision does not change that wording (`kb/static/info.js:176-187`).


## Contents

Sections: [Deployment (Railway)](#deployment-railway), [Environment and LLM provider](#environment--llm-provider), [Access roles and tokens](#access-roles--tokens), [HTTP API reference](#http-api-reference), [MCP server](#mcp-server), [GitHub organization sync](#github-organization-sync), [Linear workspace sync](#linear-workspace-sync), [Google Chat update digest](#google-chat-update-digest), [Obsidian vault sync](#obsidian-vault-sync), [Knowledge conflicts](#knowledge-conflicts), and [Vault backup mirror](#vault-backup-mirror).


---

## Deployment (Railway)

[VERIFIED] The web service starts `python -m kb.lite_runtime` (`railway.toml:13`). Its liveness check is `/healthz`. Projection readiness uses `/health/ready` and authenticated `/readyz` (`railway.toml:14-15`).

[VERIFIED] The Lite profile sets `DB_PROVIDER=sqlite`, `GRAPH_DATABASE_PROVIDER=ladybug`, and `VECTOR_DB_PROVIDER=qdrant` (`kb/lite_runtime.py:103-120`). Do not start the web image with `python -m scripts.run_railway`.

The Postgres, pgvector, and Ladybug shape below is a self-host alternative. It is not the current Lite profile.

### Self-host alternative

Add runtime dependencies in the Dockerfile and `pyproject.toml`. Use one web process for the graph store and one mounted volume for its state. Keep graph-writing stages in that web process. Do not add an `evolve` or `cognify` cron service that shares the graph store.

Use a private Postgres `DATABASE_URL` as the app database binding for this alternative. Citadel derives Cognee's `DB_*` settings from it. When `VECTOR_DB_PROVIDER=pgvector`, it maps the Postgres fields into `VECTOR_DB_*`. Set explicit vector fields only when the vector store uses another Postgres target.


For the self-host Postgres alternative, use the graph and path settings below. Keep graph-writing stages in the web process. Do not start a second process that opens the same graph store.

Citadel keeps its own logs at `CITADEL_LOG_LEVEL` and applies a separate threshold to Cognee's task and retrieval loggers. The default `CITADEL_COGNEE_LOG_LEVEL=WARNING` keeps warnings and failures. Set `INFO` or `DEBUG` during diagnosis, then restore `WARNING`.

Public service surfaces read the package version from `kb.__version__` when the web process starts. `build_id` uses `RAILWAY_GIT_COMMIT_SHA`, then `CITADEL_BUILD_ID`. `deployment_id` uses the Railway deployment or snapshot identifiers. A missing source identifier stays `null`.


```bash
GRAPH_DATABASE_PROVIDER=ladybug
SYSTEM_ROOT_DIRECTORY=/data/.cognee_system
DATA_ROOT_DIRECTORY=/data/.data_storage
```


For the self-host Postgres alternative, enable pgvector before production ingest:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### Redeploy after a code change

From a machine with Railway CLI linked to the **Citadel Archive** project
(service that serves `https://citadel.utxo.ag`):

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

### Promotion and evolve scheduling

Promotion is opt-in and dry-run is enabled by default. Set the variables below only for an approved deployment. The relevance threshold and item limit remain configurable.


```bash
railway variables --service Citadel-Archive \
  --set "CITADEL_PROMOTION_ENABLED=true" \
  --set "CITADEL_PROMOTION_DRY_RUN=false" \
  --set "CITADEL_PROMOTION_RELEVANCE_THRESHOLD=0.7" \
  --set "CITADEL_PROMOTION_MAX_ITEMS=20"
```

The HTTP `POST /api/promote/run` route checks access and returns `409` with `llm_scheduled_only`. It does not run the promotion LLM from a user request (`kb/server.py:7467-7496`).

`GET /api/promotion/pending` lists redacted queue items. A seat sees its own queue. An admin can inspect all queues. Approve and reject require the `admin` role and `sources:sync` (`kb/server.py:7499-7566`).

The web scheduler runs promotion after the first projection barrier. It passes the capture watermark to the stage. A standalone `CITADEL_RUN_MODE=evolve` run has no watermark, so its promotion stage skips and exits zero. Standalone evolve does not process feedback. The standalone `cognify` mode refuses to run when the web scheduler owns the graph (`kb/server.py:932-999`, `scripts/run_railway.py:167-196`, `scripts/run_railway.py:352-432`, `scripts/run_railway.py:147-164`).

Do not add a separate `evolve` or `cognify` cron service that opens the graph store. The web process is the only graph writer. Keep any separate non-graph job away from the web service's graph state (`kb/server.py:1073-1081`).

For a separate cron job that writes state, set `restartPolicyType = "NEVER"` and mount the volume that owns its state. Keep the app and database in one project and environment.


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

[CORRECTED 2026-08-23] For OpenRouter, Cognee uses the custom-provider form.
The model id must be `openrouter/`-prefixed. The native OpenRouter route
`openrouter/free` becomes `openrouter/openrouter/free` for Cognee. OpenRouter
documents that the router filters for capabilities such as structured outputs:
https://openrouter.ai/openrouter/free.

The local default uses that free router. Production inspection still showed the
older fixed route `openrouter/nvidia/nemotron-nano-9b-v2:free` under generation
`citadel-railway-v056-free-quota-guard-20260821`. OpenRouter marks that model as
going away on August 24, 2026:
https://openrouter.ai/nvidia/nemotron-nano-9b-v2:free.
Do not treat local and production provider settings as equal until a deployment
and a known-answer canary prove parity. A bare vendor/model id without the
prefix breaks cognify (`litellm: "LLM Provider NOT provided"`).

```bash
LLM_PROVIDER=custom
LLM_ENDPOINT=https://openrouter.ai/api/v1
LLM_MODEL=openrouter/openrouter/free
LLM_API_KEY=sk-or-...                             # or OPENROUTER_API_KEY
```

When adding teammates, keep the same wrapper and change tenant/user configuration
at deployment or request boundaries — the service layer accepts dataset, session,
and tenant-aware config without changing Cognee internals.

## Access roles & tokens

Bootstrap environment keys plus a persistent access store for teammate/agent tokens:

An env access key authenticates as a bearer token on **every** endpoint, so it
is a password with no username and no rotation story. Generate them; never type
them. The server refuses to start on an env key shorter than 32 characters
(override with `CITADEL_ALLOW_WEAK_ACCESS_KEYS=true`, which you should only need
in local development).

```bash
# Generate each one:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
CITADEL_READER_KEYS=<32+ char random>,<32+ char random>
CITADEL_WRITER_KEYS=<32+ char random>
CITADEL_ADMIN_KEY=<32+ char random>
CITADEL_ACCESS_STORE_PATH=/data/.citadel/access.json
CITADEL_AUDIT_MAX_EVENTS=1000
CITADEL_COGNIFY_QUEUE_PATH=/data/.citadel/cognify_queue.json
```

Prefer minted tokens (Access page, or `POST /api/access/tokens`) over env keys
for anything but bootstrap: they carry a role, scopes, an expiry and a last-used
timestamp, they are stored only as a hash, and they can be revoked individually.
An env key can only be rotated by redeploying every consumer at once, which is
how the GitHub-Sync cron ended up 401ing against a rotated admin key.

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
### Token login and memory scope

`POST /admin/session` accepts an environment access key or a `ctdl_` token. A valid request sets an `HttpOnly`, `Secure`, `SameSite=lax` cookie with a 12-hour lifetime. `GET /api/session` accepts that session or the bearer token and returns the role, effective scopes, seat slug, readable dataset IDs, and `dataset_labels` (`kb/server.py:4452-4508`).

For a seat, the labels identify `seat:<slug>` as `Private Node`, the configured shared dataset as `Central`, and `session-traces` as `Shared Session Traces` (`kb/server.py:3067-3106`). The server checks both role and scope for each route (`kb/server.py:2275-2304`).

Seat-scoped reads and writes stay inside the caller's own `seat:` dataset. Another seat's Node returns a dataset-denied response. Shared datasets follow their own access rules. Admin and environment identities bypass the dataset allowlist (`kb/server.py:2494-2512`).


## HTTP API reference

Health probes:

`GET /healthz` is unauthenticated liveness and the Railway deploy gate. It does not run corpus or canary work. `GET /health/ready` checks dependency readiness and may return `503` while the process serves traffic. `GET /readyz` is authenticated readiness. It checks the bounded corpus probe, lifecycle census, and last canary.

| Group | Routes |
|---|---|
| Read | `GET /api/session`, `GET /api/knowledge`, `GET /api/knowledge/events`, `GET /api/mesh`, `GET /api/mesh/graph`, `GET /api/indexes`, `GET /api/sources`, `GET /api/documents/{id}`, and `GET /events`. |
| Write | `POST /ingest`, `POST /search`, `POST /feedback`, `POST /api/contribute`, and the Obsidian sync routes. |
| Admin jobs | `POST /api/self-upgrade`, `POST /api/github-sync/run`, and `POST /api/learning-agent/run`. |
| Conflicts | `GET /api/conflicts?status=open|resolved` and `POST /api/conflicts/{id}/resolve`. |
| Admin | `GET /api/access`, token creation and revocation, audit events, backup mirror status and runs, Linear sync status and runs, and lifecycle recovery routes. |

Lifecycle recovery previews active-generation failures by default. Apply requires the returned generation identity, ordered `candidate_ids`, and `expected_count`. A changed preview returns `409` without resetting a job. The route requires `admin` and `sources:sync`.

`POST /api/lifecycle/tombstone-failed` previews current-head jobs whose last error is `FileNotFoundError`. Apply uses the same confirmation fields and tombstones those source keys.


Examples:

```bash
export CITADEL_BASE_URL=https://citadel.utxo.ag

curl -fsS -H "Authorization: Bearer $CITADEL_MCP_ACCESS_TOKEN" \
  "$CITADEL_BASE_URL/api/knowledge?q=payment+flow&limit=5"

curl -fsS -X POST "$CITADEL_BASE_URL/api/contribute" \
  -H "Authorization: Bearer $CITADEL_MCP_ACCESS_TOKEN" -H "Content-Type: application/json" \
  --data '{"title":"Decision: deepseek-v4-flash","content":"Standardized on it via OpenRouter.","tags":["decision"]}'
```

`GET /api/mesh/graph` (reader+) returns `{nodes, edges, ...}` from Cognee's graph
engine with a node cap (`CITADEL_MESH_GRAPH_MAX_NODES`, default 200, or `?limit=`).
It is a projection view with scope filtering and dataset visibility, not a raw
ingest log. With no data or no graph access it returns an empty graph with
`fallback: true`.

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

[VERIFIED] `/api/ingest` may return `projection_job_id` in a successful write; poll
`/api/operations/{projection_job_id}` to confirm queued projections before the
node list appears in the graph cap.

[VERIFIED] `GET /api/mesh/projection-status` returns recent projection states for the
authenticated seat. The Next graph page polls this endpoint while open.

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
https://citadel.utxo.ag/mcp/
Authorization: Bearer ctdl_<token>
```

```json
{
  "mcpServers": {

    "citadel": {
      "type": "http",
      "url": "https://citadel.utxo.ag/mcp/",
      "headers": { "Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}" }
    }
  }
}
```

A **local stdio** server is available for offline/dev use and points at the
hosted API:

```bash
CITADEL_HTTP_BASE_URL=https://citadel.utxo.ag
CITADEL_MCP_ACCESS_TOKEN=ctdl_...
CITADEL_MCP_DEFAULT_DATASET=masumi-network
uv run python -m kb.mcp_server
```

Hosted-MCP environment (Railway web service):

```bash
CITADEL_MCP_SELF_BASE_URL=http://127.0.0.1:8000   # forwarded calls hit the API in-process
CITADEL_MCP_ALLOWED_HOSTS=citadel.utxo.ag  # optional Host/Origin allow-list
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
`citadel_improve`, `citadel_promotion_pending`, `citadel_promotion_approve`, and
`citadel_promotion_reject`.

`citadel_search` returns an `_citadel` envelope with result and retrieval metadata.
`content_hint` describes the hit text and is a relevance signal only. `trust_tier`
reports attested provenance, which is `reference-only` for shared session traces
and `unattested` for other hits. Verify API and specification claims against live
MIP or OpenAPI sources.

Every search also writes implicit telemetry to the durable feedback ledger. A seat
gets its own Node row. A seatless caller gets a presence-only row. Writers may call
`citadel_record_feedback` with a `qa_id` or `result_id` and a score of `1` or `-1`.
### Task-aware search injection

The `UserPromptSubmit` hook extracts a bounded task query from the prompt. It removes fenced code, long log lines, URLs, and command wrappers. It sends the query to `POST /search` only over HTTPS. The request uses the seat token from `CITADEL_MCP_ACCESS_TOKEN`, a five-second timeout, and at most three results (`kb/hooks/search_inject.py:18-28`, `kb/hooks/search_inject.py:84-140`, `kb/hooks/search_inject.py:227-259`).

The hook prints a context block with result IDs, titles, datasets, trust tiers, provenance, and short snippets. It marks the block as untrusted context, caps the output at 6,000 characters, and redacts secrets. Network, parse, and formatting failures print the static policy and return exit code zero (`kb/hooks/search_inject.py:174-224`, `kb/hooks/search_inject.py:267-283`).

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


Keep GitHub sync stages in the web process when graph projection is enabled. Do not create a `learning-agent` cron service against the same graph state.

## LLM-independent baseline retrieval

- [VERIFIED] Lifecycle ingestion writes retained source bytes and projection
  receipts before any Cognee projection call.
- [VERIFIED] Lifecycle search can return a deterministic lexical hit from the
  retained source store when vector recall is empty or unavailable.
- [VERIFIED] The vector-first Cognee projection route embeds chunks without
  running the graph extraction task. Graph enrichment remains a separate worker
  step.
- [VERIFIED] In pinned Cognee `1.4.1`, classification is extension-based,
  chunk extraction is chunker-based, and `index_data_points` uses the embedding
  engine. These tasks do not call a generative completion API.
- [VERIFIED] A current searchable vector receipt remains eligible while graph
  work is pending or retrying. The result envelope exposes lexical, vector, and
  graph state separately.
- [PLANNED] Production operators must verify the route with a read-only Qdrant
  point count, one known-answer CLI search, and one known-answer MCP search.
  A green node or auth check does not prove retrieval.
- [NOT DETERMINED] The deployed Railway generation does not yet prove this
  route. No production deploy ran during the local checkpoint.
- [VERIFIED] When the current lifecycle vector scope is empty, the service now
  skips provider recall and uses the retained SQLite lexical path directly.
  This avoids an unscoped Qdrant or embedding request during the zero-vector
  degraded state.
- [NOT DETERMINED] A non-empty vector scope that hangs at the provider still
  needs a bounded degraded-mode canary.

## Live runtime red-team evidence

- [VERIFIED] On `2026-08-23`, Railway reported deployment
  `e760e1b1-4539-4a01-a9f3-c86f444251f2` as `SUCCESS`.
- [VERIFIED] The live bootstrap selected the Nemotron embedding profile with
  `2048` dimensions and Qdrant collection
  `citadel_g_544df538f8b0_DocumentChunk_text_e8bbe7045b_nemotron-2048_ca06cec5`.
- [VERIFIED] Qdrant reported `status: green`, `points_count: 74`, and
  `indexed_vectors_count: 0` for that collection.
- [VERIFIED] `/api/state` reported `healthy: true`, while its nested lifecycle
  object reported `ok: false` and
  `current_generation_searchable_census_mismatch`.
- [VERIFIED] The current generation reported `634` sources and `19` searchable
  vector sources. The public source block reported `321` Linear issues, `49`
  GitHub repositories, and `94` repository-content files.
- [VERIFIED] Railway logs show OpenRouter free-model quota failures with HTTP
  status `422`. These failures explain graph enrichment failures only; they do
  not by themselves prove the cause of the search timeout.
- [NOT DETERMINED] A green Railway deployment, a green Qdrant collection, and
  a non-zero point count do not prove a working MCP search. The acceptance gate
  requires a known-answer result with a citation through both CLI and MCP.
- [PLANNED] Treat `healthy: true` with nested lifecycle `ok: false` as a health
  contract defect. Correct it before declaring the deployment ready.

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

## Linear context coverage and truthful status

- [VERIFIED] The local Linear sync queries projects, project updates, documents, comments, initiatives, and initiative updates. It uses stable source keys in the form `linear:{entity_type}:{id}`.
- [VERIFIED] `CITADEL_LINEAR_SYNC_MAX_CONTEXT_RECORDS=0` means no context cap. `CITADEL_LINEAR_SYNC_INCLUDE_ARCHIVED=false` excludes archived records by default.
- [VERIFIED] Context capture uses deterministic Markdown formatting and the light learning tier. It does not require generative LLM enrichment.
- [VERIFIED] If one context connection returns an API or response-shape error, the sync keeps records from other connections, stores `context_error`, and marks `context_listing_complete=false`. It does not tombstone prior records while coverage is incomplete.
- [VERIFIED] `/api/state` and `/api/sources` count Linear issues plus context records. `/api/sources` reports `degraded` when a context fetch error exists.
- [VERIFIED] A local GitHub organization inventory returned `57` visible repositories. The production snapshot reported `49` tracked repositories. Treat the difference as a coverage gap until the production sync runs with a server-side discovery result.
- [NOT DETERMINED] Production Linear permissions, context counts, sync flags, and deployment revision remain unverified. The read-only Railway variable-name filter found no exact `CITADEL_LINEAR_SYNC_*`, `CITADEL_REPO_CONTENT_SYNC_*`, `CITADEL_BUILD_ID`, or `CITADEL_LLM_ENRICHMENT_ENABLED` entries.
- [PLANNED] After deployment approval, run a bounded Linear count check, an all-org GitHub discovery check, and known-answer CLI and MCP searches. Record fetched, skipped, blocked, and searchable counts separately.

## Live acceptance recheck

- [VERIFIED] The local full suite returned `2353 passed, 6 skipped, 12 warnings in 57.49s`.
- [VERIFIED] The live CLI status check passed node and auth checks but failed search with `SEARCH_TIMEOUT` after `15s`.
- [VERIFIED] The live CLI search returned HTTP `504` with `The read operation timed out`.
- [VERIFIED] The live MCP search returned HTTP `504` with `Search exceeded the configured server budget.` It returned no hit, so it provides no Citadel-backed context for this audit.
- [VERIFIED] The live status payload reported `LLMQuotaExceededError`, lifecycle invariant `current_generation_searchable_census_mismatch`, vector searchable count `19`, and `0/601` fully indexed documents in the bounded corpus probe.
- [NOT DETERMINED] A production deployment is required before the new local lexical fallback and truthful status code can be tested on Railway.
- [PLANNED] After explicit deployment approval, run one known-answer CLI search and one known-answer MCP search. Accept only a citation-bearing result and a truthful degraded graph state.

## Production state-file coverage

- [VERIFIED] Railway state shows `94` repo-content files across `4` repositories. Their projection states are `80 searchable` and `14 pending`.
- [VERIFIED] Railway Linear state shows `321` issues and `listing_complete=false`. It has no context-record state keys, so the local project, document, comment, and initiative sync is not deployed there.
- [VERIFIED] The local GitHub credential sees `57` organization repositories. Production repo-content state covers `4`, and production GitHub metadata reports `49`. Keep these counts separate until one server-side inventory reconciles them.
- [NOT DETERMINED] The deployed source revision cannot be identified from the bootstrap build hash alone because the local build identity method may use Railway commit metadata.

## Repository coverage defaults

- [VERIFIED] The local GitHub inventory command returned
  `{"total":57,"archived":0,"active":57,"forks":6,"disabled":0,"private":8}`.
- [VERIFIED] The Railway repository-content state file returned `94` files
  across `4` repositories. The Railway GitHub state file returned `49` stored
  repository names and no `listing_complete` field.
- [CORRECTED] `CitadelConfig.from_env()` now defaults repository-content sync
  to all non-archived organization repositories, all eligible text files, and
  no file-count cap. Set `CITADEL_REPO_CONTENT_SYNC_ALL_REPOS=false` or
  `CITADEL_REPO_CONTENT_SYNC_ALL_TEXT=false` to narrow the scope explicitly.
- [VERIFIED] The all-text path still applies the configured byte cap and the
  security scanner. It does not ingest every binary or generated file.
- [VERIFIED] The default repository-content improvement pass is disabled. The
  baseline capture path therefore does not need a generative LLM. Vector
  projection may use embeddings, and lexical retrieval remains the fallback.
- [PLANNED] After deployment approval, run a dry-run inventory first. Compare
  repository names, discovered files, skipped files, blocked files, and
  projection states before allowing the production sync.

### Least confident decisions

1. The Railway token's repository visibility is not yet reconciled by name with
   the local credential's `57` repositories.
2. The all-text policy may need a product-defined source allowlist for very
   large repositories after the first dry-run inventory.

## Current local verification

- [VERIFIED] `uv run pytest -q` returned
  `2354 passed, 6 skipped, 12 warnings in 59.19s`.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`.
- [VERIFIED] `git diff --check` returned no output and exit code `0`.
- [VERIFIED] The documentation detector returned no findings for the updated
  progress, operations, and local-review records.
- [NOT DETERMINED] Production still needs a deploy, sync inventory, and a
  citation-bearing CLI and MCP known-answer search.

### Least confident decisions

1. The first production all-org inventory may fail on private repository scope
   even though the local credential sees all `57` repositories.
2. A green deployment or Qdrant collection will not count as acceptance until
   CLI and MCP return the same source-bound answer.

## Autonomous scheduler state

- [VERIFIED] Railway currently exposes
  `CITADEL_EVOLVE_SCHEDULER_ENABLED=true` and
  `CITADEL_EVOLVE_INTERVAL_SECONDS=3600`.
- [VERIFIED] Railway exposes no exact variables with the
  `CITADEL_PIPELINE_*`, `CITADEL_GITHUB_SYNC_*`, `CITADEL_LINEAR_SYNC_*`,
  `CITADEL_REPO_CONTENT_SYNC_*`, or `CITADEL_SELF_IMPROVE_*` prefixes in the
  read-only variable listing.
- [VERIFIED] `scripts/run_railway.py` supplies defaults for the evolve stage
  toggles. Missing variables therefore do not prove that a stage is disabled.
- [VERIFIED] Recent Railway logs still show `504 Gateway Timeout` for search,
  `search budget exceeded`, and empty-graph warnings.
- [NOT DETERMINED] The scheduler's last successful GitHub, repository-content,
  Linear, and projection counts are not proven by the current logs.
- [PLANNED] After deployment approval, inspect one complete scheduler pass and
  require per-stage counts plus a citation-bearing CLI and MCP search.

### Least confident decisions

1. The scheduler may be using deployed code and defaults that differ from the
   local worktree.
2. Log absence is not evidence that a stage did not run; a durable stage receipt
   is required.

## GitHub repository-set reconciliation

- [VERIFIED] The local credential sees `57` repositories: `49` public and `8`
  private.
- [VERIFIED] A read-only sorted name diff returned no output between the local
  public set and the stored Railway GitHub state. The Railway `49` names match
  the local public set exactly.
- [CORRECTED] The production gap is now identified as exclusion of the `8`
  private repositories, not an unexplained count difference.
- [NOT DETERMINED] The cause may be an old sync setting or production token
  scope. A post-deploy discovery run must report both separately.
- [PLANNED] Require `57` discovered repositories, or a truthful blocked count
  naming the private repositories and the permission error, before accepting
  all-org coverage.

### Least confident decisions

1. Stored state does not prove that the current Railway token can read the
   private repositories.
2. A successful repository listing does not prove that every eligible file was
   scanned or projected.

## 2026-08-23 MCP LLM-dependency boundary

- [VERIFIED] Seat MCP writes use the `light` learning tier. That tier skips
  generative enrichment and improvement in `kb/learning.py:132-137`.
- [VERIFIED] Retained-source lexical search is deterministic. It reads current
  source heads in `kb/lifecycle.py:1651-1797`; it does not need embeddings or a
  language model. The service uses this path when current vector scope is empty
  or vector recall returns no usable result.
- [VERIFIED] MCP ingest rejects `cognify=false` in `kb/mcp_server.py:1341-1342`.
  HTTP ingest also accepts only `cognify=true` in `kb/server.py:994-1001`.
  The current interface therefore requests projection for every MCP ingest.
- [INFERRED] Vector projection needs an embedding model. Graph projection and
  improvement may need a generative model. They are separate from source
  retention and lexical retrieval.
- [VERIFIED] Production search remains unaccepted. The latest MCP probe returned
  HTTP `504`, and Railway logs include
  `LLMQuotaExceededError: LLM provider quota or billing limit was reached`.
- [CORRECTED] “Baseline retrieval does not need a generative LLM” must not be
  read as “the deployed MCP workflow is LLM-independent.” The current ingest
  flag prevents that guarantee at the interface boundary.
- [PLANNED] Implement and test capture-only ingest. It must retain the source,
  return a pending projection receipt, and allow lexical search before any
  provider projection runs.

### Least confident decisions

1. The current lifecycle job can remain pending safely when capture-only mode
   defers worker start.
2. The production timeout may have more than one provider cause because the
   live evidence does not isolate the request stage.

## 2026-08-23 Capture-only user boundary correction

- [CORRECTED] The earlier boundary section described the old behavior, where
  MCP ingest requested projection by default. That statement is now historical.
- [VERIFIED] User MCP and CLI ingest capture source data only. Explicit
  `cognify=true` fails with `COGNIFY_SCHEDULER_ONLY` before provider work.
- [VERIFIED] HTTP `/ingest` rejects `cognify=true` with status `422`. Accepted
  user writes pass `defer_cognify=true` and report `projection_requested=false`.
- [VERIFIED] User search uses chunk retrieval with `allow_generative=false`.
  Graph completion, Cognee recall auto-routing, and search-time auto-feedback
  are outside this path. Embedding lookup remains part of vector retrieval.
- [VERIFIED] Local checks: MCP ingest `6 passed`, CLI ingest `23 passed`,
  server ingest `17 passed`, and Ruff `All checks passed!`.
- [NOT DETERMINED] This correction is not deployed to Railway. The production
  endpoint still requires a fresh canary before it can be accepted.

### Least confident decisions

1. Existing clients that send `cognify=true` may need to move that action to
   the scheduled projection command.
2. Production search behavior remains unknown until the new revision runs.

## 2026-08-23 Post-deploy production checkpoint

- [VERIFIED] Railway deployment `586c8c09-e1e1-48da-9ad4-cd169b8db5d8` for
  `Citadel-Archive` in `production` returned `SUCCESS`. The build passed the
  pinned embedding, offline model, and Ladybug extension checks before
  deployment.
- [VERIFIED] `/healthz` returned `{"ok":true,"service":"citadel"}`.
  `/health/ready` returned `{"ok":false,"service":"citadel"}`.
- [VERIFIED] The authenticated status check returned search success with one
  result, but reported
  `current_generation_searchable_census_mismatch` and only `40/601` sampled
  documents fully indexed. Searchable counts were graph `24`, relational
  `323`, and vector `20`.
- [VERIFIED] Source inventory remains `49` GitHub repositories, `94` repository
  files, and `321` Linear issues. The vector index reports `67` records and the
  graph index reports `7397` records. These are index counters, not proof that
  every tracked source is searchable.
- [VERIFIED] MCP search returned both a Linear issue title and a
  `masumi-network/Sokosumi-MCP` README title for the query `Sokosumi MCP
  payment`. The response identified its relevance basis as
  `lexical-term-overlap`.
- [VERIFIED] The scoped Linear search returned no results for
  `subscription credits` and warned that only `15` candidates were fetched
  before filtering. This does not prove that the Linear corpus lacks a match.
- [VERIFIED] Runtime logs contain repeated Cognee warnings with
  `Failed to parse properties JSON for node ...` and
  `No nodes found in the database`. Railway dropped `750` messages after a
  replica reached its `500 logs/sec` limit.
- [INFERRED] The service is live and can answer through fallback retrieval, but
  production is not ready for the target vector-backed coverage contract.
- [NOT DETERMINED] The first invalid graph record, the source of its malformed
  properties, and the safe repair method remain unknown.
- [PLANNED] Add stage-specific projection receipts and a bounded graph-property
  repair test. Then run the full known-answer suite again, including the scoped
  Linear path and vector backend evidence.

### Least confident decisions

1. Repairing malformed graph properties may require a versioned migration, but
   no destructive database action is approved.
2. A successful fallback search must not count as vector acceptance without
   backend evidence in the response.

## 2026-08-23 Scoped Linear filter repair

- [VERIFIED] The production scoped Linear probe returned zero hits after
  fetching `15` candidates. The same session's unfiltered search returned a
  Linear issue, so the result did not prove missing Linear data.
- [VERIFIED] Lexical lifecycle results preserve `metadata.source_key`, but
  their retrieval backend label is `lifecycle`.
- [VERIFIED] The local filter repair derives `linear-issue` only from a strict
  `linear:issue:` source-key prefix. It does not derive source scope from body
  text.
- [VERIFIED] Endpoint regression tests pass, and the full local suite returned
  `2359 passed, 6 skipped, 12 warnings in 90.93s`.
- [NOT DETERMINED] Railway does not run this repair until a new deployment is
  approved. Readiness remains false because the searchable census is still
  incomplete.
- [PLANNED] After approval, verify the scoped Linear tool returns a known issue
  with citations. Verify CLI and general MCP search separately. Treat any
  fallback response as non-accepting until backend evidence is present.

### Least confident decisions

1. Older lifecycle records may use source-key shapes outside the strict mapping.
2. A successful Linear filter check will not resolve the projection census or
   malformed graph-property findings.

## 2026-08-23 Post-deploy source identity checkpoint

- [VERIFIED] Deployment `89a6a85c-8169-4409-976b-5f4f7db1c6c0` succeeded in
  Railway production.
- [VERIFIED] Scoped Linear search now returns `2/15` candidates and confirms
  `scope_applied=true`. The first citation is DES-144, titled `Subscription
  Credits missing`, with its Linear issue URL in the reference.
- [VERIFIED] Repo-scoped search for `masumi-network/Sokosumi-MCP` returns
  `0/19` candidates. This is a separate source-identity defect.
- [VERIFIED] The local patch maps strict GitHub source keys to public `repo`
  and `path` provenance. Endpoint tests and the full suite pass.
- [VERIFIED] Recent logs still show graph-property parse warnings, `No nodes
  found in the database`, and `Messages dropped: 752`. They also show a high
  severity secret scanner block for one repository file.
- [NOT DETERMINED] Readiness remains false because the corpus probe timed out
  after `2s`, with searchable counts graph `25`, relational `323`, and vector
  `20`.
- [PLANNED] Deploy the repo/path repair after fresh approval. Verify filtered
  MCP and CLI search, then keep acceptance blocked until the corpus check is
  healthy or the remaining failure has a measured repair plan.

### Least confident decisions

1. Legacy GitHub source-key formats may need a compatibility mapping.
2. The security block may explain part of the source-count difference, but the
   current evidence does not prove that link.

## 2026-08-23 Fresh-eyes review of source identity repair

- [REPORTED] Fresh-eyes initially found possible source-key forgery and
  delimiter and legacy-key risks.
- [VERIFIED] Public ingest and MCP ingest do not accept caller-provided
  `source_key`. Contribution URLs become `contribution:<url>` before storage.
- [VERIFIED] The final parser rejects malformed Linear IDs, invalid GitHub repo
  names, and all non-printable path characters. It supports chunk-parent
  provenance before child-key fallback.
- [VERIFIED] Regression coverage includes the endpoint path. The final suite
  returned `2363 passed, 6 skipped, 12 warnings in 113.35s`.
- [REPORTED] Fresh-eyes final verdict: `APPROVED`, with no remaining real
  findings.
- [NOT DETERMINED] Production repo and path scope is unverified for this final
  revision. The prior production result was `0/19`.
- [PLANNED] After fresh deployment approval, run repo, path, Linear, and CLI
  checks. Do not claim complete retrieval until readiness and backend evidence
  also pass.

### Least confident decisions

1. Internal writer paths may still need a separate source-key audit.
2. The production candidate metadata must be inspected if repo scope remains
   empty after deployment.

## 2026-08-23 Fresh-eyes plan review

- [REPORTED] Fresh-eyes returned `CHANGES REQUIRED` for the first deployment
  plan. The gaps were artifact identity, build identity, known-answer criteria,
  mandatory raw metadata inspection for `0/19`, and an independent refute.
- [VERIFIED] Current artifact identity is `HEAD
  a910f4f37a758dd033c370b7a55c5cf7467d58b9` plus worktree patch fingerprint
  `9da920a1e1cf36fa8e8bc4b099c400f6c73505c023efe5460812690cd97bf4e6`.
- [VERIFIED] The image writes a wheel hash to `/opt/citadel/build-id`, and the
  public discovery document returns build and deployment identity.
- [PLANNED] Before acceptance, record the artifact fingerprint, match Railway
  deployment identity, check health and readiness, run known-answer MCP and
  CLI searches, inspect raw candidates for the known repo `0/19` failure, and
  ask fresh-eyes to attack the evidence.
- [NOT DETERMINED] The final parser fingerprint is not in production yet.

### Least confident decisions

1. Railway must deploy the recorded dirty-worktree patch.
2. Existing read-only diagnostics may not expose raw pre-filter candidates.

## 2026-08-23 Fresh-eyes plan review correction

- [REPORTED] The second review required an immediate pre-deploy freeze, an
  artifact-to-Railway match, exact reproducible commands, and evidence-first
  fresh-eyes review.
- [PLANNED] Immediately before deployment, rerun `git rev-parse HEAD` and the
  parser patch hash command. Require the recorded HEAD and patch hash. Stop on
  any mismatch.
- [PLANNED] Hash a fresh local `citadel_archive-0.5.1` wheel and compare that
  value with the deployed public `build_id`. Match the public deployment ID to
  Railway before search tests.
- [PLANNED] Send fresh-eyes the frozen IDs and raw outputs without a pass or
  fail conclusion. Keep repo `0/19` blocked until raw candidate metadata is
  captured or the diagnostic gap is recorded as a blocker.

### Least confident decisions

1. Wheel reproducibility must be measured before build identity can be used.
2. Raw candidate access may need a read-only diagnostic.

## 2026-08-23 Fresh-eyes plan review: build identity correction

- [REPORTED] Fresh-eyes found that runtime identity prefers
  `RAILWAY_GIT_COMMIT_SHA`, so a wheel hash cannot be assumed to be `build_id`.
- [VERIFIED] Live `/.well-known/citadel.json` returned `build_id: null` and
  deployment `89a6a85c-8169-4409-976b-5f4f7db1c6c0`.
- [CORRECTED] Treat a missing build ID as a deployment identity failure. Use a
  wheel hash only as separate build evidence when Railway exposes it.
- [PLANNED] Record the exact upload command and current worktree identity just
  before deployment. A dirty worktree with no source fingerprint blocks
  acceptance. After deployment, send fresh-eyes raw manifest, health, Railway,
  log, and search outputs without a verdict.

### Least confident decisions

1. Local Railway uploads may omit source identity.
2. A clean source revision may be required before deployment.

## 2026-08-23 Audit-record indexing checkpoint

- [VERIFIED] CLI ingest returned `NODE_UNREACHABLE`; MCP search remained
  available.
- [VERIFIED] MCP capture accepted the three fresh-eyes sections into
  `seat:sarthi` with projection jobs
  `75b5177e-002f-54a6-8227-6ffe42e89d94`,
  `492edaf6-7524-591d-8992-159cfad2d5d5`, and
  `7f8bb653-64d1-5e22-915f-852c3ae637be`.
- [VERIFIED] All three jobs remained `pending` across relational, vector, and
  graph receipts after 35 seconds. Exact searches returned older snapshot and
  source-file notes, not the new audit text.
- [INFERRED] Capture is durable, but searchable projection is not proven. The
  projection cause remains open.
- [PLANNED] Inspect the scheduled projection worker and rerun operation and
  exact-text checks before claiming these records are searchable.

### Least confident decisions

1. The jobs may be delayed rather than failed.
2. CLI and MCP may read different node configuration paths.

## 2026-08-23 Audit-record indexing follow-up

- [VERIFIED] At `2026-08-23T11:49:24Z`, all three jobs were still pending with
  `attempt: 0` and no error code. They were accepted at about `11:43:20Z`.
- [VERIFIED] The source status reported vector records `67`, graph records
  `7656`, and zero indexed chunks since restart.
- [NOT DETERMINED] The worker may wait on a lock, an older queue item, or a
  scheduled pass. The public operation response does not expose that cause.
- [PLANNED] Keep search acceptance blocked. Inspect read-only worker and log
  evidence before sending another write.

### Least confident decisions

1. The worker may not have claimed the new jobs.
2. Graph count growth is not proof that these documents are searchable.

## 2026-08-23 Live blocker refresh

- [VERIFIED] Search smoke is green, but overall status is unhealthy. The corpus
  check fails `current_generation_searchable_census_mismatch` and reports
  `61/601` fully indexed samples.
- [VERIFIED] Searchable counts are graph `24`, relational `322`, and vector
  `21`. The corpus response also says the two-second health probe timed out and
  served a cached result.
- [VERIFIED] Source listing reports repo content discovery complete with `4721`
  tracked files. Linear reports `1800` issues and `2867` context records, with
  both listings complete.
- [VERIFIED] The evolve summary is overdue. Its last completion is
  `2026-08-22T23:49:20.458291Z`.
- [NOT DETERMINED] New audit jobs have not reached attempt one. The public API
  does not show whether a lock, queue order, or scheduled pass holds them.
- [INFERRED] The current green search check is a connectivity smoke test, not a
  proof that the corpus or new documents are searchable.
- [PLANNED] Inspect read-only worker and lock evidence. Keep readiness and audit
  searchability blocked until the queue claim path works.

### Least confident decisions

1. The overdue evolve pass may be related to the queue delay.
2. The corpus timeout may hide slower reads, but readiness still fails.

## 2026-08-23 Projection operations correction

- [CORRECTED] Capture-only ingest no longer means that real Cognee sources wait
  for an LLM graph pass before vector retrieval. The baseline worker projects
  relational data and vectors, then records graph work as deferred. See
  `kb/service.py:276-280` and `kb/lifecycle_worker.py:648-650`.
- [VERIFIED] User search remains independent of the graph lane. The vector
  pipeline has no graph task and runs outside `maintenance` at
  `kb/cognee_client.py:4680-4709`.
- [VERIFIED] The scheduled graph lane is separate. It claims only deferred
  jobs, while baseline claims exclude deferred jobs. The deferred state records
  the exact reason `graph_enrichment_deferred` at `kb/lifecycle.py:2176-2189`.
- [VERIFIED] Scheduler shutdown resumes deferred work after Phase 1 cancellation
  through `_resume_lifecycle_queue(citadel, include_deferred=True)` at
  `kb/server.py:456-458`.
- [VERIFIED] Queue order now gives the deferred-only lane durable FIFO order by
  `available_at`. Baseline seat priority remains explicit in
  `kb/lifecycle.py:1891-1900`.
- [VERIFIED] Focused verification returned `309 passed, 11 warnings in 28.98s`.
  Ruff returned `All checks passed!`, and `git diff --check` returned no output.
- [CORRECTED] The focused count above predates the final Phase 2 cancellation
  regression test. The final full command returned `2373 passed, 6 skipped,
  12 warnings in 52.86s`.
- [VERIFIED] The logging diagnostic switch is reversible. Default logging
  disables LiteLLM named loggers. Explicit `DEBUG` re-enables them at
  `kb/logging_utils.py:93-112`.
- [NOT DETERMINED] The test double proves task scheduling under a held graph
  maintenance context. It does not prove safe concurrent writes inside the real
  Cognee, SQLite, or Qdrant providers.
- [NOT DETERMINED] No production deployment or Railway runtime check was run
  for this revision. Do not use this local result as live evidence.
- [NOT DETERMINED] Graph-property parse warnings, existing graph corruption, and
  the safe repair path remain unresolved. Do not delete or rewrite provider data
  until a read-only diagnosis identifies the affected records.
- [PLANNED] Deployment runbook: freeze identity, obtain current approval, deploy,
  match runtime identity, check `/healthz` and `/health/ready`, run CLI and MCP
  known-answer searches, inspect backend and citation fields, then obtain a
  fresh-eyes refutation of the raw evidence.

### Least confident decisions

1. Real-provider concurrency needs a disposable acceptance run.
2. The graph warning repair needs a measured bad record before any migration.

## 2026-08-23 Fresh-eyes final review

- [REPORTED] Fresh-eyes first reported a Phase 1 cancellation gap and an
  unavailable-Cognee test environment. The first claim was stale against the
  current worktree, and the primary environment has Cognee installed.
- [VERIFIED] Current `kb/server.py:456-458` passes
  `include_deferred=True` during Phase 1 cancellation. Current
  `kb/server.py:502-523` resumes the queue only when Phase 2 did not cancel.
- [REPORTED] Fresh-eyes rechecked the current files and returned `APPROVED`.
  It kept provider-level concurrency and malformed graph data as unresolved.
- [VERIFIED] Final full verification returned `2373 passed, 6 skipped,
  12 warnings in 52.86s`. Full Ruff returned `All checks passed!`.
- [NOT DETERMINED] Production behavior is still unverified for this local
  revision. Deployment and Railway runtime checks remain pending approval.

### Least confident decisions

1. Real Cognee provider concurrency still needs a disposable acceptance run.
2. Existing malformed graph records still need read-only diagnosis.

## 2026-08-23 Retrieval and graph-error repair checkpoint

- [VERIFIED] General multi-term search now ranks candidates by observed lexical
  term coverage when that evidence exists. It preserves provider order when all
  candidates have zero coverage.
- [VERIFIED] The local focused search contract returned `151 passed, 11
  warnings in 7.09s` for search formatting, response shaping, and MCP tests.
- [CORRECTED] MCP and CLI ingest documentation now states that the write path
  is capture-only. Background relational and vector projection may run later.
  Graph enrichment remains scheduled and does not run inline for user writes.
- [VERIFIED] Railway's current successful deployment is
  `89a6a85c-8169-4409-976b-5f4f7db1c6c0`, created at
  `2026-08-23T10:23:38.122Z`.
- [VERIFIED] Railway logs contain `Messages dropped: 760`, repeated graph
  property parse warnings, `No nodes found in the database`, and an invalid
  structured graph edge where `target_node_id` received `None`.
- [INFERRED] Graph enrichment remains an operational fault in the live image.
  The local vector lane repair isolates user retrieval from that graph lane.
- [NOT DETERMINED] The live service does not yet prove this worktree revision.
  Do not use local test results as Railway acceptance evidence.
- [PLANNED] Before deployment, run the full local suite and artifact checks.
  After deployment approval, run known-answer searches through both CLI and MCP.
  Require source identity, citations, retrieval readiness, and backend receipts.

### Least confident decisions

1. The ranking change cannot improve a query whose relevant result shares no
   lexical terms with the query.
2. Graph repair needs a bounded reproduction before changing model or provider
   settings.

## 2026-08-23 Local repair verification

- [CORRECTED] Ranking now preserves the existing multi-dataset source reserve
  after lexical ordering. `kb/server.py:3209-3229` keeps Central in a page when
  the request spans Node and Central datasets.
- [VERIFIED] The graph-only worker detects the Railway-observed malformed
  `KnowledgeGraph` edge response and defers it for one hour. It does not mark
  searchable relational or vector receipts failed. See
  `kb/lifecycle_worker.py:140-154` and `kb/lifecycle_worker.py:327-341`.
- [VERIFIED] The focused lifecycle test returned `25 passed in 6.11s`.
- [VERIFIED] The search, server, and MCP tests returned `41 passed, 11 warnings
  in 5.94s`.
- [VERIFIED] Full verification returned `2377 passed, 6 skipped, 12 warnings
  in 76.96s (0:01:16)`. Ruff returned `All checks passed!`, and
  `git diff --check` returned no output.
- [INFERRED] The local scheduled graph lane can now tolerate the observed LLM
  property-validation failure without blocking user retrieval.
- [NOT DETERMINED] No live deployment contains a proven match to this local
  revision. Railway acceptance remains pending.
- [PLANNED] After fresh-eyes review and current deployment approval, deploy once
  and verify runtime identity, readiness, CLI search, MCP search, citations,
  source filters, and relational/vector/graph receipt states.

### Least confident decisions

1. The one-hour retry delay may need tuning after live queue observation.
2. Existing malformed graph records still need a read-only record-level audit.

## 2026-08-23 Fresh-eyes latest review

- [REPORTED] Fresh-eyes reviewed the latest local changes and returned
  `APPROVED`. It found no blocking issue in ranking, source reservation, vector
  projection, or graph deferral.
- [REPORTED] Fresh-eyes kept real provider behavior and existing malformed graph
  data unresolved. These items still require live evidence.

### Least confident decisions

1. The deployment must prove it contains this worktree before acceptance.
2. Existing graph records need read-only inspection before any repair.

- [CORRECTED] The final full-suite rerun after test cleanup returned `2377
  passed, 6 skipped, 12 warnings in 76.79s (0:01:16)`.

### Least confident decisions

1. Live provider concurrency remains untested.
2. Existing graph records still need read-only inspection.

## 2026-08-23 Core-path live audit

- [VERIFIED] Citadel MCP `citadel_list_sources` reports GitHub activity for
  `masumi-network` with `tracked_repositories: 49` and
  `tracked_commit_repositories: 49`.
- [VERIFIED] The same MCP response reports repository-content sync with
  `all_repos: true`, `repo_discovery_complete: true`, `tracked_files: 4721`,
  and `refused_files: 2`. This proves source discovery and local sync state,
  not vector searchability.
- [VERIFIED] The same MCP response reports Linear with `issue_count: 1800`,
  `context_record_count: 2867`, `listing_complete: true`,
  `context_listing_complete: true`, and `last_error: null`.
- [VERIFIED] MCP `citadel_linear_search("SOK-563")` returned two hits with
  `scope_applied: true`. One returned title was
  `linear:issue:df5cff71-6960-4cf9-9ea1-5073c32f06dd`.
- [CORRECTED] Source ingestion state is green, but current-generation
  projection coverage is not. `citadel status --json --check-search
  --no-recent` returned `search: false` and the exact detail
  `timed out after 15s - node warming up`. It also reported
  `current_generation_searchable_census_mismatch`, with `graph 25`,
  `relational 323`, `vector 22`, and `64/602 sampled documents fully indexed`.
- [INFERRED] Connector health and searchability are separate operations. A
  source can have a successful sync record while its documents remain absent
  from the searchable vector generation.
- [NOT DETERMINED] The failed projection stage is not proven. Possible causes
  include queue starvation, provider latency, or incremental Cognee state.
  Do not reindex, delete graph records, change embedding dimensions, or alter
  Railway variables until a read-only operation audit identifies the stage.
- [PLANNED] The next acceptance test is a citation-bearing known answer through
  CLI and MCP, followed by a current-generation vector census.

### Least confident decisions

1. The exact failed projection stage still needs read-only operation evidence.
2. The status field `vector.records: 70` is not a document census until its
   contract is verified.

## 2026-08-23 Core projection repair checkpoint

- [VERIFIED] Authenticated Railway `GET /readyz` returned
  `"ok":false` with `current_generation_searchable_census_mismatch`.
- [VERIFIED] Its current-generation state was
  `"current_job_states":{"completed":21,"pending":9589,"running":1}` and
  `"current_searchable_by_backend":{"graph":25,"relational":323,"vector":22}`.
- [VERIFIED] Railway logs returned
  `Railway rate limit of 500 logs/sec reached for replica, update your
  application to reduce the logging rate. Messages dropped: 8137`.
- [INFERRED] The graph LLM lane is flooding logs while it handles malformed
  structured output. This can hide queue progress. It does not prove that the
  vector provider failed.
- [CORRECTED] The local vector path previously called Cognee's custom pipeline
  without `data`. Cognee 1.4.1 then used
  `get_dataset_data(dataset_id=dataset.id)` for every call, as shown at
  `.venv/lib/python3.12/site-packages/cognee/modules/pipelines/operations/pipeline.py:122`.
- [VERIFIED] The repair at `kb/cognee_client.py:1590-1639` selects only
  authorized `Data` rows for the requested dataset and source IDs. The worker
  sends one claimed source ID at `kb/lifecycle_worker.py:642-647`.
- [VERIFIED] Targeted calls set `incremental_loading=False` and
  `data_cache=False`, so stale Cognee pipeline status cannot skip an unsearchable
  source. Tests cover the flags and the authorization-scoped selector.
- [VERIFIED] Focused verification returned `115 passed, 10 warnings in 8.41s`.
  Ruff returned `All checks passed!`.
- [NOT DETERMINED] No production deploy or repair ran. Railway still runs an
  unverified revision for this worktree.
- [NOT DETERMINED] The ready check does not reveal the exact failed provider
  operation. Keep reindex and variable changes gated on that evidence.
- [PLANNED] After deployment approval, verify one new source through ingest,
  vector projection, CLI search, MCP search, citation, and lifecycle receipts.

### Least confident decisions

1. The repaired call shape needs a live timing measurement before setting a
   worker batch size.
2. Log suppression needs a separate change after identifying the noisy logger.

## 2026-08-23 Known-answer acceptance checkpoint

- [VERIFIED] CLI `citadel search "SOK-563" --json --top-k 1 --timeout 30`
  returned exit `0`, `"ok": true`, and `"timed_out": false`.
- [VERIFIED] The result had title
  `linear:issue:df5cff71-6960-4cf9-9ea1-5073c32f06dd`, source
  `linear-issue`, and citation flags `citation_required: true` and
  `citations_available: true`. The returned locator was the Linear SOK-618
  issue URL.
- [VERIFIED] MCP search for `SOK-563` in `masumi-network` returned the same
  title and document ID `e4ec6025-b6d8-54d8-aa83-55f5d7b35881`. It returned a
  citation reference and `no_lexical_match: false`.
- [CORRECTED] The live search path is partially working. The status canary
  still reports a 15-second warm-up timeout and an incomplete generation, but
  a 30-second known-answer request returns a citation-bearing result.
- [NOT DETERMINED] Do not treat this single hit as proof that repository files
  or all Linear records are vector searchable.

### Least confident decisions

1. The 15-second readiness limit may be too low for a cold node.
2. Repository known-answer coverage still needs a deployed repair.

## 2026-08-23 Vector batch cancellation checkpoint

- [VERIFIED] The vector-only lifecycle lane now claims a bounded batch of jobs
  from one dataset and sends one `vector_project` call with all source IDs.
  Each source still receives its own lifecycle receipt and lease result.
- [VERIFIED] The vector-only lane does not reconcile graph presence. Graph
  enrichment remains deferred to the separate graph worker.
- [VERIFIED] Batch cancellation during relational preparation reschedules every
  still-owned claimed lease before re-raising `asyncio.CancelledError`.
- [VERIFIED] Batch cancellation during vector projection cancels the provider
  awaitable and reschedules the current and remaining active leases.
- [VERIFIED] `uv run pytest -q tests/test_lifecycle_worker.py -x` returned
  `28 passed in 5.42s` after the cancellation tests were added.
- [VERIFIED] The bounded lifecycle, worker, service, and config suite returned
  `170 passed, 10 warnings in 17.22s` before the cancellation test additions.
- [PLANNED] Set `CITADEL_LIFECYCLE_PROJECTION_BATCH_SIZE=20` in the deployed
  revision only after explicit deployment approval. Then measure queue drain
  rate, vector receipt growth, and one repository known answer.
- [NOT DETERMINED] The batch size and provider throughput are not measured on
  Railway. This local test uses a fake projection gateway.
- [NOT DETERMINED] The local revision is not proven to be the active Railway
  deployment. No deployment or production reindex ran in this checkpoint.

### Least confident decisions

1. A batch size of 20 is a starting value, not a production capacity result.
2. The deployment still needs identity matching before its queue metrics can
   be attributed to this code.

## 2026-08-23 Local core acceptance checkpoint

- [VERIFIED] `uv run pytest -q` returned
  `2383 passed, 6 skipped, 12 warnings in 81.40s (0:01:21)`.
- [VERIFIED] `uv run ruff check .` returned `All checks passed!`.
- [VERIFIED] The avoid-AI-writing detector returned exit `0` with no output for
  `docs/progress.md`, `docs/operations.md`, and `.local-review/status.md`.
- [REPORTED] Fresh-eyes re-review of the batch cancellation and config changes
  returned the exact result `APPROVED`.
- [NOT DETERMINED] These local checks do not prove the active Railway revision,
  production queue drain, or live cross-repository vector recall.
- [PLANNED] After explicit deployment approval, match the deployed build
  identity, observe queue and receipt counts, then run CLI and MCP known-answer
  searches for one Linear record and one repository file.

### Least confident decisions

1. Railway throughput after deployment remains unmeasured.
2. Full cross-repository recall remains unproven until the live acceptance gate.

## 2026-08-23 Core functionality handoff boundary

- [VERIFIED] Railway `/healthz` returned `{"ok":true,"service":"citadel"}`.
- [VERIFIED] Read-only Railway status reported project `Citadel`, production
  environment, and service `Citadel-Archive`.
- [VERIFIED] The latest deployment list entry was
  `89a6a85c-8169-4409-976b-5f4f7db1c6c0 | SUCCESS | 2026-08-23 12:23:38 +02:00`.
- [VERIFIED] `citadel status --json --check-search --no-recent` reported
  `current_generation_searchable_census_mismatch`, `21` completed jobs,
  `9589` pending jobs, `1` running job, and vector searchability `22`.
- [VERIFIED] Railway logs reported `Messages dropped: 8137` after the Railway
  log rate limit reached `500 logs/sec` for a replica.
- [INFERRED] The graph LLM lane is flooding logs. This does not prove that the
  vector provider caused the pending queue.
- [NOT DETERMINED] The active deployment does not expose a source build ID.
  Do not attribute local projection changes to Railway until identity matches.
- [PLANNED] After explicit deployment approval, record `/readyz`, the public
  manifest, and two status snapshots. Compare pending jobs, completed jobs,
  vector receipts, and elapsed time.
- [PLANNED] Run one repository and one Linear known-answer search through both
  CLI and MCP. Check citations, retrieval mode, drilldown, and seat isolation.
- [PLANNED] Do not reindex, migrate, change Railway variables, or run a
  production ingest until the user approves that action.

### Least confident decisions

1. Queue failure stage is not exposed by the current ready check.
2. Live search result mode is not yet confirmed after the local metadata fix.
