---
name: citadel-proactive-ingest
description: Use to capture durable engineering knowledge into Citadel automatically and proactively while working in an org repo. Covers (0) proactive citadel_search at task start plus optional citadel_share_session for dead-end routes (user-approved), (1) mid-session citadel_ingest for durable facts (personal-by-default; all seat writes land on the Node — Central via Promotion Agent only), (2) git pre-push commit snapshots (universal baseline — Cursor, Codex, Claude), (3) optional Claude Code SessionEnd distill, and (4) server-side Railway cron for GitHub org sync, Linear sync, and the learning pipeline. Triggers include "auto sync my sessions", "remember this in citadel", "proactive ingest", "set up citadel autosync", "personal kb sync", "install autosync", and https://citadel.utxo.ag/skills/proactive-ingest.
---

# Citadel Proactive Ingest

Capture durable engineering knowledge into Citadel **as it happens**, with no
per-session ceremony. Five layers — proactive agent reads, dev-side hooks, plus
server-side cron — all **fail-silent** and **personal-by-default** unless
explicitly promoted or volunteered.

0. **Task start, agent-driven (proactive read).** Before coding on a project
   question, run `citadel_search` (Central + your Node + Shared Session Traces).
   Treat trace hits as **reference-only** prior work — verify before acting;
   **Central** stays org-authoritative. After a hard dead end the user wants
   teammates to skip, ask for approval then call `citadel_share_session`.
1. **Mid-session, agent-driven.** While working, the agent proactively calls
   `citadel_ingest` for durable facts and decisions. Personal-by-default.
2. **Git push, hook-driven (universal baseline).** On every `git push` (all
   IDEs), a pre-push hook snapshots commit metadata to the dev's **Node** —
   install once per clone.
3. **SessionEnd, hook-driven (optional).** On every session close (Claude
   Code), a non-blocking hook distills the transcript and POSTs a short note to
   the dev's **Node** — zero per-session steps after a one-time token export.
4. **Server-side Railway cron (org-wide).** Scheduled jobs sync GitHub org
   digests, Linear workspace → **Central** with **Seat-Scoped Mirrors**, skills
   catalog refresh, self-improvement, and backup mirror export. Devs never
   trigger these manually.

```
Personal node:  seat:{slug}   (the dev's private Citadel node — default target)
Shared Central: masumi-network (org-wide; Promotion Agent + org sync — not seat tags)
Hosted base:    https://citadel.utxo.ag
```

Setup first (token + MCP): `https://citadel.utxo.ag/skills/connect`
Read/write rules: `https://citadel.utxo.ag/skills/vault`

## Personal-by-default (read first)

| Personal (default) | Shared Central |
|---|---|
| No `dataset` field on writes | **Promotion Agent** (hourly evolve cron + on demand) |
| Lands in your `seat:{slug}` node | Org GitHub / Linear sync (operator cron) |
| Only you (+ Central readers) can read your node | Whole org reads promoted **Central** copies |

A **seat-writer token** carries `default_dataset=seat:{slug}`. Send writes with
**no `dataset` field** and the server's `resolve_write_targets` routes them to
your private **Node**. Seat-scoped writes **never** dual-write to **Central**
via tags — ADR-0007 **Seat Node Write Policy**.

**Central** updates for your notes happen when the **Promotion Agent** rules
pass (known masumi-org work) or when you **approve** a **New Org Project**
proposal in the **Operations Dashboard**, MCP, or `citadel promotion` CLI.

## Layer 0 — proactive search and shared session traces

**Search before coding.** At the start of a task — architecture, debugging,
"how do we do X here" — run `citadel_search` without waiting to be asked. Default
scope includes your **Node**, **Central**, and **`session-traces`** (Shared
Session Traces volunteered by teammates).

**Trust demotion on trace hits.** Results are split (`central`, `session_traces`,
`node`). Every trace hit carries `_citadel.trust: reference-only`, plus
`author_seat` and age. Traces are consultable prior work — **not Structured
Knowledge**. Never treat them as org truth; verify against code, Central, or the
author before acting.

