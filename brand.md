# Brand — Citadel

Citadel's mark is **Pixel Bastion**: a 7×7 crenellated fortress painted in the
magenta→cyan brand gradient. It ships on the CLI, GitHub README banner, favicon,
and web UI. The bare `citadel` home screen still uses the figlet `CITADEL`
wordmark as the large hero; the compact pixel mark is the in-command header.

Source of truth for the bitmask and column colors: [`kb/banner.py`](kb/banner.py).

## The mark — Pixel Bastion

```
■ · ■ · ■ · ■     columns: Iris #FF51FF → #B00A90
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
- **Opening hero (bare `citadel`):** figlet `CITADEL` in brand colors — Masumi
  magenta `#FA008C` fading to cyan on truecolor terminals (`COLORTERM`), bold
  cyan elsewhere.
- **Assets:** [`docs/brand/pixel-bastion.svg`](docs/brand/pixel-bastion.svg)
  (same file as web), [`docs/brand/readme-banner.svg`](docs/brand/readme-banner.svg),
  [`kb/static/pixel-bastion.svg`](kb/static/pixel-bastion.svg) (login hero),
  [`kb/static/favicon.svg`](kb/static/favicon.svg).

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

The web dashboard follows the **AGENTIC / Masumi design system** (Iris Flower
magenta `#FF51FF`) on a dark, faint-emerald-neutral base, the same accent the
public pages use, so the app, the mark, and the favicon read as one product.
Tokens live in `kb/static/styles.css` `:root`; everything derives from them.
Chrome follows
[`docs/Citadel Archive branding/Citadel Interface.dc.html`](docs/Citadel%20Archive%20branding/Citadel%20Interface.dc.html):
sidebar-first lockup, Pixel Bastion mark, Inter + JetBrains Mono. Corners are
square everywhere: the `--radius*` tokens are all `0`, and only circles
(avatars, status dots) and capsule pills keep a radius. This is a deliberate
local deviation from DESIGN.md's radius xl=14.

| Role | Token | Value | Rationale |
|---|---|---|---|
| Brand accent | `--primary` | `#FF51FF` | AGENTIC DESIGN.md's one accent (Iris Flower) |
| Accent hover/glow | `--primary-strong` | `#FF86F2` | lighter Iris |
| Success / indexed | `--success` | `#34D399` | emerald — day-to-day "healthy/indexed" status |
| Info / search | `--info` | `#22D3EE` | cyan — nod to Citadel's CLI brand |
| Danger | `--danger` | `#FA140A` | Masumi's own red |
| Warning / pending | `--warning` | `#FBBF24` | amber |
| Surfaces | `--bg` … `--surface` | `#0B0F0E` → `#131B18` | dark, faint emerald-neutral tint |
| Text | `--text` | `#F2F5F3` | Interface canvas primary |

Magenta carries brand identity (active nav, primary actions, links); emerald
reads as the "indexed / healthy" status across the timeline. The sidebar holds
the Pixel Bastion lockup + nav + seat footer. Typography: Inter body;
JetBrains Mono for data — timestamps, IDs, subtitles, seat labels.
