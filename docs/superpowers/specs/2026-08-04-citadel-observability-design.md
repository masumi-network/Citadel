# Citadel observability: instrument the vault before instrumenting agents

Date: 2026-08-04
Status: design agreed, not implemented
Related: ADR-0009 (read isolation), ADR-0010 (rebuildable projections),
ADR-0015 (one process owns the graph), ADR-0020 (Knowledge Index is an exact scan)

## Goal

Give Citadel operational visibility into its own behaviour, using OpenTelemetry,
with a self-hosted backend. Agent-facing telemetry is explicitly out of scope for
v1 and is discussed only where it constrains v1 choices.

## Why this, and why now

The motivating request was a dashboard for agents that connect to Citadel for
shared memory: traceability, discoverability, inference visibility, one seat per
agent. That remains the destination. This spec deliberately does not start there.

Two things reordered the work.

First, **the destination is half-built already.** ADR-0011 defines a **Shared
Session Trace**: a route an agent took, volunteered to the org, stored in
`session-traces`, discoverable through `citadel_search`, carrying its author
**Seat** "so it can be followed up on and so bad guidance is attributable"
(CONTEXT.md:72). That is agent traceability and discoverability at the knowledge
layer. What is missing beneath it is a metrics layer and a UI.

Second, **the same class of blindness that would justify the product is already
costing us.** CONTEXT.md:338 records six surfaces in one day reporting success
for work that did not happen. ADR-0020 added three more: search telemetry that
drops the query at the write, mesh counters that were restart-scoped under
totals-shaped names, and a per-dataset search fan-out issuing N redundant full
scans that no counter reports. These are not a backlog of unrelated bugs; they
are one missing capability.

So v1's first user is us, debugging the vault. The same pipes serve seats later.
That ordering means the work pays off against current release blockers instead of
competing with them, and it is independent of the read-boundary direction
ADR-0021 proposes, because it introduces no new content-bearing read surface.

## Decisions

1. **v1 traces Citadel's own operations only.** Not agent telemetry.
2. **The UI is the tool's own**, run as its own service behind auth and linked
   from Citadel. Citadel's frontend exists twice already (`kb/static/app.js`, a
   4,811-line vanilla JS app, and the `web/` Next.js port); a dashboard built in
   either would have to be built twice or deepen the divergence.
3. **Self-host OpenLIT + ClickHouse.** Two new Railway services, roughly
   $25-45/month, ~3GB RAM.
4. **`capture_message_content=False`.** OpenLIT defaults it on.
5. **30-day ClickHouse TTL on spans.**
6. **Telemetry fails open**, deliberately inverting this codebase's fail-closed
   default.

### Why self-hosted rather than a free hosted backend

Grafana Cloud's free tier would cost nothing, offers a Switzerland region (AWS
eu-central-2, relevant to utxo AG in Zug), and retains 50GB of traces a month.
The v1 goals need none of the sensitive fields, so a fail-closed allowlist in an
OTel Collector could make export safe in principle: the `redaction` processor
with `allow_all_keys:false` deletes any attribute not explicitly allowed.

It was rejected because that redactor becomes security-critical code, and this
repo has three confirmed cases of guards that shipped inert. A redactor that
silently passes everything looks identical to one that is working. Self-hosting
removes the boundary rather than guarding it, and $25-45/month is cheap against
building and continuously proving a filter. If the cost ever stops being
acceptable, the spans are portable OTLP and the backend is a URL change.

### Options rejected, with reasons

- **Tetragon.** Hard blocker. Its container install requires
  `--privileged --pid=host --cgroupns=host` plus a host BTF mount; Railway
  provides no privileged mode. Independently, it emits syscall and process
  security events, not retrieval latency or LLM cost.
- **RisingWave.** No stream to process at 12 seats. Every aggregate wanted here
  is a millisecond ClickHouse query. A second stateful database for no marginal
  capability.
- **OpenTelemetry into the Postgres we already run.** No maintained path exists:
  the collector-contrib Postgres exporter request was closed as not planned, and
  Promscale is discontinued with its extension archived in April 2024. The only
  UI reading Postgres directly is a 32-star Jaeger storage plugin. This option's
  sole appeal was avoiding a new datastore, and it cost building a UI, which
  decision 2 rules out anyway.
- **Grafana.** Not rejected, deferred. It runs on Railway today and Grafana Labs
  ships an official ClickHouse datasource, so it drops onto this stack whenever
  OpenLIT's own UI stops being enough. No reason to run two dashboards on day one.

