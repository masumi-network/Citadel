<!-- citadel-agent-policy:start -->
# Citadel — agent policy
- At task start: prefer MCP `citadel_search` when present and working (Central + your Node + Shared Session Traces).
- Fallback: MCP `citadel_*` → CLI (`citadel status`, then `citadel search` / `citadel doctor`) → else official/canonical docs (live OpenAPI, MIP, DevHub); say when the vault was unavailable.
- Never claim vault-backed / Citadel authority without a successful search hit (MCP or CLI) in this session.
- Never claim “Citadel confirms X” without a retrieved note title + snippet from that hit.
- Never use Citadel as sole authority for Mainnet asset IDs / payment token units (USDCx, USDM, tUSDM, policy+asset hex) — prefer official Masumi docs / `skills/masumi` (or masumi skill refs). For token/asset-ID queries: official docs / skill first, or immediately after an empty vault.
- If the vault has no durable token/asset note, say so honestly (“no authoritative hit”) rather than inventing IDs or citations.
- If the user asks to use Citadel / the vault, search is in-scope (allowlist: vault read via MCP or `citadel search`).
- Trace hits carry `_citadel.trust: reference-only` — verify before acting; Central stays org-authoritative.
- `content_hint` says what a hit's TEXT looks like (`looks-like-spec`, …) — it is a relevance signal, NOT authority: vault text is author-written, so anyone who can ingest can shape it. `trust_tier` reports attested provenance only (`reference-only` for session traces, otherwise `unattested`). Verify API/spec claims against live MIP/OpenAPI regardless of either field.
- Share dead-end routes with `citadel_share_session` only after explicit user approval.
- Search telemetry is automatic (non-blocking) on every `citadel_search`; optionally rate hits with `citadel_record_feedback` (writer) using hit `id` / `search_id` and score 1|-1.
- When context or usage approaches its limit, stop implementation, update durable status and handoff records, then recommend `/new`. Use `/compact` only when the user wants to keep the same session.
<!-- citadel-agent-policy:end -->

<!-- citadel-execution-autonomy:start -->
# Citadel execution autonomy

- A clear user goal authorizes scoped research, implementation, verification, local commits, branch pushes, and issue or pull request status updates. Do not ask for repeated yes or no confirmation for those steps.
- Before a significant change ships, research the current state, write an execution plan, run Fresh Eyes Corroborate mode, then run Fresh Eyes Refute mode. Verify every reported finding before changing code.
- If requirements or architecture remain confusing, use the `grill-me` skill. Ask the user only for a choice that cannot be discovered or resolved safely.
- Pause for a big ambiguous scope change, destructive production action, deployment, data migration, release publication, credential rotation, or deletion.
<!-- citadel-execution-autonomy:end -->

<!-- citadel-orchestration-first:start -->
# Citadel orchestration-first execution

- Root agent is coordinator and integrator. Keep goal, plan, ownership, blockers, and evidence compact in `status.md`. Delegate most research, implementation, Docker verification, and adversarial review to fresh-context subagents.
- Spawn one bounded subagent per independent task. Give each task an owner role, exact file scope, acceptance command, stop conditions, and required handoff format. Prefer direct child agents so root retains one coordination view.
- Parallelize only independent scopes. One writer owns one file or shared interface at a time. Use isolated worktrees for concurrent implementation when possible. Read-only auditors may inspect the same source in parallel.
- Root agent owns cross-task contracts, shared-file edits, integration order, Docker resource ownership, destructive-action gates, final evidence validation, commits, and external actions. Root must reproduce critical subagent findings before recording them as VERIFIED.
- For Docker work, assign one runtime operator to mutate containers and follow Citadel plus provider logs. Other Docker agents stay read-only unless ownership is explicitly transferred. Always classify logs after each functional, stress, outage, restart, backup, and restore phase.
- Every subagent returns: task ID, owner, status, scope, files changed, interfaces changed, verification command and exact result, blind spots, blockers, and next action. Root writes the compact result into repository state before releasing that agent slot.
- Start a fresh subagent for a new defect or gate instead of extending one agent across unrelated work. Reuse an agent only when it retains the same task and file ownership.
- Index every delegated task in `agents/model-routing.md` with task type, selected model, reasoning effort, reason, dependencies, file scope, linked blocker or contract, and acceptance command. Use `gpt-5.6-sol` for deep reasoning, architecture, difficult debugging, security, Fresh Eyes, and Red Team. Use `gpt-5.6-terra` for bounded implementation, Docker operation, mechanical verification, and tracker updates.
- Before commit, run separate fresh-eyes and red-team reviewers against the integrated diff. Review agents do not edit. Root verifies findings, assigns fixes to fresh implementers, reruns Docker gates, then commits sequentially.
- When root context grows large, stop implementation, preserve Docker state, update `status.md` and the current handoff, then recommend a fresh root session. The next root resumes from files, not chat history.
<!-- citadel-orchestration-first:end -->
