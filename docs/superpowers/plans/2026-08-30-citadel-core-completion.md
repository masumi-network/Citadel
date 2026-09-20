# Citadel Core Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task. Each task has a focused test cycle and a production gate.

**Goal:** Add task-aware search injection, receipt-backed synchronization, durable feedback, gated promotion, clear UI access, and seat-isolated production verification.

**Architecture:** Extend the current lifecycle, Cognee, Qdrant, Ladybug, auth, PromotionEngine, and `/app` seams. Keep one graph writer in the web process. Use lifecycle receipts as the sync contract. Store feedback events under the existing Lite state root.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Cognee 1.4.1, Qdrant 1.19.0, Ladybug, Claude Code hooks, static `/app` UI, Next preview routes, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-citadel-core-completion-design.md`

## Global Constraints

- [VERIFIED] SQLite lifecycle state is authoritative for active source revisions, projection jobs, and backend receipts.
- [VERIFIED] Production uses one web process for Ladybug graph writes.
- [PLANNED] “All stores in sync” means searchable relational, vector, and graph receipts for one active source revision. It does not mean a distributed transaction.
- [PLANNED] “Sink chop” means the in-process sync cycle. Do not add it to the domain glossary.
- [PLANNED] Search context is bounded, secret-redacted, provenance-labeled, and untrusted.
- [PLANNED] Autonomous feedback may create bounded decisions or receipt-proven repairs. It may not rewrite memory or promote content from telemetry alone.
- [PLANNED] Production writes use unique source records in `seat:canary` only.
- [PLANNED] Do not store tokens, raw provider responses, or raw source text in `.local-review`.
- [VERIFIED] The agent must not create commits or push. The user performs those actions after review.

---

## File map

[VERIFIED] Existing source paths:

- `kb/hooks/sync_start.py`: SessionStart policy and recent-activity injection.
- `kb/onboard.py`: Claude hook and MCP configuration merge.
- `kb/service.py`: search, lifecycle orchestration, feedback, and operation readback.
- `kb/lifecycle.py`: lifecycle schema, leases, receipt transitions, and census.
- `kb/lifecycle_worker.py`: relational, vector, and graph projection worker.
- `kb/cognee_client.py`: Cognee recall, vector projection, graph Cognify, and feedback.
- `kb/qdrant_adapter.py`: generation-scoped Qdrant collections and payload filters.
- `kb/search_feedback.py`: redacted implicit telemetry payloads.
- `kb/promotion.py`: promotion classification, gates, and dual-write.
- `kb/access.py`: token identity, scopes, seat datasets, and promotion queue storage.
- `kb/server.py`: HTTP auth, search, feedback, promotion, lifecycle, and UI routes.
- `kb/static/`: canonical `/app` UI.
- `web/src/`: Next preview UI.
- `scripts/run_railway.py`: source-sync and scheduled stage definitions.
- `docs/architecture.md`, `docs/operations.md`, `docs/mcp/README.md`, `CONTEXT.md`: public records to update.
- `.local-review/production-verification.md`: ignored local evidence ledger.

[PLANNED] New files:

- `kb/hooks/search_inject.py`: task-aware prompt hook.
- `kb/projection_barrier.py`: exact projection job watermark and barrier helpers.
- `kb/feedback_store.py`: durable feedback events and decisions.
- `tests/test_search_inject.py`, `tests/test_projection_barrier.py`, and `tests/test_feedback_store.py`: focused contracts.

## Task 1: Task-aware search injection

**Files:**

| Role | Path |
| --- | --- |
| Create | `kb/hooks/search_inject.py` |
| Modify | `kb/onboard.py:250-284` |
| Test | `tests/test_search_inject.py` |
| Test | `tests/test_onboard.py` |
| Check | `kb/hooks/sync_start.py:40-69` |

**Interfaces:**

- Consumes: JSON hook input with `prompt`, `session_id`, `cwd`, and `source`; `CITADEL_MCP_ACCESS_TOKEN`; `CITADEL_BASE_URL`.
- Produces: stdout context and exit code `0` for every network or parsing failure.
- Query: `extract_task_query(prompt: str, *, max_chars: int = 1000) -> str | None`.
- HTTP: `fetch_task_hits(base_url: str, token: str, query: str, *, limit: int = 3) -> dict[str, Any]`.
- Formatter: `format_task_context(payload: Mapping[str, Any], *, limit: int = 3) -> str`.

[PLANNED] The hook must not call an LLM. It must preserve issue IDs, paths, repository names, and domain terms. It must remove fenced code, long logs, URLs, and command wrappers. It must cap query and output size.

- [ ] **Step 1: Write failing tests.** Test query extraction, three-hit output, secret redaction, no-token behavior, HTTPS-only requests, redirect rejection, timeout fallback, and malformed search responses.

```python
def test_task_hook_injects_bounded_search_context(monkeypatch, capsys):
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "token")
    monkeypatch.setenv("CITADEL_BASE_URL", "https://vault.example")
    monkeypatch.setattr(module, "fetch_task_hits", lambda *a, **k: {
        "search_id": "search:test",
        "results": [{"title": "Receipt barrier", "snippet": "Use receipts.",
                     "dataset": "seat:canary",
                     "_citadel": {"result_id": "result:test",
                                  "trust_tier": "unattested"}}],
    })
    assert module.run(io.StringIO(json.dumps({"prompt": "receipt barrier"}))) == 0
    output = capsys.readouterr().out
    assert "search:test" in output
    assert "result:test" in output
    assert "untrusted" in output.lower()
