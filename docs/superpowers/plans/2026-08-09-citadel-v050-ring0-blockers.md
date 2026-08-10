# Citadel v0.5 Ring 0 blocker implementation plan

> **REQUIRED SUB-SKILL:** Execute this plan with `superpowers:subagent-driven-development`. Root remains coordinator. Fresh implementers edit bounded scopes. One runtime owner runs every Docker command and follows Citadel plus Qdrant logs.

**Goal:** Integrate the known search, backup, stress, Docker test, CI, and non-root runtime blockers into reviewable sequential commits.

**Architecture:** Keep Citadel as the authorization boundary, preserve the current zero-result status-canary semantics, and treat a generation backup as one offline application-plus-Qdrant cut. Build tests into a dedicated Docker stage. Build the shipped runtime from a separate non-root production stage.

**Tech Stack:** Python 3.12, pytest, Ruff, Cognee 1.4.1, Qdrant 1.19.0, Docker BuildKit, Docker Compose, GitHub Actions.

**Global Constraints:**

- Worktree: `/private/tmp/citadel-v050-qdrant`.
- Do not run host pytest. Every Python test and lint command runs in Docker.
- Only the assigned runtime owner may mutate Docker resources.
- Keep Citadel and every Qdrant log follower attached for live and black-box gates.
- Use `sarthib7 <sarthiborkar7@gmail.com>` for each commit.
- Commit once, wait for success, then prepare the next commit.
- Do not stage unrelated dirty hunks.
- Do not push, merge, tag, publish, deploy, mutate Railway, or touch production in this plan.
- A reported P0 or P1 remains unverified until root reproduces it.

## Task 1: Establish the Docker test stage without committing it

**Owner:** implementer, `CITADEL-DOCKER-TEST-01`

**Files:** `Dockerfile`, `.dockerignore`, `tests/test_docker_workflow.py`

**Dependency:** Search and backup test contracts are settled.

1. Run the existing structural test in a disposable Docker interpreter before editing:

```bash
cd /private/tmp/citadel-v050-qdrant
docker run --rm \
  -v "$PWD:/src:ro" \
  -w /src \
  python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c \
  python -c "import runpy; ns=runpy.run_path('tests/test_docker_workflow.py'); ns['test_ci_uses_a_dedicated_docker_test_target_for_qdrant_contracts']()"
```

Expected red: lookup for the `container-tests` job fails.

2. Change `.dockerignore` so `.github` and `tests` enter the test-stage build context. Keep `.git`, `.local-review`, virtual environments, caches, secrets, `node_modules`, `dist`, and `build` excluded.

3. Refactor `Dockerfile` into `runtime`, `test`, and `production` targets. Keep the existing runtime package install. Add this test-stage behavior:

```dockerfile
FROM runtime AS test
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install \
       build==1.5.0 \
       hatchling==1.31.0 \
       pip-audit==2.10.1 \
       pytest==9.1.1 \
       pytest-asyncio==1.4.0 \
       ruff==0.15.15
WORKDIR /opt/citadel/source
COPY --chown=10001:10001 . .
USER 10001:10001
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q"]

FROM runtime AS production
USER 10001:10001
```

4. Build the uncommitted test target. This enables all later red and green commands:

```bash
docker build --target test --tag citadel-archive:ring0-test /private/tmp/citadel-v050-qdrant
```

5. Do not commit yet. The workflow commit belongs after every referenced test and source change.

## Task 2: Integrate whole-generation backup foundation

**Owner:** implementer, `CITADEL-BACKUP-GEN-01`

**Files:** `kb/generation_backup.py`, backup command hunks in `kb/cli.py`, `tests/test_generation_backup.py`, `tests/test_generation_backup_live.py`

**Contract:** `CITADEL-INT-BACKUP-01`

**Dependency:** Task 1 test image exists.

1. Inspect the existing dirty backup diff. Exclude every non-backup hunk.

2. Retain the recorded red-to-green evidence for wrong-generation validation and rollback. Do not manufacture a second red by reverting shared source. Root already reproduced:

```text
wrong generation before fix: restore rejected the call signature instead of the generation mismatch
unit rollback before fix: recovered fake collection remained
live rollback before fix: recovered Qdrant collection remained
```

3. Run the current Docker regression set:

```bash
docker run --rm citadel-archive:ring0-test \
  python -m pytest tests/test_generation_backup.py -q \
  -k 'wrong_generation or partial_failure or count_mismatch'
```

Expected green: `3 passed` and no failed test.

4. Start two fresh authenticated Qdrant 1.19.0 containers on a disposable network. Wait for both readiness endpoints. Run count mismatch before any successful restore test:

