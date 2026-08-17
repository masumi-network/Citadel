# Teammate Rollout — onboard a dev in 5 minutes

This is the canonical one-pager for M6.5. One admin step, then each teammate
runs **four setup steps** and is done — Claude Code sessions and git pushes
auto-save to their private Citadel node, and `citadel_search` works in any MCP
client. Dashboard login is **optional** after setup (paste the same seat token
on `/login` to open your Seat home).

> **Deep-dive:** [`citadel-autosync.md`](citadel-autosync.md) (what syncs),
> [`citadel-autosync-ides.md`](citadel-autosync-ides.md) (per-IDE notes).
> This page is the short version.
> **Portal:** [`../plans/seat-scoped-portal.md`](../plans/seat-scoped-portal.md)
> (seat-scoped dashboard — token login → My Node).

---

## Admin (once per teammate)

Mint a **seat-writer** token for the teammate. Seat-writer tokens route every
capture to that teammate's private node (`seat:{slug}`) — they cannot read or
write anyone else's node, and they get no admin powers.

```bash
# Admin (CITADEL_ADMIN_KEY set): mint the seat + its seat-scoped writer token
citadel seat create "Jane Dev" jane    # prints a ctdl_… token scoped to seat:jane

# Lost the token later? Re-mint a fresh one for the existing seat:
citadel seat token jane
```

