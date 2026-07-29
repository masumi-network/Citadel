# Home page (`/`) design

- Date: 2026-07-29
- Status: Approved, not implemented
- Branch: `feat/partners-page`
- Touches: `kb/static/landing.html`, `kb/static/info.css`, a new `kb/static/landing.js`,
  `kb/server.py`, `kb/static/info.html`, `kb/static/index.html`, `tests/test_server.py`

## Goal

`/` currently reads like documentation: a hero, a TL;DR box, four cards, three
rows, two commands. It explains Citadel correctly and persuades nobody. This
rebuilds it as a landing page for two audiences at once, without the hero going
generic to serve both.

**One page, two doors.** Someone deciding whether to run Citadel, and someone
deciding whether to build with utxo AG, both get a route. The fork happens
mid-page, after the argument, not in the hero.

## Decisions

Each of these was chosen against alternatives that were built and looked at, so
they should be treated as settled rather than re-litigated during
implementation.

| Decision | Chosen | Rejected |
| --- | --- | --- |
| Structure | Proof near the top, doors as a mid-page fork | Doors in the hero; doors only at the bottom |
| Visual | Light, white hero, one Iris wash, square, flat | Dark hero with full-bleed bands; bento grid; editorial rail; split screen |
| Gradient | One drifting radial wash, plus gradient on the rotating word | Gradient borders, rules, buttons, and a giant gradient wordmark |
| Hero content | Type only | Terminal block; product screenshot; live metric tiles |
| Motion | Rotating word plus drifting glow, both ambient | Word roll only; terminal typing itself; sequenced choreography; no motion |
| Numbers | Static repo facts plus the live health pill | Live metric tiles (those stay on `/info`) |

Two consequences were accepted knowingly:

- The install commands appear only at the bottom of the page, so a visitor who
  does not scroll never learns this is a terminal-first tool.
- The right side of the hero is empty by design. The glow carries it. At very
  wide viewports it will read as airy.

## Page structure

Bands, top to bottom. Each band is full-bleed; content sits in a 940px measure.

### 1. Hero — white, glow, no chrome box

The shared `.topnav` sits inside the hero band with a transparent background, so
the page opens on white rather than on a bordered strip. `Sign in` becomes a
bordered button at the end of the nav; the other four links stay plain text.

- Eyebrow: `The organization vault`
- H1: `Citadel remembers your ` + rotating word
- Lede: "Your team already wrote it down, in commits, sessions, docs, and
  issues. Citadel captures it as it happens and gives you, and the agents
  beside you, one place to ask."
- Buttons: `Get started` (primary, `#start`) and `See what teams use it for`
  (`/use-cases`)

The rotating words, in order, cycling: `decisions.` `dead ends.` `reasons.`
`sessions.` `context.` Each is set in the Iris gradient. "Dead ends" is the only
one distinctive to Citadel; the other four are interchangeable nouns and are
worth one more editorial pass before launch, but the list is not blocking.

