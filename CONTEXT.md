# Citadel

Citadel is an organization knowledge context for shared, source-linked company memory. This language keeps product concepts distinct from storage, implementation, and integration mechanisms.

## Language

**Organization Vault**:
A cloud-hosted, access-controlled shared memory layer for humans and agents.
_Avoid_: knowledge base, database, company Obsidian

**Source Material**:
Raw company material that may be used to produce structured knowledge.
_Avoid_: knowledge, truth, memory

**Source Snapshot**:
The retained evidence or source pointer used to reproduce what the **Organization Vault** learned from **Source Material**. **Structured Knowledge** source links resolve to a **Source Snapshot**. Two forms: the **v1 minimal form** is a stable source pointer (connector id + URL + checked-at, e.g. a GitHub digest id, a **Node** capture id, a Linear issue) that already exists on the ingest paths; the **target full form** additionally retains the source evidence for reprocessing (a later plan). A page's *cross-references* to other **Structured Knowledge** resolve against the page set, not against a **Source Snapshot**.
_Avoid_: permanent dump, backup, index record

**Vault Backup Mirror**:
A secondary synced copy of vault evidence and history used for recovery, audit, and rebuilds.
_Avoid_: source of truth, runtime store, live index

**Structured Knowledge**:
Source-linked company knowledge that has been organized into explicit concepts, relationships, and context. It is the **durable source of truth** the vault owns and retains directly; the **Knowledge Index** and **Knowledge Mesh** are rebuildable projections of it, and the retrieval engine that produces them is replaceable.
_Avoid_: raw data, unprocessed sync, dump, retrieval-engine-owned

**Knowledge Index**:
A searchable organization of **Structured Knowledge** for fast retrieval.
_Avoid_: database, file store, raw storage

**Knowledge Mesh**:
A relationship map that connects **Structured Knowledge** by source, concept, and provenance.
_Avoid_: decorative graph, chat history, raw sync map, activity view

**Vault Activity**:
A live, ephemeral projection of vault operations — source syncs, searches, ingests, index updates — surfaced live on the **Operations Dashboard** and the dev CLI (`citadel activity`, `--watch`). Operational signal only; it is not **Structured Knowledge** and not the **Knowledge Mesh**, and it resets with the service. Read scope follows isolation: a **Vault Member** sees their own **Node**'s activity with content (what they captured), while the **global/org broadcast** of other seats carries **Seat Presence** only — counts, timing, and seat slug, never **Node** content.
_Avoid_: knowledge mesh, audit log, chat history, cross-seat content feed

**Learning Process**:
The governed transformation of **Source Material** into **Structured Knowledge**.
_Avoid_: self-learning, magic sync, auto-truth

**Tiered Ingestion**:
Content receives processing proportional to the claim it makes. Three tiers: **light** — raw seat-**Node** agent memory, indexed only, never enriched, so private working memory never reaches an external model; **shared** — a **Shared Session Trace**, enriched (the volunteered route is distilled into approaches and dead ends) but never synthesized, because it is consultable prior work and makes no claim of being true; **full** — org-bound syncs and **Promotion**, which receive security review, enrichment, and structuring. Canonical **Structured Knowledge** synthesis — per-topic pages maintained in place — belongs to the full tier alone: it runs on the governed **Central** path, never on light-tier **Node** captures and never on shared traces. Synthesized knowledge is a **Central** benefit; enrichment is the price of volunteering content to the org.
_Avoid_: same pipeline for all content, skip processing, full enrichment everywhere, synthesize on every seat capture, enrich private node memory

**Vault Member**:
A human participant who has permission to access an **Organization Vault**.
_Avoid_: user, teammate, account

**Seat**:
A licensed team member slot; equals one **Principal** (one human, one seat). Created by admin before **Tokens** are issued.
_Avoid_: user account, shared login, license bundle

**Node**:
A seat's private mini knowledge base; logically isolated storage for that seat's agent memory. **Nodes** do not collide; seats cannot read each other's **Nodes**.
_Avoid_: organization vault, central, shared memory

**Central**:
The organization-wide shared knowledge base. Distinct from any seat **Node**.
_Avoid_: seat node, personal vault, private agent memory