### NetBird: a real gap, deliberately deferred

NetBird's Agent Network does per-agent identity against an IdP and keyless LLM
access, holding the provider API key server-side with per-identity policies and
spend caps. It is not observability, but it names something the agent plan needs
and Citadel does not cover: **seat tokens govern access to Citadel; nothing
governs the OpenRouter key or per-agent LLM spend.** That is invisible today
because Citadel makes the LLM calls. When agents arrive with their own inference,
credential custody and cost limits become real. Phase 2, not v1: it is Beta, and
most agents run on seat-holders' machines rather than on Railway.

## Architecture

```
Citadel (existing web service)
  ├─ openlit SDK  → capture_message_content=False, local pricing_json
  ├─ otel-instrumentation-fastapi    (request spans)
  └─ otel-instrumentation-asyncpg    (pgvector queries cognee makes)
        │  OTLP/HTTP over Railway private networking
        ▼
openlit service  (ghcr.io/openlit/openlit)
  ├─ OTLP receiver :4318
  ├─ Next.js UI :3000  ← no public domain until auth is resolved
  └─ SQLite app DB      ← volume at /app/client/data
        │
        ▼
clickhouse service  ← volume, 30-day TTL on spans
```

The OpenLIT SDK is the standard opentelemetry-python SDK: `openlit.init()` adds
an OTLP exporter behind a `BatchSpanProcessor` and reuses an existing
TracerProvider if present. Its spans use official `gen_ai.*` semantic
conventions, not a proprietary format, so lock-in is close to zero. This matters
because the project is effectively 2-3 maintainers (Apache-2.0, active, ~2.7k
stars): if it stalls, we keep the instrumentation and swap the viewer.

### Attachment points

Instrumentation attaches to chokepoints that already exist, so this is additive
rather than invasive:

- `Citadel.search` (`kb/service.py:137`), whose own comment calls it "the single
  chokepoint every read path funnels through"
- `Citadel.ingest` (`kb/service.py:66`), the write equivalent
- `openrouter_chat` (`kb/llm_enrichment.py:100`), covering every Citadel-native LLM call
- `_run_stages_async` (`scripts/run_railway.py:543+`), which already logs
  starting/ok/FAILED per stage, so spans map 1:1
- Two `@app.middleware("http")` already exist (`kb/server.py:2862, 2885`)

One existing helper is **extended, not replaced**: `_log_search_timing`
(`kb/cognee_client.py:675-699`) already owns the setup-versus-recall split. It
becomes span events. A parallel timer would drift from it.

## Span tree

### Search

```
citadel.search.request                    [middleware, kb/server.py:2862]
│   tool_name, dataset_count, top_k, timed_out, result_count
├── citadel.search.budget                 [_search_within_budget :203-218]
│   │   timeout_s=20.0, timed_out
│   └── citadel.search.dataset  × N       [gather branch :1969-1979]
│       │   dataset, result_count, session_scoped
│       └── cognee.recall                 [cognee_client.py:591-673]
│               dataset, top_k, query_type, setup_ms
├── citadel.search.shape                  [:6500-6535]
│       candidates_fetched, candidates_matched
├── citadel.search.drilldown              [:6550-6607]
│       unique_ids, denied, deadline_expired
└── citadel.search.telemetry_write        [:2230-2298]
        landed(bool)
```

`citadel.search.dataset` is the highest-value span in the design. Only merged
wall time is measured today, so a slow **Central** is indistinguishable from a
slow seat **Node**, and the N-way fan-out ADR-0020 identified is inferred rather
than measured.

### Ingest and cognify

```
citadel.ingest                 write_target_count, accepted, reason, data_bytes
├── learning.learn  × target
│   ├── security.scan          [learning.py:106-117]
│   ├── enrichment.llm         [learning.py:122]
│   └── cognee.add  × chunk    [cognee_client.py:512]
├── cognee.cognify             datasets, force, writer_lock_wait_ms, duration
├── cognee.cognify.background  task-lifetime: created → finished/failed
└── citadel.cognify.verify     search_hit, attempts, graph_grew
```

`writer_lock_wait_ms` (acquisition at `cognee_client.py:1739`) is invisible
today: a cognify queued minutes behind an evolve pass looks identical to a fast
one. `cognee.cognify.background` is a task-lifetime span rather than a completion
log because the current success line (`:585`) fires only on completion, so a task
that dies with the process leaves nothing. That is the incident documented at `:580-585`.

