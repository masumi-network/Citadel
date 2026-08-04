# Telemetry Is Self-Hosted, Because A Redactor Would Be Security-Critical Code

- Status: Accepted
- Date: 2026-08-04
- Relates to: [ADR-0009](0009-mesh-read-isolation-presence-vs-content.md),
  [ADR-0015](0015-one-process-owns-the-graph.md).
- Design detail: `docs/superpowers/specs/2026-08-04-citadel-observability-design.md`.

Citadel has no OpenTelemetry, Prometheus, statsd or Sentry dependency anywhere.
The gap is not cosmetic: CONTEXT.md records a recurring defect class where
surfaces report success for work that did not happen, and ADR-0020 added three
more instances, including a per-dataset search fan-out that issues N full scans
per user query and which no counter reports. Those are observability failures,
so the first user of this work is us, debugging the vault, and agent-facing
telemetry is deliberately out of scope for v1.

Instrumentation is the OpenLIT SDK, which is the standard opentelemetry-python
SDK underneath and emits official `gen_ai.*` semantic conventions rather than a
proprietary format. Lock-in is therefore close to zero, which matters because the
project has roughly two or three maintainers: if it stalls we keep the
instrumentation and swap the viewer.

**Decision**

- **The backend is self-hosted**: OpenLIT plus ClickHouse, two Railway services,
  roughly $25 to $45 a month. Spans never leave the node.
- **`capture_message_content=False`.** OpenLIT defaults it on, which would copy
  prompts, completions and retrieved document text into span attributes.
  Enabling it later is one config line; un-leaking content already written to a
  datastore and rendered in a UI is not. Start from the cheap direction.
- **Telemetry fails open**, deliberately inverting this codebase's fail-closed
  posture everywhere else. An observability system that can take down the vault
  is a liability. Span export stays on `BatchSpanProcessor`'s background thread,
  `force_flush()` and `shutdown()` never appear in a request path, and span
  persistence never routes through `MeshState` (shared asyncio lock) or
  `AccessStore.record_event` (full-file rewrite per event, already on the search
  hot path).
- **Spans are 30-day TTL** in ClickHouse.
- **The UI is the tool's own**, run as its own service, not rebuilt inside
  Citadel. The frontend already exists twice (`kb/static/app.js` and the `web/`
  Next.js port); a dashboard built in either would have to be built twice.

**Why not a free hosted backend**

Grafana Cloud's free tier costs nothing, retains 50GB of traces a month, and
offers a Switzerland region, which is a real fit for a Swiss entity. The v1
goals need none of the sensitive fields, so a fail-closed allowlist in an
OpenTelemetry Collector could make export safe in principle: the `redaction`
processor with `allow_all_keys:false` deletes any attribute not explicitly
listed.

It was rejected because that redactor becomes security-critical code, and this
repository has three confirmed cases of guards that shipped inert. A redactor
that silently passes everything looks identical to one that is working, and the
cost of being wrong is content leaving the node. Self-hosting removes the
boundary instead of guarding it. Paying $25 to $45 a month is cheaper than
building a filter and continuously proving it still fires.

**Consequences**

- The first test written is a two-directional content check: plant a marker
  string in a query, assert it is absent from ClickHouse, then flip the capture
  flag on and assert it appears. A test that only checks absence passes when
  tracing is broken entirely.
- Cost attribution needs care. OpenLIT's pricing lookup strips one provider
  prefix, so `openrouter/deepseek/deepseek-v4-flash` resolves to a key absent
  from its default table and reports zero without erroring. Citadel's own LLM
  calls avoid the lookup entirely: `openrouter_chat`
  (`kb/llm_enrichment.py:100-158`) already receives the OpenRouter response,
  which carries a `usage` token block the code currently discards, reading only
  `choices` at `:151-158`.
- `openlit.init()` performs a synchronous `requests.get` for pricing data at
  startup. On a single shared event loop that is a blocking network call at
  boot, so a local pricing file is passed instead.
- pgvector queries are not traced for free. cognee uses asyncpg through
  SQLAlchemy while OpenLIT ships psycopg instrumentors, so
  `opentelemetry-instrumentation-asyncpg` is added separately.
- Ruled out and recorded so they are not re-proposed: **Tetragon** (its container
  install requires privileged mode plus host namespaces, which Railway does not
  offer, and it emits process security events rather than latency or cost),
  **RisingWave** (no stream to process at this scale; every aggregate wanted here
  is a millisecond ClickHouse query), and **OpenTelemetry into the existing
  Postgres** (no maintained storage path exists; the collector-contrib exporter
  request was closed as not planned and Promscale is discontinued).
- **Grafana is deferred, not rejected.** It runs on Railway today and Grafana
  Labs ships an official ClickHouse datasource, so it drops onto this stack
  whenever OpenLIT's own UI stops being sufficient.
- **NetBird is a separate opportunity, not observability.** Its Agent Network
  holds provider API keys server-side with per-identity policies and spend caps.
  Seat tokens govern access to Citadel and govern nothing about the OpenRouter
  key or per-agent LLM spend. That gap becomes real when agents arrive with their
  own inference.