**Share dead-end routes (explicit consent only).** When the session surfaced a
costly wrong turn another teammate would likely repeat, ask the user:

> "Want to share this session's route so teammates can find it via search?"

Only after **yes**, call `citadel_share_session` (from an Approved Capture Root).
It uploads **Compact Session Context** — distilled recap + dead ends, never raw
transcript. SessionEnd hooks still write **private Node** traces only; sharing is
never automatic in v1.

**Cron is not for agents.** Railway cron syncs GitHub/Linear org sources and runs
the learning pipeline — it does **not** replace agent `citadel_search` or
per-agent share decisions. Do not schedule cron jobs for per-seat agent behavior.

## Layer 1 — proactive mid-session ingest (agent behavior)

While working in an org repo, when a durable fact crystallizes, capture it
without waiting to be asked — via the headless CLI (`citadel ingest`), or the
`citadel_ingest` MCP tool when your session has it registered. Keep each note
small and curated.

**Ingest proactively when:**
- A decision is made (approach chosen, tradeoff settled, library picked).
- A non-obvious root cause is found, or a bug class is understood.
- An interface/contract, env var, or config invariant is established.
- A reusable runbook or "how we do X here" emerges.

**Personal-by-default call (lands in your node):**

```bash
# Headless CLI — works in any terminal/runner with CITADEL_MCP_ACCESS_TOKEN set
citadel ingest "Picked urllib over requests for the SessionEnd hook so it stays stdlib-only and dependency-free." \
  --tag dev-session --tag decision
```

```
# MCP equivalent (when citadel_* tools are registered in-session)
citadel_ingest(
  data="Picked urllib over requests for the SessionEnd hook so it stays "
       "stdlib-only and dependency-free.",
  tags=["dev-session", "decision"],
)
```

No `dataset` field → seat **Node**. Tag with context (`adr`, `runbook`, repo
name) for search — tags do **not** route seat writes to **Central**.

**Never ingest** (same rules as `citadel-vault`): secrets, tokens, keys, seed
phrases, PII, raw logs/debug dumps, ephemeral chatter, or large uncurated
dumps. Summarize durable decisions and source facts instead.

> **Install:** all hooks are now bundled in the `citadel-archive` package
> (`kb.hooks.*`) and installed by **`citadel onboard`** — no vendored scripts.
> See the [`citadel-onboard`](../citadel-onboard/SKILL.md) skill. The sections
> below describe what each hook does and how to wire it by hand.

## Layer 2 — SessionEnd auto-sync (the hook)

`python -m kb.hooks.sync_session` runs from a Claude Code `SessionEnd` hook. On
every session close it:

1. reads the hook payload (`transcript_path`, `cwd`, `session_id`,
   `hook_event_name`) from STDIN;
2. parses the transcript JSONL **defensively** (skips malformed lines, never
   crashes);
3. distills a **short, deterministic** note — 1-2 line recap, key decisions,
   files changed, notable facts — with **no local LLM call**;
4. derives `tags = ["dev-session", <git-branch>, <repo-name>]`;
5. truncates to `CITADEL_MCP_MAX_INGEST_BYTES` (default 200000);
6. POSTs `{data, tags}` (NO `dataset` → personal node) to `{base}/ingest` with
   `Authorization: Bearer ${CITADEL_MCP_ACCESS_TOKEN}`, **HTTPS only**;
7. **fails silently** — catches everything, exits 0, never blocks session
   close, never prints the token.

Server-side LLM enrichment (`CITADEL_LLM_ENRICHMENT_ENABLED`) does any deeper
structuring after the note lands; the hook stays deliberately dumb and fast.

### One-time token setup (the only step)

Get a **seat-writer** token from the connect wizard
(`https://citadel.utxo.ag/skills/connect`) and export
it once:

```bash
export CITADEL_MCP_ACCESS_TOKEN='ctdl_...'
```

That's it. The same token already powers `citadel_search` and the MCP config,
so most devs already have it set. With it in the environment, every session
auto-syncs to your private node. With it unset, the hook is a clean no-op.

