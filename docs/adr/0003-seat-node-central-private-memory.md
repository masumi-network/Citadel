# Seat, Node, And Central As The Private-Memory Architecture

> **Partially superseded by [ADR-0007](0007-seat-capture-promotion-write-policy.md)
> (2026-06-27):** seat-scoped writes are **Node-only** on all channels; direct
> seat org-tag ingest to **Central** is removed. **Central** is fed by governed
> sync and **Promotion Agent** instead.

Citadel separates **private agent memory** from **organization-wide knowledge** using three layers: a **Seat** (one licensed human **Principal**), a **Node** (that seat's private mini knowledge base), and **Central** (the shared organization dataset, `masumi-network`). The **Node** is the storage boundary — not the **Token**. Admins provision a seat before issuing tokens; each token inherits the seat's node scope plus read access to Central. Reads never cross seat nodes; writes default to the seat node, with org-bound paths and tagged contributions landing in Central; **Promotion** copies curated content from a node into Central via dual-write (the original stays in the node).

> **Body corrected 2026-07-31.** The sentence above still describes seat writes
> reaching **Central** through org-bound paths and tagged contributions. That
> path was removed by [ADR-0007](0007-seat-capture-promotion-write-policy.md):
> a seat-scoped caller is now refused a direct Central write on every channel,
> and **Central** is fed only by governed sync and the **Promotion Agent**. The
> supersession banner at the top of this ADR has flagged that since 2026-06-27,
> but the body text was never edited to match, so a reader taking this paragraph
> at face value would expect a write path that returns 403.

**Considered Options**

- **Lazy provisioning vs admin-first:** Auto-create a node on first token use is simpler to ship, but makes audit, seat inventory, and support overrides harder. Admin-first provisioning keeps a clear principal → seat → node → token chain and matches licensed team membership.
- **Move vs dual-write for promotion:** Moving content from node to Central loses private working memory and breaks agent continuity. Dual-write keeps the seat's draft/context in the node while publishing a curated copy to Central.
- **Central-only read vs node + Central:** Searching only Central misses private agent context; searching all nodes violates isolation. Default read scope is the caller's own node plus Central.
- **Physical database per node vs logical dataset:** Separate databases per seat would maximize isolation but multiply operational cost (migrations, backups, mesh rebuilds). Logical datasets (`seat:{slug}` for nodes, `masumi-network` for Central) in one Cognee deployment match Phase 1 infrastructure while enforcing access at the API/MCP layer.

**Consequences**

- Node naming convention: `seat:{slug}` (e.g. `seat:alice`). Central remains `masumi-network`.
- Token fields `default_dataset`, `default_session`, and `allowed_datasets` (Phase 1, implemented) scope memory without redefining the node boundary.
- **Tiered ingestion:** org-bound syncs (GitHub, tagged vault contributions, promoted content) use the full Learning Process; raw seat-node agent memory gets lighter indexing only.
- **Automatic + curated sync:** default agent writes stay in the seat node; tags and org pipelines route content to Central separately — not a full vault mirror between seats. Every write path — `/ingest`, `/api/contribute`, and Obsidian sync — routes per item through the same tag-aware resolver, so an org-bound (tagged) note promotes to Central instead of being trapped in the node.
- Admins may override scope with audit; seat-to-seat node reads remain forbidden. The `seat:` namespace is **default-deny**: a token may reach a seat node only if that node is in its own `allowed_datasets` (env/bootstrap and admin/`access:manage` callers still bypass). An unscoped or legacy token — empty `allowed_datasets` — keeps whole-vault access to ordinary datasets but **cannot** name another seat's node. When a bypassing caller that carries its own allowlist reaches outside it, the audit detail records `scope_override: true`.
- **Seats are reader/writer only.** Admin tokens bypass the dataset allowlist and so would dissolve the node boundary; they are issued directly via token creation, never as a seat. `create_seat(role="admin")` is rejected.
- **Central is curated.** A caller that holds a seat node cannot drop raw content straight into Central: a write that targets Central — whether named explicitly *or* reached as the caller's default target — must carry an org tag (`org-ready` / `vault-contribution`, which routes through promotion/dual-write) or go through `/api/contribute`. "Holds a seat node" is judged by **storage scope, not principal identity**: any token whose `default_dataset` is a `seat:` node *or* whose `allowed_datasets` contains one is gated. This deliberately covers both the human seat-holder's tokens and the **Agent Identities** scoped into that human's node — those are separate `service_account` principals carrying no `seat_slug`, so a principal-identity check would under-gate exactly the agents most likely to write. Admin/env callers and non-seat service accounts (no seat node in scope) keep their direct Central write path. The seat's Central allow-entry is derived from `central_dataset(config)` so the allowlist can never drift from the value the router targets.
- **Seat sessions stay private to the node.** A session id is private context, and session-scoped recall ignores the dataset allowlist (Cognee recalls by session without a dataset constraint). Since seat sessions are `seat-{slug}` — derived from a guessable slug — a non-bypass caller may name only its **own** `default_session`; naming any other session (e.g. another seat's) is rejected (403) so it cannot read that seat's private node. Admin/env callers keep full session reach (org sync sessions, support). Multi-dataset search then resolves a session *per dataset*: the caller's session scopes only its own node, while shared datasets like Central are searched session-wide so a seat session can never hide org-wide Central hits.
- Phase 2 adds admin seat UI, multi-dataset search, and tag routing; Phase 3 adds Linear read-only sync; Phase 4 adds external notifications and hard isolation options. See [`docs/organization-vault-plan.md`](../organization-vault-plan.md).
