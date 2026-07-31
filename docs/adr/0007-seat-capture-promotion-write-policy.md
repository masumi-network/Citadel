# ADR 0007 — Seat capture, promotion, and write policy

- **Status:** Accepted (2026-06-27)
- **Relates:** ADR-0003 (seat/node/Central private memory), ADR-0005
  (self-evolving memory + policy-gated ingestion), `CONTEXT.md` glossary terms:
  **Seat Node Write Policy**, **Approved Capture Roots**, **Capture Policy**,
  **Capture Root Tags**, **Promotion Agent**, **New Org Project**,
  **Promotion Approval**, **Operations Dashboard**.

## Context

Citadel separates private seat **Nodes** from shared **Central**. Devs connect
via MCP + seat tokens, autonomous capture (git push, session hooks), and a
planned setup CLI. Security requirements:

- Seat-scoped callers must not dump raw or sensitive content into **Central**.
- MCP and agents must not silently ingest; approved folders may auto-capture to
  the **Node** only.
- **Central** should evolve from org sync and governed **Promotion**, not direct
  seat writes or org-tag bypasses.

ADR-0003 allowed seat HTTP ingest with org-bound tags to reach **Central**.
This ADR **narrows** that path: all seat-scoped writes land in the **Node**;
**Central** is updated only through governed upstream jobs.

Partial implementation exists (2026-06-27): seat write policy on all HTTP paths,
capture CLI, capture policy API, MCP guards, promotion engine sketch (local).
Gaps vs refinements below: full promotion rule parity, `citadel promotion` CLI,
promotion metadata on Central writes, production enablement.

## Decision

### 1. Seat Node Write Policy (all channels)

Seat **Tokens** and **Agent Identities** scoped to a **Node** may **write only
to that Node**. **Central** is **read-only** for seat-scoped callers on every
channel: MCP, HTTP `/ingest`, autosync hooks, dashboard, and CLI capture.

**Central** receives **Structured Knowledge** only via:

- Org source sync (GitHub / Linear cron, Scout PR ingestion per ADR-0005)
- **Promotion Agent** (Node → Central, governed)
- Service-account **Vault Contributions** and operator/admin jobs

Direct seat ingest with org tags (`org-ready`, `repo-content`, etc.) to
**Central** is **removed** (supersedes the seat write exception in ADR-0003).

### 2. Ingest approval model

| Capture type | Approval |
|---|---|
| Inside **Approved Capture Roots** | Auto-capture to **Node** (no per-write MCP/agent prompt) |
| Outside approved roots | MCP client tool approval **and** agent explicit yes/no before `citadel_ingest` |
| All writes | Server **Security Finding** gate (block high/critical secrets) |

### 3. Approved Capture Roots + Capture Policy (hybrid)

**v1 triggers:** git push inside an approved root + manual `citadel capture`
on demand. No file watcher; no local schedule in v1.

**Storage (hybrid):**

- **Local:** **Approved Capture Roots** (filesystem paths) per machine, chosen in
  setup wizard.
- **Server:** org **Capture Policy** baseline per **Seat** (deny rules, templates).

**Governance:** admin sets org baseline; **Vault Member** may add **stricter**
local rules only — never remove org denies.

Any local path may be approved (org clones, personal, experimental). Capture
to **Node** does not imply **Central** visibility.

### 4. Capture Root Tags (setup wizard)

Each approved root is tagged at setup:

| Tag | Promotion rule |
|---|---|
| `personal` (preset) | Never auto-promote to **Central** |
| `org-work` (preset) | Only capture-root tag eligible for auto-promote (when agent rules pass) |
| Custom tags | Labels only — capture from custom-tagged roots **never** auto-promotes |

**Non-capture** writes (MCP ingest, session hooks) are **not** gated by root
tags — only by **Promotion Agent** reference checks and LLM classification.

### 5. Promotion Agent (Node → Central)

Runs on **6h Railway evolve cron** and **on demand** (Operations Dashboard or
`citadel promotion run` / API). **On demand:** each **Vault Member** may run for
their own **Node**; admins may run for any seat.

For each candidate note in a seat **Node**:

1. **Secret scan** — block on high/critical findings (fail closed).
2. **Capture Root Tags** — `personal` never auto-promotes; only **`org-work`**
   capture-root content may auto-promote; custom-tagged capture roots never
   auto-promote.
3. **Structured reference check** — compare repo hints against the masumi
   **GitHub organization repo list** and **Structured Knowledge** in **Central**.
4. **LLM classification** — relevant, not sensitive, score ≥ threshold (always
   required, even when structured match succeeds).
