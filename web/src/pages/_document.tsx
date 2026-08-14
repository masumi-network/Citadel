import { Head, Html, Main, NextScript } from "next/document";

/* The document shell.
 *
 * Two things are worth knowing here.
 *
 * 1. `/next/theme.js` is written out in full rather than resolved from
 *    basePath, because <script src> in _document is emitted verbatim. It has to
 *    stay in step with `basePath` in next.config.ts and with the route in
 *    kb/server.py; all three say /next.
 *
 * 2. There is no inline <script> and no inline <style> anywhere in this tree,
 *    and none of Next's own output adds one either. The Pages Router serialises
 *    page data into <script id="__NEXT_DATA__" type="application/json">, which
 *    the HTML parser classifies as a data block and never executes, so the
 *    strict `script-src 'self'` the rest of the site sends applies here
 *    unchanged. Adding an inline handler, a styled-jsx block or a
 *    dangerouslySetInnerHTML script would be the thing that breaks it.
 */
export default function Document() {
  return (
    <Html lang="en">
      <Head>
        <link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />
        {/* Not deferred: the attribute has to be on <html> before first paint,
            or someone who chose dark gets a white flash on every navigation.
            It costs one getItem and one setAttribute. */}
        <script src="/next/theme.js" />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
