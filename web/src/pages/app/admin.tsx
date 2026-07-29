import { AppShell, MUTED, PANEL, VIEW_H1 } from "@/components/app/app-shell";
import { useSession } from "@/lib/dashboard";

/* Admin is not built in this slice, and this page is only ever reachable by an
 * admin: kb/server.py gates the route, so a reader or writer who types the URL
 * gets the locked page instead of this one. */
export default function Admin() {
  const session = useSession();

  return (
    <AppShell
      title="Admin"
      current="/next/app/admin"
      role={session.data?.role ?? null}
      seat={session.data?.seat_slug}
    >
      <h1 className={`${VIEW_H1} mb-6`}>Admin</h1>
      <section className={PANEL}>
        <p className={MUTED}>
          Admin has not been ported yet. It becomes four tabs: seats, tokens, access and audit, and
          sources. The seats table will treat &quot;no seat&quot; as a first-class status, because a
          seat-less token authenticates but cannot search.
        </p>
        <p className={`mt-3 ${MUTED}`}>
          Until then, the current dashboard at <a href="/app">/app</a> carries all of it.
        </p>
      </section>
    </AppShell>
  );
}