**Seat Presence**:
The org-visible operational footprint of a **Seat**: it exists, its activity level, sync recency, contribution counts, and promotion-queue depth. Visible to every **Vault Member**; never includes **Node** content disclosed *involuntarily* (documents, session titles, text, or concepts extracted only from that **Node**). A **Vault Member** may deliberately volunteer specific **Node** content as a **Shared Session Trace**; disclosure by choice is not **Seat Presence** and is governed separately.
_Avoid_: activity feed, people report, session list, surveillance

**Session Trace**:
A structured record of how a **Seat**'s agent session approached a problem: the task, the approaches tried, which of them were dead ends, and the files touched. Not the conversation itself — a distilled, typed record of the route taken. Light-tier **Node** content, private to its **Seat** by default.
_Avoid_: transcript, chat log, conversation history, raw session

**Shared Session Trace**:
A **Session Trace** a **Vault Member** has volunteered to the organization so other **Seats**' agents can consult it instead of rediscovering the same route. Shared by an explicit in-session MCP act (`citadel_share_session`), never automatically in v1. Stored in the `session-traces` dataset (third layer — not a **Node**, not **Central**), cognified on share with deferred coalescing, discoverable via default **`citadel_search`** with reference-only trust demotion. Carries its author **Seat** so it can be followed up on and so bad guidance is attributable. It is consultable prior work, not **Structured Knowledge**: it is never synthesized, never promoted to **Central**, never feeds the daily improve loop, and carries no claim of being true.
_Avoid_: structured knowledge, central, vault truth, promotion, activity feed, auto-share on SessionEnd

**Compact Session Context**:
The structured **Session Trace** record produced by client distill and redaction (not the raw transcript) that is uploaded when a member shares a session.
_Avoid_: transcript, chat log, raw session dump

**Token**:
The credential a **Seat** uses to access their **Node** (and **Central** per read rules). Not the storage boundary — the **Node** is.
_Avoid_: node, storage scope, MCP key

**Agent Identity**:
A non-human actor that uses agent messaging for communication and may access an **Organization Vault**.
_Avoid_: bot, autonomous agent, service user

**Agent Messenger**:
The communication layer used by **Agent Identities** to exchange messages with each other.
_Avoid_: vault chat, shared memory, organization vault, MCP

**Access Token**:
A revocable credential that lets a **Vault Member** or **Agent Identity** access an **Organization Vault**.
_Avoid_: MCP key, shared password, API secret

**Access Role**:
A named permission level that determines what a **Vault Member** or **Agent Identity** may do in an **Organization Vault**.
_Avoid_: read me access, write access key

**Agent Action**:
A vault operation performed by an **Agent Identity**.
_Avoid_: background magic, unrestricted automation

**Vault Contribution**:
Structured or source-linked knowledge added to an **Organization Vault** by an actor with write permission.
_Avoid_: chat message, random update, raw agent conversation

**Promotion**:
A curated copy of content from a seat **Node** into **Central**. Dual-write: the original stays in the **Node**; the copy goes to **Central**. Runs on a schedule (operator cron) and via an LLM **Promotion Agent** that cross-references the note against org projects and **Structured Knowledge** already in **Central** — not a direct seat write.
_Avoid_: move, delete original, automatic merge, direct seat ingest to Central

**Promotion Agent**:
The governed job that decides whether seat **Node** content belongs in **Central**. Every candidate passes **secret scan** (block on high/critical) and **LLM classification** (relevance + sensitivity; always required, even when a structured repo match succeeds). It compares candidate notes against repos in the **GitHub organization repo list** (masumi-network) and against **Structured Knowledge** already in **Central**. Content that clearly extends work whose repo is already in the org (or already represented in **Central**) may be promoted automatically when **Capture Root Tags** allow (`org-work` only for capture-root content; `personal` and custom tags never auto-promote). Content that names a repo or initiative **not** in the masumi org repo list nor in **Central** is a **New Org Project** and requires **Promotion Approval** before syncing to **Central**. Notes with **no repo reference** auto-promote only when **Central** already contains a strong match; otherwise they stay on the **Node** (no approval queue). Runs on a 6h operator cron. **On demand:** each **Vault Member** may trigger a pass for their own **Node**; admins may trigger for any seat — via the **Operations Dashboard** or CLI.
_Avoid_: magic merge, silent upload, MCP self-promote, GitHub-only matching, promote-only-on-daily-sync

