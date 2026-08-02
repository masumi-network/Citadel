# Dashboard API contract

- Date: 2026-07-29
- Status: Reference for the Next.js port (ADR-0014)
- Source: `kb/static/app.js` (4,247 lines) and `kb/static/index.html`, read at commit time
- Scope: analysis only. Nothing here changed application code.

What this is for: the dashboard is being rewritten as React components. This records
which endpoint feeds which view, and, more importantly, **which response fields the UI
actually renders**, so the port can carry the four fields a view uses instead of
faithfully reproducing a thirty-field payload nobody reads.

Everything below was traced by reading call sites, including URLs built by string
concatenation, which a plain grep misses.

## Mechanics that apply everywhere

**One fetch helper.** `api(path, options)` at `app.js:567` sets
`Content-Type: application/json`, parses the body, and on a non-2xx throws an `Error`
carrying `message` (from `detail`, flattened if it is a Pydantic error array, else
`message`, else `"Request failed"`) and `status`.

**`error.status` is consulted in exactly one place**: a 429 from `/api/mesh/graph`,
which retries twice with jitter (`MESH_GRAPH_RETRY_BASES_MS = [400, 1200]`) before
degrading to a soft status with no toast. Everywhere else the status code is discarded
and only the message is shown.

**401 does not redirect, except at boot.** `loadSession()` catches any failure from
`GET /api/session` and does `window.location.assign("/login")`. Every other call that
401s renders its own error text in place and leaves the user on a dead page. A session
that expires mid-visit produces a screen of "Could not load ..." panels rather than a
sign-in prompt. **The port should fix this rather than reproduce it.**

**Role gating is client-side decoration over server enforcement.** `canUse(role)`
compares `state.role` against `roleOrder = {reader:1, writer:2, admin:3}`.
`applyAccessControls()` walks `[data-min-role]` and disables nav links, disables every
control inside a gated `form`/`fieldset`, and hides anything else. `setPage(name)`
resolves to the `locked` page when `canUse(page.dataset.minRole)` is false. None of this
is security; the endpoints enforce their own roles and the UI mirrors them, sometimes
incorrectly (see Gaps).

**Boot order** (`app.js`, bottom of file): `initializeGraph` → `resizeCanvas` →
`initializeThemeToggle` → `initializeSearchFilters` → `initializeHomeSearch` →
`initializeReviewControls` → `initializeNavigation` → `loadSession()` and then, in one
batch: `setPage(initialPage())`, `loadMesh()`, `loadKnowledgeGraph()`,
`loadGithubSync()`, `loadPromotionQueue()`, `loadSources()`, `loadObsidianSources()`,
`loadConflicts()`, plus `loadAccess()` and `loadSettings()` for admins, then
`connectEvents()`.

So **most views are already populated before they are opened**. Only six pages fetch on
activation (`setPage`): `home`, `review`, `access`, `agents`, `audit`, `settings`,
`conflicts`. `search`, `knowledge`, `events`, `ingest`, `feedback`, `sources` fetch
nothing when you navigate to them; they render from state loaded at boot.

**Live updates.** `connectEvents()` opens `EventSource("/events")`. A `snapshot` event
merges the graph directly; a `mesh-event` triggers `loadMesh(false)`. If `EventSource`
is unavailable, it falls back to `setInterval(() => loadMesh(false), 5000)`. That
5-second poll is the **only** timer in the app.

**Refresh button** (sidebar) re-runs `loadMesh`, `loadGithubSync`,
`loadObsidianSources`, `loadConflicts`, plus `loadSeatHome` when seated,
`loadKnowledgeGraph(true)` in knowledge mode, and `loadAccess` + `loadSettings` for
admins.

## Views

Pages are `[data-page]` sections in `index.html`. There are 15, not 11: the list below
adds `access`, `audit`, `settings` (sub-tabs of the Admin group) and `locked`.

