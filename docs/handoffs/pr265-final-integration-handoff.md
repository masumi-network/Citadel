# PR 265 Production-Readiness Handoff (2026-08-12)

## Scope
- Finalize CI hardening around log classifiers and queue timing tests.
- Keep integration production-grade by removing false-green paths.

Current check-in date: 2026-08-12

## Completed
- `.github/workflows/test.yml`
  - Replaced `rg` in log classifier helpers with portable `grep`.
  - Hard-fail tokens now include `error|traceback|exception|died|critical|panic|fatal|oom|corruption|failure|failed` first, then warning-only allowlist.
  - Added guard for `grep` exit status (`>1`) in both container-tests/runtime classifiers.
  - 4xx/5xx HTTP checks now use fixed-string `grep` for fixed route checks and `grep -Ev` for allowlist exclusions.
  - Added explicit `command -v grep >/dev/null` guards before classifier usage.
- `tests/test_docker_workflow.py`
  - Added checks that workflow shell code contains portable guarded `grep` usage and token ordering.
  - Added regressions for classifier failures with invalid regex and mixed warning+error lines.
  - Added explicit fatal-before-warning ordering assertion.
- `tests/test_cognee_client.py`
  - `test_long_cognify_renews_queue_lease` now uses deterministic clock + explicit lease-extension assertion.
  - `test_execution_guard_blocks_reclaim_until_cancellation_cleanup_stops` now uses long lease + controlled time advancement.
- `docs/handoffs/pr265-final-integration-handoff.md`
  - Added run-time updates and remaining action list for handoff.

## Verified commands
- `/.venv/bin/pytest tests/test_docker_workflow.py tests/test_cognee_client.py -q`
  - `112 passed, 10 warnings`.
- `/.venv/bin/ruff check tests/test_docker_workflow.py tests/test_cognee_client.py scripts/verify_package_artifacts.py`
  - `All checks passed!`.
- `uv run citadel bench ci`
  - `ci benchmark OK: answer_recall_at_1=1.0 answer_recall_at_5=1.0 doc_recall_at_1=1.0 doc_recall_at_5=1.0 negative_hit_rate=0.0`.
- `gh pr checks 265`
  - observed check status still includes `CI gate`, `Docker test target`, `production Compose readiness` as failing, while package smoke, frontend export, plain requirements, and most 3.12 checks pass.

## Commit
- `b78b623` on branch `fix/ship-scripts-in-wheel`
  - title: `ci(test): harden log classifiers and queue timing gate`

## Blocked/partially verified commands
- package-smoke isolation proof in this session
  - `uv build --wheel --sdist` succeeds.
  - isolated verifier run via fresh venv could not be repeated because this session blocks creating new venv instances.
- production compose/runtime proof in this session
  - `docker compose --project-name ... build citadel` fails with buildx permission/credential errors on this host:
    - `failed to update builder last activity time: open .../buildx/activity/.tmp...: operation not permitted`
    - retry with `DOCKER_BUILDKIT=0` returns `One or more parameters passed to the function were not valid. (-50)`.

## Notes
- In-session container/runtime gates and clean-package verifier were not completed due local environment limits (venv isolation and docker builder permissions).
- `git` merge/deploy proof is still external and depends on PR author/CI environment.
- Remaining external blocker is user-authorized Railway runtime verification and live MCP/search/capture/ingestion end-to-end checks.

## Handoff next
- Confirm Railway deployment health and API contract end-to-end:
  - `/readyz` unauthenticated/authenticated
  - `/.well-known/citadel.json`, `/skills`, `/skills/*`, `/api/state`, boundary routes
- Run production-style compose smoke on final image/staging equivalent:
  - assert `/src` absent and artifacts import from `purelib`
  - assert `repo_stats` fields (ADR count) come from packaged assets
- Re-run log classifiers once with:
  - injected fatal line and mixed warning+fatal lines
  - invalid `expected_*_log_pattern` regex path
- Final merge gate closes only after latest PR checks and Railway deployment job both pass.
