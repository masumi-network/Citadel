# Citadel app (`/app`) UI design

- Date: 2026-07-29
- Status: Approved, not implemented
- Relates to: `docs/superpowers/specs/2026-07-29-home-page-design.md` (public site),
  `brand.md` (whose "Web UI palette" section this supersedes)
- Touches: `kb/static/index.html`, `kb/static/styles.css`, `kb/static/app.js`,
  `kb/server.py`, `brand.md`, `tests/`

## Goal

Make the dashboard **simple and easy to understand and use**. That was the brief, and
it is the only success criterion. Today the app has seven nav entries, two of which
nobody can tell apart, several that are a worse version of a CLI command, and a front
page dominated by a graph nobody can read.

It also gains a light theme, defaulting to light, so the app and the public site stop
being two different-looking products.

## What is there now

| Nav id | Label | Gate | Fate |
| --- | --- | --- | --- |
| `home` | Home | all | **Kept**, rebuilt as the only page most people open |
| `overview` | Overview | admin | **Merged.** Its promotion queue moves to Review, its charts die, its graph moves to Explore |
| `search` | Search | all | **Kept**, rebuilt for humans |
| `knowledge` | Knowledge | all | **Renamed** Explore, moved out of the default path |
| `events` | Activity | all | **Demoted** to a secondary page reached from Home |
| `ingest` | Write | writer | **Dropped** as a page. Becomes an "Add note" action |
| `agents` | Admin | admin | **Kept**, reorganised into four tabs |

Primary nav becomes four entries: **Home · Search · Review · Admin**. Explore and
Activity are reachable, not resident. Review is new; it is where promotion approvals
live, which today are buried in an admin-only Overview.

## Decisions

| Decision | Chosen | Rejected |
| --- | --- | --- |
| Front page | Three numbers, a "Needs you" list, a recent feed | Three charts plus a full-viewport graph |
| Graph placement | Its own Explore page, opened deliberately | On the front page |
| Graph rendering | **Production behaviour, unchanged** | Capping at ~60 nodes as the only mode |
| Theme | Light default, dark toggle, public-site tokens | Dark only; keeping the emerald-tinted app palette |
| Search | Grouped Central then Node, as the CLI groups it | A flat ranked list |
| Promotion | Its own view with the buttons on the rows | A card in a right rail |
| Identity | Humans get sessions, machines get tokens | One credential doing both jobs |

### The graph, specifically

The production Knowledge Mesh stays as it is: `force-graph` on canvas, full
connectivity, the Vault Activity / Knowledge Mesh toggle, depth, fit and pause. It was
considered and rejected to replace it with a capped ego-network.

What changes is only *where* it lives and what it gains:

- It moves off Home onto `/app/explore`.
- It gains an optional **focus mode**: arriving from a document or a search hit
  pre-selects that node and offers a depth-limited neighbourhood view. This is an
  addition, not a replacement, and the full mesh remains the default.
- The inspector panel gains source, trust tier, and who promoted it, so a selected node
  can be traced rather than just admired.

Keeping `force-graph` also keeps the CSP intact: it draws to canvas, so it needs no
inline styles. React Flow is **not** used anywhere in the app. It is confined to the
public landing page, where the relaxed `style-src` is scoped to marketing routes only.

## Views

### Home — `/app`

The only page most seats open. In order down the page:

1. **Title row.** "Your Node", last sync time, overall health pill, theme toggle.
2. **Search bar**, the widest element on the page, `⌘K` focusable. Human search was an
   explicit requirement, and today it is a nav item you have to go and find.
3. **Three numbers**, not charts: notes you can read, captured this week, waiting on you.
   The third is the only one that is ever coloured.
4. **Needs you.** Promotions awaiting a decision and sources that are failing, in one
   list, each row carrying its own buttons. Empty state is a real state, not a card.
5. **Recent.** Ten rows, "See all" to Activity.

### Search — `/app/search`

Query box, filter chips (everything / Central only / my Node only, then source types),
then results **grouped Central first, then Node**, matching `_render_search` in
`kb/cli.py` so the two surfaces teach the same mental model. Every hit shows its score,
its trust tier, and its source, and opens a document view with provenance.

### Review — `/app/review`

Promotion queue plus failing sources. The only place in the app with Approve and Reject.
Each row shows origin seat, secret-scan result, and document count.

### Admin — `/app/admin`, admin only

Four tabs in one view: **Seats**, **Tokens**, **Access and audit**, **Sources**.
The seats table treats "no seat" as a first-class status, because a seat-less token
authenticates but cannot search, and that has cost debugging time before.

### Explore — `/app/explore`

The production mesh, as above.