### `home` — no role gate

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/me/summary` | `document_count` (fallback only), `recent_activity[]` (fallback only), `empty` | on view activation |
| `GET /api/mesh` | `stats.documents`, `stats.errors`, `events[].type`, `events[].message`, `events[].created_at`, `events[].dataset` | boot, SSE, refresh |
| `GET /api/promotion/pending?status=pending` | `items[]` (see Review for fields) | boot |
| `GET /api/sources` | `sources[].open_conflicts`, `.name`, `.id`, `.metadata.last_security_scan.{blocked,finding_count,highest_severity}` | boot |
| `GET /api/github-sync` | `last_checked_at` | boot, refresh |

`/api/me/summary` returns 11 fields; Home renders 3, and two of those only as a fallback
when the mesh has no events. `node_label`, `node_dataset`, `search_datasets`,
`pending_promotions`, `last_ingest_at` and `checklist` are fetched and dropped. The
checklist died with the phase 3 rebuild.

Errors: a failed `/api/me/summary` shows a toast and leaves the numbers at their
last-rendered values. `/api/sources` failures are swallowed to `[]`, so a broken sources
endpoint reads as "nothing is failing", which is exactly backwards. Empty: the Needs-you
list renders a plain sentence, not a card. Slow: Home paints immediately from whatever
state has arrived and re-renders as each fetch lands.

### `overview` — `data-min-role="admin"`, **no longer routed to**

Phase 2 removed it from the nav and `initialPage()` no longer returns it. It is
reachable only by typing `#overview`. Its markup and JS were deliberately left in place
because **the graph canvas lives inside this section**, not inside `knowledge` (see Dead
weight).

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/mesh` | `stats.{nodes,edges,documents,searches,feedback,upgrades,errors}`, `events[]`, `indexes[].{name,records,status}` | boot, SSE |
| `GET /api/github-sync` | `last_checked_at`, `tracked_repositories`, `run_improve` | boot |
| `GET /api/promotion/pending` | `items[].{seat_slug,preview,reference_reason,reference_status,id}`, `count` | boot |
| `GET /api/access` | `tokens[].{name,role,scopes,prefix,revoked_at}`, `principals[].length` | boot (admin) |

### `search` — no role gate

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `POST /search` | `results[]`, `sections.{central,session_traces,node}`, `note`, `known_datasets` | user submits, or a filter chip changes with a query present |
| `GET /api/documents/{id}` | `document.{title,path,id,body,content,source,source_type,dataset,normalized_path,current_rev,rev,updated_at,metadata.checked_at,metadata.digest_at}` | user clicks "Preview source" |
| `GET /api/mesh` | (side effect) `loadMesh(false)` after every search | after each search |

Request body: `{query, top_k: 10}` plus `dataset` when a scope chip other than
"Everything" is on, plus `types[]` when type chips are on. `dataset` for "Central only"
is taken from `state.searchDatasets` (the first entry that is neither a `seat:` dataset
nor `session-traces`), falling back to the hardcoded `CENTRAL_DATASET =
"masumi-network"`.

Per-hit fields rendered: `_citadel.rank`, `_citadel.dataset`, `_citadel.trust` or
`_citadel.trust_tier`, `_citadel.provenance.{title,source,path,session_id,source_url}`,
`_citadel.content_sha256` (first 12 chars), `_citadel.retrieval.document_drilldown_available`,
`_citadel.document_endpoint`, top-level `score`, and the body text. The whole raw hit is
also dumped into a `<details>` block.

Errors: renders an error card with a Retry button that re-submits the form. 401/403 land
there too, with no sign-in prompt. Empty: an empty-state card that lists
`known_datasets` when present and offers "Review sources" and "Add note" buttons. Slow:
three skeleton cards, and no client-side timeout, so a search that exceeds the server's
budget shows skeletons until the server returns its own truncated payload.

`document_endpoint` is only followed when `retrieval.document_drilldown_available` is
`true` and the string starts with `/api/documents/` (`safeDocumentEndpoint`); external
`source_url` values are validated to http/https before becoming a link
(`safeExternalUrl`).

### `knowledge` (Explore) — no role gate

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/github-sync` | `org`, `tracked_repositories`, `last_checked_at`, `last_digest_at` | boot, refresh |
| `GET /api/sources?type=obsidian_vault` | `sources[].{name,documents,last_push_at,open_conflicts}`, `summary.{obsidian_documents,open_conflicts}` | boot |
| `GET /api/mesh` | `stats.documents`, `stats.since_restart.errors`, `indexes[]`, `events[]` | boot, SSE |

