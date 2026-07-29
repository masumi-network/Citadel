import type { ReactNode } from "react";

import { TopNav } from "@/components/top-nav";
import { BAND_IN } from "@/components/ui";

/* The hero: white, no chrome box. The nav rides inside the band, transparent,
 * so the page opens on white rather than on a bordered strip, and the glow sits
 * behind both, in the same corner on every page.
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
    <div className="relative overflow-hidden bg-surface p-0">
      <div className="hero-glow" aria-hidden="true" />
      <TopNav current={current} />
      <div className={BAND_IN}>
        <header
          className={`relative z-[1] pt-[58px] max-[620px]:pt-[34px] ${
            wide ? "pb-[66px]" : "pb-[92px] max-[620px]:pb-[60px]"
          }`}
        >
          {children}
        </header>
      </div>
    </div>
  );
}