### Evolve

```
citadel.evolve.pass            writer_lock_hold_ms, phase1_exit_code, canary_ok
├── evolve.stage.{github_sync|repo_content_sync|self_improve|promotion|linear_sync}
│       status ∈ {skipped_env, ran_empty, ran_with_work, failed}
│       exit_code, error_type
├── evolve.cognify  + canary child
└── promotion.seat  × seat     candidates, promoted, queued, dry_run
    └── promotion.classify     [already to_thread'd, promotion.py:441]
```

The four-way `status` is load-bearing. Promotion-disabled returns `ok:true`
(`promotion.py:676-686`) and linear_sync no-ops on a missing key
(`run_railway.py:335-347`); both exit 0 and both read as success today. A binary
status would reproduce, inside the telemetry, the exact defect the telemetry
exists to detect.

### LLM cost

`llm.openrouter` at `kb/llm_enrichment.py:100` covers every Citadel-native LLM
call: digest, enrichment, self-improve, promotion. `model` and `operation` are
already parameters, and the OpenRouter response carries a `usage` token block the
code currently discards (`:151-158` reads only `choices`). Real token attribution
is one field-read away.

This also sidesteps a trap in OpenLIT's own cost calculation: its pricing lookup
strips one provider prefix, so `openrouter/deepseek/deepseek-v4-flash` resolves
to `deepseek/deepseek-v4-flash`, which is absent from the default pricing table.
It does not error. It reports zero. Using the `usage` block we already receive
avoids depending on that lookup at all for our own calls.

**Stated blind spot.** cognee's internal LLM calls (cognify graph extraction) go
through litellm inside the vendored package and are invisible at Citadel's
boundary, as `cognee_client.py:246-249` states. OpenLIT's litellm
auto-instrumentation is the plausible seam, but that it works through cognee's
call path is INFERRED, not verified. Treat it as a spike, not a commitment.

## Data flow

```
span created (µs, on the loop)
   → BatchSpanProcessor queue, maxsize 2048     ← non-blocking deque append
   → daemon export thread, 5s schedule           ← off the event loop entirely
   → OTLP/HTTP to openlit.railway.internal:4318  ← private network, never public
   → ClickHouse, 30-day TTL
```

Two correlation hooks ride channels that already exist:

- **Across the MCP loopback hop.** An MCP tool call goes async handler →
  `to_thread` → sync `urlopen` back into the same process over HTTP
  (`kb/mcp_server.py:757-802`) with automatic retries on the search POST
  (`:793-800`). A retried search currently looks like one slow call. `traceparent`
  rides the same `extra_headers` channel as the existing `X-Citadel-MCP-Tool`
  header (`:777-780`).
- **Into the existing dashboard.** `MeshState._record_event` takes free-form
  `details` (`kb/mesh.py:869-884`); adding `trace_id` links the timeline people
  already read to the trace. One field, no schema break.

## Failure handling

**Telemetry fails open, always.** This deliberately inverts the fail-closed
posture the rest of the codebase uses. An observability system that can take down
the vault is a liability.

| Failure | Behaviour |
|---|---|
| ClickHouse down | Exporter retries, then drops. App unaffected. |
| openlit service down | Same path. |
| Span queue full (2048) | Spans dropped with a logged warning; caller never blocks. |
| `openlit.init()` raises at boot | Caught; Citadel boots without tracing. |

The queue behaviour is VERIFIED from SDK source: `on_end` is a thread-safe deque
append plus an event flag, with no lock on the hot path, and export runs on a
daemon worker thread.

Three prohibitions, each from a known incident shape:

1. **No `force_flush()` or `shutdown()` in any request path.** Both block.
2. **No blocking network call at init.** `openlit.init()` defaults to a
   synchronous `requests.get` for pricing data; pass a local `pricing_json`.
3. **Span persistence never routes through `MeshState`** (shared asyncio lock,
   `kb/mesh.py:49`) **or `AccessStore.record_event`** (full-file rewrite per
   event, `kb/access.py:589-592`, already on the search hot path).

## Content rule

Every span attribute must meet the presence-only bar of ADR-0009, because a trace
backend is a shared reader.