```bash
docker run --rm --network citadel-ring0 \
  -e CITADEL_QDRANT_LIVE_URL=http://qdrant-source:6333 \
  -e CITADEL_QDRANT_LIVE_KEY=source-test-key \
  -e CITADEL_QDRANT_RESTORE_URL=http://qdrant-restore:6333 \
  -e CITADEL_QDRANT_RESTORE_KEY=restore-test-key \
  citadel-archive:ring0-test \
  python -m pytest -q \
  tests/test_generation_backup_live.py::test_generation_restore_count_mismatch_rolls_back_real_qdrant_and_lite_root
```

Expected green: `1 passed`. After the test, require zero collections in the restore target and no target application root.

5. Classify both Qdrant logs. Recovery followed by deletion is expected. Panic, fatal, corruption, failed deletion, or recovery below 100 percent fails the task.

6. Commit only the backup foundation:

```bash
git config user.name sarthib7
git config user.email sarthiborkar7@gmail.com
git add kb/generation_backup.py kb/cli.py tests/test_generation_backup.py tests/test_generation_backup_live.py
git diff --cached --check
git commit -m "feat: add whole-generation backup and restore"
```

Before committing, use interactive hunk staging for `kb/cli.py` if it contains unrelated changes.

## Task 3: Contain and privatize backup artifacts

**Owner:** implementer, `CITADEL-BACKUP-CONTAINMENT-01`

**Files:** `kb/generation_backup.py`, backup command wiring in `kb/cli.py`, `tests/test_generation_backup.py`, `tests/test_client_boundary.py`

**Contract:** `CITADEL-INT-BACKUP-01`

**Dependency:** Task 2 committed.

1. Add these six tests before source changes:

```text
test_generation_backup_rejects_nested_destination_before_artifact_write
test_generation_backup_rejects_symlink_destination_before_artifact_write
test_generation_restore_rejects_nested_target_before_provider_write
test_generation_restore_rejects_symlink_target_before_provider_write
test_generation_backup_artifacts_use_private_modes
test_backup_command_without_server_extra_returns_typed_error
```

2. Run the focused red gate:

```bash
docker build --target test --tag citadel-archive:ring0-test /private/tmp/citadel-v050-qdrant
docker run --rm citadel-archive:ring0-test \
  python -m pytest -q \
  tests/test_generation_backup.py tests/test_client_boundary.py \
  -k 'nested_destination or symlink_destination or nested_target or symlink_target or private_modes or backup_command_without_server_extra'
```

Expected red: all six cases expose the current unsafe or untyped behavior. Record exact failures and nonzero collection.

3. Add `_validate_destination()` and call it before a lock, directory, provider request, or cleanup target is created. Check the lexical path for a symlink before `.resolve()`. Reject a create destination equal to or under the resolved application data root. Reject a restore target equal to or under the resolved backup root.

4. Add `_harden_private_tree()` and apply modes before atomic rename:

```python
def _harden_private_tree(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
```

Create the temporary backup root as `0700`. Keep rollback limited to a target that already passed containment validation.

5. Route both backup handlers through the existing server-extra guard:

```python
backup_create.set_defaults(handler=_needs_server(_backup_generation_create))
backup_restore.set_defaults(handler=_needs_server(_backup_generation_restore))
```

Update parser identity assertions to inspect `handler.__wrapped__`.

6. Rebuild the test target and rerun the focused command. Expected green: six selected tests pass.

7. Rerun Task 2 unit and live rollback commands. A containment fix must not weaken rollback.

8. Commit:

```bash
git add kb/generation_backup.py kb/cli.py tests/test_generation_backup.py tests/test_client_boundary.py
git diff --cached --check
git commit -m "fix: contain and privatize generation backups"
```

Residual boundary: `manifest.sha256` detects accidental corruption. It does not authenticate an attacker-modified manifest plus artifacts. Do not add signing without a new contract.

## Task 4: Preserve empty search without hiding provider failures

**Owner:** implementer, `CITADEL-SEARCH-EMPTY-01`

**Files:** the `_is_no_data_error` hunk in `kb/cognee_client.py`, focused hunks in `tests/test_cognee_client.py`

**Contract:** `CITADEL-INT-RETRIEVAL-01`

**Dependency:** Task 1 test image exists.

1. Preserve root's recorded red evidence:

```text
1 failed, 2 passed, 93 deselected in 0.22s
DatasetNotFoundError escaped; permission and provider failures stayed visible
```

2. Keep the smallest class-based change. Do not match broad message text:

```python
return exc.__class__.__name__ in {
    "DatasetNotFoundError",
    "NoDataError",
} or "No data found in the system" in str(exc)
```

3. Rebuild and run the focused Docker green:

```bash
docker build --target test --tag citadel-archive:ring0-test /private/tmp/citadel-v050-qdrant
docker run --rm citadel-archive:ring0-test \
  python -m pytest tests/test_cognee_client.py -q \
  -k 'absent_dataset or surfaces_non_empty_search_failures'
```

Expected green: `3 passed` and no failed test.

4. Commit only the two focused hunks:

```bash
git add -p kb/cognee_client.py tests/test_cognee_client.py
git diff --cached --check
git commit -m "fix: treat absent Cognee datasets as empty search"
```

