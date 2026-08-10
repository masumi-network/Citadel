# Citadel v0.5 review and PR integration plan

> **REQUIRED SUB-SKILL:** Execute implementation tasks with `superpowers:subagent-driven-development`. Invoke the repository Fresh Eyes and Red Team skills only for their named read-only review tasks.

**Goal:** Reconcile the remaining observable-outcome work, review the exact integrated candidate, update PR 256, wait for current-head CI, and perform evidence-backed tracker maintenance without merging or publishing.

**Architecture:** PR 256 is the sole v0.5 integration lane. PR 245 is a behavior inventory, not a merge source, because its branch predates the Qdrant Lite and lifecycle architecture and now conflicts. Independent reviewers inspect one immutable candidate and one complete evidence bundle.

**Tech Stack:** Git, GitHub CLI, GitHub Actions, Docker test and production stages, production Docker Compose, pytest, Ruff.

**Global Constraints:**

- Candidate worktree: `/private/tmp/citadel-v050-qdrant`.
- Tracking repository: `/Users/sarthiborkar/masumi/Citadel Archive`.
- PR 256 head branch: `agent/citadel-v050-qdrant`.
- Remote PR 256 head observed during planning: `420be9d`. Local committed base observed during planning: `5bdcf89` plus uncommitted work.
- PR 245 head observed during planning: `e9bbf5fb1ff7d6e5d1c9b23aceedc33fd5579cc5`, merge state `CONFLICTING`.
- Use Docker for every Python test, lint, and functional gate.
- Reviewers do not edit. Root reproduces reported P0 and P1 findings.
- Push and GitHub maintenance occur only after the local immutable-candidate gates and independent reviews pass.
- Do not merge, tag, publish a release, deploy, mutate Railway, mutate production, or delete production data.

## Task 1: Reconcile PR 245 by behavior

**Owner:** reviewer first, then fresh bounded implementers

**Task ID:** `CITADEL-OBSERVED-OUTCOMES-01`

**Files to inspect:** `kb/agent_workflows.py`, `kb/cli.py`, `kb/cognee_client.py`, `kb/github_sync.py`, `kb/hooks/sync_push.py`, `kb/hooks/sync_session.py`, `kb/hooks/sync_start.py`, `kb/linear_sync.py`, `kb/mcp_server.py`, `kb/mesh.py`, `kb/models.py`, `kb/promotion.py`, `kb/repo_content_sync.py`, `kb/search_format.py`, `kb/server.py`, `kb/service.py`, `kb/status.py`, and their focused tests

**Dependency:** Ring 0 committed. Shared interface review completed first.

1. Record a behavior table for every PR 245 commit. Each row must be one of `already present`, `port required`, `replaced by lifecycle v1`, or `out of v0.5 scope`, with current source evidence and a focused test.

2. Preserve these required behaviors unless the current lifecycle contract provides a stricter equivalent:

- Local and HTTP search derive `trust_tier` from the dataset actually read and expose the basis.
- Accepted ingest is distinct from observed searchable indexing.
- GitHub and repository-content sync report requested writes separately from indexed writes and failures.
- Background cognify state is observable. A never-run canary is distinct from a passed canary.
- Capture hooks record skips and errors without leaking exception messages or full private paths.
- MCP write tools record the user-confirmation signal in the audit trail.
- Promotion approval and rejection record the observed approval signal.

3. Do not cherry-pick or merge PR 245 wholesale. Its branch lacks the current Qdrant adapter, lifecycle, Lite runtime, build identity, Docker, and retrieval-evaluation changes.

4. For each `port required` row, assign a fresh implementer one non-overlapping file scope. Add or port the focused regression first. A typical Docker red command is:

```bash
docker run --rm citadel-archive:v050-test \
  python -m pytest -q \
  tests/test_ingest_index_state.py \
  tests/test_local_search_provenance.py \
  tests/test_observable_approval_and_readiness.py \
  tests/test_observable_ingest_outcome.py
```

The runtime owner records the exact failing test names. Implementers then make the smallest lifecycle-v1-compatible source changes.

5. Rerun the focused command and all affected integration gates inside Docker. Require zero failures and nonzero collection.

6. Commit each independent behavior group sequentially. Use messages that name the behavior, for example:

```text
fix: report observed ingest projection state
fix: preserve search trust provenance across clients
fix: record approved write outcomes in audit trails
```