5. **Route:**
   - Repo in masumi org (or strong **Central** match) + rules pass →
     **auto-promote** (dual-write; original stays in **Node**).
   - External repo / **New Org Project** + LLM pass → **Promotion Approval**
     queue (one-shot per note).
   - No repo hint → auto-promote **only** on strong **Central** match + LLM pass;
     otherwise stay on **Node** (no queue).
   - Otherwise → skip (stay on **Node**).

Structured repo lists decide *whether* content is org work; the LLM decides
*whether it is safe to share*.

**Traceability:** v1 records audit events plus promotion metadata on the
**Central** copy (seat, promotion id, approver if any, timestamp). Target:
full **Source Snapshot** back-link to the seat **Node** original.

### 6. Promotion Approval surfaces

The **Promotion Agent** queues items — **Vault Members do not add them**.

- **Operations Dashboard:** pending promotion queue + approve/reject.
- **MCP:** list pending; approve/reject only after **explicit user confirmation**
  in chat (same bar as `citadel_ingest`).
- **CLI:** `citadel promotion list|approve|reject|run` with headless `--json`.
- **Visibility:** each **Vault Member** sees their own queue; admins see all
  seats and may approve on a member’s behalf (delegate flagged in audit).
- **Deciding is admin-only (amended 2026-07-31).** As written, this section
  implied a member could approve or reject items in their own queue. Issue #48
  closed that as a self-promotion path: a seat holder able to approve their own
  item can move arbitrary **Node** content into **Central**, which is exactly
  what the curated-Central rule exists to prevent. Both `approve` and `reject`
  now require `role="admin"` on every surface — HTTP routes and MCP tools alike
  — and the check runs before the item is even loaded. Members keep the rest of
  the loop: they still **list** their own pending queue and still **run** their
  own promotion pass. Only the decision is admin-locked.
- **Reject sticks** — the same note is not re-queued on later cron passes unless
  its content changes.
- **Approve is one-shot** — promotes that note only; later notes from the same
  external project still need approval or masumi org repo membership.

### 7. Operations Dashboard role

Dashboard is for **monitoring and governance**, not the primary dev write surface.
Day-to-day capture flows through MCP, approved-root autosync, and CLI.

## Consequences

- **+** Clear security boundary: **Node** = private working memory; **Central** =
  curated org memory. Auto-capture without spamming approval prompts for trusted
  folders. Promotion is explainable (GitHub + **Central** refs, tags, approval
  queue).
- **−** Build work: setup CLI wizard, local policy file, server policy API,
  promotion reference checks, approval queue UI, HTTP guard parity with MCP,
  ADR-0003 doc/consequence updates. Admin delegate approval needs careful audit
  UX.
- **Migration:** Remove or 403 seat HTTP org-tag routing to **Central**; update
  tests such as `test_org_tag_ingest_routes_to_central`. MCP seat guards already
  partially enforce this.

## Build order (suggested)

1. Enforce **Seat Node Write Policy** on all HTTP write paths (parity with MCP).
2. Server **Capture Policy** baseline API + admin UI snippet.
3. Local setup wizard + `citadel capture` + merge local roots with server template.
4. **Promotion Agent** reference checks (GitHub org + **Central** search) + tag rules.
5. **Promotion Approval** queue (dashboard + MCP tool) + admin delegate with audit.
6. Wire 6h evolve cron + on-demand trigger to promotion pass.

## Refinements (2026-06-27 design session)

Design session locked the promotion decision tree before P5/P6 implementation.
See also `CONTEXT.md` glossary (**Promotion Agent**, **Promotion Approval**,
**Capture Root Tags**).

| # | Decision |
|---|----------|
| 1 | Known org work **auto-promotes**; **New Org Project** → member **Promotion Approval** queue |
| 2 | **Hybrid tags** — capture roots follow tags; MCP/hooks skip tag gate |
| 3 | Known work requires **masumi org repo** (or **Central** representation) |
| 4 | No repo hint → **Central** match only for auto-promote; else stay on **Node** |
| 5 | **LLM + secret scan** always required (structured match alone is not enough) |
| 6 | Only **`org-work`** capture roots may auto-promote; custom roots never |
| 7 | Approval is **one-shot** per note + git-like traceability |
| 8 | Ship audit + metadata on **Central** copy (v1); target **Source Snapshot** link |
| 9 | On demand: member own seat, admin any seat |
| 10 | Surfaces: dashboard + MCP (human confirm) + **`citadel promotion`** CLI |
| 11 | **Reject sticks** — no re-queue for unchanged content |
| 12 | **Member queue** — agent proposes; member lists and runs their own queue, but **deciding is admin-only** (amended 2026-07-31, issue #48 — see §6) |

## Open questions

- Additional preset **Capture Root Tags** beyond `personal` / `org-work`.
- Exact threshold for “strong **Central** match” on no-repo-hint notes.
- **`citadel promotion`** subcommand naming (`run` vs `promote`).
