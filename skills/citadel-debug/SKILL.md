---
name: citadel-debug
description: Diagnose Citadel Node and CLI failures. Use when search hangs, MCP returns 502, sqlite reports file is not a database, status shows SEARCH_TIMEOUT, or someone curls /api/health. Triggers include "citadel 502", "cognee.db corrupt", "SEARCH_TIMEOUT", "citadel health", and https://citadel.utxo.ag/skills/debug.
---

# Citadel debug

**Skill URL:** `https://citadel.utxo.ag/skills/debug`
**CLI:** `https://citadel.utxo.ag/skills/cli`

Start with `citadel status --json --check-search`. Parse `checks[]`. Do not print
tokens.

## Symptom → cause

| What you see | What it is | What to do |
|---|---|---|
| HTTP **502** from `/search` or `/mcp/` | Proxy or Node timeout, often a long cognify | Retry `citadel status`. Do not treat as a bad token if `/healthz` is 200. |
| sqlite `file is not a database` | Corrupt `cognee.db` on the Lite volume | Lite boot quarantines it (`cognee.db.corrupt-<stamp>`) and recreates. Do not wipe `/data`. |
| `SEARCH_TIMEOUT` / HTTP 504 | Search budget exceeded (`CITADEL_SEARCH_TIMEOUT_SECONDS`) | Narrow the query. Status search smoke is opt-in (`--check-search`). |
| Curl **`/api/health`** 404 | That path does not exist | Liveness is **`GET /healthz`**. Readiness is **`GET /readyz`** (auth). |
| MCP connected, **zero tools** | Token missing from the client process env | `citadel doctor`. Claude: token in the shell that launched `claude`. Cursor: quit and `cursor .` from a token shell. |
| 401 while `node.ok` | Token problem, not install | Fix/rotate the token. Reinstall will not help. |

## Health paths (do not invent others)

```bash
curl -fsS https://citadel.utxo.ag/healthz
citadel status --json --check-search
citadel doctor
```

`/healthz` is public liveness. `/readyz` is authenticated readiness. There is
no `/api/health`.