```

- [ ] **Step 2: Run the slice red.**

Run: `.venv/bin/python -m pytest -q tests/test_search_inject.py tests/test_onboard.py`

Expected: the new module and UserPromptSubmit hook are absent.

- [ ] **Step 3: Implement the hook and installer.** Reuse the no-redirect HTTPS opener and fail-silent pattern from `sync_start.py`. Write static policy first. Add the task hook to `merge_claude_settings` and allow the existing token and base URL environment variables.

```python
def run(stream_in: Any) -> int:
    payload = read_hook_payload(stream_in)
    sys.stdout.write(AGENT_POLICY_REMINDER + "\n")
    token = os.getenv(TOKEN_ENV, "").strip()
    query = extract_task_query(str(payload.get("prompt") or ""))
    if not token or query is None:
        return 0
    try:
        context = format_task_context(
            fetch_task_hits(_base_url(), token, query, limit=3), limit=3
        )
        if context:
            sys.stdout.write(context + "\n")
    except Exception:
        pass
    return 0
```

- [ ] **Step 4: Run focused green tests.**

Run: `.venv/bin/python -m pytest -q tests/test_search_inject.py tests/test_sync_start.py tests/test_onboard.py`

- [ ] **Step 5: Run Ruff.**

Run: `.venv/bin/ruff check kb/hooks/search_inject.py kb/hooks/sync_start.py kb/onboard.py tests/test_search_inject.py tests/test_sync_start.py tests/test_onboard.py`

Expected: `All checks passed!`

## Task 2: Exact projection watermarks and barriers

**Files:**

| Role | Path |
| --- | --- |
| Create | `kb/projection_barrier.py` |
| Modify | `kb/lifecycle.py:1589-1708,2516-2600` |
| Modify | `kb/service.py:585-717,1172-1418` |
| Modify | `scripts/run_railway.py:360-411` |
| Modify | `kb/server.py:475-843` |
| Test | `tests/test_projection_barrier.py`, `tests/test_lifecycle.py`, `tests/test_server.py` |

**Interfaces:**

- `LifecycleStore.projection_job_ids_for_capture_run(capture_run_id: str, *, generation_id: str, projection_version: str, config_digest: str) -> tuple[str, ...]`.
- `async wait_for_projection_barrier(citadel: Any, job_ids: Sequence[str], *, timeout_seconds: float) -> ProjectionBarrierResult`.
- `ProjectionBarrierResult` fields: `job_ids`, `searchable_job_ids`, `pending_job_ids`, `failed_job_ids`, and `complete`.
- `run_evolve_in_loop(*, capture_run_id: str | None = None, stages: Sequence[str] | None = None) -> int`.

[PLANNED] Bind source changes to `SourceRevision.capture_run_id`. Never infer a cycle from timestamps or collection-wide Qdrant counts.

- [ ] **Step 1: Write failing tests.** Cover exact capture-run binding, pending and failed states, timeout, and deterministic ordering.

```python
@pytest.mark.asyncio
async def test_barrier_reports_pending_and_failed_jobs(fake_citadel):
    result = await wait_for_projection_barrier(
        fake_citadel, ["job:pending", "job:failed"], timeout_seconds=0.01
    )
    assert result.complete is False
    assert result.pending_job_ids == ("job:pending",)
    assert result.failed_job_ids == ("job:failed",)