**New Org Project**:
A project, repo, product, or initiative named in a seat **Node** whose repository is **not** in the masumi **GitHub organization repo list** and is **not** yet represented in **Central** org context. Promotion to **Central** waits for **Promotion Approval**. Known org work must tie to a repo already in the masumi org (or existing **Central** representation) — not an external or personal remote.
_Avoid_: personal side project auto-share, new folder name, any novel string, external-org repo auto-promote

**Promotion Approval**:
The **Vault Member** response when the **Promotion Agent** automatically proposes promoting a **New Org Project** note from their **Node** into **Central**. **Vault Members do not add items to the queue** — autonomous capture (hooks, `citadel capture`, MCP) fills the **Node**; the agent queues pending items when rules match. **Approve** = allow this one note into **Central**. **Reject** = keep it on the **Node** only; rejection **sticks** — the same note is not re-queued on later cron passes unless its content changes. Each approval is **one-shot**; later notes from the same external project still need approval or masumi org repo membership. Every promote and approve/reject is **source-linked and auditable** (actor, seat, timestamp, repo hints, preview). v1: audit events plus promotion metadata on the **Central** copy; target: full **Source Snapshot** back-link. Surfaces: **Operations Dashboard**, MCP (approve/reject only after explicit user confirmation), and CLI (`citadel promotion …`, `--json`). Each **Vault Member** sees their own queue; admins see all seats and may approve on a member’s behalf (delegate flagged in audit).
_Avoid_: auto-approve novel work, silent admin override, chat-only approval, standing bypass for external repos

**Operations Dashboard**:
The Citadel web UI for operators and **Vault Members** to monitor vault health, seat activity (as **Seat Presence**), memory and usage, **Promotion Approval**, and **Access** — not the primary dev write surface (MCP and autonomous capture feed the **Node**). A member's own **Node** content (recent sessions, drill-down) is visible to that member; other seats show **Seat Presence** only; admins may drill into content for support and audit.
_Avoid_: main editor, Obsidian replacement, admin-only console, per-person activity report

**Seat Node Write Policy**:
A **Seat** or its **Agent Identities** may write only to that seat's **Node**. **Central** is read-only for seat-scoped callers; **Central** receives **Structured Knowledge** only through governed upstream paths (org source sync, **Promotion**, service-account **Vault Contributions**, operator/admin jobs).
_Avoid_: tag your way into Central, shared personal dump, MCP bypass

**Capture Root Tags**:
Labels assigned to each **Approved Capture Root** during setup. Preset tags carry hard promotion rules for **capture-root** content (git push / `citadel capture`): `personal` never auto-promotes to **Central**; only `org-work` roots may auto-promote, and only when the **Promotion Agent** finds a masumi org repo or **Central** match plus LLM safety checks. Custom tags are labels for search and context only — capture from custom-tagged roots never auto-promotes. **Non-capture** writes (MCP ingest, session hooks) are not gated by root tags — only by **Promotion Agent** reference checks and relevance classification.
_Avoid_: tag into Central, org-ready on capture, silent promotion override

**Capture Policy**:
The rules attached to **Approved Capture Roots** — which paths, file types, and events may be ingested, and which must never be (secrets, `.env`, credentials, raw logs). Applies to autonomous capture; does not override the server **Security Finding** gate. v1 triggers: **git push** inside an approved root and **manual CLI capture** on demand (`citadel capture`); no file watcher or local schedule in v1. **Hybrid storage:** org-wide deny/template rules live on the server per **Seat**; **Approved Capture Roots** (local paths) are chosen on each machine during setup. **Governance:** operators set an org **Capture Policy** baseline (admin); a **Vault Member** may add stricter local rules but may not remove org denies.
_Avoid_: ingest everything, trust the agent, skip secret scan, ingest on every save, one global laptop path, opt out of secret excludes

**Explicit Capture Approval**:
Per-write confirmation required when ingest is outside **Approved Capture Roots** — MCP client tool approval plus agent yes/no before `citadel_ingest`.
_Avoid_: silent MCP write, auto-ingest whole chat

