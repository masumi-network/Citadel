# Brand — Citadel

Citadel's mark is **Pixel Bastion**: a 7×7 crenellated fortress painted in the
Iris magenta column ramp. It ships on the CLI, GitHub README banner, favicon,
and web UI. The bare `citadel` home screen shows Pixel Bastion and nothing else
(cascading on a wide TTY with color, static otherwise), with `CITADEL` and the
tagline set beside the mark. The figlet wordmark is still built, as
`banner_large()`, but no command calls it.

Source of truth for the bitmask and column colors: [`kb/banner.py`](kb/banner.py).

## The mark — Pixel Bastion

```
■ · ■ · ■ · ■     columns: 7-step Iris ramp #FF51FF → #B00A90
■■■■■■■
■■■■■■■           interior cells blink on idle
■■■■■■■
■■·■·■■
■■·■·■■           gate shaft
■■·■·■■
```

One fortress everywhere. The bitmask in `kb/banner.py` is what the CLI, the
favicon, the brand SVGs, the README banner, the dashboard sidebar, and the web
nav all draw; `tests/test_banner.py` pins it so the web copy cannot drift again.
Cells are square and there is no chrome box around the mark.

- **Wordmark:** `CITADEL` (bold).
- **Product chip:** `ARCHIVE` (mono, web / README).
- **Tagline:** `the organization vault` (dim / mono).
- **Figlet hero (`banner_large()`):** ASCII `CITADEL` in Masumi magenta
  `#FA008C` fading to cyan `#22D3EE`, on truecolor terminals (`COLORTERM`) or
  their xterm-256 approximation, bold cyan elsewhere. These two anchors live
  only here, in `_BRAND_MAGENTA` / `_BRAND_CYAN`; nothing else in the product
  still uses them. No command calls this function, so it is a helper kept for
  callers who want the ASCII wordmark, not a screen anyone sees.
- **Assets:** [`docs/brand/pixel-bastion.svg`](docs/brand/pixel-bastion.svg)
  (same file as web), [`docs/brand/readme-banner.svg`](docs/brand/readme-banner.svg),
  [`kb/static/pixel-bastion.svg`](kb/static/pixel-bastion.svg) (standalone copy,
  currently referenced by nothing: `/login` draws the mark as a CSS grid like
  every other page), [`kb/static/favicon.svg`](kb/static/favicon.svg).

Canonical 7×7 flags and column stops live as `PIXEL_FLAGS` / `PIXEL_COLS_HEX`
in `kb/banner.py`. Window cells (the four “eyes”) are lit at rest and
idle-blink on TTY cascade, sidebar grid (`.brand-pixel--window`), and the
external SVG mark/favicon (`@keyframes` inside the SVG — safe under page CSP).

## Terminal palette (ANSI)

| Element | Style |
|---|---|
| Lit pixels | column gradient `#FF51FF` → `#B00A90` (truecolor / 256); cyan fallback |
| Wordmark | bold + cyan |
| Tagline | dim |
| Status OK `✓` | green |
| Status fail `✗` | red |
| Verdict ("Not fully connected") | yellow |

Color is **TTY-aware**: applied only on a real terminal, and suppressed when
output is piped, under `--json`, or when `NO_COLOR` / `TERM=dumb` is set — so
headless/agent output stays clean and parseable. (`banner.supports_color`.)

On TTY + color, `citadel status` / `citadel doctor` play a short pixel cascade
then one window blink; `citadel onboard` reveals the mark line-by-line.

## Voice

Terse, operational, honest. The CLI mirrors the system's guarantees in how it
speaks:

- **Personal-by-default** — capture lands in your private Node unless explicitly promoted.
- **Fail-silent** — hooks never block your `git push` or session close.
- **No surprises** — masked tokens, explicit exit codes, errors on stderr.

Words we use: Node, Central, seat, Approved Capture Roots, Capture Root Tags,
promotion. See [`CONTEXT.md`](CONTEXT.md) for the full domain glossary.

---

## Web UI palette — _set 2026-06-29_ (shell restyle 2026-07-21)

_Accent + shape revised 2026-07-28: one accent, square corners._
_Light-first restyle 2026-07-29: the dashboard converged on the public ramp._

The web dashboard follows the **AGENTIC / Masumi design system** (Iris Flower
magenta `#FF51FF`) on a neutral white / off-white ramp, the same accent and the
same surfaces the public pages use, so the app, the mark, and the favicon read
as one product. Tokens live on `:root` in **both** stylesheets, each with a
light block and a `:root[data-theme="dark"]` override: `kb/static/styles.css`
for the authed dashboard, `kb/static/info.css` for the public pages and
`/login`. Everything derives from them; nothing downstream hard-codes a colour.
Chrome follows
[`docs/Citadel Archive branding/Citadel Interface.dc.html`](docs/Citadel%20Archive%20branding/Citadel%20Interface.dc.html):
sidebar-first lockup, Pixel Bastion mark, Inter + JetBrains Mono. Corners are
square everywhere: the `--radius*` tokens are all `0`, and only circles
(avatars, status dots) and capsule pills keep a radius. This is a deliberate
local deviation from DESIGN.md's radius xl=14.

