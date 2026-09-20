# Shared Session Traces — cross-agent route reuse

**Date:** 2026-07-20
**Status:** v1 implemented (2026-07-20); v1.1 items tracked below
**ADR:** [ADR-0011](../../adr/0011-shared-session-traces.md) — amends ADR-0007, relates ADR-0003 / ADR-0009
**Glossary:** `CONTEXT.md` — **Session Trace**, **Shared Session Trace**, **Compact Session Context**, amended **Seat Presence**, amended **Tiered Ingestion**

> Terminology note: an earlier draft called these "trajectories." The domain term
> is **Session Trace** (private) / **Shared Session Trace** (volunteered). The
> dataset is `session-traces`.

## Problem

A **Vault Member**'s AI coding session solves a problem. Another member starts
the same problem the next day and their agent rediscovers everything from
scratch — the same dead ends, the same wrong turns, the same tokens.

Citadel today stores **semantic** memory: source-linked facts and decisions in
**Central**. What is missing is **episodic** memory — what was tried, what
failed, and why. Teammates already onboard with **`citadel_search`** over their
**Node** + **Central**; other seats' private **Nodes** and hook auto-ingest are
not org-visible. The expensive thing to rediscover is the dead end, and nothing
records it in a place teammates can find.

## What exists today

| Component | File | State |
|---|---|---|
| SessionEnd distiller | `kb/hooks/sync_session.py` | Ships. Deterministic distill into task / outcome / files / decision-marker snippets. Writes to the seat **Node** only. |
| SessionStart injector | `kb/hooks/sync_start.py` | Ships. Proactive search guidance; may inject bounded repo-scoped workspace candidates, but does not push or share traces. |
| Share + distill | `kb/session_trace_distill.py`, `kb/session_trace.py`, `POST /api/share-session`, `citadel_share_session` | **Shipped (v1).** Client distill + redact; server LLM dead-end refinement when tool errors exist; dual-write to Node + `session-traces`. |
| Search | `citadel_search`, `POST /search` | **Shipped (v1).** Default scope: seat **Node** + **Central** + `session-traces`; split sections; reference-only trust on trace hits. |

Confirmed gaps:

- `resolve_write_targets` (`kb/server.py:1142`) routes every seat write to
  exactly one target — `seat:<slug>`, tier `light`. A teammate's trace is
  structurally unreachable.
- No trace matching of any kind. Ingest dedup is exact SHA-256
  (`kb/service.py:77`); `kb/lint.py` (near-duplicate cosine bands) is planned
  and does not exist.
- **The distiller is blind to dead ends.** `sync_session.py:207` inspects only
  `btype == "text"` on assistant entries. Failures live in `tool_result` blocks,
  which it never reads. It also keeps only the *last* assistant message as
  `Outcome:`.

## v1 design (grill-resolved)

### Architecture

A new shared dataset, `session-traces`, written under explicit consent and
readable by all seats. It sits **outside Central and outside every Node** — a
third storage layer. See ADR-0011 for why that is not a violation of ADR-0003 or
ADR-0007.

**Central boundary:** shared traces are **never** synthesized into **Structured
Knowledge**, **never** promoted to **Central**, and **never** fed to the daily
Citadel improve / self-improvement loop. Org truth stays curated; traces stay
consultable prior work.

```
WRITE (v1 — explicit MCP share only)
  agent + user approve → citadel_share_session(transcript_path, cwd)
    → client: distill_trace() + redact_commands()     compact session context
    → server: Approved Capture Root check on cwd
    → server: LLM dead-end distillation               only if tool-error pairs
    → dual-write:
        seat Node copy (light tier, unchanged)
        session-traces (shared tier, defer_cognify=true)
    → coalesced cognify (~5–15 min)                   searchable via citadel_search

READ (v1 — search, not a dedicated tool)
  agent → citadel_search(query)                        default scope: Node + Central + session-traces
    → split results: central | session_traces | node
    → trace hits: reference-only trust + author_seat + age
    → agent verifies before acting; Central hits stay org-authoritative

RETRACT (v1.1 path for standing consent)
  author → citadel unshare <trace-id>                  soft hide from search
  admin  → hard delete                                 audited
  TTL    → ~90 days                                    prune expired rows
```

SessionEnd hook continues to write private **Node** traces only. **No**
SessionEnd auto-share in v1. Automatic `share_traces=true` on **Approved Capture
Roots** is deferred until `citadel unshare` exists.

### Key decisions

1. **A third storage layer, not a Central lane.** **Central** is curated
   **Structured Knowledge**. Traces make no truth claim, so they get their own
   dataset. `resolve_write_targets` gains a branch for explicit share that does
   not pass through `guard_curated_central`.

2. **Dual-write, never move.** The seat **Node** keeps its copy regardless.
   Sharing adds a copy to `session-traces`; it never relocates personal memory.

3. **Device is source only.** Transcript + client distill stay on the machine.
   Upload is **Compact Session Context** (structured **Session Trace**), never
   raw transcript.

4. **Approved Capture Roots are the outer boundary.** Server-side root check on
   `cwd` for every share. Outside a root → refused with an actionable message.

   | v1 | In root | Not a root |
   |---|---|---|
   | **`citadel_share_session` (MCP)** | shares | **refused** |
   | **SessionEnd auto-share** | **off in v1** | off |

   `CaptureRoot.share_traces` (automatic standing consent) ships after retraction.

5. **Redact client-side, don't reject server-side.** `redact_commands()` runs
   before transport. Server secret scan remains defense in depth.

6. **Search, not push.** Agents discover traces via **`citadel_search`**.
   SessionStart may inject bounded repo-scoped workspace candidates, but it
   does not push or share traces. **`citadel_prior_work`** (overlap-ranked lookup)
   is **v1.1**.