**Approved Capture Roots**:
Filesystem directories a **Vault Member** opts in during Citadel setup. Content from these roots may sync to the seat **Node** without per-write MCP prompts, subject to **Capture Policy** and **Capture Root Tags**. Any path may be approved; reaching **Central** still requires **Promotion**.
_Avoid_: whole home directory, silent org-wide upload, org repos only

**Automatic + Curated Sync**:
Default agent memory stays in the seat **Node** via autonomous capture (git push, session hooks). **Central** is updated by operator cron and governed org pipelines — not by direct seat writes. Integration sources (e.g. Linear) sync org-wide into **Central**; seat-relevant subsets (e.g. issues assigned to that seat-holder) are also **Mirrored** into that seat's **Node**.
_Avoid_: full vault mirror, seat-to-seat sync, chat log sync, seat writes to Central

**Seat-Scoped Mirror**:
A filtered copy of **Central** content relevant to one **Seat** (e.g. Linear issues assigned to that seat-holder) stored in that seat's **Node** so personal agent queries stay local without re-querying **Central**.
_Avoid_: full Central duplicate, seat-to-seat sync, personal vault

**Repository Daily Update**:
A source-linked summary of meaningful changes in one repository over a day.
_Avoid_: employee report, department update, surveillance

**Meaningful Source Change**:
A source-linked change that affects work visibility, product behavior, architecture, reliability, decisions, blockers, or active repository momentum.
_Avoid_: raw activity, commit spam, productivity tracking

**Organization Update Digest**:
A source-linked summary of meaningful changes, features, decisions, and ongoing work produced from the **Organization Vault**.
_Avoid_: chat transcript, people report, raw activity feed, surveillance

**Security Finding**:
A source-linked, redacted report of a potential secret exposure or high-risk issue that requires team attention.
_Avoid_: secret dump, raw match, vague warning

**Knowledge Conflict**:
A visible disagreement between pieces of **Structured Knowledge** or their supporting **Source Snapshots**. Because **Structured Knowledge** is maintained as canonical per-topic knowledge revised in place, a revision that *contradicts* the existing page raises a **Knowledge Conflict** and keeps both sides visible instead of silently overwriting; a non-contradicting revision just updates the page. Prior versions stay recoverable through the **Vault Backup Mirror**.
_Avoid_: merge, overwrite, silent correction

**Knowledge Maturity**:
How settled a piece of **Structured Knowledge** is, surfaced to readers as a trust signal — `seed` (a single source, or an open **Knowledge Conflict**), `growing` (a few corroborating sources), `stable` (multiple corroborating sources, no open conflict). It reflects corroboration and contradiction state; it is **not** a **Promotion** gate — **Promotion** keeps its own gates (secret scan, org reference, relevance), and **Knowledge Maturity** simply tells a reader how corroborated a **Central** answer is.
_Avoid_: approval status, promotion gate, workflow stage, review state

## Relationships