**Keep off:** query text in any form, including redacted or truncated (use
`query_sha256` + `query_length`; precedent at `kb/server.py:6631-6632`); hit ids,
urls, doc types, per-hit dataset (counts are fine); `seat_slug`, `actor_id`,
`session_id`, `client_hint`; document text, first lines, previews; LLM prompts and
completions; raw error strings, which pass through `redact_secrets` first as mesh
does (`kb/mesh.py:776-780`).

**Safe:** dataset names (presence is universal under ADR-0009), timings, counts,
booleans, stage names, exit codes, model ids, canary markers.

`capture_message_content=False` covers OpenLIT's side. Hand-added attributes must
obey the same rule independently. The flag does not police them.

Chosen over a debug flag because the asymmetry is stark: enabling content capture
later is one config line, while un-leaking content already written to ClickHouse
and rendered in a UI is a deletion exercise plus a disclosure question. A
flag-based middle path was rejected specifically because a debug flag nobody sets
is indistinguishable from one that does not work, and this repo has three
confirmed inert guards.

## Testing

1. **Prove content is not captured, in both directions.** Plant a marker string
   in a search query, run it, assert zero rows in ClickHouse. Then flip
   `capture_message_content=True`, re-run, and assert the marker *does* appear. A
   test that only checks absence passes when tracing is broken entirely. The
   `True` case exists only inside this test; the shipped configuration is always
   `False`.
2. **Prove token attribution is not silently zero.** Assert `usage` tokens > 0 on
   a real `llm.openrouter` span.
3. **Prove the four-way stage status discriminates.** Run with promotion
   disabled; assert `status == skipped_env`, not `ran_empty`.
4. **Prove the app survives the backend being down.** Stop ClickHouse; assert
   `/search` still returns 200 with an unchanged response body.
5. **Measure overhead rather than assuming it.** Search p95 with and without
   instrumentation. A third-party microbenchmark suggests ~35µs per span
   operation, which at 40-60 spans per request is ~2-3ms against a 311-627ms
   baseline. That figure is an extrapolation, not our number.

## Implementation shape

This spec is deliberately two implementation plans, not one. The seam is clean
and the first half is independently useful.

**Plan A, infrastructure.** Stand up ClickHouse and the openlit service on
Railway, resolve the UI authentication question below, confirm the stack comes up
without the compose-mounted asset files, and prove the app survives the backend
being down (test 4). Deliverable: an OTLP endpoint on the private network that
accepts spans and a UI nobody can reach without auth.

**Plan B, instrumentation.** Add the SDK and the two OTel instrumentation
packages, wire the span tree, extend `_log_search_timing`, read the `usage` block
in `openrouter_chat`, and land tests 1, 2, 3 and 5. Deliverable: the fan-out,
writer-lock waits, and per-call LLM cost become visible.

Plan A ships nothing user-visible and can proceed while Plan B is still being
designed. Plan B is worthless without Plan A, so the order is fixed.

## Open items

- **OpenLIT UI authentication is not determined.** It ships auth against a SQLite
  app DB; whether that is sufficient for a public Railway domain serving
  operational data, or whether it must sit behind Citadel's access model, has not
  been verified. Resolve before exposing a domain.
- **No Railway template exists for OpenLIT.** Its compose file mounts three local
  files from `./assets/` (ClickHouse config, ClickHouse init script, collector
  config) which cannot be mounted into a template image on Railway. Whether the
  stack comes up unmodified is NOT DETERMINED; a derived image may be required.
- **litellm instrumentation through cognee's vendored call path is unverified.**
- **cognee's own `queries` table** already stores raw query strings durably, on by
  default (ADR-0020). This design must not add a second copy, and whether to
  disable or own that surface is a separate decision.

## Out of scope, found while designing this

Two live defects surfaced during the instrumentation survey. Neither is an
observability problem and neither is addressed here.

- **Enrichment blocks the event loop.** `kb/learning.py:122` calls
  `enrich_source_material` → `openrouter_chat`, a synchronous urllib POST
  (`kb/llm_enrichment.py:100-158`, `timeout=60` plus retries), directly from the
  async `learn()` with no `to_thread`. On one shared loop that stalls every
  request for up to a minute per document. Promotion does this correctly
  (`kb/promotion.py:441`); enrichment does not. Currently masked because
  enrichment is off by default.
- **The audit store rewrites itself on every search.** `kb/access.py:589-592`
  performs a full load-and-save of the entire access-store JSON per event, and
  `/search` writes an audit row per request (`kb/server.py:6629-6656`).
