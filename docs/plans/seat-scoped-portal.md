# Seat-Scoped Portal — Product & Architecture Plan

**Status:** Phase 1 **SHIPPED** (PR [#95](https://github.com/masumi-network/Citadel/pull/95), 2026-07-21) — Phase 2 remaining  
**Date:** 2026-07-21  
**Brand UX:** Login + sidebar use the **Pixel Bastion** mark and Interface chrome (PR [#96](https://github.com/masumi-network/Citadel/pull/96)); does not change seat auth or Node isolation.  
**Relates:** [ADR-0003](../adr/0003-seat-node-central-private-memory.md), [ADR-0006](../adr/0006-agent-auth-and-onboarding.md), [ADR-0007](../adr/0007-seat-capture-promotion-write-policy.md), [ADR-0009](../adr/0009-mesh-read-isolation-presence-vs-content.md), [`agent-access-model.md`](../agent-access-model.md), [`organization-vault-plan.md`](../organization-vault-plan.md), [`onboarding/teammate-rollout.md`](../onboarding/teammate-rollout.md)

## Vision

Every licensed human has a **Seat**. That person should:

1. Log into **their own account** using their seat **Access Token** as the credential (not only the org admin key).
2. See **their Node** — knowledge, activity, and graphs from that seat’s point of view.
3. See **Central** in the same workspace, clearly linked to their seat context (read Central; write Node; promotion is the bridge).
4. Stop experiencing seats as empty, disconnected “slots” on an admin console.
5. Trust that **ingest via a seat Token always lands in that Seat’s Node**, and that the portal makes that link readable (Seat ↔ Node/KB ↔ graph ↔ activity).

Today the **data model and API already encode seat → Node + Central**, and **seat-token write routing already forces Node-only ingest** (`resolve_write_targets` → `seat:{slug}`). **Phase 1** productized seat-token browser login, session chrome, and **My Node** (Seat home). Remaining gap is mostly **Phase 2 observability**: Access inventory health deep-links, graph “you” highlight / hub context, and the org-wide **seat activity analytics** table.

---

## Current state

### How seats and tokens work

| Layer | Today |
|---|---|
| **Seat** | Admin-provisioned **Principal** (`create_seat` / Access UI / `citadel seat create`). One human → one slug → one Node dataset `seat:{slug}`. Seats cannot be admin. |
| **Node** | Logical Cognee dataset `seat:{slug}`. Private working memory. Filled by MCP ingest, capture hooks, CLI capture, Linear seat-scoped mirror — not by creating the seat alone. |
| **Central** | `masumi-network`. Org knowledge from GitHub/Linear sync, Promotion Agent, service-account contributions. |
| **Token** | `ctdl_…` from AccessStore. Seat-writer tokens carry `seat_slug`, `default_dataset=seat:{slug}`, `default_session=seat-{slug}`, `allowed_datasets` including Node + Central. |
| **Write policy** | ADR-0007: seat-scoped callers write **Node only**. Central is read-only for seats. Enforced in `resolve_write_targets` / `guard_seat_write_policy`. |
| **Read policy** | Own Node + Central. Other seats’ Node **content** never. Seat **presence** (hub + counts) visible to all (ADR-0009). |

Admin-first provisioning is intentional (inventory, licensing, audit). Tokens are the credential; the Node is the storage boundary.

### Ingest attribution — code reality (challenge)

**Write path is linked today for a real seat identity:**

1. `POST /ingest` (and MCP `citadel_ingest`) resolve the caller via AccessStore → `AccessIdentity` with `seat_slug` / `default_dataset`.
2. `resolve_write_targets` for seat identities **ignores** a foreign requested dataset and returns only `WriteTarget(seat:{slug}, "light")`.
3. Mesh activity records the **dataset** on each ingest event; Knowledge Mesh graph attribution uses `dataset_map` so documents hang under `seat:{slug}` hubs when Cognee attribution succeeds.
4. Audit events store `actor_id` / `actor_name` / `role` / **`dataset`** — not a dedicated `seat_slug` column. Seat can usually be derived as `dataset.removeprefix("seat:")` when the write hit a Node.

**So “ingest via a seat token is not properly linked” is likely one of these, not a missing write-router:**

| Hypothesis | Evidence |
|---|---|
| **H1 — Wrong credential** | Token without `seat_slug` (service / env / legacy). `citadel status` prints “This token has no seat”; writes go to org default, not a Node. |
| **H2 — Empty Node UX** | Seat create does not seed docs; graph shows presence hub with 0 documents → feels “unlinked.” |
| **H3 — UI / observability gap** | Session chrome ignores `seat_slug`; no Seat home; Audit is admin-only; no per-seat activity analytics; interlinking Seat ↔ KB ↔ graph is undocumented in-product. |
| **H4 — Attribution display bug** | Cognee `node_dataset_map` / soft-degrade path leaves some graph nodes without `dataset` — content exists but UI doesn’t hang it under the seat hub. |
| **H5 — Actual routing bug** | Would require a seat identity writing outside Node — contradicts current `resolve_write_targets` + tests; treat as regression if reproduced. |

### Working diagnosis (2026-07-21 grill)

User: seat Tokens (own + teammates’) **are** seat-linked; still “don’t see the seats linked to anything.”

→ **Working hypothesis: B and/or C** (portal/graph/UI does not surface Seat↔KB; and/or empty/disconnected UX). **Not A** (no-seat token). **Not claiming D** without a routing repro.

**What surfaces seat linkage (post–Phase 1):**

| Surface | What you get | Still missing (Phase 2+) |
|---|---|---|
| **Access → Seats** (admin) | Name, slug, `node_dataset` string, token counts/revoke | Doc/ingest counts; click-through to graph/search; last activity |
| **Knowledge Graph** | Synthetic `seat:{slug}` presence hubs + doc counts; inspector says “Seat presence” | “You” highlight; hub → seat context panel; empty hubs still possible until capture |
| **Session chrome** | Seat slug + Node label + role from `/api/session` | — |
| **Member default page** | **My Node** (Seat home) via `GET /api/me/summary` | Analytics / Access deep-links |
| **Audit** | `actor_*` + `dataset` (admin-only) | Seat-centric analytics rollup |

Attribution remains covered by existing seat write tests + home `node_dataset` / last-ingest fields — product work still targets **visibility + interlinking**, not a second write router.

### Auth surfaces today

- **Browser login** (`POST /admin/session`): accepts env bootstrap keys **and** AccessStore tokens. A seat `ctdl_` token mints an HttpOnly cookie session (`token:{role}:{token_id}:{sig}`).
- **API / MCP**: `Authorization: Bearer` with the same token → `AccessIdentity` with seat fields.
- **Session payload** (`GET /api/session` → `role_payload`): returns `seat_slug`, `node_label`, `default_dataset`, `search_datasets`, capabilities.
- **Login copy (Phase 1):** member-first seat-token framing (Pixel Bastion lockup). Ops still paste admin/bootstrap keys. Optional portal path documented in `teammate-rollout.md` alongside MCP.

### What UI exists (Phase 1 shipped)

Single SPA (`kb/static/index.html` + `app.js`):

| Area | Behavior |
|---|---|
| **Login** | One field; seat-token copy + Pixel Bastion brand. |
| **Default page** | Seat holders → **My Node** (`home`); admin → Overview. |
| **Seat home** | `GET /api/me/summary` — Node stats, last ingest, empty checklist, links to search/graph/activity. |
| **Search / Knowledge Graph** | Scoped by caller identity (Node content + Central; other seats as presence hubs). Search badges: My Node vs Central. |
| **Access** | Admin-only (`access:manage`): create seats, issue tokens, connect wizard, capture policy. |
| **Audit / Settings / Sources sync** | Admin-oriented; hidden from non-admin nav. |
| **Promotion queue** | API supports member own-seat + admin all; Phase 3 polish for member workflow framing. |
| **Session chrome** | Seat slug + Node label + role. |
| **Seat activity analytics** | **Not shipped** (Phase 2). Vault Activity + Audit exist; no per-seat calls/counts table. |

### Why seats can still feel empty / not fully interconnected

1. **Empty Node by design until capture.** Creating a seat creates the principal + dataset name; it does not seed documents (checklist explains fill paths).
2. **Primary write path remains headless.** MCP + hooks are still the main ingest story; portal is optional visibility.
3. **Phase 2 interlinking unfinished.** Access inventory health, graph “you” highlight / hub context, analytics table.
4. **Admin Access inventory ≠ member experience.** Seats on the Access page are still provisioning objects.
5. **Central can dominate visually** when the member’s Node is empty (search still returns Central).

---

## Locked decisions

| Decision | Choice | Notes |
|---|---|---|
| **Architecture** | **Option A** — productize existing token cookie login | No Option B / SSO in Phase 1–2. |
| **Ingest ↔ Seat** | Verify + **surface** linkage (working hypothesis **B+C**) | Write router already Node-only for seat identities; product gap is visibility. |
| **“Linked” surfaces** | **All three (4)** | Phase 1: chrome + Seat home. Phase 2: Access health + graph interlinking. |
| **Seat activity analytics** | Columns + audience **B** | requests/calls (7d), reads (7d), writes (7d), docs (lifetime); all members see org-wide counts; not a ranked leaderboard. **Phase 2.** |
| **Central write from UI** | **Promotion Approval only** | No exceptions for human seats (ADR-0007). |
| **Graph default** | **Always** show universal presence canvas | Highlight “you”; other hubs presence-only. **Phase 2 polish.** |
| **Empty Node SLA** | **Empty until first capture** | Strong Seat-home checklist; no fake seed content. |
| **Token rotation (Phase 1)** | **Admin-only** | Self-service deferred to Phase 3 / Option B. |
| **Readability / interlinking** | Phase 1 partial; Phase 2 full | Phase 1: chrome + home links to search/graph/activity. |

**Grill status:** **Closed** 2026-07-21. **Phase 1 shipped** (PR #95). **Phase 2** is next.

---

## Goals / non-goals

### Goals

- Make **token-as-session** the first-class member login story (seat credential = portal access).
- Give each seat holder a **seat-scoped portal**: clear “you / your Node / Central” framing.
- Surface **Node knowledge, activity, graphs, and seat activity analytics** from the seat POV, with Central visible and linked.
- Make **Seat ↔ Node ↔ graph ↔ activity** navigable and readable in-product.
- Prove (or fix) **ingest attribution** so a seat Token’s writes are visibly that Seat’s Node.
- Align onboarding docs and UI empty-states so “empty seat” means “not yet capturing,” not “broken account.”
- Preserve ADR isolation: no cross-seat Node content; Central stays curated.

### Non-goals (this plan)

- Full OAuth 2.1 / Google SSO (ADR-0006 Phases B–C) — complementary later; token portal is the MVP bridge.
- Option B dual credential in Phase 1 (short-lived portal token) — deferred unless grill reopens it.
- Physical DB-per-seat isolation.
- Seat-to-seat Node sync or glass-walls (rejected in ADR-0009).
- Replacing MCP / CLI as primary **write** surfaces (dashboard remains monitor + light governance per ADR-0007).
- Letting seats become admin or write raw content into Central from the UI.

---

## UX: seat login with token

### Login

1. Member opens `/login` (rename framing: “Citadel” / “Seat access,” not “Admin”).
2. Pastes their `ctdl_…` seat token (same secret used for MCP / `CITADEL_MCP_ACCESS_TOKEN`).
3. Server already validates via AccessStore and sets cookie for 12h.
4. Redirect into **seat home**, not a generic admin overview.

Optional later: short-lived “portal-only” session tokens vs long-lived MCP tokens; MVP reuses the seat-writer token.

### What they see after login (target)

| Surface | Seat member | Admin (unchanged / enhanced) |
|---|---|---|
| **Chrome** | Seat slug + Node label + role | Admin + seat inventory |
| **Home** | My Node summary: doc/capture counts, last capture, promotion pending, “how to fill your Node,” deep links into graph + activity | Org health overview + **seat activity analytics** |
| **Search** | Multi-dataset: Node + Central (already on API path) with source badges | Same + bypass |
| **Graph** | Own Node content + Central + **all seats as presence**; highlight “you”; click hub → seat/home context | Full content (support) |
| **Activity** | Own Node activity with content; other seats presence-only | Org broadcast + analytics |
| **Seat activity analytics** | Org-wide presence-safe counts (decision B); own row deep-links to detail | Full per-seat analytics table |
| **Promotion** | Own pending queue approve/reject | All seats + delegate |
| **Access** | Hidden (or read-only “your tokens” later) | Full provisioning |

Central remains **read** in UI for seats. Writes stay Node-bound. Promotion is the only member path that affects Central visibility of their work.

---

## Architecture options

### Option A — Productize existing token sessions — **LOCKED for Phase 1–2**

**Idea:** Keep AccessStore + cookie login. Teach the SPA to read `seat_slug` / `node_label` from `/api/session`, add a seat home route, role-gated nav, seat-aware empty states, activity analytics + interlinking. Thin API additions where member-safe aggregates are missing (e.g. `GET /api/me/node-summary`, seat analytics aggregates).

| Pros | Cons |
|---|---|
| Ships on current auth; no new IdP | Long-lived token pasted into browser (same as today’s MCP secret) |
| Matches ADR-0003/0007 mental model | Login UX still “paste secret” until ADR-0006 |
| Smallest diff; reuses mesh isolation | Must carefully hide admin nav for writers |

**Fits:** MVP in days/weeks, not a platform rewrite.

### Option B — Dual credential: portal session vs agent token (deferred)

**Idea:** Seat login mints a short-lived **browser session** credential distinct from the MCP `ctdl_` agent token.

**Fits:** Phase 3 hardening after portal UX lands — not a Phase 1 gate.

### Option C — Identity-first portal (Google SSO → seat map) (deferred)

**Idea:** ADR-0006 track. Do not block member Node views on SSO.

### Locked flow (Option A)

```
Member browser                    Citadel Node
─────────────                     ────────────
Paste ctdl_ token ──POST /admin/session──► AccessStore auth
                      ◄── cookie ────────── AccessIdentity(seat)
GET /api/session      ◄── seat_slug, node ── role_payload
Seat home / graph / search / analytics
Writes ──────────────────────► Node only (ADR-0007; already enforced)
Promotion approve ────────────► Central (governed)
```

---

## Phased roadmap

### Phase 1 — Seat login + Node view + attribution proof (MVP) — **SHIPPED (PR #95)**

**Scope (delivered)**

- Relabel login for members (copy, title, helper: “seat token from admin / `citadel seat token`”).
- Persist and display seat identity in chrome from `/api/session` (`seat_slug`, `node_label`, role).
- **Seat home** page (default for seat holders): Node stats, recent activity for own Node, pending promotions count, onboarding checklist when empty; **links** to search / graph / activity.
- Hide Access / Audit / Settings / Overview admin nav for non-admin.
- Empty-state copy: Node fills via capture/ingest — Central still searchable.
- Search badges distinguish My Node vs Central; seat tokens keep multi-dataset search.
- Attribution: existing seat write tests + home `node_dataset` / last-ingest fields.
- `teammate-rollout.md`: optional “open the portal with your token” path alongside MCP.

**Acceptance criteria**

- [x] Member logs in with seat-writer token only; lands on seat home showing their slug and Node id.
- [x] Admin-only pages unreachable (UI + API 403).
- [x] Search returns Central hits even when Node is empty; Node hits appear after a test ingest.
- [x] Session chrome shows seat, not only “Read write.”
- [x] After seat-token ingest, home/audit/mesh show that write under `seat:{slug}` (not Central / unset).
- [x] Docs mention portal login without replacing MCP onboard.

### Phase 2 — Graphs + readability / interlinking + seat activity analytics

**Scope**

- Graph default focus: highlight caller’s seat hub; depth-from-you; Central as shared hub.
- Legend / filters: “My Node” vs “Central” vs “Other seats (presence).”
- Drill-down: own Node docs + Central docs; other hubs presence-only; **clickable hub → seat context panel** (slug, doc count, last activity, link to home/search scoped).
- Optional: side panel “links to Central” for promoted docs.
- Vault Activity: seat POV default; org presence strip secondary.
- **Access inventory health:** per-seat doc count, last ingest, jump-to-graph / search scoped to `seat:{slug}`.
- **Graph interlinking:** legend, “you” highlight, hub → seat context panel (slug, counts, links home).
- **Seat activity analytics** table — **not ranked/competitive**. Columns locked: requests/calls (7d), reads/searches (7d), writes/Node ingests (7d), docs (lifetime). **Audience B:** all members see org-wide counts; admin full table; never peer Node content.
- Cross-links: analytics row → seat hub / seat home; activity event → dataset / graph; Seat Access row → Node id + open graph.

**Dependencies**

- Phase 1 chrome + identity + attribution proof.
- Knowledge mesh endpoints already filter by caller.
- Metric definitions locked in grill (what counts as request vs read vs write; retention).

**Risks**

- Aggregated overview hides personal content — keep a “My Node only” toggle.
- Performance if every login pulls full mesh — keep server caps / soft degrade.
- Analytics from ephemeral Vault Activity alone would reset on deploy — prefer audit (durable, capped) or explicit rollup store.
- Over-exposing peer detail beyond ADR-0009 Seat Presence.

**Acceptance criteria**

- [ ] Seat member can distinguish their hub from Central and from other seats in one glance.
- [ ] Clicking another seat hub never reveals that Node’s documents.
- [ ] With content in Node + Central, both appear under the member’s scope without admin login.
- [ ] From Seat home, member can hop Seat → graph hub → activity without re-deriving dataset names mentally.
- [ ] Analytics dashboard shows per-seat **requests/calls (7d)**, **reads/searches (7d)**, **writes/Node ingests (7d)**, **docs (lifetime)** for **all members** (presence-safe counts); admin full table; never other seats’ Node content.

### Phase 3 — Interconnection / promotion visibility / portal polish

**Scope**

- Promotion Approval as first-class member workflow on seat home + dedicated page.
- Visibility of what left the Node for Central (promotion history for own seat).
- Shared Session Traces discoverability from portal (read-only list / search entry points).
- Seat-scoped Linear mirror status.
- Option B: short-lived portal sessions + token rotation UX.
- Kick off ADR-0006 device grant / SSO design against this IA (Option C).

**Dependencies**

- Promotion Agent + queue APIs (partially shipped).
- Shared session traces (ADR-0011).
- Phase 1–2 portal shell.

**Risks**

- Scope creep into full SSO before portal IA is stable.
- Over-notifying members about other seats’ presence.

**Acceptance criteria**

- [ ] Member can approve/reject own promotion items from portal and see outcome reflected in Central search.
- [ ] Member can explain Node vs Central vs shared traces from in-app copy alone.
- [ ] Token rotation path documented and usable without admin guessing which token is MCP vs portal (if Option B shipped).

---

## Suggested implementation sketch (Phase 1 only)

Not a commitment — orientation for implementers after grill closes:

1. `loadSession`: store `seat_slug`, `node_label`, `search_datasets`, capabilities; render seat chip.
2. `initialPage()`: if `seat_slug` → `home` (new); else admin overview / search.
3. New `GET /api/me/summary` (or compose existing mesh + promotion pending + session): counts + last events for `identity.seat_slug` only.
4. Nav: `data-min-role="admin"` audit on Access/Audit/Settings; ensure writer cannot call `/api/access`.
5. Login HTML: member-first copy; keep accepting admin key for operators.
6. Smoke: seat token `POST /ingest` → assert dataset `seat:{slug}` on result + audit `dataset` + mesh event.
7. Docs: one section in teammate rollout + pointer from this plan.

Phase 2 sketch (after metric grill): seat analytics aggregate endpoint + SPA panel; graph/activity deep-links keyed by `seat_slug` / `seat:{slug}`.

---

## Open questions (grill queue)

**All resolved.** Closing pack accepted (recommendations):

1. ~~Ingest diagnosis~~ → B+C  
2. ~~Linked surfaces~~ → 4 (all); Phase 1 ships chrome/home first  
3. ~~Analytics columns~~ → 7d requests/reads/writes + lifetime docs  
4. ~~Analytics audience~~ → B (org-wide counts for members)  
5a. ~~Central write UI~~ → Promotion only  
5b. ~~Graph default~~ → always presence canvas  
5c. ~~Empty Node~~ → empty until capture + checklist  
5d. ~~Token rotation~~ → admin-only in Phase 1  

---

## Phase 1 ready-to-build checklist

- [x] Login framing: Citadel / seat token (not “Admin”)
- [x] `loadSession` stores `seat_slug`, `node_label`, `search_datasets`, capabilities
- [x] Session chrome shows seat + Node (not only role)
- [x] Seat **home** page default for seat holders; Node stats + empty checklist
- [x] Admin nav (Access / Audit / Settings / Overview) hidden from non-admin
- [x] Search result badges distinguish My Node vs Central
- [x] `GET /api/me/summary` for home stats
- [x] Teammate rollout mentions optional portal login
- [x] Tests for `/api/me/summary` + seat session chrome data
- [x] Attribution smoke covered by existing seat write tests / summary `node_dataset` field

**Out of Phase 1 (Phase 2+):** seat activity analytics table, Access inventory deep-links, graph “you” highlight / hub context panel.

---

## References

- Seat / Node / Central: ADR-0003, `CONTEXT.md` glossary  
- Write policy: ADR-0007  
- Mesh presence vs content: ADR-0009  
- Future SSO: ADR-0006  
- Headless rollout today: `docs/onboarding/teammate-rollout.md`  
- Dashboard model (aspirational apps): `docs/agent-access-model.md` § Dashboard Model  

---

## Decision log

| Date | Decision |
|---|---|
| 2026-07-21 | **Option A locked** (productize token sessions) for Phase 1–2; defer Option B dual credential and Option C SSO. |
| 2026-07-21 | **Ingest↔Seat linkage** is a Phase requirement: verify write attribution and surface it; do not assume router is missing without smoke evidence. |
| 2026-07-21 | **Seat activity analytics** (per-seat calls/counts dashboard — not a competitive leaderboard) and **readability/interlinking** added to Phase 1–2 scope. |
| 2026-07-21 | Grill Q1: tokens are seat-linked; user still doesn’t *see* linkage → **working hypothesis B+C** (UI/observability + empty/disconnected UX). Not A; D unproven. |
| 2026-07-21 | Grill Q3 clarify: **not a leaderboard** — rename to **seat activity analytics** (fields/counts/calls per seat). |
| 2026-07-21 | Grill Q3b: analytics columns locked — **requests/calls (7d)**, **reads/searches (7d)**, **writes/Node ingests (7d)**, **docs (lifetime)**. |
| 2026-07-21 | Grill Q4: analytics audience **B** — all members see org-wide presence-safe seat counts; admin full table; no peer Node content. |
| 2026-07-21 | Grill Q2: **“Linked” = all surfaces (4)** — Access inventory + Knowledge Graph + session chrome/Seat home. Phase 1 ships chrome/home first; Phase 2 ships Access deep-links + graph focus/legend. |
| 2026-07-21 | Grill closing pack accepted: Promotion-only Central UI; always presence graph; empty Node + checklist; admin-only token rotation in Phase 1. |
| 2026-07-21 | **Grill closed.** Phase 1 implementation on `cursor/seat-scoped-portal-phase1`. |
| 2026-07-21 | **Phase 1 merged** (PR #95). Phase 2 remains: analytics table, Access deep-links, graph “you” / hub context. |
| 2026-07-21 | **Pixel Bastion** brand (PR #96) lands on login + portal chrome; no auth/isolation change. |