- An **Organization Vault** is accessed by humans and agents.
- An **Organization Vault** contains **Structured Knowledge**.
- **Source Material** becomes useful to the **Organization Vault** through a **Learning Process**.
- A **Source Snapshot** preserves enough evidence to cite, audit, or reprocess **Structured Knowledge**.
- A **Vault Backup Mirror** keeps a redundant copy of vault evidence without serving live retrieval.
- A **Learning Process** produces **Structured Knowledge**, a **Knowledge Index**, and a **Knowledge Mesh**.
- A **Vault Member** or **Agent Identity** uses an **Access Token** to access an **Organization Vault**.
- An **Access Role** limits what a **Vault Member** or **Agent Identity** may read or change.
- An **Agent Action** is constrained by the **Agent Identity**'s **Access Role**.
- An **Agent Identity** communicates with other **Agent Identities** through the **Agent Messenger**.
- A **Vault Member** or **Agent Identity** with write permission may create a **Vault Contribution**.
- **Agent Messenger** messages do not become **Vault Contributions** unless an actor with write permission adds them.
- A **Repository Daily Update** summarizes repository changes, not individual people.
- A **Repository Daily Update** is composed from **Meaningful Source Changes** in one repository.
- An **Organization Update Digest** can include one or more **Repository Daily Updates** plus other source-linked changes from the **Organization Vault**.
- An **Organization Update Digest** highlights **Meaningful Source Changes** across repositories and other approved sources over a defined time window.
- An **Organization Update Digest** can be delivered to an external communication surface without becoming **Source Material** itself.
- A **Security Finding** is separate from an **Organization Update Digest** and should never expose secret values in external communication surfaces.
- A **Knowledge Conflict** should be shown when source-linked knowledge disagrees.
- A **Seat** is one human **Principal** that may hold several **Tokens**; **Agent Identities** acting for that human are separate principals that may be granted access to the seat's **Node**.
- A caller is treated as holding a **Node** when a seat **Node** is in its access scope — **Seat Node Write Policy** applies: writes land in that **Node** only; **Central** is read-only for that caller.
- **Central** gains new **Structured Knowledge** from org source sync, **Promotion** (cron + **Promotion Agent**), service-account **Vault Contributions**, and operator jobs — not from direct seat-scoped writes.
- When **Promotion** finds content that extends known org work (repo in the masumi org list or strong **Central** match), it may copy to **Central** without **Vault Member** action only after secret scan and LLM relevance checks pass; structured repo lists decide *whether* something is org work, not whether it is safe to share.
- When **Promotion** detects a **New Org Project**, it must obtain **Promotion Approval** (dashboard, MCP with user confirm, or CLI) before syncing to **Central**; **Vault Members** respond to agent-proposed queue items — they do not add items manually.
- **CLI** (`citadel onboard`, `setup`, `capture`, `status`) handles setup and Node capture over HTTP; **MCP** handles in-session search, deliberate ingest, and promotion approve/reject — hooks do not use MCP.
- Integration sources (e.g. Linear) sync org-wide into **Central**; **Seat-Scoped Mirrors** copy assignee-relevant subsets into each seat's **Node** (e.g. John's assigned Linear issues appear in John's **Node** and in **Central**).
- A **Session Trace** is private to its **Seat**'s **Node** by default; a **Shared Session Trace** is a volunteered copy in `session-traces` readable by every **Seat** via **`citadel_search`**, never **Central** and never another seat's **Node**.
- **Shared Session Traces** do not become **Structured Knowledge** and do not feed the daily improve loop; **Central** stays curated org truth.

## Autonomous sync (Phase 2)

Background capture keeps each seat's **Node** and org **Central** current with no
per-capture dev steps. All layers are **fail-silent** — outages never block git
push, session close, or agent work.

| Layer | Trigger | Destination | Who runs it |
|---|---|---|---|
| Git pre-push hook | every `git push` in an **Approved Capture Root** | seat **Node** | dev (once per clone) |
| Manual CLI capture | **Vault Member** runs `citadel capture` on approved roots | seat **Node** | dev (on demand) |
| SessionEnd hook (Claude Code) | session close | seat **Node** | dev (optional) |
| Explicit MCP / agent ingest | user-approved write outside auto-capture | seat **Node** | **Vault Member** + agent (MCP; not hooks) |
| Railway `learning-agent` cron | daily schedule | **Central** | operator |
| Railway `linear-sync` cron | scheduled | **Central** + **Seat-Scoped Mirror** | operator |
| Railway **Promotion Agent** cron | every 6h + on demand | seat **Node** → **Central** (governed) | cron: operator; on demand: **Vault Member** (own seat) or admin (any seat) via dashboard or CLI |

**Member day-to-day:** update the **Node** only (hooks, `citadel capture`, optional MCP ingest). **Central** updates automatically when the **Promotion Agent** rules pass, or when the member **approves** a **New Org Project** proposal in their queue.

Install once: `citadel onboard` (idempotent). It writes a self-contained git pre-push hook (`python -m kb.hooks.sync_push`), SessionEnd/SessionStart hooks (`kb.hooks.sync_session` / `kb.hooks.sync_start`), the proactive agent policy (`AGENTS.md` plus native rules for Cursor, Windsurf, and Gemini when detected), project `.mcp.json`, and detected client MCP configs (Claude Code, Cursor, Codex, Gemini, Windsurf). On macOS it also runs `launchctl setenv` so Dock-launched Cursor can see `CITADEL_MCP_ACCESS_TOKEN` until logout.
Register **Approved Capture Roots** locally, assign **Capture Root Tags**, and merge the server **Capture Policy** template during seat setup (Citadel CLI wizard).

