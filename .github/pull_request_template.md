<!--
Thanks for contributing to Citadel.

Title format: <type>(<optional scope>): <description>
  e.g.  fix(mcp): resolve tools/list role in-process
Allowed types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert

Every commit needs a DCO sign-off (`git commit -s`). See CONTRIBUTING.md.
-->

## What

<!-- What does this change do? One or two sentences. -->

## Why

<!-- What problem does it solve? Link the issue below. -->

Fixes #

<!--
Use "Fixes #123" to close the issue on merge, or "Refs #123" to link without
closing. If this genuinely has no issue (a typo fix, say), ask a maintainer to
apply the `no-issue` label.
-->

## How it was verified

<!--
Say what you actually ran, not what should work. For example:
  uv run pytest tests/ -q          -> 402 passed
  uv run ruff check .              -> clean
  Manual: citadel search "x" against a local node
-->

## Checklist

- [ ] Commits are signed off (`git commit -s`) — the DCO check enforces this
- [ ] PR title follows Conventional Commits
- [ ] Tests added or updated for the behaviour changed
- [ ] `uv run pytest tests/ -q` passes locally
- [ ] `uv run ruff check .` is clean
- [ ] No secrets, tokens, `.env` contents or vault exports in the diff
- [ ] Documentation updated if behaviour or interfaces changed

<!--
Note on scope: unrelated refactors and repo-wide reformatting will be asked to
be split out, even when the change itself is an improvement. Small, reviewable
diffs get merged faster.
-->
