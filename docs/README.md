# Documentation map

Where things are, and which of them still describe the system as it runs.

Several documents here are dated records of a plan or an investigation. They
were true when written and were not revised afterwards. Treat anything carrying
a date, a sprint name, or a phase number as a record rather than as current
guidance, and check the code before acting on it.

## Start here

| Document | What it is |
|---|---|
| [`../README.md`](../README.md) | What Citadel is and how to install it. |
| [`../CONTEXT.md`](../CONTEXT.md) | Domain language. The definitions that keep product concepts distinct from storage and integration mechanisms. Read this before writing anything for the project. |
| [`architecture.md`](architecture.md) | How the pieces fit together. |
| [`concepts.md`](concepts.md) | The product concepts in plainer terms than CONTEXT.md. |
| [`operations.md`](operations.md) | Deployment, environment, integrations, self-hosting. |
| [`performance.md`](performance.md) | Measured numbers and how they were produced. |
| [`adr/`](adr/README.md) | Architecture decision records, indexed. |

## Current design work

The 2026-08-03 design session repositioned Citadel as a governed data substrate.
Its output is four decision records (ADR-0020 to ADR-0023) and these documents:

| Document | What it is |
|---|---|
| [`superpowers/specs/2026-08-03-citadel-substrate-design.md`](superpowers/specs/2026-08-03-citadel-substrate-design.md) | The decisions, the evidence behind each, and an explicit list of what was not decided. |
| [`superpowers/specs/2026-08-04-citadel-execution-plan.md`](superpowers/specs/2026-08-04-citadel-execution-plan.md) | Every open issue ordered into stages, with an exit criterion per stage. |
| [`superpowers/specs/2026-08-04-graph-store-migration-runbook.md`](superpowers/specs/2026-08-04-graph-store-migration-runbook.md) | Nine gates that execute ADR-0020. |
| [`superpowers/specs/2026-08-19-agent-decision-trace-schema.md`](superpowers/specs/2026-08-19-agent-decision-trace-schema.md) | Agent decision and promotion trace contract for duplicate-safe autonomy and repair handoff. |
| [`superpowers/specs/2026-08-04-control-plane-design.md`](superpowers/specs/2026-08-04-control-plane-design.md) | Per-organisation instances, lifecycle, fleet upgrades, and the cost model. |
| [`eu-partner-proposal.md`](eu-partner-proposal.md) | Partner-facing. A business document rather than an engineering one. |

None of ADR-0020 through ADR-0023 is implemented. The records exist so the
decision is written down before the code moves, not because the code has moved.

## Reference

| Document | What it is |
|---|---|
| [`agent-access-model.md`](agent-access-model.md) | How agents authenticate and what they may reach. |
| [`public-and-private.md`](public-and-private.md) | What belongs in this public repository and what does not. |
| [`dashboard-api-contract.md`](dashboard-api-contract.md) | The API the dashboard consumes. |
| [`web-bundle.md`](web-bundle.md) | How the single JavaScript bundle is built. |
| [`mcp/`](mcp/) | MCP server reference. |
| [`onboarding/`](onboarding/) | Teammate rollout and setup. |
| [`brand/`](brand/) | Marks, banners, and screenshots. |
| [`diagrams/`](diagrams/) | Source for the architecture diagrams. |

## Plans and investigations, by date

These record what was intended or found at a point in time. Some shipped, some
were superseded, and the file does not always say which.

`organization-vault-plan.md`, `phase-2-shipping-plan.md`,
`adr-0007-shipping-plan.md`, `read-side-hardening-sprint.md`,
`internal-update-agent-architecture.md`, `obsidian-integration-plan.md`,
`google-chat-organization-update-digest-plan.md`, `mcp-safety-plan.md`,
`private-github-sync-security.md`, `mesh-architecture-research.md`,
`architecture-deepening-opportunities.md`, `live-knowledge-graph-timeline.md`,
`vault-backup-mirror.md` (superseded by ADR-0022),
`evolve-kuzu-lock-findings.md`, `uat-2026-07-23-findings.md`,
`team-share-smoke-test.md`, `progress.md`, and [`plans/`](plans/).

## How documentation reaches the vault

Citadel's repository-content sync reads a repository's **default branch**
(`kb/repo_content_sync.py:471`, `ref = repo.default_branch`). Documentation on a
feature branch is not searchable in the vault until the branch merges. Nothing
about opening a pull request makes a document retrievable.