### Activity — `/app/activity`

The Home feed with a who column and filters. **Counts and timing are shown for every
seat; titles only for your own.** That row-level difference is ADR-0009 read isolation
made visible instead of explained, and it is worth a test.

## Theming

Light is the default. Dark is an explicit, remembered choice.

- `kb/static/styles.css` restructures its `:root` into a light token set plus a
  `:root[data-theme="dark"]` override, the same pattern `info.css` already uses.
- The app **shares the `citadel-info-theme` localStorage key** with the public pages, so
  a theme chosen on the landing page survives into the dashboard.
- No `prefers-color-scheme` following. Light unless told otherwise, matching the public
  site's existing deliberate choice.

### Palette

The app moves onto the public token set. `brand.md`'s "Web UI palette" section describes
a dark, faint-emerald base and is superseded by this.

| Role | App today | Light | Dark |
| --- | --- | --- | --- |
| surface | `#131B18` | `#ffffff` | `#171717` |
| ground | `#0B0F0E` | `#fafafa` | `#0a0a0a` |
| accent fill | `#FF51FF` | `#ff51ff` | `#ff51ff` |
| accent text | `#FF51FF` | `#c010a0` | `#ff86f2` |
| success | `#34D399` | `#16a34a` | `#4ade80` |
| warning | `#FBBF24` | `#b45309` | `#f59e0b` |
| danger | `#FA140A` | `#b91c1c` | `#f87171` |
| info | `#22D3EE` | `#0891b2` | `#22d3ee` |

Emerald and cyan do not survive the move to a white ground; they fail contrast. The light
column is what `info.css` already ships, so this is convergence rather than invention.
Square corners and flat elevation are unchanged.

## Identity

**Humans get sessions, machines get tokens.** Today a seat token is both, which is why
`/login` is "paste a token" and why one leak compromises a person and their agents at
once. There is no password anywhere in the codebase; `AccessPrincipal` carries an
optional email and nothing else.

- **Today, shipped.** Admin runs `citadel seat create`, sends the token out of band, the
  user pastes it into `/login` and it becomes a session cookie. The same string goes in
  the shell for CLI and MCP.
- **Next, after the foundation work.** Admin invites an email. The invite is single-use
  and expires. The user sets a password or uses a magic link; a seat is bound to them.
  The web app holds a session and never displays a token again. Machine tokens are minted
  per device from Admin and revoked individually.
- **Later, parked.** Company registration, where each company gets its own separate and
  private knowledge base. Blocked behind the security and privacy decisions, by explicit
  instruction. The isolation model is undecided and it constrains everything else.

Only the first is in scope here. The second and third are recorded so the UI does not
paint itself into a corner: in particular Admin's Tokens tab is designed as
"machine credentials", not "your login".

## Phasing

Deliberately incremental, smallest independently shippable pieces first.

1. **Theme.** Restructure tokens, add the toggle, share the storage key, update
   `brand.md`. Touches no layout, ships alone, and is reversible.
2. **Navigation.** Seven entries to four, Explore and Activity demoted, Write removed as
   a page. Routing and gates only.
3. **Home.** Rebuild the front page: search bar, three numbers, Needs you, Recent.
4. **Search and Review.** The two views that carry daily work.
5. **Admin.** Four tabs.
6. **Explore.** Move the existing mesh, add focus mode and the richer inspector.

Identity work is a separate project and is not scheduled here.

## Testing

Server-side string assertions, consistent with the existing suite.

1. `test_app_nav_has_four_primary_entries` — the shell renders exactly Home, Search,
   Review, Admin, and no longer renders Overview or Write.
2. `test_app_defaults_to_light` — `styles.css` defines light values on `:root` and dark
   under `[data-theme="dark"]`, not the reverse.
3. `test_app_and_site_share_a_theme_key` — both `app.js` and `info.js` reference
   `citadel-info-theme`.
4. `test_activity_titles_are_seat_scoped` — an activity payload for a foreign seat
   carries counts and timestamps but no titles. This is the read-isolation guarantee, so
   it is an API test, not a string check.
5. `test_admin_view_is_role_gated` — a writer token rendering the shell does not receive
   the Admin entry.
6. `test_explore_still_uses_force_graph` — `vendor/force-graph.min.js` is still
   referenced, pinning the decision that the production mesh was kept.

## Out of scope

- Signup, passwords, invites, sessions. Recorded above, not built.
- Company registration and tenancy.
- Any change to the retrieval layer, ranking, or the API contracts the views read.
- The open P0s (#105 event-loop starvation, #50 search latency). They are the foundation
  work this UI sits on, and they are tracked as issues, not here.
