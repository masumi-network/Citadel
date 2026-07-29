# web/ — the Citadel frontend

Next.js, TypeScript and Tailwind, statically exported into `kb/webui/` so that
FastAPI serves it as plain files. The decision and its reasoning are in
[`docs/adr/0014-nextjs-frontend-static-export.md`](../docs/adr/0014-nextjs-frontend-static-export.md).

Today this holds the five public pages, served as previews:

| preview | ports | still served by |
| --- | --- | --- |
| `/next` | the landing page | `kb/static/landing.html` |
| `/next/info` | State of the Vault | `kb/static/info.html` |
| `/next/use-cases` | use cases and partnering | `kb/static/use-cases.html` |
| `/next/contact` | the enquiry form | `kb/static/contact.html` |
| `/next/login` | seat access | `LOGIN_HTML` in `kb/server.py` |

…and the first slice of the dashboard, behind the same session `/app` requires:

| preview | ports | minimum role |
| --- | --- | --- |
| `/next/app` | Home | reader |
| `/next/app/search` | placeholder, not built | reader |
| `/next/app/review` | the promotion queue | writer |
| `/next/app/admin` | placeholder, not built | admin |

Every existing route still serves what it served before; nothing has switched
over. `/login` is the one worth switching first: it is generated from a Python
string literal, which is why it is the page that keeps drifting from the other
four.

## The dashboard's gates

`kb/server.py` gates each dashboard route: no session redirects to `/login` with
a 303, and a role below the view's minimum is served `app/locked.html` with a
403. That is a change of mechanism, not of policy. The dashboard this replaces
ships every page's markup to every seat and hides what the role cannot use, so a
writer really does receive the Admin markup and only client-side code keeps it
off screen. A static export cannot vary its HTML by role at all, so the gate has
to be the route.

The nav follows the same rule from the other side: Home and Search carry no gate
and render immediately, while Review and Admin are added only once
`GET /api/session` reports a role that reaches them. No exported document
contains the Admin link at any role. Two tests pin both halves.

`docs/dashboard-api-contract.md` is the spec for what each view may read. It
records which response fields the old dashboard actually rendered, and a list of
gaps where the design asks for data no endpoint returns. Where a gap applies, the
port renders nothing rather than a substitute: Review shows no secret-scan result
because no scan runs, and Home shows a dash where `readable_document_count` and
`captured_last_7d` are not yet returned.

## Build

```sh
npm install          # from the repo root: this is an npm workspace
npm run build:web    # next build, then copy web/out/ -> kb/webui/
npm run dev:web      # http://localhost:3000/next
```

`kb/webui/` is **committed**. A self-hoster runs `pip install citadel-archive`
on a machine that has Python and may not have Node, so the built files travel in
the wheel as package data (`artifacts` in `pyproject.toml`). The corollary is
that source and output can disagree: rebuild and commit together.

`web/.next/` and `web/out/` are build scratch and are ignored.

## Two things that are not free to change

### 1. The router is the Pages Router, because of the CSP

The site sends `script-src 'self'` with no `'unsafe-inline'` and no nonce, on
every route. A static export cannot use a nonce at all: a nonce has to be unique
per response, and these files are written once, at build time.

The App Router serialises its render payload into the document as executable
inline `<script>self.__next_f.push(...)</script>` blocks. Under this policy the
browser refuses to run them, and the page never hydrates. The only ways out are
`'unsafe-inline'` (not on the table) or a build-time hash allow-list bolted onto
the header (a per-build, per-page coupling between the export and the Python
service).

The Pages Router emits the same data as
`<script id="__NEXT_DATA__" type="application/json">`. A `<script>` whose type is
not a JavaScript type is a *data block*: the HTML parser never executes it, and
CSP never applies to it. Everything else Next emits is an external file under
`/next/_next/`, which `'self'` covers. So the export is CSP-clean as built, and
`/next` sends the site's default policy with no exemption at all.

`tests/test_next_preview.py` reads the committed export and fails if an inline
`<script>`, an inline `<style>` or a `style=""` attribute ever appears in it.
When that test fails, the fix is the markup, never the policy.

What this rules out, concretely: server components, `next/font` (it inlines a
`<style>` block; the fonts are declared with plain `@font-face` in
`src/styles/globals.css` against the woff2 files already served from
`/static/fonts/`), styled-jsx, and `next/script` with an inline body.

**And `next/link`.** This one is not obvious and is the easiest to undo by
accident. Next's Pages Router swaps stylesheets on a client-side route change by
building a `<style>` element and appending the new page's CSS as a text node. A
DOM-created `<style>` is still an inline style as far as CSP is concerned, so
under `style-src 'self'` the browser drops it and the visitor lands on an
unstyled page. Next's own answer is a per-response nonce, which a static export
cannot have.

So every link on this site is a plain `<a>`: a full document load, which fetches
the next page's stylesheet as a `<link>` the policy allows. Cross-document view
transitions are what make that feel continuous rather than abrupt.
`test_the_frontend_never_navigates_client_side` fails if `next/link`,
`useRouter` or `router.push` appears anywhere in `src/`.

React Flow is fine here despite what ADR-0014 predicted, because it is rendered
client-side only (`ssr: false`). React writes element styles through the CSSOM
(`node.style.setProperty`), which CSP does not govern; what CSP blocks is a
`style` *attribute* in parsed markup, and a client-only render never produces
one.

### 2. Square corners are enforced, not remembered

`src/styles/globals.css` sets every step of Tailwind's radius scale to `0`, so
`rounded`, `rounded-md` and `rounded-xl` all mean the same nothing.
`rounded-full` is not a scale step, so status dots and pills keep their curve
and nothing else can pick one up by accident.

## Layout

```
web/
  next.config.ts            static export, basePath /next, pinned workspace root
  public/theme.js           applies the remembered theme before first paint
  scripts/copy-export.mjs   web/out/ -> kb/webui/
  src/pages/                one file per public page, plus _app, _document and 404
  src/pages/app/            the dashboard views
  src/components/ui.tsx     the shared design system: bands, cards, chips, rows, fields
  src/components/           hero-band, top-nav, section-index, commit-chart,
                            pipeline-diagram, pipeline-flow, contact-form, mark, theme-button
  src/components/app/       app-shell: the dashboard nav and its view furniture
  src/lib/vault-state.ts    the one /api/state read, shared by the pill and the tiles
  src/lib/api.ts            the authed fetch helper; a 401 anywhere goes to /login
  src/lib/dashboard.ts      the endpoint shapes each view reads, and nothing more
  src/styles/globals.css    the design tokens, as a Tailwind theme
  flow/index.jsx            NOT part of this app — see below
```

Anything used by more than one page belongs in `ui.tsx`. Five hand-written HTML
files drifting apart is the problem this migration exists to fix, and five page
components with their own copies of the same class strings would rebuild it.

`web/flow/index.jsx` is the esbuild entry point for `kb/static/vendor/flow.js`,
the committed bundle that the hand-written `/` page still loads on scroll. It
predates this app and stays until `/` is retired; `npm run build:flow` from the
repo root still builds it. `src/components/pipeline-flow.tsx` is the same
diagram as a real component, lazily imported, which is the version this app
uses.

## Theme

Light is the default for everyone. Dark is an explicit choice, stored under the
`citadel-info-theme` localStorage key — the same key the hand-written pages use,
so the choice survives crossing between `/next` and `/info` — and applied as
`data-theme="dark"` on `<html>`. `prefers-color-scheme` is deliberately not
consulted.
