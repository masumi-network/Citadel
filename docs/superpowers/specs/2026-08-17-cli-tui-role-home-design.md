# CLI TUI: role-aware home, writer self-mint, mcp picker

- Date: 2026-08-17
- Status: Approved
- Branch: `release/0.5.1` (PR #301)
- Touches: `kb/cli.py`, `kb/access_client.py`, `kb/status.py`, tests. Reuses `kb/prompt.py` and `kb/banner.py`.

Claims about current code are tagged. Untagged sentences are design rules for this change.

## Progress

Checked 2026-08-17 against `release/0.5.1` HEAD `644393f` (PR #301).

- Design: approved. This file is on disk. `git status` shows it untracked.
- Implementation: in flight in this worktree, not in HEAD. [VERIFIED] `git status` shows `M kb/cli.py`, `M kb/access_client.py`, `M kb/status.py`. Not in PR #301 HEAD `644393f`.
- Local installed `citadel` still showed writer `seat` on home (terminal this session, before those uncommitted edits). Do not claim landed.
- Related work already on PR #301 (not this spec): stamp `0.5.1`, Docker wheel pin, sqlite quarantine, TTY Y/N updater, skills pack. [VERIFIED] `git log origin/main..HEAD`.
- Not done: merge, tag `v0.5.1`, PyPI 0.5.1. [VERIFIED] PyPI JSON `0.4.0`. [VERIFIED] `git tag -l 'v0.5*'` is `v0.5.0` only.

## Goal

Bare `citadel` on a TTY lists commands the caller can use. Writers do not see seat admin. Writers mint a token for their own seat through the existing self-service API. Bare `citadel mcp` opens the onboard coding-tools checkbox. `mcp install` help matches `mcp add`. Stdlib ANSI only. No Rich. No Textual.

## Decisions

| Decision | Chosen | Rejected |
|---|---|---|
| Home filter | One `/api/session` fetch (`check_auth`, same as status auth) | Full `gather_status`; one static menu |
| Writer / reader home | Omit the `seat` row. Keep `token` as self-mint | Hide `token`; keep both rows |
| Admin home | `CITADEL_ADMIN_KEY`, or session `access:manage` / `capabilities.admin` | Role string `admin` with no scope |
| Fetch fail | Hide `seat`. Keep self-mint `token` | Show admin rows on a blind fail |
| Writer mint | `POST /api/access/self/tokens` with the seat token | Admin key; web-only mint |
| Admin mint for a slug | `citadel token create --seat <slug>` on the existing admin path | Self-mint when `--seat` is set |
| Both credentials | `--seat` stays admin. Admin key plus no `--seat` keeps picker / standalone. Self-mint only when a seat token is present and the admin key is absent | Always self-mint when a seat token exists |
| Writer types `seat create` | Exit before the admin API. Say admin only. Hint `citadel token create` | Let the client raise missing admin key |
| Bare `citadel mcp` | Onboard checkbox (`checkbox_select` + `tool_detect.apply`). Esc/q: no writes, exit 0 | Required subcommand listing |
| `mcp install` help | Same text as `mcp add` (alias dest lookup) | Blank help on the alias name |

## Home

On TTY, bare `citadel` still prints the banner and the setup line. Then it chooses the Connect rows.

1. If `CITADEL_ADMIN_KEY` is set and non-empty, show `seat`. Skip the identity fetch.
2. Else call `check_auth` (GET `/api/session`) with the seat token. Timeout 3s.
3. If that call succeeds and `capabilities.admin` is true, or `access:manage` is in `actor.scopes`, show `seat`.
4. If the call fails, or the caller is reader/writer without that scope, omit `seat`. Keep `token`.

Do not show `seat create` after a failed fetch when the admin key is absent. An admin key already present still shows `seat` (step 1).

Writer `token` copy: mint a token for your seat. Admin `token` copy stays the admin create/revoke line. Connect title is `Connect` for members and `Connect & admin` for admins.

Identity data comes from `check_auth` (**VERIFIED**, `kb/status.py` `check_auth` GET `/api/session`). This change also stores `actor.scopes` on that identity so home can see `access:manage`.

## token create

When all of these hold:

- no `--seat`
- no `--dataset`
- no `--kind`
- no `--expires-at`
- a seat token is present (`CITADEL_MCP_ACCESS_TOKEN` or the writer-keys fallback)
- `CITADEL_ADMIN_KEY` is absent

then:

1. GET `/api/session` for the seat role.
2. POST `/api/access/self/tokens` (**VERIFIED**, `kb/server.py` `create_self_seat_token`).
3. Send `Authorization: Bearer <seat token>`. Do not send `CITADEL_ADMIN_KEY`.
4. Body: `token_name` (the CLI name argument) and `role` capped to the seat role. Never `admin`. A reader seat cannot mint writer. A writer seat may mint reader.

`--seat <slug>` keeps `POST /api/access/seats/<slug>/tokens` with the admin key (**VERIFIED**, `kb/access_client.py` `issue_seat_token`).

`--role` with `--seat` still errors (seat tokens inherit). `--role` on the self-mint path is allowed at or below the seat role.

401/403: print the server `detail`. Do not print the bearer token.

TTY plus admin key plus no target still opens the existing seat picker.

## seat create

If `CITADEL_ADMIN_KEY` is missing, return 1 before `create_seat`. The human message says admin only and hints `citadel token create`. JSON prints `ok: false` and that error. Do not call the admin API.

`seat list` and `seat token` stay on the admin client. This spec does not change those messages.

## mcp

`citadel mcp` with no subcommand, on a TTY: reuse `_wire_detected_tools` (checkbox + `tool_detect.apply`). Esc/q from `checkbox_select` returns None. That path must not call `tool_detect.apply` for selected tools. Exit 0.

Non-TTY: do not prompt. Do not write. Tell the caller to run `citadel mcp add <tool>`. Exit 2.

`install` is an argparse alias of `add` (**VERIFIED**, `kb/cli.py` `aliases=["install"]`). Help lookup for a missing-subcommand listing must follow the shared parser object, not `action.dest` alone, so `install` gets the `add` help string.

## Out of scope

Rich or Textual rewrite. A `citadel admin` command group. `railway.toml` edits. Merging PR #301. Retagging `v0.5.0`. Search canary work in #228 / #247.

## Tests

- Writer home has no `seat create`. Admin home has `seat`.
- `token create` with a seat token mocks POST `/api/access/self/tokens` and does not read `CITADEL_ADMIN_KEY`.
- `citadel mcp` with no args uses the checkbox path (mock select).
- `install` help contains `add` help.
- Status fetch fail: home still has no `seat`.

## Least confident decisions

1. Admin key present skips the session fetch and always shows `seat`, even if `/api/session` would fail. Fetch-fail hiding applies when the admin key is absent.
2. A machine with both `CITADEL_ADMIN_KEY` and a seat token uses the admin picker / `--seat` path, not self-mint, unless the admin key is unset.
3. `access:manage` is read from session `actor.scopes` (plus `capabilities.admin`). A writer token that has that scope sees `seat` on home, but `seat create` still needs `CITADEL_ADMIN_KEY` because the admin client only sends that key.