**Agent sync policy:** rely on hooks + cron for allowlisted org repos. Agents read via `citadel_search`,
`citadel_linear_my_issues`, and `citadel_linear_search`. Do **not** trigger
admin sync (`POST /api/linear-sync/run`, learning-agent runs) unless the user
explicitly asks for an immediate refresh.

## Graph views (Phase 2)

The **Operations Dashboard** renders two canvases over org memory, both showing
a **universal org view** — every seat and **Central** (`masumi-network`) on one
force-directed graph, **Central** pinned at the centre as the largest hub, with
a depth slider (0–3 hops) filtering the neighbourhood around the selected node.
There is no All/My Node/Central scope toggle — org memory is one connected view.

- **Vault Activity** — the live operations projection: seat vaults tiered by
  activity, optional Central↔seat spokes, source/sync/search events as they
  happen. Restart-transient by design.
- **Knowledge Mesh** — the source-linked relationship map itself: documents,
  concepts, and the **Seat Presence** hubs their content belongs to.

**Presence vs content (2026-07-13):** "universal" means every seat is *visible*,
not that every seat's memory is *readable*. Other seats appear as **presence** —
the seat exists, with activity level and contribution counts. **Structured
Knowledge** content (documents, their text, extracted concepts) follows the same
read scope as search: **Central** plus the caller's own **Node**, never another
seat's **Node** content (ADR-0003 read isolation). Admin/operator callers see all
content for support and audit. This applies to every content surface of the
mesh, including per-item drill-down.

## Example Dialogue

> **Dev:** "Should this GitHub repository be added to the database?"
> **Domain expert:** "Add it as **Source Material**. It becomes **Structured Knowledge** only after the **Learning Process** extracts useful context from it."
>
> **Dev:** "Can we search new material immediately?"
> **Domain expert:** "Only after the **Learning Process** has made it part of the **Knowledge Index** and **Knowledge Mesh**."
>
> **Dev:** "Do we keep the raw material forever?"
> **Domain expert:** "No. Keep a **Source Snapshot** when it is needed for citation, audit, or reprocessing; keep the **Knowledge Index** and **Knowledge Mesh** rebuildable."
>
> **Dev:** "Should the backup repository be the live knowledge store?"
> **Domain expert:** "No. The **Vault Backup Mirror** is redundant storage for recovery and rebuilds, not the runtime vault."
>
> **Dev:** "Should every teammate get an MCP key?"
> **Domain expert:** "Give each **Vault Member** or **Agent Identity** its own **Access Token** with the right **Access Role**."
>
> **Dev:** "Do agents talk to each other through the Organization Vault?"
> **Domain expert:** "No. **Agent Identities** communicate through the **Agent Messenger** and use the **Organization Vault** as shared memory."
>
> **Dev:** "Can an agent save something it discussed with another agent?"
> **Domain expert:** "Yes, if its **Access Role** allows writes, it can create a **Vault Contribution**."
>
> **Dev:** "What can an agent do without approval?"
> **Domain expert:** "Reader agents can read and search. Writer agents can add contributions, submit feedback, and provide updates."
>
> **Dev:** "Should daily updates say what each person did?"
> **Domain expert:** "No. A **Repository Daily Update** summarizes meaningful changes in one repository."
>
> **Dev:** "What belongs in a repository daily update?"
> **Domain expert:** "Meaningful commits, pull requests, and repository changes."
>
> **Dev:** "Should the digest list every commit from the last day?"
> **Domain expert:** "No. It should highlight **Meaningful Source Changes**, especially open pull requests, merged work, active repositories, blockers, and the vault's source-linked interpretation of the last 24 hours."
>
> **Dev:** "Should the Google Chat bot summarize what everyone said in chat?"
> **Domain expert:** "No. It should deliver an **Organization Update Digest** from source-linked vault context, not turn chat transcripts into vault memory."
>
> **Dev:** "If a future PR check finds an API key, should the digest post it?"
> **Domain expert:** "No. Create a **Security Finding** with redacted details and link to the GitHub check; never post the secret value."
>
> **Dev:** "What if a note disagrees with a newer repository change?"
> **Domain expert:** "Prefer the newer source-linked repository truth for code behavior, but keep a visible **Knowledge Conflict**."
>
> **Dev:** "Do members approve what goes into their Node?"
> **Domain expert:** "No. Hooks, `citadel capture`, and approved MCP ingest write the **Node** automatically. Members only interact with **Promotion Approval** when the **Promotion Agent** proposes moving a **New Org Project** note to **Central**."
>
> **Dev:** "Can a member reject something they never added?"
> **Domain expert:** "Yes — **Reject** means 'do not promote this agent-proposed note to **Central**.' The note stays on their **Node**. Rejection sticks; the same note is not re-queued unless its content changes."