Do not use a merge commit from PR 245.

## Task 2: Freeze the immutable review candidate

**Owner:** coordinator

**Task ID:** `CITADEL-CANDIDATE-FREEZE-01`

**Files:** no source changes

**Dependency:** Rings 0 through 4 green and all required PR 245 behaviors accounted for.

1. Require a clean implementation worktree. Untracked evidence may remain only under ignored `.local-review/`.

2. Configure identity and record the candidate:

```bash
cd /private/tmp/citadel-v050-qdrant
git config user.name sarthib7
git config user.email sarthiborkar7@gmail.com
git status --short
git rev-parse HEAD
git log -1 --format='%H%n%an%n%ae%n%s'
```

3. Build immutable tags from that commit:

```bash
docker build --target test --tag citadel-archive:v050-test-$(git rev-parse --short=12 HEAD) .
docker build --target production --tag citadel-archive:v050-rc-$(git rev-parse --short=12 HEAD) .
```

4. Record image IDs and digests. Run no source edit after this point without invalidating the candidate and rebuilding every affected gate.

5. Assemble the evidence bundle under `.local-review/release-evidence/<commit>/` with red and green test outputs, Compose commands, marker manifest, censuses, benchmark reports, resource samples, and classified Citadel plus Qdrant logs. Do not store secrets.

## Task 3: Fresh Eyes Corroborate review

**Owner:** fresh reviewer, `gpt-5.6-sol`, high effort

**Task ID:** `CITADEL-FRESH-EYES-CORROBORATE-01`

**Scope:** read-only integrated diff, contracts, accepted design, implementation plans, and exact evidence bundle

**Dependency:** Task 2 freeze.

1. Invoke Fresh Eyes in Corroborate mode in a fresh context.

2. Ask the reviewer to prove which release claims the evidence supports. Require exact file, line, command, output, candidate SHA, and blind spot for every claim.

3. The reviewer must inspect search identity, lifecycle receipts, graph scoping, capture, session traces, seat isolation, promotion, outage recovery, backup restore, benchmark comparability, non-root packaging, and CI parity.

4. The reviewer returns findings only. No edits and no Docker mutation.

## Task 4: Fresh Eyes Refute review

**Owner:** separate fresh reviewer, `gpt-5.6-sol`, high effort

**Task ID:** `CITADEL-FRESH-EYES-REFUTE-01`

**Scope:** same immutable candidate and evidence, read-only

**Dependency:** Task 2 freeze. May run in parallel with Task 3.

1. Invoke Fresh Eyes in Refute mode.

2. Try to falsify each release claim. Focus on evidence that accidentally exercised a test stage instead of production, a changed corpus, a stale generation, a top-k pseudo-census, a hidden provider failure, or a surface that bypassed Citadel authorization.

3. Return severity, reproduction command, expected and observed result, source location, confidence, and blind spot. No edits.

## Task 5: Red Team review

**Owner:** separate fresh reviewer, `gpt-5.6-sol`, xhigh effort

**Task ID:** `CITADEL-RED-TEAM-01`

**Scope:** same immutable candidate and evidence, read-only

**Dependency:** Task 2 freeze. May run in parallel with Tasks 3 and 4.

1. Invoke Red Team against data loss, authorization bypass, cross-seat reads or writes, direct-Qdrant scope bypass, secret exposure, symlink or nested backup targets, incomplete restore rollback, stale-generation visibility, concurrent writer behavior, all-throttled stress, and forged success receipts.

2. Return exact reproduction for every P0 or P1. Do not change source or mutate Docker.

## Task 6: Reproduce and resolve review findings

**Owner:** coordinator and fresh implementers

**Task ID:** `CITADEL-REVIEW-FIX-01`

**Dependency:** Tasks 3 through 5 complete.

1. Root reproduces every reported P0 or P1 against the frozen candidate. A source argument alone remains `REPORTED` until reproduction.

2. Record reproduced blockers in `agents/blockers.md` and `status.md`. Assign one fresh implementer per bounded scope. Update `docs/interfaces.md` or `docs/decisions.md` before any cross-boundary change.

3. Each fix follows Docker red, minimal source change, focused Docker green, affected Compose gate, and sequential commit.

4. Rebuild a new immutable candidate. Rerun the complete Rings 0 through 4 matrix if a finding affects generation, source retention, authorization, storage, retrieval identity, or backup. Otherwise rerun every directly affected gate plus full non-live Docker suite and Ruff.

