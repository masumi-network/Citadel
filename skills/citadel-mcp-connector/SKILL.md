---
name: citadel-mcp-connector
description: >-
  Connects Claude Code, Cursor, Codex, or any MCP-capable agent to the Citadel
  Archive Organization Vault over its hosted MCP endpoint (no clone, no Python,
  no local install). Use when the user shares this skill URL, pastes a Citadel
  token, or says connect/set up/configure Citadel MCP, citadel plugin,
  organization vault MCP, or add citadel to my agent. Run the full workflow:
  detect client, collect only the token, write the remote MCP config
  (URL + Authorization header), verify, then search the vault. Triggers include
  connect citadel, set up citadel mcp, citadel mcp connector, citadel archive mcp,
  and https://citadel-archive-production.up.railway.app/skills/connect.
---

# Citadel MCP Connector

Citadel is served as a **hosted MCP endpoint**. Agents connect with a URL and a
token — there is **no repository to clone**, no `uv`, and no local Python.

```
MCP endpoint:  https://citadel-archive-production.up.railway.app/mcp/
Auth:          Authorization: Bearer ctdl_<your-token>
```

## Public vs private (read first)

| Public | Private |
|---|---|
| The hosted REST + MCP API surface and these `/skills/*` docs | The vault contents — only readable with a `ctdl_` token |
| [Citadel-Archive](https://github.com/masumi-network/Citadel-Archive) — app code, skills | Team/organization memory behind the token |

Never commit tokens. Never copy vault search results into a public repo or issue.
Boundary detail: `https://citadel-archive-production.up.railway.app/skills/boundary`

## Skill URLs

- Connect (this skill): `https://citadel-archive-production.up.railway.app/skills/connect`
- After MCP works: `https://citadel-archive-production.up.railway.app/skills/vault`
- Full repo skill install: `npx skills add masumi-network/citadel-archive --skill citadel-archive`

If the user shares the `npx skills add` command, install the `citadel-archive`
skill first (optionally `--skill '*'` for all bundled skills), then continue
this connector workflow.

## Agent workflow (run in order)

When this skill is loaded — especially from the URL above — **do not stop at
explaining**. Execute the steps below unless the user only asked for docs.

### 1. Collect the one required secret

Ask for the **Citadel access token** if it is not already in the environment or
client config:

- Must start with `ctdl_`.
- Create one in the Citadel UI → **Access** → **Create Token** (reader is enough
  for search; writer for ingest; admin for ops), or from the terminal with
  `citadel seat create` (admin, needs `CITADEL_ADMIN_KEY`). This `ctdl_` seat
  token is the teammate's API key.
- The user pastes it once. **Never echo it back** in chat, logs, or commits.

That is the only secret. Do **not** ask for clone paths, `uv`, seed phrases,
wallet keys, or unrelated API keys.

Defaults (override only if the user does):

| Setting | Default |
|---|---|
| MCP endpoint | `https://citadel-archive-production.up.railway.app/mcp/` |
| Token env name | `CITADEL_MCP_ACCESS_TOKEN` |
| Search dataset | `masumi-network` (the server defaults this too) |

### 2. Write the remote MCP config

Pick the user's client. Each config points at the hosted `/mcp/` URL and sends the
token in the `Authorization` header. Store the token in an env var or the client's
secret store — **never** as a literal in a tracked file.

> **Shortcut:** with the CLI installed (`pipx install citadel-archive`),
> `citadel mcp add <tool>` writes this config for you — it auto-configures
> Cursor, Codex, Gemini, and Windsurf, and prints a paste-in snippet for the
> rest (`citadel mcp list` shows supported targets). The manual configs below
> stay valid if you prefer to edit by hand.

#### Claude Code — project `.mcp.json`

```json
{
  "mcpServers": {
    "citadel": {
      "type": "http",
      "url": "https://citadel-archive-production.up.railway.app/mcp/",
      "headers": {
        "Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}"
      }
    }
  }
}
```

Or one command (token expanded from your shell):

```bash
claude mcp add --transport http citadel \
  https://citadel-archive-production.up.railway.app/mcp/ \
  --header "Authorization: Bearer ${CITADEL_MCP_ACCESS_TOKEN}"
```

Export the token first: `export CITADEL_MCP_ACCESS_TOKEN='ctdl_...'`.

**Zero tools / missing env (Claude Code):** `.mcp.json` uses
`Bearer ${CITADEL_MCP_ACCESS_TOKEN}`. Claude expands that only when the variable
is in the **process environment** that launched Claude — PRs and docs never
inject secrets.

| Where | Fix |
|---|---|
| **Local CLI** | `source ~/.zshrc` (or open a new terminal), then start `claude` from that shell |
| **Claude cloud** | Set `CITADEL_MCP_ACCESS_TOKEN` in cloud **environment settings** |

Verify: `claude mcp list` (no missing-env warning), `/mcp` inside Claude (citadel
tools listed, not "connected with zero tools"), and `citadel doctor`.

#### Cursor — `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "citadel": {
      "url": "https://citadel-archive-production.up.railway.app/mcp/",
      "headers": { "Authorization": "Bearer ctdl_..." }
    }
  }
}
```

#### Codex / stdio-only hosts — `mcp-remote` bridge

For a client that only speaks stdio, bridge to the hosted endpoint with
`mcp-remote` (no clone, just `npx`):

```toml
# ~/.codex/config.toml
[mcp_servers.citadel]
command = "npx"
args = [
  "-y", "mcp-remote@0.1.38",
  "https://citadel-archive-production.up.railway.app/mcp/",
  "--header", "Authorization: Bearer ${CITADEL_MCP_ACCESS_TOKEN}",
]
```

Pin `mcp-remote` (do not use floating `mcp-remote` / latest). Prefer env
substitution for the token — never commit a literal `ctdl_` value.

#### Any other MCP host

Point it at the streamable-HTTP URL `…/mcp/` with header
`Authorization: Bearer ctdl_…`. If it cannot send headers, use the `mcp-remote`
bridge above.

### 3. Verify (before claiming success)

**A. HTTP reachability (works immediately):**

```bash
curl -fsS "https://citadel-archive-production.up.railway.app/healthz"
curl -fsS -H "Authorization: Bearer $CITADEL_MCP_ACCESS_TOKEN" \
  "https://citadel-archive-production.up.railway.app/api/session"
