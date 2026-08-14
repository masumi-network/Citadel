---
name: citadel-onboard
description: One-command teammate onboarding for Citadel. Use when a teammate wants to set up Citadel in a repo — connect their seat token, install the autonomous git-push + SessionEnd capture hooks, add the Citadel MCP server, and (optionally) declare Approved Capture Roots. Triggers include "onboard me to citadel", "set up citadel", "citadel onboard", "connect citadel", "install citadel here", and https://citadel.utxo.ag/skills/onboard.
---

# Citadel Onboard

`citadel onboard` collapses the whole teammate rollout into **one idempotent
command**. Install the CLI and run it from your repo — the autosync hooks are
**bundled in the package** (`kb.hooks.*`), so no vendored skill directory is needed.

```bash
npx skills add masumi-network/citadel --skill citadel
pipx install citadel-archive    # the zero-dep `citadel` base client
# upgrade: pipx install --force citadel-archive --pip-args=--no-cache-dir
#          (plain `pipx upgrade` can land a stale cached build)
citadel onboard                 # or just run `citadel` on a fresh terminal — it auto-onboards
```

On a terminal you'll see the Pixel Bastion mark (magenta→cyan, bold wordmark);
`--json`/piped output is always plain. A bare `citadel` on a fresh interactive
terminal auto-enters this wizard (then shows the home screen); skip with
`--no-onboard` or `CITADEL_NO_ONBOARD=1`. It walks these steps, merging into
existing config (never clobbering) and safe to re-run:

