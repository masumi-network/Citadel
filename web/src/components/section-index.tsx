import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { MEASURE } from "@/components/ui";
import { useVaultState, versionLabel } from "@/lib/vault-state";

export type Section = { id: string; label: string };

const FALLBACK_VERSION = "v0.5.0";

/* The topmost band currently in view owns the mark. Tracking the set of
   intersecting bands, rather than the last entry the callback handed us, keeps
   the state right when a fast scroll crosses two boundaries in one frame. */
function useActiveSection(sections: Section[]): string | null {
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;

    const bands = sections
      .map((section) => document.getElementById(section.id))
      .filter((band): band is HTMLElement => band !== null);
    if (!bands.length) return;

    const visible = new Set<string>();
    const style = getComputedStyle(document.documentElement);
    const topnav = parseFloat(style.getPropertyValue("--topnav-h")) || 62;
    const sectionnav = parseFloat(style.getPropertyValue("--sectionnav-h")) || 46;
    const chromeH = `${topnav + sectionnav}px`;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        setActive(sections.find((section) => visible.has(section.id))?.id ?? null);
      },
      { rootMargin: `-${chromeH} 0px -55% 0px` }
    );
    bands.forEach((band) => observer.observe(band));
    return () => observer.disconnect();
  }, [sections]);

  return active;
}

function useHealth(): { text: string; down: boolean } {
  const { state } = useVaultState();
  if (!state) return { text: `Live · ${FALLBACK_VERSION}`, down: false };
  const version = versionLabel(state.version) || FALLBACK_VERSION;
  return state.healthy === false
    ? { text: `Degraded · ${version}`, down: true }
    : { text: `Live · ${version}`, down: false };
}

const LINK =
  "inline-flex h-full shrink-0 items-center whitespace-nowrap border-b-2 px-[11px] text-[13px] font-medium no-underline transition-[color,border-color] duration-150 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent max-[620px]:px-2";

/** TopNav plus SectionIndex as one sticky unit. They cannot split on scroll. */
export function StickyChrome({ children }: { children: ReactNode }) {
  return <div className="topnav sticky top-0 z-20 bg-ground">{children}</div>;
}

/** Full-width subnav. Lives inside StickyChrome, directly under TopNav. */
export function SectionIndex({ sections }: { sections: Section[] }) {
  const active = useActiveSection(sections);
  const health = useHealth();

  return (
    <nav className="border-b border-border bg-ground" aria-label="Sections">
      <div
        className={`${MEASURE} flex h-[var(--sectionnav-h)] items-stretch gap-0 overflow-x-auto`}
      >
        {sections.map((section) => (
          <a
            key={section.id}
            href={`#${section.id}`}
            className={`${LINK} ${
              active === section.id
                ? "border-accent text-ink"
                : "border-transparent text-ink-2 hover:text-ink"
            }`}
          >
            {section.label}
          </a>
        ))}
        <span
          className={`ml-auto inline-flex flex-none items-center gap-[7px] self-center rounded-full border border-transparent px-[13px] py-1.5 text-[12.5px] font-medium max-[620px]:px-2.5 max-[620px]:text-[12px] max-[720px]:sticky max-[720px]:right-0 max-[720px]:z-[1] ${
            health.down
              ? "bg-warn-bg text-warn max-[720px]:[background:linear-gradient(var(--warn-bg),var(--warn-bg)),var(--ground)]"
              : "bg-good-bg text-good max-[720px]:[background:linear-gradient(var(--good-bg),var(--good-bg)),var(--ground)]"
          }`}
        >
          <span className={`size-[7px] rounded-full ${health.down ? "bg-warn" : "bg-good"}`} />
          <span>{health.text}</span>
        </span>
      </div>
    </nav>
  );
}

/** Pages with in-page sections. SectionIndex lives in HeroBand under TopNav. */
export function WithSectionRail({ children }: { children: ReactNode }) {
  return children;
}