### Wire the hook into a repo

`citadel onboard` merges a `SessionEnd` hook into `.claude/settings.json` whose
command is `"<python>" -m kb.hooks.sync_session`. To do it by hand, add that
command under `hooks.SessionEnd` with `allowedEnvVars: ["CITADEL_MCP_ACCESS_TOKEN"]`.

## Layer 3 — Git push auto-sync (universal baseline)

`python -m kb.hooks.sync_push` runs from a git **pre-push** hook. On every push it:

1. reads pre-push ref lines from STDIN (or HEAD when invoked manually);
2. collects commit hash, **subject** (not body), author, branch, and changed
   file paths — **no raw diffs and no commit message body**;
3. POSTs `{data, tags}` with `tags = ["git-push", <branch>, <repo>]` (NO
   `dataset` → personal **Node**), HTTPS only;
4. **fails silently** — always exits 0, never blocks `git push`.

**Install:** `citadel onboard` writes a self-contained `.git/hooks/pre-push`
that runs the bundled module. To do it by hand:

```bash
printf '#!/bin/sh\n"%s" -m kb.hooks.sync_push "$@" 2>/dev/null || true\nexit 0\n' \
  "$(command -v python3)" > .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

Same `CITADEL_MCP_ACCESS_TOKEN` as SessionEnd and MCP. Works in Cursor, Codex,
and Claude — any tool that uses git. This is the **only required dev-side sync
step** for non-Claude IDEs.

### Approved Capture Roots (required allowlist)

Push capture is **fail-closed**. Without `~/.citadel/capture.json` (or with an
empty/corrupt allowlist), the pre-push hook captures nothing. `citadel onboard`
seeds the current repo as a root; to add more folders:

```bash
citadel setup                                   # interactive
citadel setup --root "$HOME/work/repo=org-work" # or non-interactive
```

Each root carries **Capture Root Tags**: `personal` (never promoted to Central)
or `org-work` (eligible for Promotion Agent review). The token is **not** stored
in the file — it stays in the environment.

Once roots exist, `sync_push.py` only captures pushes from inside an Approved
Capture Root (others are skipped with a warning). Capture on demand with:

```bash
citadel capture --dry-run   # preview per-root summaries (no network)
citadel capture             # POST git-metadata + README summaries to your Node
```

Hardening notes: the Node URL must be **HTTPS** (`citadel capture` refuses
`http://` before sending the token, and never follows redirects). Summaries are
size-capped (`CITADEL_MCP_MAX_INGEST_BYTES`, default 200000) and git-remote
credentials are redacted. `citadel capture` exits **non-zero** on any failure
(corrupt config, no matching root, missing token, or a per-root POST error), so
it is safe to gate CI on.

## Layer 4 — Server-side Railway cron (org-wide)

Background jobs on Railway keep **Central** and **Seat-Scoped Mirrors** fresh.
Devs and agents **never** need to trigger these — cron is the source of truth for
org-wide source material.

