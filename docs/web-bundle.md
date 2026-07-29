# The one JavaScript bundle

The public landing page at `/` carries an interactive pipeline diagram built on
[React Flow](https://reactflow.dev). It is the only part of Citadel that needs a
JavaScript build, and it is deliberately kept off the deploy path: Railway
installs from `requirements.txt`, CI runs pytest, and neither has Node.

So the built output is committed to the repo and served as a static file.

```
web/flow/index.jsx        source
scripts/build-flow.mjs    esbuild driver
kb/static/vendor/flow.js  committed output, loaded on demand by landing.js
kb/static/vendor/flow.css committed output, React Flow's own stylesheet
```

## Rebuilding

Only needed when `web/flow/` changes.

```sh
npm install
npm run build:flow      # or npm run watch:flow while iterating
```

Then commit `kb/static/vendor/flow.js` and `kb/static/vendor/flow.css` together
with the source change. `node_modules/` is gitignored; `package-lock.json` is
not, so the build is reproducible.

## Two things to know before editing it

- **The node styling is not in the bundle.** It lives in `kb/static/info.css`
  under the `/* --- / flow --- */` banner, so the diagram reads from the page's
  design tokens and follows the light and dark toggle. Only React Flow's own
  stylesheet ships in `flow.css`.
- **`/` has a wider CSP than every other route.** React Flow positions nodes
  with inline `transform` styles, so `kb/server.py` serves `style-src 'self'
  'unsafe-inline'` for the path `/` and the strict `style-src 'self'` everywhere
  else, including `/app` and `/login`. That exception is pinned by
  `test_only_the_landing_page_relaxes_style_src`. Do not widen it, and do not
  add the bundle to another page without deciding about the policy first.
