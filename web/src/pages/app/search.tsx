import { useEffect, useState } from "react";

import { AppShell, MUTED, PANEL, VIEW_H1 } from "@/components/app/app-shell";
import { useSession } from "@/lib/dashboard";

/* Search is not built in this slice. The page exists so the nav has four
 * entries and so Home's search bar submits somewhere real rather than into a
 * 404, and it echoes the query back so the visitor can see it arrived.
 *
 * The query is read from `window.location.search` rather than from
 * `useRouter`, which is banned here: a router import turns navigations into
 * client-side ones, and the Pages Router swaps stylesheets on those by
 * injecting a <style> element that `style-src 'self'` drops.
 */
export default function Search() {
  const session = useSession();
  const [query, setQuery] = useState("");

  useEffect(() => {
    setQuery(new URLSearchParams(window.location.search).get("q") ?? "");
  }, []);

  return (
    <AppShell
      title="Search"
      current="/next/app/search"
      role={session.data?.role ?? null}
      seat={session.data?.seat_slug}
    >
      <h1 className={`${VIEW_H1} mb-6`}>Search</h1>
      <section className={PANEL}>
        <p className={MUTED}>
          Search has not been ported yet. It is the next view, and it will group results Central
          first and then your Node, the way <code className="font-mono text-[.9em]">citadel search</code>{" "}
          already groups them.
        </p>
        {query ? (
          <p className={`mt-3 ${MUTED}`}>
            Your query was <b className="text-ink">{query}</b>. For now, run{" "}
            <code className="bg-surface-2 px-1.5 py-[1.5px] font-mono text-[.9em] text-ink">
              citadel search {query}
            </code>{" "}
            or use the dashboard at <a href="/app">/app</a>.
          </p>
        ) : null}
      </section>
    </AppShell>
  );
}
