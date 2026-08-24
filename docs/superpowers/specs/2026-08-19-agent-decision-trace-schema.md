# Agent Decision Trace schema v1 (Autonomy slice)

**Date:** 2026-08-19
**Status:** proposed

Goal: make arbitration, promotion, and dedupe decisions observable and auditable as the core of self-maintenance.

## 1. Scope

This trace schema covers one autonomous decision cycle:

- ingest candidates
- score/dedupe arbitration
- promotion eligibility check
- candidate replacement / retire flow
- repair retry and fallback actions

It is in-vocab for:

- OpenTelemetry spans in the web and worker processes
- OpenLLMetry spans around model calls
- durable Postgres trace ledger

## 2. Trace identity

Every cycle has one `global_trace_id` (ULID / UUID v7).

Every decision action has one `decision_trace_id` (UUID v7).

Every span and child span must include:

- `citadel.trace.global_id`
- `citadel.decision_id` (for event spans only)
- `citadel.run_id` (for long loops)
- `citadel.span_level`: `agent`, `arbitration`, `maintenance`, `llm`

## 3. Event types

### 3.1 Lifecycle events

- `agent.lifecycle.started`
- `agent.lifecycle.finished`
- `agent.lifecycle.failed`
- `agent.lifecycle.retry_scheduled`

### 3.2 Arbitration events

- `agent.arbitration.started`
- `agent.arbitration.candidate.generated`
- `agent.arbitration.duplicate_matched`
- `agent.arbitration.winner.selected`
- `agent.arbitration.rejection`

### 3.3 Maintenance events

- `agent.maintenance.replace`
- `agent.maintenance.demote`
- `agent.maintenance.retain`
- `agent.maintenance.repair_started`
- `agent.maintenance.repair_succeeded`
- `agent.maintenance.repair_failed`

## 4. Canonical event payload (JSON)

```json
{
  "schema_version": 1,
  "event_id": "evt_decision_01H... ",
  "event_type": "agent.arbitration.winner.selected",
  "global_trace_id": "01H...",
  "run_id": "01H...",
  "decision_trace_id": "01H...",
  "occurred_at": "2026-08-19T12:34:56.789Z",
  "node_id": "seat:sarthi",
  "agent": "promotion-agent-v2",
  "actor_type": "agent",
  "model": "mistral-small",
  "llm_span_id": "span_abc123",
  "tool_name": "promotion",
  "dataset": "masumi-network",
  "source_revision_new": "rev_9f1...c3",
  "source_revision_old": null,
  "claim_id": "claim_7a1...",
  "document_id": "doc_44...",
  "candidate_id": "cand_88...",
  "promoted_to_central": false,
  "replacement_action": null,
  "source_updated_at": "2026-08-18T10:00:00Z",
  "freshness_delta_h": 3.2,
  "novelty_score": 0.41,
  "confidence": 0.94,
  "decision_thresholds": {
    "freshness_h": 12,
    "novelty": 0.15,
    "confidence": 0.8
  },
  "arbitration": {
    "score": 0.94,
    "evidence_count": 4,
    "duplicate_similarity": 0.87,
    "winner_reason": "confidence_high_fresh_and_novel",
    "conflict_resolution": {
      "rule": "confidence_then_freshness_then_node",
      "losers": ["evt_decision_01H..."]
    }
  },
  "retention": {
    "action": "replace_old",
    "retire_event_id": "evt_decision_01H...",
    "retained_rows": [
      {
        "old_document_id": "doc_12...",
        "reason": "duplicated_content"
      }
    ]
  },
  "status": "ok",
  "outcome": {
    "accepted": true,
    "accepted_as": "central_candidate",
    "next_action": "await_approval_or_auto_promote"
  },
  "request_id": "req_abc...",
  "trace_link": {
    "otel_trace_id": "4bf9...",
    "otel_span_id": "1f2d...",
    "parent_trace_id": null
  },
  "payload_hash": "sha256:..."
}
```

Mandatory fields per row:

- `schema_version`
- `event_id`
- `event_type`
- `global_trace_id`
- `run_id`
- `decision_trace_id`
- `occurred_at`
- `node_id`
- `actor_type`
- `model`
- `confidence`
- `candidate_id`
- `status`
- `payload_hash`

## 5. Postgres DDL

