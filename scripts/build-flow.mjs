/* Bundles the React Flow pipeline diagram on / into kb/static/vendor/.
 *
 * This is the repo's only JavaScript build, and it is deliberately not wired
 * into CI or the deploy. Railway installs from requirements.txt and there is no
 * Node on the server, so the output is committed to the repo and served as a
 * plain static file. Run it by hand when web/flow/ changes:
 *
 *     npm install && npm run build:flow
 *
 * Then commit kb/static/vendor/flow.js and flow.css alongside the source.
 * docs/web-bundle.md is the longer version of this note.
 */
import { build, context } from "esbuild";
import { statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const outfile = join(root, "kb", "static", "vendor", "flow.js");

/** @type {import("esbuild").BuildOptions} */
const options = {
  entryPoints: [join(root, "web", "flow", "index.jsx")],
  outfile,
  bundle: true,
  minify: true,
  format: "iife",
  // landing.js calls CitadelFlow.mount() once the script has loaded.
  globalName: "CitadelFlow",
  target: ["es2019"],
  jsx: "automatic",
  // React ships dev-only warning paths behind this check; without it the
  // bundle carries several tens of KB of development machinery.
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "none",
  logLevel: "info",
};

const watch = process.argv.includes("--watch");

if (watch) {
  const ctx = await context(options);
  await ctx.watch();
  console.log("watching web/flow/ ...");
} else {
  await build(options);
  const js = statSync(outfile).size;
  const css = statSync(outfile.replace(/\.js$/, ".css")).size;
  const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
  console.log(`flow.js  ${kb(js)}\nflow.css ${kb(css)}\ntotal    ${kb(js + css)} (uncompressed)`);
}
