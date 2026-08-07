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
import {
  countOrDash,
  relativeTime,
  useEndpoint,
  useSession,
  type Source,
} from "@/lib/dashboard";

type SourcesResponse = {
  sources?: Source[];
  summary?: {
    evolve?: { stale?: boolean; last_run_at?: string | null; interval_seconds?: number };
    [key: string]: unknown;
  };
};

type IndexRecord = {
  id?: string;
  name?: string;
  status?: string;
  records?: number | null;
  updated_at?: string | null;
};

type IndexesResponse = {
  indexes?: IndexRecord[];
  stats?: {
    nodes?: number | null;
    edges?: number | null;
    tracked_sources?: number | null;
    last_indexed_at?: string | null;
  };
};

function sourceName(source: Source): string {
  return source.name || source.id || "Unnamed source";
}

function sourceStatus(source: Source): string {
  if (source.status === "error" || source.last_error) return "Error";
  if (source.status === "tracked") return "Tracked";
  if (source.status === "ready") return "Ready";
  return source.status || "Unknown";
}

function checkedAt(value: string | null | undefined): string {
  const relative = relativeTime(value);
  return relative ? `Checked ${relative}` : "Not checked yet";
}

function statusClass(source: Source): string {
  if (source.status === "error" || source.last_error) return "bg-warn-bg text-warn";
  if (source.status === "tracked") return "bg-good-bg text-good";
  return "bg-surface-2 text-ink-2";
}

export default function Sources() {
  const session = useSession();
  const sources = useEndpoint<SourcesResponse>("/api/sources");
  const indexes = useEndpoint<IndexesResponse>("/api/indexes");
  const sourceRows = sources.data?.sources ?? [];
  const indexRows = indexes.data?.indexes ?? [];
  const evolve = sources.data?.summary?.evolve;
  const stats = indexes.data?.stats;

  return (
    <AppShell
      title="Sources"
      current="/next/app/sources"
      role={session.data?.role ?? null}
      seat={session.data?.seat_slug}
    >
      <header className="mb-8 flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className={VIEW_H1}>Sources and index health</h1>
          <p className="mt-2 text-[14.5px] text-ink-2">
            Read-only view of connector state and reported index totals.
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            sources.reload();
            indexes.reload();
          }}
          className="cursor-pointer border border-border-2 bg-surface px-4 py-2 text-[13px] font-medium text-ink transition-[border-color,color] duration-150 hover:border-accent hover:text-accent-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Refresh
        </button>
      </header>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Connected sources</h2>
          <span className={PANEL_NOTE}>
            {sourceRows.length ? `${sourceRows.length} sources reported` : "Connector status"}
          </span>
        </div>
        {sources.error ? <LoadError what="connected sources" message={sources.error} /> : null}
        {sources.loading ? <Empty>Loading source status.</Empty> : null}
        {!sources.loading && !sources.error && !sourceRows.length ? (
          <Empty>No sources are reported by this node.</Empty>
        ) : null}
        {!sources.error && sourceRows.length ? (
          <div>
            {sourceRows.map((source) => (
              <article
                key={source.id ?? source.name ?? source.source_type ?? "source"}
                className="border-t border-border py-4 first:border-t-0 first:pt-0 last:pb-0"
              >
                <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
                  <div className="min-w-0">
                    <h3 className="break-words text-[14.5px] font-medium text-ink">
                      {sourceName(source)}
                    </h3>
                    <p className="mt-1 break-words text-[12.5px] text-ink-3">
                      {source.source_type || "unknown type"} / {checkedAt(source.last_checked_at)}
                    </p>
                  </div>
                  <span className={`shrink-0 px-[11px] py-[5px] text-[11px] font-semibold uppercase tracking-[.04em] ${statusClass(source)}`}>
                    {sourceStatus(source)}
                  </span>
                </div>
                <dl className="mt-3 flex flex-wrap gap-x-7 gap-y-2 text-[12.5px] text-ink-2">
                  <div>
                    <dt className="text-ink-3">Tracked items</dt>
                    <dd className="font-mono text-ink">{countOrDash(source.documents)}</dd>
                  </div>
                  <div>
                    <dt className="text-ink-3">Open conflicts</dt>
                    <dd className="font-mono text-ink">{countOrDash(source.open_conflicts)}</dd>
                  </div>
                </dl>
                {source.last_error ? (
                  <p className="mt-3 break-words border border-warn-bg bg-warn-bg px-3 py-2 text-[12.5px] leading-[1.5] text-warn">
                    {source.last_error}
                    {source.last_error_at ? ` (${relativeTime(source.last_error_at)})` : ""}
                  </p>
                ) : null}
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className={`${PANEL} mb-3`}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Index projection</h2>
          <span className={PANEL_NOTE}>Counts reported by the node health snapshot</span>
        </div>
        {indexes.error ? <LoadError what="index health" message={indexes.error} /> : null}
        {indexes.loading ? <Empty>Loading index health.</Empty> : null}
        {!indexes.loading && !indexes.error && !indexRows.length ? (
          <Empty>No index projections are reported by this node.</Empty>
        ) : null}
        {stats && !indexes.error ? (
          <dl className="mb-5 flex flex-wrap gap-x-7 gap-y-3 border-b border-border pb-5 text-[12.5px] text-ink-2">
            <div>
              <dt className="text-ink-3">Indexed document rows</dt>
              <dd className="font-mono text-ink">{countOrDash(stats.nodes)}</dd>
            </div>
            <div>
              <dt className="text-ink-3">Tracked source items</dt>
              <dd className="font-mono text-ink">{countOrDash(stats.tracked_sources)}</dd>
            </div>
            <div>
              <dt className="text-ink-3">Indexed edges</dt>
              <dd className="font-mono text-ink">{countOrDash(stats.edges)}</dd>
            </div>
            <div>
              <dt className="text-ink-3">Last indexed</dt>
              <dd className="text-ink">{relativeTime(stats.last_indexed_at) || "Not reported"}</dd>
            </div>
          </dl>
        ) : null}
        {!indexes.error && indexRows.length ? (
          <div>
            {indexRows.map((index) => (
              <div
                key={index.id ?? index.name ?? "index"}
                className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-t border-border py-3 first:border-t-0 first:pt-0 last:pb-0"
              >
                <div>
                  <span className="text-[14px] text-ink">{index.name || index.id || "Unnamed index"}</span>
                  <span className={`ml-2 text-[11.5px] uppercase tracking-[.04em] ${index.status === "active" ? "text-good" : "text-ink-3"}`}>
                    {index.status || "unknown"}
                  </span>
                </div>
                <span className="font-mono text-[12.5px] text-ink-2">
                  {countOrDash(index.records)} records
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className={PANEL}>
        <div className={PANEL_HEAD}>
          <h2 className={PANEL_TITLE}>Evolve freshness</h2>
          <span className={PANEL_NOTE}>Maintenance status, separate from source sync</span>
        </div>
        {sources.error ? <LoadError what="evolve status" message={sources.error} /> : null}
        {!sources.error && evolve ? (
          <p className={evolve.stale ? "border border-warn-bg bg-warn-bg px-4 py-3 text-[13.5px] leading-[1.6] text-warn" : MUTED}>
            {evolve.stale ? "Evolve is stale." : "Evolve is current."} {evolve.last_run_at ? `Last run ${relativeTime(evolve.last_run_at)}.` : "No completed run is reported."}
          </p>
        ) : null}
        {!sources.error && !sources.loading && !evolve ? <Empty>No evolve status is reported.</Empty> : null}
      </section>
    </AppShell>
  );
}
