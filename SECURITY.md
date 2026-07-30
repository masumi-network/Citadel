# Security

## Reporting a vulnerability

**Report privately through GitHub, never in a public issue.**

Open the repository's **Security** tab and choose **Report a vulnerability**, or
go straight to:

https://github.com/masumi-network/Citadel/security/advisories/new

This creates a private security advisory visible only to you and the
maintainers. Use it for anything that could compromise a vault, a token, or a
node.

**Do not open a public issue, pull request, or discussion for an unpatched
vulnerability**, and do not describe one in a commit message. This repository is
public — a public report is a public exploit until a fix ships. If you have
already opened a public issue by mistake, say so in the private advisory so we
can prioritise accordingly.

### What to include

The more of this you can provide, the faster we can confirm and fix:

- **What the issue is** — the vulnerability class (auth bypass, injection,
  secret exposure, privilege escalation, cross-seat data leak, …).
- **Where it is** — affected component (CLI, HTTP API, MCP endpoint, sync jobs,
  web UI) and the file, route, or tool name if you know it.
- **How to reproduce it** — minimal steps, request/response pairs, or a short
  proof-of-concept. Redact any real credentials from the report itself.
- **Impact** — what an attacker gains, and what access they need to start
  (no token, a reader token, a seat token, an admin token).
- **Version** — the commit SHA, release tag, or `citadel --version` output you
  tested against, and whether it was a self-hosted node or the hosted one.

### What to expect

We aim to acknowledge a report within a few days and will keep you updated as we
confirm, fix, and release. We do not offer a fixed response-time SLA and we do
not run a paid bug-bounty programme. Please give us a reasonable window to ship
a fix before disclosing publicly; we are happy to credit you in the advisory
unless you prefer otherwise.

## Supported versions

Only the latest release on `main` receives security fixes. Older tags and forks
are not patched — a merge to `main` auto-deploys the hosted node, and fixes ship
forward from there. If you are self-hosting, update to the current release
before reporting, and update promptly when an advisory is published.

## Scope

**In scope** — the application in this repository:

- The `citadel` CLI and its capture hooks
- The FastAPI HTTP API, access control, tokens, and audit paths
- The MCP server and its tool surface
- Sync, ingestion, promotion, and secret-scanning pipelines
- The web dashboard and seat portal
- Packaging and release workflows in this repo

**Out of scope:**

- **Data in the hosted production node.** Its contents are private organization
  memory. Do not attempt to access, enumerate, or exfiltrate vault content, and
  do not test against the production node — reproduce against your own
  self-hosted instance. Report the *flaw*, not the data.
- **Third-party dependencies.** Report those upstream (Cognee, FastAPI, Cognee's
  storage backends, and everything else declared in `pyproject.toml`). If a
  known CVE in a pinned dependency affects Citadel specifically, or our pin or
  `pip-audit` ignore is wrong, that *is* in scope — tell us.
- **Social engineering, physical access, and denial-of-service testing** against
  any Masumi Network infrastructure.
- Findings from automated scanners with no demonstrated impact.

## Data boundaries

Citadel separates a **public application repository** from a **private live vault**
and a **private backup mirror**. See [docs/public-and-private.md](docs/public-and-private.md).

**Never commit:** `ctdl_` tokens, `.env`, database credentials, or exported vault content.

**Rotate immediately** if a token or admin key may have been exposed.

## Agent-facing summary

https://citadel-archive-production.up.railway.app/skills/boundary