```

- [ ] **Step 2: Run red.**

Run: `.venv/bin/python -m pytest -q tests/test_projection_barrier.py`

Expected: the new interfaces are absent.

- [ ] **Step 3: Implement the query and barrier.** Resume the lifecycle queue before polling. Treat a job as complete only when all required backend receipts are searchable. Return failures without converting them to success.

- [ ] **Step 4: Reorder scheduled stages.** Run source sync with inline Cognify suppressed. Wait for exact relational, vector, and selected graph receipts. Run feedback, promotion, and self-improvement after the barrier. Capture and drain jobs created by those stages through a second barrier. Preserve `CITADEL_EVOLVE_COGNIFY_ENABLED=false` during graph rebuild.

- [ ] **Step 5: Run focused tests and Ruff.**

Run: `.venv/bin/python -m pytest -q tests/test_projection_barrier.py tests/test_lifecycle.py tests/test_server.py -k 'lifecycle or evolve or projection'`

Run: `.venv/bin/ruff check kb/projection_barrier.py kb/lifecycle.py kb/service.py scripts/run_railway.py kb/server.py tests/test_projection_barrier.py tests/test_lifecycle.py tests/test_server.py`

Expected: tests pass and Ruff reports `All checks passed!`

## Task 3: Durable feedback and autonomous decisions

**Files:**

| Role | Path |
| --- | --- |
| Create | `kb/feedback_store.py` |
| Modify | `kb/search_feedback.py:142-260` |
| Modify | `kb/server.py:2889-3008,8603-8633,8980-9068` |
| Modify | `kb/service.py:1422-1499` |
| Modify | `kb/config.py:243-265,430-490` |
| Test | `tests/test_feedback_store.py`, `tests/test_search_feedback.py`, `tests/test_server.py` |

**Interfaces:**

- `FeedbackStore(path: str | Path)`.
- `FeedbackStore.record_event(event: Mapping[str, Any]) -> str`.
- `FeedbackStore.list_unprocessed(*, limit: int = 100) -> tuple[FeedbackEvent, ...]`.
- `FeedbackStore.record_decision(event_id: str, decision: str, reason: str) -> FeedbackDecision`.
- `process_feedback_events(store: FeedbackStore, lifecycle: LifecycleStore, *, limit: int = 100) -> FeedbackProcessResult`.

[PLANNED] Store `feedback.sqlite3` under `CITADEL_STATE_DIRECTORY`. Use a unique event key from event kind, search ID, actor ID, and result ID. Store IDs, dataset, score, trust tier, bounded reason, and timestamps. Do not store query text, source text, tokens, or provider bodies.

- [ ] **Step 1: Write failing tests.** Cover idempotent insert, reopen persistence, malformed event rejection, one decision per event, and repair decisions only for missing lifecycle receipts.

```python
def test_feedback_event_is_idempotent_and_survives_reopen(tmp_path):
    event = {"event_key": "search:test|result:test|actor:canary",
             "kind": "implicit_search", "search_id": "search:test",
             "result_id": "result:test", "dataset": "seat:canary", "score": 0.1}
    first = FeedbackStore(tmp_path / "feedback.sqlite3").record_event(event)
    second = FeedbackStore(tmp_path / "feedback.sqlite3").record_event(event)
    assert first == second
    assert len(FeedbackStore(tmp_path / "feedback.sqlite3").list_unprocessed()) == 1
```

- [ ] **Step 2: Run red.**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_store.py`

Expected: `FeedbackStore` is absent.

- [ ] **Step 3: Implement durable storage.** Use SQLite transactions, a unique event key, a unique decision binding, string redaction, and bounded fields. Reject malformed events.

- [ ] **Step 4: Integrate existing telemetry and agent feedback.** Persist a redacted implicit event after current non-blocking MeshState recording. Link HTTP `/feedback` and MCP `citadel_record_feedback` by search ID and result ID. Preserve the current Cognee feedback and durable-note fallback.

- [ ] **Step 5: Implement the bounded consumer.** Emit `no_action`, `ranking_eval_candidate`, or `projection_repair_candidate`. Queue a repair only after lifecycle evidence proves a missing receipt. Never rewrite memory, change ranking weights, or trigger promotion from telemetry alone.

