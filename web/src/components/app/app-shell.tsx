import Head from "next/head";
import type { ReactNode } from "react";

import { Mark } from "@/components/mark";
import { ThemeButton } from "@/components/theme-button";
import { canUse, type Role } from "@/lib/dashboard";

/* The dashboard chrome.
 *
 * Four primary entries, per the design spec: Home, Search, Review, Admin.
 * Overview and Write are gone as pages; Explore and Activity are reachable, not
 * resident, and are not in this slice.
 *
 * Two things about how the nav is built are load-bearing.
 *
 * 1. **Gated entries are rendered client-side, once the session resolves.** The
 *    export is one static document served to every role, so a gated entry
 *    written into the markup would be an entry every role receives. Home and
 *    Search carry no gate and render immediately; Review (writer) and Admin
 *    (admin) appear only after `/api/session` says so. Nobody, at any role,
 *    receives Admin markup in the document. The routes are gated server-side
 *    too, so this is presentation, not enforcement.
 *
 * 2. **Every entry is a plain <a>.** `next/link` would turn these into
 *    client-side navigations, and the Pages Router swaps stylesheets on those
 *    by injecting a <style> element, which `style-src 'self'` drops. The
 *    dashboard is where that import feels most natural and it is exactly as
 *    forbidden here as on the public pages. `test_the_frontend_never_navigates_
 *    client_side` is the guard.
 */
const ENTRIES: Array<{ href: string; label: string; minRole?: Role }> = [
  { href: "/next/app", label: "Home" },
  { href: "/next/app/search", label: "Search" },
  { href: "/next/app/sources", label: "Sources" },
  { href: "/next/app/graph", label: "Graph" },
  { href: "/app#access", label: "Access" },
  { href: "/next/app/review", label: "Review", minRole: "writer" },
  { href: "/next/app/admin", label: "Admin", minRole: "admin" },
];

const LINK =
  "whitespace-nowrap px-[11px] py-1.5 text-[13px] font-medium no-underline text-ink-2 transition-[color,background-color] duration-150 hover:text-ink hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent max-[620px]:px-2 max-[620px]:text-[12.5px]";
const LINK_CURRENT = "text-accent-ink bg-accent-soft hover:text-accent-ink hover:bg-accent-soft";

export function AppShell({
  title,
  current,
  role,
  seat,
  children,
}: {
  title: string;
  current: string;
  role: Role | null;
  /** The signed-in seat, shown so a visitor can tell whose vault they are in. */
  seat?: string | null;
  children: ReactNode;
}) {
  return (
    <>
      <Head>
        <title>{`${title} · Citadel`}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        {/* The dashboard is behind a session; there is nothing here to index. */}
        <meta name="robots" content="noindex" />
      </Head>

      <div className="min-h-screen bg-ground">
        <nav className="topnav sticky top-0 z-20 border-b border-border bg-ground" aria-label="Main">
          {/* A writer/admin session puts four entries plus Sign out in this
              row, which needs ~392px: below 430px the row wraps instead of
              pushing Sign out (and the theme toggle) off-screen. Row one is
              brand + theme + Sign out, row two the nav entries, full width. */}
          <div className="mx-auto flex max-w-[1200px] items-center gap-3.5 px-10 py-[11px] max-[620px]:gap-2 max-[620px]:px-4 max-[620px]:py-[9px] max-[430px]:flex-wrap max-[430px]:gap-y-1">
            <a
              href="/next/app"
              className="mr-auto flex items-center gap-2.5 text-inherit no-underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent"
            >
              <Mark />
              <span className="text-[13px] font-semibold uppercase leading-[1.1] tracking-[.12em] max-[620px]:hidden">
                Citadel
              </span>
            </a>
            <div className="flex items-center gap-0.5 max-[430px]:order-3 max-[430px]:w-full max-[430px]:justify-between max-[430px]:gap-0">
              {ENTRIES.filter((entry) => !entry.minRole || canUse(role, entry.minRole)).map(
                (entry) => (
                  <a
                    key={entry.href}
                    href={entry.href}
                    aria-current={entry.href === current ? "page" : undefined}
                    className={entry.href === current ? `${LINK} ${LINK_CURRENT}` : LINK}
                  >
                    {entry.label}
                  </a>
                )
              )}
            </div>
            {seat ? (
              <span className="ml-1.5 font-mono text-[11.5px] text-ink-3 max-[900px]:hidden">
                {seat}
              </span>
            ) : null}
            <ThemeButton />
            <form method="post" action="/admin/logout">
              <button
                type="submit"
                className="cursor-pointer border border-border bg-surface px-[10px] py-1.5 text-xs font-medium text-ink-2 transition-[color,border-color] duration-150 hover:border-border-2 hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
                Sign out
              </button>
            </form>
          </div>
        </nav>

        <main className="mx-auto max-w-[1200px] px-10 pb-24 pt-9 max-[620px]:px-4 max-[620px]:pt-6">
          {children}
        </main>
      </div>
    </>
  );
}

/* --- shared view furniture ---------------------------------------------- */

export const PANEL = "border border-border bg-surface p-6 max-[620px]:p-4";
export const PANEL_HEAD = "mb-4 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2";
export const PANEL_TITLE = "text-[15px] font-semibold";
export const PANEL_NOTE = "text-[12.5px] text-ink-3";
export const VIEW_H1 = "text-[clamp(24px,3.4vw,32px)] font-light tracking-[-.025em]";
export const MUTED = "text-[14.5px] leading-[1.6] text-ink-2";

/** A failed read is not an empty result, and must never render as one. */
export function LoadError({ what, message }: { what: string; message: string }) {
  return (
    <p className="border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn">
      Could not load {what}. {message}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className={MUTED}>{children}</p>;
}
