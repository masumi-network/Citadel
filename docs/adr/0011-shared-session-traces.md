# ADR 0011 — Shared Session Traces as a third storage layer

- **Status:** Accepted (2026-07-20 grill); **v1 shipped** in [PR #93](https://github.com/masumi-network/Citadel/pull/93) (`design/shared-session-index`)
- **Amends:** ADR-0007 (seat capture, promotion, and write policy), ADR-0003
  (seat/node/Central private memory)
- **Relates:** ADR-0009 (mesh read isolation — presence vs content),
  `CONTEXT.md` glossary terms: **Session Trace**, **Shared Session Trace**,
  **Compact Session Context**, **Seat Presence**, **Approved Capture Roots**,
  **Capture Policy**.
- **Design spec:** [`docs/superpowers/specs/2026-07-20-shared-session-index-design.md`](../superpowers/specs/2026-07-20-shared-session-index-design.md)

## Context

When one **Vault Member**'s agent works through a problem, the route it took —
what was tried, what failed, and why — is discarded at session end. The next
member's agent rediscovers the same dead ends from scratch. The expensive thing
to rediscover is the failed attempt, and nothing in the system records it in a
place teammates can search.

Citadel today stores **semantic** memory: source-linked facts and decisions,
curated in **Central**. What is missing is **episodic** memory: the route.

The distiller already exists (`kb/hooks/sync_session.py`, SessionEnd hook) and
writes a **Session Trace** to the member's own **Node**. The gap is entirely on
the sharing and retrieval side.

Two prior decisions constrain any solution:

- **ADR-0007:** seat-scoped writes are **Node-only on all channels**; **Central**
  is read-only for seat callers.
- **ADR-0003:** reads never cross seat **Nodes**.

A shared trace index appears to violate both. It does not, but the reason is
non-obvious enough that it must be written down.

## Decision

A **Shared Session Trace** is a **Session Trace** its author has volunteered to
the organization. Shared traces live in a dataset (`session-traces`) that is
**neither a Node nor Central**: readable by every **Seat**, writable by a seat
only for its own traces via explicit in-session MCP share, and never curated.

### 1. A third storage layer, deliberately

| Layer | Dataset | Write | Read | Curated |
|---|---|---|---|---|
| **Node** | `seat:{slug}` | own seat only | own seat only | no (light tier) |
| **Shared Session Traces** | `session-traces` | own seat, explicit MCP share | **all seats** | **no — never** |
| **Central** | `masumi-network` | governed sync + **Promotion** | all seats | yes (full tier) |

Traces are **consultable prior work, not Structured Knowledge**. They carry no
claim of being true, are never synthesized into canonical pages, are never
promoted to **Central**, and **never feed the daily improve / self-improvement
loop**. Routing unverified episodic records through the curated path would
poison the synthesis that makes **Central** trustworthy.

### 2. How this amends ADR-0007

ADR-0007's "Node-only on all channels" becomes:

> Node-only, **except** content the seat explicitly volunteers as a
> **Shared Session Trace** via `citadel_share_session`.

The invariant ADR-0007 was actually protecting survives intact: **no
involuntary seat write leaves the Node.** **Central** remains strictly read-only
for seat callers.

### 3. How this relates to ADR-0003

"Reads never cross seat **Nodes**" holds **literally and without exception**. A
**Shared Session Trace** is a *copy* written to a separate dataset; no seat ever
reads another seat's **Node**.

### 4. v1 share surface and consent

| v1 | In Approved Capture Root | Outside root |
|---|---|---|
| **`citadel_share_session` (MCP, user-approved)** | shares | **refused** |
| **SessionEnd auto-share** | **off** | off |
| **`CaptureRoot.share_traces`** | **deferred** (after `citadel unshare`) | — |

Server-side root check on `cwd` for every share.

### 5. Compact Session Context and distillation

Share uploads **Compact Session Context** — a structured **Session Trace**
record, not raw transcript:

1. Client: `distill_trace()` + `redact_commands()` (reuse SessionEnd logic)
2. Server: LLM dead-end distillation **only when client distill captured
   tool-error pairs**
3. Dual-write: seat **Node** (light, deterministic) + `session-traces` (shared)
4. **Deferred + coalesced cognify** (~5–15 min) — not inline before MCP returns

Private **Node** memory is never enriched.

### 6. Retrieval (v1)

Extend default **`citadel_search`** scope to include `session-traces`. Results
are **split** (`central` vs `session_traces`) with **reference-only** trust
demotion, `author_seat`, and age on every trace hit.

**`citadel_prior_work`** (overlap-ranked lookup) is **v1.1**, not v1.

Read scope is **org-wide** for traces, consistent with whole-vault **Access
Tokens** (department-scoped access was considered and resolved against).

### 7. Retraction and retention

- **`citadel unshare <trace-id>`:** soft retract (hidden from search); **Node**
  copy untouched; per-trace only
- **Admin hard-delete:** audited removal from `session-traces`
- **TTL ~90 days:** prune expired traces
- Automatic standing consent (`share_traces=true`) ships **after** unshare

### 8. Seat Presence and Central improve loop

**Seat Presence** unchanged — shared traces are disclosure by choice, not
involuntary **Node** leakage.

Daily Citadel improve / self-improvement runs on **Central Structured Knowledge
only**, never on `session-traces`.

## Considered Options

- **Route traces through the Promotion queue.** Rejected: approval queue dies from
  neglect at session volume; promotion gates org truth; traces make no truth
  claim.

- **Put traces in Central.** Rejected: would synthesize unverified episodic
  records into canonical pages; reopens seat→Central write path.

- **Share every Session Trace by default.** Rejected: reverses personal-by-default
  invariant.

- **Auto-synthesize traces into Central via a curator LLM.** Rejected at grill:
  risks polluting org context; traces stay separate; agents opt in via search.

- **Volume-only store without cognify.** Considered for cost; rejected at grill:
  teammates discover traces via **`citadel_search`**; cognify on explicit share
  only, with defer + coalesce to protect Railway budget.

- **Keep traces Node-private.** Rejected: status quo; distiller ships Node traces
  and none are reachable cross-seat.

## Consequences

- **+** Episodic memory becomes reusable across the org without touching
  **Central**'s curation guarantees or the **Node** isolation boundary.
- **+** Teammates use existing **`citadel_search`** habit; trust demotion prevents
  trace hits being read as org truth.
- **+** Explicit share-only + gated LLM + coalesced cognify keeps Railway cost
  bounded.
- **−** A third dataset class in a codebase whose routers assume a Node/Central
  dichotomy — latent bugs until audited.
- **−** Cognify contention on the single-writer Kuzu lock (#47) if share volume
  exceeds explicit-only assumptions.
- **− Accepted risk:** cross-seat prompt injection is **contained, not
  prevented** (typed fields, split results, reference-only demotion, attribution).

## Resolved (2026-07-20 grill)

| Question | Resolution |
|---|---|
| Retention | TTL ~90 days; prune job TBD |
| Retraction | Soft unshare + admin hard-delete; blocks standing auto-share until shipped |
| Cognify timing | Deferred + coalesced (~5–15 min) |
| Central boundary | Never auto-update Central or improve loop from traces |
| Retrieval v1 | Extended `citadel_search`; `citadel_prior_work` → v1.1 |

## Open (v1.1 / tuning)

- Exact cognify coalescing window from measured seat volume (v1 uses per-share defer + coalesce over dual-write targets, Linear-sync pattern)
- `citadel unshare`, TTL enforcement, and `CaptureRoot.share_traces` standing consent
- `citadel_prior_work` overlap-ranked retrieval
- Hard delete maturity in Cognee vs AccessStore retraction overlay
- Injection hardening beyond structured fields (M2 promotion-gate audit item)