```sql
CREATE TABLE IF NOT EXISTS agent_decision_trace (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  schema_version INT NOT NULL DEFAULT 1,
  global_trace_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  decision_trace_id TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  node_id TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  agent TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  dataset TEXT NOT NULL,
  claim_id TEXT,
  document_id TEXT,
  candidate_id TEXT,
  source_revision_new TEXT NOT NULL,
  source_revision_old TEXT,
  freshness_delta_h DOUBLE PRECISION,
  novelty_score DOUBLE PRECISION,
  confidence DOUBLE PRECISION NOT NULL,
  promoted_to_central BOOLEAN NOT NULL,
  replacement_action TEXT,
  status TEXT NOT NULL,
  payload JSONB NOT NULL,
  otel_trace_id TEXT,
  otel_span_id TEXT,
  llm_span_id TEXT,
  payload_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_decision_trace_global
ON agent_decision_trace (global_trace_id);
CREATE INDEX IF NOT EXISTS idx_agent_decision_trace_decision
ON agent_decision_trace (decision_trace_id);
CREATE INDEX IF NOT EXISTS idx_agent_decision_trace_candidate
ON agent_decision_trace (candidate_id);
CREATE INDEX IF NOT EXISTS idx_agent_decision_trace_occurred
ON agent_decision_trace (occurred_at DESC);
```

## 6. OTel + OpenLLMetry attribute map

### 6.1 OTel span attributes

- `citadel.event.id` → `event_id`
- `citadel.event.type` → `event_type`
- `citadel.trace.global_id` → `global_trace_id`
- `citadel.trace.run_id` → `run_id`
- `citadel.trace.decision_id` → `decision_trace_id`
- `citadel.node.id` → `node_id`
- `citadel.actor.type` → `actor_type`
- `citadel.confidence` → `confidence`
- `citadel.freshness.hours` → `freshness_delta_h`
- `citadel.novelty.score` → `novelty_score`
- `citadel.candidate.id` → `candidate_id`
- `citadel.source.revision.new` → `source_revision_new`
- `citadel.source.revision.old` → `source_revision_old`
- `citadel.promote` → `promoted_to_central`
- `citadel.replacement.action` → `replacement_action`
- `citadel.outcome` → JSON stringified `outcome`
- `citadel.payload.hash` → `payload_hash`

### 6.2 OpenLLMetry (llm span)

- `openai.request.model`
- `openai.response.model`
- `openai.token_usage.total`
- `openai.token_usage.prompt`
- `openai.token_usage.completion`
- `gen_ai.choice.index`
- `gen_ai.response.finish_reason`
- `gen_ai.system` or provider id from current config

## 7. Replacement invariants

- duplicate match requires retention action before acceptance of the winner
- every acceptance writes exactly one `agent.arbitration.winner.selected`
- every replacement writes one `agent.maintenance.replace`
- every retire writes one `agent.maintenance.demote`
- old source may not be deleted; it must be marked retired by `retire_event_id`
- all replacement chains are linkable through `global_trace_id`

## 8. Poisoning and bad-context detection (v1)

If a context path injects untrusted facts, the first diagnosis must answer:

- Which input changed before the bad output?
- Which trace made the choice?
- What replacement was blocked?
- What source was retired?

Add two required event classes for this state:

- `agent.lifecycle.anomaly_detected`
- `agent.maintenance.quarantine`

Each must include:

- `trace_guard`: `policy_violation`, `conflict_ticker`, `context_delta`, `low_signal`, `tool_error`
- `context_hash_before`
- `context_hash_after`
- `quarantine_reason`
- `quarantine_until`
- `rollback_to_trace_id` (if replayable)

If an anomaly reaches threshold:

1. emit `agent.lifecycle.anomaly_detected`
2. stop `auto_promote` for the same `global_trace_id`
3. emit `agent.maintenance.quarantine`
4. require explicit human action to resume

## 9. Autonomy loop hooks for this slice

1. ingest phase emits `agent.lifecycle.started`
2. candidate generation emits `agent.arbitration.candidate.generated`
3. duplicate arbitration emits `agent.arbitration.duplicate_matched`
4. winner write emits `agent.arbitration.winner.selected`
5. if replacing, emit `agent.maintenance.replace`
6. write audit summary `agent.lifecycle.finished` with `next_action`

This forms the first self-maintaining brain slice; later slices add repair policies and auto-tuning on this trace feed.
