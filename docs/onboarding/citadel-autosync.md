# Citadel Auto-Sync — dev onboarding

Citadel captures engineering context **autonomously** — no per-session or
per-push ceremony after a one-time token + hook install. Personal capture lands
in your private **Node** (`seat:{slug}`); org-wide sources (GitHub, Linear)
sync to **Central** on Railway cron.

> **Quick start:** [`teammate-rollout.md`](teammate-rollout.md) (5-minute one-pager)

## The one step

Mint a **seat-writer** token from the connect wizard and export it once:

```bash
# https://citadel.utxo.ag/skills/connect
export CITADEL_MCP_ACCESS_TOKEN='ctdl_...'
```

Add it to your `~/.zshrc` / `~/.bashrc` so it persists. This is the same token
that powers `citadel_search`, MCP tools, and all background hooks.

## Install hooks (one-liner)

From the repo root:

```bash
citadel onboard
```

This is idempotent: it wires the token into your shell rc, installs the git
pre-push hook (`python -m kb.hooks.sync_push`), the Claude Code SessionEnd hook
(`python -m kb.hooks.sync_session`), project `.mcp.json`, detected coding
clients (Claude Code, Cursor, Codex, Gemini, Windsurf), and capture roots. On
macOS it also runs `launchctl setenv` so Dock-launched Cursor can see the
token until logout. Requires the `citadel` CLI (`pipx install citadel-archive`).

## What auto-syncs, and when

| Layer | Trigger | Captures | Destination |
|---|---|---|---|
| **Git push** (all IDEs) | every `git push` | commit hash, message, author, branch, changed paths | your **Node** |
| **SessionEnd** (Claude Code) | session close | 1–2 line recap, decisions, files changed | your **Node** |
| **GitHub cron** (Railway) | daily schedule | org repo digest | **Central** |
| **Linear cron** (Railway) | scheduled | workspace issues → **Central**; your assigned issues **Seat-Scoped Mirror** → your **Node** | **Central** + **Node** |

Dev-side hooks (git push, SessionEnd) are **fail-silent** — if Citadel is down
or the token is unset, your push and session close still succeed instantly.
Server-side cron runs independently; you never trigger it manually.

### Git push (universal baseline)

- **When:** on every `git push` (pre-push hook).
- **What:** commit metadata — not raw diffs.
- **Tags:** `git-push`, branch name, repo name.
- **Install:** `citadel onboard` (writes a `.git/hooks/pre-push` that runs
  `python -m kb.hooks.sync_push`).

### Session close (Claude Code — optional extra)

- **When:** once, on every Claude Code `SessionEnd`.
- **What:** a short distilled note — **not** the raw transcript.
- **Tags:** `dev-session`, your git branch, and the repo name.
- **Setup:** `citadel onboard` (writes a `.claude/settings.json` SessionEnd hook
  that runs `python -m kb.hooks.sync_session`).

### Server-side cron (operators — not devs)

Railway cron services keep org memory fresh. Key run modes:

| `CITADEL_RUN_MODE` | Syncs |
|---|---|
| `learning-agent` | GitHub org digest → **Central** |
| `linear-sync` | Linear workspace → **Central**; assignee issues **Seat-Scoped Mirror** → each **Node** |
| `pipeline` | GitHub + skills refresh + self-improve + backup mirror |

Env vars: see `.env.example` (`CITADEL_LINEAR_API_KEY`, `CITADEL_GITHUB_ORG`,
`CITADEL_PIPELINE_*`). Agents read synced content via MCP — they do not run
cron jobs.

**Linear cron (Railway):** create a third cron service from this repo with
`CITADEL_RUN_MODE=linear-sync` and the same Postgres/volume bindings as the web
service. Set `CITADEL_LINEAR_API_KEY` to a Linear personal API key with **Read**
scope only (no Write/Admin). Optional: restrict the key to the teams Citadel
should ingest. Suggested schedule — every 6 hours:

```cron
0 */6 * * *
```

Or daily at 09:00 UTC:

```cron
0 9 * * *
```

Also set `CITADEL_LINEAR_API_KEY` on the **web** service so manual
`POST /api/linear-sync/run` works. Verify with `GET /api/linear-sync` (reader
token): check `enabled`, `last_synced_at`, and `issue_count`.

### Linear tasks (after cron sync)

Ask your agent: *"What do I need to do?"* — MCP tool `citadel_linear_my_issues`
reads your **Seat-Scoped Mirror** from the latest Linear sync. Org-wide Linear
search: `citadel_linear_search`.

## Personal vs shared

| | Goes where | Who reads it |
|---|---|---|
| **Auto-sync (default)** | your private **Node** `seat:{slug}` | you (+ org readers after **Promotion**) |
| **Promotion Agent** (6h cron + on demand) | curated copy → **Central** | the whole org |
| **New Org Project approval** | your approve → **Central** copy | the whole org |

Auto-sync sends no `dataset` field, so your seat-writer token routes it to your
own **Node**. Seat tags do **not** promote to **Central** (ADR-0007). Approve
**Promotion Agent** proposals in the dashboard, MCP, or `citadel promotion` CLI
when you want external-project notes shared org-wide.

## Privacy

- Token read from `CITADEL_MCP_ACCESS_TOKEN` only — never printed, echoed, or
  committed.
- Distilled note / commit metadata, never raw transcripts or diffs.
- HTTPS only.

## Opt-out

- **Quick:** unset `CITADEL_MCP_ACCESS_TOKEN` (hooks become no-ops).
- **Persistent:** add a `SessionEnd` override in `.claude/settings.local.json`
  (gitignored, dev-local).

## More

- Skill: `https://citadel.utxo.ag/skills/proactive-ingest`
- Connect wizard: `https://citadel.utxo.ag/skills/connect`
- Per-IDE notes: [`citadel-autosync-ides.md`](citadel-autosync-ides.md)
- Repo setup details: `skills/citadel-proactive-ingest/README.md`
