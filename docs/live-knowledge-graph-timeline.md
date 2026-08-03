# Live Knowledge Graph Timeline

Citadel makes knowledge movement visible while keeping retrieval fast. The current implementation deepens the existing mesh activity stream into a live, indexed timeline that can drive graph focus and source inspection.

## Product Map

The activity surface becomes a three-part workspace:

- Timeline: newest source, chunk, retrieval, conflict, and agent events, streamed live and resumable by event id.
- Local graph focus: the graph centers on the selected event's source, dataset, document, query, or conflict neighborhood.
- Inspector: provenance, freshness, chunk/index counters, citations, actor hints, and failure details for the selected event.

The goal is not a giant static graph. The graph should answer "what changed, why, and what can I trust right now?"

## Event Model

Each mesh event keeps the current stable fields (`id`, `type`, `message`, `details`, `created_at`) and adds a normalized timeline envelope:

- `kind`: source sync, chunk indexing, graph update, retrieval, feedback, conflict, agent action, digest, or error.
- `status`: synced, indexed, searched, recorded, detected, failed, or pending.
- `dataset`: the affected dataset when known.
- `source`: GitHub, Obsidian, manual ingest, search, feedback, conflict detector, or runtime.
- `metrics`: small numeric counters only, such as chunks, results, repos, accepted notes, conflicts, or failures.

Events stay small. Full document bodies, raw source text, and sensitive request payloads never belong in the live event payload.

## Fast Read Path

Search and graph rendering should not fetch raw sources synchronously:

1. Source sync records source metadata and stores raw snapshots outside the live payload.
2. Ingestion chunks content into indexed units with hashes, timestamps, dataset, source id, and tags.
3. Vector and graph indexes are updated incrementally.
4. Search reads from the indexes and returns citations immediately.
5. Full source content is fetched lazily only when the user opens a citation or source detail.

The dashboard should show freshness from the index state: last indexed time, indexed chunks, pending chunks, failed chunks, and latest event id.

## Live Update Path

The runtime already exposes `/events` over SSE. The timeline should use that channel for live updates, and `/api/knowledge/events` for page load, backfill, and resume:

1. Source connectors emit `source_synced`.
2. Ingestion emits `chunk_indexed`.
3. Entity extraction and graph projection emit graph update events as they become available.
4. Conflict detection emits `conflict_detected`.
5. Search and agent actions emit retrieval/action events with compact metrics.
6. The UI merges events by monotonic id and focuses the selected graph neighborhood.

This keeps the first implementation simple while leaving room for a durable queue later.

## Shipped Surface

- Backend: `GET /api/knowledge/events` returns a bounded, newest-first event timeline with `after_id`, `limit`, `type`, and `kind` filters.
- Snapshot stats: `/api/mesh` includes `last_indexed_at` and `latest_event_id`; restart-scoped counters (`searches`, `feedback`, `upgrades`, `errors`, `pending_chunks`, `indexed_chunks`) live under `stats.since_restart` (#196, ADR-0020), and `failed_chunks` is gone — it duplicated `errors` (#197). Top-level `indexed_chunks` was removed: it was assigned the same corpus figure as `nodes`, so it never counted chunks (ADR-0020).
- SSE: `/events` still sends live mesh updates; emitted events now include the normalized `timeline` envelope.
- UI: the Activity page is now the Live Knowledge Timeline with freshness counters, a selectable event list, event inspector, and graph focus.
- Operational check: the production GitHub sync cron was run manually on June 11, 2026 and ingested new source activity into the production Citadel service.

## Implementation Slices

1. Roadmap doc: shipped in `2ea4f46`.
2. Backend timeline API: shipped in `e17d9af`.
3. Frontend timeline: shipped in `b484817`.
4. Graph focus: shipped in `b484817` for dataset/source/vault/org event neighborhoods in the existing mesh projection.
5. Persistence upgrade: still open. Recent events are bounded in memory today; write them to JSONL or a database-backed store if restart recovery becomes required.

## Performance Rules

- Never rebuild the whole graph for one event.
- Never put raw document bodies into SSE payloads.
- Keep event payloads bounded and redact sensitive details.
- Search indexed chunks, not raw GitHub or Obsidian content.
- Update graph neighborhoods incrementally.
- Cap timeline backfill and use `after_id` for resume.
- Show stale, pending, and failed index state explicitly instead of hiding lag.