(The web Access page —
`https://citadel.utxo.ag/skills/connect` — does the
same thing if you'd rather click.)

> ⚠️ **Always bind the token to a seat.** On the Access page choose the seat under
> *Assign to seat*; do **not** leave it on *No seat — service account*. A seat-less
> token has no default dataset, so the teammate authenticates fine but every
> search fails with `DatasetNotFoundError` and writes route to the shared org
> dataset. Confirm before handing it over: the token should show `seat_slug` and
> `default_dataset: seat:<slug>` in `citadel status --json`.

Hand the token to the teammate over a private channel. It is a secret — treat
it like a password.

---

## Teammate — fast path (one command)

Install the CLI, then run onboard from the repo:

```bash
pipx install citadel-archive     # the `citadel` command (lightweight, zero-dep client)
# upgrade: pipx install --force citadel-archive --pip-args=--no-cache-dir
citadel onboard
```

```
  ■ · ■ · ■ · ■
  ■■■■■■■         CITADEL
  ■■·■·■■         the organization vault     ← citadel onboard / status
  ■■·■·■■
  ■■■■■■■
  ■■···■■
  ■■···■■
```

Pixel Bastion mark (magenta→cyan). See [`brand.md`](../../brand.md).

This runs all of the steps below for you: pastes your token into your shell rc
(once, masked), installs the bundled git-push + SessionEnd/SessionStart hooks
(`kb.hooks.*`, no vendoring), writes the proactive agent policy for your coding
tools (`AGENTS.md` always; Cursor / Windsurf / Gemini native rules when detected;
Claude Code via SessionStart), adds the Citadel MCP server to project
`.mcp.json` **and** to detected clients (Claude Code, Cursor, Codex, Gemini,
Windsurf; same path as `citadel mcp add`), and on macOS publishes the token to
the GUI session with `launchctl setenv` (until logout). Then it offers Approved
Capture Roots. Idempotent; safe to re-run. `--no-mcp` for capture-only;
`--no-tools` to skip client wiring; `--non-interactive --json --token …` for
agents/CI (that path now auto-wires write-tier clients). See the
[`citadel-onboard`](../../skills/citadel-onboard/SKILL.md) skill.

The manual steps below are what `onboard` does under the hood (and the path if
you'd rather do it by hand).

### What onboard installs (rules vs skill vs MCP)

| Layer | Installed by onboard | Purpose |
|---|---|---|
| **Rules / SessionStart** | `AGENTS.md` + Cursor/Windsurf rules; Claude `SessionStart` hook | Always-on: search before coding; traces are reference-only; share only with approval; if no `citadel_*` tools, use CLI (`citadel search` / `status` / `doctor`) |
| **Skill** | Not installed by onboard — load via `npx skills add` or hosted `/skills/*` | How-to: connect, vault usage, onboard workflows |
| **MCP** | Project `.mcp.json` with `${CITADEL_MCP_ACCESS_TOKEN}`, plus `~/.cursor/mcp.json` / Codex / Claude user-scope / Gemini / Windsurf when those tools are detected | Live tools. They only work when the token is in the **process env** that launched the client. On macOS, onboard also runs `launchctl setenv` so Dock apps can see the token until logout. |

**Claude MCP root cause (teammates hit this often):** the token must be in
Claude's process environment. Local: `source ~/.zshrc` then launch `claude`.
Cloud: set `CITADEL_MCP_ACCESS_TOKEN` in Claude cloud environment settings.
PRs/docs do **not** inject secrets. Verify with `claude mcp list`, `/mcp`, and
`citadel doctor`.

**Cursor MCP root cause:** Cursor.app from Dock/Spotlight does not read
`~/.zshrc`. After onboard, quit Cursor fully (Cmd-Q) and relaunch from a shell
that has the token: `cursor .`. `launchctl setenv` (run by onboard on macOS)
covers GUI launches until logout. A reconnect in `/mcp` will not pick up a
token that was missing at launch.

## Teammate — manual (4 steps)

> All four use the **same** `CITADEL_MCP_ACCESS_TOKEN`. Set it once; everything
> else points at it.

### 1 · Save your token

```bash
echo "export CITADEL_MCP_ACCESS_TOKEN='ctdl_…'" >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
```

### 2 · Add the MCP server

`citadel onboard` writes this. So does `citadel mcp add cursor` (or `claude`,
`codex`, `gemini`, `windsurf`). Citadel is a hosted HTTP MCP server. The token
is read from the env var set in step 1.

```json
{
  "mcpServers": {
    "citadel": {
      "type": "http",
      "url": "https://citadel.utxo.ag/mcp/",
      "headers": { "Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}" }
    }
  }
}
```

This gives any MCP client (Claude Code, Codex, Cursor) the `citadel_search`,
`citadel_session`, and `citadel_ingest` tools.

### 3 · Claude Code session hook (auto-sync on session close)

Copy the project template into the repo's Claude settings. This runs the
distill-and-save once per `SessionEnd`:

```bash
# From the repo root
mkdir -p .claude
# `citadel onboard` writes this for you; to do it by hand, add a SessionEnd
# hook to .claude/settings.json that runs: "<python>" -m kb.hooks.sync_session
```

The hook wires a `SessionEnd` hook → `kb.hooks.sync_session` with a 20s timeout.

### 4 · Git push hook (auto-sync on every push — all IDEs)

```bash
citadel onboard
```

Works for Cursor, Codex, and Claude — git hooks are IDE-agnostic. Installs a
self-contained `.git/hooks/pre-push` that runs `"<python>" -m kb.hooks.sync_push`,
and wires the SessionEnd hook for Claude Code.

### 5 · Approved Capture Roots (required for push capture)

Push capture is **fail-closed**. Without Approved Capture Roots the pre-push
hook captures nothing. `citadel onboard` seeds the current repo; to add more
folders:

```bash
# Interactive wizard — pick folders + Capture Root Tags, writes ~/.citadel/capture.json
citadel setup

# Or non-interactive:
citadel setup --root "$HOME/work/our-repo=org-work" --root "$HOME/notes=personal"
```

Each root is tagged: `personal` (never promoted to Central) or `org-work`
(eligible for Promotion Agent review). The seat token stays in your environment
— it is never written to `capture.json`.

The push hook **only** captures pushes from inside an Approved Capture Root;
pushes from other repos are skipped with a warning. A missing, corrupt, or empty
config fails closed (captures nothing). You can also capture on demand:

```bash
citadel capture --dry-run   # preview the per-root summaries (no network)
citadel capture             # POST summaries to your Node
```

`citadel capture` sends a compact summary (git metadata + README blurb), never a
raw file dump. The Node URL must be **HTTPS** (it refuses `http://` before the
token is sent), and the command exits **non-zero** on any failure — safe to use
in scripts/CI.

---

## Verify (30 seconds)

```bash
citadel status        # connection + identity + local setup + knowledge mesh (expect all ●); --json for agents
# optional: citadel status --check-search   # smoke /search (off by default; never gates health)
# or:  citadel doctor # diagnose setup issues; --fix repairs the safe ones

# Token works + MCP search returns results:
#   in Claude Code, ask: "use citadel_search to find what we decided about the vault"
# Linear tasks (after server cron sync):
#   ask: "what do I need to do?" → citadel_linear_my_issues
```

`citadel status` is the teammate's dashboard replacement — it shows the Node
health, your seat + role, and whether your hooks/MCP/capture roots are wired up.
It does **not** smoke-test `/search` unless you pass `--check-search`.
Agents run `citadel status --json` to check connectivity programmatically.

### See what Citadel is doing

Capture is silent by default, so once you're onboarded, `citadel activity` shows
what your Node is actually doing:

```bash
citadel activity              # recent captures, syncs, promotions, searches on your Node
citadel activity --watch      # live-tail as it happens
citadel activity --global     # team presence board — every seat's contribution count
citadel activity --local      # offline capture receipts (works with no server)
```

Every `git push` / session close now leaves a one-line receipt in
`~/.citadel/activity.log` (set `CITADEL_HOOK_VERBOSE=1` to also echo it to your
terminal). `--global` shows **Seat Presence only** — counts and slugs, never
another seat's Node content.

You should get a short note back sourced from your node / Central. If you get an
auth error, your token isn't in the environment of the client that's running
(restart the client so it picks up the new env var).

---

## Optional: open the portal with your seat token

MCP + hooks remain the primary write path. To **see** your Node in the browser:

1. Open the Node URL `/login` (same host as `CITADEL_NODE_URL`).
2. Paste your `ctdl_…` seat token (same secret as `CITADEL_MCP_ACCESS_TOKEN`).
3. You land on **My Node** (Seat home): doc counts, empty-state checklist, links
   to Search / Graph / Activity. Session chrome shows your seat slug.

Central stays searchable even when your Node is empty. Token rotation stays
admin-only for now (`citadel seat token <slug>` or Access → revoke).

---

## What auto-syncs, and the guarantees

| Trigger | Captures | Destination |
|---|---|---|
| Claude Code `SessionEnd` | 1–2 line recap, decisions, files changed | your **Node** |
| `git push` | commit hash, **subject**, author, branch, changed paths | your **Node** |
| Railway GitHub cron | org repo digest | **Central** |
| Railway Linear cron | workspace + your assigned issues (**Seat-Scoped Mirror**) | **Central** + your **Node** |

- **Always fail-silent.** Every hook ends in `exit 0` / `|| true`. If Citadel is
  down or the token is missing, your session close and your push still succeed.
- **Always personal.** Auto-sync sends no `dataset`, so it lands in your private
  **Node**. **Central** copies come from the **Promotion Agent** (known org work)
  or your **Promotion Approval** when a **New Org Project** is proposed — not
  from ingest tags.
- **Allowlist-required (fail-closed).** Without Approved Capture Roots the push
  hook captures nothing; `citadel onboard` / `citadel setup` must approve folders
  first.
- **No raw content.** Hooks capture metadata + a distilled recap, never raw
  transcripts or diffs.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| MCP connected, **zero tools** in Claude | Token not in Claude's process env: local → `source ~/.zshrc && claude`; cloud → add `CITADEL_MCP_ACCESS_TOKEN` in cloud env settings. Run `claude mcp list` and `citadel doctor`. Check `.mcp.json` for legacy stdio (`command`/`kb.mcp_server`) — re-run `citadel doctor --fix`. |
| `citadel_search` auth error | Restart the MCP client so it re-reads `CITADEL_MCP_ACCESS_TOKEN`. |
| Nothing lands in my node after a session | Confirm `.claude/settings.json` exists and the token env var is exported in the shell that *launched* Claude Code (not a later shell). |
| Push still works but no node entry | Re-run `citadel onboard` from the repo root to reinstall the `.git/hooks/pre-push` hook. |
| Token leaked in chat/logs | Ask the admin to **revoke + re-mint** (Access page → revoke). Old captures stay. |

---

## Rollout checklist (for the admin driving the team)

- [ ] Minted a seat-writer token for each teammate
- [ ] Each teammate confirmed `citadel_search` returns results
- [ ] Each teammate has `.claude/settings.json` + `.git/hooks/pre-push` in the org repos they work in
- [ ] Reminded everyone: leaked tokens get revoked, not recovered
- [ ] (Post-rollout) rotate `CITADEL_ADMIN_KEY` if it was shared during setup
