# Citadel agent operating manual

Last updated: 2026-08-09
Owner: coordinator
Status: In Progress

## Operating model

Root agent is coordinator and integrator. Root keeps goal, plan, task ownership, shared contracts, blockers, Docker resource ownership, and evidence current. Fresh-context subagents perform most bounded research, implementation, test operation, and review.

Maximum active layout with four slots:

1. Root coordinator.
2. One bounded implementer or researcher.
3. One independent implementer or Docker runtime operator.
4. One read-only reviewer or tracker.

Do not fill slots without independent work. Do not give two writers the same file.

## Roles

- `coordinator`: owns plan, task IDs, interfaces, integration order, shared files, evidence validation, commits, and confirmation gates.
- `researcher`: read-only source or official-doc investigation. Returns exact citations and uncertainty.
- `implementer`: owns one bounded file scope and one acceptance command. Uses an isolated worktree when another writer is active.
- `runtime`: owns Docker mutations, container names, volumes, ports, test sequence, resource samples, and live Citadel plus provider log followers.
- `reviewer`: read-only intent, contract, security, and regression review. Fresh Eyes and Red Team use separate fresh contexts.
- `release`: owns release-readiness evidence. Never deploys, publishes, pushes, merges, tags, or mutates production without the required user gate.

## Assignment contract

Every delegated task includes:

```text
Task ID:
Owner role:
Task type: reason | execute | resolve | review | track
Model:
Reasoning effort:
Model reason:
Goal:
Read-only or write:
Exact file scope:
Dependencies:
Acceptance command:
Stop conditions:
Required handoff:
```

One task means one defect, one interface slice, one Docker phase, or one review question. Start a fresh agent for unrelated work.

Model routing and the current task index live in `agents/model-routing.md`. Root updates the index when a task changes owner, model, dependency, or status.

## Coordination rules

- Root claims tasks in `status.md` before writers edit.
- Shared interfaces are serialized. Architect or coordinator updates the contract before dependent implementation.
- Parallel work needs disjoint file scopes. If scopes collide, root orders them and transfers ownership through a handoff.
- One runtime agent mutates Docker at a time. Other agents inspect source or existing outputs only.
- Runtime agent starts Citadel and provider log followers before each Docker phase, polls both during the phase, and classifies both after it.
- Root reproduces P0 and P1 findings before recording VERIFIED or assigning a fix.
- Root reads every required skill instruction itself. Skill interpretation is not delegated.
- Root integrates and commits one change at a time after Docker verification and independent review.
- External GitHub, deployment, publication, release, migration, credential, and production actions keep their explicit gates.

## Handoff contract

Every agent returns:

```text
Task ID:
From owner:
To owner:
Status: Completed | In Progress | Blocked | Planned
Scope:
Files changed:
Interfaces changed:
Verification command and exact result:
Evidence and blind spots:
Known blockers:
Next action:
```

Root writes the compact result into `status.md`, `agents/blockers.md`, an interface or decision record when applicable, and the active handoff. Chat output alone is not durable coordination.

## Fresh-session gate

When root context becomes large:

1. Stop new implementation and log followers.
2. Preserve running containers and volumes unless cleanup is explicitly part of the task.
3. Update `status.md`, blockers, GitHub index, and one active handoff with the exact first command.
4. Record dirty worktrees, container state, verified results, failed results, blind spots, and forbidden actions.
5. Start a fresh root session. Resume from repository files and spawn new bounded agents.
