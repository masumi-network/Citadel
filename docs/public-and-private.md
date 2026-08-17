# What Is Public vs Private

Citadel splits **open integration surface** from **closed organization memory**.
Use this page when deciding what belongs in git, what stays on Railway, and what
agents may repeat in chat.

## Three layers

| Layer | Where | Visibility | Contains |
|---|---|---|---|
| **Application** | [Citadel](https://github.com/masumi-network/Citadel) | **Public** | Python app, MCP wrapper, agent skills, docs, UI code, tests |
| **Live vault** | Railway (+ Postgres / pgvector / Kuzu volume) | **Private** | Structured knowledge, embeddings, mesh, audit, access tokens (hashed) |
| **Backup mirror** | [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror) | **Private** | Text-first export of vault evidence (snapshots, manifests)—not live search |

```text
Agents / humans ──MCP/HTTPS + ctdl token──► Railway (live vault)
                                              │
                                              ▼ export (planned)
                                    Vault-Backup-Mirror (private git)
```

## Public (safe in the Citadel git repo)

- Source code, licenses, and architecture docs
- Agent skills at `/skills/connect`, `/skills/vault`, `/skills/boundary`
- MCP and HTTP **API shapes** (tool names, routes, role model)
- Example env var **names** in `.env.example` (never real values)
- Generic deployment guides (Railway, `uv`, MCP templates with placeholders)
- Domain language (`CONTEXT.md`, ADRs) with no customer data

## Private (never in the public repo)

- `ctdl_...` access tokens, `CITADEL_*_KEYS`, `GITHUB_TOKEN`, API keys, DB passwords
- `.env`, Railway variable values, connection strings
- Organization vault **content** (search results, ingested notes, mesh exports)
- Full source snapshots or PII from the mirror
- Personal machine paths in committed config (use `.` or ask the user for their clone path)

## Private but not in git (where secrets actually live)

| Secret / data | Store here |
|---|---|
| Service-account tokens | User shell env, `~/.codex/config.toml`, Cursor MCP env UI |
| Production DB | Railway Postgres binding |
| GitHub sync token | Railway env (`CITADEL_GITHUB_TOKEN`) |
| Mirror push token | Railway env (mirror repo `contents: write` only) |
| Local dev overrides | `.env` (gitignored) |

## What agents may access

| With | Agent can |
|---|---|
| Reader `ctdl_` token + MCP | Search, mesh, sources, read resources |
| Writer token | Above + ingest and feedback (with approval) |
| Admin token | Above + learning agent, improve, token APIs |
| No token | Only public `/healthz`, `/.well-known/citadel.json`, `/skills`, and hosted **skill markdown** (`/skills/*`) |

Agents must **not** assume the public GitHub repo contains team memory. All vault
queries go to Railway with a user-provided token.

## What humans should publish

| Action | OK? |
|---|---|
| Open-source Citadel | ✅ |
| Share `/skills/connect` URL | ✅ |
| Paste a `ctdl_` token in Discord/PR | ❌ |
| Commit `.mcp.json` with a real token | ❌ |
| Put vault exports in the Citadel git repo | ❌ |
| Put synthetic docs/examples in the Citadel git repo | ✅ (no real org data) |

## GitHub org repos

- **Citadel** (`masumi-network/Citadel`) — public; integration and app. Formerly `Citadel-Archive`.
- **Vault-Backup-Mirror** — private; backup evidence only.
- **Other masumi-network repos** — may be public or private; GitHub sync ingests only what the configured token can read. Synced **content** stays on Railway, not in the Citadel git repo.

## Agent skill URL

Boundary summary for agents (also served over HTTP):

```
https://citadel.utxo.ag/skills/boundary
```

## Related

- [Vault Backup Mirror](vault-backup-mirror.md)
- [MCP integration](mcp/README.md)
- [Agent access model](agent-access-model.md)
