import type { ReactNode } from "react";

import { TopNav } from "@/components/top-nav";
import { BAND_IN } from "@/components/ui";

/* The hero: white, no chrome box. TopNav sits above this band as a page-level
 * sticky bar. The glow is clipped here, on a child that does not wrap the nav:
 * overflow-hidden on an ancestor of a sticky element would pin the bar inside
 * the band instead of the viewport.
 *
 * The glow is deliberately not given a view-transition-name. Naming an element
 * hands it to the transition machinery, which snapshots it and freezes its own
 * animation for the duration; on a looping ambient animation that reads as the
 * blob simply stopping.
 */
export function HeroBand({
  current,
  wide,
  children,
}: {
  current?: string;
  /** These pages open on a sentence rather than three words, so the headline
      gets a wider measure and the band a shorter tail. */
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <>
      <TopNav current={current} />
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
