import { Mark } from "@/components/mark";
import { ThemeButton } from "@/components/theme-button";

const LINKS: Array<{ href: string; label: string }> = [
  { href: "/", label: "Home" },
  { href: "/info", label: "Status" },
  { href: "/use-cases", label: "Use cases" },
  { href: "/contact", label: "Contact" },
];

const LINK =
  "px-[11px] py-1.5 text-[13px] font-medium no-underline text-ink-2 transition-[color,background-color] duration-150 hover:text-ink hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent max-[620px]:px-2 max-[620px]:text-[12.5px]";

const LINK_CURRENT = "text-accent-ink bg-accent-soft hover:text-accent-ink hover:bg-accent-soft";

/* Sign in is the one destination in the nav that is a door rather than a page,
   so it carries a border. */
const SIGN_IN =
  "ml-1.5 px-3 py-[5px] text-[13px] font-medium no-underline text-ink border border-border-2 transition-[color,border-color] duration-150 hover:text-accent-ink hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent max-[620px]:px-2 max-[620px]:text-[12.5px]";

/* The shared top navigation.
 *
 * Every link points at a hand-written page, so these are plain <a> and not
 * next/link: they are full document loads by definition, and a client-side
 * router would only be able to prefetch a route it does not own. The one
 * exception is Home, which is where the visitor already is.
 *
 * `topnav` is the class the cross-document view transition keys off, so the bar
 * holds still while the rest of the page cross-fades.
 */
export function TopNav({ current }: { current?: string }) {
  return (
    <nav
      className="topnav relative z-20 border-b-0 bg-transparent"
      aria-label="Main"
    >
      <div className="mx-auto flex max-w-[940px] items-center gap-3.5 px-[26px] py-[11px] max-[620px]:gap-2 max-[620px]:px-4 max-[620px]:py-[9px]">
        <a
          href="/"
          className="mr-auto flex items-center gap-2.5 text-inherit no-underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent"
        >
          <Mark />
          <span className="text-[13px] font-semibold uppercase leading-[1.1] tracking-[.12em] max-[620px]:hidden">
            Citadel
          </span>
        </a>
        <div className="flex items-center gap-0.5">
          {LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              aria-current={link.href === current ? "page" : undefined}
              className={link.href === current ? `${LINK} ${LINK_CURRENT}` : LINK}
            >
              {link.label}
            </a>
          ))}
          <a href="/login" className={SIGN_IN}>
            Sign in
          </a>
        </div>
        <ThemeButton />
      </div>
    </nav>
  );
}
