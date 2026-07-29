/* Moves the static export into the Python package.
 *
 * `next build` writes to web/out/. That directory is not shipped: a self-hoster
 * installs a wheel and never sees this repo, so the export has to sit inside
 * `kb/` to be packaged. kb/webui/ is that home, and it is deliberately NOT
 * kb/static/ — that directory holds the fonts, the favicon, the committed
 * vendor bundle and the hand-written pages the live site still serves, and a
 * generated tree dropped on top of it would be one `rm -rf` away from taking
 * them with it.
 *
 * Run by `npm run build` in this workspace, right after `next build`.
 */
import { cp, mkdir, readdir, rm, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = dirname(dirname(fileURLToPath(import.meta.url)));
const repo = dirname(web);
const source = join(web, "out");
const target = join(repo, "kb", "webui");

async function exists(path) {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function totalBytes(dir) {
  let bytes = 0;
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    bytes += entry.isDirectory() ? await totalBytes(path) : (await stat(path)).size;
  }
  return bytes;
}

if (!(await exists(join(source, "index.html")))) {
  console.error(`no export at ${source} — did \`next build\` run?`);
  process.exit(1);
}

// Replace rather than merge: a file that stops being emitted must stop being
// served, and a stale chunk left behind is exactly the source/output drift
// ADR-0014 warns about. The guard keeps this from ever pointing at a hand
// written directory by mistake.
if (await exists(target)) {
  if (!(await exists(join(target, "index.html")))) {
    console.error(`${target} exists but does not look like an export — refusing to replace it`);
    process.exit(1);
  }
  await rm(target, { recursive: true });
}

await mkdir(target, { recursive: true });
await cp(source, target, { recursive: true });

const bytes = await totalBytes(target);
console.log(`exported ${(bytes / 1024).toFixed(1)} KB to kb/webui/`);