| Step | What it does | Required? |
|---|---|---|
| **Token** | Prompts for your `ctdl_…` seat token, writes `export CITADEL_MCP_ACCESS_TOKEN=…` to your shell rc (once) | yes |
| **Git pre-push hook** | Installs `.git/hooks/pre-push` → commit snapshots to your **Node** | yes |
| **Session hooks** | Merges the Claude Code `SessionEnd` + `SessionStart` hooks into user-scope `.claude/settings.json` (SessionEnd → private Node trace; SessionStart → proactive policy reminder) | yes |
| **Agent policy** | Same three-rule proactive policy for every coding agent you use — see [Proactive agent policy](#proactive-agent-policy-after-onboard) | yes |
| **MCP server** | Adds the `citadel` HTTP MCP server to `.mcp.json` (in-session `citadel_search`, `citadel_ingest`, `citadel_share_session`) | optional, default on (`--no-mcp` to skip) |
| **Capture roots** | Optional `citadel setup` wizard → `~/.citadel/capture.json` | optional, prompted |

## Where to get the token

A Citadel admin creates one seat for the human. The CLI requires both the
human name and slug:

```bash
citadel seat create "Alice Smith" alice --role writer
```

The initial `ctdl_` token is shown once. The human can use it at `/login`, then
open Access to mint a distinct seat-bound token for each agent process. A
writer seat can mint reader or writer tokens. A reader seat can mint reader
tokens. Use reader tokens for search-only processes. Never share one token
between agent processes.

Use a standalone service-account token only for an explicit non-human service
with an intentional dataset and role. Do not use one for teammate onboarding.

## Security

- The seat token is written to **exactly one place** (your shell rc). The
  `.mcp.json` block references it as `${CITADEL_MCP_ACCESS_TOKEN}` — the secret
  is **never** stored in project config or echoed to the terminal (the summary
  masks it).
- Every dev-side hook is **fail-silent** — if Citadel is down or the token is
  unset, your `git push` and session close still succeed.
- **Fail-closed capture:** push snapshots require Approved Capture Roots in
  `~/.citadel/capture.json`. Onboard seeds the current repo as a root; without
  roots the pre-push hook captures nothing (never "all repos by default").
- SessionEnd only reads transcripts under known agent dirs
  (`~/.claude`, `~/.cursor`, `~/.codex`, `~/.agents`).
- Opt out: unset `CITADEL_MCP_ACCESS_TOKEN`, remove hooks, or delete capture
  roots.

## MCP — needed or not?

- **Autonomous background sync** (git push, session close) is plain HTTPS +
  token — it does **not** need MCP.
- **In-session vault search and explicit ingest** (`citadel_search`,
  `citadel_ingest`, `citadel_share_session`) are MCP tools. Search first with
  `citadel_search`. Call ingest or share only after explicit user approval.
  Enable MCP (default)
  if you want your IDE agent to ground answers in the vault and volunteer Shared
  Session Traces after user approval; skip with `--no-mcp` for capture-only.
- Manual ingest/search always works via the CLI (`citadel ingest`,
  `citadel search`, `citadel capture`) — both HTTP-backed against the Node by
  default (`--local` runs the in-process server stack instead).
- Onboard writes `.mcp.json` for Claude Code. To add the Citadel MCP server to
  another client, run `citadel mcp add <tool>` (auto-configures Cursor, Codex,
  Gemini, Windsurf; prints a snippet for the rest — `citadel mcp list` shows
  targets).

## Non-interactive / scripted

```bash
export CITADEL_MCP_ACCESS_TOKEN="ctdl_…"
citadel onboard --non-interactive --json \
  --repo /path/to/repo --shell-rc ~/.zshrc --no-capture
```

Flags: `--token`, `--node-url`, `--repo`, `--shell-rc`, `--no-mcp`,
`--no-capture`, `--non-interactive`. Prefer `CITADEL_MCP_ACCESS_TOKEN` so the
secret does not appear in `argv`. Exits non-zero if no token is available.

## Check status (the dashboard replacement)

Teammates have no web dashboard requirement — the CLI is the day-to-day window
into Citadel. Optionally open `/login` with your seat token for **My Node**.

```bash
citadel status      # one-shot: connection, identity (seat/role), local setup, recent activity, knowledge-mesh stats
citadel status --json --check-search  # verify seat_slug, default_dataset, and /search access
citadel doctor      # diagnose setup; `citadel doctor --fix` repairs common issues
```

`citadel status` checks the Node (`/healthz`), your token (`/api/session` →
seat + role + capabilities), and local setup (token in env, `.mcp.json`, git +
SessionEnd hooks, capture roots). Search smoke is **off by default** — pass
`--check-search` when you want it. It exits non-zero when not connected, so it
doubles as a doctor.

### Headless — for AI agents (Claude Code / Codex / Cursor) and CI

Every teammate command is fully headless: pass `--json` for a clean,
parseable object on stdout (no prompts, errors on stderr, meaningful exit codes).
Set the token in the environment (`CITADEL_MCP_ACCESS_TOKEN`) so it never appears
in `argv`/process lists.

| Command | JSON shape | Use |
|---|---|---|
| `citadel status --json --check-search` | `{healthy, identity{seat_slug,default_dataset,role,…}, checks[…], recent[…]}` | verify the seat, private dataset, and search access |
| `citadel onboard --non-interactive --json` | `{ok, repo, steps[{name,status}], token_masked}` | set a machine up (token from env) |
| `citadel setup --non-interactive --json --root PATH=tag` | the saved `capture.json` | declare Approved Capture Roots |
| `citadel capture --json` (`--dry-run` to preview) | `{ok, results[…]}` / `[…]` | push summaries to the Node |

`citadel status` additionally sees **local** hook/config state the server can't
 (the MCP `citadel_session` tool is the in-session whoami). For in-session reads,
prefer the `citadel_search` MCP tool. MCP does not ingest automatically. Use
`citadel_ingest` only after explicit approval. Capture hooks run only after the
user explicitly installs them through onboard and approves capture roots.

## Proactive agent policy (after onboard)

Onboard installs the **same three-rule policy** for every supported coding agent
(idempotent; safe to re-run):

| Agent / tool | Where onboard writes it |
|---|---|
| **Codex, Pi, Cline, Zed**, and other AGENTS.md-aware clients | `AGENTS.md` (repo root, always) |
| **Cursor** | `.cursor/rules/citadel-agent-policy.mdc` (`alwaysApply`) when Cursor is detected |
| **Windsurf** | `.windsurf/rules/citadel-agent-policy.md` (`always_on`) when detected |
| **Gemini CLI** | `GEMINI.md` when detected |
| **Claude Code** | SessionStart hook in user-scope `.claude/settings.json` (`kb.hooks.sync_start`) |

The policy itself — agents should use Citadel **without waiting to be asked**:

1. **Search at task start** — `citadel_search` before coding (Central + your Node
   + Shared Session Traces). Trace hits are `reference-only`; Central is
   org-authoritative.
2. **Share dead-end routes with consent** — after a costly wrong turn, ask the
   user, then `citadel_share_session` (Approved Capture Root required).
3. **Never auto-share** — SessionEnd hooks write private Node traces only;
   Railway cron syncs org sources, not per-agent search or share decisions.

Load the hosted proactive-ingest skill for hook/sync detail:
`https://citadel.utxo.ag/skills/proactive-ingest`

## Verify

Restart the shell (or `source ~/.zshrc`), then run `citadel status` (expect all
`●`), or in your agent ask: *"use citadel_search to find what we decided about
the vault."* A grounded answer means the token + MCP work.

**Claude Code (local CLI or cloud):** the token must be in the environment that
launched Claude — not only in your shell rc. Local: `source ~/.zshrc` before
`claude`. Cloud: add `CITADEL_MCP_ACCESS_TOKEN` in cloud env settings. Verify
with `claude mcp list` and `/mcp` (citadel tools, not zero tools).

See [`docs/onboarding/teammate-rollout.md`](../../docs/onboarding/teammate-rollout.md)
for the manual step-by-step and what auto-syncs.