5. Defer the production black-box proof until the exact production image is rebuilt. Correct acceptance is HTTP `200` with `results == []`. Do not change `kb/status.py`: zero results intentionally keep the search canary unavailable. A seeded exact hit in Ring 1 must close the later `citadel status --check-search` gate.

## Task 5: Reject an all-throttled stress run

**Owner:** implementer, `CITADEL-STRESS-429-01`

**Files:** `scripts/stress_qdrant_search.py`, `tests/test_stress_qdrant_search.py`

**Dependency:** Task 1 test image exists.

1. Add `test_main_rejects_all_429_even_with_retry_after`. Monkeypatch `_search_once` to return only `RequestResult(status=429, retry_after="1")`. Set a dummy `CITADEL_MCP_ACCESS_TOKEN`, pass bounded arguments through `sys.argv`, and assert `main() == 1`.

2. Run red:

```bash
docker build --target test --tag citadel-archive:ring0-test /private/tmp/citadel-v050-qdrant
docker run --rm citadel-archive:ring0-test \
  python -m pytest tests/test_stress_qdrant_search.py -q -k all_429
```

Expected red: `assert 0 == 1`.

3. Add `or successful == 0` to the failure predicate in `main()`.

4. Rebuild and rerun the same command. Expected green: `1 passed`.

5. Commit:

```bash
git add scripts/stress_qdrant_search.py tests/test_stress_qdrant_search.py
git diff --cached --check
git commit -m "fix: fail stress gate when every request is throttled"
```

Live concurrency remains in Ring 2 because it requires an exact seeded marker.

## Task 6: Finish non-root Docker and CI integration

**Owner:** implementer, `CITADEL-DOCKER-TEST-01`

**Files:** `Dockerfile`, `.dockerignore`, `.github/workflows/test.yml`, `tests/test_docker_workflow.py`

**Dependency:** Tasks 2 through 5 committed.

1. Add a `container-tests` job that builds `--target test`, proves nonzero collection, runs the full non-live suite, and runs Ruff inside the image.

2. Start two pinned Qdrant 1.19.0 containers for the live adapter, lifecycle, and backup suites. Set `CITADEL_QDRANT_SERVER_IMAGE`. Keep lifecycle enabled. Wait for both readiness endpoints before tests. Follow both logs until classification finishes.

3. Replace the weakened raw `container-smoke` path with the production Compose shape, or make it call that shape. Do not disable lifecycle.

4. Add `container-tests` and the production Compose job to `gate.needs`.

5. Rebuild and run structural, collection, non-live, and lint gates:

```bash
docker build --target test --tag citadel-archive:ring0-test /private/tmp/citadel-v050-qdrant
docker run --rm citadel-archive:ring0-test \
  python -m pytest tests/test_docker_workflow.py -q
docker run --rm citadel-archive:ring0-test \
  python -m pytest --collect-only -q -m 'not live'
docker run --rm citadel-archive:ring0-test \
  python -m pytest -q -m 'not live'
docker run --rm citadel-archive:ring0-test python -m ruff check .
```

Every command must collect at least one test and exit `0`.

6. Prove shipped identity:

```bash
docker build --target production --tag citadel-archive:ring0-production /private/tmp/citadel-v050-qdrant
docker run --rm --entrypoint id citadel-archive:ring0-production -u
```

Expected UID: `10001`.

7. Commit:

```bash
git add Dockerfile .dockerignore .github/workflows/test.yml tests/test_docker_workflow.py
git diff --cached --check
git commit -m "build: run release gates in non-root Docker images"
```

## Task 7: Ring 0 root integration gate

**Owner:** coordinator

**Files:** no source edits; evidence and coordination records only

**Dependency:** Tasks 2 through 6 committed.

1. Verify the worktree has no unstaged Ring 0 hunk. Preserve unrelated Ring 1 work.

2. Rebuild both test and production images from the committed checkout. Record commit SHA, image IDs, image digests, Cognee version, Qdrant version, and UID.

3. Run the complete Docker non-live suite, Ruff, three live Qdrant files, and the production Compose smoke. Keep exact output and nonzero collection counts.

4. Against an empty production dataset, call `/search` and require HTTP `200` plus `results: []`. Run `citadel status --check-search` and record the honest zero-result detail. Do not require the search subcheck to be healthy until Ring 1 seeds a marker.

5. Classify Citadel and Qdrant logs. Root reproduces any reported P0 or P1 before changing code.

6. Update `agents/model-routing.md`, `agents/blockers.md`, `status.md`, and the active handoff with commands, exact results, blind spots, and the next owner.

## Plan self-check

- Search-empty acceptance does not conflict with the zero-result canary contract.
- Backup containment happens before any filesystem or provider write.
- Backup rollback remains covered by fake and real Qdrant tests.
- The Docker target exists before Dockerized red tests, but its commit remains last.
- All Python tests and lint run inside Docker.
- No step authorizes GitHub writes or public release actions.