Nothing fetches on activation. Note the page shows sources and indexes; the mesh canvas
is not here.

### `events` (Activity) — no role gate

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/mesh` | `events[].{id,type,message,created_at,details.*,timeline?}`, `stats.{indexed_chunks,last_indexed_at}`, `stats.since_restart.{pending_chunks,errors}` | boot, SSE, refresh |

`timelineEnvelope(event)` prefers a server-provided `event.timeline` object and
otherwise synthesises `{kind, status, dataset, source, metrics}` from
`event.details.{status,dataset,org,vault_id,source,operation}`. `details.reason` is
surfaced on the card unless it equals `"accepted"`.

Renders the first 40 events. No pagination, no filters, no per-seat column.

### `review` — `data-min-role="writer"`

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/promotion/pending?status=pending` | `items[].{id,seat_slug,preview,reference_status,reference_reason,repo_hints,sensitive}`, `count` | on view activation, boot, Refresh button |
| `GET /api/sources` | as Home | on view activation, boot |
| `POST /api/promotion/pending/{id}/approve` | response discarded | user clicks Approve (admin only) |
| `POST /api/promotion/pending/{id}/reject` | response discarded | user clicks Reject (admin only) |

The queue endpoint is reader-gated and filtered to the caller's own seat by
`promotion_pending_filter_seat`. Approve and reject require **admin + `sources:sync`**,
so the buttons render only under `canUse("admin")`; writers see the rows with a "waiting
on an admin" chip. Both decisions go through `window.confirm()` first.

`PromotionPendingItem` declares 18 fields; `candidate_text` is stripped server-side by
`redact_pending_item`, so 17 arrive and the UI renders 7. `score`, `relevant`,
`delegate`, `status`, `created_at`, `decided_at`, `decided_by`, `decided_by_name`,
`candidate_hash` and `seat_dataset` are fetched and unused.

Errors: `state.promotions` resets to `[]` and the list shows "No promotions are waiting
for a decision", which is indistinguishable from success. Empty: a plain sentence.

### `ingest` (Write) — `data-min-role="writer"`

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `POST /ingest` | **none** | user submits |
| `GET /api/mesh` | side effect only | after a successful ingest |

Body: `{data, dataset, tags[]}`. The response (`accepted`, `reason`, `dataset`, `tags`)
is discarded entirely; success is signalled by clearing the textarea. A server that
accepts the request but rejects the note (`accepted: false`) is indistinguishable from a
clean save. **The port should render the result.**

### `feedback` — `data-min-role="writer"`

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `POST /feedback` | `recorded`, `improved` | user submits |
| `GET /api/mesh` | side effect only | after submit |

Body: `{qa_id, score, text, dataset, session_id}`. Reached from Search via "Use for
feedback", which fills `qa_id` from `findFeedbackId(result)`.

### `sources` — page ungated; individual controls gated

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/github-sync` | `org`, `source_url`, `tracked_repositories`, `last_checked_at`, `run_improve` | boot, refresh |
| `GET /api/sources?type=obsidian_vault` | `sources[].{id,name,documents,open_conflicts,last_push_at}`, `summary.{obsidian_vaults,obsidian_documents,open_conflicts}` | boot |
| `POST /api/obsidian/vaults` | response discarded | user submits the vault form (`data-min-role="writer"`) |
| `POST /api/learning-agent/run` | `sources.github.{repos_scanned,changed_count,open_pull_request_count,merged_pull_request_count}`, `organization_digest.{meaningful,preview}`, `notifications.google_chat.{sent,reason,status_category,enabled}`, `ingested` | user clicks "Run learning agent" (`data-min-role="admin"`) |
| `POST /api/learning-agent/google-chat/test` | `notifications.google_chat.*` | user clicks "Send Google Chat test" (admin) |

The button with id `githubSyncButton` posts to **`/api/learning-agent/run`**, not to
`/api/github-sync/run`. Its `force` checkbox and `post_to_chat` checkbox go into that
body along with `include_digest_preview: true`.

### `conflicts` — page ungated, resolve is `writer`

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/conflicts[?status=]` | `conflicts[].{id,kind,status,summary,detected_at,side_a,side_b,resolution_note,resolved_by,resolved_at}`, `open_count` | on view activation, boot, refresh, filter chip |
| `POST /api/conflicts/{id}/resolve` | response discarded | user submits a resolution note |