7. **Resolution is a fact about an approach, never a verdict on a person.**
   Recorded per dead end, not stamped on the session or its author.

8. **Cost controls (Railway).** Explicit share only (low volume). **Deferred +
   coalesced cognify** (Linear-sync pattern). Server LLM dead-end distillation
   **only when client distill captured tool-error pairs** — not on clean sessions.

### Distillation — deterministic client, gated LLM server, shared only

Extend `distill_transcript` to read `tool_result` blocks and emit raw
`(tool_use, is_error, error_text)` pairs. Reuse the same logic from SessionEnd
and `citadel_share_session`.

| Tier | Enrichment | Synthesis | Cognify |
|---|---|---|---|
| Node capture (light) | no | no | no (v1) |
| **Shared Session Trace** | LLM dead-ends **if tool errors** | **no** | **yes, deferred + coalesced** |
| Central (full) | yes | yes | yes |

Private **Node** memory is never enriched. Shared traces are enriched but **never
synthesized**.

### Propagation

Push-on-write for the structured record; cognify is **deferred + coalesced**
(~5–15 min batch window, tune before 15-seat rollout). MCP share returns
immediately; searchability self-heals after the batch cognify.

**Contention:** all cognify passes through one Kuzu writer lock (#47). Explicit-
share-only volume + coalescing keeps traces from queueing ahead of GitHub/Linear
sync under normal use.

### Record schema

```python
@dataclass(frozen=True)
class SessionTrace:
    task: str                        # first user prompt, <=280
    approach: str                    # what finally worked, <=500
    dead_ends: tuple[DeadEnd, ...]   # <=6
    files: tuple[str, ...]           # <=40
    commands: tuple[str, ...]        # <=12, redacted, <=200 each
    repo: str
    branch: str
    author_seat: str
    created_at: str                  # ISO8601 UTC

@dataclass(frozen=True)
class DeadEnd:
    tried: str                                              # <=200
    failed_because: str                                     # <=200
    resolution: Literal["solved", "superseded", "dead_end"]
```

`created_at` lives in the record because search must express staleness.

### Retrieval interface (v1)

Extend **`citadel_search`** / `POST /search`:

- Default scope for seat tokens: **Node + Central + `session-traces`**
- Response sections: `central`, `session_traces`, `node` (when applicable)
- Every trace hit: `_citadel.trust: reference-only`, `author_seat`, age

Example trace hit shape:

```json
{
  "content": "Task: make evolve subprocess write to the graph\nDead end (dead_end): ran cognify in subprocess → Kuzu lock held by web process\nFiles: kb/evolve.py",
  "_citadel": {
    "trust": "reference-only",
    "author_seat": "priya",
    "created_at": "2026-07-11T14:30:00Z",
    "dataset": "session-traces"
  }
}
```

### Retrieval interface (v1.1)

```python
citadel_prior_work(task: str, files: list[str] = [], repo: str = "", limit: int = 3)
```

Overlap-ranked lookup over `session-traces` when generic search misses.

### Retraction and retention

| Mechanism | Behavior |
|---|---|
| **`citadel unshare <trace-id>`** | Soft retract — hidden from search; **Node** copy untouched; per-trace only |
| **Admin hard-delete** | Remove from `session-traces`; audited |
| **TTL ~90 days** | Prune expired traces (exact job TBD) |
| **`share_traces=true`** | Deferred until unshare ships |

Retraction metadata may overlay the trace index (AccessStore-family flags) until
hard Cognee delete matures.

### Injection defense

Cross-seat prompt injection is **contained, not prevented**: typed fields, split
search sections, reference-only trust demotion, always-displayed `author_seat`.
No LLM screening in v1. Accepted risk (ADR-0011).

## Failure modes

| Failure | Behavior |
|---|---|
| Redaction misses a secret | Server scan raises; share dropped. Loud in logs, nothing leaks. |
| Share outside Approved Capture Root | Refused server-side with actionable message. |
| MCP share without user approval | Client policy gate; server rejects unauthenticated writes. |
| `session-traces` empty | Search returns no trace section; not an error. |
| Server LLM distillation fails | Fall back to deterministic error pairs. |
| No tool errors in session | Skip LLM; deterministic trace only. |
| Cognify backlog | Added but not yet searchable. Self-heals after coalesced batch. |
| Poisoned trace | Contained by structure + attribution, not prevented. |

## Testing

1. `redact_commands` — table-driven secret scrubbing.
2. Redaction precedes transport — no raw secret in POST body.
3. `distill_trace` — fixture JSONL with failed-then-succeeded tool sequence.
4. **Cross-seat authz:** Seat A shares → Seat B sees in search. Seat A does not share → Seat B sees nothing.
5. **Root boundary:** share from outside Approved Capture Root refused server-side.
6. **Tier isolation:** private Node trace never reaches shared enrichment path.
7. **Search trust:** trace hits carry `reference-only`; Central hits do not.
8. **LLM gate:** no tool errors → no server LLM call.
9. **Central isolation:** shared trace never appears in Central dataset or improve loop input.
10. Empty `session-traces` → no trace section, no exception.

## Non-goals (v1)

- LLM screening of shared traces for injection
- Auto-share on SessionEnd or `share_traces=true` standing consent
- `citadel_prior_work` MCP tool
- Curator / auto-synthesis from traces into **Central**
- Cross-repo trace linking
- GPT / Codex / Cursor transcript adapters beyond shared distill logic
- Editing shared traces (retraction only)

## v1.1 candidates

- `citadel_prior_work` — overlap-ranked retrieval
- `citadel unshare` + TTL enforcement job
- `CaptureRoot.share_traces` automatic standing consent (after retraction)
- Semantic fill tuning; cognify coalescing window from measured seat volume
