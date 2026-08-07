# Dataset-Level Cognify Retry Queue

- Status: Accepted
- Date: 2026-08-07
- Relates to: [#228](https://github.com/masumi-network/Citadel/issues/228),
  [ADR-0015](0015-one-process-owns-the-graph.md)

## Context

`cognee.add()` writes relational and vector state. The graph projection is
written later by `cognify()`. Before this decision, `schedule_cognify()` created
an in-process task and logged failures. A process exit, a missing event loop, or
a failed cognify could therefore leave accepted data without a durable retry
record.

## Decision

Citadel uses a content-free, dataset-level retry queue stored at
`CITADEL_COGNIFY_QUEUE_PATH`. The default path is `cognify_queue.json` under the
configured Citadel state root. The production path belongs on the persistent
Railway volume.

Each record contains only dataset names and retry metadata. Queue mutations use
a sidecar file lock, atomic replacement, and an fsynced file and directory.
Workers claim leases before cognifying. Successful work is acknowledged;
failures are returned with bounded exponential backoff; expired leases become
available again. A failed job schedules a wakeup in the live process, so it can
retry without a new ingest or a process restart. Long-running cognify work
renews its lease; shutdown cancellation returns active work to the queue. The
web lifespan starts a drain on boot, so work queued by a previous process can
resume.

The queue does not replace the in-process graph writer lock. It controls durable
ownership of deferred work; the existing writer lock still serializes graph
writes within a process.

## Options

- In-memory task tracking: rejected because it loses work on process exit.
- Direct retry in the request: rejected because graph writes would extend write
  latency and still would not survive a process failure.
- A database queue: deferred. The current queue needs only small dataset-level
  records, and the state-volume file matches the existing deployment state
  pattern without adding schema migration work to #228.

## Consequences

- `schedule_cognify()` reports whether the durable queue accepted the work.
- Queue corruption or an unavailable queue fails closed and logs an explicit
  error. It is not interpreted as an empty queue.
- The queue stores no document bodies, IDs, or user content.
- A live worker keeps retry timing and lease ownership durable across failures;
  the lease heartbeat prevents a long cognify from being reclaimed by another
  worker while it is still running.
- A production repair and post-repair census are still required for historical
  zero-chunk documents. This decision does not close that operational part of
  #228.

## Verification

- [VERIFIED] Queue tests cover atomic writes, cross-process enqueue, leases,
  lease renewal, backoff, stale-lease recovery, acknowledgement, and malformed
  state.
- [VERIFIED] Client tests cover no-loop persistence, live failure retry,
  lease renewal during long cognify, cancellation rescheduling, startup drain,
  and truthful failure reporting.