`side_a`/`side_b` render `{source, excerpt, timestamp}`. `open_count` also drives the
nav badge (now on Review) and the knowledge conflict counter. The status filter is a
server-side query param, not a client filter.

### `access` — `data-min-role="admin"` (Admin sub-tab)

| Endpoint | Fields rendered | Fires |
| --- | --- | --- |
| `GET /api/access` | `principals[].{id,name,kind,role,scopes,seat_slug,default_dataset,team_id}`, `tokens[].{id,name,prefix,role,scopes,principal_id,team_id,last_used_at,default_dataset,revoked_at,expires_at}`, `audit_events[]` | on view activation, boot (admin), refresh |
| `GET /api/access/seats` | `seats[].{name,seat_slug,node_dataset,role,token_count,active_token_count,tokens[].{id,prefix,revoked,last_used_at}}` | inside `renderAccess` |
| `POST /api/access/seats` | `token`, `principal.{seat_slug,default_dataset}` | seat creation form |
| `POST /api/access/seats/{slug}/tokens` | `token` | token form, when a seat is selected |
| `POST /api/access/tokens` | `token` | token form, when no seat is selected |
| `POST /api/access/tokens/{id}/revoke` | response discarded | Revoke button |
| `GET /api/access/seats/{slug}/capture-policy` | `baseline.deny_globs[]` | "Capture policy" button, into a `window.prompt` |
| `PUT /api/access/seats/{slug}/capture-policy` | response discarded | same button, on confirm |

The capture-policy editor is a `window.prompt` with newline-separated globs. That is the
whole editing surface.

### `agents` (Admin) — `data-min-role="admin"`

Same `GET /api/access` payload, filtered client-side to tokens whose principal has
`kind === "service_account"`. No dedicated endpoint.

### `audit` (Admin) — `data-min-role="admin"`

`GET /api/access` → `audit_events[]`, sliced to the last 12 in the Access tab and
filtered client-side by `state.auditFilter` in the Audit tab
(`isMcpAuditEvent` matches `action` containing `mcp`). Event fields rendered:
`action` or `type`, `success`, `detail`/`details`, `created_at`, actor fields via
`eventListItem`. No server-side filtering or pagination exists.

### `settings` (Admin) — `data-min-role="admin"`

Four calls in one `Promise.all`, two of which have `.catch` fallbacks:

| Endpoint | Fields rendered | On failure |
| --- | --- | --- |
| `GET /readyz` | `service`, `tenant_id`, `ok`, `default_dataset`, `auto_improve`, `build_global_context_index` | rejects the whole `Promise.all` |
| `GET /api/learning-agent` | `ok`, `capabilities[]`, `sources.github.{org,tracked_repositories,last_checked_at}` | rejects the whole `Promise.all` |
| `GET /api/backup-mirror` | `repo`, `branch`, `enabled`, `summary.{available_files,tracked_files,total_bytes,missing_files}`, `latest_export.{snapshot_id,exported_at}` | caught, renders "Mirror status unavailable" |
| `GET /api/access/capture-baseline` | `env_exclude_patterns[]`, `default_org_deny_globs[]`, `effective_deny_globs[]` | caught, renders "Could not load capture baseline" |
| `POST /api/self-upgrade` | **none** | inline error text |

Because `/readyz` and `/api/learning-agent` are not individually caught, either one
failing blanks the entire Settings tab including the two panels that did load.

### `locked`

No endpoints. The fallback `setPage` renders when `canUse` fails.

## Endpoint index

34 distinct method + path combinations.

