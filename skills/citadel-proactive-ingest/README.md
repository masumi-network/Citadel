# citadel-proactive-ingest — org repo setup

Add autonomous personal-KB sync to a project repo: **SessionEnd** distill (Claude
Code) and **git push** commit snapshots (all IDEs) auto-sync to **their**
private Citadel node (`seat:{slug}`). One-time token setup per dev.

## What ships in this skill

| Path | Purpose |
|---|---|
| `SKILL.md` | Agent behavior + auto-sync docs |
| `README.md` | This file |

The auto-sync hooks ship in the installed `citadel-archive` package, not in this
skill directory:

| Module | Purpose |
|---|---|
| `kb.hooks.sync_session` | SessionEnd distill + POST (stdlib only) |
| `kb.hooks.sync_push` | Git pre-push commit snapshot + POST (stdlib only) |
| `citadel onboard` | Idempotent install: token → shell rc, git pre-push hook, SessionEnd hook, MCP server, capture roots |

## 1. Install the package and onboard

Install the `citadel-archive` package, then run `citadel onboard` from the repo
root. It is idempotent and self-contained — it writes the token export to your
shell rc, a `.git/hooks/pre-push` that runs `python -m kb.hooks.sync_push`, a
`.claude/settings.json` SessionEnd hook that runs `python -m kb.hooks.sync_session`,
the MCP server (`.mcp.json`), and the capture roots. No vendored skill directory
is needed.

```bash
pipx install citadel-archive   # lightweight `citadel` CLI (zero-dep client; extra: [server])
citadel onboard                # idempotent — safe to re-run
```

## 2. The SessionEnd hook `citadel onboard` writes

`citadel onboard` writes this block into the repo's `.claude/settings.json`
(creating the file if absent):

```jsonc
{
  // SessionEnd fires once when a Claude Code session closes. The command is
  // NON-BLOCKING (the script always exits 0) and bounded by `timeout` seconds,
  // so a Citadel outage can never delay a dev's session close.
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            // Bundled in the installed package; "<python>" is the resolved interpreter.
            "command": "\"<python>\" -m kb.hooks.sync_session",
            "timeout": 20,
            // Only this var is exposed to the hook process — the seat token.
            "allowedEnvVars": ["CITADEL_MCP_ACCESS_TOKEN"]
          }
        ]
      }
    ]
  },
  // Whitelist the same var for the hook transport. The token is read from the
  // environment ONLY; it is never written into this committed file.
  "httpHookAllowedEnvVars": ["CITADEL_MCP_ACCESS_TOKEN"]
}
```

`.claude/settings.json` is committed and shared by the whole team, but it
contains **no secret** — only the var *name*. The token value lives in each
dev's shell environment.

## 3. One-time token export (per dev)

Each dev runs the connect wizard once to mint a **seat-writer** token and
exports it (the same token that powers `citadel_search` and MCP — most devs
already have it):

```bash
# https://citadel.utxo.ag/skills/connect
export CITADEL_MCP_ACCESS_TOKEN='ctdl_...'
```

A seat-writer token has `default_dataset=seat:{slug}`, so the hook's POST omits
the `dataset` field and the write lands in that dev's private node. Add the
export to `~/.zshrc` / `~/.bashrc` to make it durable across shells.

Optional: set `CITADEL_BASE_URL` to override the hosted base (defaults to the
production URL). It must be `https://`.

## 4. Git push hook (universal — Cursor, Codex, Claude)

`citadel onboard` (step 1) already installs this once per clone so every **push**
snapshots commit metadata to your private **Node**. To install it by hand, write
a `.git/hooks/pre-push` that runs the bundled module:

```bash
printf '#!/bin/sh\nexec "%s" -m kb.hooks.sync_push\n' "$(command -v python3)" > .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

The hook runs `kb.hooks.sync_push` on pre-push. It captures commit hash,
message, author, branch, and changed file paths (not raw diffs). Same token,
same personal-by-default routing — **no `dataset` field**. Always exits 0; never
blocks push.

## 5. Server-side cron (operators — not devs)

Railway cron services keep **Central** and **Seat-Scoped Mirrors** fresh. Devs
do not configure or trigger these.

| Mode | Purpose |
|---|---|
| `CITADEL_RUN_MODE=learning-agent` | Daily GitHub org digest → Central |
| `CITADEL_RUN_MODE=linear-sync` | Linear workspace → Central; assignee issues mirrored to each **Node** |
| `CITADEL_RUN_MODE=pipeline` | Full scheduled run (GitHub + skills + self-improve + backup mirror) |

See `.env.example` for `CITADEL_LINEAR_*`, `CITADEL_PIPELINE_*`, and
`CITADEL_GITHUB_SYNC_*` vars. Agents read synced content via MCP — they should
not trigger admin sync unless the user explicitly asks.

## 6. Opt-out

Either is enough:

- **Per dev, per machine:** unset `CITADEL_MCP_ACCESS_TOKEN`. With no token the
  hook is a clean no-op (no POST, exit 0).
- **Per dev, persistent:** add a `SessionEnd` override to
  `.claude/settings.local.json` (gitignored, dev-local) that drops or no-ops the
  hook. `settings.local.json` overrides the shared `settings.json`.

## Privacy posture

- **Personal-by-default.** Auto-sync sends **no `dataset` field** → the
  seat-writer token routes it to the dev's `seat:{slug}` node. Only that dev
  (and Central) can read it.
- **Distilled, not raw.** The hook sends a short curated note (recap, key
  decisions, files changed) — never the raw transcript.
- **Token never committed.** Read from the environment only; never printed,
  echoed, or written to `.claude/settings.json` or any tracked file.
- **HTTPS only.** The POST refuses any non-`https://` base URL.
- **Non-blocking.** The script catches everything and exits 0; it cannot block
  or fail a session close.

## Share with Central (Promotion Agent)

Auto-sync is always personal (**Node** only). **Central** copies are governed:
the **Promotion Agent** auto-promotes known masumi-org work; **New Org Project**
notes wait for your **Promotion Approval** (dashboard, MCP with confirm, or
`citadel promotion` CLI). Seat ingest tags do **not** dual-write to **Central**
— see ADR-0007 and `SKILL.md`.