```

Expect HTTP 200 and JSON with `role` / `actor`. On 401 the token is missing,
wrong, or revoked. Never print the token in errors.

**B. MCP (after the user restarts the client):**

Ask the user to **restart** the client so the new server loads, then call tools:

1. `citadel_discovery` — confirm the MCP endpoint, skill hashes, tool policy,
   and public/private boundary metadata.
2. `citadel_session` — confirm role and capabilities.
3. `citadel_search` with a small query (e.g. `architecture` or a project name).
4. From a search hit, pass its `id` to `citadel_get_document` to drill down.

Production smoke status, last verified 2026-06-02 at commit `7a4a1d9`:

- hosted MCP initializes and lists Citadel tools;
- `citadel_discovery` returns the safe public manifest;
- `citadel_session` returns the caller role and capabilities;
- `citadel_search` returns company search results;
- `citadel_ingest` succeeds with a writer token.

### 4. Start fetching (normal operation)

- **Before** answering project/architecture/source questions → `citadel_search`.
- Cite from each hit's `_citadel.provenance` and `_citadel.content_sha256`.
- To open a hit in full → `citadel_get_document` with the result `id`, but only
  when `_citadel.retrieval.document_drilldown_available` is true.
- **When** the user asks to remember something durable → `citadel_contribute`
  for titled notes or `citadel_ingest` for raw context (writer token + approval).
- **When** the user asks "what do I need to do?" → `citadel_linear_my_issues`
  (reads the **Seat-Scoped Mirror** from the latest Linear cron sync).
- Follow the **citadel-vault** skill for read/write/admin rules.
- For autonomous personal capture (git push + optional SessionEnd), point the user
  to `citadel onboard` (installs the git pre-push and SessionEnd hooks that run
  `python -m kb.hooks.sync_push` / `python -m kb.hooks.sync_session`) or
  [`docs/onboarding/teammate-rollout.md`](../../docs/onboarding/teammate-rollout.md).

### Seat-writer tokens (recommended for devs)

Mint a **seat-writer** token from the connect wizard. It carries
`default_dataset=seat:{slug}` so writes with no `dataset` field land in the
dev's private **Node**. One token powers MCP search, ingest, and background
hooks (`CITADEL_MCP_ACCESS_TOKEN`). Never share tokens between seats.

**Security model for seat MCP:**

| Action | Seat MCP | Central |
|---|---|---|
| Search / read | Own node + Central | Read-only |
| `citadel_ingest` | Personal node only (after user approval) | Blocked |
| `citadel_contribute` | Blocked | Use org sync / promotion instead |
| Admin sync tools | Blocked (admin token only) | Cron-driven |

Configure the MCP client to **always ask before** `citadel_ingest` and other
write tools. The server also runs a secret/sensitivity scan on every write.

## Tools

| Tool | Role | What it does |
|---|---|---|
| `citadel_discovery` | reader | Safe manifest with MCP endpoint, skill hashes, and tool policy |
| `citadel_session` | reader | Authenticated role, actor, scopes |
| `citadel_search` | reader | Search the vault (dataset defaults server-side) |
| `citadel_get_document` | reader | Fetch a full document by a search hit `id` |
| `citadel_get_mesh` | reader | Knowledge-mesh snapshot |
| `citadel_list_sources` | reader | GitHub sync, learning-agent, Linear sync, index status |
| `citadel_linear_my_issues` | reader | Your assigned Linear tasks (**Seat-Scoped Mirror** in your **Node**) |
| `citadel_linear_search` | reader | Org-wide Linear context in **Central** |
| `citadel_recent_contributions` | reader | Recent vault contributions (`mine=true` for yours) |
| `citadel_ingest` | writer | Add durable context |
| `citadel_contribute` | writer | Add a titled Vault Contribution (enrichment + conflict detection) |
| `citadel_record_feedback` | writer | Record feedback on a search hit or QA result (pass `qa_id` **or** `result_id`) |
| `citadel_share_session` | writer | Volunteer a Shared Session Trace (dead ends) — **explicit user approval only** |
| `citadel_run_learning_agent` | admin | Run source learning |
| `citadel_promotion_pending` | admin | List contributions awaiting promotion to Central |
| `citadel_promotion_approve` | admin | Promote a pending contribution to Central |
| `citadel_promotion_reject` | admin | Reject a pending contribution |
| `citadel_run_repo_content_sync` | admin | Sync READMEs, skills, and docs from allowlisted repos through cognify |
| `citadel_backup_mirror_status` | admin | Inspect backup mirror manifest status |
| `citadel_run_backup_mirror` | admin | Run backup mirror manifest export |
| `citadel_audit_events` | admin | Inspect bounded audit events |
| `citadel_improve` | admin | Run Cognee improvement |

## Safety rules

- **Always ask the user before any write tool** (`citadel_ingest`, `citadel_contribute`,
  `citadel_record_feedback`, `citadel_share_session`). Configure your MCP client to
  require approval for those tools (see `docs/mcp/codex-hosted.config.toml`).
  `citadel_share_session` publishes a trace to teammates — never call it on your
  own initiative.
- **Seat-writer tokens:** MCP writes land in the personal node only. Central is
  read-only from MCP; org sync and selective promotion update Central.
- Do not commit tokens to git or paste them into PRs/issues.
- Do not echo tokens in chat, logs, or tool output.
- Prefer **reader** tokens; use writer only when the user explicitly asks to save something.
- Never ingest secrets, API keys, tokens, passwords, private keys, seed phrases, PII,
  or raw logs — the server secret gate blocks many patterns but curation is still required.
- Approval-gate all write/admin tools when the client supports per-tool approval.

## Security

This connector only configures a **remote MCP URL** plus an Authorization
header. It does not install background hooks by itself. Trust boundary:

- Hosted endpoint is the org's Citadel Node (default Railway URL above).
- The token stays in env / client secret store — never in tracked git files.
- Prefer native HTTP MCP (`type: http`) over `mcp-remote`; when a stdio bridge
  is required, pin `mcp-remote@0.1.38` and pass the token via env expansion.
- If no `citadel_*` tools register, fall back to the CLI (`citadel status`, then
  `citadel search` / `citadel doctor`) instead of re-authing MCP forever. If CLI
  is unhealthy, use official/canonical docs and do not claim vault-backed authority.
  Never claim “Citadel confirms X” without a retrieved title + snippet. Never use
  Citadel as sole authority for Mainnet payment token units (USDCx / USDM / policy
  hex) — prefer official Masumi docs / `skills/masumi`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| 401 on session/search | Set the token; check it is not revoked |
| 403 on ingest | Token is reader-only; create a writer token |
| MCP connected, **zero tools** (Claude) | Token not in Claude's process env — see zero-tools table above; run `claude mcp list` + `citadel doctor` |
| Tools missing after config | Restart the MCP host |
| Client can't send headers | Use the `mcp-remote` stdio bridge above |
| Endpoint unreachable | Check `…/healthz`; confirm the `…/mcp/` URL |
| No `citadel_*` tools at all | CLI: `citadel status`, then `citadel search` / `citadel doctor`. If CLI unhealthy: official docs (OpenAPI, MIP, DevHub) — never claim vault-backed authority without a search hit |

## Reference

- Hosted MCP URL: `https://citadel-archive-production.up.railway.app/mcp/`
- Hosted UI (create tokens): `https://citadel-archive-production.up.railway.app`
- Vault usage skill: `https://citadel-archive-production.up.railway.app/skills/vault`
