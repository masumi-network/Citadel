import type { ReactNode } from "react";

import { SectionIndex, StickyChrome, type Section } from "@/components/section-index";
import { TopNav } from "@/components/top-nav";
import { BAND_IN } from "@/components/ui";

/* The hero: white, no chrome box. TopNav and optional section subnav sit above
 * this band as page-level sticky chrome. The glow is clipped here, on a child
 * that does not wrap the nav: overflow-hidden on an ancestor of a sticky
 * element would pin the bar inside the band instead of the viewport.
 */
export function HeroBand({
  current,
  wide,
  sections,
  nav = true,
  children,
}: {
  current?: string;
  wide?: boolean;
  sections?: Section[];
  /** Set false when TopNav and SectionIndex are rendered by the page. */
  nav?: boolean;
  children: ReactNode;
}) {
  const chrome = nav || Boolean(sections?.length);

  return (
    <>
      {chrome ? (
        <StickyChrome>
          {nav ? <TopNav current={current} embedded /> : null}
          {sections?.length ? <SectionIndex sections={sections} /> : null}
        </StickyChrome>
      ) : null}
      <div className="relative overflow-hidden bg-surface p-0">
        <div className="hero-glow" aria-hidden="true" />
        <div className={BAND_IN}>
          <header
            className={`relative z-[1] pt-10 max-[620px]:pt-8 ${
              wide ? "pb-12 max-[620px]:pb-10" : "pb-14 max-[620px]:pb-10"
            }`}
          >
            {children}
          </header>
        </div>
      </div>
    </>
  );
}
