# Citadel Archive MCP Plugin

Codex-compatible plugin that connects coding agents to the [Citadel Organization Vault](https://github.com/masumi-network/Citadel-Archive) via MCP.

## What is public vs private

| Public | Private |
|---|---|
| This plugin and [Citadel-Archive](https://github.com/masumi-network/Citadel-Archive) | Railway vault (team memory) |
| Skill markdown at `/skills/*` | [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror) |

Agents read vault content only through MCP with a user `ctdl_` token — never from git.

## Agent skill URLs

| Skill | URL |
|---|---|
| Connect MCP | `https://citadel-archive-production.up.railway.app/skills/connect` |
| Use the vault | `https://citadel-archive-production.up.railway.app/skills/vault` |
| Public vs private | `https://citadel-archive-production.up.railway.app/skills/boundary` |

The agent will ask for a `ctdl_...` access token, configure MCP locally, verify the connection, and start searching the vault.

> **Hosted MCP is the supported path.** Connect any client to
> `https://citadel-archive-production.up.railway.app/mcp` with
> `Authorization: Bearer ctdl_...` — no clone needed. This plugin is a legacy
> stdio wrapper kept for offline/dev use.

## Bundled skills

The agent skills now live in the repo's top-level [`skills/`](../../skills/)
directory (so they install via
`npx skills add masumi-network/citadel --skill '*'`):

| Directory | Purpose |
|---|---|
| `skills/citadel/` | Primary skill (search, connect, safety overview) |
| `skills/citadel-mcp-connector/` | Setup: token, MCP config, verify |
| `skills/citadel-vault/` | Daily use: search, ingest, safety |
| `skills/citadel-data-boundary/` | What must stay private |

## Install as Codex plugin (legacy stdio)

Point Codex at this directory (`plugins/citadel-archive-mcp/`). The bundled `.mcp.json` uses `"../.."` as the repo root when the plugin lives inside a Citadel-Archive clone. For the no-clone path, use the hosted `/mcp` endpoint via the connect skill instead.

See [docs/mcp/README.md](../../docs/mcp/README.md) and [docs/public-and-private.md](../../docs/public-and-private.md).
