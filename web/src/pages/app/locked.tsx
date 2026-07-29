import { AppShell, MUTED, PANEL, VIEW_H1 } from "@/components/app/app-shell";
import { useSession } from "@/lib/dashboard";

/* What a seat sees when it asks for a view its role does not reach.
 *
 * kb/server.py serves this document, with a 403, in place of the view. That is
 * a change from the dashboard it replaces, where every page's markup ships to
 * every role and the client hides what the role cannot use: here the markup for
 * a view you cannot open never reaches the browser at all.
 */
export default function Locked() {
  const session = useSession();
  const role = session.data?.role ?? null;

  return (
    <AppShell title="Not available" current="" role={role} seat={session.data?.seat_slug}>
      <h1 className={`${VIEW_H1} mb-6`}>That view is not open to your seat</h1>
      <section className={PANEL}>
        <p className={MUTED}>
          {role
            ? `Your seat has the ${role} role, which does not reach this view. An admin can raise it.`
            : "Your seat does not reach this view. An admin can raise its role."}
        </p>
        <p className={`mt-3 ${MUTED}`}>
          <a href="/next/app">Back to Home</a>.
        </p>
      </section>
    </AppShell>
  );
}