- [ ] **Step 6: Run focused tests and Ruff.**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_store.py tests/test_search_feedback.py tests/test_server.py -k 'feedback or search'`

Run: `.venv/bin/ruff check kb/feedback_store.py kb/search_feedback.py kb/server.py kb/service.py kb/config.py tests/test_feedback_store.py tests/test_search_feedback.py`

Expected: tests pass and Ruff reports `All checks passed!`

## Task 4: Promotion evidence and second barrier

**Files:**

| Role | Path |
| --- | --- |
| Modify | `kb/access.py:821-938` |
| Modify | `kb/promotion.py:247-827` |
| Modify | `scripts/run_railway.py:316-357` |
| Modify | `kb/server.py:7149-7258` |
| Test | `tests/test_access.py`, `tests/test_promotion.py`, `tests/test_server.py` |

**Interfaces:**

- `PromotionPendingItem.secret_scan` remains unchanged across list, approve, and reject transitions.
- Promotion consumes records accepted by the first projection barrier.
- Promotion writes create a capture watermark consumed by the second barrier.

- [ ] **Step 1: Write the failing scan-preservation test.**

```python
def test_decide_promotion_preserves_secret_scan(access_store, pending_item):
    access_store.add_promotion_pending(pending_item)
    updated = access_store.decide_promotion_pending(
        pending_item.id, decision="rejected", actor_id="admin", actor_name="Admin"
    )
    assert updated.secret_scan == pending_item.secret_scan
```

- [ ] **Step 2: Run red.**

Run: `.venv/bin/python -m pytest -q tests/test_access.py::test_decide_promotion_preserves_secret_scan`

Expected: the reconstructed pending item drops `secret_scan`.

- [ ] **Step 3: Preserve scan evidence and enforce stage order.** Keep secret-scan, reference, relevance, LLM, dry-run, and admin approval gates. Run promotion only after the first barrier. Send successful promotion writes through the second barrier.

- [ ] **Step 4: Run focused tests and Ruff.**

Run: `.venv/bin/python -m pytest -q tests/test_access.py tests/test_promotion.py tests/test_server.py -k 'promotion or evolve'`

Run: `.venv/bin/ruff check kb/access.py kb/promotion.py scripts/run_railway.py kb/server.py tests/test_access.py tests/test_promotion.py`

Expected: tests pass and Ruff reports `All checks passed!`

## Task 5: UI token login and seat clarity

**Files:**

| Role | Path |
| --- | --- |
| Modify | `kb/server.py:3901-3929,4140-4198,5948-6168` |
| Modify | `kb/static/index.html:163-760` |
| Modify | `kb/static/app.js:220-250,758-862,3774-3781` |
| Modify | `web/src/pages/login.tsx:20-48` |
| Modify | `web/src/components/app/app-shell.tsx:31-143` |
| Modify | `web/src/lib/dashboard.ts:15-76` |
| Test | `tests/test_next_preview.py`, `tests/test_server.py` |
| Browser | production HTTPS UI |

**Interfaces:**

- `/admin/session` accepts env credentials and `ctdl_` tokens and sets the secure cookie.
- `/api/session` returns role, seat slug, scopes, and readable dataset labels.
- Search, graph, and drill-down responses keep dataset and trust metadata.
- Preview routes require effective capability and scope.

- [ ] **Step 1: Write failing auth and scope tests.** Cover seat scope payload, writer access to admin preview rejection, and result dataset labels.

```python
def test_session_payload_exposes_seat_scope(api_client, seat_token):
    response = api_client.get(
        "/api/session", headers={"Authorization": f"Bearer {seat_token}"}
    )
    assert response.status_code == 200
    assert response.json()["seat_slug"] == "canary"
    assert "seat:canary" in response.json()["datasets"]