| Endpoint | Views |
| --- | --- |
| `GET /api/session` | boot; gates every view |
| `GET /api/me/summary` | home |
| `GET /api/mesh` | home, overview, knowledge, events, search (side effect), ingest/feedback (side effect) |
| `GET /api/mesh/graph?limit=1000` | the mesh canvas (currently inside overview) |
| `GET /api/documents/{id}` | search preview, graph node inspector |
| `POST /search` | search |
| `POST /ingest` | ingest |
| `POST /feedback` | feedback |
| `GET /api/sources` | home, review |
| `GET /api/sources?type=obsidian_vault` | knowledge, sources |
| `POST /api/obsidian/vaults` | sources |
| `GET /api/github-sync` | home, overview, knowledge, sources |
| `GET /api/conflicts` | conflicts |
| `POST /api/conflicts/{id}/resolve` | conflicts |
| `GET /api/promotion/pending` | home, review, overview |
| `POST /api/promotion/pending/{id}/approve` | review |
| `POST /api/promotion/pending/{id}/reject` | review |
| `GET /api/access` | access, agents, audit, overview |
| `GET /api/access/seats` | access |
| `POST /api/access/seats` | access |
| `GET /api/access/seats/{slug}/capture-policy` | access |
| `PUT /api/access/seats/{slug}/capture-policy` | access |
| `POST /api/access/seats/{slug}/tokens` | access |
| `POST /api/access/tokens` | access |
| `POST /api/access/tokens/{id}/revoke` | access |
| `GET /api/access/capture-baseline` | settings |
| `GET /api/learning-agent` | settings |
| `POST /api/learning-agent/run` | sources |
| `POST /api/learning-agent/google-chat/test` | sources |
| `GET /api/backup-mirror` | settings |
| `GET /readyz` | settings |
| `POST /api/self-upgrade` | settings |
| `POST /admin/logout` | sidebar |
| `GET /events` (SSE) | live updates for every mesh-backed view |

## Gaps

Data the redesigned views in `docs/superpowers/specs/2026-07-29-app-ui-design.md` need
that no current endpoint returns.

1. **"Notes you can read" (Home number 1).** No endpoint returns a readable-corpus
   count. `/api/mesh` `stats.documents` counts document nodes in a mesh that is
   transient across process restarts; `/api/me/summary` `document_count` is Node-only.
   *Would need:* `/api/me/summary` to return a `readable_document_count` computed from
   the caller's resolved search datasets, not from mesh nodes.

2. **"Captured this week" (Home number 2).** Nothing returns a windowed count. It is
   currently derived client-side by filtering `/api/mesh` `events[]` for
   `type === "ingest"` within 7 days, so it under-reports after every redeploy and is
   bounded by however many events the mesh is holding.
   *Would need:* `/api/me/summary` to return `captured_last_7d`, counted from the
   durable AccessStore audit trail it already reads for `last_ingest_at`.

3. **"Waiting on you" (Home number 3).** Derivable but only by calling
   `/api/promotion/pending` and `/api/sources` and adding them up client-side.
   *Would need:* a single count on `/api/me/summary`, or acceptance of the two calls.

4. **Last sync time and the health pill (Home title row).** `last_checked_at` on
   `/api/github-sync` is GitHub-only; there is no vault-wide "last sync". Health is
   inferred from `/api/mesh` `stats.errors`.
   *Would need:* a small `/api/health` or fields on `/api/me/summary`.

5. **Per-hit score (Search).** `score` is whatever the retrieval layer attached and is
   frequently absent, so the UI falls back to printing the rank. Not fixable in the API
   layer; noted so the port does not build a UI that assumes a number.

6. **Per-promotion document count (Review).** A `PromotionPendingItem` is a single
   candidate note. There is no count to show, and the spec asks for one.
   *Would need:* the promotion queue to group candidates, which is a data-model change,
   not an API change.

7. **Per-promotion secret-scan result (Review).** No secret scan runs over a promotion
   candidate. The security scan is a GitHub-sync concept (`last_security_scan`). The
   queue carries `sensitive` from LLM enrichment, which is a different and weaker claim.
   *Would need:* `build_pending_item` to run `kb/security_scan.scan_text_entries` over
   the candidate and store the result on the item.

