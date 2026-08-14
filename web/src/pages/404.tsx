import Head from "next/head";

import { TopNav } from "@/components/top-nav";
import { BAND_IN } from "@/components/ui";

/* Next ships a built-in 404, and that page styles itself with an inline
 * <style> block and half a dozen style="" attributes. Under this site's
 * `style-src 'self'` it would render as unstyled text, and it would put the
 * only inline styles in the whole export into the wheel. Replacing it is
 * cheaper than explaining it.
 */
export default function NotFound() {
  return (
    <>
      <Head>
        <title>Citadel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>
      <TopNav />
      <div className="relative overflow-hidden bg-surface p-0">
        <div className="hero-glow" aria-hidden="true" />
        <div className={BAND_IN}>
          <header className="relative z-[1] pb-14 pt-10 max-[620px]:pb-10 max-[620px]:pt-8">
            <p className="mb-[18px] font-mono text-xs font-semibold uppercase tracking-[.16em] text-accent-ink">
              404
            </p>
            <h1 className="mb-[22px] max-w-[18ch] text-balance text-[clamp(30px,5.4vw,62px)] font-light leading-[1.03] tracking-[-.038em]">
              Nothing at this address
            </h1>
            <p className="mb-[30px] max-w-[58ch] text-[17.5px] leading-[1.6] text-ink-2">
              The page you asked for is not here. It may have moved, or the link may have been
              typed from memory.
            </p>
            <div className="flex flex-wrap gap-2.5">
              <a
                className="inline-flex items-center justify-center border border-accent bg-accent px-5 py-[11px] text-sm font-medium text-on-accent no-underline transition-[filter] duration-150 hover:brightness-[1.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                href="/"
              >
                Back to the front door
              </a>
              <a
                className="inline-flex items-center justify-center border border-border-2 bg-surface px-5 py-[11px] text-sm font-medium text-ink no-underline transition-[border-color,color] duration-150 hover:border-ink-3 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                href="/info"
              >
                Status page
              </a>
            </div>
          </header>
        </div>
      </div>
    </>
  );
}
