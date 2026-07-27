# Contributing to Citadel

Citadel is a self-hosted **Organization Vault** — shared, access-controlled memory for a team and its AI agents — plus a zero-dependency seat-capture CLI, a hosted MCP endpoint, and a web dashboard. It is a Python/FastAPI application that uses [Cognee](https://github.com/topoteretes/cognee) as its knowledge engine; storage, access control, sync pipelines, MCP, CLI, and UI are Citadel's own.

Issues and pull requests are welcome. This page tells you exactly how to get from a clone to a merged PR.

---

## Before you start: public app, private vault

This is the single most important thing to understand about the project.

Citadel splits an **open application** from **closed organization memory**. Full detail lives in [`docs/public-and-private.md`](docs/public-and-private.md); the short version:

| Layer | Where | Visibility |
|---|---|---|
| **Application** (this repo) | `masumi-network/Citadel` | **Public** — code, MCP wrapper, agent skills, docs, UI, tests |
| **Live vault** | Railway node + Postgres/pgvector/Kuzu | **Private** — structured knowledge, embeddings, mesh, audit, hashed tokens |
| **Backup mirror** | separate private repo | **Private** — exported vault evidence |

**Outside contributors work on the application only.** You will not be issued a `ctdl_` token for the production node, and you do not need one. No pull request requires vault access to write, test, or review.

### What you can do locally

- Run the **entire test suite offline**. It needs no token, no network, and no database — the tests mock their dependencies, and `tests/conftest.py` redirects `CITADEL_HOME` to a throwaway directory so your real `~/.claude` config is never touched.
- Run `uv run ruff check .`.
- Run the client CLI (`uv run citadel --help`, `uv run citadel doctor`) — the base package is standard-library only.
- Run the server against **your own** infrastructure: `cp .env.example .env`, supply your own Postgres/pgvector, your own LLM API key, then `uv run uvicorn kb.server:app --reload --port 8000`.

### What you cannot do locally

- Read, search, or export the live Organization Vault. Its content is private and is never mirrored into git.
- Exercise the opt-in live canary in `tests/test_agent_canary.py`. It is skipped unless `CITADEL_CANARY_LIVE=1` and a seat token are both present, and it is not part of CI's merge gate. Maintainers run it against the production node.
- Point local development at the production node. Use your own instance.

### Never commit

`ctdl_` tokens, `.env`, database credentials, real values from `.env.example`, exported vault content, search results, ingested notes, mesh exports, or personal machine paths in committed config. See [`SECURITY.md`](SECURITY.md). If you find a vulnerability, **do not open a public issue** — use private reporting, described in `SECURITY.md`.

---

## Development setup

Citadel uses [`uv`](https://docs.astral.sh/uv/) as its package manager.

```bash
git clone https://github.com/masumi-network/Citadel.git
cd Citadel
uv sync --all-extras --dev
```

`--all-extras` pulls in the `server` optional-dependency group (fastapi, mcp, cognee, …). The test suite imports `kb.server` and the MCP module, so a base-only sync fails collection with `ModuleNotFoundError`.

Then run exactly what CI runs:

```bash
uv run ruff check .          # lint — must be clean
uv run pytest tests/ -q      # full suite — must pass
```

These two commands are what CI runs against your code, on both 3.11 and 3.12. If they pass locally, that leg of CI will almost certainly agree. CI additionally runs a `pip-audit` dependency audit, a DCO sign-off check, and a gitleaks secret scan — see [Pull requests](#pull-requests).

### Python version

**The real floor is Python 3.11.** Several `kb/` modules import `datetime.UTC` (3.11+) and `tests/test_railway_entrypoint.py` imports `tomllib` (3.11 stdlib). CI runs the suite on **3.11 and 3.12**; the repo pins `.python-version` to 3.12 for local work.

`pyproject.toml` currently declares `requires-python = ">=3.10"`. That is a known bug and is tracked as an issue — do not treat it as permission to write 3.10-compatible code, and do not "fix" it in an unrelated PR.

### Formatting

`ruff format` is **not** enforced in this repo and CI does not check it. Do not run `uv run ruff format` across the tree — it would rewrite dozens of files and bury your actual change in noise. Match the style of the file you are editing; `line-length` is 100 (see `[tool.ruff]` in `pyproject.toml`).

### Keep the client boundary intact

The base `citadel-archive` install is **stdlib-only** so the teammate CLI stays a zero-dependency client. `tests/test_client_boundary.py` enforces this by importing `kb.cli` in a clean subprocess and failing if any server module leaks in. If you add an import to a client code path, that test will tell you.

---

## Picking something to work on

Start with issues labelled **`good first issue`** or **`help wanted`**. Those are scoped so a newcomer can finish them without vault access or tribal knowledge.

- [good first issue](https://github.com/masumi-network/Citadel/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
- [help wanted](https://github.com/masumi-network/Citadel/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22)

Every triaged issue carries labels from three or four groups. Read them as: *what kind of change*, *which part of the system*, *how urgent*, and *is it actually ready to work on*.

### `type/*` — what kind of change

| Label | Meaning |
|---|---|
| `type/bug` | Something behaves incorrectly. Needs a regression test. |
| `type/feature` | New capability or a meaningful extension of one. |
| `type/docs` | Documentation, examples, agent skills, README/ADR content. |
| `type/chore` | Dependencies, tooling, refactors, release plumbing. |
| `type/security` | Hardening, access-control, and secret-handling work. Note: an *unpatched vulnerability* is never filed as an issue — see `SECURITY.md`. |

### `area/*` — which part of the system

| Label | Roughly maps to |
|---|---|
| `area/cli` | The `citadel` command — `kb/cli.py`, onboard, status, capture, doctor |
| `area/mcp` | The hosted MCP endpoint and tool surface — `kb/mcp_server.py` |
| `area/server` | FastAPI app, HTTP API, access control, audit — `kb/server.py`, `kb/access.py` |
| `area/search` | Retrieval, ranking, trust tiers, result formatting |
| `area/ingest` | Write paths, the Learning Process, tiered ingestion, security scanning |
| `area/sync` | GitHub / Linear / repo-content / Obsidian sync, capture hooks, promotion |
| `area/graph` | Kuzu graph, Knowledge Mesh, mesh projections |
| `area/web` | Dashboard and portal UI — `kb/static/` |
| `area/ci` | GitHub Actions workflows, packaging, release automation |
| `area/docs` | The `docs/` tree and repo-root documentation |

An issue may carry more than one `area/*` label when a change genuinely spans subsystems.

### `priority/P0`–`P3` — how urgent

| Label | Meaning |
|---|---|
| `priority/P0` | Production is broken or a security issue is live. Drop other work. |
| `priority/P1` | Important; scheduled for the current cycle. |
| `priority/P2` | Normal. The default for most accepted work. |
| `priority/P3` | Nice to have; no timeline attached. |

Priority is set by maintainers. Please don't relabel your own issue upward.

### `status/*` — is it ready

| Label | Meaning |
|---|---|
| `status/needs-triage` | Filed, not yet reviewed. **Do not start work on these** — scope may change. |
| `status/needs-info` | Waiting on the reporter. Blocked until answered. |
| `status/blocked` | Waiting on something external — an upstream fix, a decision, another PR. |
| `status/in-progress` | Someone is actively working on it. |

**An issue is ready to work on when it has a `type/*`, an `area/*`, a `priority/*`, and no blocking `status/*`.** Comment on it before you start so a maintainer can add `status/in-progress` and nobody duplicates your work.

---

## Issue lifecycle

1. **New issue filed** — it lands with `status/needs-triage`.
2. **Maintainer triages** — adds `type/*`, `area/*`, and `priority/*`; asks for detail with `status/needs-info` if the report is incomplete; marks `status/blocked` if it depends on something else.
3. **Ready to work** — labelled, unblocked, and open. Claim it in a comment.
4. **In progress** — a maintainer applies `status/in-progress`.
5. **PR opened** — the pull request references the issue (`Fixes #123`).
6. **Merged** — the merge closes the issue automatically.

For bugs, please include: what you ran, what you expected, what happened, the Python version, and whether you were running the client, a self-hosted server, or the hosted MCP. Never paste a token or vault content into an issue.

---

## Commit conventions

### Conventional Commits

Every commit message uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <imperative summary>
```

Types in use in this repo:

| Type | For |
|---|---|
| `feat` | New user-visible capability |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Tests only |
| `refactor` | Behaviour-preserving restructure |
| `style` | Formatting or visual styling, no behaviour change |
| `ci` | Workflows and CI configuration |
| `build` | Packaging, build backend, dependency plumbing |
| `chore` | Everything else that ships no behaviour |
| `release` | Version bumps and release commits (maintainers) |

Scope is optional but encouraged — use the `area/*` name or the module: `fix(mcp):`, `fix(cli):`, `docs(adr):`, `feat(agents):`.

Real examples from this repo's history:

```
fix(mcp): resolve tools/list role in-process, not via a self-deadlocking self-call (#108)
fix(cli): emit JSON on every search/ingest failure path under --json
docs(mcp): troubleshoot "connected but fails / reconnect does nothing" (#110)
```

Reference the issue number in the summary or the body.

### DCO sign-off (required)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/) — **not** a CLA. Signing off means you certify you wrote the contribution, or otherwise have the right to submit it under the project's licence.

Every commit must carry a `Signed-off-by:` trailer. Git adds it for you with `-s`:

```bash
git commit -s -m "fix(cli): reject --dataset without --local instead of dropping it"
```

That produces:

```
fix(cli): reject --dataset without --local instead of dropping it

Signed-off-by: Your Name <you@example.com>
```

The name and email come from your `git config user.name` and `user.email`, so set those correctly first. **The sign-off email must match the commit author email** — a `DCO sign-off` check runs on every pull request and fails any commit whose trailer is missing or addressed to someone else. Merge commits are exempt; they author no change of their own.

**Forgot to sign off?** Add the trailer to every commit on your branch and force-push:

```bash
git rebase --signoff origin/main
git push --force-with-lease
```

---

## Pull requests

### Branch naming

Branch from `main` using `<type>/<short-slug>`, matching your commit type:

```bash
git switch -c fix/mcp-tools-list-timeout
git switch -c feat/seat-token-rotation
git switch -c docs/contributing-labels
```

### Requirements

A pull request is mergeable when all of the following hold:

- **The title is a conventional commit line.** `fix(search): stop dedup stripping reference-only from a shared trace` — not `Fixed search`.
- **It links an issue.** Put `Fixes #123` (or `Closes #123`) in the description so the merge closes it. If no issue exists, file one first — it is how the work gets triaged and prioritised.
- **Every commit is signed off.** See DCO above; the `DCO sign-off` check enforces it.
- **CI is green.** The required status check is named **`CI gate`**. It aggregates lint + tests on Python 3.11 and 3.12, plus a `pip-audit` dependency audit. Two further checks run on every pull request and must also pass: **`DCO sign-off`** and the **gitleaks secret scan**. A red run blocks the merge — a merge to `main` auto-deploys to the production node, so this gate is deliberately strict.
- **One approving review.** `main` is protected: no direct pushes, PR required, one approval required.
- **The description says what changed and why.** If behaviour changed, say how you verified it.

### Scope

One PR, one concern. A focused 40-line diff gets reviewed the same day; a 900-line diff that fixes a bug *and* renames things *and* reformats two files does not.

---

## What gets a PR rejected fast

- **Secrets in the diff.** A `ctdl_` token, a `.env` file, a real API key, a database URL, or a connection string. This repo is public — anything committed here is public forever, and the credential must then be rotated. A gitleaks scan runs on every PR (rules in `.gitleaks.toml`, including a Citadel-specific `ctdl_` pattern) and blocks the merge. If it fires on a real credential, do not just delete the line and push again — the value stays reachable in git history. Rotate it first (see [`SECURITY.md`](SECURITY.md)), then remove it from history. If it is a false positive, add a narrow allowlist entry to `.gitleaks.toml` and explain why in the PR.
- **Vault content.** Search results, ingested notes, mesh exports, or any real organization data. Use synthetic examples.
- **Unrelated refactors.** Renaming, reorganising, or reformatting files your change did not need to touch. This especially includes repo-wide `ruff format` runs.
- **A bug fix with no test.** Every `type/bug` fix needs a regression test that fails before the fix and passes after it. State in the PR which test that is.
- **Red CI.** Please don't open a PR asking a maintainer to tell you why `uv run pytest tests/ -q` fails; run it first.
- **Missing DCO sign-off.** Fixable in one command (above), but it blocks the merge until it is.
- **Breaking the client boundary.** Adding a server-stack import to a client code path — `tests/test_client_boundary.py` will catch it.

---

## Licensing of contributions

Citadel is licensed under the **Apache License, Version 2.0** (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)). Copyright 2026 Masumi Network.

Inbound licence equals outbound licence. Per **Apache-2.0 section 5**:

> Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions.

So: contributions you submit here are licensed under Apache-2.0, on the same terms as the rest of the project. **There is no CLA to sign.** The DCO sign-off on each commit is the record that you had the right to submit the work.

If you are contributing on behalf of an employer, make sure you have the right to do so before you sign off.

---

## Code of Conduct

Participation is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md). Please read it.
