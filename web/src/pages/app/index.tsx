import { useEffect, useRef } from "react";

import {
  AppShell,
  Empty,
  LoadError,
  MUTED,
  PANEL,
  PANEL_HEAD,
  PANEL_NOTE,
  PANEL_TITLE,
  VIEW_H1,
} from "@/components/app/app-shell";
import { FIELD_INPUT } from "@/components/ui";
import {
  countOrDash,
  failingSources,
  failureReason,
  relativeTime,
  useEndpoint,
  useSession,
  type GithubSync,
  type MeSummary,
  type Mesh,
  type PromotionItem,
  type Source,
} from "@/lib/dashboard";

type PendingResponse = { items?: PromotionItem[]; count?: number };
type SourcesResponse = { sources?: Source[] };

/* Home.
 *
 * The only page most seats open: a search bar, three numbers, what needs you,
 * and what happened. Five fetches, all of which paint independently, so a slow
 * or broken one degrades its own panel instead of the page.
 */
export default function AppHome() {
  const session = useSession();
  const summary = useEndpoint<MeSummary>("/api/me/summary");
  const mesh = useEndpoint<Mesh>("/api/mesh");
  const pending = useEndpoint<PendingResponse>("/api/promotion/pending?status=pending");
  const sources = useEndpoint<SourcesResponse>("/api/sources");
  const github = useEndpoint<GithubSync>("/api/github-sync");

  const search = useRef<HTMLInputElement>(null);

  // The search bar is the widest element on the page and the spec asks for it
  // to be reachable from the keyboard, since human search used to be a nav item
  // you had to go and find.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey)) return;
      event.preventDefault();
      search.current?.focus();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const promotions = pending.data?.items ?? [];
  const failing = failingSources(sources.data?.sources ?? []);
  const needsYouBroke = Boolean(pending.error || sources.error);
  const waitingOnYou = needsYouBroke ? null : promotions.length + failing.length;

  // Activity counters are published only under stats.since_restart (ADR-0019).
  const errors = mesh.data?.stats?.since_restart?.errors ?? 0;
  const syncedAt = relativeTime(github.data?.last_checked_at);

  // The mesh feed is the real one; me/summary's recent_activity is the fallback
  // for a caller whose mesh view is empty, which is what the page it replaces
  // did too.
  const feed = (mesh.data?.events ?? []).length
    ? (mesh.data?.events ?? []).map((event) => ({
        id: event.id,
        type: event.type,
        message: event.message,
        created_at: event.created_at,
        dataset: event.details?.dataset,
      }))
    : (summary.data?.recent_activity ?? []);

  return (
    <AppShell
      title="Home"
      current="/next/app"
      role={session.data?.role ?? null}
      seat={session.data?.seat_slug}
    >
      <header className="mb-8 flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <h1 className={VIEW_H1}>{summary.data?.node_label ?? "Your Node"}</h1>
        <div className="flex flex-wrap items-center gap-2">
          {/* Deliberately not called "last sync". `last_checked_at` is the
              GitHub connector's clock; there is no vault-wide sync time to
              report (contract map gap 4), and a label that implied one would
              be wrong on every other source. */}
          {syncedAt ? (
            <span className="rounded-full border border-border bg-surface px-[13px] py-1.5 text-[12.5px] font-medium text-ink-2">
              GitHub synced {syncedAt}
            </span>
          ) : null}
          {mesh.error ? null : (
            <span
              className={`inline-flex items-center gap-[7px] rounded-full px-[13px] py-1.5 text-[12.5px] font-medium ${
                errors > 0 ? "bg-warn-bg text-warn" : "bg-good-bg text-good"
              }`}
            >
              <span className={`size-[7px] rounded-full ${errors > 0 ? "bg-warn" : "bg-good"}`} />
              {/* Wrapped, so the dot and the label are the only two flex items.
                  The section index's pill does the same. */}
              <span>
                {errors > 0 ? `${errors} indexing ${errors === 1 ? "error" : "errors"}` : "Healthy"}
              </span>
            </span>
          )}
        </div>
      </header>

      {/* A plain GET form, so submitting is a document load. The results page
          reads `q` from the query string. */}
      <form method="get" action="/next/app/search" className="mb-9 flex gap-2.5">
        <input
          ref={search}
          className={`${FIELD_INPUT} font-sans text-[15px]`}
          type="search"
          name="q"
          placeholder="Search your Node and Central"
          aria-label="Search the vault"
          autoComplete="off"
        />
        <button
          type="submit"
          className="shrink-0 cursor-pointer border border-accent bg-accent px-5 text-sm font-medium text-on-accent transition-[filter] duration-150 hover:brightness-[1.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Search
        </button>
      </form>

      <div className="mb-9 grid grid-cols-3 gap-3 max-[760px]:grid-cols-1">
        <Figure
          value={countOrDash(summary.data?.readable_document_count)}
          label="Notes you can read"
          note={
            summary.error
              ? "Unavailable"
              : typeof summary.data?.readable_document_count === "number"
                ? undefined
                : "Not reported by this node yet"
          }
        />
        <Figure
          value={countOrDash(summary.data?.captured_last_7d)}
          label="Captured this week"
          note={
            summary.error
              ? "Unavailable"
              : typeof summary.data?.captured_last_7d === "number"
                ? undefined
                : "Not reported by this node yet"
          }
        />
        {/* The only number that is ever coloured. */}
        <Figure
          value={waitingOnYou === null ? "—" : String(waitingOnYou)}
          label="Waiting on you"
          accent={Boolean(waitingOnYou)}
        />
      </div>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Needs you</h2>
          <span className={PANEL_NOTE}>Promotions awaiting a decision, and sources in trouble</span>
        </div>
        {pending.error ? <LoadError what="the promotion queue" message={pending.error} /> : null}
        {sources.error ? (
          <p className="mt-2">
            <LoadError what="connected sources" message={sources.error} />
          </p>
        ) : null}
        {!needsYouBroke && !promotions.length && !failing.length ? (
          <Empty>Nothing is waiting on you.</Empty>
        ) : null}
        <div>
          {promotions.map((item) => (
            <NeedsRow
              key={item.id}
              kind="Promotion"
              title={item.preview || item.id}
              detail={`from ${item.seat_slug ?? "an unknown seat"}${
                item.reference_reason ? ` · ${item.reference_reason}` : ""
              }`}
              href="/next/app/review"
              action="Review"
            />
          ))}
          {failing.map((source) => (
            <NeedsRow
              key={source.id ?? source.name ?? "source"}
              kind="Source"
              title={source.name || source.id || "Unnamed source"}
              detail={failureReason(source)}
            />
          ))}
        </div>
      </section>

      <section className={PANEL}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Recent</h2>
          <span className={PANEL_NOTE}>The last ten things the vault recorded</span>
        </div>
        {mesh.error ? <LoadError what="recent activity" message={mesh.error} /> : null}
        {!mesh.error && !feed.length ? (
          <Empty>
            {summary.data?.empty
              ? "Nothing has been captured to this seat yet. Run citadel onboard to wire up capture."
              : "No activity recorded yet."}
          </Empty>
        ) : null}
        <ul className="m-0 list-none p-0">
          {feed.slice(0, 10).map((event, index) => (
            <li
              key={event.id ?? index}
              className="grid grid-cols-[110px_1fr_auto] items-baseline gap-4 border-t border-border py-3 text-[13.5px] max-[620px]:grid-cols-1 max-[620px]:gap-1"
            >
              <span className="font-mono text-[11.5px] uppercase tracking-[.06em] text-ink-3">
                {event.type ?? "event"}
              </span>
              {/* min-w-0 + break-words: mesh messages carry unbroken tokens
                  (file paths, text_<md5> ids, URLs) that would otherwise set
                  the column's min-content width and drag the page sideways. */}
              <span className="min-w-0 break-words text-ink-2">{event.message ?? ""}</span>
              <span className="font-mono text-[11.5px] text-ink-3">
                {relativeTime(event.created_at)}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </AppShell>
  );
}

function Figure({
  value,
  label,
  note,
  accent,
}: {
  value: string;
  label: string;
  note?: string;
  accent?: boolean;
}) {
  return (
    <div className="border border-border bg-surface p-6">
      <div
        className={`font-mono text-[34px] font-medium leading-[1.05] tracking-[-.02em] tabular-nums ${
          accent ? "text-accent-ink" : "text-ink"
        }`}
      >
        {value}
      </div>
      <div className="mt-2 text-[13.5px] text-ink-2">{label}</div>
      {note ? <div className="mt-1 text-[11.5px] text-ink-3">{note}</div> : null}
    </div>
  );
}

function NeedsRow({
  kind,
  title,
  detail,
  href,
  action,
}: {
  kind: string;
  title: string;
  detail: string;
  href?: string;
  action?: string;
}) {
  return (
    <div className="grid grid-cols-[92px_1fr_auto] items-start gap-4 border-t border-border py-4 max-[620px]:grid-cols-1 max-[620px]:gap-2">
      <span className="whitespace-nowrap bg-surface-2 px-[11px] py-[5px] text-[11px] font-semibold uppercase tracking-[.04em] text-ink-2">
        {kind}
      </span>
      {/* Promotion previews quote vault content: paths and ids are unbroken
          tokens, so the cell must be shrinkable and the text breakable or the
          longest token sets the page width. */}
      <div className="min-w-0">
        <p className="m-0 break-words text-[14.5px] leading-[1.5] text-ink">{title}</p>
        <p className={`m-0 mt-1 break-words text-[12.5px] ${MUTED}`}>{detail}</p>
      </div>
      {href && action ? (
        <a
          href={href}
          className="whitespace-nowrap border border-border-2 px-4 py-2 text-[13px] font-medium text-ink no-underline transition-[border-color,color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          {action}
        </a>
      ) : null}
    </div>
  );
}