This replaces the current H1 ("Your team already wrote it down. Citadel
remembers it."), which cannot host a rotating word. That sentence survives as
the opening clause of the lede.

### 2. Section index — sticky

A 46px sticky bar directly under the hero: `What it is`, `How it works`, `Two
ways in`, `Get started`, with the live health pill pushed to the right. The
active entry carries a 2px Iris underline. It is `position: sticky; top: 0`, so
it replaces the hero nav as the page scrolls.

### 3. Proof tiles — white

Four bordered tiles: `Apache-2.0` / open source, self-hosted · `889` / tests, CI
on every push · `25` / MCP tools for agents · `12` / architecture decision
records. First tile takes the accent colour for its number.

Those four are stamped strings and they drift. `889` is the collected count on
this branch and rises with the five tests below; `12` is the file count in
`docs/adr/` (numbered to 0013, one number was reclaimed during a branch
collision). Verify all four at implementation time rather than copying them from
this document, and again at release.

Under them, one dim line: "This page is served by the system it describes. Live
numbers, releases, and the roadmap are on the status page." with `/info` linked.

These are static strings, deliberately. Live tiles belong to `/info`, and
`test_each_public_page_owns_its_subject` pins that.

### 4. `01 · What it is` — grey, bento

A full-width tile holding the Node → Promotion → Central diagram (the existing
`.arch` markup, restyled to three columns with the source chips beneath each
store), then the four pillars two-up: Personal by default, Central is curated,
Agents are first class, Source linked. Copy carries over from the current page
unchanged.

The access-and-data-model deep dive currently at the bottom of this section stays
as a collapsed `<details>`, with its first sentence rewritten to remove the
vendor name (see below).

### 5. `02 · How it works` — white, hairline rows

Three rows separated by hairlines, each a chip in the left column and a heading
plus one sentence on the right: Capture, Search, Promote. No cards. Copy carries
over unchanged.

### 6. `03 · Where to go next` — accent tint, the fork

Two doors side by side. The left one takes an Iris border:

- **Use it** → "Run it on your own work". Buttons: `Get started`, `Use cases`.
- **Work with us** → "Build it into your project". Buttons: `Partnering
  profile` (`/use-cases#fit`), `Contact us` (`/contact`).

### 7. `04 · Get started` — grey

The two commands as bordered mono blocks, then a footer row of three links:
sign in, live status, source.

## Motion

Two animations, both ambient, neither sequenced.

| What | Timing | Loops |
| --- | --- | --- |
| Glow drift | 20s ease-in-out, alternating, translate + scale | yes |
| Word roll | 17s total across five words, `cubic-bezier(.62,0,.28,1)` | yes |

Rules that hold regardless:

1. Nothing loops except these two.
2. Under `prefers-reduced-motion: reduce`, both stop. The word list shows its
   first entry and the glow sits still. `info.css` already carries a global
   reduced-motion block; these must be inside it.
3. No animation may change layout after the page settles. The rotator is inside
   a fixed-height, `overflow: hidden` inline-flex column, so a longer word
   cannot reflow the headline.

## Implementation

### Files

- **`kb/static/landing.html`** — rewritten. Same `<head>` (info.css, favicon,
  info.js), plus `landing.js`.
- **`kb/static/info.css`** — new blocks appended under a `/* --- / landing --- */`
  banner: `.hero-glow`, `.roll`, `.index`, `.tiles`/`.tile`, `.doors`/`.door`,
  and the band backgrounds. The existing `.arch`, `.store`, `.bridge`, `.card`,
  `.nrow`, `.chip`, `.cmd` rules are reused, not duplicated.
- **`kb/static/landing.js`** — new, tiny. Only job: paint the Pixel Bastion mark
  and drive the sticky index's active state from an `IntersectionObserver`. The
  rotator and the glow are pure CSS and need no script.
- **`kb/server.py`** — no route changes. Only the OpenAPI `description` string.

### CSP

`script-src 'self'; style-src 'self'` is unchanged and non-negotiable: no inline
`<style>`, no inline `<script>`, no `style="` attributes. Every animation is a
class in `info.css`; every behaviour is in an external module. This is why the
rotator is CSS keyframes rather than a JS interval.

### Dark mode

Light is the default and stays the default. Under `:root[data-theme="dark"]` the
bands invert to the existing dark ramp, the glow drops to
`rgba(255,81,255,.14)` so it does not bloom on a dark ground, and the rotating
word switches to the dark-mode accent (`--accent-ink` is already redefined
there). No band should be hard-coded to `#fff`.

### Vendor name removal

Not part of the redesign, but blocking on the same release, since the redesign
touches three of the four files. No user-facing surface names the retrieval
dependency:

| File | Line | Now | Fix |
| --- | --- | --- | --- |
| `kb/static/landing.html` | 121 | "a FastAPI service over a cognee-backed retrieval layer" | "a FastAPI service over a retrieval layer" |
| `kb/server.py` | 396 | `description="Self-hosted Organization Vault wrapper around Cognee."` | "Self-hosted Organization Vault." — this string is served publicly at `/openapi.json` and `/docs` |
| `kb/static/info.html` | 174 | "how we stop being 'just a cognee wrapper'" | rewrite the roadmap item without the comparison |
| `kb/static/index.html` | 341, 718 | "Cognee has not produced graph data yet", "QA ID returned by Cognee search" | "The graph has no data yet", "QA ID returned by search" |

Code comments, module names, and imports are untouched. This is about what a
reader sees, not what the source says.

## Testing

Extends `tests/test_server.py`. All assertions are server-side string checks on
the rendered page, consistent with how the other public-page tests work.

1. `test_home_hero_has_no_terminal_block` — the hero carries no `.term` or
   `<pre>` element. Pins the decision that the terminal was removed on purpose,
   so it cannot drift back in.
2. `test_home_rotator_has_a_fixed_word_list` — the `.roll` list contains exactly
   the five words, in order. Catches a half-edited list.
3. `test_home_owns_install_and_the_diagram` — extends the existing
   `test_each_public_page_owns_its_subject`: `class="arch"` and
   `pipx install citadel-archive` on `/` only, `id="m-version"` on `/info` only.
4. `test_no_vendor_name_on_user_facing_surfaces` — every served HTML page plus
   the OpenAPI description are checked, case-insensitively, for the vendor name.
   This is the test that keeps the fix from rotting.
5. `test_home_motion_respects_reduced_motion` — `info.css` contains a
   `prefers-reduced-motion` block that neutralises both `rollup` and `drift`.
   A CSS-content assertion, weak but better than nothing.

## Out of scope

- `/info`, `/use-cases`, `/contact`, `/login` keep their current layouts, with
  one deliberate exception: the `Sign in` nav link becomes a bordered button on
  all five pages, because a nav that differs between pages is worse than a nav
  that changes everywhere at once.
- The em dash sweep of existing prose on `/info` and `/use-cases` stays pending
  a separate decision.
- The `/contact` placeholders (NAME, EMAIL, REGISTERED ADDRESS) are unrelated
  and still open.
- No screenshot of the dashboard. `docs/brand/readme-dashboard.jpg` is not served
  from `kb/static/`, and adding an image to a page that currently ships zero of
  them is a separate decision.
