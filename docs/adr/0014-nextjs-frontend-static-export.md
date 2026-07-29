# ADR-0014: the web frontend is Next.js, statically exported, inside this monorepo

- Status: Accepted
- Date: 2026-07-29
- Relates to: `docs/superpowers/specs/2026-07-29-home-page-design.md` and
  `2026-07-29-app-ui-design.md` (whose designs survive this, and whose
  implementation notes do not)

## Context

Everything the browser touches today is hand-written: five static HTML pages, a
399-line stylesheet for the public site, a 2,879-line stylesheet for the app, and
`kb/static/app.js`, 4,247 lines of imperative DOM code. It works, it is fast, it
ships no framework, and it holds a strict CSP. It is also the reason every new
dashboard view costs more than the last one.

Three forces landed at once:

1. **The org already picked a stack.** `masumi-network/sokosumi-landing` is an npm
   workspaces monorepo where `apps/masumi`, `apps/kodosumi`, `apps/serviceplan` and
   `apps/dev` are all Next.js + React + TypeScript + Tailwind, with a shared
   component package. Only `apps/sokosumi` is hand-written HTML, and its own
   `package.json` calls itself a static placeholder. Citadel being the second
   exception would be a choice to diverge, not a default.
2. **React Flow was requested, and the org already ships it.** `apps/dev` depends on
   `@xyflow/react`. Bolting it onto vanilla pages meant a bundler, a committed
   bundle, and a CSP exemption, all to host one component in a page that had no
   framework. That is the tail wagging the dog.
3. **Citadel is self-hosted.** It installs with `pip install citadel-archive` and
   runs on a machine that has Python and may not have Node. Any frontend that needs
   a Node runtime in production breaks that promise, which is load-bearing both for
   the open-source story and for the EU partnering profile.

## Decision

The web frontend becomes a **Next.js + TypeScript + Tailwind** application living in
this repository, at `web/`, under npm workspaces so more packages can join later.
It is built with **static export** into `kb/static/`, and FastAPI serves the output
exactly as it serves hand-written files today.

Consequences of choosing static export specifically:

- **No SSR, no server actions, no route handlers.** Anything dynamic is a client
  fetch against the existing FastAPI endpoints. This is not a limitation in
  practice: the marketing pages are static content and the dashboard is
  authenticated, client-side, and behind a cookie.
- **Same origin is preserved.** The session cookie, the API, and the MCP endpoint
  stay where they are. No CORS, no cookie-domain work, no second deploy target,
  and `requirements.txt` is untouched.
- **Node is a development dependency only.** Contributors and CI need it to build;
  a self-hoster does not.
- **Marketing pages stay indexable**, because static export pre-renders them. That
  was the main thing an SPA would have cost us.

React Flow (`@xyflow/react`) is used for the "how it works" explainer on the public
landing page, matching what `apps/dev` already does. It is not used in the
dashboard, where the Knowledge Mesh keeps `force-graph` on canvas.

## Consequences

- **The design work survives; the implementation does not.** The two specs from
  2026-07-29 were written against hand-written HTML. Their page structure, copy,
  motion rules, palette and information architecture all carry over unchanged.
  Their "Implementation" sections are superseded by this ADR.
- **The hand-written pages ship first and are then replaced.** The rebuilt public
  site and the app's theme and navigation are finished and green as of this date.
  They are being kept and shipped rather than discarded, so the migration starts
  from a working, tested baseline instead of a half-built one.
- **Design tokens move into the Tailwind theme.** `info.css`'s light and dark token
  sets become the Tailwind config. The square-corner deviation and the
  one-accent rule from `brand.md` come with them, and `brand.md` needs updating to
  say where tokens now live.
- **The CSP does not have to be revisited. This ADR was wrong about that.**
  The original text claimed React Flow "positions nodes with inline `transform`
  style attributes, which `style-src 'self'` blocks", and called it the single
  security regression in this decision. That is false. React sets element styles
  through the CSSOM (`node.style.setProperty`), which CSP does not govern; what
  `style-src` blocks is a `style` attribute in **parsed markup**, which a
  client-side render never produces. Confirmed statically against the built
  bundle and every Next chunk: no `setAttribute("style"`, and the lazy chunk's
  CSS arrives as `<link rel="stylesheet">` rather than an injected `<style>`.
  Consequence: the `'unsafe-inline'` exemption currently live on `/` is probably
  unnecessary and should be removed once a browser confirms the diagram still
  renders. `CSP_INLINE_STYLE_PATHS` is correct as a mechanism; it may simply
  have no members.
- **`script-src` was the real obstacle, and it chose the router for us.** The
  App Router serialises its render payload into executable
  `<script>self.__next_f.push(...)</script>` blocks. A static export cannot
  nonce them, since a nonce must be unique per response and these files are
  written once at build time, leaving only `'unsafe-inline'` or a build-time
  hash allow-list. The Pages Router emits the same data as
  `<script type="application/json">`, which the parser treats as a data block
  and never executes, so CSP does not apply. **The app uses the Pages Router and
  the strict policy is unchanged.** Two knock-on constraints: `next/font` is
  unusable because it inlines a `<style>` block, so fonts are declared with
  plain `@font-face`; and Next's built-in 404 must be replaced, since it ships
  one inline `<style>` and six `style=""` attributes.
- **Build output is committed.** `kb/static/` gains generated files, which makes
  diffs noisier and creates a class of bug where the source and the built output
  disagree. A CI check that rebuilds and fails on a dirty tree is the mitigation.
- **`app.js` gets deleted, eventually.** 4,247 lines of imperative DOM is the real
  prize here, and also the real cost: every dashboard view has to be ported
  deliberately, against the existing API contracts, without changing them.
