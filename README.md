<p align="center">
  <img src="docs/brand/readme-banner.svg" alt="Citadel Archive" width="860" />
</p>

# Citadel

> Self-hosted memory for engineering teams and the agents working alongside them.

[![State of the Vault](https://img.shields.io/badge/live-state%20of%20the%20vault-FF51FF?style=flat&labelColor=0a0a0a)](https://citadel-archive-production.up.railway.app/info)
[![Test](https://github.com/masumi-network/Citadel/actions/workflows/test.yml/badge.svg)](https://github.com/masumi-network/Citadel/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP](https://img.shields.io/badge/MCP-hosted-7c3aed)

Your team already writes down everything worth knowing. It ends up scattered across commits, pull requests, Linear tickets, and coding sessions nobody can search a week later. New engineers ask questions that were answered in March. Coding agents start every task with no idea what your team already decided.

Citadel collects that material as it is produced and puts it behind one query interface, for people and for agents. It runs on your own infrastructure.

<p align="center">
  <img src="docs/brand/readme-dashboard.jpg" alt="Citadel dashboard showing the knowledge graph" width="860" />
</p>

## Status: in testing, not yet open

Citadel currently runs for two organisations, Masumi Network and Sokosumi Network, who are using it daily and finding the problems. It is not open for general use yet.

If you are interested, watch the repository and wait for the stable release. We would rather you adopt something that works than something that is still moving under you. We publish benchmark numbers as we go, including the parts that are not good yet, so you can judge readiness for yourself rather than taking our word for it.

A self-hosted path for individuals to try it is planned. It is not ready today.

## Who it is being built for

Teams running coding agents get the most out of it. Claude Code, Cursor, and anything else speaking MCP query the same vault the engineers do, through a hosted endpoint. An agent that can look up why you dropped Postmark for Resend does not re-litigate it.

It also suits companies that cannot ship their context to a vendor. Apache-2.0, `pip install`, runs where you decide. One caveat worth stating plainly: document text is sent to an external model provider for enrichment and digests, so this is data you control the storage of, not data that never leaves the building.

And it is for anyone tired of filing things. Capture runs from git and editor hooks. Nobody tags anything.

## How it works

Capture happens without you. A git pre-push hook and a Claude Code session hook snapshot work as it is produced, while GitHub activity, repository content, and Linear issues sync on a schedule.

You ask from wherever you already are: the CLI, the web interface, or MCP. Answers carry a link back to the commit, issue, or document behind them, and retrieved text is marked as untrusted context worth checking before you act on it.

Promotion is the deliberate part. Captured work lands in your own space, and reaching shared org memory takes an explicit step. That gate is what keeps the shared layer worth reading instead of turning it into everyone's scratch notes.

## Quick start

```bash
pipx install citadel-archive
citadel onboard      # token, hooks, MCP config, capture roots
citadel status       # connection, identity, local setup
```

No Python yet? `curl -fsSL https://raw.githubusercontent.com/masumi-network/Citadel/main/install.sh | sh` checks for 3.11+, asks before installing, then sets up pipx and the CLI.

Self-hosting the server:

```bash
uv sync --dev
cp .env.example .env
uv run uvicorn kb.server:app --reload --port 8000
```

Full walkthrough in [`docs/onboarding/teammate-rollout.md`](docs/onboarding/teammate-rollout.md), deployment in [`docs/operations.md`](docs/operations.md).

## Connecting an agent

Agents need a URL and a token. `citadel onboard` writes this for you:

```json
{
  "mcpServers": {
    "citadel": {
      "type": "http",
      "url": "https://citadel-archive-production.up.railway.app/mcp/",
      "headers": { "Authorization": "Bearer ${CITADEL_MCP_ACCESS_TOKEN}" }
    }
  }
}
```

Twenty-two tools cover search, document fetch, ingest, contribution, and the admin surface. Every CLI command speaks `--json`. Setup per client and the full tool table are in [`docs/mcp/README.md`](docs/mcp/README.md).

## Measured performance

Most tools in this space publish nothing. We publish the numbers, including the ones that look bad.

Search runs at a 311 ms median through the API. A 69-question golden harness scores `answer_recall@5` at 0.8974 over the 39 questions carrying validated answer spans. Roughly a third of stored documents are not currently reachable by search, and ranking correlates poorly with query relevance. Both are open work.

Full table, definitions, and how to reproduce: [`docs/performance.md`](docs/performance.md).

## Documentation

| | |
|---|---|
| [Concepts and glossary](docs/concepts.md) | Seats, nodes, central, promotion, the learning process |
| [Architecture](docs/architecture.md) | Subsystems, storage, how the pieces fit |
| [MCP and agents](docs/mcp/README.md) | Client setup, tool reference, agent policy |
| [Performance](docs/performance.md) | Benchmark results and the harness |
| [Operations](docs/operations.md) | Deployment, environment, integrations |
| [Decisions](docs/adr/) | Architecture decision records |
| [Domain language](CONTEXT.md) | Terms this codebase uses precisely |

## Contributing

Issues and pull requests welcome. Commits need a DCO sign-off (`git commit -s`, no CLA), PR titles follow Conventional Commits, and the `CI gate` check must pass. Python 3.11+.

Start with [`good first issue`](https://github.com/masumi-network/Citadel/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) or read [`CONTRIBUTING.md`](CONTRIBUTING.md).

Found a security issue? Do not open a public issue. Use [private vulnerability reporting](https://github.com/masumi-network/Citadel/security/advisories/new). See [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
