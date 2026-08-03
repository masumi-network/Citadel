# Architecture decision records

One file per decision, numbered in the order taken. A record states what was
decided and why the alternatives lost. It is not documentation of how the system
currently behaves: a decision can be recorded before it ships, so check the code
before assuming an ADR describes today.

Number 0008 was never used.

| # | Decision |
|---|---|
| [0001](0001-github-vault-backup-mirror.md) | A private GitHub repository is the Phase 1 Vault Backup Mirror. Superseded by 0022. |
| [0002](0002-google-chat-app-auth-for-update-digests.md) | Organization update digests authenticate as a Google Chat app. |
| [0003](0003-seat-node-central-private-memory.md) | Seat, Node and Central are the private-memory architecture. Reads never cross seat Nodes. |
| [0004](0004-linear-seat-scoped-mirror.md) | Linear issues mirror into the assignee's own Node, not into Central. |
| [0005](0005-self-evolving-memory-policy-gated-ingestion.md) | Memory evolves on a schedule, and every write passes a policy gate. |
| [0006](0006-agent-auth-and-onboarding.md) | Agents authenticate by OAuth 2.1 with a device grant. |
| [0007](0007-seat-capture-promotion-write-policy.md) | What a seat captures stays on its Node; reaching Central requires promotion. |
| [0009](0009-mesh-read-isolation-presence-vs-content.md) | Every seat's presence is visible on the mesh; content stays scoped to the caller. |
| [0010](0010-structured-knowledge-durable-source-of-truth.md) | Structured Knowledge is the durable truth; the retrieval engine is a rebuildable projection. |
| [0011](0011-shared-session-traces.md) | Shared Session Traces are a third storage layer, consultable and never promoted. |
| [0012](0012-attested-trust-vs-content-hint.md) | `trust_tier` reports attestation, `content_hint` reports shape. They are not the same axis. |
| [0013](0013-public-contact-endpoint.md) | The contact form relays to Chat and never writes into the vault. |
| [0014](0014-nextjs-frontend-static-export.md) | The web frontend is Next.js, statically exported, inside this monorepo. |
| [0015](0015-one-process-owns-the-graph.md) | One process owns the graph. Revisitable once 0020 ships. |
| [0016](0016-ingested-documents-are-byte-stable.md) | An unchanged source produces a byte-identical ingested document. |
| [0017](0017-structural-provenance-outranks-inherited-trust.md) | Where a claim structurally came from beats what it inherited. |
| [0018](0018-corpus-totals-are-authoritative-not-uptime.md) | Corpus totals come from the corpus, not from how long the process has been up. |
| [0019](0019-activity-counters-are-named-by-their-scope.md) | A counter is named for what it counts, and one event increments one counter. |
| [0020](0020-graph-store-on-postgres-and-dataset-scoped-reads.md) | The graph store moves to PostgreSQL and graph reads resolve per dataset. Not yet implemented. |
| [0021](0021-retrieval-interface-owns-ranking-and-provenance.md) | Citadel defines the retrieval interface; the engine is one implementation behind it. Not yet implemented. |
| [0022](0022-evidence-is-retained-and-attested-at-capture.md) | Evidence is retained and fingerprinted at capture, not recomputed at read. Not yet implemented. |
| [0023](0023-control-plane-outside-the-application.md) | One organization, one instance. The control plane is a separate system. Not yet implemented. |

The four newest records come from the design session written up in
[`../superpowers/specs/2026-08-03-citadel-substrate-design.md`](../superpowers/specs/2026-08-03-citadel-substrate-design.md).
ADR-0020 is executed by the
[graph store migration runbook](../superpowers/specs/2026-08-04-graph-store-migration-runbook.md).
