import { useEffect, useState } from "react";

import { useVaultState, versionLabel } from "@/lib/vault-state";

export type Section = { id: string; label: string };

const FALLBACK_VERSION = "v0.5.0";

/* The topmost band currently in view owns the underline. Tracking the set of
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
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        setActive(sections.find((section) => visible.has(section.id))?.id ?? null);
      },
      // The sticky bar is 46px tall, and the bottom margin keeps a band from
      // claiming the underline while it is still only a sliver at the fold.
      { rootMargin: "-46px 0px -55% 0px" }
    );
    bands.forEach((band) => observer.observe(band));
    return () => observer.disconnect();
  }, [sections]);

  return active;
}

/* The live pill, from the same public endpoint the /info tiles read. A failed
   fetch leaves the baked-in label rather than showing an error: this is one
   word on a marketing page, and "unknown" is worse than slightly stale. */
function useHealth(): { text: string; down: boolean } {
  const { state } = useVaultState();
  if (!state) return { text: `Live · ${FALLBACK_VERSION}`, down: false };
  const version = versionLabel(state.version) || FALLBACK_VERSION;
  return state.healthy === false
    ? { text: `Degraded · ${version}`, down: true }
    : { text: `Live · ${version}`, down: false };
}

/** The sticky section index. It takes over from the hero nav on scroll. */
export function SectionIndex({ sections }: { sections: Section[] }) {
  const active = useActiveSection(sections);
  const health = useHealth();

  return (
    <nav
      className="sticky top-0 z-20 h-[46px] border-b border-border bg-ground"
      aria-label="Sections"
    >
      <div className="mx-auto flex h-full max-w-[940px] items-center gap-0.5 overflow-x-auto px-[26px] max-[620px]:px-4">
        {sections.map((section) => (
          <a
            key={section.id}
            href={`#${section.id}`}
            className={`inline-flex h-full items-center whitespace-nowrap border-b-2 px-[11px] text-[13px] font-medium no-underline transition-[color,border-color] duration-150 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
              active === section.id
                ? "border-accent text-ink"
                : "border-transparent text-ink-2 hover:text-ink"
            }`}
          >
            {section.label}
          </a>
        ))}
        {/* Sticky below 940px: the pill sits at the far end of a row that
            overflows on phones (and on /use-cases at tablet width), so it was
            entirely off-screen there. When the bar does not overflow the
            sticky constraint is never violated and nothing moves. Its tint is
            translucent, so a --ground layer underneath keeps scrolled links
            from showing through; over the bar's own ground it paints the
            same. */}
        <span
          className={`ml-auto inline-flex flex-none items-center gap-[7px] rounded-full border border-transparent px-[13px] py-1.5 text-[12.5px] font-medium max-[940px]:sticky max-[940px]:right-0 max-[940px]:z-[1] ${
            health.down
              ? "bg-warn-bg text-warn max-[940px]:[background:linear-gradient(var(--warn-bg),var(--warn-bg)),var(--ground)]"
              : "bg-good-bg text-good max-[940px]:[background:linear-gradient(var(--good-bg),var(--good-bg)),var(--ground)]"
          }`}
        >
          <span className={`size-[7px] rounded-full ${health.down ? "bg-warn" : "bg-good"}`} />
          <span>{health.text}</span>
        </span>
      </div>
    </nav>
  );
}