```

- [ ] **Step 2: Run red.**

Run: `.venv/bin/python -m pytest -q tests/test_next_preview.py tests/test_server.py -k 'session or next or scope'`

Expected: the new scope payload or capability gate is absent.

- [ ] **Step 3: Implement minimal UI changes.** Keep `/app` canonical. Render private Node, Central, and Shared Session Traces as separate scopes. Add dataset and trust labels. Add capability checks before preview rendering. Keep secure cookies unchanged.

[PLANNED] In the graph section, keep the canvas but add a native-button node directory. Add 44px Zoom in, Zoom out, Fit all, Fit selection, and Clear selection controls. Add `aria-describedby` instructions and an `aria-live` selection status. Make connection rows readable, grouped, keyboardable, and capped at ten with `Show all N connections`. Mark aggregate or hidden targets `Not shown in this map` and provide `Show on canvas`.

[PLANNED] Do not change `kb/static/info.js` status wording in this task. Keep `Degraded` while `/api/state` reports failed lifecycle or source-sync state. Health wording belongs to a separate repair task after the seven failed graph jobs and repo-content error are classified.

- [ ] **Step 4: Run local tests.**

Run: `.venv/bin/python -m pytest -q tests/test_next_preview.py tests/test_next_search_filters.py tests/test_server.py -k 'session or scope or graph or search'`

- [ ] **Step 5: Run the HTTPS browser test.** Use the browser tool against production `/login`. Enter a canary seat token without printing it. Confirm `/app`, visible scope labels, foreign-seat exclusion, and logout.

- [ ] **Step 6: Run Ruff and the full local suite.**

Run: `.venv/bin/ruff check .`

Run: `.venv/bin/python -m pytest -q`

Expected: zero failures.

## Task 6: Sequential production verification

**Files:**

| Role | Path |
| --- | --- |
| Update | `.local-review/production-verification.md` |
| Constraint | No production source edits during checks. |
| Process | Use one fresh production tester subagent per feature. Never run write tests concurrently. |

- [ ] **Step 1: Search injection.** Verify task-aware context, three-hit cap, `search_id`, result ID, trust tier, provenance, and static-policy fallback.
- [ ] **Step 2: Cognee and Qdrant retrieval.** Verify authenticated `/search`, Qdrant health and dimensions, lifecycle vector receipt filtering, and document drill-down.
- [ ] **Step 3: Store synchronization.** Create one unique `seat:canary` source. Verify one current revision and searchable relational, vector, and graph receipts.
- [ ] **Step 4: Graph update.** Verify canary graph readback with `fallback=false`. Confirm tombstone exclusion receipts remain unchanged.
- [ ] **Step 5: Autonomous feedback.** Verify one implicit event, one explicit link, one durable decision, and restart idempotency.
- [ ] **Step 6: Agent feedback sending.** Verify HTTP `/feedback` and MCP `citadel_record_feedback` with required scope and redacted audit.
- [ ] **Step 7: Promotion.** Verify canary dry run, secret-scan evidence, pending queue state, and zero Central write.
- [ ] **Step 8: UI sign-in.** Verify HTTPS token login, secure cookie, and visible scope.
- [ ] **Step 9: Seat isolation.** Verify a second seat cannot retrieve the canary Node source while documented shared datasets remain visible.
- [ ] **Step 10: Records.** Re-read public docs and `.local-review`; scan written files for AI-writing issues and secrets.

[PLANNED] Each record must include exact HTTP status, operation ID, source revision ID, receipt states, search ID, result ID, graph evidence, and response hashes. Never store credentials, raw bodies, or source text.

## Task 7: Public docs, issue audit, and PR handoff

**Files:**

| Role | Path |
| --- | --- |
| Modify | `docs/architecture.md` |
| Modify | `docs/operations.md` |
| Modify | `docs/mcp/README.md` |
| Modify | `CONTEXT.md` |
| Modify | `web/README.md` |
| Modify | `docs/README.md` |
| Update | `.local-review/production-verification.md` |
| Constraint | Do not delete issues or files. |

- [ ] **Step 1: Update architecture and operations docs.** Put current SQLite, Qdrant, Ladybug, lifecycle, scheduler, feedback, promotion, auth, and seat behavior before stale alternatives. Remove instructions that create a second graph-writing cron process.
- [ ] **Step 2: Update agent and UI docs.** Document task-aware injection, untrusted context, feedback tools, promotion approval, token login, and private Node boundaries. Keep “sink chop” as user shorthand only.
- [ ] **Step 3: Run the writing detector.**

```bash
node -e 'const fs=require("fs"); const A=require("/Users/sarthiborkar/.agents/skills/avoid-ai-writing/detector/patterns.js"); for (const p of ["docs/architecture.md","docs/operations.md","docs/mcp/README.md","CONTEXT.md","web/README.md","docs/README.md","docs/superpowers/specs/2026-08-30-citadel-core-completion-design.md","docs/superpowers/plans/2026-08-30-citadel-core-completion.md",".local-review/production-verification.md"]) { const r=A.analyzeText(fs.readFileSync(p,"utf8"),{contextMode:"technical"}); console.log(JSON.stringify({path:p,score:r.score,label:r.label,issues:r.issues.length})); }'
```

Expected: no high-severity writing issues. Preserve quoted evidence and code blocks.

- [ ] **Step 4: Audit GitHub issues.** Separate code-solved issues from runtime-acceptance issues. Every triage comment starts with `> *This was generated by AI during triage.` Update only proven solved issues. Keep open blockers and do not delete issues.
- [ ] **Step 5: Prepare the PR packet.** Include a Conventional Commit title, What and Why, exact local and production verification, deployment IDs, rollback path, ignored `.local-review` note, and the list of untracked files that must be added. The agent does not create the commit or push.

## Final verification gate

- [ ] Run `.venv/bin/ruff check .`.
- [ ] Run `.venv/bin/python -m pytest -q`.
- [ ] Run each production verification task sequentially.
- [ ] Re-read `.local-review/production-verification.md` and public docs.
- [ ] Run fresh-eyes review on the final diff.
- [ ] Confirm the current production deployment and clean-cutover receipt before PR handoff.
