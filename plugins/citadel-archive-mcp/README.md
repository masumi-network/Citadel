# Citadel Archive MCP Plugin

Codex-compatible plugin that connects coding agents to the [Citadel Organization Vault](https://github.com/masumi-network/Citadel) via MCP.

## What is public vs private

| Public | Private |
|---|---|
| This plugin and [Citadel](https://github.com/masumi-network/Citadel) | Hosted Organization Vault (team memory) |
| Skill markdown at `/skills/*` | [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror) |

Agents read vault content only through MCP with a user `ctdl_` token — never from git.

## Agent skill URLs

| Skill | URL |
|---|---|
| Connect MCP | `https://citadel.utxo.ag/skills/connect` |
| Use the vault | `https://citadel.utxo.ag/skills/vault` |
| Public vs private | `https://citadel.utxo.ag/skills/boundary` |

The agent will ask for a `ctdl_...` access token, configure MCP locally, verify the connection, and start searching the vault.

> **Hosted MCP is the supported path.** Connect any client to
> `https://citadel.utxo.ag/mcp` with
> `Authorization: Bearer ctdl_...` — no clone needed. This plugin is a legacy
> stdio wrapper kept for offline/dev use.

## Bundled skills

The agent skills now live in the repo's top-level [`skills/`](../../skills/)
directory (so they install via
`npx skills add masumi-network/citadel --skill '*'`). This plugin directory
exposes the same tree at `./skills` (symlink) because
`.codex-plugin/plugin.json` points `"skills": "./skills/"`.

| Directory | Purpose |
|---|---|
| `skills/citadel/` | Entry router: when to use Citadel, which satellite to load |
| `skills/citadel-mcp-connector/` | Setup: token, MCP config, verify |
| `skills/citadel-vault/` | Daily use: search, ingest, feedback |
| `skills/citadel-cli/` | `citadel` CLI: status, search, mcp add, update, onboard, doctor |
| `skills/citadel-debug/` | 502, corrupt sqlite, SEARCH_TIMEOUT, `/healthz` |
| `skills/citadel-data-boundary/` | What must stay private |
| `skills/citadel-onboard/` | One-command teammate setup |
| `skills/citadel-proactive-ingest/` | Git push / SessionEnd capture |

## Install as Codex plugin (legacy stdio)

Point Codex at this directory (`plugins/citadel-archive-mcp/`). The bundled `.mcp.json` uses `"../.."` as the repo root when the plugin lives inside a Citadel clone. For the no-clone path, use the hosted `/mcp` endpoint via the connect skill instead.

See [docs/mcp/README.md](../../docs/mcp/README.md) and [docs/public-and-private.md](../../docs/public-and-private.md).
