import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

import type { NextConfig } from "next";

/* Static export, because Citadel is self-hosted: `pip install citadel-archive`
 * has to produce a working web frontend on a machine that has Python and may
 * not have Node. So `next build` pre-renders every route to HTML and the
 * FastAPI service serves the files exactly as it serves the hand-written pages.
 * See docs/adr/0014-nextjs-frontend-static-export.md.
 *
 * The router is the *Pages* Router, deliberately. The App Router emits the RSC
 * payload as executable inline <script>self.__next_f.push(...)</script> blocks,
 * which `script-src 'self'` blocks outright and which a static export cannot
 * nonce (a nonce has to be unique per response; these files are written once at
 * build time). The Pages Router emits the same data as
 * <script id="__NEXT_DATA__" type="application/json">, which is a data block
 * rather than a script the browser ever executes, so CSP does not apply to it
 * at all. That is the whole reason /next can send the site's strict policy
 * unchanged instead of asking for an exemption. See web/README.md.
 */
const nextConfig: NextConfig = {
  output: "export",

  // Served at /next while the hand-written pages keep serving /. Every asset
  // URL Next generates is prefixed with this, so the export drops straight
  // into kb/webui/ and mounts there. kb/server.py pins the same string.
  basePath: "/next",

  // next/image's default loader wants a server to resize on. There is none.
  images: { unoptimized: true },

  reactStrictMode: true,

  // One URL per page, no trailing slash, matching every existing route.
  trailingSlash: false,

  // The repository root, pinned. Next otherwise infers the workspace root from
  // the nearest lockfile and picks the wrong one when a developer happens to
  // have a stray package-lock.json above the repo, and an inferred root changes
  // which files get traced into the build. It has to be the repo root rather
  // than this directory because npm workspaces hoist node_modules up there.
  turbopack: { root: dirname(dirname(fileURLToPath(import.meta.url))) },
};

export default nextConfig;