| `CITADEL_RUN_MODE` | What it syncs | Typical schedule |
|---|---|---|
| `learning-agent` (or `github-sync`) | GitHub org digest → Central | Daily (`0 8 * * *` UTC) |
| `linear-sync` | Linear workspace → Central; assignee issues **Seat-Scoped Mirror** → each **Node** | Daily or hourly (operator choice) |
| `pipeline` (also `all`/`cron`) | GitHub sync + skills refresh + self-improve + backup mirror (each stage env-toggleable) | Daily |
| `evolve` | Self-evolving cycle: github sync → repo-content → self-improve → promotion → Linear sync → cognify (each `CITADEL_EVOLVE_*`-toggleable) | Every 1h via the in-process scheduler in the web service (`CITADEL_EVOLVE_SCHEDULER_ENABLED`, `CITADEL_EVOLVE_INTERVAL_SECONDS=3600`) — not a separate Railway service (promotion/cognify need the web's single-writer Kuzu volume) |
| `backup-mirror` | Vault Backup Mirror manifest export | Daily |

Key env vars (Railway cron service / web service — not dev shells):

- `CITADEL_LINEAR_API_KEY` — required for `linear-sync` (and the evolve Linear stage)
- `CITADEL_EVOLVE_SCHEDULER_ENABLED` / `CITADEL_EVOLVE_INTERVAL_SECONDS` — the hourly in-process evolve scheduler (web service; default `3600`)
- `CITADEL_LINEAR_USER_MAP` — optional JSON map Linear user id → seat slug
- `CITADEL_GITHUB_ORG`, `CITADEL_GITHUB_SYNC_*` — GitHub org sync scope
- `CITADEL_PIPELINE_*_ENABLED` — toggle individual pipeline stages

Manual admin triggers (only when the user explicitly asks):

- `POST /api/github-sync/run` or MCP `citadel_run_learning_agent`
- `POST /api/linear-sync/run`
- `POST /api/learning-agent/optimize`

## Agent sync policy

**Rely on cron + hooks — do not add dev ceremony.**

| Situation | Agent action |
|---|---|
| Starting work on a project/architecture/debug task | **`citadel_search` first** (Central + own **Node** + `session-traces`) |
| Trace hit in search | Read as **reference-only**; verify before acting; cite `author_seat` |
| User asks a project/architecture question mid-task | `citadel_search` (Central + own **Node** + traces) |
| Costly dead end the user wants teammates to skip | Ask approval → `citadel_share_session` (inside Approved Capture Root) |
| User asks "what do I need to do?" / task list | `citadel_linear_my_issues` (**Seat-Scoped Mirror** in own **Node**) |
| Org-wide Linear context | `citadel_linear_search` (Central) |
| Durable fact crystallizes mid-session | `citadel_ingest` (personal-by-default; promote with tag only when user asks) |
| Git push or session close | **Nothing** — hooks handle it automatically |
| Org source material stale | **Do not** trigger admin sync unless user explicitly requests a refresh |
| User asks to run learning agent / Linear sync now | Admin tools only, with approval |

All background sync (hooks and cron) is **fail-silent**: outages never block git
push, session close, or agent work.

## Privacy posture

- **Token from env only.** Read solely from `CITADEL_MCP_ACCESS_TOKEN`; never
  printed, echoed, or written to a tracked file.
- **Distilled, not raw.** The SessionEnd hook sends a curated short note, not
  the transcript. Push hooks send subject + paths only.
- **Allowlist-required.** Missing `capture.json` → push capture is a no-op.
- **Transcript path allowlist.** SessionEnd refuses paths outside
  `~/.claude` / `~/.cursor` / `~/.codex` / `~/.agents`.
- **Personal-by-default.** No `dataset` field on auto-sync → your seat node.
- **HTTPS only.** The POST refuses any non-`https://` base URL.
- **Non-blocking.** The hook always exits 0; a Citadel outage never delays your
  session close.

## Opt-out

Drop a `SessionEnd` override into `.claude/settings.local.json` (gitignored), or
simply unset `CITADEL_MCP_ACCESS_TOKEN` for that shell. With no token the hook
does nothing. See `README.md`.

## Reference

- This skill: `https://citadel.utxo.ag/skills/proactive-ingest`
- Connect wizard: `https://citadel.utxo.ag/skills/connect`
- Vault read/write rules: `https://citadel.utxo.ag/skills/vault`
- Dev onboarding: [`docs/onboarding/citadel-autosync.md`](../../docs/onboarding/citadel-autosync.md)
- Teammate one-pager: [`docs/onboarding/teammate-rollout.md`](../../docs/onboarding/teammate-rollout.md)
- Ingest API: `POST /ingest` with `{data, dataset?, tags?, session_id?}` (writer token)
- Seat/**Node**/Central + **Seat-Scoped Mirror**: [ADR-0003](https://github.com/masumi-network/Citadel-Archive/blob/main/docs/adr/0003-seat-node-central-private-memory.md), [ADR-0004](https://github.com/masumi-network/Citadel-Archive/blob/main/docs/adr/0004-linear-seat-scoped-mirror.md)
