# Autonomous sync — Cursor and Codex

Citadel autosync is **IDE-agnostic** for git workflows. Background capture is
**fail-silent** — no extra dev steps after one-time setup.

## Universal: git push hook (required)

Install once per clone:

```bash
citadel onboard
```

`citadel onboard` is idempotent: it writes a self-contained `.git/hooks/pre-push`
that runs `"<python>" -m kb.hooks.sync_push`, plus the SessionEnd hook, project
`.mcp.json`, detected client MCP configs (Cursor, Codex, Claude, Gemini,
Windsurf), and capture roots. On macOS it publishes the token with
`launchctl setenv` until logout.

Requires `CITADEL_MCP_ACCESS_TOKEN` in your environment (same token as MCP).
Every push snapshots commit metadata to your private **Node** — always exits 0,
never blocks push.

## Claude Code (optional extra)

`citadel onboard` also writes a `.claude/settings.json` SessionEnd hook running
`"<python>" -m kb.hooks.sync_session` for session distill on top of git push
snapshots.

## Cursor

`citadel onboard` writes `~/.cursor/mcp.json` when Cursor is detected
(`${env:CITADEL_MCP_ACCESS_TOKEN}` in the header, token stays in the shell rc).
No Cursor-specific capture hook is required. The git pre-push hook is the
universal baseline.

Dock/Spotlight launches do not inherit `~/.zshrc`. After onboard: quit Cursor
(Cmd-Q), then from that shell run `cursor .`. Onboard's `launchctl setenv` on
macOS covers GUI launches until logout.

Optionally add a project rule reminding agents to call `citadel_ingest` for
durable decisions mid-session (see `skills/citadel-proactive-ingest/SKILL.md`).

## Codex / other agents

Same as Cursor: git push hook + MCP token. Agents read your **Node** and
**Central** via `citadel_search` and `citadel_session`.

## Linear tasks (server cron + MCP)

Railway cron (`CITADEL_RUN_MODE=linear-sync`) syncs the Linear workspace to
**Central** and **Seat-Scoped Mirrors** assignee issues into each dev's **Node**.

Ask your agent:

- *"What do I need to do?"* → `citadel_linear_my_issues` (your **Node** mirror)
- *"Search Linear for …"* → `citadel_linear_search` (**Central**)

Agents should **not** trigger `POST /api/linear-sync/run` unless the user
explicitly asks for an immediate refresh — cron is the default.

## Agent sync policy (for project rules)

| Situation | Action |
|---|---|
| Project/architecture question | `citadel_search` |
| Task list / assigned work | `citadel_linear_my_issues` |
| Durable fact mid-session | `citadel_ingest` (personal-by-default) |
| Git push or session close | **Nothing** — hooks handle it |
| Stale org sources | **Do not** trigger admin sync unless user asks |

## Server-side cron (operators)

Devs do not configure Railway cron. Operators set `CITADEL_RUN_MODE` on cron
services:

- `learning-agent` — daily GitHub org digest
- `linear-sync` — Linear workspace + **Seat-Scoped Mirrors**
- `pipeline` — full scheduled run

See `.env.example` and `README.md` for env vars and schedules.