## Flagged Ambiguities

- "knowledge base" and "database" were used for the same product concept; resolved: the product concept is **Organization Vault**.
- "raw data retention" was unclear; resolved: keep **Source Snapshots** for citation, audit, and reprocessing, while treating indexes and mesh structures as derived.
- "knowledge sync repository" sounded like a source of truth; resolved: the canonical term is **Vault Backup Mirror**, a redundant copy for recovery, audit, and rebuilds.
- "self-learning" was used to mean automatic structuring of raw inputs; resolved: the domain term is **Learning Process**.
- "indexed mesh" was used to describe retrieval; resolved: **Knowledge Index** supports search, while **Knowledge Mesh** represents relationships.
- "MCP key" and "access token" were used for credentials; resolved: the domain term is **Access Token**.
- "department-scoped access" was considered for the first version; resolved: initial **Access Tokens** grant whole-vault access constrained by **Access Role**.
- "agent action approval" was broad; resolved: read/search is open to reader agents, writer agents may contribute, submit feedback, and provide updates, while sensitive admin actions stay gated.
- "agent identity" was used as if it only meant vault access; resolved: **Agent Identities** communicate through the **Agent Messenger** and access the vault separately.
- "agent messenger" was used near shared memory; resolved: **Agent Messenger** is the communication layer, while the **Organization Vault** is the shared memory layer.
- "agent messages" were discussed as possible vault content; resolved: messages stay in **Agent Messenger** unless an actor with write permission creates a **Vault Contribution**.
- "daily update" was initially broad; resolved: daily updates are **Repository Daily Updates**, not people or department reports.
- "repository daily update detail" was too broad; resolved: include meaningful commits, pull requests, and repository changes only.
- "meaningful change" was vague; resolved: **Meaningful Source Changes** are source-linked changes around pull requests, merged work, repository momentum, blockers, decisions, reliability, architecture, or product behavior.
- "org update" was broader than repository changes; resolved: an **Organization Update Digest** summarizes meaningful source-linked changes, features, decisions, and ongoing work from the **Organization Vault**.
- "secret reporting" was initially mixed into the update digest; resolved: potential secrets and high-risk issues are **Security Findings**, redacted and handled separately from daily digests.
- "conflicting knowledge" was unresolved; resolved: prefer newer source-linked repository truth for code behavior, while marking **Knowledge Conflicts** visibly.
- "is a caller a Seat?" was conflated with principal identity; resolved: for **Central** curation the test is whether a seat **Node** is in the caller's access scope, which covers both the human's **Tokens** and their **Agent Identities** — agents are not the human **Principal** and so carry no seat marker of their own.
- "member adds then rejects promotion items" was confused with queue ownership; resolved: the **Promotion Agent** queues **New Org Project** proposals automatically; **Promotion Approval** is the member's approve/reject response.
- "org-ready tag promotes seat MCP writes to Central" (ADR-0003 era); resolved: **Seat Node Write Policy** — seat writes stay on the **Node**; **Central** only via **Promotion Agent**, org sync, or service accounts (ADR-0007).
- "who gates promotion to Central?" (2026-06-27 grill); resolved: known masumi-org work auto-promotes after secret scan + LLM; **New Org Project** requires **Vault Member** **Promotion Approval** (admin may delegate with audit); admin governs org repos and operator cron, not every member note.
- "universal org view" (2026-07-13 grill) was being read as every seat's content on one canvas; resolved: universal means every seat's *presence* (hub, activity, counts) is visible to all **Vault Members**, while **Structured Knowledge** content stays scoped to **Central** plus the caller's own **Node** — the **Knowledge Mesh** view and its drill-down enforce the same read isolation as search (ADR-0003).
- "who owns the source of truth — the retrieval engine or the vault?" (2026-07-15 grill) was inverted in the implementation: the retrieval engine held the only durable copy. Resolved: **Structured Knowledge** is the durable, first-class source of truth the vault owns, held in the runtime vault and **synced to the Vault Backup Mirror** for recovery; the **Knowledge Index** and **Knowledge Mesh** are rebuildable projections produced by a replaceable retrieval engine. This is per-dataset — each **Node** and **Central** own their own **Structured Knowledge**, so read isolation (ADR-0009) holds by construction.
- "what do a Structured Knowledge page's source links point to?" (2026-07-15 grill); resolved: they resolve to a **Source Snapshot**, whose **v1 form is the stable connector pointer** that already exists on ingest (GitHub digest id, **Node** capture id, Linear issue) — no new evidence store is a prerequisite of durable **Structured Knowledge**. Retained-evidence **Source Snapshot** (the full form) is deferred to the future plan. A page's cross-references to other pages resolve against the page set, not a **Source Snapshot**.
- "dev-side visibility / global activity broadcast" (2026-07-16 grill) risked leaking cross-seat content; resolved: **Vault Activity** is surfaced on the dev CLI (`citadel activity`), and the **global/org broadcast carries Seat Presence only** — counts, timing, seat slug, content stripped. A **Vault Member** sees their own **Node**'s activity with content; other seats appear as **Seat Presence** (ADR-0009). The fail-silent capture hooks may leave a legible one-line receipt but never block, never surface the **Token**, and never show another seat's **Node** content.
- "Shared Session Trace v1" (2026-07-20 grill): resolved end-to-end model — **explicit in-session MCP share only** (`citadel_share_session`); SessionEnd still writes private **Node** traces (light tier). Shared **Compact Session Context** dual-writes to `session-traces` (shared tier): client distill + redact, server LLM dead-end distillation **only when tool-error pairs exist**, then **deferred + coalesced cognify** (~5–15 min). Default **`citadel_search`** includes `session-traces` with split results and **reference-only** demotion; **`citadel_prior_work` deferred to v1.1**. **Never auto-update Central** or the daily improve loop from traces. **Retraction:** soft **`citadel unshare <trace-id>`** (author) + admin hard-delete; **TTL ~90 days**; automatic `share_traces=true` on **Approved Capture Roots** deferred until unshare ships.
- "can the evolve pipeline run in its own process?" (2026-07-29, #88); resolved: **no**. The retrieval engine opens the **Knowledge Mesh** graph with an exclusive OS file lock held for the lifetime of whichever process opens it, and there is no working read-only mode. A second process cannot open the graph at all, for reads or writes, so **one process owns the graph** and every graph-touching stage runs inside it (ADR-0015). This is why the evolve subprocess could never succeed, why the loop-binding workaround became unnecessary, why a second uvicorn worker is not a configuration option, and why a separate evolve cron service sharing `/data` is wrong even though it looks reasonable. The constraint belongs to the engine, not to Citadel: it is the sharpest argument for ADR-0010, since owning **Structured Knowledge** is what would let the engine be swapped for one that permits concurrent openers.
- "does a green stage mean the work happened?" (2026-07-29); resolved: **no, and that gap is a recurring defect class rather than a set of unrelated bugs**. Six independent surfaces reported success for work that did not occur, in one day: an evolve pass exiting 0 with two dead stages, **Promotion** reporting zero failures while every seat's search died, the cognify canary passing on graph growth instead of retrievability, a scheduler task death swallowed by its done-callback, an unconfigured delivery gateway making both the contact endpoint and the **Organization Update Digest** do nothing, and a timed-out search recorded as a successful one. The shape is always the same: ordinary failure paths are handled carefully, while the "nothing was configured" and "everything failed" paths fall through to the success branch. A **Learning Process** stage, a delivery, or a canary must report what it actually did, and an empty result caused by failure must be distinguishable from an empty result caused by there being nothing to do.