5. Ask the reporting reviewer to check the fix read-only. Do not release the review slot until the finding is closed or recorded as an accepted non-P0/P1 residual.

## Task 7: Push and update PR 256

**Owner:** coordinator and release

**Task ID:** `CITADEL-PR256-UPDATE-01`

**Dependency:** no unresolved reproduced P0 or P1, final candidate green, evidence bundle complete.

1. Refresh remote state immediately before the write:

```bash
git fetch origin main agent/citadel-v050-qdrant
gh pr view 256 --repo masumi-network/Citadel \
  --json headRefOid,baseRefOid,isDraft,mergeable,statusCheckRollup
```

2. If the remote head changed from the expected ancestor, stop and reconcile. Do not force-push over unknown work.

3. Push the reviewed commit sequence:

```bash
git push origin agent/citadel-v050-qdrant
```

4. Write a PR body file under ignored `.local-review/`. Include candidate SHA and image digest, exact Docker commands and results, test counts, deterministic seed and GitHub census, HTTP/CLI/MCP parity, seat and trace isolation, promotion, restore, benchmark quality and p95, review findings, fixed findings, and residual boundaries. Every claim uses `VERIFIED`, `REPORTED`, or `INFERRED`.

5. Update PR 256, then watch checks for the new head:

```bash
gh pr edit 256 --repo masumi-network/Citadel \
  --body-file .local-review/pr-256-release-evidence.md
gh pr checks 256 --repo masumi-network/Citadel --watch
```

6. Confirm the checked head equals the pushed candidate SHA. Old green checks do not count.

7. Mark PR 256 ready only after current-head required checks are green:

```bash
gh pr ready 256 --repo masumi-network/Citadel
```

Stop before merge.

## Task 8: Refresh and maintain GitHub trackers

**Owner:** release, `CITADEL-GITHUB-REFRESH-01`

**Dependency:** PR 256 replacement work is visible remotely and current-head checks are recorded.

1. Re-enumerate all open issues and pull requests. Record live heads, checks, merge state, labels, and acceptance evidence. The planning snapshot of 19 issues and 16 PRs is stale after any external write.

2. Close PR 246 as obsolete only after PR 256 visibly contains the Qdrant 1.19.0 and mandatory seat-isolation design that replaces its Cognee 1.2.2 and cross-seat assumptions.

3. Close PR 254 only after commit `0f2d4c1` or its exact replacement is present in PR 256. Close PR 255 only after the approved seed-first design and implementation plans are linked from PR 256.

4. Close PR 245 only if its complete behavior table from Task 1 has no `port required` row and PR 256 links the replacement tests. Otherwise leave it open with a precise remaining-work comment.

5. Do not mass-close issues. An issue may close only when its own acceptance behavior has current candidate evidence. In particular:

- Issue 228 needs proof that an accepted source cannot remain without its required searchable projections.
- Issue 247 needs frozen tail-marker retrieval after the required reindex or current-generation projection.
- Issue 249 needs deterministic trust provenance across HTTP, CLI, and MCP.
- Issue 46 needs live GitHub sync acceptance under the final retry and failure contract.

6. Use a concise evidence comment before each closure. Link PR 256 and name exact candidate SHA and gate output. Then close one item at a time.

7. Record every GitHub write in `status.md` and the handoff. No issue or PR closure is evidence that the implementation works.

## Task 9: Release-approval stop

**Owner:** coordinator and release

**Dependency:** PR 256 current-head CI green and tracker refresh complete.

1. Update `status.md`, contracts, decisions, blockers, metrics, and the final handoff. State any residual security or benchmark boundary.

2. Report whether the v0.5 candidate meets every approved release-readiness exit criterion.

3. Stop. Ask separately for merge, tag, release publication, deployment, or Railway mutation. This plan authorizes none of them.

## Plan self-check

- PR 245 is reconciled by current behavior, not merged across incompatible architecture.
- Three independent read-only reviews inspect one immutable candidate.
- P0 and P1 findings require root reproduction before a fix or VERIFIED label.
- GitHub writes occur only after local evidence and review.
- PR 256 checks must attest the pushed candidate head.
- PR closure is conditional on visible replacement. Issue closure is conditional on issue-specific acceptance evidence.
- Merge, publication, deployment, and production changes remain blocked.
