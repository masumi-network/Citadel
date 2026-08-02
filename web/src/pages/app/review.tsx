import { useState } from "react";

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
import { api, errorMessage } from "@/lib/api";
import {
  canUse,
  failingSources,
  failureReason,
  useEndpoint,
  useSession,
  type PromotionItem,
  type Source,
} from "@/lib/dashboard";

type PendingResponse = { items?: PromotionItem[]; count?: number };
type SourcesResponse = { sources?: Source[] };

/* Review: the promotion queue, plus the sources that are in trouble.
 *
 * The only place in the app with Approve and Reject. The queue endpoint is
 * reader-gated and already filtered to the caller's own seat server-side; the
 * two decision endpoints require admin plus `sources:sync`, so writers see the
 * rows and a chip saying who has to act, rather than buttons that would 403.
 *
 * Two things the design spec asked for are deliberately not rendered, because
 * the API cannot honestly supply them:
 *
 * - **A secret-scan result per row.** No secret scan runs over a promotion
 *   candidate today (contract map gap 7). The item does carry `sensitive`, from
 *   LLM enrichment, but that is a weaker and different claim and showing it
 *   under a "scanned" label would be a false assurance. An absent assurance is
 *   fine; a false one is not. When `build_pending_item` starts storing a real
 *   scan result, this is where it goes.
 * - **A document count per row.** A pending item is a single candidate note.
 *   There is nothing to count (gap 6), so the column is gone rather than
 *   showing 1 on every row.
 */
export default function Review() {
  const session = useSession();
  const pending = useEndpoint<PendingResponse>("/api/promotion/pending?status=pending");
  const sources = useEndpoint<SourcesResponse>("/api/sources");

  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const role = session.data?.role ?? null;
  const canDecide = canUse(role, "admin");
  const items = pending.data?.items ?? [];
  const failing = failingSources(sources.data?.sources ?? []);

  async function decide(item: PromotionItem, decision: "approve" | "reject") {
    const verb = decision === "approve" ? "Promote" : "Reject";
    if (!window.confirm(`${verb} this candidate from ${item.seat_slug ?? "this seat"}?`)) return;
    setBusy(item.id);
    setFailure(null);
    try {
      await api(`/api/promotion/pending/${encodeURIComponent(item.id)}/${decision}`, {
        method: "POST",
      });
      pending.reload();
    } catch (error) {
      // The dashboard this replaces discards the response entirely, so a
      // rejected decision looked exactly like an accepted one.
      setFailure(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <AppShell
      title="Review"
      current="/next/app/review"
      role={role}
      seat={session.data?.seat_slug}
    >
      <header className="mb-8 flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <h1 className={VIEW_H1}>Review</h1>
        <button
          type="button"
          onClick={() => {
            pending.reload();
            sources.reload();
          }}
          className="cursor-pointer border border-border-2 bg-surface px-4 py-2 text-[13px] font-medium text-ink transition-[border-color,color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Refresh
        </button>
      </header>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Waiting for a decision</h2>
          <span className={PANEL_NOTE}>
            {canDecide
              ? "Promoting copies the note into Central, where the whole org can read it"
              : "An admin approves or rejects these"}
          </span>
        </div>

        {failure ? (
          <p className="mb-3 border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn">
            That decision did not go through. {failure}
          </p>
        ) : null}

        {/* A failed read is not an empty queue. The page this replaces resets
            the list to [] on failure and renders "No promotions are waiting",
            which is indistinguishable from success. */}
        {pending.error ? <LoadError what="the promotion queue" message={pending.error} /> : null}
        {!pending.error && !pending.loading && !items.length ? (
          <Empty>No promotions are waiting for a decision.</Empty>
        ) : null}

        <div>
          {items.map((item) => (
            <article
              key={item.id}
              className="grid grid-cols-[1fr_auto] items-start gap-6 border-t border-border py-5 max-[760px]:grid-cols-1 max-[760px]:gap-3"
            >
              <div className="min-w-0">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="whitespace-nowrap bg-accent-soft px-[11px] py-[5px] font-mono text-[11px] font-medium text-accent-ink">
                    {item.seat_slug ?? "unknown seat"}
                  </span>
                  {item.reference_status ? (
                    <span className="whitespace-nowrap bg-surface-2 px-[11px] py-[5px] text-[11px] font-semibold uppercase tracking-[.04em] text-ink-2">
                      {item.reference_status}
                    </span>
                  ) : null}
                  {(item.repo_hints ?? []).map((hint) => (
                    <span key={hint} className="font-mono text-[11.5px] text-ink-3">
                      {hint}
                    </span>
                  ))}
                </div>
                {/* pre-wrap alone wraps at whitespace only, and previews are
                    exactly where paths, note ids and URLs live: break-words
                    lets those tokens wrap instead of setting the page width. */}
                <p className="m-0 whitespace-pre-wrap break-words text-[14.5px] leading-[1.6] text-ink">
                  {item.preview}
                </p>
                {item.reference_reason ? (
                  <p className={`m-0 mt-2 break-words text-[12.5px] ${MUTED}`}>
                    {item.reference_reason}
                  </p>
                ) : null}
              </div>

              {canDecide ? (
                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    disabled={busy === item.id}
                    onClick={() => decide(item, "approve")}
                    className="cursor-pointer whitespace-nowrap border border-accent bg-accent px-4 py-2 text-[13px] font-medium text-on-accent transition-[filter,opacity] duration-150 hover:brightness-[1.06] disabled:cursor-default disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                  >
                    {busy === item.id ? "Working" : "Promote"}
                  </button>
                  <button
                    type="button"
                    disabled={busy === item.id}
                    onClick={() => decide(item, "reject")}
                    className="cursor-pointer whitespace-nowrap border border-border-2 bg-surface px-4 py-2 text-[13px] font-medium text-ink transition-[border-color,color,opacity] duration-150 hover:border-ink-3 disabled:cursor-default disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                  >
                    Reject
                  </button>
                </div>
              ) : (
                <span className="shrink-0 whitespace-nowrap bg-surface-2 px-[11px] py-[5px] text-[11px] font-semibold uppercase tracking-[.04em] text-ink-2">
                  Waiting on an admin
                </span>
              )}
            </article>
          ))}
        </div>
      </section>

      <section className={PANEL}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Sources in trouble</h2>
          {/* Said plainly, because the inference is all the API supports.
              /api/sources has no last_error (contract map gap 8), so a source
              that failed for any other reason does not appear here at all, and
              an empty list is not proof that everything is fine. */}
          <span className={PANEL_NOTE}>
            Open conflicts, and blocked security scans on the GitHub source
          </span>
        </div>
        {sources.error ? <LoadError what="connected sources" message={sources.error} /> : null}
        {!sources.error && !sources.loading && !failing.length ? (
          <Empty>No source is reporting a conflict or a blocked scan.</Empty>
        ) : null}
        <div>
          {failing.map((source) => (
            <div
              key={source.id ?? source.name ?? "source"}
              className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-t border-border py-4"
            >
              <span className="text-[14.5px] text-ink">
                {source.name || source.id || "Unnamed source"}
              </span>
              <span className="text-[12.5px] text-warn">{failureReason(source)}</span>
            </div>
          ))}
        </div>
      </section>
    </AppShell>
  );
}