**Light is the default, in both stylesheets, and `prefers-color-scheme` is
never consulted.** Dark is an explicit choice, made with the theme toggle and
remembered in `localStorage` under `citadel-info-theme`, applied as
`data-theme="dark"` on `:root`. `info.js` and `app.js` read and write that same
one key, so a theme picked on the landing page survives the click into the
dashboard. Defaulting to light rather than to the OS also removes the dark
flash an OS-dark visitor used to get before the deferred script ran.

The two stylesheets name their tokens differently and line their values up:

| Role | Dashboard (`styles.css`) | Public (`info.css`) | Light | Dark |
|---|---|---|---|---|
| Page ground | `--bg` | `--ground` | `#fafafa` | `#0a0a0a` |
| Card surface | `--surface` | `--surface` | `#ffffff` | `#171717` |
| Raised surface | `--surface-raised` | `--surface-2` | `#f5f5f5` | `#1f1f1f` |
| Hairline | `--border` | `--border` | `#e5e5e5` | `rgba(255,255,255,.10)` |
| Text | `--text` | `--ink` | `#0a0a0a` | `#fafafa` |
| Accent fill | `--primary` | `--accent` | `#ff51ff` | `#ff51ff` |
| Accent text | `--primary-strong` | `--accent-ink` | `#c010a0` | `#ff86f2` |
| Accent tint | `--primary-soft` | `--accent-soft` | `rgba(255,81,255,.10)` | `.16` app / `.15` public |
| Success | `--success` | `--good` | `#16a34a` | `#4ade80` |
| Warning | `--warning` | `--warn` | `#b45309` | `#f59e0b` |
| Danger | `--danger` | (none) | `#b91c1c` | `#f87171` |
| Info | `--info` | (none) | `#0891b2` | `#22d3ee` |

The accent inverts by role, not by hue. `#FF51FF` is the fill in both themes,
but accent *text* darkens to `#C010A0` on white, because `#FF51FF` on white
fails contrast, and lightens to `#FF86F2` on black. The old emerald `#34D399`,
amber `#FBBF24`, red `#FA140A` and the dark surfaces `#0B0F0E` / `#131B18` are
gone from the stylesheet entirely; cyan `#22D3EE` survives only as the dark
`--info`. The public stylesheet carries success and warning and no others: the
public pages have no destructive action and nothing to mark as info.

Magenta carries brand identity (active nav, primary actions, links); success
green reads as the "indexed / healthy" status across the timeline. The sidebar
holds the Pixel Bastion lockup + nav + seat footer. Typography: Inter body;
JetBrains Mono for data: timestamps, IDs, subtitles, seat labels.

## Public pages: motion and the pipeline diagram

Three things the public pages add on top of the palette. The styling for all
three is in `kb/static/info.css`; the diagram's swap is driven by
`kb/static/landing.js`. Each degrades to nothing where unsupported.

- **Drifting hero glow.** Every public page opens on a `.hero-glow`: an 840px
  radial magenta circle, `pointer-events: none`, drifting on a 20s alternating
  `@keyframes`, fainter in dark mode. It is anchored differently per page so the
  set does not read as one template stamped five times: base top-right on `/`,
  `--left` on `/info` and `/contact`, `--low` on `/use-cases`, `--center` behind
  the card on `/login`. Its container has to supply `position: relative` and
  `overflow: hidden`, or an 840px circle at `top: -300px` spills and adds a
  horizontal scrollbar. `.band-hero` supplies both on the four band pages;
  `/login` is a single centred card, so there the glow sits inside `.auth`,
  which carries the same two properties.
- **Cross-document view transitions.** `@view-transition { navigation: auto; }`
  means the browser snapshots both documents on a same-origin nav and animates
  between them, instead of tearing one page down and painting the next. Only
  `.topnav` is given a `view-transition-name`, so the bar holds still while the
  rest cross-fades. The glow is deliberately left unnamed: naming an element
  freezes its own animation for the duration, which on a looping ambient
  animation reads as the blob simply stopping.
- **The pipeline diagram, in two forms.** `/` ships a four-step `.spine` in
  plain markup (Capture, Your Node, Promotion, Central). `landing.js` fetches
  React Flow on an IntersectionObserver and swaps in the interactive version in
  place, the first time the diagram comes near the viewport. The bundle is
  ~330 KB on a page that otherwise ships almost nothing, so someone who never
  scrolls that far, or who has JavaScript off, still gets a correct picture and
  never pays for it. The diagram is on `/` only, and `/` is the one route with
  `style-src 'self' 'unsafe-inline'`, because React Flow positions nodes with
  inline transforms.

Both ambient animations and the view transitions stop under
`prefers-reduced-motion: reduce`.