8. **Per-source failure state (Review, Home).** `/api/sources` has no `last_error`.
   "Failing" is currently inferred from `open_conflicts > 0` and, for GitHub only,
   `metadata.last_security_scan.blocked`. A source that failed for any other reason is
   invisible.
   *Would need:* `/api/sources` to return `last_error` and `last_error_at` per source.

9. **"No seat" as a first-class status (Admin, Seats tab).** `/api/access/seats` returns
   seats; `/api/access` returns tokens. A token with no seat exists only as the absence
   of a `seat_slug`, and joining the two lists is client-side today.
   *Would need:* `/api/access/seats` to return a `seatless_tokens[]` block, or
   `/api/access` tokens to carry an explicit `seat_slug: null` plus a flag.

10. **Audit filtering and pagination (Admin).** `/api/access` returns the whole
    `audit_events` array; the UI slices 12 and filters MCP events client-side. This
    grows unboundedly.
    *Would need:* `GET /api/access/audit?limit=&cursor=&kind=`.

11. **Activity's row-level read isolation (spec item 4).** The spec wants counts and
    timing for every seat with titles only for your own. `/api/mesh` is scoped by
    `scope_mesh_snapshot` and `/api/me/summary` is Node-only; I found no endpoint that
    returns other seats' counts without their titles. This needs a server-side decision
    before the Activity view can be built as specified.

## Dead weight

**Endpoints called whose result is never rendered.**

- `POST /ingest` — response discarded. `accepted: false` is invisible; the textarea
  clears either way. This is the worst of the set and should not be ported as-is.
- `POST /api/self-upgrade` — response discarded.
- `POST /api/obsidian/vaults` — response discarded.
- `POST /api/access/tokens/{id}/revoke` — response discarded, followed by a full
  `loadAccess()` refetch.
- `PUT /api/access/seats/{slug}/capture-policy` — response discarded.
- `POST /api/conflicts/{id}/resolve` — response discarded, followed by a full
  `loadConflicts()`.
- `GET /api/me/summary` — 8 of its 11 fields dropped (see Home).
- `GET /api/promotion/pending` — 10 of the 17 delivered item fields dropped (see Review).

**UI that renders into nothing.**

- **The entire `overview` page.** Since phase 2 it is not in the nav and
  `initialPage()` never returns it, yet `loadPromotionQueue`, `renderSnapshot`,
  `renderOverviewAnalytics`, `renderDashboardIndexes`, `renderDashboardOpenIssue`,
  `renderDashboardMcpAccess` and `loadGithubSync` all still write into it on every mesh
  poll. Three chart renderers run against a page nobody can open.
- **The sidebar `.side-status` section** (`index.html:124`) carries `hidden` in the
  markup and nothing ever removes it. `loadGithubSync` writes `githubSyncStatus`,
  `syncLastChecked`, `syncTrackedRepos` and `githubSourceLink` into it on every load and
  every refresh. Those four writes have never been visible.
- **The `.hidden-stats` block** (`index.html:257`, `display: none` in CSS) receives
  `statSearches`, `statFeedback`, `statUpgrades` and `statErrors` from every
  `renderSnapshot`. Only `stats.errors` is read again, from the snapshot object rather
  than the DOM.

**Server endpoints the dashboard never calls.**

- `POST /api/github-sync/run` exists and is admin-gated, but no dashboard control
  reaches it. The button labelled "Run learning agent" posts to
  `/api/learning-agent/run` instead. Whether the dashboard should be able to force a
  GitHub sync is a product question the port should settle deliberately.

**The one to check before porting.**

The mesh canvas (`graphCanvas`, `mesh`, `meshAlert`, `graphLegend`, `selectedNode`,
`fitButton`, `pauseButton`, `graphDepthInput`, `canvasEmpty`, `realGraphEmpty`,
`graphMeta`) is markup **inside the `overview` section**, not inside `knowledge`. Since
`overview` was unlinked, the production Knowledge Mesh is reachable only by typing
`#overview`, and only by an admin. The port should place it on Explore.
