---
name: citadel-cli
description: How to use the citadel CLI for status, search, document drilldown, skill management, MCP wiring, update, onboard, and doctor. Use when the user runs citadel commands, asks how to upgrade the CLI, or when MCP tools are missing and the headless CLI is the fallback. Triggers include "citadel status", "citadel search", "citadel document", "citadel skills", "citadel update", "citadel mcp add", "citadel doctor", "citadel onboard", and https://citadel.utxo.ag/skills/cli.
---

# Citadel CLI

**Skill URL:** `https://citadel.utxo.ag/skills/cli`
**MCP setup:** `https://citadel.utxo.ag/skills/connect`
**Vault usage:** `https://citadel.utxo.ag/skills/vault`

Install: `pipx install citadel-archive`. Need `>= 0.5.2`. Upgrade: `citadel update`.

On a TTY, `citadel` (home) and `citadel status` ask **Update available, should I
update? [y/N]** when PyPI is newer (cached 24h). **Y** runs pipx upgrade, rewrites
write-tier MCP URLs to `https://citadel.utxo.ag/mcp/` (skips a custom
`node_url` in `~/.citadel/capture.json`), then refreshes skills with
`npx skills add masumi-network/citadel --skill '*'`. Non-TTY and CI never prompt.

```bash
citadel --version
citadel status --json --check-search
citadel search "your question" --json --top-k 5
citadel search "your question" --source linear-context --json
citadel document "<id-or-document_id>" --json
citadel skills list --json
citadel skills show vault --json
citadel ingest "durable note" --tag decision
citadel mcp add cursor          # also: claude, codex, windsurf; `citadel mcp list`
citadel doctor --fix
citadel onboard --non-interactive --json
citadel update
```

Every read command exits `1` on 401 / missing token, `0` on success. Prefer
`CITADEL_MCP_ACCESS_TOKEN` in the environment so the token is not on `argv`.

Use `--source` when a query must stay within one sync lane:
`linear-issue`, `linear-context`, `linear-workspace`, or `repo-content`.

Default Node URL is `https://citadel.utxo.ag`. Override with `--node-url` or
`~/.citadel/capture.json`. After 0.4.0, `citadel update` rewrites stale
`*.up.railway.app` MCP URLs unless that file holds a custom node.

Bare `citadel onboard` is interactive. Agents and CI use `--non-interactive --json`.
